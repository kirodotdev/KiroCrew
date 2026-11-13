"""Integration test for api_agent_config PUT.

Regression test for bug where local variable 'config_path' shadowed the
imported config_path() function, causing "'PosixPath' object is not callable".
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from config_pending_helpers import HAND_EDIT, make_config_json_pending

import kiro_crew.config.loader as loader_module
from kiro_crew.dashboard.handlers import api_agent_config
from kiro_crew.testing.wait import await_until


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    """Run as the dashboard owner: these tests exercise handler behavior PAST
    the owner boundary on the agents module's mutating endpoints, which has
    its own enumerate-the-invariant coverage in
    test_agents_endpoints_owner_auth.py."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


@pytest.mark.asyncio
async def test_api_agent_config_put_succeeds(tmp_path):
    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {"config": {"name": "test", "tools": ["a"], "allowedTools": ["b"]}}

    request.json = mock_json

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch(
            "kiro_crew.agent.build_agent_config",
            return_value={"toolsSettings": {"execute_bash": {"deniedCommands": ["rm -rf"]}}},
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": ["a", "c"], "allowedTools": ["b"]},
        ),
    ):

        response = await api_agent_config(request)

    assert response.status == 200
    # Verify the handler actually wrote the config files
    assert installed.exists()
    assert json.loads(installed.read_text(encoding="utf-8"))["name"] == "test"
    assert mc_cfg.exists()
    assert json.loads(mc_cfg.read_text(encoding="utf-8"))["removedTools"]["tools"] == ["c"]


@pytest.mark.asyncio
async def test_api_agent_config_put_of_a_pending_config_json_hands_the_refusal_on(
    tmp_path, monkeypatch
):
    """config.json has a change pending owner review: the removedTools record is
    refused before the spec is written, and the refusal reaches the dashboard chain,
    which answers 409, not this handler's own 500."""
    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    loader_module.KiroCrewConfig().save()
    path = make_config_json_pending(monkeypatch)

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {"config": {"name": "test", "tools": ["a"], "allowedTools": ["b"]}}

    request.json = mock_json

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": ["a", "c"], "allowedTools": ["b"]},
        ),
        pytest.raises(loader_module.PendingEditBlocksSave),
    ):
        await api_agent_config(request)

    assert path.read_bytes() == HAND_EDIT
    assert json.loads(installed.read_text(encoding="utf-8")) == {"name": "kirocrew"}


@pytest.mark.asyncio
async def test_api_agent_config_put_strips_governed_grants(tmp_path, monkeypatch):
    """A dashboard PUT persists the config verbatim, so it MUST run the whole map
    through the governance filter — else a governed @denied allowedTools entry or
    a governed server's autoApprove written here restores the bypass the per-ref
    writers close. Executable (not source-inspection) coverage of that writer."""
    import kiro_crew.platform.governance as gov
    from kiro_crew.config import live as _live
    from kiro_crew.config.loader import KiroCrewConfig as _Cfg

    # Govern @denied only; everything else may auto-approve.
    monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: ref != "@denied")
    # The subject is the ceiling, not the local floor: both fixture grants are
    # hand-written, so the opt-in is pinned on to keep @ok's the control.
    _cfg = _Cfg()
    _cfg.mcp.honour_auto_approve = True
    monkeypatch.setattr(_live, "snapshot", lambda: _cfg)

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {
            "config": {
                "name": "test",
                # mount list must NOT be filtered (mounting != auto-approving)
                "tools": ["@denied", "@ok"],
                "allowedTools": ["@ok", "@denied"],
                "mcpServers": {
                    "denied": {"url": "u", "autoApprove": ["dangerous"]},
                    "ok": {"url": "u", "autoApprove": ["fine"]},
                },
            }
        }

    request.json = mock_json

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": [], "allowedTools": []},
        ),
    ):
        response = await api_agent_config(request)

    assert response.status == 200
    written = json.loads(installed.read_text(encoding="utf-8"))
    # Governed @denied dropped from auto-approve; ungoverned @ok kept.
    assert written["allowedTools"] == ["@ok"]
    # Mount list is untouched — @denied stays mounted, just not auto-approved.
    assert written["tools"] == ["@denied", "@ok"]
    # Governed server loses autoApprove; ungoverned server keeps it.
    assert "autoApprove" not in written["mcpServers"]["denied"]
    assert written["mcpServers"]["ok"]["autoApprove"] == ["fine"]


