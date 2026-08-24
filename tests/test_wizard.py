from pathlib import Path

import yaml

from wpfreeze.wizard import (
    RecommendedCommand,
    build_config_dict,
    describe_configs,
    find_resumable_configs,
    print_overview,
    run_wizard,
    scan_configs,
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


def test_build_config_dict_aggressive_preset_has_no_delay_but_wayback_stays_throttled():
    ask = _answers(
        "https://example.com",
        "",
        "3",  # aggressive preset
        "",  # wayback default yes
        "",  # snapshot date blank
        "n",  # xml_backup: no
    )
    config_dict, _ = build_config_dict(ask=ask, tell=lambda m: None)

    assert config_dict["rate_limit"] == 0.0
    # Wayback is a separate, shared, third-party service that bans
    # impolite clients regardless of how fast we go against our own site
    # -- it must never inherit the 0 from an aggressive rate_limit.
    assert config_dict["wayback_rate_limit"] == 3.0


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

    # find_resumable_configs scans the current directory by default -- pin it
    # to an empty tmp_path so this test is immune to whatever real *.yaml
    # files happen to sit in the repo's actual working directory.
    monkeypatch.chdir(tmp_path)

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


def test_run_wizard_auto_resumes_real_run_after_a_preceding_dry_run(tmp_path, monkeypatch):
    """A dry-run already writes manifest.json; saying yes to the real run
    right after must not trip run_acquire's "manifest already exists,
    pass --resume" collision guard -- that guard exists for genuinely
    separate prior runs, not the wizard's own just-completed dry-run."""
    from wpfreeze.cli import SiteConfig

    monkeypatch.chdir(tmp_path)

    fake_config = SiteConfig(base_url="https://example.com/", output_dir=tmp_path / "out")
    monkeypatch.setattr("wpfreeze.cli.load_config", lambda path: fake_config)

    acquire_calls = []
    monkeypatch.setattr(
        "wpfreeze.cli.run_acquire",
        lambda config, resume, dry_run: acquire_calls.append((resume, dry_run)) or 0,
    )

    config_path = tmp_path / "example-com.yaml"
    ask = _answers(
        "https://example.com",
        "",
        "",
        "",
        "",
        "n",  # xml_backup: no
        str(config_path),
        "y",  # dry run now
        "y",  # real run now
    )
    exit_code = run_wizard(ask=ask, tell=lambda m: None)

    assert exit_code == 0
    assert acquire_calls == [(False, True), (True, False)]  # real run resumed the dry-run's manifest


def test_run_wizard_reports_full_resume_command_on_manifest_collision(tmp_path, monkeypatch):
    """If a manifest already existed at this output_dir independently of
    this wizard session (no dry-run just ran), run_acquire's guard still
    refuses (exit code 2) -- the wizard's message must give the exact
    command to fix it, not just point at a report that was never written."""
    from wpfreeze.cli import SiteConfig

    monkeypatch.chdir(tmp_path)

    fake_config = SiteConfig(base_url="https://example.com/", output_dir=tmp_path / "out")
    monkeypatch.setattr("wpfreeze.cli.load_config", lambda path: fake_config)
    monkeypatch.setattr("wpfreeze.cli.run_acquire", lambda config, resume, dry_run: 2)

    config_path = tmp_path / "example-com.yaml"
    ask = _answers(
        "https://example.com",
        "",
        "",
        "",
        "",
        "n",  # xml_backup: no
        str(config_path),
        "n",  # dry run now: no
        "y",  # real run now
    )
    messages = []
    exit_code = run_wizard(ask=ask, tell=messages.append)

    assert exit_code == 2
    assert any(f"wpfreeze acquire --config {config_path} --resume" in m for m in messages)


# ---------------------------------------------------------------------------
# find_resumable_configs: scanning a directory for configs with a prior run
# ---------------------------------------------------------------------------


def _write_site_yaml(path: Path, base_url: str, output_dir: Path) -> Path:
    path.write_text(
        yaml.safe_dump({"base_url": base_url, "output_dir": str(output_dir)}), encoding="utf-8"
    )
    return path


def _write_fake_manifest(output_dir: Path) -> None:
    from wpfreeze.manifest import Manifest

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="sitemap")
    manifest.save(output_dir / "manifest.json")


def test_find_resumable_configs_finds_config_with_existing_manifest(tmp_path):
    site_yaml = _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")
    _write_fake_manifest(tmp_path / "out")

    candidates = find_resumable_configs(tmp_path)

    assert len(candidates) == 1
    path, config = candidates[0]
    assert path == site_yaml
    assert config.base_url == "https://example.com/"


