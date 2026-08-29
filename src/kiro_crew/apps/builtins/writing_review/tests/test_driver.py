"""Unit tests for the Slice 2 driver: prompt building and scanner dispatch.

Each ``TestCase`` maps to Behaviour Table rows:

* :class:`TestScannerPromptBuild`      -> Behaviour #16
* :class:`TestRunScan`                 -> Behaviours #6, #7, #8, #14
* :class:`TestScannerToggle`           -> Behaviour #15
* :class:`TestFindingId`               -> Behaviour #6 (stable ID contract)
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kiro_crew.apps.builtins.writing_review import (
    ALWAYS_ON_SCANNERS,
    ReviewContext,
    _coerce_finding,
    _resolve_scanners,
    _scanner_prompt,
    finding_id,
    run_scan,
)

_POOL_GET_PATH = "kiro_crew.apps.builtins.writing_review.get_pool"


def _fake_pool_with_dispatch(
    *,
    return_value: dict | None = None,
    side_effect=None,
) -> SimpleNamespace:
    """Return a stand-in for :class:`ScannerPool` whose ``dispatch`` is mocked.

    Also stubs ``begin_batch`` / ``end_batch`` / ``resize`` so run_scan's
    bracket calls succeed without exercising real runtime lifecycle.
    """
    fake_pool = SimpleNamespace()
    if side_effect is not None:
        fake_pool.dispatch = AsyncMock(side_effect=side_effect)
    else:
        fake_pool.dispatch = AsyncMock(return_value=return_value or {"findings": []})
    fake_pool.begin_batch = AsyncMock()
    fake_pool.end_batch = AsyncMock()
    fake_pool.resize = lambda _max_concurrent: None
    return fake_pool


class TestScannerPromptBuild(unittest.TestCase):
    """Behaviour #16 -- prompt template combines brief, document, and context."""

    def test_builds_prompt_with_brief_and_document(self) -> None:
        prompt = _scanner_prompt(
            scanner_name="clarity",
            scanner_brief="# Clarity\nRule 1: be specific.",
            document_text="The project delivered results.",
            context=ReviewContext(
                audience="VP",
                doc_type="decision doc",
                tone="concise",
            ),
        )

        self.assertIn("# Clarity", prompt)
        self.assertIn("The project delivered results.", prompt)
        self.assertIn("VP", prompt)
        self.assertIn("decision doc", prompt)
        self.assertIn("concise", prompt)

    def test_includes_context_additional_context(self) -> None:
        prompt = _scanner_prompt(
            scanner_name="clarity",
            scanner_brief="# Clarity\n...",
            document_text="doc text",
            context=ReviewContext(additional_context=["FY2025 is correct"]),
        )

        self.assertIn("FY2025 is correct", prompt)

    def test_includes_context_ask_when_populated(self) -> None:
        # An "Ask" is a free-form directive from the author: what
        # decision they want the reviewer to focus on. The scanner
        # prompt MUST surface it so findings can be weighted against
        # the author's actual concern (e.g. "focus on whether the
        # architecture is sound" vs "focus on tone for the exec
        # audience"). Value is rendered verbatim near audience/type/tone.
        prompt = _scanner_prompt(
            scanner_name="clarity",
            scanner_brief="# Clarity\n...",
            document_text="doc text",
            context=ReviewContext(
                audience="VP",
                doc_type="strategy",
                tone="concise",
                ask="Is the phased rollout timeline realistic?",
            ),
        )

        self.assertIn("Is the phased rollout timeline realistic?", prompt)

    def test_omits_ask_line_when_ask_is_empty(self) -> None:
        # An empty ask must not pollute the prompt with a stray "Ask:
        # not specified" filler. The audience/type/tone lines use the
        # ``or 'not specified'`` fallback because those are select
        # controls with defaults; ask is a free-form textarea whose
        # natural state is empty, and adding filler text there would
        # push every scan to reason about "why is this not specified".
        prompt = _scanner_prompt(
            scanner_name="clarity",
            scanner_brief="# Clarity\n...",
            document_text="doc text",
            context=ReviewContext(audience="team"),
        )

        # No "Ask:" line, no "asking for" directive prose.
        self.assertNotIn("Ask:", prompt)
        self.assertNotIn("asking for", prompt)

    def test_prompt_demands_confidence_field(self) -> None:
        prompt = _scanner_prompt(
            scanner_name="clarity",
            scanner_brief="# Clarity\nRule 1: be specific.",
            document_text="The project delivered results.",
            context=ReviewContext(),
        )
        self.assertIn("confidence", prompt)
        self.assertIn('"high", "medium", or "low"', prompt)

    def test_prompt_demands_substantive_issue(self) -> None:
        prompt = _scanner_prompt(
            scanner_name="clarity",
            scanner_brief="# Clarity\nRule 1.",
            document_text="text",
            context=ReviewContext(),
        )
        self.assertIn("2-3 sentences", prompt)
        self.assertIn("why it matters", prompt)


class TestCoerceFinding(unittest.TestCase):
    """Behaviours #5, #6 -- confidence parsing and defaults."""

    def test_coerce_finding_parses_confidence(self) -> None:
        finding = _coerce_finding(
            "clarity",
            {
                "section": "Intro",
                "paragraph": 1,
                "issue": "x",
                "rule": "1",
                "severity": "high",
                "proposed_fix": "y",
                "confidence": "low",
            },
        )
        self.assertEqual(finding.confidence, "low")

    def test_coerce_finding_defaults_confidence_when_missing(self) -> None:
        finding = _coerce_finding(
            "clarity",
            {
                "section": "Intro",
                "paragraph": 1,
                "issue": "x",
                "rule": "1",
                "severity": "high",
                "proposed_fix": "y",
            },
        )
        self.assertEqual(finding.confidence, "medium")

    def test_coerce_finding_defaults_confidence_when_invalid(self) -> None:
        finding = _coerce_finding(
            "clarity",
            {
                "section": "Intro",
                "paragraph": 1,
                "issue": "x",
                "rule": "1",
                "severity": "high",
                "proposed_fix": "y",
                "confidence": "VERY_HIGH",
            },
        )
        self.assertEqual(finding.confidence, "medium")


