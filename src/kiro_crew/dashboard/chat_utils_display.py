"""What a stored chat row looks like on the dashboard wire.

Display redaction of row text and metadata, with its bounded cache and the memo of
verdicts already found clean; the redaction records and variants a row carries; wire
serialisation; and preparing a slot's stored rows for the client -- collapsing wire
rows, expiring a dead child's OAuth banners, restoring allowed links and dropping
unknown row fields.

Served through :mod:`kiro_crew.dashboard.chat_utils`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections import OrderedDict
from collections.abc import Callable
from threading import RLock
from typing import Any, cast

from kiro_crew.dashboard import display_verdicts as _display_verdicts
from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.dashboard.display_battery import display_battery
from kiro_crew.dashboard.state import DashboardState, _ChatSlot, parse_cls_meta
from kiro_crew.security import (
    CREDENTIAL_REDACTION_TAGS,
    EXFILTRATION_REDACTION_TAG_PREFIX,
    _exempt_exact_hosts,
    bounded_blocked_links,
    oauth_url_contains_credential,
)
from kiro_crew.security.credential_sources import bounded_credential_records
from kiro_crew.security.exfil import (
    current_scoped_exempt_hosts,
    restore_allowed_links,
    scoped_exempt_hosts,
)
from kiro_crew.security.redaction import _token_param_exempt_prefixes
from kiro_crew.security.redaction_allow import allowed_hosts_for

logger = logging.getLogger("kiro_crew.dashboard.chat_utils")


def _redact_value(v):  # type: ignore[no-untyped-def]
    """Recursively redact any value (str, dict, list/tuple, or passthrough)."""
    if isinstance(v, str):
        return _redact_for_display(v)
    if isinstance(v, dict):
        return _redact_meta(v)
    if isinstance(v, (list, tuple)):
        # Snapshot for the same reason as _redact_meta — the flush thread reads
        # containers the event loop is still appending to.
        return [_redact_value(i) for i in list(v)]
    return v


#: Every record set a row carries about its own redactions: the key it lives
#: under, the one bounded constructor that rebuilds it from a transcript line,
#: and the placeholder tags its text must still hold for the records to describe
#: anything. One table, so a helper that moves or bounds records covers every
#: kind at once and a new kind is one row, not a new call site to forget.
REDACTION_RECORD_FIELDS: dict[str, tuple[Callable[[object], list[dict]], tuple[str, ...]]] = {
    "blocked_links": (bounded_blocked_links, (EXFILTRATION_REDACTION_TAG_PREFIX,)),
    "redactions": (bounded_credential_records, tuple(CREDENTIAL_REDACTION_TAGS)),
}

#: Set (True) beside ``blocked_links`` when the writer vouches they are in reply
#: order, which is what lets ``restore_allowed_links`` pair several different
#: addresses on one host. Rows written before it existed may be out of order, so
#: its absence keeps the older rule. It travels with its records, never alone.
BLOCKED_LINKS_ORDERED_KEY = "blocked_links_ordered"


def variant_from_row(row: dict) -> dict:
    """Stash a row as a variant, taking its turn stats and redaction records with it.

    The mirror of ``adopt_variant_text``: that one moves a variant onto the row,
    this one moves the row into the variant list. Both directions carry the
    records because the records cannot be recovered from the text -- the stashed
    content is already redacted, so what they describe is gone and a rescan
    finds nothing to describe. A stash that dropped them would destroy the
    explanation for good, and the reader switching back would be handed a bare
    placeholder with no way to learn what was removed.
    """
    entry = {"content": row.get("content", ""), "ts": row.get("ts", "")}
    meta = row.get("meta")
    if isinstance(meta, dict):
        turn_stats = meta.get("turn_stats")
        if isinstance(turn_stats, dict):
            entry["turn_stats"] = dict(turn_stats)
        # Bounded here, at retention: the row's meta was read off a transcript
        # line, and a variant list outlives the render that would bound it later.
        for key, (bound, _tags) in REDACTION_RECORD_FIELDS.items():
            records = bound(meta.get(key))
            if records:
                entry[key] = records
        if entry.get("blocked_links") and meta.get(BLOCKED_LINKS_ORDERED_KEY) is True:
            entry[BLOCKED_LINKS_ORDERED_KEY] = True
    return entry


def with_bounded_redaction_records(container: dict) -> dict:
    """``container`` with each redaction record set rebuilt through its one
    bounded constructor, or removed when nothing valid is left.

    Every place that RETAINS a row's meta or a variant read off a transcript line
    goes through this, not only the places that display it: a bound applied at
    render leaves the slot holding whatever the line carried. Returns the same
    object when there is no such key, so the common row costs nothing.
    """
    if not any(key in container for key in REDACTION_RECORD_FIELDS):
        return container
    out = dict(container)
    for key, (bound, _tags) in REDACTION_RECORD_FIELDS.items():
        if key not in out:
            continue
        records = bound(out[key])
        if records:
            out[key] = records
        else:
            del out[key]
    return out


def drop_records_without_placeholders(container: dict, text: str) -> None:
    """Remove each record set with no placeholder of its kind in ``text``.

    Records describe one text; a text with no placeholder of their kind has
    nothing for them to explain.
    """
    for key, (_bound, tags) in REDACTION_RECORD_FIELDS.items():
        if key in container and not any(tag in text for tag in tags):
            container.pop(key, None)


def _variant_for_emit(variant: dict) -> dict:
    """A regenerate variant as the client receives it: allowed links restored,
    display-redacted text, and its redaction records rebuilt through their
    bounded constructors. Runs inside the same allowed-host scope as the row it
    belongs to, so a variant shows the links its row shows."""
    bounded = with_bounded_redaction_records(variant)
    text = bounded.get("content", "")
    if isinstance(text, str) and "blocked_links" in bounded:
        text, left = restore_allowed_links(
            text,
            bounded["blocked_links"],
            current_scoped_exempt_hosts(),
            ordered=bounded.get(BLOCKED_LINKS_ORDERED_KEY) is True,
        )
        bounded = {**bounded, "content": text}
        if left:
            bounded["blocked_links"] = left
        else:
            bounded.pop("blocked_links", None)
    return with_bounded_redaction_records(
        {**bounded, "content": redact_display_content(bounded.get("content", ""))}
    )


def adopt_variant_text(row: dict, variant: dict) -> None:
    """Move a row onto one of its variants, text and row-specific metadata together.

    A record describes ONE text: it names what was removed from a placeholder
    standing in that text. A caller that takes a variant's content without its
    records leaves the row explaining something absent from the text on screen.
    The records are replaced when the variant carries them and REMOVED when it
    does not. Turn stats move with the reply too; legacy variants without stats
    clear the active reply's stale stats instead of showing another variant's.
    """
    row["content"] = variant.get("content", "")
    row["ts"] = variant.get("ts", row.get("ts", ""))
    meta = row.get("meta")
    turn_stats = variant.get("turn_stats")
    if isinstance(turn_stats, dict):
        if not isinstance(meta, dict):
            meta = {}
            row["meta"] = meta
        meta["turn_stats"] = dict(turn_stats)
    elif isinstance(meta, dict):
        meta.pop("turn_stats", None)
    for key, (bound, _tags) in REDACTION_RECORD_FIELDS.items():
        records = bound(variant.get(key))
        if records:
            if not isinstance(meta, dict):
                meta = {}
                row["meta"] = meta
            meta[key] = records
        elif isinstance(meta, dict):
            meta.pop(key, None)
    if isinstance(meta, dict):
        # The marker describes the records just moved, so it moves with them.
        meta.pop(BLOCKED_LINKS_ORDERED_KEY, None)
        if meta.get("blocked_links") and variant.get(BLOCKED_LINKS_ORDERED_KEY) is True:
            meta[BLOCKED_LINKS_ORDERED_KEY] = True
    if isinstance(meta, dict) and not meta:
        row.pop("meta", None)


def _redact_meta(meta: dict) -> dict:
    """Recursively redact string values in meta dict.

    Iterates a SNAPSHOT of the dict, never the live object. ``_redact_meta`` is
    reached from ``_save_slot_to_history``, which runs in the flush executor
    thread while the event loop is still mutating that same message's meta
    (streaming tool calls, growing file-change lists). Iterating ``meta.items()``
    directly therefore raised ``RuntimeError: dictionary changed size during
    iteration``, which propagated out of ``_save_slot_to_history`` and aborted
    the whole slot's save — the transcript for that flush was lost.

    A shallow copy per level suffices: the copy's key set is stable, and nested
    containers get their own snapshot from the recursive call.
    """
    return {k: _redact_value(v) for k, v in list(meta.items())}


def _redact_meta_for_role(role: str, meta: dict) -> dict:
    """Redact meta, but preserve role-specific user-actionable external URLs (e.g. mcp_oauth).

    Lives here (the display-redaction module) rather than in chat_persistence
    because it is called on the EMIT path — see _prepare_messages. The
    dependency runs chat_persistence -> chat_utils, so keeping it here lets both
    the save path and the emit path share one implementation without a cycle.
    """
    # Redaction records (REDACTION_RECORD_FIELDS) are born at the redaction that
    # removed each value (chat_runner._flush_segment) and carried with their
    # text; the generic string redaction below would blank them. Preserve them
    # across every role, but the transcript line is attacker-writable, so each
    # set is rebuilt through its one bounded constructor: it re-validates each
    # record, drops any that fails on its own -- never the message -- and bounds
    # the count, because this is a RETENTION point that reads the line and it
    # runs on every render of the message that holds it.
    validated_records = {
        key: bound(meta.get(key)) for key, (bound, _tags) in REDACTION_RECORD_FIELDS.items()
    }

    if role == "mcp_oauth":
        out: dict = {}
        for k, v in list(meta.items()):
            if k in REDACTION_RECORD_FIELDS:
                continue
            if k == "oauth_url" and isinstance(v, str):
                # Two gates, and deliberately NOT a third:
                #   1. http(s)-only — a tampered history line can't smuggle a
                #      javascript:/data: URL into <a href>.
                #   2. URL must not embed an actual credential — a legit OAuth
                #      consent URL never carries credential patterns; presence of
                #      one means it's tampered/bogus.
                #
                # The exfiltration gate is parameter-aware: standard high-entropy
                # OAuth values are exempt only at exact code-owned endpoints, while
                # fixed/encoded credentials, heavy percent encoding, and unknown
                # params remain fail-closed.
                #
                # This function runs on the EMIT path (_prepare_messages), which
                # serves the slot-detail endpoint that the frontend refetches on
                # `chat_done`, on WS reconnect, and on switchSlot. Blanking the URL
                # here therefore hits a PRE-TERMINAL banner: renderMcpOAuthMessage
                # returns null when `oauth_url` is empty and neither completed nor
                # failed is set, so the Authorize banner would silently vanish and
                # the user could never authorize the server. Keeping the two gates
                # aligned is what prevents that.
                lower = v.lower()
                safe_scheme = lower.startswith("https://") or lower.startswith("http://")
                out[k] = v if (safe_scheme and not oauth_url_contains_credential(v)) else ""
            else:
                out[k] = _redact_value(v)
        out.update({k: v for k, v in validated_records.items() if v})
        return out
    out = {k: _redact_value(v) for k, v in list(meta.items()) if k not in REDACTION_RECORD_FIELDS}
    out.update({k: v for k, v in validated_records.items() if v})
    if role == "user" and isinstance(meta.get("quote"), dict):
        # The whole-message quote record (``chat_delivery.quote_meta``) must
        # byte-match the ``>`` block that opens this row's content, and a user
        # row's content is served as typed (``_prepare_messages``): the record
        # follows the same rule, or the card is drawn beside the raw block and
        # the next queue edit drops it. A sender other than the session's human
        # had the record redacted where it entered (``quote_meta``). Rebuilt
        # through the same bounded validator rather than passed through: a
        # transcript line is attacker-writable, so an oversized or malformed
        # record is dropped here like every other retention point drops it.
        from kiro_crew.dashboard.chat_delivery import quote_meta

        bounded = quote_meta({"quote": meta["quote"]}, user_origin=True)
        if bounded:
            out["quote"] = bounded["quote"]
        else:
            out.pop("quote", None)
    return out


# One process-local LRU shared by HTTP snapshot renders and live WS emission.
# It retains at most _DISPLAY_REDACTION_CACHE_MAX_ENTRIES entries and 16 MiB of
# key-input plus output payload; least-recently-used entries leave first, and an
# individually oversized value bypasses the cache. Each entry retains exactly a
# fixed-size key -- the 32-byte SHA-256 digest and the input byte length -- plus
# the redacted output, and every one of those is counted against the byte cap;
# nothing else, and never the raw credential-bearing key material.
#
# The digest covers every input the battery's OUTPUT depends on, not just the
# text: ``redact_exfiltration_urls`` also reads the active PlatformContext's
# exempt-host set, which changes mid-process (a companion loads after boot, a
# policy tightens). Keyed on content alone, a hot entry computed under the old set
# kept being served -- a tenant link stayed ``[REDACTED]`` after its host was
# exempted, or a URL the tightened policy now redacts kept displaying in plaintext
# until eviction. The host set is folded INTO the digest rather than carried as a
# key component: a container per entry would sit outside the byte cap, and with
# a large tenant list it would dwarf the payload the cap is declared to bound.
#
# The entry cap counts individual STRINGS, and a rendered row costs several --
# ``_prepare_messages`` redacts the content plus every meta string (a row's
# unique ``meta.mid`` alone takes a slot) plus each variant. The backend page
# ceiling is ``SLOT_DETAIL_MAX_LIMIT`` rows, so the cap must hold one full page with headroom:
# below that, a page's oldest-to-newest pass evicts its own head before the
# next render reaches it, and the hit rate on exactly the multi-MB sessions this
# cache exists for collapses to near zero. The 16 MiB byte cap is the real bound.
#
# ONE literal for the slot-detail page ceiling. The handler clamps ``?limit=`` to
# it and the cache cap is derived from it, so raising the page size cannot leave
# the cache sized for the old one. The frontend mirrors it as
# ``SLOT_DETAIL_MAX_LIMIT`` in ``website/src/store/chat/paging.ts``.
SLOT_DETAIL_MAX_LIMIT = 500
_DISPLAY_REDACTION_STRINGS_PER_ROW = 8
# The entry cap must also hold the strings the battery DOES change across the
# sessions a reader moves between: 39 real dashboard transcripts held 35.6k of
# them, so the old 8,000-entry cap evicted them on every render. The 16 MiB byte
# cap below is unchanged and still bounds the memory.
_DISPLAY_REDACTION_CACHE_MAX_ENTRIES = max(
    SLOT_DETAIL_MAX_LIMIT * _DISPLAY_REDACTION_STRINGS_PER_ROW * 2, 40_000
)
_DISPLAY_REDACTION_CACHE_MAX_BYTES = 16 * 1024 * 1024
_DisplayRedactionKey = tuple[bytes, int]
# Per-process HMAC key for the cache digest. The digested text is credential-bearing
# (the battery exists to redact it), so the key is a keyed MAC rather than a bare
# hash: a retained digest cannot be checked offline against a guessed plaintext.
# Random at import, never persisted, never logged; a fresh key per process only
# means the cache starts cold, which it does anyway.
_DISPLAY_REDACTION_SALT: bytes = secrets.token_bytes(32)
_display_redaction_cache: OrderedDict[_DisplayRedactionKey, tuple[str, int]] = OrderedDict()
_display_redaction_cache_bytes = 0
_display_redaction_cache_lock = RLock()


_NO_BASE = object()


def _display_redaction_cache_key(
    text: str, base: object = _NO_BASE, exempt_hosts: frozenset[str] | None = None
) -> tuple[_DisplayRedactionKey, int]:
    """Digest the exact string entering the battery together with the exempt sets it reads.

    ``exempt_hosts`` is the caller's one read of the platform set and the scoped
    override, the same snapshot it hands the battery, so the key names the set
    the output is computed under. ``None`` reads the set here.

    The key is fixed-size: a 32-byte digest and the input byte length. The host set
    is sorted and folded into the MAC input behind a NUL separator (a host never
    contains NUL), so a changed set yields a different key while no per-entry
    container is retained. A non-empty pass-4 token-parameter prefix set follows
    behind a ``\\x01`` separator (a host never contains one either); an empty set adds
    nothing, so the MAC input is the host-only form whenever no companion sets one.
    The length component constrains a digest collision.
    The digest is an HMAC under the per-process ``_DISPLAY_REDACTION_SALT``: one
    hash per lookup, so the cache stays cheaper than the battery it fronts.
    """
    raw = text.encode("utf-8", errors="surrogatepass")
    if exempt_hosts is None:
        exempt_hosts = _exempt_exact_hosts() | current_scoped_exempt_hosts()
    hosts = "\0".join(sorted(exempt_hosts)).encode("utf-8", errors="surrogatepass")
    prefixes = _token_param_exempt_prefixes()
    tail = hosts
    if prefixes:
        tail += b"\x01" + "\0".join(prefixes).encode("utf-8", errors="surrogatepass")
    if base is _NO_BASE:
        base = _display_verdicts.mac_base()
    if base is None:
        digest = hmac.new(_DISPLAY_REDACTION_SALT, raw + b"\0" + tail, hashlib.sha256).digest()
    else:
        # Persistent verdicts (display_verdicts): the same keyed MAC, under the
        # gateway's verdict subkey with the rules version already absorbed, so a
        # digest names the same text under the same rules across restarts. The
        # stored tag is the digest alone, so the text's length goes INTO the MAC:
        # without it ``T + NUL + host`` under a smaller host set would hash the
        # same bytes as ``T`` under the larger one.
        mac = cast("hmac.HMAC", base).copy()
        mac.update(len(raw).to_bytes(8, "big"))
        mac.update(raw)
        mac.update(b"\0")
        mac.update(tail)
        digest = mac.digest()
    return (digest, len(raw)), len(raw)


def _clear_display_redaction_cache() -> None:
    """Reset the process-local caches for deterministic tests."""
    global _display_redaction_cache_bytes, _display_clean_young, _display_clean_old
    global _display_preview_cache_bytes
    with _display_redaction_cache_lock:
        _display_redaction_cache.clear()
        _display_redaction_cache_bytes = 0
        _display_clean_young = set()
        _display_clean_old = set()
        _display_preview_cache.clear()
        _display_preview_cache_bytes = 0


# Entries keyed under the per-process salt are unreachable once the
# persistent verdict key turns on; drop them rather than let them age out.
_display_verdicts.on_enable(lambda: _clear_display_redaction_cache())


def _display_redaction_cache_info() -> tuple[int, int]:
    """Return ``(entries, accounted_bytes)`` for invariant tests."""
    with _display_redaction_cache_lock:
        return len(_display_redaction_cache), _display_redaction_cache_bytes


# Verdicts for strings the battery returned UNCHANGED -- nearly every string a
# transcript holds (on a real 708k-string corpus, 110 were redacted). The output
# cache above has to store each output, so its 16 MiB / entry caps hold a few
# thousand rows, while one slot-detail render of a long session redacts hundreds
# of thousands: every render evicted the previous one and the cache never hit.
# For an unchanged string the input IS the output, so the verdict needs only the
# key -- 128 bits of the same keyed digest (which already covers the exempt-host
# set) plus the input byte length, as one int of about 86 bytes with its set
# slot. Strings the battery changes still go
# through the output cache, so a returned value is always either a cached output
# for this exact key or the caller's own string the battery left untouched.
#
# Bounded as two generations of at most _DISPLAY_CLEAN_GENERATION entries each:
# a hit in the old generation is copied to the young one, and when the young one
# fills it becomes the old one and the previous old one is dropped. So at most
# 2 * _DISPLAY_CLEAN_GENERATION verdicts (~34 MB) are held, and what is dropped
# first is what no render has touched for a whole generation. 39 real dashboard
# transcripts needed ~160k verdicts; a generation smaller than the set a reader
# cycles through evicts it before the next render reaches it (at 150k a repeat
# pass still redacted 45.6k strings, at 200k none).
_DISPLAY_CLEAN_GENERATION = 200_000
_display_clean_young: set[int] = set()
_display_clean_old: set[int] = set()


def _clean_verdict_key(key: _DisplayRedactionKey) -> int:
    """128 bits of the digest with the input byte length above them.

    The MAC input ``text + NUL + hosts`` alone does not say where the text ends:
    a text that itself ends in ``NUL + host`` under a smaller host set yields the
    same bytes. The length in the key is what separates those two, exactly as it
    does for the output cache, so the verdict must keep it too.
    """
    digest, length = key
    return (length << 128) | int.from_bytes(digest[:16], "big")


def _display_clean_seen(ck: int) -> bool:
    """Whether *ck* is a known-unchanged string. Caller holds the cache lock."""
    if ck in _display_clean_young:
        return True
    if ck in _display_clean_old:
        _display_clean_add(ck)
        return True
    return False


def _display_clean_add(ck: int) -> None:
    """Record a known-unchanged string. Caller holds the cache lock."""
    global _display_clean_young, _display_clean_old
    if len(_display_clean_young) >= _DISPLAY_CLEAN_GENERATION:
        _display_clean_old = _display_clean_young
        _display_clean_young = set()
    _display_clean_young.add(ck)


def _display_scope_changes_battery() -> bool:
    """Whether this call runs inside a scope that changes the battery's answer.

    The owner-view scope stands the credential pass down and ``code_text`` relaxes the
    bare-secret pass. Neither is part of :func:`_display_redaction_cache_key`, so
    inside either the display caches must be neither read nor filled: a cached
    answer would be the wrong one there, and the scoped one wrong everywhere else.
    """
    # Imported here, as ``display_verdicts.active`` does, so the chat_utils facade
    # does not forward the two predicates as names of its own.
    from kiro_crew.security.redaction_switch import (
        bare_secret_pass_enabled,
        credential_pass_bypassed,
    )

    return credential_pass_bypassed() or not bare_secret_pass_enabled()


def _redact_for_display(text: str) -> str:
    """Apply all display redactors, reusing only an exact content-hash match.

    Inside a scope that changes the battery's answer
    (:func:`_display_scope_changes_battery`) the battery runs uncached: no cache
    and no stored verdict is read or written.

    The exempt-host set is read ONCE and that snapshot feeds both the key and
    the battery. A second read inside the battery could see another set (the
    platform read moves mid-process and degrades a failure to the empty set),
    and the cache would store one set's output under another set's key.
    """
    scoped = _display_scope_changes_battery()
    stored, base = (None, None) if scoped else _display_verdicts.active()
    hosts = _exempt_exact_hosts() | current_scoped_exempt_hosts()
    key, input_bytes = _display_redaction_cache_key(text, base, hosts)
    ck = _clean_verdict_key(key)
    tag = key[0]
    if not scoped:
        with _display_redaction_cache_lock:
            if _display_clean_seen(ck):
                if stored is not None:
                    stored.note(tag, _display_verdicts.CLEAN, from_memory=True)
                return text
            cached = _display_redaction_cache.get(key)
            if cached is not None:
                _display_redaction_cache.move_to_end(key)
                if stored is not None:
                    stored.note(tag, cached[0], from_memory=True)
                return cached[0]

    if stored is not None:
        # The session's stored verdicts (display_verdicts): read once per render,
        # only after the process cache missed, and only ever under a tag that
        # binds this exact text to the current rules and host set.
        verdict = stored.lookup(tag)
        if verdict is _display_verdicts.CLEAN:
            with _display_redaction_cache_lock:
                _display_clean_add(ck)
            return text
        if isinstance(verdict, str):
            _remember_redacted(key, input_bytes, verdict)
            return verdict

    redacted = display_battery(text, exempt_hosts=hosts)
    if scoped:
        return redacted
    if redacted == text:
        with _display_redaction_cache_lock:
            _display_clean_add(ck)
        if stored is not None:
            stored.note(tag, _display_verdicts.CLEAN, from_memory=False)
        return text
    if stored is not None:
        stored.note(tag, redacted, from_memory=False)
    _remember_redacted(key, input_bytes, redacted)
    return redacted


def _remember_redacted(key: _DisplayRedactionKey, input_bytes: int, redacted: str) -> None:
    """File one changed string's output in the bounded output cache."""
    global _display_redaction_cache_bytes
    entry_bytes = len(key[0]) + input_bytes + len(redacted.encode("utf-8", errors="surrogatepass"))
    if entry_bytes > _DISPLAY_REDACTION_CACHE_MAX_BYTES:
        return

    with _display_redaction_cache_lock:
        cached = _display_redaction_cache.get(key)
        if cached is not None:
            _display_redaction_cache.move_to_end(key)
            return
        _display_redaction_cache[key] = (redacted, entry_bytes)
        _display_redaction_cache_bytes += entry_bytes
        while (
            len(_display_redaction_cache) > _DISPLAY_REDACTION_CACHE_MAX_ENTRIES
            or _display_redaction_cache_bytes > _DISPLAY_REDACTION_CACHE_MAX_BYTES
        ):
            _, (_, evicted_bytes) = _display_redaction_cache.popitem(last=False)
            _display_redaction_cache_bytes -= evicted_bytes