def test_find_resumable_configs_skips_config_without_manifest(tmp_path):
    _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")
    # no manifest.json written -- nothing to resume

    assert find_resumable_configs(tmp_path) == []


def test_find_resumable_configs_skips_unparseable_yaml(tmp_path):
    (tmp_path / "not-a-config.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    (tmp_path / "also-bad.yaml").write_text("no_base_url: true\n", encoding="utf-8")

    assert find_resumable_configs(tmp_path) == []


def test_find_resumable_configs_empty_directory_returns_empty(tmp_path):
    assert find_resumable_configs(tmp_path) == []


def test_find_resumable_configs_multiple_candidates(tmp_path):
    _write_site_yaml(tmp_path / "a.yaml", "https://a.example.com/", tmp_path / "a-out")
    _write_fake_manifest(tmp_path / "a-out")
    _write_site_yaml(tmp_path / "b.yaml", "https://b.example.com/", tmp_path / "b-out")
    _write_fake_manifest(tmp_path / "b-out")

    candidates = find_resumable_configs(tmp_path)

    assert {path.name for path, _ in candidates} == {"a.yaml", "b.yaml"}


def _write_complete_manifest(output_dir: Path) -> None:
    """A manifest with no pending/retrying records -- acquisition looks
    finished, distinct from _write_fake_manifest's single pending record."""
    from wpfreeze.manifest import Manifest, Status

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest()
    record = manifest.get_or_create("https://example.com/", discovered_via="sitemap")
    record.status = Status.FETCHED.value
    manifest.save(output_dir / "manifest.json")


# ---------------------------------------------------------------------------
# scan_configs: like find_resumable_configs, but keeps every valid config
# (not just ones with a manifest) and reports what didn't parse
# ---------------------------------------------------------------------------


def test_scan_configs_includes_a_config_with_no_manifest_yet(tmp_path):
    _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")

    valid, invalid = scan_configs(tmp_path)

    assert [path.name for path, _ in valid] == ["site.yaml"]
    assert invalid == []


def test_scan_configs_reports_unparseable_yaml_instead_of_dropping_it(tmp_path):
    (tmp_path / "not-a-config.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")

    valid, invalid = scan_configs(tmp_path)

    assert valid == []
    assert [path.name for path in invalid] == ["not-a-config.yaml"]


def test_scan_configs_empty_directory(tmp_path):
    assert scan_configs(tmp_path) == ([], [])


# ---------------------------------------------------------------------------
# print_overview: bare `wpfreeze`, no stdin, no side effects
# ---------------------------------------------------------------------------


def _run_overview(tmp_path) -> list[str]:
    lines: list[str] = []
    print_overview(tmp_path, tell=lines.append)
    return lines


def test_print_overview_not_yet_acquired(tmp_path):
    _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")

    lines = _run_overview(tmp_path)
    text = "\n".join(lines)

    assert "Not yet acquired." in text
    assert "wpfreeze acquire --config site.yaml --dry-run" in text
    assert "wpfreeze acquire --config site.yaml" in text
    assert "wpfreeze status" not in text


def test_print_overview_resumable_run_suggests_resume_only(tmp_path):
    _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")
    _write_fake_manifest(tmp_path / "out")  # one pending record

    lines = _run_overview(tmp_path)
    text = "\n".join(lines)

    assert "wpfreeze acquire --config site.yaml --resume" in text
    assert "wpfreeze status" not in text
    assert "wpfreeze build" not in text


def test_print_overview_complete_not_built(tmp_path):
    _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")
    _write_complete_manifest(tmp_path / "out")

    lines = _run_overview(tmp_path)
    text = "\n".join(lines)

    assert "Built: no." in text
    assert "wpfreeze status --config site.yaml" in text
    assert "wpfreeze build" in text
    assert "wpfreeze validate" not in text
    assert "wpfreeze upload-script" not in text


def test_print_overview_built_recommends_upload_script_even_with_no_remote_configured(tmp_path):
    # upload-script's --local mode needs neither remote (see upload.py), so
    # it's worth recommending as soon as a site is built, not gated on
    # upload.remote being set the way it used to be.
    _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")
    _write_complete_manifest(tmp_path / "out")
    (tmp_path / "out" / "site").mkdir(parents=True)

    lines = _run_overview(tmp_path)
    text = "\n".join(lines)

    assert "Built: yes." in text
    assert "wpfreeze validate" in text
    assert "wpfreeze upload-script --config site.yaml" in text


def test_print_overview_built_with_upload_remote_still_recommends_upload_script(tmp_path):
    site_yaml = tmp_path / "site.yaml"
    site_yaml.write_text(
        yaml.safe_dump(
            {
                "base_url": "https://example.com/",
                "output_dir": str(tmp_path / "out"),
                "upload": {"remote": "user@host:/path"},
            }
        ),
        encoding="utf-8",
    )
    _write_complete_manifest(tmp_path / "out")
    (tmp_path / "out" / "site").mkdir(parents=True)

    lines = _run_overview(tmp_path)
    text = "\n".join(lines)

    assert "wpfreeze upload-script --config site.yaml" in text


def test_print_overview_lists_non_conformant_yaml_by_name_only(tmp_path):
    (tmp_path / "broken.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")

    lines = _run_overview(tmp_path)
    text = "\n".join(lines)

    assert "Found 1 YAML file that doesn't look like a valid site config: broken.yaml" in text


def test_print_overview_pluralizes_multiple_non_conformant_files(tmp_path):
    (tmp_path / "broken-a.yaml").write_text("- x\n", encoding="utf-8")
    (tmp_path / "broken-b.yaml").write_text("- y\n", encoding="utf-8")

    lines = _run_overview(tmp_path)
    text = "\n".join(lines)

    assert "Found 2 YAML files that don't look like a valid site config: broken-a.yaml, broken-b.yaml" in text


def test_print_overview_always_points_at_wizard_and_setup(tmp_path):
    lines = _run_overview(tmp_path)
    text = "\n".join(lines)

    assert "wpfreeze wizard" in text
    assert "SETUP.md" in text


def test_print_overview_never_reads_stdin_and_returns_zero(tmp_path):
    _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")
    exit_code = print_overview(tmp_path, tell=lambda m: None)
    assert exit_code == 0


# ---------------------------------------------------------------------------
# describe_configs / RecommendedCommand: the data both print_overview and
# wpfreeze.picker render from -- see wizard.py's own docstrings for why the
# split exists.
# ---------------------------------------------------------------------------


def test_recommended_command_argv_includes_config_and_extra_flags():
    cmd = RecommendedCommand("acquire", ("--resume",))
    assert cmd.argv("site.yaml") == ["acquire", "--config", "site.yaml", "--resume"]


def test_recommended_command_argv_with_no_extra_flags():
    cmd = RecommendedCommand("status")
    assert cmd.argv("site.yaml") == ["status", "--config", "site.yaml"]


def test_describe_configs_not_yet_acquired(tmp_path):
    _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")

    statuses, invalid = describe_configs(tmp_path)

    assert invalid == []
    assert len(statuses) == 1
    status = statuses[0]
    assert status.path.name == "site.yaml"
    assert status.status_lines == ("Not yet acquired.",)
    assert [c.argv("site.yaml") for c in status.commands] == [
        ["acquire", "--config", "site.yaml", "--dry-run"],
        ["acquire", "--config", "site.yaml"],
    ]


def test_describe_configs_resumable_run_recommends_resume_only(tmp_path):
    _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")
    _write_fake_manifest(tmp_path / "out")

    statuses, _ = describe_configs(tmp_path)

    assert [c.argv("site.yaml") for c in statuses[0].commands] == [
        ["acquire", "--config", "site.yaml", "--resume"],
    ]


def test_describe_configs_complete_recommends_status_and_build(tmp_path):
    _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")
    _write_complete_manifest(tmp_path / "out")

    statuses, _ = describe_configs(tmp_path)

    assert [c.subcommand for c in statuses[0].commands] == ["status", "build"]


def test_describe_configs_matches_print_overview_output(tmp_path):
    # Same data, two renderers -- the whole point of the refactor. If these
    # two ever disagree, the picker and the plain-text fallback would show
    # different things for the same directory.
    _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")
    _write_complete_manifest(tmp_path / "out")
    (tmp_path / "out" / "site").mkdir(parents=True)

    statuses, _ = describe_configs(tmp_path)
    lines = _run_overview(tmp_path)
    text = "\n".join(lines)

    for line in statuses[0].status_lines:
        assert line in text
    for cmd in statuses[0].commands:
        assert f"wpfreeze {cmd.subcommand}" in text
        if cmd.argv_extra:
            assert " ".join(cmd.argv_extra) in text


# ---------------------------------------------------------------------------
# run_wizard: offering to resume an existing run before the question flow
# ---------------------------------------------------------------------------


def test_run_wizard_offers_resume_and_skips_the_question_flow_on_yes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")
    _write_fake_manifest(tmp_path / "out")

    acquire_calls = []
    monkeypatch.setattr(
        "wpfreeze.cli.run_acquire",
        lambda config, resume, dry_run: acquire_calls.append((resume, dry_run)) or 0,
    )

    ask = _answers("y")  # yes, resume it -- no further question should be asked
    messages = []
    exit_code = run_wizard(ask=ask, tell=messages.append)

    assert exit_code == 0
    assert acquire_calls == [(True, False)]
    assert any("Resuming" in m for m in messages)


def test_run_wizard_multiple_candidates_lets_user_pick_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_site_yaml(tmp_path / "a.yaml", "https://a.example.com/", tmp_path / "a-out")
    _write_fake_manifest(tmp_path / "a-out")
    _write_site_yaml(tmp_path / "b.yaml", "https://b.example.com/", tmp_path / "b-out")
    _write_fake_manifest(tmp_path / "b-out")

    acquire_calls = []
    monkeypatch.setattr(
        "wpfreeze.cli.run_acquire",
        lambda config, resume, dry_run: acquire_calls.append((config.base_url, resume, dry_run)) or 0,
    )

    ask = _answers("2")  # pick the second listed candidate
    messages = []
    exit_code = run_wizard(ask=ask, tell=messages.append)

    assert exit_code == 0
    assert len(acquire_calls) == 1
    base_url, resume, dry_run = acquire_calls[0]
    assert base_url == "https://b.example.com/"
    assert resume is True
    assert dry_run is False


def test_run_wizard_multiple_candidates_none_of_these_falls_through(tmp_path, monkeypatch):
    from wpfreeze.cli import SiteConfig

    monkeypatch.chdir(tmp_path)
    _write_site_yaml(tmp_path / "a.yaml", "https://a.example.com/", tmp_path / "a-out")
    _write_fake_manifest(tmp_path / "a-out")
    _write_site_yaml(tmp_path / "b.yaml", "https://b.example.com/", tmp_path / "b-out")
    _write_fake_manifest(tmp_path / "b-out")

    fake_config = SiteConfig(base_url="https://new-site.example.com/", output_dir=tmp_path / "new-out")
    monkeypatch.setattr("wpfreeze.cli.load_config", lambda path: fake_config)
    monkeypatch.setattr("wpfreeze.cli.run_acquire", lambda config, resume, dry_run: 0)

    config_path = tmp_path / "new-site.yaml"
    ask = _answers(
        "3",  # neither a nor b -- "None of these" is the 3rd option with 2 candidates
        "https://new-site.example.com",
        "",
        "",
        "n",  # wayback: no
        "n",  # xml_backup: no
        str(config_path),
        "n",
        "n",
    )
    exit_code = run_wizard(ask=ask, tell=lambda m: None)

    assert exit_code == 0
    assert config_path.exists()


def test_run_wizard_falls_through_to_full_flow_when_resume_declined(tmp_path, monkeypatch):
    from wpfreeze.cli import SiteConfig

    monkeypatch.chdir(tmp_path)
    _write_site_yaml(tmp_path / "site.yaml", "https://example.com/", tmp_path / "out")
    _write_fake_manifest(tmp_path / "out")

    fake_config = SiteConfig(base_url="https://new-site.example.com/", output_dir=tmp_path / "new-out")
    monkeypatch.setattr("wpfreeze.cli.load_config", lambda path: fake_config)
    acquire_calls = []
    monkeypatch.setattr(
        "wpfreeze.cli.run_acquire",
        lambda config, resume, dry_run: acquire_calls.append((resume, dry_run)) or 0,
    )

    config_path = tmp_path / "new-site.yaml"
    ask = _answers(
        "n",  # decline the resume offer
        "https://new-site.example.com",  # base_url
        "",  # output_dir
        "",  # rate preset
        "n",  # wayback: no
        "n",  # xml_backup: no
        str(config_path),  # save-as path
        "n",  # dry run now
        "n",  # real run now
    )
    exit_code = run_wizard(ask=ask, tell=lambda m: None)

    assert exit_code == 0
    assert config_path.exists()  # the normal wizard flow actually ran


def test_run_wizard_no_candidates_goes_straight_to_question_flow(tmp_path, monkeypatch):
    from wpfreeze.cli import SiteConfig

    monkeypatch.chdir(tmp_path)  # empty directory -- nothing to offer

    fake_config = SiteConfig(base_url="https://example.com/", output_dir=tmp_path / "out")
    monkeypatch.setattr("wpfreeze.cli.load_config", lambda path: fake_config)
    monkeypatch.setattr("wpfreeze.cli.run_acquire", lambda config, resume, dry_run: 0)

    config_path = tmp_path / "example-com.yaml"
    ask = _answers(
        "https://example.com",  # base_url
        "",  # output_dir
        "",  # rate preset
        "n",  # wayback: no (skips the follow-up snapshot-date question)
        "n",  # xml_backup: no
        str(config_path),  # save-as path
        "n",  # dry run now
        "n",  # real run now
    )
    exit_code = run_wizard(ask=ask, tell=lambda m: None)

    assert exit_code == 0
    assert config_path.exists()
