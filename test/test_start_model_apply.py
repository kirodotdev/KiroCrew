"""One function puts a starting session on its configured model, on both starts.

``apply_configured_model`` (``acp/session_handle_model_selection.py``) is reached
from a process's own first session (``AcpProvider``'s runtime start) and from a
sub-agent's session on its parent's process (``_bind_shared_handle``). The table
below runs every case through BOTH callers, so a pin is judged, spelled, sent,
withheld and recorded the same way whichever process serves the session.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _make_runtime, _register

from kiro_crew import model_registry
from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_STRANDS,
    ACP_BACKENDS_MODEL_AT_LAUNCH,
    ACP_BACKENDS_SESSION_SHARING,
)
from kiro_crew.agent_sdk.capabilities import UNKNOWN_BACKEND_CAPABILITIES, capabilities_for
from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.ids import UNALLOCATABLE_PID

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _apply():
    """The function under test, imported where it is used so that this file still
    collects on a tree without it and each test fails on its own assertion."""
    from kiro_crew.acp.session_handle_model_selection import apply_configured_model

    return apply_configured_model


def _per_session_set() -> frozenset:
    from kiro_crew.agent_sdk import backends

    return backends.ACP_BACKENDS_MODEL_PER_SESSION


SERVED = ["served-a", "served-b"]
#: A model the codex catalog lists and the kiro one does not: a foreign pin on kiro.
FOREIGN = "foreign-x"

#: name -> (pin, what a forced revalidation answers, the model sent, the pin recorded
#: as refused). ``None`` for the sent model means no model request at all.
CASES: dict[str, tuple[str, list[str], str | None, str]] = {
    "a served pin is sent": ("served-b", SERVED, "served-b", ""),
    "auto sends nothing": ("auto", SERVED, None, ""),
    "a stale qualifier is folded onto the advertised spelling": (
        "other::served-a",
        SERVED,
        "served-a",
        "",
    ),
    "a pin the account is not served is withheld and recorded": (
        "missing-m",
        SERVED,
        None,
        "missing-m",
    ),
    "a pin the revalidation finds is sent after all": (
        "late-m",
        [*SERVED, "late-m"],
        "late-m",
        "",
    ),
    "a pin another harness owns is not sent and is no refusal": (FOREIGN, SERVED, None, ""),
}


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers) -> None:
    """Every manager built here opens tasks.db; close it at teardown, not at GC."""


@pytest.fixture(autouse=True)
def _catalogs(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Two harness catalogs, so a foreign pin is foreign whatever the host has cached."""
    _floor_monkeypatch.setattr(
        model_registry, "_ADVERTISED_MODELS", {"acp": list(SERVED), "codex": [FOREIGN]}
    )
    # ``agent.model`` is ``auto`` here; its collapse reads the installed agents dir.
    _floor_monkeypatch.setattr(KiroCrewConfig, "_resolve_agent_model", staticmethod(lambda: ""))


def _handle(refreshed: list[str]) -> MagicMock:
    handle = MagicMock()
    handle.session_id = "sess-1"
    handle.memory_mode = "persistent"
    handle.available_models = [{"modelId": model} for model in SERVED]
    handle.refresh_available_models = AsyncMock(
        return_value=[{"modelId": model} for model in refreshed]
    )
    handle.set_model = AsyncMock()
    handle.model_pin_refused = ""
    return handle


async def _dedicated_start(pin: str, handle: MagicMock, tmp_path) -> None:
    """The process's own first session: ``AcpProvider``'s runtime start."""
    provider = AcpProvider(model=pin, acp_backend=ACP_BACKEND_KIRO)
    provider._client._work_dir = str(tmp_path)
    provider._client._agent = "kirocrew"
    provider._client._resume_session_id = ""
    runtime = MagicMock(pid=UNALLOCATABLE_PID)
    runtime.spawn = AsyncMock()
    runtime.create_session = AsyncMock(return_value=handle)
    with (
        patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime),
        patch(
            "kiro_crew.providers.acp.AcpSessionProvider",
            side_effect=lambda h, r, **kw: MagicMock(_handle=h, _runtime=r, resumed=False),
        ),
        patch("pathlib.Path.exists", return_value=False),
    ):
        await provider._start_kiro_runtime_impl({}, {})


async def _shared_start(pin: str, handle: MagicMock, tmp_path) -> None:
    """A sub-agent's session on its parent's process: ``_bind_shared_handle``."""
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=None)
    info = SubagentInfo(id="child", task="t", agent="kirocrew", parent_session_key="parent")
    # What ``_run_inner`` records before it acquires the session.
    info.requested_model = pin
    runtime = MagicMock(pid=UNALLOCATABLE_PID)
    runtime.acp_backend = ACP_BACKEND_KIRO
    cfg = KiroCrewConfig(agent=AgentConfig())
    with patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)):
        await manager._bind_shared_handle(info, "subagent:child", runtime, handle)
    assert info._session_sharing is True