@pytest.mark.asyncio
async def test_api_agent_config_put_strips_bookkeeping_keys(tmp_path):
    """A dashboard PUT must not re-pollute the kiro spec with Kiro Crew keys.

    The whole-config PUT strips ``model_managed`` / ``cc_model``, matching the
    agent-detail PATCH. Persisting them verbatim is fatal:
    kiro-cli ``deny_unknown_fields`` then rejects the entire agent until the
    next ``migrate_agent_specs`` heal on gateway rebuild.
    """
    from kiro_crew import agent_state

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {
            "config": {
                "name": "kirocrew",
                "tools": ["a"],
                "allowedTools": ["b"],
                "model_managed": True,
                "cc_model": "claude-sonnet-4.6",
            }
        }

    request.json = mock_json

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.agent.get_shipped_tools", return_value={"tools": [], "allowedTools": []}),
    ):
        response = await api_agent_config(request)

    assert response.status == 200
    written = json.loads(installed.read_text(encoding="utf-8"))
    assert "model_managed" not in written
    assert "cc_model" not in written
    assert written["name"] == "kirocrew"
    # Lifted into the sidecar when unset (same rule as migrate).
    assert agent_state.get_model_managed("kirocrew") is True
    assert agent_state.get_cc_model("kirocrew") == "claude-sonnet-4.6"


@pytest.mark.asyncio
async def test_api_agent_config_put_does_not_clobber_sidecar(tmp_path):
    """A stale bookkeeping key in the PUT body must not overwrite the sidecar."""
    from kiro_crew import agent_state

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    agent_state.set_model_managed("kirocrew", False)
    agent_state.set_cc_model("kirocrew", "test-model-stub")

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {
            "config": {
                "name": "kirocrew",
                "tools": ["a"],
                "allowedTools": ["b"],
                "model_managed": True,
                "cc_model": "claude-sonnet-4.6",
            }
        }

    request.json = mock_json

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.agent.get_shipped_tools", return_value={"tools": [], "allowedTools": []}),
    ):
        response = await api_agent_config(request)

    assert response.status == 200
    written = json.loads(installed.read_text(encoding="utf-8"))
    assert "model_managed" not in written
    assert "cc_model" not in written
    assert agent_state.get_model_managed("kirocrew") is False
    assert agent_state.get_cc_model("kirocrew") == "test-model-stub"


@pytest.mark.asyncio
async def test_api_agent_config_put_uses_atomic_write(tmp_path):
    """PUT persists the installed spec via write_config_atomically, never a bare write_text.

    A truncating write leaves the spec corrupt on a crash or full disk, and kiro-cli
    reads the spec at every session spawn.
    """

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {"config": {"name": "test", "tools": [], "allowedTools": []}}

    request.json = mock_json

    atomic_calls: list = []

    def _fake_atomic(path, data, **kwargs):
        atomic_calls.append((path, data))
        # Actually write so downstream read-backs don't break.
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": [], "allowedTools": []},
        ),
        # Intercept write_config_atomically as imported into agents.py.
        patch(
            "kiro_crew.dashboard.handlers.agents.write_config_atomically",
            side_effect=_fake_atomic,
        ),
    ):
        response = await api_agent_config(request)

    assert response.status == 200
    # write_config_atomically must be called for the installed spec path.
    # (It is also called for the mc_cfg sidecar on the same code path, so
    # total call count may be > 1 — we care only that the installed spec write
    # went through the atomic helper, not the total invocation count.)
    installed_spec_calls = [(p, d) for p, d in atomic_calls if p == installed]
    assert len(installed_spec_calls) == 1, (
        f"Expected write_config_atomically to be called once with installed_path={installed!r}; "
        f"got calls to: {[str(p) for p, _ in atomic_calls]}.  "
        f"A bare write_text was likely used for the installed spec instead."
    )
    _, written_data = installed_spec_calls[0]
    assert written_data.get("name") == "test"


