"""Tests for the crew runtime — session, brief injection, nudge, watcher sweep.

Nothing here spawns a real session or touches the network: the dashboard state is a
fake that records what was asked of it, the provider client is patched, and every
store read/write is scoped to ``tmp_path``.

The coverage is weighted toward the failures that are SILENT, because a crew runs
with nobody watching:

  * **The length guard on brief injection.** A compaction summary that merely
    quotes the sentinel is the failure mode that matters: a sentinel-only check
    reads it as a hit and the crew spends the rest of the day running on a
    paraphrase of its own instructions, with no error anywhere. So the guard has a
    test of its own, and it is one of the two tests falsified below.
  * **Trust.** Granting it to an attended crew is an unattended-tool-execution
    bug; failing to re-establish it for an unattended one parks the crew in an
    approval prompt for two hours and then denies it.
  * **First observation.** A cold fingerprint must report NOTHING, or every
    gateway restart wakes every crew on every open item at once.
  * **Each of the six signals.** Missing one means an item stalls forever with no
    trace, which is exactly what the sweep exists to prevent.
"""

import asyncio
import contextlib
import inspect
import re
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.apps.builtins.issue_radar.backend import provider
from kiro_crew.apps.builtins.issue_radar.backend import watch as watch_mod
from kiro_crew.dashboard import chat_runner
from kiro_crew.safety_override import reset_singleton, safety_override

OWNER, REPO = "o", "r"
_KEY = provider.key_from_parts(OWNER, REPO)


def _effectively_trusted(slot: Any) -> bool:
    """Would the SHARED approval path auto-approve this slot's tools right now?

    The one assertion worth making about a crew's trust. Every alternative is a
    proxy that can pass while the crew is in fact untrusted (or vice versa): the
    ``unattended`` flag is only intent, and ``slot._trust`` is a different grant
    this module must never write. So the tests below ask the real consumer.
    """
    return chat_runner._slot_is_trusted(slot)


# ── fakes ───────────────────────────────────────────────────────────────────


class _FakeSlot:
    """Stand-in for _ChatSlot: records the prompt a turn would have run with."""

    def __init__(self, key: str = "crew-c_1", agent: str = "", model: str = "", workspace: str = ""):
        self.key = key
        self.title = ""
        self._titled = False
        self._trust = False
        self._trust_scope = ""
        self.agent = agent
        self.model = model
        self.workspace = workspace
        self.messages: list[dict[str, Any]] = []
        self.running = False
        self.prompts: list[str] = []
        self.runners: list[Any] = []

    def append(self, role: str, content: str, cls: str = "", **kw: Any) -> None:
        self.messages.append({"role": role, "content": content, "cls": cls})

    def enqueue_or_run_prompt(self, prompt: str, run_chat_coro: Any, state: Any) -> bool:
        self.prompts.append(prompt)
        self.runners.append(run_chat_coro)
        self.append("user", prompt)
        return not self.running


class _FakeState:
    """Minimal DashboardState: slot registry plus the calls the runtime makes."""

    def __init__(self) -> None:
        self.slots: dict[str, _FakeSlot] = {}
        self.created: list[dict[str, Any]] = []
        self.pushes = 0
        #: Slot keys whose turns were charged against the background-turn cap.
        self.capped: list[str] = []
        #: When set, the cap never hands out a permit — the turn never runs.
        self.permit_timeout = False

    def get_slot(self, key: str) -> _FakeSlot | None:
        return self.slots.get(key)

    async def run_background_turn(self, slot: Any, coro: Any) -> Any:
        """The app-owned concurrency cap, recording what was charged against it.

        Present on the fake precisely because the runtime MUST route every crew
        turn through it: a fake without this method would let a dispatch that calls
        ``_run_chat`` itself pass unnoticed, which is the defect these tests pin.
        The real one queues at the cap and abandons the turn after its own wait
        budget, so ``permit_timeout`` closes the coroutine rather than running it.
        """
        self.capped.append(str(getattr(slot, "key", "")))
        if self.permit_timeout:
            coro.close()
            raise TimeoutError("queued behind the background-turn cap")
        return await coro

    def get_or_create_slot(
        self,
        name: str = "",
        agent: str = "",
        workspace: str = "default",
        model: str = "",
        app: str = "",
        **kw: Any,
    ) -> _FakeSlot:
        self.created.append(
            {"name": name, "agent": agent, "workspace": workspace, "model": model, "app": app}
        )
        slot = self.slots.get(name)
        if slot is None:
            slot = _FakeSlot(name, agent=agent, model=model, workspace=workspace)
            self.slots[name] = slot
        return slot

    def push_slots_update(self) -> None:
        self.pushes += 1

    def push_slot_title(self, key: str, title: str) -> None:
        self.pushes += 1


class _FakeLoop:
    """One armed autonudge loop — the part of ``NudgeLoop`` the runtime reads."""

    def __init__(self, loop_id: str, slot_key: str, active: bool = True):
        self.id = loop_id
        self.slot_key = slot_key
        self.active = active


class _FakeNudge:
    """Minimal AutoNudgeService: the loop registry plus the three calls made here.

    Needed because the real ``get_instance()`` returns ``None`` outside a running
    gateway, so an unstubbed test only ever exercises the no-service branch — and
    the restart bug lives in the branch where a loop DOES exist.
    """

    def __init__(self, loops: list[_FakeLoop] | None = None) -> None:
        self.loops: list[_FakeLoop] = list(loops or [])
        self.added: list[str] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def get_by_slot(self, slot_key: str) -> _FakeLoop | None:
        return next((lp for lp in self.loops if lp.slot_key == slot_key), None)

    def list_all(self) -> list[_FakeLoop]:
        return list(self.loops)

    async def add(self, slot_key: str = "", message: str = "", **kw: Any) -> _FakeLoop:
        self.added.append(slot_key)
        loop = _FakeLoop(f"nl_{len(self.loops)}", slot_key)
        self.loops.append(loop)
        return loop

    async def update(self, loop_id: str, **kw: Any) -> None:
        self.updates.append((loop_id, dict(kw)))
        for lp in self.loops:
            if lp.id == loop_id and "active" in kw:
                lp.active = bool(kw["active"])


def _app(state: _FakeState | None) -> Any:
    return cast(Any, {"state": state})


def _crew(root, name="Andromeda", **spec) -> dict[str, Any]:
    return cs.create_crew(OWNER, REPO, {"name": name, **spec}, root)


def _item(root, crew_id, number, **patch) -> dict[str, Any]:
    return cs.upsert_work_item(OWNER, REPO, crew_id, number, patch, root)


# ── brief injection ─────────────────────────────────────────────────────────


class TestBriefInjection(unittest.TestCase):
    def test_brief_carries_the_sentinel(self):
        self.assertTrue(cr.brief_text().startswith(cr.BRIEF_SENTINEL))

    def test_injects_when_sentinel_absent(self):
        slot = _FakeSlot()
        slot.append("nudge", "[crew turn] Andromeda · o/r — advance one item")
        self.assertFalse(cr.brief_is_present(slot))

    def test_does_not_inject_when_brief_present(self):
        slot = _FakeSlot()
        slot.append("user", cr.brief_text() + "\n\n---\n\nnudge body")
        self.assertTrue(cr.brief_is_present(slot))

    def test_injects_when_only_a_short_quote_of_the_sentinel_is_present(self):
        """THE length guard.

        A compaction summary quotes the marker it saw. It contains the sentinel and
        it is far shorter than the brief, so it must NOT count as a hit — otherwise
        the crew keeps running on a summary of its own instructions.
        """
        slot = _FakeSlot()
        slot.append(
            "assistant",
            "Summary of earlier turns: the session opened with "
            f"{cr.BRIEF_SENTINEL} and a work list, then claimed #2201.",
        )
        self.assertFalse(cr.brief_is_present(slot))

    def test_a_padded_summary_still_needs_the_sentinel(self):
        """The guard is length AND sentinel, not length alone."""
        slot = _FakeSlot()
        slot.append("assistant", "x" * (len(cr.brief_text()) + 500))
        self.assertFalse(cr.brief_is_present(slot))

    def test_the_brief_says_publish_and_move_on_instead_of_waiting(self):
        """The brief is the only place the crew learns what to do with a decision.

        Both halves are pinned because either one alone is the old behaviour: a
        brief that says to comment but not to release holds the claim anyway, and
        one that says to release but not to comment leaves a label nobody can act
        on. The prohibition on polling is pinned separately — a crew that comes back
        to check is holding the issue in everything but name.
        """
        brief = cr.brief_text()
        for phrase in (
            "never hold an issue waiting",
            "Release your claim",
            "needs-decision",
            "needs-investigation",
            "do not come back to poll for a reply",
            "what decision you believe",
        ):
            self.assertIn(phrase, brief)

    def test_the_brief_carries_no_escalation_concept(self):
        crew_words = ("escalat", "hand back", "hand-back", "handback", "crew: needs decision")
        lowered = cr.brief_text().lower()
        for word in crew_words:
            self.assertNotIn(word, lowered)

    def test_turn_prompt_carries_the_brief_only_on_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crew = _crew(root)
            slot = _FakeSlot()
            first = cr.compose_turn_prompt(slot, OWNER, REPO, crew, root)
            self.assertIn(cr.BRIEF_SENTINEL, first)
            # The carrying message is brief + nudge, so it satisfies its own guard.
            slot.append("user", first)
            second = cr.compose_turn_prompt(slot, OWNER, REPO, crew, root)
            self.assertNotIn(cr.BRIEF_SENTINEL, second)
            self.assertIn("[crew turn]", second)


# ── nudge composition ───────────────────────────────────────────────────────


