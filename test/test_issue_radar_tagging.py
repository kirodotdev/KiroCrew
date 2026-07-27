"""Tests for Issue Radar's Tagging dashboard backend.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

Four deterministic, subprocess-free surfaces:

  * **store tagging cache** — merge semantics (a batch wins for the issues it
    covers, everything else survives), prune-on-apply, schema guard, and the
    tolerant normalizer.
  * **queue selection** (``routes._untagged``) — the strict "zero labels"
    definition and newest-first order.
  * **model-output validation** (``routes._compute_tagging_suggestions``) — the
    security-relevant half: a label the repo doesn't define, or an issue number
    that wasn't in the batch, must never survive, because the issue text fed to
    the model is attacker-controlled.
  * **bulk apply route** — the unknown-label pre-check fires before ANY write,
    partial failure is reported rather than swallowed, and only the issues that
    actually got labelled leave the queue.
"""
import contextlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import routes, store


class TestTaggingCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_absent_returns_none(self):
        self.assertIsNone(store.read_tagging_cache("o", "r", self.tmp))

    def test_merge_roundtrip_stamps_generated_at(self):
        res = store.merge_tagging_suggestions(
            "o", "r", {"7": [{"name": "bug", "reason": "crash"}]}, root=self.tmp
        )
        self.assertEqual(res["suggestions"], {"7": [{"name": "bug", "reason": "crash"}]})
        self.assertTrue(res["generated_at"])
        got = store.read_tagging_cache("o", "r", self.tmp)
        assert got is not None
        self.assertEqual(got["suggestions"], {"7": [{"name": "bug", "reason": "crash"}]})

    def test_merge_keeps_other_issues_and_replaces_covered_one(self):
        store.merge_tagging_suggestions(
            "o", "r", {"7": [{"name": "bug", "reason": "a"}], "8": [{"name": "docs", "reason": "b"}]},
            root=self.tmp,
        )
        # Regenerating #7 must replace its stale proposal but leave #8 alone —
        # this is what lets the dashboard walk a long queue in slices.
        res = store.merge_tagging_suggestions(
            "o", "r", {"7": [{"name": "question", "reason": "c"}]}, root=self.tmp
        )
        self.assertEqual(res["suggestions"]["7"], [{"name": "question", "reason": "c"}])
        self.assertEqual(res["suggestions"]["8"], [{"name": "docs", "reason": "b"}])

    def test_empty_list_is_persisted_not_dropped(self):
        # "Analysed, nothing applies" must be recorded, else the next batch would
        # keep handing back the same unlabelable issue forever.
        store.merge_tagging_suggestions("o", "r", {"9": []}, root=self.tmp)
        got = store.read_tagging_cache("o", "r", self.tmp)
        assert got is not None
        self.assertIn("9", got["suggestions"])
        self.assertEqual(got["suggestions"]["9"], [])

    def test_drop_removes_only_named_issues(self):
        store.merge_tagging_suggestions(
            "o", "r", {"7": [{"name": "bug", "reason": ""}], "8": [{"name": "docs", "reason": ""}]},
            root=self.tmp,
        )
        res = store.drop_tagging_suggestions("o", "r", [7], root=self.tmp)
        self.assertEqual(list(res["suggestions"]), ["8"])
        got = store.read_tagging_cache("o", "r", self.tmp)
        assert got is not None
        self.assertEqual(list(got["suggestions"]), ["8"])

    def test_drop_with_no_cache_is_noop(self):
        res = store.drop_tagging_suggestions("o", "r", [1, 2], root=self.tmp)
        self.assertEqual(res["suggestions"], {})

    def test_stale_schema_reads_as_miss(self):
        path = store.tagging_cache_path("o", "r", self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema": 0, "suggestions": {"7": []}}', encoding="utf-8")
        self.assertIsNone(store.read_tagging_cache("o", "r", self.tmp))

    def test_corrupt_json_reads_as_miss(self):
        path = store.tagging_cache_path("o", "r", self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(store.read_tagging_cache("o", "r", self.tmp))

    def test_normalizer_drops_junk_and_dedupes(self):
        got = store._normalize_tagging({
            "7": [{"name": "bug", "reason": "x"}, {"name": "bug", "reason": "dup"}, {"name": ""}, "nope"],
            "not-a-number": [{"name": "bug"}],
            "-3": [{"name": "bug"}],
            "8": "not-a-list",
        })
        self.assertEqual(got, {"7": [{"name": "bug", "reason": "x"}]})


class TestUntaggedQueue(unittest.TestCase):
    def test_only_zero_label_issues_newest_first(self):
        issues = [
            {"number": 1, "labels": [], "created_at": "2024-01-01T00:00:00Z"},
            {"number": 2, "labels": ["bug"], "created_at": "2024-05-01T00:00:00Z"},
            {"number": 3, "labels": [], "created_at": "2024-03-01T00:00:00Z"},
        ]
        self.assertEqual([i["number"] for i in routes._untagged(issues)], [3, 1])

    def test_tolerates_missing_fields_and_non_dicts(self):
        issues = [{"number": 5}, "junk", {"number": 6, "labels": None}]
        self.assertEqual({i["number"] for i in routes._untagged(issues)}, {5, 6})


class TestShortRationale(unittest.TestCase):
    """The reason line is prose only — the evidence is the `examples` list.

    The prompt asks for one short clause without issue references, but the model
    reliably slips a "(see #123, #456)" in, which duplicates the linked examples
    rendered directly below it and pushes the actual reason out of the row.
    """

    def test_strips_issue_references(self):
        self.assertEqual(
            routes._short_rationale("Several crashes reported (#12, #34)"),
            "Several crashes reported",
        )
        self.assertNotIn("#", routes._short_rationale("see #12 #34 #56 in the tracker"))
        self.assertNotIn("#", routes._short_rationale("#7 and #8 both need it"))

    def test_keeps_only_the_first_sentence(self):
        self.assertEqual(
            routes._short_rationale("Six issues touch login. Also the docs are stale. And more."),
            "Six issues touch login",
        )

    def test_clamps_length(self):
        got = routes._short_rationale("word " * 80)
        self.assertLessEqual(len(got), routes._RATIONALE_MAX_CHARS)

    def test_empty_and_none_are_safe(self):
        self.assertEqual(routes._short_rationale(None), "")
        self.assertEqual(routes._short_rationale(""), "")
        # A rationale that was NOTHING but issue refs collapses to empty rather
        # than leaving stray punctuation behind.
        self.assertEqual(routes._short_rationale("(#12, #34)"), "")

    def test_a_decimal_does_not_end_the_sentence(self):
        self.assertEqual(
            routes._short_rationale("Blocks the 1.2 release train"),
            "Blocks the 1.2 release train",
        )


class TestRecoPromptIsStyleNeutral(unittest.TestCase):
    """The taxonomy prompt must not preset a label naming convention.

    Real repos are split across mutually incompatible styles — flat (`bug`),
    slash namespaces (`kind/bug`), colon+space (`Type: Bug`), hyphen prefixes
    (`type-bug`), single-letter codes (`A-diagnostics`) — so any house style
    baked into the prompt is wrong for most repos. The earlier version told the
    model to "use conventional names (`priority: high`, `area: <x>`, …)", which
    made it propose prefixed names for repos whose whole label set is flat.
    """

    FLAT_REPO_LABELS = [
        {"name": "bug", "description": "Something isn't working"},
        {"name": "documentation", "description": ""},
        {"name": "release-blocker", "description": ""},
    ]
    SAMPLE = [{"number": 12, "title": "login times out", "body": "…", "labels": []}]

    def _prompt(self) -> str:
        return routes._build_reco_prompt("o", "r", self.FLAT_REPO_LABELS, self.SAMPLE)

    def test_no_hardcoded_naming_style(self):
        prompt = self._prompt()
        # None of the five schools may appear as a prescribed example.
        for anchor in ("priority: high", "area: auth", "type: bug", "kind/bug",
                       "Type: Bug", "A-diagnostics"):
            self.assertNotIn(anchor, prompt, f"prompt presets a naming style: {anchor}")

    def test_instructs_the_model_to_match_the_existing_convention(self):
        prompt = self._prompt()
        self.assertIn("MATCH THE NAMING CONVENTION ALREADY IN USE", prompt)
        # And it must actually be given the set to infer from.
        self.assertIn("release-blocker", prompt)

    def test_category_is_not_advertised_as_a_prefix(self):
        # `category` drives the UI tag + triage-role mapping; pasting it into the
        # name is exactly the failure mode this guards.
        self.assertIn("`category` is metadata for the UI, NOT a prefix", self._prompt())

    def test_falls_back_to_flat_names_when_there_is_nothing_to_copy(self):
        prompt = routes._build_reco_prompt("o", "r", [], self.SAMPLE)
        self.assertIn("this repo defines no labels yet", prompt)
        self.assertIn("plain lowercase names with no", prompt)


LABELS = [
    {"name": "bug", "color": "ee0000", "description": ""},
    {"name": "docs", "color": "0000ee", "description": ""},
    {"name": "question", "color": "00ee00", "description": ""},
    {"name": "enhancement", "color": "cccccc", "description": ""},
]
BATCH = [
    {"number": 7, "title": "crash on start", "body": "boom"},
    {"number": 8, "title": "typo in readme", "body": "typo"},
]


async def _compute(payload_text: str) -> dict:
    with mock.patch.object(
        routes, "_run_oneshot_model", new=AsyncMock(return_value=payload_text)
    ):
        return await routes._compute_tagging_suggestions(
            MagicMock(), "o", "r", LABELS, BATCH
        )


@pytest.mark.asyncio
async def test_valid_assignment_survives():
    got = await _compute('{"assignments": [{"number": 7, "labels": '
                         '[{"name": "bug", "reason": "reports a crash"}]}]}')
    assert got == {"7": [{"name": "bug", "reason": "reports a crash"}]}


@pytest.mark.asyncio
async def test_label_not_defined_on_repo_is_dropped():
    # The prompt-injection guard: issue bodies are attacker-controlled, so an
    # invented label must not reach /labels/apply.
    got = await _compute('{"assignments": [{"number": 7, "labels": '
                         '[{"name": "P0-DROP-EVERYTHING", "reason": "x"}]}]}')
    assert got == {}


@pytest.mark.asyncio
async def test_issue_outside_the_batch_is_dropped():
    got = await _compute('{"assignments": [{"number": 999, "labels": '
                         '[{"name": "bug", "reason": "x"}]}]}')
    assert got == {}


@pytest.mark.asyncio
async def test_per_issue_cap_and_dedupe():
    got = await _compute(
        '{"assignments": [{"number": 8, "labels": ['
        '{"name": "bug", "reason": "1"}, {"name": "bug", "reason": "dup"},'
        '{"name": "docs", "reason": "2"}, {"name": "question", "reason": "3"},'
        '{"name": "enhancement", "reason": "4"}]}]}'
    )
    names = [row["name"] for row in got["8"]]
    assert names == ["bug", "docs", "question"][:routes._TAG_MAX_PER_ISSUE]
    assert len(names) <= routes._TAG_MAX_PER_ISSUE


@pytest.mark.asyncio
async def test_bare_string_labels_and_unparsable_output_degrade_gracefully():
    assert await _compute('{"assignments": [{"number": 7, "labels": ["docs"]}]}') == {
        "7": [{"name": "docs", "reason": ""}]
    }
    assert await _compute("I am afraid I cannot do that") == {}


def _bulk_request(body: dict):
    req = make_mocked_request("POST", "/api/apps/issue-radar/labels/apply-bulk")
    req.json = AsyncMock(return_value=body)
    return req


@pytest.mark.asyncio
async def test_bulk_apply_rejects_unknown_label_before_any_write(tmp_path):
    add_labels = MagicMock()
    with (
        mock.patch.object(store, "is_repo_connected", return_value=True),
        mock.patch.object(routes, "_repo_can_write", return_value=True),
        mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=LABELS)),
        mock.patch.object(store, "issue_write_lock", lambda *a, **k: contextlib.nullcontext()),
        mock.patch.object(gh, "add_issue_labels", add_labels),
    ):
        resp = await routes._handle_labels_apply_bulk(_bulk_request({
            "owner": "o", "repo": "r",
            "changes": [{"number": 7, "add": ["bug"]}, {"number": 8, "add": ["nope"]}],
        }))
    assert resp.status == 400
    # All-or-nothing: a typo must not leave half the batch applied.
    add_labels.assert_not_called()


