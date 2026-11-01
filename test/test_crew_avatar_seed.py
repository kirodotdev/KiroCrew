"""Parity tests for the backend name-seeded default ICON (``crew_avatar_seed``).

The headless create paths (``POST /api/agents``, ``kirocrew agent create``) must
default a new crew with no avatar to the SAME ``{kind: icon, pose, bg}`` the
frontend posts for the same name, so the stored value matches the UI preview. The frontend derives pose/bg through ``@dicebear/core``'s seeded
PRNG in ``website/src/lib/avatarPoses.ts``; the backend re-implements that
generator and the exact draw sequence ``createAvatar`` makes.

Three things are pinned here:

1. **Parity.** ``EXPECTED`` is a frozen table of ``name -> (pose, bg)`` captured
   from the REAL frontend (``@dicebear/core`` 9.4.3 driving the actual
   ``seedIconPose`` / ``seedIconBg``). The backend must reproduce every row. The
   battery spans ASCII, case, CJK, accented, an astral emoji (a UTF-16 surrogate
   pair -- the case a code-point hash would get wrong), whitespace to trim, and a
   long name. To regenerate after an intentional frontend change, run
   ``seededIconPick`` from ``avatarPoses.ts`` over these names and replace the
   table; the drift guard below says when that is necessary.

2. **Determinism.** The same name always yields the same icon, and the shape is
   exactly the validated ``{kind, pose, bg}`` override.

3. **Drift.** The pose list, background palette, and default constants are read
   back out of the frontend source and asserted equal to the backend's copies,
   so a change to the shipped art set (a new pose, a recoloured tile) fails this
   test and forces the two sides -- and the frozen table -- to be re-synced,
   rather than letting the headless default silently diverge from the UI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kiro_crew.crew_avatar_seed import (
    BLANK_LOOK_SEED,
    DEFAULT_POSE,
    DEFAULT_POSE_BG,
    ICON_BG_PALETTE,
    POSE_IDS,
    default_icon_avatar,
    seed_icon_bg,
    seed_icon_pose,
)

# Repo root from THIS file's location, so the frontend-source reads resolve under
# xdist workers that may change CWD. test/ -> repo root is one parent.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_AVATAR_POSES_TS = _REPO_ROOT / "website" / "src" / "lib" / "avatarPoses.ts"
_GHOST_TS = _REPO_ROOT / "website" / "src" / "lib" / "kiroGhostAvatar.ts"
_WEBSITE_PACKAGE_JSON = _REPO_ROOT / "website" / "package.json"

# The @dicebear/core version the frozen EXPECTED table was captured from. The
# seeding reproduces that version's PRNG and draw sequence exactly; a bump can
# change the draw order, moving the frontend's pick while the backend keeps
# producing the old result. Pinning it here turns a Dependabot bump of
# website/package.json into a failed test that forces the table to be recaptured
# and this constant re-pinned, rather than a silent UI/headless mismatch.
_DICEBEAR_CAPTURE_VERSION = "9.4.3"

# name -> (pose, bg), captured from the real frontend seeding. The empty and
# whitespace-only names exercise the blank-seed fallback (seed == "crewmate").
EXPECTED: dict[str, tuple[str, str]] = {
    "crewmate": ("pose-8", "#eeae4f"),
    "Atlas": ("pose-7", "#de2121"),
    "atlas": ("pose-6", "#de2121"),
    "Scout": ("pose-10", "#ee4f7e"),
    "Nova": ("pose-11", "#de2121"),
    "Orion": ("pose-3", "#259d85"),
    "zephyr": ("pose-7", "#21de21"),
    "Mira": ("pose-9", "#eeae4f"),
    "A": ("pose-6", "#9046ff"),
    "ab": ("pose-11", "#21de21"),
    "abc": ("pose-7", "#259d85"),
    "Zephyr Quartz": ("pose-6", "#25679d"),
    "hello world": ("pose-9", "#21de21"),
    "日本語クルー": ("pose-5", "#ee7e4f"),
    "Ömer": ("pose-11", "#eeae4f"),
    "Café": ("pose-9", "#eeae4f"),
    "naïve": ("pose-5", "#21d4de"),
    "🚀 Rocket": ("pose-2", "#9d6725"),
    "x" * 50: ("pose-9", "#ee7e4f"),
    "Data-Pipeline-7": ("pose-6", "#25679d"),
    "test_agent": ("pose-5", "#9d6725"),
    "MixedCASEName": ("pose-7", "#979d25"),
    # Blank / whitespace-only -> the "crewmate" fallback seed, same as above.
    "": ("pose-8", "#eeae4f"),
    "   ": ("pose-8", "#eeae4f"),
}


@pytest.mark.parametrize(("name", "expected"), list(EXPECTED.items()))
def test_backend_seeding_matches_frontend(name: str, expected: tuple[str, str]) -> None:
    """The backend reproduces the frontend's pose/bg pick for each name."""
    exp_pose, exp_bg = expected
    assert seed_icon_pose(name) == exp_pose
    assert seed_icon_bg(name) == exp_bg
    assert default_icon_avatar(name) == {"kind": "icon", "pose": exp_pose, "bg": exp_bg}


def test_default_icon_avatar_shape() -> None:
    """The override is exactly the validated icon shape -- kind, pose, bg, nothing else."""
    av = default_icon_avatar("Atlas")
    assert set(av) == {"kind", "pose", "bg"}
    assert av["kind"] == "icon"
    assert av["pose"] in POSE_IDS
    assert re.fullmatch(r"#[0-9a-f]{6}", av["bg"])