# The sidebar's one-line preview of a tab's last message, memoized beside the string
# cache: ``_redact_for_display(strip_markdown_preview(text))`` cut to a limit. Every
# slot-list read and slots broadcast re-projects every open tab on the event loop,
# and stripping and redacting each whole message is most of that cost while the
# message rarely changes between two reads. The key is the string cache's keyed
# digest of the RAW text (exempt-host and token-parameter sets folded in) plus the
# limit, and the value is the redacted, cut preview, so an entry retains no
# credential-bearing text. Bounded by entries and by bytes of key plus output;
# ``_clear_display_redaction_cache`` empties it with the string cache.
_DISPLAY_PREVIEW_CACHE_MAX_ENTRIES = 8_192
_DISPLAY_PREVIEW_CACHE_MAX_BYTES = 2 * 1024 * 1024
_DisplayPreviewKey = tuple[bytes, int, int]
_display_preview_cache: OrderedDict[_DisplayPreviewKey, tuple[str, int]] = OrderedDict()
_display_preview_cache_bytes = 0


def _cut_preview(text: str, limit: int) -> str:
    return text[:limit] + "…" if len(text) > limit else text


def _redacted_preview(text: str, limit: int) -> str:
    """*text* stripped of markdown, redacted for display, cut to *limit* characters plus "…".

    Equal to ``_redact_for_display(strip_markdown_preview(text))`` cut that way,
    which is what a hit returns. The strip runs before the redaction so markdown
    cannot split a credential signature and rejoin it on the wire.
    """
    # Imported here so the chat_utils facade does not forward the strip as its own name.
    from kiro_crew.preview_text import strip_markdown_preview

    if _display_scope_changes_battery():
        # _redact_for_display runs the battery uncached here too.
        return _cut_preview(_redact_for_display(strip_markdown_preview(text)), limit)
    (digest, length), _ = _display_redaction_cache_key(text)
    key = (digest, length, limit)
    with _display_redaction_cache_lock:
        cached = _display_preview_cache.get(key)
        if cached is not None:
            _display_preview_cache.move_to_end(key)
            return cached[0]
    preview = _cut_preview(_redact_for_display(strip_markdown_preview(text)), limit)
    # Filed only when the sets the key folds in did not change while it was
    # computed, so an entry never holds an answer from a context its key does not name.
    if _display_redaction_cache_key(text)[0][0] == digest:
        _remember_preview(key, preview)
    return preview


