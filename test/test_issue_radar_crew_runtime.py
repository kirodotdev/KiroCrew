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

The bullets above span this module and its two siblings. This one pins seeding, brief
injection and its length guard, nudge composition, the provider vocabulary, the crew
ledger's provider scoping, the session launch and a pause racing the wake. The wake's
liveness guard, revocation and the trust grant are pinned in
``test_issue_radar_crew_runtime_trust.py``; turn dispatch, the triage model, unblock
detection (the first observation and the six signals), the sweep and dismissal in
``test_issue_radar_crew_runtime_sweep.py``; the shared fakes and autouse fixtures live in
``issue_radar_crew_runtime_helpers.py``.
"""

import ast
import asyncio
import contextlib
import inspect
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from issue_radar_crew_runtime_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    OWNER,
    REPO,
    _app_is_on,
    _crew,
    _effectively_trusted,
    _FakeLoop,
    _FakeNudge,
    _FakeSlot,
    _FakeState,
    _isolated_crew_log,
    _item,
    _private_sel_root_per_test,
    _runtime_modules,
    _unit_for,
)

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.apps.builtins.issue_radar.backend import provider
from kiro_crew.apps.builtins.issue_radar.backend import store as store_mod
from kiro_crew.safety_override import reset_singleton, safety_override


class TestSeedingSurvivesABriefLogLock:
    """``_item`` must not depend on the crew log's writer having let go yet."""

    def _hold_the_log_lock_during_read_back(self, monkeypatch) -> list[bool]:
        """Hold the unit's log lock from another thread across the read-back.

        The holder lets go on an EVENT, never on a timer, so a slow runner cannot
        turn the negative control into a pass or the positive case into a vacuous
        one. Every acquire of the log lock by the read-back's own thread first probes it
        once without waiting; that probe must be refused (recorded in the
        returned list), which proves the lock was really held when the waiter came.
        Off the loop the probe then releases the holder, and the real acquire waits
        it out. On the loop nothing releases it until the read-back has given up, so
        each on-loop attempt meets a held lock.
        """
        from kiro_crew import platform_lock_compat
        from kiro_crew.crew_log import store as log_store
        from kiro_crew.crew_log.schema import KIND_SESSION

        real_landed = cs._landed_since
        real_file_lock = log_store.file_lock
        holder: dict[str, Any] = {}
        released = threading.Event()
        probes: list[bool] = []

        def _identity(st: os.stat_result) -> tuple[int, int]:
            return (st.st_dev, st.st_ino)

        @contextlib.contextmanager
        def watched_file_lock(fd, *args, **kwargs):
            if (
                "identity" in holder
                and holder["waiter"] is threading.current_thread()
                and _identity(os.fstat(fd)) == holder["identity"]
            ):
                got = platform_lock_compat.try_acquire_lock(fd, exclusive=True)
                if got:
                    platform_lock_compat.release_lock(fd)
                probes.append(not got)
                if not platform_lock_compat._on_event_loop():
                    released.set()
            with real_file_lock(fd, *args, **kwargs):
                yield

        def contended(projection, session_id, *args, **kwargs):
            taken = threading.Event()
            # Only the read-back's own thread is the waiter under test: the crew log
            # also reads the unit on its own threads, and one of those must neither
            # release the holder nor count as a probe.
            holder["waiter"] = threading.current_thread()

            def hold() -> None:
                holder["thread"] = threading.current_thread()
                lock_path = log_store._lock_path(KIND_SESSION, session_id)
                with log_store._open_lock(lock_path):
                    holder["identity"] = _identity(os.stat(lock_path))
                    taken.set()
                    # A lost-run ceiling only: the release is an event.
                    released.wait(30)

            threading.Thread(target=hold, daemon=True).start()
            assert taken.wait(5), "the competing holder never took the lock"
            try:
                return real_landed(projection, session_id, *args, **kwargs)
            finally:
                released.set()

        monkeypatch.setattr(log_store, "file_lock", watched_file_lock)
        monkeypatch.setattr(cs, "_landed_since", contended)
        return probes

    def test_a_seed_from_an_async_test_waits_out_a_held_log_lock(self, tmp_path, monkeypatch):
        crew = _crew(tmp_path, unattended=True)
        _item(tmp_path, crew["id"], 2201, phase="awaiting-ci")
        probes = self._hold_the_log_lock_during_read_back(monkeypatch)

        async def from_the_loop() -> dict[str, Any]:
            return _item(tmp_path, crew["id"], 2201, phase="resolved")

        assert asyncio.run(from_the_loop())["phase"] == "resolved"
        assert probes and probes[0], f"the read-back never met the held lock: {probes}"

    def test_the_held_lock_is_real_contention(self, tmp_path, monkeypatch):
        """Negative control: the same write made ON the loop is refused.

        The setup write leaves the crew log's own background holders behind it --
        the writer thread landing the entry and the eager folder reading the unit
        right after -- and an on-loop acquire makes one attempt, so meeting either
        of them raises a bare ``OSError`` from the write's preparing fold before
        the read-back this control is about is ever reached. Both are settled
        first, so the only holder the on-loop write can meet is the planted one.
        """
        from kiro_crew.crew_log import eager as crew_log_eager
        from kiro_crew.crew_log import emit as crew_log_emit

        crew = _crew(tmp_path, unattended=True)
        _item(tmp_path, crew["id"], 2201, phase="awaiting-ci")
        assert crew_log_emit.flush(timeout=30), "the setup write never landed"
        assert crew_log_eager.drain(timeout=30), "the setup write's fold never settled"
        probes = self._hold_the_log_lock_during_read_back(monkeypatch)

        async def on_the_loop() -> None:
            cs.commit_work_progress(
                OWNER,
                REPO,
                crew["id"],
                2201,
                {"phase": "resolved"},
                "claim",
                "seeded",
                root=tmp_path,
                session_id=_unit_for(crew["id"]),
            )

        with pytest.raises(cs.CrewLedgerNotRecorded):
            asyncio.run(on_the_loop())
        assert probes and all(probes), f"an on-loop attempt found the lock free: {probes}"


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
        """A crew must not be told a concept the protocol does not have.

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


# ── provider vocabulary in the prompt ───────────────────────────────────────


class TestProviderVocabulary(unittest.TestCase):
    """The nudge and the Never block must speak the repo's forge, not GitHub's.

    ``provider.terms`` existed with no callers, so every crew on every provider was
    told GitHub's vocabulary. Two of those are not cosmetic:

      * ``#12`` and ``!12`` address DIFFERENT items on GitLab and Azure DevOps, so a
        crew that quotes the nudge back into a comment points at an unrelated item.
      * "Never merge a PR" is the prohibition a crew is most likely to reason
        around, and one phrased in a vocabulary its forge does not use reads as
        being about something else.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _nudge(self, provider_name: str) -> str:
        key = provider.key_from_parts(OWNER, REPO, provider_name, "gitlab.example")
        # Crew names are unique per repo, and these fixtures share one root.
        crew = _crew(self.root, name=f"Andromeda-{provider_name}-{len(self.root.name)}", labels=[])
        _item(self.root, crew["id"], 2201, phase="awaiting-ci", pr_number=88, next="round 3")
        return cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root, key))

    def test_github_rendering_is_unchanged(self):
        nudge = self._nudge("github")
        self.assertIn("(PR #88)", nudge)
        self.assertIn("merge a PR yourself", nudge)
        self.assertIn("every open issue", nudge)

    def test_gitlab_says_merge_request_with_its_own_sigil(self):
        nudge = self._nudge("gitlab")
        self.assertIn("(MR !88)", nudge)
        self.assertIn("merge a MR yourself", nudge)
        self.assertNotIn("(PR #88)", nudge)
        # The tracked item keeps `#` on every provider — only the change-request
        # sequence diverges — so the item's own number must NOT gain a `!`.
        self.assertIn("- #2201 awaiting-ci", nudge)

    def test_azure_says_work_item_and_uses_its_pull_request_sigil(self):
        nudge = self._nudge("azure")
        self.assertIn("(PR !88)", nudge)
        self.assertIn("every open work item", nudge)
        # Azure DevOps has no `issue` primitive, so naming one sends the crew
        # looking for a work item TYPE it was never told to filter on.
        self.assertNotIn("every open issue", nudge)
        self.assertIn("- #2201 awaiting-ci", nudge)

    def test_the_ci_clause_names_no_provider_path(self):
        """``.github/`` was wrong on two of three providers, and a per-provider path
        would still be wrong: a GitHub repo can be gated from outside ``.github/``
        and an Azure pipeline file can be named anything. The prohibition is about
        the files the gates run from, not a location."""
        for name in ("github", "gitlab", "azure"):
            nudge = self._nudge(name)
            self.assertIn("CI or gate configuration", nudge)
            self.assertNotIn(".github", nudge)
            self.assertNotIn("azure-pipelines", nudge)
            self.assertNotIn(".gitlab-ci", nudge)

    def test_an_unknown_or_absent_provider_falls_back_to_github(self):
        """A snapshot built before this field existed, or a corrupted record, must
        still render a complete prompt rather than raising mid-turn."""
        self.assertEqual(cr.vocabulary(), provider.terms(provider.RepoKey()))
        crew = _crew(self.root)
        snapshot = cr.build_snapshot(OWNER, REPO, crew, self.root)
        snapshot.pop("provider")
        self.assertIn("merge a PR yourself", cr.compose_nudge(snapshot))

    def test_the_snapshot_carries_the_provider(self):
        crew = _crew(self.root)
        key = provider.key_from_parts(OWNER, REPO, "azure")
        self.assertEqual(
            cr.build_snapshot(OWNER, REPO, crew, self.root, key)["provider"], "azure"
        )

    def test_every_prompt_path_forwards_the_key(self):
        """A path that drops ``key`` silently renders GitHub's vocabulary, which is
        indistinguishable from a correctly-rendered GitHub repo — so the seam is
        pinned by signature rather than by output."""
        for fn in (
            cr.build_snapshot,
            cr.compose_turn_prompt,
            cr.compose_turn_prompt_async,
            cr.launch_crew,
            cr.wake_crew,
            cr.watchdog_cycle,
        ):
            self.assertIn("key", inspect.signature(fn).parameters, fn.__name__)
        # The sweep is the only production caller, and it holds the real key.
        source = inspect.getsource(cr.sweep_repo)
        self.assertIn("watchdog_cycle(state, key.owner, key.repo, crews, scope, key)", source)
        self.assertRegex(source, r"wake_crew\(\s*state,\s*key\.owner,\s*key\.repo,[\s\S]*?\bkey,")


