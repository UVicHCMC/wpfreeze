"""Manifest module: the spine of wpfreeze's acquisition pipeline.

One record per canonical resource, persisted atomically to manifest.json
after every batch of updates so an interrupted run never corrupts state.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from wpfreeze.urlnorm import SiteProfile

logger = logging.getLogger(__name__)


class Status(str, Enum):
    PENDING = "pending"
    FETCHED = "fetched"
    FETCHED_WAYBACK = "fetched_wayback"
    RETRYING = "retrying"
    MISSING = "missing"
    EXCLUDED = "excluded"
    EXTERNAL_UNFETCHABLE = "external_unfetchable"


class Source(str, Enum):
    LIVE = "live"
    WAYBACK = "wayback"
    NONE = "none"


# Flags recorded during Stage 5 analysis (see CLAUDE-acquire.md). This is
# not an enforced enum -- flags are free-form strings -- just named
# constants so callers don't retype the literal.
FLAG_ORPHAN = "orphan"
FLAG_UNLISTED = "unlisted"
FLAG_CONTAINS_FORM = "contains_form"
FLAG_PLUGIN_MARKUP = "plugin_markup"
FLAG_ATTACHMENT_PAGE = "attachment_page"
FLAG_HASH_DUPLICATE = "hash_duplicate"
FLAG_AMBIGUOUS_CANONICAL = "ambiguous_canonical"
FLAG_AUTH_GATED = "auth_gated"
FLAG_ODD_RESPONSE = "odd_response"
FLAG_XML_UNRESOLVED = "xml_unresolved"
FLAG_RETRY_EXHAUSTED = "retry_exhausted"

# Flags that indicate the archive is genuinely incomplete (drive the CLI's
# exit code), as opposed to flags on successfully-captured content that
# merely wants editorial attention.
GAP_FLAGS = frozenset(
    {
        FLAG_AMBIGUOUS_CANONICAL,
        FLAG_AUTH_GATED,
        FLAG_ODD_RESPONSE,
        FLAG_XML_UNRESOLVED,
    }
)

GAP_STATUSES = frozenset({Status.MISSING.value, Status.EXTERNAL_UNFETCHABLE.value})


def utc_now() -> str:
    """Current UTC timestamp, ISO 8601 with offset."""
    return datetime.now(timezone.utc).isoformat()


def _site_profile_to_dict(profile: SiteProfile) -> dict[str, Any]:
    """SiteProfile as JSON-safe dict -- frozensets become sorted lists so
    the same profile always serializes to the same bytes."""
    return {
        "canonical_host": profile.canonical_host,
        "site_hosts": sorted(profile.site_hosts),
        "use_https": profile.use_https,
        "trailing_slash": profile.trailing_slash,
        "base_path": profile.base_path,
        "extra_hosts": sorted(profile.extra_hosts),
    }


def _site_profile_from_dict(data: dict[str, Any]) -> SiteProfile:
    return SiteProfile(
        canonical_host=data["canonical_host"],
        site_hosts=frozenset(data.get("site_hosts", ())),
        use_https=data.get("use_https", True),
        trailing_slash=data.get("trailing_slash", True),
        base_path=data.get("base_path", "/"),
        extra_hosts=frozenset(data.get("extra_hosts", ())),
    )


@dataclass
class ManifestRecord:
    url: str
    aliases: list[str] = field(default_factory=list)
    status: str = Status.PENDING.value
    http_status: int | None = None
    source: str = Source.NONE.value
    wayback_url: str | None = None
    content_hash: str | None = None
    content_type: str | None = None
    local_path: str | None = None
    output_path: str | None = None
    discovered_via: list[str] = field(default_factory=list)
    redirect_from: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    first_seen: str = field(default_factory=utc_now)
    last_fetched: str | None = None
    fetch_attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManifestRecord:
        return cls(**data)

    def add_alias(self, alias: str) -> None:
        if alias != self.url and alias not in self.aliases:
            self.aliases.append(alias)

    def add_discovered_via(self, provenance: str) -> None:
        if provenance not in self.discovered_via:
            self.discovered_via.append(provenance)

    def add_redirect_from(self, origin: str) -> None:
        if origin not in self.redirect_from:
            self.redirect_from.append(origin)

    def add_flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)

    def is_gap(self) -> bool:
        """True if this record represents an archive gap (drives exit code 1)."""
        return self.status in GAP_STATUSES or any(f in GAP_FLAGS for f in self.flags)


class Manifest:
    """In-memory manifest keyed by canonical URL, with atomic persistence.

    `lock` is a re-entrant lock guarding *compound* read-then-mutate
    sequences (e.g. resolve_redirect followed by mutating the merge
    target, or store_bytes's collision repair) during concurrent
    crawling -- not held by individual dict/list accessors below.
    Single-threaded callers may ignore it entirely.
    """

    SCHEMA_VERSION = 2

    def __init__(self) -> None:
        self._records: dict[str, ManifestRecord] = {}
        self._redirect_aliases: dict[str, str] = {}
        self.site_profile: SiteProfile | None = None
        self.lock = threading.RLock()

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, url: str) -> bool:
        return url in self._records

    def __iter__(self):
        return iter(self._records.values())

    def get(self, url: str) -> ManifestRecord | None:
        return self._records.get(url)

    def all(self) -> list[ManifestRecord]:
        return list(self._records.values())

    def by_status(self, status: str) -> list[ManifestRecord]:
        return [r for r in self._records.values() if r.status == status]

    def has_gaps(self) -> bool:
        return any(r.is_gap() for r in self._records.values())

    def upsert(self, record: ManifestRecord) -> None:
        self._records[record.url] = record

    def get_or_create(self, url: str, discovered_via: str | None = None) -> ManifestRecord:
        """Fetch the record for `url`, creating a pending one if absent.

        Safe to call repeatedly during discovery/crawl: existing records are
        never reset to pending, only annotated with additional provenance.

        If `url` is already known to redirect elsewhere (recorded by an
        earlier resolve_redirect call), this resolves straight to that
        target instead of resurrecting `url` as fresh pending work. Without
        this, a URL that redirects to a page whose own content links back
        to it (e.g. a stray comment quoting the pre-redirect URL of the
        very post it's on) creates a self-sustaining loop: fetch, redirect,
        fold away, get rediscovered via the target's own content, fetch
        again, forever. This surfaced on a real crawl.
        """
        target_url = self._redirect_aliases.get(url)
        if target_url is not None:
            seen = {url}
            while target_url in self._redirect_aliases and target_url not in seen:
                seen.add(target_url)
                target_url = self._redirect_aliases[target_url]
            target = self.get_or_create(target_url, discovered_via)
            target.add_redirect_from(url)
            target.add_alias(url)
            return target

        record = self._records.get(url)
        if record is None:
            record = ManifestRecord(url=url)
            self._records[url] = record
            logger.debug("new pending record for %s", url)
        if discovered_via is not None:
            record.add_discovered_via(discovered_via)
        return record

    def resolve_redirect(self, from_url: str, to_url: str) -> ManifestRecord:
        """Fold `from_url`'s record into `to_url`'s (creating the target
        if needed): the target gains `from_url` as both a redirect origin
        and an alias, and `from_url` stops existing as an independent
        (pending) manifest entry. `from_url` is remembered as a known
        redirect source (see get_or_create) so it's never resurrected as
        pending again if rediscovered later."""
        target = self.get_or_create(to_url)
        if from_url == to_url:
            return target
        target.add_redirect_from(from_url)
        target.add_alias(from_url)
        source = self._records.pop(from_url, None)
        self._redirect_aliases[from_url] = to_url
        if source is not None:
            for provenance in source.discovered_via:
                target.add_discovered_via(provenance)
        return target

    def save(self, path: Path) -> None:
        """Write manifest.json atomically: temp file in the same directory,
        then os.replace, so interruption mid-write never corrupts the
        existing file.

        Only the snapshot of `_records` is taken under `self.lock` --
        re-serializing every record to JSON and writing it out is real
        work on a large manifest, and holding the lock across it would
        serialize every concurrent worker's bookkeeping behind the
        slowest save. The snapshot is a plain list assembled while the
        lock is held, so it's safe against a concurrent insert; the
        actual dump/replace happens after releasing the lock.
        """
        path = Path(path)
        with self.lock:
            records_snapshot = [r.to_dict() for r in self._records.values()]
            redirect_aliases_snapshot = dict(self._redirect_aliases)
            site_profile_snapshot = self.site_profile
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "generated": utc_now(),
            "records": records_snapshot,
            "redirect_aliases": redirect_aliases_snapshot,
            "site_profile": (
                _site_profile_to_dict(site_profile_snapshot)
                if site_profile_snapshot is not None
                else None
            ),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.write("\n")
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        logger.debug("saved %d records to %s", len(self._records), path)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        path = Path(path)
        manifest = cls()
        if not path.exists():
            return manifest
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        for raw in payload.get("records", []):
            record = ManifestRecord.from_dict(raw)
            manifest._records[record.url] = record
        manifest._redirect_aliases = dict(payload.get("redirect_aliases", {}))
        profile_data = payload.get("site_profile")
        manifest.site_profile = (
            _site_profile_from_dict(profile_data) if profile_data is not None else None
        )
        return manifest
