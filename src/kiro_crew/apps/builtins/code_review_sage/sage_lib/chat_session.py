#!/usr/bin/env python3
"""Post-review chat — keep ONE review session alive so the reviewer can be asked
about the findings it just produced.

Why a whole module for this: a review's reasoning lives in its session context,
not in the report. The report carries the *conclusions* (observation /
consequence / suggestion); "why did you decide that?" is answerable only by the
session that decided it. So the deep-review session is adopted here instead of
being ``destroy()``ed when its task returns.

Two things had to stop killing it, and both are deliberate:

  * **The session.** ``ReviewPool.send(..., keep_session_key=...)`` skips its
    ``destroy()`` and hands the live handle here.
  * **The runtime.** ``_BatchRuntimeHolder`` reference-counts the shared kiro-cli
    subprocess and kills it when the count drains to 0 — that teardown is how the
    pool reclaims RSS, because there is no per-turn compaction. An adopted
    session therefore takes a **batch lease**: ``begin_batch()`` on adopt,
    ``end_batch()`` on close. A chat is, to the holder, just another batch that
    has not finished.

That lease is the whole cost of this feature: while a chat is open the subprocess
cannot be reclaimed. It is bounded on four sides, and every one of them ends in
the same release path — so "chat is open" can never become "runtime leaked":

  * ``CHAT_IDLE_TTL_SECS`` — swept once idle (the common case; nobody closes tabs)
  * explicit close — the user ends the conversation
  * ``MAX_CHAT_SESSIONS`` — a new adopt evicts the least-recently-used idle chat
  * ``close_all()`` — app disable / shutdown

The registry holds live sessions and their turns in memory only. Persisting the
transcript is the caller's job (see ``backend/routes.py``): disk is what the UI
reads, so history survives expiry and restart, while ``live`` tells the UI
whether the composer can still be used. Keeping those separate is what lets an
archived run show what was discussed without offering an input box that would
fail.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from kiro_crew.acp.types import (
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        EVENT_TEXT_CHUNK,
        EVENT_THINKING_CHUNK,
        EVENT_TOOL_CALL,
        STOP_REASON_STALE_RECOVER,
        STOP_REASON_TOOL_STALL,
    )
except ImportError:  # pragma: no cover - standalone / test fallback
    EVENT_TEXT_CHUNK = "text_chunk"  # type: ignore[assignment]
    EVENT_THINKING_CHUNK = "thinking_chunk"  # type: ignore[assignment]
    EVENT_TOOL_CALL = "tool_call"  # type: ignore[assignment]
    EVENT_PERMISSION_REQUEST = "permission_request"  # type: ignore[assignment]
    EVENT_COMPLETE = "complete"  # type: ignore[assignment]
    STOP_REASON_STALE_RECOVER = "stale_recover"  # type: ignore[assignment]
    STOP_REASON_TOOL_STALL = "error: tool stall"  # type: ignore[assignment]

try:
    from kiro_crew.safety_override import safety_override
except Exception:  # pragma: no cover - standalone / test fallback
    safety_override = None  # type: ignore[assignment]

try:
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.hooks import (
        TOOL_DENY,
        HookManager,
        hooks_config_from_config_dict,
    )
except ImportError:  # pragma: no cover - standalone / test fallback
    KiroCrewConfig = None  # type: ignore[assignment,misc]
    HookManager = None  # type: ignore[assignment,misc]
    hooks_config_from_config_dict = None  # type: ignore[assignment]
    TOOL_DENY = "deny"

# Module scope, per the repo's top-level-imports rule. No cycle: `store` and
# `results` do not import this module, and `review_pool` (which does) imports it
# lazily inside its own functions.
from sage_lib import results, store  # noqa: E402

logger = logging.getLogger(__name__)

# How long an adopted session may sit unused before the sweep closes it. Short on
# purpose: the lease pins a kiro-cli subprocess, and the realistic usage is a few
# questions right after reading the report, not an all-day conversation. A closed
# chat still shows its transcript; only the ability to continue is lost.
CHAT_IDLE_TTL_SECS = 1800.0

# Absolute lifetime, idle or not. Bounds the pathological case of a page left
# polling forever, which would renew the idle clock indefinitely.
CHAT_MAX_AGE_SECS = 6 * 3600.0

# Concurrent adopted chats. Each one pins the shared subprocess, so this is a
# memory bound, not a throughput knob.
MAX_CHAT_SESSIONS = 4

# Per-question ceiling. Well under the review task timeout: a follow-up question
# is one turn, not a whole review.
CHAT_TURN_TIMEOUT = 300.0


def override_active() -> bool:
    """Whether the platform safety override is active.

    The one gate for chat tool use, and therefore for whether a chat turn may run
    at all. Fails CLOSED when the module is unavailable: "cannot tell" must not
    read as "allowed".
    """
    if safety_override is None:  # pragma: no cover - standalone fallback
        return False
    try:
        return bool(safety_override().is_active())
    except Exception:  # pragma: no cover - defensive
        logger.debug("override probe failed", exc_info=True)
        return False


def override_runway_secs() -> float:
    """Seconds of authorization left, or ``float('inf')`` for a declared grant.

    ``is_active()`` answers "right now", which is not the question a turn needs:
    a grant with twenty seconds left is active and still cannot authorize a turn
    that may run for minutes. Returns 0.0 when inactive or unknown, so every
    caller fails closed.
    """
    if safety_override is None:  # pragma: no cover - standalone fallback
        return 0.0
    try:
        st = safety_override().status()
    except Exception:  # pragma: no cover - defensive
        logger.debug("override status probe failed", exc_info=True)
        return 0.0
    if not getattr(st, "active", False):
        return 0.0
    if getattr(st, "permanent", False):
        return float("inf")
    remaining = getattr(st, "remaining_secs", 0)
    try:
        return max(0.0, float(remaining))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0.0


def chat_key(run_id: str, change_id: str) -> str:
    """Identity of one chat: the review that produced the findings.

    Scoped to (run, change) rather than change alone because re-reviewing a PR
    produces different reasoning, and a chat must belong to the report the user is
    actually looking at.
    """
    return f"{run_id}:{change_id}"


ROLE_USER = "user"
ROLE_REVIEWER = "reviewer"

# Why a question produced no answer, surfaced verbatim to the UI.
REFUSED_NO_YOLO = "tool_refused_no_override"

# A turn was not even attempted because tool use could not be gated. See
# ``_override_active`` and ``ask``.
ERR_NEEDS_OVERRIDE = "chat_needs_override"

# The turn ended abnormally (timeout / tool-stall / stale-recovery / error:*), so
# whatever text arrived is partial and must NOT be shown as a finished answer.
ERR_ABNORMAL = "chat_turn_incomplete"

# The run this chat belongs to was deleted while the chat was still live.
ERR_RUN_GONE = "chat_run_deleted"

# The override is active but lapses before a turn could finish, so starting one
# would run tools past the end of its own authorization.
ERR_OVERRIDE_TOO_SHORT = "chat_override_expiring"

# Authorization ended DURING the turn.
ERR_OVERRIDE_LAPSED = "chat_override_lapsed"

# An ancestor of the transcript path is a link, so writing would land outside the
# run directory.
ERR_LINKED_DIR = "chat_transcript_dir_unsafe"

#: A tool the reviewer asked for was refused by the platform's
#: governance chokepoint (denied command, sensitive path, or the
#: enterprise profile ceiling).
ERR_TOOL_DENIED = "chat_tool_denied"

#: Every retained chat is mid-answer, so the cap cannot be honoured by
#: evicting one. Adoption is refused rather than exceeding the bound.
ERR_CAPACITY_FULL = "chat_capacity_full"


def _is_abnormal_stop(reason: str) -> bool:
    """True when an EVENT_COMPLETE stop_reason means the turn did NOT finish.

    Same predicate the review path applies in ``review_pool._is_abnormal_stop``,
    and for the same reason: a 300s timeout still emits EVENT_COMPLETE, so
    breaking on the event alone would store a truncated sentence as a finished
    answer. Duplicated rather than imported because ``review_pool`` imports THIS
    module, and a top-level import back would be circular.
    """
    r = (reason or "").strip().lower()
    if not r:
        return False
    if r in (str(STOP_REASON_TOOL_STALL).lower(),
             str(STOP_REASON_STALE_RECOVER).lower(), "timeout"):
        return True
    return r.startswith("error")


def _scrub(text: str) -> str:
    """Credential + exfiltration-URL scrub for anything leaving this module."""
    try:
        return store.redact_text(text or "")
    except Exception:  # pragma: no cover - defensive
        logger.debug("chat redaction failed", exc_info=True)
        # Fail CLOSED: an unscrubbable string is dropped rather than emitted raw.
        return ""


def can_ask(timeout: float = CHAT_TURN_TIMEOUT) -> bool:
    """Whether a question asked right now would be accepted.

    The SAME predicate ``ask()`` enforces, exported so the UI cannot invite a
    question the backend will deterministically refuse. Checking mere liveness
    here (while ``ask()`` requires runway) enabled the composer with under a
    turn's worth of grant left — the user typed, and the answer was a refusal.
    """
    return override_active() and override_runway_secs() >= timeout


def governance_denial(ev: object, *, session_key: str, agent: str) -> str:
    """A deny reason from the PLATFORM's governance chokepoint, or "" to allow.

    Being inside an authorized turn is NOT the same as the tool being allowed.
    The turn-level override answers "may this session use tools at all"; it says
    nothing about WHICH tool, so the per-request checks (operator
    denied-commands, the ``~/.aws`` / ``~/.ssh`` sensitive-path blocks, the
    enterprise profile ceiling) are applied here too.

    READ THIS BEFORE RELYING ON IT — the placement is a real limitation, not an
    oversight to tidy up. ``EVENT_TOOL_CALL`` is a NOTIFICATION: for a tool
    pre-approved through the spec's ``allowedTools`` there is no permission
    request to reject and no ``request_id``, and the call has already been made
    by the time this runs. Compare ``auto_improvement``'s runner, which gates the
    equivalent check at ``EVENT_PERMISSION_REQUEST`` where it CAN reject before
    execution.

    So for a pre-approved tool this is a POST-HOC gate. It still does real work —
    it aborts the turn, which stops every SUBSEQUENT tool and withholds the
    answer, and it is a genuine pre-execution gate for tools that do raise a
    permission event — but it does NOT prevent the first pre-approved call from
    running. Preventing that requires the session to have no pre-approved tools;
    it cannot be fixed by moving this check, and ``prompt()`` has no per-turn tool
    restriction to fall back on.

    That is load-bearing here specifically because this session's context is
    attacker-influenced: it was built by reviewing a pull request whose diff and
    description are written by an outsider, and this feature then lets a human
    keep prompting that same session. An instruction planted in the diff could
    steer a follow-up answer into a pre-approved ``fs_read`` of a credential
    file. Routing every tool call through the same ``HookManager`` the dashboard
    and Slack paths use closes that gap; the turn-level override stays as an
    ADDITIONAL restriction on top.

    Fail-CLOSED on an unavailable or broken hook layer: this gate is the only
    thing enforcing those protections on this path, so skipping it on error
    would silently drop them.
    """
    if HookManager is None or KiroCrewConfig is None:  # pragma: no cover
        return "governance hooks unavailable"
    try:
        cfg = KiroCrewConfig.load()
        # `hooks_config_from_config_dict`, NOT `HooksConfig.from_dict`: the latter
        # reads only config.json's `hooks` section, while the operator's
        # denied-command state lives in the keystone `denied_commands.json` — the
        # sole source, so an agent that edits config.json cannot affect the deny
        # ceiling.
        manager = HookManager(
            hooks_config_from_config_dict(getattr(cfg, "hooks", {}) or {}))
        tool_kind = (getattr(ev, "tool_kind", "")
                     or getattr(ev, "tool_purpose", ""))
        command = str(getattr(ev, "requested_command", "") or "") or None
        # `tool_name` / `mcp_server_name`, NOT `title`. `acp/types.py` states the
        # contract outright: a security gate must key on these, because `title` is
        # LLM-authored (`select_tool_title` even prefers the model's own
        # `description`). Passing the title made every MCP allow/deny rule
        # unmatchable — the policy keys on server + canonical tool name, and it was
        # being handed prose. Both are empty for built-ins and on a cache miss,
        # which is the fail-closed direction: no trusted identity, no match.
        tool_name = str(getattr(ev, "tool_name", "") or "")
        mcp_server = str(getattr(ev, "mcp_server_name", "") or "")
        result = manager.on_tool_call(
            (tool_name or getattr(ev, "title", "") or tool_kind or "").strip(),
            session_key=session_key,
            agent=agent,
            app="code-review-sage",
            mcp_server_name=mcp_server,
            mcp_tool_name=tool_name,
            tool_kind=tool_kind,
            raw_params=getattr(ev, "raw_tool_params", None),
            command=command,
            # From the EVENT, not derived from the command. `on_tool_call` denies
            # when `is_shell and not command` — a shell tool whose command could
            # not be recovered must not be judged on its LLM-authored title.
            # Computing this as `bool(command)` would invert exactly that case.
            is_shell=bool(getattr(ev, "is_shell", False)) or bool(command),
        )
        if getattr(result, "action", "") == TOOL_DENY:
            return (getattr(result, "reason", "")
                    or "denied by governance policy").strip()
    except Exception as exc:  # noqa: BLE001 - a broken gate must DENY
        logger.warning("chat governance hook unavailable; denying (fail-closed)",
                       exc_info=True)
        return f"governance hook unavailable: {exc}"
    return ""


async def _close_gen(gen: object) -> None:
    """Best-effort deterministic close of a prompt generator."""
    aclose = getattr(gen, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:  # pragma: no cover - defensive
        logger.debug("chat prompt generator close failed", exc_info=True)


def _coerce_turn(item: object) -> dict | None:
    """Normalize one on-disk turn into the known shape, or reject it.

    Routed back through ``ChatTurn.to_dict`` so the scrub and the field set have
    exactly one definition. The role is restricted to the two values the UI
    renders: a planted role must not reach a branch nobody designed.
    """
    if not isinstance(item, dict):
        return None
    role = item.get("role")
    if role not in (ROLE_USER, ROLE_REVIEWER):
        return None

    def _str(value: object) -> str:
        return value if isinstance(value, str) else ""

    def _strs(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [v for v in value if isinstance(v, str)]

    raw_ts = item.get("ts")
    return ChatTurn(
        role=str(role),
        text=_str(item.get("text")),
        thinking=_str(item.get("thinking")),
        tools=_strs(item.get("tools")),
        refusals=_strs(item.get("refusals")),
        ts=float(raw_ts) if isinstance(raw_ts, (int, float)) else 0.0,
    ).to_dict()


@dataclass
class ChatTurn:
    """One exchange, as the UI renders it."""

    role: str
    text: str = ""
    thinking: str = ""
    tools: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """Serialize for the API and for disk, scrubbed.

        Every string here is model-written or model-influenced: the reviewer can
        repeat a credential it read in the diff, and a tool title carries the
        arguments it was called with. This is the single boundary both the HTTP
        response and the persisted transcript pass through, so the scrub belongs
        here rather than at each call site. The user's own text is scrubbed too —
        a pasted token is exactly as bad once it is on disk.
        """
        return {
            "role": self.role,
            "text": _scrub(self.text),
            "thinking": _scrub(self.thinking),
            "tools": [_scrub(t) for t in self.tools],
            "refusals": [_scrub(r) for r in self.refusals],
            "ts": self.ts,
        }


@dataclass
class ChatSession:
    """A review session that outlived its review, plus its conversation."""

    key: str
    handle: Any
    #: The kiro-cli agent this session runs as. Threaded in from the pool that
    #: resolved it rather than read off the handle: the governance gate resolves
    #: the enterprise PROFILE ceiling by agent name, so guessing it would apply
    #: the wrong ceiling. The review agent spec may be absent, in which case the
    #: pool degrades to the fallback agent — whichever it actually used is what
    #: has to be judged.
    agent: str = ""
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    turns: list[ChatTurn] = field(default_factory=list)
    # Held for the duration of one question. The underlying handle rejects a
    # concurrent prompt outright, so serializing here turns a race into a clean
    # "busy" answer instead of an AcpRuntimeError.
    busy: bool = False

    def idle_expired(self, now: float | None = None) -> bool:
        """Unused for longer than the idle TTL. Respects ``busy``: a session
        answering right now is in use, and closing it would kill the answer."""
        now = time.time() if now is None else now
        return (now - self.last_used_at) >= CHAT_IDLE_TTL_SECS

    def aged_out(self, now: float | None = None) -> bool:
        """Past the absolute lifetime, busy or not.

        Deliberately NOT subject to the busy exemption. ``busy`` is what a stuck
        turn looks like, so exempting it from every bound is exactly how a pinned
        subprocess would survive forever — this cap is the backstop for that case.
        """
        now = time.time() if now is None else now
        return (now - self.created_at) >= CHAT_MAX_AGE_SECS

    def expired(self, now: float | None = None) -> bool:
        """Either bound reached. Kept for callers that do not care which."""
        return self.idle_expired(now) or self.aged_out(now)


class ChatSessionRegistry:
    """Owns adopted review sessions and their batch leases.

    One instance per app (see ``backend/routes.py``). All mutation of the session
    map is under ``_lock``; the ACP round-trip for a question deliberately runs
    OUTSIDE that lock, so one slow question cannot stall an unrelated close or
    sweep.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._sessions: dict[str, ChatSession] = {}
        self._lock = asyncio.Lock()

    async def adopt(self, key: str, handle: Any, agent: str = "") -> None:
        """Take ownership of a live session handle and lease the runtime.

        Called from ``ReviewPool.send`` after a kept task completes. Takes the
        lease FIRST: if ``begin_batch()`` raises we must not register a session
        whose runtime nobody is holding, and the caller destroys the handle.

        Refuses outright when the safety override is inactive — see below.
        """
        # A chat that cannot answer is not worth a subprocess. Without the
        # override every question is refused, so adopting would pin the shared
        # runtime for the idle TTL after EVERY review to serve a panel that can
        # only say "turn on YOLO". Checked at adoption time; the caller treats a
        # refusal as a normal non-adoption and destroys the handle as before.
        if not override_active():
            raise RuntimeError(ERR_NEEDS_OVERRIDE)
        await self._pool.begin_batch()
        leased = True
        try:
            async with self._lock:
                # Eviction can only reclaim an IDLE session — evicting a busy one
                # would fail a question mid-answer. So when the map is full and
                # every entry is busy there is no victim, and inserting anyway
                # would hold MAX+1 sessions, each pinning a runtime lease. That is
                # the exact resource the bound exists to protect, so adoption is
                # refused instead. Replacing this key's own session is always
                # allowed: the map does not grow.
                if (key not in self._sessions
                        and len(self._sessions) >= MAX_CHAT_SESSIONS
                        and all(s.busy for s in self._sessions.values())):
                    raise RuntimeError(ERR_CAPACITY_FULL)
                prior = self._sessions.pop(key, None)
                self._sessions[key] = ChatSession(key=key, handle=handle, agent=agent)
                leased = False  # the map now owns the lease
                victims = self._overflow_victims_locked()
            # Close outside the lock — destroy() and end_batch() both await.
            if prior is not None:
                await self._retire(prior, reason="replaced")
            for victim in victims:
                await self._retire(victim, reason="evicted")
        finally:
            if leased:
                # Registration failed after the lease was taken; hand it back so
                # the count cannot drift upward and pin the subprocess forever.
                await self._release_lease()

    def _overflow_victims_locked(self) -> list[ChatSession]:
        """Least-recently-used idle sessions above the cap. Busy ones are never
        evicted — a question in flight would fail mid-answer."""
        if len(self._sessions) <= MAX_CHAT_SESSIONS:
            return []
        idle = sorted(
            (s for s in self._sessions.values() if not s.busy),
            key=lambda s: s.last_used_at)
        victims = idle[:max(0, len(self._sessions) - MAX_CHAT_SESSIONS)]
        for victim in victims:
            self._sessions.pop(victim.key, None)
        return victims

    def status(self, key: str) -> dict:
        """Whether ``key`` can still be asked, for the UI's composer state."""
        session = self._sessions.get(key)
        if session is None:
            return {"live": False, "busy": False, "turns": []}
        return {
            "live": True,
            "busy": session.busy,
            "turns": [t.to_dict() for t in session.turns],
        }

    async def ask(self, key: str, message: str,
                  timeout: float = CHAT_TURN_TIMEOUT) -> dict:
        """Put one question to the adopted reviewer and return its answer.

        Returns ``{"ok": bool, "turns": list[dict], "error": str}`` rather than
        raising: every failure here is something the UI must render (expired,
        busy, refused, timed out), not a 500.

        Both sides of the exchange come back, in order, so the caller can append
        them to the persisted transcript. Returning only the reply would force the
        caller to re-derive the question it just sent, and would lose the
        server-side timestamp that orders the two.
        """
        # Refuse BEFORE prompting when the safety override is inactive.
        #
        # ``_decide_permission`` only ever sees tools the provider ASKS about, and
        # an agent spec's ``allowedTools`` pre-approves tools so they execute with
        # no permission event at all — the reviewer agent pre-approves one MCP
        # server and the fallback ``kirocrew`` agent pre-approves thirty entries.
        # So rejecting at the permission event cannot be the only gate: for a
        # pre-approved tool there is nothing to reject, and by the time
        # EVENT_TOOL_CALL arrives the tool has already run.
        #
        # The session's spec cannot be narrowed after the fact — it is the review's
        # own session, which is the entire point of keeping it — so authorization is
        # enforced in TWO places, because neither alone is sufficient:
        #
        #   1. HERE, per turn: may this session use tools at all. A chat turn is
        #      user-driven text, and running it un-gated is what this seam exists
        #      to prevent.
        #   2. At each EVENT_TOOL_CALL, `governance_denial()`: WHICH tool. This is
        #      the only point a pre-approved tool can be judged against the
        #      operator's denied commands, the sensitive-path blocks and the
        #      enterprise profile ceiling, since it raises no permission event.
        #
        # (1) without (2) authorizes every tool in the spec at once; (2) without (1)
        # never asks whether this user may prompt the session in the first place.
        if not self._override_active():
            return {"ok": False, "turns": [], "error": ERR_NEEDS_OVERRIDE}
        # Active is not sufficient: a timed grant with less runway than the turn
        # would lapse mid-answer, and the pre-approved tools this session carries
        # keep executing regardless — tools would run past the end of their own
        # authorization. Demand enough runway, or say why not.
        if override_runway_secs() < timeout:
            return {"ok": False, "turns": [], "error": ERR_OVERRIDE_TOO_SHORT}
        async with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return {"ok": False, "turns": [], "error": "chat_expired"}
            if session.busy:
                return {"ok": False, "turns": [], "error": "chat_busy"}
            session.busy = True
            session.last_used_at = time.time()

        user_turn = ChatTurn(role=ROLE_USER, text=message)
        try:
            try:
                reply = await self._run_turn(session, message, timeout)
            except Exception as e:
                logger.debug("chat turn failed", exc_info=True)
                return {"ok": False, "turns": [], "error": str(e)}
        finally:
            # A `finally`, not just the two exit paths: a cancelled handler (client
            # disconnect) raises BaseException, which an `except Exception` never
            # sees. Leaving `busy` set there would exempt the session from the idle
            # sweep and from eviction, pinning the shared subprocess until the app
            # is disabled — the exact leak the bounds exist to prevent.
            #
            # Deliberately WITHOUT `self._lock`: awaiting anything here is unsafe
            # on the path this exists for. Inside a cancelled task every `await`
            # re-raises CancelledError, so acquiring the lock would skip the very
            # clear it guards and re-strand `busy`. The loop is single-threaded and
            # these are two plain attribute writes, so no await is needed — the
            # lock protects the session MAP, not fields of one session.
            session.busy = False
            session.last_used_at = time.time()

        async with self._lock:
            # Record both sides only on success, so a failed question does not
            # leave a dangling user turn with no answer under it.
            session.turns.append(user_turn)
            session.turns.append(reply)
        return {"ok": True,
                "turns": [user_turn.to_dict(), reply.to_dict()],
                "error": ""}

    async def _run_turn(self, session: ChatSession, message: str,
                        timeout: float) -> ChatTurn:
        """Drive one ``prompt()`` and fold its event stream into a turn.

        The handle's guard rejects only a *concurrent* prompt, so a later
        sequential one on the same handle is exactly what makes the reviewer
        remember its own reasoning.
        """
        turn = ChatTurn(role=ROLE_REVIEWER)
        parts: list[str] = []
        thinking: list[str] = []
        stop_reason = ""
        handle = session.handle
        gen = handle.prompt(message, timeout=timeout)
        try:
            async for ev in gen:
                kind = getattr(ev, "kind", None)
                if kind == EVENT_TEXT_CHUNK:
                    parts.append(getattr(ev, "text", "") or "")
                elif kind == EVENT_THINKING_CHUNK:
                    # The review dispatch loop drops this kind; a chat is where
                    # the reasoning is the point, so it is kept and shown.
                    thinking.append(getattr(ev, "text", "") or "")
                elif kind == EVENT_TOOL_CALL:
                    # Re-checked per tool, not once per turn: an operator can
                    # revoke mid-answer, and this bounds what runs afterwards to
                    # the call already in flight instead of the whole turn.
                    if not self._override_active():
                        # Audit BEFORE raising. The rule is that every tool
                        # invocation and permission decision reaches SEL, and for
                        # a spec-pre-approved tool this event says the call has
                        # already run — raising first would leave a revoked
                        # invocation with no record anywhere. `on_tool_call` emits
                        # nothing itself; the caller owns the audit.
                        await self._audit(handle, ev,
                                          outcome="denied_override_lapsed")
                        raise RuntimeError(ERR_OVERRIDE_LAPSED)
                    # The override says this session MAY use tools; it does not
                    # say which. Pre-approved tools reach us with no permission
                    # event, so this is the only place the platform's per-request
                    # gate can run for them.
                    # `to_thread`: the gate loads config and the keystone
                    # denied-commands file from disk. Reading those inline would
                    # block the event loop on every tool call of every chat, and
                    # this loop is also streaming the answer.
                    denial = await asyncio.to_thread(
                        governance_denial, ev,
                        session_key=session.key, agent=session.agent)
                    if denial:
                        # Same reason as the lapse branch above, and it matches
                        # what `_decide_permission` records on its own deny path.
                        await self._audit(handle, ev,
                                          outcome="denied_by_governance")
                        raise RuntimeError(f"{ERR_TOOL_DENIED}: {denial}")
                    title = str(getattr(ev, "title", "") or "")
                    if title:
                        turn.tools.append(title)
                    await self._audit(handle, ev)
                elif kind == EVENT_PERMISSION_REQUEST:
                    await self._decide_permission(
                        handle, ev, turn, session)
                elif kind == EVENT_COMPLETE:
                    stop_reason = getattr(ev, "stop_reason", "") or ""
                    break
            # Close the generator deterministically on the NORMAL path instead of
            # leaving it suspended-until-GC after the EVENT_COMPLETE break.
            await _close_gen(gen)
        except BaseException:
            # Do NOT await aclose() while unwinding. A generator suspended on an
            # await that is itself being cancelled can block there indefinitely,
            # and this frame is exactly the cancellation path — hanging here would
            # strand the turn instead of releasing it. The loop reclaims the
            # generator; releasing the session is what actually matters.
            raise
        # A timeout / tool-stall / stale-recovery still emits EVENT_COMPLETE, so
        # breaking on the event alone would file a truncated sentence as a
        # finished answer — worse than no answer, because nothing marks it partial.
        if _is_abnormal_stop(stop_reason):
            raise RuntimeError(ERR_ABNORMAL)
        turn.text = "".join(parts)
        turn.thinking = "".join(thinking)
        return turn

    async def _decide_permission(self, handle: Any, ev: Any,
                                 turn: ChatTurn,
                                 session: "ChatSession | None" = None) -> None:
        """Approve a tool only when BOTH the override and governance allow it.

        A review auto-approves everything because its prompt is scripted and
        bounded. A chat turn is driven by whatever the user typed, so the same
        blanket approval would let a sentence run shell with no gate. The gate is
        the existing safety override (YOLO) rather than an app-local flag, so
        this surface cannot drift from the platform's posture.

        The override is necessary but NOT sufficient: it answers "may this
        session use tools", never "is THIS tool allowed". A request that reaches
        here can still be one the operator's denied-commands list, the
        ``~/.aws``/``~/.ssh`` sensitive-path blocks, or the enterprise profile
        ceiling forbids — so the platform gate runs before the approval.

        This is the one path where that gate is genuinely PRE-execution: there is
        a request to reject and a ``request_id`` to reject it with. (A tool
        pre-approved through the spec's ``allowedTools`` never arrives here at
        all, which is a separate and unresolved exposure — see
        ``governance_denial``.) Either way the decision is audited.
        """
        req_id = getattr(ev, "request_id", "")
        if self._override_active():
            denial = await asyncio.to_thread(
                governance_denial, ev,
                session_key=(session.key if session is not None else ""),
                agent=(session.agent if session is not None else ""))
            if denial:
                try:
                    await handle.reject_tool(req_id)
                except Exception:
                    logger.debug("chat tool reject failed", exc_info=True)
                else:
                    await self._audit(handle, ev, request_id=req_id,
                                      outcome="denied_by_governance")
                title = str(getattr(ev, "title", "") or "")
                turn.refusals.append(title or REFUSED_NO_YOLO)
                return
            try:
                await handle.approve_tool(req_id)
            except Exception:
                logger.debug("chat tool approve failed", exc_info=True)
            else:
                await self._audit(handle, ev, request_id=req_id,
                                  outcome="auto_approved")
            return
        try:
            await handle.reject_tool(req_id)
        except Exception:
            logger.debug("chat tool reject failed", exc_info=True)
        else:
            await self._audit(handle, ev, request_id=req_id,
                              outcome="rejected")
        title = str(getattr(ev, "title", "") or "")
        turn.refusals.append(title or REFUSED_NO_YOLO)

    @staticmethod
    def _override_active() -> bool:
        return override_active()

    async def _audit(self, handle: Any, ev: Any, *,
                     request_id: Any = None,
                     outcome: str = "auto_approved") -> None:
        audit = getattr(self._pool, "audit_tool_event", None)
        if audit is None:  # pragma: no cover - standalone fallback
            return
        try:
            await audit(handle, ev, request_id=request_id, outcome=outcome)
        except Exception:  # pragma: no cover - defensive
            logger.debug("chat tool audit failed", exc_info=True)

    async def close(self, key: str) -> bool:
        """End one chat and hand its lease back. Idempotent by key."""
        async with self._lock:
            session = self._sessions.pop(key, None)
        if session is None:
            return False
        await self._retire(session, reason="closed")
        return True

    async def sweep(self) -> int:
        """Close idle/aged-out chats. Safe to call on any cadence."""
        now = time.time()
        async with self._lock:
            # Busy exempts a session from the IDLE clock only. The absolute cap
            # applies regardless: a session that has been "busy" for six hours is
            # not working, it is stuck, and it is holding the shared subprocess.
            due = [s for s in self._sessions.values()
                   if s.aged_out(now) or (not s.busy and s.idle_expired(now))]
            for session in due:
                self._sessions.pop(session.key, None)
        for session in due:
            await self._retire(session, reason="expired")
        return len(due)

    async def close_all(self) -> int:
        """Drop every chat — app disable / shutdown."""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await self._retire(session, reason="shutdown")
        return len(sessions)

    async def _retire(self, session: ChatSession, *, reason: str) -> None:
        """Destroy the handle and release its lease.

        Ordering is destroy-then-release: once the lease is gone the runtime may
        be killed, and destroying a session on a dead runtime logs noise.

        Called EXACTLY once per session because every caller removes it from
        ``_sessions`` under ``_lock`` before calling this — that removal, not a
        flag on the session, is what makes the release single. It has to be: the
        lease decrements a count shared with live reviews, so releasing twice
        could tear down a runtime another review is still using. Any new caller
        must pop-under-lock first.
        """
        try:
            await session.handle.destroy()
        except Exception:
            logger.debug("chat session destroy error (%s)", reason,
                         exc_info=True)
        await self._release_lease()

    async def _release_lease(self) -> None:
        try:
            await self._pool.end_batch()
        except Exception:  # pragma: no cover - defensive
            logger.debug("chat lease release failed", exc_info=True)


