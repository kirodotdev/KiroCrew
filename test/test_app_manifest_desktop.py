"""The manifest ``desktop`` block: the windows and shortcuts the desktop shell hosts.

The block is the whole of what an app may ask the shell for, so every rule here is a
refusal at install. ``website/electron/test/app-window-host-rules.test.js`` pins the
shell's copy of the same rules against the same cases.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.desktop_manifest import (
    MAX_DESKTOP_SHORTCUTS,
    MAX_DESKTOP_WINDOWS,
    DesktopConfig,
    canonical_accelerator,
)
from kiro_crew.apps.discovery import _manifest_to_builtin_dict
from kiro_crew.apps.manifest import AppManifest

_BASE: dict[str, Any] = {
    "name": "desk-probe",
    "version": "1.0.0",
    "displayName": "Probe",
    "description": "A probe",
}

_DESKTOP: dict[str, Any] = {
    "windows": [
        {"id": "pet", "entry": "desktop/pet.mjs", "kind": "overlay", "openOnEnable": True},
        {
            "id": "panel",
            "entry": "desktop/panel.mjs",
            "kind": "panel",
            "width": 420,
            "height": 600,
            "position": "top-right",
        },
    ],
    "shortcuts": [
        {"id": "toggle-panel", "accelerator": "CommandOrControl+Shift+M", "window": "panel"}
    ],
}


def _manifest(desktop: Any) -> AppManifest:
    return AppManifest.from_dict({**_BASE, "desktop": desktop})


def _errors(desktop: Any) -> list[str]:
    return _manifest(desktop).validate()


def _with_window(**fields: Any) -> dict[str, Any]:
    return {"windows": [{"id": "w", "entry": "w.mjs", "kind": "overlay", **fields}]}


def test_a_valid_block_round_trips_and_is_signed() -> None:
    m = _manifest(_DESKTOP)
    assert m.validate() == []
    expected = json.loads(json.dumps(_DESKTOP))
    assert m.to_dict()["desktop"] == expected
    assert "desktop" not in m.extra
    assert AppManifest.from_dict(m.to_dict()).desktop == m.desktop
    assert json.loads(m.signing_payload())["desktop"] == expected


def test_a_panel_gets_its_defaults_spelled_out() -> None:
    m = _manifest({"windows": [{"id": "p", "entry": "p.js", "kind": "panel"}]})
    assert m.validate() == []
    assert m.to_dict()["desktop"]["windows"] == [
        {
            "id": "p",
            "entry": "p.js",
            "kind": "panel",
            "width": 400,
            "height": 560,
            "position": "bottom-right",
        }
    ]


def test_an_absent_block_changes_nothing() -> None:
    m = AppManifest.from_dict(dict(_BASE))
    assert m.desktop == DesktopConfig()
    assert "desktop" not in m.to_dict()
    assert "desktop" not in json.loads(m.signing_payload())


def test_retargeting_a_shortcut_changes_the_signed_payload() -> None:
    other = json.loads(json.dumps(_DESKTOP))
    other["shortcuts"][0]["window"] = "pet"
    assert _manifest(_DESKTOP).signing_payload() != _manifest(other).signing_payload()


def test_a_builtin_keeps_its_block_through_discovery() -> None:
    assert _manifest_to_builtin_dict(_manifest(_DESKTOP))["desktop"] == (
        _manifest(_DESKTOP).to_dict()["desktop"]
    )


@pytest.mark.parametrize(
    ("desktop", "needle"),
    [
        ("yes", "desktop must be an object"),
        ({}, "desktop.windows must be a non-empty list"),
        ({"windows": [], "shortcuts": []}, "non-empty list"),
        ({**_with_window(), "menus": []}, "unknown keys ['menus']"),
        (_with_window(kind="background"), "kind must be one of"),
        (_with_window(id="Pet"), "id must match"),
        (_with_window(entry="../escape.mjs"), "entry must be a relative"),
        (_with_window(entry="/abs.mjs"), "entry must be a relative"),
        (_with_window(entry="page.html"), "entry must be a relative"),
        (_with_window(entry="a\\b.mjs"), "entry must be a relative"),
        (_with_window(width=300), "apply to a panel only"),
        (_with_window(alwaysOnTop=False), "unknown keys ['alwaysOnTop']"),
        (_with_window(openOnEnable="yes"), "openOnEnable must be a boolean"),
        (
            {"windows": [{"id": "p", "entry": "p.js", "kind": "panel", "width": 99999}]},
            "width must be an integer",
        ),
        (
            {"windows": [{"id": "p", "entry": "p.js", "kind": "panel", "height": True}]},
            "height must be an integer",
        ),
        (
            {"windows": [{"id": "p", "entry": "p.js", "kind": "panel", "position": "left"}]},
            "position must be one of",
        ),
        (
            {"windows": [_with_window()["windows"][0]] * 2},
            "duplicate id 'w'",
        ),
        (
            {
                "windows": [
                    {"id": f"w{i}", "entry": "w.mjs", "kind": "overlay"}
                    for i in range(MAX_DESKTOP_WINDOWS + 1)
                ]
            },
            f"at most {MAX_DESKTOP_WINDOWS} windows",
        ),
        (
            {**_with_window(), "shortcuts": [{"id": "s", "accelerator": "Ctrl+Alt+K"}]},
            "window must name one of the app's desktop.windows",
        ),
        (
            {
                **_with_window(),
                "shortcuts": [{"id": "s", "accelerator": "Ctrl+Alt+K", "window": "other"}],
            },
            "window must name one of the app's desktop.windows",
        ),
        (
            {
                **_with_window(),
                "shortcuts": [
                    {"id": "a", "accelerator": "Ctrl+Alt+K", "window": "w"},
                    {"id": "b", "accelerator": "alt+control+k", "window": "w"},
                ],
            },
            "repeats another shortcut's chord",
        ),
        (
            {
                **_with_window(),
                "shortcuts": [
                    {"id": f"s{i}", "accelerator": f"Ctrl+Alt+{i}", "window": "w"}
                    for i in range(MAX_DESKTOP_SHORTCUTS + 1)
                ],
            },
            f"at most {MAX_DESKTOP_SHORTCUTS} shortcuts",
        ),
    ],
)
def test_a_block_outside_the_rules_is_refused(desktop: Any, needle: str) -> None:
    errors = _errors(desktop)
    assert any(needle in e for e in errors), errors


@pytest.mark.parametrize(
    "accelerator",
    [
        "CommandOrControl+C",  # one modifier: would take copy from every program
        "Ctrl+V",
        "CmdOrCtrl+Ctrl+C",  # Ctrl+C on Windows and Linux
        "Command+Super+V",  # Cmd+V on macOS
        "CommandOrControl+Command+S",  # Cmd+S on macOS
        "Shift+Alt",  # no key
        "Shift+Shift+K",
        "Ctrl+Hyper+K",
        "Ctrl+Alt+Tab",  # unsupported key
        "Ctrl+Alt+",
        "",
        "Ctrl+Alt+" + "K" * 70,
    ],
)
def test_a_chord_an_app_may_not_claim_is_refused(accelerator: str) -> None:
    chord, reason = canonical_accelerator(accelerator)
    assert chord == ""
    assert reason
    errors = _errors(
        {**_with_window(), "shortcuts": [{"id": "s", "accelerator": accelerator, "window": "w"}]}
    )
    assert any("accelerator" in e for e in errors), errors


def test_shift_plus_one_other_modifier_is_two_modifiers() -> None:
    assert canonical_accelerator("Shift+CmdOrCtrl+k") == ("CommandOrControl+Shift+K", "")
    assert canonical_accelerator("alt+super+F12") == ("Alt+Super+F12", "")
    assert canonical_accelerator("Option+Meta+space") == ("Alt+Super+Space", "")
    assert canonical_accelerator("Shift+Shift+K")[0] == ""


def test_an_entry_escaping_the_app_root_is_refused_on_disk(tmp_path: Path) -> None:
    outside = tmp_path / "outside.mjs"
    outside.write_text("export default 1", encoding="utf-8")
    root = tmp_path / "app"
    root.mkdir()
    (root / "link.mjs").symlink_to(outside)
    m = _manifest({"windows": [{"id": "w", "entry": "link.mjs", "kind": "overlay"}]})
    errors = m.validate(root)
    assert any("desktop.windows entry" in e for e in errors), errors