class TestNudge(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_nudge_carries_the_volatile_fields(self):
        crew = _crew(self.root, labels=["bug", "area:cli"], max_open=3)
        cid = crew["id"]
        _item(self.root, cid, 2201, phase="implementing", next="add the Windows branch")
        _item(self.root, cid, 2244, phase="awaiting-ci", next="round 3")
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root))

        self.assertIn("Andromeda", nudge)
        self.assertIn(f"{OWNER}/{REPO}", nudge)
        self.assertIn(cid, nudge)                       # crew id, not just the name
        self.assertIn("bug, area:cli", nudge)           # label scope
        self.assertIn("Open 2/3", nudge)
        self.assertIn("#2201 implementing", nudge)
        self.assertIn("add the Windows branch", nudge)  # the `next` of every open item
        self.assertIn("#2244 awaiting-ci", nudge)
        self.assertIn("round 3", nudge)
        for label in cr.writable_labels(cs.read_settings(OWNER, REPO, self.root)):
            self.assertIn(label, nudge)

    def test_the_nudge_names_exactly_the_writable_labels_and_no_others(self):
        """The `Writable labels:` line is the crew's whole authority on labels.

        Pinned as an EQUALITY on the rendered line rather than a membership check,
        because both failure directions are silent and land on a stranger's issue:
        naming one label too few leaves a crew unable to hand an issue to a human,
        and naming one too many is a label of the crew's own invention on someone
        else's repository. A membership check passes on both.
        """
        crew = _crew(self.root)
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root))
        line = next(ln for ln in nudge.splitlines() if ln.startswith("Writable labels:"))
        named = re.findall(r"`([^`]+)`", line)
        resolved = list(cr.writable_labels(cs.read_settings(OWNER, REPO, self.root)))
        self.assertEqual(named, resolved)
        self.assertEqual(named, ["crew: in progress", "crew: needs human"])
        # The two labels escalation owned are gone, and neither may be written now.
        for retired in ("crew: needs decision", "crew: awaiting reply"):
            self.assertNotIn(retired, nudge)

    def test_a_renamed_needs_human_label_reaches_the_nudge(self):
        """The label is a repo setting, so a rename has to reach the crew.

        A constant would keep telling every crew in every install to write
        `crew: needs human`, which on this repo is a label nobody is watching — the
        crew would believe it had handed the issue over and nothing would have.
        """
        cs.write_settings(OWNER, REPO, {"needs_human_label": "triage: human"}, self.root)
        crew = _crew(self.root)
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root))
        line = next(ln for ln in nudge.splitlines() if ln.startswith("Writable labels:"))
        self.assertEqual(re.findall(r"`([^`]+)`", line), ["crew: in progress", "triage: human"])
        # The Never block travels on every turn and must agree with that line.
        self.assertIn("other than `crew: in progress`, `triage: human`", nudge)
        self.assertNotIn("crew: needs human", nudge)

    def test_the_nudge_never_mentions_escalation(self):
        """A crew must not be told a concept the protocol no longer has.

        The nudge is re-sent every turn and is the most recent instruction in the
        window, so a stale counter here outranks the brief: a crew reading
        `escalated 0/3` would look for the mechanism, not find it, and improvise.
        """
        crew = _crew(self.root, labels=["bug"])
        _item(self.root, crew["id"], 2201, phase="awaiting-reply", next="asked for a repro")
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root))
        lowered = nudge.lower()
        for word in ("escalat", "needs decision", "hand back", "handback"):
            self.assertNotIn(word, lowered)

    def test_an_empty_label_scope_means_every_open_issue(self):
        """A crew is created with no labels by default and none are required.

        Reading empty as "pick up nothing" — which this line did — told every
        default-configured crew to do nothing, and it would idle for its whole life
        with no error anywhere to explain it. Nothing in the backend filters on this
        list; the crew self-applies it from the brief, so this wording IS the
        contract.
        """
        crew = _crew(self.root, labels=[])
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root))
        self.assertIn("every open issue", nudge)
        self.assertNotIn("pick up nothing", nudge)

    def test_nudge_carries_the_never_block(self):
        crew = _crew(self.root)
        settings = cs.read_settings(OWNER, REPO, self.root)
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root))
        self.assertIn(cr.never_block(cr.writable_labels(settings)), nudge)
        for fragment in (
            "CI or gate configuration",
            "Never write any label other than",
            "Never push to main",
            "two worktrees",
            "without writing the ledger",
            "absolute path",
            "exit code",
        ):
            self.assertIn(fragment, nudge)
        # The prefix rule it replaces cannot express a configurable label, and would
        # read as permission for any `crew:`-prefixed label this protocol dropped.
        self.assertNotIn("outside the `crew:` prefix", nudge)

    def test_never_block_is_compressed(self):
        # It rides on EVERY turn, so its size is a running cost. The ceiling is a
        # bloat alarm rather than a budget: the labels it names are configurable, so
        # a few of these words belong to whatever this repository called them.
        self.assertLess(len(cr.never_block().split()), 125)

    def test_writable_labels_falls_back_rather_than_naming_an_empty_label(self):
        """A blank or missing setting must not render as an empty backtick pair.

        `` `` in the nudge reads as "you may write a label with no name", which is
        the one wrong answer worse than either real one.
        """
        self.assertEqual(cr.writable_labels({"needs_human_label": "   "}), cr.writable_labels())
        self.assertEqual(cr.writable_labels(None)[0], cr.CLAIM_LABEL)
        for label in cr.writable_labels({}):
            self.assertTrue(label.strip())

    def test_item_without_a_next_says_so(self):
        crew = _crew(self.root)
        _item(self.root, crew["id"], 7, phase="claimed")
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root))
        self.assertIn("no next step recorded", nudge)


# ── session launch / trust ──────────────────────────────────────────────────


