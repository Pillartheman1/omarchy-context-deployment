#!/usr/bin/env python3
"""Last desktop-context session, undo snapshot, and restore."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path


# Only the files an omarchy-skill edit is likely to touch. Skip plugins,
# themes, and wallpapers so undo stays small and cannot overwrite this plugin.
HYPR_EXCLUDES = ["*.bak", "*.bak.*"]
AGENT_WINDOW_CLASS = "org.omarchy.agent"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local/state"
    return root / "omarchy/desktop-context"


def last_path() -> Path:
    return state_dir() / "last.json"


def snapshot_dir() -> Path:
    return state_dir() / "undo" / "snapshot"


def config_root() -> Path:
    return Path.home() / ".config"


def tracked_specs():
    config = config_root()
    trees = (
        ("hypr", config / "hypr", HYPR_EXCLUDES),
        ("omarchy/extensions", config / "omarchy/extensions", []),
        ("omarchy/hooks", config / "omarchy/hooks", []),
        ("alacritty", config / "alacritty", []),
        ("foot", config / "foot", []),
        ("kitty", config / "kitty", []),
        ("ghostty", config / "ghostty", []),
    )
    files = (
        ("omarchy/shell.json", config / "omarchy/shell.json"),
        ("starship.toml", config / "starship.toml"),
    )
    return trees, files


def load_last() -> dict:
    path = last_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_last(update: dict, *, keep_undo: bool = False) -> dict:
    state_dir().mkdir(parents=True, exist_ok=True)
    data = load_last()
    snapshot_dir_value = data.get("snapshotDir")
    snapshot_at = data.get("snapshotAt")
    can_undo_flag = data.get("canUndo")
    data.update(update)
    data["version"] = 1
    data["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if keep_undo:
        if snapshot_dir_value:
            data["snapshotDir"] = snapshot_dir_value
        if snapshot_at:
            data["snapshotAt"] = snapshot_at
        if can_undo_flag is not None and "canUndo" not in update:
            data["canUndo"] = can_undo_flag
    target = last_path()
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return data


def snapshot_exists(last: dict | None = None) -> bool:
    last = last if last is not None else load_last()
    path = Path(last.get("snapshotDir") or snapshot_dir())
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


def safe_rel(rel: str) -> bool:
    if not rel or rel.startswith("/"):
        return False
    parts = Path(rel).parts
    return ".." not in parts and parts != (".",)


def name_ignored(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def iter_tracked():
    trees, files = tracked_specs()
    for prefix, root, excludes in trees:
        if not root.exists():
            continue
        if root.is_symlink() or root.is_file():
            if not name_ignored(root.name, excludes):
                yield prefix, root
            continue
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [name for name in dirnames if not name_ignored(name, excludes)]
            for name in filenames:
                if name_ignored(name, excludes):
                    continue
                path = Path(dirpath) / name
                rel = Path(prefix) / path.relative_to(root)
                text = rel.as_posix()
                if safe_rel(text):
                    yield text, path
    for rel, path in files:
        if path.is_file() or path.is_symlink():
            yield rel, path


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_symlink():
        digest.update(b"link:")
        digest.update(os.readlink(path).encode())
        return digest.hexdigest()
    digest.update(b"file:")
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_manifest() -> dict:
    files = {}
    for rel, path in iter_tracked():
        files[rel] = hash_file(path)
    return {"files": files}


def changed_paths(baseline: dict, live: dict) -> list[str]:
    old = baseline.get("files") or {}
    new = live.get("files") or {}
    if not isinstance(old, dict) or not isinstance(new, dict):
        return []
    changed = []
    for rel, digest in old.items():
        if isinstance(rel, str) and safe_rel(rel) and new.get(rel) != digest:
            changed.append(rel)
    for rel in new:
        if isinstance(rel, str) and safe_rel(rel) and rel not in old:
            changed.append(rel)
    return sorted(set(changed))


def copy_tracked_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or (dest.exists() and not dest.is_dir()):
        dest.unlink()
    shutil.copy2(src, dest, follow_symlinks=False)


def new_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def populate_snapshot(staging: Path) -> dict:
    manifest = collect_manifest()
    if not manifest["files"]:
        raise RuntimeError("nothing to snapshot")
    for rel in manifest["files"]:
        copy_tracked_file(config_root() / rel, staging / "files" / rel)
    meta = staging / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    _write_json(meta / "manifest.json", manifest)
    _write_json(meta / "changes.json", {"paths": []})
    sid = new_id()
    _write_json(
        meta / "sessions" / f"{sid}.json",
        {"id": sid, "baseline": manifest["files"], "sealed": False},
    )
    (meta / "active-session").write_text(sid + "\n", encoding="utf-8")
    return manifest


def _rename(src: Path, dst: Path) -> None:
    src.rename(dst)


@contextlib.contextmanager
def session_lock():
    state_dir().mkdir(parents=True, exist_ok=True)
    handle = open(state_dir() / "session.lock", "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def publish_snapshot(staging: Path) -> Path | None:
    """Swap in a finished snapshot. Return the previous directory, still on disk.

    The caller deletes that backup only after session metadata is saved. A failed
    swap puts the previous directory back.
    """
    target = snapshot_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.parent / f".snapshot-backup-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    with session_lock():
        moved = False
        try:
            if target.exists():
                _rename(target, backup)
                moved = True
            _rename(staging, target)
        except Exception:
            if moved and backup.exists() and not target.exists():
                try:
                    _rename(backup, target)
                except OSError:
                    pass
            raise
    if moved and backup.exists():
        return backup
    return None


def discard_backup(backup: Path | None) -> None:
    if backup is not None and backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def rollback_backup(backup: Path | None) -> None:
    """Put the previous restore point back. Used when metadata never commits."""
    if backup is None or not backup.exists():
        return
    target = snapshot_dir()
    with session_lock():
        if not backup.exists():
            return
        if target.exists():
            shutil.rmtree(target)
        _rename(backup, target)


def stage_snapshot() -> tuple[Path, Path | None]:
    """Publish a snapshot and keep the previous restore point until the caller commits."""
    parent = snapshot_dir().parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".snapshot-staging-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        populate_snapshot(staging)
        backup = publish_snapshot(staging)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return snapshot_dir(), backup


def take_snapshot() -> Path:
    snap, backup = stage_snapshot()
    discard_backup(backup)
    return snap


def read_change_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_records(path: Path) -> dict[str, dict]:
    """Map a tracked path to the bytes before the edit and the bytes the agent left."""
    data = read_change_file(path)
    files = data.get("files")
    records: dict[str, dict] = {}
    if isinstance(files, dict):
        for rel, record in files.items():
            if not isinstance(rel, str) or not safe_rel(rel) or not isinstance(record, dict):
                continue
            records[rel] = {
                "before": record.get("before") if isinstance(record.get("before"), str) else None,
                "after": record.get("after") if isinstance(record.get("after"), str) else None,
            }
    return records


def read_changes(path: Path) -> list[str]:
    records = read_records(path)
    if records:
        return sorted(records)
    data = read_change_file(path)
    paths = data.get("paths")
    if not isinstance(paths, list):
        return []
    return [item for item in paths if isinstance(item, str) and safe_rel(item)]


def snapshot_manifest_files(snap: Path) -> dict:
    try:
        data = json.loads((snap / "meta" / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def write_records(path: Path, records: dict[str, dict]) -> None:
    _write_json(path, {"paths": sorted(records), "files": records})


def seal_session(session_path: Path, attributed: set[str] | None = None) -> list[str]:
    """Record files the agent touched and the bytes it left behind.

    `attributed` is the set the watcher observed. Paths outside that set stay
    out of the undo, even if they changed while the agent window was open.
    """
    if not session_path.is_file():
        return []
    snap = session_path.parents[2]
    changes_path = snap / "meta" / "changes.json"
    owned = {rel for rel in (attributed or set()) if isinstance(rel, str) and safe_rel(rel)}
    with session_lock():
        if not session_path.is_file():
            return []
        try:
            data = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("undo session is unreadable") from exc
        if not isinstance(data, dict):
            raise RuntimeError("undo session is unreadable")
        records = read_records(changes_path)
        if data.get("sealed"):
            return sorted(records)
        stored = {
            item
            for item in (data.get("observed") or [])
            if isinstance(item, str) and safe_rel(item)
        }
        owned = stored if attributed is None else stored | owned
        live = collect_manifest()["files"]
        manifest = snapshot_manifest_files(snap)
        for rel in sorted(owned):
            after = live.get(rel) if isinstance(live.get(rel), str) else None
            if rel in records:
                records[rel]["after"] = after
                continue
            before = manifest.get(rel) if isinstance(manifest.get(rel), str) else None
            if before == after:
                continue
            records[rel] = {"before": before, "after": after}
        data["sealed"] = True
        data["changed"] = sorted(records)
        _write_json(session_path, data)
        write_records(changes_path, records)
        return sorted(records)


def session_sealed(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(data, dict) and data.get("sealed"))


def seal_unsealed(snap: Path) -> list[str]:
    sessions = snap / "meta" / "sessions"
    if sessions.is_dir():
        for path in sorted(sessions.glob("*.json")):
            seal_session(path)
    return read_changes(snap / "meta" / "changes.json")


def active_session_path(snap: Path) -> Path | None:
    pointer = snap / "meta" / "active-session"
    if not pointer.is_file():
        return None
    sid = pointer.read_text(encoding="utf-8").strip()
    if not sid or not safe_rel(sid) or "/" in sid:
        return None
    path = snap / "meta" / "sessions" / f"{sid}.json"
    return path if path.is_file() else None


def begin_followup_session(snap: Path | None = None) -> Path:
    """Track a Continue against the current files without replacing the snapshot."""
    snap = snap or snapshot_dir()
    if not (snap / "meta" / "manifest.json").is_file():
        raise RuntimeError("undo snapshot has no manifest")
    live = collect_manifest()
    sid = new_id()
    path = snap / "meta" / "sessions" / f"{sid}.json"
    _write_json(path, {"id": sid, "baseline": live["files"], "sealed": False})
    (snap / "meta" / "active-session").write_text(sid + "\n", encoding="utf-8")
    return path


def restore_rel(snap: Path, rel: str) -> None:
    if not safe_rel(rel):
        return
    src = snap / "files" / rel
    dest = config_root() / rel
    if src.is_symlink() or src.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_dir() and not dest.is_symlink():
            return
        copy_tracked_file(src, dest)
        return
    if dest.is_symlink() or dest.is_file():
        dest.unlink()


def content_token(path: Path) -> str | None:
    if path.is_symlink() or path.is_file():
        return hash_file(path)
    return None


def restore_recorded(snap: Path, rel: str, record: dict) -> str | None:
    """Put back one agent edit. Return the path when a newer edit is kept."""
    if not safe_rel(rel):
        return None
    before = record.get("before") if isinstance(record.get("before"), str) else None
    after = record.get("after") if isinstance(record.get("after"), str) else None
    current = content_token(config_root() / rel)
    if current == after:
        restore_rel(snap, rel)
        return None
    if current == before:
        return None
    return rel


def restore_snapshot(last: dict | None = None) -> list[str]:
    """Restore the agent's edits. Paths in the result were left as they are now."""
    last = last if last is not None else load_last()
    path = Path(last.get("snapshotDir") or snapshot_dir())
    if not snapshot_exists({"snapshotDir": str(path)}):
        raise RuntimeError("no undo snapshot")
    if not (path / "meta" / "manifest.json").is_file():
        raise RuntimeError("undo snapshot has no file manifest")
    seal_unsealed(path)
    records = read_records(path / "meta" / "changes.json")
    conflicts = []
    for rel in sorted(records):
        kept = restore_recorded(path, rel, records[rel])
        if kept:
            conflicts.append(kept)
    shell_json = config_root() / "omarchy/shell.json"
    if shell_json.exists():
        shell_json.touch()
    try:
        subprocess.run(["hyprctl", "reload"], check=False, capture_output=True)
    except OSError:
        pass
    return conflicts


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
    pause = os.environ.get("DESKTOP_CONTEXT_RECORD_PAUSE")
    if pause:
        time.sleep(float(pause))
    if snapshot:
        backup = None
        try:
            snap, backup = stage_snapshot()
            update["canUndo"] = True
            update["snapshotDir"] = str(snap)
            update["snapshotAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            saved = save_last(update, keep_undo=False)
        except Exception:
            rollback_backup(backup)
            raise
        discard_backup(backup)
        return saved
    if mode == "continue" and can_undo():
        begin_followup_session(Path(load_last().get("snapshotDir") or snapshot_dir()))
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


def agent_addresses(clients) -> set[str]:
    found = set()
    if not isinstance(clients, list):
        return found
    for client in clients:
        if not isinstance(client, dict):
            continue
        classes = (client.get("class"), client.get("initialClass"))
        if not any(item == AGENT_WINDOW_CLASS for item in classes):
            continue
        address = str(client.get("address") or "")
        if address:
            found.add(address)
    return found


def read_ppid(stat_path: Path) -> int | None:
    try:
        text = stat_path.read_text(encoding="utf-8")
    except OSError:
        return None
    marker = text.rfind(")")
    if marker < 0:
        return None
    fields = text[marker + 2 :].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def descendant_pids(roots: set[int], proc_root: Path | None = None) -> set[int]:
    proc_root = proc_root or Path("/proc")
    children: dict[int, list[int]] = {}
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            ppid = read_ppid(entry / "stat")
            if ppid is None:
                continue
            children.setdefault(ppid, []).append(int(entry.name))
    seen = set(roots)
    stack = list(roots)
    while stack:
        pid = stack.pop()
        for child in children.get(pid, []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def tracked_index() -> dict[str, str]:
    index = {}
    for rel, path in iter_tracked():
        try:
            index[str(path.resolve())] = rel
        except OSError:
            index[str(path)] = rel
    return index


def rel_for_fd_target(target: str, index: dict[str, str]) -> str | None:
    cleaned = target.split(" (deleted)", 1)[0]
    if cleaned in index:
        return index[cleaned]
    for full, rel in index.items():
        if cleaned.startswith(full + ".") or cleaned.startswith(full + "~"):
            return rel
    return None


def open_tracked_files(pids: set[int], proc_root: Path | None = None, index: dict[str, str] | None = None) -> set[str]:
    """Tracked files currently open by these processes, including temp siblings."""
    proc_root = proc_root or Path("/proc")
    index = tracked_index() if index is None else index
    found: set[str] = set()
    for pid in pids:
        fd_dir = proc_root / str(pid) / "fd"
        if not fd_dir.is_dir():
            continue
        try:
            entries = list(fd_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            rel = rel_for_fd_target(target, index)
            if rel:
                found.add(rel)
    return found


def agent_window_pids(clients) -> set[int]:
    pids = set()
    if not isinstance(clients, list):
        return pids
    for client in clients:
        if not isinstance(client, dict):
            continue
        classes = (client.get("class"), client.get("initialClass"))
        if not any(item == AGENT_WINDOW_CLASS for item in classes):
            continue
        pid = client.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            continue
        pids.add(pid)
    return pids


def files_touched_by_clients(clients, proc_root: Path | None = None) -> set[str]:
    roots = agent_window_pids(clients)
    if not roots:
        return set()
    return open_tracked_files(descendant_pids(roots, proc_root), proc_root)


def remember_observed(session_path: Path, paths: set[str]) -> None:
    if not paths or not session_path.is_file():
        return
    fresh = {rel for rel in paths if isinstance(rel, str) and safe_rel(rel)}
    if not fresh:
        return
    with session_lock():
        if not session_path.is_file():
            return
        try:
            data = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or data.get("sealed"):
            return
        current = {
            item
            for item in (data.get("observed") or [])
            if isinstance(item, str) and safe_rel(item)
        }
        merged = current | fresh
        if merged == current:
            return
        data["observed"] = sorted(merged)
        _write_json(session_path, data)


def watch_should_seal(known: set[str], seen: set[str], current: set[str]) -> tuple[set[str], bool]:
    """Seal after every agent window that appeared after `known` has closed."""
    seen = set(seen) | (set(current) - set(known))
    if seen and seen.isdisjoint(current):
        return seen, True
    return seen, False


def watch_until_done(
    session_path: Path,
    clients_fn,
    *,
    known: set[str] | None = None,
    sleep_fn=time.sleep,
    poll: float = 0.4,
    max_polls: int | None = None,
    observe_fn=None,
) -> bool:
    if known is None:
        known = agent_addresses(clients_fn())
    seen: set[str] = set()
    attributed: set[str] = set()
    polls = 0
    while session_path.exists() and not session_sealed(session_path):
        if max_polls is not None and polls >= max_polls:
            return False
        polls += 1
        clients = clients_fn()
        if observe_fn is not None:
            found = set(observe_fn(clients) or [])
        else:
            found = files_touched_by_clients(clients)
        attributed |= found
        remember_observed(session_path, found)
        seen, done = watch_should_seal(known, seen, agent_addresses(clients))
        if done:
            seal_session(session_path, attributed=attributed)
            return True
        sleep_fn(poll)
    return False


def watch(ready: str | None = None) -> int:
    last = load_last()
    snap = Path(last.get("snapshotDir") or snapshot_dir())
    session_path = active_session_path(snap)
    if session_path is None:
        print("no active undo session", file=sys.stderr)
        return 1
    pid_path = state_dir() / "watch.pid"
    pid_path.write_text(str(os.getpid()) + "\n", encoding="utf-8")

    def clients():
        try:
            data = hyprctl_json("clients")
        except (OSError, RuntimeError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    try:
        # Record windows that already exist before the caller is told to launch.
        known = agent_addresses(clients())
        if ready:
            Path(ready).parent.mkdir(parents=True, exist_ok=True)
            Path(ready).write_text("ready\n", encoding="utf-8")
        watch_until_done(session_path, clients, known=known)
    finally:
        try:
            if pid_path.is_file() and pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("last")
    sub.add_parser("snapshot")
    sub.add_parser("restore")
    sub.add_parser("can-continue")
    sub.add_parser("can-undo")
    watch_cmd = sub.add_parser("watch")
    watch_cmd.add_argument("--ready")

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
        try:
            path = take_snapshot()
        except Exception as exc:
            print(f"snapshot failed: {exc}", file=sys.stderr)
            return 1
        sys.stdout.write(str(path) + "\n")
        return 0
    if args.cmd == "restore":
        try:
            conflicts = restore_snapshot()
        except Exception as exc:
            print(f"undo failed: {exc}", file=sys.stderr)
            return 1
        for rel in conflicts:
            sys.stdout.write(f"conflict: {rel}\n")
        save_last(
            {
                "lastMode": "undo",
                "canContinue": False,
                "canUndo": False,
            },
            keep_undo=True,
        )
        return 0
    if args.cmd == "watch":
        return watch(args.ready)
    if args.cmd == "record":
        context_path = Path(args.context)
        caption, kind, screenshot = context_meta(context_path)
        try:
            record_session(
                mode=args.mode,
                prompt=args.prompt,
                caption=caption,
                kind=kind,
                context_path=str(context_path),
                screenshot=screenshot,
                snapshot=args.snapshot,
            )
        except Exception as exc:
            print(f"snapshot failed: {exc}", file=sys.stderr)
            return 1
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