@pytest.mark.asyncio
async def test_bulk_apply_reports_partial_failure_and_prunes_only_applied():
    def fake_add(owner, repo, number, names):
        if number == 8:
            raise gh.GhCliError("issue is locked")
        return [{"name": n, "color": "ee0000", "description": ""} for n in names]

    dropped: list[list[int]] = []
    with (
        mock.patch.object(store, "is_repo_connected", return_value=True),
        mock.patch.object(routes, "_repo_can_write", return_value=True),
        mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=LABELS)),
        mock.patch.object(store, "issue_write_lock", lambda *a, **k: contextlib.nullcontext()),
        mock.patch.object(gh, "add_issue_labels", side_effect=fake_add),
        mock.patch.object(store, "apply_label_change_to_caches"),
        mock.patch.object(
            store, "drop_tagging_suggestions",
            side_effect=lambda o, r, numbers: dropped.append(list(numbers)),
        ),
    ):
        resp = await routes._handle_labels_apply_bulk(_bulk_request({
            "owner": "o", "repo": "r",
            "changes": [{"number": 7, "add": ["bug"]}, {"number": 8, "add": ["docs"]}],
        }))

    assert resp.status == 200
    body = resp.body.decode()
    assert '"number": 7' in body
    assert "issue is locked" in body
    # #8 keeps its suggestion so the user can retry it.
    assert dropped == [[7]]


@pytest.mark.asyncio
async def test_bulk_apply_is_denied_on_a_read_only_repo():
    add_labels = MagicMock()
    with (
        mock.patch.object(store, "is_repo_connected", return_value=True),
        mock.patch.object(routes, "_repo_can_write", return_value=None),
        mock.patch.object(store, "issue_write_lock", lambda *a, **k: contextlib.nullcontext()),
        mock.patch.object(gh, "add_issue_labels", add_labels),
    ):
        resp = await routes._handle_labels_apply_bulk(_bulk_request({
            "owner": "o", "repo": "r", "changes": [{"number": 7, "add": ["bug"]}],
        }))
    # `None` means "couldn't tell" and must fail CLOSED, like the single-issue route.
    assert resp.status == 403
    add_labels.assert_not_called()


@pytest.mark.asyncio
async def test_bulk_apply_validates_the_request_shape():
    for body, status in (
        ({"owner": "o", "repo": "r", "changes": []}, 400),
        ({"owner": "", "repo": "r", "changes": [{"number": 1, "add": ["bug"]}]}, 400),
        ({"owner": "o", "repo": "r", "changes": [{"number": 0, "add": ["bug"]}]}, 400),
        ({"owner": "o", "repo": "r", "changes": [{"number": 1, "add": []}]}, 400),
        ({"owner": "o", "repo": "r",
          "changes": [{"number": n, "add": ["bug"]} for n in range(routes._TAG_BULK_MAX + 2)]}, 400),
    ):
        resp = await routes._handle_labels_apply_bulk(_bulk_request(body))
        assert resp.status == status, body


# ── regression coverage for the correctness controls ─────────────────────────
#
# Each test below exists because REMOVING the thing it covers would be silent.