class TestSession(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # Scoped grants live in the process-wide singleton, so one test's grant
        # would otherwise still be active in the next one.
        reset_singleton()
        self.addCleanup(reset_singleton)

    async def test_session_key_agent_workspace_and_model_come_from_the_record(self):
        crew = _crew(self.root, agent="kirocrew", model="claude-opus-5")
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        self.assertEqual(slot.key, f"crew-{crew['id']}")
        created = state.created[-1]
        self.assertEqual(created["name"], f"crew-{crew['id']}")
        self.assertEqual(created["agent"], "kirocrew")
        self.assertEqual(created["app"], "issue-radar")
        # The record's model is passed EXPLICITLY, which is what overrides the
        # agent's own pin.
        self.assertEqual(created["model"], "claude-opus-5")

    async def test_title_is_locked_so_the_auto_titler_never_fires(self):
        crew = _crew(self.root)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        self.assertTrue(slot._titled)
        self.assertIn("Andromeda", slot.title)
        self.assertIn(f"{OWNER}/{REPO}", slot.title)

    async def test_trust_only_when_unattended(self):
        state = _FakeState()
        unattended = _crew(self.root, name="Whirlpool", unattended=True)
        attended = _crew(self.root, name="Draco", unattended=False)
        hot = await cr.ensure_crew_session(state, OWNER, REPO, unattended)
        cold = await cr.ensure_crew_session(state, OWNER, REPO, attended)
        self.assertTrue(_effectively_trusted(hot))
        self.assertFalse(_effectively_trusted(cold))

    async def test_trust_comes_from_the_scope_and_never_from_the_session_flag(self):
        """THE finding. An unattended crew must end up auto-approved, and the thing
        that makes it so must be an expiring audited grant — not ``slot._trust``,
        which never expires and which only a human's click should ever set."""
        crew = _crew(self.root, unattended=True)
        slot = await cr.ensure_crew_session(_FakeState(), OWNER, REPO, crew)
        self.assertTrue(_effectively_trusted(slot))
        self.assertFalse(slot._trust)  # the unbounded flag was NOT stamped
        scope = cr.autoapprove_scope(crew["id"])
        self.assertEqual(slot._trust_scope, scope)
        self.assertTrue(safety_override().is_scope_active(scope))
        # And it is genuinely bounded, rather than a scope with no deadline.
        self.assertGreater(safety_override().scope_remaining_secs(scope), 0)
        self.assertLessEqual(safety_override().scope_remaining_secs(scope), cr.TRUST_TTL_SECS)

    async def test_trust_is_reestablished_every_cycle(self):
        """The grant is in-memory only, so a restart drops it — the watchdog is
        what makes it restart-durable."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        # As a gateway restart leaves it: slot rehydrated, grant gone.
        safety_override().deactivate_scope(cr.autoapprove_scope(crew["id"]))
        slot._trust_scope = ""
        self.assertFalse(_effectively_trusted(slot))
        await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertTrue(_effectively_trusted(slot))

    async def test_trust_is_revoked_when_unattended_is_turned_off(self):
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        self.assertTrue(_effectively_trusted(slot))
        flipped = cs.update_crew(OWNER, REPO, crew["id"], {"unattended": False}, self.root)
        await cr.watchdog_cycle(state, OWNER, REPO, [flipped], self.root)
        self.assertFalse(_effectively_trusted(slot))
        # Revoked at the SOURCE, not merely unhooked from the slot: a stale scope
        # left live would re-trust the crew the moment any slot picked the key up.
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    async def test_a_lapsed_grant_is_not_trusted(self):
        """What the finding was actually about: with nothing renewing it, the grant
        RUNS OUT. Driven by making the scope inactive, never by sleeping."""
        crew = _crew(self.root, unattended=True)
        slot = await cr.ensure_crew_session(_FakeState(), OWNER, REPO, crew)
        self.assertTrue(_effectively_trusted(slot))
        # The slot still names the scope — the record still says unattended — and
        # that must not be enough on its own.
        self.assertEqual(slot._trust_scope, cr.autoapprove_scope(crew["id"]))
        with mock.patch.object(
            type(safety_override()), "is_scope_active", return_value=False
        ):
            self.assertFalse(_effectively_trusted(slot))

    async def test_a_grant_whose_audit_fails_is_never_usable(self):
        """Fail-closed. ``activate_scoped`` audits to the SEL BEFORE committing, so
        a SEL that cannot be written must leave the crew untrusted rather than
        auto-approving tools with no record that it was ever allowed to."""
        crew = _crew(self.root, unattended=True)
        with mock.patch(
            "kiro_crew.safety_override.sel", side_effect=OSError("SEL unavailable")
        ):
            slot = await cr.ensure_crew_session(_FakeState(), OWNER, REPO, crew)
        self.assertFalse(_effectively_trusted(slot))
        self.assertEqual(slot._trust_scope, "")
        self.assertFalse(slot._trust)  # and no fallback onto the unbounded flag
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    async def test_watchdog_revokes_trust_for_a_paused_or_retired_crew(self):
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        paused = cs.update_crew(
            OWNER, REPO, crew["id"], {"paused_reason": "operator paused"}, self.root
        )
        self.assertFalse(cr.is_live(paused))
        await cr.watchdog_cycle(state, OWNER, REPO, [paused], self.root)
        self.assertFalse(_effectively_trusted(slot))

    async def test_sync_trust_refuses_a_crew_that_is_not_live_whatever_calls_it(self):
        """Liveness is re-checked inside the grant, not only by the watchdog that
        usually calls it — so no future caller can hand a paused crew a grant."""
        crew = _crew(self.root, unattended=True)
        paused = cs.update_crew(
            OWNER, REPO, crew["id"], {"paused_reason": "operator paused"}, self.root
        )
        slot = _FakeSlot(f"crew-{crew['id']}")
        self.assertFalse(cr.sync_trust(slot, paused))
        self.assertFalse(_effectively_trusted(slot))

    async def test_wake_runs_a_turn_carrying_the_brief_and_the_nudge(self):
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci", next="round 3")
        runner = mock.Mock()
        # ``_run_chat`` is bound at this module's scope, so it is patched by name
        # here rather than through ``sys.modules``. The slot is handed the CAPPED
        # wrapper, not ``_run_chat`` itself — see :class:`TestTurnDispatch`.
        with mock.patch.object(cr, "_run_chat", runner):
            started = await cr.wake_crew(
                state, OWNER, REPO, crew, "#2201 ci-changed", self.root
            )
        self.assertTrue(started)
        self.assertEqual(slot.runners, [cr._capped_run_chat])
        self.assertEqual(len(slot.prompts), 1)
        prompt = slot.prompts[0]
        self.assertIn("[crew wake: #2201 ci-changed]", prompt)
        self.assertIn(cr.BRIEF_SENTINEL, prompt)     # first turn — brief injected
        self.assertIn("#2201 awaiting-ci", prompt)
        settings = cs.read_settings(OWNER, REPO, self.root)
        self.assertIn(cr.never_block(cr.writable_labels(settings)), prompt)

    async def test_wake_is_dropped_not_queued_while_the_crew_is_mid_turn(self):
        """A queued wake can carry the whole brief. Three busy sweeps would hand the
        crew three stacked copies of its own instructions, so a wake it cannot use
        is dropped — the refreshed loop message and the crew's own per-turn
        reconciliation both still cover the signal."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        slot.running = True
        with mock.patch.object(cr, "_run_chat", mock.Mock()):
            started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertFalse(started)
        self.assertEqual(slot.prompts, [])

    async def test_wake_without_a_session_is_not_a_crash(self):
        crew = _crew(self.root)
        state = _FakeState()
        with mock.patch.object(cr, "_rehydrate", return_value=None):
            self.assertFalse(
                await cr.wake_crew(state, OWNER, REPO, crew, "signal", self.root)
            )

    # ── the snapshot never runs on the event loop ──────────────────────────

    @contextlib.contextmanager
    def _snapshot_threads(self):
        """Record which thread each :func:`build_snapshot` call ran on."""
        seen: list[int] = []
        real = cr.build_snapshot

        def _record(*a: Any, **kw: Any) -> dict[str, Any]:
            seen.append(threading.get_ident())
            return real(*a, **kw)

        with mock.patch.object(cr, "build_snapshot", _record):
            yield seen

    def _assert_off_loop(self, seen: list[int], loop_thread: int) -> None:
        self.assertTrue(seen, "build_snapshot was never called")
        self.assertNotIn(loop_thread, seen)

    async def test_launch_composes_the_prompt_off_the_event_loop(self):
        """The snapshot globs the crew's item dir and parses every open item, and it
        grows with the crew's workload — on the loop it stalls the gateway and the
        always-on poll loop that is the only thing able to wake a crew when CI
        turns red."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        _item(self.root, crew["id"], 2201, phase="awaiting-ci", next="round 3")
        svc = _FakeNudge()
        loop_thread = threading.get_ident()
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            with self._snapshot_threads() as seen:
                await cr.launch_crew(state, OWNER, REPO, crew, self.root)
        self._assert_off_loop(seen, loop_thread)
        self.assertEqual(svc.added, [f"crew-{crew['id']}"])

    async def test_wake_composes_the_prompt_off_the_event_loop(self):
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci", next="round 3")
        loop_thread = threading.get_ident()
        with self._snapshot_threads() as seen:
            started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self._assert_off_loop(seen, loop_thread)
        self.assertTrue(started)
        # Off-loop composition must still produce the same prompt: brief + nudge.
        self.assertIn(cr.BRIEF_SENTINEL, slot.prompts[0])
        self.assertIn("#2201 awaiting-ci", slot.prompts[0])

    async def test_the_presence_check_stays_on_the_event_loop(self):
        """``slot.messages`` is loop-affine — a running turn appends to it — so only
        the store read is hoisted, the same split ``rehydrate_slot_from_history_async``
        documents."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real = cr.brief_is_present

        def _record(arg: Any) -> bool:
            seen.append(threading.get_ident())
            return real(arg)

        with mock.patch.object(cr, "brief_is_present", _record):
            await cr.compose_turn_prompt_async(slot, OWNER, REPO, crew, self.root)
        self.assertEqual(seen, [loop_thread])

    # ── restart: a persisted loop that outlived its slot ───────────────────

    async def test_watchdog_rehydrates_and_trusts_a_crew_whose_loop_outlived_its_slot(self):
        """The silent one. An armed loop is PERSISTED and fires against the slot key
        whether or not the gateway still holds the slot, while the auto-approve grant
        is in-memory and does not survive a restart. Skipping the rehydrate here left
        the crew's first post-restart turn untrusted, and an unattended crew then
        parks on an approval nobody is there to answer — no error, no symptom, until
        someone notices the crew stopped."""
        crew = _crew(self.root, unattended=True)
        slot_key = f"crew-{crew['id']}"
        state = _FakeState()  # no resident slot, as a restart leaves it
        revived = _FakeSlot(slot_key)
        svc = _FakeNudge([_FakeLoop("nl_0", slot_key, active=True)])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc), mock.patch.object(
            cr,
            "rehydrate_slot_from_history_async",
            new=mock.AsyncMock(return_value=revived),
        ) as rehydrate:
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        rehydrate.assert_awaited_once()
        self.assertTrue(_effectively_trusted(revived))
        # The loop already existed, so it must NOT be re-armed — a second loop on
        # one slot would double every crew's turn rate.
        self.assertEqual(svc.added, [])

    async def test_watchdog_creates_the_session_when_there_is_no_history_to_rehydrate(self):
        """A crew armed and then never given a turn has nothing on disk. The loop
        still needs a trusted session to fire into, so the session is created."""
        crew = _crew(self.root, unattended=True)
        slot_key = f"crew-{crew['id']}"
        state = _FakeState()
        svc = _FakeNudge([_FakeLoop("nl_0", slot_key, active=True)])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc), mock.patch.object(
            cr, "rehydrate_slot_from_history_async", new=mock.AsyncMock(return_value=None)
        ):
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertIn(slot_key, state.slots)
        self.assertTrue(_effectively_trusted(state.slots[slot_key]))
        self.assertEqual(svc.added, [])  # still no second loop

    async def test_watchdog_reactivates_a_deactivated_loop_after_rehydrating(self):
        """Reactivation and rehydration are independent: a crew that came back with
        no resident slot AND a deactivated loop needs both, so neither branch may
        shadow the other."""
        crew = _crew(self.root, unattended=True)
        slot_key = f"crew-{crew['id']}"
        state = _FakeState()
        revived = _FakeSlot(slot_key)
        svc = _FakeNudge([_FakeLoop("nl_0", slot_key, active=False)])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc), mock.patch.object(
            cr,
            "rehydrate_slot_from_history_async",
            new=mock.AsyncMock(return_value=revived),
        ):
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertTrue(_effectively_trusted(revived))
        self.assertTrue(svc.get_by_slot(slot_key).active)

    async def test_watchdog_does_not_rehydrate_a_crew_that_is_not_live(self):
        """A retired or paused crew must not be brought back into memory — the
        rehydrate exists to keep an armed loop trusted, and a dead crew has no
        business holding a session."""
        crew = _crew(self.root, unattended=True)
        retired = cs.update_crew(
            OWNER, REPO, crew["id"], {"paused_reason": "operator paused"}, self.root
        )
        svc = _FakeNudge([_FakeLoop("nl_0", f"crew-{crew['id']}", active=True)])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc), mock.patch.object(
            cr, "rehydrate_slot_from_history_async", new=mock.AsyncMock()
        ) as rehydrate:
            await cr.watchdog_cycle(_FakeState(), OWNER, REPO, [retired], self.root)
        rehydrate.assert_not_awaited()
        self.assertEqual(svc.updates, [("nl_0", {"active": False})])


# ── revoking execution (the two grants the record does not express) ─────────