@pytest.mark.asyncio
@pytest.mark.parametrize("start", [_dedicated_start, _shared_start], ids=["dedicated", "shared"])
@pytest.mark.parametrize("case", sorted(CASES))
async def test_a_start_applies_its_model_the_same_way_on_both_paths(
    case: str, start, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    pin, refreshed, sent, refused = CASES[case]
    handle = _handle(refreshed)

    with caplog.at_level(logging.WARNING):
        await start(pin, handle, tmp_path)

    if sent is None:
        handle.set_model.assert_not_awaited()
    else:
        handle.set_model.assert_awaited_once_with(sent)
    assert handle.model_pin_refused == refused
    withheld = [r for r in caplog.records if "is not available to this account" in r.getMessage()]
    assert bool(withheld) is bool(refused)


@pytest.mark.asyncio
async def test_a_foreign_pin_is_withheld_by_the_function_itself() -> None:
    """The shared start's selection scopes a foreign pin away before the function
    sees it; the dedicated start can hand one straight in, and it is still not sent."""
    handle = _handle(SERVED)

    assert await _apply()(handle, FOREIGN, backend=ACP_BACKEND_KIRO) is None

    handle.set_model.assert_not_awaited()
    handle.refresh_available_models.assert_not_awaited()
    assert handle.model_pin_refused == ""


@pytest.mark.asyncio
async def test_a_model_write_reports_how_long_it_took() -> None:
    """The dedicated start records the write as its ``set_model`` phase."""
    handle = _handle(SERVED)

    took = await _apply()(handle, "served-a", backend=ACP_BACKEND_KIRO)

    assert isinstance(took, float) and took >= 0.0
    assert await _apply()(handle, "", backend=ACP_BACKEND_KIRO) is None


@pytest.mark.asyncio
async def test_each_session_of_one_runtime_is_asked_for_its_own_model() -> None:
    """The request names its session, which is what lets two sessions of one
    process run two models: the second session's pin does not move the first."""
    rt, _, proc = _make_runtime()
    queues = _register(rt, "parent-sess", "child-sess")
    wire_send = rt.send_request

    async def _send_and_answer(method, params, **kwargs):
        req_id = await wire_send(method, params, **kwargs)
        queues[params["sessionId"]].put_nowait(JsonRpcMessage(id=req_id, result={}))
        return req_id

    rt.send_request = _send_and_answer  # type: ignore[method-assign]
    parent = AcpSessionHandle("parent-sess", queues["parent-sess"], rt)
    child = AcpSessionHandle("child-sess", queues["child-sess"], rt)
    for handle in (parent, child):
        handle._available_models = [{"modelId": model} for model in SERVED]

    await _apply()(parent, "served-a", backend=rt.acp_backend)
    await _apply()(child, "served-b", backend=rt.acp_backend)

    frames = [json.loads(call.args[0].decode()) for call in proc.stdin.write.call_args_list]
    asked = [(f["params"]["sessionId"], f["params"]["modelId"]) for f in frames]
    assert [f["method"] for f in frames] == ["session/set_model", "session/set_model"]
    assert asked == [("parent-sess", "served-a"), ("child-sess", "served-b")]
    assert (parent.model, child.model) == ("served-a", "served-b")


def test_the_per_session_model_capability_is_opt_in() -> None:
    """Granted by membership, read as a capability, withheld from a stranger."""
    assert _per_session_set() == frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_CODEX})
    for backend in _per_session_set():
        assert capabilities_for(backend).takes_model_per_session is True
    for backend in (ACP_BACKEND_KAS, ACP_BACKEND_STRANDS, "a-harness-nobody-registered"):
        assert capabilities_for(backend).takes_model_per_session is False
    assert UNKNOWN_BACKEND_CAPABILITIES.takes_model_per_session is False


def test_a_model_fixed_at_launch_is_never_a_per_session_model() -> None:
    assert not _per_session_set() & ACP_BACKENDS_MODEL_AT_LAUNCH


def test_every_sharing_backend_today_takes_a_model_per_session() -> None:
    """Not a rule of the two sets -- a harness may share and still need a process
    for a pin -- but a sharing backend missing here loses the shared pinned start,
    so the day it happens is a decision, not an accident."""
    assert ACP_BACKENDS_SESSION_SHARING <= _per_session_set()
