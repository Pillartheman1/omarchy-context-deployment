#!/usr/bin/env python3
"""run-action.sh sends one request and will not launch without a snapshot."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from isolate import run_module  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "PREVIOUS-SECRET-REQUEST"
RUN = ROOT / "scripts" / "run-action.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _stubs(bin_dir: Path, launched: Path) -> None:
    bin_dir.mkdir()
    _write_executable(bin_dir / "omarchy", "#!/bin/sh\nexit 0\n")
    _write_executable(bin_dir / "omarchy-default-agent", "#!/bin/sh\necho claude\n")
    _write_executable(bin_dir / "omarchy-cmd-missing", "#!/bin/sh\nexit 1\n")
    _write_executable(
        bin_dir / "omarchy-agent",
        f"#!/bin/sh\nprintf '%s\\n' launched >> '{launched}'\nexit 0\n",
    )


def _env(home: Path, state: Path, bin_dir: Path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_STATE_HOME"] = str(state)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    return env


def _context(path: Path, shot: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"crop-bytes")
    payload = {
        "kind": "desktop",
        "caption": "Desktop",
        "lastPrompt": PREVIOUS,
        "screenshot": str(shot),
        "actions": [
            {"actionId": "edit", "label": "Edit", "hint": "Change the desktop"},
            {
                "actionId": "continue",
                "label": "Continue",
                "hint": f"Follow up on: {PREVIOUS}",
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _kill_watcher(state: Path) -> None:
    pid_path = state / "omarchy" / "desktop-context" / "watch.pid"
    if not pid_path.is_file():
        return
    try:
        os.kill(int(pid_path.read_text(encoding="utf-8").strip()), signal.SIGTERM)
    except (OSError, ValueError):
        pass


def _run(args, env):
    return subprocess.run(
        [str(RUN), *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_fresh_actions_omit_previous_request_and_keep_screenshot():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home"
        state = root / "state"
        (home / ".config").mkdir(parents=True)
        bin_dir = root / "bin"
        launched = root / "launched"
        _stubs(bin_dir, launched)
        shot = root / "captures" / "one" / "surroundings.png"
        context = root / "captures" / "one" / "context.json"
        _context(context, shot)
        env = _env(home, state, bin_dir)
        prompt = state / "omarchy" / "desktop-context" / "prompt.md"

        edit = _run(["edit", "make the bar blue", str(context)], env)
        assert edit.returncode != 0, edit.stderr
        assert not launched.exists()
        edit_text = prompt.read_text(encoding="utf-8")
        assert "make the bar blue" in edit_text
        assert PREVIOUS not in edit_text
        assert "Follow up on:" not in edit_text
        assert str(shot) in edit_text
        assert shot.read_bytes() == b"crop-bytes"

        inspect = _run(["inspect", str(context)], env)
        assert inspect.returncode == 0, inspect.stderr
        assert launched.is_file()
        inspect_text = prompt.read_text(encoding="utf-8")
        assert PREVIOUS not in inspect_text
        assert "What is this?" in inspect_text
        assert shot.read_bytes() == b"crop-bytes"

        last = {
            "prompt": PREVIOUS,
            "caption": "Status bar",
            "lastMode": "edit",
            "canContinue": True,
            "canUndo": False,
        }
        last_path = state / "omarchy" / "desktop-context" / "last.json"
        last_path.parent.mkdir(parents=True, exist_ok=True)
        last_path.write_text(json.dumps(last), encoding="utf-8")
        launched.unlink()
        follow = _run(["continue", "now make it green", str(context)], env)
        assert follow.returncode != 0, follow.stderr
        assert not launched.exists()
        follow_text = prompt.read_text(encoding="utf-8")
        assert follow_text.count(PREVIOUS) == 1
        assert "now make it green" in follow_text
        assert shot.read_bytes() == b"crop-bytes"


def test_edit_launches_only_after_snapshot_succeeds():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home"
        state = root / "state"
        config = home / ".config" / "hypr"
        config.mkdir(parents=True)
        (config / "hyprland.conf").write_text("general { }\n", encoding="utf-8")
        bin_dir = root / "bin"
        launched = root / "launched"
        _stubs(bin_dir, launched)
        shot = root / "captures" / "two" / "surroundings.png"
        context = root / "captures" / "two" / "context.json"
        _context(context, shot)
        env = _env(home, state, bin_dir)
        try:
            result = _run(["edit", "only this request", str(context)], env)
            assert result.returncode == 0, result.stderr
            assert launched.is_file()
            prompt = (state / "omarchy" / "desktop-context" / "prompt.md").read_text(encoding="utf-8")
            assert "only this request" in prompt
            assert PREVIOUS not in prompt
            assert shot.read_bytes() == b"crop-bytes"
            snap = state / "omarchy" / "desktop-context" / "undo" / "snapshot"
            assert (snap / "files" / "hypr" / "hyprland.conf").is_file()
            assert (snap / "meta" / "manifest.json").is_file()
        finally:
            _kill_watcher(state)


if __name__ == "__main__":
    run_module(globals())