@pytest.mark.asyncio
async def test_agent_config_write_holds_the_mcp_transaction_lock(tmp_path):
    """The agent-spec write participates in the MCP transaction lock.

    MCP config transactions in ``handlers/mcp.py`` read agent specs and act on them
    under ``_get_mcp_lock``; a write between the read and the act would go unseen.
    """
    import contextlib

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {"config": {"name": "test", "tools": ["a"], "allowedTools": ["b"]}}

    request.json = mock_json

    # Two INDEPENDENT observations per write, not one boolean: a recorder that
    # only tracks "some lock was held" passes just as happily when the wrong
    # lock is held. The write must be covered by the settings/mcp.lock
    # transaction lock AND by bridges' kirocrew.lock file lock, and the file
    # lock must be keyed on the file actually being written.
    lock_held: list[bool] = []
    writes: list[tuple[str, bool, tuple[str, ...]]] = []
    holding = False
    file_locked: list[Path] = []

    @contextlib.asynccontextmanager
    async def _recording_lock():
        nonlocal holding
        holding = True
        lock_held.append(True)
        try:
            yield
        finally:
            holding = False

    @contextlib.contextmanager
    def _recording_file_lock(*, exclusive: bool = True, target=None):  # noqa: ANN001
        file_locked.append(target)
        try:
            yield
        finally:
            file_locked.remove(target)

    def _recording_write(path, config):  # noqa: ANN001 - mirrors the real signature
        writes.append((str(path), holding, tuple(str(t) for t in file_locked)))

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch(
            "kiro_crew.agent.build_agent_config",
            return_value={"toolsSettings": {}},
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": ["a", "c"], "allowedTools": ["b"]},
        ),
        patch(
            "kiro_crew.dashboard.handlers.mcp._get_mcp_lock",
            _recording_lock,
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents._agent_file_lock",
            _recording_file_lock,
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.write_config_atomically",
            _recording_write,
        ),
    ):
        resp = await api_agent_config(request)

    assert resp.status == 200
    # Only the agent-SPEC write must hold the lock: the spec file is a census
    # source for the MCP config transactions. The config.json sidecar write is
    # not.
    spec_writes = [entry for entry in writes if entry[0] == str(installed)]
    assert spec_writes, "the agent-spec write never happened"
    assert all(
        held for _p, held, _f in spec_writes
    ), "the agent-spec write ran OUTSIDE the MCP transaction lock"
    # bridges_mcp.py does whole-file read-modify-writes of THIS SAME file under its
    # own flock (kirocrew.lock), which the transaction lock does not cover: a
    # concurrent app enable and this PUT would each write the whole file and the
    # last atomic rename would silently discard the other side. Assert the file
    # lock was held too, and that it was keyed on the installed path -- a lock on
    # any other sidecar serializes against nothing.
    for _path, _held, locked in spec_writes:
        assert locked, "the agent-spec write ran WITHOUT bridges' kirocrew.json file lock"
        assert str(installed) in locked, (
            "bridges' file lock was taken on the wrong target: "
            f"{locked} does not include {installed}"
        )


@pytest.mark.asyncio
async def test_agent_config_write_goes_through_one_shielded_offload(tmp_path):
    """Every durable write happens inside ONE shielded offload, none outside it.

    A cancelled PUT releases ``_get_mcp_lock`` while a bare ``asyncio.to_thread``
    worker keeps writing; ``_offload_config_write`` drains it first. All three writes
    (bookkeeping lift, removedTools sidecar, installed spec) must sit in one dispatch,
    or a cancellation point opens between two of them.
    """
    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {"config": {"name": "test", "tools": ["a"], "allowedTools": ["b"]}}

    request.json = mock_json

    offloaded: list[str] = []
    inside_offload = False
    writes: list[tuple[str, bool]] = []

    async def _recording_offload(fn, *args, **kwargs):
        nonlocal inside_offload
        offloaded.append(getattr(fn, "__name__", repr(fn)))
        inside_offload = True
        try:
            return fn(*args, **kwargs)
        finally:
            inside_offload = False

    def _recording_lift(config, name):  # noqa: ANN001 - mirrors the real signature
        writes.append(("bookkeeping", inside_offload))
        return False

    def _recording_write(path, data, **kwargs):  # noqa: ANN001 - mirrors the real signature
        writes.append(("spec", inside_offload))
        Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    real_locked = loader_module.update_config_locked

    def _recording_locked(path, **kwargs):  # noqa: ANN001 - mirrors the real signature
        # The ``config.json`` half is now one locked read-modify-write, so this
        # is where that durable write is observed.
        writes.append(("mc_cfg", inside_offload))
        return real_locked(path, **kwargs)

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.agent.build_agent_config", return_value={"toolsSettings": {}}),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": ["a", "c"], "allowedTools": ["b"]},
        ),
        patch("kiro_crew.agent_state.lift_and_strip_bookkeeping", _recording_lift),
        patch(
            "kiro_crew.dashboard.handlers.agents.write_config_atomically",
            _recording_write,
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.update_config_locked",
            _recording_locked,
        ),
        patch("kiro_crew.dashboard.handlers.mcp._offload_config_write", _recording_offload),
    ):
        resp = await api_agent_config(request)

    assert resp.status == 200
    assert len(offloaded) == 1, (
        "the PUT's durable writes were dispatched as several offloads, so a "
        f"cancellation can land between them: {offloaded}"
    )
    assert {label for label, _ in writes} == {
        "bookkeeping",
        "mc_cfg",
        "spec",
    }, f"not every durable write of the PUT was observed: {writes}"
    outside = [label for label, inside in writes if not inside]
    assert not outside, (
        f"{outside} ran OUTSIDE the shielded offload: a cancellation at the "
        "await that dispatched it releases the transaction lock with the write "
        "still in flight"
    )