def _remember_preview(key: _DisplayPreviewKey, preview: str) -> None:
    global _display_preview_cache_bytes
    entry_bytes = len(key[0]) + len(preview.encode("utf-8", errors="surrogatepass"))
    with _display_redaction_cache_lock:
        if key in _display_preview_cache:
            _display_preview_cache.move_to_end(key)
            return
        _display_preview_cache[key] = (preview, entry_bytes)
        _display_preview_cache_bytes += entry_bytes
        while (
            len(_display_preview_cache) > _DISPLAY_PREVIEW_CACHE_MAX_ENTRIES
            or _display_preview_cache_bytes > _DISPLAY_PREVIEW_CACHE_MAX_BYTES
        ):
            _, (_, evicted_bytes) = _display_preview_cache.popitem(last=False)
            _display_preview_cache_bytes -= evicted_bytes


def _display_preview_cache_info() -> tuple[int, int]:
    """Return ``(entries, accounted_bytes)`` of the preview cache for invariant tests."""
    with _display_redaction_cache_lock:
        return len(_display_preview_cache), _display_preview_cache_bytes


def redact_display_content(content: Any) -> str:
    """Redact a message row's ``content`` for display, tolerating structured shapes.

    The display-time content boundary — :func:`_prepare_messages` on the HTTP
    history path and ``state._broadcast_chat_message`` on the live SSE path —
    must neither skip a structured (list/dict) non-user value nor feed it to
    the regex redactors, which accept only ``str`` and raise ``TypeError`` on
    anything else. No current writer produces non-string content, but a
    pre-rule, legacy, or hand-edited transcript row can — exactly the class of
    row read-side redaction exists for. Both sites route through this ONE
    helper; do not add another copy of the guard.

    Delegates to :func:`_redact_value`, the same recursive walker the meta
    redaction uses: ``str`` leaves get ``redact_exfiltration_urls`` then
    ``redact_credentials`` (the order the existing sites apply), dict VALUES
    are recursed, and list/tuple elements are recursed into a new list. The
    result is then passed through :func:`serialize_wire_content`, so a
    non-string value leaves as JSON text — and that text gets one final
    redaction pass: the recursive walker does not rewrite dict KEYS in place
    (two distinct keys redacting to the same string would collapse into one
    entry), but on the serialized wire string a key is plain content, so the
    text pass closes the key channel with no entry loss. Never mutates the
    input (rows are shared by reference and ``dict(m)`` is shallow) and
    never raises for JSON-shaped input.
    """
    redacted = _redact_value(content)
    if isinstance(redacted, str):
        return redacted
    wire = serialize_wire_content(redacted)
    return _redact_for_display(wire)