class TestTaggingCacheLockSerializesWriters(unittest.TestCase):
    """The tagging cache lock must serialize a merge against a prune.

    Both mutations are read-modify-write over the WHOLE document, so without the
    lock an interleaved pair loses one of them: a prune that read before a merge
    wrote will re-persist the pruned-away entry, resurrecting a suggestion for an
    issue whose labels were just applied. This drives real thread contention
    rather than asserting the lock exists, so deleting the lock fails it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        store.merge_tagging_suggestions(
            "o", "r",
            {str(n): [{"name": "bug", "reason": ""}] for n in range(1, 21)},
            root=self.tmp,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_merge_and_prune_both_survive(self):
        import threading

        start = threading.Barrier(2)
        errors: list[BaseException] = []

        def merge():
            try:
                start.wait(timeout=5)
                for n in range(100, 120):
                    store.merge_tagging_suggestions(
                        "o", "r", {str(n): [{"name": "docs", "reason": ""}]}, root=self.tmp
                    )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        def prune():
            try:
                start.wait(timeout=5)
                for n in range(1, 21):
                    store.drop_tagging_suggestions("o", "r", [n], root=self.tmp)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=merge), threading.Thread(target=prune)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])

        final = store.read_tagging_cache("o", "r", self.tmp)
        assert final is not None
        got = set(final["suggestions"])
        # Every merged issue is present and every pruned one is gone. A lost
        # update shows up as a missing 1xx key or a resurrected 1..20 key.
        self.assertEqual(got, {str(n) for n in range(100, 120)})


class TestUntaggedQueueRoute(unittest.IsolatedAsyncioTestCase):
    """GET /tagging: what it returns, and that ``refresh`` reaches GitHub."""

    ISSUES = [
        {"number": 7, "title": "crash", "url": "https://github.com/o/r/issues/7",
         "labels": [], "created_at": "2026-07-02T00:00:00Z", "author": "alice"},
        {"number": 8, "title": "typo", "url": "https://github.com/o/r/issues/8",
         "labels": ["bug"], "created_at": "2026-07-03T00:00:00Z", "author": "bob"},
    ]

    def _req(self, query: str = "owner=o&repo=r"):
        return make_mocked_request("GET", f"/api/apps/issue-radar/tagging?{query}")

    async def test_returns_rows_for_zero_label_issues_only(self):
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_issues_cache", return_value=self.ISSUES),
            mock.patch.object(store, "read_tagging_cache", return_value=None),
        ):
            resp = await routes._handle_get_tagging(self._req())
        assert resp.status == 200
        body = json.loads(resp.body)
        # Rows, not just numbers — the frontend must not have to resolve them.
        self.assertEqual([r["number"] for r in body["issues"]], [7])
        self.assertEqual(body["issues"][0]["title"], "crash")
        self.assertEqual(body["untagged"], [7])
        self.assertEqual(body["open_count"], 2)
        self.assertEqual(body["batch_size"], routes._TAG_BATCH_MAX)

    async def test_drops_suggestions_for_issues_labelled_elsewhere(self):
        cached = {"suggestions": {"7": [{"name": "bug", "reason": "x"}],
                                  "8": [{"name": "docs", "reason": "y"}]},
                  "generated_at": "2026-07-26T00:00:00Z"}
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_issues_cache", return_value=self.ISSUES),
            mock.patch.object(store, "read_tagging_cache", return_value=cached),
        ):
            resp = await routes._handle_get_tagging(self._req())
        body = json.loads(resp.body)
        # #8 picked up a label, so its cached proposal is moot.
        self.assertEqual(list(body["suggestions"]), ["7"])

    async def test_refresh_bypasses_the_cache_and_refetches(self):
        read_cache = mock.Mock(return_value=self.ISSUES)
        # Patch the atomic helper, not `write_issues_cache`: the route now goes
        # through `refresh_issues_cache`, which would otherwise take a real lock
        # and write the developer's actual app data dir during the test.
        refreshed = mock.Mock(side_effect=lambda o, r, fetch, **kw: fetch())
        listed = mock.Mock(return_value=self.ISSUES)
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_issues_cache", read_cache),
            mock.patch.object(store, "refresh_issues_cache", refreshed),
            mock.patch.object(store, "read_tagging_cache", return_value=None),
            mock.patch.object(gh, "list_open_issues", listed),
        ):
            resp = await routes._handle_get_tagging(self._req("owner=o&repo=r&refresh=1"))
        assert resp.status == 200
        # Labels get added on GitHub itself, so reload MUST re-read from gh —
        # and it must do so through the fetch-and-write-under-one-lock helper.
        refreshed.assert_called_once()
        listed.assert_called_once()
        read_cache.assert_not_called()

    async def test_refresh_holds_the_lock_across_fetch_and_write(self):
        # Locking only the write loses data rather than ordering it: a patch that
        # lands between the fetch and the write is overwritten by the pre-write
        # snapshot, so a just-applied label vanishes from the dashboard.
        tmp = Path(tempfile.mkdtemp())
        try:
            held: list[bool] = []

            def fetch() -> list[dict]:
                # Inside `refresh_issues_cache`, so the lock must already be ours.
                # A second acquisition from this thread would deadlock, so assert
                # on the lock FILE existing instead.
                held.append(
                    store.issues_cache_path("o", "r", tmp, "open")
                    .with_suffix(".json.lock").is_file()
                )
                return [{"number": 1, "labels": []}]

            store.refresh_issues_cache("o", "r", fetch, root=tmp, state="open")
            self.assertEqual(held, [True])
            self.assertEqual(store.read_issues_cache("o", "r", tmp, state="open"),
                             [{"number": 1, "labels": []}])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_serves_label_counts_and_titles_over_the_open_set(self):
        # Both used to be derived from the frontend's shared issue list, which
        # follows the user's open/closed filter — so entering Tagging from a
        # Closed filter reported closed counts as open ones.
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_issues_cache", return_value=self.ISSUES),
            mock.patch.object(store, "read_tagging_cache", return_value=None),
        ):
            resp = await routes._handle_get_tagging(self._req())
        body = json.loads(resp.body)
        self.assertEqual(body["label_counts"], {"bug": 1})
        self.assertEqual(body["titles"], {"7": "crash", "8": "typo"})

    async def test_unconnected_repo_is_404(self):
        with mock.patch.object(store, "is_repo_connected", return_value=False):
            resp = await routes._handle_get_tagging(self._req())
        self.assertEqual(resp.status, 404)


class TestGenerateTaggingRoute(unittest.IsolatedAsyncioTestCase):
    """POST /tagging: automatic slice vs explicit numbers, and what gets cached."""

    ISSUES = [
        {"number": n, "title": f"issue {n}", "body": "", "labels": [],
         "created_at": f"2026-07-{n:02d}T00:00:00Z"}
        for n in range(1, 6)
    ]
    LABELS = [{"name": "bug", "color": "ee0000", "description": ""}]

    def _req(self, body: dict):
        req = make_mocked_request("POST", "/api/apps/issue-radar/tagging")
        req.json = AsyncMock(return_value=body)
        return req

    def _patches(self, computed: dict, cached: dict | None, merged: list):
        return (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_issues_cache", return_value=self.ISSUES),
            mock.patch.object(store, "read_labels_cache", return_value=self.LABELS),
            mock.patch.object(store, "read_tagging_cache", return_value=cached),
            mock.patch.object(
                routes, "_compute_tagging_suggestions", new=AsyncMock(return_value=computed)
            ),
            mock.patch.object(
                store, "merge_tagging_suggestions",
                side_effect=lambda o, r, batch: (
                    merged.append(batch) or {"suggestions": batch, "generated_at": "t"}
                ),
            ),
        )

    async def test_records_analysed_issues_the_model_declined_to_label(self):
        merged: list = []
        with contextlib.ExitStack() as stack:
            for p in self._patches({"1": [{"name": "bug", "reason": "x"}]}, None, merged):
                stack.enter_context(p)
            resp = await routes._handle_generate_tagging(self._req({"owner": "o", "repo": "r"}))
        assert resp.status == 200
        body = json.loads(resp.body)
        self.assertEqual(sorted(body["analyzed"]), [1, 2, 3, 4, 5])
        # Issues 2..5 got no label but MUST still be recorded, else the "next
        # un-analysed slice" would hand back the same unlabelable issues forever.
        self.assertEqual(merged[0]["1"], [{"name": "bug", "reason": "x"}])
        self.assertEqual(merged[0]["5"], [])

    async def test_automatic_slice_skips_already_analysed_issues(self):
        merged: list = []
        cached = {"suggestions": {"1": [], "2": []}, "generated_at": "t"}
        with contextlib.ExitStack() as stack:
            for p in self._patches({}, cached, merged):
                stack.enter_context(p)
            resp = await routes._handle_generate_tagging(self._req({"owner": "o", "repo": "r"}))
        body = json.loads(resp.body)
        self.assertEqual(sorted(body["analyzed"]), [3, 4, 5])

    async def test_explicit_numbers_reanalyse_even_when_already_cached(self):
        merged: list = []
        cached = {"suggestions": {str(n): [] for n in range(1, 6)}, "generated_at": "t"}
        with contextlib.ExitStack() as stack:
            for p in self._patches({"3": [{"name": "bug", "reason": "x"}]}, cached, merged):
                stack.enter_context(p)
            resp = await routes._handle_generate_tagging(
                self._req({"owner": "o", "repo": "r", "numbers": [3]})
            )
        body = json.loads(resp.body)
        self.assertEqual(body["analyzed"], [3])

    async def test_reports_what_is_left_after_the_cap(self):
        merged: list = []
        with (
            mock.patch.object(routes, "_TAG_BATCH_MAX", 2),
            contextlib.ExitStack() as stack,
        ):
            for p in self._patches({}, None, merged):
                stack.enter_context(p)
            resp = await routes._handle_generate_tagging(self._req({"owner": "o", "repo": "r"}))
        body = json.loads(resp.body)
        self.assertEqual(len(body["analyzed"]), 2)
        self.assertEqual(body["remaining"], 3)

    async def test_a_repo_with_no_labels_is_rejected_before_the_model_runs(self):
        compute = AsyncMock()
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_labels_cache", return_value=[]),
            mock.patch.object(gh, "list_repo_labels", return_value=[]),
            mock.patch.object(store, "write_labels_cache"),
            mock.patch.object(store, "read_issues_cache", return_value=self.ISSUES),
            mock.patch.object(routes, "_compute_tagging_suggestions", new=compute),
        ):
            resp = await routes._handle_generate_tagging(self._req({"owner": "o", "repo": "r"}))
        self.assertEqual(resp.status, 400)
        compute.assert_not_called()


class TestSingleApplyPrunesTheQueue(unittest.IsolatedAsyncioTestCase):
    """A successful single-issue apply must drop that issue's cached proposal.

    Without it, accepting a suggestion from the issue detail pane leaves the
    Tagging dashboard offering to re-label an issue that is already labelled."""

    def _req(self, body: dict):
        req = make_mocked_request("POST", "/api/apps/issue-radar/labels/apply")
        req.json = AsyncMock(return_value=body)
        return req

    async def test_prunes_on_success(self):
        dropped: list = []
        final = [{"name": "bug", "color": "ee0000", "description": ""}]
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=LABELS)),
            mock.patch.object(store, "issue_write_lock", lambda *a, **k: contextlib.nullcontext()),
            mock.patch.object(gh, "add_issue_labels", return_value=final),
            mock.patch.object(store, "apply_label_change_to_caches"),
            mock.patch.object(
                store, "drop_tagging_suggestions",
                side_effect=lambda o, r, numbers: dropped.append(list(numbers)),
            ),
        ):
            resp = await routes._handle_labels_apply(
                self._req({"owner": "o", "repo": "r", "number": 7, "add": ["bug"]})
            )
        self.assertEqual(resp.status, 200)
        self.assertEqual(dropped, [[7]])

    async def test_does_not_prune_when_the_write_failed(self):
        drop = mock.Mock()
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=LABELS)),
            mock.patch.object(store, "issue_write_lock", lambda *a, **k: contextlib.nullcontext()),
            mock.patch.object(gh, "add_issue_labels", side_effect=gh.GhCliError("boom")),
            mock.patch.object(store, "drop_tagging_suggestions", drop),
        ):
            resp = await routes._handle_labels_apply(
                self._req({"owner": "o", "repo": "r", "number": 7, "add": ["bug"]})
            )
        self.assertEqual(resp.status, 502)
        drop.assert_not_called()


class TestSuggestionRedaction(unittest.IsolatedAsyncioTestCase):
    """The model's `reason` text is an attacker-controlled path to the dashboard.

    Chain: anyone opens an issue -> its body reaches the model -> the model echoes
    part of it into a suggestion's ``reason`` -> the dashboard renders that string.
    So the reason goes through ``redact`` on the way out. Without this test,
    deleting that call would leave every existing test green while credentials and
    exfiltration URLs planted in an issue body flowed straight to the UI.

    Both patterns are asserted because ``redact`` is two passes with different
    triggers (exfiltration URLs, then credentials) and losing either is a hole.
    """

    LABELS = [{"name": "bug", "color": "ee0000", "description": ""}]
    BATCH = [{"number": 7, "title": "t", "body": "b"}]
    # Assembled at runtime so the fixture is not itself a greppable secret.
    FAKE_TOKEN = "ghp" + "_" + ("z4Kq" * 9)
    # A credential-BEARING URL. A plain link is deliberately not redacted — that
    # would flag every legitimate URL — so the exfil pass is asserted against the
    # case it actually exists for: a secret smuggled out in the query string.
    EXFIL_URL = (
        "https://attacker.example.com/collect?aws_secret_access_key=" + ("A1b2C3d4" * 5)
    )

    async def test_reason_is_redacted_before_it_reaches_the_dashboard(self):
        planted = f"reported with {self.FAKE_TOKEN} see {self.EXFIL_URL}"
        payload = json.dumps({"assignments": [{
            "number": 7,
            "labels": [{"name": "bug", "reason": planted}],
        }]})
        with mock.patch.object(
            routes, "_run_oneshot_model", new=AsyncMock(return_value=payload)
        ):
            got = await routes._compute_tagging_suggestions(
                MagicMock(), "o", "r", self.LABELS, self.BATCH
            )
        reason = got["7"][0]["reason"]
        self.assertNotIn(self.FAKE_TOKEN, reason)
        self.assertNotIn("aws_secret_access_key", reason)
        # The label itself still survives — redaction must not eat the signal.
        self.assertEqual(got["7"][0]["name"], "bug")


class TestMalformedModelOutput(unittest.IsolatedAsyncioTestCase):
    """Malformed model output must degrade to "no suggestions", never raise.

    Anything that escapes ``_compute_tagging_suggestions`` becomes a 502 the user
    reads as "could not be generated", so a scalar where a list belongs has to be
    absorbed here rather than crashing the parse."""

    LABELS = [{"name": "bug", "color": "ee0000", "description": ""}]
    BATCH = [{"number": 1, "title": "t", "body": "b"}]

    async def _compute(self, payload: str) -> dict:
        with mock.patch.object(
            routes, "_run_oneshot_model", new=AsyncMock(return_value=payload)
        ):
            return await routes._compute_tagging_suggestions(
                MagicMock(), "o", "r", self.LABELS, self.BATCH
            )

    async def test_scalar_assignments_is_absorbed(self):
        self.assertEqual(await self._compute('{"assignments": 1}'), {})

    async def test_scalar_labels_is_absorbed(self):
        self.assertEqual(
            await self._compute('{"assignments": [{"number": 1, "labels": 3}]}'), {}
        )

    async def test_boolean_number_is_not_treated_as_issue_one(self):
        # bool is a subclass of int, so `true` would otherwise label issue #1.
        got = await self._compute(
            '{"assignments": [{"number": true, "labels": [{"name": "bug", "reason": "x"}]}]}'
        )
        self.assertEqual(got, {})


class TestBooleanNumbersRejectedOnWritePaths(unittest.IsolatedAsyncioTestCase):
    """JSON ``true`` must not validate as a positive issue number."""

    async def test_bulk_apply_rejects_a_boolean_number(self):
        add = MagicMock()
        req = make_mocked_request("POST", "/api/apps/issue-radar/labels/apply-bulk")
        req.json = AsyncMock(return_value={
            "owner": "o", "repo": "r", "changes": [{"number": True, "add": ["bug"]}],
        })
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(store, "issue_write_lock", lambda *a, **k: contextlib.nullcontext()),
            mock.patch.object(gh, "add_issue_labels", add),
        ):
            resp = await routes._handle_labels_apply_bulk(req)
        self.assertEqual(resp.status, 400)
        add.assert_not_called()

    async def test_single_apply_rejects_a_boolean_number(self):
        add = MagicMock()
        req = make_mocked_request("POST", "/api/apps/issue-radar/labels/apply")
        req.json = AsyncMock(return_value={
            "owner": "o", "repo": "r", "number": True, "add": ["bug"],
        })
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(store, "issue_write_lock", lambda *a, **k: contextlib.nullcontext()),
            mock.patch.object(gh, "add_issue_labels", add),
        ):
            resp = await routes._handle_labels_apply(req)
        self.assertEqual(resp.status, 400)
        add.assert_not_called()


class TestIssuesCacheLockSerializesWriters(unittest.TestCase):
    """A full refresh and a post-write patch must not clobber each other.

    Both write the same list cache — the refresh replaces it, the patch does a
    read-modify-write — so without a shared lock a writer that read before another
    wrote re-persists its stale copy and the other's update is lost. Two dashboard
    tabs, or a second API client, make that a routine interleaving.

    The race is driven through two concurrent PATCHES over disjoint issues, because
    that has a deterministic invariant: every patched issue must end up labelled.
    A refresh-versus-patch pair does not — whichever legitimately goes last wins —
    so it cannot distinguish a lost update from correct ordering. Both paths take
    the same lock (asserted below), so covering one covers the mechanism.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refresh_and_patch_share_one_lock_file(self):
        # The whole point is that the two writers contend on the SAME lock; if
        # they ever diverge onto separate files the serialization is a no-op.
        expected = store.issues_cache_path("o", "r", self.tmp, "open").with_suffix(".json.lock")
        with store.issues_cache_lock("o", "r", self.tmp, "open"):
            self.assertTrue(expected.is_file())

    def test_concurrent_patches_do_not_lose_each_other(self):
        import threading

        store.write_issues_cache(
            "o", "r", [{"number": n, "labels": []} for n in range(1, 41)],
            root=self.tmp, state="open",
        )

        start = threading.Barrier(2)
        errors: list[BaseException] = []

        def patch(numbers, label):
            def run():
                try:
                    start.wait(timeout=5)
                    for n in numbers:
                        store.apply_label_change_to_caches(
                            "o", "r", n,
                            [{"name": label, "color": "ee0000", "description": ""}],
                            root=self.tmp,
                        )
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
            return run

        threads = [
            threading.Thread(target=patch(range(1, 21), "bug")),
            threading.Thread(target=patch(range(21, 41), "docs")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])

        cached = store.read_issues_cache("o", "r", self.tmp, state="open")
        assert cached is not None
        by_number = {i["number"]: i.get("labels") or [] for i in cached}
        self.assertEqual(len(by_number), 40)
        # Every patch must survive. A lost update shows up as an empty label list
        # on an issue one of the threads definitely wrote.
        unlabelled = sorted(n for n, labels in by_number.items() if not labels)
        self.assertEqual(unlabelled, [], f"lost updates for issues {unlabelled}")


