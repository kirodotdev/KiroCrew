"""Shared by the KAS engine-child gate tests: a mounted batch, a session handle that
registered it, and the permission requests a real engine sent, replayed through the handle.

The requests are in ``fixtures/kas_native_subagents/child-permission-requests.jsonl``: a
line of provenance, then a request and its ``tool_call`` frame on each line.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp import kas_engine_subagent_target as target
from kiro_crew.acp import kas_engine_subagents as engine_switch
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import ACP_BACKEND_KAS, AcpEvent, JsonRpcMessage
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.hooks import HookManager, HooksConfig, hook_gate_kwargs

_DEFAULTS = Path(kas_harness.__file__).parents[2] / "config" / "defaults.json"
_FIXTURE = (
    Path(__file__).parent / "fixtures" / "kas_native_subagents" / "child-permission-requests.jsonl"
)
_RECORDED = {
    r["label"]: r
    for r in map(json.loads, _FIXTURE.read_text(encoding="utf-8").splitlines())
    if "label" in r
}
_HELPER = target.KAS_HELPER_AGENT
_KEY = "dashboard:t1"

_DIRECT_HELPER = "the helper, started by the session's agent"
_DIRECT_OWN = "a person's agent, started by the session's agent"
_NESTED_HELPER = "the helper, started by a person's agent"
_NESTED_OWN = "a person's agent, started by a person's agent"
_OWN_STARTING_THE_HELPER = "a person's agent starting the helper"

#: Every field the gate reads a spec's grant from.
_GRANTS = (
    "spec_granted_builtins",
    "spec_hook_held",
    "spec_granted_commands",
    "spec_granted_paths",
    "spec_granted_read_paths",
)
_STAMPED = [f.name for f in dataclasses.fields(AcpEvent) if f.name.startswith("spec_")]


def _agents_dir(monkeypatch, tmp_path) -> Path:
    """Fixture body: an empty user agents folder. A test file binds it by name,
    ``agents_dir = pytest.fixture(_agents_dir)``."""
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return user


#: The ``kas_subagents`` preview per value: ``"on"``, ``"off"``, and
#: ``"unset"`` with no ``feature_previews`` key at all, which must read as off.
_PREVIEWS: dict[str, dict[str, bool] | None] = {
    "on": {engine_switch.KAS_SUBAGENTS_PREVIEW: True},
    "off": {engine_switch.KAS_SUBAGENTS_PREVIEW: False},
    "unset": None,
}


def _setting(monkeypatch):
    """Fixture body: sets the ``kas_subagents`` preview. Bound as
    ``setting = pytest.fixture(_setting)``."""

    def _set(value: str) -> None:
        cfg = KiroCrewConfig()
        previews = _PREVIEWS[value]
        if previews is not None:
            cfg.dashboard.feature_previews = previews
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls, *a, **k: cfg))

    return _set


def _default_spec(**extra: Any) -> dict[str, Any]:
    """The shipped default agent, also granting the shell so a grant has a call to answer."""
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    return {
        "name": "kirocrew",
        "description": "the default agent",
        "prompt": "You are the default agent.",
        "tools": shipped["tools"],
        "allowedTools": [*shipped["allowedTools"], "execute_bash"],
        **extra,
    }


def _own(name: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"the person's {name}",
        "prompt": f"You are {name}.",
        "tools": ["fs_read", "execute_bash"],
        **extra,
    }


def _write(directory: Path, spec: dict[str, Any]) -> None:
    (directory / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")


class _Batch(list):  # type: ignore[type-arg]
    """A registered batch, carrying the agent its registration mounted the helper for."""

    helper_parent = ""


async def _batch(agent: str = "kirocrew", key: str = _KEY, **kw: Any) -> _Batch:
    extras = await KasHarness().session_extras(agent, work_dir=None, session_key=key, **kw)
    batch = _Batch(extras.custom_agents)
    batch.helper_parent = extras.shipped_helper_parent
    return batch


def _handle(
    batch: list[dict[str, Any]],
    cwd: Path,
    *,
    agent: str = "kirocrew",
    sid: str,
    backend: str = ACP_BACKEND_KAS,
) -> AcpSessionHandle:
    rt = MagicMock()
    rt.acp_backend = backend
    rt.pid = None
    rt._remote_workspace = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    handle = AcpSessionHandle(sid, asyncio.Queue(), rt)
    handle._bound_cwd = str(cwd)
    KasHarness().record_session_projection(
        handle, list(batch), agent, getattr(batch, "helper_parent", "")
    )
    return handle


def _sid(label: str) -> str:
    return _RECORDED[label]["request"]["sessionId"]


def _replay(
    handle: AcpSessionHandle, label: str, request: dict[str, Any] | None = None
) -> AcpEvent:
    """The recorded ``tool_call`` frame, then the request (the recorded one unless given)."""
    record = _RECORDED[label]
    sid = record["request"]["sessionId"]
    handle._handle_update(
        JsonRpcMessage(
            method="session/update", params={"sessionId": sid, "update": record["tool_call"]}
        )
    )
    event = handle._build_permission_event(
        JsonRpcMessage(
            id=7,
            method="session/request_permission",
            params=copy.deepcopy(record["request"]) if request is None else request,
        )
    )
    assert event is not None
    return event


def _own_request(handle: AcpSessionHandle, label: str) -> AcpEvent:
    """The same command as the session agent's own call: no sub-agent on either frame."""
    record = copy.deepcopy(_RECORDED[label])
    call, request = record["tool_call"], record["request"]
    call["toolCallId"] = request["toolCall"]["toolCallId"] = "the-parents-own-call"
    del call["_meta"]["kiro"]["agentSubtaskId"]
    del request["_meta"]["kiro"]["requester"]["subagent"]
    handle._handle_update(
        JsonRpcMessage(
            method="session/update", params={"sessionId": request["sessionId"], "update": call}
        )
    )
    event = handle._build_permission_event(
        JsonRpcMessage(id=8, method="session/request_permission", params=request)
    )
    assert event is not None and not event.native_child_call
    return event


def _verdict(event: AcpEvent) -> tuple[str, str]:
    result = HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))
    return result.action, result.tier


def _granted(event: AcpEvent) -> bool:
    return any(getattr(event, name) for name in _GRANTS)


async def _mounted_with_own_agents(agents_dir: Path, setting) -> _Batch:
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("own-model"))
    _write(agents_dir, _own("own-star", tools=["*"]))
    setting("on")
    batch = await _batch()
    assert {"own-model", "own-star", _HELPER} <= {a["id"] for a in batch}
    return batch
