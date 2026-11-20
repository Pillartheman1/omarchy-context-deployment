#!/usr/bin/env python3
"""Closing the prompt cancels only a recording this plugin started."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "dictation.js"
NODE = shutil.which("node") or "/exec-daemon/node"


CHECK = textwrap.dedent(
    r"""
    const fs = require("fs");
    const src = fs.readFileSync(process.argv[1], "utf8").replace(/^\.pragma library\s*/, "");
    const api = new Function(`${src}
      return { ingest, dictateCommand, cancelCommand, isDictating };
    `)();

    function assert(cond, message) {
      if (!cond) throw new Error(message);
    }

    // A recording started somewhere else is not ours, so close does not cancel it.
    let external = api.cancelCommand(false, false);
    assert(external.command === "", "close canceled an external recording");
    assert(external.owned === false, "close took ownership of an external recording");
    let ignored = api.dictateCommand(false, false, "recording");
    assert(ignored.command === "", "Dictate stopped a recording the plugin did not start");
    assert(api.isDictating(false, "recording") === false, "external recording blocks the prompt");

    // Starting Dictate from idle owns the session. Close then cancels it.
    let started = api.dictateCommand(false, false, "idle");
    assert(started.command === "toggle", "Dictate did not start from idle");
    assert(started.owned === true, "Dictate did not take ownership");
    let pending = api.ingest(started.owned, started.sawActive, "idle");
    assert(pending.owned === true, "ownership dropped before Voxtype reported recording");
    let active = api.ingest(pending.owned, pending.sawActive, "recording");
    assert(active.sawActive === true, "recording was not marked active");
    assert(api.isDictating(active.owned, "recording") === true, "owned recording was ignored");
    let closed = api.cancelCommand(active.owned, active.sawActive);
    assert(closed.command === "cancel", "close did not cancel the plugin recording");
    assert(closed.owned === false, "ownership survived cancel");

    // After our recording returns to idle, a later external one is left alone.
    let finished = api.ingest(active.owned, active.sawActive, "idle");
    assert(finished.owned === false, "finished recording stayed owned");
    let later = api.cancelCommand(finished.owned, finished.sawActive);
    assert(later.command === "", "close canceled a recording after ours finished");

    // Transcribing that we started is canceled, not toggled.
    let spinning = api.dictateCommand(true, true, "transcribing");
    assert(spinning.command === "cancel", "transcribing was not canceled");
    """
)


def test_dictation_ownership():
    result = subprocess.run(
        [NODE, "-e", CHECK, str(LIBRARY)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)


if __name__ == "__main__":
    try:
        test_dictation_ownership()
    except Exception as exc:
        print("FAIL", "test_dictation_ownership", exc)
        raise SystemExit(1)
    print("ok", "test_dictation_ownership")
