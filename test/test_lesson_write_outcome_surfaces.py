"""The CLI, the lesson routes and the learn tools report what happened to a lesson.

This file pins what each surface tells its reader. The ``learn add`` CLI writes no
second JSONL record on a decline, exits non-zero on a refusal, echoes no category on a
no-op or an enrichment and qualifies a write with the pending-embedding note.
``api_lessons_create`` names the outcome, reason, winner and superseded rules, pushes
a dashboard refresh on a decline as well as a write, redacts stored text and withholds
a credential-shaped scope, reports the JSONL store's own outcome and its capacity
refusal, leaves unembedded rows to the repair sweep and keeps a standing rule out of
the contradiction sweep; ``api_lessons`` marks a legacy volatile row withheld. The
``learn_add`` tool says "NOT saved" for a refusal or a dedup and names why, names the
stored lesson that covers a dedup and coaches its removal only when that lesson does
not outrank the write, and quotes a removed rule in full; ``learn_list`` shows why a
row is withheld. The CLI strips terminal control sequences from a superseded rule
before it redacts it, the route redacts it too, and no dedup log line carries lesson
text. The store-side outcome lives in ``test_lesson_write_outcome.py`` and the
store-side supersede in ``test_lesson_write_outcome_supersede.py``.
"""

from __future__ import annotations

from contextlib import closing
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from body_stream_helpers import attach_body
from lesson_write_outcome_helpers import (
    _INJECTION,
    _rule_of,
    _store,
)

from kiro_crew.history import ConversationLog
from kiro_crew.vector_memory import LessonWriteOutcome, LessonWriteResult


class TestCliLearnAddReportsTheOutcome:
    """``kirocrew learn add`` writes no second record on a decline.

    It read every falsy return as "the vector store did not take it" and wrote into
    ``lessons.jsonl``. For a no-op that duplicated a lesson already stored correctly;
    for a REFUSAL it was a validation bypass, because the JSONL store validates no
    content and the context builder reads that file whenever the vector store holds
    no lessons.
    """

    def _run(self, tmp_path, negative=None, rule="prefer ruff over flake8", pre=None):
        import argparse

        from kiro_crew import cli_commands

        store = _store(tmp_path)
        if pre is not None:
            store.write_lesson(*pre)
        jsonl = MagicMock()
        args = argparse.Namespace(learn_action="add", rule=rule, category="tool", negative=negative)
        with (
            closing(store),
            patch.object(cli_commands, "VectorMemoryStore", return_value=store),
            patch.object(cli_commands, "LessonStore", return_value=jsonl),
            patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
        ):
            try:
                cli_commands._learn(args)
                exited = None
            except SystemExit as exc:  # refusal path
                exited = exc.code
        return jsonl, exited

    def test_no_op_resubmit_writes_no_jsonl_record(self, tmp_path, capsys):
        jsonl, exited = self._run(tmp_path, pre=("prefer ruff over flake8", "tool", None))
        jsonl.save_or_enrich.assert_not_called()
        jsonl.save.assert_not_called()
        assert exited is None
        out = capsys.readouterr().out
        assert "Already stored, nothing written" in out
        assert not out.startswith("Saved:")
        # The submitted category is not echoed: the store keeps the stored one.
        assert "[tool]" not in out

    def test_refusal_writes_no_jsonl_record_and_exits_non_zero(self, tmp_path, capsys):
        jsonl, exited = self._run(tmp_path, negative=_INJECTION)
        jsonl.save_or_enrich.assert_not_called()
        assert exited == 1
        assert "NOT saved" in capsys.readouterr().err

    def test_dedup_decline_writes_no_jsonl_record(self, tmp_path, capsys):
        jsonl, exited = self._run(
            tmp_path,
            rule="run the linter",
            pre=("always run the linter before pushing a branch", "tool", None),
        )
        jsonl.save_or_enrich.assert_not_called()
        assert exited is None
        assert "already covers this" in capsys.readouterr().out

    def test_a_real_write_still_reports_saved(self, tmp_path, capsys):
        jsonl, exited = self._run(tmp_path)
        jsonl.save_or_enrich.assert_not_called()
        assert exited is None
        assert capsys.readouterr().out.startswith("Saved: prefer ruff over flake8")

    def test_an_insert_qualifies_saved_with_the_embedding_note(self, tmp_path, capsys):
        """A CLI insert says the vector is pending, not an unqualified success.

        The CLI builds its store with no ``embed_fn``, so every row it inserts
        lands with a NULL embedding and only becomes vector-searchable after the
        gateway's boot-time re-embed sweep. An unqualified ``Saved:`` told the
        user the lesson was fully indexed when it was not.
        """
        jsonl, exited = self._run(tmp_path)
        assert exited is None
        out = capsys.readouterr().out
        assert "Saved: prefer ruff over flake8" in out
        assert "keyword-searchable now" in out
        assert "re-embed sweep" in out
        assert "once its embedding backend" in out
        assert "Semantic dedup did not run" in out

    def test_an_enrichment_carries_the_same_embedding_note(self, tmp_path, capsys):
        """An enrichment changes the stored value, which CLEARS the row's vector.

        The semantic upsert keeps an embedding only when the value is unchanged,
        so attaching a clause leaves the row NULL for the same sweep an insert
        waits on — the note applies to both write outcomes. But NOT the dedup
        clause: an enrichment matched its row in write_lesson's pass 1, which
        skips the generic substring/topic-overlap scan (pass 2 iterates
        ``[] if matched else lesson_rows``), so claiming that scan ran would
        report a check that never happened.
        """
        jsonl, exited = self._run(
            tmp_path,
            negative="not for typing",
            pre=("prefer ruff over flake8", "knowledge", None),
        )
        assert exited is None
        out = capsys.readouterr().out
        assert "Updated the stored lesson with this clause" in out
        assert "re-embed sweep" in out
        assert "cleared the row's existing embedding vector" in out
        assert "Semantic dedup did not run" not in out

    def test_non_writing_outcomes_do_not_carry_the_embedding_note(self, tmp_path, capsys):
        """The note describes a row this call wrote; a decline wrote none.

        A no-op re-submit and a dedup decline leave the store exactly as it was,
        so promising a backfill would attribute a pending repair to a write that
        never happened.
        """
        jsonl, exited = self._run(tmp_path, pre=("prefer ruff over flake8", "tool", None))
        assert exited is None
        assert "re-embed sweep" not in capsys.readouterr().out
        jsonl, exited = self._run(
            tmp_path,
            rule="run the linter",
            pre=("always run the linter before pushing a branch", "tool", None),
        )
        assert exited is None
        assert "re-embed sweep" not in capsys.readouterr().out

    def test_an_enrichment_does_not_echo_the_submitted_category(self, tmp_path, capsys):
        """Only an INSERT may echo the category, because only then is it what is stored.

        The store builds an enrichment with the STORED category (write-once), falling
        back to the submitted one only when the row has none -- so re-submitting under
        a new category to attach a clause enriches the row while KEEPING the old
        category. Printing the category that was just typed would name a value the
        store does not hold, which is the reporting defect this PR exists to remove.
        """
        jsonl, exited = self._run(
            tmp_path,
            negative="not for typing",
            pre=("prefer ruff over flake8", "knowledge", None),
        )
        jsonl.save_or_enrich.assert_not_called()
        assert exited is None
        out = capsys.readouterr().out
        assert "Updated the stored lesson with this clause" in out
        assert "not for typing" in out, "the clause that WAS applied is worth showing"
        # `_run` submits category "tool" while the stored row holds "knowledge".
        assert "[tool]" not in out
        assert "[knowledge]" not in out, "stored values belong in `learn list`, not here"


