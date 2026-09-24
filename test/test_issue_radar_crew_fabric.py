"""Tests for Crew Fabric — the server-side fold (``crew_store.fold_fabric``) and
its route (``crew_routes._handle_crew_fabric``, ``GET /crew/fabric``).

The fold turns a crew's append-only ledger into one lane per work item across the
phase enum. Three mistakes a naive fold makes are each pinned here, and each has a
MUTATION-VERIFIED assertion recorded in the PR write-up (break the code, watch the
test go red, restore):

  * **The live phase is the record's, authoritative — never the max timeline
    index.** A review can send an item LEFT of where it has been, so keying the head
    off the furthest column reached puts the item in a phase it already left.
  * **Off-spine phases are an ``exit``, not a timeline entry**, and the exit stands
    only when the item's live phase is itself off-spine (a reopen clears it).
  * **Re-entering a phase after an exit is a reopen**, counted, and each restarts
    the dwell clock.

Plus the two conditions whose failure is otherwise silent: a work item CARRIED from a
pre-projection ledger (whose history has no phase-bearing lines) must fold rather
than crash, and the route must answer a non-GitHub provider / an empty repo with
``items: []`` at HTTP 200.

The fold tests drive the store directly against a ``tmp_path`` root -- every store
function threads ``root`` for exactly this reason -- and against an isolated crew
log data home, since a crew's ledger is the ``radar`` fold of its own crew log: each
crew these tests create gets the unit its slot runs on (:func:`_crew`), and every
write goes through ``commit_work_progress`` with that unit as ``session_id``, exactly
as the write route does. The route tests look the handler up out of a real
``web.Application`` (so registration and the gates are proven too) and isolate all
data behind one ``routes._scope`` patch, mirroring ``test_issue_radar_crew_routes``.
"""

import itertools
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import (
    crew_routes,
    crew_store,
    routes,
    store,
)
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.store import CrewLog

OWNER, REPO = "kirodotdev", "KiroCrew"  # brand-ok: the repository name
BASE = "/api/apps/issue-radar"


# ── fold fixtures ────────────────────────────────────────────────────────────

#: crew id -> the live crew log unit its slot runs on, for the crews a test made.
_UNITS: dict[str, str] = {}
_SEQ = itertools.count(1)


def _isolate_crew_log(case: unittest.TestCase) -> None:
    """Own crew log data home, crew log on, writer state reset around *case*."""
    home = tempfile.TemporaryDirectory()
    case.addCleanup(home.cleanup)
    env = mock.patch.dict(os.environ, {"KIROCREW_HOME": home.name, crew_log_emit.CREW_LOG_ENV: "1"})
    env.start()
    case.addCleanup(env.stop)
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()
    _UNITS.clear()

    def _teardown() -> None:
        crew_log_emit.drain_for_shutdown(timeout=2.0)
        crew_log_emit.reset_caches()
        crew_log.forget_slot_folds()
        _UNITS.clear()

    case.addCleanup(_teardown)


def _crew(root: Path, name: str = "Andromeda") -> dict:
    """A crew plus the crew log unit its slot runs on, so it can record."""
    crew = crew_store.create_crew(OWNER, REPO, {"name": name}, root)
    sid = f"acp-{next(_SEQ)}"
    CrewLog.create(
        KIND_SESSION, sid, owner="owner", agent="kirocrew", slot=crew_store.slot_key_for(crew["id"])
    )
    _UNITS[crew["id"]] = sid
    return crew


def _tick() -> None:
    """Let the writer's millisecond clock move between two stamps."""
    time.sleep(0.004)


def _work(root: Path, crew_id: str, number: int, patch: dict) -> dict:
    """Drive one update through the real write path so the entry carries the phase
    exactly as production writes it -- no hand-built event dicts."""
    return crew_store.commit_work_progress(
        OWNER,
        REPO,
        crew_id,
        number,
        {k: v for k, v in patch.items() if not k.startswith("_")},
        patch.get("_event_kind", "claim"),
        patch.get("_event_text", "step"),
        skip_reason=patch.get("_skip_reason"),
        skip_scope=patch.get("_skip_scope", ""),
        root=root,
        session_id=_UNITS[crew_id],
    )


