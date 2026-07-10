from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests
import yaml

from wpfreeze.cli import (
    ConfigError,
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
# main() dispatch: no subcommand -> wizard
# ---------------------------------------------------------------------------


def test_main_with_no_args_launches_wizard(monkeypatch):
    calls = []
    monkeypatch.setattr("wpfreeze.wizard.run_wizard", lambda: calls.append("wizard") or 0)
    assert main([]) == 0
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
