"""Crew Mode — engineered orchestrator pipeline for multi-topic chat.

Design of record: docs/request-for-change/rfc-orchestrator-chat-sessions.md
(v5, post two adversarial council rounds). The user-selected agent runs only
in continuable sub-sessions ("topics"); this manager is the engineered
control plane: durable ingress queue, single-flight decision agent with
structured I/O, validating executor with idempotent dispatch, and verbatim
summary forwarding with mechanical attribution. The LLM only ever chooses
among legal moves — durability, ordering, attribution, and delivery are
owned by code.

Threading contract: everything here runs on the event loop; the only
blocking work (store writes) is small atomic JSON files, mirroring the
subagent ``state.json`` pattern. The decision LLM call is awaited via
``run_bg_oneliner`` (tool-free, timeout-capped).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from typing import Any

from kiro_crew.config.paths import data_home
from kiro_crew.history import append_if_absent_off_loop
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# Queue entry states (RFC v5 contract):
# pending -> claimed(decision) -> accepted(run_id) -> running -> done|failed
# 'ask' = parked awaiting the user's clarification (returns to routing on
# the next user message).

_DECISION_TIMEOUT = 45.0
_FORWARD_COALESCE_SECS = 2.0
_TERMINAL_STATES = ("done", "failed", "steered", "stopped")
_QUEUE_TERMINAL_CAP = 200
_SUMMARY_RE = re.compile(r"<<<SUMMARY\s*(.*?)\s*>>>", re.DOTALL)
_ACK_TEMPLATES = [
    "On it.",
    "Got it — working on that.",
    "Picking that up now.",
]
_SUB_TASK_SUFFIX = (
    "\n\n---\nDo this work YOURSELF. Do NOT spawn subagents. End your reply "
    "with a summary of the result (<=150 words) wrapped EXACTLY as: "
    "<<<SUMMARY your summary here >>>"
)

_DECISION_PROMPT = """You are the routing decision function for a multi-topic chat. \
Decide what to do with each PENDING message. Reply with ONLY a JSON object, no prose.

