"""The Strands Decider launcher's request rewrite: null rubrics reach it as ``""``.

Kiro Crew's wire format types each option's rubric as nullable and sends ``null``
for an option with none; Strands Decider's server types every rubric as a string
and answers 422 to a null one, so without the rewrite every decision would be
skipped. The launcher runs in the model's own environment and imports torch and
strands_decider at module level, so these tests load only its two rewrite
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
_NAMES = {"_blank_null_criteria", "_NullCriteriaToEmpty"}


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
    namespace: dict = {"json": json, "Any": Any}
    code = compile(
        ast.Module(body=defs, type_ignores=[]), str(_LAUNCHER), "exec", dont_inherit=True
    )
    # Executes two definitions parsed from a tracked repository file, no external input.
    # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
    exec(code, namespace)  # noqa: S102
    return namespace


_NS = _load()
_blank = _NS["_blank_null_criteria"]
_Wrapper = _NS["_NullCriteriaToEmpty"]


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