@pytest.mark.asyncio
async def test_agent_config_put_persists_nothing_before_the_transaction_lock(tmp_path):
    """A PUT cancelled while WAITING for the transaction lock must leave no trace.

    The lock wait in ``_McpFileLock.__aenter__`` is a cancellation point. A write made
    before it would leave removedTools bookkeeping in ``config.json`` out of step with
    the spec, and the next upgrade would silently remove a tool. No write may precede
    the lock.
    """
    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew", "tools": ["a", "c"]}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"
    # A baseline that must survive an aborted PUT byte-for-byte.
    mc_cfg.write_text(json.dumps({"default_agent": "kirocrew"}))
    before_cfg = mc_cfg.read_text()
    before_spec = installed.read_text()

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        # Drops shipped tool "c", so the handler WOULD record removedTools.
        return {"config": {"name": "test", "tools": ["a"], "allowedTools": ["b"]}}

    request.json = mock_json

    reached_lock = asyncio.Event()
    never_granted = asyncio.Event()

    class _ContendedLock:
        """Models a transaction lock already held by another writer.

        Suspends in ``__aenter__`` exactly as the real ``_McpFileLock`` does
        while its executor thread blocks on the flock -- deterministically, and
        without touching the host's ``~/.kiro/settings/mcp.lock``.
        """

        async def __aenter__(self) -> None:
            reached_lock.set()
            await never_granted.wait()

        async def __aexit__(self, *args: object) -> None:
            return None

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.agent.build_agent_config", return_value={"toolsSettings": {}}),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": ["a", "c"], "allowedTools": ["b"]},
        ),
        patch(
            "kiro_crew.dashboard.handlers.mcp._get_mcp_lock",
            lambda: _ContendedLock(),
        ),
    ):
        task = asyncio.ensure_future(api_agent_config(request))
        try:
            await await_until(reached_lock, what="handler reached the MCP lock")
            # Cancel while the handler is suspended on the lock wait.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            never_granted.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    assert mc_cfg.read_text() == before_cfg, (
        "the removedTools bookkeeping was persisted BEFORE the transaction lock "
        "was held, so a cancel at the lock wait leaves it recorded with no "
        "matching agent-spec write"
    )
    assert installed.read_text() == before_spec, "the aborted PUT still rewrote the agent spec"


