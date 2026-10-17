"""Tests for Issue Radar's Tagging dashboard backend: the tagging queue.

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

Beside them sit the label-recommendation helpers: the short rationale, and a prompt
that presets no label naming convention. Then the first correctness controls, on the
queue's own paths: the tagging cache lock, ``GET /tagging``, ``POST /tagging`` and the
prune a single apply makes. The remaining controls are in ``test_issue_radar_tagging.py``.
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
from dashboard_owner_helpers import NoConfiguredOwner

from kiro_crew.apps.builtins.issue_radar.backend import github_client as gh
from kiro_crew.apps.builtins.issue_radar.backend import routes, store


def _merge(*args, ui_language: str = "", **kwargs):
    """``store.merge_tagging_suggestions`` with an AGREEING in-lock verifier.

    The verifier is required in production -- there is one caller and it always has
    a language to check against, so an optional one would only offer a way to write
    without the guard that makes writing safe. Most tests here are not about the
    language, so they pass a verifier that agrees: the guard is exercised on its
    pass path rather than bypassed. The tests that ARE about it pass their own,
    which `setdefault` leaves alone.
    """
    kwargs.setdefault("verify_language", lambda: ui_language)
    return store.merge_tagging_suggestions(*args, ui_language=ui_language, **kwargs)


class TestTaggingCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_absent_returns_none(self):
        self.assertIsNone(store.read_tagging_cache("o", "r", self.tmp))

    def test_merge_roundtrip_stamps_generated_at(self):
        res = _merge("o", "r", {"7": [{"name": "bug", "reason": "crash"}]}, root=self.tmp)
        self.assertEqual(res["suggestions"], {"7": [{"name": "bug", "reason": "crash"}]})
        self.assertTrue(res["generated_at"])
        got = store.read_tagging_cache("o", "r", self.tmp)
        assert got is not None
        self.assertEqual(got["suggestions"], {"7": [{"name": "bug", "reason": "crash"}]})

    def test_merge_keeps_other_issues_and_replaces_covered_one(self):
        _merge(
            "o",
            "r",
            {"7": [{"name": "bug", "reason": "a"}], "8": [{"name": "docs", "reason": "b"}]},
            root=self.tmp,
        )
        # Regenerating #7 must replace its stale proposal but leave #8 alone —
        # this is what lets the dashboard walk a long queue in slices.
        res = _merge("o", "r", {"7": [{"name": "question", "reason": "c"}]}, root=self.tmp)
        self.assertEqual(res["suggestions"]["7"], [{"name": "question", "reason": "c"}])
        self.assertEqual(res["suggestions"]["8"], [{"name": "docs", "reason": "b"}])

    def test_empty_list_is_persisted_not_dropped(self):
        # "Analysed, nothing applies" must be recorded, else the next batch would
        # keep handing back the same unlabelable issue forever.
        _merge("o", "r", {"9": []}, root=self.tmp)
        got = store.read_tagging_cache("o", "r", self.tmp)
        assert got is not None
        self.assertIn("9", got["suggestions"])
        self.assertEqual(got["suggestions"]["9"], [])

    def test_drop_removes_only_named_issues(self):
        _merge(
            "o",
            "r",
            {"7": [{"name": "bug", "reason": ""}], "8": [{"name": "docs", "reason": ""}]},
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

    # ── the language the `reason` prose was written in ────────────────────────
    #
    # Each cached entry carries prose the Tagging queue renders as a tooltip, so
    # the document has to record which language that prose is in. Without it a
    # dashboard language switch left every tooltip in the old language for good.

    def test_merge_stamps_the_language_it_was_generated_in(self):
        _merge(
            "o",
            "r",
            {"7": [{"name": "bug", "reason": "Absturz"}]},
            root=self.tmp,
            ui_language="de-DE",
        )
        got = store.read_tagging_cache("o", "r", self.tmp)
        assert got is not None
        self.assertEqual(got["ui_language"], "de-DE")

    def test_an_unstamped_document_reads_as_the_unconfigured_sentinel(self):
        # No schema bump was taken, so a document written before this field is
        # still a HIT. It has to read as "" — the same value an install with no
        # configured language resolves to — or the upgrade would silently discard
        # every existing suggestion.
        path = store.tagging_cache_path("o", "r", self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": store.TAGGING_CACHE_SCHEMA,
                    "owner": "o",
                    "repo": "r",
                    "suggestions": {"7": [{"name": "bug", "reason": "crash"}]},
                    "generated_at": "2026-08-18T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        got = store.read_tagging_cache("o", "r", self.tmp)
        assert got is not None
        self.assertEqual(got["ui_language"], "")
        self.assertEqual(got["suggestions"], {"7": [{"name": "bug", "reason": "crash"}]})

    def test_a_batch_in_a_new_language_replaces_instead_of_merging(self):
        _merge(
            "o",
            "r",
            {
                "7": [{"name": "bug", "reason": "reports a crash"}],
                "8": [{"name": "docs", "reason": "docs only"}],
            },
            root=self.tmp,
            ui_language="en",
        )
        # Accumulating across a switch would leave #8 English beside #7 Chinese,
        # with nothing on screen to say which row is which. A regenerate the user
        # can trigger beats a permanently mixed queue.
        res = _merge(
            "o",
            "r",
            {"7": [{"name": "bug", "reason": "Absturzbericht"}]},
            root=self.tmp,
            ui_language="de-DE",
        )
        self.assertEqual(list(res["suggestions"]), ["7"])
        # The document is re-stamped, which is what makes the new entries servable.
        got = store.read_tagging_cache("o", "r", self.tmp)
        assert got is not None
        self.assertEqual(got["ui_language"], "de-DE")

    def test_a_batch_in_the_same_language_still_accumulates(self):
        _merge(
            "o",
            "r",
            {"8": [{"name": "docs", "reason": "b"}]},
            root=self.tmp,
            ui_language="ja",
        )
        res = _merge(
            "o",
            "r",
            {"7": [{"name": "bug", "reason": "a"}]},
            root=self.tmp,
            ui_language="ja",
        )
        self.assertEqual(sorted(res["suggestions"]), ["7", "8"])

    # ── the in-lock language verifier ─────────────────────────────────────────
    #
    # The caller checks the language before calling, but that check and this write
    # are not atomic: a switch landing between them lets a stale generation replace
    # a newer-language one that already landed, and the replace semantics turn a
    # lost race into lost data. Re-checking inside the lock is the only place the
    # check and the write it authorises cannot be separated.

    def test_a_verifier_that_disagrees_refuses_the_write(self):
        _merge(
            "o",
            "r",
            {"8": [{"name": "docs", "reason": "aktuell"}]},
            root=self.tmp,
            ui_language="de-DE",
        )
        # The batch was generated under "en", but the configured language is now
        # "de-DE" -- so this write would replace the German entry with an English one.
        res = _merge(
            "o",
            "r",
            {"7": [{"name": "bug", "reason": "stale"}]},
            root=self.tmp,
            ui_language="en",
            verify_language=lambda: "de-DE",
        )
        self.assertTrue(res["stale_language"])
        # The refusal returns the UNTOUCHED document, not an empty one.
        self.assertEqual(list(res["suggestions"]), ["8"])
        got = store.read_tagging_cache("o", "r", self.tmp)
        assert got is not None
        self.assertEqual(list(got["suggestions"]), ["8"])
        self.assertEqual(got["ui_language"], "de-DE")

    def test_a_verifier_that_agrees_writes_normally(self):
        res = _merge(
            "o",
            "r",
            {"7": [{"name": "bug", "reason": "Absturz"}]},
            root=self.tmp,
            ui_language="de-DE",
            verify_language=lambda: "de-DE",
        )
        self.assertFalse(res["stale_language"])
        self.assertEqual(list(res["suggestions"]), ["7"])

    def test_drop_carries_the_language_through(
        self,
    ):  # `drop` rewrites the document. Losing the tag here would make every
        # surviving entry read as "generated with no language configured" and
        # become servable again after a switch.
        _merge(
            "o",
            "r",
            {"7": [{"name": "bug", "reason": "a"}], "8": [{"name": "docs", "reason": "b"}]},
            root=self.tmp,
            ui_language="de",
        )
        res = store.drop_tagging_suggestions("o", "r", [7], root=self.tmp)
        self.assertEqual(list(res["suggestions"]), ["8"])
        got = store.read_tagging_cache("o", "r", self.tmp)
        assert got is not None
        self.assertEqual(got["ui_language"], "de")

    def test_corrupt_json_reads_as_miss(self):
        path = store.tagging_cache_path("o", "r", self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(store.read_tagging_cache("o", "r", self.tmp))

    def test_normalizer_drops_junk_and_dedupes(self):
        got = store._normalize_tagging(
            {
                "7": [
                    {"name": "bug", "reason": "x"},
                    {"name": "bug", "reason": "dup"},
                    {"name": ""},
                    "nope",
                ],
                "not-a-number": [{"name": "bug"}],
                "-3": [{"name": "bug"}],
                "8": "not-a-list",
            }
        )
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
        for anchor in (
            "priority: high",
            "area: auth",
            "type: bug",
            "kind/bug",
            "Type: Bug",
            "A-diagnostics",
        ):
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
    with mock.patch.object(routes, "_run_oneshot_model", new=AsyncMock(return_value=payload_text)):
        return await routes._compute_tagging_suggestions(MagicMock(), "o", "r", LABELS, BATCH)


@pytest.mark.asyncio
async def test_valid_assignment_survives():
    got = await _compute(
        '{"assignments": [{"number": 7, "labels": '
        '[{"name": "bug", "reason": "reports a crash"}]}]}'
    )
    assert got == {"7": [{"name": "bug", "reason": "reports a crash"}]}


@pytest.mark.asyncio
async def test_label_not_defined_on_repo_is_dropped():
    # The prompt-injection guard: issue bodies are attacker-controlled, so an
    # invented label must not reach /labels/apply.
    got = await _compute(
        '{"assignments": [{"number": 7, "labels": '
        '[{"name": "P0-DROP-EVERYTHING", "reason": "x"}]}]}'
    )
    assert got == {}


@pytest.mark.asyncio
async def test_issue_outside_the_batch_is_dropped():
    got = await _compute(
        '{"assignments": [{"number": 999, "labels": ' '[{"name": "bug", "reason": "x"}]}]}'
    )
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
    assert names == ["bug", "docs", "question"][: routes._TAG_MAX_PER_ISSUE]
    assert len(names) <= routes._TAG_MAX_PER_ISSUE


@pytest.mark.asyncio
async def test_bare_string_labels_and_unparsable_output_degrade_gracefully():
    assert await _compute('{"assignments": [{"number": 7, "labels": ["docs"]}]}') == {
        "7": [{"name": "docs", "reason": ""}]
    }
    assert await _compute("I am afraid I cannot do that") == {}


def _bulk_request(body: dict):
    req = make_mocked_request("POST", "/api/apps/issue-radar/labels/apply-bulk")
    req.app["state"] = NoConfiguredOwner()
    req["user"] = "local-app"
    req["app"] = ""
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
        resp = await routes._handle_labels_apply_bulk(
            _bulk_request(
                {
                    "owner": "o",
                    "repo": "r",
                    "changes": [{"number": 7, "add": ["bug"]}, {"number": 8, "add": ["nope"]}],
                }
            )
        )
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
            store,
            "drop_tagging_suggestions",
            side_effect=lambda o, r, numbers, **kw: dropped.append(list(numbers)),
        ),
    ):
        resp = await routes._handle_labels_apply_bulk(
            _bulk_request(
                {
                    "owner": "o",
                    "repo": "r",
                    "changes": [{"number": 7, "add": ["bug"]}, {"number": 8, "add": ["docs"]}],
                }
            )
        )

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
        resp = await routes._handle_labels_apply_bulk(
            _bulk_request(
                {
                    "owner": "o",
                    "repo": "r",
                    "changes": [{"number": 7, "add": ["bug"]}],
                }
            )
        )
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
        (
            {
                "owner": "o",
                "repo": "r",
                "changes": [{"number": n, "add": ["bug"]} for n in range(routes._TAG_BULK_MAX + 2)],
            },
            400,
        ),
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
        _merge(
            "o",
            "r",
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
                # flake-ok: pre-existing 5 s barrier bound; an expiry raises into errors, which the test asserts empty, moved verbatim; tracked for a de-flake pass
                start.wait(timeout=5)
                for n in range(100, 120):
                    _merge("o", "r", {str(n): [{"name": "docs", "reason": ""}]}, root=self.tmp)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        def prune():
            try:
                # flake-ok: pre-existing 5 s barrier bound; an expiry raises into errors, which the test asserts empty, moved verbatim; tracked for a de-flake pass
                start.wait(timeout=5)
                for n in range(1, 21):
                    store.drop_tagging_suggestions("o", "r", [n], root=self.tmp)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=merge), threading.Thread(target=prune)]
        for t in threads:
            t.start()
        for t in threads:
            # flake-ok: pre-existing 30 s join bound whose expiry is silent before the errors check, moved verbatim; tracked for a de-flake pass
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
        {
            "number": 7,
            "title": "crash",
            "url": "https://github.com/o/r/issues/7",
            "labels": [],
            "created_at": "2026-07-02T00:00:00Z",
            "author": "alice",
        },
        {
            "number": 8,
            "title": "typo",
            "url": "https://github.com/o/r/issues/8",
            "labels": ["bug"],
            "created_at": "2026-07-03T00:00:00Z",
            "author": "bob",
        },
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
        cached = {
            "suggestions": {
                "7": [{"name": "bug", "reason": "x"}],
                "8": [{"name": "docs", "reason": "y"}],
            },
            "generated_at": "2026-07-26T00:00:00Z",
        }
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_issues_cache", return_value=self.ISSUES),
            mock.patch.object(store, "read_tagging_cache", return_value=cached),
        ):
            resp = await routes._handle_get_tagging(self._req())
        body = json.loads(resp.body)
        # #8 picked up a label, so its cached proposal is moot.
        self.assertEqual(list(body["suggestions"]), ["7"])

    async def test_serves_suggestions_generated_in_the_current_language(self):
        cached = {
            "suggestions": {"7": [{"name": "bug", "reason": "Absturz"}]},
            "generated_at": "2026-08-18T00:00:00Z",
            "ui_language": "de-DE",
        }
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_issues_cache", return_value=self.ISSUES),
            mock.patch.object(store, "read_tagging_cache", return_value=cached),
            mock.patch.object(routes, "_ui_language", return_value="de-DE"),
        ):
            resp = await routes._handle_get_tagging(self._req())
        self.assertEqual(list(json.loads(resp.body)["suggestions"]), ["7"])

    async def test_withholds_suggestions_written_before_a_language_switch(self):
        # THE REPORTED DEFECT. Each `reason` is rendered as a tooltip, and the
        # frontend wraps it in a LOCALIZED template -- so a stale-language reason
        # produces a half-translated tooltip rather than a plainly foreign one.
        # Serving nothing offers the regenerate the user can act on.
        cached = {
            "suggestions": {"7": [{"name": "bug", "reason": "reports a crash"}]},
            "generated_at": "2026-08-18T00:00:00Z",
            "ui_language": "en",
        }
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_issues_cache", return_value=self.ISSUES),
            mock.patch.object(store, "read_tagging_cache", return_value=cached),
            mock.patch.object(routes, "_ui_language", return_value="de-DE"),
        ):
            resp = await routes._handle_get_tagging(self._req())
        body = json.loads(resp.body)
        self.assertEqual(body["suggestions"], {})
        # The QUEUE itself is unaffected -- only the prose is stale, so the issue
        # must still be offered for tagging.
        self.assertEqual(body["untagged"], [7])

    async def test_an_unstamped_cache_still_serves_an_unconfigured_install(
        self,
    ):  # The upgrade path: a document written before the tag existed, on an install
        # that never set a language. Both sides read "", so nothing is discarded.
        cached = {
            "suggestions": {"7": [{"name": "bug", "reason": "reports a crash"}]},
            "generated_at": "2026-08-18T00:00:00Z",
        }
        with (
            mock.patch.object(store, "is_repo_connected", return_value=True),
            mock.patch.object(store, "read_issues_cache", return_value=self.ISSUES),
            mock.patch.object(store, "read_tagging_cache", return_value=cached),
            mock.patch.object(routes, "_ui_language", return_value=""),
        ):
            resp = await routes._handle_get_tagging(self._req())
        self.assertEqual(list(json.loads(resp.body)["suggestions"]), ["7"])

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
                    .with_suffix(".json.lock")
                    .is_file()
                )
                return [{"number": 1, "labels": []}]

            store.refresh_issues_cache("o", "r", fetch, root=tmp, state="open")
            self.assertEqual(held, [True])
            self.assertEqual(
                store.read_issues_cache("o", "r", tmp, state="open"), [{"number": 1, "labels": []}]
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_serves_label_counts_and_titles_over_the_open_set(self):
        # Counts and titles come over the open set, not the frontend's shared
        # issue list, which follows the user's open/closed filter — deriving from
        # it would report closed counts as open ones.
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
        {
            "number": n,
            "title": f"issue {n}",
            "body": "",
            "labels": [],
            "created_at": f"2026-07-{n:02d}T00:00:00Z",
        }
        for n in range(1, 6)
    ]
    LABELS = [{"name": "bug", "color": "ee0000", "description": ""}]

    def _req(self, body: dict):
        req = make_mocked_request("POST", "/api/apps/issue-radar/tagging")
        req.app["state"] = NoConfiguredOwner()
        req["user"] = "local-app"
        req["app"] = ""
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
                store,
                "merge_tagging_suggestions",
                side_effect=lambda o, r, batch, **kw: (
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

    async def test_the_store_refusing_under_its_lock_claims_nothing_analysed(self):
        """The route's own pre-write check can pass and the store still refuse.

        Those two are not atomic, so a switch landing between them is caught only by
        the in-lock check. When it fires, the response must not claim a batch the
        store did not persist -- otherwise the client marks the slice done and the
        rows are never re-earned.
        """
        merged: list = []
        with contextlib.ExitStack() as stack:
            for p in self._patches({"1": [{"name": "bug", "reason": "x"}]}, None, merged):
                stack.enter_context(p)
            # Language holds across the route's own check...
            stack.enter_context(mock.patch.object(routes, "_ui_language", return_value="en"))
            # ...but the store reports it moved before the write.
            stack.enter_context(
                mock.patch.object(
                    store,
                    "merge_tagging_suggestions",
                    return_value={
                        "suggestions": {"9": [{"name": "docs", "reason": "neuer"}]},
                        "generated_at": "t",
                        "ui_language": "de-DE",
                        "stale_language": True,
                    },
                )
            )
            resp = await routes._handle_generate_tagging(self._req({"owner": "o", "repo": "r"}))
        assert resp.status == 200
        body = json.loads(resp.body)
        self.assertEqual(body["analyzed"], [])
        self.assertEqual(body["remaining"], 5)
        # Nothing comes back. The untouched document this refusal protected may
        # still be in the language the switch just left, and returning it would put
        # exactly the stale prose this route withholds into the client's cache --
        # the GET route gates on the language, so an error path that skips the gate
        # would reintroduce the defect through the back door.
        self.assertEqual(body["suggestions"], {})
        self.assertIsNone(body["generated_at"])

    async def test_a_generation_whose_language_held_is_written(
        self,
    ):  # The control: same path, language unchanged across the generation.
        merged: list = []
        with contextlib.ExitStack() as stack:
            for p in self._patches({"1": [{"name": "bug", "reason": "x"}]}, None, merged):
                stack.enter_context(p)
            stack.enter_context(mock.patch.object(routes, "_ui_language", return_value="de-DE"))
            resp = await routes._handle_generate_tagging(self._req({"owner": "o", "repo": "r"}))
        self.assertEqual(len(merged), 1)
        self.assertEqual(sorted(json.loads(resp.body)["analyzed"]), [1, 2, 3, 4, 5])

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
        req.app["state"] = NoConfiguredOwner()
        req["user"] = "local-app"
        req["app"] = ""
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
                store,
                "drop_tagging_suggestions",
                side_effect=lambda o, r, numbers, **kw: dropped.append(list(numbers)),
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