_REGISTRY: ChatSessionRegistry | None = None


#: The gateway's event loop, recorded by the startup path (which runs ON it).
#: The review driver fans work out across a ThreadPoolExecutor, so code there has
#: no running loop of its own and cannot schedule a close without this.
_LOOP: "asyncio.AbstractEventLoop | None" = None


def bind_loop(loop: "asyncio.AbstractEventLoop") -> None:
    """Record the gateway loop. Called once from the app's startup path."""
    global _LOOP
    _LOOP = loop


def close_soon(key: str) -> bool:
    """Schedule a close from ANY thread. True when it was scheduled.

    ``asyncio.get_running_loop()`` is not usable for this: the review driver runs
    its per-change work in a ``ThreadPoolExecutor``, so a caller there has no
    running loop and would take the failure path every time — a close that never
    happens. Callers on the loop get a task; callers off it get
    ``run_coroutine_threadsafe`` against the recorded loop.
    """
    registry = peek_registry()
    if registry is None:
        return False
    coro = registry.close(key)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        running.create_task(coro)
        return True
    if _LOOP is None or _LOOP.is_closed():
        coro.close()  # never awaited; do not leave a pending coroutine behind
        return False
    asyncio.run_coroutine_threadsafe(coro, _LOOP)
    return True


def get_registry(pool: Any) -> ChatSessionRegistry:
    """Process-wide chat registry, rebuilt if the pool singleton was replaced.

    Rebinding on a new pool matters: ``review_pool.get_pool()`` makes a fresh
    ReviewPool after a shutdown, and a registry still holding leases against the
    OLD pool would decrement a counter nobody reads while the new pool's
    subprocess is pinned by nothing.
    """
    global _REGISTRY
    if _REGISTRY is None or _REGISTRY._pool is not pool:
        _REGISTRY = ChatSessionRegistry(pool)
    return _REGISTRY


