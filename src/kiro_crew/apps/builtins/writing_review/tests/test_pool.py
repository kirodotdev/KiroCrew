"""Unit tests for the scanner worker pool.

Uses FakeRuntime/FakeHandle pattern from code_review_sage/tests/test_review_pool.py.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from kiro_crew.apps.builtins.writing_review import pool as pool_module
from kiro_crew.apps.builtins.writing_review.pool import (
    DEFAULT_MAX_CONCURRENT,
    MAX_CONCURRENT_CEIL,
    ScannerPool,
    TruncatedResponseError,
    _parse_first_json_object,
)

# --- Fakes (same pattern as sage) ---


def _ev(kind, **kwargs):
    return type("Ev", (), {"kind": kind, **kwargs})()


class FakeHandle:
    def __init__(self, runtime, session_id, script=None):
        self.session_id = session_id
        self._script = script or []
        self.destroyed = False

    async def prompt(self, message, timeout=0):
        for event in self._script:
            yield event
        yield _ev(pool_module.EVENT_COMPLETE, stop_reason="end_turn")

    async def approve_tool(self, request_id):
        pass

    async def destroy(self):
        self.destroyed = True


class FakeRuntime:
    instances: list = []

    def __init__(self, agent=None, work_dir=None, sandbox_mode="auto"):
        self.spawned = False
        self.killed = False
        self._seq = 0
        self.script: list = []
        FakeRuntime.instances.append(self)

    def is_alive(self):
        return self.spawned and not self.killed

    async def spawn(self):
        self.spawned = True

    async def kill(self, *, expected=False):
        self.killed = True

    async def create_session(self, cwd=None, agent=None):
        self._seq += 1
        return FakeHandle(self, f"s{self._seq}", script=list(self.script))


def _install_fake_runtime(test_case, script=None):
    FakeRuntime.instances = []

    def factory(agent=None, work_dir=None, sandbox_mode="auto"):
        runtime = FakeRuntime(agent=agent, work_dir=work_dir, sandbox_mode=sandbox_mode)
        runtime.script = script or []
        return runtime

    original = pool_module.AcpRuntime
    # ``setattr`` (mirroring the cleanup line just below) is what keeps mypy
    # ``[misc, assignment]`` happy — direct assignment to a class-typed
    # module attribute is flagged as ``Cannot assign to a type``.
    setattr(pool_module, "AcpRuntime", factory)
    test_case.addCleanup(lambda: setattr(pool_module, "AcpRuntime", original))


class TestPoolConstruction(unittest.IsolatedAsyncioTestCase):
    """Constructor clamps and defaults."""

    async def test_defaults_match_constants(self):
        _install_fake_runtime(self)
        scanner_pool = ScannerPool()
        # The default is 9 -- the maximum parallel scanner wave:
        # 8 always-on scanners plus at most one conditional scanner
        # (design XOR email). The concurrent-scan guard in the UI blocks
        # a second scan from starting while one is in flight, so no more
        # than one wave is ever admitted at a time.
        self.assertEqual(scanner_pool._max_concurrent, DEFAULT_MAX_CONCURRENT)
        self.assertEqual(DEFAULT_MAX_CONCURRENT, 9)

    async def test_ceiling_clamps_max_concurrent(self):
        _install_fake_runtime(self)
        scanner_pool = ScannerPool(max_concurrent=999)
        self.assertEqual(scanner_pool._max_concurrent, MAX_CONCURRENT_CEIL)
        # Ceiling collapsed onto the default because the concurrent-scan
        # guard means the pool never needs to admit a second wave.
        self.assertEqual(MAX_CONCURRENT_CEIL, 9)


class TestPoolLifecycle(unittest.IsolatedAsyncioTestCase):
    """Behaviour #11, #12 -- ref-counted runtime lifecycle."""

    async def test_lazy_no_runtime_until_begin_batch(self):
        _install_fake_runtime(self)
        ScannerPool(max_concurrent=3)
        self.assertEqual(FakeRuntime.instances, [])

    async def test_begin_batch_spawns_runtime(self):
        _install_fake_runtime(
            self,
            script=[_ev(pool_module.EVENT_TEXT_CHUNK, text='{"findings":[]}')],
        )
        scanner_pool = ScannerPool(max_concurrent=3)
        await scanner_pool.begin_batch()
        self.assertEqual(len(FakeRuntime.instances), 1)
        self.assertTrue(FakeRuntime.instances[0].spawned)
        await scanner_pool.end_batch()

    async def test_end_batch_kills_runtime_when_last(self):
        _install_fake_runtime(
            self,
            script=[_ev(pool_module.EVENT_TEXT_CHUNK, text='{"findings":[]}')],
        )
        scanner_pool = ScannerPool(max_concurrent=3)
        await scanner_pool.begin_batch()
        await scanner_pool.dispatch("warmup")
        await scanner_pool.end_batch()
        self.assertTrue(FakeRuntime.instances[0].killed)

    async def test_overlapping_batches_share_runtime(self):
        """Two concurrent begin_batch calls reuse the same runtime; only the last end_batch kills it."""
        _install_fake_runtime(
            self,
            script=[_ev(pool_module.EVENT_TEXT_CHUNK, text='{"findings":[]}')],
        )
        scanner_pool = ScannerPool(max_concurrent=3)
        await scanner_pool.begin_batch()  # first scan enters
        await scanner_pool.begin_batch()  # second scan enters
        self.assertEqual(len(FakeRuntime.instances), 1)  # only one runtime spawned

        await scanner_pool.end_batch()  # first scan drains
        self.assertFalse(FakeRuntime.instances[0].killed)  # runtime kept alive for second scan

        await scanner_pool.end_batch()  # last scan drains -> runtime dies
        self.assertTrue(FakeRuntime.instances[0].killed)