Rules:
- A message continuing an existing topic (by meaning) routes to that topic.
- An unrelated new request becomes a new topic (give a short title, <=6 words, in the user's language).
- If genuinely torn between two topics, use "ask" with ONE short casual question.
- A topic with status "running" cannot take a routed message now: use "hold" (it will be dispatched when the topic finishes). Exception: a droppable advisory correction to in-flight work (style/approach preference, "prefer X", "don't touch Y") may use "steer".
- Messages that are meta-questions about the topics themselves ("what's in flight?", "list topics") use "meta".

Output schema:
{"actions": [
  {"do": "route", "msg_id": "<id>", "topic_id": "<id>"},
  {"do": "spawn", "msg_id": "<id>", "title": "<short title>"},
  {"do": "hold",  "msg_id": "<id>", "topic_id": "<id>"},
  {"do": "steer", "msg_id": "<id>", "topic_id": "<id>"},
  {"do": "ask",   "msg_id": "<id>", "question": "<one short line>"},
  {"do": "meta",  "msg_id": "<id>"}
]}

STATE:
%s
"""


def _now() -> float:
    return time.time()


class CrewStore:
    """Durable per-slot queue + topic store (atomic JSON, restart-safe)."""

    def __init__(self, slot_key: str) -> None:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", slot_key)
        self.dir = data_home() / "crew" / safe
        self.dir.mkdir(parents=True, exist_ok=True)
        self.queue: list[dict[str, Any]] = self._load("queue.json")
        self.topics: list[dict[str, Any]] = self._load("topics.json")
        self.forwards: list[dict[str, Any]] = self._load("forwards.json")
        # Off-loop write machinery (see _save).
        # Two-lock split so the event loop NEVER blocks on filesystem I/O:
        #  - _seq_lock guards ONLY the sequence bookkeeping dicts and is never
        #    held across a disk write (both the event-loop seq bump and the
        #    worker's newest-wins check/advance take it for microseconds).
        #  - _io_locks[name] is a per-store lock held BY THE WORKER across the
        #    write+replace to serialize concurrent executor writes to the same
        #    file; the event loop never acquires it, so a slow disk cannot
        #    stall chats/heartbeats waiting on a seq bump.
        self._seq_lock = threading.Lock()
        self._io_locks_guard = threading.Lock()
        self._io_locks: dict[str, threading.Lock] = {}
        self._write_seq: dict[str, int] = {}
        self._written_seq: dict[str, int] = {}
        self._pending_writes: set[Any] = set()

    def _load(self, name: str) -> list[dict[str, Any]]:
        try:
            data = json.loads((self.dir / name).read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _save(self, name: str, data: list[dict[str, Any]]) -> None:
        """Persist one store file without blocking the event loop.

        Serialization happens on the caller (cheap); the disk write is
        offloaded to the default executor when a loop is running (AUTOSDE
        no-blocking-call-on-event-loop). A per-name generation counter
        guarded by ``_seq_lock`` makes newest-wins deterministic even if the
        executor runs writes out of order. The actual write+replace runs
        under a per-name ``_io_locks[name]`` held ONLY by the worker — the
        event loop never acquires it, so a slow disk cannot stall the seq
        bump below. ``_seq_lock`` is never held across the disk I/O. Sync
        callers (tests, reconcile at boot) write inline.
        """
        payload = json.dumps(data, ensure_ascii=False, indent=1)
        with self._seq_lock:
            self._write_seq[name] = seq = self._write_seq.get(name, 0) + 1

        def _write() -> None:
            # Serialize concurrent writes to the SAME store file; worker-only,
            # never acquired on the event loop.
            with self._io_locks_guard:
                io_lock = self._io_locks.setdefault(name, threading.Lock())
            with io_lock:
                with self._seq_lock:
                    if seq <= self._written_seq.get(name, 0):
                        return  # a newer snapshot already landed
                tmp = self.dir / f".{name}.tmp"
                tmp.write_text(payload, encoding="utf-8")
                tmp.replace(self.dir / name)
                # Advance ONLY after the atomic replace succeeded — a failed
                # write must stay retryable, not be recorded as landed. The
                # per-name io_lock guarantees no concurrent writer for this
                # store raced us between the check and this advance.
                with self._seq_lock:
                    if seq > self._written_seq.get(name, 0):
                        self._written_seq[name] = seq

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _write()
            return
        fut = loop.run_in_executor(None, _write)
        self._pending_writes.add(fut)
        fut.add_done_callback(self._pending_writes.discard)

    async def wait_writes(self) -> None:
        """Await all in-flight store writes; PROPAGATES the first failure so
        durability-dependent callers (the ingest ack) fail loudly instead of
        acknowledging a write that never landed."""
        pending = list(self._pending_writes)
        if pending:
            results = await asyncio.gather(*pending, return_exceptions=True)
            for r in results:
                if isinstance(r, BaseException):
                    raise r

    def save(self) -> None:
        # Keep queue.json bounded: terminal entries are only needed for quote
        # attribution of recent completions — prune the oldest beyond a cap.
        # Live states (pending/ask/held/claimed/accepted) are never pruned.
        terminal = [e for e in self.queue if e.get("state") in _TERMINAL_STATES]
        if len(terminal) > _QUEUE_TERMINAL_CAP:
            drop = {id(e) for e in terminal[: len(terminal) - _QUEUE_TERMINAL_CAP]}
            self.queue = [e for e in self.queue if id(e) not in drop]
        self._save("queue.json", self.queue)
        self._save("topics.json", self.topics)
        self._save("forwards.json", self.forwards)

    # -- pending-forward helpers (crash-safe delivery) --
    def add_forward(self, body: str) -> str:
        fid = uuid.uuid4().hex[:8]
        self.forwards.append({"fid": fid, "body": body, "ts": _now()})
        self._save("forwards.json", self.forwards)
        return fid

    def remove_forwards(self, fids: set[str]) -> None:
        self.forwards = [f for f in self.forwards if f.get("fid") not in fids]
        self._save("forwards.json", self.forwards)

    # -- queue helpers --
    def add_msg(self, text: str) -> dict[str, Any]:
        entry = {"msg_id": uuid.uuid4().hex[:8], "text": text, "ts": _now(), "state": "pending"}
        self.queue.append(entry)
        self.save()
        return entry

    def entry(self, msg_id: str) -> dict[str, Any] | None:
        return next((e for e in self.queue if e.get("msg_id") == msg_id), None)

    def pending(self) -> list[dict[str, Any]]:
        return [e for e in self.queue if e.get("state") in ("pending", "ask")]

    # -- topic helpers --
    def topic(self, topic_id: str) -> dict[str, Any] | None:
        return next((t for t in self.topics if t.get("topic_id") == topic_id), None)

    def topic_by_run(self, run_id: str) -> dict[str, Any] | None:
        return next((t for t in self.topics if t.get("active_run_id") == run_id), None)

    def add_topic(self, topic_id: str, run_id: str, title: str, origin_msg: str) -> dict[str, Any]:
        t = {
            "topic_id": topic_id, "active_run_id": run_id, "title": title,
            "digest": "", "status": "running", "last_activity": _now(),
            "origin_msg_id": origin_msg, "held": [],
        }
        self.topics.append(t)
        self.save()
        return t


class CrewOrchestrator:
    """The control plane for crew-mode slots (one instance, all slots)."""

    def __init__(self, state: Any, sessions: Any, subagents: Any, cfg: Any = None) -> None:
        self._state = state
        self._sessions = sessions
        self._subagents = subagents
        self._cfg = cfg
        self._stores: dict[str, CrewStore] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._rerun: dict[str, bool] = {}
        self._owned: dict[str, str] = {}  # run_id -> slot_key
        self._forward_buf: dict[str, list[tuple[str, str]]] = {}  # slot_key -> (fid, body)
        self._forward_task: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._ack_i = 0
        self._decision_model = getattr(
            getattr(cfg, "dashboard", None), "crew_decision_model", None
        ) or None

    # ---- wiring ----

    def owns(self, run_id: str) -> bool:
        return run_id in self._owned

    def _store(self, slot_key: str) -> CrewStore:
        st = self._stores.get(slot_key)
        if st is None:
            st = CrewStore(slot_key)
            self._stores[slot_key] = st
            self._reconcile(slot_key, st)
        return st

    def _reconcile(self, slot_key: str, st: CrewStore) -> None:
        """Restart reconciliation: re-own live runs; re-open interrupted
        dispatches (claimed/accepted whose run is unknown -> pending)."""
        for t in st.topics:
            rid = t.get("active_run_id") or ""
            if t.get("status") == "running" and rid:
                info = self._subagents.get(rid) if self._subagents else None
                if info is not None and not info.done:
                    self._owned[rid] = slot_key
                else:
                    t["status"] = "idle"  # completion may have been lost
        for e in st.queue:
            state = e.get("state")
            if state not in ("claimed", "accepted", "held"):
                continue
            if state == "held":
                # A held entry is queued behind a running topic; it never
                # spawned a run of its own, so reopening it cannot re-execute.
                e["state"] = "pending"
                continue
            # claimed/accepted may have already STARTED a run before the crash.
            # Adopt it by its stable dispatch id instead of blindly reopening to
            # pending, which would re-execute a possibly-mutating task (GPT
            # finding on 84dfff5b). ``_apply`` persists dispatch_id (== the run's
            # preassigned id) BEFORE spawning, so this lookup is reliable.
            rid = e.get("run_id") or e.get("dispatch_id") or ""
            info = self._subagents.get(rid) if (rid and self._subagents) else None
            if info is not None:
                # The run exists (started before the crash) — re-own it and make
                # sure a topic tracks it; do NOT re-dispatch.
                self._owned[rid] = slot_key
                e["state"] = "accepted"
                e["run_id"] = rid
                if not st.topic(e.get("topic_id") or rid):
                    st.add_topic(rid, rid, (e.get("text", "")[:24] or rid), e.get("msg_id", ""))
                e.setdefault("topic_id", rid)
            elif state == "claimed":
                # No run was ever registered for this id: the spawn never took
                # effect, so re-dispatching cannot double-execute.
                e["state"] = "pending"
            # else (accepted, run already gone): leave it accepted rather than
            # reopen — re-executing a completed mutating task is the worse
            # failure; its topic carries delivery via the completion/idle path.
        # Held msg_ids were just reopened to pending; drop them from every
        # topic's held list so a later completion cannot double-dispatch.
        for t in st.topics:
            t["held"] = [
                m for m in t.get("held", []) if (st.entry(m) or {}).get("state") == "held"
            ]
        st.save()
        # Re-deliver forwards that were persisted but not yet posted when the
        # previous process died inside the coalesce window (at-least-once).
        if st.forwards:
            slot = self._state.get_slot(slot_key) if self._state else None
            if slot is not None:
                buf = self._forward_buf.setdefault(slot_key, [])
                buf.extend((f["fid"], f["body"]) for f in st.forwards)
                task = self._forward_task.get(slot_key)
                if task is None or task.done():
                    try:
                        self._forward_task[slot_key] = asyncio.create_task(
                            self._flush_forwards(slot)
                        )
                    except RuntimeError:
                        # No running loop (sync caller): the durable copies
                        # stay in forwards.json; the buffered entries flush
                        # with the next scheduled forward.
                        pass

    # ---- transcript posting (workflow_inject shape) ----

    def _post(self, slot: Any, content: str, kind: str = "crew") -> None:
        # Never trust LLM output: every _post payload is LLM-authored on some
        # path (forwarded summaries, decision-agent questions, meta renders).
        # Redact once at the sole delivery chokepoint, mirroring
        # workflow_inject._redact and gateway._subagent_done.
        try:
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        except Exception:
            logger.warning("crew: redaction failed; refusing to post raw", exc_info=True)
            return
        try:
            # broadcast=False: the explicit chat_message frame below is the
            # single broadcast (append's implicit _on_message would duplicate
            # it — GPT review finding on 120fd95e; persistence is unaffected).
            slot.append("assistant", content, "msg msg-a", broadcast=False)
            self._state.broadcast_ws(
                "chat_message",
                {"slot": slot.key, "role": "assistant", "content": content, "kind": kind},
            )
        except Exception:
            logger.warning("crew: transcript post failed for %s", slot.key, exc_info=True)
        try:
            append_if_absent_off_loop(
                self._state.conversation_log,
                getattr(slot, "linked_session_key", "") or f"dashboard:{slot.key}",
                "assistant",
                content,
            )
        except Exception:
            logger.debug("crew: conversation_log append failed", exc_info=True)

    # ---- ingress ----

    async def ingest(self, slot: Any, message: str) -> None:
        """Called from api_chat for crew slots, after the user message is
        already in the transcript. Enqueue durably, ack instantly, schedule
        the decision pass."""
        # App-governance boundary: Crew orchestration dispatches work through
        # spawn / continue_conversation, and continue_conversation carries no
        # ``app`` — so an app-owned slot entering Crew would run its subagents
        # (and their host-permitted tools) OUTSIDE the app's profile. Until the
        # whole dispatch path preserves ``slot._app``, refuse Crew for app-owned
        # slots rather than silently drop the app identity (GPT finding on
        # 84dfff5b). Dashboard-created Crew slots have no _app and are unaffected.
        # isinstance guard (not truthiness): test doubles are MagicMock, whose
        # auto-created ._app attribute is truthy — only a real, non-empty str
        # marks an app-owned slot (mirrors the CrewOrchestrator isinstance
        # check in gateway._subagent_done).
        _slot_app = getattr(slot, "_app", "")
        if isinstance(_slot_app, str) and _slot_app:
            self._post(
                slot,
                "Crew mode isn't available in an app-owned session — start a "
                "regular chat for multi-topic orchestration.",
                kind="crew_ask",
            )
            return
        st = self._store(slot.key)
        # Re-deliver forwards persisted while the tab was closed (see
        # on_subagent_done's closed-slot branch): within one process the store
        # is cached, so a slot reopen does NOT pass through _reconcile — flush
        # the durable copies here. Duplicate-vs-loss: the flush removes them
        # only after posting, and a duplicate beats a silently lost result.
        if st.forwards and not self._forward_buf.get(slot.key):
            buf = self._forward_buf.setdefault(slot.key, [])
            buf.extend((f["fid"], f["body"]) for f in st.forwards)
            task = self._forward_task.get(slot.key)
            if task is None or task.done():
                self._forward_task[slot.key] = asyncio.create_task(self._flush_forwards(slot))
        st.add_msg(message)
        # Durability BEFORE the ack: "on it" is a promise the message survives
        # a crash — wait for the executor write to land (GPT review finding on
        # 120fd95e; RFC C1: the queue is the durable record).
        await st.wait_writes()
        ack = _ACK_TEMPLATES[self._ack_i % len(_ACK_TEMPLATES)]
        self._ack_i += 1
        self._post(slot, ack, kind="crew_ack")
        asyncio.create_task(self._decide(slot))

    # ---- single-flight decision loop ----

    async def _decide(self, slot: Any) -> None:
        lock = self._locks.setdefault(slot.key, asyncio.Lock())
        if lock.locked():
            self._rerun[slot.key] = True  # fold into next snapshot
            return
        async with lock:
            while True:
                self._rerun[slot.key] = False
                try:
                    await self._decide_once(slot)
                except Exception:
                    logger.warning("crew: decision pass failed for %s", slot.key, exc_info=True)
                if not self._rerun.get(slot.key):
                    break

    def _snapshot(self, st: CrewStore) -> str:
        return json.dumps(
            {
                "queue": [
                    {"msg_id": e["msg_id"], "text": e["text"][:400], "state": e["state"]}
                    for e in st.pending()
                ],
                "topics": [
                    {
                        "topic_id": t["topic_id"], "title": t["title"],
                        "digest": t.get("digest", "")[:200], "status": t["status"],
                    }
                    for t in st.topics if t.get("status") != "released"
                ],
            },
            ensure_ascii=False,
        )

    async def _decide_once(self, slot: Any) -> None:
        st = self._store(slot.key)
        if not st.pending():
            return
        prompt = _DECISION_PROMPT % self._snapshot(st)
        raw = ""
        for attempt in (1, 2):
            try:
                raw = await run_bg_oneliner(
                    self._sessions, prompt, model=self._decision_model,
                    sel_source="crew_decision", timeout=_DECISION_TIMEOUT,
                )
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                actions = json.loads(m.group(0))["actions"] if m else []
                break
            except Exception:
                if attempt == 2:
                    logger.warning("crew: unparseable decision, deferring: %r", raw[:200])
                    return
        for a in actions:
            try:
                await self._apply(slot, st, a)
            except Exception:
                logger.warning("crew: action failed: %r", a, exc_info=True)
        st.save()

    # ---- executor (validates every action; LLM only picks legal moves) ----

    async def _apply(self, slot: Any, st: CrewStore, a: dict[str, Any]) -> None:
        do = a.get("do")
        e = st.entry(str(a.get("msg_id", "")))
        if e is None or e.get("state") not in ("pending", "ask"):
            return  # unknown/settled msg — reject silently
        if do == "spawn":
            # Persist a STABLE dispatch identity before spawning so a crash in
            # the window between spawn() starting the run and the accepted-state
            # write is recoverable WITHOUT re-executing the task (GPT finding on
            # 84dfff5b). ``_preassigned_id`` makes the subagent's id equal this
            # token, so _reconcile can look it up and adopt the already-started
            # run instead of blindly reopening the entry to pending.
            dispatch_id = uuid.uuid4().hex[:8]
            e["state"] = "claimed"
            e["dispatch_id"] = dispatch_id
            st.save()
            await st.wait_writes()  # durable BEFORE the side-effecting spawn
            info = self._subagents.spawn(
                (e["text"] + _SUB_TASK_SUFFIX),
                parent_session_key=f"dashboard:{slot.key}",
                agent=getattr(slot, "agent", "") or "",
                keep=True,
                _preassigned_id=dispatch_id,
            )
            if info is None or (getattr(info, "done", False) and getattr(info, "error", "")):
                e["state"] = "pending"
                self._post(
                    slot,
                    "Couldn't start that one — say the word and I'll retry.",
                    kind="crew_ask",
                )
                return
            title = str(a.get("title") or e["text"][:24])
            st.add_topic(info.id, info.id, title, e["msg_id"])
            self._owned[info.id] = slot.key
            e["state"] = "accepted"
            e["run_id"] = info.id
            e["topic_id"] = info.id
        elif do == "route":
            t = st.topic(str(a.get("topic_id", "")))
            if t is None or t.get("status") == "released":
                return
            if t.get("status") == "running":
                t.setdefault("held", []).append(e["msg_id"])
                e["state"] = "held"
                return
            self._dispatch_continue(slot, st, t, e)
        elif do == "hold":
            t = st.topic(str(a.get("topic_id", "")))
            if t is None:
                return
            if t.get("status") != "running":
                # Stale hold: the topic completed while the decision was in
                # flight. A held entry would strand forever (no future
                # completion dispatches it) — continue the topic instead.
                if t.get("status") == "released":
                    return
                self._dispatch_continue(slot, st, t, e)
                return
            t.setdefault("held", []).append(e["msg_id"])
            e["state"] = "held"
        elif do == "steer":
            t = st.topic(str(a.get("topic_id", "")))
            if t is None or t.get("status") != "running":
                return
            ok, _detail = await self._subagents.steer_run(t["active_run_id"], e["text"])
            if ok:
                e["state"] = "steered"
                return
            # Steer failed — the run may have completed DURING the await
            # (GPT finding on 85f8fbe2): holding now would strand the message
            # forever since no future completion dispatches it. Recheck.
            if t.get("status") == "running":
                t.setdefault("held", []).append(e["msg_id"])
                e["state"] = "held"
            else:
                self._dispatch_continue(slot, st, t, e)
        elif do == "ask":
            e["state"] = "ask"
            self._post(slot, str(a.get("question") or "Quick check — is that about an existing topic, or something new?"), kind="crew_ask")
        elif do == "meta":
            e["state"] = "done"
            self._post(slot, self._render_topics(st), kind="crew_meta")

    def _dispatch_continue(self, slot: Any, st: CrewStore, t: dict[str, Any], e: dict[str, Any]) -> None:
        e["state"] = "claimed"
        info = self._subagents.continue_conversation(
            t["topic_id"], e["text"] + _SUB_TASK_SUFFIX,
            parent_session_key=f"dashboard:{slot.key}",
            agent=getattr(slot, "agent", "") or "",
        )
        if info is None:
            e["state"] = "pending"
            return
        if getattr(info, "done", False) and getattr(info, "error", ""):
            err = str(info.error)
            if err.startswith("conversation_busy"):
                t.setdefault("held", []).append(e["msg_id"])
                e["state"] = "held"
            else:
                # conversation_gone / resume_failed: respawn with digest +
                # original payload — the user never re-types (RFC v5 D-gone).
                seed = f"Context digest of a prior thread: {t.get('digest', '(none)')}\n\nTask: {e['text']}"
                fresh = self._subagents.spawn(
                    seed + _SUB_TASK_SUFFIX,
                    parent_session_key=f"dashboard:{slot.key}",
                    agent=getattr(slot, "agent", "") or "", keep=True,
                )
                if fresh is not None and not (
                    getattr(fresh, "done", False) and getattr(fresh, "error", "")
                ):
                    t["topic_id"] = fresh.id
                    t["active_run_id"] = fresh.id
                    t["status"] = "running"
                    self._owned[fresh.id] = slot.key
                    e["state"] = "accepted"
                    e["run_id"] = fresh.id
                else:
                    # Refused respawn (SubagentInfo(done=True, error=...)):
                    # recording it as live would wedge the topic forever.
                    e["state"] = "pending"
                    self._post(
                        slot,
                        "Couldn't pick that one back up — say the word and I'll retry.",
                        kind="crew_ask",
                    )
            return
        t["active_run_id"] = info.id
        t["status"] = "running"
        t["last_activity"] = _now()
        self._owned[info.id] = slot.key
        e["state"] = "accepted"
        e["run_id"] = info.id

    def _render_topics(self, st: CrewStore) -> str:
        live = [t for t in st.topics if t.get("status") != "released"]
        if not live:
            return "Nothing in flight right now — everything's wrapped up."
        lines = ["Here's what's in flight:"]
        for t in live:
            n = len(t.get("held", []))
            extra = f" (+{n} queued)" if n else ""
            lines.append(f"- **{t['title']}** — {t['status']}{extra}: {t.get('digest') or 'just started'}")
        return "\n".join(lines)

    # ---- completion delivery ----

    async def on_subagent_done(self, info: Any) -> None:
        """Called from gateway._subagent_done for owned runs (default
        injection suppressed). Forward the summary with attribution, then
        dispatch the topic's held queue."""
        slot_key = self._owned.pop(info.id, "")
        if not slot_key:
            return
        st = self._store(slot_key)
        t = st.topic_by_run(info.id)
        if t is None:
            logger.info("crew: stale completion %s (no topic) — ignored", info.id)
            return
        # Extract the contracted summary; fall back to truncated result.
        raw = str(getattr(info, "result", "") or "")
        m = _SUMMARY_RE.search(raw)
        summary = (m.group(1).strip() if m else raw.strip()[:800]) or "(no output)"
        # Canonical three-way outcome (SubagentInfo.outcome docstring: consumers
        # MUST use it): a user-stopped run is neither success nor failure.
        outcome = str(getattr(info, "outcome", "") or ("failed" if getattr(info, "error", "") else "completed"))
        if outcome == "stopped":
            summary = "Stopped at your request."
        elif outcome == "failed":
            summary = f"Hit a problem: {getattr(info, 'error', '') or 'unknown error'}"
        origin = st.entry(t.get("origin_msg_id", ""))
        for e in st.queue:
            if e.get("run_id") == info.id and e.get("state") == "accepted":
                e["state"] = {"completed": "done", "stopped": "stopped"}.get(outcome, "failed")
                origin = e
        quote = (origin or {}).get("text", "")[:80]
        t["status"] = "idle"
        t["digest"] = summary[:200]
        t["last_activity"] = _now()
        # Settle + persist BEFORE the slot check: a slot closed mid-run must
        # not leave the topic wedged in "running" or the entry in "accepted"
        # (GPT review finding on 7d6f4d7a). Delivery below is best-effort.
        st.save()
        body = f"↩ re: “{quote}”\n\n{summary}" if quote else summary
        slot = self._state.get_slot(slot_key)
        if slot is None:
            # Tab closed mid-run (GPT finding on 9eb28ee4). Two obligations:
            # (1) the RESULT must stay reachable — persist it as a durable
            #     forward so _reconcile re-delivers it when the slot reopens;
            # (2) HELD follow-ups must not strand — with the topic now idle no
            #     future completion will dispatch them, so reopen them to
            #     pending for the reopened slot's decision pass.
            # Plus a best-effort dashboard notification so the user learns the
            # result even if they never reopen the tab.
            st.add_forward(body)
            for mid in t.get("held", []):
                he = st.entry(mid)
                if he is not None and he.get("state") == "held":
                    he["state"] = "pending"
            t["held"] = []
            st.save()
            try:
                self._state.notify(
                    "crew",
                    f"Crew result: {t.get('title') or 'topic'}",
                    summary[:500],
                )
            except Exception:
                logger.debug("crew: closed-slot notification failed", exc_info=True)
            logger.info("crew: completion %s settled for closed slot %s (forward persisted)", info.id, slot_key)
            return
        self._queue_forward(slot, body)
        # Dispatch held messages (head first).
        held = t.get("held", [])
        if held:
            head = st.entry(held.pop(0))
            if head is not None:
                head["state"] = "pending"
                self._dispatch_continue(slot, st, t, head)
        st.save()

    def _queue_forward(self, slot: Any, body: str) -> None:
        """Burst coalescing: completions within a short window deliver as one
        grouped message (RFC v5, council r2). The body is persisted BEFORE the
        delayed flush is scheduled, so a restart inside the coalesce window
        re-delivers on reconcile instead of losing the result (at-least-once)."""
        if slot is None:
            return
        st = self._store(slot.key)
        fid = st.add_forward(body)
        buf = self._forward_buf.setdefault(slot.key, [])
        buf.append((fid, body))
        if slot.key in self._forward_task and not self._forward_task[slot.key].done():
            return
        self._forward_task[slot.key] = asyncio.create_task(self._flush_forwards(slot))

    async def _flush_forwards(self, slot: Any) -> None:
        await asyncio.sleep(_FORWARD_COALESCE_SECS)
        buf = self._forward_buf.pop(slot.key, [])
        if not buf:
            return
        self._post(slot, "\n\n---\n\n".join(b for _, b in buf), kind="crew_result")
        # Clear the durable copies only after the post; a crash between post
        # and clear re-delivers (duplicate beats silent loss).
        self._store(slot.key).remove_forwards({fid for fid, _ in buf})