class TestRevocation(unittest.IsolatedAsyncioTestCase):
    """Stopping a crew has to reach state the crew RECORD cannot express.

    ``enabled``, ``paused_reason`` and ``retired_at`` are all on disk; the two
    things that actually give a crew a turn are not. Its autonudge loop is a live
    timer owned by another service, and its auto-approve grant is an in-memory
    ``SafetyOverride`` scope that makes its tool calls auto-approve. Anything that
    writes only the record leaves both armed until the watchdog notices — and that
    runs on the app's poll interval, which is long enough for an idle timer to fire
    one more unattended turn on a crew a human just stopped.

    One helper serves the routes and the watchdog, so these tests pin the helper and
    the watchdog's use of it; the routes' own timing is pinned in the route tests.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        reset_singleton()
        self.addCleanup(reset_singleton)

    async def _armed(self, **spec) -> tuple[dict[str, Any], _FakeState, _FakeSlot, _FakeNudge]:
        """A crew that is genuinely running: trusted slot, active loop."""
        crew = _crew(self.root, unattended=True, **spec)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        # the grant this revocation has to remove
        self.assertTrue(_effectively_trusted(slot))
        return crew, state, slot, _FakeNudge([_FakeLoop("nl_0", slot.key, active=True)])

    async def test_it_clears_trust_and_deactivates_the_loop(self):
        crew, state, slot, svc = await self._armed()
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            self.assertTrue(await cr.revoke_crew_execution(state, crew, "paused"))
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))
        self.assertFalse(svc.get_by_slot(slot.key).active)

    async def test_it_also_clears_an_interactive_grant_a_human_left_behind(self):
        """Stopping a crew means stopped. ``sync_trust`` runs unprompted every cycle
        and so leaves a human's session trust alone, but this runs only because
        someone decided the crew must not run — including its slot."""
        crew, state, slot, svc = await self._armed()
        slot._trust = True  # as the approval card's "Trust all tools" leaves it
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.revoke_crew_execution(state, crew, "retired")
        self.assertFalse(slot._trust)
        self.assertFalse(_effectively_trusted(slot))

    async def test_revoking_twice_is_a_no_op_rather_than_an_error(self):
        """Retiring an already-paused crew, or a route racing the watchdog."""
        crew, state, slot, svc = await self._armed()
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.revoke_crew_execution(state, crew, "paused")
            self.assertFalse(await cr.revoke_crew_execution(state, crew, "retired"))
        self.assertEqual(svc.updates, [("nl_0", {"active": False})])

    async def test_a_failing_loop_service_still_clears_trust(self):
        """Best-effort, and the halves are independent: the grant that lets a turn
        run auto-approved must go even when the timer service cannot be reached."""
        crew, state, slot, svc = await self._armed()
        svc.update = mock.AsyncMock(side_effect=RuntimeError("registry busy"))  # type: ignore[method-assign]
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.revoke_crew_execution(state, crew, "retired")
        self.assertFalse(_effectively_trusted(slot))

    async def test_a_crew_with_no_resident_slot_is_still_un_armed(self):
        """After a restart the loop is persisted and the slot is not. Revocation
        must still reach the timer, or the crew gets a turn with no session."""
        crew = _crew(self.root, unattended=True)
        svc = _FakeNudge([_FakeLoop("nl_0", f"crew-{crew['id']}", active=True)])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            self.assertTrue(await cr.revoke_crew_execution(_FakeState(), crew, "paused"))
        self.assertFalse(svc.loops[0].active)

    async def test_the_watchdog_remains_the_backstop(self):
        """A crew stopped by editing the record directly never went through a
        route, so the sweep has to keep revoking on its own."""
        crew, state, slot, svc = await self._armed()
        paused = cs.update_crew(
            OWNER, REPO, crew["id"], {"paused_reason": "operator paused"}, self.root
        )
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.watchdog_cycle(state, OWNER, REPO, [paused], self.root)
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(svc.get_by_slot(slot.key).active)


# ── turn dispatch runs under the background-turn cap ────────────────────────


class TestTurnDispatch(unittest.IsolatedAsyncioTestCase):
    """Every crew turn must be charged against the background-turn cap.

    The cap lives INSIDE ``DashboardState.run_background_turn``, so a dispatch that
    hands ``_run_chat`` straight to ``enqueue_or_run_prompt`` is not merely
    uncounted — it is uncapped. Crews are the one fleet in the product that arms N
    independent loops, so simultaneous wakes plus a human's guidance injection could
    put more turns on the runtime than the cap allows while its counters reported a
    smaller number, which reads as a healthy fleet.

    Both dispatch sites go through :func:`crew_runtime.dispatch_crew_turn`; the wake
    is pinned here and the guidance route in the route tests.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    async def test_a_wake_hands_the_slot_the_capped_runner(self):
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertTrue(started)
        self.assertEqual(slot.runners, [cr._capped_run_chat])

    async def test_the_dispatched_turn_actually_goes_through_the_cap(self):
        """Not just "a wrapper was handed over": the wrapper is run, and the turn it
        starts is the one the cap holds a permit for."""
        state = _FakeState()
        slot = _FakeSlot()
        ran: list[str] = []

        async def _turn(_state: Any, _slot: Any, prompt: str) -> None:
            ran.append(prompt)

        with mock.patch.object(cr, "_run_chat", _turn):
            self.assertTrue(cr.dispatch_crew_turn(state, slot, "advance one item"))
            await slot.runners[-1](state, slot, slot.prompts[-1])
        self.assertEqual(state.capped, [slot.key])
        self.assertEqual(ran, ["advance one item"])

    async def test_a_turn_that_never_got_a_permit_says_so_in_the_transcript(self):
        """A refused turn and a finished one must not look the same.

        ``run_background_turn`` queues rather than rejecting, so reaching this means
        the cap's whole wait budget expired and NOTHING ran. Reported in the crew's
        own session because that is where a human looks when a crew seems stalled.
        """
        state = _FakeState()
        state.permit_timeout = True
        slot = _FakeSlot()

        async def _turn(_state: Any, _slot: Any, prompt: str) -> None:
            raise AssertionError("the turn must not run without a permit")

        with mock.patch.object(cr, "_run_chat", _turn):
            cr.dispatch_crew_turn(state, slot, "advance one item")
            await slot.runners[-1](state, slot, slot.prompts[-1])
        cards = [m for m in slot.messages if m["role"] == "error"]
        self.assertEqual(len(cards), 1)
        self.assertIn("never started", cards[0]["content"])

    async def test_a_turn_that_ran_leaves_no_card(self):
        state = _FakeState()
        slot = _FakeSlot()
        with mock.patch.object(cr, "_run_chat", mock.AsyncMock()):
            cr.dispatch_crew_turn(state, slot, "advance one item")
            await slot.runners[-1](state, slot, slot.prompts[-1])
        self.assertEqual([m for m in slot.messages if m["role"] == "error"], [])


# ── unblock signal detection (pure) ─────────────────────────────────────────


class TestDetectUnblocks(unittest.TestCase):
    BASE = {
        "issue_comments": 2,
        "checks": "failure",
        "check_counts": {"failure": 1, "success": 40, "running": 0, "other": 2},
        "review_decision": "",
        "conflicted": False,
        "merged": False,
        "pr_comments": 3,
    }

    def _detect(self, **changes):
        return cr.detect_unblocks(dict(self.BASE), {**self.BASE, **changes})

    def test_first_observation_reports_nothing(self):
        # Cold start seeds the mark. Reporting here would wake every crew on every
        # open item the moment the gateway restarts.
        self.assertEqual(cr.detect_unblocks(None, dict(self.BASE)), [])
        self.assertEqual(cr.detect_unblocks({}, dict(self.BASE)), [])

    def test_no_change_reports_nothing(self):
        self.assertEqual(self._detect(), [])

    def test_requester_replied(self):
        self.assertEqual(self._detect(issue_comments=3), [cr.SIG_REPLY])

    def test_ci_state_changed(self):
        self.assertEqual(self._detect(checks="success"), [cr.SIG_CI])

    def test_ci_counts_changed_without_the_rollup_moving(self):
        counts = {"failure": 1, "success": 41, "running": 0, "other": 2}
        self.assertEqual(self._detect(check_counts=counts), [cr.SIG_CI])

    def test_unknown_ci_is_not_a_ci_change(self):
        # A failed enrichment call reports None. Treating unknown-vs-known as
        # movement would wake the crew every time the GraphQL leg flakes.
        self.assertEqual(self._detect(checks=None, check_counts=None), [])

    def test_review_approved_and_changes_requested(self):
        self.assertEqual(self._detect(review_decision="approved"), [cr.SIG_REVIEW])
        self.assertEqual(
            self._detect(review_decision="changes_requested"), [cr.SIG_REVIEW]
        )

    def test_a_withdrawn_verdict_is_not_a_signal(self):
        prev = {**self.BASE, "review_decision": "approved"}
        self.assertEqual(cr.detect_unblocks(prev, dict(self.BASE)), [])

    def test_merge_conflict_appeared(self):
        self.assertEqual(self._detect(conflicted=True), [cr.SIG_CONFLICT])

    def test_conflict_already_known_is_not_re_reported(self):
        prev = {**self.BASE, "conflicted": True}
        cur = {**self.BASE, "conflicted": True}
        self.assertEqual(cr.detect_unblocks(prev, cur), [])

    def test_pr_merged(self):
        self.assertEqual(self._detect(merged=True), [cr.SIG_MERGED])

    def test_post_merge_comment(self):
        prev = {**self.BASE, "merged": True}
        cur = {**self.BASE, "merged": True, "pr_comments": 4}
        self.assertEqual(cr.detect_unblocks(prev, cur), [cr.SIG_POST_MERGE])

    def test_uncomputed_mergeability_is_not_a_conflict(self):
        # GitHub answers mergeable: null on a cold read and computes it in the
        # background; truthiness would report every cold read as a conflict.
        self.assertFalse(cr._is_conflicted(None, "unknown"))
        self.assertTrue(cr._is_conflicted(False, "unknown"))
        self.assertTrue(cr._is_conflicted(None, "dirty"))

    def test_every_signal_has_a_detector(self):
        # Guards against a signal being added to the table and never wired up.
        seen = set()
        for changes in (
            {"issue_comments": 9},
            {"checks": "success"},
            {"review_decision": "approved"},
            {"conflicted": True},
            {"merged": True},
        ):
            seen.update(self._detect(**changes))
        prev = {**self.BASE, "merged": True}
        seen.update(cr.detect_unblocks(prev, {**prev, "pr_comments": 99}))
        self.assertEqual(seen, set(cr.UNBLOCK_SIGNALS))


# ── the sweep ───────────────────────────────────────────────────────────────


