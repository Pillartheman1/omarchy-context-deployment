#!/usr/bin/env python3
"""Undo snapshots stay scoped and are not thrown away early."""

from __future__ import annotations

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
            calls = {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("swap failed")
                original(src, dst)

            session._rename = flaky
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
            parent = snapshot_dir().parent
            names = [path.name for path in parent.iterdir()]
            assert snapshot_dir().name in names
            assert not any(name.startswith(".snapshot-") for name in names)


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