def _write_test_document(target_dir: Path) -> Path:
    document_path = target_dir / "doc.md"
    document_path.write_text("# Intro\nHello\n", encoding="utf-8")
    return document_path


class TestRunScan(unittest.IsolatedAsyncioTestCase):
    """Behaviours #1, #4, #7, #8 -- run_scan dispatches every scanner via the pool."""

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.workspace_root = Path(self._tempdir.name)
        self.test_document = _write_test_document(self.workspace_root)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    async def test_dispatches_all_scanners_via_pool(self) -> None:
        fake_pool = _fake_pool_with_dispatch(return_value={"findings": []})
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            result = await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(audience="team", doc_type="update"),
            )

        # Empty findings -> synthesis pass is skipped, so exactly one dispatch per scanner.
        self.assertEqual(fake_pool.dispatch.await_count, len(ALWAYS_ON_SCANNERS))
        self.assertEqual(result.scanners_run, list(ALWAYS_ON_SCANNERS))
        self.assertEqual(result.failed_scanners, [])
        self.assertEqual(result.findings, [])

    async def test_builds_failed_scanner_with_metadata(self) -> None:
        call_counter = {"n": 0}

        async def flaky_dispatch(_prompt):
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                raise RuntimeError("worker died on first scanner")
            return {"findings": []}

        fake_pool = _fake_pool_with_dispatch(side_effect=flaky_dispatch)
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            result = await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(),
            )

        self.assertEqual(len(result.failed_scanners), 1)
        failure_record = result.failed_scanners[0]
        self.assertIsInstance(failure_record.name, str)
        self.assertEqual(failure_record.reason_class, "worker_died")
        self.assertIn("worker died", failure_record.message)
        self.assertTrue(failure_record.at)  # ISO-8601 non-empty
        self.assertGreaterEqual(failure_record.duration_ms, 0)
        # partial_failure semantic: any scanner failed -> flag is true.
        self.assertTrue(result.partial_failure)

    async def test_truncated_response_maps_to_truncated_reason_class(self) -> None:
        """A truncation error MUST classify as ``truncated_response`` — not
        the generic ``invalid_json`` — so the UI banner and the discussion
        agent can say "the model ran out of room" rather than blaming the
        JSON contract. Regression test for the design-scanner cut-off the
        team saw on a large real-world design document.

        With the truncation retry in place (``_TRUNCATION_RETRY_SUFFIX``),
        a single ``TruncatedResponseError`` now recovers via a stricter
        retry rather than surfacing as a failure. This test pins the
        BOTH-attempts-fail case: only when the retry ALSO truncates does
        the scanner end up in ``failed_scanners`` with the expected
        reason_class.
        """
        from kiro_crew.apps.builtins.writing_review.pool import TruncatedResponseError

        async def always_truncates(_prompt):
            raise TruncatedResponseError(
                "scanner response was truncated mid-output "
                "(len=11754, Unterminated string starting at at char 42)"
            )

        fake_pool = _fake_pool_with_dispatch(side_effect=always_truncates)
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            result = await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(),
            )

        # Every scanner failed the same way — pick any and verify the class.
        self.assertGreaterEqual(len(result.failed_scanners), 1)
        failure_record = result.failed_scanners[0]
        self.assertEqual(failure_record.reason_class, "truncated_response")
        self.assertIn("truncated", failure_record.message)
        self.assertTrue(result.partial_failure)

    async def test_layer4_merges_first_attempt_salvage_with_retry(self) -> None:
        """Layer 4 — first attempt truncates with two partial findings; retry
        returns a third overlapping the second. Merged output must contain
        all three unique findings, with the overlapping ``id`` deduplicated.

        The value here is exactly the case the four-layer stack was built
        for: a scanner that streamed 8-of-10 findings before hitting the
        model's output-token ceiling used to lose all 8. Layer 1 salvages
        them; Layer 4 merges them with whatever the tighter-capped retry
        recovers. Net: more real signal than either attempt alone.
        """
        from kiro_crew.apps.builtins.writing_review import (
            _run_one_scanner as run_one_scanner_target,
        )
        from kiro_crew.apps.builtins.writing_review.pool import TruncatedResponseError

        first_attempt_partial = [
            {
                "section": "Intro",
                "paragraph": 1,
                "issue": "wordy sentence",
                "rule": "1",
                "severity": "medium",
                "proposed_fix": "shorten it",
                "confidence": "medium",
            },
            {
                "section": "Intro",
                "paragraph": 2,
                "issue": "passive voice",
                "rule": "2",
                "severity": "low",
                "proposed_fix": "make it active",
                "confidence": "high",
            },
        ]
        # Retry returns one overlap (paragraph=2 same rule => same id) plus
        # a new one (paragraph=3). The overlap must be dropped by the
        # ``id``-keyed dedup.
        retry_response = {
            "findings": [
                {
                    "section": "Intro",
                    "paragraph": 2,
                    "issue": "passive voice",
                    "rule": "2",
                    "severity": "low",
                    "proposed_fix": "make it active",
                    "confidence": "high",
                },
                {
                    "section": "Body",
                    "paragraph": 3,
                    "issue": "unsupported claim",
                    "rule": "3",
                    "severity": "high",
                    "proposed_fix": "add citation",
                    "confidence": "high",
                },
            ]
        }
        dispatch_calls = {"count": 0}

        async def truncate_then_succeed(_prompt):
            dispatch_calls["count"] += 1
            if dispatch_calls["count"] == 1:
                raise TruncatedResponseError(
                    "scanner response was truncated mid-output",
                    partial_findings=first_attempt_partial,
                )
            return retry_response

        fake_pool = _fake_pool_with_dispatch(side_effect=truncate_then_succeed)
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            merged_findings = await run_one_scanner_target(
                scanner_name="clarity",
                document_text="some doc text",
                context=ReviewContext(),
            )

        # Three distinct id values -- the paragraph=2 overlap survived
        # once, not twice.
        distinct_ids = {finding.id for finding in merged_findings}
        self.assertEqual(len(distinct_ids), 3)
        self.assertEqual(dispatch_calls["count"], 2)

    async def test_layer4_merges_when_retry_also_truncates(self) -> None:
        """Layer 4 — retry ALSO truncates, but salvages additional findings
        beyond the first attempt's. Both partial lists must be merged.
        Failing this test means the retry's truncation escaped without
        salvage, wasting a whole scan.
        """
        from kiro_crew.apps.builtins.writing_review import (
            _run_one_scanner as run_one_scanner_target,
        )
        from kiro_crew.apps.builtins.writing_review.pool import TruncatedResponseError

        first_attempt_partial = [
            {
                "section": "Intro",
                "paragraph": 1,
                "issue": "wordy",
                "rule": "1",
                "severity": "medium",
                "proposed_fix": "shorten",
                "confidence": "medium",
            }
        ]
        retry_partial = [
            {
                "section": "Body",
                "paragraph": 5,
                "issue": "missing evidence",
                "rule": "4",
                "severity": "high",
                "proposed_fix": "cite source",
                "confidence": "high",
            }
        ]

        async def both_truncate(_prompt):
            if _prompt.endswith(
                "high → medium → low. Skip advisory findings entirely on this retry."
            ):
                raise TruncatedResponseError(
                    "retry also truncated",
                    partial_findings=retry_partial,
                )
            raise TruncatedResponseError(
                "first attempt truncated",
                partial_findings=first_attempt_partial,
            )

        fake_pool = _fake_pool_with_dispatch(side_effect=both_truncate)
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            with self.assertRaises(TruncatedResponseError) as caught:
                await run_one_scanner_target(
                    scanner_name="clarity",
                    document_text="some doc text",
                    context=ReviewContext(),
                )

        # Even though both attempts truncated, the raised error carries
        # the merged salvage so a caller who catches it (currently just
        # the driver's failed_scanners recorder) could still surface
        # real findings. This is the compounding value of Layer 4.
        from kiro_crew.apps.builtins.writing_review import finding_id as compute_finding_id

        merged_partial_ids = {
            compute_finding_id(
                "clarity",
                finding_dict["section"],
                finding_dict["paragraph"],
                finding_dict["rule"],
            )
            for finding_dict in caught.exception.partial_findings
        }
        self.assertEqual(len(merged_partial_ids), 2)

    async def test_warns_on_majority_failure(self) -> None:
        async def always_fail(_prompt):
            raise RuntimeError("everything broken")

        fake_pool = _fake_pool_with_dispatch(side_effect=always_fail)
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            result = await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(),
            )

        self.assertTrue(result.partial_failure)
        self.assertEqual(len(result.failed_scanners), len(ALWAYS_ON_SCANNERS))

    async def test_conditional_email_scanner(self) -> None:
        fake_pool = _fake_pool_with_dispatch(return_value={"findings": []})
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            result = await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(doc_type="email"),
            )

        self.assertIn("email", result.scanners_run)
        self.assertNotIn("design", result.scanners_run)

    async def test_log_reference_populated(self) -> None:
        fake_pool = _fake_pool_with_dispatch(return_value={"findings": []})
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            result = await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(),
                review_id="test-review-id",
            )

        self.assertIn("path", result.log_reference)
        self.assertIn("search_hint", result.log_reference)
        self.assertIn("test-review-id", result.log_reference["search_hint"])

    async def test_run_scan_does_not_emit_on_phase_done(self) -> None:
        """``on_phase("done", ...)`` MUST NOT be emitted by ``run_scan``.

        The caller (``_run_scan_job`` in ``backend/routes.py``) is the sole
        writer of the terminal ``status="done"`` record. When ``run_scan``
        also fires an ``on_phase("done", ...)`` callback, the backend queues
        that as an ``asyncio.create_task`` that writes ``status="running"``.
        That queued task can execute after the completion write and clobber
        it — the review persists on disk but the job record stays stuck at
        ``running`` with ``phase="done"`` forever, hanging the frontend
        poll loop. Removing the terminal ``on_phase`` call keeps the
        contract clean: ``on_phase`` is progress only, terminal state is
        the caller's job. This is a belt-and-braces companion to the
        monotonic guard in ``_record_job_state``; the guard closes the
        class of races, this call removes the specific offender so the
        guard never has to fire in production.
        """
        captured_phase_names: list[str] = []

        def capturing_on_phase(phase_name: str, _detail: dict) -> None:
            captured_phase_names.append(phase_name)

        fake_pool = _fake_pool_with_dispatch(return_value={"findings": []})
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(),
                on_phase=capturing_on_phase,
            )

        self.assertNotIn(
            "done",
            captured_phase_names,
            "run_scan must not emit an on_phase('done') callback; the "
            "backend completion writer owns the terminal state.",
        )
        # Progress phases still fire, otherwise the frontend would never
        # see "fetch" / "scanner" / "cross_validate" tick over.
        self.assertIn("fetch", captured_phase_names)


