"""Interactive picker: what bare `wpfreeze` runs in a real terminal (see
`_dispatch` in cli.py, which falls back to `wizard.print_overview`'s plain
text when stdin/stdout isn't a TTY -- piped output, CI, scripts).

Split into two halves on purpose:

- Everything in this file up to `run_picker` is a pure state machine --
  `PickerState` in, `PickerState` (and sometimes a `Launch`) out, no
  terminal I/O anywhere. It's fully unit-testable without a real terminal,
  same as the rest of this codebase's logic.
- `run_picker` and everything below it is a thin `curses` adapter: draw
  the current state, read one key, translate it into a state-machine call,
  repeat. This part has no automated test coverage -- curses needs a real
  terminal to initialize, the same limitation this codebase already has
  for the search-results-panel JS (see the project notes) -- so keep it thin enough
  that "obviously correct by reading it" plus a manual smoke test in a
  real terminal is enough.

Both render from `wizard.describe_configs`'s data -- the same status text
and recommended commands `print_overview` prints as plain text, so the
interactive picker and the non-interactive fallback never describe a
directory differently.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from wpfreeze.wizard import ConfigStatus


@dataclass(frozen=True)
class ConfigRow:
    config_index: int


@dataclass(frozen=True)
class StatusLineRow:
    """Display-only -- one of a config's status_lines. Never selectable:
    skipped by cursor movement and excluded from the numbered list."""

    config_index: int
    text: str


@dataclass(frozen=True)
class CommandRow:
    config_index: int
    command_index: int


@dataclass(frozen=True)
class StartNewSiteRow:
    """The trailing "Starting a new site..." entry -- a selectable row
    like any config or command, not a special-cased footer line."""


Row = ConfigRow | StatusLineRow | CommandRow | StartNewSiteRow


@dataclass(frozen=True)
class Launch:
    """What to do once the picker exits because a command row (or the
    "Starting a new site..." row) was activated. `run_picker` tears the
    curses screen down before acting on this -- both `wpfreeze.wizard.
    run_wizard` and a launched subcommand read stdin/print normally, same
    as if they'd been typed directly."""

    kind: str  # "command" or "wizard"
    argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class PickerState:
    statuses: tuple[ConfigStatus, ...]
    invalid: tuple[Path, ...]
    expanded: int | None = None  # index into `statuses` currently expanded, or None
    cursor: int = 0  # index into selectable_rows(state), not flatten_rows(state)


def flatten_rows(state: PickerState) -> list[Row]:
    """Every row to display, in display order -- selectable and not.
    Accordion-style: at most one config's status/commands are expanded at
    a time (state.expanded), matching a single cursor/number list rather
    than a tree with independent branches."""
    rows: list[Row] = []
    for i, status in enumerate(state.statuses):
        rows.append(ConfigRow(i))
        if state.expanded == i:
            for line in status.status_lines:
                rows.append(StatusLineRow(i, line))
            for j in range(len(status.commands)):
                rows.append(CommandRow(i, j))
    rows.append(StartNewSiteRow())
    return rows


def selectable_rows(state: PickerState) -> list[Row]:
    """flatten_rows minus StatusLineRow -- what the cursor/number selector
    actually moves across."""
    return [r for r in flatten_rows(state) if not isinstance(r, StatusLineRow)]


def cursor_row(state: PickerState) -> Row:
    rows = selectable_rows(state)
    return rows[max(0, min(state.cursor, len(rows) - 1))]


def move_cursor(state: PickerState, delta: int) -> PickerState:
    rows = selectable_rows(state)
    if not rows:
        return state
    new_cursor = max(0, min(len(rows) - 1, state.cursor + delta))
    return replace(state, cursor=new_cursor)


def jump_to(state: PickerState, one_based_index: int) -> PickerState:
    """Move the cursor to the Nth currently-visible selectable row (1-based,
    matching the numbers `run_picker` draws next to each row) -- a no-op if
    out of range for however many rows are visible right now."""
    rows = selectable_rows(state)
    idx = one_based_index - 1
    if 0 <= idx < len(rows):
        return replace(state, cursor=idx)
    return state


def collapse(state: PickerState) -> PickerState:
    """Esc/Left: collapse whatever's expanded. If the cursor was sitting on
    one of the collapsing config's own status/command rows, it moves back
    to that config's own row rather than landing on whatever row happens
    to occupy its old numeric position afterward."""
    if state.expanded is None:
        return state
    expanded = state.expanded
    current = cursor_row(state)
    collapsed = replace(state, expanded=None)
    if isinstance(current, CommandRow) and current.config_index == expanded:
        for idx, row in enumerate(selectable_rows(collapsed)):
            if isinstance(row, ConfigRow) and row.config_index == expanded:
                return replace(collapsed, cursor=idx)
    return replace(collapsed, cursor=min(state.cursor, len(selectable_rows(collapsed)) - 1))


