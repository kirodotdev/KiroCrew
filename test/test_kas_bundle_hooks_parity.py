"""The bundled engine route hands Kiro Crew's tool gate the same hook events as the relay.

Kiro Crew's PreToolUse gate (``HookManager.on_tool_call``) and the SEL tool records
are fed from three agent-to-client frame classes: the ``tool_call`` update, the
``session/request_permission`` request, and the ``tool_call_update`` frames through
``completed``. On the KAS relay route those shapes were captured live in
``test/fixtures/acp_frames/kas/session.jsonl``. ``agent.kas_bundle`` spawns the same
engine from Kiro Crew's own pinned bundle with no kiro-cli in between, and nothing
pinned that the gate still sees the same thing -- a regression there would be silent,
because the gate would simply not fire.

``kas/bundle-tool-path.jsonl`` is the live capture off that route (see its ``_meta``
note for the spawn and the reduction step). These tests lay the two captures side by
side, frame class by frame class, and then run both through the same dispatch and
gate code the product runs. They pin PARITY, not either route's absolute shape: the
replay corpus already pins each capture's events against its own snapshot.

Two things are deliberately not compared: the command text and the ids, which the
capture prompt and the engine chose, and ``rawInput.description``, which the model
authored on one run and not the other. A field the model writes is not a wire shape.

The ``_kiro/hooks/*`` surface is not part of this comparison because it is identical
by construction: ``KasHarness.client_capabilities`` is the one ``KAS_CLIENT_CAPABILITIES``
constant on both routes, it announces no ``hooks`` capability, and neither capture
carries a hooks request. The last test pins that, so flipping the announcement for one
route only cannot go unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from acp_frame_replay_harness import CORPUS, read_fixture, replay_frames

from kiro_crew.acp import kas_wire
from kiro_crew.acp._dispatch import build_permission_event, parse_session_update
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.types import KAS_CLIENT_CAPABILITIES, JsonRpcMessage
from kiro_crew.hook_runtime.tool_identity import hook_gate_kwargs
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig

RELAY = CORPUS / "kas" / "session.jsonl"
BUNDLE = CORPUS / "kas" / "bundle-tool-path.jsonl"

#: The engine tool both captures exercised. Everything the gate keys on for a shell
#: call hangs off this id, so a capture that ran a different tool compares nothing.
SHELL_TOOL_ID = "run_command"

#: A model-authored argument: present when the model chose to describe its call.
MODEL_AUTHORED_ARGS = frozenset({"description"})


# ── fixture access ──────────────────────────────────────────────────────────


def _frames(path: Path) -> list[dict[str, Any]]:
    _meta, frames = read_fixture(path)
    return frames


def _meta(path: Path) -> dict[str, Any]:
    meta, _frames = read_fixture(path)
    return meta


def _updates(frames: list[dict[str, Any]], discriminant: str) -> list[dict[str, Any]]:
    out = []
    for frame in frames:
        if frame.get("method") != "session/update":
            continue
        update = frame["params"]["update"]
        if update.get("sessionUpdate") == discriminant:
            out.append(update)
    return out


def _permission(frames: list[dict[str, Any]]) -> dict[str, Any]:
    requests = [f for f in frames if f.get("method") == "session/request_permission"]
    assert len(requests) == 1, "each capture carries exactly one permission request"
    return requests[0]["params"]


def _shell_tool_call(frames: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``tool_call`` update for the shell call the permission request names."""
    call_id = _permission(frames)["toolCall"]["toolCallId"]
    calls = [u for u in _updates(frames, "tool_call") if u.get("toolCallId") == call_id]
    assert len(calls) == 1
    return calls[0]


