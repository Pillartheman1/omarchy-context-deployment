"""Temporary HOME for session and action tests."""

from __future__ import annotations

import contextlib
import os
import traceback
from pathlib import Path


@contextlib.contextmanager
def isolated_home(tmp: Path):
    home = tmp / "home"
    (home / ".config").mkdir(parents=True)
    state = tmp / "state"
    state.mkdir()
    keys = ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME")
    old = {key: os.environ.get(key) for key in keys}
    os.environ["HOME"] = str(home)
    os.environ["XDG_STATE_HOME"] = str(state)
    os.environ.pop("XDG_CONFIG_HOME", None)
    try:
        yield home
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_module(globals_):
    tests = [fn for name, fn in globals_.items() if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("ok", fn.__name__)
        except Exception as exc:
            failed += 1
            traceback.print_exc()
            print("FAIL", fn.__name__, exc)
    raise SystemExit(failed)
