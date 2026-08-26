from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import requests
import yaml

from wpfreeze.cli import (
    ConfigError,
    FreezeSettings,
    SiteConfig,
    load_config,
    main,
    probe_site,
    run_acquire,
    run_freeze,
    run_report,
    run_rescan,
    run_status,
)
from wpfreeze.fetch import RateLimiter
from wpfreeze.manifest import Manifest, Status

from fixture_site import FixtureSite


def _write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data))
    return path


# ---------------------------------------------------------------------------
# Config loading and validation
# ---------------------------------------------------------------------------


def test_load_config_xml_backup_absent_proceeds(tmp_path: Path):
    path = _write_yaml(tmp_path / "site.yaml", {"base_url": "https://example.com/", "output_dir": "out"})
    config = load_config(path)
    assert config.xml_backup is None


def test_load_config_xml_backup_path_resolves(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {"base_url": "https://example.com/", "output_dir": "out", "xml_backup": "some/export.xml"},
    )
    config = load_config(path)
    assert config.xml_backup == Path("some/export.xml")


def test_load_config_defaults():
    pass  # covered implicitly below; kept as a placeholder for clarity


def test_load_config_rejects_concurrency_below_one(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {"base_url": "https://example.com/", "output_dir": "out", "concurrency": 0},
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_concurrency_plumbs_through(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {"base_url": "https://example.com/", "output_dir": "out", "concurrency": 5},
    )
    config = load_config(path)
    assert config.concurrency == 5


def test_load_config_applies_defaults(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {"base_url": "https://example.com", "output_dir": "out"},
    )
    config = load_config(path)
    assert config.base_url == "https://example.com/"
    assert config.rate_limit == 1.0
    assert config.wayback_rate_limit == 3.0
    assert config.concurrency == 2
    assert config.exclusions == []
    assert config.wayback.enabled is True
    assert config.upload.remote is None


def test_load_config_upload_remote(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {
            "base_url": "https://example.com/",
            "output_dir": "out",
            "upload": {"remote": "user@example.com:/var/www/html"},
        },
    )
    config = load_config(path)
    assert config.upload.remote == "user@example.com:/var/www/html"


def test_load_config_wayback_settings(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {
            "base_url": "https://example.com/",
            "output_dir": "out",
            "wayback": {"enabled": False, "prefer_snapshots_near": "2022-06-15"},
        },
    )
    config = load_config(path)
    assert config.wayback.enabled is False
    from datetime import date

    assert config.wayback.prefer_snapshots_near == date(2022, 6, 15)


def test_load_config_search_defaults(tmp_path: Path):
    path = _write_yaml(tmp_path / "site.yaml", {"base_url": "https://example.com/", "output_dir": "out"})
    config = load_config(path)
    assert config.search.enabled is False
    assert config.search.body_selectors == ("body.wp-singular .entry-content",)
    assert config.search.ignore_selectors == ()
    assert config.search.force_language is None
    assert config.search.exclude_pages == ()
    assert config.search.acknowledged_thin_pages == ()


def test_load_config_search_body_selectors_explicit_empty_list_opts_out(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {
            "base_url": "https://example.com/",
            "output_dir": "out",
            "policy": {"strip_search_forms": False},
            "search": {"enabled": True, "body_selectors": []},
        },
    )
    config = load_config(path)
    assert config.search.body_selectors == ()


def test_load_config_search_settings_plumb_through(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {
            "base_url": "https://example.com/",
            "output_dir": "out",
            "policy": {"strip_search_forms": False},
            "search": {
                "enabled": True,
                "body_selectors": [".entry-content"],
                "ignore_selectors": [".related-posts"],
                "force_language": "en",
                "exclude_pages": ["/blog.html", "/projects.html"],
                "acknowledged_thin_pages": ["/portfolio-item/a.html"],
            },
        },
    )
    config = load_config(path)
    assert config.search.enabled is True
    assert config.search.body_selectors == (".entry-content",)
    assert config.search.ignore_selectors == (".related-posts",)
    assert config.search.force_language == "en"
    assert config.search.exclude_pages == ("/blog.html", "/projects.html")
    assert config.search.acknowledged_thin_pages == ("/portfolio-item/a.html",)


def test_load_config_picks_up_explicit_name(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {"name": "landscapes", "base_url": "https://example.com/", "output_dir": "out"},
    )
    config = load_config(path)
    assert config.name == "landscapes"


def test_load_config_name_falls_back_to_file_stem(tmp_path: Path):
    path = _write_yaml(tmp_path / "my-site.yaml", {"base_url": "https://example.com/", "output_dir": "out"})
    config = load_config(path)
    assert config.name == "my-site"


def test_load_config_rejects_name_with_path_traversal(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {"name": "../evil", "base_url": "https://example.com/", "output_dir": "out"},
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_rejects_explicit_empty_name(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {"name": "", "base_url": "https://example.com/", "output_dir": "out"},
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_freeze_steps_default(tmp_path: Path):
    path = _write_yaml(tmp_path / "site.yaml", {"base_url": "https://example.com/", "output_dir": "out"})
    config = load_config(path)
    assert config.freeze.steps == ("acquire", "build", "validate")


def test_load_config_freeze_steps_custom_order_honoured(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {
            "base_url": "https://example.com/",
            "output_dir": "out",
            "freeze": {"steps": ["build", "acquire", "diagnose"]},
        },
    )
    config = load_config(path)
    assert config.freeze.steps == ("build", "acquire", "diagnose")


def test_load_config_freeze_steps_rejects_unknown_step(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {"base_url": "https://example.com/", "output_dir": "out", "freeze": {"steps": ["acquire", "bogus"]}},
    )
    with pytest.raises(ConfigError, match="bogus"):
        load_config(path)


def test_load_config_freeze_steps_rejects_duplicate(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {"base_url": "https://example.com/", "output_dir": "out", "freeze": {"steps": ["acquire", "acquire"]}},
    )
    with pytest.raises(ConfigError, match="acquire"):
        load_config(path)


def test_search_enabled_with_no_policy_key_defaults_to_keeping_the_form(tmp_path: Path):
    # search.enabled implies "keep this site's search form" when the config
    # never addressed strip_search_forms at all -- the whole point of
    # turning search on. No ConfigError, and the form survives the build.
    path = _write_yaml(
        tmp_path / "site.yaml",
        {"base_url": "https://example.com/", "output_dir": "out", "search": {"enabled": True}},
    )
    config = load_config(path)  # must not raise
    assert config.search.enabled is True
    assert config.policy.strip_search_forms is False


def test_search_enabled_with_explicit_strip_search_forms_true_raises(tmp_path: Path):
    # An *explicit* strip_search_forms: true next to search.enabled: true is
    # a real contradiction -- the config says both "keep the form" and
    # "strip the form" -- and must still be rejected, not silently
    # overridden by the absent-key default above.
    path = _write_yaml(
        tmp_path / "site.yaml",
        {
            "base_url": "https://example.com/",
            "output_dir": "out",
            "policy": {"strip_search_forms": True},
            "search": {"enabled": True},
        },
    )
    with pytest.raises(ConfigError, match="strip_search_forms"):
        load_config(path)


def test_search_enabled_with_explicit_strip_forms_true_and_no_strip_search_forms_key_still_defaults(
    tmp_path: Path,
):
    # strip_forms: true is just restating that dataclass field's own
    # default -- it says nothing about strip_search_forms specifically, so
    # it must not block the absent-key default from kicking in.
    path = _write_yaml(
        tmp_path / "site.yaml",
        {
            "base_url": "https://example.com/",
            "output_dir": "out",
            "policy": {"strip_forms": True},
            "search": {"enabled": True},
        },
    )
    config = load_config(path)  # must not raise
    assert config.policy.strip_forms is True
    assert config.policy.strip_search_forms is False


def test_search_enabled_with_strip_search_forms_false_loads(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {
            "base_url": "https://example.com/",
            "output_dir": "out",
            "policy": {"strip_search_forms": False},
            "search": {"enabled": True},
        },
    )
    config = load_config(path)
    assert config.search.enabled is True


def test_search_enabled_with_strip_forms_false_also_loads(tmp_path: Path):
    # strip_forms: false leaves search forms alive too, independently of
    # strip_search_forms -- the case the first draft of this validation
    # would have wrongly rejected.
    path = _write_yaml(
        tmp_path / "site.yaml",
        {
            "base_url": "https://example.com/",
            "output_dir": "out",
            "policy": {"strip_forms": False},
            "search": {"enabled": True},
        },
    )
    config = load_config(path)
    assert config.search.enabled is True


def test_search_disabled_never_raises_regardless_of_policy(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {"base_url": "https://example.com/", "output_dir": "out", "search": {"enabled": False}},
    )
    config = load_config(path)  # must not raise
    assert config.search.enabled is False


def test_example_site_yaml_parses():
    repo_root = Path(__file__).resolve().parent.parent
    config = load_config(repo_root / "example-site.yaml")
    assert config.xml_backup is None


# ---------------------------------------------------------------------------
# Site probing (against a real local fixture server -- not the internet)
# ---------------------------------------------------------------------------


def test_probe_site_http_only_fixture_reports_no_https():
    with FixtureSite() as site:
        profile = probe_site(site.site_base, requests.Session(), "test-agent", [], timeout=1.0)
        assert profile.use_https is False
        assert "127.0.0.1" in profile.site_hosts


# ---------------------------------------------------------------------------
# run_acquire / run_report / run_status against the fixture site
# ---------------------------------------------------------------------------


def _config_for(site: FixtureSite, output_dir: Path):
    from wpfreeze.cli import SiteConfig, WaybackSettings

    return SiteConfig(
        base_url=site.site_base + "/",
        output_dir=output_dir,
        rate_limit=0.0,
        wayback_rate_limit=0.0,
        exclusions=[],
        wayback=WaybackSettings(enabled=False),  # no real Wayback in tests
    )


def test_run_acquire_end_to_end_produces_artefacts(tmp_path: Path):
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        exit_code = run_acquire(config, resume=False, dry_run=False)

        assert exit_code == 1  # /gone/ and /secret/ are real gaps (no Wayback in this test)
        output_dir = config.output_dir
        assert (output_dir / "manifest.json").exists()
        assert (output_dir / "report.json").exists()
        assert (output_dir / "report.html").exists()
        assert (output_dir / "redirects.htaccess").exists()

        manifest = Manifest.load(output_dir / "manifest.json")
        about = manifest.get(site.site_base + "/about/")
        assert about is not None
        assert about.output_path == "/about.html"


def test_run_acquire_prints_a_timing_wrapup(tmp_path: Path, capsys):
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)
        out = capsys.readouterr().out
        assert "done in" in out
        assert "acquire" in out
        assert "Manifest" in out
        assert str(config.output_dir / "report.html") in out


def test_run_acquire_refuses_without_resume_when_manifest_exists(tmp_path: Path):
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)
        exit_code = run_acquire(config, resume=False, dry_run=False)
        assert exit_code == 2


def test_run_acquire_resume_succeeds_when_manifest_exists(tmp_path: Path):
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)
        exit_code = run_acquire(config, resume=True, dry_run=False)
        assert exit_code == 1  # still has the same real gaps, but ran without refusing


