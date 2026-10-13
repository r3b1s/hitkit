"""Shared fixtures for hitkit's unit tests."""

from __future__ import annotations

import os
import pwd

import pytest

from hitkit.paths import UserContext


@pytest.fixture
def user(tmp_path, monkeypatch) -> UserContext:
    """A UserContext whose home is tmp_path, so every derived path lands under it.

    XDG overrides are removed so config/data/cache paths derive from ``home``.
    """
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    return UserContext(
        name=pwd.getpwuid(os.getuid()).pw_name,
        uid=os.getuid(),
        gid=os.getgid(),
        home=tmp_path,
    )


@pytest.fixture(autouse=True)
def _reset_trace():
    """Tracing is process-global; give every test a clean slate (no handlers, no secrets)."""
    import logging

    from hitkit import trace

    yield
    root = logging.getLogger(trace.ROOT)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    trace._known.clear()
    trace._state.update(trace="", cmd="")
