"""Slot identity and lookup: keys, caps, origin, and getting or creating a slot.

Key normalising and minting, the live and per-creator caps, ``SlotOrigin``, the
native-subagent output tail and which Slack link is a genuine thread link.
"""

from __future__ import annotations

import hashlib
import re

from kiro_crew.messaging.link import SLACK_NAMESPACE, split_namespaced_channel_id

#: The single ceiling on live slots, owned by the module that owns the slot
#: table (see :meth:`DashboardState.live_slot_count`). Every path that allocates
#: a slot -- session create, chat fork, session import -- tests ``live_slot_count()``
#: against this one number, so raising the ceiling is a single edit and no entry
#: point can silently drift to a different limit.
MAX_LIVE_SLOTS = 500


#: The most live slots ONE creator may hold, as a sub-ceiling under
#: :data:`MAX_LIVE_SLOTS`. The global ceiling alone bounds the total but not the
#: distribution, so a single automated creator working through a nudge loop can
#: reach 500 on its own and every later create -- including the person opening a
#: new chat tab -- gets the 429. That is the availability half of the cap: a
#: bounded resource that one caller may exhaust entirely is not bounded from
#: anybody else's point of view.
#:
#: Deliberately far above real use and far below the global ceiling: a decomposed
#: goal runs on the order of ten concurrent sessions, so 50 never binds honest
#: work, while 450 slots stay reachable by everyone else no matter what one
#: caller does. Enforced in ``session_control.create_session``, which is the only
#: entry point that creates on some OTHER caller's behalf; a person's own new tab
#: and a fork are attributed to nobody and so are bounded by the global ceiling
#: alone.
MAX_SLOTS_PER_CREATOR = 50


def _is_genuine_slack_link(thread_ts: str | None, channel_id: str | None) -> bool:
    """True only for a complete Slack link, never another channel's legacy id."""
    namespaced = split_namespaced_channel_id(channel_id)
    return bool(
        thread_ts and channel_id and (namespaced is None or namespaced[0] == SLACK_NAMESPACE)
    )


# Native kiro-cli subagent reconnect policy. The slot state, writer, and replay
# path all import these bounds so retention cannot drift between modules.
NATIVE_SUBAGENT_OUTPUT_TAIL = 40_000


NATIVE_SUBAGENT_OUTPUT_HARD = 80_000


NATIVE_SUBAGENT_DONE_RESULT_CAP = 8_000


NATIVE_SUBAGENT_DONE_TRUNC_MARKER = "…(earlier output truncated)\n"


NATIVE_SUBAGENT_TERMINAL_KEEP = 50


NATIVE_SUBAGENT_TERMINAL_TTL_SECS = 3600.0


def native_subagent_output_tail(chunks: list[str], limit: int = NATIVE_SUBAGENT_OUTPUT_TAIL) -> str:
    """Join only the trailing ``limit`` characters of native-card output."""
    if limit <= 0:
        return ""
    collected: list[str] = []
    total = 0
    for chunk in reversed(chunks):
        collected.append(chunk)
        total += len(chunk)
        if total >= limit:
            break
    collected.reverse()
    return "".join(collected)[-limit:]


# Auto-minted dashboard slot keys share the shape "<prefix>-<N>-<ts>" where
# <prefix> is chat (the only auto-mint prefix in this fork), <N> is the
# monotonic _slot_counter, and <ts> is a unix second. Minting and index-parsing
# both go through these helpers so the format lives in exactly one place — a
# future change to the key shape can't silently desync the minter from
# reseed_slot_counter() (which would let the post-restart tab<->session
# collision quietly return).
def _mint_slot_key(prefix: str, counter: int, ts: int) -> str:
    """Build an auto-minted slot key of the canonical ``<prefix>-<N>-<ts>`` shape."""
    return f"{prefix}-{counter}-{ts}"


def _slot_index_from_key(key: str) -> int | None:
    """Return the ``<N>`` index from a ``<prefix>-<N>-<ts>`` slot key, else None.

    Non-auto-minted keys (Slack sessions, ascii-sanitized display names) don't
    match the shape and return ``None``. The ``isascii()`` guard keeps a stray
    unicode-digit char (``str.isdigit()`` is True for e.g. superscripts, but
    ``int()`` would raise) from aborting boot-time reseeding.
    """
    parts = key.rsplit("-", 2)
    if len(parts) == 3 and parts[1].isascii() and parts[1].isdigit():
        return int(parts[1])
    return None


# Bare chat-N label matcher used by DashboardState.resolve_slot() for prefix fallback.
# Gates the prefix lookup to prevent broad matches (e.g. bare "chat" binding to any slot).
_CHAT_N_RE = re.compile(r"chat-\d+")