def test_run_acquire_dry_run_fetches_nothing_beyond_inventory(tmp_path: Path):
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        exit_code = run_acquire(config, resume=False, dry_run=True)
        assert exit_code == 0
        manifest = Manifest.load(config.output_dir / "manifest.json")
        # base_url was seeded, but never actually fetched.
        base_record = manifest.get(site.site_base + "/")
        assert base_record.status == Status.PENDING.value
        assert not (config.output_dir / "raw").exists()


def test_run_acquire_dry_run_writes_a_readiness_verdict(tmp_path: Path, capsys):
    """End-to-end: assess_dry_run is computed from the real discover_
    inventory sources/manifest _run_acquire_locked has at hand, and reaches
    the console, report.json, and report.html -- see
    CLAUDE-dry-run-readiness.md for the individual rules' own unit tests
    (test_report.py), this just proves the wiring."""
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        exit_code = run_acquire(config, resume=False, dry_run=True)
        assert exit_code == 0

        out = capsys.readouterr().out
        assert "Readiness:" in out
        assert "Based on inventory discovery only" in out

        report = json.loads((config.output_dir / "report.json").read_text())
        assert report["readiness"] is not None
        assert report["readiness"]["verdict"] in ("ready", "review", "attention")

        html = (config.output_dir / "report.html").read_text()
        assert 'id="readiness"' in html


def test_run_acquire_real_run_readiness_is_null(tmp_path: Path):
    """readiness is acquire-dry-run-only -- a real (non-dry) run's report
    carries a null verdict rather than a stale inventory-only one, since
    a completed crawl has strictly better signals it isn't using here."""
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)
        report = json.loads((config.output_dir / "report.json").read_text())
        assert report["readiness"] is None


def test_run_acquire_releases_lock_after_completion(tmp_path: Path):
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)
        assert not (config.output_dir / ".wpfreeze.lock").exists()


def test_run_acquire_refuses_when_lock_held_by_live_process(tmp_path: Path):
    import os

    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        config.output_dir.mkdir(parents=True)
        (config.output_dir / ".wpfreeze.lock").write_text(str(os.getpid()))  # this test process is "alive"

        exit_code = run_acquire(config, resume=False, dry_run=False)

        assert exit_code == 2
        assert not (config.output_dir / "manifest.json").exists()


# ---------------------------------------------------------------------------
# run_rescan
# ---------------------------------------------------------------------------


def _patch_discover_links_to_inject(monkeypatch, source_url: str, injected_url: str):
    """Simulates the actual feature: an extraction-code improvement finds a
    reference on `source_url`'s already-stored bytes that the original
    crawl's extraction did not."""
    import wpfreeze.rescan as rescan_module
    from wpfreeze.extract import HYPERLINK, ExtractedLink

    real_discover_links = rescan_module.discover_links

    def patched(content, final_url, kind):
        links = real_discover_links(content, final_url, kind)
        if final_url == source_url:
            links = [*links, ExtractedLink(injected_url, HYPERLINK, "test:injected")]
        return links

    monkeypatch.setattr(rescan_module, "discover_links", patched)


def test_run_rescan_report_only_writes_nothing(tmp_path: Path, monkeypatch):
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)
        _patch_discover_links_to_inject(
            monkeypatch, site.site_base + "/about/", site.site_base + "/discovered-by-rescan/"
        )
        manifest_path = config.output_dir / "manifest.json"
        before_bytes = manifest_path.read_bytes()

        exit_code = run_rescan(config, apply=False, profile_from_config=False)

        assert exit_code == 0
        assert manifest_path.read_bytes() == before_bytes


