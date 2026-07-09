import logging

import pytest


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
