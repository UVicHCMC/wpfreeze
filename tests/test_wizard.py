from pathlib import Path

import yaml

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


def test_build_config_dict_no_xml_backup():
    ask = _answers(
        "https://example.com",  # base_url
        "",  # output_dir default
        "",  # rate preset default
        "",  # wayback enabled default (yes)
        "",  # prefer_snapshots_near blank
        "n",  # xml_backup: no
    )
    config_dict, suggested_path = build_config_dict(ask=ask, tell=lambda m: None)

    assert config_dict["base_url"] == "https://example.com"
    assert config_dict["output_dir"] == "./output/example-com"
    assert config_dict["rate_limit"] == 1.0
    assert config_dict["wayback"]["enabled"] is True
    assert "xml_backup" not in config_dict
    assert suggested_path == Path("example-com.yaml")


def test_build_config_dict_adds_https_prefix_when_missing():
    ask = _answers("example.com", "", "", "n", "n")
    config_dict, _ = build_config_dict(ask=ask, tell=lambda m: None)
    assert config_dict["base_url"] == "https://example.com"
    assert config_dict["wayback"]["enabled"] is False
    assert "xml_backup" not in config_dict


def test_build_config_dict_with_xml_backup():
    ask = _answers(
        "https://example.com",
        "",
        "1",  # gentle preset
        "",  # wayback default yes
        "",  # snapshot date blank
        "y",  # xml_backup: yes
        "/tmp/export.WordPress.xml",  # path
    )
    config_dict, _ = build_config_dict(ask=ask, tell=lambda m: None)

    assert config_dict["rate_limit"] == 2.0
    assert config_dict["xml_backup"] == "/tmp/export.WordPress.xml"


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
        "n",  # xml_backup: no
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
