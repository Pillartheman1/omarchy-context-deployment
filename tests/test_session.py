#!/usr/bin/env python3
"""Undo snapshots stay scoped and are not thrown away early."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import session  # noqa: E402
from isolate import isolated_home, run_module  # noqa: E402
from session import (  # noqa: E402
    active_session_path,
    agent_addresses,
    begin_followup_session,
    files_touched_by_clients,
    load_last,
    record_session,
    restore_snapshot,
    seal_session,
    snapshot_dir,
    take_snapshot,
    watch_should_seal,
    watch_until_done,
)


def _write(home: Path, rel: str, text: str) -> Path:
    path = home / ".config" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _record(home: Path, prompt: str, *, snapshot: bool, mode: str = "edit") -> None:
    context = home / "context.json"
    context.write_text('{"caption": "Desk", "kind": "desktop"}\n', encoding="utf-8")
    record_session(
        mode=mode,
        prompt=prompt,
        caption="Desk",
        kind="desktop",
        context_path=str(context),
        screenshot="",
        snapshot=snapshot,
    )


def test_restore_reverts_only_files_changed_before_seal():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            edited = _write(home, "hypr/edited.conf", "original-edited")
            untouched = _write(home, "hypr/untouched.conf", "original-untouched")
            backup = _write(home, "hypr/local.bak", "keep-bak")
            take_snapshot()
            edited.write_text("agent-edit", encoding="utf-8")
            created = _write(home, "hypr/created.conf", "agent-created")
            seal_session(
                active_session_path(snapshot_dir()),
                attributed={"hypr/edited.conf", "hypr/created.conf"},
            )
            untouched.write_text("independent-later", encoding="utf-8")
            backup.write_text("independent-bak", encoding="utf-8")
            _write(home, "hypr/after.conf", "independent-new")
            restore_snapshot()
            assert edited.read_text(encoding="utf-8") == "original-edited"
            assert not created.exists()
            assert untouched.read_text(encoding="utf-8") == "independent-later"
            assert backup.read_text(encoding="utf-8") == "independent-bak"
            assert (home / ".config/hypr/after.conf").read_text(encoding="utf-8") == "independent-new"


def test_deleted_tracked_file_comes_back_and_later_edits_stay():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            kept = _write(home, "hypr/kept.conf", "kept-original")
            deleted = _write(home, "hypr/deleted.conf", "deleted-original")
            take_snapshot()
            deleted.unlink()
            seal_session(active_session_path(snapshot_dir()), attributed={"hypr/deleted.conf"})
            kept.write_text("independent", encoding="utf-8")
            restore_snapshot()
            assert deleted.read_text(encoding="utf-8") == "deleted-original"
            assert kept.read_text(encoding="utf-8") == "independent"


def test_followup_keeps_snapshot_and_skips_independent_edits():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            primary = _write(home, "hypr/primary.conf", "v1")
            other = _write(home, "hypr/other.conf", "stable")
            take_snapshot()
            primary.write_text("v2", encoding="utf-8")
            seal_session(active_session_path(snapshot_dir()), attributed={"hypr/primary.conf"})
            other.write_text("user-edit", encoding="utf-8")
            begin_followup_session()
            created = _write(home, "hypr/follow.conf", "from-continue")
            seal_session(active_session_path(snapshot_dir()), attributed={"hypr/follow.conf"})
            other.write_text("still-user", encoding="utf-8")
            snapshot_copy = (snapshot_dir() / "files" / "hypr" / "primary.conf").read_text(encoding="utf-8")
            assert snapshot_copy == "v1"
            restore_snapshot()
            assert primary.read_text(encoding="utf-8") == "v1"
            assert not created.exists()
            assert other.read_text(encoding="utf-8") == "still-user"


def test_failed_snapshot_keeps_previous_restore_point():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            target = _write(home, "hypr/a.conf", "v1")
            _record(home, "first", snapshot=True)
            assert load_last()["canUndo"] is True
            marker = (snapshot_dir() / "files" / "hypr" / "a.conf").read_text(encoding="utf-8")
            assert marker == "v1"
            target.unlink()
            try:
                _record(home, "SECOND-PROMPT", snapshot=True)
            except RuntimeError as exc:
                assert "nothing to snapshot" in str(exc)
            else:
                raise AssertionError("snapshot should have failed")
            last = load_last()
            assert last["prompt"] == "first"
            assert last["canUndo"] is True
            assert (snapshot_dir() / "files" / "hypr" / "a.conf").read_text(encoding="utf-8") == "v1"
            parent = snapshot_dir().parent
            names = [path.name for path in parent.iterdir()]
            assert not any(name.startswith(".snapshot-") for name in names)


def test_copy_failure_does_not_publish_or_delete_previous():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            _write(home, "hypr/a.conf", "v1")
            _write(home, "hypr/b.conf", "b")
            take_snapshot()
            original = session.copy_tracked_file

            def boom(src, dest):
                raise RuntimeError("disk full")

            session.copy_tracked_file = boom
            try:
                try:
                    take_snapshot()
                except RuntimeError as exc:
                    assert "disk full" in str(exc)
                else:
                    raise AssertionError("copy should have failed")
            finally:
                session.copy_tracked_file = original
            assert (snapshot_dir() / "files" / "hypr" / "a.conf").read_text(encoding="utf-8") == "v1"
            parent = snapshot_dir().parent
            names = [path.name for path in parent.iterdir()]
            assert not any(name.startswith(".snapshot-staging") or name.startswith(".snapshot-backup") for name in names)


def test_publish_failure_restores_previous_directory():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            _write(home, "hypr/a.conf", "v1")
            take_snapshot()
            _write(home, "hypr/a.conf", "v2")
            original = session._rename

            def boom(src, dst):
                raise OSError("swap failed")

            session._rename = boom
            try:
                try:
                    take_snapshot()
                except OSError as exc:
                    assert "swap failed" in str(exc)
                else:
                    raise AssertionError("publish should have failed")
            finally:
                session._rename = original
            assert (snapshot_dir() / "files" / "hypr" / "a.conf").read_text(encoding="utf-8") == "v1"
            undo = snapshot_dir().parents[1]
            names = [path.name for path in undo.iterdir()]
            assert not any(name.startswith(".staging-") or name.startswith(".snapshot-") for name in names)


def test_watch_seals_when_its_agent_window_closes():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            edited = _write(home, "kitty/kitty.conf", "old")
            other = _write(home, "kitty/theme.conf", "theme")
            take_snapshot()
            edited.write_text("new", encoding="utf-8")
            frames = [
                [{"class": "org.omarchy.agent", "address": "0xagent"}],
                [{"class": "org.omarchy.agent", "address": "0xagent", "initialClass": "org.omarchy.agent"}],
                [],
            ]
            cursor = {"i": 0}

            def clients():
                frame = frames[min(cursor["i"], len(frames) - 1)]
                cursor["i"] += 1
                return frame

            assert watch_until_done(
                active_session_path(snapshot_dir()),
                clients,
                known=set(),
                sleep_fn=lambda _seconds: None,
                observe_fn=lambda _clients: {"kitty/kitty.conf"},
            )
            other.write_text("independent-theme", encoding="utf-8")
            restore_snapshot()
            assert edited.read_text(encoding="utf-8") == "old"
            assert other.read_text(encoding="utf-8") == "independent-theme"


def test_restore_keeps_a_file_changed_again_after_the_agent():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            edited = _write(home, "hypr/edited.conf", "original")
            _write(home, "hypr/other.conf", "other")
            take_snapshot()
            edited.write_text("agent-version", encoding="utf-8")
            seal_session(active_session_path(snapshot_dir()), attributed={"hypr/edited.conf"})
            edited.write_text("user-version", encoding="utf-8")
            conflicts = restore_snapshot()
            assert edited.read_text(encoding="utf-8") == "user-version"
            assert conflicts == ["hypr/edited.conf"]


def test_change_during_the_window_is_kept_unless_the_agent_touched_it():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            agent = _write(home, "hypr/agent.conf", "agent-original")
            user = _write(home, "hypr/user.conf", "user-original")
            take_snapshot()
            agent.write_text("agent-edit", encoding="utf-8")
            user.write_text("user-during", encoding="utf-8")
            seal_session(active_session_path(snapshot_dir()), attributed={"hypr/agent.conf"})
            assert restore_snapshot() == []
            assert agent.read_text(encoding="utf-8") == "agent-original"
            assert user.read_text(encoding="utf-8") == "user-during"


def test_agent_process_fds_do_not_include_other_files(tmp_path=None):
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            agent = _write(home, "hypr/agent.conf", "agent")
            _write(home, "hypr/user.conf", "user")
            proc = Path(tmp) / "proc"
            parent = proc / "4242"
            child = proc / "4243"
            (parent / "fd").mkdir(parents=True)
            (child / "fd").mkdir(parents=True)
            (parent / "stat").write_text("4242 (foot) S 1 0\n", encoding="utf-8")
            (child / "stat").write_text("4243 (claude) S 4242 0\n", encoding="utf-8")
            os_symlink = __import__("os").symlink
            os_symlink(str(agent.resolve()), child / "fd" / "4")
            os_symlink(str(agent.resolve()) + ".tmp", child / "fd" / "5")
            (child / "fdinfo").mkdir()
            (child / "fdinfo" / "4").write_text("flags:\t0100001\n", encoding="utf-8")
            (child / "fdinfo" / "5").write_text("flags:\t0100002\n", encoding="utf-8")
            clients = [{"class": "org.omarchy.agent", "pid": 4242, "address": "0x1"}]
            assert files_touched_by_clients(clients, proc_root=proc) == {"hypr/agent.conf"}


def test_metadata_save_failure_keeps_the_previous_restore_point():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            target = _write(home, "hypr/a.conf", "v1")
            _record(home, "first", snapshot=True)
            target.write_text("v2", encoding="utf-8")
            original = session.save_last

            def boom(update, keep_undo=False):
                raise OSError("metadata")

            session.save_last = boom
            try:
                try:
                    _record(home, "SECOND-PROMPT", snapshot=True)
                except OSError as exc:
                    assert "metadata" in str(exc)
                else:
                    raise AssertionError("metadata save should have failed")
            finally:
                session.save_last = original
            last = load_last()
            assert last["prompt"] == "first"
            assert last["canUndo"] is True
            assert (snapshot_dir() / "files" / "hypr" / "a.conf").read_text(encoding="utf-8") == "v1"
            names = [path.name for path in snapshot_dir().parent.iterdir()]
            assert not any(name.startswith(".snapshot-") for name in names)


def test_read_only_open_is_not_an_agent_edit():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            agent = _write(home, "hypr/agent.conf", "agent")
            user = _write(home, "hypr/user.conf", "user")
            proc = Path(tmp) / "proc"
            pid = proc / "50"
            (pid / "fd").mkdir(parents=True)
            (pid / "fdinfo").mkdir()
            (pid / "stat").write_text("50 (claude) S 1 0\n", encoding="utf-8")
            os_symlink = __import__("os").symlink
            os_symlink(str(agent.resolve()), pid / "fd" / "3")
            os_symlink(str(user.resolve()), pid / "fd" / "4")
            (pid / "fdinfo" / "3").write_text("flags:\t0100000\n", encoding="utf-8")
            (pid / "fdinfo" / "4").write_text("flags:\t0100001\n", encoding="utf-8")
            clients = [{"class": "org.omarchy.agent", "pid": 50, "address": "0x1"}]
            assert files_touched_by_clients(clients, proc_root=proc) == {"hypr/user.conf"}


def test_unidentified_writer_is_left_out_of_undo():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            edited = _write(home, "hypr/edited.conf", "original")
            take_snapshot()
            edited.write_text("user-only", encoding="utf-8")
            seal_session(active_session_path(snapshot_dir()))
            assert restore_snapshot() == []
            assert edited.read_text(encoding="utf-8") == "user-only"


def test_user_edit_before_close_is_kept():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            edited = _write(home, "hypr/edited.conf", "original")
            take_snapshot()
            edited.write_text("agent-version", encoding="utf-8")
            agent_hash = session.content_token(edited)
            state = {"n": 0}

            def clients():
                state["n"] += 1
                if state["n"] < 3:
                    return [{"class": "org.omarchy.agent", "address": "0xagent", "pid": 9}]
                return []

            def observe(_clients):
                if state["n"] == 1:
                    return {"hypr/edited.conf": agent_hash}
                if state["n"] == 2:
                    edited.write_text("user-version", encoding="utf-8")
                return {}

            assert watch_until_done(
                active_session_path(snapshot_dir()),
                clients,
                known=set(),
                sleep_fn=lambda _seconds: None,
                observe_fn=observe,
            )
            conflicts = restore_snapshot()
            assert edited.read_text(encoding="utf-8") == "user-version"
            assert conflicts == ["hypr/edited.conf"]


def test_overlapping_metadata_failure_keeps_the_other_edit():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            target = _write(home, "hypr/a.conf", "v1")
            _record(home, "first", snapshot=True)
            original = session.save_last

            def hook(update, keep_undo=False):
                session.save_last = original
                target.write_text("v-other", encoding="utf-8")
                staging = session.build_staging()
                session.activate_generation(
                    staging,
                    {
                        "lastMode": "edit",
                        "prompt": "other-edit",
                        "caption": "Desk",
                        "kind": "desktop",
                        "contextPath": "",
                        "screenshot": "",
                        "canContinue": True,
                    },
                )
                raise OSError("metadata")

            session.save_last = hook
            try:
                target.write_text("v-this", encoding="utf-8")
                try:
                    _record(home, "this-edit", snapshot=True)
                except OSError as exc:
                    assert "metadata" in str(exc)
                else:
                    raise AssertionError("metadata save should have failed")
            finally:
                session.save_last = original
            last = load_last()
            assert last["prompt"] == "other-edit"
            assert last["canUndo"] is True
            assert (snapshot_dir() / "files" / "hypr" / "a.conf").read_text(encoding="utf-8") == "v-other"


def _fake_proc(proc: Path, pid: int, ppid: int, environ: bytes, fds=()) -> None:
    base = proc / str(pid)
    (base / "fd").mkdir(parents=True)
    (base / "fdinfo").mkdir()
    (base / "stat").write_text(f"{pid} (agent) S {ppid} 0\n", encoding="utf-8")
    (base / "environ").write_bytes(environ)
    for name, target, flags in fds:
        os.symlink(str(target), base / "fd" / str(name))
        (base / "fdinfo" / str(name)).write_text(f"flags:\t{flags}\n", encoding="utf-8")


def test_preexisting_agent_write_is_not_recorded():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            ours = _write(home, "hypr/ours.conf", "original-ours")
            theirs = _write(home, "hypr/theirs.conf", "original-theirs")
            take_snapshot()
            ours.write_text("our-edit", encoding="utf-8")
            theirs.write_text("their-edit", encoding="utf-8")
            proc = Path(tmp) / "proc"
            action = "action-new"
            write_flags = "0100001"
            _fake_proc(proc, 10, 1, b"HOME=/tmp\0", [(4, theirs.resolve(), write_flags)])
            _fake_proc(
                proc,
                20,
                1,
                f"DESKTOP_CONTEXT_ACTION={action}\0".encode(),
                (),
            )
            _fake_proc(proc, 21, 20, b"HOME=/tmp\0", [(4, ours.resolve(), write_flags)])

            def retire(_seconds):
                for pid in (20, 21):
                    path = proc / str(pid)
                    if path.exists():
                        shutil.rmtree(path)

            assert watch_until_done(
                active_session_path(snapshot_dir()),
                action=action,
                proc_root=proc,
                sleep_fn=retire,
            )
            assert restore_snapshot() == []
            assert ours.read_text(encoding="utf-8") == "original-ours"
            assert theirs.read_text(encoding="utf-8") == "their-edit"


def test_tracking_stops_only_when_this_action_exits():
    with tempfile.TemporaryDirectory() as tmp:
        with isolated_home(Path(tmp)) as home:
            edited = _write(home, "hypr/edited.conf", "original")
            take_snapshot()
            edited.write_text("other-session", encoding="utf-8")
            proc = Path(tmp) / "proc"
            _fake_proc(proc, 10, 1, b"HOME=/tmp\0DESKTOP_CONTEXT_ACTION=someone-else\0", [(4, edited.resolve(), "0100001")])
            assert (
                watch_until_done(
                    active_session_path(snapshot_dir()),
                    action="action-new",
                    proc_root=proc,
                    sleep_fn=lambda _seconds: None,
                    max_polls=3,
                )
                is False
            )
            assert not session.session_sealed(active_session_path(snapshot_dir()))
            assert restore_snapshot() == []
            assert edited.read_text(encoding="utf-8") == "other-session"


def test_existing_agent_window_does_not_seal_this_edit():
    known = {"0xalready"}
    seen, done = watch_should_seal(known, set(), {"0xalready"})
    assert seen == set()
    assert done is False
    seen, done = watch_should_seal(known, seen, {"0xalready", "0xnew"})
    assert seen == {"0xnew"}
    assert done is False
    seen, done = watch_should_seal(known, seen, {"0xalready"})
    assert done is True
    assert agent_addresses(
        [{"class": "foot", "initialClass": "org.omarchy.agent", "address": "0x9"}, {"class": "kitty", "address": "0x1"}]
    ) == {"0x9"}


if __name__ == "__main__":
    run_module(globals())