class TestBooleanNumberOnEveryMutationPath(unittest.IsolatedAsyncioTestCase):
    """``true`` must not target issue #1 on ANY handler that takes a number.

    ``bool`` is a subclass of ``int``, so a bare ``isinstance(number, int)`` accepts
    it and ``int(True) == 1``. The label handlers were covered already; these are
    the two remaining mutations — closing/reopening an issue, and overwriting its
    investigation record — where the same regression would be silent."""

    async def test_issue_state_rejects_a_boolean_number(self):
        setter = MagicMock()
        req = make_mocked_request("POST", "/api/apps/issue-radar/issue/state")
        req.json = AsyncMock(return_value={
            "owner": "o", "repo": "r", "number": True, "state": "closed",
        })
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(gh, "set_issue_state", setter),
        ):
            resp = await routes._handle_issue_state(req)
        self.assertEqual(resp.status, 400)
        setter.assert_not_called()

    async def test_investigation_put_rejects_a_boolean_number(self):
        writer = MagicMock()
        req = make_mocked_request("PUT", "/api/apps/issue-radar/investigation")
        req.json = AsyncMock(return_value={
            "owner": "o", "repo": "r", "number": True, "status": "investigating",
        })
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "write_investigation", writer),
        ):
            resp = await routes._handle_put_investigation(req)
        self.assertEqual(resp.status, 400)
        writer.assert_not_called()