class TestScannerToggle(unittest.IsolatedAsyncioTestCase):
    """Behaviour #15 -- an explicit ``False`` toggle removes a scanner."""

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.workspace_root = Path(self._tempdir.name)
        self.test_document = _write_test_document(self.workspace_root)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    async def test_respects_disabled_scanner(self) -> None:
        fake_pool = _fake_pool_with_dispatch(return_value={"findings": []})
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            result = await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(doc_type="design doc"),
                scanner_toggles={"design": False},
            )

        self.assertNotIn("design", result.scanners_run)
        # Design is the only conditional for "design doc"; the always-on
        # scanners must still run.
        for always_on_name in ALWAYS_ON_SCANNERS:
            self.assertIn(always_on_name, result.scanners_run)


class TestResolveScanners(unittest.TestCase):
    """_resolve_scanners: pure function that picks scanner names."""

    def test_email_doc_type_adds_email_scanner(self) -> None:
        chosen = _resolve_scanners(ReviewContext(doc_type="email"))
        self.assertIn("email", chosen)
        self.assertNotIn("design", chosen)

    def test_design_doc_type_adds_design_scanner(self) -> None:
        chosen = _resolve_scanners(ReviewContext(doc_type="Design Document"))
        self.assertIn("design", chosen)


class TestFindingId(unittest.TestCase):
    """finding_id is stable across calls with the same inputs."""

    def test_same_inputs_produce_same_id(self) -> None:
        first_id = finding_id("clarity", "Intro", 1, "1")
        second_id = finding_id("clarity", "Intro", 1, "1")
        self.assertEqual(first_id, second_id)

    def test_different_inputs_produce_different_ids(self) -> None:
        first_id = finding_id("clarity", "Intro", 1, "1")
        different_id = finding_id("clarity", "Intro", 1, "2")
        self.assertNotEqual(first_id, different_id)