@pytest.mark.asyncio
class TestLessonsRouteReportsTheOutcome:
    """The response names the outcome, not a bare ``{"ok": true}`` on every path."""

    def _request(
        self,
        rule: str = "a real rule",
        negative: str | None = None,
        category: str = "knowledge",
    ):
        request = MagicMock()
        state = MagicMock()
        state.conversation_log = ConversationLog()
        state._background_tasks = set()
        request.app = {"state": state}
        request.headers = {"X-Session-Key": "dashboard:ui"}
        body = {"rule": rule, "category": category}
        if negative is not None:
            body["negative"] = negative
        attach_body(request, body)
        return request, state

    async def _post(self, result):
        from kiro_crew.dashboard.handlers import cron

        request, state = self._request()
        vs = MagicMock()
        vs.embed_lesson.return_value = [0.1] * 4
        vs.find_contradiction_candidates.return_value = []
        vs.write_lesson.return_value = result
        with (
            patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=vs)),
            patch.object(cron, "_is_restricted_session", return_value=False),
            patch.object(cron, "_sel"),
            patch.object(cron, "_resolve_and_supersede", new=AsyncMock()),
        ):
            resp = await cron.api_lessons_create(request)
        for task in list(state._background_tasks):
            # flake-ok: pre-existing unbounded await of a background task the route under test scheduled, moved verbatim; tracked for a de-flake pass
            await task
        return resp, state

    async def test_refusal_reports_not_ok_with_its_reason(self):
        resp, state = await self._post(
            LessonWriteResult(LessonWriteOutcome.REFUSED, "injection_blocked")
        )
        assert resp.status == 200
        import json as _json

        body = _json.loads(resp.text)
        assert body == {
            "ok": False,
            "outcome": "refused",
            "reason": "injection_blocked",
            "superseded": [],
        }

    async def test_dashboard_post_refuses_volatile_identity(self, tmp_path) -> None:
        from kiro_crew.dashboard.handlers import cron

        request, state = self._request("The active model identity is gpt-5.6-sol.")
        store = _store(tmp_path)
        try:
            with (
                patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store)),
                patch.object(cron, "_is_restricted_session", return_value=False),
                patch.object(cron, "_sel"),
            ):
                resp = await cron.api_lessons_create(request)

            import json as _json

            body = _json.loads(resp.text)
            assert body == {
                "ok": False,
                "outcome": "refused",
                "reason": "volatile_session_fact",
                "superseded": [],
            }
            assert store.get_lessons() == []
            state.push_refresh.assert_called_once_with("lessons")
        finally:
            store.close()

    async def test_the_save_leaves_unembedded_lessons_to_the_repair_sweep(self, tmp_path) -> None:
        """One save must not run bulk inference on the request path.

        Embedding a stored lesson that still lacks a vector is PRIORITY_BULK work
        with no deadline, queued behind every other bulk job, so doing it inside
        the request holds it past the MCP client's read timeout while the write
        still lands later. The row must stay NULL for the repair sweep instead.
        """
        import json as _json

        from kiro_crew.dashboard.handlers import cron
        from kiro_crew.embeddings import PRIORITY_BULK

        legacy_rule = "prefer tabs over spaces in makefiles"
        store = _store(tmp_path)
        try:
            # Written with no embedder bound, so the row has no vector: the state a
            # consolidation pass leaves when it defers embedding to the sweep.
            store.write_lesson(legacy_rule)
            assert [row["embedding"] for row in store.get_lessons()] == [None]

            priorities: list[int] = []

            def embed(text: str, priority: int = -1) -> list[float]:
                priorities.append(priority)
                # Orthogonal vectors, so no semantic match can retire the legacy row.
                return [1.0, 0.0, 0.0, 0.0] if text.startswith("pin") else [0.0, 1.0, 0.0, 0.0]

            embed.accepts_priority = True  # type: ignore[attr-defined]
            store.embed_fn = embed

            request, state = self._request("pin every dependency to an exact version")
            with (
                patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store)),
                patch.object(cron, "_is_restricted_session", return_value=False),
                patch.object(cron, "_sel"),
                patch.object(cron, "_resolve_and_supersede", new=AsyncMock()),
            ):
                resp = await cron.api_lessons_create(request)
            for task in list(state._background_tasks):
                # flake-ok: pre-existing unbounded await of a background task the route under test scheduled, moved verbatim; tracked for a de-flake pass
                await task

            assert _json.loads(resp.text)["outcome"] == "inserted"
            assert PRIORITY_BULK not in priorities, f"bulk embed on the request path: {priorities}"
            legacy = [row for row in store.get_lessons() if _rule_of(row) == legacy_rule]
            assert len(legacy) == 1
            assert legacy[0]["embedding"] is None, "the sweep owns this row's vector"
        finally:
            store.close()

    async def test_jsonl_fallback_refuses_volatile_negative_clause(self, tmp_path) -> None:
        import json as _json

        from kiro_crew.dashboard.handlers import cron
        from kiro_crew.learn import LessonStore

        request, state = self._request(
            "use automatic model selection",
            negative="use gpt-5.6-sol",
            category="preference",
        )
        store = LessonStore(base_dir=tmp_path)
        state.lessons = store
        with (
            patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=None)),
            patch.object(cron, "_is_restricted_session", return_value=False),
            patch.object(cron, "_sel"),
        ):
            resp = await cron.api_lessons_create(request)

        assert _json.loads(resp.text) == {
            "ok": False,
            "outcome": "refused",
            "reason": "volatile_session_fact",
            "superseded": [],
        }
        assert store.load_all() == []
        state.push_refresh.assert_called_once_with("lessons")

    async def test_a_clause_only_enrichment_still_guards_the_sweep(self, tmp_path) -> None:
        """The guard must read the PERSISTED tier, not the submitted one.

        The tier is write-once, so enriching a stored finding with a new clause --
        the ordinary re-submit this route documents -- omits ``applies``. That
        arrives as ``None`` while the row keeps ``on_topic``, so a guard gated on
        the submitted value is skipped on exactly that input and the sweep can
        retire a contradictory standing rule. There is no recovery: the
        self-heals-next-time note covers a MISSED sweep, not a wrong deletion.
        """
        import json as _json

        from kiro_crew.dashboard.handlers import cron
        from kiro_crew.lesson_validation import LESSON_APPLIES_ON_TOPIC

        store = _store(tmp_path)
        try:
            # A stored FINDING, and a standing rule the sweep must not touch.
            store.write_lesson("flush the widget cache", applies="on_topic")
            store.write_lesson("never force push to a protected branch", applies="always")

            # Re-submit the finding with a clause only -- no `applies`.
            request, state = self._request(
                "flush the widget cache", negative="do not flush it mid-deploy"
            )
            captured: dict = {}

            async def fake_sweep(_state, _sk, _rule, candidates, _vs):
                captured["candidates"] = candidates

            with (
                patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store)),
                patch.object(cron, "_is_restricted_session", return_value=False),
                patch.object(cron, "_sel"),
                patch.object(cron, "_resolve_and_supersede", new=fake_sweep),
                patch.object(
                    store,
                    "find_contradiction_candidates",
                    return_value=[
                        {
                            "key": "lesson.standing",
                            "rule": "never force push to a protected branch",
                            "similarity": 0.6,
                            "applies": "always",
                        }
                    ],
                ),
            ):
                await cron.api_lessons_create(request)
            for task in list(state._background_tasks):
                # flake-ok: pre-existing unbounded await of a background task the route under test scheduled, moved verbatim; tracked for a de-flake pass
                await task

            # The write enriched the stored finding, so the effective tier is the
            # PERSISTED one and the guard has to fire. Filtering every candidate out
            # leaves nothing to sweep, so the route schedules no task at all -- both
            # "never scheduled" and "scheduled with an empty list" are the guard
            # holding; a standing rule appearing here is the defect.
            reached = captured.get("candidates") or []
            assert (
                reached == []
            ), f"a standing rule reached the sweep on a clause-only enrichment: {reached}"
            rules = {_json.loads(row["value_json"])["rule"] for row in store.get_lessons()}
            assert "never force push to a protected branch" in rules
            result = store.write_lesson(
                "flush the widget cache", negative="another clause", applies=None
            )
            assert (
                result.applies == LESSON_APPLIES_ON_TOPIC
            ), "the write must report the persisted tier, not the submitted None"
        finally:
            store.close()

    async def test_jsonl_capacity_refusal_is_not_reported_as_volatile_text(self, tmp_path) -> None:
        """The JSONL store's TWO refusals must not collapse into one reason.

        Both the volatile-text predicate and the row cap answer with the bare
        ``refused`` string, so the route re-derives the cause. Reporting a
        capacity refusal as ``volatile_session_fact`` sends a user whose store is
        full to reword a rule whose wording was never the problem -- advice that
        cannot succeed, on the one write outcome a caller has to act on.
        """
        import json as _json

        from kiro_crew.dashboard.handlers import cron
        from kiro_crew.learn import _MAX_LESSONS_TOTAL, Lesson, LessonStore
        from kiro_crew.lesson_validation import (
            LESSON_APPLIES_ALWAYS,
            LESSON_REFUSED_AT_CAPACITY,
            contains_volatile_lesson_fact,
        )

        # A full store of AUTHORED rules: every retained row outranks an incoming
        # finding, so the prune drops the submission itself.
        store = LessonStore(base_dir=tmp_path)
        store._write_all(
            [
                Lesson(
                    ts=f"2026-01-{i % 28 + 1:02d}",
                    rule=f"standing rule number {i} about deploy step {i}",
                    category="preference",
                    applies=LESSON_APPLIES_ALWAYS,
                )
                for i in range(_MAX_LESSONS_TOTAL)
            ]
        )
        submitted = "flush the widget cache after a rebuild"
        assert not contains_volatile_lesson_fact(submitted, None), "fixture must be ordinary text"

        request, state = self._request(submitted, category="knowledge")
        state.lessons = store
        with (
            patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=None)),
            patch.object(cron, "_is_restricted_session", return_value=False),
            patch.object(cron, "_sel"),
        ):
            resp = await cron.api_lessons_create(request)

        assert _json.loads(resp.text) == {
            "ok": False,
            "outcome": "refused",
            "reason": LESSON_REFUSED_AT_CAPACITY,
            "superseded": [],
        }
        # The rule the user did submit is genuinely absent, and no authored row
        # was evicted to make room for it.
        rules = [le.rule for le in store.load_all()]
        assert submitted not in rules
        assert len(rules) == _MAX_LESSONS_TOTAL

    async def test_list_marks_legacy_volatile_jsonl_row_withheld(self, tmp_path) -> None:
        import json as _json

        from kiro_crew.dashboard.handlers import cron
        from kiro_crew.learn import LessonStore

        (tmp_path / "lessons.jsonl").write_text(
            _json.dumps(
                {
                    "ts": "2026-09-11T00:00:00+00:00",
                    "rule": "always use gpt-5.6-sol for cron summaries",
                    "category": "preference",
                    "negative": None,
                    "repo_scope": None,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        request = MagicMock()
        state = MagicMock()
        state.lessons = LessonStore(base_dir=tmp_path)
        request.app = {"state": state}
        request.headers = {"X-Session-Key": "dashboard:ui"}
        request.query = {}
        with (
            patch.object(cron, "_blocks_reads_session", return_value=False),
            patch.object(
                cron,
                "resolve_lesson_memory_store",
                new=AsyncMock(return_value=(None, None)),
            ),
            patch.object(
                cron,
                "_prepare_member_lesson_store",
                new=AsyncMock(return_value=None),
            ),
            patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=None)),
            patch.object(cron, "_get_active_workspace", return_value="default"),
        ):
            resp = await cron.api_lessons(request)

        body = _json.loads(resp.text)
        assert body["lessons"][0]["withheld_reason"] == "volatile_session_fact"

    async def test_list_marks_legacy_string_vector_row_withheld(self, tmp_path) -> None:
        import json as _json

        from kiro_crew.dashboard.handlers import cron

        store = _store(tmp_path)
        try:
            assert (
                store.set_semantic(
                    "lesson.legacystring",
                    "For reviews, use claude-opus-4.6",
                    1.0,
                    "user_explicit",
                )
                is None
            )
            request = MagicMock()
            state = MagicMock()
            request.app = {"state": state}
            request.headers = {"X-Session-Key": "dashboard:ui"}
            request.query = {}
            with (
                patch.object(cron, "_blocks_reads_session", return_value=False),
                patch.object(
                    cron,
                    "resolve_lesson_memory_store",
                    new=AsyncMock(return_value=(None, None)),
                ),
                patch.object(
                    cron,
                    "_prepare_member_lesson_store",
                    new=AsyncMock(return_value=None),
                ),
                patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store)),
            ):
                resp = await cron.api_lessons(request)

            body = _json.loads(resp.text)
            assert body["lessons"][0]["withheld_reason"] == "volatile_session_fact"
        finally:
            store.close()

    async def test_list_marks_legacy_mapping_negative_pin_withheld(self, tmp_path) -> None:
        import json as _json

        from kiro_crew.dashboard.handlers import cron

        store = _store(tmp_path)
        try:
            assert (
                store.set_semantic(
                    "lesson.legacynegative",
                    {
                        "rule": "automatic selection is safest",
                        "category": "knowledge",
                        "negative": "Select gpt-5.6-sol for reviews",
                    },
                    1.0,
                    "user_explicit",
                )
                is None
            )
            request = MagicMock()
            state = MagicMock()
            request.app = {"state": state}
            request.headers = {"X-Session-Key": "dashboard:ui"}
            request.query = {}
            with (
                patch.object(cron, "_blocks_reads_session", return_value=False),
                patch.object(
                    cron,
                    "resolve_lesson_memory_store",
                    new=AsyncMock(return_value=(None, None)),
                ),
                patch.object(
                    cron,
                    "_prepare_member_lesson_store",
                    new=AsyncMock(return_value=None),
                ),
                patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store)),
            ):
                resp = await cron.api_lessons(request)

            body = _json.loads(resp.text)
            assert body["lessons"][0]["withheld_reason"] == "volatile_session_fact"
        finally:
            store.close()

    async def test_no_op_stays_ok_but_names_the_outcome(self):
        resp, state = await self._post(LessonWriteResult(LessonWriteOutcome.UNCHANGED))
        import json as _json

        body = _json.loads(resp.text)
        assert body["ok"] is True, "the lesson is stored; there was nothing to write"
        assert body["outcome"] == "unchanged"

    async def test_a_real_write_pushes_a_refresh(self):
        resp, state = await self._post(LessonWriteResult(LessonWriteOutcome.INSERTED))
        import json as _json

        assert _json.loads(resp.text)["outcome"] == "inserted"
        state.push_refresh.assert_called_once_with("lessons")

    @pytest.mark.parametrize(
        "result",
        [
            LessonWriteResult(LessonWriteOutcome.DEDUPED, "substring_covered"),
            LessonWriteResult(LessonWriteOutcome.UNCHANGED),
            LessonWriteResult(LessonWriteOutcome.REFUSED, "injection_blocked"),
        ],
    )
    async def test_a_declining_outcome_still_refreshes(self, result):
        """A decline does NOT mean the store is unchanged, so the refresh is not gated.

        ``write_lesson``'s second pass DELETES a row it supersedes and keeps
        scanning, so a containment chain (A inside R inside B) visited A-first removes
        A and then returns ``deduped`` for B -- and the scan order is effectively
        random, since ``get_lessons`` orders by md5 key. An earlier revision gated the
        push on the write having landed, which left connected dashboards showing a
        lesson that had just been deleted. An extra refresh costs a redundant list
        fetch; a missed one shows data that is gone.
        """
        resp, state = await self._post(result)
        assert resp.status == 200
        state.push_refresh.assert_called_once_with("lessons")

    async def test_jsonl_branch_reports_that_store_own_outcome(self):
        """The JSONL store's three words reach the body unchanged."""
        from kiro_crew.dashboard.handlers import cron

        request, state = self._request()
        state.lessons.save_or_enrich.return_value = "unchanged"
        with (
            patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=None)),
            patch.object(cron, "_is_restricted_session", return_value=False),
            patch.object(cron, "_sel"),
        ):
            resp = await cron.api_lessons_create(request)
        assert resp.status == 200
        import json as _json

        body = _json.loads(resp.text)
        assert body == {
            "ok": True,
            "outcome": "unchanged",
            "reason": None,
            "superseded": [],
        }

    async def test_a_deduped_response_names_the_winner_and_its_scope(self):
        resp, _state = await self._post(
            LessonWriteResult(
                LessonWriteOutcome.DEDUPED,
                "substring_covered",
                winner="always run the linter before pushing a branch",
                winner_repo_scope="src/pkg",
            )
        )
        import json as _json

        body = _json.loads(resp.text)
        assert body["outcome"] == "deduped"
        assert body["winner"] == "always run the linter before pushing a branch"
        assert body["winner_repo_scope"] == "src/pkg"

    async def test_a_global_winner_is_sent_as_the_explicit_global_selector(self):
        """``""`` is the delete selector that names only the global row."""
        resp, _state = await self._post(
            LessonWriteResult(
                LessonWriteOutcome.DEDUPED, "substring_covered", winner="a global rule"
            )
        )
        import json as _json

        assert _json.loads(resp.text)["winner_repo_scope"] == ""

    async def test_a_credential_shaped_winner_scope_is_withheld(self):
        """A stored scope the redaction chain would alter never leaves the route."""
        token = "ghp_" + "C" * 36
        resp, _state = await self._post(
            LessonWriteResult(
                LessonWriteOutcome.DEDUPED,
                "substring_covered",
                winner="a scoped rule",
                winner_repo_scope=f"repos/{token}",
            )
        )
        assert token not in resp.text
        import json as _json

        assert _json.loads(resp.text)["winner_repo_scope"] is None

    async def test_the_winner_is_redacted_before_it_leaves_the_process(self):
        """The winner is stored user text, so it rides the same redaction chain."""
        from kiro_crew.dashboard.handlers import cron

        with patch.object(
            cron, "_redact_memory_field", side_effect=lambda v: "[REDACTED]" if v else v
        ):
            resp, _state = await self._post(
                LessonWriteResult(
                    LessonWriteOutcome.DEDUPED, "substring_covered", winner="a secret rule"
                )
            )
        import json as _json

        body = _json.loads(resp.text)
        assert body["winner"] == "[REDACTED]"

    async def test_a_response_with_no_winner_omits_the_field(self):
        resp, _state = await self._post(LessonWriteResult(LessonWriteOutcome.INSERTED))
        import json as _json

        body = _json.loads(resp.text)
        assert "winner" not in body
        assert "winner_repo_scope" not in body


