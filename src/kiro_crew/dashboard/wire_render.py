"""Slot-detail messages as JSON text, reusing each row's rendered form.

A slot-detail response renders every row it carries: the display redaction
digests and scans every string in the row, and the result is serialized. A
reader re-opens and re-fetches the same session many times (switching tabs,
reconnecting, the refetch after every turn), and nearly every row is unchanged
between those reads, so almost all of that work repeats.

:func:`prepare_messages_json` keeps the serialized output of each rendered row,
keyed by a digest of the exact row it was rendered from plus everything else
the render reads. A row that comes back unchanged costs one ``json.dumps`` of
the row and one hash instead of a digest per string, the redaction and a
second ``json.dumps``. The text it returns is byte-for-byte what
``json.dumps(_prepare_messages(...))`` returns for the same input.

Filling the memo costs more than rendering without it (the row's digest, a
decoded snapshot, the store), so the fill is kept off the request path: a
session's page (:func:`plan_read`) is answered by the unmemoized render,
exactly as before, until :func:`schedule_fill` has filled the memo for its rows
in a worker thread. Later reads of the page reuse it, and only rows that are new
since the fill are rendered and stored on the way.

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
"""

from __future__ import annotations

import asyncio
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

# The pages a reader has opened, oldest first, each either waiting for its
# background fill or filled. A scope is a session's page shape, so this holds a
# few tuples per session a reader has opened.
_SCOPES_MAX = 4096
_PENDING = "pending"
_FILLED = "filled"
_scopes: OrderedDict[tuple[object, ...], str] = OrderedDict()


def plan_read(scope: tuple[object, ...]) -> tuple[bool, bool]:
    """``(use_memo, fill)`` for one read of *scope* (hashable).

    A page is served from the memo only once its background fill has finished.
    Until then every read of it is the unmemoized render, so no read pays for
    filling; the first one also asks for the fill (``fill``).
    """
    with _memo_lock:
        state = _scopes.get(scope)
        if state is None:
            _scopes[scope] = _PENDING
            while len(_scopes) > _SCOPES_MAX:
                _scopes.popitem(last=False)
            return False, True
        _scopes.move_to_end(scope)
        return state == _FILLED, False


def _settle_scope(scope: tuple[object, ...], filled: bool) -> None:
    """Mark *scope* filled, or forget it so its next read asks for a fill again."""
    with _memo_lock:
        if scope not in _scopes:
            return
        if filled:
            _scopes[scope] = _FILLED
        else:
            del _scopes[scope]


def _context_digest() -> bytes:
    """Digest of everything outside the row that the display redaction reads.

    Read once per render, inside the workspace's allowed-host scope.
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
    with _cu.scoped_exempt_hosts(
        _cu.allowed_hosts_for(workspace), _cu.normalize_workspace(workspace)
    ):
        bypassed = credential_pass_bypassed()
        memo = use_memo and not bypassed
        context = b"" if bypassed else _context_digest()
        parts: list[str | None] = []
        identities: list[bytes | None] = []
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
            parts.append(
                _memo_render(source, digest, running, live_child, context)
                if memo
                else _render_row(json.loads(source), running, live_child)
            )
            identities.append(digest + context + (b"r" if running else b"-"))
    return parts, identities


def _snapshot(m: dict) -> tuple[str, bytes] | None:
    """The row serialized once and its keyed digest, or None when it is not plain JSON."""
    try:
        source = _dumps(m)
    except (TypeError, ValueError, RuntimeError, RecursionError):
        return None
    return source, hashlib.blake2b(source.encode("ascii"), key=_KEY, digest_size=16).digest()


# Fills running in the background, at most this many at once. A read that finds
# the limit reached schedules none: its page fills on its next read instead.
_FILLS_MAX = 2
_fills: set[asyncio.Task[None]] = set()
# Seconds between answering a first read and starting its fill. The fill holds
# the GIL for about as long as a repeat render of the page; started at once it
# competes with sending the response it follows and with the reader's next
# requests on open.
FILL_DELAY_S = 1.0


def schedule_fill(
    scope: tuple[object, ...],
    messages: list[dict],
    running: bool,
    *,
    live_child: str,
    workspace: str | None,
) -> None:
    """Fill the memo with *messages*' rows off the request path, then mark *scope* filled.

    Called on the event loop after :func:`plan_read` asked for a fill. After
    :data:`FILL_DELAY_S`, runs the memoized render in a worker thread and drops
    its result. A fill that cannot start (too many running) or fails leaves the
    scope unfilled, and its next read asks again.
    """

    async def _fill() -> None:
        filled = False
        try:
            await asyncio.to_thread(
                prepare_messages_json,
                messages,
                running,
                live_child=live_child,
                workspace=workspace,
            )
            filled = True
        except Exception:
            logger.debug("slot-detail render memo fill failed", exc_info=True)
        finally:
            _settle_scope(scope, filled)

    def _start() -> None:
        if len(_fills) >= _FILLS_MAX:
            _settle_scope(scope, False)
            return
        task = loop.create_task(_fill())
        _fills.add(task)
        task.add_done_callback(_fills.discard)

    loop = asyncio.get_running_loop()
    loop.call_later(FILL_DELAY_S, _start)


def _memo_render(
    source: str,
    digest: bytes,
    running: bool,
    live_child: str,
    context: bytes,
) -> str | None:
    """The memoized render of the row serialized as *source* (its digest *digest*).

    The key's third slot names a live child only for OAuth rows, which never
    reach the memo, so it is always empty here.
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
    _remember(key, fragment)
    return fragment


def clear_render_memo() -> None:
    """Drop every retained row (tests, and any change that must re-render)."""
    global _memo_bytes
    with _memo_lock:
        _memo.clear()
        _memo_bytes = 0
        _scopes.clear()


def render_memo_info() -> tuple[int, int]:
    """``(entries, accounted_bytes)``."""
    with _memo_lock:
        return len(_memo), _memo_bytes
