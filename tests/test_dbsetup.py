import gzip
from pathlib import Path

import pytest

from wpfreeze.dbsetup import (
    RootConnectionUnavailable,
    SetupPlan,
    check_localhost_only,
    create_scoped_database_and_user,
    default_db_name_from_dump,
    import_dump,
    render_config_yaml,
    resolve_root_password,
    run_as_root,
    run_setup,
    slugify_identifier,
    to_db_dict,
    verify_import,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_slugify_identifier_strips_unsafe_characters():
    assert slugify_identifier("Landscapes of Injustice!") == "landscapes_of_injustice"


def test_slugify_identifier_empty_falls_back_to_prefix():
    assert slugify_identifier("???", prefix="wpfreeze_") == "wpfreeze_db"


def test_default_db_name_from_dump_strips_sql_suffix():
    assert default_db_name_from_dump(Path("site-export.sql")) == "wpfreeze_site_export"


def test_default_db_name_from_dump_strips_sql_gz_suffix():
    assert default_db_name_from_dump(Path("site-export.sql.gz")) == "wpfreeze_site_export"


def test_to_db_dict_uses_socket_when_set():
    plan = SetupPlan(
        dump_path=Path("d.sql"), db_name="db1", stage_user="u", stage_password="p", socket="/tmp/x.sock"
    )
    d = to_db_dict(plan)
    assert d["socket"] == "/tmp/x.sock"
    assert "host" not in d


def test_to_db_dict_uses_host_when_no_socket():
    plan = SetupPlan(
        dump_path=Path("d.sql"), db_name="db1", stage_user="u", stage_password="p",
        socket=None, host="localhost", port=3306,
    )
    d = to_db_dict(plan)
    assert d["host"] == "localhost"
    assert d["port"] == 3306
    assert "socket" not in d


def test_render_config_yaml_round_trips_through_yaml(tmp_path):
    import yaml

    plan = SetupPlan(dump_path=Path("d.sql"), db_name="db1", stage_user="u", stage_password="p")
    parsed = yaml.safe_load(render_config_yaml(plan))
    assert parsed["db"]["name"] == "db1"
    assert parsed["db"]["password_env"] == "WPFREEZE_STAGE_DB_PASSWORD"


# ---------------------------------------------------------------------------
# run_as_root / resolve_root_password / check_localhost_only
# ---------------------------------------------------------------------------


def test_run_as_root_uses_passwordless_sudo_first(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)

        class Result:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    output = run_as_root("SELECT 1;")

    assert calls[0][:2] == ["sudo", "mysql"]
    assert output == "ok\n"


def test_run_as_root_falls_back_to_defaults_file_when_sudo_fails(monkeypatch):
    """When root_password is None, a failed passwordless `sudo mysql`
    attempt falls back to a defaults-file connection with no password
    line at all -- distinct from the explicit-password case below."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["sudo", "mysql"]:
            class Failed:
                returncode = 1
                stdout = ""
                stderr = "sudo: a password is required"

            return Failed()

        defaults_arg = next(a for a in args if a.startswith("--defaults-extra-file="))
        defaults_path = Path(defaults_arg.split("=", 1)[1])
        content = defaults_path.read_text()
        assert "user=root" in content
        assert "password=" not in content

        class Result:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    output = run_as_root("SELECT 1;")

    assert len(calls) == 2
    assert output == "ok\n"


def test_run_as_root_with_explicit_password_skips_sudo_attempt(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        defaults_arg = next(a for a in args if a.startswith("--defaults-extra-file="))
        defaults_path = Path(defaults_arg.split("=", 1)[1])
        assert "password=hunter2" in defaults_path.read_text()

        class Result:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    output = run_as_root("SELECT 1;", root_password="hunter2")

    assert len(calls) == 1
    assert calls[0][:2] != ["sudo", "mysql"]
    assert output == "ok\n"


def test_run_as_root_raises_when_both_paths_fail(monkeypatch):
    def fake_run(args, **kwargs):
        class Failed:
            returncode = 1
            stdout = ""
            stderr = "access denied"

        return Failed()

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(RootConnectionUnavailable):
        run_as_root("SELECT 1;", root_password="wrong")


def test_resolve_root_password_returns_none_when_passwordless_works(monkeypatch):
    monkeypatch.setattr("wpfreeze.dbsetup.run_as_root", lambda sql, root_password=None: "1\n")
    assert resolve_root_password(None, ask_password=lambda: "should-not-be-called") is None


def test_resolve_root_password_prompts_when_passwordless_fails(monkeypatch):
    def fake_run_as_root(sql, root_password=None):
        raise RootConnectionUnavailable("nope")

    monkeypatch.setattr("wpfreeze.dbsetup.run_as_root", fake_run_as_root)
    assert resolve_root_password(None, ask_password=lambda: "typed-password") == "typed-password"


def test_resolve_root_password_returns_existing_without_prompting():
    assert resolve_root_password("already-known", ask_password=lambda: 1 / 0) == "already-known"


def test_check_localhost_only_parses_bind_address(monkeypatch):
    monkeypatch.setattr(
        "wpfreeze.dbsetup.run_as_root",
        lambda sql, root_password=None: "Variable_name\tValue\nbind_address\t127.0.0.1\n",
    )
    assert check_localhost_only() == "127.0.0.1"


def test_check_localhost_only_returns_none_on_failure(monkeypatch):
    def fake_run_as_root(sql, root_password=None):
        raise RootConnectionUnavailable("nope")

    monkeypatch.setattr("wpfreeze.dbsetup.run_as_root", fake_run_as_root)
    assert check_localhost_only() is None


# ---------------------------------------------------------------------------
# create_scoped_database_and_user
# ---------------------------------------------------------------------------


def test_create_scoped_database_and_user_issues_expected_sql(monkeypatch):
    captured = {}

    def fake_run_as_root(sql, root_password=None):
        captured["sql"] = sql
        captured["password"] = root_password
        return ""

    monkeypatch.setattr("wpfreeze.dbsetup.run_as_root", fake_run_as_root)
    plan = SetupPlan(dump_path=Path("d.sql"), db_name="mydb", stage_user="stage", stage_password="secret")
    create_scoped_database_and_user(plan, root_password="rootpw")

    assert "CREATE DATABASE `mydb`;" in captured["sql"]
    assert "CREATE USER 'stage'@'localhost' IDENTIFIED BY 'secret';" in captured["sql"]
    assert "GRANT ALL PRIVILEGES ON `mydb`.* TO 'stage'@'localhost';" in captured["sql"]
    assert captured["password"] == "rootpw"


# ---------------------------------------------------------------------------
# import_dump
# ---------------------------------------------------------------------------


def test_import_dump_plain_sql_streams_file_as_stdin(tmp_path, monkeypatch):
    dump_path = tmp_path / "export.sql"
    dump_path.write_text("CREATE TABLE t (id int);\n")

    captured = {}

    def fake_run(args, stdin=None, check=None):
        captured["args"] = args
        captured["stdin_content"] = stdin.read()
        return None

    monkeypatch.setattr("subprocess.run", fake_run)

    plan = SetupPlan(
        dump_path=dump_path, db_name="mydb", stage_user="stage", stage_password="secret", socket=None,
        host="localhost",
    )
    import_dump(plan)

    assert captured["stdin_content"] == b"CREATE TABLE t (id int);\n"
    assert captured["args"][-1] == "mydb"
    assert not any(a.startswith("secret") for a in captured["args"])


def test_import_dump_decompresses_gzip_before_importing(tmp_path, monkeypatch):
    dump_path = tmp_path / "export.sql.gz"
    with gzip.open(dump_path, "wb") as f:
        f.write(b"INSERT INTO t VALUES (1);\n")

    captured = {}

    def fake_run(args, stdin=None, check=None):
        captured["stdin_content"] = stdin.read()
        return None

    monkeypatch.setattr("subprocess.run", fake_run)

    plan = SetupPlan(dump_path=dump_path, db_name="mydb", stage_user="stage", stage_password="secret")
    import_dump(plan)

    assert captured["stdin_content"] == b"INSERT INTO t VALUES (1);\n"


def test_import_dump_cleans_up_temp_files(tmp_path, monkeypatch):
    dump_path = tmp_path / "export.sql.gz"
    with gzip.open(dump_path, "wb") as f:
        f.write(b"SELECT 1;\n")

    seen = {}

    def fake_run(args, stdin=None, check=None):
        defaults_arg = next(a for a in args if a.startswith("--defaults-extra-file="))
        seen["defaults_path"] = Path(defaults_arg.split("=", 1)[1])
        assert seen["defaults_path"].exists()
        seen["dump_path"] = Path(stdin.name)
        assert seen["dump_path"].exists()
        return None

    monkeypatch.setattr("subprocess.run", fake_run)

    plan = SetupPlan(dump_path=dump_path, db_name="mydb", stage_user="stage", stage_password="secret")
    import_dump(plan)

    assert not seen["defaults_path"].exists()
    assert not seen["dump_path"].exists()


# ---------------------------------------------------------------------------
# verify_import
# ---------------------------------------------------------------------------


def test_verify_import_parses_count(monkeypatch):
    monkeypatch.setattr(
        "wpfreeze.dbsetup.run_mysql_query", lambda db_config, sql: "COUNT(*)\n42\n"
    )
    plan = SetupPlan(dump_path=Path("d.sql"), db_name="mydb", stage_user="stage", stage_password="secret")
    assert verify_import(plan) == 42


# ---------------------------------------------------------------------------
# run_setup end-to-end (all subprocess-adjacent seams monkeypatched)
# ---------------------------------------------------------------------------


def test_run_setup_happy_path(tmp_path, monkeypatch):
    dump_path = tmp_path / "export.sql"
    dump_path.write_text("SELECT 1;\n")

    monkeypatch.setattr("wpfreeze.dbsetup.client_installed", lambda: True)
    monkeypatch.setattr("wpfreeze.dbsetup.server_appears_running", lambda: True)
    monkeypatch.setattr("wpfreeze.dbsetup.resolve_root_password", lambda existing, ask_password=None: None)
    monkeypatch.setattr("wpfreeze.dbsetup.check_localhost_only", lambda root_password=None: "127.0.0.1")

    created = {}
    monkeypatch.setattr(
        "wpfreeze.dbsetup.create_scoped_database_and_user",
        lambda plan, root_password=None: created.setdefault("plan", plan),
    )
    monkeypatch.setattr("wpfreeze.dbsetup.import_dump", lambda plan: None)
    monkeypatch.setattr("wpfreeze.dbsetup.verify_import", lambda plan: 7)

    messages = []
    plan = run_setup(dump_path=dump_path, stage_password="fixedpw", tell=messages.append)

    assert plan.db_name == "wpfreeze_export"
    assert plan.stage_password == "fixedpw"
    assert any("7 row" in m for m in messages)
    assert any("db:" in m for m in messages)


def test_run_setup_raises_for_missing_dump(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_setup(dump_path=tmp_path / "does-not-exist.sql")


def test_run_setup_prompts_before_installing_missing_client(tmp_path, monkeypatch):
    dump_path = tmp_path / "export.sql"
    dump_path.write_text("SELECT 1;\n")

    monkeypatch.setattr("wpfreeze.dbsetup.client_installed", lambda: False)
    install_calls = []
    monkeypatch.setattr(
        "wpfreeze.dbsetup.install_server",
        lambda assume_yes=False, ask=input: install_calls.append(assume_yes),
    )
    monkeypatch.setattr("wpfreeze.dbsetup.resolve_root_password", lambda existing, ask_password=None: None)
    monkeypatch.setattr("wpfreeze.dbsetup.check_localhost_only", lambda root_password=None: None)
    monkeypatch.setattr("wpfreeze.dbsetup.create_scoped_database_and_user", lambda plan, root_password=None: None)
    monkeypatch.setattr("wpfreeze.dbsetup.import_dump", lambda plan: None)
    monkeypatch.setattr("wpfreeze.dbsetup.verify_import", lambda plan: 0)

    run_setup(dump_path=dump_path, stage_password="pw", assume_yes=True, tell=lambda m: None)

    assert install_calls == [True]