@pytest.mark.asyncio
async def test_agent_config_put_lock_releases_only_after_every_write_landed(tmp_path):
    """The transaction lock must not release while ANY write of the PUT is in flight.

    A cancel at a plain await releases the lock while a worker thread keeps writing,
    so the next transaction reads a state no writer committed. At release time every
    durable write of the PUT must already have completed.
    """
    import contextlib
    import threading

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {"config": {"name": "test", "tools": ["a"], "allowedTools": ["b"]}}

    request.json = mock_json

    release = threading.Event()
    worker_entered = threading.Event()
    # Names of the durable writes that have COMPLETED, in order.
    completed: list[str] = []
    # A snapshot of ``completed`` taken at each lock release. Snapshotting (not
    # aliasing) matters: on the broken shape the abandoned worker thread appends
    # to ``completed`` after the lock is already gone.
    at_release: list[list[str]] = []

    def _blocking_bookkeeping(config, name):  # noqa: ANN001 - mirrors the real signature
        """Stand in for the first durable write, parked mid-write on demand."""
        worker_entered.set()
        release.wait(timeout=10)
        completed.append("bookkeeping")
        return False

    def _recording_write(path, data, **kwargs):  # noqa: ANN001 - mirrors the real signature
        completed.append("spec")
        Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    real_locked = loader_module.update_config_locked

    def _recording_locked(path, **kwargs):  # noqa: ANN001 - mirrors the real signature
        result = real_locked(path, **kwargs)
        completed.append("mc_cfg")
        return result

    @contextlib.asynccontextmanager
    async def _recording_lock():
        try:
            yield
        finally:
            at_release.append(list(completed))

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.agent.build_agent_config", return_value={"toolsSettings": {}}),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": ["a", "c"], "allowedTools": ["b"]},
        ),
        # Neutral: the filter persists nothing, and pinning it here would only
        # couple this test to the host's governance profiles.
        patch(
            "kiro_crew.platform.governance.sanitize_agent_config_governance",
            lambda config: None,
        ),
        patch("kiro_crew.agent_state.lift_and_strip_bookkeeping", _blocking_bookkeeping),
        patch("kiro_crew.dashboard.handlers.mcp._get_mcp_lock", _recording_lock),
        patch(
            "kiro_crew.dashboard.handlers.agents.write_config_atomically",
            _recording_write,
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.update_config_locked",
            _recording_locked,
        ),
    ):
        task = asyncio.ensure_future(api_agent_config(request))
        # try/finally: the assertions below run while a worker thread is parked
        # in ``release.wait(timeout=10)``. Abandoning it would keep a default
        # executor thread blocked for the full 10s and degrade every test that
        # shares the executor, so release and reap unconditionally.
        try:
            await asyncio.to_thread(worker_entered.wait, 5)
            assert worker_entered.is_set(), "test bug: the first durable write never started"

            # Cancel with the write parked. cancel() wakes the task through the
            # loop alone (no thread is involved until ``release`` opens), so a
            # few loop turns deliver it and run whatever unwind the handler's
            # shape allows while the worker is still parked.
            task.cancel()
            for _ in range(10):
                if task.done():
                    break
                await asyncio.sleep(0)

            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert len(at_release) == 1, f"the transaction lock was entered {len(at_release)} times"
    landed = at_release[0]
    assert set(landed) == {"bookkeeping", "mc_cfg", "spec"}, (
        "the transaction lock was released with writes still in flight: only "
        f"{landed} had completed. A cancellation between the lock and a durable "
        "write lets the next holder observe a half-applied PUT."
    )
    # Order that is load-bearing (as opposed to incidental): the bookkeeping lift
    # STRIPS Kiro Crew keys out of the very dict the spec write persists, so it
    # must complete first or the spec lands with keys kiro-cli rejects. The
    # sidecar write's position relative to the other two is not pinned.
    assert landed.index("bookkeeping") < landed.index("spec")


@pytest.mark.asyncio
async def test_agent_config_put_unreadable_config_persists_nothing(tmp_path, monkeypatch):
    """A fallible READ must not sit after a durable write.

    ``read_config_for_update`` fails closed on a corrupt ``config.json``. After the
    bookkeeping lift, that 500 would leave ``agent_model_state.json`` naming a model
    for a spec never written. All three durable targets must stay byte-identical.
    """
    from kiro_crew.config.loader import ConfigReadError

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew", "tools": ["a", "c"]}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"
    mc_cfg.write_text(json.dumps({"default_agent": "kirocrew"}))
    state = tmp_path / "agent_model_state.json"
    # ``model_managed`` already set, so only the ``cc_model`` below can lift --
    # enough for the bookkeeping write to be observable, and it keeps the
    # sidecar off the developer's real ~/.kiro/crew.
    state.write_text(
        json.dumps({"test": {"model_managed": False}}, indent=2, sort_keys=True) + "\n"
    )
    monkeypatch.setattr("kiro_crew.agent_state._state_path", lambda: state)

    before_cfg = mc_cfg.read_text()
    before_spec = installed.read_text()
    before_state = state.read_text()

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {
            "config": {
                "name": "test",
                "tools": ["a"],
                "allowedTools": ["b"],
                # Unset in the sidecar above, so the bookkeeping lift WOULD write.
                "cc_model": "claude-sonnet-4.6",
            }
        }

    request.json = mock_json

    def _unreadable(path, **kwargs):  # noqa: ANN001 - mirrors the real signature
        # The fallible read now lives INSIDE the locked primitive, which raises
        # it before invoking the mutate callback, so the seam moves here. The
        # property is unchanged: the unit's first durable step refuses.
        raise ConfigReadError("config.json is corrupt")

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.agent.build_agent_config", return_value={"toolsSettings": {}}),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": ["a", "c"], "allowedTools": ["b"]},
        ),
        patch(
            "kiro_crew.platform.governance.sanitize_agent_config_governance",
            lambda config: None,
        ),
        patch("kiro_crew.dashboard.handlers.agents.update_config_locked", _unreadable),
    ):
        response = await api_agent_config(request)

    assert response.status == 500
    assert json.loads(response.text)["code"] == "config_unreadable"
    assert state.read_text() == before_state, (
        "the bookkeeping sidecar was written before the config read failed, so "
        "the 500 leaves bookkeeping durable for a spec that was never written"
    )
    assert mc_cfg.read_text() == before_cfg, "the refused PUT still rewrote the sidecar"
    assert installed.read_text() == before_spec, "the refused PUT still rewrote the agent spec"


