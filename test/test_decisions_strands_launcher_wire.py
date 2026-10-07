"""The Strands Decider launcher's ASGI wrappers: null rubrics and one decision at a time.

Kiro Crew's wire format types each option's rubric as nullable and sends ``null``
for an option with none; Strands Decider's server types every rubric as a string
and answers 422 to a null one, so without the rewrite every decision would be
skipped. Its endpoint also runs every request concurrently on a thread pool, so
the launcher admits one decision at a time and refuses one that cannot start in
time. The launcher runs in the model's own environment and imports torch and
strands_decider at module level, so these tests load only its wrapper
definitions from source and drive them as plain ASGI with no server.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

from kiro_crew.decisions.impl_jev import _to_wire
from kiro_crew.decisions.types import Choice

_LAUNCHER = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "decisions"
    / "local_servers"
    / "strands_decider_cpu.py"
)
_NAMES = {"_blank_null_criteria", "_NullCriteriaToEmpty", "_OneAtATime"}


def _load() -> dict:
    tree = ast.parse(_LAUNCHER.read_text(encoding="utf-8"))
    defs: list[ast.stmt] = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in _NAMES
    ]
    assert {d.name for d in defs if isinstance(d, (ast.FunctionDef, ast.ClassDef))} == _NAMES
    # The launcher's own imports are not lifted, so the names its definitions
    # use are seeded here; compiling with dont_inherit keeps this module's
    # __future__ flags from deciding whether its annotations evaluate.
    namespace: dict = {"json": json, "Any": Any, "asyncio": asyncio}
    code = compile(
        ast.Module(body=defs, type_ignores=[]), str(_LAUNCHER), "exec", dont_inherit=True
    )
    # Executes definitions parsed from a tracked repository file, no external input.
    # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
    exec(code, namespace)  # noqa: S102
    return namespace


_NS = _load()
_blank = _NS["_blank_null_criteria"]
_Wrapper = _NS["_NullCriteriaToEmpty"]
_Gate = _NS["_OneAtATime"]


def _request(criteria: dict) -> dict:
    return {"questions": {"q": {"type": "choice", "criteria": criteria}}, "state": {}}


def test_the_client_sends_null_rubrics_and_they_arrive_as_empty_strings() -> None:
    wire = _to_wire({}, "strands-decider-2b", [Choice(id="q", prompt="Which?", options=["a", "b"])])
    assert wire["questions"]["q"]["criteria"] == {"a": None, "b": None}

    out = json.loads(_blank(json.dumps(wire).encode()))

    assert out["questions"]["q"]["criteria"] == {"a": "", "b": ""}
    assert out["questions"]["q"]["instructions"] == "Which?"
    assert out["model"] == "strands-decider-2b"


def test_rubric_text_and_list_criteria_are_kept() -> None:
    body = json.dumps(_request({"a": None, "b": "pick b when it rains"})).encode()
    body_score = json.dumps(
        {"questions": {"s": {"type": "score", "criteria": ["low", "high"]}}}
    ).encode()

    out = json.loads(_blank(body))

    assert out["questions"]["q"]["criteria"] == {"a": "", "b": "pick b when it rains"}
    assert json.loads(_blank(body_score))["questions"]["s"]["criteria"] == ["low", "high"]


def test_bodies_without_criteria_pass_through_byte_identical() -> None:
    for body in (b"not json", b"[1, 2]", json.dumps({"questions": []}).encode()):
        assert _blank(body) == body


def _drive(path: str, chunks: list[bytes]) -> tuple[bytes, dict]:
    seen: dict = {}

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        message = await receive()
        seen["body"] = message["body"]
        seen["headers"] = dict(scope["headers"])

    pending = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]

    async def receive():  # type: ignore[no-untyped-def]
        return pending.pop(0)

    raw = b"".join(chunks)
    scope = {
        "type": "http",
        "path": path,
        "headers": [(b"content-length", str(len(raw)).encode()), (b"x-other", b"1")],
    }
    asyncio.run(_Wrapper(inner)(scope, receive, None))
    return seen["body"], seen["headers"]


def test_decision_requests_reach_the_app_rewritten_with_a_matching_length() -> None:
    raw = json.dumps(_request({"a": None, "b": None})).encode()

    body, headers = _drive("/v1/systemone", [raw[:10], raw[10:]])

    assert json.loads(body)["questions"]["q"]["criteria"] == {"a": "", "b": ""}
    assert headers[b"content-length"] == str(len(body)).encode()
    assert headers[b"x-other"] == b"1"


def test_other_routes_are_not_touched() -> None:
    raw = json.dumps(_request({"a": None})).encode()

    body, _ = _drive("/kirocrew-attest", [raw])

    assert body == raw


#: Admission wait for a request that must be admitted. It only bounds a run that
#: has lost its release; every ordering below is driven by events, not by time.
_LOST_RUN_SECS = 30.0
#: How long a held decision waits for its release before failing the test, so a
#: gate that admits a second decision fails instead of blocking the run.
_HOLD_BOUND_SECS = 5.0
_DECIDE = "/v1/systemone"


class _Inference:
    """Stand-in app: a decision holds until ``release`` is set; other routes do not."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.now = 0
        self.peak = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        self.now += 1
        self.peak = max(self.peak, self.now)
        self.entered.set()
        try:
            if scope["path"] == _DECIDE:
                await asyncio.wait_for(self.release.wait(), _HOLD_BOUND_SECS)
            if self.fail:
                raise RuntimeError("inference failed")
            await send({"type": "http.response.start", "status": 200, "headers": []})
        finally:
            self.now -= 1