def peek_registry() -> ChatSessionRegistry | None:
    """The registry if one exists, without creating it (status handlers)."""
    return _REGISTRY


async def shutdown_registry() -> int:
    """Close every chat and drop the singleton (app disable / shutdown)."""
    global _REGISTRY
    closed = 0
    if _REGISTRY is not None:
        closed = await _REGISTRY.close_all()
        _REGISTRY = None
    return closed


# --- transcript persistence -------------------------------------------------
# Separate from the registry on purpose: the registry owns *live* sessions, disk
# owns *history*. That split is what lets an archived run render what was
# discussed after its session is long gone, instead of showing an input box that
# cannot work.

def chat_dir(run_id: str, change_id: str,
             root: "Path | None" = None) -> "Path":
    """The run's ``chat/`` directory, refusing ANY linked ancestor.

    Guarding the transcript FILE against symlinks is not enough: the reviewer has
    shell and these paths are predictable, so it can plant a link at ``chat``, or
    at the RUN directory holding it. ``mkdir(exist_ok=True)`` followed by
    ``mkstemp(dir=...)`` would then create and replace a file outside the app's
    data tree entirely.

    Containment is anchored at the RUNS ROOT, not at each component's own parent.
    Checking ``chat`` against ``run`` is vacuous when ``run`` is itself the link —
    both sides move together and the comparison passes while everything is
    outside. So containment anchors at the runs root, and EVERY component from
    that root down is checked for being a link.

    Order matters: the anchor is checked for being a link BEFORE its path is
    resolved. Resolving first is what makes containment attacker-relative — a
    link planted at ``runs`` is followed by ``resolve()``, the anchor moves to the
    attacker's directory, and every child underneath then compares as legitimately
    "inside" it. Once the chain is known to be link-free the lexical path IS the
    real path, which is what makes the resolved comparison meaningful rather than
    circular.

    ``change_id`` is unused here but kept in the signature so callers pass the
    same pair everywhere.
    """
    runs = store.runs_root(root)
    run = store.run_dir(run_id, root)
    d = run / "chat"
    # The runs root is FIRST, and unresolved: a link here would otherwise carry
    # the anchor with it.
    for part in (runs, run, d):
        if part.is_symlink():
            raise FileNotFoundError(ERR_LINKED_DIR)
    try:
        anchor = runs.resolve()
    except (OSError, ValueError):  # pragma: no cover - defensive
        raise FileNotFoundError(ERR_LINKED_DIR) from None
    for part in (run, d):
        if not part.exists():
            continue
        try:
            inside = part.resolve().is_relative_to(anchor)
        except (OSError, ValueError):  # pragma: no cover - defensive
            raise FileNotFoundError(ERR_LINKED_DIR) from None
        if not inside:
            raise FileNotFoundError(ERR_LINKED_DIR)
    return d


