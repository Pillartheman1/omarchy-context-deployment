#!/usr/bin/env python3
"""Last desktop-context session, undo snapshot, and restore."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omarchy/desktop-context"
LAST_PATH = STATE_DIR / "last.json"
SNAPSHOT_DIR = STATE_DIR / "undo" / "snapshot"

# Only the files an omarchy-skill edit is likely to touch. Skip plugins,
# themes, and wallpapers so undo stays small and cannot overwrite this plugin.
CONFIG = Path.home() / ".config"
HYPR_EXCLUDES = ["*.bak", "*.bak.*"]
TREES = (
    ("hypr", CONFIG / "hypr", HYPR_EXCLUDES),
    ("omarchy/extensions", CONFIG / "omarchy/extensions", []),
    ("omarchy/hooks", CONFIG / "omarchy/hooks", []),
    ("alacritty", CONFIG / "alacritty", []),
    ("foot", CONFIG / "foot", []),
    ("kitty", CONFIG / "kitty", []),
    ("ghostty", CONFIG / "ghostty", []),
)
FILES = (
    ("omarchy/shell.json", CONFIG / "omarchy/shell.json"),
    ("starship.toml", CONFIG / "starship.toml"),
)


def load_last() -> dict:
    if not LAST_PATH.is_file():
        return {}
    try:
        data = json.loads(LAST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_last(update: dict, *, keep_undo: bool = False) -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = load_last()
    snapshot_dir = data.get("snapshotDir")
    snapshot_at = data.get("snapshotAt")
    can_undo = data.get("canUndo")
    data.update(update)
    data["version"] = 1
    data["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if keep_undo:
        if snapshot_dir:
            data["snapshotDir"] = snapshot_dir
        if snapshot_at:
            data["snapshotAt"] = snapshot_at
        if can_undo is not None and "canUndo" not in update:
            data["canUndo"] = can_undo
    LAST_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def snapshot_exists(last: dict | None = None) -> bool:
    last = last if last is not None else load_last()
    path = Path(last.get("snapshotDir") or SNAPSHOT_DIR)
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    return True


def can_continue(last: dict | None = None) -> bool:
    last = last if last is not None else load_last()
    return bool(last.get("canContinue"))


def can_undo(last: dict | None = None) -> bool:
    last = last if last is not None else load_last()
    return bool(last.get("canUndo")) and snapshot_exists(last)


def _copy_tree(src: Path, dest: Path, *, excludes: list[str], delete: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("rsync"):
        cmd = ["rsync", "-a"]
        if delete:
            cmd.append("--delete")
        for name in excludes:
            cmd.append(f"--exclude={name}")
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            cmd.append(f"{src}/")
            cmd.append(f"{dest}/")
        else:
            cmd.append(str(src))
            cmd.append(str(dest))
        subprocess.run(cmd, check=True, capture_output=True)
        return

    ignore = shutil.ignore_patterns(*excludes) if excludes else None
    if src.is_dir():
        if dest.exists() and delete:
            shutil.rmtree(dest)
        shutil.copytree(src, dest, dirs_exist_ok=True, ignore=ignore)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def take_snapshot() -> Path:
    if SNAPSHOT_DIR.exists():
        shutil.rmtree(SNAPSHOT_DIR)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    copied = False
    for name, src, excludes in TREES:
        if not src.exists():
            continue
        _copy_tree(src, SNAPSHOT_DIR / name, excludes=excludes, delete=True)
        copied = True
    for name, src in FILES:
        if not src.exists():
            continue
        _copy_tree(src, SNAPSHOT_DIR / name, excludes=[], delete=False)
        copied = True
    if not copied:
        raise RuntimeError("nothing to snapshot")
    return SNAPSHOT_DIR


def restore_snapshot(last: dict | None = None) -> Path:
    last = last if last is not None else load_last()
    path = Path(last.get("snapshotDir") or SNAPSHOT_DIR)
    if not snapshot_exists({"snapshotDir": str(path)}):
        raise RuntimeError("no undo snapshot")
    for name, dest, excludes in TREES:
        src = path / name
        if not src.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        _copy_tree(src, dest, excludes=excludes, delete=True)
    for name, dest in FILES:
        src = path / name
        if not src.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        _copy_tree(src, dest, excludes=[], delete=False)
    shell_json = Path.home() / ".config/omarchy/shell.json"
    if shell_json.exists():
        shell_json.touch()
    subprocess.run(["hyprctl", "reload"], check=False, capture_output=True)
    return path


def record_session(
    *,
    mode: str,
    prompt: str,
    caption: str,
    kind: str,
    context_path: str,
    screenshot: str,
    snapshot: bool,
) -> dict:
    update = {
        "lastMode": mode,
        "prompt": prompt,
        "caption": caption,
        "kind": kind,
        "contextPath": context_path,
        "screenshot": screenshot,
        "canContinue": True,
    }
    if snapshot:
        try:
            snap = take_snapshot()
            update["canUndo"] = True
            update["snapshotDir"] = str(snap)
            update["snapshotAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            return save_last(update, keep_undo=False)
        except Exception as exc:
            print(f"snapshot skipped: {exc}", file=sys.stderr)
            update["canUndo"] = False
    return save_last(update, keep_undo=True)


def context_meta(context_path: Path) -> tuple[str, str, str]:
    caption = ""
    kind = ""
    screenshot = ""
    if context_path.is_file():
        try:
            payload = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            caption = str(payload.get("caption") or "")
            kind = str(payload.get("kind") or "")
            screenshot = str(payload.get("screenshot") or "")
    return caption, kind, screenshot


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("last")
    sub.add_parser("snapshot")
    sub.add_parser("restore")
    sub.add_parser("can-continue")
    sub.add_parser("can-undo")

    rec = sub.add_parser("record")
    rec.add_argument("--mode", required=True)
    rec.add_argument("--prompt", default="")
    rec.add_argument("--context", required=True)
    rec.add_argument("--snapshot", action="store_true")

    args = parser.parse_args()

    if args.cmd == "last":
        sys.stdout.write(json.dumps(load_last(), indent=2) + "\n")
        return 0
    if args.cmd == "can-continue":
        return 0 if can_continue() else 1
    if args.cmd == "can-undo":
        return 0 if can_undo() else 1
    if args.cmd == "snapshot":
        path = take_snapshot()
        sys.stdout.write(str(path) + "\n")
        return 0
    if args.cmd == "restore":
        try:
            restore_snapshot()
        except Exception as exc:
            print(f"undo failed: {exc}", file=sys.stderr)
            return 1
        save_last(
            {
                "lastMode": "undo",
                "canContinue": False,
                "canUndo": False,
            },
            keep_undo=True,
        )
        return 0
    if args.cmd == "record":
        context_path = Path(args.context)
        caption, kind, screenshot = context_meta(context_path)
        record_session(
            mode=args.mode,
            prompt=args.prompt,
            caption=caption,
            kind=kind,
            context_path=str(context_path),
            screenshot=screenshot,
            snapshot=args.snapshot,
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
