#!/usr/bin/env python3
"""Click context, workspace filtering, and per-interaction captures."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from context import (  # noqa: E402
    allocate_capture,
    context_for_agent,
    current_workspace_id,
    discard_capture,
    window_at,
)
from isolate import run_module  # noqa: E402


PREVIOUS = "PREVIOUS-SECRET-REQUEST"


def _client(class_name, title, workspace, at, size, *, floating=False, pinned=False, address="0x1"):
    return {
        "class": class_name,
        "title": title,
        "address": address,
        "mapped": True,
        "hidden": False,
        "floating": floating,
        "pinned": pinned,
        "at": at,
        "size": size,
        "workspace": {"id": workspace, "name": str(workspace)},
        "focusHistoryID": 1 if floating else 5,
    }


def test_window_at_ignores_other_workspaces():
    current = _client("Foot", "on this workspace", 1, [0, 0], [800, 600], address="0xcurrent")
    other = _client(
        "Alacritty",
        "secret on workspace 2",
        2,
        [0, 0],
        [800, 600],
        floating=True,
        pinned=True,
        address="0xother",
    )
    hit = window_at(10, 10, [other, current], workspace_id=1)
    assert hit["address"] == "0xcurrent"
    assert hit["title"] == "on this workspace"
    assert window_at(10, 10, [other], workspace_id=1) is None


def test_current_workspace_prefers_special():
    monitor = {
        "activeWorkspace": {"id": 3, "name": "3"},
        "specialWorkspace": {"id": -98, "name": "special:scratch"},
    }
    assert current_workspace_id(monitor) == -98
    idle = {"activeWorkspace": {"id": 3}, "specialWorkspace": {"id": 0, "name": ""}}
    assert current_workspace_id(idle) == 3
    assert window_at(
        10,
        10,
        [_client("Foot", "regular", 3, [0, 0], [200, 200]), _client("Foot", "scratch", -98, [0, 0], [200, 200])],
        workspace_id=current_workspace_id(monitor),
    )["title"] == "scratch"


def test_agent_context_omits_previous_request():
    payload = {
        "kind": "window",
        "caption": "Foot — shell",
        "lastPrompt": PREVIOUS,
        "window": {"class": "Foot", "title": "shell", "workspace": 1},
        "actions": [
            {"actionId": "edit", "label": "Edit", "hint": "Change this window"},
            {"actionId": "inspect", "label": "What is this?", "hint": "Explain"},
            {"actionId": "continue", "label": "Continue", "hint": f"Follow up on: {PREVIOUS}"},
        ],
    }
    for mode in ("edit", "inspect"):
        stored = context_for_agent(payload, mode)
        encoded = json.dumps(stored)
        assert "lastPrompt" not in stored
        assert PREVIOUS not in encoded
        assert stored["caption"] == "Foot — shell"
        assert stored["window"]["workspace"] == 1
    follow = context_for_agent(payload, "continue")
    assert PREVIOUS not in json.dumps(follow)


def test_captures_are_unique_and_cancel_keeps_inflight():
    root = Path(__file__).resolve().parent / ".capture-test"
    if root.exists():
        import shutil

        shutil.rmtree(root)
    root.mkdir()
    try:
        state = root
        first_ctx, first_shot = allocate_capture(state)
        first_shot.parent.mkdir(parents=True)
        first_shot.write_bytes(b"crop-inflight")
        first_ctx.write_text("{}\n", encoding="utf-8")
        second_ctx, second_shot = allocate_capture(state)
        assert first_shot != second_shot
        assert first_ctx.parent != second_ctx.parent
        assert first_ctx.parent.parent.name == "captures"
        assert not (state / "surroundings.png").exists()
        second_shot.parent.mkdir(parents=True)
        second_shot.write_bytes(b"crop-canceled")
        assert discard_capture(second_ctx, state)
        assert not second_shot.exists()
        assert first_shot.read_bytes() == b"crop-inflight"
        outside = state / "notes.txt"
        outside.write_text("keep", encoding="utf-8")
        assert not discard_capture(outside, state)
        assert outside.read_text(encoding="utf-8") == "keep"
        assert first_shot.read_bytes() == b"crop-inflight"
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    run_module(globals())