def test_run_rescan_apply_writes_new_pending_record(tmp_path: Path, monkeypatch):
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)
        new_url = site.site_base + "/discovered-by-rescan/"
        _patch_discover_links_to_inject(monkeypatch, site.site_base + "/about/", new_url)

        exit_code = run_rescan(config, apply=True, profile_from_config=False)

        assert exit_code == 0
        manifest = Manifest.load(config.output_dir / "manifest.json")
        record = manifest.get(new_url)
        assert record is not None
        assert record.status == Status.PENDING.value
        assert not (config.output_dir / ".wpfreeze.lock").exists()  # released


def test_run_rescan_no_manifest_found(tmp_path: Path):
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        exit_code = run_rescan(config, apply=False, profile_from_config=False)
        assert exit_code == 2


def test_run_rescan_apply_refuses_on_manifest_without_persisted_profile(tmp_path: Path):
    """A schema-1 manifest has no persisted SiteProfile -- guessing one
    from config alone can disagree with the real crawl's normalization and
    flood the manifest with spurious duplicates, so --apply refuses
    without an explicit override."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"schema_version": 1, "generated": "x", "records": []}))
    config = SiteConfig(base_url="http://example.com/", output_dir=output_dir, rate_limit=0.0)

    exit_code = run_rescan(config, apply=True, profile_from_config=False)

    assert exit_code == 2
    assert json.loads(manifest_path.read_text())["records"] == []


def test_run_rescan_apply_proceeds_with_explicit_profile_override(tmp_path: Path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"schema_version": 1, "generated": "x", "records": []}))
    config = SiteConfig(base_url="http://example.com/", output_dir=output_dir, rate_limit=0.0)

    exit_code = run_rescan(config, apply=True, profile_from_config=True)

    assert exit_code == 0


def test_run_rescan_report_only_never_refuses_on_legacy_manifest(tmp_path: Path):
    """Report-only mode just warns -- it writes nothing, so there is
    nothing for a wrong-guessed profile to corrupt."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"schema_version": 1, "generated": "x", "records": []}))
    config = SiteConfig(base_url="http://example.com/", output_dir=output_dir, rate_limit=0.0)

    exit_code = run_rescan(config, apply=False, profile_from_config=False)

    assert exit_code == 0


def test_run_rescan_apply_refuses_when_lock_held_by_live_process(tmp_path: Path):
    import os

    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)
        (config.output_dir / ".wpfreeze.lock").write_text(str(os.getpid()))

        exit_code = run_rescan(config, apply=True, profile_from_config=False)

        assert exit_code == 2


def test_run_report_regenerates_without_refetching(tmp_path: Path):
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)
        requests_before = len(site.site_request_log)

        (config.output_dir / "report.html").unlink()
        exit_code = run_report(config, html_only=False, json_only=False)

        assert exit_code == 1
        assert (config.output_dir / "report.html").exists()
        assert len(site.site_request_log) == requests_before  # no new fetches


def test_run_report_missing_manifest_returns_error(tmp_path: Path):
    from wpfreeze.cli import SiteConfig

    config = SiteConfig(base_url="https://example.com/", output_dir=tmp_path / "nope")
    assert run_report(config, html_only=False, json_only=False) == 2


def test_main_creates_logs_directory_with_content(tmp_path: Path):
    with FixtureSite() as site:
        output_dir = tmp_path / "out"
        config_path = _write_yaml(
            tmp_path / "site.yaml",
            {
                "base_url": site.site_base + "/",
                "output_dir": str(output_dir),
                "rate_limit": 0.0,
                "wayback_rate_limit": 0.0,
                "wayback": {"enabled": False},
            },
        )
        exit_code = main(["acquire", "--config", str(config_path)])
        assert exit_code == 1

        log_files = list((output_dir / "logs").glob("*.log"))
        assert len(log_files) == 1
        content = log_files[0].read_text()
        assert "fetched" in content.lower()


def test_main_status_by_project_name_behaves_like_by_config(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_yaml(tmp_path / "landscapes.yaml", {"name": "landscapes", "base_url": "https://example.com/", "output_dir": "out"})

    exit_code_by_name = main(["status", "landscapes"])
    by_name = capsys.readouterr().out
    exit_code_by_config = main(["status", "--config", "landscapes.yaml"])
    by_config = capsys.readouterr().out

    assert exit_code_by_name == exit_code_by_config == 0
    assert by_name == by_config


def test_main_project_and_config_together_is_an_error(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_yaml(tmp_path / "landscapes.yaml", {"name": "landscapes", "base_url": "https://example.com/", "output_dir": "out"})

    assert main(["status", "landscapes", "--config", "landscapes.yaml"]) == 2


def test_main_neither_project_nor_config_is_an_error(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["status"]) == 2


def test_main_help_prints_usage_and_exits_zero(capsys):
    # `wpfreeze help` is a bare-word alias for -h/--help (git/docker/npm-
    # style muscle memory) -- not project-addressed, must not fall into
    # the project/--config resolution every other subcommand goes through.
    exit_code = main(["help"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "usage: wpfreeze" in out
    assert "acquire" in out and "freeze" in out


def test_main_help_matches_bare_dash_h(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["-h"])
    assert exc_info.value.code == 0
    dash_h_out = capsys.readouterr().out

    main(["help"])
    help_out = capsys.readouterr().out

    assert dash_h_out == help_out


def test_main_unknown_project_name_exits_2_with_known_names_listed(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_yaml(tmp_path / "landscapes.yaml", {"name": "landscapes", "base_url": "https://example.com/", "output_dir": "out"})

    exit_code = main(["status", "nope"])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "no project called nope" in out
    assert "landscapes" in out


def test_run_status_reports_counts(tmp_path: Path, capsys):
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)
        exit_code = run_status(config)
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Total records" in captured.out
        assert "fetched" in captured.out


# ---------------------------------------------------------------------------
# run_diagnose / diagnostics.json
# ---------------------------------------------------------------------------


def test_run_diagnose_writes_report_and_prints_summary(tmp_path: Path, capsys):
    from wpfreeze.diagnostics import build_diagnostics
    from wpfreeze.cli import run_diagnose

    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)

        exit_code = run_diagnose(config)

        assert exit_code == 0
        diagnostics_path = config.output_dir / "diagnostics.json"
        assert diagnostics_path.exists()
        captured = capsys.readouterr()
        assert "Diagnostics:" in captured.out
        assert str(diagnostics_path) in captured.out

        import json

        saved = json.loads(diagnostics_path.read_text())
        assert saved["homepage"]["found"] is True
        assert saved["duplicate_local_paths"] == []
        assert saved["disk_hash_mismatches"] == []


def test_latest_log_path_skips_empty_stub_logs(tmp_path: Path):
    """Every wpfreeze invocation creates its own log file (even a bare
    `status`/`diagnose` call), almost always empty -- picking the most
    recently modified file would nearly always return a trivial stub
    rather than the substantive acquire-run log."""
    from wpfreeze.cli import _latest_log_path

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "20260710T100000.log").write_text("real acquire run content\n")
    (logs_dir / "20260710T110000.log").write_text("")  # e.g. a later `status` call

    assert _latest_log_path(tmp_path) == logs_dir / "20260710T100000.log"


def test_latest_log_path_returns_none_when_all_logs_empty(tmp_path: Path):
    from wpfreeze.cli import _latest_log_path

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "20260710T100000.log").write_text("")

    assert _latest_log_path(tmp_path) is None


def test_run_diagnose_missing_manifest_returns_error(tmp_path: Path):
    from wpfreeze.cli import SiteConfig, run_diagnose

    config = SiteConfig(base_url="https://example.com/", output_dir=tmp_path / "nope")
    assert run_diagnose(config) == 2


def test_run_acquire_does_not_prompt_when_not_a_tty(tmp_path: Path, monkeypatch):
    """acquire is routinely launched unattended/backgrounded -- the
    end-of-run diagnostics offer must never block on input() outside a
    real interactive terminal (pytest's stdin already isn't a tty, this
    just makes the guarantee explicit and future-proof)."""
    import sys

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("input() must not be called when stdin is not a tty")

    monkeypatch.setattr("builtins.input", _fail_if_called)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)  # must not raise/hang