def serialize_wire_content(content: Any) -> str:
    """Wire-shape half of the display boundary: a row's ``content`` leaves as text.

    ``str`` passes through unchanged; ``None`` — the absent-content shape a
    legacy or hand-edited row can carry — becomes the empty string (its
    display rendering; JSON "null" would put literal text in the chat); any
    other value — container or scalar — is serialized to JSON text. Every
    frontend consumer treats ``content`` as a string (``matchAll`` /
    ``startsWith`` / ``replace`` / ``includes``), so any non-string on the
    wire, ``None`` included, crashes the chat render.

    No redaction happens here. The two display-boundary invariants have
    different scopes — redaction is role-scoped (non-user only) while the
    wire-string shape covers EVERY row, user rows and falsy values
    included — so they are held separately: :func:`redact_display_content`
    composes both for the redacted rows, and emission sites call this one
    directly for the rows the redaction gate exempts. ``default=str`` keeps
    it total for stray non-JSON leaves.
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, default=str)


def _collapse_wire_rows(messages: list[dict]) -> list[dict]:
    """Reduce wire-only rows so that one row means one displayed message.

    ``chunk`` and ``done`` are wire-only roles that are never persisted.
    ``chunk`` is appended once per streamed delta, so a text segment still in
    flight occupies hundreds of rows that render as a single message, and
    ``done`` is a turn terminator that renders as nothing at all. A caller that
    bounds by row count is therefore counting stream progress and terminators
    rather than messages, and a bound taken before this reduction is spent on
    rows the response will not contain.

    Runs of ``chunk`` fold into one ``chunk`` row; ``done`` rows drop. This is
    the canonical first pass for :func:`_prepare_messages`, so dropping a
    terminator here rather than letting it split a run defines the render
    behaviour too. No redaction is applied and no other role is rewritten,
    which is what lets this run ahead of a slice without changing what the
    slice renders as.

    Input dicts are never mutated: a merged row is a fresh dict, because these
    rows are shared with the live window the event loop appends to.
    """

    def _merged(run: list[dict]) -> dict:
        if len(run) == 1:
            return run[0]
        # One join across the run, not a new string per delta: a long reply is
        # hundreds of deltas, and pairwise concatenation copies the text
        # accumulated so far every time, which is quadratic in the reply size.
        merged = {**run[0], "content": "".join(m.get("content", "") for m in run)}
        # The fold stands for every delta in the run, so it carries the run's
        # NEWEST seq: that is the floor a client seeds its replay guard from,
        # and the first delta's seq would let every later one be applied twice.
        # Older rows carry no seq (legacy window); then the fold carries none.
        seqs = [m["seq"] for m in run if isinstance(m.get("seq"), int)]
        if seqs:
            merged["seq"] = max(seqs)
        else:
            merged.pop("seq", None)
        return merged

    out: list[dict] = []
    run: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "chunk":
            run.append(m)
            continue
        if role == "done":
            continue
        if run:
            out.append(_merged(run))
            run = []
        out.append(m)
    if run:
        out.append(_merged(run))
    return out


# One opaque id for THIS gateway process, minted at import and never written to
# disk. That is the entire mechanism behind the gate below: an `mcp_oauth` banner
# records the generation that minted it, and a generation that is not this one is
# provably gone. Every ACP child is a subprocess of the gateway, so the loopback
# listener and the PKCE verifier that made the banner's URL redeemable died with it.
#
def _live_child_instance(state: "DashboardState", slot: "_ChatSlot") -> str:
    """Process-instance identity of the live ACP child serving *slot*, or ``""``.

    The verdict `_prepare_messages` needs to judge an open MCP OAuth banner: a
    banner's Authorize link is redeemable only while the child process that
    minted it is alive, because that process holds the loopback callback
    listener and the PKCE verifier (see `_expire_dead_child_oauth_meta`).
    Resolved here — on the event loop, where the session pool is in scope —
    because `_prepare_messages` itself is synchronous, may run in a worker
    thread, and has no ACP access.

    Resolves the ACTIVE TURN's session key first and the slot's routing only
    as a fallback, for the reason `_cancel_target` documents: a running turn
    owns a stable identity, while ``linked_session_key`` is mutable underneath
    it (a cron injection can rebind a live slot mid-turn). A banner minted
    during that turn belongs to the turn's own child; re-deriving the key from
    the routing would compare it against a different or absent provider and
    withdraw a link that still works.

    ``""`` covers every no-live-child case in one answer: no session in the
    pool (idle sweep, RSS recycle, reset, gateway restart), a session whose
    process has exited on its own, and a provider not backed by a process at
    all. Never raises: a probe failure answers ``""``, which withdraws a
    possibly-dead button rather than failing the read it rides on.
    """
    try:
        key = getattr(slot, "_active_turn_session_key", "") or effective_session_key(slot)
        provider = state.sessions.get_provider(key)
        if provider is None or not provider.is_process_alive():
            return ""
        return str(getattr(provider, "process_instance", "") or "")
    except Exception:
        logger.debug("live-child probe failed for slot %s", slot.key, exc_info=True)
        return ""


def _broadcast_expired_oauth_banners(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Push the read-time gate's current verdict on *slot*'s OAuth banners to open tabs.

    The gate in `_prepare_messages` runs only when a client FETCHES, so an
    already-open tab keeps rendering a dead Authorize link until its next
    refetch. This closes that window for the teardowns the dashboard can see:
    call it after any step that may have ended the slot's child (a session
    reset, an agent switch, a conversation discard, a watchdog recycle) and it
    re-broadcasts the withdrawal frame for every open banner the gate now
    judges dead.

    Verdict-driven, not ordering-driven: it resolves the live child at call
    time and applies the SAME predicate as the read gate, so it needs no
    pre-teardown snapshot and no outcome gating. Called after a teardown that
    failed or was declined, the child is still alive, the stamps still match,
    and nothing is broadcast; a successor session's banner names the successor
    child and is never swept. That is what lets a caller invoke it
    unconditionally where the old write-time retirement needed a doomed-set
    snapshot and a confirmed-teardown gate.

    Deliberately does NOT rewrite the stored rows — the read gate remains the
    one liveness mechanism and this is presentation freshness only. The frame
    carries ``oauth_url: ""`` (not an absent key) because the client merges
    incoming meta over the row it already has, and pre-upgrade JS keeps
    rendering a link whose key was merely omitted. Best-effort: teardowns the
    dashboard never observes (the idle sweep, a child exiting on its own) are
    still caught by the read gate on the tab's next refetch.
    """
    # Candidates first, pool second: almost every slot holds no open banner,
    # and the live-child probe reads the session pool — a side effect the
    # common case must not pay (and one that would burn a pool read at every
    # teardown site this helper rides).
    #
    # A restored sidebar row whose window is still pending has sent no row to
    # any tab in this process (opening the tab loads the window first), so no
    # tab shows one of its banners to withdraw; asking would load its whole
    # transcript on the loop for nothing.
    if getattr(slot, "window_pending", False) is True:
        return
    candidates = [
        message
        for message in slot.messages
        if message.get("role") == "mcp_oauth"
        for meta in (message.get("meta") or {},)
        if meta.get("oauth_url")
        and not (meta.get("completed") or meta.get("failed") or meta.get("superseded"))
    ]
    if not candidates:
        return
    live_child = _live_child_instance(state, slot)
    for message in candidates:
        meta = message.get("meta") or {}
        verdict = _expire_dead_child_oauth_meta("mcp_oauth", meta, live_child)
        if verdict is meta:
            # The gate returns the input unchanged when nothing applies — the
            # banner is live, already terminal, or has no link to withdraw.
            continue
        new_meta = _redact_meta_for_role("mcp_oauth", verdict)
        new_meta.pop("oauth_url", None)
        # Meta only, no content override: the read gate rewrites nothing but
        # meta, so a refetch serves the stored content with `expired` set and
        # the client renders the expired branch from the meta alone. A content
        # override here would diverge from what the next fetch says.
        state.broadcast_ws(
            "chat_message_update",
            {
                "slot": slot.key,
                "ts": message.get("ts", ""),
                "mid": str(meta.get("mid") or ""),
                "meta": {**new_meta, "oauth_url": ""},
            },
        )


