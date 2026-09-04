#!/usr/bin/env python3
"""Offline checks for portable click context."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from context import (  # noqa: E402
    actions_for,
    as_int,
    bar_edges,
    crop_box,
    monitor_at,
    point_in_box,
    point_on_bar,
    reserved_edges,
)


def test_reserved_hyprland_list():
    edges = reserved_edges({"reserved": [10, 26, 4, 8]})
    assert edges == {"left": 10, "top": 26, "right": 4, "bottom": 8}


def test_reserved_dict():
    edges = reserved_edges({"reserved": {"top": 40, "bottom": 12}})
    assert edges["top"] == 40
    assert edges["bottom"] == 12
    assert edges["left"] == 0


def test_bar_hit_bottom():
    monitor = {"width": 1920, "height": 1080, "reserved": [0, 0, 0, 40]}
    assert point_on_bar(100, 1050, monitor)
    assert not point_on_bar(100, 200, monitor)


def test_bar_hit_left():
    monitor = {"width": 1920, "height": 1080, "reserved": [48, 0, 0, 0]}
    assert point_on_bar(10, 400, monitor)
    assert not point_on_bar(200, 400, monitor)


def test_actions_never_use_qml_id():
    actions = actions_for("window", {})
    assert [a["actionId"] for a in actions] == ["edit", "inspect"]
    for action in actions:
        assert "id" not in action


def test_zero_reserved_falls_back_to_an_edge():
    monitor = {"width": 1920, "height": 1200, "reserved": [0, 0, 0, 0]}
    edges = bar_edges(monitor)
    assert sum(edges.values()) > 0


def test_crop_box_stays_on_monitor():
    monitor = {"x": 100, "y": 200, "width": 800, "height": 600}
    left, top, width, height = crop_box(100, 200, monitor)
    assert left >= 100
    assert top >= 200
    assert left + width <= 900
    assert top + height <= 800
    assert width == 512
    assert height == 320


def test_crop_box_near_right_edge():
    monitor = {"x": 0, "y": 0, "width": 400, "height": 300}
    left, top, width, height = crop_box(399, 299, monitor)
    assert left == 0
    assert top == 0
    assert width == 400
    assert height == 300


def test_as_int_handles_none_and_junk():
    assert as_int(None) == 0
    assert as_int("12") == 12
    assert as_int("nope") == 0


def test_point_in_box_rejects_objects():
    assert not point_in_box(10, 10, {"x": 0, "y": 0}, {"w": 20, "h": 20})
    assert point_in_box(10, 10, [0, 0], [20, 20])


def test_monitor_at_skips_disabled_and_uses_focused():
    monitors = [
        {"name": "eDP-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "disabled": True},
        {"name": "DP-2", "x": 1920, "y": 0, "width": 2560, "height": 1440, "focused": True},
    ]
    hit = monitor_at(2000, 10, monitors)
    assert hit["name"] == "DP-2"
    gap = monitor_at(-10, -10, monitors)
    assert gap["name"] == "DP-2"


if __name__ == "__main__":
    tests = [fn for name, fn in list(globals().items()) if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("ok", fn.__name__)
        except Exception as exc:
            failed += 1
            print("FAIL", fn.__name__, exc)
    raise SystemExit(failed)