class TestCrossValidation(unittest.IsolatedAsyncioTestCase):
    """Behaviour #10 -- synthesis pass populates cross_validation and conflicts."""

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.workspace_root = Path(self._tempdir.name)
        self.test_document = _write_test_document(self.workspace_root)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    async def test_populates_conflicts_field(self) -> None:
        import re as regex_module

        async def responder(prompt):
            # Synthesis pass: echo every input finding id back with a
            # conflicts verdict so the merge path is exercised.
            if "[WRITING-REVIEW SYNTHESIS PASS]" in prompt:
                input_ids = regex_module.findall(r'"id":\s*"([0-9a-f]+)"', prompt)
                return {
                    "results": [
                        {
                            "id": input_id,
                            "cross_validation": "conflicts",
                            "conflicts": ["structure rule 1 disagrees with clarity rule 3"],
                        }
                        for input_id in input_ids
                    ]
                }
            # Scanner pass: return one finding per scanner.
            return {
                "findings": [
                    {
                        "section": "Intro",
                        "paragraph": 1,
                        "issue": "test issue",
                        "rule": "1",
                        "severity": "medium",
                        "proposed_fix": "reword",
                    }
                ]
            }

        fake_pool = _fake_pool_with_dispatch(side_effect=responder)
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            result = await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(),
            )

        conflicting_findings = [
            finding for finding in result.findings if finding.cross_validation == "conflicts"
        ]
        self.assertGreaterEqual(len(conflicting_findings), 1)
        self.assertEqual(
            conflicting_findings[0].conflicts[0],
            "structure rule 1 disagrees with clarity rule 3",
        )

    async def test_redundant_findings_dropped_after_cross_validation(self) -> None:
        """Behaviour: findings the synthesis pass tags ``redundant`` MUST NOT
        appear in the ``run_scan`` result.

        The synthesis LLM identifies scanner overlap (multiple scanners
        flagging the same underlying issue from different angles) and tags
        the lower-priority duplicates as ``redundant``. Nothing consuming
        those tags is worse than not producing them at all -- the user
        sees an inflated finding count and can't tell which entries are
        genuine issues versus scanner overlap. Filtering here brings the
        surfaced count in line with the "distinct issues" number the
        author cares about (Watson feedback: 49 raw findings collapse to
        ~30 distinct concerns).

        ``conflicts`` findings MUST survive -- those are genuine tensions
        between scanners (one says "shorten this", another says "keep the
        detail"), and the author needs to see both sides to resolve them.
        """
        import re as regex_module

        # Patch the brief loader so all scanner briefs (and the synthesis
        # brief) collapse to short marker strings without example JSON.
        # The real ``synthesis.md`` includes example JSON with ``"id"``
        # and ``"scanner"`` fields; those confuse the responder's regexes
        # below because they can't tell finding JSON from documentation
        # JSON. Stubbing removes the ambiguity.
        stub_briefs: dict[str, str] = {
            "clarity": "# Clarity\n\nCLARITY_STUB_BRIEF",
            "naturalness": "# Naturalness\n\nNATURALNESS_STUB_BRIEF",
            "structure": "# Structure\n\nSTRUCTURE_STUB_BRIEF",
            "synthesis": "# Synthesis\n\nSYNTHESIS_STUB_BRIEF (no example JSON)",
        }

        def fake_load_brief(scanner_name: str) -> str:
            if scanner_name not in stub_briefs:
                raise FileNotFoundError(scanner_name)
            return stub_briefs[scanner_name]

        # Each scanner emits findings in a distinct section/paragraph so
        # the structural ``dedup_findings`` pass (which collapses by
        # location) does not collapse them before synthesis runs.
        section_by_scanner: dict[str, str] = {
            "clarity": "IntroClean",
            "naturalness": "IntroRedundant",
            "structure": "IntroConflicts",
        }
        scanner_names_seen: list[str] = []

        async def responder(prompt):
            if "[WRITING-REVIEW SYNTHESIS PASS]" in prompt:
                input_ids = regex_module.findall(r'"id":\s*"([0-9a-f]+)"', prompt)
                input_scanners = regex_module.findall(r'"scanner":\s*"([^"]+)"', prompt)
                tag_by_scanner: dict[str, str] = {
                    "clarity": "clean",
                    "naturalness": "redundant",
                    "structure": "conflicts",
                }
                return {
                    "results": [
                        {
                            "id": finding_id,
                            "cross_validation": tag_by_scanner.get(scanner_name, "clean"),
                            "conflicts": (
                                ["genuine tension"]
                                if tag_by_scanner.get(scanner_name) == "conflicts"
                                else []
                            ),
                        }
                        for finding_id, scanner_name in zip(input_ids, input_scanners)
                    ]
                }
            # Scanner pass -- match on the stubbed brief marker for a
            # deterministic scanner identification.
            for scanner_name, section_name in section_by_scanner.items():
                stub_marker = f"{scanner_name.upper()}_STUB_BRIEF"
                if stub_marker in prompt:
                    scanner_names_seen.append(scanner_name)
                    return {
                        "findings": [
                            {
                                "section": section_name,
                                "paragraph": 1,
                                "issue": f"issue from {scanner_name}",
                                "rule": "1",
                                "severity": "medium",
                                "proposed_fix": "reword",
                            }
                        ]
                    }
            return {"findings": []}

        fake_pool = _fake_pool_with_dispatch(side_effect=responder)
        with (
            patch(_POOL_GET_PATH, return_value=fake_pool),
            patch(
                "kiro_crew.apps.builtins.writing_review._load_scanner_brief",
                side_effect=fake_load_brief,
            ),
        ):
            result = await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(),
                scanner_toggles={scanner_name: False for scanner_name in ALWAYS_ON_SCANNERS}
                | {"clarity": True, "naturalness": True, "structure": True},
            )

        surfaced_cross_validation_values = {finding.cross_validation for finding in result.findings}
        self.assertIn(
            "clean",
            surfaced_cross_validation_values,
            f"scanners_seen={scanner_names_seen} findings={[(f.scanner, f.cross_validation) for f in result.findings]}",
        )
        self.assertIn("conflicts", surfaced_cross_validation_values)
        self.assertNotIn(
            "redundant",
            surfaced_cross_validation_values,
            "redundant findings must be dropped by the synthesis dedup filter",
        )