class TestLearnAddVolatileSessionFactBoundary:
    """``learn_add`` must not turn one session's runtime identity into policy."""

    @staticmethod
    def _call(rule: str, response: dict | None = None):
        from kiro_crew.mcp_tools import learn

        with (
            patch.object(learn.mcp_core, "_post", return_value=response or {"ok": True}) as post,
            patch.object(learn.mcp_core, "_resolve_session_key", return_value="dashboard:ui"),
            patch.object(learn.mcp_core, "_vet_memory_writes_governance", return_value=None),
        ):
            text = learn.learn_add("learn_add", {"rule": rule, "category": "preference"})
        return text, post

    @pytest.mark.parametrize(
        "rule",
        [
            ("The current model identity shown by runtime/system context is " "gpt-5.6-sol."),
            "The current model is selected by this session.",
            "The active model identity changes per tab.",
            "This session is running as the model chosen by the runtime.",
        ],
    )
    def test_volatile_session_identity_is_rejected_by_the_shared_writer(self, rule: str) -> None:
        text, post = self._call(
            rule,
            {
                "ok": False,
                "outcome": "refused",
                "reason": "volatile_session_fact",
                "superseded": [],
            },
        )

        assert text.startswith("Error: volatile_session_fact:")
        assert "NOT saved" in text
        assert "reusable behavioral rule" in text
        assert "agent.role_models.<role>" in text
        assert "config" in text
        post.assert_called_once_with(
            "/api/lessons",
            {"rule": rule, "category": "preference", "scope": "global"},
        )

    def test_cross_model_reviewer_preference_still_persists(self) -> None:
        rule = "for cross-model review prefer the latest Claude Sonnet reviewer"
        text, post = self._call(rule)

        assert text == f"Saved lesson: {rule}"
        post.assert_called_once_with(
            "/api/lessons",
            {"rule": rule, "category": "preference", "scope": "global"},
        )

    def test_descriptor_warns_before_the_model_calls_the_tool(self) -> None:
        from kiro_crew.mcp_tools import learn

        descriptor = next(item for item in learn.schemas() if item["name"] == "learn_add")
        description = descriptor["description"]
        assert "unrelated future sessions" in description
        assert "active model identity" in description
        assert "volatile" in description


