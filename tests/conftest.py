import inspect
import logging
import os
import sys
import types
import warnings

import pytest

# Imported here, at collection time, rather than left to whichever test
# first reaches one of wpfreeze's deferred imports: requests (pulled in by
# wpfreeze.cli) installs warning filters when it is imported, and the guard
# below snapshots warnings.filters. A first import inside a test body
# therefore reads as "this test mutated process state and did not restore
# it" -- which is how tests/test_projects.py and tests/test_wizard.py
# failed the guard when either file was run on its own, while passing in a
# full-suite run where something earlier had already imported cli. Doing it
# up front puts the side effect before any snapshot is taken.
import wpfreeze.cli  # noqa: F401
import wpfreeze.wizard  # noqa: F401


@pytest.fixture(autouse=True)
def _restore_wpfreeze_logger_state():
    """_configure_logging (cli.py) mutates the process-wide "wpfreeze"
    logger -- handlers, level, propagate -- and nothing resets it between
    tests. Left alone, one test's call into the CLI (e.g. via main())
    permanently sets propagate=False, which silently breaks any later
    test that relies on caplog (caplog's handler sits on the root logger
    and only sees records that propagate that far)."""
    logger = logging.getLogger("wpfreeze")
    orig_handlers = list(logger.handlers)
    orig_level = logger.level
    orig_propagate = logger.propagate
    yield
    logger.handlers[:] = orig_handlers
    logger.setLevel(orig_level)
    logger.propagate = orig_propagate


# ---------------------------------------------------------------------------
# Global-state leak guard
#
# tests/test_cli.py's _patch_step_module once used a bare setattr() to stub
# run_acquire/run_build/... on the shared wpfreeze.cli module. A bare setattr
# has no teardown, so the stub outlived the test that installed it and any
# later test importing that name silently ran against the fake -- for a while,
# nondeterministically, until something happened to overwrite the same name.
# It was found only by accident, so this guard makes the whole bug class loud:
# snapshot the process-wide state a test could plausibly corrupt, then compare
# after teardown (i.e. after monkeypatch has undone its own work) and fail the
# test that left something behind.
# ---------------------------------------------------------------------------

_SNAPSHOT_ATTR = "_wpfreeze_state_snapshot"
_CONTAINERS = (list, dict, set, frozenset, tuple)


def _module_state_snapshot() -> dict[tuple[str, str], tuple[int, str | None]]:
    """Identity (and, for containers, contents) of everything a test could
    rebind or mutate in place, across every imported wpfreeze module."""
    snapshot: dict[tuple[str, str], tuple[int, str | None]] = {}
    for modname in list(sys.modules):
        if modname != "wpfreeze" and not modname.startswith("wpfreeze."):
            continue
        module = sys.modules[modname]
        if module is None:
            continue
        try:
            attrs = list(vars(module))
        except TypeError:
            continue
        for attr in attrs:
            if attr.startswith("__"):
                continue
            try:
                value = getattr(module, attr)
            except Exception:
                continue
            # id() alone catches rebinding; a repr catches a module-level
            # container that was mutated in place without being rebound.
            contents = None
            if isinstance(value, _CONTAINERS):
                try:
                    rendered = repr(value)
                except Exception:
                    rendered = "<unreprable>"
                contents = rendered if len(rendered) < 4000 else f"<{len(rendered)} chars>"
            snapshot[(modname, attr)] = (id(value), contents)

            # Setting an attribute *on* a class does not change the module
            # attribute's identity, so descend one level into our own classes.
            if inspect.isclass(value) and getattr(value, "__module__", "").startswith("wpfreeze"):
                for class_attr in list(vars(value)):
                    if class_attr.startswith("__"):
                        continue
                    # Read the raw __dict__ entry rather than using getattr: a
                    # classmethod fetched via getattr builds a fresh bound-method
                    # object every time, so its id() would always differ. A real
                    # mutation still lands in vars(), so nothing is hidden.
                    try:
                        class_value = vars(value)[class_attr]
                    except Exception:
                        continue
                    snapshot[(modname, f"{attr}.{class_attr}")] = (id(class_value), None)

    root = logging.getLogger()
    wpfreeze_logger = logging.getLogger("wpfreeze")
    snapshot[("<process>", "root_logger")] = (
        0,
        repr(([type(h).__name__ for h in root.handlers], root.level)),
    )
    snapshot[("<process>", "wpfreeze_logger")] = (
        0,
        repr(
            (
                [type(h).__name__ for h in wpfreeze_logger.handlers],
                wpfreeze_logger.level,
                wpfreeze_logger.propagate,
            )
        ),
    )
    snapshot[("<process>", "warning_filters")] = (0, repr(warnings.filters))
    snapshot[("<process>", "cwd")] = (0, repr(os.getcwd()))
    snapshot[("<process>", "environ")] = (0, repr(sorted(os.environ.items())))
    snapshot[("<process>", "sys_path")] = (0, repr(sys.path))
    return snapshot


def _describe_drift(before: dict, after: dict) -> list[str]:
    # A module imported for the first time during the test shows up as a wall
    # of new attributes; that is a lazy import, not a leak.
    known_modules = {modname for modname, _ in before}
    drift: list[str] = []
    for key in sorted(set(before) | set(after)):
        was, now = before.get(key), after.get(key)
        if was == now:
            continue
        modname, attr = key
        if modname not in known_modules:
            continue
        if was is None:
            # Importing wpfreeze.wizard binds `wizard` as an attribute of the
            # already-known parent package, so a lazy submodule import slips
            # past the known_modules filter above and gets reported as a leak.
            # Only shows up when a subset of the suite runs (a full run has
            # usually imported everything already), which is exactly when it
            # is most annoying: `pytest tests/test_cli.py` errored on it.
            if isinstance(getattr(sys.modules.get(modname), attr, None), types.ModuleType):
                continue
            drift.append(f"{modname}.{attr} was added")
        elif now is None:
            drift.append(f"{modname}.{attr} was deleted")
        elif was[0] != now[0]:
            drift.append(f"{modname}.{attr} was rebound")
        else:
            drift.append(f"{modname}.{attr} was mutated in place")
    return drift


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    setattr(item, _SNAPSHOT_ATTR, _module_state_snapshot())
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    result = yield
    # Post-yield, so every fixture -- monkeypatch included -- has finalised.
    before = getattr(item, _SNAPSHOT_ATTR, None)
    if before is None:
        return result
    delattr(item, _SNAPSHOT_ATTR)
    drift = _describe_drift(before, _module_state_snapshot())
    if drift:
        listed = "\n  ".join(drift)
        raise AssertionError(
            "this test left global state modified after teardown:\n  "
            f"{listed}\n\n"
            "Something mutated shared state without restoring it -- most often a "
            "bare setattr() on a module or class where monkeypatch.setattr() was "
            "meant. Use the monkeypatch fixture so the change is undone."
        )
    return result