# ── provider scoping of the crew ledger ─────────────────────────────────────


class TestCrewStoreScoping(unittest.TestCase):
    """Every crew-ledger path is provider-separated ONLY by its ``root=``.

    ``crew_store.crews_dir(owner, repo, root)`` is the base of the crew records, the
    events log and the repo-wide shared skip index, and no signature in that module
    carries a provider or a host. So the separation is entirely a CALLER discipline:
    a call that forgets the scoped root writes one provider's skip decisions into
    another's index, and ``crew_store`` cannot detect it — on public GitHub the
    scoped root IS the base data dir (``store.provider_root`` returns it unchanged
    for the legacy layout), so "unscoped" and "correctly scoped for GitHub" are the
    same value.

    That is why the guard lives here, at the call sites, where the question is
    decidable, rather than inside the store where it is not.
    """

    #: ``crew_store`` members that are not per-repo and take no ``root``.
    _ROOTLESS = frozenset({"is_crew_id"})

    def _offenders(self, module: Any) -> list[str]:
        tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
        bad: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (
                isinstance(fn, ast.Attribute)
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "crew_store"
            ):
                continue
            if fn.attr in self._ROOTLESS:
                continue
            scoped = any(
                self._is_scope(arg) for arg in node.args
            ) or any(
                kw.arg in {"root", "scope"} for kw in node.keywords
            )
            if not scoped:
                bad.append(f"{fn.attr} at line {node.lineno}")
        return bad

    @staticmethod
    def _is_scope(arg: Any) -> bool:
        """Whether an argument is plausibly the provider-scoped root.

        A NAME (``root`` / ``scope``) or a call to the scope helper. Deliberately
        syntactic: what this gate can prove is that the scope was passed at all,
        which is the mistake with no symptom. Whether the name holds the right value
        is what ``routes._scope`` and ``store.provider_root`` are tested for.
        """
        if isinstance(arg, ast.Name):
            return arg.id in {"root", "scope"}
        if isinstance(arg, ast.Attribute):
            return arg.attr in {"root", "scope"}
        if isinstance(arg, ast.Call):
            fn = arg.func
            return isinstance(fn, ast.Attribute) and fn.attr in {"_scope", "provider_root"}
        return False

    def test_no_unscoped_crew_store_call_survives(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_routes

        for module in (crew_routes, *_runtime_modules()):
            offenders = self._offenders(module)
            self.assertEqual(
                offenders, [], f"unscoped crew_store calls in {module.__name__}: {offenders}"
            )

    def test_the_skip_index_is_separated_only_by_the_scoped_root(self):
        """The property the gate protects, demonstrated rather than asserted about.

        Two providers, same ``owner/repo`` — which is entirely ordinary, the same
        slug exists on github.com and on a self-managed GitLab. Scoped, their skip
        indexes are independent; handed the same root they are ONE index, and the
        second crew reads the first's decision as its own repository's.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            gh = store_mod.provider_root(root=base, provider="github", host="github.com")
            gl = store_mod.provider_root(root=base, provider="gitlab", host="gitlab.example")
            self.assertNotEqual(cs.crews_dir(OWNER, REPO, gh), cs.crews_dir(OWNER, REPO, gl))

            crew = _crew(gh)
            cs.commit_work_progress(
                OWNER, REPO, crew["id"], 7, {"phase": "skipped"}, "skip", "architecture call",
                skip_reason="architecture call", skip_scope="architecture",
                root=gh, session_id=_unit_for(crew["id"]),
            )
            # The index is a fold across the crews UNDER A ROOT, so the scoped roots
            # are independent indexes.
            self.assertEqual(cs.read_skips(OWNER, REPO, gl), {})
            self.assertIn("7", cs.read_skips(OWNER, REPO, gh))

            # The same call with the scope forgotten. It cannot raise and cannot be
            # detected downstream: for public GitHub the scoped root and the base
            # data dir are the same path.
            self.assertEqual(cs.crews_dir(OWNER, REPO, base), cs.crews_dir(OWNER, REPO, gh))


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
        # wrapper, not ``_run_chat`` itself — see :class:`TestTurnDispatch` in
        # ``test_issue_radar_crew_runtime_sweep.py``.
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


# ── a pause that lands while a crew is being woken ──────────────────────────


class TestPauseRacesTheWake(unittest.IsolatedAsyncioTestCase):
    """The operator pauses a crew DURING the wake that is about to run it.

    Every path here is handed a snapshot of the record, and a pause writes only to
    the store — so a liveness check made against the snapshot reads live for a crew
    that is already stopped, and the crew gets an auto-approve grant and one
    unattended turn after being told to stop. Each test below drives the pause into
    one specific ``await`` in the path, which is the only way to pin the window: the
    record on disk is paused and the caller's snapshot still says live.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        reset_singleton()
        self.addCleanup(reset_singleton)

    def _pause_mid_flight(self, crew_id: str, result: Any) -> Any:
        """An async stand-in that pauses *crew_id* and then returns *result*.

        The pause is taken through ``crew_store`` — the same call the pause route
        and the tab-close hook make — so the test reproduces the real interleaving
        rather than hand-editing the snapshot the code under test is holding.
        """

        async def _paused(*_a: Any, **_kw: Any) -> Any:
            cs.set_crew_paused(OWNER, REPO, crew_id, True, "operator paused", self.root)
            return result

        return _paused

    async def test_a_pause_during_the_rehydrate_gets_no_trust_and_no_turn(self):
        """The window the wake opens BEFORE it grants: the crew's slot is not
        resident, so the wake rehydrates it, and the pause lands in that await."""
        crew = _crew(self.root, unattended=True)
        slot_key = f"crew-{crew['id']}"
        revived = _FakeSlot(slot_key)
        state = _FakeState()  # no resident slot, as a closed tab leaves it
        with mock.patch.object(
            cr,
            "rehydrate_slot_from_history_async",
            new=self._pause_mid_flight(crew["id"], revived),
        ), mock.patch.object(cr, "_run_chat", mock.Mock()):
            started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertFalse(started)
        self.assertEqual(revived.prompts, [])  # and nothing was queued either
        self.assertFalse(_effectively_trusted(revived))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    async def test_a_pause_after_the_grant_takes_the_grant_back_and_runs_nothing(self):
        """The second window, which a single re-read would miss: composing the
        prompt and refreshing the loop message are both awaits AFTER the grant, so a
        pause landing there leaves a live grant on a stopped crew — and its armed
        loop can fire into it long before a watchdog cycle notices."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        self.assertTrue(_effectively_trusted(slot))
        real = cr.compose_turn_prompt_async

        async def _pause_then_compose(*a: Any, **kw: Any) -> str:
            cs.set_crew_paused(OWNER, REPO, crew["id"], True, "operator paused", self.root)
            return await real(*a, **kw)

        with mock.patch.object(
            cr, "compose_turn_prompt_async", new=_pause_then_compose
        ), mock.patch.object(cr, "_run_chat", mock.Mock()):
            started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertFalse(started)
        self.assertEqual(slot.prompts, [])
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    async def test_a_resume_between_the_reads_leaves_the_crew_TRUSTED_for_its_turn(self):
        """The mirror of the test above, and what reconciling rather than
        hand-checking buys: a crew paused when the wake started and resumed before it
        dispatched had its grant taken away by the first read, and the pre-dispatch
        reconciliation gives it back. Dispatching an unattended crew with no grant
        would park it on an approval prompt nobody is watching."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        paused = cs.set_crew_paused(
            OWNER, REPO, crew["id"], True, "operator paused", self.root
        )
        real = cr.compose_turn_prompt_async

        async def _resume_then_compose(*a: Any, **kw: Any) -> str:
            cs.set_crew_paused(OWNER, REPO, crew["id"], False, "", self.root)
            return await real(*a, **kw)

        with mock.patch.object(
            cr, "compose_turn_prompt_async", new=_resume_then_compose
        ), mock.patch.object(cr, "_run_chat", mock.Mock()):
            started = await cr.wake_crew(state, OWNER, REPO, paused, "ci-changed", self.root)
        self.assertTrue(started)
        self.assertEqual(len(slot.prompts), 1)
        self.assertTrue(_effectively_trusted(slot))

    async def test_a_pause_inside_the_grants_own_thread_hop_dispatches_nothing(self):
        """The residue every ORDERING leaves, and why the last read before the
        dispatch is taken on the event loop. ``sync_trust`` runs in a worker thread,
        so the loop is free while it runs: a pause committing there revokes, the
        worker then re-mints the grant from its pre-pause record, and a liveness
        check made before that hop reads live. Only a read with no suspension point
        before ``dispatch_crew_turn`` sees it."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        real = cr.sync_trust

        def _pause_then_sync(slot_arg: Any, crew_arg: dict[str, Any]) -> bool:
            # Runs on the worker thread, exactly where the real pause interleaves.
            cs.set_crew_paused(OWNER, REPO, crew["id"], True, "operator paused", self.root)
            return real(slot_arg, crew_arg)

        with mock.patch.object(cr, "sync_trust", _pause_then_sync), mock.patch.object(
            cr, "_run_chat", mock.Mock()
        ):
            started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertFalse(started)
        self.assertEqual(slot.prompts, [])
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    def _pause_and_revoke_inside_sync_trust(self, crew_id: str) -> Any:
        """A ``sync_trust`` stand-in that plays the pause route's whole move first.

        The route writes the record and then revokes execution BEFORE it answers, and
        both happen on the event loop while the real ``sync_trust`` runs on a worker
        thread. Reproducing the revoke as well as the write is what makes the
        resurrection observable: the grant is gone when the worker re-mints it.
        """
        real = cr.sync_trust

        def _paused(slot_arg: Any, crew_arg: dict[str, Any]) -> bool:
            cs.set_crew_paused(OWNER, REPO, crew_id, True, "operator paused", self.root)
            safety_override().deactivate_scope(cr.autoapprove_scope(crew_id))
            return real(slot_arg, crew_arg)

        return _paused

    async def test_a_pause_during_the_grant_does_not_survive_a_MID_TURN_wake(self):
        """The exit that skips the dispatch gate entirely. A busy crew's wake is
        dropped and returns early, so a grant re-minted from a pre-pause record
        outlives the operator's stop — and the in-flight turn keeps auto-approving
        its tools. Mid-turn is the normal state of a working crew, so this is the
        common case rather than a corner of one."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        slot.running = True
        with mock.patch.object(
            cr, "sync_trust", self._pause_and_revoke_inside_sync_trust(crew["id"])
        ), mock.patch.object(cr, "_run_chat", mock.Mock()):
            started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertFalse(started)
        self.assertEqual(slot.prompts, [])
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    async def test_a_pause_during_a_wake_that_RAISES_still_loses_the_grant(self):
        """The exit no per-return guard can ever cover, which is why the check sits in
        a ``finally``. The sweep catches a failed wake and moves on, so without this
        an exception mid-wake leaves a resurrected grant behind with nothing to
        report it."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)

        async def _raise(*_a: Any, **_kw: Any) -> str:
            raise RuntimeError("the forge went away mid-compose")

        # The pause (and its revocation) lands inside the grant's thread hop, so the
        # grant is genuinely resurrected — and THEN the wake fails, before any exit
        # that re-checks. Without the finally the resurrected grant is what remains.
        with mock.patch.object(
            cr, "sync_trust", self._pause_and_revoke_inside_sync_trust(crew["id"])
        ), mock.patch.object(
            cr, "compose_turn_prompt_async", new=_raise
        ), mock.patch.object(cr, "_run_chat", mock.Mock()):
            with self.assertRaises(RuntimeError):
                await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertEqual(slot.prompts, [])
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    async def test_turning_UNATTENDED_off_mid_wake_takes_the_grant_away(self):
        """The half of the grant's predicate a liveness check cannot see. ``is_live``
        reads enabled/retired/paused, so a crew whose ``unattended`` flag is switched
        off stays live — and the downgrade is exactly as much a governance decision as
        a pause. The wake must end with the grant the CURRENT record calls for, which
        means reconciling through ``sync_trust`` rather than checking liveness."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        self.assertTrue(_effectively_trusted(slot))
        real = cr.compose_turn_prompt_async

        async def _downgrade_then_compose(*a: Any, **kw: Any) -> str:
            # ONLY the record changes. Revoking here by hand would make the assertion
            # below true whatever the code did.
            cs.update_crew(OWNER, REPO, crew["id"], {"unattended": False}, self.root)
            return await real(*a, **kw)

        with mock.patch.object(
            cr, "compose_turn_prompt_async", new=_downgrade_then_compose
        ), mock.patch.object(cr, "_run_chat", mock.Mock()):
            started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        # The crew is still LIVE, so it legitimately gets its turn — attended.
        self.assertTrue(started)
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))
        self.assertEqual(slot._trust_scope, "")
        self.assertFalse(slot._trust)  # and never a fallback onto the unbounded flag

    async def test_turning_UNATTENDED_off_mid_cycle_takes_the_grant_away(self):
        """Same rule on the watchdog pass, whose trailing guard was liveness-only
        too."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        svc = _FakeNudge([_FakeLoop("nl_0", slot.key, active=False)])
        real_update = svc.update

        async def _downgrade_then_update(loop_id: str, **kw: Any) -> None:
            cs.update_crew(OWNER, REPO, crew["id"], {"unattended": False}, self.root)
            await real_update(loop_id, **kw)

        svc.update = _downgrade_then_update  # type: ignore[method-assign]
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertFalse(_effectively_trusted(slot))
        # Still live, so its clock stays on — only the grant went away.
        self.assertTrue(svc.get_by_slot(slot.key).active)

    def _stop_inside_the_grant(self, state: Any, crew: dict[str, Any], stop: Any) -> Any:
        """A ``sync_trust`` stand-in that runs *stop* on the EVENT LOOP mid-mint.

        The real ``sync_trust`` runs in a worker; the stop paths run on the loop.
        Reproducing that means the stop has to execute on the loop while the worker
        is inside the mint, so it is scheduled there with ``call_soon_threadsafe``
        and the worker waits for it before it mints from its (now stale) record.
        """
        loop = asyncio.get_running_loop()
        real = cr.sync_trust

        def _synced(slot_arg: Any, crew_arg: dict[str, Any]) -> bool:
            done = threading.Event()

            def _on_loop() -> None:
                try:
                    stop()
                finally:
                    done.set()

            loop.call_soon_threadsafe(_on_loop)
            done.wait(5)
            return real(slot_arg, crew_arg)

        return _synced

    async def test_a_pause_completing_inside_the_mint_still_wins(self):
        """THE resurrection. The pause route writes the record and revokes -- on the
        loop, while the worker is inside ``sync_trust`` holding a pre-pause record.
        The worker then mints. Without a revocation the mint could observe, that
        grant outlives the stop and the crew's next approval is automatic."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)

        def _pause() -> None:
            # Exactly what the route does before it answers: write, then revoke.
            cs.set_crew_paused(OWNER, REPO, crew["id"], True, "operator paused", self.root)
            asyncio.get_running_loop().create_task(
                cr.revoke_crew_execution(state, crew, "paused")
            )

        with mock.patch.object(
            cr, "sync_trust", self._stop_inside_the_grant(state, crew, _pause)
        ), mock.patch.object(cr, "_run_chat", mock.Mock()):
            started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertFalse(started)
        self.assertEqual(slot.prompts, [])
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    async def test_an_APP_DISABLE_completing_inside_the_mint_still_wins(self):
        """The half the crew record cannot express. ``revoke_crew_grants`` runs
        synchronously on the loop and reads no record, so a re-read cannot see it --
        only the generation can."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        slot._app = cr.APP_NAME
        state._slots = {slot.key: slot}

        with mock.patch.object(
            cr,
            "sync_trust",
            self._stop_inside_the_grant(state, crew, lambda: cr.revoke_crew_grants(state)),
        ), mock.patch.object(cr, "_run_chat", mock.Mock()):
            started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertFalse(started)
        self.assertEqual(slot.prompts, [])
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    async def test_the_watchdog_honours_a_stop_that_lands_inside_its_own_mint(self):
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        slot._app = cr.APP_NAME
        state._slots = {slot.key: slot}
        svc = _FakeNudge([_FakeLoop("nl_0", slot.key, active=True)])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc), mock.patch.object(
            cr,
            "sync_trust",
            self._stop_inside_the_grant(state, crew, lambda: cr.revoke_crew_grants(state)),
        ):
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    async def test_an_app_disable_that_COMPLETED_before_the_wake_never_regrants(self):
        """The disable finished, the record still says unattended and live, and the
        counter has already absorbed the bump -- so neither the record nor the
        generation can see it. Only the gate can, and the gate is what the grant now
        consults. Without it the wake re-minted a grant on a switched-off app."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        slot._app = cr.APP_NAME
        state._slots = {slot.key: slot}
        cr.revoke_crew_grants(state)  # the disable, in full, BEFORE the wake
        self.assertFalse(_effectively_trusted(slot))
        with mock.patch.object(cr, "is_app_enabled", return_value=False), mock.patch.object(
            cr, "_run_chat", mock.Mock()
        ):
            started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertFalse(started)
        self.assertEqual(slot.prompts, [])
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    async def test_an_app_disable_landing_INSIDE_the_hop_is_seen_by_the_generation(self):
        """The gate read ON in the hop (the disable had not landed yet), and no file
        is re-read on the loop afterwards -- so the only thing that can see the
        disable is the generation ``revoke_crew_grants`` bumped for this crew."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        slot._app = cr.APP_NAME
        state._slots = {slot.key: slot}
        with mock.patch.object(
            cr,
            "sync_trust",
            self._stop_inside_the_grant(state, crew, lambda: cr.revoke_crew_grants(state)),
        ):
            self.assertFalse(await cr._reconcile_trust(state, slot, crew))
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    async def test_the_watchdog_reconciles_a_session_it_just_CREATED(self):
        """A crew with a loop but no resident slot and nothing to rehydrate has its
        session created inside the pass. The trailing reconciliation must reach that
        session: a stop landing during the pass otherwise leaves the one grant the
        pass minted standing, because the local ``slot`` still reads None."""
        crew = _crew(self.root, unattended=True)
        slot_key = f"crew-{crew['id']}"
        state = _FakeState()
        svc = _FakeNudge([_FakeLoop("nl_0", slot_key, active=True)])
        real_ensure = cr.ensure_crew_session

        async def _create_then_downgrade(*a: Any, **kw: Any) -> Any:
            created = await real_ensure(*a, **kw)
            # The stop lands after the session (and its grant) exist.
            cs.update_crew(OWNER, REPO, crew["id"], {"unattended": False}, self.root)
            return created

        with mock.patch.object(cr, "_autonudge_instance", lambda: svc), mock.patch.object(
            cr, "rehydrate_slot_from_history_async", new=mock.AsyncMock(return_value=None)
        ), mock.patch.object(cr, "ensure_crew_session", _create_then_downgrade):
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertIn(slot_key, state.slots)
        self.assertFalse(_effectively_trusted(state.slots[slot_key]))

    async def test_another_crews_stop_does_not_de_trust_this_one(self):
        """The generation is per crew. A process-wide counter made crew B's pause
        tear down crew A's freshly-minted grant -- fail-closed, but it parked a live
        unattended crew on an approval prompt for a whole cycle."""
        a = _crew(self.root, name="Andromeda", unattended=True)
        b = _crew(self.root, name="Draco", unattended=True)
        state = _FakeState()
        slot_a = await cr.ensure_crew_session(state, OWNER, REPO, a)
        await cr.ensure_crew_session(state, OWNER, REPO, b)

        def _pause_b() -> None:
            cs.set_crew_paused(OWNER, REPO, b["id"], True, "operator paused", self.root)
            asyncio.get_running_loop().create_task(cr.revoke_crew_execution(state, b, "paused"))

        with mock.patch.object(cr, "sync_trust", self._stop_inside_the_grant(state, a, _pause_b)):
            self.assertTrue(await cr._reconcile_trust(state, slot_a, a))
        self.assertTrue(_effectively_trusted(slot_a))

    async def test_a_disable_whose_flag_is_not_yet_written_still_denies(self):
        """The window between the two other sources. The disable HOOK has run --
        grants cleared, generation bumped -- but ``installed.json`` is not written
        yet, so the gate still reads on. A reconciliation starting HERE samples the
        already-bumped generation and an enabled gate, and without the latch it
        re-mints a grant on an app whose disable is in flight."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        slot._app = cr.APP_NAME
        state._slots = {slot.key: slot}
        cr.revoke_crew_grants(state)  # the hook's synchronous half; gate still True
        self.assertFalse(_effectively_trusted(slot))
        with mock.patch.object(cr, "_run_chat", mock.Mock()):
            started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertFalse(started)
        self.assertEqual(slot.prompts, [])
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    async def test_re_enabling_the_app_releases_the_latch_through_the_watchdog(self):
        """One-way until the store agrees: only a watchdog pass -- which the sweep
        runs solely while the app reads enabled -- clears it, and the crew is then
        trusted again."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        slot._app = cr.APP_NAME
        state._slots = {slot.key: slot}
        cr.revoke_crew_grants(state)
        self.assertFalse(await cr._reconcile_trust(state, slot, crew))  # latched
        await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)  # re-enabled
        self.assertTrue(_effectively_trusted(slot))

    async def test_a_quiet_reconciliation_keeps_the_grant(self):
        """The generation check must not cost a grant when nothing revoked."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        self.assertTrue(await cr._reconcile_trust(state, slot, crew))
        self.assertTrue(_effectively_trusted(slot))

    async def test_the_watchdog_neither_trusts_nor_re_arms_a_crew_paused_mid_cycle(self):
        """The same window in the cycle that is supposed to be the RECOVERY for it.
        Its roster is read before the pass, and its own rehydrate is an await — so a
        pause landing there is granted trust and has its loop switched back on,
        which hands the crew a turn on its next idle fire."""
        crew = _crew(self.root, unattended=True)
        slot_key = f"crew-{crew['id']}"
        revived = _FakeSlot(slot_key)
        svc = _FakeNudge([_FakeLoop("nl_0", slot_key, active=False)])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc), mock.patch.object(
            cr,
            "rehydrate_slot_from_history_async",
            new=self._pause_mid_flight(crew["id"], revived),
        ):
            await cr.watchdog_cycle(_FakeState(), OWNER, REPO, [crew], self.root)
        self.assertFalse(_effectively_trusted(revived))
        self.assertFalse(svc.get_by_slot(slot_key).active)
        self.assertEqual(svc.added, [])  # and no new loop was armed for it

    async def test_the_watchdog_does_not_regrant_a_RESIDENT_crew_paused_since_the_roster_read(self):
        """The window has nothing to do with rehydration: a crew whose slot the
        gateway still holds skips that branch entirely, and its snapshot is exactly
        as stale — the roster was read before the pass and every crew ahead of it in
        the loop awaited. So the re-read cannot be conditional on rehydrating."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)  # resident
        svc = _FakeNudge([_FakeLoop("nl_0", slot.key, active=False)])
        # The pause lands after the roster was read: the store says stopped while
        # the snapshot the cycle is holding still says live.
        cs.set_crew_paused(OWNER, REPO, crew["id"], True, "operator paused", self.root)
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(svc.get_by_slot(slot.key).active)

    async def test_the_watchdog_undoes_its_own_pass_when_the_pause_lands_inside_it(self):
        """The residue a single early re-read leaves: the pass itself awaits — the
        grant, the launch, the re-arm — so a pause landing between them is still
        answered by trusting the crew and switching its clock on. The trailing read
        makes the cycle undo its own work, which is what makes the guard total rather
        than merely earlier."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        svc = _FakeNudge([_FakeLoop("nl_0", slot.key, active=False)])
        real_update = svc.update

        async def _pause_then_update(loop_id: str, **kw: Any) -> None:
            # The pause lands while the cycle is re-arming the crew's clock.
            cs.set_crew_paused(OWNER, REPO, crew["id"], True, "operator paused", self.root)
            await real_update(loop_id, **kw)

        svc.update = _pause_then_update  # type: ignore[method-assign]
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(svc.get_by_slot(slot.key).active)

    async def test_a_live_crew_is_still_woken_when_no_pause_lands(self):
        """The re-reads must not cost a wake. Same path, same rehydrate, nothing
        paused — the crew is trusted and gets its turn."""
        crew = _crew(self.root, unattended=True)
        slot_key = f"crew-{crew['id']}"
        revived = _FakeSlot(slot_key)
        with mock.patch.object(
            cr, "rehydrate_slot_from_history_async", new=mock.AsyncMock(return_value=revived)
        ), mock.patch.object(cr, "_run_chat", mock.Mock()):
            started = await cr.wake_crew(
                _FakeState(), OWNER, REPO, crew, "ci-changed", self.root
            )
        self.assertTrue(started)
        self.assertEqual(len(revived.prompts), 1)
        self.assertTrue(_effectively_trusted(revived))

    async def test_a_record_that_cannot_be_read_FAILS_CLOSED(self):
        """Liveness is what authorizes the grant, so a record nothing can read is not
        permission to keep granting from a snapshot. Falling back to the caller's
        snapshot would answer the governance question with the very value whose
        staleness is in question — a deleted or corrupt record would go on authorizing
        unattended turns forever."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        self.assertTrue(_effectively_trusted(slot))
        cs.crew_path(OWNER, REPO, crew["id"], self.root).unlink()
        with mock.patch.object(cr, "_run_chat", mock.Mock()):
            started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertFalse(started)
        self.assertEqual(slot.prompts, [])
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(safety_override().is_scope_active(cr.autoapprove_scope(crew["id"])))

    async def test_a_corrupt_record_fails_closed_in_the_watchdog_too(self):
        """Same rule on the cycle that re-establishes trust: unreadable is stopped,
        so a crew whose record went bad does not keep its grant and its clock."""
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        svc = _FakeNudge([_FakeLoop("nl_0", slot.key, active=True)])
        cs.crew_path(OWNER, REPO, crew["id"], self.root).write_text("{not json")
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertFalse(_effectively_trusted(slot))
        self.assertFalse(svc.get_by_slot(slot.key).active)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