class TestDedupCollation(unittest.IsolatedAsyncioTestCase):
    """Slice 2 -- redundant findings collate under their primary.

    The cross-validation pass tags findings as ``clean`` / ``conflicts``
    / ``redundant`` and, for redundant ones, names the ``primary_id`` of
    the finding they duplicate. Instead of dropping the redundants, the
    collation step attaches each redundant's location to its primary's
    ``related_locations`` list. The user sees ONE card per underlying
    issue with an "Also appears in" list, not N cards for the same
    problem.

    Two edge cases are pinned here:

    * **Chain redundancy** (A -> B -> C): the two-pass resolver must
      collate A onto C, not leave A stranded pointing at B.
    * **Orphan redundancy**: if the LLM tags a finding redundant but
      names a ``primary_id`` that does not exist, the finding survives
      -- but demoted to ``cross_validation="clean"``. Anything less
      leaves the output with dangling ``"redundant"`` tags whose
      referent nothing consumes.
    """

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.workspace_root = Path(self._tempdir.name)
        self.test_document = _write_test_document(self.workspace_root)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def _finding_id_for(self, scanner_name: str, section: str, paragraph: int, rule: str) -> str:
        from kiro_crew.apps.builtins.writing_review import finding_id as compute_finding_id

        return compute_finding_id(scanner_name, section, paragraph, rule)

    async def _run_scan_with_synthesis_response(
        self,
        *,
        scanner_findings: dict[str, list[dict]],
        synthesis_results: list[dict],
    ):
        """Helper: run a scan with canned scanner and synthesis responses.

        ``scanner_findings`` maps scanner name to the list of finding
        dicts that scanner should return. ``synthesis_results`` is the
        list of ``{id, cross_validation, primary_id?}`` entries the
        synthesis pass will return. Only clarity/naturalness/structure
        run so tests do not need to enumerate every always-on scanner.
        """
        stub_briefs: dict[str, str] = {
            "clarity": "# Clarity\n\nCLARITY_STUB",
            "naturalness": "# Naturalness\n\nNATURALNESS_STUB",
            "structure": "# Structure\n\nSTRUCTURE_STUB",
        }

        def fake_load_brief(scanner_name: str) -> str:
            if scanner_name not in stub_briefs:
                raise FileNotFoundError(scanner_name)
            return stub_briefs[scanner_name]

        async def responder(prompt):
            if "[WRITING-REVIEW SYNTHESIS PASS]" in prompt:
                return {"results": synthesis_results}
            for scanner_name in scanner_findings:
                if f"# {scanner_name.capitalize()}" in prompt:
                    return {"findings": scanner_findings[scanner_name]}
            return {"findings": []}

        fake_pool = _fake_pool_with_dispatch(side_effect=responder)
        with (
            patch(_POOL_GET_PATH, return_value=fake_pool),
            patch(
                "kiro_crew.apps.builtins.writing_review._load_scanner_brief",
                side_effect=fake_load_brief,
            ),
        ):
            return await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(),
                scanner_toggles={scanner_name: False for scanner_name in ALWAYS_ON_SCANNERS}
                | {"clarity": True, "naturalness": True, "structure": True},
            )

    async def test_redundant_finding_collated_onto_primary(self) -> None:
        primary_finding_id = self._finding_id_for("clarity", "Intro", 1, "1")
        redundant_finding_id = self._finding_id_for("naturalness", "Body", 2, "1")

        result = await self._run_scan_with_synthesis_response(
            scanner_findings={
                "clarity": [
                    {
                        "section": "Intro",
                        "paragraph": 1,
                        "issue": "wordy",
                        "rule": "1",
                        "severity": "high",
                        "proposed_fix": "shorten",
                    }
                ],
                "naturalness": [
                    {
                        "section": "Body",
                        "paragraph": 2,
                        "issue": "same idea, different section",
                        "rule": "1",
                        "severity": "low",
                        "proposed_fix": "shorten too",
                    }
                ],
                "structure": [],
            },
            synthesis_results=[
                {"id": primary_finding_id, "cross_validation": "clean"},
                {
                    "id": redundant_finding_id,
                    "cross_validation": "redundant",
                    "primary_id": primary_finding_id,
                },
            ],
        )

        # The redundant finding must NOT appear as its own card...
        surfaced_ids = {finding.id for finding in result.findings}
        self.assertIn(primary_finding_id, surfaced_ids)
        self.assertNotIn(redundant_finding_id, surfaced_ids)
        # ...but its location must be listed on the primary's
        # ``related_locations``.
        primary_finding = next(f for f in result.findings if f.id == primary_finding_id)
        self.assertEqual(len(primary_finding.related_locations), 1)
        self.assertEqual(primary_finding.related_locations[0].section, "Body")
        self.assertEqual(primary_finding.related_locations[0].paragraph, 2)
        self.assertEqual(primary_finding.related_locations[0].scanner, "naturalness")

    async def test_chain_redundancy_flattens_to_terminal_primary(self) -> None:
        # A -> B -> C. Two-pass resolver must walk the chain so A and B
        # both land on C's ``related_locations``.
        a_finding_id = self._finding_id_for("clarity", "Intro", 1, "1")
        b_finding_id = self._finding_id_for("naturalness", "Body", 2, "1")
        c_finding_id = self._finding_id_for("structure", "Body", 3, "1")

        result = await self._run_scan_with_synthesis_response(
            scanner_findings={
                "clarity": [
                    {
                        "section": "Intro",
                        "paragraph": 1,
                        "issue": "A",
                        "rule": "1",
                        "severity": "low",
                        "proposed_fix": "fix A",
                    }
                ],
                "naturalness": [
                    {
                        "section": "Body",
                        "paragraph": 2,
                        "issue": "B",
                        "rule": "1",
                        "severity": "medium",
                        "proposed_fix": "fix B",
                    }
                ],
                "structure": [
                    {
                        "section": "Body",
                        "paragraph": 3,
                        "issue": "C",
                        "rule": "1",
                        "severity": "high",
                        "proposed_fix": "fix C",
                    }
                ],
            },
            synthesis_results=[
                {
                    "id": a_finding_id,
                    "cross_validation": "redundant",
                    "primary_id": b_finding_id,
                },
                {
                    "id": b_finding_id,
                    "cross_validation": "redundant",
                    "primary_id": c_finding_id,
                },
                {"id": c_finding_id, "cross_validation": "clean"},
            ],
        )

        # Only C should surface.
        surfaced_ids = {finding.id for finding in result.findings}
        self.assertEqual(surfaced_ids, {c_finding_id})
        # C should have BOTH A and B in related_locations.
        c_finding = next(f for f in result.findings if f.id == c_finding_id)
        related_scanners = {location.scanner for location in c_finding.related_locations}
        self.assertEqual(related_scanners, {"clarity", "naturalness"})

    async def test_orphan_redundant_demoted_to_clean_and_kept(self) -> None:
        # LLM tagged the finding redundant but named a ``primary_id`` that
        # does not match any finding. The finding must survive so we do
        # not silently drop signal, but its ``cross_validation`` must be
        # demoted from ``"redundant"`` to ``"clean"`` -- keeping it as
        # ``"redundant"`` would leak an inert tag into the UI whose
        # referent nothing consumes.
        orphan_finding_id = self._finding_id_for("clarity", "Intro", 1, "1")

        result = await self._run_scan_with_synthesis_response(
            scanner_findings={
                "clarity": [
                    {
                        "section": "Intro",
                        "paragraph": 1,
                        "issue": "wordy",
                        "rule": "1",
                        "severity": "medium",
                        "proposed_fix": "shorten",
                    }
                ],
                "naturalness": [],
                "structure": [],
            },
            synthesis_results=[
                {
                    "id": orphan_finding_id,
                    "cross_validation": "redundant",
                    "primary_id": "nonexistent_id",
                }
            ],
        )

        surfaced_ids = {finding.id for finding in result.findings}
        self.assertIn(orphan_finding_id, surfaced_ids)
        orphan_finding = next(f for f in result.findings if f.id == orphan_finding_id)
        self.assertEqual(orphan_finding.cross_validation, "clean")

    async def test_primary_id_cycle_treated_as_orphan(self) -> None:
        # Guards :func:`_resolve_primary_chain` against A -> B -> A style
        # cycles the LLM could emit under confused tagging. Cycle members
        # must NOT collate onto each other (which would silently drop
        # signal and could recurse infinitely on a naive resolver); the
        # cycle-safety guard demotes them all to ``clean`` orphans so
        # every finding still reaches the user.
        #
        # Placed at DIFFERENT paragraphs so the pre-synthesis
        # ``dedup_findings`` pass does not collapse them before the
        # synthesis tagging runs -- the guard under test lives in the
        # collation phase, not the dedup phase.
        clarity_finding_id = self._finding_id_for("clarity", "Intro", 1, "1")
        naturalness_finding_id = self._finding_id_for("naturalness", "Body", 2, "1")

        result = await self._run_scan_with_synthesis_response(
            scanner_findings={
                "clarity": [
                    {
                        "section": "Intro",
                        "paragraph": 1,
                        "issue": "wordy",
                        "rule": "1",
                        "severity": "medium",
                        "proposed_fix": "shorten",
                    }
                ],
                "naturalness": [
                    {
                        "section": "Body",
                        "paragraph": 2,
                        "issue": "AI phrasing",
                        "rule": "1",
                        "severity": "medium",
                        "proposed_fix": "rewrite",
                    }
                ],
                "structure": [],
            },
            synthesis_results=[
                {
                    "id": clarity_finding_id,
                    "cross_validation": "redundant",
                    "primary_id": naturalness_finding_id,
                },
                {
                    "id": naturalness_finding_id,
                    "cross_validation": "redundant",
                    "primary_id": clarity_finding_id,
                },
            ],
        )

        surfaced_ids = {finding.id for finding in result.findings}
        # Both cycle members must survive; neither should have collated
        # onto the other and neither should be marked redundant in the
        # final surface (redundant would leak an inert tag whose primary
        # never resolves).
        self.assertIn(clarity_finding_id, surfaced_ids)
        self.assertIn(naturalness_finding_id, surfaced_ids)
        for surfaced_finding in result.findings:
            if surfaced_finding.id in {
                clarity_finding_id,
                naturalness_finding_id,
            }:
                self.assertEqual(surfaced_finding.cross_validation, "clean")
                self.assertEqual(surfaced_finding.related_locations, [])