class TestLearnListWithheldRows:
    def test_withheld_reason_is_visible(self) -> None:
        from kiro_crew.mcp_tools import learn

        with patch.object(
            learn.mcp_core,
            "_get",
            return_value={
                "lessons": [
                    {
                        "rule": "always use gpt-5.6-sol for cron summaries",
                        "category": "preference",
                        "withheld_reason": "volatile_session_fact",
                    }
                ]
            },
        ):
            text = learn.learn_list("learn_list", {})

        assert "WITHHELD: volatile_session_fact" in text


class TestLearnAddToolReportsTheOutcome:
    """The MCP tool told the model "Saved lesson" for a refused write."""

    def _call(self, response):
        from kiro_crew.mcp_tools import learn

        with (
            patch.object(learn.mcp_core, "_post", return_value=response),
            patch.object(learn.mcp_core, "_resolve_session_key", return_value="dashboard:ui"),
            patch.object(learn.mcp_core, "_vet_memory_writes_governance", return_value=None),
        ):
            return learn.learn_add("learn_add", {"rule": "a rule", "category": "tool"})

    def test_refusal_says_not_saved(self):
        text = self._call({"ok": False, "outcome": "refused", "reason": "injection_blocked"})
        assert "NOT saved" in text
        assert "injection_blocked" in text
        assert "Saved lesson" not in text

    def test_dedup_says_covered_by_an_existing_lesson(self):
        text = self._call({"ok": False, "outcome": "deduped", "reason": "substring_covered"})
        assert "NOT saved" in text
        assert "already covers it" in text
        # The message quotes the SUBMITTED rule. It must say so: the old wording put
        # that quote straight after "an existing stored lesson already covers it",
        # which read as a quote of the stored lesson, so a caller thought it had been
        # shown the winner and could not tell a correct dedup from a dropped
        # correction. It must also name the replace path.
        assert "DROPPED" in text
        assert "NOT the stored lesson" in text
        assert "learn_remove" in text

    def test_semantic_dedup_reply_never_coaches_removing_the_protected_lesson(self):
        """``semantic_similarity`` is only reachable when the stored row OUTRANKS
        the write. Coaching the remove-and-re-add path there would walk an
        automated caller through deleting the row the store just protected."""
        text = self._call({"ok": False, "outcome": "deduped", "reason": "semantic_similarity"})
        assert "NOT saved" in text
        assert "higher authority" in text
        assert "DROPPED" in text
        assert "NOT the stored lesson" in text
        assert "learn_remove" not in text

    def test_dedup_names_the_winning_stored_lesson_verbatim(self):
        """With the winner named, the reply points at it instead of the list."""
        winner = "always run the linter before pushing a branch -- NOT: for docs"
        text = self._call(
            {
                "ok": False,
                "outcome": "deduped",
                "reason": "substring_covered",
                "winner": winner,
                "winner_repo_scope": "src/pkg",
            }
        )
        assert "NOT saved" in text
        assert f"The stored lesson that covers it (applies only in src/pkg): {winner}" in text
        assert 'learn_remove with that stored text as query and repo_scope="src/pkg"' in text
        assert "learn_list" not in text

    def test_a_global_winner_points_learn_remove_at_the_global_row_only(self):
        """An unscoped remove would reach every scope; ``""`` names only the global row."""
        text = self._call(
            {
                "ok": False,
                "outcome": "deduped",
                "reason": "substring_covered",
                "winner": "a global rule",
                "winner_repo_scope": "",
            }
        )
        assert "The stored lesson that covers it: a global rule" in text
        assert 'repo_scope=""' in text

    def test_a_withheld_winner_scope_names_the_winner_but_keeps_the_list_path(self):
        """Without a selector a remove cannot be scoped, so it is not coached."""
        text = self._call(
            {
                "ok": False,
                "outcome": "deduped",
                "reason": "substring_covered",
                "winner": "a scoped rule",
                "winner_repo_scope": None,
            }
        )
        assert "The stored lesson that covers it: a scoped rule" in text
        assert "repo_scope=" not in text
        assert "learn_list" in text

    def test_semantic_dedup_names_the_winner_and_still_protects_it(self):
        text = self._call(
            {
                "ok": False,
                "outcome": "deduped",
                "reason": "semantic_similarity",
                "winner": "the user's own rule",
                "winner_repo_scope": None,
            }
        )
        assert "The stored lesson that covers it: the user's own rule" in text
        assert "higher authority" in text
        assert "learn_remove" not in text

    @pytest.mark.parametrize("winner", [None, "", "   ", 7, ["x"]])
    def test_a_missing_or_malformed_winner_keeps_the_list_recovery(self, winner):
        """An older gateway sends no winner; a bad value is not rendered as one."""
        response = {"ok": False, "outcome": "deduped", "reason": "substring_covered"}
        if winner is not None:
            response["winner"] = winner
        text = self._call(response)
        assert "The stored lesson that covers it" not in text
        assert "learn_list" in text

    def test_no_op_says_already_stored_without_claiming_an_exact_match(self):
        """``unchanged`` does not mean the stored row equals the submission.

        The category is write-once in the store, so a re-submit under a NEW category
        lands on this branch with the old category still stored. Claiming it was saved
        "exactly as submitted" was false, and hid why the new category did not apply.
        """
        text = self._call({"ok": True, "outcome": "unchanged", "reason": None})
        assert "already stored" in text
        assert "exactly as submitted" not in text
        assert "does not rewrite the stored category" in text
        assert "NOT saved" not in text

    def test_kept_clause_no_op_does_not_claim_an_exact_match(self):
        """A bare re-submit against a stored NOT-clause also reports ``unchanged``.

        The stored lesson carries a clause this submission did not, so telling the
        model it was saved "exactly as submitted" is false -- and a model reading that
        could conclude the clause it omitted is gone.
        """
        text = self._call({"ok": True, "outcome": "unchanged", "reason": "kept_stored_clause"})
        assert "exactly as submitted" not in text
        assert "NOT-clause" in text
        assert "kept, not removed" in text

    def test_enrichment_says_updated(self):
        text = self._call({"ok": True, "outcome": "enriched", "reason": None})
        assert "Updated the stored lesson" in text

    def test_a_gateway_that_sends_no_outcome_still_reports_saved(self):
        """Version skew during an update must not turn a real save into a scare."""
        text = self._call({"ok": True})
        assert text == "Saved lesson: a rule"

    def test_a_read_timeout_reports_an_unknown_outcome_not_a_failure(self):
        """The client stops waiting at its read timeout; the route can still finish the save.

        A bare ``Error: timed out`` reads as a failed save, so the model retries a
        write already in flight and tells the user the lesson was lost. The payload
        is what ``mcp_core._post`` returns for a socket read timeout
        (``test_mcp_core_coverage.py`` pins that classification).
        """
        text = self._call({"error": "timed out", "transport_error": True})
        assert text.startswith("Error: timed out.")
        assert "outcome is unknown" in text
        assert "learn_list" in text
        assert "NOT saved" not in text

    def test_a_refused_connection_stays_a_plain_error(self):
        """Nothing reached the gateway, so nothing can still be saving."""
        text = self._call({"error": "gateway not reachable", "refused": True})
        assert text == "Error: gateway not reachable"