async def _call(gate: Any, path: str) -> Any:
    sent: list = []

    async def send(message):  # type: ignore[no-untyped-def]
        sent.append(message)

    try:
        await gate({"type": "http", "path": path, "headers": []}, None, send)
    except RuntimeError:
        return "raised"
    return sent[0]["status"]


async def _yield_to_others() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def test_a_decision_that_cannot_start_in_time_is_refused_with_503() -> None:
    async def main() -> tuple:
        app = _Inference()
        gate = _Gate(app, 0.01)
        first = asyncio.create_task(_call(gate, _DECIDE))
        await app.entered.wait()
        # The first decision holds the gate until both others have been refused.
        refused = await asyncio.gather(_call(gate, _DECIDE), _call(gate, _DECIDE))
        app.release.set()
        return await first, list(refused), app.peak

    first, refused, peak = asyncio.run(main())

    assert first == 200
    assert refused == [503, 503]
    assert peak == 1


def test_decisions_that_fit_in_the_wait_run_one_after_another() -> None:
    async def main() -> tuple:
        app = _Inference()
        gate = _Gate(app, _LOST_RUN_SECS)
        first = asyncio.create_task(_call(gate, _DECIDE))
        await app.entered.wait()
        second = asyncio.create_task(_call(gate, _DECIDE))
        await _yield_to_others()
        running_while_held = app.now
        app.release.set()
        return list(await asyncio.gather(first, second)), running_while_held, app.peak

    outcomes, running_while_held, peak = asyncio.run(main())

    assert outcomes == [200, 200]
    assert running_while_held == 1
    assert peak == 1


def test_a_failed_inference_releases_the_gate() -> None:
    async def main() -> list:
        app = _Inference(fail=True)
        app.release.set()
        gate = _Gate(app, _LOST_RUN_SECS)
        return [await _call(gate, _DECIDE), await _call(gate, _DECIDE)]

    assert asyncio.run(main()) == ["raised", "raised"]


def test_other_routes_bypass_the_gate() -> None:
    async def main() -> tuple:
        app = _Inference()
        gate = _Gate(app, 0.01)
        held = asyncio.create_task(_call(gate, _DECIDE))
        await app.entered.wait()
        # The decision still holds the gate, so a gated health check could only 503.
        health = await _call(gate, "/health")
        app.release.set()
        return await held, health, app.peak

    held, health, peak = asyncio.run(main())

    assert (held, health) == (200, 200)
    assert peak == 2
