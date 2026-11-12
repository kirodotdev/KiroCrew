"""Agent-spec home isolation: derived specs follow the template spec's owner.

Derived specs are written on dispatch, long after boot, so a non-default home must
not write them into a shared agents directory it does not own, and a refused write
is never reported as done. The KIRO_HOME seam and the worktree decline guard are in
``test_agent_home_isolation.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from agent_home_isolation_helpers import _isolate_default_home_fixture  # noqa: F401
from agent_home_isolation_helpers import _pin_default_home_and_breadcrumb  # noqa: F401
from agent_home_isolation_helpers import (
    _durable_checkout,
    _pretend_target_is_shared,
    _spec_pinned_to,
)

from kiro_crew.subprocess_utf8 import UTF8_TEXT

REPO_ROOT = Path(__file__).resolve().parents[1]


def _child_env(**overrides: str) -> dict[str, str]:
    """Env for a probe interpreter that must import THIS worktree.

    ``PYTHONPATH`` leads with this worktree's ``src``, or the child imports another
    checkout's editable install. The parent's ``site-packages`` follow it, because the
    autouse fixture repoints ``HOME`` and would hide ``$HOME/.local`` packages.
    """
    deps = [p for p in sys.path if p and ("site-packages" in p or "dist-packages" in p)]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), *deps]),
    }
    env.pop("KIROCREW_HOME", None)
    env.update(overrides)
    return env


# Derived specs (worker, conductor, service, read-only side spec) write through
# ``agent._atomic_json_write`` on dispatch and obey the same owner check as the
# template spec, or a second gateway would re-pin the machine-wide worker spec.
class TestForeignHomeDerivedSpecWrites:
    """A non-default home must not write DERIVED specs into a shared agents dir."""

    @staticmethod
    def _foreign_shared_dir(monkeypatch, agent_mod, tmp_path) -> Path:
        """A shared agents dir whose owned specs are pinned to ANOTHER home."""
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "my-home"))
        _durable_checkout(monkeypatch, agent_mod)
        shared = tmp_path / "agents"
        shared.mkdir()
        (shared / agent_mod.AGENT_FILENAME).write_text(
            _spec_pinned_to(tmp_path / "someone-elses-home"), encoding="utf-8"
        )
        _pretend_target_is_shared(monkeypatch, agent_mod, shared)
        return shared

    def test_a_new_derived_spec_is_not_created(self, monkeypatch, tmp_path):
        """The write is refused, not merely warned about."""
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        target = shared / WORKER_AGENT_FILENAME

        with pytest.raises(agent.SharedAgentHomeRefused):
            agent._atomic_json_write(target, {"name": "kirocrew-worker"})

        assert (
            not target.exists()
        ), "a non-default-home instance created a derived spec in the shared agents dir"

    def test_the_refusal_is_an_oserror(self, monkeypatch, tmp_path):
        """Callers report the refusal through the ``except OSError`` arm they have.

        The spec editor, the side spec and the Connections mint already treat
        ``OSError`` as a failure, so the base class is the contract.
        """
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)

        assert issubclass(agent.SharedAgentHomeRefused, OSError)
        with pytest.raises(OSError):
            agent._atomic_json_write(shared / WORKER_AGENT_FILENAME, {"name": "x"})

    def test_an_existing_derived_spec_is_left_byte_identical(self, monkeypatch, tmp_path):
        """The harm is an OVERWRITE of the real install's working spec."""
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        target = shared / WORKER_AGENT_FILENAME
        sentinel = _spec_pinned_to(tmp_path / "someone-elses-home")
        target.write_text(sentinel, encoding="utf-8")

        with pytest.raises(agent.SharedAgentHomeRefused):
            agent._atomic_json_write(target, {"name": "kirocrew-worker", "sentinel": "mine"})

        assert (
            target.read_text(encoding="utf-8") == sentinel
        ), "a non-default-home instance re-pinned another home's derived spec"

    def test_a_real_derived_writer_is_covered(self, monkeypatch, tmp_path):
        """Not just the primitive: an actual materialization path must refuse too.

        ``_install_guest_agent`` is the smallest real writer (no locks, no
        dispatch machinery) and reaches the shared directory the same way the
        other writers that go through :func:`_atomic_json_write` do.
        """
        from kiro_crew import agent
        from kiro_crew.agent_files import GUEST_AGENT_FILENAME
        from kiro_crew.agent_materialization import service_agents

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)

        with pytest.raises(agent.SharedAgentHomeRefused):
            service_agents._install_guest_agent()

        assert not (
            shared / GUEST_AGENT_FILENAME
        ).exists(), "a real derived-spec writer bypassed the shared-home ownership guard"

    def test_every_denial_is_audited(self, monkeypatch, tmp_path):
        """Every refusal records its own SEL event; only the WARNING is deduped.

        A permission decision missing from the audit trail cannot be reviewed, so
        ``_declined_home_warned`` dedupes only the log line, never the denied event.
        """
        from kiro_crew import agent
        from kiro_crew.agent_files import GUEST_AGENT_FILENAME, WORKER_AGENT_FILENAME

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        events: list[dict] = []
        warned: list[tuple] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Sel())
        monkeypatch.setattr(agent, "_declined_home_warned", set())
        monkeypatch.setattr(
            agent.logger, "warning", lambda *a, **k: warned.append(a), raising=False
        )

        for name in (WORKER_AGENT_FILENAME, GUEST_AGENT_FILENAME):
            with pytest.raises(agent.SharedAgentHomeRefused):
                agent._atomic_json_write(shared / name, {"name": "x"})

        denied = [e for e in events if e.get("operation") == "agent_home_write"]
        assert len(denied) == 2, (
            f"expected one audited denial per refused write, got {len(denied)}; "
            f"a throttled audit trail loses permission decisions"
        )
        assert all(e["outcome"] == "denied" for e in denied)
        assert all(e["source"] == "derived-spec" for e in denied)
        assert all(str(shared) in e["resources"] for e in denied)
        assert len(warned) == 1, (
            f"the operator-facing WARNING must still dedupe per directory, "
            f"got {len(warned)} lines"
        )

    def test_the_worker_mirror_is_not_recorded_as_derived(self, monkeypatch, tmp_path):
        """A refused worker write must not stamp the mirror as re-derived.

        The spawn gate reads ``set_mirrored_from``: a stamp for unwritten bytes would
        pass it while the mirror still carries grants revoked from ``kirocrew.json``.
        """
        from kiro_crew import agent, agent_state
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME
        from kiro_crew.agent_materialization import worker_agent

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        recorded: list[tuple] = []
        monkeypatch.setattr(
            agent_state,
            "set_mirrored_from",
            lambda *a, **k: recorded.append(a),
        )

        with pytest.raises(OSError):
            worker_agent._write_worker_spec(
                {"name": "kirocrew-worker"},
                shared / WORKER_AGENT_FILENAME,
                template_grants=[],
            )

        assert not recorded, (
            "a refused worker spec write still recorded the mirror as re-derived; "
            "the spawn-path freshness gate would accept revoked grants"
        )

    @pytest.mark.parametrize(
        "module, function, needs_specific_arm",
        [
            ("kiro_crew.dashboard.agent_admin.agent_detail", "api_agent_detail", True),
            ("kiro_crew.dashboard.side_readonly_spec", "publish_readonly_spec", False),
            ("kiro_crew.dashboard.handlers.mcp_agent_sync", "_sync_mcp_to_agent_unlocked", True),
            (
                "kiro_crew.dashboard.handlers.mcp_agent_sync",
                "_sync_mcp_to_agent_batch_unlocked",
                True,
            ),
        ],
    )
    def test_person_facing_writers_report_the_refusal(self, module, function, needs_specific_arm):
        """Each path that reports success to a PERSON carries an arm for the refusal.

        Named per function: the modules also hold writes this guard never refuses.
        ``needs_specific_arm``: the MCP syncs' ``except OSError`` logs and returns, so
        they must name the subtype; ``publish_readonly_spec`` re-raises, so any arm works.
        """
        import ast as _ast
        import importlib

        mod = importlib.import_module(module)
        tree = _ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        target = next(
            (
                node
                for node in _ast.walk(tree)
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and node.name == function
            ),
            None,
        )
        assert target is not None, f"{module}.{function} is gone; has the call site moved?"

        arms = [
            _ast.unparse(handler.type) if handler.type is not None else "bare"
            for node in _ast.walk(target)
            if isinstance(node, _ast.Try)
            for handler in node.handlers
        ]
        if needs_specific_arm:
            assert any("SharedAgentHomeRefused" in arm for arm in arms), (
                f"{module}.{function} catches the refusal only as a generic OSError "
                f"(arms found: {arms}); that arm logs and returns, so the caller still "
                f"reports a removal the spec never took"
            )
        else:
            assert any(
                "SharedAgentHomeRefused" in arm or "OSError" in arm or arm == "bare" for arm in arms
            ), (
                f"{module}.{function} has no arm that catches the shared-home refusal "
                f"(arms found: {arms}); a refused spec write would be reported as a save"
            )

    @pytest.mark.asyncio
    async def test_the_mcp_remove_reports_the_refusal(self, monkeypatch, tmp_path):
        """A refused removal must not answer ``removed: true``.

        Otherwise the spec keeps the server in ``mcpServers`` with its ``autoApprove``,
        a grant that never reaches the PreToolUse gate, while the UI says it is gone.
        """
        import json
        from unittest.mock import MagicMock

        from aiohttp import web
        from body_stream_helpers import attach_body

        from kiro_crew import agent
        from kiro_crew.dashboard.handlers import mcp as mcp_mod
        from kiro_crew.dashboard.handlers.mcp import api_mcp_remove

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", shared)
        spec = shared / agent.AGENT_FILENAME
        before = spec.read_text(encoding="utf-8")

        global_mcp = tmp_path / "settings" / "mcp.json"
        global_mcp.parent.mkdir(parents=True, exist_ok=True)
        global_mcp.write_text(
            json.dumps({"mcpServers": {"some-server": {"command": "x"}}}), encoding="utf-8"
        )
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", global_mcp)
        # The lock sidecar is a module constant derived from the config path at
        # IMPORT time, so redirecting only the config leaves the handler touching
        # a lock under whatever HOME the suite happened to have when this module
        # was first imported -- a FileNotFoundError that depends on test order.
        monkeypatch.setattr(mcp_mod, "_MCP_LOCK_PATH", global_mcp.with_suffix(".lock"))

        async def _no_denial(request, operation):
            return None

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
            _no_denial,
        )

        request = MagicMock(spec=web.Request)
        request.app = {"state": MagicMock()}
        attach_body(request, {"name": "some-server"})

        async def _json():
            return {"name": "some-server"}

        request.json = _json
        resp = await api_mcp_remove(request)

        assert resp.status == 409, "a refused removal was reported as a successful uninstall"
        assert b"agent_home_not_owned" in resp.body
        assert b'"removed": true' not in resp.body
        assert (
            spec.read_text(encoding="utf-8") == before
        ), "the refused removal still modified the spec on disk"
        # The registry is the mutation that runs FIRST. Refusing only at the
        # spec write would leave the server gone from its source while the
        # spec this instance cannot rewrite keeps mounting it.
        assert (
            "some-server" in json.loads(global_mcp.read_text(encoding="utf-8"))["mcpServers"]
        ), "the refused removal still purged the user-level registry"

    @pytest.mark.asyncio
    async def test_the_mcp_toggle_reports_the_refusal(self, monkeypatch, tmp_path):
        """A refused disable must not answer ``applied: true``.

        Same shape as the removal: the toggle marks the entry ``disabled`` in
        memory, and a swallowed refusal leaves the server mounted while the UI
        shows it off.
        """
        import json
        from unittest.mock import MagicMock

        from aiohttp import web
        from body_stream_helpers import attach_body

        from kiro_crew import agent
        from kiro_crew.dashboard.handlers import mcp as mcp_mod
        from kiro_crew.dashboard.handlers.mcp import api_mcp_toggle

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", shared)
        spec = shared / agent.AGENT_FILENAME
        before = spec.read_text(encoding="utf-8")

        global_mcp = tmp_path / "settings" / "mcp.json"
        global_mcp.parent.mkdir(parents=True, exist_ok=True)
        global_mcp.write_text(
            json.dumps({"mcpServers": {"some-server": {"command": "x"}}}), encoding="utf-8"
        )
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", global_mcp)
        # The lock sidecar is a module constant derived from the config path at
        # IMPORT time, so redirecting only the config leaves the handler touching
        # a lock under whatever HOME the suite happened to have when this module
        # was first imported -- a FileNotFoundError that depends on test order.
        monkeypatch.setattr(mcp_mod, "_MCP_LOCK_PATH", global_mcp.with_suffix(".lock"))

        async def _no_denial(request, operation):
            return None

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
            _no_denial,
        )

        request = MagicMock(spec=web.Request)
        request.app = {"state": MagicMock()}
        attach_body(request, {"name": "some-server", "enabled": False})

        async def _json():
            return {"name": "some-server", "enabled": False}

        request.json = _json
        resp = await api_mcp_toggle(request)

        assert resp.status == 409, "a refused disable was reported as applied"
        assert b"agent_home_not_owned" in resp.body
        assert b'"applied": true' not in resp.body
        assert (
            spec.read_text(encoding="utf-8") == before
        ), "the refused toggle still modified the spec on disk"
        # Same ordering: the disabled flag is written to the registry before
        # the spec, so a late refusal shows the row off while the spec mounts it.
        assert (
            "disabled"
            not in json.loads(global_mcp.read_text(encoding="utf-8"))["mcpServers"]["some-server"]
        ), "the refused toggle still wrote the registry flag"

    def test_the_spawn_gate_names_the_unowned_home(self, monkeypatch, tmp_path):
        """A refused mirror re-derive must not read as a generation mismatch.

        ``rederive_worker_agent`` never raises, so the gate asks the ownership question
        itself. The remedy is removing stale ``kirocrew*.json`` specs, not ``KIRO_HOME``,
        which moves kiro-cli session storage that Kiro Crew still reads from the host.
        """
        import json

        from kiro_crew import agent
        from kiro_crew.agent_materialization import worker_agent

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", shared)
        # A mirror that exists, parses, and carries the two marks every
        # derivation writes (the declared name and a kirocrew-work reference),
        # so the gate attributes it as ours and reaches the re-derive instead
        # of refusing earlier as a foreign spec.
        (shared / worker_agent._WORKER_AGENT_FILENAME).write_text(
            json.dumps(
                {
                    "name": "kirocrew-worker",
                    "mcpServers": {"kirocrew-work": {"command": "x"}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(worker_agent, "_derived_spec_matches_default", lambda name: False)

        with pytest.raises(worker_agent.DerivedSpecStale) as caught:
            worker_agent._require_fresh_worker_spec(None)

        message = str(caught.value)
        assert str(shared) in message, "the refusal does not name the directory it cannot write"
        assert "kirocrew*.json" in message, "the refusal does not name the remedy"

    def test_a_refused_fork_refresh_blocks_its_sessions(self, monkeypatch, tmp_path):
        """An ownership refusal must record the fork as unrefreshed.

        ``require_fork_governance`` refuses forks in ``_fork_refresh_failed``. That
        record is the only control on ``allowedTools`` and ``autoApprove``, grants
        that never reach the PreToolUse gate, and no other instance refreshes this fork.
        """
        import json
        from types import SimpleNamespace

        from kiro_crew import agent, agent_state
        from kiro_crew.agent_materialization import fork_refresh

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        fork_name = "kirocrew-fork-of-something"
        crew = "some-crew"
        (shared / f"{fork_name}.json").write_text(
            json.dumps({"name": fork_name, "mcpServers": {}}),
            encoding="utf-8",
        )

        # The fork must clear every gate BEFORE the write, or the test would
        # pass on a fork that never reached the refused call at all: lineage
        # onto an owned template, a crew binding that corroborates it, and no
        # capability record (that branch reconciles and returns early).
        monkeypatch.setattr(
            agent_state,
            "all_fork_info",
            lambda: {fork_name: {"forked_from": "kirocrew", "private_to": crew}},
        )
        monkeypatch.setattr(agent_state, "get_capabilities", lambda name: None)

        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.setattr(
            KiroCrewConfig,
            "load",
            classmethod(
                lambda cls: SimpleNamespace(agents={crew: SimpleNamespace(kiro_agent=fork_name)})
            ),
        )
        monkeypatch.setattr(fork_refresh, "_fork_refresh_failed", frozenset())

        fork_refresh._refresh_forked_templates_locked(gated_off=frozenset())

        assert fork_name in fork_refresh._fork_refresh_failed, (
            "a refused fork refresh left the fork unrecorded; require_fork_governance "
            "would admit sessions on allowedTools and autoApprove this instance never "
            "re-filtered"
        )

    @pytest.mark.asyncio
    async def test_the_dashboard_patch_reports_the_refusal(self, monkeypatch, tmp_path):
        """A PATCH on a mis-homed gateway must not answer 200 for a discarded edit.

        A silent refusal would show the in-memory merge as saved while the file
        on disk is unchanged.
        """
        from unittest.mock import MagicMock

        from aiohttp import web

        from kiro_crew import agent
        from kiro_crew.dashboard.handlers.agents import api_agent_detail

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        spec = shared / "kirocrew.json"
        before = spec.read_text(encoding="utf-8")

        request = MagicMock(spec=web.Request)
        request.method = "PATCH"
        request.match_info = {"name": "kirocrew"}
        request.app = {"state": MagicMock()}

        async def _json():
            return {"model": "a-model-the-user-just-picked"}

        request.json = _json

        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", shared)
        resp = await api_agent_detail(request)

        assert resp.status != 200, "a discarded edit was reported as a successful save"
        assert resp.status == 409
        assert b"agent_home_not_owned" in resp.body
        assert (
            spec.read_text(encoding="utf-8") == before
        ), "the refused PATCH still modified the spec on disk"
        # The model branch writes a per-home sidecar BEFORE the spec, so ownership
        # must be checked first or the sidecar records a model the spec never got.
        from kiro_crew import agent_state

        assert not agent_state._state_path().exists(), (
            "the refused model PATCH still wrote the per-home sidecar, which now "
            "disagrees with the unwritten spec"
        )

    def test_a_broken_audit_sink_still_refuses(self, monkeypatch, tmp_path):
        """A lost record must not turn a refusal into a write.

        Audit calls tolerate an unwritable SEL log, but the refusal must still hold
        with the sink raising on every call.
        """
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)

        class _Broken:
            def log_api_access(self, **kw):
                raise OSError("audit sink unavailable")

        monkeypatch.setattr(agent, "sel", lambda: _Broken())

        with pytest.raises(agent.SharedAgentHomeRefused):
            agent._atomic_json_write(shared / WORKER_AGENT_FILENAME, {"name": "x"})
        assert not (
            shared / WORKER_AGENT_FILENAME
        ).exists(), "a broken audit sink let a foreign-home write through"

    def test_the_mint_write_is_authorized_not_exempt_by_name(self, monkeypatch, tmp_path):
        """A Connect must still work on an instance that owns nothing here.

        The mint copies one server entry from the owner's spec, so refusing it protects
        nothing. It is authorized by the writer, not by filename, since any writer could
        pick the right name; both an authorized and an unauthorized write are pinned.
        """
        from kiro_crew import agent
        from kiro_crew.connections.mint import _mint_spec_name

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        target = shared / f"{_mint_spec_name('some-provider')}.json"

        agent.write_owner_derived_spec(target, {"name": target.stem, "mcpServers": {}})
        assert target.is_file(), (
            "the ownership guard refused an authorized mint write; every Connect on a "
            "non-owning instance would fail"
        )

        unauthorized = shared / f"{_mint_spec_name('other-provider')}.json"
        with pytest.raises(agent.SharedAgentHomeRefused):
            agent._atomic_json_write(unauthorized, {"name": unauthorized.stem})
        assert not unauthorized.exists(), "a mint-shaped NAME bought access to the directory"

    def test_the_mint_itself_passes_the_authorization(self):
        """The mint's own write is the authorized one, asserted at its call site.

        Read structurally: calling the entry point directly says nothing about the mint.
        """
        import ast as _ast

        source = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "connections" / "mint.py"
        ).read_text(encoding="utf-8")
        authorized = [
            node.lineno
            for node in _ast.walk(_ast.parse(source))
            if isinstance(node, _ast.Call)
            and isinstance(node.func, _ast.Attribute)
            and node.func.attr == "write_owner_derived_spec"
        ]
        assert authorized, (
            "no spec write in connections/mint.py goes through "
            "write_owner_derived_spec; the ownership guard would refuse the mint spec "
            "and every Connect on a non-owning instance would fail"
        )

    def test_the_batch_uninstall_arms_its_purge_against_the_refusal(self):
        """The batched uninstall's purge must not surface the refusal as a 500.

        ``_purge_server_config`` strips the scopes before the spec, so a refusal leaves
        the spec mounting the server with nothing to reconcile it. Read structurally:
        reaching it at runtime needs the two-phase apply plus an ownership flip.
        """
        import ast as _ast

        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "kiro_crew"
            / "dashboard"
            / "handlers"
            / "mcp.py"
        ).read_text(encoding="utf-8")
        tree = _ast.parse(source)
        apply_fn = next(
            node
            for node in _ast.walk(tree)
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
            and node.name == "_do_mcp_apply"
        )
        armed = False
        for node in _ast.walk(apply_fn):
            if not isinstance(node, _ast.Try):
                continue
            calls = {
                inner.func.id
                for inner in _ast.walk(node)
                if isinstance(inner, _ast.Call) and isinstance(inner.func, _ast.Name)
            }
            if "_purge_server_config" not in calls and "_offload_config_write" not in calls:
                continue
            for handler in node.handlers:
                if handler.type is not None and "SharedAgentHomeRefused" in _ast.dump(handler.type):
                    armed = True
        assert armed, (
            "the batched apply's purge has no arm for the shared-home refusal; the scopes "
            "are stripped before the spec, so a refusal there answers 500 and leaves the "
            "server mounted with nothing to reconcile it"
        )

    def test_the_default_home_instance_still_writes_derived_specs(self, monkeypatch, tmp_path):
        """The other half of the contract: the real install keeps owning them."""
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _durable_checkout(monkeypatch, agent)
        shared = tmp_path / "agents"
        shared.mkdir()
        _pretend_target_is_shared(monkeypatch, agent, shared)
        target = shared / WORKER_AGENT_FILENAME

        agent._atomic_json_write(target, {"name": "kirocrew-worker"})

        assert target.is_file(), "the default-home instance must still write derived specs"

    def test_self_pinned_specs_keep_their_writer(self, monkeypatch, tmp_path):
        """A relocated install that wrote these specs must not lock itself out."""
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        own_home = (tmp_path / "relocated-home").resolve()
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(own_home))
        _durable_checkout(monkeypatch, agent)
        shared = tmp_path / "agents"
        shared.mkdir()
        (shared / agent.AGENT_FILENAME).write_text(_spec_pinned_to(own_home), encoding="utf-8")
        _pretend_target_is_shared(monkeypatch, agent, shared)
        target = shared / WORKER_AGENT_FILENAME

        agent._atomic_json_write(target, {"name": "kirocrew-worker"})

        assert target.is_file(), "provenance matching must let the writer keep refreshing"

    def test_a_private_target_is_untouched(self, monkeypatch, tmp_path):
        """A redirected (private) agents dir is nobody else's — write it."""
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "my-home"))
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _durable_checkout(monkeypatch, agent)
        private = tmp_path / "private-agents"
        private.mkdir()
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", private)
        monkeypatch.setattr(agent, "ambient_agents_dir", lambda: tmp_path / "ambient-agents")
        target = private / WORKER_AGENT_FILENAME

        agent._atomic_json_write(target, {"name": "kirocrew-worker"})

        assert target.is_file(), "a private target must not be guarded"

    def test_a_non_spec_write_is_never_guarded(self, monkeypatch, tmp_path):
        """The guard is scoped to the agents dir; other JSON writers are untouched."""
        from kiro_crew import agent

        self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        target = elsewhere / "config.json"

        agent._atomic_json_write(target, {"unrelated": True})

        assert target.is_file(), "a write outside the agents dir must not be refused"