class TestSynthesisPromptPrimaryId(unittest.TestCase):
    """Slice 3 -- ``_synthesis_prompt`` must instruct the LLM to emit ``primary_id``.

    The collation logic in :func:`run_scan` walks each redundant
    finding's ``primary_id`` chain to resolve which primary it collates
    onto. Without the LLM emitting that field, every redundant finding
    falls into the orphan path -- correct behaviour by design, but the
    happy path is unreachable and the app's finding-count collapse
    never materialises. This test pins the instruction so a
    well-meaning prompt cleanup does not silently drop it.
    """

    def test_prompt_asks_llm_for_primary_id_on_redundant(self) -> None:
        from kiro_crew.apps.builtins.writing_review import _synthesis_prompt

        rendered_prompt = _synthesis_prompt(document_text="doc", findings=[])
        self.assertIn("primary_id", rendered_prompt)


class TestScanResultDocName(unittest.IsolatedAsyncioTestCase):
    """``ScanResult.doc_name`` shows the human filename, not the uuid storage key.

    Files uploaded through the browse-file flow are stored on disk as
    ``{uuid_hex_16}_original_name.md`` so two uploads with the same original
    filename cannot clobber each other. The user shouldn't see the uuid —
    the review record's ``doc_name`` strips the prefix so the sidebar and
    review detail show ``original_name.md`` instead of the storage key.

    A ``doc_path`` that DOESN'T start with a uuid pattern (e.g. the user
    pointed at their own file with ``doc_path='./notes.md'``) is passed
    through untouched — the strip only fires against our own storage
    convention.
    """

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.workspace_root = Path(self._tempdir.name)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    async def test_uuid_prefix_is_stripped_from_stashed_doc_name(self) -> None:
        # Storage path shape ``.../<16 hex>_hapi.md``.
        uuid_prefixed_path = self.workspace_root / "abcd1234efab5678_hapi.md"
        uuid_prefixed_path.write_text("# Title\n\nBody paragraph.\n", encoding="utf-8")

        fake_pool = SimpleNamespace(
            begin_batch=AsyncMock(return_value=None),
            end_batch=AsyncMock(return_value=None),
            resize=lambda *_a, **_k: None,
            dispatch=AsyncMock(return_value={"findings": []}),
        )
        with patch(
            "kiro_crew.apps.builtins.writing_review.get_pool",
            return_value=fake_pool,
        ):
            result = await run_scan(
                doc_path=uuid_prefixed_path,
                context=ReviewContext(),
            )
        self.assertEqual(result.doc_name, "hapi.md")

    async def test_non_uuid_prefixed_doc_name_passes_through(self) -> None:
        plain_path = self.workspace_root / "notes.md"
        plain_path.write_text("# Title\n\nBody.\n", encoding="utf-8")

        fake_pool = SimpleNamespace(
            begin_batch=AsyncMock(return_value=None),
            end_batch=AsyncMock(return_value=None),
            resize=lambda *_a, **_k: None,
            dispatch=AsyncMock(return_value={"findings": []}),
        )
        with patch(
            "kiro_crew.apps.builtins.writing_review.get_pool",
            return_value=fake_pool,
        ):
            result = await run_scan(
                doc_path=plain_path,
                context=ReviewContext(),
            )
        self.assertEqual(result.doc_name, "notes.md")


