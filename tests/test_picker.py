"""Unit tests for wpfreeze.picker's pure state machine only -- flatten_rows,
selectable_rows, move_cursor, jump_to, activate, collapse. No curses/terminal
involved anywhere here; see picker.py's own module docstring for why the
curses adapter (run_picker, _run_loop, _render) has no automated coverage.
"""
from __future__ import annotations

from pathlib import Path

from wpfreeze.picker import (
    CommandRow,
    ConfigRow,
    Launch,
    PickerState,
    StartNewSiteRow,
    StatusLineRow,
    activate,
    collapse,
    cursor_row,
    flatten_rows,
    jump_to,
    move_cursor,
    selectable_rows,
)
from wpfreeze.wizard import ConfigStatus, RecommendedCommand


class _FakeConfig:
    def __init__(self, base_url: str, name: str):
        self.base_url = base_url
        self.name = name


def _status(name: str, *, status_line: str = "Not yet acquired.", commands=None) -> ConfigStatus:
    commands = commands if commands is not None else [RecommendedCommand("acquire", ("--dry-run",))]
    return ConfigStatus(
        path=Path(name),
        config=_FakeConfig(f"https://{name}.example.com/", name),
        status_lines=(status_line,),
        commands=tuple(commands),
    )


def _state(*names: str, expanded: int | None = None, cursor: int = 0) -> PickerState:
    return PickerState(
        statuses=tuple(_status(n) for n in names),
        invalid=(),
        expanded=expanded,
        cursor=cursor,
    )


# --- flatten_rows / selectable_rows -----------------------------------------


def test_flatten_rows_empty_directory_is_just_start_new_site():
    state = _state()
    assert flatten_rows(state) == [StartNewSiteRow()]
    assert selectable_rows(state) == [StartNewSiteRow()]


def test_flatten_rows_collapsed_configs_show_only_the_config_row():
    state = _state("a", "b")
    assert flatten_rows(state) == [ConfigRow(0), ConfigRow(1), StartNewSiteRow()]


def test_flatten_rows_expanded_config_shows_status_lines_and_commands():
    commands = [RecommendedCommand("acquire", ("--dry-run",)), RecommendedCommand("acquire")]
    state = PickerState(
        statuses=(_status("a", status_line="Not yet acquired.", commands=commands),),
        invalid=(),
        expanded=0,
    )
    assert flatten_rows(state) == [
        ConfigRow(0),
        StatusLineRow(0, "Not yet acquired."),
        CommandRow(0, 0),
        CommandRow(0, 1),
        StartNewSiteRow(),
    ]


def test_selectable_rows_excludes_status_lines():
    state = _state("a", expanded=0)
    assert all(not isinstance(r, StatusLineRow) for r in selectable_rows(state))
    assert any(isinstance(r, StatusLineRow) for r in flatten_rows(state))


# --- move_cursor / jump_to ---------------------------------------------------


def test_move_cursor_down_and_up():
    state = _state("a", "b")  # rows: ConfigRow(0), ConfigRow(1), StartNewSiteRow()
    state = move_cursor(state, 1)
    assert cursor_row(state) == ConfigRow(1)
    state = move_cursor(state, 1)
    assert cursor_row(state) == StartNewSiteRow()
    state = move_cursor(state, -1)
    assert cursor_row(state) == ConfigRow(1)


def test_move_cursor_clamps_at_both_ends():
    state = _state("a")
    state = move_cursor(state, -5)
    assert state.cursor == 0
    state = move_cursor(state, 5)
    assert cursor_row(state) == StartNewSiteRow()
    state = move_cursor(state, 5)  # already at the end -- stays put
    assert cursor_row(state) == StartNewSiteRow()


def test_move_cursor_on_empty_state_is_a_no_op():
    state = PickerState(statuses=(), invalid=())
    assert move_cursor(state, 1).cursor == 0