@pytest.mark.asyncio
async def test_agent_config_put_keeps_a_concurrent_config_write_that_lands_before_the_worker(
    tmp_path,
):
    """A ``config.json`` write landing before the worker must not be reverted.

    The read feeding the ``removedTools`` write must sit inside the offloaded unit. A
    read in the handler leaves a gap where a whole-file writer such as
    ``api_default_agent`` lands, and the stale snapshot then silently reverts it.
    The writer is injected at the offload dispatch, so only an in-unit read keeps it.
    """
    import contextlib

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"
    mc_cfg.write_text(json.dumps({"default_agent": "kirocrew"}))

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        # Drops shipped tool "c", so the PUT WILL record removedTools.
        return {"config": {"name": "test", "tools": ["a"], "allowedTools": ["b"]}}

    request.json = mock_json

    @contextlib.asynccontextmanager
    async def _neutral_lock():
        # Neutral: this test is about the read's POSITION, and the real lock
        # would touch the host's ~/.kiro/settings/mcp.lock.
        yield

    async def _offload_after_a_concurrent_write(fn, *args, **kwargs):
        # A sibling whole-file RMW commits here -- between the old read point and
        # the worker's write. Modelled on api_default_agent's unlocked RMW.
        other = json.loads(mc_cfg.read_text(encoding="utf-8"))
        other["default_agent"] = "other"
        mc_cfg.write_text(json.dumps(other), encoding="utf-8")
        return fn(*args, **kwargs)

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.agent.build_agent_config", return_value={"toolsSettings": {}}),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": ["a", "c"], "allowedTools": ["b"]},
        ),
        patch(
            "kiro_crew.platform.governance.sanitize_agent_config_governance",
            lambda config: None,
        ),
        patch("kiro_crew.dashboard.handlers.mcp._get_mcp_lock", _neutral_lock),
        patch(
            "kiro_crew.dashboard.handlers.mcp._offload_config_write",
            _offload_after_a_concurrent_write,
        ),
    ):
        response = await api_agent_config(request)

    assert response.status == 200
    after = json.loads(mc_cfg.read_text(encoding="utf-8"))
    assert after["default_agent"] == "other", (
        "the PUT published a config.json snapshot it had read BEFORE a concurrent "
        "writer committed, silently reverting that writer's unrelated field"
    )
    # And the PUT's own write still landed -- the point is that both survive.
    assert after["removedTools"]["tools"] == ["c"]