def _expire_dead_child_oauth_meta(role: str, meta: dict, live_child: str) -> dict:
    """Present an ``mcp_oauth`` banner whose minting child is gone as terminal.

    A read-time gate, and it has to be read-time. The entity whose death
    invalidates an Authorize link is the ACP child process that minted it — that
    process holds the loopback callback listener and the PKCE verifier the code
    is exchanged with. A child can die without any code of ours running at a
    site that knows about banners: a hard gateway restart, the idle sweep, the
    RSS recycle, or the child simply exiting. Revalidating on every read is the
    same discipline ``connections.mint.expire_dead_holder`` already applies to
    the mint feed, instead of trying to enumerate every way a flow can end.

    ``live_child`` is the process-instance identity of the child CURRENTLY
    serving the slot, resolved by the caller (empty when there is no live
    child). It is a per-spawn token, never the ACP session id: a resume carries
    the same session id onto a NEW process, so a session-id comparison would
    fail open exactly when the minting process is gone.

    Withdraws the link only when ALL of these hold:

    * the row still carries an ``oauth_url`` — there is a live-looking button to
      take away. A row without one has nothing to withdraw and is left
      untouched, which is what keeps the two rejected-URL banners and an
      already-retired row alone.
    * the row is not already terminal. ``completed`` / ``failed`` /
      ``superseded`` are authoritative outcomes recorded by the process that
      observed them, and a later read must not reinterpret them.
    * the row's ``child`` stamp does not name the live child.

    A row carrying NO ``child`` stamp is treated as stale, and that is a
    deduction rather than a guess: the stamp is written by the same build that
    reads it, so an unstamped row was persisted by an older build — and running
    this build means this process replaced the one that hosted that row's
    child. The effect is that the fix also retires banners that went stale
    before it shipped, including rows carrying only the older gateway-``gen``
    stamp.

    Returns the input dict unchanged when nothing applies, so the caller can use
    the result unconditionally; only the withdrawing path copies.
    """
    if role != "mcp_oauth":
        return meta
    if not meta.get("oauth_url"):
        return meta
    if meta.get("completed") or meta.get("failed") or meta.get("superseded"):
        return meta
    child = meta.get("child")
    if child and live_child and child == live_child:
        return meta
    out = dict(meta)
    out.pop("oauth_url", None)
    out["expired"] = True
    return out


