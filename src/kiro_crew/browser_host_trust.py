"""Host trust: a person trusts the ``browser`` tool on one site, for reading or for acting.

The approval card of a ``browser`` call otherwise offers one standing grant,
"Trust all tools for this session", which covers every tool. A host grant is
narrower: it covers the ``browser`` tool on ONE host (``www.example.com``, with
its port when the URL names one), for one class of op
(:mod:`kiro_crew.browser_harness.op_classes`):

* ``read`` covers opening that host's pages and reading them;
* ``act`` covers that, plus clicking, typing and picking on them.

It rides the existing command-scoped trust tier. The card's ``trust_command``
row grants :func:`trust_key` for the host and class of the call in front of it,
for this session only (the approve route refuses a lifetime on a host key), and
the trusted-pattern rung matches the next call's key against the slot's grants.
An ``act`` grant is stored as :func:`grant_pattern`, which matches both
classes, so a person who trusted acting on a site is not asked again to read it.

The host comes from the call when it opens a page (``navigate``, ``tabs new``),
and otherwise from the page the chat's browser is on, as the gateway's live
hub last saw it. That page can move between the approval and the op (a
redirect, a script), so an approval resting on a host grant (the trusted-pattern
rung's, or the card click that made the grant) is noted here against the call
it approved (:func:`note_host_approval`). The op route takes the note of every
call it binds (:func:`take_host_approval`) and checks the tab's real address
before a noted call runs (:func:`recheck`): off that host, the op is refused
with ``trust_scope_changed`` and the agent's retry meets a card.

Only the chat's own call by its verified identity, on a harness whose
subagent calls carry their own session, gets a key: a subagent (a native KAS
child included) drives its own browser session, whose page this gateway does
not read here.
"""

from __future__ import annotations

import glob
import hashlib
import json
import threading
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from kiro_crew.browser_harness.op_classes import CLASS_ACT, CLASS_READ, host_trust_class
from kiro_crew.trust_patterns import HOST_TRUST_KEY_PREFIX as KEY_PREFIX

#: The longest host a key names: a DNS name's 253 characters (an address literal is shorter).
_MAX_HOST = 253
#: The verified identity of the one tool a host grant covers.
BROWSER_SERVER = "kirocrew-core"
BROWSER_TOOL = "browser"
#: The ``tool_id`` a Tool Search deferred call names this tool by (plain and resource-v2).
_DEFERRED_IDS = frozenset(
    {f"{BROWSER_SERVER}::{BROWSER_TOOL}", f"tool::{BROWSER_SERVER}::{BROWSER_TOOL}"}
)
#: Refusal code when the tab left the trusted host between approval and op.
SCOPE_CHANGED_CODE = "trust_scope_changed"