class TestSupersedeReachesTheSurfacesAHumanReads:
    """A field nothing renders is not a fix. These pin the two report surfaces."""

    @pytest.mark.asyncio
    async def test_the_route_forwards_the_superseded_rules(self):
        """Drives the real route, so it fails on main where the response omits the key."""
        resp, _state = await TestLessonsRouteReportsTheOutcome()._post(
            LessonWriteResult(
                LessonWriteOutcome.INSERTED,
                None,
                ("never force push to a shared branch",),
            )
        )
        import json as _json

        body = _json.loads(resp.text)
        assert body["ok"] is True, "the submitted lesson did land"
        assert body["outcome"] == "inserted"
        assert body["superseded"] == ["never force push to a shared branch"]

    def _tool_call(self, response):
        from kiro_crew.mcp_tools import learn

        with (
            patch.object(learn.mcp_core, "_post", return_value=response),
            patch.object(learn.mcp_core, "_resolve_session_key", return_value="dashboard:ui"),
            patch.object(learn.mcp_core, "_vet_memory_writes_governance", return_value=None),
        ):
            return learn.learn_add("learn_add", {"rule": "a rule", "category": "tool"})

    def test_the_tool_warns_and_quotes_the_removed_rule_in_full(self):
        text = self._tool_call(
            {
                "ok": True,
                "outcome": "inserted",
                "reason": None,
                "superseded": ["never force push to a shared branch"],
            }
        )
        assert "Saved lesson" in text
        assert "REMOVED 1 stored lesson" in text
        # Quoted in full, not counted or previewed: this text is the last readable
        # copy of a tombstoned row.
        assert "never force push to a shared branch" in text

    def test_the_tool_says_nothing_when_nothing_was_superseded(self):
        text = self._tool_call({"ok": True, "outcome": "inserted", "reason": None})
        assert "Saved lesson" in text
        assert "REMOVED" not in text

    def test_the_tool_ignores_a_superseded_field_that_is_not_a_list_of_text(self):
        """It crosses HTTP, so the shape is not this tool's to trust.

        A malformed value must read as "none reported" -- which is what every gateway
        older than this field says -- rather than rendering a repr into a warning.
        """
        for junk in ({"a": 1}, "a string", [None, 3, "  "], 7):
            text = self._tool_call(
                {"ok": True, "outcome": "inserted", "reason": None, "superseded": junk}
            )
            assert "REMOVED" not in text, f"rendered a warning for {junk!r}"