@pytest.mark.asyncio
async def test_agent_config_put_failed_config_write_leaves_bookkeeping_untouched(
    tmp_path, monkeypatch
):
    """A failing ``config.json`` write must leave the bookkeeping sidecar untouched.

    The unit is non-cancellable but not rollback-atomic, so order decides the damage.
    The ``config.json`` write must be the first durable step; if bookkeeping ran first,
    an I/O failure would leave ``agent_model_state.json`` naming an unwritten spec.
    """
    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew", "tools": ["a", "c"]}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"
    mc_cfg.write_text(json.dumps({"default_agent": "kirocrew"}))
    state = tmp_path / "agent_model_state.json"
    # ``model_managed`` already set, so only the ``cc_model`` below can lift --
    # enough for the bookkeeping write to be observable, and it keeps the
    # sidecar off the developer's real ~/.kiro/crew.
    state.write_text(
        json.dumps({"test": {"model_managed": False}}, indent=2, sort_keys=True) + "\n"
    )
    monkeypatch.setattr("kiro_crew.agent_state._state_path", lambda: state)

    before_state = state.read_text()
    before_spec = installed.read_text()

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {
            "config": {
                "name": "test",
                "tools": ["a"],
                "allowedTools": ["b"],
                # Unset in the sidecar above, so the bookkeeping lift WOULD write.
                "cc_model": "claude-sonnet-4.6",
            }
        }

    request.json = mock_json

    def _config_write_fails(path, **kwargs):  # noqa: ANN001 - mirrors the real signature
        # The ``config.json`` write is now the locked primitive, so the injected
        # I/O failure moves there. Still the unit's FIRST durable step, which is
        # what the assertions below pin.
        raise OSError("disk full")

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.agent.build_agent_config", return_value={"toolsSettings": {}}),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": ["a", "c"], "allowedTools": ["b"]},
        ),
        patch(
            "kiro_crew.platform.governance.sanitize_agent_config_governance",
            lambda config: None,
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.update_config_locked",
            _config_write_fails,
        ),
    ):
        response = await api_agent_config(request)

    assert response.status == 500
    assert state.read_text() == before_state, (
        "the bookkeeping lift ran BEFORE the config.json write that failed, so "
        "the 500 leaves agent_model_state.json recording a model for a spec "
        "that was never installed"
    )
    assert (
        installed.read_text() == before_spec
    ), "the agent spec was written even though an earlier write of the unit failed"


@pytest.mark.asyncio
async def test_offload_config_write_drains_through_repeated_cancellation():
    """The drain itself must survive being cancelled.

    A bare ``await future`` drain is cancellable, so a second cancel would release the
    MCP transaction lock mid-write. The write must complete, and only then may the
    ``CancelledError`` escape.
    """
    import threading

    from kiro_crew.dashboard.handlers.mcp import _offload_config_write

    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()

    def _blocking_write():
        started.set()
        # Block until the test has delivered both cancellations.
        release.wait(timeout=10)
        finished.set()
        return "written"

    task = asyncio.ensure_future(_offload_config_write(_blocking_write))

    # try/finally: every assertion below runs while a worker thread is parked in
    # ``release.wait(timeout=10)`` and ``task`` is deliberately
    # cancellation-resistant. An assertion failing before ``release.set()`` would
    # abandon both -- the executor thread blocks for the full 10s and the pending
    # task outlives the test, so ONE genuine regression here degrades every test
    # that shares the default executor. Release and drain unconditionally.
    try:
        # Let the worker thread actually enter the write.
        await asyncio.to_thread(started.wait, 5)
        await asyncio.sleep(0)

        # First cancellation: the shielded await raises, the drain begins.
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "the task completed before the worker was drained"

        # Second cancellation, delivered while the drain is in flight. Pre-fix this
        # cancels the drain and the task finishes with the worker still writing.
        task.cancel()
        await asyncio.sleep(0)

        assert not finished.is_set(), "test bug: the worker finished before it was released"
        assert not task.done(), (
            "the drain was cancelled: _offload_config_write returned while the "
            "worker thread was still writing, so the caller's lock is released "
            "mid-write"
        )

        # Now let the worker complete and confirm the cancellation still propagates.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert finished.is_set(), "the write did not run to completion"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        # Reap the task either way, so a failed assertion cannot leave it pending.
        await asyncio.gather(task, return_exceptions=True)
        # Do not leave the executor thread parked for the rest of the session.
        await asyncio.to_thread(finished.wait, 10)


@pytest.mark.asyncio
async def test_offload_config_write_propagates_write_errors():
    """A failing write still raises -- the drain must not swallow it."""
    from kiro_crew.dashboard.handlers.mcp import _offload_config_write

    def _failing_write():
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        await _offload_config_write(_failing_write)