class TestMalformedRequestShapes(unittest.IsolatedAsyncioTestCase):
    """Malformed bodies must be 400s, not 500s, and must not start work."""

    def _generate(self, body: dict):
        req = make_mocked_request("POST", "/api/apps/issue-radar/tagging")
        req.json = AsyncMock(return_value=body)
        return req

    async def test_non_string_owner_is_a_400_not_a_crash(self):
        # `(body.get("owner") or "").strip()` raised AttributeError on a truthy
        # non-string, which the gateway surfaced as a 500.
        for bad in (1, [], {}, True):
            resp = await routes._handle_generate_tagging(
                self._generate({"owner": bad, "repo": "r"})
            )
            self.assertEqual(resp.status, 400, bad)

        req = make_mocked_request("POST", "/api/apps/issue-radar/labels/apply-bulk")
        req.json = AsyncMock(return_value={
            "owner": 1, "repo": "r", "changes": [{"number": 1, "add": ["bug"]}],
        })
        resp = await routes._handle_labels_apply_bulk(req)
        self.assertEqual(resp.status, 400)

    async def test_empty_numbers_array_analyses_nothing(self):
        # An explicit empty list means "these issues (none)". Treating it as an
        # omission started a whole automatic batch the caller never asked for.
        compute = AsyncMock()
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_labels_cache", return_value=[
                {"name": "bug", "color": "ee0000", "description": ""},
            ]),
            mock.patch.object(store, "read_issues_cache", return_value=[
                {"number": 1, "labels": [], "title": "t", "body": ""},
            ]),
            mock.patch.object(store, "read_tagging_cache", return_value=None),
            mock.patch.object(routes, "_compute_tagging_suggestions", new=compute),
        ):
            resp = await routes._handle_generate_tagging(
                self._generate({"owner": "o", "repo": "r", "numbers": []})
            )
        self.assertEqual(resp.status, 200)
        compute.assert_not_called()
        self.assertEqual(json.loads(resp.body)["analyzed"], [])


class TestBulkApplyMergesDuplicateEntries(unittest.IsolatedAsyncioTestCase):
    """Two entries for the same issue must be merged, not silently halved.

    Skipping the duplicate dropped its labels while still reporting success — the
    caller was told about a write that never happened."""

    LABELS = [
        {"name": "bug", "color": "ee0000", "description": ""},
        {"name": "docs", "color": "0000ee", "description": ""},
    ]

    async def test_duplicate_numbers_are_merged(self):
        calls: list[tuple[int, list[str]]] = []

        def fake_add(owner, repo, number, names):
            calls.append((number, list(names)))
            return [{"name": n, "color": "ee0000", "description": ""} for n in names]

        req = make_mocked_request("POST", "/api/apps/issue-radar/labels/apply-bulk")
        req.json = AsyncMock(return_value={
            "owner": "o", "repo": "r", "changes": [
                {"number": 7, "add": ["bug"]},
                {"number": 7, "add": ["docs", "bug"]},
            ],
        })
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=self.LABELS)),
            mock.patch.object(store, "issue_write_lock", lambda *a, **k: contextlib.nullcontext()),
            mock.patch.object(gh, "add_issue_labels", side_effect=fake_add),
            mock.patch.object(store, "apply_label_change_to_caches"),
            mock.patch.object(store, "drop_tagging_suggestions"),
        ):
            resp = await routes._handle_labels_apply_bulk(req)
        self.assertEqual(resp.status, 200)
        # One request per issue, carrying the union of both entries' labels.
        self.assertEqual(calls, [(7, ["bug", "docs"])])


class TestCacheFailureDoesNotFailTheWrite(unittest.IsolatedAsyncioTestCase):
    """A local cache error after a successful GitHub write must not be reported
    as a failed apply — the labels ARE live, and saying otherwise sends the user
    to re-apply something that already happened."""

    LABELS = [{"name": "bug", "color": "ee0000", "description": ""}]

    async def test_bulk_apply_still_reports_success(self):
        req = make_mocked_request("POST", "/api/apps/issue-radar/labels/apply-bulk")
        req.json = AsyncMock(return_value={
            "owner": "o", "repo": "r", "changes": [{"number": 7, "add": ["bug"]}],
        })
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=self.LABELS)),
            mock.patch.object(store, "issue_write_lock", lambda *a, **k: contextlib.nullcontext()),
            mock.patch.object(gh, "add_issue_labels", return_value=self.LABELS),
            mock.patch.object(
                store, "apply_label_change_to_caches", side_effect=OSError("disk full")
            ),
            mock.patch.object(store, "drop_tagging_suggestions"),
        ):
            resp = await routes._handle_labels_apply_bulk(req)
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual([r["number"] for r in body["applied"]], [7])
        self.assertEqual(body["failed"], [])

    async def test_single_apply_still_reports_success(self):
        req = make_mocked_request("POST", "/api/apps/issue-radar/labels/apply")
        req.json = AsyncMock(return_value={
            "owner": "o", "repo": "r", "number": 7, "add": ["bug"],
        })
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=self.LABELS)),
            mock.patch.object(store, "issue_write_lock", lambda *a, **k: contextlib.nullcontext()),
            mock.patch.object(gh, "add_issue_labels", return_value=self.LABELS),
            mock.patch.object(
                store, "apply_label_change_to_caches", side_effect=OSError("disk full")
            ),
        ):
            resp = await routes._handle_labels_apply(req)
        self.assertEqual(resp.status, 200)


