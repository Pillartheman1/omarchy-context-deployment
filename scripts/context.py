#!/usr/bin/env python3
"""Capture click-site context and a screenshot around the cursor."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from session import can_continue, can_undo, load_last


REGION_W = 512
REGION_H = 320


def hyprctl_json(*args):
    try:
        result = subprocess.run(
            ["hyprctl", "-j", *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"hyprctl {' '.join(args)} failed") from exc
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"hyprctl {' '.join(args)} returned invalid JSON") from exc


def clamp(value, low, high):
    return max(low, min(high, value))


def as_int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def point_in_box(x, y, at, size):
    if not isinstance(at, (list, tuple)) or not isinstance(size, (list, tuple)):
        return False
    if len(at) < 2 or len(size) < 2:
        return False
    left, top = as_int(at[0]), as_int(at[1])
    width, height = as_int(size[0]), as_int(size[1])
    return left <= x < left + width and top <= y < top + height


def monitor_at(x, y, monitors):
    enabled = [
        monitor
        for monitor in monitors
        if isinstance(monitor, dict) and not monitor.get("disabled")
    ]
    for monitor in enabled:
        mx, my = as_int(monitor.get("x")), as_int(monitor.get("y"))
        mw, mh = as_int(monitor.get("width")), as_int(monitor.get("height"))
        if mx <= x < mx + mw and my <= y < my + mh:
            return monitor
    for monitor in enabled:
        if monitor.get("focused"):
            return monitor
    return enabled[0] if enabled else None


def reserved_edges(monitor):
    """Hyprland reserved is [left, top, right, bottom] in layout pixels."""
    empty = {"left": 0, "top": 0, "right": 0, "bottom": 0}
    if not monitor:
        return empty
    reserved = monitor.get("reserved") or [0, 0, 0, 0]
    if isinstance(reserved, dict):
        return {
            "left": int(reserved.get("left") or 0),
            "top": int(reserved.get("top") or 0),
            "right": int(reserved.get("right") or 0),
            "bottom": int(reserved.get("bottom") or 0),
        }
    if isinstance(reserved, list) and len(reserved) >= 4:
        return {
            "left": int(reserved[0] or 0),
            "top": int(reserved[1] or 0),
            "right": int(reserved[2] or 0),
            "bottom": int(reserved[3] or 0),
        }
    return empty


def omarchy_bar_edge():
    path = Path.home() / ".config/omarchy/shell.json"
    if not path.is_file():
        return "top"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "top"
    bar = data.get("bar") if isinstance(data, dict) else None
    pos = str((bar or {}).get("position") or "top").lower()
    if pos in ("top", "bottom", "left", "right"):
        return pos
    return "top"


def bar_edges(monitor):
    edges = reserved_edges(monitor)
    if any(edges.values()):
        return edges
    # Compositor often reports zeros even with an Omarchy bar. Fall back to
    # the configured bar edge so Super+right-click on the bar still classifies.
    fallback = 32
    pos = omarchy_bar_edge()
    edges[pos] = fallback
    return edges


def point_on_bar(local_x, local_y, monitor):
    if not monitor:
        return False
    edges = bar_edges(monitor)
    width = as_int(monitor.get("width"))
    height = as_int(monitor.get("height"))
    if edges["top"] and local_y <= edges["top"]:
        return True
    if edges["bottom"] and height and local_y >= height - edges["bottom"]:
        return True
    if edges["left"] and local_x <= edges["left"]:
        return True
    if edges["right"] and width and local_x >= width - edges["right"]:
        return True
    return False


def window_at(x, y, clients):
    hits = []
    for client in clients:
        if not isinstance(client, dict):
            continue
        if not client.get("mapped") or client.get("hidden"):
            continue
        if not point_in_box(x, y, client.get("at"), client.get("size")):
            continue
        hits.append(client)
    if not hits:
        return None

    def sort_key(client):
        floating = 0 if client.get("floating") else 1
        history = client.get("focusHistoryID", 10**6)
        return (floating, history)

    hits.sort(key=sort_key)
    return hits[0]


def caption_for(kind, window):
    if kind == "bar":
        return "Status bar"
    if kind == "desktop":
        return "Desktop"
    if not window:
        return "Desktop"
    title = (window.get("title") or "").strip()
    klass = (window.get("class") or "").strip()
    if title and klass:
        return f"{klass} — {title}"
    return title or klass or "Window"


def truncate(text, limit=80):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def actions_for(kind, last):
    if kind == "bar":
        hint = "Change the status bar, widgets, or theme"
    elif kind == "window":
        hint = "Change this window or the app around the click"
    else:
        hint = "Change the desktop around the click"

    def action(action_id, label, hint):
        # Never use the key "id": QML treats it as an object id, not a field.
        return {"actionId": action_id, "label": label, "hint": hint}

    actions = [
        action("edit", "Edit", hint),
        action("inspect", "What is this?", "Explain this without changing anything"),
    ]
    if can_continue(last):
        previous = truncate(last.get("prompt") or "")
        continue_hint = "Keep going in the last agent session with this new click"
        if previous:
            continue_hint = f"Follow up on: {previous}"
        actions.append(action("continue", "Continue", continue_hint))
    if can_undo(last):
        actions.append(
            action(
                "undo",
                "Undo last edit",
                "Restore config from before the last agent change",
            )
        )
    return actions


def crop_box(x, y, monitor, region_w=REGION_W, region_h=REGION_H):
    if not monitor:
        return None
    mx, my = as_int(monitor.get("x")), as_int(monitor.get("y"))
    mw, mh = as_int(monitor.get("width")), as_int(monitor.get("height"))
    if mw < 8 or mh < 8:
        return None
    crop_w = min(region_w, mw)
    crop_h = min(region_h, mh)
    left = clamp(int(x - crop_w / 2), mx, mx + mw - crop_w)
    top = clamp(int(y - crop_h / 2), my, my + mh - crop_h)
    return left, top, crop_w, crop_h


def screenshot_region(x, y, monitor, dest: Path) -> str | None:
    box = crop_box(x, y, monitor)
    if not box:
        return None
    left, top, width, height = box
    dest.parent.mkdir(parents=True, exist_ok=True)
    geometry = f"{left},{top} {width}x{height}"
    try:
        subprocess.run(["grim", "-g", geometry, str(dest)], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return str(dest) if dest.exists() else None


def notify(message):
    subprocess.run(
        ["omarchy", "notification", "send", "-g", "󰀦", "Context Deployment", message],
        check=False,
        capture_output=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--out")
    parser.add_argument("--screenshot")
    args = parser.parse_args()
    if args.open:
        state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omarchy/desktop-context"
        args.out = args.out or str(state / "context.json")
        args.screenshot = args.screenshot or str(state / "surroundings.png")
    elif not args.out or not args.screenshot:
        parser.error("--out and --screenshot are required unless --open")

    try:
        return capture_and_emit(args)
    except Exception as exc:
        if args.open:
            notify("Could not read the click context.")
            print(exc, file=sys.stderr)
            return 1
        raise


def capture_and_emit(args) -> int:

    cursor = hyprctl_json("cursorpos")
    if not isinstance(cursor, dict):
        raise RuntimeError("hyprctl cursorpos returned no position")
    x, y = as_int(cursor.get("x")), as_int(cursor.get("y"))
    monitors = hyprctl_json("monitors") or []
    if not isinstance(monitors, list):
        monitors = []
    clients = hyprctl_json("clients") or []
    if not isinstance(clients, list):
        clients = []

    monitor = monitor_at(x, y, monitors)
    window = window_at(x, y, clients)
    edges = bar_edges(monitor)
    local_x = x - (as_int(monitor.get("x")) if monitor else 0)
    local_y = y - (as_int(monitor.get("y")) if monitor else 0)

    if point_on_bar(local_x, local_y, monitor):
        kind = "bar"
        window = None
    elif window:
        kind = "window"
    else:
        kind = "desktop"

    workspace = None
    if window and isinstance(window.get("workspace"), dict):
        workspace = window["workspace"]

    screenshot = screenshot_region(x, y, monitor, Path(args.screenshot))
    last = load_last()

    payload = {
        "cursor": {"x": x, "y": y},
        "local": {"x": local_x, "y": local_y},
        "monitor": {
            "name": monitor.get("name") if monitor else "",
            "x": monitor.get("x", 0) if monitor else 0,
            "y": monitor.get("y", 0) if monitor else 0,
            "width": monitor.get("width", 0) if monitor else 0,
            "height": monitor.get("height", 0) if monitor else 0,
            "scale": monitor.get("scale", 1) if monitor else 1,
            "transform": monitor.get("transform", 0) if monitor else 0,
            "reserved": edges,
        },
        "kind": kind,
        "caption": caption_for(kind, window),
        "window": None
        if not window
        else {
            "class": window.get("class") or "",
            "title": window.get("title") or "",
            "address": window.get("address") or "",
            "floating": bool(window.get("floating")),
            "workspace": (window.get("workspace") or {}).get("id"),
        },
        "workspace": workspace,
        "screenshot": screenshot,
        "contextPath": str(Path(args.out)),
        "dictation": shutil.which("voxtype") is not None,
        "lastPrompt": last.get("prompt") or "",
        "actions": actions_for(kind, last),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    # Summon IPC gets a small UI payload. The agent still reads the full file.
    ui = {
        "contextPath": payload["contextPath"],
        "screenshot": payload["screenshot"] or "",
        "caption": payload["caption"],
        "kind": payload["kind"],
        "local": payload["local"],
        "monitor": {
            "name": payload["monitor"]["name"],
            "width": payload["monitor"]["width"],
            "height": payload["monitor"]["height"],
        },
        "dictation": payload["dictation"],
        "lastPrompt": payload["lastPrompt"],
        "actions": payload["actions"],
    }
    encoded = json.dumps(ui, separators=(",", ":"), ensure_ascii=True)
    if args.open:
        plugin_id = json.loads(
            (Path(__file__).resolve().parent.parent / "manifest.json").read_text(encoding="utf-8")
        ).get("id") or ""
        if not plugin_id:
            raise RuntimeError("plugin manifest is missing an id")
        os.execvp("omarchy-shell", ["omarchy-shell", "shell", "summon", plugin_id, encoded])
        return 0
    sys.stdout.write(encoded + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
