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


def test_a_spanning_overlay_a_background_window_and_a_gutter_round_trip() -> None:
    desktop = {
        "windows": [
            {"id": "pet", "entry": "pet.mjs", "kind": "overlay", "displays": "all"},
            {"id": "brain", "entry": "brain.mjs", "kind": "background", "openOnEnable": True},
            {
                "id": "panel",
                "entry": "panel.mjs",
                "kind": "panel",
                "width": 384,
                "height": 404,
                "position": "bottom-right",
                "transparent": True,
                "gutter": 32,
            },
        ]
    }
    m = _manifest(desktop)
    assert m.validate() == []
    assert m.to_dict()["desktop"] == desktop
    assert json.loads(m.signing_payload())["desktop"] == desktop
    # The defaults are left out, so a manifest that predates them signs the same.
    plain = _manifest({"windows": [{"id": "w", "entry": "w.mjs", "kind": "overlay"}]})
    assert plain.to_dict()["desktop"] == {
        "windows": [{"id": "w", "entry": "w.mjs", "kind": "overlay"}]
    }


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
        (_with_window(kind="toast"), "kind must be one of"),
        (_with_window(id="Pet"), "id must match"),
        (_with_window(entry="../escape.mjs"), "entry must be a relative"),
        (_with_window(entry="/abs.mjs"), "entry must be a relative"),
        (_with_window(entry="page.html"), "entry must be a relative"),
        (_with_window(entry="a\\b.mjs"), "entry must be a relative"),
        (_with_window(width=300), "apply to a panel only"),
        (_with_window(gutter=8), "apply to a panel only"),
        (_with_window(displays="everywhere"), "displays must be one of"),
        (
            {"windows": [{"id": "p", "entry": "p.js", "kind": "panel", "displays": "all"}]},
            "apply to an overlay only",
        ),
        (
            {"windows": [{"id": "b", "entry": "b.js", "kind": "background", "width": 400}]},
            "apply to a panel only",
        ),
        (
            {"windows": [{"id": "b", "entry": "b.js", "kind": "background", "displays": "all"}]},
            "apply to an overlay only",
        ),
        (
            {"windows": [{"id": "p", "entry": "p.js", "kind": "panel", "gutter": 49}]},
            "gutter must be an integer",
        ),
        (
            {"windows": [{"id": "p", "entry": "p.js", "kind": "panel", "gutter": True}]},
            "gutter must be an integer",
        ),
        (
            {
                "windows": [
                    {"id": "a", "entry": "a.js", "kind": "overlay", "displays": "all"},
                    {"id": "b", "entry": "b.js", "kind": "overlay", "displays": "all"},
                ]
            },
            "at most one overlay may span all displays",
        ),
        (
            {
                "windows": [
                    {"id": "a", "entry": "a.js", "kind": "background"},
                    {"id": "b", "entry": "b.js", "kind": "background"},
                ]
            },
            "at most one background window",
        ),
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


_PER_PLATFORM: dict[str, Any] = {
    "windows": [{"id": "pet", "entry": "desktop/pet.mjs", "kind": "overlay"}],
    "shortcuts": [
        {
            "id": "toggle-window",
            "accelerator": "Alt+Shift+M",
            "platformAccelerators": {"macos": "CommandOrControl+Shift+M"},
            "window": "pet",
        }
    ],
}


def test_per_platform_defaults_round_trip_and_are_signed() -> None:
    m = _manifest(_PER_PLATFORM)
    assert m.validate() == []
    assert m.to_dict()["desktop"] == _PER_PLATFORM
    assert AppManifest.from_dict(m.to_dict()).desktop == m.desktop
    assert json.loads(m.signing_payload())["desktop"] == _PER_PLATFORM
    shortcut = m.desktop.shortcuts[0]
    assert [shortcut.default_for(p) for p in ("macos", "windows", "linux")] == [
        "CommandOrControl+Shift+M",
        "Alt+Shift+M",
        "Alt+Shift+M",
    ]
    assert _manifest_to_builtin_dict(m)["desktop"] == _PER_PLATFORM


def test_changing_a_platform_default_changes_the_signed_payload() -> None:
    other = json.loads(json.dumps(_PER_PLATFORM))
    other["shortcuts"][0]["platformAccelerators"]["macos"] = "CommandOrControl+Shift+J"
    assert _manifest(_PER_PLATFORM).signing_payload() != _manifest(other).signing_payload()


def _shortcut(**fields: Any) -> dict[str, Any]:
    return {"id": "a", "accelerator": "Alt+Shift+M", "window": "pet", **fields}


@pytest.mark.parametrize(
    ("shortcuts", "needle"),
    [
        (
            [_shortcut(platformAccelerators={"macos": "CommandOrControl+C"})],
            "platformAccelerators.macos",
        ),
        ([_shortcut(platformAccelerators={"darwin": "Alt+Shift+N"})], "may name only"),
        ([_shortcut(platformAccelerators=["Alt+Shift+N"])], "must be an object"),
        (
            [
                _shortcut(platformAccelerators={"macos": "CommandOrControl+Shift+M"}),
                _shortcut(
                    id="b",
                    accelerator="Alt+Shift+N",
                    platformAccelerators={"macos": "Shift+CmdOrCtrl+M"},
                ),
            ],
            "chord on macos",
        ),
    ],
)
def test_a_platform_default_outside_the_rules_is_refused(
    shortcuts: list[dict[str, Any]], needle: str
) -> None:
    desktop = {
        "windows": [{"id": "pet", "entry": "pet.mjs", "kind": "overlay"}],
        "shortcuts": shortcuts,
    }
    assert any(needle in e for e in _errors(desktop)), _errors(desktop)


def test_a_repeated_chord_is_one_refusal_naming_only_the_platforms_it_repeats_on() -> None:
    windows = [{"id": "pet", "entry": "pet.mjs", "kind": "overlay"}]
    everywhere = _errors(
        {
            "windows": windows,
            "shortcuts": [_shortcut(), _shortcut(id="b", accelerator="Shift+Alt+M")],
        }
    )
    assert [e for e in everywhere if "repeats" in e] == [
        "desktop.shortcuts[1]: accelerator repeats another shortcut's chord"
    ]
    on_two = _errors(
        {
            "windows": windows,
            "shortcuts": [
                _shortcut(),
                _shortcut(
                    id="b",
                    accelerator="Shift+Alt+M",
                    platformAccelerators={"macos": "CommandOrControl+Shift+M"},
                ),
            ],
        }
    )
    assert [e for e in on_two if "repeats" in e] == [
        "desktop.shortcuts[1]: accelerator repeats another shortcut's chord on windows, linux"
    ]


def test_two_shortcuts_may_share_a_chord_only_on_different_platforms() -> None:
    desktop = {
        "windows": [{"id": "pet", "entry": "pet.mjs", "kind": "overlay"}],
        "shortcuts": [
            _shortcut(platformAccelerators={"macos": "CommandOrControl+Shift+M"}),
            _shortcut(
                id="b", accelerator="Alt+Shift+N", platformAccelerators={"linux": "Alt+Shift+O"}
            ),
            _shortcut(
                id="c", accelerator="Alt+Shift+O", platformAccelerators={"linux": "Alt+Shift+P"}
            ),
        ],
    }
    assert _errors(desktop) == []


def test_a_desktop_block_asks_for_no_legacy_import() -> None:
    errors = _errors({**_DESKTOP, "legacyMachinePrefs": {"gatewayChoice": "petInstance"}})
    assert any("unknown keys" in e for e in errors), errors