def _step(root: Path, crew_id: str, number: int, phase: str, kind: str, text: str, **extra) -> dict:
    patch = {"phase": phase, "_event_kind": kind, "_event_text": text, **extra}
    return _work(root, crew_id, number, patch)


def _fold_item(root: Path, number: int) -> dict:
    items = crew_store.fold_fabric(OWNER, REPO, root)
    match = [it for it in items if it["number"] == number]
    assert len(match) == 1, f"expected exactly one item #{number}, got {match}"
    return match[0]


def _bulk_no_move_lines(
    root: Path, crew_id: str, number: int, count: int, *, tag: str, kind: str = "ci"
) -> None:
    """Append *count* no-move entries to the crew's log through the real writer.

    Each entry is shaped exactly as ``commit_work_progress`` writes for a CI round
    that does NOT move the item: a ``radar/recorded`` entry with a ``ci_state`` delta
    and no ``phase``. The volume the fold reads past is the entry COUNT, not the
    write PATH -- the property under test is that an item's phase-entry history is
    kept per item by the fold, not derived from the bounded event tail these lines
    fill -- so the entries are queued straight onto the writer and drained once,
    instead of *count* round trips through the store's refusal checks.

    ``text`` carries *tag* plus the index so every line id is distinct -- a
    duplicated id would collapse on read and undercount the volume.
    """
    sid = _UNITS[crew_id]
    for i in range(count):
        crew_log_emit.on_radar_recorded(
            sid,
            {
                "crew_id": crew_id,
                "owner": OWNER,
                "repo": REPO,
                "number": int(number),
                "ci_state": {"round": i},
                "event": f"{tag}-{i}",
                "event_kind": kind,
            },
        )
    assert crew_log_emit.flush(timeout=30.0), "the crew log writer did not drain the filler"


class FoldTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        _isolate_crew_log(self)
        self.crew = _crew(self.root)
        self.cid = self.crew["id"]

    # ── empty ────────────────────────────────────────────────────────────────

    def test_empty_repo_folds_to_no_items(self):
        # A crew exists but has taken nothing.
        self.assertEqual(crew_store.fold_fabric(OWNER, REPO, self.root), [])

    def test_no_crews_at_all_folds_to_no_items(self):
        # TemporaryDirectory rather than mkdtemp + rmtree(ignore_errors=True): the
        # suppressed form hides a failure to remove the tree, so a leak survives the
        # run silently. This raises instead, which is what a cleanup fault should do.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(crew_store.fold_fabric(OWNER, REPO, Path(tmp.name)), [])

    # ── single item ────────────────────────────────────────────────────────

    def test_single_item_linear_progression(self):
        _step(self.root, self.cid, 5109, "claimed", "claim", "claimed it")
        _step(self.root, self.cid, 5109, "implementing", "implement", "cut worktree")
        _step(self.root, self.cid, 5109, "awaiting-ci", "ci", "PR opened", pr_number=5144)
        _step(self.root, self.cid, 5109, "awaiting-merge", "merge", "61/61 green")
        item = _fold_item(self.root, 5109)

        self.assertEqual(item["phase"], "awaiting-merge")  # live, from the record
        self.assertEqual(item["crew_id"], self.cid)
        self.assertEqual(item["pr_number"], 5144)
        self.assertIsNone(item["exit"])
        self.assertEqual(item["reopens"], 0)
        self.assertEqual(
            [t["phase"] for t in item["timeline"]],
            ["claimed", "implementing", "awaiting-ci", "awaiting-merge"],
        )
        # timeline is in TIME order — non-decreasing timestamps.
        ats = [t["at"] for t in item["timeline"]]
        self.assertEqual(ats, sorted(ats))

    def test_next_surfaces_from_the_record_and_ci_state_is_not_shipped(self):
        _step(
            self.root,
            self.cid,
            5109,
            "claimed",
            "claim",
            "claimed",
            next="add the Windows branch to _safe_chmod",
        )
        _step(
            self.root,
            self.cid,
            5109,
            "awaiting-ci",
            "ci",
            "checks",
            ci_state={"state": "success", "passed": 61, "total": 61},
        )
        item = _fold_item(self.root, 5109)
        # `next` is the crew's resumable INTENT and surfaces under its OWN name —
        # never as the title (defect: item 4).
        self.assertEqual(item["next"], "add the Windows branch to _safe_chmod")
        # `ci_state` is deliberately NOT in the payload even though the record holds
        # it: nothing reads it, and it was the only field forwarding an arbitrary
        # nested dict, where one non-finite float serializes as bare `NaN` and makes
        # the whole response unparseable for the browser.
        self.assertNotIn("ci_state", item)

    def test_next_is_not_used_as_the_title(self):
        # No cached issue/PR title exists for this number, and the crew recorded a
        # `next`. The OLD code showed `next` as the title; the fold must NOT — the
        # title falls back to empty, and `next` stays under its own name.
        _step(
            self.root,
            self.cid,
            5109,
            "claimed",
            "claim",
            "claimed",
            next="rebase onto main and re-run the Windows shard",
        )
        item = _fold_item(self.root, 5109)
        self.assertEqual(item["title"], "")
        self.assertEqual(item["next"], "rebase onto main and re-run the Windows shard")

    def test_title_comes_from_the_issues_list_cache(self):
        # Seed the issues list cache the way Issue Radar keeps it, then the fold
        # joins the REAL title onto the lane at zero extra API cost (defect item 4,
        # mirroring how the dependency graph seeds number -> title).
        store.write_issues_cache(
            OWNER,
            REPO,
            [{"number": 5109, "title": "pr_status rollup degrades to red on a torn cache"}],
            root=self.root,
            state="open",
        )
        _step(self.root, self.cid, 5109, "claimed", "claim", "claimed", next="some intent")
        item = _fold_item(self.root, 5109)
        self.assertEqual(item["title"], "pr_status rollup degrades to red on a torn cache")
        # …and `next` is still its own field, untouched by the title join.
        self.assertEqual(item["next"], "some intent")

    def test_title_comes_from_the_pulls_cache_and_wins_over_the_issue(self):
        # A work item that became a PR is keyed by its ISSUE number; the PR title is
        # the more specific label and wins on a collision.
        store.write_issues_cache(
            OWNER,
            REPO,
            [{"number": 5109, "title": "the issue title"}],
            root=self.root,
            state="open",
        )
        store.write_pulls_cache(
            OWNER,
            REPO,
            [{"number": 5109, "title": "the PR title"}],
            root=self.root,
            state="open",
        )
        _step(self.root, self.cid, 5109, "awaiting-ci", "ci", "PR opened", pr_number=5144)
        item = _fold_item(self.root, 5109)
        self.assertEqual(item["title"], "the PR title")

    def test_title_reads_the_closed_caches_too(self):
        # A finished lane's issue/PR is closed, so the OPEN cache alone would drop
        # exactly the resolved lanes — the fold must read the closed caches too.
        store.write_pulls_cache(
            OWNER,
            REPO,
            [{"number": 5109, "title": "a merged pull request"}],
            root=self.root,
            state="closed",
        )
        _step(self.root, self.cid, 5109, "resolved", "merge", "merged", pr_number=5144)
        item = _fold_item(self.root, 5109)
        self.assertEqual(item["title"], "a merged pull request")

    def test_title_join_mutation_a_number_without_a_hint_is_empty(self):
        # MUTATION-VERIFIED companion: a number the caches never saw must fold to an
        # empty title, NOT to the crew's `next`. If the join regressed to
        # `record.get("next")` this would come back non-empty.
        store.write_issues_cache(
            OWNER,
            REPO,
            [{"number": 9999, "title": "an unrelated issue"}],
            root=self.root,
            state="open",
        )
        _step(self.root, self.cid, 5109, "claimed", "claim", "claimed", next="not a title")
        item = _fold_item(self.root, 5109)
        self.assertEqual(item["title"], "")

    # ── round-trip (the head-marker bug) ─────────────────────────────────────

    def test_review_round_trip_head_is_last_in_time_not_furthest_reached(self):
        # awaiting-ci -> addressing-review -> awaiting-ci. The item ends in
        # awaiting-ci, which is LEFT of addressing-review on the spine.
        _step(self.root, self.cid, 5071, "claimed", "claim", "claimed")
        _step(self.root, self.cid, 5071, "implementing", "implement", "edit")
        _step(self.root, self.cid, 5071, "awaiting-ci", "ci", "PR opened, round 1")
        _step(self.root, self.cid, 5071, "addressing-review", "review", "2 blocking findings")
        _step(self.root, self.cid, 5071, "awaiting-ci", "ci", "pushed a fix, round 2")
        item = _fold_item(self.root, 5071)

        # The whole point: live phase is awaiting-ci, NOT the furthest-right
        # addressing-review the item passed through.
        self.assertEqual(item["phase"], "awaiting-ci")
        spine = crew_store.SPINE_PHASES
        max_reached = max(spine.index(t["phase"]) for t in item["timeline"])
        self.assertEqual(spine[max_reached], "addressing-review")
        self.assertNotEqual(item["phase"], spine[max_reached])
        # The timeline REPEATS awaiting-ci — that is the round-trip, preserved.
        self.assertEqual(
            [t["phase"] for t in item["timeline"]],
            ["claimed", "implementing", "awaiting-ci", "addressing-review", "awaiting-ci"],
        )
        # A round-trip inside the spine (no exit stood) is NOT a reopen.
        self.assertEqual(item["reopens"], 0)
        self.assertIsNone(item["exit"])

    # ── reopen-after-exit ────────────────────────────────────────────────────

    def test_reopen_after_exit_clears_the_exit_and_counts_the_reopen(self):
        # claimed -> implementing -> yielded (off-spine) -> implementing (reopen)
        # -> awaiting-ci. The store clears outcome/finished_at on the reopen.
        _step(self.root, self.cid, 5120, "claimed", "claim", "claimed")
        _step(self.root, self.cid, 5120, "implementing", "implement", "edit")
        _step(self.root, self.cid, 5120, "yielded", "yield", "dependency-blocked")
        _step(self.root, self.cid, 5120, "implementing", "implement", "reopened")
        _step(self.root, self.cid, 5120, "awaiting-ci", "ci", "PR opened")
        item = _fold_item(self.root, 5120)

        self.assertEqual(item["phase"], "awaiting-ci")  # on-spine now
        self.assertIsNone(item["exit"])  # the yield does not hold
        self.assertEqual(item["reopens"], 1)  # implementing re-entered after the exit
        # yielded is off-spine, so it never appears in the timeline spine.
        self.assertNotIn("yielded", [t["phase"] for t in item["timeline"]])

    def test_two_reopens_are_both_counted(self):
        _step(self.root, self.cid, 5120, "claimed", "claim", "claimed")
        _step(self.root, self.cid, 5120, "implementing", "implement", "edit")
        _step(self.root, self.cid, 5120, "yielded", "yield", "blocked")
        _step(self.root, self.cid, 5120, "implementing", "implement", "reopened")
        _step(self.root, self.cid, 5120, "yielded", "yield", "sibling holds the slot")
        _step(self.root, self.cid, 5120, "implementing", "implement", "reopened again")
        item = _fold_item(self.root, 5120)
        self.assertEqual(item["phase"], "implementing")
        self.assertIsNone(item["exit"])
        self.assertEqual(item["reopens"], 2)

    def test_item_ending_off_spine_keeps_its_exit(self):
        # claimed -> investigating -> awaiting-reply (off-spine, and where it ends).
        _step(self.root, self.cid, 4997, "claimed", "claim", "claimed")
        _step(self.root, self.cid, 4997, "investigating", "investigate", "reading")
        _step(self.root, self.cid, 4997, "awaiting-reply", "reply", "asked the maintainer")
        item = _fold_item(self.root, 4997)

        self.assertEqual(item["phase"], "awaiting-reply")
        self.assertIsNotNone(item["exit"])
        self.assertEqual(item["exit"]["phase"], "awaiting-reply")
        # awaiting-reply is off-spine, so the spine timeline holds only the two
        # on-spine phases.
        self.assertEqual([t["phase"] for t in item["timeline"]], ["claimed", "investigating"])

    def test_skipped_item_exits_and_is_absent_from_the_spine(self):
        _step(self.root, self.cid, 3664, "claimed", "claim", "claimed")
        _step(self.root, self.cid, 3664, "skipped", "skip", "duplicate — open PR exists")
        item = _fold_item(self.root, 3664)
        self.assertEqual(item["phase"], "skipped")
        self.assertIsNotNone(item["exit"])
        self.assertEqual(item["exit"]["phase"], "skipped")
        self.assertEqual([t["phase"] for t in item["timeline"]], ["claimed"])

    # ── a work item carried from the pre-projection files ────────────────────

    def _carried(self, number: int, phase: str) -> None:
        """Plant a pre-projection item file; the crew's next write carries it."""
        path = crew_store.work_item_path(OWNER, REPO, self.cid, number, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "crew_id": self.cid,
                    "owner": OWNER,
                    "repo": REPO,
                    "number": number,
                    "phase": phase,
                    "tried": [],
                    "claimed_at": "2026-01-01T00:00:00Z",
                    "last_progress_at": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )

    def test_a_carried_item_with_no_phase_history_is_tolerated(self):
        # A pre-projection ledger had a live phase on the record but no per-item
        # phase history; the carry re-states the record, so the lane opens at its
        # live phase, stamped at the record's OWN last progress, and the fold does
        # not crash on a history one line long.
        self._carried(5109, "awaiting-ci")
        _step(self.root, self.cid, 7, "claimed", "claim", "the write that carries")
        item = _fold_item(self.root, 5109)

        self.assertEqual(item["phase"], "awaiting-ci")  # still authoritative
        self.assertEqual(item["timeline"], [{"phase": "awaiting-ci", "at": "2026-01-01T00:00:00Z"}])
        self.assertIsNone(item["exit"])
        self.assertEqual(item["reopens"], 0)

    def test_a_carried_item_then_real_writes_fold_both(self):
        # A carried record, then real writes on top of it.
        self._carried(5109, "selected")
        _step(self.root, self.cid, 5109, "implementing", "implement", "edit")
        item = _fold_item(self.root, 5109)
        self.assertEqual([t["phase"] for t in item["timeline"]], ["selected", "implementing"])
        self.assertEqual(item["phase"], "implementing")
        # The carry happened once: the file is marked and its bytes untouched.
        marker = (
            crew_store.work_item_path(OWNER, REPO, self.cid, 5109, self.root).parent / ".carried"
        )
        self.assertTrue(marker.is_file())

    # ── the store change itself ──────────────────────────────────────────────

    def test_event_line_records_phase_after_the_write(self):
        committed = _step(self.root, self.cid, 5109, "implementing", "implement", "edit")
        self.assertEqual(committed["event"]["phase"], "implementing")
        # And it is durable on the ledger line, not just in the return value.
        events = crew_store.read_events(OWNER, REPO, self.root, crew_id=self.cid)
        self.assertEqual(events[0]["phase"], "implementing")

    # ── ordering across items ────────────────────────────────────────────────

    def test_items_are_newest_progress_first(self):
        # Stamps come off the crew log's own clock, so distinct progress timestamps
        # are established by letting that clock move between writes.
        _step(self.root, self.cid, 100, "claimed", "claim", "older")
        _tick()
        _step(self.root, self.cid, 200, "claimed", "claim", "newer")
        numbers = [it["number"] for it in crew_store.fold_fabric(OWNER, REPO, self.root)]
        self.assertEqual(numbers[0], 200)
        self.assertIn(100, numbers)

        # Progress on the older item must outrank creation time on the newer item.
        _tick()
        _step(self.root, self.cid, 100, "implementing", "implement", "resumed work")
        numbers = [it["number"] for it in crew_store.fold_fabric(OWNER, REPO, self.root)]
        self.assertEqual(numbers, [100, 200])