class TestScannerPromptOrderingCap(unittest.TestCase):
    """Behaviour: prompt tells the model to order by severity + cap at 10.

    The model's max_output_tokens is around 4096 for the default Claude,
    which is genuinely small when a heavy scanner (e.g. ``design``) runs
    on a long doc — output truncates mid-string and any findings that
    hadn't been emitted yet are lost.

    The cap directive is the first line of defence: instructing the model
    to return AT MOST 10 findings, ordered by severity (high first) means
    truncation, if it fires at all, drops the LOWEST-severity tail rather
    than a random slice. The post-filter in ``run_scan`` (Slice 2) is the
    guarantee that the cap holds regardless of how well the model obeys.
    """

    def test_prompt_demands_severity_ordering(self) -> None:
        prompt = _scanner_prompt(
            scanner_name="clarity",
            scanner_brief="# Clarity",
            document_text="doc",
            context=ReviewContext(),
        )
        self.assertIn("high", prompt.lower())
        # The prompt must explicitly instruct the model to sort by severity —
        # a brief matching "severity" alone (which was true before this
        # slice) would let the model return findings in any order it likes.
        self.assertRegex(prompt, r"(?i)order.*(severity|high.*medium.*low)")

    def test_prompt_demands_at_most_ten_findings(self) -> None:
        prompt = _scanner_prompt(
            scanner_name="clarity",
            scanner_brief="# Clarity",
            document_text="doc",
            context=ReviewContext(),
        )
        # The cap number is exposed in the prompt as an explicit number.
        # The exact wording can evolve; the invariant is "10 finding cap
        # is present in the response contract".
        self.assertIn("10", prompt)
        self.assertRegex(prompt, r"(?i)(at most|max(imum)?|no more than)\s+10")