def transcript_path(run_id: str, change_id: str,
                    root: "Path | None" = None) -> "Path":
    """Where one chat's history lives.

    Under the run's own ``chat/`` subdir rather than ``results/``: result records
    are globbed by ``results.list_results``, and a transcript sitting among them
    would be read as a malformed review record.

    ``change_id`` is routed through ``results.safe_change_id`` because it arrives
    from a request and lands in a filename.
    """
    safe = results.safe_change_id(change_id)
    return chat_dir(run_id, change_id, root) / f"{safe}.json"


class TranscriptUnreadable(Exception):
    """On-disk history exists but could not be read.

    Distinct from "no history". `read_transcript` deliberately reads a missing,
    oversized, unreadable or malformed file as ``[]`` so the panel still renders
    — tolerance that is correct for DISPLAY and destructive for a MERGE. The
    append path does ``read + new_turns -> write``, so treating unreadable
    content as empty silently overwrites the entire prior conversation with just
    the latest exchange. An oversized transcript is exactly the long
    conversation with the most to lose.
    """


def read_transcript_for_merge(run_id: str, change_id: str,
                              root: "Path | None" = None) -> list[dict]:
    """History for the APPEND path, raising rather than reading as empty.

    Returns ``[]`` only when there genuinely is no transcript yet. When a file is
    present but its content cannot be accounted for, this raises instead — a
    refused save is recoverable (the bytes are still on disk under the run), an
    overwrite is not.
    """
    path = transcript_path(run_id, change_id, root)   # may raise FileNotFoundError
    if not path.exists():
        return []                                     # first turn of this chat
    raw_text = store.read_text_nolink(path, store.run_dir(run_id, root))
    if raw_text is None:
        # Missing (raced), a planted link, over the size cap, or not UTF-8.
        raise TranscriptUnreadable("transcript could not be read")
    if not raw_text.strip():
        return []                                     # empty file: nothing to lose
    try:
        items = json.loads(raw_text)
    except ValueError as exc:
        raise TranscriptUnreadable("transcript is not valid JSON") from exc
    if not isinstance(items, list):
        raise TranscriptUnreadable("transcript is not a list of turns")
    return [t for t in (_coerce_turn(i) for i in items) if t is not None]