# ---------------------------------------------------------------------------
# run_build / run_validate: cleanup-todo announcement
# ---------------------------------------------------------------------------


def _minimal_capture(output_dir: Path) -> SiteConfig:
    manifest = Manifest()
    record = manifest.get_or_create("https://example.com/")
    record.status = Status.FETCHED.value
    record.http_status = 200
    record.output_path = "/index.html"
    record.local_path = "raw/index.html"
    record.content_type = "text/html"
    (output_dir / "raw").mkdir(parents=True)
    (output_dir / "raw" / "index.html").write_text("<html><body>hi</body></html>", encoding="utf-8")
    manifest.save(output_dir / "manifest.json")
    return SiteConfig(base_url="https://example.com/", output_dir=output_dir)


def test_run_build_writes_build_report_and_cleanup_todo(tmp_path: Path, capsys):
    from wpfreeze.cli import run_build

    output_dir = tmp_path / "out"
    config = _minimal_capture(output_dir)

    run_build(config, None, verify=False)

    assert (output_dir / "build-report.json").exists()
    assert (output_dir / "cleanup-todo.md").exists()
    assert (output_dir / "cleanup-todo.html").exists()
    out = capsys.readouterr().out
    assert f"Cleanup checklist: {output_dir / 'cleanup-todo.md'}" in out
    assert "cleanup-todo.html" in out


def test_run_build_prints_a_timing_wrapup(tmp_path: Path, capsys):
    from wpfreeze.cli import run_build

    output_dir = tmp_path / "out"
    config = _minimal_capture(output_dir)

    run_build(config, None, verify=False)

    out = capsys.readouterr().out
    assert "done in" in out
    assert "build" in out
    assert "Built site" in out
    assert str(output_dir / "site") in out


def test_run_build_verify_ignores_a_pre_existing_file_in_site_dir(tmp_path: Path):
    """Regression test: a file already sitting in the site directory before
    this build ran (e.g. one the user copied in for their own purposes) must
    not be scanned by verification as if it were this run's own output."""
    from wpfreeze.cli import run_build

    output_dir = tmp_path / "out"
    config = _minimal_capture(output_dir)
    site_dir = output_dir / "site"
    site_dir.mkdir(parents=True)
    (site_dir / "stray.html").write_text('<a href="does-not-exist.html">dead</a>', encoding="utf-8")

    exit_code = run_build(config, None, verify=True)

    assert exit_code == 0
    build_report = json.loads((output_dir / "build-report.json").read_text(encoding="utf-8"))
    assert build_report["verification"]["broken"] == 0


def test_run_build_never_writes_an_upload_script(tmp_path: Path):
    """upload.sh is written only by the explicit `upload-script` command,
    never as a side effect of `build` -- regression test for that
    boundary, regardless of whether `upload.remote` is configured."""
    import dataclasses

    from wpfreeze.cli import UploadSettings, run_build

    output_dir = tmp_path / "out"
    config = dataclasses.replace(
        _minimal_capture(output_dir), upload=UploadSettings(remote="user@example.com:/var/www/html")
    )

    run_build(config, None, verify=False)

    assert not (output_dir / "upload.sh").exists()


def test_upload_script_writes_when_remote_configured(tmp_path: Path, capsys):
    import dataclasses

    from wpfreeze.cli import UploadSettings, run_build, run_upload_script

    output_dir = tmp_path / "out"
    config = dataclasses.replace(
        _minimal_capture(output_dir), upload=UploadSettings(remote="user@example.com:/var/www/html")
    )
    run_build(config, None, verify=False)  # so the "built site exists" check passes

    exit_code = run_upload_script(config, None)

    upload_path = output_dir / "upload.sh"
    assert exit_code == 0
    assert upload_path.exists()
    assert 'REMOTE="user@example.com:/var/www/html"' in upload_path.read_text(encoding="utf-8")
    assert f"Upload script: {upload_path}" in capsys.readouterr().out


def test_upload_script_writes_even_without_a_remote_configured(tmp_path: Path, capsys):
    # Unlike the old single-remote behavior, this is no longer a refusal --
    # --local needs neither remote, so the script is always worth writing.
    from wpfreeze.cli import run_build, run_upload_script

    output_dir = tmp_path / "out"
    config = _minimal_capture(output_dir)
    run_build(config, None, verify=False)

    exit_code = run_upload_script(config, None)

    upload_path = output_dir / "upload.sh"
    out = capsys.readouterr().out
    assert exit_code == 0
    assert upload_path.exists()
    assert "no upload.remote set" in out
    assert "no upload.prod_remote set" in out


def test_upload_script_writes_prod_remote_alongside_staging_remote(tmp_path: Path, capsys):
    import dataclasses

    from wpfreeze.cli import UploadSettings, run_build, run_upload_script

    output_dir = tmp_path / "out"
    config = dataclasses.replace(
        _minimal_capture(output_dir),
        upload=UploadSettings(remote="user@staging:/path", prod_remote="user@prod:/path"),
    )
    run_build(config, None, verify=False)

    exit_code = run_upload_script(config, None)

    content = (output_dir / "upload.sh").read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert exit_code == 0
    assert 'REMOTE="user@staging:/path"' in content
    assert 'PROD_REMOTE="user@prod:/path"' in content
    assert "no upload.remote set" not in out
    assert "no upload.prod_remote set" not in out


def test_upload_script_without_a_built_site_errors(tmp_path: Path, capsys):
    import dataclasses

    from wpfreeze.cli import UploadSettings, run_upload_script

    output_dir = tmp_path / "out"
    config = dataclasses.replace(
        _minimal_capture(output_dir), upload=UploadSettings(remote="user@example.com:/var/www/html")
    )
    # No run_build call -- output_dir/site never gets created.

    exit_code = run_upload_script(config, None)

    assert exit_code == 2
    assert not (output_dir / "upload.sh").exists()
    assert "run `wpfreeze build` first" in capsys.readouterr().out


def test_run_build_no_todo_flag_skips_cleanup_todo_but_still_writes_build_report(tmp_path: Path):
    from wpfreeze.cli import run_build

    output_dir = tmp_path / "out"
    config = _minimal_capture(output_dir)

    run_build(config, None, verify=False, write_todo=False)

    assert (output_dir / "build-report.json").exists()
    assert not (output_dir / "cleanup-todo.md").exists()
    assert not (output_dir / "cleanup-todo.html").exists()


def _mock_pagefind_index(monkeypatch, *, ok: bool = True, pages_indexed: int = 1,
                          languages: tuple[str, ...] = ("en",), error: str = "", raises: Exception | None = None):
    from wpfreeze.search import SearchIndexResult

    calls = []

    def _fake(site_dir, settings, timeout=1800.0):
        calls.append((site_dir, settings))
        if raises is not None:
            raise raises
        return SearchIndexResult(ok=ok, pages_indexed=pages_indexed, languages=languages, error=error)

    monkeypatch.setattr("wpfreeze.cli.run_pagefind_index", _fake)
    return calls