class TestAddSettingLabel(unittest.TestCase):
    """Appending a triage-label role happens server-side, under the config lock.

    The settings PUT replaces the whole document, so a client read-modify-write can
    only serialize ITSELF: two dashboard tabs each read the same settings, both
    issue a full replacement, and the later write permanently drops the other's
    label. No amount of client-side chaining fixes that — the read and the write
    have to be one critical section for every caller."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        store.add_connected_repo("o", "r", root=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_appends_without_touching_other_roles(self):
        store.write_repo_settings("o", "r", {
            "triage_labels": ["existing"], "unlabeled_is_untriaged": False,
            "good_first_issue_labels": ["starter"], "notify_on_new_issue": True,
        }, root=self.tmp)
        got = store.add_setting_label("o", "r", "triage_labels", "needs-triage", root=self.tmp)
        self.assertEqual(got["triage_labels"], ["existing", "needs-triage"])
        # Every unrelated preference survives — this is the whole point.
        self.assertEqual(got["good_first_issue_labels"], ["starter"])
        self.assertFalse(got["unlabeled_is_untriaged"])
        self.assertTrue(got["notify_on_new_issue"])

    def test_is_idempotent(self):
        store.add_setting_label("o", "r", "triage_labels", "dup", root=self.tmp)
        got = store.add_setting_label("o", "r", "triage_labels", "dup", root=self.tmp)
        self.assertEqual(got["triage_labels"], ["dup"])

    def test_unknown_role_and_unconnected_repo_raise(self):
        with self.assertRaises(ValueError):
            store.add_setting_label("o", "r", "not_a_role", "x", root=self.tmp)
        with self.assertRaises(KeyError):
            store.add_setting_label("o", "nope", "triage_labels", "x", root=self.tmp)

    def test_concurrent_appends_to_different_roles_both_survive(self):
        import threading

        start = threading.Barrier(2)
        errors: list[BaseException] = []

        def append(role, labels):
            def run():
                try:
                    start.wait(timeout=5)
                    for name in labels:
                        store.add_setting_label("o", "r", role, name, root=self.tmp)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
            return run

        triage = [f"t{i}" for i in range(15)]
        first = [f"f{i}" for i in range(15)]
        threads = [
            threading.Thread(target=append("triage_labels", triage)),
            threading.Thread(target=append("good_first_issue_labels", first)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])

        final = store.read_repo_settings("o", "r", self.tmp)
        # A lost update shows up as a missing label in either role.
        self.assertEqual(sorted(final["triage_labels"]), sorted(triage))
        self.assertEqual(sorted(final["good_first_issue_labels"]), sorted(first))


class TestRationaleCitationStripping(unittest.TestCase):
    """A parenthetical citation must be removed whole, not ref-by-ref.

    Matching each ``#12`` individually left the opening fragment behind, so
    "crashes (see #12, #34)" became "crashes (see" — a dangling bracket shown to
    the user as the reason."""

    def test_parenthetical_citation_is_removed_as_a_unit(self):
        self.assertEqual(routes._short_rationale("crashes (see #12, #34)"), "crashes")
        self.assertEqual(
            routes._short_rationale("Several crashes reported (#12, #34)"),
            "Several crashes reported",
        )
        self.assertEqual(
            routes._short_rationale("six issues touch login (e.g. #7)"),
            "six issues touch login",
        )

    def test_bare_references_outside_brackets_still_go(self):
        self.assertNotIn("#", routes._short_rationale("see #12 #34 in the tracker"))

    def test_no_dangling_bracket_is_ever_left(self):
        for text in ("crashes (see #12, #34)", "x (cf. #9)", "y (#1; #2)", "(#12, #34)"):
            got = routes._short_rationale(text)
            self.assertNotIn("(", got, text)
            self.assertNotIn(")", got, text)


class TestSettingsRevisionConflict(unittest.TestCase):
    """A full-document PUT built on a stale snapshot must be REFUSED.

    The append lock alone is not enough: a settings tab reads S, `/settings/role`
    appends a label (S+1), then the tab PUTs its copy of S — replacing every field
    and permanently deleting the appended label. Locking cannot detect that,
    because the PUT is a legitimate single write; only a revision check can."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        store.add_connected_repo("o", "r", root=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_append_then_stale_put_is_refused(self):
        read = store.read_repo_settings("o", "r", self.tmp)
        # Another tab appends while this one holds `read`.
        store.add_setting_label("o", "r", "triage_labels", "needs-triage", root=self.tmp)

        with self.assertRaises(store.SettingsConflict) as ctx:
            store.write_repo_settings(
                "o", "r", {**read, "notify_on_new_issue": True},
                expected_revision=read["revision"], root=self.tmp,
            )
        # The exception carries the current settings so the caller can re-read.
        self.assertIn("needs-triage", ctx.exception.current["triage_labels"])
        # And nothing was lost.
        self.assertIn("needs-triage", store.read_repo_settings("o", "r", self.tmp)["triage_labels"])

    def test_a_current_put_succeeds_and_bumps_the_revision(self):
        read = store.read_repo_settings("o", "r", self.tmp)
        saved = store.write_repo_settings(
            "o", "r", {**read, "notify_on_new_issue": True},
            expected_revision=read["revision"], root=self.tmp,
        )
        self.assertEqual(saved["revision"], read["revision"] + 1)
        self.assertTrue(saved["notify_on_new_issue"])

    def test_append_bumps_the_revision_so_the_next_put_can_detect_it(self):
        before = store.read_repo_settings("o", "r", self.tmp)["revision"]
        after = store.add_setting_label("o", "r", "triage_labels", "x", root=self.tmp)
        self.assertEqual(after["revision"], before + 1)
        # Idempotent re-append must NOT bump — nothing changed to conflict with.
        again = store.add_setting_label("o", "r", "triage_labels", "x", root=self.tmp)
        self.assertEqual(again["revision"], after["revision"])

    def test_omitting_the_revision_keeps_the_old_unchecked_behaviour(self):
        store.add_setting_label("o", "r", "triage_labels", "y", root=self.tmp)
        # No expected_revision -> no conflict check (used by callers that
        # legitimately want last-writer-wins).
        saved = store.write_repo_settings("o", "r", {"triage_labels": []}, root=self.tmp)
        self.assertEqual(saved["triage_labels"], [])


