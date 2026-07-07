"""Interactive (or flag-driven) one-time setup of a local, localhost-only
MariaDB database with a WordPress SQL dump imported into it.

This exists because most real acquisition targets are on hosting where the
production database is never reachable -- the only thing available is a
`.sql`/`.sql.gz` export. Rather than teaching the acquisition pipeline to
import dumps itself, this module gets you from "I have a dump file" to a
normal, persistent, scoped local database -- then wpfreeze's existing live
`db:` connection path (see wpfreeze.inventory.discover_database) is used
against it completely unchanged. inventory.py/DbConfig/discover_database
have no knowledge that a dump was ever involved.

See DB-DUMP-SETUP.md for the manual walkthrough this automates.
"""
from __future__ import annotations

import getpass
import gzip
import re
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from wpfreeze.inventory import DbConfig, run_mysql_query

_IDENT_RE = re.compile(r"[^a-zA-Z0-9_]")

DEFAULT_SOCKET = "/var/run/mysqld/mysqld.sock"
LOCALHOST_BIND_VALUES = ("127.0.0.1", "localhost", "::1")


def slugify_identifier(text: str, prefix: str = "") -> str:
    """Sanitize free text into a safe MySQL identifier (letters, digits,
    underscore only). Not a security boundary on its own -- callers still
    backtick-quote identifiers -- just keeps generated names sane."""
    cleaned = _IDENT_RE.sub("_", text).strip("_").lower()
    return f"{prefix}{cleaned}" if cleaned else f"{prefix}db"


def default_db_name_from_dump(dump_path: Path) -> str:
    stem = dump_path.name
    for suffix in (".sql.gz", ".sql"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return slugify_identifier(stem, prefix="wpfreeze_")


@dataclass
class SetupPlan:
    dump_path: Path
    db_name: str
    stage_user: str
    stage_password: str
    table_prefix: str = "wp_"
    socket: str | None = DEFAULT_SOCKET
    host: str | None = None
    port: int | None = None


class RootConnectionUnavailable(RuntimeError):
    pass


def client_installed() -> bool:
    return shutil.which("mysql") is not None


def server_appears_running() -> bool:
    for service in ("mariadb", "mysql", "mysqld"):
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            capture_output=True,
        )
        if result.returncode == 0:
            return True
    return False