def test_run_build_indexes_automatically_when_search_enabled(tmp_path: Path, monkeypatch, capsys):
    import dataclasses

    from wpfreeze.cli import SearchSettings, run_build

    output_dir = tmp_path / "out"
    config = dataclasses.replace(_minimal_capture(output_dir), search=SearchSettings(enabled=True))
    calls = _mock_pagefind_index(monkeypatch, ok=True, pages_indexed=1, languages=("en",))

    exit_code = run_build(config, None, verify=False)

    assert exit_code == 0
    assert len(calls) == 1
    build_report = json.loads((output_dir / "build-report.json").read_text(encoding="utf-8"))
    assert build_report["search"]["index_ok"] is True
    assert build_report["search"]["indexed_pages"] == 1
    assert "Search:" in capsys.readouterr().out


def test_run_build_reports_content_issues_in_summary_and_report(tmp_path: Path, monkeypatch, capsys):
    import dataclasses

    from wpfreeze.cli import SearchSettings, run_build

    output_dir = tmp_path / "out"
    # _minimal_capture's page ("hi") has no wp-singular class, so the
    # default body_selectors matches nothing anywhere -- sitewide tagging
    # is off, and the whole-body fallback ("hi", 1 word) is what
    # scan_content_issues actually sees, same as a real untagged build.
    config = dataclasses.replace(_minimal_capture(output_dir), search=SearchSettings(enabled=True))
    _mock_pagefind_index(monkeypatch, ok=True, pages_indexed=1, languages=("en",))

    run_build(config, None, verify=False)

    out = capsys.readouterr().out
    assert "thin content" in out
    assert "echoed content" in out
    build_report = json.loads((output_dir / "build-report.json").read_text(encoding="utf-8"))
    assert build_report["content_issues"]["thin_pages"] == [
        {"page": "/index.html", "word_count": 1, "acknowledged": False}
    ]


def test_run_build_does_not_index_when_search_disabled(tmp_path: Path, monkeypatch):
    from wpfreeze.cli import run_build

    output_dir = tmp_path / "out"
    config = _minimal_capture(output_dir)
    calls = _mock_pagefind_index(monkeypatch)

    run_build(config, None, verify=False)

    assert calls == []


def test_run_build_fails_when_search_index_fails(tmp_path: Path, monkeypatch):
    import dataclasses

    from wpfreeze.cli import SearchSettings, run_build

    output_dir = tmp_path / "out"
    config = dataclasses.replace(_minimal_capture(output_dir), search=SearchSettings(enabled=True))
    _mock_pagefind_index(monkeypatch, ok=False, error="boom")

    exit_code = run_build(config, None, verify=False)

    assert exit_code == 1
    build_report = json.loads((output_dir / "build-report.json").read_text(encoding="utf-8"))
    assert build_report["search"]["index_ok"] is False
    assert build_report["search"]["index_error"] == "boom"


def test_run_build_returns_2_when_pagefind_unavailable(tmp_path: Path, monkeypatch, capsys):
    import dataclasses

    from wpfreeze.cli import SearchSettings, run_build
    from wpfreeze.search import SearchUnavailable

    output_dir = tmp_path / "out"
    config = dataclasses.replace(_minimal_capture(output_dir), search=SearchSettings(enabled=True))
    _mock_pagefind_index(monkeypatch, raises=SearchUnavailable("pagefind not installed"))

    exit_code = run_build(config, None, verify=False)

    assert exit_code == 2
    assert "pagefind not installed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# search-enabled-but-no-search-form pushback (Part 4 of CLAUDE-freeze-ux.md)
# ---------------------------------------------------------------------------


def _capture_with_search_form(output_dir: Path) -> SiteConfig:
    """Like _minimal_capture, but the page has a real, recognizable search
    form -- apply_search will tag it, so stats.search.forms_tagged > 0."""
    manifest = Manifest()
    record = manifest.get_or_create("https://example.com/")
    record.status = Status.FETCHED.value
    record.http_status = 200
    record.output_path = "/index.html"
    record.local_path = "raw/index.html"
    record.content_type = "text/html"
    (output_dir / "raw").mkdir(parents=True)
    (output_dir / "raw" / "index.html").write_text(
        '<html><body><form role="search"><input name="s"></form></body></html>', encoding="utf-8"
    )
    manifest.save(output_dir / "manifest.json")
    return SiteConfig(base_url="https://example.com/", output_dir=output_dir)


def test_no_search_forms_non_tty_proceeds_and_warns(tmp_path: Path, monkeypatch, capsys):
    import dataclasses

    from wpfreeze.cli import SearchSettings, run_build

    output_dir = tmp_path / "out"
    config = dataclasses.replace(_minimal_capture(output_dir), search=SearchSettings(enabled=True))
    calls = _mock_pagefind_index(monkeypatch, ok=True, pages_indexed=1, languages=("en",))

    exit_code = run_build(config, None, verify=False)
    out = capsys.readouterr().out

    assert "Search is enabled, but this capture has no search form." in out
    assert len(calls) == 1  # still indexed -- non-tty proceeds as before
    assert exit_code == 0


def test_no_search_forms_tty_declined_skips_indexing(tmp_path: Path, monkeypatch):
    import dataclasses

    from wpfreeze.cli import SearchSettings, run_build

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    output_dir = tmp_path / "out"
    config = dataclasses.replace(_minimal_capture(output_dir), search=SearchSettings(enabled=True))
    calls = _mock_pagefind_index(monkeypatch, ok=True, pages_indexed=1, languages=("en",))

    run_build(config, None, verify=False)

    assert calls == []
    build_report = json.loads((output_dir / "build-report.json").read_text(encoding="utf-8"))
    assert build_report["search"]["enabled"] is False


def test_no_search_forms_tty_accepted_indexes_normally(tmp_path: Path, monkeypatch):
    import dataclasses

    from wpfreeze.cli import SearchSettings, run_build

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    output_dir = tmp_path / "out"
    config = dataclasses.replace(_minimal_capture(output_dir), search=SearchSettings(enabled=True))
    calls = _mock_pagefind_index(monkeypatch, ok=True, pages_indexed=1, languages=("en",))

    exit_code = run_build(config, None, verify=False)

    assert len(calls) == 1
    assert exit_code == 0


def test_no_search_forms_acknowledged_skips_warning_and_prompt(tmp_path: Path, monkeypatch, capsys):
    import dataclasses

    from wpfreeze.cli import SearchSettings, run_build

    def _fail_if_called(prompt):
        raise AssertionError("acknowledged_no_forms must suppress the prompt entirely")

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _fail_if_called)
    output_dir = tmp_path / "out"
    config = dataclasses.replace(
        _minimal_capture(output_dir), search=SearchSettings(enabled=True, acknowledged_no_forms=True)
    )
    calls = _mock_pagefind_index(monkeypatch, ok=True, pages_indexed=1, languages=("en",))

    exit_code = run_build(config, None, verify=False)
    out = capsys.readouterr().out

    assert "Search is enabled, but this capture has no search form." not in out
    assert len(calls) == 1
    assert exit_code == 0