def _host_trust_backends() -> frozenset[str]:
    """Harnesses whose subagent permission requests name the subagent's session.

    A host key reads the CHAT's page, so a backend that reports a subagent's call
    as the chat's own would key it on the wrong page. Named positively: a harness
    is added here once its subagent calls are known to carry ``sub_session_id``.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

    return frozenset({ACP_BACKEND_KAS, ACP_BACKEND_KIRO})


def page_host(url: object) -> str:
    """The host a grant names for *url*: lower case, its port when given; ``""`` for none.

    Only an http(s) URL with a host and no user info has one, so ``about:blank``,
    a ``data:`` page or an unreadable address get no host grant. A Unicode host is
    named in punycode, as Chromium reports the page, so the card cannot show a
    look-alike and the grant matches the page; one the ``idna`` codec cannot
    encode, or a name longer than DNS allows, gets none.
    shortcut: the stdlib codec is IDNA 2003 and Chromium's is UTS 46, so a host they
    spell differently (``ß``) never matches its grant and keeps asking.
    """
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return ""
    host = (parts.hostname or "").rstrip(".").lower()
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
    if not host or len(host) > _MAX_HOST or any(ch.isspace() or ch in "/\\%" for ch in host):
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}" if port is not None else host


def browser_call(raw_params: object) -> tuple[str, dict[str, Any]] | None:
    """``(op, args)`` of a ``browser`` call's params, as the tool posts them; ``None`` if unreadable.

    A Tool Search deferred call (:func:`kiro_crew.session_directive.deferred_call_arguments`)
    is read by its inner arguments, only when its ``tool_id`` names this tool, and so
    is Codex's ``{server, tool, arguments}`` envelope, only when it names this server
    and tool; the caller has already verified the call's own server and tool identity.
    """
    from kiro_crew.session_directive import deferred_call_arguments

    if isinstance(raw_params, dict) and (inner := deferred_call_arguments(raw_params)) is not None:
        if raw_params["tool_id"] not in _DEFERRED_IDS:
            return None
        raw_params = inner
    elif isinstance(raw_params, dict) and set(raw_params) - {"_meta"} == {
        "server",
        "tool",
        "arguments",
    }:
        if raw_params.get("tool") != BROWSER_TOOL or raw_params.get("server") not in (
            BROWSER_SERVER,
            f"@{BROWSER_SERVER}",
        ):
            return None
        raw_params = raw_params["arguments"]
    if not isinstance(raw_params, dict):
        return None
    op = raw_params.get("op")
    args = raw_params.get("args") or {}
    if not isinstance(op, str) or not isinstance(args, dict):
        return None
    return op, args


def call_scope(raw_params: object, page_url: str) -> tuple[str, str]:
    """``(host, class)`` a host grant for this ``browser`` call names, or ``("", "")``.

    The host is the page the call opens for ``navigate`` and ``tabs new``, and *page_url* (the chat's current page) for every other op.
    """
    call = browser_call(raw_params)
    if call is None:
        return "", ""
    op, args = call
    cls = host_trust_class(op, args)
    if not cls:
        return "", ""
    opens = op == "navigate" or (op == "tabs" and args.get("action") == "new")
    host = page_host(args.get("url") if opens else page_url)
    return (host, cls) if host else ("", "")


def trust_key(host: str, cls: str) -> str:
    """The internal trust key for *cls* ops on *host*."""
    return f"{KEY_PREFIX}{host.encode('utf-8', 'surrogatepass').hex()}:{cls}"


def is_host_key(key: str) -> bool:
    """Whether *key* is a host trust key."""
    return isinstance(key, str) and key.startswith(KEY_PREFIX)


def grant_pattern(key: str) -> str:
    """The fnmatch pattern a grant of *key* stores in the slot's trust store.

    Any key is stored exactly (every metacharacter escaped), except a host ``act``
    key, which is stored as its host's prefix plus ``*``: it covers both classes on
    that host and nothing else, because the hex host ends at the ``:`` before the
    class.
    """
    if is_host_key(key) and key.endswith(f":{CLASS_ACT}"):
        return glob.escape(key[: -len(CLASS_ACT)]) + "*"
    return glob.escape(key)


def browser_session_key(session_key: str) -> str:
    """The live hub's key for the chat session *session_key* (the op route's spelling)."""
    from kiro_crew.dashboard.state import _normalize_slot_key

    return _normalize_slot_key(session_key) or session_key


def _eligible(event: Any, client: Any, state: Any) -> bool:
    """Whether *event* is the chat's own ``browser`` call, by an identity the client proved.

    Not when an operator ``hooks.confirm_tools`` pattern names the tool: such a
    call is always put to a person, so no card offers a grant for it either.
    """
    if not (
        getattr(event, "mcp_identity_trusted", False)
        and getattr(event, "raw_params_trusted", False)
        and not getattr(event, "mcp_identity_unreadable", False)
        and not getattr(event, "is_shell", False)
        and not getattr(event, "sub_session_id", "")
        and not getattr(event, "native_child_call", False)
        and getattr(event, "mcp_server_name", "") == BROWSER_SERVER
        and getattr(event, "tool_name", "") == BROWSER_TOOL
        and isinstance(getattr(event, "raw_tool_params", None), dict)
    ):
        return False
    from kiro_crew.agent_sdk.capabilities import SessionCapabilities

    # The client's own capabilities, never the unknown-backend default, whose
    # backend id is Kiro CLI's empty string.
    caps = getattr(client, "capabilities", None)
    if not isinstance(caps, SessionCapabilities) or caps.backend not in _host_trust_backends():
        return False
    # The runner is the one import path of its owners' functions.
    from kiro_crew.dashboard.chat_runner import _operator_confirms_call

    return not _operator_confirms_call(state, event)


def page_url_for(event: Any, session_key: str, client: Any, state: Any) -> str | None:
    """The chat's current page for a host key, ``""`` when it has none, ``None`` if ineligible.

    ``None`` means the call gets no host key at all. ``""`` still lets a call
    that opens a page (``navigate``, ``tabs new``) get one from its own URL.
    """
    if not _eligible(event, client, state):
        return None
    from kiro_crew.browser_harness.live_hub import get_live_hub

    sess = get_live_hub().get(browser_session_key(session_key))
    return str(getattr(sess, "url", "") or "") if sess is not None else ""


# ── The op route's re-check ─────────────────────────────────────────────────

_pending_lock = threading.Lock()
#: Live hub key -> ``(call digest, host)`` per host-grant approval not yet run, oldest first.
_pending: dict[str, list[tuple[bytes, str]]] = {}
#: Most sessions remembered; the oldest go first.
_PENDING_CAP = 4096
#: Most approvals remembered per session; the oldest go first.
_PER_SESSION_CAP = 16


def _call_digest(op: str, args: dict[str, Any]) -> bytes:
    """The one fixed-size spelling of a call both sides compare: the approval's, the route's."""
    canonical = json.dumps([op, args], sort_keys=True, separators=(",", ":"), default=repr)
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def note_host_approval(session_key: str, key: str, raw_params: object) -> bool:
    """Remember that a host grant approved the ``browser`` call *raw_params* of *session_key*.

    *key* is the matched call's trust key; a key that is not a host key notes
    nothing. The note binds that call (a digest of its op and arguments, as the
    route will receive them), so only that call is checked and every other op is not.
    shortcut: the call carries no id the route sees, so two pending approvals of the
    same op with the same arguments are told apart only by order; a note whose call
    never reaches the route waits for an identical call and refuses it if the tab is
    then off the noted host. A per-call id through the shim would end both.

    ``False`` when the note cannot be kept (the session or the store is full): no
    note is ever dropped to make room, since a dropped note lets its call run
    unchecked, so the caller must not auto-approve on the grant. ``True`` otherwise.
    """
    call = browser_call(raw_params)
    if not is_host_key(key) or call is None:
        return True
    hex_host = key[len(KEY_PREFIX) :].rsplit(":", 1)[0]
    try:
        host = bytes.fromhex(hex_host).decode("utf-8", "surrogatepass")
    except ValueError:
        return False
    live = browser_session_key(session_key)
    # Bounded at the store too; a longer host is no key ``page_host`` made, and
    # its note can only refuse.
    note = (_call_digest(*call), host[: _MAX_HOST + 8])
    with _pending_lock:
        notes = _pending.get(live)
        if notes is None:
            if len(_pending) >= _PENDING_CAP:
                return False
            notes = _pending[live] = []
        if len(notes) >= _PER_SESSION_CAP:
            return False
        notes.append(note)
    return True


def site_grant_covers(key: str, slot: Any) -> bool:
    """Whether a live host grant on *slot* covers the host key *key* now."""
    if not is_host_key(key):
        return False
    import time

    from kiro_crew.trust_patterns import live_trusted_patterns, matches_trusted_pattern

    live = live_trusted_patterns(
        set(getattr(slot, "_trusted_patterns", ()) or ()),
        getattr(slot, "_trusted_pattern_deadlines", None) or {},
        wall=time.time(),
        mono=time.monotonic(),
    )
    return matches_trusted_pattern(key, live) is not None


def take_host_approval(live_key: str, op: str, args: dict[str, Any]) -> str | None:
    """The host the oldest pending approval of this call named, removed; ``None`` if none."""
    call = _call_digest(op, args)
    with _pending_lock:
        notes = _pending.get(live_key) or []
        for i, (noted, host) in enumerate(notes):
            if noted == call:
                del notes[i]
                if not notes:
                    del _pending[live_key]
                return host
    return None


_SCOPE_CHANGED_TEXT = (
    "the page is no longer on {host}, the site this op was trusted for, so it did "
    "not run; take a snapshot to see where the page is, then repeat the op if it "
    "still fits (the user will be asked)"
)


async def recheck(
    host: str | None,
    op: str,
    args: dict[str, Any],
    helper: Callable[..., Awaitable[Any]],
) -> dict[str, Any] | None:
    """Refuse the op when its approval rested on a host grant for another host.

    *host* is the call's own note (:func:`take_host_approval`). Run by the op
    route after the tab is bound and before the op touches it. ``None`` lets it
    run: no host grant approved this call, or the tab (for ``navigate`` and
    ``tabs new``, the page it opens) is on
    the granted host. The tab's address
    is read from the browser itself; a read that fails refuses.
    """
    if host is None:
        return None
    if op == "navigate" or (op == "tabs" and args.get("action") == "new"):
        now = page_host(args.get("url"))
    else:
        try:
            tab = await helper("current_tab")
        except Exception:  # noqa: BLE001 - an unreadable tab is not the trusted host
            tab = None
        now = page_host(tab.get("url")) if isinstance(tab, dict) else ""
    if now == host:
        return None
    return {
        "ok": False,
        "code": SCOPE_CHANGED_CODE,
        "error": _SCOPE_CHANGED_TEXT.format(host=host[:200]),
    }


__all__ = [
    "BROWSER_SERVER",
    "BROWSER_TOOL",
    "CLASS_ACT",
    "CLASS_READ",
    "KEY_PREFIX",
    "SCOPE_CHANGED_CODE",
    "browser_call",
    "browser_session_key",
    "call_scope",
    "grant_pattern",
    "is_host_key",
    "note_host_approval",
    "page_host",
    "page_url_for",
    "recheck",
    "site_grant_covers",
    "take_host_approval",
    "trust_key",
]
