"""The name-seeded default ICON a headless crew-create wears when no avatar is
supplied -- so every create path, UI or headless, defaults a crewmate to an
icon, never the trait ghost a missing avatar falls back to on render.

The frontend already sends ``avatar: defaultLookFor(name)`` on create (the
dialog and the template-enroll action). The headless paths -- ``POST /api/agents``
with no ``avatar`` and ``kirocrew agent create`` -- posted nothing, so the
record stored an empty override and the roster drew the ghost. This module
produces the SAME ``{"kind": "icon", "pose", "bg"}`` the UI would have sent, so
the stored value matches the UI preview for a given name, and applies it at the
one shared backend create point both headless paths pass through.

Parity is the whole point, so the pick is reproduced exactly, not approximated.
``website/src/lib/avatarPoses.ts`` draws the pose and background through
DiceBear's seeded ``prng`` (``@dicebear/core`` 9.4.3) -- the same generator the
ghost tier uses -- via ``createAvatar({schema: {}, create}, {seed}).toJson()``.
This re-implements that generator and, critically, the exact sequence of
``prng`` draws ``createAvatar`` makes before the style's deferred ``extra``
closure runs its ``prng.pick`` (the background-type pick, the two draws
``getBackgroundColors`` makes for an empty colour list, and the
background-rotation integer). Miss one draw and every pick shifts. The parity is
pinned name-for-name against the real frontend in
``test/test_crew_avatar_seed.py``.

The seed is UTF-16: DiceBear hashes ``seed.charCodeAt(i)`` over the string's
UTF-16 code units and bounds it to ``charCodeAt``'s first 1024 units, so the
hash is computed over code units here too -- a name with an astral character (an
emoji) is two units, and hashing its Unicode code point instead would diverge.
"""

from __future__ import annotations

import math

#: The shipped icon poses, in picker order -- mirrors ``POSE_IDS`` in
#: ``avatarPoses.ts``. The pick is an index into this list, so its ORDER is part
#: of the parity contract, not just its membership.
POSE_IDS: tuple[str, ...] = (
    "pose-1",
    "pose-2",
    "pose-3",
    "pose-4",
    "pose-5",
    "pose-6",
    "pose-7",
    "pose-8",
    "pose-9",
    "pose-10",
    "pose-11",
)

#: The pose a blank/whitespace seed falls back to (``DEFAULT_POSE``).
DEFAULT_POSE: str = POSE_IDS[0]

#: Brand purple -- the background a blank seed falls back to (``DEFAULT_POSE_BG``).
DEFAULT_POSE_BG: str = "#9046ff"

#: The icon background palette: brand purple followed by the ghost tile colours
#: (``ICON_BG_PALETTE = [BRAND_PURPLE, ...TILES]`` in ``avatarPoses.ts`` /
#: ``kiroGhostAvatar.ts``). Order is part of the parity contract.
ICON_BG_PALETTE: tuple[str, ...] = (
    DEFAULT_POSE_BG,
    "#de2121",
    "#21de21",
    "#21a5de",
    "#3d259d",
    "#eeae4f",
    "#ee4fee",
    "#259d85",
    "#9d2561",
    "#9d6725",
    "#25679d",
    "#979d25",
    "#ee4f7e",
    "#21d4de",
    "#ee7e4f",
)

#: The seed a blank name falls back to, so an unnamed crew still gets a stable
#: icon rather than the default pose/colour (``BLANK_LOOK_SEED`` in
#: ``avatarPoses.ts``). ``validate_member_name`` refuses an empty or
#: edge-whitespace name before any create reaches this module, so this only ever
#: bites a direct caller, but it mirrors the frontend so the two cannot drift.
BLANK_LOOK_SEED: str = "crewmate"

_PRNG_MIN = -2147483648
_PRNG_MAX = 2147483647
_MAX_SEED_LENGTH = 1024
_U32 = 0xFFFFFFFF


def _to_int32(value: int) -> int:
    """Coerce to a signed 32-bit int, the way JS ``| 0`` and the shift ops do.

    Every DiceBear PRNG step keeps its accumulator in int32; Python ints are
    unbounded, so each operation that JS performs in 32-bit must be masked and
    sign-folded here or the hash drifts after the first overflow.
    """
    value &= _U32
    return value - 0x100000000 if value & 0x80000000 else value


def _xorshift(value: int) -> int:
    """One xorshift round, matching ``@dicebear/core``'s ``prng`` exactly.

    The right shift is ARITHMETIC (sign-propagating), like JS ``>>`` on a 32-bit
    value: ``value >> 17`` on a Python int that already carries the sign bit is
    arithmetic, so no extra masking is needed on that step -- only the two left
    shifts can overflow 32 bits and are folded back.
    """
    value = _to_int32(value ^ ((value << 13) & _U32))
    value = _to_int32(value ^ (value >> 17))
    value = _to_int32(value ^ ((value << 5) & _U32))
    return value


def _utf16_code_units(text: str) -> list[int]:
    """The UTF-16 code units of ``text`` -- what JS ``charCodeAt`` iterates.

    An astral code point (>= U+10000, e.g. most emoji) becomes a surrogate PAIR
    of two units, and the hash walks units, not code points, so a Python port
    that iterated ``ord(ch)`` over code points would diverge on any such name.
    """
    units: list[int] = []
    for ch in text:
        cp = ord(ch)
        if cp <= 0xFFFF:
            units.append(cp)
        else:
            cp -= 0x10000
            units.append(0xD800 + (cp >> 10))
            units.append(0xDC00 + (cp & 0x3FF))
    return units


def _hash_seed(units: list[int]) -> int:
    """DiceBear's ``hashSeed``: a djb2-shaped accumulator xorshifted each step."""
    h = 0
    for unit in units:
        h = _to_int32(((h << 5) - h + unit) & _U32)
        h = _xorshift(h)
    return h