def test_search_forms_present_no_warning(tmp_path: Path, monkeypatch, capsys):
    """Guard against a regression that warns whenever *some* page lacks a
    form -- the condition is forms_tagged == 0 across the whole capture,
    not "every page has one"."""
    import dataclasses

    from wpfreeze.cli import SearchSettings, run_build
    from wpfreeze.policy import Policy

    output_dir = tmp_path / "out"
    config = dataclasses.replace(
        _capture_with_search_form(output_dir),
        search=SearchSettings(enabled=True),
        policy=Policy(strip_search_forms=False),  # otherwise policy strips the form before apply_search sees it
    )
    calls = _mock_pagefind_index(monkeypatch, ok=True, pages_indexed=1, languages=("en",))

    run_build(config, None, verify=False)
    out = capsys.readouterr().out

    assert "Search is enabled, but this capture has no search form." not in out
    assert len(calls) == 1


def test_load_config_reads_acknowledged_no_forms(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {
            "base_url": "https://example.com/",
            "output_dir": "out",
            "policy": {"strip_search_forms": False},
            "search": {"enabled": True, "acknowledged_no_forms": True},
        },
    )
    config = load_config(path)
    assert config.search.acknowledged_no_forms is True


def test_load_config_acknowledged_no_forms_defaults_false(tmp_path: Path):
    path = _write_yaml(tmp_path / "site.yaml", {"base_url": "https://example.com/", "output_dir": "out"})
    config = load_config(path)
    assert config.search.acknowledged_no_forms is False


def test_search_index_without_search_enabled_errors(tmp_path: Path, capsys):
    from wpfreeze.cli import run_search_index

    output_dir = tmp_path / "out"
    config = _minimal_capture(output_dir)

    exit_code = run_search_index(config, None)

    assert exit_code == 2
    assert "nothing to index" in capsys.readouterr().out


def test_search_index_without_a_built_site_errors(tmp_path: Path, capsys):
    import dataclasses

    from wpfreeze.cli import SearchSettings, run_search_index

    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True)
    config = dataclasses.replace(
        SiteConfig(base_url="https://example.com/", output_dir=output_dir), search=SearchSettings(enabled=True)
    )
    # No run_build call -- output_dir/site never gets created.

    exit_code = run_search_index(config, None)

    assert exit_code == 2
    assert "run `wpfreeze build` first" in capsys.readouterr().out


def test_search_index_reindexes_an_already_built_site(tmp_path: Path, monkeypatch, capsys):
    import dataclasses

    from wpfreeze.cli import SearchSettings, run_build, run_search_index

    output_dir = tmp_path / "out"
    config = dataclasses.replace(_minimal_capture(output_dir), search=SearchSettings(enabled=True))
    _mock_pagefind_index(monkeypatch, ok=True, pages_indexed=1, languages=("en",))
    run_build(config, None, verify=False)  # so the built site exists

    calls = _mock_pagefind_index(monkeypatch, ok=True, pages_indexed=1, languages=("en",))
    exit_code = run_search_index(config, None)

    assert exit_code == 0
    assert len(calls) == 1
    assert "Search index: 1 page(s)" in capsys.readouterr().out


def test_search_index_reports_content_issues_and_notes_checklist_not_refreshed(
    tmp_path: Path, monkeypatch, capsys
):
    import dataclasses

    from wpfreeze.cli import SearchSettings, run_build, run_search_index

    output_dir = tmp_path / "out"
    config = dataclasses.replace(_minimal_capture(output_dir), search=SearchSettings(enabled=True))
    _mock_pagefind_index(monkeypatch, ok=True, pages_indexed=1, languages=("en",))
    run_build(config, None, verify=False)

    _mock_pagefind_index(monkeypatch, ok=True, pages_indexed=1, languages=("en",))
    capsys.readouterr()  # discard run_build's own output
    exit_code = run_search_index(config, None)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "thin content" in out
    assert "echoed content" in out
    assert "not refreshed" in out


def test_search_index_returns_1_on_a_failed_index(tmp_path: Path, monkeypatch, capsys):
    import dataclasses

    from wpfreeze.cli import SearchSettings, run_build, run_search_index

    output_dir = tmp_path / "out"
    config = dataclasses.replace(_minimal_capture(output_dir), search=SearchSettings(enabled=True))
    _mock_pagefind_index(monkeypatch, ok=True)
    run_build(config, None, verify=False)

    _mock_pagefind_index(monkeypatch, ok=False, error="index exploded")
    exit_code = run_search_index(config, None)

    assert exit_code == 1
    assert "index exploded" in capsys.readouterr().out


def test_run_validate_writes_cleanup_todo(tmp_path: Path, monkeypatch):
    from wpfreeze.cli import run_validate
    from wpfreeze.validate import ValidationReport

    output_dir = tmp_path / "out"
    (output_dir / "site").mkdir(parents=True)
    config = SiteConfig(base_url="https://example.com/", output_dir=output_dir)

    monkeypatch.setattr("wpfreeze.cli.ensure_vnu_jar", lambda: Path("/fake/vnu.jar"))
    monkeypatch.setattr(
        "wpfreeze.cli.validate_site",
        lambda jar, target: ValidationReport(documents_checked=1, total_messages=0, issues=[]),
    )

    run_validate(config, None)

    assert (output_dir / "cleanup-todo.md").exists()
    assert (output_dir / "cleanup-todo.html").exists()


def test_run_validate_no_todo_flag_skips_cleanup_todo(tmp_path: Path, monkeypatch):
    from wpfreeze.cli import run_validate
    from wpfreeze.validate import ValidationReport

    output_dir = tmp_path / "out"
    (output_dir / "site").mkdir(parents=True)
    config = SiteConfig(base_url="https://example.com/", output_dir=output_dir)

    monkeypatch.setattr("wpfreeze.cli.ensure_vnu_jar", lambda: Path("/fake/vnu.jar"))
    monkeypatch.setattr(
        "wpfreeze.cli.validate_site",
        lambda jar, target: ValidationReport(documents_checked=1, total_messages=0, issues=[]),
    )

    run_validate(config, None, write_todo=False)

    assert not (output_dir / "cleanup-todo.md").exists()
    assert not (output_dir / "cleanup-todo.html").exists()


# ---------------------------------------------------------------------------
# end-of-acquire build+validate offer
# ---------------------------------------------------------------------------


def _tty_config(tmp_path: Path, monkeypatch) -> SiteConfig:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    return SiteConfig(base_url="https://example.com/", output_dir=tmp_path / "out")


def test_build_validate_offer_skipped_when_not_a_tty(tmp_path: Path, monkeypatch):
    from wpfreeze.cli import _maybe_offer_build_and_validate

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("input() must not be called when stdin is not a tty")

    monkeypatch.setattr("builtins.input", _fail_if_called)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    config = SiteConfig(base_url="https://example.com/", output_dir=tmp_path / "out")

    _maybe_offer_build_and_validate(config)  # must not raise/hang