def _shell_tool_updates(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    call_id = _permission(frames)["toolCall"]["toolCallId"]
    return [u for u in _updates(frames, "tool_call_update") if u.get("toolCallId") == call_id]


def _key_paths(obj: Any, prefix: str = "") -> set[str]:
    """Every key path in *obj*, values dropped: the SHAPE of a frame."""
    out: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            out.add(path)
            out |= _key_paths(value, path)
    elif isinstance(obj, list):
        for value in obj:
            out |= _key_paths(value, prefix + "[]")
    return out


def _without_model_args(paths: set[str]) -> set[str]:
    return {p for p in paths if p.rsplit(".", 1)[-1] not in MODEL_AUTHORED_ARGS}


# ── provenance ──────────────────────────────────────────────────────────────


def test_the_bundle_capture_is_live_and_names_its_route() -> None:
    meta = _meta(BUNDLE)
    assert meta["backend"] == "kas"
    assert meta["recorded"] == "live", "a synthesized file would prove nothing about the engine"
    assert meta["route"] == "kas-bundle"
    assert "--auth=acp-callback" in meta["note"] and "no kiro-cli" in meta["note"]
    relay = _meta(RELAY)
    assert relay["recorded"] == "live"
    assert "--agent-engine v3" in relay["note"], "the comparison baseline is the relay spawn"


def test_both_captures_exercise_the_same_engine_tool() -> None:
    for path in (RELAY, BUNDLE):
        params = _permission(_frames(path))
        assert params["_meta"]["kiro"]["toolId"] == SHELL_TOOL_ID, path.name
        assert _shell_tool_call(_frames(path))["kind"] == "execute", path.name


# ── the three frame classes the gate reads ──────────────────────────────────


def test_the_permission_request_has_the_same_shape_on_both_routes() -> None:
    relay = _permission(_frames(RELAY))
    bundle = _permission(_frames(BUNDLE))
    assert _key_paths(relay) == _key_paths(bundle)
    # The engine-written identity block the gate trusts: same keys, same tool.
    assert set(relay["_meta"]["kiro"]) == set(bundle["_meta"]["kiro"])
    assert set(relay["_meta"]["kiro"]["consent"]) == set(bundle["_meta"]["kiro"]["consent"])
    assert bundle["_meta"]["kiro"]["consent"]["capability"] == "shell"
    assert bundle["_meta"]["kiro"]["consent"]["askType"] == "implicit"
    # The command the gate judges is stated twice and the two agree, as on the relay.
    for params in (relay, bundle):
        kiro = params["_meta"]["kiro"]
        assert kiro["command"] == params["toolCall"]["title"]
        assert kiro["consent"]["resource"] == kiro["command"]
        assert params["toolCall"]["status"] == "pending"


def test_the_permission_options_are_spelled_the_same_on_both_routes() -> None:
    relay = _permission(_frames(RELAY))["options"]
    bundle = _permission(_frames(BUNDLE))["options"]
    spell = lambda options: [(o["optionId"], o["kind"], o["name"]) for o in options]  # noqa: E731
    assert spell(relay) == spell(bundle)
    assert [o["optionId"] for o in bundle] == ["accept", "always-accept", "reject", "always-reject"]


def test_the_tool_call_frame_has_the_same_shape_on_both_routes() -> None:
    relay = _shell_tool_call(_frames(RELAY))
    bundle = _shell_tool_call(_frames(BUNDLE))
    assert _without_model_args(_key_paths(relay)) == _without_model_args(_key_paths(bundle))
    for update in (relay, bundle):
        assert update["kind"] == "execute"
        assert update["status"] == "pending"
        assert update["_meta"]["kiro"] == {"toolOrigin": "default"}
        assert {"command", "run_in_background"} <= set(update["rawInput"])
        assert isinstance(update["rawInput"]["command"], str) and update["rawInput"]["command"]


def test_the_tool_call_updates_reach_completed_the_same_way_on_both_routes() -> None:
    relay = _shell_tool_updates(_frames(RELAY))
    bundle = _shell_tool_updates(_frames(BUNDLE))
    assert [u["status"] for u in relay] == [u["status"] for u in bundle]
    assert [u["status"] for u in bundle] == ["in_progress", "in_progress", "completed"]
    relay_done, bundle_done = relay[-1], bundle[-1]
    assert _without_model_args(_key_paths(relay_done)) == _without_model_args(
        _key_paths(bundle_done)
    )
    for done in (relay_done, bundle_done):
        assert set(done["rawOutput"]) == {"output", "exitCode", "message"}
        assert done["rawOutput"]["exitCode"] == 0
        assert done["content"][0]["type"] == "content"
        assert done["content"][0]["content"]["type"] == "text"


# ── what the dispatch layer and the gate make of them ──────────────────────

#: The permission-event fields the gate and the SEL records key on. The command text,
#: the ids and the displayed title differ per capture by design and are excluded.
GATE_FACING = (
    "kind",
    "harness_tool_id",
    "tool_name",
    "is_shell",
    "shell_classified",
    "mcp_identity_trusted",
    "raw_params_trusted",
    "request_id",
)


def _replayed(path: Path) -> list[dict[str, Any]]:
    return replay_frames(_frames(path))


def _one(entries: list[dict[str, Any]], **where: Any) -> dict[str, Any]:
    hits = [e for e in entries if all(e.get(k) == v for k, v in where.items())]
    assert len(hits) == 1, (where, [e.get("frame") for e in entries])
    return hits[0]


def test_the_gate_sees_the_same_permission_event_from_both_routes() -> None:
    relay = _one(_replayed(RELAY), action="permission")
    bundle = _one(_replayed(BUNDLE), action="permission")
    relay_event, bundle_event = relay["events"][0], bundle["events"][0]
    assert {k: relay_event.get(k) for k in GATE_FACING} == {
        k: bundle_event.get(k) for k in GATE_FACING
    }
    assert bundle_event["harness_tool_id"] == SHELL_TOOL_ID
    assert bundle_event["is_shell"] is True and bundle_event["shell_classified"] is True
    assert relay_event["options"] == bundle_event["options"]
    assert relay["recorded_options"] == bundle["recorded_options"]
    # The judged command is the engine-stated one, not a model-authored title.
    assert bundle_event["title"] == _permission(_frames(BUNDLE))["_meta"]["kiro"]["command"]


def _gate_kwargs(path: Path) -> tuple[str, dict[str, Any]]:
    """``(title, kwargs)`` as a dispatcher hands them to ``HookManager.on_tool_call``.

    Built the way the live reader loop builds them: the ``tool_call`` primes the
    per-session caches, then the permission request is read against them.
    """
    caches: dict[str, Any] = {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "tool_input_redacted_cache": {},
        "cache_scope": "parity",
    }
    frames = _frames(path)
    parse_session_update(_shell_tool_call(frames), **caches)
    request = next(f for f in frames if f.get("method") == "session/request_permission")
    event, _options = build_permission_event(JsonRpcMessage.from_dict(request), **caches)
    return event.title, hook_gate_kwargs(event)


def test_the_gate_keywords_are_the_same_on_both_routes() -> None:
    """``hook_gate_kwargs`` is what every dispatcher hands ``on_tool_call``."""
    relay, bundle = _gate_kwargs(RELAY)[1], _gate_kwargs(BUNDLE)[1]
    assert set(relay) == set(bundle)
    differing = {k for k in relay if relay[k] != bundle[k]}
    # Only the command and its argument map may differ, and they differ by text alone.
    assert differing <= {"command", "raw_params"}, differing
    assert relay["is_shell"] is True and bundle["is_shell"] is True
    # The engine's tool identity reaches the gate as the trusted ``_meta.kiro`` name.
    assert relay["mcp_tool_name"] == bundle["mcp_tool_name"] == SHELL_TOOL_ID
    assert relay["mcp_identity_trusted"] is True and bundle["mcp_identity_trusted"] is True
    assert relay["mcp_server_name"] == bundle["mcp_server_name"] == ""
    assert bundle["command"] == _permission(_frames(BUNDLE))["_meta"]["kiro"]["command"]


def test_the_gate_judges_both_routes_alike(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same event shape in, same decision out -- and the judged text is the command."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    manager = HookManager(HooksConfig())
    decisions = []
    for path in (RELAY, BUNDLE):
        title, kwargs = _gate_kwargs(path)
        assert kwargs["command"] == _permission(_frames(path))["_meta"]["kiro"]["command"]
        decisions.append(manager.on_tool_call(title, **kwargs).action)
    assert decisions[0] == decisions[1]
    assert decisions[1] != TOOL_DENY, "an echo is not something the floor denies"


def test_the_post_tool_use_result_is_the_same_event_on_both_routes() -> None:
    def _result(path: Path) -> dict[str, Any]:
        updates = [e for e in _replayed(path) if e.get("session_update") == "tool_call_update"]
        results = [ev for e in updates for ev in e["events"] if ev.get("kind") == "tool_result"]
        assert len(results) == 1, path.name
        return results[0]

    relay, bundle = _result(RELAY), _result(BUNDLE)
    assert set(relay) == set(bundle)
    for result in (relay, bundle):
        assert result["tool_final"] is True
        assert result["tool_status"] == "completed"
        assert result["tool_output"]
    assert bundle["tool_call_id"] == _permission(_frames(BUNDLE))["toolCall"]["toolCallId"]


# ── the _kiro/hooks surface: identical by construction ──────────────────────


def _methods(frames: list[dict[str, Any]]) -> Iterator[str]:
    for frame in frames:
        method = frame.get("method")
        if isinstance(method, str):
            yield method


def test_neither_route_is_asked_for_hooks_and_both_announce_the_same_handshake() -> None:
    for path in (RELAY, BUNDLE):
        hooks_methods = [m for m in _methods(_frames(path)) if m.startswith("_kiro/hooks/")]
        assert hooks_methods == [], (path.name, hooks_methods)
    assert kas_wire.hooks_announced() is False
    # One constant, read with no route branch: the bundle flag cannot change it.
    assert KasHarness().client_capabilities is KAS_CLIENT_CAPABILITIES
    assert "hooks" not in json.dumps(KAS_CLIENT_CAPABILITIES)
