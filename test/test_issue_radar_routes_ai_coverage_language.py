"""How Issue Radar's model-backed routes follow the dashboard language.

This file pins the output-language half of the AI routes: an empty tag leaves each
of the four one-shot prompts byte-identical and a set tag appends its directive
after the fenced untrusted block; the language keys the PR-summary fingerprint;
the store keeps each language's issue summary in its own partition and a label
edit clears them all; every compute path and handler threads the resolved tag
into its prompt and cache; and a browser's per-request hint steers the prose only
when nothing is configured, while the tagging routes ignore it. The per-route
behaviour the language rides on lives in ``test_issue_radar_routes_ai_coverage.py``
(``/issue-ai``, ``/pull-ai``) and
``test_issue_radar_routes_ai_coverage_recommendations_and_tagging.py``
(``/recommendations``, ``/tagging``), and the shared request builders and patch
helpers in ``issue_radar_routes_ai_coverage_helpers.py``.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

from issue_radar_routes_ai_coverage_helpers import (
    LABELS,
    _body,
    _connected,
    _get,
    _json_request,
    _oneshot,
    _sessions,
    _stream,
)

from kiro_crew.apps.builtins.issue_radar.backend import routes, store

# ── AI output-language localization ──────


class TestAiPromptLocalization(unittest.TestCase):
    """The four one-shot prompts localize their PROSE to the dashboard language.

    Two invariants: an EMPTY tag (the follow-the-browser sentinel) leaves each
    prompt BYTE-IDENTICAL to what unconfigured installs have always sent, and a
    non-empty tag appends the directive AFTER the fenced untrusted block, so
    issue/PR text cannot restate it as data."""

    DETAIL = {"number": 7, "title": "crash", "body": "boom", "labels": []}
    PR_DETAIL = {"number": 12, "title": "t", "body": "b", "state": "open"}
    RECO_ISSUES = [{"number": 7, "title": "t", "body": "b", "labels": ["bug"]}]
    TAG_ISSUES = [{"number": 7, "title": "t", "body": "b", "labels": []}]

    def test_an_empty_tag_leaves_the_issue_prompt_byte_identical(self):
        legacy = routes._build_ai_prompt("o", "r", self.DETAIL, LABELS, [])
        self.assertEqual(
            routes._build_ai_prompt("o", "r", self.DETAIL, LABELS, [], ui_language=""),
            legacy,
        )

    def test_a_tag_appends_the_language_line_to_the_issue_prompt(self):
        legacy = routes._build_ai_prompt("o", "r", self.DETAIL, LABELS, [])
        got = routes._build_ai_prompt("o", "r", self.DETAIL, LABELS, [], ui_language="ko")
        # Append-only: the fenced block and every instruction above it is
        # untouched, and the directive sits after the closing fence.
        self.assertTrue(got.startswith(legacy))
        tail = got[len(legacy) :]
        self.assertIn("BCP-47 tag ko", tail)
        self.assertIn('"summary"', tail)
        self.assertIn('"reason"', tail)
        self.assertIn("label names", tail)

    def test_an_empty_tag_leaves_the_pr_prompt_byte_identical(self):
        legacy = routes._build_pr_ai_prompt("o", "r", self.PR_DETAIL, [], [])
        self.assertEqual(
            routes._build_pr_ai_prompt("o", "r", self.PR_DETAIL, [], [], ui_language=""),
            legacy,
        )

    def test_a_tag_appends_the_language_line_to_the_pr_prompt(self):
        legacy = routes._build_pr_ai_prompt("o", "r", self.PR_DETAIL, [], [])
        got = routes._build_pr_ai_prompt("o", "r", self.PR_DETAIL, [], [], ui_language="ja")
        self.assertTrue(got.startswith(legacy))
        self.assertIn("BCP-47 tag ja", got[len(legacy) :])

    def test_an_empty_tag_leaves_the_reco_prompt_byte_identical(self):
        legacy = routes._build_reco_prompt("o", "r", LABELS, self.RECO_ISSUES)
        self.assertEqual(
            routes._build_reco_prompt("o", "r", LABELS, self.RECO_ISSUES, ui_language=""),
            legacy,
        )

    def test_a_tag_localizes_only_the_reco_rationale(self):
        legacy = routes._build_reco_prompt("o", "r", LABELS, self.RECO_ISSUES)
        got = routes._build_reco_prompt("o", "r", LABELS, self.RECO_ISSUES, ui_language="de")
        self.assertTrue(got.startswith(legacy))
        tail = got[len(legacy) :]
        self.assertIn("BCP-47 tag de", tail)
        self.assertIn('"rationale"', tail)
        # name/description become repo content via /labels/create when a
        # proposal is applied, so the directive must explicitly exempt them.
        self.assertIn('"name" and "description"', tail)

    def test_an_empty_tag_leaves_the_tagging_prompt_byte_identical(self):
        legacy = routes._build_tagging_prompt("o", "r", LABELS, self.TAG_ISSUES)
        self.assertEqual(
            routes._build_tagging_prompt("o", "r", LABELS, self.TAG_ISSUES, ui_language=""),
            legacy,
        )

    def test_a_tag_localizes_only_the_tagging_reason(self):
        # The fourth builder, and the one this class was missing: the
        # tagging queue renders each `reason` as a tooltip, so an unsteered prompt
        # left that tooltip English inside a localized UI.
        legacy = routes._build_tagging_prompt("o", "r", LABELS, self.TAG_ISSUES)
        got = routes._build_tagging_prompt("o", "r", LABELS, self.TAG_ISSUES, ui_language="zh-CN")
        self.assertTrue(got.startswith(legacy))
        tail = got[len(legacy) :]
        self.assertIn("BCP-47 tag zh-CN", tail)
        self.assertIn('"reason"', tail)
        # The directive lands AFTER the </issues> fence, so attacker-controlled
        # issue text cannot present itself as part of the instruction.
        self.assertNotIn("</issues>", tail)

    def test_the_tagging_directive_never_steers_the_label_names(self):
        # Names are intersected against the repo's real label set downstream, so a
        # translated name would be discarded as invented and the issue would come
        # back unlabelled. The shared directive exempts everything it does not
        # name, which is what makes this safe -- pinned because the failure is
        # silent (fewer suggestions, no error).
        got = routes._build_tagging_prompt("o", "r", LABELS, self.TAG_ISSUES, ui_language="zh-CN")
        tail = got[len(routes._build_tagging_prompt("o", "r", LABELS, self.TAG_ISSUES)) :]
        self.assertIn("Everything else is never", tail)
        self.assertNotIn('"name"', tail)


class TestPrAiFingerprintLanguage(unittest.TestCase):
    """The dashboard language is a summary INPUT, so it must key the cache."""

    DETAIL = {"number": 12, "state": "open", "head_sha": "a" * 40}

    def test_an_empty_tag_keeps_the_legacy_digest(self):
        # No one-time invalidation of every cached summary on upgrade.
        legacy = routes._pr_ai_fingerprint(self.DETAIL, [], [])
        self.assertEqual(routes._pr_ai_fingerprint(self.DETAIL, [], [], ui_language=""), legacy)

    def test_a_language_switch_changes_the_digest(self):
        base = routes._pr_ai_fingerprint(self.DETAIL, [], [])
        ko = routes._pr_ai_fingerprint(self.DETAIL, [], [], ui_language="ko")
        ja = routes._pr_ai_fingerprint(self.DETAIL, [], [], ui_language="ja")
        self.assertNotEqual(ko, base)
        self.assertNotEqual(ko, ja)


class TestIssueAiCachePartition(unittest.TestCase):
    """The REAL store must keep each language's summary in its own partition.

    A writer that shared one slot would make every open by a second-language
    browser a miss AND destroy the first browser's summary -- one model call per
    open, forever. Invisible to the route tests, which mock the store.
    """

    def test_two_languages_coexist_instead_of_evicting_each_other(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store.write_issue_ai_cache(
                "o",
                "r",
                7,
                {"summary": "ko text", "suggested_labels": []},
                root=root,
                ui_language="ko",
            )
            store.write_issue_ai_cache(
                "o",
                "r",
                7,
                {"summary": "ja text", "suggested_labels": []},
                root=root,
                ui_language="ja",
            )
            ko = store.read_issue_ai_cache("o", "r", 7, root=root, ui_language="ko")
            ja = store.read_issue_ai_cache("o", "r", 7, root=root, ui_language="ja")
            absent = store.read_issue_ai_cache("o", "r", 7, root=root, ui_language="de")
        assert ko is not None and ja is not None
        self.assertEqual(ko["summary"], "ko text")
        self.assertEqual(ja["summary"], "ja text")
        # A language with nothing generated is absent, never another's prose.
        self.assertIsNone(absent)

    def test_a_legacy_entry_is_the_unconfigured_partition(self):
        # Written before partitioning existed, at the unsuffixed path -- which is
        # exactly the partition an install that never set a language reads, so the
        # upgrade needs no migration.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = store.issue_ai_cache_path("o", "r", 7, root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"summary": "old", "suggested_labels": []}), encoding="utf-8"
            )
            got = store.read_issue_ai_cache("o", "r", 7, root=root)
        assert got is not None
        self.assertEqual(got["summary"], "old")

    def test_a_label_edit_clears_every_language(self):
        # The applied label changes what counts as "already on the issue", so the
        # suggestions are stale in EVERY language -- dropping only one partition
        # would leave another browser reading advice that predates the edit.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for lang in ("", "ko", "ja"):
                store.write_issue_ai_cache(
                    "o",
                    "r",
                    7,
                    {"summary": lang or "en", "suggested_labels": []},
                    root=root,
                    ui_language=lang,
                )
            # A neighbouring issue must survive: `issue-7-ai-*` must not match it.
            store.write_issue_ai_cache(
                "o", "r", 70, {"summary": "other", "suggested_labels": []}, root=root
            )
            store.delete_issue_ai_cache("o", "r", 7, root)
            gone = [
                store.read_issue_ai_cache("o", "r", 7, root=root, ui_language=lang)
                for lang in ("", "ko", "ja")
            ]
            neighbour = store.read_issue_ai_cache("o", "r", 70, root=root)
        self.assertEqual(gone, [None, None, None])
        assert neighbour is not None
        self.assertEqual(neighbour["summary"], "other")


class TestAiLanguageWiring(unittest.IsolatedAsyncioTestCase):
    """Each compute path threads the RESOLVED tag into the prompt it sends."""

    DETAIL = {"number": 7, "title": "crash", "body": "boom", "labels": []}

    async def test_compute_issue_ai_threads_the_language_it_was_given(self):
        with _oneshot('{"summary": "s"}') as model:
            await routes._compute_issue_ai(
                _get("issue-ai"), "o", "r", 7, self.DETAIL, LABELS, ui_language="ko"
            )
        prompt = model.call_args[0][2]
        self.assertIn("BCP-47 tag ko", prompt)

    async def test_issue_ai_handler_reads_and_writes_the_language_partition(self):
        # The route does not compare a stored tag -- it addresses the partition
        # for the resolved language, so another language's summary is absent here
        # rather than evicted. Dropping either wire regresses: reading the wrong
        # partition serves foreign prose, writing it destroys someone else's.
        compute = AsyncMock(return_value={"summary": "fresh", "suggested_labels": []})
        with (
            _connected(),
            mock.patch.object(routes, "_ui_language", return_value="ko"),
            mock.patch.object(store, "read_issue_ai_cache", return_value=None) as read,
            mock.patch.object(
                routes, "_load_detail_for_ai", new=AsyncMock(return_value={"number": 7})
            ),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=LABELS)),
            mock.patch.object(routes, "_compute_issue_ai", new=compute),
            mock.patch.object(store, "write_issue_ai_cache") as write,
        ):
            response = await routes._handle_issue_ai(
                _get("issue-ai", {"owner": "o", "repo": "r", "number": "7"})
            )
        payload = _body(response)
        self.assertFalse(payload["from_cache"])
        self.assertEqual(payload["summary"], "fresh")
        self.assertEqual(compute.call_args.kwargs["ui_language"], "ko")
        self.assertEqual(read.call_args.kwargs["ui_language"], "ko")
        self.assertEqual(write.call_args.kwargs["ui_language"], "ko")

    async def test_issue_ai_handler_serves_a_cache_entry_from_its_partition(self):
        cached = {"summary": "cached ko", "suggested_labels": []}
        with (
            _connected(),
            mock.patch.object(routes, "_ui_language", return_value="ko"),
            mock.patch.object(store, "read_issue_ai_cache", return_value=cached),
            _oneshot() as model,
        ):
            response = await routes._handle_issue_ai(
                _get("issue-ai", {"owner": "o", "repo": "r", "number": "7"})
            )
        payload = _body(response)
        self.assertTrue(payload["from_cache"])
        self.assertEqual(payload["summary"], "cached ko")
        model.assert_not_called()

    async def test_compute_pr_ai_threads_the_language_it_was_given(self):
        with _oneshot('{"summary": "s"}') as model:
            await routes._compute_pr_ai(
                _get("pull-ai"),
                "o",
                "r",
                12,
                {"number": 12, "state": "open"},
                [],
                [],
                ui_language="ja",
            )
        self.assertIn("BCP-47 tag ja", model.call_args[0][2])

    async def test_pull_ai_handler_feeds_one_resolved_tag_to_prompt_and_cache_key(self):
        # The handler resolves the tag ONCE and hands it to both the fingerprint
        # (cache key) and the model prompt — dropping either wire regresses:
        # prompt-only keeps serving stale-language cache, key-only re-keys the
        # cache but keeps generating English.
        detail = {"number": 12, "title": "t", "body": "b", "state": "open", "head_sha": "a" * 40}
        with (
            _connected(),
            mock.patch.object(routes, "_ui_language", return_value="ko"),
            mock.patch.object(
                store,
                "read_pr_detail_cache",
                return_value={"detail": detail, "timeline": [], "checks": []},
            ),
            mock.patch.object(store, "read_pr_ai_cache", return_value=None) as read_ai,
            mock.patch.object(store, "write_pr_ai_cache"),
            _oneshot('{"summary": "s"}') as model,
        ):
            request = _get("pull-ai", {"owner": "o", "repo": "r", "number": "12"})
            response = await routes._handle_pull_ai(request)
        self.assertEqual(response.status, 200)
        self.assertIn("BCP-47 tag ko", model.call_args[0][2])
        expected = routes._pr_ai_fingerprint(detail, [], [], ui_language="ko")
        self.assertEqual(read_ai.call_args.kwargs["fingerprint"], expected)

    async def test_compute_label_recommendations_threads_the_dashboard_language(self):
        state = _sessions()
        request = _json_request("POST", "recommendations", {"owner": "o", "repo": "r"}, state=state)
        with _stream('{"recommendations": []}') as stream:
            await routes._compute_label_recommendations(
                request, "o", "r", LABELS, [], ui_language="de"
            )
        prompt = stream.call_args[0][1]
        self.assertIn("BCP-47 tag de", prompt)


class TestBrowserLanguageHint(unittest.IsolatedAsyncioTestCase):
    """A browser's own resolved language reaches the prose prompts.

    The dashboard's default is "follow the browser", resolved client-side in the
    SPA's ``resolveLanguage()``; the backend reads ``Accept-Language`` nowhere, so
    before this every AI surface stayed English on an install that never set
    ``dashboard.language`` — a fully localized UI wrapped around an English card.
    The SPA now sends the tag it already resolved as a per-request, NON-persisted
    hint, and the backend consults it only when nothing is configured.
    """

    DETAIL = {"number": 7, "title": "crash", "body": "boom", "labels": []}

    # ── the resolver: precedence, and validation of a client-supplied value ──

    def test_a_hint_steers_the_prose_when_nothing_is_configured(self):
        # The case the whole change exists for: implicit locale, no config.
        with mock.patch.object(routes, "_ui_language", return_value=""):
            self.assertEqual(routes._resolve_ui_language("zh-CN"), "zh-CN")

    def test_an_explicit_configuration_outranks_a_conflicting_hint(self):
        # dashboard.language is a workspace-wide instruction; a browser's own
        # detection must not override what an operator wrote down.
        with mock.patch.object(routes, "_ui_language", return_value="ko"):
            self.assertEqual(routes._resolve_ui_language("zh-CN"), "ko")

    def test_an_unusable_hint_appends_nothing(self):
        # This value arrives from the client on every AI call, so anything that is
        # not a shipped, tag-shaped catalog name must resolve to "" rather than
        # reach a prompt: "ar" ships no catalog, "en-XA" is the dev pseudolocale,
        # "zh_CN" is the wrong shape, and the rest are not tags at all.
        with mock.patch.object(routes, "_ui_language", return_value=""):
            for raw in (
                "ar",
                "en-XA",
                "zh_CN",
                "'; DROP TABLE",
                "write in pirate",
                "  ",
                None,
                7,
                ["zh-CN"],
                {"lang": "zh-CN"},
            ):
                self.assertEqual(routes._resolve_ui_language(raw), "", repr(raw))

    def test_an_english_hint_is_treated_as_no_hint(self):
        # The directive-free prompt already produces English, so an `en` hint adds
        # no information — while honouring it would restamp every unconfigured
        # install's caches from "" to "en" on upgrade and throw away summaries
        # whose prose is already in the right language.
        with mock.patch.object(routes, "_ui_language", return_value=""):
            self.assertEqual(routes._resolve_ui_language("en"), "")

    def test_a_request_with_no_hint_resolves_exactly_as_before(self):
        # Non-SPA callers and older clients keep byte-identical prompts.
        with mock.patch.object(routes, "_ui_language", return_value=""):
            self.assertEqual(routes._resolve_ui_language(), "")
        with mock.patch.object(routes, "_ui_language", return_value="ja"):
            self.assertEqual(routes._resolve_ui_language(), "ja")

    def test_an_unhinted_prompt_is_byte_identical_to_the_legacy_one(self):
        legacy = routes._build_ai_prompt("o", "r", self.DETAIL, LABELS, [])
        with mock.patch.object(routes, "_ui_language", return_value=""):
            lang = routes._resolve_ui_language(None)
        self.assertEqual(
            routes._build_ai_prompt("o", "r", self.DETAIL, LABELS, [], ui_language=lang),
            legacy,
        )

    # ── each surface actually carries the hint from the wire to the prompt ──

    async def test_issue_ai_localizes_for_an_unconfigured_install(self):
        compute = AsyncMock(return_value={"summary": "s", "suggested_labels": []})
        with (
            _connected(),
            mock.patch.object(routes, "_ui_language", return_value=""),
            mock.patch.object(store, "read_issue_ai_cache", return_value=None),
            mock.patch.object(
                routes, "_load_detail_for_ai", new=AsyncMock(return_value={"number": 7})
            ),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=LABELS)),
            mock.patch.object(routes, "_compute_issue_ai", new=compute),
            mock.patch.object(store, "write_issue_ai_cache") as write,
        ):
            response = await routes._handle_issue_ai(
                _get("issue-ai", {"owner": "o", "repo": "r", "number": "7", "lang": "zh-CN"})
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(compute.call_args.kwargs["ui_language"], "zh-CN")
        # Written into zh-CN's partition, so the next open is a plain hit and no
        # other browser's summary is destroyed.
        self.assertEqual(write.call_args.kwargs["ui_language"], "zh-CN")

    async def test_pull_ai_feeds_the_hint_to_both_the_prompt_and_the_cache_key(self):
        # Dropping either wire regresses: prompt-only keeps serving a stale-language
        # cache, key-only re-keys the cache but keeps generating English.
        detail = {"number": 12, "title": "t", "body": "b", "state": "open", "head_sha": "a" * 40}
        with (
            _connected(),
            mock.patch.object(routes, "_ui_language", return_value=""),
            mock.patch.object(
                store,
                "read_pr_detail_cache",
                return_value={"detail": detail, "timeline": [], "checks": []},
            ),
            mock.patch.object(store, "read_pr_ai_cache", return_value=None) as read_ai,
            mock.patch.object(store, "write_pr_ai_cache"),
            _oneshot('{"summary": "s"}') as model,
        ):
            response = await routes._handle_pull_ai(
                _get("pull-ai", {"owner": "o", "repo": "r", "number": "12", "lang": "fr"})
            )
        self.assertEqual(response.status, 200)
        self.assertIn("BCP-47 tag fr", model.call_args[0][2])
        expected = routes._pr_ai_fingerprint(detail, [], [], ui_language="fr")
        self.assertEqual(read_ai.call_args.kwargs["fingerprint"], expected)
        # Fingerprint AND partition: the fingerprint says whether the PR moved,
        # the partition keeps another language from evicting this summary.
        self.assertEqual(read_ai.call_args.kwargs["ui_language"], "fr")

    async def test_recommendations_localize_and_write_their_own_partition(self):
        compute = AsyncMock(return_value={"recommendations": [{"name": "bug"}]})
        with (
            _connected(),
            mock.patch.object(routes, "_ui_language", return_value=""),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=LABELS)),
            mock.patch.object(routes, "_load_open_issues_for_reco", new=AsyncMock(return_value=[])),
            mock.patch.object(routes, "_compute_label_recommendations", new=compute),
            mock.patch.object(store, "write_recommendations_cache") as write,
        ):
            response = await routes._handle_generate_recommendations(
                _json_request("POST", "recommendations", {"owner": "o", "repo": "r", "lang": "pt"})
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(compute.call_args.kwargs["ui_language"], "pt")
        # Written into pt's own partition, so regenerating here cannot destroy the
        # set another language already produced.
        self.assertEqual(write.call_args.kwargs["ui_language"], "pt")

    async def test_recommendations_read_their_own_partition(self):
        with (
            _connected(),
            mock.patch.object(routes, "_ui_language", return_value=""),
            mock.patch.object(store, "read_recommendations_cache", return_value=None) as read,
        ):
            response = await routes._handle_get_recommendations(
                _get("recommendations", {"owner": "o", "repo": "r", "lang": "pt"})
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(read.call_args.kwargs["ui_language"], "pt")

    async def test_unhinted_recommendations_read_the_legacy_partition(self):
        # A pre-upgrade cache lives at the unsuffixed path, which is the "" one --
        # so installs that never had a language keep their set with no migration.
        with (
            _connected(),
            mock.patch.object(routes, "_ui_language", return_value=""),
            mock.patch.object(
                store,
                "read_recommendations_cache",
                return_value={"recommendations": [{"name": "bug"}], "generated_at": "t"},
            ) as read,
        ):
            response = await routes._handle_get_recommendations(
                _get("recommendations", {"owner": "o", "repo": "r"})
            )
        payload = _body(response)
        self.assertEqual(read.call_args.kwargs["ui_language"], "")
        self.assertTrue(payload["from_cache"])
        self.assertEqual(payload["recommendations"], [{"name": "bug"}])

    async def test_tagging_deliberately_ignores_a_hint(self):
        """The tagging routes must stay on the CONFIGURED language.

        Their cache is ONE accumulating document per repo, and
        ``store.merge_tagging_suggestions`` drops every accumulated entry when the
        batch's language differs from the stored one. That is sound for a
        deliberate, install-wide operator switch; under a per-browser language it
        becomes a loop where two browsers alternate and each wipes the queue the
        other just paid a model to build, with neither user having done anything.
        So a hint on these routes must have NO effect until the cache is
        partitioned by language -- honouring it is a one-line change away and the
        damage would be silent, so it is pinned here.
        """
        issues = [
            {
                "number": 1,
                "title": "i1",
                "body": "",
                "labels": [],
                "created_at": "2026-07-01T00:00:00Z",
            }
        ]
        with (
            _connected(),
            mock.patch.object(routes, "_ui_language", return_value=""),
            mock.patch.object(routes, "_load_labels_for_ai", new=AsyncMock(return_value=LABELS)),
            mock.patch.object(
                routes, "_load_open_issues_for_reco", new=AsyncMock(return_value=issues)
            ),
            mock.patch.object(store, "read_tagging_cache", return_value=None),
            mock.patch.object(
                routes,
                "_compute_tagging_suggestions",
                new=AsyncMock(return_value={"1": [{"name": "bug", "reason": "raison"}]}),
            ) as compute,
            mock.patch.object(
                store,
                "merge_tagging_suggestions",
                return_value={"suggestions": {"1": []}, "generated_at": "t"},
            ) as merge,
        ):
            response = await routes._handle_generate_tagging(
                _json_request("POST", "tagging", {"owner": "o", "repo": "r", "lang": "it"})
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(compute.call_args.kwargs["ui_language"], "")
        self.assertEqual(merge.call_args.kwargs["ui_language"], "")
        # The in-lock verifier must agree with what the batch was stamped under, or
        # every write is refused as a language switch and nothing is ever persisted.
        self.assertEqual(merge.call_args.kwargs["verify_language"](), "")

    async def test_tagging_get_ignores_a_hint_and_still_serves_its_cache(self):
        issues = [
            {
                "number": 1,
                "title": "i1",
                "url": "u",
                "labels": [],
                "created_at": "2026-07-01T00:00:00Z",
                "author": "a",
            }
        ]
        cached = {
            "suggestions": {"1": [{"name": "bug", "reason": "english"}]},
            "generated_at": "t",
            "ui_language": "",
        }
        with (
            _connected(),
            mock.patch.object(routes, "_ui_language", return_value=""),
            mock.patch.object(
                routes, "_load_open_issues_for_reco", new=AsyncMock(return_value=issues)
            ),
            mock.patch.object(store, "read_tagging_cache", return_value=cached),
        ):
            hinted = _body(
                await routes._handle_get_tagging(
                    _get("tagging", {"owner": "o", "repo": "r", "lang": "it"})
                )
            )
        # A hint neither localizes nor invalidates here: the GET must resolve the
        # language the same way the POST stamps it, or the gate drops everything.
        self.assertEqual(hinted["suggestions"], cached["suggestions"])
