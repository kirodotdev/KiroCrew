"""Slot-detail messages as JSON text, reusing each row's rendered form.

A slot-detail response renders every row it carries: the display redaction
digests and scans every string in the row, and the result is serialized. A
reader re-opens and re-fetches the same session many times (switching tabs,
reconnecting, the refetch after every turn), and nearly every row is unchanged
between those reads, so almost all of that work repeats.

:func:`prepare_message_rows` renders a page row by row and gives each row an
identity: a keyed digest of the exact row it was rendered from plus everything
else the render reads. The incremental cursor read (``slot_detail_delta``) needs
those identities anyway, so it also keeps each row's serialized output under
its identity, and a row that comes back unchanged skips the redaction and the
output ``json.dumps``. The text is byte-for-byte what
``json.dumps(_prepare_messages(...))`` returns for the same input.

Only that cursor read uses this module. A plain slot-detail read renders with
``_prepare_messages`` and no row identities: the identity serializes and hashes
every source row, which costs more than the per-string display cache it would
skip, so a memo in front of a read that does not need identities is a net cost.

Security properties:

* What the memo retains is the rendered output, already redacted, which is
  what the response sends. No unredacted input is kept: the key is a keyed
  BLAKE2b digest of the source row under a random per-process key, so a
  retained key cannot be checked offline against a guessed plaintext.
* The key covers the whole source row (any field, nesting or key order) and
  reuses the per-string display cache's context key. Workspace host permissions
  are keyed separately because link restoration reads those alone. OAuth rows
  are never memoized: each read checks live child identity and endpoint approval.
* Inside an owner-view scope (``credential_pass_bypassed``) the memo is neither
  read nor written, so an unredacted render can never be served to a later
  reader, and a redacted one is never served to the owner view.
* A row that is not plain JSON (a value ``json.dumps`` refuses, or a dict the
  event loop resizes while it is read) is rendered as before, without the memo.
* On a miss the row is rendered from the decoded copy of the bytes that were
  hashed, not from the live dict, so a row the event loop changes mid-read can
  never file one row's output under another row's key.
* The redaction context is digested before the rows are rendered and again
  after. When it moved, every row the memo answered is rendered again against
  the live context and nothing from that read is stored, so an exemption that
  came and went mid-read can neither be served from a stale entry nor leave an
  output it relaxed filed under a context that never had it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock

from kiro_crew.dashboard import chat_utils as _cu
from kiro_crew.security.redaction_switch import credential_pass_bypassed

logger = logging.getLogger(__name__)

# Bounded by the bytes of the retained output: the newest pages of well over a
# hundred sessions, or a few long sessions read whole. Each entry is charged
# its output's length plus a fixed overhead for the key and the dict slot, and
# the least recently used entries leave first.
RENDER_MEMO_MAX_BYTES = 16 * 1024 * 1024
RENDER_MEMO_MAX_ENTRIES = 60_000
# Charged per entry on top of the output's length: the 16-byte digest, the key
# tuple, the str header and the OrderedDict slot. Set so the charged total
# matches what tracemalloc attributes to the memo when it is full.
_ENTRY_OVERHEAD = 384
# One row larger than this is rendered every time rather than evicting a
# large share of the memo for itself.
_MAX_ENTRY_BYTES = RENDER_MEMO_MAX_BYTES // 16

_KEY: bytes = secrets.token_bytes(32)
_memo: OrderedDict[tuple[bytes, bytes, str], tuple[str, int]] = OrderedDict()
_memo_bytes = 0
_memo_lock = Lock()


def _make_dumps() -> Callable[[object], str]:
    """``json.dumps`` with its default arguments, without building an encoder per call.

    ``json.dumps`` constructs a C encoder on every call, which is a large share
    of the cost for one small row. This builds it once with the same arguments
    the default encoder uses, so the output is the same bytes. It is built
    without the circular-reference check: a cycle then raises ``RecursionError``
    instead of ``ValueError``, and callers treat both alike. Falls back to
    ``json.dumps`` where the C accelerator is unavailable.
    """
    make = getattr(json.encoder, "c_make_encoder", None)
    if make is None:
        return json.dumps
    defaults = json.JSONEncoder()
    encode = make(
        None,
        defaults.default,
        json.encoder.encode_basestring_ascii,
        None,
        ": ",
        ", ",
        False,
        False,
        True,
    )

    def dumps(o: object) -> str:
        return "".join(encode(o, 0))

    return dumps


_dumps = _make_dumps()


def _context_digest() -> bytes:
    """Digest of everything outside the row that the display redaction reads.

    Read before and after each render, inside the workspace's allowed-host scope.
    """
    # Reuse the string cache's complete dependency key instead of maintaining
    # a second list of redaction inputs. The empty text is constant, so only
    # the redaction context contributes to this part of the row key.
    (redaction_context, _), _ = _cu._display_redaction_cache_key("")
    # Link restoration reads the workspace hosts alone, not their union with
    # policy exemptions. Keep that distinction even if the union is unchanged.
    restored_hosts = _dumps(sorted(_cu.current_scoped_exempt_hosts())).encode("ascii")
    return hashlib.blake2b(redaction_context + restored_hosts, key=_KEY, digest_size=16).digest()


def _remember(key: tuple[bytes, bytes, str], fragment: str) -> None:
    global _memo_bytes
    cost = len(fragment) + _ENTRY_OVERHEAD
    if cost > _MAX_ENTRY_BYTES:
        return
    with _memo_lock:
        if key in _memo:
            _memo.move_to_end(key)
            return
        _memo[key] = (fragment, cost)
        _memo_bytes += cost
        while len(_memo) > RENDER_MEMO_MAX_ENTRIES or _memo_bytes > RENDER_MEMO_MAX_BYTES:
            _, (_, evicted) = _memo.popitem(last=False)
            _memo_bytes -= evicted


def _render_row(m: dict, running: bool, live_child: str) -> str | None:
    """One collapsed row rendered and serialized, or None when it renders as nothing."""
    rows = _cu._prepare_messages_scoped([m], running, live_child=live_child)
    return _dumps(rows[0]) if rows else None


def prepare_messages_json(
    messages: list[dict],
    running: bool,
    *,
    live_child: str,
    workspace: str | None = None,
) -> str:
    """``json.dumps(_prepare_messages(messages, ...))``, reusing unchanged rows.

    Same arguments and the same contract as ``chat_utils._prepare_messages``;
    the result is the JSON array text instead of the list.
    """
    parts, _ = prepare_message_rows(messages, running, live_child=live_child, workspace=workspace)
    return "[" + ", ".join(f for f in parts if f is not None) + "]"


def prepare_message_rows(
    messages: list[dict],
    running: bool,
    *,
    live_child: str,
    workspace: str | None = None,
    use_memo: bool = True,
) -> tuple[list[str | None], list[bytes | None]]:
    """``(fragments, identities)``: each collapsed row's render and what it was rendered from.

    One entry per row of ``_collapse_wire_rows(messages)``, so a position in the
    lists is a position in the page. ``fragments[i]`` is row *i*'s serialized
    render, ``None`` where it renders as nothing; the joined non-``None`` entries
    are the text :func:`prepare_messages_json` returns. ``use_memo=False`` renders
    every row afresh.

    ``identities[i]`` is a keyed digest of the exact source row the fragment was
    rendered from, plus the redaction context and the running flag, so two rows
    with equal identities render the same. Both come from ONE serialization of the
    row: the render reads the decoded copy, so an edit the event loop makes to the
    live row mid-render can never pair one row's fragment with another row's
    identity. ``None`` where no identity can vouch for the render: a streaming
    chunk, an OAuth row (its render reads live endpoint approvals), a row that is
    not plain JSON, and every row inside an owner-view scope.
    """
    with _cu.scoped_exempt_hosts(_cu.allowed_hosts_for(workspace)):
        bypassed = credential_pass_bypassed()
        memo = use_memo and not bypassed
        context = b"" if bypassed else _context_digest()
        parts: list[str | None] = []
        identities: list[bytes | None] = []
        # Misses rendered by this read, stored only once the context is known
        # not to have moved while they were rendered.
        fresh: list[tuple[tuple[bytes, bytes, str], str]] = []
        # (position, source) of every row the memo could have answered.
        looked_up: list[tuple[int, str]] = []
        for m in _cu._collapse_wire_rows(messages):
            # Streaming changes every delta. OAuth URLs depend on revocable
            # endpoint approvals, so neither role is safe or useful to memoize.
            # Compare with ==: a hand-edited row may carry an unhashable role.
            role = m.get("role", "")
            if bypassed or role == "chunk" or role == "mcp_oauth":
                parts.append(_render_row(m, running, live_child))
                identities.append(None)
                continue
            snap = _snapshot(m)
            if snap is None:
                parts.append(_render_row(m, running, live_child))
                identities.append(None)
                continue
            source, digest = snap
            if memo:
                looked_up.append((len(parts), source))
            parts.append(
                _memo_render(source, digest, running, live_child, context, fresh)
                if memo
                else _render_row(json.loads(source), running, live_child)
            )
            identities.append(digest + context + (b"r" if running else b"-"))
        if not bypassed and _context_digest() != context:
            # The exempt sets changed while the rows were rendered: no identity
            # can say which context a fragment was rendered under. A hit was
            # filed under the context from before the change, so every row the
            # memo answered is rendered again against the live one.
            for i, source in looked_up:
                parts[i] = _render_row(json.loads(source), running, live_child)
            return parts, [None] * len(identities)
    for key, fragment in fresh:
        _remember(key, fragment)
    return parts, identities


def _snapshot(m: dict) -> tuple[str, bytes] | None:
    """The row serialized once and its keyed digest, or None when it is not plain JSON."""
    try:
        source = _dumps(m)
    except (TypeError, ValueError, RuntimeError, RecursionError):
        return None
    return source, hashlib.blake2b(source.encode("ascii"), key=_KEY, digest_size=16).digest()


def _memo_render(
    source: str,
    digest: bytes,
    running: bool,
    live_child: str,
    context: bytes,
    fresh: list[tuple[tuple[bytes, bytes, str], str]],
) -> str | None:
    """The memoized render of the row serialized as *source* (its digest *digest*).

    A miss is rendered and appended to *fresh* for the caller to store. The
    key's third slot names a live child only for OAuth rows, which never reach
    the memo, so it is always empty here.
    """
    key = (digest, context, "")
    with _memo_lock:
        hit = _memo.get(key)
        if hit is not None:
            _memo.move_to_end(key)
            return hit[0]
    fragment = _render_row(json.loads(source), running, live_child)
    if fragment is None:
        # Only a chunk row renders as nothing, and chunk rows never reach here.
        return None
    fresh.append((key, fragment))
    return fragment


def clear_render_memo() -> None:
    """Drop every retained row (tests, and any change that must re-render)."""
    global _memo_bytes
    with _memo_lock:
        _memo.clear()
        _memo_bytes = 0


def render_memo_info() -> tuple[int, int]:
    """``(entries, accounted_bytes)``."""
    with _memo_lock:
        return len(_memo), _memo_bytes