class _FakeClient:
    """The gh layer, stubbed. Counts calls so the sweep's API cost is assertable."""

    def __init__(self, issue=None, pr=None, timeline=None, enriched=None):
        self.issue = issue or {"comments": 0, "state": "open"}
        self.pr = pr or {}
        self.timeline = timeline or []
        self.enriched = enriched or []
        self.calls: list[str] = []

    def get_issue_detail(self, owner, repo, number, **kw):
        self.calls.append(f"issue:{number}")
        return dict(self.issue)

    def get_pr_detail(self, owner, repo, number, **kw):
        self.calls.append(f"pr:{number}")
        return dict(self.pr)

    def list_issue_timeline(self, owner, repo, number, **kw):
        self.calls.append(f"timeline:{number}")
        return list(self.timeline)

    def enrich_pulls_by_number(self, owner, repo, pulls, **kw):
        self.calls.append("enrich")
        return list(self.enriched)


class TestSweep(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    async def _sweep(self, client, state=None):
        with mock.patch.object(provider, "client_for", return_value=client), \
             mock.patch.object(cr.provider, "client_for", return_value=client), \
             mock.patch.object(cr, "wake_crew", new=mock.AsyncMock(return_value=True)) as wake:
            woken = await cr.sweep_repo(_app(state), _KEY, self.root)
        return woken, wake

    async def test_first_sweep_seeds_without_waking(self):
        crew = _crew(self.root, unattended=True)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci")
        client = _FakeClient(issue={"comments": 2, "state": "open"})
        woken, wake = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {})
        wake.assert_not_awaited()
        # The mark is stored, so the SECOND sweep has something to compare against.
        stored = cr.read_signals(OWNER, REPO, self.root)
        self.assertIn(f"{crew['id']}:2201", stored)

    async def test_second_sweep_wakes_the_owning_crew_on_a_reply(self):
        crew = _crew(self.root, unattended=True)
        _item(self.root, crew["id"], 2201, phase="awaiting-reply")
        client = _FakeClient(issue={"comments": 2, "state": "open"})
        await self._sweep(client, _FakeState())
        # Backdate the mark so the phase's recheck interval has elapsed.
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew['id']}:2201"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)

        client.issue = {"comments": 3, "state": "open"}
        woken, wake = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {crew["id"]: [cr.SIG_REPLY]})
        wake.assert_awaited_once()
        self.assertIn("requester-replied", wake.await_args.args[4])

    async def test_selected_items_are_never_fetched(self):
        # Pre-claim and local only: there is nothing public to watch, and reading
        # it would cost an API call per crew per minute for every shortlisted issue.
        crew = _crew(self.root)
        _item(self.root, crew["id"], 42, phase="selected")
        client = _FakeClient()
        woken, _ = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {})
        self.assertEqual(client.calls, [])

    async def test_a_retired_crew_is_not_swept(self):
        crew = _crew(self.root)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci")
        cs.retire_crew(OWNER, REPO, crew["id"], self.root)
        client = _FakeClient()
        woken, _ = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {})
        self.assertEqual(client.calls, [])

    async def test_api_cost_per_item_is_two_reads_plus_one_batched_enrichment(self):
        crew = _crew(self.root)
        cid = crew["id"]
        _item(self.root, cid, 2201, phase="awaiting-ci", pr_number=101)
        _item(self.root, cid, 2202, phase="awaiting-ci", pr_number=102)
        client = _FakeClient(
            issue={"comments": 1, "state": "open"},
            pr={"comments": 0, "updated_at": "t0", "merged": False, "mergeable": True},
            enriched=[
                {"number": 101, "checks_state": "success", "checks_counts": {}},
                {"number": 102, "checks_state": "success", "checks_counts": {}},
            ],
        )
        await self._sweep(client, _FakeState())
        # One issue read + one PR read per item, and ONE batched enrichment for the
        # whole repo (two GraphQL round-trips inside it) — not one per PR.
        self.assertEqual(client.calls.count("enrich"), 1)
        self.assertEqual(client.calls.count("issue:2201"), 1)
        self.assertEqual(client.calls.count("pr:101"), 1)
        self.assertEqual(len([c for c in client.calls if c.startswith("timeline")]), 2)

    async def test_review_read_is_skipped_when_the_pr_did_not_move(self):
        crew = _crew(self.root)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci", pr_number=101)
        client = _FakeClient(
            issue={"comments": 1, "state": "open"},
            pr={"comments": 0, "updated_at": "t0", "merged": False, "mergeable": True},
            enriched=[{"number": 101, "checks_state": "success", "checks_counts": {}}],
        )
        await self._sweep(client, _FakeState())
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew['id']}:2201"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)
        client.calls.clear()
        await self._sweep(client, _FakeState())
        # updated_at unchanged -> the paginated timeline read is not paid again.
        self.assertNotIn("timeline:101", client.calls)

    async def test_review_verdict_is_read_when_the_pr_moved(self):
        crew = _crew(self.root, unattended=True)
        _item(self.root, crew["id"], 2201, phase="addressing-review", pr_number=101)
        client = _FakeClient(
            issue={"comments": 1, "state": "open"},
            pr={"comments": 0, "updated_at": "t0", "merged": False, "mergeable": True},
            enriched=[{"number": 101, "checks_state": "success", "checks_counts": {}}],
        )
        await self._sweep(client, _FakeState())
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew['id']}:2201"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)

        client.pr = {"comments": 0, "updated_at": "t1", "merged": False, "mergeable": True}
        client.timeline = [
            {"kind": "reviewed", "review_state": "APPROVED", "created_at": "t1"},
        ]
        woken, wake = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {crew["id"]: [cr.SIG_REVIEW]})
        wake.assert_awaited_once()

    async def test_a_failed_item_read_leaves_its_mark_untouched(self):
        crew = _crew(self.root)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci")
        client = _FakeClient()
        client.get_issue_detail = mock.Mock(side_effect=RuntimeError("gh exploded"))
        woken, _ = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {})
        # No mark: the change (whatever it was) is still pending next cycle rather
        # than being silently consumed by the error.
        self.assertEqual(cr.read_signals(OWNER, REPO, self.root), {})

    async def test_recheck_cadence_is_phase_aware(self):
        self.assertLess(cr.RECHECK_SEC["awaiting-ci"], cr.RECHECK_SEC["awaiting-reply"])
        stored = {"checked_at": 1000.0}
        self.assertTrue(cr._is_due({"phase": "awaiting-ci"}, stored, 1000.0 + 61))
        self.assertFalse(cr._is_due({"phase": "awaiting-reply"}, stored, 1000.0 + 61))
        # A mark in the future (clock correction) must not park the item forever.
        self.assertTrue(cr._is_due({"phase": "awaiting-reply"}, stored, 900.0))

    async def test_two_signalling_items_wake_the_crew_once_with_both_reasons(self):
        crew = _crew(self.root, unattended=True)
        cid = crew["id"]
        _item(self.root, cid, 2201, phase="awaiting-reply")
        _item(self.root, cid, 2202, phase="awaiting-reply")
        client = _FakeClient(issue={"comments": 1, "state": "open"})
        await self._sweep(client, _FakeState())
        stored = cr.read_signals(OWNER, REPO, self.root)
        for k in stored:
            stored[k]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)

        client.issue = {"comments": 5, "state": "open"}
        woken, wake = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {cid: [cr.SIG_REPLY, cr.SIG_REPLY]})
        # ONE turn, both reasons — the second call would have been dropped as
        # mid-turn, so the crew would only have heard about the first item.
        wake.assert_awaited_once()
        reason = wake.await_args.args[4]
        self.assertIn("#2201", reason)
        self.assertIn("#2202", reason)

    async def test_marks_for_finished_items_are_pruned(self):
        crew = _crew(self.root, unattended=True)
        cid = crew["id"]
        _item(self.root, cid, 2201, phase="awaiting-ci")
        client = _FakeClient(issue={"comments": 1, "state": "open"})
        await self._sweep(client, _FakeState())
        self.assertIn(f"{cid}:2201", cr.read_signals(OWNER, REPO, self.root))
        # Resolved items leave the open set, so their fingerprints must go too —
        # otherwise a long-lived crew rewrites every issue it ever closed, every
        # minute, forever.
        _item(self.root, cid, 2201, phase="resolved")
        await self._sweep(client, _FakeState())
        self.assertEqual(cr.read_signals(OWNER, REPO, self.root), {})

    async def test_schema_mismatch_is_a_cache_miss(self):
        cr.signals_path(OWNER, REPO, self.root).write_text(
            '{"schema": 999, "items": {"c_x:1": {"fp": {}}}}'
        )
        self.assertEqual(cr.read_signals(OWNER, REPO, self.root), {})


# ── how the sweep is gated in the poll loop ─────────────────────────────────