def activate(state: PickerState) -> tuple[PickerState, Launch | None]:
    """Enter (or a number key) on the current row. A ConfigRow toggles its
    own expansion in place -- collapsing it if it's already the expanded
    one, matching `collapse`'s own cursor-homing behavior when doing so.
    A CommandRow or the StartNewSiteRow returns a Launch instead of a new
    state; the caller (run_picker) is expected to stop the loop and act on
    it rather than keep rendering."""
    row = cursor_row(state)
    if isinstance(row, ConfigRow):
        if state.expanded == row.config_index:
            return collapse(state), None
        return replace(state, expanded=row.config_index), None
    if isinstance(row, CommandRow):
        status = state.statuses[row.config_index]
        cmd = status.commands[row.command_index]
        return state, Launch("command", tuple(cmd.argv(status.config.name)))
    if isinstance(row, StartNewSiteRow):
        return state, Launch("wizard")
    return state, None  # StatusLineRow can't be the cursor row; unreachable


# --- curses adapter: no automated test coverage past this point, see the ---
# --- module docstring for why.                                          ---


def _row_text(state: PickerState, row: Row) -> str:
    if isinstance(row, ConfigRow):
        status = state.statuses[row.config_index]
        return f"{status.config.name}  ({status.config.base_url}, {status.path.name})"
    if isinstance(row, StatusLineRow):
        return row.text
    if isinstance(row, CommandRow):
        status = state.statuses[row.config_index]
        cmd = status.commands[row.command_index]
        extra = f" {' '.join(cmd.argv_extra)}" if cmd.argv_extra else ""
        return f"wpfreeze {cmd.subcommand} {status.config.name}{extra}{cmd.note}"
    return "Starting a new site... (run `wpfreeze wizard`)"


def _row_indent(row: Row) -> int:
    if isinstance(row, (StatusLineRow, CommandRow)):
        return 4
    return 0


def _render(stdscr, state: PickerState) -> None:
    import curses

    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()
    rows = flatten_rows(state)
    current = cursor_row(state)

    stdscr.addstr(0, 0, "wpfreeze -- static-archive WordPress sites"[: max_x - 1])
    y = 2
    number = 0
    for row in rows:
        if y >= max_y - 2:
            break
        selectable = not isinstance(row, StatusLineRow)
        if selectable:
            number += 1
        indent = _row_indent(row)
        prefix = f"{number:>2}. " if selectable else "    "
        text = f"{prefix}{_row_text(state, row)}"
        attr = curses.A_REVERSE if selectable and row == current else curses.A_NORMAL
        stdscr.addstr(y, indent, text[: max(0, max_x - indent - 1)], attr)
        y += 1

    if state.invalid and y < max_y - 2:
        names = ", ".join(p.name for p in state.invalid)
        stdscr.addstr(y, 0, f"(not shown: {len(state.invalid)} YAML file(s) that don't parse: {names})"[: max_x - 1])

    stdscr.addstr(max_y - 1, 0, "up/down or 1-9 move, enter select, esc back, q quit"[: max_x - 1])
    stdscr.refresh()


def _run_loop(stdscr, state: PickerState) -> Launch | None:
    import curses

    curses.curs_set(0)
    stdscr.keypad(True)
    while True:
        _render(stdscr, state)
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            state = move_cursor(state, -1)
        elif key in (curses.KEY_DOWN, ord("j")):
            state = move_cursor(state, 1)
        elif ord("1") <= key <= ord("9"):
            state = jump_to(state, key - ord("0"))
            state, launch = activate(state)
            if launch is not None:
                return launch
        elif key in (curses.KEY_ENTER, 10, 13):
            state, launch = activate(state)
            if launch is not None:
                return launch
        elif key in (curses.KEY_LEFT, 27):
            state = collapse(state)
        elif key in (ord("q"), ord("Q")):
            return None


def run_picker(directory: Path = Path(".")) -> int:
    """Bare `wpfreeze` in a real terminal -- see the module docstring.
    Builds the state from `wizard.describe_configs`, runs the curses loop,
    then -- once curses has torn itself down (`curses.wrapper` guarantees
    this even if the loop raises) -- acts on whatever was launched, if
    anything, with a normal terminal back in the caller's hands.
    """
    import curses

    from wpfreeze.wizard import describe_configs

    statuses, invalid = describe_configs(directory)
    state = PickerState(tuple(statuses), tuple(invalid))
    launch = curses.wrapper(_run_loop, state)

    if launch is None:
        return 0
    if launch.kind == "wizard":
        from wpfreeze.wizard import run_wizard

        return run_wizard()

    from wpfreeze.cli import main as cli_main

    return cli_main(list(launch.argv))