#: Key on an in-memory slot message holding the top-level fields a NEWER build wrote
#: on its transcript row, which this build does not know (kept by restore, written
#: back by the slot save; see ``slot_persistence.message_entries``). Server-side
#: only: every path that sends a message out removes it with
#: :func:`without_unknown_row_fields`.
UNKNOWN_ROW_FIELDS_KEY = "_unknown_row_fields"


def without_unknown_row_fields(m: dict) -> dict:
    """A copy of *m* without :data:`UNKNOWN_ROW_FIELDS_KEY`; the one strip every egress uses."""
    if UNKNOWN_ROW_FIELDS_KEY not in m:
        return dict(m)
    return {key: value for key, value in m.items() if key != UNKNOWN_ROW_FIELDS_KEY}


def _prepare_messages(
    messages: list[dict], running: bool, *, live_child: str, workspace: str | None = None
) -> list[dict]:
    """Prepare messages for API response.

    ``live_child`` is the process-instance identity of the ACP child currently
    serving the slot ("" when none is alive) — see
    :func:`_expire_dead_child_oauth_meta`. REQUIRED and keyword-only on
    purpose: a caller that forgot it would silently withdraw every live OAuth
    banner it renders, and no test would see the omission. Resolve it with
    :func:`_live_child_instance` on the event loop, as close to the render as
    possible (a verdict sampled long before use can name a child that has
    since died, serving one stale read).

    ``workspace`` is the slot's workspace: the display redaction relaxes the
    hosts a reader allowed there, the same hosts the segment flush relaxed, so
    an allowed link reaches the page it was kept for.
    """
    with scoped_exempt_hosts(allowed_hosts_for(workspace)):
        return _prepare_messages_scoped(messages, running, live_child=live_child)