class TestPerScannerFindingCap(unittest.IsolatedAsyncioTestCase):
    """A scanner that returns >10 findings is capped to 10 server-side.

    The prompt directive in ``_PROMPT_RESPONSE_FORMAT`` (Slice 1) asks the
    model to self-limit; this cap is the guarantee that the limit holds
    regardless of how well the model obeyed. Ordering matters — we keep
    the 10 HIGHEST-severity findings, so a scanner returning 12 findings
    with 8 "high" + 4 "low" surfaces all 8 highs and 2 lows.
    """

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.workspace_root = Path(self._tempdir.name)
        self.test_document = _write_test_document(self.workspace_root)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    async def test_scanner_returning_more_than_ten_is_capped(self) -> None:
        # 8 high + 4 low = 12 findings. Cap trims to 10 (all 8 high + top 2 low).
        oversized_response = {
            "findings": [
                {
                    "section": f"section-{index}",
                    "paragraph": 1,
                    "issue": f"high-severity issue {index}",
                    "rule": "1",
                    "severity": "high",
                    "proposed_fix": "fix",
                    "confidence": "high",
                }
                for index in range(8)
            ]
            + [
                {
                    "section": f"section-low-{index}",
                    "paragraph": 1,
                    "issue": f"low-severity issue {index}",
                    "rule": "2",
                    "severity": "low",
                    "proposed_fix": "fix",
                    "confidence": "medium",
                }
                for index in range(4)
            ]
        }
        fake_pool = _fake_pool_with_dispatch(return_value=oversized_response)
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            result = await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(),
                scanner_toggles={scanner_name: False for scanner_name in ALWAYS_ON_SCANNERS}
                | {"clarity": True},
            )

        clarity_findings = [f for f in result.findings if f.scanner == "clarity"]
        self.assertEqual(len(clarity_findings), 10)
        # All 8 highs must survive; the 2 remaining slots go to the lows.
        severities_in_order = [f.severity for f in clarity_findings]
        self.assertEqual(severities_in_order.count("high"), 8)
        self.assertEqual(severities_in_order.count("low"), 2)
        # High findings come first in the ordering.
        self.assertEqual(severities_in_order[:8], ["high"] * 8)

    async def test_scanner_returning_fewer_than_ten_is_untouched(self) -> None:
        normal_response = {
            "findings": [
                {
                    "section": f"section-{index}",
                    "paragraph": 1,
                    "issue": f"issue {index}",
                    "rule": str(index),
                    "severity": "medium",
                    "proposed_fix": "fix",
                    "confidence": "medium",
                }
                for index in range(3)
            ]
        }
        fake_pool = _fake_pool_with_dispatch(return_value=normal_response)
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            result = await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(),
                scanner_toggles={scanner_name: False for scanner_name in ALWAYS_ON_SCANNERS}
                | {"clarity": True},
            )

        clarity_findings = [f for f in result.findings if f.scanner == "clarity"]
        self.assertEqual(len(clarity_findings), 3)


class TestScannerTruncationRetry(unittest.IsolatedAsyncioTestCase):
    """Behaviour: a scanner that trips ``TruncatedResponseError`` gets one
    retry with a stricter cap prompt before we surface the failure.

    Even with the ``at most 10`` directive in the base prompt (Slice 1)
    and the server-side cap (Slice 2), an under-obedient model can still
    emit 15 findings and truncate at the ceiling. The retry rescues this
    case cheaply: dispatch once with a "top 5 highest severity only"
    suffix, take those findings, ship them. Only if the retry ALSO
    truncates does the scanner report ``truncated_response`` — the user
    then sees the failure banner but only for genuinely pathological
    cases, not everyday variability.
    """

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.workspace_root = Path(self._tempdir.name)
        self.test_document = _write_test_document(self.workspace_root)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    async def test_truncation_triggers_retry_with_stricter_cap(self) -> None:
        from kiro_crew.apps.builtins.writing_review.pool import TruncatedResponseError

        captured_prompts: list[str] = []

        async def flaky_dispatch(prompt_text: str):
            captured_prompts.append(prompt_text)
            # Filter for scanner-clarity prompts specifically; the
            # cross-validation synthesis call arrives on the same pool
            # and is not what this retry test is measuring.
            is_synthesis_pass = "SYNTHESIS PASS" in prompt_text
            if is_synthesis_pass:
                return {"results": []}
            scanner_prompts_so_far = [p for p in captured_prompts if "SYNTHESIS PASS" not in p]
            if len(scanner_prompts_so_far) == 1:
                raise TruncatedResponseError("cut off mid-string")
            return {
                "findings": [
                    {
                        "section": "intro",
                        "paragraph": 1,
                        "issue": "issue",
                        "rule": "1",
                        "severity": "high",
                        "proposed_fix": "fix",
                        "confidence": "high",
                    }
                ]
            }

        fake_pool = _fake_pool_with_dispatch(side_effect=flaky_dispatch)
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            result = await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(),
                scanner_toggles={scanner_name: False for scanner_name in ALWAYS_ON_SCANNERS}
                | {"clarity": True},
            )

        scanner_prompts = [p for p in captured_prompts if "SYNTHESIS PASS" not in p]
        # Retry fired exactly once — two scanner-dispatch calls total.
        self.assertEqual(len(scanner_prompts), 2)
        # Second prompt carries an explicit stricter cap directive.
        self.assertRegex(scanner_prompts[1], r"(?i)(top|only)\s+5")
        # Retry findings surface on the review, no ``truncated_response``
        # failure record was written for this scanner.
        self.assertEqual(len(result.findings), 1)
        self.assertFalse(
            any(fs.name == "clarity" for fs in result.failed_scanners),
            "clarity should have recovered via retry, not appeared in failed_scanners",
        )

    async def test_truncation_on_retry_surfaces_as_failure(self) -> None:
        from kiro_crew.apps.builtins.writing_review.pool import TruncatedResponseError

        async def always_truncates(_prompt: str):
            raise TruncatedResponseError("still truncated after retry")

        fake_pool = _fake_pool_with_dispatch(side_effect=always_truncates)
        with patch(_POOL_GET_PATH, return_value=fake_pool):
            result = await run_scan(
                doc_path=self.test_document,
                context=ReviewContext(),
                scanner_toggles={scanner_name: False for scanner_name in ALWAYS_ON_SCANNERS}
                | {"clarity": True},
            )

        # Retry ALSO failed → clarity ends up in failed_scanners with
        # the ``truncated_response`` reason_class.
        clarity_failure = next(
            (fs for fs in result.failed_scanners if fs.name == "clarity"),
            None,
        )
        self.assertIsNotNone(clarity_failure)
        assert clarity_failure is not None  # for the type checker
        self.assertEqual(clarity_failure.reason_class, "truncated_response")