def install_server(assume_yes: bool = False, ask: Callable[[str], str] = input) -> None:
    if not assume_yes:
        answer = ask(
            "mariadb-server/mariadb-client not found. Install now with "
            "`sudo apt install mariadb-server mariadb-client`? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            raise RuntimeError("mariadb-server is required; aborting setup.")
    subprocess.run(["sudo", "apt", "install", "-y", "mariadb-server", "mariadb-client"], check=True)


def start_server(assume_yes: bool = False, ask: Callable[[str], str] = input) -> None:
    if not assume_yes:
        answer = ask(
            "MariaDB isn't running. Start and enable it now with "
            "`sudo systemctl enable --now mariadb`? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            raise RuntimeError("MariaDB must be running; aborting setup.")
    subprocess.run(["sudo", "systemctl", "enable", "--now", "mariadb"], check=True)


def run_as_root(sql: str, root_password: str | None = None) -> str:
    """Run an administrative statement against the local server as root.

    Tries passwordless `sudo mysql` first (Debian/Ubuntu's default
    unix_socket auth plugin for the root user), then falls back to an
    explicit root password via a temporary --defaults-extra-file -- the same
    "credentials never touch argv" pattern as inventory.run_mysql_query.
    """
    if root_password is None:
        try:
            result = subprocess.run(
                ["sudo", "mysql", "--batch", "--raw", "-e", sql],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                return result.stdout
        except FileNotFoundError:
            pass

    defaults_lines = ["[client]", "user=root"]
    if root_password:
        defaults_lines.append(f"password={root_password}")
    fd, tmp_name = tempfile.mkstemp(prefix="wpfreeze-root-", suffix=".cnf")
    tmp_path = Path(tmp_name)
    try:
        tmp_path.chmod(0o600)
        tmp_path.write_text("\n".join(defaults_lines) + "\n", encoding="utf-8")
        result = subprocess.run(
            ["mysql", f"--defaults-extra-file={tmp_path}", "--batch", "--raw", "-e", sql],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RootConnectionUnavailable(result.stderr.strip())
        return result.stdout
    finally:
        tmp_path.unlink(missing_ok=True)


def resolve_root_password(
    existing: str | None,
    ask_password: Callable[[], str] = lambda: getpass.getpass("MariaDB root password: "),
) -> str | None:
    """Returns a usable root_password, or None if passwordless root access
    already works. Only prompts if passwordless access fails."""
    if existing is not None:
        return existing
    try:
        run_as_root("SELECT 1;", None)
        return None
    except RootConnectionUnavailable:
        return ask_password()


def check_localhost_only(root_password: str | None = None) -> str | None:
    """Best-effort read of the server's bind_address. Returns None if it
    can't be determined -- this is a warning, never a gate on setup."""
    try:
        raw = run_as_root("SHOW VARIABLES LIKE 'bind_address';", root_password)
    except (RootConnectionUnavailable, subprocess.SubprocessError):
        return None
    lines = raw.splitlines()
    if len(lines) < 2:
        return None
    return lines[1].split("\t")[-1]


def create_scoped_database_and_user(plan: SetupPlan, root_password: str | None = None) -> None:
    statements = (
        f"CREATE DATABASE `{plan.db_name}`;",
        f"CREATE USER '{plan.stage_user}'@'localhost' IDENTIFIED BY '{plan.stage_password}';",
        f"GRANT ALL PRIVILEGES ON `{plan.db_name}`.* TO '{plan.stage_user}'@'localhost';",
        "FLUSH PRIVILEGES;",
    )
    run_as_root("\n".join(statements), root_password)


def _stage_db_config(plan: SetupPlan) -> DbConfig:
    return DbConfig(
        host=plan.host,
        socket=plan.socket,
        port=plan.port,
        name=plan.db_name,
        user=plan.stage_user,
        password=plan.stage_password,
        table_prefix=plan.table_prefix,
    )


def import_dump(plan: SetupPlan) -> None:
    """Import `plan.dump_path` (.sql or .sql.gz) into the already-created
    database as the scoped staging user. Streams via a real file handle
    rather than reading the whole dump into memory as a Python string --
    unlike run_mysql_query's small inventory queries, dumps can be large."""
    db_config = _stage_db_config(plan)
    sql_path = plan.dump_path
    tmp_decompressed: Path | None = None
    try:
        if sql_path.suffix == ".gz":
            fd, tmp_name = tempfile.mkstemp(prefix="wpfreeze-dump-", suffix=".sql")
            tmp_decompressed = Path(tmp_name)
            with gzip.open(sql_path, "rb") as src, open(fd, "wb") as dst:
                shutil.copyfileobj(src, dst)
            sql_path = tmp_decompressed

        defaults_lines = ["[client]", f"user={db_config.user}"]
        if db_config.password is not None:
            defaults_lines.append(f"password={db_config.password}")
        if db_config.host:
            defaults_lines.append(f"host={db_config.host}")
        if db_config.socket:
            defaults_lines.append(f"socket={db_config.socket}")
        if db_config.port:
            defaults_lines.append(f"port={db_config.port}")

        defaults_fd, defaults_name = tempfile.mkstemp(prefix="wpfreeze-db-", suffix=".cnf")
        defaults_path = Path(defaults_name)
        try:
            defaults_path.chmod(0o600)
            defaults_path.write_text("\n".join(defaults_lines) + "\n", encoding="utf-8")
            with open(sql_path, "rb") as dump_file:
                subprocess.run(
                    ["mysql", f"--defaults-extra-file={defaults_path}", db_config.name],
                    stdin=dump_file,
                    check=True,
                )
        finally:
            defaults_path.unlink(missing_ok=True)
    finally:
        if tmp_decompressed is not None:
            tmp_decompressed.unlink(missing_ok=True)


def verify_import(plan: SetupPlan) -> int:
    db_config = _stage_db_config(plan)
    raw = run_mysql_query(db_config, f"SELECT COUNT(*) FROM {plan.table_prefix}posts;")
    lines = raw.splitlines()
    return int(lines[1]) if len(lines) > 1 else 0


def to_db_dict(plan: SetupPlan) -> dict:
    """The `db:` mapping for this plan, suitable for embedding in a site
    config (wizard.py) or standalone yaml.safe_dump (render_config_yaml)."""
    d: dict = {}
    if plan.socket:
        d["socket"] = plan.socket
    else:
        d["host"] = plan.host
        if plan.port:
            d["port"] = plan.port
    d["name"] = plan.db_name
    d["user"] = plan.stage_user
    d["password_env"] = "WPFREEZE_STAGE_DB_PASSWORD"
    d["table_prefix"] = plan.table_prefix
    return d


def render_config_yaml(plan: SetupPlan) -> str:
    return yaml.safe_dump({"db": to_db_dict(plan)}, sort_keys=False).rstrip("\n")


def run_setup(
    dump_path: Path,
    db_name: str | None = None,
    stage_user: str = "wpfreeze_stage",
    stage_password: str | None = None,
    table_prefix: str = "wp_",
    socket: str | None = DEFAULT_SOCKET,
    root_password: str | None = None,
    assume_yes: bool = False,
    ask: Callable[[str], str] = input,
    tell: Callable[[str], None] = print,
) -> SetupPlan:
    """End-to-end: check prerequisites, create the scoped db+user, import the
    dump, verify it, and return the resulting SetupPlan. Callers print or
    fold `render_config_yaml(plan)` / `to_db_dict(plan)` into a site config.
    """
    if not dump_path.exists():
        raise FileNotFoundError(f"dump file not found: {dump_path}")

    if not client_installed():
        install_server(assume_yes=assume_yes, ask=ask)
    elif not server_appears_running():
        start_server(assume_yes=assume_yes, ask=ask)

    root_password = resolve_root_password(root_password)

    bind_address = check_localhost_only(root_password)
    if bind_address and bind_address not in LOCALHOST_BIND_VALUES:
        tell(
            f"WARNING: this server's bind_address is '{bind_address}', not "
            "localhost-only. Fix this in your MariaDB config before "
            "trusting this setup as localhost-only."
        )

    plan = SetupPlan(
        dump_path=dump_path,
        db_name=db_name or default_db_name_from_dump(dump_path),
        stage_user=stage_user,
        stage_password=stage_password or secrets.token_urlsafe(24),
        table_prefix=table_prefix,
        socket=socket,
    )

    create_scoped_database_and_user(plan, root_password)
    import_dump(plan)
    count = verify_import(plan)
    tell(f"Imported dump: {count} row(s) in `{plan.table_prefix}posts`.")
    tell("")
    tell("Add this to your site config:")
    tell(render_config_yaml(plan))
    tell(f"\nexport WPFREEZE_STAGE_DB_PASSWORD={plan.stage_password!r}")
    return plan


def build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(prog="wpfreeze setup-db")
    parser.add_argument("--dump", type=Path, help="path to your .sql or .sql.gz dump file")
    parser.add_argument("--db-name")
    parser.add_argument("--stage-user", default="wpfreeze_stage")
    parser.add_argument("--stage-password")
    parser.add_argument("--table-prefix", default="wp_")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument(
        "--yes", action="store_true", help="don't prompt for install/start confirmations"
    )
    return parser


def interactive_main(argv: list[str] | None = None) -> int:
    """Entry point for `wpfreeze setup-db`."""
    args = build_arg_parser().parse_args(argv)

    dump_path = args.dump
    if dump_path is None:
        dump_path = Path(input("Path to your .sql or .sql.gz dump file: ").strip())

    try:
        run_setup(
            dump_path=dump_path,
            db_name=args.db_name,
            stage_user=args.stage_user,
            stage_password=args.stage_password,
            table_prefix=args.table_prefix,
            socket=args.socket,
            assume_yes=args.yes,
        )
    except (RuntimeError, FileNotFoundError, RootConnectionUnavailable) as exc:
        print(f"setup-db failed: {exc}")
        return 2
    return 0