class TestPoolConcurrency(unittest.IsolatedAsyncioTestCase):
    """Semaphore bounds concurrent dispatches; resize takes effect on next batch."""

    async def test_concurrency_bounded_by_semaphore(self):
        # Instrument at the ``_run_one_session`` boundary rather than
        # inside the async generator ``prompt`` — ``async for`` consumers
        # that ``break`` early leave the generator ``finally`` block
        # unrun until GC, which makes generator-level enter/exit counting
        # unreliable. A regular async function has clean try/finally
        # semantics.
        concurrent_in_flight = 0
        max_concurrent_observed = 0

        _install_fake_runtime(
            self,
            script=[_ev(pool_module.EVENT_TEXT_CHUNK, text='{"findings":[]}')],
        )
        scanner_pool = ScannerPool(max_concurrent=2)

        original_run_one_session = scanner_pool._run_one_session

        async def instrumented_run_one_session(runtime, prompt_text):
            nonlocal concurrent_in_flight, max_concurrent_observed
            concurrent_in_flight += 1
            max_concurrent_observed = max(max_concurrent_observed, concurrent_in_flight)
            try:
                # Yield to the loop so other pending dispatches actually
                # get a chance to try to enter the semaphore body while
                # this one is still holding a slot.
                await asyncio.sleep(0.01)
                return await original_run_one_session(runtime, prompt_text)
            finally:
                concurrent_in_flight -= 1

        scanner_pool._run_one_session = instrumented_run_one_session  # type: ignore[method-assign]

        await scanner_pool.begin_batch()
        try:
            results = await asyncio.gather(
                *[scanner_pool.dispatch(f"task {index}") for index in range(5)]
            )
        finally:
            await scanner_pool.end_batch()
        self.assertEqual(len(results), 5)
        self.assertLessEqual(
            max_concurrent_observed,
            2,
            "semaphore did not bound in-flight dispatches to max_concurrent",
        )

    async def test_resize_updates_semaphore(self):
        _install_fake_runtime(self)
        scanner_pool = ScannerPool(max_concurrent=3)
        # Pick an in-range value below the ceiling so the resize is not
        # clamped away by ``test_resize_clamps_to_ceiling``'s codepath.
        scanner_pool.resize(7)
        self.assertEqual(scanner_pool._max_concurrent, 7)

    async def test_resize_clamps_to_ceiling(self):
        _install_fake_runtime(self)
        scanner_pool = ScannerPool(max_concurrent=3)
        scanner_pool.resize(999)
        self.assertEqual(scanner_pool._max_concurrent, MAX_CONCURRENT_CEIL)