# The user-level MCP registry stays on the home its writer writes.
class TestTheMcpRegistryStaysWithItsWriter:
    """The user-level MCP registry is read from the file its writer writes.

    The spec-merge read also supplies the per-server ``disabled`` mute, written to a
    fixed ``Path.home()`` registry. Isolated, the mute reads as absent and a muted
    server comes back in ``allowedTools``, which never reaches the PreToolUse gate.
    """

    def test_the_restriction_reader_stays_with_its_writer(self, tmp_path):
        """The registry a RESTRICTION and a MUTE are read from must not follow ``KIRO_HOME``.

        ``dashboard.handlers.mcp`` writes ``disabledTools`` and ``disabled`` to a fixed
        ``Path.home() / ".kiro" / "settings" / "mcp.json"``; moving the reader alone
        would read a restriction as allow.
        """
        kiro_home_dir = tmp_path / "gateway-kiro"
        kiro_home_dir.mkdir()
        probe = (
            "import json, pathlib\n"
            "from kiro_crew import agent\n"
            "from kiro_crew.agent_materialization import mcp_sources\n"
            "from kiro_crew.dashboard.handlers import mcp as mcp_handlers\n"
            "import inspect\n"
            "print(json.dumps({\n"
            "    'reader': str(agent._KIRO_MCP_JSON),\n"
            "    'merge_names_the_constant':\n"
            "        '_KIRO_MCP_JSON' in inspect.getsource(mcp_sources.merge_mcp_sources),\n"
            "    'writer': str(mcp_handlers._GLOBAL_MCP_JSON),\n"
            "    'home': str(pathlib.Path.home()),\n"
            "}))\n"
        )
        env = _child_env(
            KIRO_HOME=str(kiro_home_dir),
            PYTHONPYCACHEPREFIX=str(tmp_path / "pycache"),
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            env=env,
            cwd=str(REPO_ROOT),
            **UTF8_TEXT,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        import json as _json

        seen = _json.loads(proc.stdout.strip().splitlines()[-1])
        assert seen["reader"] == seen["writer"], (
            "the MCP restriction reader and its writer must name the SAME file; "
            "a split lets a disabled tool read as enabled"
        )
        assert seen["reader"] == str(
            Path(seen["home"]) / ".kiro" / "settings" / "mcp.json"
        ), "the restriction registry must stay on the host home its writer uses"
        assert str(kiro_home_dir) not in seen["reader"]
        assert seen["merge_names_the_constant"], (
            "the rebuild's merge reads some other registry than the one its writer "
            "writes; the per-server mute lives in that file, so a split re-enables a "
            "muted server and re-appends it to allowedTools"
        )


def _stub_disconnect_collaborators(monkeypatch, *, url: str, purge) -> None:
    """Stub everything ``remove_provider_entry`` reaches, leaving its judgment real.

    Patched at each DEFINING module: ``remove_provider_entry`` imports them inside its
    body, so a patch on ``ownership`` would do nothing. ``spec_census`` lives in
    ``ownership``; its one ``kirocrew``-scope entry is the minimum that makes the slug
    owned with no second holder, so the revoke is reached.
    """
    monkeypatch.setattr(
        "kiro_crew.connections.ownership.spec_census",
        lambda *a, **k: ({"kirocrew": {"demo": {"url": url}}}, ()),
    )
    monkeypatch.setattr("kiro_crew.mcp_discovery.list_servers", lambda *a, **k: [])
    monkeypatch.setattr("kiro_crew.mcp_grant.revoke_local_grant", lambda *a, **k: [])
    monkeypatch.setattr("kiro_crew.mcp_grant.surviving_grant_artifacts", lambda *a, **k: ())
    monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp._purge_server_config", purge)
    monkeypatch.setattr("kiro_crew.agent.rebuild_agent_config", lambda *a, **k: None)
    monkeypatch.setattr(
        "kiro_crew.connections.warm.rearm_invalidated_provider", lambda *a, **k: None
    )


# A refusal reported as success stops the operator from fixing what is still broken.
class TestARefusedWriteIsNotReportedAsDone:
    """A refused spec write must not be reported to the caller as a completed one."""

    @pytest.mark.asyncio
    async def test_a_refused_entry_purge_is_not_reported_as_removed(self, monkeypatch):
        """A disconnect whose config purge is refused must answer ``entry_removed`` false.

        The purge may fail without stopping the grant revoke, but nothing reconciles
        the leftover entry, so ``entryRemoved`` must not claim it is gone.
        """
        from kiro_crew.agent import SharedAgentHomeRefused
        from kiro_crew.connections import ownership as own

        url = "https://example.invalid/mcp"
        refused: list[str] = []

        def _purge(*_a, **_k):
            refused.append("purge")
            raise SharedAgentHomeRefused("another data home owns the shared agent specs")

        _stub_disconnect_collaborators(monkeypatch, url=url, purge=_purge)

        scope = await own.remove_provider_entry("demo", url, ())

        assert refused == ["purge"], "the purge never ran, so this proves nothing about a refusal"
        assert scope.entry_removed is False, (
            "a refused config purge was reported as entry_removed; the dashboard renders "
            "that as removed while the server is still mounted with its autoApprove"
        )

    @pytest.mark.asyncio
    async def test_a_permitted_entry_purge_is_still_reported_as_removed(self, monkeypatch):
        """The control: a purge that COMMITS must still report the removal.

        Without this, making the refusal honest could be satisfied by reporting
        false unconditionally, which would tell every operator their disconnect
        failed.
        """
        from kiro_crew.connections import ownership as own

        url = "https://example.invalid/mcp"
        _stub_disconnect_collaborators(
            monkeypatch, url=url, purge=lambda *_a, **_k: {"purged": True}
        )

        scope = await own.remove_provider_entry("demo", url, ())

        assert scope.entry_removed is True, "a committed purge must still report the removal"


# The rebuild's derived installs run after it writes kirocrew.json, and the temp-checkout
# arm declines once a spec exists, so a per-write answer would refuse the rebuild itself.
class TestTheRebuildsOwnDecisionCoversItsDerivedSpecs:
    """One ownership decision per rebuild, not one per spec it writes."""

    @staticmethod
    def _temp_checkout(monkeypatch, agent_mod, shared: Path) -> None:
        """Present a plain clone under the system temp root, with an empty shared dir.

        Callers build under ``tempfile.gettempdir()``, not ``tmp_path``: pytest's basetemp
        predates the suite's tempfile redirect, so the temp arm would be missed.
        """
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _pretend_target_is_shared(monkeypatch, agent_mod, shared)

    def test_a_fresh_temp_install_is_refused_once_its_own_main_spec_exists(
        self, monkeypatch, tmp_path
    ):
        """The defect, stated as the guard's own answer flipping mid-rebuild.

        An empty shared dir is writable; writing ``kirocrew.json`` flips the answer. This
        pins :func:`_decline_shared_agent_home` so the fix is about the rebuild's scope.
        """
        import tempfile

        from kiro_crew import agent

        shared = tmp_path / "agents"
        shared.mkdir()
        with tempfile.TemporaryDirectory(prefix="kc-fresh-") as scratch_name:
            clone = Path(scratch_name) / "repo"
            (clone / "src" / "kiro_crew").mkdir(parents=True)
            (clone / ".git").mkdir()  # a DIRECTORY -> ordinary clone, not a worktree
            monkeypatch.setattr(agent, "__file__", str(clone / "src" / "kiro_crew" / "agent.py"))
            self._temp_checkout(monkeypatch, agent, shared)

            assert agent._decline_shared_agent_home(audit=False) is None, (
                "an empty shared home must be writable from a temp clone; this test's "
                "premise is gone and the fix below proves nothing"
            )

            (shared / agent.AGENT_FILENAME).write_text("{}", encoding="utf-8")

            assert agent._decline_shared_agent_home(audit=False) is not None, (
                "the temp arm no longer declines once a spec is present, so the "
                "mid-rebuild flip this class exists for cannot happen"
            )

    def test_a_derived_write_inside_an_admitted_rebuild_is_not_refused(self, monkeypatch, tmp_path):
        """The fix: the rebuild's admission covers the specs it installs.

        Driven through :func:`_declined_foreign_spec_write` in the rebuild's state: a
        temp checkout whose main spec is already on disk.
        """
        import tempfile

        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        shared = tmp_path / "agents"
        shared.mkdir()
        (shared / agent.AGENT_FILENAME).write_text("{}", encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="kc-fresh-") as scratch_name:
            clone = Path(scratch_name) / "repo"
            (clone / "src" / "kiro_crew").mkdir(parents=True)
            (clone / ".git").mkdir()
            monkeypatch.setattr(agent, "__file__", str(clone / "src" / "kiro_crew" / "agent.py"))
            self._temp_checkout(monkeypatch, agent, shared)
            target = shared / WORKER_AGENT_FILENAME

            assert agent._declined_foreign_spec_write(target) is True, (
                "outside a rebuild this write must still be refused; if it is not, the "
                "assertion below cannot tell the fix from no guard at all"
            )

            _rebuild_prev = agent._rebuild_spec_install_admitted.get()
            agent._rebuild_spec_install_admitted.set(True)
            try:
                assert agent._declined_foreign_spec_write(target) is False, (
                    "a derived spec was refused inside the rebuild that was already "
                    "admitted, so a fresh temp-checkout install writes its main spec and "
                    "none of its derived ones"
                )
            finally:
                agent._rebuild_spec_install_admitted.set(_rebuild_prev)

    def test_the_admission_does_not_outlive_a_raising_rebuild(self, monkeypatch, tmp_path):
        """A rebuild that raises mid-install must not leave the guard exempted.

        The flag must reset in a ``finally``, or one raise exempts every later write in
        the process from the guard.
        """
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        shared = self._foreign_shared_dir_for_rebuild(monkeypatch, agent, tmp_path)

        def _boom() -> None:
            raise RuntimeError("AIM capabilities install blew up")

        monkeypatch.setattr(agent, "_install_aim_capabilities", _boom)
        monkeypatch.setattr(agent, "_decline_shared_agent_home", lambda **_k: None)
        monkeypatch.setattr(agent, "migrate_agent_specs", lambda: None)
        monkeypatch.setattr(agent.default_spec_commit, "write_default_spec", lambda *a, **k: None)

        with pytest.raises(RuntimeError):
            agent.rebuild_agent_config()

        assert agent._rebuild_spec_install_admitted.get() is False, (
            "the rebuild's admission survived an exception, so every later shared "
            "write in this process is exempt from the ownership guard"
        )
        # And the guard is demonstrably live again, not merely flagged off.
        monkeypatch.setattr(agent, "_decline_shared_agent_home", lambda **_k: shared)
        assert agent._declined_foreign_spec_write(shared / WORKER_AGENT_FILENAME) is True

    def test_the_admission_does_not_cross_into_another_threads_write(self, monkeypatch, tmp_path):
        """A concurrent write on another thread still consults the guard.

        The exemption is a ``ContextVar`` confined to the rebuild's context, so a
        dashboard or dispatch write on another thread sees the default and is refused.
        """
        import threading

        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        shared = self._foreign_shared_dir_for_rebuild(monkeypatch, agent, tmp_path)
        monkeypatch.setattr(agent, "_decline_shared_agent_home", lambda **_k: shared)
        target = shared / WORKER_AGENT_FILENAME

        # Simulate being mid-rebuild on this stack: the exemption is set here.
        _rebuild_prev = agent._rebuild_spec_install_admitted.get()
        agent._rebuild_spec_install_admitted.set(True)
        try:
            # On this stack the exemption holds -- the rebuild's own writes pass.
            assert agent._declined_foreign_spec_write(target) is False

            # A write dispatched to another thread (as a dashboard/dispatch
            # writer would be) must NOT inherit the exemption set after the
            # thread began: it sees the default False and the guard refuses it.
            result: dict[str, bool] = {}

            def _write_from_other_thread() -> None:
                result["declined"] = agent._declined_foreign_spec_write(target)

            worker = threading.Thread(target=_write_from_other_thread)
            worker.start()
            # flake-ok: pre-existing unbounded join of a thread whose target is one synchronous guard call, moved verbatim; tracked for a de-flake pass
            worker.join()

            assert result["declined"] is True, (
                "a concurrent write on another thread was exempted by a rebuild's "
                "admission; the exemption leaked past the rebuild's own context"
            )
        finally:
            agent._rebuild_spec_install_admitted.set(_rebuild_prev)

    @staticmethod
    def _foreign_shared_dir_for_rebuild(monkeypatch, agent_mod, tmp_path) -> Path:
        """A shared agents dir this instance does not own, for the raise test."""
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "my-home"))
        _durable_checkout(monkeypatch, agent_mod)
        shared = tmp_path / "agents"
        shared.mkdir()
        (shared / agent_mod.AGENT_FILENAME).write_text(
            _spec_pinned_to(tmp_path / "someone-elses-home"), encoding="utf-8"
        )
        _pretend_target_is_shared(monkeypatch, agent_mod, shared)
        return shared