def test_seeding_is_deterministic() -> None:
    """Same name -> same icon, every call."""
    for name in ("Atlas", "日本語クルー", "🚀 Rocket", ""):
        assert default_icon_avatar(name) == default_icon_avatar(name)


def test_blank_and_whitespace_seed_the_same_as_the_fallback() -> None:
    """An empty or all-whitespace name seeds from BLANK_LOOK_SEED, not the default
    pose/colour -- a nameless crew still gets a stable icon."""
    fallback = default_icon_avatar(BLANK_LOOK_SEED)
    assert default_icon_avatar("") == fallback
    assert default_icon_avatar("   ") == fallback
    # The fallback is a real seeded pick, not the bare defaults.
    assert fallback != {"kind": "icon", "pose": DEFAULT_POSE, "bg": DEFAULT_POSE_BG}


def test_names_spread_across_the_pose_and_bg_sets() -> None:
    """The point of seeding: a roster of un-customized crews is not all one face.
    The frozen battery must land on several distinct poses and colours."""
    poses = {seed_icon_pose(n) for n in EXPECTED}
    bgs = {seed_icon_bg(n) for n in EXPECTED}
    assert len(poses) >= 6
    assert len(bgs) >= 6


def _ts_string_array(source: str, const: str) -> list[str]:
    """Pull a ``const <name> ... = [ '...', ... ]`` string array out of TS source.

    Anchored on the ``const <name>`` DECLARATION, not a bare mention, so a
    comment referencing the name earlier in the file cannot capture an empty
    match. The ``=`` run stops at the first ``[`` after the name.
    """
    m = re.search(rf"const\s+{const}\b[^=]*=\s*\[(.*?)\]", source, re.DOTALL)
    assert m, f"could not find const {const} in frontend source"
    return re.findall(r"'([^']*)'", m.group(1))


def test_pose_ids_match_frontend_source() -> None:
    """POSE_IDS mirrors avatarPoses.ts; a new/removed pose fails here and forces a
    re-sync of the backend list AND the frozen parity table."""
    src = _AVATAR_POSES_TS.read_text(encoding="utf-8")
    assert _ts_string_array(src, "POSE_IDS") == list(POSE_IDS)


def test_palette_matches_frontend_source() -> None:
    """ICON_BG_PALETTE == [BRAND_PURPLE, ...TILES] from kiroGhostAvatar.ts."""
    ghost = _GHOST_TS.read_text(encoding="utf-8")
    brand = re.search(r"BRAND_PURPLE\s*=\s*'([^']+)'", ghost)
    assert brand, "could not find BRAND_PURPLE in frontend source"
    tiles = _ts_string_array(ghost, "TILES")
    assert list(ICON_BG_PALETTE) == [brand.group(1), *tiles]
    # The backend's own default-bg constant is that brand purple.
    assert DEFAULT_POSE_BG == brand.group(1)


def test_defaults_match_frontend_source() -> None:
    """DEFAULT_POSE and BLANK_LOOK_SEED mirror avatarPoses.ts."""
    src = _AVATAR_POSES_TS.read_text(encoding="utf-8")
    # DEFAULT_POSE = POSE_IDS[0]
    assert DEFAULT_POSE == POSE_IDS[0]
    blank = re.search(r"BLANK_LOOK_SEED\s*=\s*'([^']+)'", src)
    assert blank, "could not find BLANK_LOOK_SEED in frontend source"
    assert BLANK_LOOK_SEED == blank.group(1)


def test_dicebear_version_matches_the_capture() -> None:
    """The pinned @dicebear/core version is the one the frozen table was captured
    from. A Dependabot bump of website/package.json can change the PRNG draw
    order, moving the frontend's pick while this backend keeps the old result;
    pinning the version here makes that bump fail until the table is recaptured
    and _DICEBEAR_CAPTURE_VERSION re-pinned, instead of a silent mismatch."""
    pkg = json.loads(_WEBSITE_PACKAGE_JSON.read_text(encoding="utf-8"))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    pinned = deps.get("@dicebear/core")
    assert pinned is not None, "@dicebear/core not found in website/package.json"
    assert pinned == _DICEBEAR_CAPTURE_VERSION, (
        f"@dicebear/core is pinned to {pinned} but the parity table was captured "
        f"from {_DICEBEAR_CAPTURE_VERSION}. Recapture EXPECTED from the new "
        f"version's seedIconPose/seedIconBg and update _DICEBEAR_CAPTURE_VERSION."
    )


def test_pick_tolerates_the_max_draw_index() -> None:
    """A draw landing on the PRNG maximum makes integer(0, len-1) return len, one
    past the end. JS reads arr[len] as undefined and its `?? fallback` yields the
    fallback; the Python port must do the same, not raise IndexError -- otherwise
    a valid crew name whose seed hits that draw crashes headless creation."""
    from kiro_crew.crew_avatar_seed import _PRNG_MAX, _Prng

    prng = _Prng("anything")
    # Force the NEXT draw to the maximum (integer() calls next() and scales its
    # return): span becomes 1.0, so integer(0, len-1) computes len, out of range.
    prng.next = lambda: _PRNG_MAX  # type: ignore[method-assign]
    choices = ["a", "b", "c"]
    assert prng.integer(0, len(choices) - 1) == len(choices)
    prng2 = _Prng("anything")
    prng2.next = lambda: _PRNG_MAX  # type: ignore[method-assign]
    # pick must fall back rather than raise on that out-of-range index.
    assert prng2.pick(choices, "FALLBACK") == "FALLBACK"


def test_no_name_in_a_battery_crashes() -> None:
    """Belt-and-braces: the public helpers never raise across the battery."""
    for name in EXPECTED:
        default_icon_avatar(name)
        seed_icon_pose(name)
        seed_icon_bg(name)