# ── route ────────────────────────────────────────────────────────────────────


def _registered() -> dict:
    app = web.Application()
    crew_routes.register_crew_routes(app)
    return {
        (r.method, str(r.resource.canonical)[len(BASE) :]): r.handler for r in app.router.routes()
    }


def _payload(response: web.Response) -> dict:
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


class RouteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        _isolate_crew_log(self)
        for patcher in (
            mock.patch.object(routes, "_scope", return_value=self.root),
            mock.patch.object(routes, "is_app_enabled", return_value=True),
            mock.patch.object(store, "is_repo_connected", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _get(self, *, provider="github", host="github.com", connected=True) -> web.Response:
        handler = _registered()[("GET", "/crew/fabric")]
        q = f"owner={OWNER}&repo={REPO}&provider={provider}&host={host}"
        req = make_mocked_request("GET", f"{BASE}/crew/fabric?{q}")
        with mock.patch.object(store, "is_repo_connected", return_value=connected):
            return await handler(req)  # type: ignore[operator]

    # registration + gates

    def test_route_is_registered(self):
        self.assertIn(("GET", "/crew/fabric"), _registered())

    async def test_not_connected_is_404(self):
        resp = await self._get(connected=False)
        self.assertEqual(resp.status, 404)

    # empty

    async def test_empty_repo_answers_200_with_no_items(self):
        resp = await self._get()
        self.assertEqual(resp.status, 200)
        body = _payload(resp)
        self.assertEqual(body["items"], [])
        self.assertEqual(body["schema"], crew_store.FABRIC_SCHEMA)
        self.assertEqual(body["phases"], list(crew_store.SPINE_PHASES))
        self.assertEqual(body["owner"], OWNER)
        self.assertEqual(body["repo"], REPO)
        self.assertTrue(body["generated_at"])

    # non-github provider

    async def test_non_github_provider_answers_200_empty(self):
        # Even if a GitLab repo somehow had crew records on disk, the route must
        # answer items:[] — crews are a GitHub-only feature.
        crew = _crew(self.root)
        _step(self.root, crew["id"], 5109, "claimed", "claim", "claimed")
        resp = await self._get(provider="gitlab", host="gitlab.com")
        self.assertEqual(resp.status, 200)
        body = _payload(resp)
        self.assertEqual(body["items"], [])
        self.assertEqual(body["provider"], "gitlab")

    # populated

    async def test_populated_repo_returns_folded_items(self):
        crew = _crew(self.root)
        cid = crew["id"]
        for phase, kind, text in (
            ("claimed", "claim", "claimed"),
            ("implementing", "implement", "edit"),
            ("awaiting-ci", "ci", "PR opened"),
        ):
            _step(self.root, cid, 5109, phase, kind, text)
        resp = await self._get()
        self.assertEqual(resp.status, 200)
        body = _payload(resp)
        self.assertEqual(len(body["items"]), 1)
        item = body["items"][0]
        self.assertEqual(item["number"], 5109)
        self.assertEqual(item["phase"], "awaiting-ci")
        self.assertEqual(
            [t["phase"] for t in item["timeline"]],
            ["claimed", "implementing", "awaiting-ci"],
        )


if __name__ == "__main__":
    unittest.main()


class TitleHintDegradationTest(unittest.TestCase):
    """A title cache that cannot be read costs the TITLES, not the endpoint.

    The caches belong to Issue Radar and its refresh rewrites them, so a reader can
    lose the race between checking a file exists and reading it. Letting that surface
    means `GET /crew/fabric` answers 500 because a decorative lookup failed -- while
    the same join already degrades gracefully everywhere else, rendering a number with
    no cached title as its id alone.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        _isolate_crew_log(self)
        crew = _crew(self.root)
        self.cid = crew["id"]
        _step(self.root, self.cid, 5109, "claimed", "claim", "mine")

    def test_an_unreadable_title_cache_still_folds_the_lane(self):
        import kiro_crew.apps.builtins.issue_radar.backend.store as ir_store

        def boom(*a, **k):
            raise OSError("cache vanished mid-refresh")

        with (
            mock.patch.object(ir_store, "read_issues_cache", boom),
            mock.patch.object(ir_store, "read_pulls_cache", boom),
        ):
            items = crew_store.fold_fabric(OWNER, REPO, self.root)

        self.assertEqual([i["number"] for i in items], [5109])
        # No title is the CORRECT degradation -- the lane renders as its id.
        self.assertEqual(items[0].get("title", ""), "")


@pytest.mark.timeout(300)
class TestStalledLaneSurvivesLedgerVolume(unittest.TestCase):
    """A lane parked in one phase keeps its entry timestamp no matter how many
    newer lines the crew's log accumulates.

    The fold keeps the lines that ENTERED a phase per item, apart from the bounded
    tail of progress lines a crew page shows. Before that, the fabric derived them
    from a newest-first read of every line under a cap, so the cap dropped the
    OLDEST lines: a lane that had sat in a phase long enough for the crew to log a
    few thousand later steps lost the line that says WHEN it entered -- so its dwell
    could not be computed and it dropped out of the queue summary's longest-wait.
    The direction was perverse: the longer an item stalled, the more certain the
    board was to hide it, which is the one thing the board exists to show. These
    cases fill the event tail well past its bound and read the entry back.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        _isolate_crew_log(self)

    def test_an_old_phase_entry_survives_thousands_of_later_writes(self):
        crew = _crew(self.root)
        cid = crew["id"]

        # The stalled lane enters awaiting-ci and never moves again.
        _step(self.root, cid, 4242, "awaiting-ci", "ci", "waiting on checks")
        entry = _fold_item(self.root, 4242)
        self.assertEqual(entry["phase"], "awaiting-ci")
        stamped_at = entry["timeline"][-1]["at"]
        self.assertTrue(stamped_at, "the lane must carry the moment it entered the phase")

        # The crew stays busy: three times the event tail's bound in further writes
        # on the SAME item that do not move it (CI rounds), which is how production
        # accumulates volume without transitions. The count is load-bearing (it is
        # well past the bound the tail keeps), but the write PATH is not -- the
        # property lives in the fold keeping phase entries per item -- so the volume
        # is queued straight onto the writer instead of driven through the store.
        _bulk_no_move_lines(self.root, cid, 4242, 3 * crew_log.RADAR_EVENT_LIMIT, tag="round")

        after = _fold_item(self.root, 4242)
        self.assertEqual(
            after["timeline"][-1]["at"],
            stamped_at,
            "the entry timestamp must survive the newer lines that buried it",
        )
        self.assertEqual(after["phase"], "awaiting-ci")

    def test_a_stalled_lane_is_not_buried_by_other_items_volume(self):
        crew = _crew(self.root)
        cid = crew["id"]

        # A SPINE phase: an off-spine one (awaiting-reply, skipped, yielded,
        # handed-back, preempted) is an exit stub and carries no timeline entry
        # at all, so it could not show this truncation either way.
        _step(self.root, cid, 77, "implementing", "implement", "started the fix")
        stalled_at = _fold_item(self.root, 77)["timeline"][-1]["at"]

        # Other work items generate the volume, so the stalled lane itself is
        # written once and then never again -- the worst case for a shared tail.
        # Together the three fill the tail three times over (see _bulk_no_move_lines).
        for other in (900, 901, 902):
            _bulk_no_move_lines(self.root, cid, other, crew_log.RADAR_EVENT_LIMIT, tag=f"x{other}")

        self.assertEqual(
            _fold_item(self.root, 77)["timeline"][-1]["at"],
            stalled_at,
            "another item's write volume must not erase this lane's entry",
        )

    def test_phase_filter_alone_saves_the_entry_when_the_cap_cannot(self):
        """The per-item phase history is load-bearing ON ITS OWN, not the tail cap.

        The two cases above go red only if the phase history were derived from an
        event tail SHORTER than their filler. This case fills the tail one line past
        the fold's own bound (``RADAR_EVENT_LIMIT``), so a fabric that read phase
        entries out of the tail would find the lone old phase line evicted -- the
        smallest count that still goes red under that mutation, since the phase
        line is then exactly the single oldest line the bound evicts.
        """
        crew = _crew(self.root)
        cid = crew["id"]

        # The stalled lane enters its phase once and never moves again.
        _step(self.root, cid, 555, "awaiting-ci", "ci", "waiting on checks")
        entry = _fold_item(self.root, 555)
        self.assertEqual(entry["phase"], "awaiting-ci")
        stamped_at = entry["timeline"][-1]["at"]
        self.assertTrue(stamped_at, "the lane must carry the moment it entered the phase")

        # One filler past the tail's bound: the phase line is outside the tail
        # entirely, and the lane still knows when it entered.
        filler_n = crew_log.RADAR_EVENT_LIMIT + 1
        _bulk_no_move_lines(self.root, cid, 900, filler_n, tag="filler")
        tail = crew_store.read_events(OWNER, REPO, self.root, crew_id=cid, limit=filler_n + 5)
        self.assertNotIn("waiting on checks", [line["text"] for line in tail])

        after = _fold_item(self.root, 555)
        self.assertEqual(
            after["timeline"][-1]["at"],
            stamped_at,
            "the phase history alone must keep the entry when the tail cannot",
        )
        self.assertEqual(after["phase"], "awaiting-ci")