class TestDismissal(unittest.IsolatedAsyncioTestCase):
    """Closing a crew's chat tab must PAUSE that crew — and only that case.

    The bug this pins: the watchdog re-establishes a live crew's missing nudge loop
    (correct after a restart, which drops the in-memory registry) and so undid the
    close handler's deliberate loop removal on the very next sweep, resurrecting a
    tab the user had just dismissed.

    Both directions are asserted here on purpose. Gating the re-arm on any signal
    idle archival also writes (``closed``/``closed_at`` — both paths stamp both)
    would trade a visible resurrection for a silent death: a crew that was merely
    quiet would never be re-armed and would sit enabled and stopped with nothing
    explaining why.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _teardown_mod(self):
        """Imported per-test, never in ``setUp``.

        A ``setUp`` that touches the hook registry makes EVERY test in the class
        fail with an ``AttributeError`` the moment the seam is missing — including
        ``test_a_crew_with_no_loop_and_no_dismissal_IS_re_armed``, whose whole job
        is to stay green when the fix is removed. A shared fixture that couples an
        independent assertion to the change under test destroys the only signal
        that assertion carries.
        """
        from kiro_crew.apps import teardown

        self.addCleanup(teardown.unregister_slot_close_hook, cr.APP_NAME)
        return teardown

    def _repos(self):
        """``list_connected_repos`` is what turns a bare slot key into a repo."""
        return mock.patch.object(
            cr.store,
            "list_connected_repos",
            return_value=[{"owner": OWNER, "repo": REPO}],
        )

    async def _sweep(self, crew, state, nudge):
        with mock.patch.object(cr, "_autonudge_instance", return_value=nudge):
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)

    # ── the fix ─────────────────────────────────────────────────────────────

    async def test_dismissing_the_tab_pauses_the_crew(self):
        crew = _crew(self.root)
        with self._repos():
            await cr._on_slot_closed(crew["slot_key"], root=self.root)
        after = cs.read_crew(OWNER, REPO, crew["id"], self.root)
        assert after is not None
        self.assertFalse(after["enabled"])
        self.assertEqual(after["paused_reason"], cr.DISMISSED_PAUSE_REASON)
        # Visible AND not live, so the sweep's existing revocation branch takes it
        # from here — no new watchdog state.
        self.assertFalse(cr.is_live(after))

    async def test_taking_the_dismissal_back_resumes_the_crew(self):
        """A close that failed after the pause must not leave the worker stopped.

        The close cannot make the slot table, the history file and the crew store
        atomic, so the pause has to be undoable: without this the user gets an
        error AND a silently disabled crew.
        """
        crew = _crew(self.root)
        with self._repos():
            await cr._on_slot_closed(crew["slot_key"], root=self.root)
            paused = cs.read_crew(OWNER, REPO, crew["id"], self.root)
            assert paused is not None
            self.assertFalse(paused["enabled"])
            await cr._on_slot_close_undone(crew["slot_key"], root=self.root)
        after = cs.read_crew(OWNER, REPO, crew["id"], self.root)
        assert after is not None
        self.assertTrue(after["enabled"], "the crew stayed paused after a failed close")
        self.assertFalse(after.get("paused_reason"))

    async def test_taking_it_back_never_resumes_a_pause_someone_else_set(self):
        """Only THIS hook's own pause is undone.

        ``_on_slot_closed`` refuses to overwrite an existing ``paused_reason``, so a
        crew stopped for any other cause was never touched by the close — resuming
        it would turn a failed tab close into a worker restart nobody asked for.
        """
        crew = _crew(self.root)
        cs.set_crew_paused(OWNER, REPO, crew["id"], True, "you paused this", self.root)
        with self._repos():
            await cr._on_slot_closed(crew["slot_key"], root=self.root)
            await cr._on_slot_close_undone(crew["slot_key"], root=self.root)
        after = cs.read_crew(OWNER, REPO, crew["id"], self.root)
        assert after is not None
        self.assertFalse(after["enabled"], "someone else's pause was overridden")
        self.assertEqual(after.get("paused_reason"), "you paused this")

    async def test_a_dismissed_crew_is_not_re_armed(self):
        """The regression. A live crew, loop removed, tab dismissed."""
        crew = _crew(self.root)
        state = _FakeState()
        nudge = _FakeNudge()  # the close handler already removed the loop
        with self._repos():
            await cr._on_slot_closed(crew["slot_key"], root=self.root)
        dismissed = cs.read_crew(OWNER, REPO, crew["id"], self.root)
        assert dismissed is not None
        await self._sweep(dismissed, state, nudge)
        self.assertEqual(nudge.added, [])
        self.assertEqual(state.created, [])

    async def test_a_crew_with_no_loop_and_no_dismissal_IS_re_armed(self):
        """The behaviour the fix must not cost: recovery after a restart.

        Same observable input as the test above — live crew, no loop, no resident
        slot — differing only in that nobody dismissed it. An unattended crew with
        no loop has no clock at all, so this MUST re-arm.
        """
        crew = _crew(self.root)
        state = _FakeState()
        nudge = _FakeNudge()
        with mock.patch.object(cr, "_rehydrate", new=mock.AsyncMock(return_value=None)):
            await self._sweep(crew, state, nudge)
        self.assertEqual(nudge.added, [crew["slot_key"]])

    async def test_idle_archival_does_not_reach_the_hook(self):
        """Only a deliberate ✕ pauses. Quietness must leave the record alone."""
        teardown = self._teardown_mod()
        crew = _crew(self.root)
        calls: list[str] = []

        async def _hook(slot_key: str) -> None:
            calls.append(slot_key)

        teardown.register_slot_close_hook(cr.APP_NAME, _hook)
        # The bulk idle-archive path persists ``closed=True`` + ``closed_at`` for
        # this same slot and never notifies — which is the whole distinction.
        await teardown.notify_slot_closed("some-other-app", crew["slot_key"])
        self.assertEqual(calls, [])
        await teardown.notify_slot_closed(cr.APP_NAME, crew["slot_key"])
        self.assertEqual(calls, [crew["slot_key"]])

    # ── the seam ────────────────────────────────────────────────────────────

    async def test_the_watchdog_re_registers_the_hook_after_a_restart(self):
        """The registry is process memory, so a one-shot registration would leave
        the ✕ silently ignored for the rest of that process's life."""
        teardown = self._teardown_mod()
        crew = _crew(self.root)
        teardown.unregister_slot_close_hook(cr.APP_NAME)
        await self._sweep(crew, _FakeState(), _FakeNudge())
        self.assertIn(cr.APP_NAME, teardown._SLOT_CLOSE_HOOKS)

    async def test_the_watcher_registers_the_hook_before_it_ever_sweeps(self):
        """The watchdog's registration is a full poll interval away.

        ``_watch_loop`` sleeps ``POLL_INTERVAL_SEC`` before its first sweep, so a
        registration that only happens inside that sweep leaves the ✕ ignored for
        the first minute of every process — and the sweep that finally arrives is
        the thing that resurrects the crew the user just closed.

        ``_poll_once`` is stubbed to abort the loop, so the sweep (and therefore
        the watchdog's own idempotent registration) never runs. The hook can only
        be present here if the loop registered it on entry.
        """
        from kiro_crew.apps.builtins.issue_radar.backend import watch

        teardown = self._teardown_mod()
        teardown.unregister_slot_close_hook(cr.APP_NAME)

        async def _abort(_app):
            raise asyncio.CancelledError

        with mock.patch.object(watch, "POLL_INTERVAL_SEC", 0), mock.patch.object(
            watch, "_poll_once", _abort
        ):
            await watch._watch_loop(mock.MagicMock())

        self.assertIn(cr.APP_NAME, teardown._SLOT_CLOSE_HOOKS)

    async def test_the_disable_hook_is_installed_before_the_first_sleep(self):
        """Same timing argument, sharper consequence.

        Until the disable hook is registered, switching the app off only writes a
        flag: the crews keep their auto-approve grants and their armed loops until
        this loop next wakes, which is the whole window the hook exists to close. So
        it cannot wait for the first sweep either, and the state it captures must be
        the gateway's — a hook holding nothing revokes nothing.
        """
        installed: list[Any] = []

        async def _abort(_app):
            raise asyncio.CancelledError

        with (
            mock.patch.object(watch_mod, "POLL_INTERVAL_SEC", 0),
            mock.patch.object(watch_mod, "_poll_once", _abort),
            mock.patch.object(
                watch_mod.crew_runtime,
                "install_app_disable_hook",
                side_effect=lambda state: bool(installed.append(state)) or True,
            ),
        ):
            await watch_mod._watch_loop(cast(Any, {"state": "the-gateway-state"}))
        self.assertEqual(installed, ["the-gateway-state"])

    async def test_a_crew_on_another_providers_root_is_still_found(self):
        """The registry holds ONE hook per app, and the watchdog registers it per
        swept repo with that repo's PROVIDER root — so the last sweep's provider won.
        A mixed GitHub/GitLab install keeps each provider's records under its own
        root, so closing the tab of a crew on the losing provider found nothing: the
        crew stayed live and the next sweep re-armed its auto-approved session.

        Here the hook is registered scoped to a root that does NOT hold the crew, and
        the crew must still be found through the provider fallback.
        """
        teardown = self._teardown_mod()
        crew = _crew(self.root)
        elsewhere = Path(self._tmp.name) / "other-provider"
        elsewhere.mkdir(parents=True, exist_ok=True)

        # Registered against a root with no crews in it at all.
        cr.install_slot_close_hook(elsewhere)
        with self._repos(), mock.patch.object(
            cr, "_lookup_scopes", return_value=[elsewhere, self.root]
        ):
            await teardown.notify_slot_closed(cr.APP_NAME, str(crew["slot_key"]))

        after = cs.read_crew(OWNER, REPO, str(crew["id"]), self.root)
        assert after is not None
        self.assertEqual(after.get("paused_reason"), cr.DISMISSED_PAUSE_REASON)

    async def test_a_failing_hook_never_blocks_the_close(self):
        teardown = self._teardown_mod()

        async def _boom(slot_key: str) -> None:
            raise RuntimeError("store busy")

        teardown.register_slot_close_hook(cr.APP_NAME, _boom)
        await teardown.notify_slot_closed(cr.APP_NAME, "crew-c_1")

    async def test_an_unknown_slot_key_is_ignored(self):
        with self._repos():
            await cr._on_slot_closed("crew-c_deadbeef", root=self.root)

    async def test_a_specific_pause_reason_is_not_overwritten(self):
        crew = _crew(self.root)
        cs.set_crew_paused(OWNER, REPO, crew["id"], True, "waiting on a decision", self.root)
        with self._repos():
            await cr._on_slot_closed(crew["slot_key"], root=self.root)
        after = cs.read_crew(OWNER, REPO, crew["id"], self.root)
        assert after is not None
        self.assertEqual(after["paused_reason"], "waiting on a decision")

    async def test_resuming_clears_the_reason_and_re_arms(self):
        """Reversible: the existing pause route's resume brings the crew back."""
        crew = _crew(self.root)
        with self._repos():
            await cr._on_slot_closed(crew["slot_key"], root=self.root)
        resumed = cs.set_crew_paused(OWNER, REPO, crew["id"], False, "", self.root)
        self.assertTrue(resumed["enabled"])
        self.assertEqual(resumed["paused_reason"], "")
        self.assertTrue(cr.is_live(resumed))
        nudge = _FakeNudge()
        with mock.patch.object(cr, "_rehydrate", new=mock.AsyncMock(return_value=None)):
            await self._sweep(resumed, _FakeState(), nudge)
        self.assertEqual(nudge.added, [crew["slot_key"]])