def read_transcript(run_id: str, change_id: str,
                    root: "Path | None" = None) -> list[dict]:
    """History for a chat, or ``[]`` when there is none.

    Tolerant by design: a missing, unreadable or malformed file reads as "no
    history" so the panel still renders.

    Every surviving turn is re-normalized and re-scrubbed rather than trusted.
    Scrubbing on write is not sufficient: the reviewer has shell and can derive
    this path, so it can write the file ITSELF — a planted transcript carrying a
    credential would otherwise be handed to the dashboard verbatim. Re-coercing
    also means a truncated or hand-edited file cannot inject an unknown role and
    reach a render path the UI does not expect.
    """
    try:
        path = transcript_path(run_id, change_id, root)
    except FileNotFoundError:
        # A linked `chat/` dir reads as no history rather than raising: the panel
        # still renders, and the write path is where it is refused loudly.
        return []
    # The reviewer has shell and this path is predictable, so a prompt-injected
    # worker can plant a symlink here. `read_text` would follow it and copy an
    # arbitrary file into a transcript the dashboard renders; the app's chokepoint
    # opens O_NOFOLLOW, confines the resolved inode to the run dir, and caps size.
    try:
        raw_text = store.read_text_nolink(
            path, store.run_dir(run_id, root))
    except Exception:
        return []
    if not raw_text:
        return []
    try:
        raw = json.loads(raw_text)
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        turn = _coerce_turn(item)
        if turn is not None:
            out.append(turn)
    return out


def write_transcript(run_id: str, change_id: str, turns: list[dict],
                     root: "Path | None" = None) -> None:
    """Persist a chat's history atomically.

    Temp-then-replace so a crash mid-write cannot destroy the history that was
    already readable — the same reason ``results.write_result`` does it.
    """
    path = transcript_path(run_id, change_id, root)
    # Do NOT create the run dir. The chat outlives its review, so a question asked
    # from a stale tab after the run was deleted would otherwise resurrect the run
    # directory that deletion just removed.
    if not path.parent.parent.is_dir():
        raise FileNotFoundError(ERR_RUN_GONE)
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp, not a predictable `<name>.json.tmp`: the worker can pre-plant a
    # symlink at a name it can guess, and writing through it would land this
    # content on whatever it points at (the app's own config.json, for instance).
    # An O_EXCL temp with a random name cannot be pre-empted, and os.replace does
    # not follow a symlink at the destination.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".chat-", suffix=".json")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(turns, ensure_ascii=False))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