@pytest.mark.asyncio
async def test_default_agent_write_holds_the_config_lock(tmp_path):
    """PUT /api/config/default-agent holds the dashboard config lock for its RMW.

    The agent-config PUT runs its RMW in a worker thread, so the event loop does not
    serialize the two. A recorder checks the lock covers both read and write.
    """
    import contextlib

    from kiro_crew.config.loader import update_config_locked
    from kiro_crew.dashboard.handlers import api_default_agent

    mc_cfg = tmp_path / "config.json"
    mc_cfg.write_text(json.dumps({"default_agent": "kirocrew", "unrelated": "keep me"}))

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {"agent": "kirocrew"}

    request.json = mock_json

    holding = False
    observed: list[tuple[str, bool]] = []

    @contextlib.asynccontextmanager
    async def _recording_lock():
        nonlocal holding
        holding = True
        try:
            yield
        finally:
            holding = False

    def _recording_locked(path, *, mutate, **kwargs):  # noqa: ANN001 - real signature
        """Observe both halves of the locked read-modify-write.

        The read happens inside ``update_config_locked`` just before *mutate* runs,
        so the callback marks the read and the return marks the write.
        """

        def _observed_mutate(data: dict) -> dict:
            observed.append(("read", holding))
            return mutate(data)

        result = update_config_locked(path, mutate=_observed_mutate, **kwargs)
        observed.append(("write", holding))
        return result

    loaded = MagicMock()
    loaded.agents = {"kirocrew": MagicMock()}
    fake_config_cls = MagicMock()
    fake_config_cls.load.return_value = loaded

    with (
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.dashboard.handlers.agents.KiroCrewConfig", fake_config_cls),
        patch("kiro_crew.dashboard.handlers.agents._get_config_lock", _recording_lock),
        patch("kiro_crew.dashboard.handlers.agents.update_config_locked", _recording_locked),
    ):
        resp = await api_default_agent(request)

    assert resp.status == 200
    assert [step for step, _held in observed] == ["read", "write"], observed
    # BOTH halves, not just the write: a lock taken around the write alone still
    # lets a sibling RMW land between this read and it, which is the whole defect.
    assert all(
        held for _step, held in observed
    ), f"the default-agent read-modify-write ran OUTSIDE the config lock: {observed}"
    # And the ADVISORY lock was taken too, not just the in-process one.
    # The asyncio lock above serializes same-loop callers only, so on its own it
    # leaves the CLI and other processes free to interleave with this RMW.
    assert (tmp_path / "config.json.lock").exists(), (
        "the default-agent RMW did not route through update_config_locked: no "
        "<config>.lock sidecar was taken, so it is still unserialized against "
        "the CLI, worker threads and other processes"
    )
    # And the RMW itself still preserves unrelated settings.
    assert json.loads(mc_cfg.read_text(encoding="utf-8")) == {
        "default_agent": "kirocrew",
        "unrelated": "keep me",
    }


@pytest.mark.asyncio
async def test_agent_config_put_governs_against_the_ceiling_current_at_commit(
    tmp_path, monkeypatch
):
    """The governance verdict must be taken INSIDE the locked region.

    The lock wait is unbounded, so a ceiling revoked meanwhile makes a pre-lock verdict
    stale and the write would republish a withheld auto-approve grant. The fake lock
    revokes the grant as it is acquired.
    """
    import contextlib

    import kiro_crew.platform.governance as gov

    # Mutable ceiling: everything is grantable until the lock wait revokes
    # @revoked_during_wait.
    revoked: set[str] = set()
    monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: ref not in revoked)

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {
            "config": {
                "name": "test",
                "allowedTools": ["@kept", "@revoked_during_wait"],
            }
        }

    request.json = mock_json

    @contextlib.asynccontextmanager
    async def _revoking_lock():
        # Stand-in for a contended, unbounded cross-process flock wait: the
        # ceiling moves while this PUT is queued behind another transaction.
        await asyncio.sleep(0)
        revoked.add("@revoked_during_wait")
        yield

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            # Ships the ref the submission also carries, so a diff taken AFTER
            # the filter would see it missing and record a phantom user removal.
            return_value={"tools": [], "allowedTools": ["@revoked_during_wait"]},
        ),
        patch("kiro_crew.dashboard.handlers.mcp._get_mcp_lock", _revoking_lock),
    ):
        resp = await api_agent_config(request)

    assert resp.status == 200
    written = json.loads(installed.read_text(encoding="utf-8"))
    assert written["allowedTools"] == ["@kept"], (
        "the PUT persisted an auto-approve grant the ceiling withheld before any "
        "durable write: the governance verdict was taken outside the lock and went "
        f"stale during the wait (got {written['allowedTools']})"
    )
    # removedTools must STILL be diffed from the SUBMITTED, pre-governance map.
    # The submission carries @revoked_during_wait, so the user removed nothing —
    # recording it here would suppress that tool on every future upgrade.
    assert "removedTools" not in json.loads(mc_cfg.read_text(encoding="utf-8")), (
        "a ceiling-withheld ref was recorded as a user removal: removed_per_key "
        "was diffed against the POST-governance config"
    )