class TestWatchGating(unittest.IsolatedAsyncioTestCase):
    """The two gates in ``watch.py`` and the difference between them."""

    def _watch(self):
        from kiro_crew.apps.builtins.issue_radar.backend import watch

        watch._crews_suspended = False
        return watch

    async def test_sweep_does_not_inherit_the_notify_preference(self):
        watch = self._watch()
        entries = [{"owner": OWNER, "repo": REPO, "provider": "github", "host": "github.com"}]
        with contextlib.ExitStack() as stack:
            use = stack.enter_context
            use(mock.patch.object(watch, "is_app_enabled", return_value=True))
            use(mock.patch.object(watch.store, "list_connected_repos", return_value=entries))
            use(
                mock.patch.object(
                    watch.store,
                    "read_repo_settings",
                    return_value={"notify_on_new_issue": False},
                )
            )
            poll = use(mock.patch.object(watch, "_poll_repo", new=mock.AsyncMock()))
            sweep = use(
                mock.patch.object(
                    watch.crew_runtime, "sweep_repo", new=mock.AsyncMock(return_value={})
                )
            )
            await watch._poll_once(_app(_FakeState()))
        # Muting the bell must not stop a crew reconciling its own pull requests.
        poll.assert_not_awaited()
        sweep.assert_awaited_once()

    async def test_a_failing_new_issue_poll_still_runs_the_sweep(self):
        watch = self._watch()
        entries = [{"owner": OWNER, "repo": REPO}]
        with contextlib.ExitStack() as stack:
            use = stack.enter_context
            use(mock.patch.object(watch, "is_app_enabled", return_value=True))
            use(mock.patch.object(watch.store, "list_connected_repos", return_value=entries))
            use(
                mock.patch.object(
                    watch.store,
                    "read_repo_settings",
                    return_value={"notify_on_new_issue": True},
                )
            )
            use(
                mock.patch.object(
                    watch, "_poll_repo", new=mock.AsyncMock(side_effect=RuntimeError("gh down"))
                )
            )
            sweep = use(
                mock.patch.object(
                    watch.crew_runtime, "sweep_repo", new=mock.AsyncMock(return_value={})
                )
            )
            await watch._poll_once(_app(_FakeState()))
        sweep.assert_awaited_once()

    async def test_disabling_the_app_suspends_the_crews(self):
        watch = self._watch()
        with contextlib.ExitStack() as stack:
            use = stack.enter_context
            use(mock.patch.object(watch, "is_app_enabled", return_value=False))
            suspend = use(
                mock.patch.object(
                    watch.crew_runtime, "suspend_crews", new=mock.AsyncMock(return_value=0)
                )
            )
            repos = use(mock.patch.object(watch.store, "list_connected_repos"))
            await watch._poll_once(_app(_FakeState()))
            await watch._poll_once(_app(_FakeState()))
        # A disabled app stays silent — no config walk — and suspends only ONCE,
        # because nothing re-establishes trust while the app is off.
        repos.assert_not_called()
        suspend.assert_awaited_once()

    async def test_suspension_clears_trust_on_resident_crew_slots(self):
        reset_singleton()
        self.addCleanup(reset_singleton)
        state = _FakeState()
        slot = _FakeSlot("crew-c_abc")
        slot._app = "issue-radar"
        cr.sync_trust(slot, {"id": "c_abc", "unattended": True, "enabled": True})
        self.assertTrue(_effectively_trusted(slot))
        other = _FakeSlot("chat-1")
        other._app = ""
        other._trust = True
        state._slots = {"crew-c_abc": slot, "chat-1": other}
        cleared = await cr.suspend_crews(state)
        self.assertEqual(cleared, 1)
        self.assertFalse(_effectively_trusted(slot))
        # Revoked at the source too, so re-attaching a slot cannot inherit it.
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope("c_abc")))
        self.assertTrue(other._trust)  # a user's own session is not ours to touch


class TestDisablingTheAppRevokesInline(unittest.IsolatedAsyncioTestCase):
    """Disabling the app must stop the crews IN THE REQUEST, not on the next sweep.

    The window this pins: the sweep runs every ``POLL_INTERVAL_SEC``, so a
    suspension that only happened there left up to a full minute in which an
    already-armed nudge fired a whole auto-approved turn for an app the operator had
    just switched off. "Stop" has to mean stopped, the way pause and retire already
    revoke inline before they answer.

    Nothing here sleeps or polls: every assertion is made on the state left behind
    the moment the disable hook's coroutine returns.
    """

    def setUp(self):
        reset_singleton()
        self.addCleanup(reset_singleton)
        from kiro_crew.apps import teardown

        # ``watchdog_cycle`` re-registers the dismissal hook, which is process-wide
        # state; drop it again so these tests cannot change another's outcome.
        self.addCleanup(teardown.unregister_slot_close_hook, cr.APP_NAME)
        self.state = _FakeState()
        self.slot = _FakeSlot("crew-c_d15ab1ed")
        self.slot._app = cr.APP_NAME
        cr.sync_trust(self.slot, {"id": "c_d15ab1ed", "unattended": True, "enabled": True})
        self.assertTrue(_effectively_trusted(self.slot), "fixture never got its grant")
        # Both registries: ``get_slot`` reads the public one, the suspension walks
        # the private one it can enumerate.
        self.state.slots[self.slot.key] = self.slot
        self.state._slots = {self.slot.key: self.slot}
        self.nudge = _FakeNudge([_FakeLoop("nl_dis", self.slot.key)])

    def _register(self) -> Any:
        """Install the hook against a stand-in for core's registry, and return it.

        ``create=True``: the registry is core's to add (see the report accompanying
        this change), and the app must keep working — on the sweep alone — against a
        build that has none. Patching it in is what lets this test assert the app
        registers the right callable under its own name, so the seam is live the
        moment core calls it.
        """
        from kiro_crew.apps import teardown

        registry: dict[str, Any] = {}
        patch = mock.patch.object(
            teardown,
            "register_app_disable_hook",
            create=True,
            side_effect=lambda app, hook: registry.__setitem__(app, hook),
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.assertTrue(cr.install_app_disable_hook(self.state))
        self.assertIn(cr.APP_NAME, registry)
        return registry[cr.APP_NAME]

    async def test_the_registered_hook_revokes_with_no_poll_at_all(self):
        hook = self._register()
        with (
            mock.patch.object(cr, "_autonudge_instance", return_value=self.nudge),
            mock.patch.object(watch_mod, "_poll_once", new=mock.AsyncMock()) as poll,
        ):
            # Core passes the disabled app's name; the registration is already
            # per-app, so the argument is accepted and ignored.
            cleared = await hook(cr.APP_NAME)
        self.assertEqual(cleared, 1)
        self.assertFalse(_effectively_trusted(self.slot))
        # Revoked at the source too, so re-attaching a slot cannot inherit it.
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope("c_dis")))
        # And the crew's clock is stopped, so no later turn is even scheduled.
        self.assertEqual(self.nudge.updates, [("nl_dis", {"active": False})])
        poll.assert_not_awaited()

    async def test_the_grant_is_gone_before_the_first_await(self):
        """GRANTS BEFORE LOOPS is the ordering that closes the window.

        Deactivating a loop awaits, so it hands the event loop back. Doing it first
        would leave the grants live across that suspension point, and an armed timer
        firing in the gap would take its auto-approved turn from the very call that
        was stopping it. Asserted from inside the await itself, because that is the
        only moment at which a reversed order is observable.
        """
        observed: list[bool] = []

        async def _update(loop_id: str, **kw: Any) -> None:
            observed.append(_effectively_trusted(self.slot))
            self.nudge.updates.append((loop_id, dict(kw)))

        with (
            mock.patch.object(cr, "_autonudge_instance", return_value=self.nudge),
            mock.patch.object(self.nudge, "update", new=_update),
        ):
            await cr.on_app_disabled(self.state)
        self.assertEqual(observed, [False], "the loop was stopped before the grant")

    async def test_an_armed_nudge_that_fires_after_disable_is_not_trusted(self):
        """The turn a nudge fires is run by core, which asks the slot at fire time.

        So the assertion that matters is not "did we call something" but what the
        shared approval path answers for this slot AFTER the disable — and it must
        answer the same for the human ``_trust`` flag, which a crew session can also
        be carrying when someone clicked it.
        """
        self.slot._trust = True
        hook = self._register()
        with mock.patch.object(cr, "_autonudge_instance", return_value=self.nudge):
            await hook(cr.APP_NAME)
        # Simulate the fire landing anyway (a timer already inside its sleep, or a
        # gateway with no hook registry at all): core resolves trust here.
        self.assertFalse(_effectively_trusted(self.slot))
        self.assertFalse(self.slot._trust)
        self.assertEqual(self.slot._trust_scope, "")

    async def test_the_sweep_still_suspends_a_flag_flipped_behind_our_back(self):
        """The BACKSTOP. ``kirocrew app disable`` runs in another process and an
        ``installed.json`` can be edited on disk, so neither can reach an in-process
        hook. The sweep is the only thing that catches those, and it must keep
        working with no hook registered at all."""
        watch_mod._crews_suspended = False
        self.addCleanup(setattr, watch_mod, "_crews_suspended", False)
        with (
            mock.patch.object(watch_mod, "is_app_enabled", return_value=False),
            mock.patch.object(cr, "_autonudge_instance", return_value=self.nudge),
            mock.patch.object(watch_mod.store, "list_connected_repos") as repos,
        ):
            await watch_mod._poll_once(_app(self.state))
        self.assertFalse(_effectively_trusted(self.slot))
        self.assertEqual(self.nudge.updates, [("nl_dis", {"active": False})])
        repos.assert_not_called()  # a disabled app reads no config

    async def test_enabling_again_restores_trust_and_the_loop(self):
        """Reversible: the revocation is in-memory and the record is untouched, so
        the next watchdog cycle for a still-live crew brings both back."""
        hook = self._register()
        with mock.patch.object(cr, "_autonudge_instance", return_value=self.nudge):
            await hook(cr.APP_NAME)
        self.assertFalse(_effectively_trusted(self.slot))

        crew = {"id": "c_d15ab1ed", "unattended": True, "enabled": True, "slot_key": self.slot.key}
        with mock.patch.object(cr, "_autonudge_instance", return_value=self.nudge):
            await cr.watchdog_cycle(self.state, OWNER, REPO, [crew])
        self.assertTrue(_effectively_trusted(self.slot))
        self.assertIn(("nl_dis", {"active": True}), self.nudge.updates)

    async def test_a_look_alike_chat_tab_keeps_its_own_loop(self):
        """Disabling this app must not touch a loop it does not own.

        The slot-key prefix is not a namespace this app owns: a person can name an
        ordinary chat tab ``crew-notes`` and arm their own monitoring loop on it.
        Matching on the prefix alone deactivated that loop and PERSISTED it
        inactive, so someone else's monitoring silently stopped because an
        unrelated app was switched off — and nothing in the tab explains why.

        The crew's own loop must still be deactivated in the same pass, or this
        test would also pass on a build that had simply stopped suspending crews.
        """
        mine = _FakeLoop("nl_mine", self.slot.key)
        # Valid prefix, suffix that the store's id grammar rejects — which is
        # exactly what a hand-named tab looks like.
        theirs = _FakeLoop("nl_theirs", "crew-notes")
        nudge = _FakeNudge([mine, theirs])

        hook = self._register()
        with mock.patch.object(cr, "_autonudge_instance", return_value=nudge):
            await hook(cr.APP_NAME)

        self.assertIn(("nl_mine", {"active": False}), nudge.updates, "the crew's loop survived")
        self.assertNotIn(
            "nl_theirs",
            [lid for lid, _ in nudge.updates],
            "an unrelated chat tab's monitoring loop was deactivated",
        )

    def test_a_gateway_without_the_registry_says_so_rather_than_pretending(self):
        """A security control that silently did not install is worse than one that
        is absent: the sweep is still the backstop, but the operator must be able to
        learn that "stop" means "within a minute" on this build.

        The absence is SIMULATED rather than read off the real module, so this keeps
        testing the degradation path after core lands the registry.
        """
        from kiro_crew.apps import teardown

        with mock.patch.object(teardown, "register_app_disable_hook", None, create=True):
            self.assertFalse(cr.install_app_disable_hook(self.state))


