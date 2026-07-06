"""Manifest module: the spine of wpfreeze's acquisition pipeline.

One record per canonical resource, persisted atomically to manifest.json
after every batch of updates so an interrupted run never corrupts state.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

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
FLAG_DB_UNRESOLVED = "db_unresolved"
FLAG_RETRY_EXHAUSTED = "retry_exhausted"

# Flags that indicate the archive is genuinely incomplete (drive the CLI's
# exit code), as opposed to flags on successfully-captured content that
# merely wants editorial attention.
GAP_FLAGS = frozenset(
    {
        FLAG_AMBIGUOUS_CANONICAL,
        FLAG_AUTH_GATED,
        FLAG_ODD_RESPONSE,
        FLAG_DB_UNRESOLVED,
    }
)

GAP_STATUSES = frozenset({Status.MISSING.value, Status.EXTERNAL_UNFETCHABLE.value})


def utc_now() -> str:
    """Current UTC timestamp, ISO 8601 with offset."""
    return datetime.now(timezone.utc).isoformat()


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
    """In-memory manifest keyed by canonical URL, with atomic persistence."""

    SCHEMA_VERSION = 1

    def __init__(self) -> None:
        self._records: dict[str, ManifestRecord] = {}

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
        """
        record = self._records.get(url)
        if record is None:
            record = ManifestRecord(url=url)
            self._records[url] = record
            logger.debug("new pending record for %s", url)
        if discovered_via is not None:
            record.add_discovered_via(discovered_via)
        return record

    def save(self, path: Path) -> None:
        """Write manifest.json atomically: temp file in the same directory,
        then os.replace, so interruption mid-write never corrupts the
        existing file."""
        path = Path(path)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "generated": utc_now(),
            "records": [r.to_dict() for r in self._records.values()],
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
        return manifest