class TestASupersededRuleIsSanitizedBeforeItIsShown:
    """The reported rule is stored USER TEXT, so echoing it raw is a new exposure.

    Reporting a superseded rule means printing text a user typed once, on a path that
    did not print it before. Both surfaces already had the right treatment for stored
    text and this write path was simply not routed through it -- `learn list` strips
    terminal control sequences on every print, and `api_lessons_create` already sends
    the lesson's own rule through `_redact_memory_field`.
    """

    def test_the_cli_strips_terminal_control_sequences_from_a_removed_rule(self, tmp_path):
        """An OSC payload stored in a lesson must not be executed by reporting it.

        This is the one write path guaranteed to print a rule the user is NOT looking
        at, so an escape sequence here retitles the window or writes the clipboard for
        a rule the user never asked to see.
        """
        import argparse

        from kiro_crew import cli_commands

        store = _store(tmp_path)
        # A stored general rule carrying an OSC window-title payload and a CSI clear.
        evil = "never \x1b]0;pwned\x07force \x1b[2Jpush"
        store.write_lesson(evil, "tool")
        args = argparse.Namespace(
            learn_action="add",
            rule=f"during a release {evil} and tell the release manager",
            category="tool",
            negative=None,
        )
        with (
            closing(store),
            patch.object(cli_commands, "VectorMemoryStore", return_value=store),
            patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
            patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
        ):
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                cli_commands._learn(args)
            out = buf.getvalue()
        assert "REMOVED 1 stored lesson" in out, "the supersede must still be reported"
        # Scoped to the REMOVED block, which is what this change prints. The `Saved:`
        # line above it echoes the rule the user just typed on their own command line,
        # unsanitized on main and unchanged here -- a different threat model (their own
        # input, in the same breath) from a STORED rule, which consolidation, an import
        # or a subagent may have written and which this block prints when the user is
        # not looking at it. Masking a string the user literally just typed would tell
        # them nothing and hide their own command from them.
        removed_block = out[out.index("REMOVED") :]
        # The readable rule survives around the sequences. "pwned" is deliberately NOT
        # asserted present: it is the OSC sequence's own payload, and the regex consumes
        # a whole OSC run, so removing it is correct rather than over-broad.
        assert "never" in removed_block and "push" in removed_block
        assert "pwned" not in removed_block, "the OSC payload itself must not survive"
        assert "\x1b]" not in removed_block, "raw OSC reached the terminal"
        assert "\x1b[" not in removed_block, "raw CSI reached the terminal"
        assert "\x07" not in removed_block

    def test_the_cli_redacts_a_credential_in_a_removed_rule(self, tmp_path):
        """The CLI must agree with the route about whether a superseded rule may leak.

        Both deliver the same field, so a secret redacted on one path and printed on the
        other is one feature disagreeing with itself. A lesson written by history
        consolidation carries model output the user never typed, so this is not
        "their own secret in their own terminal".
        """
        import argparse
        import io
        from contextlib import redirect_stdout

        from kiro_crew import cli_commands

        secret = "AKIAIOSFODNN7EXAMPLE"
        store = _store(tmp_path)
        store.write_lesson(f"never commit {secret} to the repository", "tool")
        args = argparse.Namespace(
            learn_action="add",
            rule=f"during a release never commit {secret} to the repository and tell the lead",
            category="tool",
            negative=None,
        )
        with (
            closing(store),
            patch.object(cli_commands, "VectorMemoryStore", return_value=store),
            patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
            patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli_commands._learn(args)
            out = buf.getvalue()
        assert "REMOVED 1 stored lesson" in out, "the supersede must still be reported"
        removed_block = out[out.index("REMOVED") :]
        assert secret not in removed_block, "raw credential printed for a removed rule"
        assert "never commit" in removed_block, "the rule is redacted, not dropped"

    def test_the_cli_cannot_reassemble_a_credential_split_by_a_control_sequence(self, tmp_path):
        """Pins the ORDER of the two treatments, which is where this went wrong once.

        The intuitive order is the unsafe one. Redacting first leaves
        ``AKIA<CSI>IOSFODNN7EXAMPLE`` untouched -- the escape breaks the token so the
        credential regex does not match -- and the control-strip then REASSEMBLES the
        complete credential and prints it. Stripping first can only JOIN characters,
        never split a token, so the regex sees the whole credential.

        This is why the two earlier tests are not enough between them: each shows one
        treatment happens, neither shows they compose in the safe order. A stored
        credential is planted pre-split here so a future reordering reddens.
        """
        import argparse
        import io
        from contextlib import redirect_stdout

        from kiro_crew import cli_commands

        secret = "AKIAIOSFODNN7EXAMPLE"
        split = "AKIA\x1b[0mIOSFODNN7EXAMPLE"
        store = _store(tmp_path)
        store.write_lesson(f"never commit {split} to the repository", "tool")
        args = argparse.Namespace(
            learn_action="add",
            rule=f"during a release never commit {split} to the repository and tell the lead",
            category="tool",
            negative=None,
        )
        with (
            closing(store),
            patch.object(cli_commands, "VectorMemoryStore", return_value=store),
            patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
            patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli_commands._learn(args)
            out = buf.getvalue()
        assert "REMOVED 1 stored lesson" in out, "the supersede must still be reported"
        removed_block = out[out.index("REMOVED") :]
        assert secret not in removed_block, "control stripping reassembled the credential"
        assert split not in removed_block, "the split credential was printed as stored"

    @pytest.mark.asyncio
    async def test_the_route_redacts_a_credential_in_a_removed_rule(self):
        """A credential a user once put in a lesson must not be echoed on deletion.

        Worse than a read: the row is being deleted, so this response is the only place
        that text is returned at all, and it reaches both the dashboard and the
        ``learn_add`` tool.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        resp, _state = await TestLessonsRouteReportsTheOutcome()._post(
            LessonWriteResult(
                LessonWriteOutcome.INSERTED, None, (f"never commit the key {secret}",)
            )
        )
        import json as _json

        body = _json.loads(resp.text)
        assert len(body["superseded"]) == 1, "the supersede must still be reported"
        assert secret not in body["superseded"][0], "raw credential returned in a response"
        assert secret not in resp.text

    def test_no_dedup_log_line_carries_lesson_text(self, tmp_path, caplog):
        """The whole scan logs IDENTITIES, never content, on every branch.

        A lesson can hold whatever the user tells the agent -- credentials, paths,
        names -- so a log line carrying its text turns a silent-deletion bug into a
        disclosure bug, on a sink that persists to disk and may reach a notification
        channel. Filtering the text on the way out only narrows that; logging the
        store's own row ids removes it, and those ids are what let an operator join the
        line to the tombstoned row and to the ``delete_semantic`` audit record.

        The result is the read path where the text belongs, and the last assertion
        checks it still carries the full rule -- so this test cannot be satisfied by
        losing the recovery channel along with the exposure.

        Each of the three dedup branches is driven separately, and every record emitted
        is checked, because the three lines are independent and only one of them was
        added by this change.
        """
        import logging

        secret = "AKIAIOSFODNN7EXAMPLE"
        cases = {
            # label -> (stored rule, submitted rule) selecting one branch each
            "substring-delete": (
                f"never commit {secret} to the repository",
                f"during a release never commit {secret} to the repository and tell the lead",
            ),
            "substring-covered": (
                f"always rotate {secret} on the first of the month",
                f"rotate {secret}",
            ),
            "topic-overlap": (
                f"rotate the deployment key {secret} monthly and audit the log",
                f"rotate the deployment key {secret} weekly instead",
            ),
        }
        for label, (stored, submitted) in cases.items():
            store = _store(tmp_path / label.replace("-", "_"))
            try:
                store.write_lesson(stored, "tool")
                caplog.clear()
                with caplog.at_level(logging.DEBUG, logger="kiro_crew.vector_memory"):
                    result = store.write_lesson(submitted, "tool")
                emitted = [r.getMessage() for r in caplog.records]
                assert emitted, f"{label}: no log record, so this asserts nothing"
                for message in emitted:
                    assert secret not in message, f"{label}: a credential reached the log"
                    assert "rotate" not in message, f"{label}: rule text reached the log"
                    assert "commit" not in message, f"{label}: rule text reached the log"
                    assert "lesson." in message, f"{label}: rows should be named by id"
                if label == "substring-delete":
                    # The recovery channel still carries the full text.
                    assert result.superseded, "the supersede must have happened"
                    assert secret in result.superseded[0], "the result carries the text"
            finally:
                store.close()