class TestPoolShutdown(unittest.IsolatedAsyncioTestCase):
    """Explicit shutdown force-kills the runtime and rejects further dispatches."""

    async def test_shutdown_kills_runtime(self):
        _install_fake_runtime(
            self,
            script=[_ev(pool_module.EVENT_TEXT_CHUNK, text='{"findings":[]}')],
        )
        scanner_pool = ScannerPool(max_concurrent=3)
        await scanner_pool.begin_batch()
        await scanner_pool.dispatch("warmup")
        await scanner_pool.shutdown()
        self.assertTrue(FakeRuntime.instances[0].killed)

    async def test_dispatch_after_shutdown_raises(self):
        _install_fake_runtime(self)
        scanner_pool = ScannerPool(max_concurrent=3)
        await scanner_pool.shutdown()
        with self.assertRaises(RuntimeError):
            await scanner_pool.dispatch("test")


class TestPoolRetry(unittest.IsolatedAsyncioTestCase):
    """Behaviour #2 -- retry once on worker death."""

    async def test_retries_once_on_session_error_then_succeeds(self):
        call_count = {"n": 0}
        _install_fake_runtime(self)

        async def flaky_create(self_rt, cwd=None, agent=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("worker died")
            self_rt._seq += 1
            return FakeHandle(
                self_rt,
                f"s{self_rt._seq}",
                script=[_ev(pool_module.EVENT_TEXT_CHUNK, text='{"findings":[]}')],
            )

        with patch.object(FakeRuntime, "create_session", flaky_create):
            scanner_pool = ScannerPool(max_concurrent=3, retry_on_worker_death=1)
            await scanner_pool.begin_batch()
            try:
                result = await scanner_pool.dispatch("test")
                self.assertIn("findings", result)
            finally:
                await scanner_pool.end_batch()

    async def test_fails_after_retry_exhausted(self):
        _install_fake_runtime(self)

        async def always_fail(self_rt, cwd=None, agent=None):
            raise RuntimeError("worker died permanently")

        with patch.object(FakeRuntime, "create_session", always_fail):
            scanner_pool = ScannerPool(max_concurrent=3, retry_on_worker_death=1)
            await scanner_pool.begin_batch()
            try:
                with self.assertRaises(RuntimeError):
                    await scanner_pool.dispatch("test")
            finally:
                await scanner_pool.end_batch()


class TestParseFirstJsonObject(unittest.TestCase):
    """`_parse_first_json_object` — trailing prose, preamble, empty, truncation.

    Truncation cases are the reason ``TruncatedResponseError`` exists: the LLM
    hit its ``max_output_tokens`` and the last chunk we received ends mid-string
    (or mid-object). We MUST raise a distinct exception so the driver can
    classify the failure as ``truncated_response`` rather than the generic
    ``invalid_json`` — an operator reading the failed-scanners list should see
    "the model ran out of room" and not "your JSON was bad".
    """

    def test_parses_clean_object(self) -> None:
        parsed_object = _parse_first_json_object('{"findings":[]}')
        self.assertEqual(parsed_object, {"findings": []})

    def test_strips_leading_preamble(self) -> None:
        parsed_object = _parse_first_json_object('Here is your response:\n{"findings":[]}')
        self.assertEqual(parsed_object, {"findings": []})

    def test_ignores_trailing_commentary(self) -> None:
        parsed_object = _parse_first_json_object(
            '{"findings":[]}\n\nLet me know if you need anything else.'
        )
        self.assertEqual(parsed_object, {"findings": []})

    def test_empty_response_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            _parse_first_json_object("")
        with self.assertRaises(ValueError):
            _parse_first_json_object("   \n  ")

    def test_no_brace_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            _parse_first_json_object("no json here at all")

    def test_truncated_mid_string_raises_truncated_response_error(self) -> None:
        # Mimics the observed "design scanner cut off at 4096 output tokens"
        # failure -- the model opened a string and never closed it.
        truncated_output = '{"findings":[{"issue":"the design lacks'
        with self.assertRaises(TruncatedResponseError):
            _parse_first_json_object(truncated_output)

    def test_truncated_mid_object_raises_truncated_response_error(self) -> None:
        # Truncated after a comma but before the next key -- also a truncation
        # signal, not "bad JSON syntax". Parser reaches end of input with
        # an "Expecting property name" message at the final position.
        truncated_output = '{"findings":[{"issue":"a","rule":"1"},'
        with self.assertRaises(TruncatedResponseError):
            _parse_first_json_object(truncated_output)

    def test_truncated_response_error_is_a_value_error(self) -> None:
        # Callers that already catch ``ValueError`` (like the existing driver
        # ``_classify_scanner_failure`` fallback) MUST still catch this one so
        # nothing regresses if the classifier isn't updated in lockstep.
        self.assertTrue(issubclass(TruncatedResponseError, ValueError))


class TestParseFailureLogging(unittest.TestCase):
    """Layer 3 — raw-response preview logging on parse failure.

    A parse failure without the raw response text buried in the log is
    forensically opaque: an operator seeing ``invalid_json`` in gateway.log
    has no way to know whether the model wrote garbage or the pipeline
    truncated an otherwise-clean response. Emitting a bounded preview
    of the raw text alongside the exception turns every parse failure
    into a debuggable artefact.
    """

    def test_logs_raw_preview_when_response_is_truncated(self) -> None:
        truncated_output = '{"findings":[{"issue":"the design lacks'
        with patch.object(pool_module.logger, "warning") as mock_warning:
            with self.assertRaises(TruncatedResponseError):
                _parse_first_json_object(truncated_output)
        # One warning call, whose formatted output contains at least a
        # recognisable prefix of the raw response so an operator reading
        # the log can tell WHICH response failed.
        self.assertEqual(mock_warning.call_count, 1)
        formatted_message = mock_warning.call_args[0][0] % mock_warning.call_args[0][1:]
        self.assertIn('{"findings":[{"issue":"the design lacks', formatted_message)

    def test_long_raw_preview_is_bounded_and_marked(self) -> None:
        # A 600-char body exceeds the 500-char preview budget; the log
        # must show only the first 500 chars plus an explicit
        # ``[+N more chars]`` marker so no one thinks they saw the
        # full response.
        long_body = "x" * 600
        malformed_output = "{" + long_body
        with patch.object(pool_module.logger, "warning") as mock_warning:
            with self.assertRaises(ValueError):
                _parse_first_json_object(malformed_output)
        formatted_message = mock_warning.call_args[0][0] % mock_warning.call_args[0][1:]
        self.assertIn("[+", formatted_message)
        self.assertIn("more chars]", formatted_message)


class TestTruncationClassification(unittest.TestCase):
    """Layer 2 — brace-count truncation detection.

    The prior position-based heuristic (Spock #1) misclassified any parse
    failure at end-of-input as truncation, so a structurally-closed but
    semantically-malformed body like ``{"foo":}`` was reported as
    ``truncated_response`` and retried with a smaller output cap — which
    of course produced the same malformed body, wasting a whole second
    scan for no useful outcome. Structural brace-counting fixes that.
    """

    def test_unterminated_string_is_truncation(self) -> None:
        with self.assertRaises(TruncatedResponseError):
            _parse_first_json_object('{"foo":"unterminated')

    def test_missing_colon_value_is_truncation(self) -> None:
        # Cut off mid-object after a colon -- container is still open,
        # so any parse error here is truncation, not malformed.
        with self.assertRaises(TruncatedResponseError):
            _parse_first_json_object('{"foo":')

    def test_trailing_comma_cutoff_is_truncation(self) -> None:
        # Model emitted a comma then stopped -- the container is still
        # open by one ``{``; truncation.
        with self.assertRaises(TruncatedResponseError):
            _parse_first_json_object('{"foo":1,')

    def test_structurally_closed_but_missing_value_is_malformed(self) -> None:
        # THE Spock #1 regression test: ``{"foo":}`` has balanced braces
        # and a parse failure at the ``}``. It is NOT truncation -- it
        # is a genuine bad-JSON response we cannot recover by retrying.
        # The parser fails on the closing brace; a raw ValueError (not
        # TruncatedResponseError) is the correct signal.
        with self.assertRaises(ValueError) as caught:
            _parse_first_json_object('{"foo":}')
        self.assertNotIsInstance(caught.exception, TruncatedResponseError)

    def test_bare_close_brace_is_malformed(self) -> None:
        # No opener at all -- cannot be truncation of an object we never
        # saw the start of. Malformed.
        with self.assertRaises(ValueError) as caught:
            _parse_first_json_object("}")
        self.assertNotIsInstance(caught.exception, TruncatedResponseError)


class TestExtractCompleteFindings(unittest.TestCase):
    """Layer 1 — partial JSON recovery from a truncated ``findings`` array.

    A response that truncates mid-array still contains completed finding
    objects before the cutoff, and those are real signal we currently
    discard. Salvaging them from the raw text (before the retry runs at
    all) recovers real data on every truncation failure — biggest data
    preservation win of the four-layer stack.
    """

    def _extract(self, raw_text: str):
        # Late-imported so a failing import surfaces in RED cleanly.
        from kiro_crew.apps.builtins.writing_review.pool import (
            _extract_complete_findings,
        )

        return _extract_complete_findings(raw_text)

    def test_complete_response_yields_every_finding(self) -> None:
        recovered = self._extract('{"findings":[{"a":1},{"b":2},{"c":3}]}')
        self.assertEqual(len(recovered), 3)
        self.assertEqual(recovered[0], {"a": 1})

    def test_mid_object_cutoff_recovers_all_completed(self) -> None:
        # Third object opened but not closed -- first two survived to
        # completion and are perfectly usable.
        recovered = self._extract('{"findings":[{"a":1},{"b":2},{"c":')
        self.assertEqual(len(recovered), 2)

    def test_mid_object_no_close_recovers_only_the_complete_one(self) -> None:
        # Second object mid-key, first is complete.
        recovered = self._extract('{"findings":[{"a":1},{"b":2')
        self.assertEqual(len(recovered), 1)

    def test_empty_findings_array_recovers_nothing(self) -> None:
        recovered = self._extract('{"findings":[]')
        self.assertEqual(recovered, [])

    def test_finding_with_brace_inside_string_is_intact(self) -> None:
        # A ``}`` inside a JSON string must NOT be mistaken for the
        # object's closing brace by the walker -- otherwise a real
        # finding with braces in its ``issue`` field would truncate
        # inside itself and produce a garbage partial.
        recovered = self._extract('{"findings":[{"a":"has}brace"}]}')
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["a"], "has}brace")

    def test_no_findings_key_recovers_nothing(self) -> None:
        recovered = self._extract("[]")
        self.assertEqual(recovered, [])


class TestTruncatedResponseErrorPartialFindings(unittest.TestCase):
    """Layer 1 wire contract (Option b) — partial findings attached to error."""

    def test_truncated_error_carries_partial_findings(self) -> None:
        # A truncated body with two complete findings before the cutoff
        # should raise ``TruncatedResponseError`` whose ``partial_findings``
        # attribute contains both.
        truncated_body = '{"findings":[{"a":1},{"b":2},{"c":'
        with self.assertRaises(TruncatedResponseError) as caught:
            _parse_first_json_object(truncated_body)
        self.assertEqual(len(caught.exception.partial_findings), 2)

    def test_truncated_error_partial_findings_empty_when_no_content(self) -> None:
        # A truncation before any finding completed still constructs the
        # error, but with an empty partial-findings list. Callers can
        # treat empty the same as "no salvage possible".
        truncated_body = '{"findings":[{"a":'
        with self.assertRaises(TruncatedResponseError) as caught:
            _parse_first_json_object(truncated_body)
        self.assertEqual(caught.exception.partial_findings, [])