def test_build_validate_offer_declines_build_skips_validate(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    config = _tty_config(tmp_path, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    build_called = []
    validate_called = []
    monkeypatch.setattr(cli, "run_build", lambda *a, **k: build_called.append(1) or 0)
    monkeypatch.setattr(cli, "run_validate", lambda *a, **k: validate_called.append(1) or 0)

    cli._maybe_offer_build_and_validate(config)

    assert build_called == []
    assert validate_called == []


def test_build_validate_offer_runs_both_when_accepted_and_java_present(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    config = _tty_config(tmp_path, monkeypatch)
    answers = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/java")
    build_called = []
    validate_called = []
    monkeypatch.setattr(cli, "run_build", lambda *a, **k: build_called.append(1) or 0)
    monkeypatch.setattr(cli, "run_validate", lambda *a, **k: validate_called.append(1) or 0)

    cli._maybe_offer_build_and_validate(config)

    assert build_called == [1]
    assert validate_called == [1]


def test_build_validate_offer_skips_validate_offer_without_java(tmp_path: Path, monkeypatch, capsys):
    import wpfreeze.cli as cli

    config = _tty_config(tmp_path, monkeypatch)

    def _fail_on_second_prompt(prompt):
        raise AssertionError("must not prompt to validate when java is missing")

    prompts = iter(["y"])

    def _input(prompt):
        try:
            return next(prompts)
        except StopIteration:
            return _fail_on_second_prompt(prompt)

    monkeypatch.setattr("builtins.input", _input)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "run_build", lambda *a, **k: 0)
    validate_called = []
    monkeypatch.setattr(cli, "run_validate", lambda *a, **k: validate_called.append(1) or 0)

    cli._maybe_offer_build_and_validate(config)

    assert validate_called == []
    assert "java" in capsys.readouterr().out.lower()


def test_build_validate_offer_skips_validate_when_build_hard_fails(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    config = _tty_config(tmp_path, monkeypatch)

    def _fail_if_called(prompt):
        raise AssertionError("must not prompt to validate when build hard-failed")

    prompts = iter(["y"])

    def _input(prompt):
        try:
            return next(prompts)
        except StopIteration:
            return _fail_if_called(prompt)

    monkeypatch.setattr("builtins.input", _input)
    monkeypatch.setattr(cli, "run_build", lambda *a, **k: 2)  # e.g. no manifest found
    validate_called = []
    monkeypatch.setattr(cli, "run_validate", lambda *a, **k: validate_called.append(1) or 0)

    cli._maybe_offer_build_and_validate(config)

    assert validate_called == []


# ---------------------------------------------------------------------------
# main() dispatch: no subcommand -> wizard
# ---------------------------------------------------------------------------


def test_main_with_no_args_not_a_tty_prints_overview(monkeypatch):
    """Bare `wpfreeze` without a real terminal (piped output, CI, a script
    capturing stdout) falls back to the non-interactive overview -- not
    the interactive picker, and not the interactive wizard either. `wizard`
    (the subcommand) is what launches run_wizard now."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    overview_calls = []
    wizard_calls = []
    picker_calls = []
    monkeypatch.setattr("wpfreeze.wizard.print_overview", lambda: overview_calls.append("overview") or 0)
    monkeypatch.setattr("wpfreeze.wizard.run_wizard", lambda: wizard_calls.append("wizard") or 0)
    monkeypatch.setattr("wpfreeze.picker.run_picker", lambda: picker_calls.append("picker") or 0)

    assert main([]) == 0

    assert overview_calls == ["overview"]
    assert wizard_calls == []
    assert picker_calls == []


def test_main_with_no_args_in_a_real_terminal_launches_the_picker(monkeypatch):
    """Bare `wpfreeze` with both stdin and stdout attached to a real
    terminal launches the interactive picker instead of printing the
    static overview -- see _dispatch's own comment for the isatty gate."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    overview_calls = []
    picker_calls = []
    monkeypatch.setattr("wpfreeze.wizard.print_overview", lambda: overview_calls.append("overview") or 0)
    monkeypatch.setattr("wpfreeze.picker.run_picker", lambda: picker_calls.append("picker") or 0)

    assert main([]) == 0

    assert picker_calls == ["picker"]
    assert overview_calls == []


def test_main_with_no_args_only_one_stream_a_tty_still_falls_back_to_overview(monkeypatch):
    # Piping stdout while stdin is still a real terminal (or vice versa)
    # is exactly the "not really interactive" case the `and` in the gate
    # exists for -- a curses screen with no real place to draw, or reading
    # keys from a pipe, would just hang or crash.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    overview_calls = []
    picker_calls = []
    monkeypatch.setattr("wpfreeze.wizard.print_overview", lambda: overview_calls.append("overview") or 0)
    monkeypatch.setattr("wpfreeze.picker.run_picker", lambda: picker_calls.append("picker") or 0)

    assert main([]) == 0

    assert overview_calls == ["overview"]
    assert picker_calls == []


def test_main_wizard_subcommand_launches_the_interactive_wizard(monkeypatch):
    calls = []
    monkeypatch.setattr("wpfreeze.wizard.run_wizard", lambda: calls.append("wizard") or 0)
    assert main(["wizard"]) == 0
    assert calls == ["wizard"]


def test_main_reports_keyboard_interrupt_instead_of_a_traceback(monkeypatch, capsys):
    def raise_interrupt(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr("wpfreeze.cli._dispatch", raise_interrupt)
    exit_code = main(["acquire", "--config", "unused.yaml"])
    assert exit_code == 130
    captured = capsys.readouterr()
    assert "Interrupted" in captured.out
    assert "--resume" in captured.out


# ---------------------------------------------------------------------------
# run_freeze
# ---------------------------------------------------------------------------


def _freeze_config(tmp_path: Path, steps=("acquire", "build", "validate")) -> SiteConfig:
    return SiteConfig(
        name="landscapes",
        base_url="https://example.com/",
        output_dir=tmp_path / "out",
        freeze=FreezeSettings(steps=tuple(steps)),
    )


def test_run_freeze_runs_default_three_steps_in_order(tmp_path: Path):
    import wpfreeze.cli as cli

    calls: list[str] = []
    for step in ("acquire", "build", "validate"):
        _patch_step_module(cli, calls, step, 0)
    config = _freeze_config(tmp_path)

    exit_code = run_freeze(config, config.name)

    assert calls == ["acquire", "build", "validate"]
    assert exit_code == 0


def _patch_step_module(cli_module, calls, name, result=0):
    attr = {
        "acquire": "run_acquire",
        "build": "run_build",
        "validate": "run_validate",
        "diagnose": "run_diagnose",
        "report": "run_report",
        "checklinks": "run_checklinks",
        "search-index": "run_search_index",
        "upload-script": "run_upload_script",
    }[name]
    setattr(cli_module, attr, lambda *a, **k: calls.append(name) or result)


def test_run_freeze_step_returning_2_stops_sequence(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    calls: list[str] = []
    _patch_step_module(cli, calls, "acquire", 2)
    _patch_step_module(cli, calls, "build", 0)
    _patch_step_module(cli, calls, "validate", 0)
    config = _freeze_config(tmp_path)

    exit_code = run_freeze(config, config.name)

    assert calls == ["acquire"]  # build/validate never called
    assert exit_code == 2


def test_run_freeze_step_returning_1_continues_and_sets_exit_code(tmp_path: Path):
    import wpfreeze.cli as cli

    calls: list[str] = []
    _patch_step_module(cli, calls, "acquire", 1)
    _patch_step_module(cli, calls, "build", 0)
    _patch_step_module(cli, calls, "validate", 0)
    config = _freeze_config(tmp_path)

    exit_code = run_freeze(config, config.name)

    assert calls == ["acquire", "build", "validate"]
    assert exit_code == 1


def test_run_freeze_custom_step_order_is_honoured(tmp_path: Path):
    import wpfreeze.cli as cli

    calls: list[str] = []
    for step in ("build", "acquire", "diagnose"):
        _patch_step_module(cli, calls, step, 0)
    config = _freeze_config(tmp_path, steps=("build", "acquire", "diagnose"))

    run_freeze(config, config.name)

    assert calls == ["build", "acquire", "diagnose"]


def test_run_freeze_calls_acquire_with_resume_true_when_manifest_exists(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True)
    (output_dir / "manifest.json").write_text("{}", encoding="utf-8")

    captured_kwargs = {}

    def fake_run_acquire(config, resume, dry_run, **kwargs):
        captured_kwargs.update(kwargs)
        captured_kwargs["resume"] = resume
        return 0

    monkeypatch.setattr(cli, "run_acquire", fake_run_acquire)
    monkeypatch.setattr(cli, "run_build", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "run_validate", lambda *a, **k: 0)

    config = SiteConfig(name="landscapes", base_url="https://example.com/", output_dir=output_dir)
    run_freeze(config, config.name)

    assert captured_kwargs["resume"] is True


def test_run_freeze_calls_acquire_with_offer_followups_false(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    captured_kwargs = {}

    def fake_run_acquire(config, resume, dry_run, **kwargs):
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_acquire", fake_run_acquire)
    monkeypatch.setattr(cli, "run_build", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "run_validate", lambda *a, **k: 0)

    config = _freeze_config(tmp_path)
    run_freeze(config, config.name)

    assert captured_kwargs["offer_followups"] is False


def test_direct_acquire_still_offers_followups_by_default(tmp_path: Path, monkeypatch):
    """A direct `wpfreeze acquire <name>` (not via freeze) must keep its
    existing end-of-run offer behaviour -- only run_freeze suppresses it,
    via offer_followups=False."""
    import wpfreeze.cli as cli

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    calls: list[str] = []
    monkeypatch.setattr(cli, "_maybe_offer_diagnostics", lambda *a, **k: calls.append("diagnostics"))
    monkeypatch.setattr(cli, "_maybe_offer_build_and_validate", lambda *a, **k: calls.append("build_and_validate"))

    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)

    assert calls == ["diagnostics", "build_and_validate"]


def test_run_freeze_suppresses_acquires_followup_offers(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    calls: list[str] = []
    monkeypatch.setattr(cli, "_maybe_offer_diagnostics", lambda *a, **k: calls.append("diagnostics"))
    monkeypatch.setattr(cli, "_maybe_offer_build_and_validate", lambda *a, **k: calls.append("build_and_validate"))
    monkeypatch.setattr(cli, "run_build", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "run_validate", lambda *a, **k: 0)

    import dataclasses

    with FixtureSite() as site:
        config = dataclasses.replace(
            _config_for(site, tmp_path / "out"),
            name="landscapes",
            freeze=FreezeSettings(steps=("acquire", "build", "validate")),
        )
        run_freeze(config, config.name)

    assert calls == []


# --- end-of-run checklinks offer ---------------------------------------------


def _freeze_tty_config(tmp_path: Path, monkeypatch, steps=("acquire", "build", "validate")) -> SiteConfig:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    return _freeze_config(tmp_path, steps=steps)


def _patch_default_steps(monkeypatch, cli_module):
    monkeypatch.setattr(cli_module, "run_acquire", lambda *a, **k: 0)
    monkeypatch.setattr(cli_module, "run_build", lambda *a, **k: 0)
    monkeypatch.setattr(cli_module, "run_validate", lambda *a, **k: 0)


def test_checklinks_offer_not_asked_when_already_declared(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    config = _freeze_tty_config(tmp_path, monkeypatch, steps=("acquire", "build", "validate", "checklinks"))
    (config.output_dir / "site").mkdir(parents=True)
    _patch_default_steps(monkeypatch, cli)
    checklinks_calls = []
    monkeypatch.setattr(cli, "run_checklinks", lambda *a, **k: checklinks_calls.append(1) or 0)

    def _fail_if_called(prompt):
        raise AssertionError("must not offer checklinks again -- it was already declared")

    monkeypatch.setattr("builtins.input", _fail_if_called)

    run_freeze(config, config.name)

    assert checklinks_calls == [1]  # ran once, as the declared step


def test_checklinks_offer_not_asked_after_exit_2_abort(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    config = _freeze_tty_config(tmp_path, monkeypatch)
    (config.output_dir / "site").mkdir(parents=True)
    monkeypatch.setattr(cli, "run_acquire", lambda *a, **k: 2)

    def _fail_if_called(prompt):
        raise AssertionError("must not offer checklinks after an aborted sequence")

    monkeypatch.setattr("builtins.input", _fail_if_called)

    exit_code = run_freeze(config, config.name)

    assert exit_code == 2


def test_checklinks_offer_not_asked_with_no_built_site(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    config = _freeze_tty_config(tmp_path, monkeypatch)  # no site/ dir created
    _patch_default_steps(monkeypatch, cli)

    def _fail_if_called(prompt):
        raise AssertionError("must not offer checklinks with nothing built to scan")

    monkeypatch.setattr("builtins.input", _fail_if_called)

    run_freeze(config, config.name)


def test_checklinks_offer_not_asked_on_non_tty(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    config = _freeze_config(tmp_path)  # isatty not patched True -> non-tty
    (config.output_dir / "site").mkdir(parents=True)
    _patch_default_steps(monkeypatch, cli)

    def _fail_if_called(prompt):
        raise AssertionError("must not prompt or check links on a non-tty")

    monkeypatch.setattr("builtins.input", _fail_if_called)

    run_freeze(config, config.name)


def test_checklinks_offer_bare_enter_declines(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    config = _freeze_tty_config(tmp_path, monkeypatch)
    (config.output_dir / "site").mkdir(parents=True)
    _patch_default_steps(monkeypatch, cli)
    checklinks_calls = []
    monkeypatch.setattr(cli, "run_checklinks", lambda *a, **k: checklinks_calls.append(1) or 0)
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    run_freeze(config, config.name)

    assert checklinks_calls == []


def test_checklinks_offer_accepted_broken_links_dont_change_exit_code(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    config = _freeze_tty_config(tmp_path, monkeypatch)
    (config.output_dir / "site").mkdir(parents=True)
    _patch_default_steps(monkeypatch, cli)
    checklinks_calls = []
    monkeypatch.setattr(cli, "run_checklinks", lambda *a, **k: checklinks_calls.append(1) or 1)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    exit_code = run_freeze(config, config.name)

    assert checklinks_calls == [1]
    assert exit_code == 0  # offered checklinks' own 1 (broken links) is informational only


def test_declared_checklinks_step_returning_1_does_set_exit_code(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    config = _freeze_tty_config(tmp_path, monkeypatch, steps=("acquire", "build", "validate", "checklinks"))
    (config.output_dir / "site").mkdir(parents=True)
    _patch_default_steps(monkeypatch, cli)
    monkeypatch.setattr(cli, "run_checklinks", lambda *a, **k: 1)

    exit_code = run_freeze(config, config.name)

    assert exit_code == 1


# ---------------------------------------------------------------------------
# _offer_new_project (unknown project -> offer to start one via the wizard)
# ---------------------------------------------------------------------------


def test_unknown_project_non_tty_exits_2_without_calling_input(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    def _fail_if_called(prompt):
        raise AssertionError("must not prompt on a non-tty")

    monkeypatch.setattr("builtins.input", _fail_if_called)

    assert main(["build", "foo"]) == 2


def test_unknown_project_tty_accepted_launches_wizard_with_initial_name(tmp_path: Path, monkeypatch):
    import wpfreeze.cli as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    wizard_calls = []
    monkeypatch.setattr(
        "wpfreeze.wizard.run_wizard",
        lambda **kwargs: wizard_calls.append(kwargs) or 0,
    )

    exit_code = cli.main(["build", "foo"])

    assert exit_code == 0
    assert wizard_calls == [{"initial_name": "foo", "then_freeze": True}]


def test_unknown_project_tty_declined_exits_2(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    assert main(["build", "foo"]) == 2