class _Prng:
    """A faithful port of ``@dicebear/core``'s seeded PRNG (``utils/prng.js``).

    Only the three methods the icon seeding reaches are ported -- ``next``,
    ``integer``, ``pick`` and the ``shuffle`` that ``getBackgroundColors``
    calls. ``bool`` and ``string`` are unused here and omitted deliberately
    rather than ported untested.
    """

    def __init__(self, seed: str) -> None:
        units = _utf16_code_units(seed)[:_MAX_SEED_LENGTH]
        # ``|| 1``: a seed that hashes to 0 would make ``next`` a fixed point.
        self.value = _hash_seed(units) or 1

    def next(self) -> int:
        self.value = _xorshift(self.value)
        return self.value

    def integer(self, minimum: int, maximum: int) -> int:
        span = (self.next() - _PRNG_MIN) / (_PRNG_MAX - _PRNG_MIN)
        return math.floor(span * (maximum + 1 - minimum) + minimum)

    def pick(self, choices: list[str], fallback: str) -> str:
        if not choices:
            # An empty list still advances the generator, by contract, so the
            # draw count stays identical whether or not there was anything to pick.
            self.next()
            return fallback
        # `integer(0, len-1)` can return `len` when the draw lands on the PRNG's
        # maximum (the scale factor reaches 1.0), so the index is bounds-checked:
        # JS reads `arr[i]` as `undefined` there and its `?? fallback` yields the
        # fallback, which an unchecked Python index would instead raise
        # IndexError on. The generator is advanced either way, so parity holds.
        index = self.integer(0, len(choices) - 1)
        return choices[index] if 0 <= index < len(choices) else fallback

    def shuffle(self, items: list[str]) -> list[str]:
        # Each shuffle advances the MAIN generator exactly once (to seed an
        # internal one), then does its Fisher-Yates work on that internal PRNG.
        internal = _Prng(str(self.next()))
        working = list(items)
        for i in range(len(working) - 1, 0, -1):
            j = internal.integer(0, i)
            working[i], working[j] = working[j], working[i]
        return working


def _advance_for_background(prng: _Prng) -> None:
    """Make the exact ``prng`` draws ``createAvatar`` makes before the style's
    deferred ``extra`` closure runs, for the default options a ``{schema: {}}``
    style resolves to (``backgroundType: ['solid']``, no ``backgroundColor``,
    ``backgroundRotation: [0, 360]``).

    ``createAvatar`` runs these BETWEEN building the result (whose ``extra`` is a
    deferred closure that has not drawn yet) and ``toJson`` invoking that
    closure -- so the pose/bg pick is NOT the generator's first draw. The three
    steps are, in order:

    1. ``prng.pick(['solid'], 'solid')`` -- one draw.
    2. ``getBackgroundColors(prng, [], 'solid')`` -- ``shuffle([])`` advances the
       main generator once, then the ``length <= 1`` branch advances it once
       more: two draws.
    3. ``prng.integer(0, 360)`` for the rotation -- one draw.
    """
    prng.pick(["solid"], "solid")
    # getBackgroundColors with an empty colour list: shuffle([]) draws once to
    # seed its internal PRNG, then the length<=1 branch calls prng.next() once.
    prng.shuffle([])
    prng.next()
    prng.integer(0, 360)


def _seeded_icon_pick(seed: str, choices: tuple[str, ...], fallback: str) -> str:
    """Draw one entry from ``choices`` deterministically from ``seed``, matching
    ``seededIconPick`` in ``avatarPoses.ts`` through the shared generator.
    """
    prng = _Prng(seed)
    _advance_for_background(prng)
    return prng.pick(list(choices), fallback)


def _look_seed(name: str) -> str:
    """The seed a name derives its icon from: the trimmed name, or the blank
    fallback for an empty one -- the backend twin of ``lookSeed(name)``.

    JS ``String.prototype.trim`` strips the ECMAScript WhiteSpace + LineTerminator
    set, which is Unicode whitespace plus U+FEFF (the BOM / zero-width no-break
    space). Python ``str.strip`` strips Unicode whitespace but NOT U+FEFF, so the
    BOM is stripped explicitly here to match. In practice ``validate_member_name``
    has already refused an edge-whitespace name before any create reaches this
    module, so the trim only ever fires for a direct caller -- but mirroring it
    keeps the two implementations from drifting.
    """
    return name.strip().strip("\ufeff").strip() or BLANK_LOOK_SEED


def seed_icon_pose(name: str) -> str:
    """The pose a name's default icon wears -- the backend twin of ``seedIconPose``."""
    seed = _look_seed(name)
    return _seeded_icon_pick(seed, POSE_IDS, DEFAULT_POSE)


def seed_icon_bg(name: str) -> str:
    """The background a name's default icon wears -- the backend twin of
    ``seedIconBg``. Salted with a ``:bg`` suffix so colour and pose are drawn
    from independent streams and never move in lockstep.
    """
    seed = _look_seed(name)
    return _seeded_icon_pick(seed + ":bg", ICON_BG_PALETTE, DEFAULT_POSE_BG)


def default_icon_avatar(name: str) -> dict[str, str]:
    """The name-seeded default ICON a create applies when no avatar was supplied
    -- the backend twin of ``defaultLookFor(name)``, producing the SAME
    ``{"kind": "icon", "pose", "bg"}`` the UI posts for the same name.

    Call this at the shared backend create point ONLY when the caller supplied
    no avatar; an explicitly supplied avatar is never overridden. The result is
    a plain validated-shape override (``_safe_avatar`` accepts it unchanged).
    """
    return {"kind": "icon", "pose": seed_icon_pose(name), "bg": seed_icon_bg(name)}