class TestGrantRenewal(unittest.TestCase):
    """Renewal is a SLIDE, not a re-activation, and that is a security property.

    The watchdog calls ``sync_trust`` every 60s. Re-activating on each of those
    would write a critical SEL entry per cycle — 1,440 a day per crew, burying the
    activation an auditor came for — and would reset ``activated_at``, so
    ``SafetyOverride``'s 24h ceiling could never be reached: the grant would be
    perpetual with an audit trail that merely looked busy.
    """

    def setUp(self):
        reset_singleton()
        self.addCleanup(reset_singleton)
        self.crew = {"id": "c_r", "unattended": True, "enabled": True}
        self.slot = _FakeSlot("crew-c_r")

    def test_repeated_cycles_activate_once_and_slide_thereafter(self):
        so = safety_override()
        with mock.patch.object(
            type(so), "activate_scoped", wraps=so.activate_scoped
        ) as activate, mock.patch.object(
            type(so), "renew_scoped", wraps=so.renew_scoped
        ) as renew:
            for _ in range(5):
                self.assertTrue(cr.sync_trust(self.slot, self.crew))
        self.assertEqual(activate.call_count, 1, "re-minted the grant on a live scope")
        self.assertEqual(renew.call_count, 4)

    def test_a_slide_carries_the_crew_ttl_and_not_the_six_hour_default(self):
        """``renew_scoped``'s default TTL is the 6h ad-hoc one. Letting it default
        would silently widen the grant to 6h on the very first watchdog cycle."""
        cr.sync_trust(self.slot, self.crew)
        so = safety_override()
        with mock.patch.object(type(so), "renew_scoped", wraps=so.renew_scoped) as renew:
            cr.sync_trust(self.slot, self.crew)
        self.assertEqual(renew.call_args.kwargs.get("ttl"), cr.TRUST_TTL_SECS)
        self.assertLessEqual(
            so.scope_remaining_secs(cr.autoapprove_scope("c_r")), cr.TRUST_TTL_SECS
        )

    def test_a_grant_at_its_ceiling_is_reminted_rather_than_left_to_lapse(self):
        """At the 24h ceiling the slide is refused. Minting a fresh grant re-audits
        the decision AND keeps a mid-turn crew from losing trust in the gap."""
        cr.sync_trust(self.slot, self.crew)
        so = safety_override()
        refused = type(so).renew_scoped(so, "nope", source="x")  # renewed=False shape
        self.assertFalse(refused.renewed)
        with mock.patch.object(
            type(so), "renew_scoped", return_value=refused
        ), mock.patch.object(
            type(so), "activate_scoped", wraps=so.activate_scoped
        ) as activate:
            self.assertTrue(cr.sync_trust(self.slot, self.crew))
        activate.assert_called_once()


class TestNoBackendTrustGrant(unittest.TestCase):
    """The load-bearing invariant, mirroring ``spec_builder``'s own source check.

    ``slot._trust`` is the grant a HUMAN makes. It never expires and nothing audits
    its activation, because the click is the record. A backend that stamps it
    manufactures an unbounded auto-approval out of nothing, which is why
    ``spec_builder`` was made to stop — and asserting on module SOURCE is what stops
    it coming back, since a behavioural test only covers the paths it happens to
    drive.

    Revoking is not granting: ``= False`` writes are fine and are what
    ``revoke_crew_execution`` and ``suspend_crews`` are for.
    """

    def test_crew_runtime_never_grants_slot_trust(self):
        src = inspect.getsource(cr)
        assert "slot._trust = True" not in src
        # And no revive-by-another-name: not via a variable, not via setattr, and
        # not via the ``slot._trust = want`` form this replaced — which is the one
        # that actually shipped, and which no ``= True`` search would have caught.
        for revived in (
            "_trust = True",
            "_trust = want",
            "_trust = bool",
            "_trust = grant",
            'setattr(slot, "_trust"',
            "setattr(slot, '_trust'",
        ):
            assert revived not in src, f"{revived} is a backend trust grant"
        # Every write to the flag in this module must be a revocation.
        writes = re.findall(r"\._trust\s*=\s*([^\n]+)", src)
        self.assertTrue(writes, "expected the revocation writes to still exist")
        for value in writes:
            self.assertEqual(
                value.strip(), "False", f"non-revoking write to _trust: {value!r}"
            )

    def test_the_grant_is_ttl_bounded_and_below_the_ceiling(self):
        """A scope with no TTL, or one at the 24h ceiling, would be the same
        unbounded grant wearing a scope key."""
        self.assertGreater(cr.TRUST_TTL_SECS, 0)
        self.assertLess(cr.TRUST_TTL_SECS, 86400)


class TestSharedApprovalPathIsUnchangedWithoutAScope(unittest.TestCase):
    """The scope check is STRICTLY ADDITIVE.

    Every ordinary chat session reaches the same code, so a slot that carries no
    scope key must take exactly the decision it took before this existed — in both
    directions. A change here is a change to approval semantics for every session.
    """

    class _Bare:
        """A slot from before ``_trust_scope`` existed: the attribute is absent."""

        def __init__(self, trust: bool) -> None:
            self._trust = trust

    def test_an_untrusted_slot_with_no_scope_key_is_untrusted(self):
        self.assertFalse(_effectively_trusted(self._Bare(False)))

    def test_a_trusted_slot_with_no_scope_key_is_trusted(self):
        self.assertTrue(_effectively_trusted(self._Bare(True)))

    def test_an_empty_scope_key_is_not_a_grant(self):
        slot = _FakeSlot("chat-1")
        self.assertEqual(slot._trust_scope, "")
        self.assertFalse(_effectively_trusted(slot))

    def test_no_scope_key_means_the_override_is_never_consulted(self):
        """Not just the same ANSWER — the same work. A live grant under some other
        key must not be reachable by a slot that names no scope."""
        with mock.patch.object(
            type(safety_override()), "is_scope_active", return_value=True
        ) as probe:
            self.assertFalse(_effectively_trusted(self._Bare(False)))
        probe.assert_not_called()

    def test_the_interactive_flag_still_short_circuits(self):
        """A human's grant needs no scope and must not depend on one being live."""
        slot = _FakeSlot("chat-1")
        slot._trust = True
        slot._trust_scope = "crew:c_x:autoapprove"  # not active
        self.assertTrue(_effectively_trusted(slot))


class TestAutoApproveProvenance(unittest.TestCase):
    """The SEL reason has to name WHICH grant approved a tool, or the audit trail
    cannot distinguish a human's session trust from a worker's expiring grant."""

    def test_yolo_outranks_everything(self):
        slot = _FakeSlot("chat-1")
        slot._trust_scope = "crew:c_x:autoapprove"
        self.assertEqual(chat_runner._auto_approve_reason(slot, True), "yolo")

    def test_a_scoped_grant_is_named_as_such(self):
        slot = _FakeSlot("crew-c_x")
        slot._trust_scope = "crew:c_x:autoapprove"
        self.assertEqual(chat_runner._auto_approve_reason(slot, False), "trust_scope")

    def test_session_trust_is_still_reported_as_trust(self):
        slot = _FakeSlot("chat-1")
        slot._trust = True
        self.assertEqual(chat_runner._auto_approve_reason(slot, False), "trust")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
