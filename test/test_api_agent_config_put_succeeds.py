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

import kiro_crew.config.loader as loader_module
from kiro_crew.dashboard.handlers import api_agent_config


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
async def test_api_agent_config_put_strips_governed_grants(tmp_path, monkeypatch):
    """A dashboard PUT persists the config verbatim, so it MUST run the whole map
    through the governance filter — else a governed @denied allowedTools entry or
    a governed server's autoApprove written here restores the bypass the per-ref
    writers close. Executable (not source-inspection) coverage of that writer."""
    import kiro_crew.platform.governance as gov

    # Govern @denied only; everything else may auto-approve.
    monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: ref != "@denied")

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
        patch("kiro_crew.dashboard.handlers.agents.get_shipped_tools", return_value={"tools": [], "allowedTools": []}),
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

    Regression for #2570: the agent-detail PATCH strips ``model_managed`` /
    ``cc_model``, but the whole-config PUT used to persist them verbatim.
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
    # Lifted into the sidecar when previously unset (same rule as migrate).
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
    """PUT must persist the installed spec via write_config_atomically, not a
    bare write_text.

    Regression for #5086: a truncating in-place write leaves the spec corrupt
    on a mid-write crash or disk-full, breaking every subsequent session start
    because kiro-cli reads the spec at spawn.  The fix routes the write through
    write_config_atomically (temp-file + os.replace), matching the mc_cfg sidecar
    write already in the same handler.

    This test patches write_config_atomically at the site the handler imports it
    from and asserts it is called exactly once with the right args; it also
    patches Path.write_text to assert the handler never falls back to a bare,
    non-atomic write on the installed-spec path.
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

    Agent spec files are a census source for the MCP config transactions in
    ``handlers/mcp.py``, which read current state and then act on it while
    holding ``_get_mcp_lock``. A config write landing between that read and the
    act would have the transaction commit a decision about contents that changed
    underneath it. External writers cannot be serialized; the gateway's own
    writer must be.
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
    assert all(held for _p, held, _f in spec_writes), (
        "the agent-spec write ran OUTSIDE the MCP transaction lock"
    )
    # bridges.py does whole-file read-modify-writes of THIS SAME file under its
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

    Holding the lock is not enough. A cancelled PUT unwinds the async context and
    releases ``_get_mcp_lock`` while a bare ``asyncio.to_thread`` worker keeps
    writing, so an MCP config transaction can take the lock, read the OLD state,
    act on it, and only then have the worker publish a change the transaction
    never saw. ``_offload_config_write`` exists for exactly this: it drains the
    worker before the lock is released (its own drain behaviour is pinned in
    mcp.py).

    This asserts the structural property that makes the window unrepresentable,
    in the form the invariant actually needs: there is exactly ONE dispatch
    through the shielded offload, and all three durable writes -- the bookkeeping
    lift, the removedTools sidecar and the installed spec -- happen inside it.
    A second dispatch, or any write outside one, re-creates a cancellation point
    between two writes however wide the lock is.

    Supersedes an earlier version that asserted the offloaded callable was named
    ``_write_installed_config_locked``. That name pinned the arrangement in which
    the spec write was the only offloaded write and the other two ran on the
    event loop -- the arrangement R3a and R3b were defects of. Asserting the
    write set and its containment pins the property instead of the spelling, and
    the spec write is still covered (it is one of the three).
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
    assert {label for label, _ in writes} == {"bookkeeping", "mc_cfg", "spec"}, (
        f"not every durable write of the PUT was observed: {writes}"
    )
    outside = [label for label, inside in writes if not inside]
    assert not outside, (
        f"{outside} ran OUTSIDE the shielded offload: a cancellation at the "
        "await that dispatched it releases the transaction lock with the write "
        "still in flight"
    )


@pytest.mark.asyncio
async def test_agent_config_put_persists_nothing_before_the_transaction_lock(tmp_path):
    """A PUT cancelled while WAITING for the transaction lock must leave no trace.

    ``_McpFileLock.__aenter__`` awaits ``run_in_executor(acquire_lock)``, so the
    lock WAIT is a cancellation point -- and a contended flock makes that wait
    unbounded, however long the other holder takes. A gateway shutdown (or a
    client disconnect) that cancels the request there raises out of
    ``__aenter__``, so the handler never reaches the agent-spec write. Anything
    it already persisted at that moment is a HALF-APPLIED PUT: the removedTools
    bookkeeping in ``config.json`` records that the user dropped a shipped tool
    while the spec on disk still grants it, and nothing ever reconciles the two
    -- the next upgrade honours the bookkeeping and silently removes a tool the
    user never saw removed.

    Pins the invariant structurally rather than probabilistically: NO durable
    write happens before the lock is held, so every write in the sequence
    commits under the same lock and cancellation at the wait tears nothing.
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
            await asyncio.wait_for(reached_lock.wait(), timeout=5)
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

    The half of the invariant about cancellation. Holding the lock across all
    three durable writes is not enough on its own: if any of them is reached
    through an await that is a plain cancellation point, a cancel delivered
    there unwinds ``async with`` and releases the lock while the worker thread
    it dispatched keeps writing. The next PUT (or any MCP transaction) then
    enters the lock against an in-flight write and reads a state no writer ever
    committed -- the very interleaving the lock exists to forbid.

    Pinned structurally rather than probabilistically: at the moment the lock
    is released, EVERY durable write of the PUT must already have completed.
    That is only representable when the writes form one uninterruptible unit,
    so this fails on any shape that leaves a cancellable await between the
    lock and a write.
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

            # Cancel with the write parked. Give the loop room to deliver it and
            # to run any unwind the handler's shape allows.
            task.cancel()
            for _ in range(10):
                if task.done():
                    break
                await asyncio.sleep(0.01)

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

    The other half of the invariant. ``read_config_for_update`` fails closed on
    a corrupt ``config.json`` and the handler answers 500 -- correct only while
    nothing has been written yet. Reached AFTER the bookkeeping lift, the same
    500 leaves ``agent_model_state.json`` recording a model for a spec that was
    never written: bookkeeping durable while the spec it describes is not, which
    is exactly the half-applied state the transaction lock was widened to make
    unreachable.

    Pins all three durable targets byte-identical, so the property is "nothing
    was persisted" rather than "the write I happened to think of was skipped".
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
    state.write_text(json.dumps({"test": {"model_managed": False}}, indent=2, sort_keys=True) + "\n")
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

    The read that feeds the ``removedTools`` write used to happen in the handler,
    one executor hop before the worker published the result. Any concurrent
    whole-file ``config.json`` writer landing in that gap -- ``api_default_agent``
    does exactly such a read-modify-write, and takes no lock at all -- had its
    unrelated fields silently reverted when the PUT renamed its already-stale
    snapshot into place. Nothing reports that: the setting simply goes back to
    its old value.

    Injects that writer at exactly that point, the offload dispatch. On the
    pre-fix shape the baseline is already captured by the time this runs, so the
    write below is lost; with the read moved inside the unit, immediately
    adjacent to the write it feeds, it is the READ that runs afterwards and the
    concurrent field survives.
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

    The unit is non-cancellable but NOT rollback-atomic, so its ORDER is what
    decides the damage a failed write leaves behind. With the bookkeeping lift
    running first, an I/O failure on the ``config.json`` write (permission,
    quota, disk full, a failed atomic rename) answered 500 with
    ``agent_model_state.json`` already recording a model for a spec that never
    landed -- strictly worse than the pre-lock ordering, which wrote
    ``config.json`` before bookkeeping.

    Pins the restored prefix: the ``config.json`` write is the first durable
    step, so its failure leaves the other two targets byte-identical.
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

    ``_offload_config_write`` shields the write and, on ``CancelledError``,
    re-awaits the future to drain the worker. A bare ``await future`` in that
    handler is itself cancellable: a SECOND cancellation arriving during the
    drain cancels the drain, so the caller unwinds -- releasing the MCP
    transaction lock -- while the worker thread is still writing. That is the
    exact window the offload exists to close, so it must hold under repeated
    cancellation, not just the first one.

    Pins all three guarantees: the write runs to completion before control
    returns, the ``CancelledError`` still escapes, and completion happens
    BEFORE it escapes.
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
    """PUT /api/config/default-agent read-modify-writes config.json, so it must
    hold the same in-process lock every sibling RMW in the dashboard takes.

    The agent-config PUT moved its own RMW into a WORKER THREAD, holding
    ``_get_config_lock`` across the offload. The event loop therefore no longer
    serializes the two for free: an unlocked read here can capture a baseline
    the worker is about to republish, and the last atomic rename silently
    reverts the other side's unrelated settings.

    Instrumentation, not a true race: the lock has to cover BOTH halves of the
    read-modify-write, and a recorder proves that deterministically where a
    timing test would only prove it sometimes.
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

        The read is no longer a separate call the handler makes: it happens
        inside ``update_config_locked``, immediately before it invokes *mutate*.
        Recording at the callback is therefore the read, and recording after the
        primitive returns is the write -- the same two observations as before,
        taken where they now happen.
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
    assert all(held for _step, held in observed), (
        f"the default-agent read-modify-write ran OUTSIDE the config lock: {observed}"
    )
    # And the ADVISORY lock was taken too, not just the in-process one (#8032).
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

    The transaction lock is a cross-process flock and its wait is unbounded, so
    a ceiling revoked while this PUT queues behind another transaction leaves a
    pre-lock verdict stale — and the write then republishes an auto-approve
    grant governance has already withheld, which is precisely the bypass the
    filter exists to close.

    Modelled deterministically rather than by timing: the fake transaction lock
    revokes the grant as it is acquired, i.e. strictly after any pre-lock
    snapshot and strictly before any durable write.
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