def _ascii_slot_key(name: str) -> str:
    """Return *name* with any character outside printable ASCII replaced by ``-``.

     A slot key becomes the session key (``dashboard:{slot.key}``) that
     kirocrew-core sends as the ``X-Session-Key`` HTTP header on every gateway
     call. Header values are latin-1 per RFC 7230, so a non-latin-1 char (e.g.
     an em-dash from a title-derived slot name) would abort every tool call
    . ASCII control characters (notably CR/LF) are excluded too, so
     a name can never inject into or split the header. Idempotent;
     printable-ASCII names — including the auto-generated ``chat-N-<ts>`` keys —
     are returned unchanged. (Path-separator/traversal containment for keys later
     used as filesystem paths is enforced separately at the persistence layer.)
    """
    return re.sub(r"[^\x20-\x7e]", "-", name)


# Characters that survive the history layer's ``_safe_key()`` filename fold
# (``re.sub(r"[^\w\-.]", "_", key)``). ``re.ASCII`` pins ``\w`` to
# ``[a-zA-Z0-9_]`` — the input is already ASCII-folded, so this matches what
# ``_safe_key`` produces byte-for-byte.
_SLOT_KEY_FILENAME_UNSAFE_RE = re.compile(r"[^\w\-.]", flags=re.ASCII)


# Longest slot key kept verbatim. A transcript is ``<stem>.jsonl`` -- the stem
# is ``dashboard_<key>`` for a dashboard slot and the key itself for a channel
# slot -- and filesystems refuse a name over 255 bytes (ENAMETOOLONG). So a key
# longer than this could never have had a transcript of either kind; only such
# a key is shortened, and a key whose transcript may already exist keeps its
# spelling so a restart still finds it. A longer key keeps its first
# ``_SLOT_KEY_KEEP_CHARS`` characters plus ``-`` and a short sha256 of the whole
# folded key: a valid filename stem, stable across calls, distinct per name, and
# short enough to stay unchanged when folded again.
_SLOT_KEY_MAX_CHARS = 255 - len(".jsonl")


_SLOT_KEY_KEEP_CHARS = 180


_SLOT_KEY_HASH_CHARS = 12


def _normalize_slot_key(name: str) -> str:
    """Return *name* folded to the exact charset of a persisted session filename.

    Guarantees the invariant a restart depends on: for any input,
    ``_safe_key(_history_key_for(key))`` == ``f"dashboard_{key}"`` — i.e. the
    slot key equals its JSONL filename stem minus the ``dashboard_`` prefix.

    Three steps compose: strip a ``dashboard:``/``dashboard_`` transport
    prefix (a full session key or filename stem sometimes reaches slot-name
    positions; ``_history_key_for`` strips the same prefixes when building the
    history key, so such names already share one transcript with their bare
    form and must share one slot), then :func:`_ascii_slot_key` (header
    safety), then a filename fold using the same character class as
    ``history._safe_key``.

    Without the filename fold, a display-style slot name (e.g.
    ``Artifact: My Doc`` from the artifact iterate flow) diverges from its
    sanitized filename stem. After a gateway restart, ``restore_open_slots``
    rehydrates the raw key from ``open_slots.json`` while
    ``restore_recent_sessions`` derives a second slot from the filename stem —
    the dedup guards compare mismatched strings, so the user sees two
    identical sidebar sessions backed by one transcript, and the next
    ``_persist_open_slots`` flush cements both keys. Idempotent;
    auto-generated ``chat-N-<ts>`` keys are returned unchanged. A key longer
    than ``_SLOT_KEY_MAX_CHARS`` is shortened (see the constant) so its
    transcript filename stays under the filesystem's name limit.
    """
    if name.startswith("dashboard:"):
        name = name[len("dashboard:") :]
    while name.startswith("dashboard_"):
        name = name[len("dashboard_") :]
    key = _SLOT_KEY_FILENAME_UNSAFE_RE.sub("_", _ascii_slot_key(name))
    if len(key) > _SLOT_KEY_MAX_CHARS:
        digest = hashlib.sha256(key.encode("ascii")).hexdigest()[:_SLOT_KEY_HASH_CHARS]
        key = f"{key[:_SLOT_KEY_KEEP_CHARS]}-{digest}"
    return key


class SlotOrigin:
    """Slot creation origin — who initiated the slot.

    Used by the WS event scope gate to decide which events an app token may
    receive (e.g. ``slots:user`` grants visibility into ``USER``-origin slots
    regardless of their ``_app`` owner).
    """

    USER = "user"  # initiated from the dashboard UI (no app token)
    APP = "app"  # initiated by an app SDK call (carries owner _app)
    CRON = "cron"  # initiated by a cron job
    SYSTEM = "system"  # gateway-internal (startup, migration, etc.)