class TestSettingsConflictRoute(unittest.IsolatedAsyncioTestCase):
    """PUT /settings answers 409 (not a silent overwrite) on a stale revision."""

    async def test_stale_revision_is_a_409_carrying_the_current_settings(self):
        current = {**store.DEFAULT_REPO_SETTINGS, "revision": 5,
                   "triage_labels": ["needs-triage"]}
        req = make_mocked_request("PUT", "/api/apps/issue-radar/settings")
        req.json = AsyncMock(return_value={
            "owner": "o", "repo": "r",
            "settings": {**store.DEFAULT_REPO_SETTINGS, "revision": 4},
        })
        with mock.patch.object(
            store, "write_repo_settings", side_effect=store.SettingsConflict(current)
        ):
            resp = await routes._handle_put_settings(req)
        self.assertEqual(resp.status, 409)
        body = json.loads(resp.body)
        # The client needs the newer document to recover from this.
        self.assertEqual(body["settings"]["triage_labels"], ["needs-triage"])

    async def test_the_revision_the_client_read_is_forwarded(self):
        writer = mock.Mock(return_value=store.DEFAULT_REPO_SETTINGS)
        req = make_mocked_request("PUT", "/api/apps/issue-radar/settings")
        req.json = AsyncMock(return_value={
            "owner": "o", "repo": "r",
            "settings": {**store.DEFAULT_REPO_SETTINGS, "revision": 7},
        })
        with mock.patch.object(store, "write_repo_settings", writer):
            resp = await routes._handle_put_settings(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(writer.call_args.kwargs["expected_revision"], 7)


class TestLabelsCacheLock(unittest.TestCase):
    """Creating labels from two clients must not lose one from the cache.

    ``add_label_to_cache`` is a read-modify-write, and client-side serialization
    cannot help across tabs (separate processes). A lost append leaves a label that
    exists on GitHub invisible in every picker until a manual refresh."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        store.write_labels_cache("o", "r", [], root=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_appends_all_survive(self):
        import threading

        start = threading.Barrier(2)
        errors: list[BaseException] = []

        def add(prefix):
            def run():
                try:
                    start.wait(timeout=5)
                    for i in range(15):
                        store.add_label_to_cache(
                            "o", "r",
                            {"name": f"{prefix}{i}", "color": "ee0000", "description": ""},
                            root=self.tmp,
                        )
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
            return run

        threads = [threading.Thread(target=add("a")), threading.Thread(target=add("b"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])

        cached = store.read_labels_cache("o", "r", self.tmp)
        assert cached is not None
        names = {lab["name"] for lab in cached}
        expected = {f"{p}{i}" for p in ("a", "b") for i in range(15)}
        self.assertEqual(names, expected)


class TestRevisionIsMandatory(unittest.IsolatedAsyncioTestCase):
    """A PUT without a usable revision is rejected, not silently unchecked.

    An opt-out is indistinguishable from a stale client that never learned to send
    one, so "no revision means don't check" reopened exactly the hole the revision
    exists to close: that write could still erase newer settings."""

    def _req(self, settings: dict):
        req = make_mocked_request("PUT", "/api/apps/issue-radar/settings")
        req.json = AsyncMock(return_value={"owner": "o", "repo": "r", "settings": settings})
        return req

    async def test_missing_or_invalid_revision_is_a_400_and_writes_nothing(self):
        writer = mock.Mock()
        base = {k: v for k, v in store.DEFAULT_REPO_SETTINGS.items() if k != "revision"}
        for bad in ({}, {"revision": None}, {"revision": "3"}, {"revision": True},
                    {"revision": -1}, {"revision": 1.5}):
            with mock.patch.object(store, "write_repo_settings", writer):
                resp = await routes._handle_put_settings(self._req({**base, **bad}))
            self.assertEqual(resp.status, 400, bad)
        writer.assert_not_called()

    async def test_zero_is_a_valid_revision(self):
        # A never-written repo sits at revision 0; that must not read as "missing".
        writer = mock.Mock(return_value=store.DEFAULT_REPO_SETTINGS)
        with mock.patch.object(store, "write_repo_settings", writer):
            resp = await routes._handle_put_settings(
                self._req({**store.DEFAULT_REPO_SETTINGS, "revision": 0})
            )
        self.assertEqual(resp.status, 200)
        self.assertEqual(writer.call_args.kwargs["expected_revision"], 0)


class TestLabelsRefreshIsAtomic(unittest.TestCase):
    """The labels refresh must hold its lock across the fetch AND the write.

    Locking only the write loses data: the fetch returns a list from before a
    label was created, ``add_label_to_cache`` appends it, and the refresh's write
    replaces the cache with the pre-create snapshot — so a label that exists on
    GitHub is invisible in every picker until someone refreshes again."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lock_is_held_while_fetching(self):
        store.write_labels_cache("o", "r", [], root=self.tmp)
        held: list[bool] = []

        def fetch() -> list[dict]:
            # Re-acquiring from this thread would deadlock, so assert on the lock
            # FILE rather than trying to take it again.
            held.append(
                store.labels_cache_path("o", "r", self.tmp)
                .with_suffix(".json.lock").is_file()
            )
            return [{"name": "bug", "color": "ee0000", "description": ""}]

        got = store.refresh_labels_cache("o", "r", fetch, root=self.tmp)
        self.assertEqual(held, [True])
        self.assertEqual([lab["name"] for lab in got], ["bug"])
        cached = store.read_labels_cache("o", "r", self.tmp)
        assert cached is not None
        self.assertEqual([lab["name"] for lab in cached], ["bug"])

    def test_a_create_during_the_fetch_is_not_overwritten(self):
        # The interleaving the finding describes: the refresh is mid-fetch when a
        # create lands. With the lock held across both, the create must WAIT, so it
        # is applied on top of the refreshed list rather than being replaced by it.
        import threading

        store.write_labels_cache("o", "r", [], root=self.tmp)
        fetch_started = threading.Event()
        creator_done = threading.Event()
        errors: list[BaseException] = []

        def fetch() -> list[dict]:
            fetch_started.set()
            # Give the creator a real chance to interleave.
            creator_done.wait(timeout=1.0)
            return [{"name": "from-refresh", "color": "ee0000", "description": ""}]

        def create():
            try:
                fetch_started.wait(timeout=5)
                store.add_label_to_cache(
                    "o", "r",
                    {"name": "brand-new", "color": "00ee00", "description": ""},
                    root=self.tmp,
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                creator_done.set()

        t = threading.Thread(target=create)
        t.start()
        store.refresh_labels_cache("o", "r", fetch, root=self.tmp)
        t.join(timeout=10)
        self.assertEqual(errors, [])

        cached = store.read_labels_cache("o", "r", self.tmp)
        assert cached is not None
        names = {lab["name"] for lab in cached}
        # Both survive: the refresh's list, plus the label created around it.
        self.assertIn("from-refresh", names)
        self.assertIn("brand-new", names)


class TestSettingsRouteRejectsMalformedOwner(unittest.IsolatedAsyncioTestCase):
    """Non-string owner/repo on the settings routes must be 400, not 500.

    `(body.get("owner") or "").strip()` raises AttributeError on a truthy
    non-string, which the gateway surfaces as a 500. `_str_field` fixed it, but no
    settings-route test covered it — so reverting the fix stayed green."""

    async def _put(self, body: dict):
        req = make_mocked_request("PUT", "/api/apps/issue-radar/settings")
        req.json = AsyncMock(return_value=body)
        return await routes._handle_put_settings(req)

    async def _role(self, body: dict):
        req = make_mocked_request("POST", "/api/apps/issue-radar/settings/role")
        req.json = AsyncMock(return_value=body)
        return await routes._handle_add_settings_label(req)

    async def test_put_rejects_non_string_owner_or_repo_without_touching_the_store(self):
        writer = mock.Mock()
        settings = {**store.DEFAULT_REPO_SETTINGS, "revision": 0}
        with mock.patch.object(store, "write_repo_settings", writer):
            for owner, repo in ((1, "r"), ([], "r"), ({}, "r"), (True, "r"),
                                ("o", 1), ("o", []), ("o", {})):
                resp = await self._put({"owner": owner, "repo": repo, "settings": settings})
                self.assertEqual(resp.status, 400, (owner, repo))
        writer.assert_not_called()

    async def test_role_route_rejects_non_string_fields_without_touching_the_store(self):
        adder = mock.Mock()
        with mock.patch.object(store, "add_setting_label", adder):
            for body in (
                {"owner": 1, "repo": "r", "role": "triage_labels", "label": "x"},
                {"owner": "o", "repo": [], "role": "triage_labels", "label": "x"},
                {"owner": "o", "repo": "r", "role": 5, "label": "x"},
                {"owner": "o", "repo": "r", "role": "triage_labels", "label": {}},
            ):
                resp = await self._role(body)
                self.assertEqual(resp.status, 400, body)
        adder.assert_not_called()


class TestRefreshPathsUseTheAtomicHelper(unittest.IsolatedAsyncioTestCase):
    """Every refresh call path must go through its fetch-and-write-under-one-lock
    helper.

    The helpers have their own tests, but those stay green if a CALL SITE is
    reverted to fetch-then-write — which restores exactly the race the helpers
    exist to close. These tests pin the wiring: `refresh_*_cache` must be the
    thing that runs, and the unlocked `write_*_cache` must not."""

    ISSUES = [{"number": 7, "title": "t", "labels": [], "created_at": "2026-07-01T00:00:00Z"}]
    LABELS = [{"name": "bug", "color": "ee0000", "description": ""}]

    async def test_issues_route_refresh_uses_refresh_issues_cache(self):
        refreshed = mock.Mock(side_effect=lambda o, r, fetch, **kw: fetch())
        unlocked = mock.Mock()
        req = make_mocked_request("GET", "/api/apps/issue-radar/issues?owner=o&repo=r&refresh=1")
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "refresh_issues_cache", refreshed),
            mock.patch.object(store, "write_issues_cache", unlocked),
            mock.patch.object(gh, "list_open_issues", return_value=self.ISSUES),
        ):
            resp = await routes._handle_issues(req)
        self.assertEqual(resp.status, 200)
        refreshed.assert_called_once()
        unlocked.assert_not_called()

    async def test_labels_route_refresh_uses_refresh_labels_cache(self):
        refreshed = mock.Mock(side_effect=lambda o, r, fetch, **kw: fetch())
        unlocked = mock.Mock()
        req = make_mocked_request("GET", "/api/apps/issue-radar/labels?owner=o&repo=r&refresh=1")
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "refresh_labels_cache", refreshed),
            mock.patch.object(store, "write_labels_cache", unlocked),
            mock.patch.object(gh, "list_repo_labels", return_value=self.LABELS),
        ):
            resp = await routes._handle_labels(req)
        self.assertEqual(resp.status, 200)
        refreshed.assert_called_once()
        unlocked.assert_not_called()

    async def test_ai_label_loader_uses_refresh_labels_cache_on_a_miss(self):
        refreshed = mock.Mock(side_effect=lambda o, r, fetch, **kw: fetch())
        unlocked = mock.Mock()
        with (
            mock.patch.object(store, "read_labels_cache", return_value=None),
            mock.patch.object(store, "refresh_labels_cache", refreshed),
            mock.patch.object(store, "write_labels_cache", unlocked),
            mock.patch.object(gh, "list_repo_labels", return_value=self.LABELS),
        ):
            got = await routes._load_labels_for_ai("o", "r")
        self.assertEqual(got, self.LABELS)
        refreshed.assert_called_once()
        unlocked.assert_not_called()

    async def test_the_tagging_loader_uses_refresh_issues_cache_when_refreshing(self):
        refreshed = mock.Mock(side_effect=lambda o, r, fetch, **kw: fetch())
        read = mock.Mock(return_value=self.ISSUES)
        with (
            mock.patch.object(store, "read_issues_cache", read),
            mock.patch.object(store, "refresh_issues_cache", refreshed),
            mock.patch.object(gh, "list_open_issues", return_value=self.ISSUES),
        ):
            await routes._load_open_issues_for_reco("o", "r", refresh=True)
        refreshed.assert_called_once()
        read.assert_not_called()


