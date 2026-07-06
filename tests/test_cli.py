from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests
import yaml

from wpfreeze.cli import (
    ConfigError,
    DB_MISSING_MESSAGE,
    load_config,
    main,
    probe_site,
    run_acquire,
    run_report,
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


def test_load_config_missing_db_key_refuses(tmp_path: Path):
    path = _write_yaml(tmp_path / "site.yaml", {"base_url": "https://example.com/", "output_dir": "out"})
    with pytest.raises(ConfigError) as exc_info:
        load_config(path)
    assert str(exc_info.value) == DB_MISSING_MESSAGE


def test_load_config_db_none_proceeds(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {"base_url": "https://example.com/", "output_dir": "out", "db": "none"},
    )
    config = load_config(path)
    assert config.db is None


def test_load_config_db_block_with_password_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_DB_PASSWORD", "hunter2")
    path = _write_yaml(
        tmp_path / "site.yaml",
        {
            "base_url": "https://example.com/",
            "output_dir": "out",
            "db": {
                "host": "localhost",
                "name": "wpdb",
                "user": "wpuser",
                "password_env": "TEST_DB_PASSWORD",
                "table_prefix": "wp_",
            },
        },
    )
    config = load_config(path)
    assert config.db is not None
    assert config.db.password == "hunter2"
    assert config.db.host == "localhost"


def test_load_config_defaults():
    pass  # covered implicitly below; kept as a placeholder for clarity


def test_load_config_applies_defaults(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {"base_url": "https://example.com", "output_dir": "out", "db": "none"},
    )
    config = load_config(path)
    assert config.base_url == "https://example.com/"
    assert config.rate_limit == 1.0
    assert config.wayback_rate_limit == 3.0
    assert config.concurrency == 2
    assert config.exclusions == []
    assert config.wayback.enabled is True


def test_load_config_wayback_settings(tmp_path: Path):
    path = _write_yaml(
        tmp_path / "site.yaml",
        {
            "base_url": "https://example.com/",
            "output_dir": "out",
            "db": "none",
            "wayback": {"enabled": False, "prefer_snapshots_near": "2022-06-15"},
        },
    )
    config = load_config(path)
    assert config.wayback.enabled is False
    from datetime import date

    assert config.wayback.prefer_snapshots_near == date(2022, 6, 15)


def test_example_site_yaml_parses():
    repo_root = Path(__file__).resolve().parent.parent
    config = load_config(repo_root / "example-site.yaml")
    assert config.db is not None
    assert config.db.table_prefix == "wp_"


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
        db=None,
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

    config = SiteConfig(base_url="https://example.com/", output_dir=tmp_path / "nope", db=None)
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
                "db": "none",
            },
        )
        exit_code = main(["acquire", "--config", str(config_path)])
        assert exit_code == 1

        log_files = list((output_dir / "logs").glob("*.log"))
        assert len(log_files) == 1
        content = log_files[0].read_text()
        assert "fetched" in content.lower()


def test_run_status_reports_counts(tmp_path: Path, capsys):
    with FixtureSite() as site:
        config = _config_for(site, tmp_path / "out")
        run_acquire(config, resume=False, dry_run=False)
        exit_code = run_status(config)
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Total records" in captured.out
        assert "fetched" in captured.out