def test_move_cursor_skips_status_lines_when_a_config_is_expanded():
    state = _state("a", expanded=0)  # rows: ConfigRow(0), StatusLineRow, CommandRow(0,0), StartNewSiteRow
    state = move_cursor(state, 1)
    assert cursor_row(state) == CommandRow(0, 0)


def test_jump_to_moves_cursor_to_the_nth_selectable_row():
    state = _state("a", "b")
    state = jump_to(state, 2)
    assert cursor_row(state) == ConfigRow(1)


def test_jump_to_out_of_range_is_a_no_op():
    state = _state("a")
    original = state
    assert jump_to(state, 99) == original
    assert jump_to(state, 0) == original


# --- activate: ConfigRow toggles expansion -----------------------------------


def test_activate_config_row_expands_it():
    state = _state("a")
    state, launch = activate(state)
    assert launch is None
    assert state.expanded == 0


def test_activate_expanded_config_row_collapses_it():
    state = _state("a", expanded=0)
    state, launch = activate(state)
    assert launch is None
    assert state.expanded is None


def test_activate_expanding_one_config_replaces_any_other_expanded_one():
    # rows: ConfigRow(0), StatusLineRow, CommandRow(0,0), ConfigRow(1), StartNewSiteRow
    # selectable: ConfigRow(0), CommandRow(0,0), ConfigRow(1), StartNewSiteRow
    state = _state("a", "b", expanded=0)
    state = jump_to(state, 3)  # b's ConfigRow -- a's status/command are still showing
    assert cursor_row(state) == ConfigRow(1)
    state, launch = activate(state)
    assert launch is None
    assert state.expanded == 1


# --- activate: CommandRow / StartNewSiteRow launch ---------------------------


def test_activate_command_row_returns_a_launch_with_full_argv():
    # selectable: ConfigRow(0), CommandRow(0,0), StartNewSiteRow  <- cursor=1 here
    commands = [RecommendedCommand("acquire", ("--resume",))]
    state = PickerState(
        statuses=(_status("site.yaml", status_line="Acquired: ...", commands=commands),),
        invalid=(),
        expanded=0,
        cursor=1,
    )
    state, launch = activate(state)
    assert launch == Launch("command", ("acquire", "site.yaml", "--resume"))


def test_activate_start_new_site_row_returns_a_wizard_launch():
    state = _state("a")
    state = move_cursor(state, 1)  # ConfigRow(0), StartNewSiteRow <- here
    assert cursor_row(state) == StartNewSiteRow()
    state, launch = activate(state)
    assert launch == Launch("wizard")


def test_activate_on_empty_directory_launches_wizard_directly():
    state = PickerState(statuses=(), invalid=())
    state, launch = activate(state)
    assert launch == Launch("wizard")


# --- collapse -----------------------------------------------------------------


def test_collapse_when_nothing_expanded_is_a_no_op():
    state = _state("a")
    assert collapse(state) == state


def test_collapse_moves_cursor_back_to_the_config_row_from_a_command_row():
    # rows: ConfigRow(0), StatusLineRow, CommandRow(0,0), StartNewSiteRow
    # selectable: ConfigRow(0), CommandRow(0,0), StartNewSiteRow  <- cursor=1 here
    commands = [RecommendedCommand("acquire", ("--dry-run",))]
    state = PickerState(
        statuses=(_status("a", commands=commands),),
        invalid=(),
        expanded=0,
        cursor=1,
    )
    assert cursor_row(state) == CommandRow(0, 0)
    collapsed = collapse(state)
    assert collapsed.expanded is None
    assert cursor_row(collapsed) == ConfigRow(0)


def test_collapse_leaves_cursor_on_an_unrelated_row_untouched():
    state = _state("a", "b", expanded=0)
    state = move_cursor(state, 100)  # push cursor to StartNewSiteRow, past a's commands
    assert cursor_row(state) == StartNewSiteRow()
    collapsed = collapse(state)
    assert cursor_row(collapsed) == StartNewSiteRow()