def with_allowed_links_restored(m: dict) -> dict:
    """``m`` with the blocked links its reader has since allowed shown again.

    Runs inside an allowed-host scope (:func:`_prepare_messages` and the live
    ``chat_message`` frame), before the
    display redaction, so a restored address is checked by the same pass that
    let the host through (see :func:`restore_allowed_links`).
    """
    text = m.get("content")
    meta = m.get("meta")
    if not isinstance(text, str) or not isinstance(meta, dict) or "blocked_links" not in meta:
        return m
    records = bounded_blocked_links(meta.get("blocked_links"))
    new_text, left = restore_allowed_links(
        text,
        records,
        current_scoped_exempt_hosts(),
        ordered=meta.get(BLOCKED_LINKS_ORDERED_KEY) is True,
    )
    if new_text == text:
        return m
    new_meta = dict(meta)
    if left:
        new_meta["blocked_links"] = left
    else:
        new_meta.pop("blocked_links", None)
    return {**m, "content": new_text, "meta": new_meta}


def _prepare_messages_scoped(messages: list[dict], running: bool, *, live_child: str) -> list[dict]:
    """:func:`_prepare_messages` inside its allowed-host scope."""
    out: list[dict] = []
    for m in _collapse_wire_rows(messages):
        role = m.get("role", "")
        if role == "chunk":
            text = m.get("content", "")
            if text:
                text = redact_display_content(text)
                row: dict[str, Any] = {"role": "streaming", "content": text, "cls": "msg msg-a"}
                # The newest chunk seq folded into this row (see
                # _collapse_wire_rows). The client seeds its replay guard from
                # it, so a live frame that races this snapshot and carries a
                # seq at or below it is dropped instead of appended twice.
                # Omitted when the window rows carry none: a client treats a
                # missing seq as "apply as before".
                if isinstance(m.get("seq"), int):
                    row["seq"] = m["seq"]
                    # The process that numbered it, so a client can tell a
                    # floor from before a gateway restart apart (chunk_generation).
                    if isinstance(m.get("gen"), str):
                        row["gen"] = m["gen"]
                out.append(row)
            continue
        text = m.get("content", "")
        # Gate is `!= "user"`, NOT `not in ("user", "system")`. This is the
        # display-time redaction boundary for everything the slot detail
        # endpoint returns — including the frozen-prefix lines read straight
        # off disk — so it must cover every non-user role. The load path does
        # not redact on load, and `system` content is written to disk
        # unredacted (see _build_message_entry's gate), so excluding it here
        # would emit raw stored bytes.
        # User-authored content stays raw: the user typed it and is the only
        # one who sees it back.
        # Content may be structured (a legacy or hand-edited row):
        # redact_display_content recurses into it rather than raising.
        if role != "user" and text:
            m = with_allowed_links_restored(m)
            m = {**m, "content": redact_display_content(m.get("content", ""))}
        else:
            # The wire-string invariant covers EVERY row, not just the
            # redacted ones: a structured user row or a falsy container
            # serializes to text (without redaction) instead of shipping a
            # raw container the client render chokes on.
            wire = serialize_wire_content(text)
            if wire is not text:
                m = {**m, "content": wire}
        msg_out = without_unknown_row_fields(m)
        if msg_out.get("variants"):
            # Snapshot for the same reason as _redact_meta — this runs in a
            # worker thread (slot-detail render offload) while the event
            # loop may still be appending variants to the live list. A variant's
            # records are re-validated here exactly as a row's are in
            # _redact_meta_for_role: they can hold a full address a reader may
            # open, and a transcript line is attacker-writable, so no record
            # reaches a client without the serve-time check.
            msg_out["variants"] = [
                _variant_for_emit(v) for v in list(msg_out["variants"]) if isinstance(v, dict)
            ]
        meta = parse_cls_meta(m.get("cls", ""))
        if meta is not None:
            # cls replaces meta, but a permission row's delivery id (`meta.mid`,
            # stamped by _ChatSlot.append) is not in cls, and its approval is
            # bound to that id (register_approval). Keep it for permission and
            # elicitation rows only (the two the approval registry binds, and
            # the two whose resolution frame names a `mid`), so every other
            # row's wire meta stays exactly as before.
            stored = m.get("meta")
            stored_mid = stored.get("mid") if isinstance(stored, dict) else None
            keep_mid = isinstance(stored_mid, str) and stored_mid and "mid" not in meta
            if role in ("permission", "elicitation") and keep_mid:
                meta = {**meta, "mid": stored_mid}
            msg_out["meta"] = _expire_dead_child_oauth_meta(
                role, _redact_meta_for_role(role, meta), live_child
            )
        elif isinstance(msg_out.get("meta"), dict):
            # Redact the STORED meta too. Without this branch the stored dict
            # passes through by reference (dict(m) is shallow), so it would
            # reach the client exactly as loaded. This is the only guard on
            # meta for the slot-detail response (the load path does not
            # redact meta).
            msg_out["meta"] = _expire_dead_child_oauth_meta(
                role, _redact_meta_for_role(role, msg_out["meta"]), live_child
            )
        out.append(msg_out)
    return out