class TestStateChangeCacheLock(unittest.TestCase):
    """Two concurrent close/reopen writes must not resurrect a removed issue.

    ``apply_state_change_to_caches`` drops the issue from the list it left, which
    is a read-modify-write. Without the shared lock, two threads read the same
    document and each writes back its own copy — so one of the two removals is
    undone and a closed issue reappears as open. The existing concurrency test
    covers LABEL patches only, so this path was unguarded by tests."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        store.write_issues_cache(
            "o", "r", [{"number": n, "labels": [], "state": "open"} for n in range(1, 41)],
            root=self.tmp, state="open",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_disjoint_state_changes_both_stick(self):
        import threading

        start = threading.Barrier(2)
        errors: list[BaseException] = []

        def close(numbers):
            def run():
                try:
                    start.wait(timeout=5)
                    for n in numbers:
                        store.apply_state_change_to_caches(
                            "o", "r", n, "closed", "completed", root=self.tmp
                        )
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
            return run

        threads = [
            threading.Thread(target=close(range(1, 21))),
            threading.Thread(target=close(range(21, 41))),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])

        cached = store.read_issues_cache("o", "r", self.tmp, state="open")
        assert cached is not None
        # Every close must have stuck. A lost update resurrects an issue here.
        self.assertEqual([i["number"] for i in cached], [], "resurrected closed issues")


class TestSameIssueApplyIsSerialized(unittest.TestCase):
    """One issue's GitHub write and its cache patch happen as one ordered step.

    Two concurrent applies to the same issue each get an authoritative label set
    back, but nothing ordered the two cache patches — so the SECOND response could
    be written first, leaving the cache missing whatever the later mutation added
    until the next refresh."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_lock_is_held_across_the_write_and_the_patch(self):
        import threading

        # Records the order of (enter, patch) pairs. With the lock held across
        # both, they must never interleave: no patch may land between another
        # call's write and its own patch.
        events: list[str] = []
        gate = threading.Event()

        def fake_add(owner, repo, number, names):
            events.append(f"write:{names[0]}")
            # Give the other thread every chance to interleave.
            gate.wait(timeout=0.5)
            return [{"name": names[0], "color": "ee0000", "description": ""}]

        def fake_patch(owner, repo, number, labels, *, root=None):
            events.append(f"patch:{labels[0]['name']}")

        # Bind the REAL lock before patching, or the replacement resolves back to
        # itself and recurses.
        real_lock = store.issue_write_lock
        tmp = self.tmp

        with (
            mock.patch.object(store, "issue_write_lock",
                              lambda o, r, n, root=None: real_lock(o, r, n, tmp)),
            mock.patch.object(gh, "add_issue_labels", side_effect=fake_add),
            mock.patch.object(store, "apply_label_change_to_caches", side_effect=fake_patch),
        ):
            threads = [
                threading.Thread(target=routes._apply_label_change,
                                 args=("o", "r", 7, ["a"], [])),
                threading.Thread(target=routes._apply_label_change,
                                 args=("o", "r", 7, ["b"], [])),
            ]
            for t in threads:
                t.start()
            gate.set()
            for t in threads:
                t.join(timeout=30)

        self.assertEqual(len(events), 4, events)
        # Each write is immediately followed by ITS OWN patch.
        self.assertEqual(events[0].split(":")[1], events[1].split(":")[1], events)
        self.assertEqual(events[2].split(":")[1], events[3].split(":")[1], events)

    def test_different_issues_are_not_serialized_against_each_other(self):
        # Per-issue, so unrelated applies still run concurrently.
        with store.issue_write_lock("o", "r", 7, self.tmp):
            with store.issue_write_lock("o", "r", 8, self.tmp):
                pass  # would deadlock if the lock were per-repo


class TestNoOpRemovalRepairsTheCache(unittest.TestCase):
    """A removals-only change where every label was already absent must still
    repair the cache, and must do it inside the per-issue lock.

    GitHub 404s a label that is not on the issue, so it tells us nothing about the
    remaining set. Skipping the cache patch in that case left stale labels
    surviving reloads; doing the re-read after the lock released reopened the
    ordering hole the lock exists to close."""

    LABELS = [{"name": "bug", "color": "ee0000", "description": ""}]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_authoritative_labels_are_read_and_cached_under_the_lock(self):
        patched: list[list[dict]] = []
        held: list[bool] = []
        real_lock = store.issue_write_lock
        tmp = self.tmp

        def fake_detail(owner, repo, number, **kw):
            # Must run INSIDE the lock — the whole point of moving it in here.
            held.append(
                (store.repo_data_dir(owner, repo, tmp) / f"issue-{number}.write.lock").is_file()
            )
            return {"labels": self.LABELS}

        with (
            mock.patch.object(store, "issue_write_lock",
                              lambda o, r, n, root=None: real_lock(o, r, n, tmp)),
            mock.patch.object(gh, "remove_issue_label", return_value=None),
            mock.patch.object(gh, "get_issue_detail", side_effect=fake_detail),
            mock.patch.object(store, "apply_label_change_to_caches",
                              side_effect=lambda o, r, n, labels, **kw: patched.append(labels)),
        ):
            got = routes._apply_label_change("o", "r", 7, [], ["already-gone"])

        self.assertEqual(got, self.LABELS)
        self.assertEqual(patched, [self.LABELS], "cache was not repaired")
        self.assertEqual(held, [True], "the re-read escaped the lock")

    def test_a_failed_re_read_returns_none_without_patching(self):
        patch = mock.Mock()
        real_lock = store.issue_write_lock
        tmp = self.tmp
        with (
            mock.patch.object(store, "issue_write_lock",
                              lambda o, r, n, root=None: real_lock(o, r, n, tmp)),
            mock.patch.object(gh, "remove_issue_label", return_value=None),
            mock.patch.object(gh, "get_issue_detail", side_effect=gh.GhCliError("boom")),
            mock.patch.object(store, "apply_label_change_to_caches", patch),
        ):
            got = routes._apply_label_change("o", "r", 7, [], ["already-gone"])
        # Better to leave the cache alone than to write a guess into it.
        self.assertIsNone(got)
        patch.assert_not_called()


class TestFallbackRereadRepairsTheCache(unittest.IsolatedAsyncioTestCase):
    """A successful handler-level retry must patch the caches, not just answer.

    `_apply_label_change` returns None when every removal was a no-op AND its
    in-lock re-read failed. The handler retries, and that retry used to only build
    the response: the caller saw the label gone while the cache still held it, so
    the next reload put it back — the exact bug a user reports as "the label
    came back by itself"."""

    LABELS = [{"name": "bug", "color": "d73a4a"}]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_the_retry_patches_the_cache_it_read_for(self):
        patched: list[list[dict]] = []
        reads: list[int] = []
        real_lock = store.issue_write_lock
        tmp = self.tmp

        def flaky_detail(owner, repo, number, **kw):
            reads.append(number)
            # Fail the in-lock read, succeed on the handler's retry.
            if len(reads) == 1:
                raise gh.GhCliError("transient")
            return {"labels": self.LABELS}

        req = make_mocked_request("POST", "/api/apps/issue-radar/labels/apply")
        req.json = AsyncMock(return_value={
            "owner": "o", "repo": "r", "number": 7, "remove": ["gone"],
        })
        with (
            mock.patch.object(store, "issue_write_lock",
                              lambda o, r, n, root=None: real_lock(o, r, n, tmp)),
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(routes, "_repo_can_write", return_value=True),
            mock.patch.object(routes, "_load_labels_for_ai",
                              new=AsyncMock(return_value=self.LABELS)),
            mock.patch.object(gh, "remove_issue_label", return_value=None),
            mock.patch.object(gh, "get_issue_detail", side_effect=flaky_detail),
            mock.patch.object(store, "drop_tagging_suggestions"),
            mock.patch.object(store, "apply_label_change_to_caches",
                              side_effect=lambda o, r, n, labels, **kw: patched.append(labels)),
        ):
            resp = await routes._handle_labels_apply(req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body)["labels"], self.LABELS)
        self.assertEqual(len(reads), 2, "the handler did not retry the read")
        self.assertEqual(patched, [self.LABELS], "the successful retry left the cache stale")


class TestQueueResponseBoundsAndCaps(unittest.IsolatedAsyncioTestCase):
    """`/tagging` serves the bulk cap, and bounds the titles it ships.

    Both come from Claude's advisory pass. The cap was duplicated as a client-side
    constant, which silently turns every large bulk apply into a 400 the day the
    backend value changes; the titles map was built over EVERY open issue while the
    only consumer resolves at most one example title per recommendation, so a repo
    with a large backlog shipped hundreds of KB nothing reads."""

    def _req(self):
        return make_mocked_request("GET", "/api/apps/issue-radar/tagging?owner=o&repo=r")

    async def _body(self, issues: list[dict]) -> dict:
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_issues_cache", return_value=issues),
            mock.patch.object(store, "read_tagging_cache", return_value=None),
        ):
            resp = await routes._handle_get_tagging(self._req())
        self.assertEqual(resp.status, 200)
        return json.loads(resp.body)

    async def test_serves_the_bulk_cap(self):
        body = await self._body([{"number": 1, "labels": [], "title": "t"}])
        self.assertEqual(body["bulk_max"], routes._TAG_BULK_MAX)

    async def test_titles_are_bounded_to_what_an_example_can_cite(self):
        # A recommendation's `examples` are validated against the slice the model
        # was shown, so titles beyond it can never be rendered.
        issues = [
            {"number": n, "labels": ["bug"], "title": f"issue {n}"}
            for n in range(1, routes._RECO_ISSUE_SAMPLE + 51)
        ]
        body = await self._body(issues)
        self.assertEqual(len(body["titles"]), routes._RECO_ISSUE_SAMPLE)
        self.assertIn("1", body["titles"])
        self.assertNotIn(str(routes._RECO_ISSUE_SAMPLE + 50), body["titles"])
        # Counts still cover the WHOLE open set — they are a different question.
        self.assertEqual(body["label_counts"]["bug"], len(issues))
