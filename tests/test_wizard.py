from pathlib import Path

import yaml

from wpfreeze.dbsetup import SetupPlan
from wpfreeze.wizard import (
    build_config_dict,
    run_wizard,
    slugify_domain,
)


def _answers(*values):
    it = iter(values)

    def ask(prompt_text: str) -> str:
        return next(it)

    return ask


def test_slugify_domain_replaces_dots():
    assert slugify_domain("https://www.site-c.example/") == "www-site-c-com"


def test_build_config_dict_no_db():
    ask = _answers(
        "https://example.com",  # base_url
        "",  # output_dir default
        "",  # rate preset default
        "",  # wayback enabled default (yes)
        "",  # prefer_snapshots_near blank
        "1",  # db: no
    )
    config_dict, suggested_path = build_config_dict(ask=ask, tell=lambda m: None)

    assert config_dict["base_url"] == "https://example.com"
    assert config_dict["output_dir"] == "./output/example-com"
    assert config_dict["rate_limit"] == 1.0
    assert config_dict["wayback"]["enabled"] is True
    assert config_dict["db"] == "none"
    assert suggested_path == Path("example-com.yaml")


def test_build_config_dict_adds_https_prefix_when_missing():
    ask = _answers("example.com", "", "", "n", "1")
    config_dict, _ = build_config_dict(ask=ask, tell=lambda m: None)
    assert config_dict["base_url"] == "https://example.com"
    assert config_dict["wayback"]["enabled"] is False


def test_build_config_dict_live_db_with_socket():
    ask = _answers(
        "https://example.com",
        "",
        "1",  # gentle preset
        "",  # wayback default yes
        "",  # snapshot date blank
        "2",  # db: live connection
        "",  # host blank -> socket path
        "mydb",  # name
        "myuser",  # user
        "",  # password_env default
        "",  # table_prefix default
        "/tmp/custom.sock",  # socket
    )
    config_dict, _ = build_config_dict(ask=ask, tell=lambda m: None)

    assert config_dict["rate_limit"] == 2.0
    db = config_dict["db"]
    assert db["name"] == "mydb"
    assert db["user"] == "myuser"
    assert db["socket"] == "/tmp/custom.sock"
    assert "host" not in db
    assert db["password_env"] == "WPFREEZE_DB_PASSWORD"
    assert db["table_prefix"] == "wp_"


def test_build_config_dict_live_db_with_host():
    ask = _answers(
        "https://example.com",
        "",
        "",
        "",
        "",
        "2",  # db: live connection
        "dbhost.internal",  # host given -> no socket question
        "mydb",
        "myuser",
        "",
        "",
    )
    config_dict, _ = build_config_dict(ask=ask, tell=lambda m: None)
    db = config_dict["db"]
    assert db["host"] == "dbhost.internal"
    assert "socket" not in db


def test_build_config_dict_dump_file_delegates_to_dbsetup(monkeypatch):
    fake_plan = SetupPlan(
        dump_path=Path("/tmp/x.sql"), db_name="wpfreeze_x", stage_user="stage", stage_password="pw"
    )
    monkeypatch.setattr("wpfreeze.dbsetup.run_setup", lambda dump_path, ask, tell: fake_plan)

    ask = _answers(
        "https://example.com",
        "",
        "",
        "",
        "",
        "3",  # db: I have a dump
        "/tmp/x.sql",
    )
    config_dict, _ = build_config_dict(ask=ask, tell=lambda m: None)

    assert config_dict["db"]["name"] == "wpfreeze_x"
    assert config_dict["db"]["user"] == "stage"


def test_run_wizard_writes_config_and_offers_dry_run(tmp_path, monkeypatch):
    from wpfreeze.cli import SiteConfig

    fake_config = SiteConfig(base_url="https://example.com/", output_dir=tmp_path / "out")
    monkeypatch.setattr("wpfreeze.cli.load_config", lambda path: fake_config)

    acquire_calls = []
    monkeypatch.setattr(
        "wpfreeze.cli.run_acquire",
        lambda config, resume, dry_run: acquire_calls.append((resume, dry_run)) or 0,
    )

    config_path = tmp_path / "example-com.yaml"
    ask = _answers(
        "https://example.com",  # base_url
        "",  # output_dir
        "",  # rate preset
        "",  # wayback enabled
        "",  # snapshot date
        "1",  # db none
        str(config_path),  # save-as path
        "y",  # dry run now
        "n",  # real run now
    )
    messages = []
    exit_code = run_wizard(ask=ask, tell=messages.append)

    assert exit_code == 0
    assert config_path.exists()
    written = yaml.safe_load(config_path.read_text())
    assert written["base_url"] == "https://example.com"
    assert acquire_calls == [(False, True)]  # only the dry run happened
    assert any("Wrote" in m for m in messages)
