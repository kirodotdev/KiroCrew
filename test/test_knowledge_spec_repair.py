"""The knowledge pool keeps Crew's own zero-tool spec clean between boots.

``kirocrew-knowledge.json`` lives in the shared agents directory, where another tool
may rewrite every spec it finds: a launcher that adds its own MCP server to ``tools``,
``allowedTools`` and a ``permissions`` block, for one. The pool's confirmation in
``AcpWorker.start`` correctly refuses such a spec, so these tests pin the repair that
puts the template back before each worker spawn
(``service_agents.repair_knowledge_agent_spec``), and that the refusal still stands
when the spec cannot be restored.

The worker tests drive a real ``AcpClient``'s zero-tool confirmation against the files
on disk, with only the process spawn replaced, so "the effective spec" below is the
view or the authored file the harness would load.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import skill_projection as projection
from kiro_crew.acp_backends import ACP_BACKEND_KIRO, ACP_BACKEND_OPENCODE
from kiro_crew.agent_files import KNOWLEDGE_AGENT_FILENAME
from kiro_crew.agent_materialization import auto_approve, service_agents
from kiro_crew.config import paths
from kiro_crew.knowledge import llm_pool
from kiro_crew.knowledge.llm_pool import AGENT_NAME, AcpWorker

#: A value only the injected server's ``env`` carries, so a log line that quotes a
#: value would show it.
_NONCE = "nonce-value-7c41d2"


@pytest.fixture
def agents_dir() -> Path:
    """The one agents directory the repair writes and every reader resolves."""
    agents = agent_mod.kiro_agents_dir_path()
    assert agents == paths.kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    return agents


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agents_dir: Path) -> Path:
    """The pool's cwd, with the native skill projection confined to this test's tree."""
    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    kiro_home = tmp_path / "kiro-home"
    kiro_home.mkdir()
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    monkeypatch.setattr(projection, "kiro_home", lambda: kiro_home)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents_dir)
    monkeypatch.setattr(projection.platform_compat, "path_volume_is_remote", lambda path: False)
    monkeypatch.setattr(projection.platform_compat, "first_linked_ancestor", lambda path: None)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", kiro_home / "settings" / "mcp.json")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name=AGENT_NAME, filename=KNOWLEDGE_AGENT_FILENAME, scope="global")
        ],
    )
    return work_dir


def _spec_path(agents: Path) -> Path:
    return agents / KNOWLEDGE_AGENT_FILENAME


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _template(agents: Path) -> dict[str, Any]:
    """What the boot installer writes, read back from its own write."""
    service_agents._install_knowledge_agent()
    return _read(_spec_path(agents))


def _inject(path: Path) -> None:
    """Rewrite *path* the way a launcher that mounts its own server into every spec does."""
    spec = _read(path) if path.exists() else {"name": AGENT_NAME, "tools": []}
    spec["mcpServers"] = {
        "launcher-creds": {
            "command": "launcher",
            "args": ["mcp"],
            "env": {"LAUNCHER_NONCE": _NONCE},
        }
    }
    spec["tools"] = ["@launcher-creds"]
    spec["allowedTools"] = ["@launcher-creds"]
    spec["permissions"] = {
        "rules": [
            {"capability": "mcp", "match": ["launcher-creds/*"], "effect": "allow"},
            {"capability": "fs_read", "match": ["./**"], "effect": "allow"},
        ]
    }
    spec["kiroUpgrade"] = {"revision": 2, "fingerprint": "f", "source": "sha256:0"}
    spec["toolsSettings"] = {}
    path.write_text(json.dumps(spec, indent=4), encoding="utf-8")


def _assert_mounts_nothing(spec: dict[str, Any]) -> None:
    assert spec["tools"] == []
    assert not spec.get("mcpServers")
    assert "allowedTools" not in spec
    assert "permissions" not in spec


# -- the repair ------------------------------------------------------------------------


class TestRepairKnowledgeAgentSpec:
    def test_an_injected_spec_is_rewritten_to_the_installed_template(self, agents_dir):
        template = _template(agents_dir)
        _inject(_spec_path(agents_dir))

        assert service_agents.repair_knowledge_agent_spec() is True

        repaired = _read(_spec_path(agents_dir))
        assert repaired == template
        _assert_mounts_nothing(repaired)

    def test_the_installed_template_is_left_alone(self, agents_dir):
        _template(agents_dir)
        before = _spec_path(agents_dir).stat()

        assert service_agents.repair_knowledge_agent_spec() is False

        after = _spec_path(agents_dir).stat()
        # The governed write swaps in a new inode, so an unchanged one means no write.
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)

    def test_a_reserialized_copy_of_the_template_is_left_alone(self, agents_dir):
        template = _template(agents_dir)
        reordered = dict(reversed(list(template.items())))
        _spec_path(agents_dir).write_text(json.dumps(reordered), encoding="utf-8")

        assert service_agents.repair_knowledge_agent_spec() is False
        assert _spec_path(agents_dir).read_text(encoding="utf-8") == json.dumps(reordered)

    def test_a_missing_spec_is_written(self, agents_dir):
        assert not _spec_path(agents_dir).exists()

        assert service_agents.repair_knowledge_agent_spec() is True

        _assert_mounts_nothing(_read(_spec_path(agents_dir)))

    def test_a_spec_that_is_not_json_is_rewritten(self, agents_dir):
        _spec_path(agents_dir).write_text("{not json", encoding="utf-8")

        assert service_agents.repair_knowledge_agent_spec() is True

        _assert_mounts_nothing(_read(_spec_path(agents_dir)))

    def test_a_value_of_another_json_type_counts_as_drift(self, agents_dir):
        """``0`` equals ``False`` to Python but is a different file to kiro-cli's schema."""
        template = _template(agents_dir)
        _spec_path(agents_dir).write_text(
            json.dumps({**template, "includeMcpJson": 0}), encoding="utf-8"
        )

        assert service_agents.repair_knowledge_agent_spec() is True
        assert _read(_spec_path(agents_dir))["includeMcpJson"] is False

    def test_the_warning_names_the_keys_and_never_their_values(self, agents_dir, caplog):
        _template(agents_dir)
        _inject(_spec_path(agents_dir))

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert service_agents.repair_knowledge_agent_spec() is True

        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        repair = [m for m in messages if "was not the spec Kiro Crew writes" in m]
        assert len(repair) == 1
        for key in ("allowedTools", "kiroUpgrade", "mcpServers", "permissions", "tools"):
            assert repr(key) in repair[0]
        assert not any(_NONCE in m for m in messages)
        assert not any("launcher-creds" in m for m in messages)

    def test_a_declined_write_leaves_the_file_and_reports_no_repair(self, agents_dir, monkeypatch):
        """A data home that does not own the agents directory may not write into it."""
        _template(agents_dir)
        _inject(_spec_path(agents_dir))
        injected = _spec_path(agents_dir).read_text(encoding="utf-8")

        def refuse(*args: object, **kwargs: object) -> None:
            raise agent_mod.SharedAgentHomeRefused("this data home does not own it")

        monkeypatch.setattr(auto_approve, "write_governed_spec", refuse)

        assert service_agents.repair_knowledge_agent_spec() is False
        assert _spec_path(agents_dir).read_text(encoding="utf-8") == injected

    def test_a_failed_write_is_logged_and_reports_no_repair(self, agents_dir, monkeypatch, caplog):
        _inject(_spec_path(agents_dir))

        def fail(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(auto_approve, "write_governed_spec", fail)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert service_agents.repair_knowledge_agent_spec() is False
        assert any(
            "Could not rewrite the knowledge agent spec" in r.getMessage() for r in caplog.records
        )


# -- the worker ------------------------------------------------------------------------


def _client_factory(
    work_dir: Path,
    made: list[acp_client.AcpClient],
    after_publication: Callable[[projection.NativeSkillProjection], None] | None = None,
) -> Callable[..., acp_client.AcpClient]:
    """Real ``AcpClient`` objects whose spawn prepares the projection and nothing else.

    ``client_start`` prepares the native skill projection inside ``ensure_ready``, from
    the authored spec as it is at that moment, and hands the view to the harness; this
    stand-in does the same. *after_publication* runs on the prepared projection once
    its alias file is published, where a write by another process would land.
    """

    def make(**kwargs: Any) -> acp_client.AcpClient:
        client = acp_client.AcpClient(
            agent=kwargs["agent"], work_dir=work_dir, acp_backend=kwargs["acp_backend"]
        )

        async def ensure_ready() -> None:
            client._native_skill_projection = projection.prepare_native_skill_projection(
                work_dir, per_session_element=False
            )
            if after_publication is not None and client._native_skill_projection is not None:
                after_publication(client._native_skill_projection)

        client.ensure_ready = ensure_ready
        client.shutdown = AsyncMock()
        client.send_message = AsyncMock(return_value="{}")
        made.append(client)
        return client

    return make


async def _start(
    monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> tuple[AcpWorker, list[acp_client.AcpClient]]:
    made: list[acp_client.AcpClient] = []
    monkeypatch.setattr(llm_pool, "AcpClient", _client_factory(work_dir, made))
    worker = AcpWorker(acp_backend=ACP_BACKEND_KIRO, sandbox_mode="auto")
    await worker.start()
    return worker, made


class TestKnowledgeWorkerOnAnInjectedSpec:
    @pytest.mark.asyncio
    async def test_a_worker_starts_and_the_view_it_runs_under_mounts_nothing(
        self, agents_dir, workspace, monkeypatch
    ):
        template = _template(agents_dir)
        _inject(_spec_path(agents_dir))

        worker, made = await _start(monkeypatch, workspace)

        assert len(made) == 1 and worker._client is made[0]
        prepared = made[0]._native_skill_projection
        assert prepared is not None
        _assert_mounts_nothing(prepared.specs[AGENT_NAME])
        alias = agents_dir / f"{prepared.agent(AGENT_NAME)}.json"
        _assert_mounts_nothing(_read(alias))
        assert _read(_spec_path(agents_dir)) == template

    @pytest.mark.asyncio
    async def test_without_a_projection_the_worker_starts_on_the_repaired_spec(
        self, agents_dir, workspace, monkeypatch
    ):
        """With the projection switched off kiro-cli loads the named spec itself."""
        monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
        _template(agents_dir)
        _inject(_spec_path(agents_dir))

        worker, made = await _start(monkeypatch, workspace)

        assert len(made) == 1 and worker._client is made[0]
        assert made[0]._native_skill_projection is None
        _assert_mounts_nothing(_read(_spec_path(agents_dir)))

    @pytest.mark.asyncio
    async def test_a_spec_that_cannot_be_restored_still_refuses(
        self, agents_dir, workspace, monkeypatch
    ):
        _template(agents_dir)
        _inject(_spec_path(agents_dir))

        def refuse(*args: object, **kwargs: object) -> None:
            raise agent_mod.SharedAgentHomeRefused("this data home does not own it")

        monkeypatch.setattr(auto_approve, "write_governed_spec", refuse)
        made: list[acp_client.AcpClient] = []
        monkeypatch.setattr(llm_pool, "AcpClient", _client_factory(workspace, made))
        worker = AcpWorker(acp_backend=ACP_BACKEND_KIRO, sandbox_mode="auto")

        with pytest.raises(RuntimeError, match="could not be confirmed"):
            await worker.send_message("untrusted document text")

        assert len(made) == 1
        made[0].shutdown.assert_awaited_once()
        made[0].send_message.assert_not_awaited()
        assert worker._client is None
        assert _read(_spec_path(agents_dir))["tools"] == ["@launcher-creds"]


def _alias_path(agents: Path, prepared: projection.NativeSkillProjection) -> Path:
    return agents / f"{prepared.agent(AGENT_NAME)}.json"


class TestTheConfirmationReadsThePublishedAlias:
    """With the projection on, kiro-cli loads the alias file published under the agents
    directory, so the confirmation reads that file after start as well as the view."""

    @pytest.mark.asyncio
    async def test_a_tool_written_to_the_alias_after_publication_refuses(
        self, agents_dir, workspace, monkeypatch
    ):
        _template(agents_dir)

        def add_a_server(prepared: projection.NativeSkillProjection) -> None:
            path = _alias_path(agents_dir, prepared)
            spec = _read(path)
            spec["mcpServers"] = {"another-server": {"command": "another-server", "args": []}}
            spec["tools"] = ["@another-server"]
            path.write_text(json.dumps(spec), encoding="utf-8")

        made: list[acp_client.AcpClient] = []
        monkeypatch.setattr(
            llm_pool, "AcpClient", _client_factory(workspace, made, after_publication=add_a_server)
        )
        worker = AcpWorker(acp_backend=ACP_BACKEND_KIRO, sandbox_mode="auto")

        with pytest.raises(RuntimeError, match="could not be confirmed"):
            await worker.send_message("untrusted document text")

        assert len(made) == 1
        prepared = made[0]._native_skill_projection
        assert prepared is not None
        # The view held in memory still mounts nothing; only the file kiro-cli loads
        # does, and that is what refuses.
        _assert_mounts_nothing(prepared.specs[AGENT_NAME])
        assert _read(_alias_path(agents_dir, prepared))["tools"] == ["@another-server"]
        made[0].shutdown.assert_awaited_once()
        made[0].send_message.assert_not_awaited()
        assert worker._client is None

    @pytest.mark.asyncio
    async def test_a_rewritten_alias_that_still_declares_no_tools_starts(
        self, agents_dir, workspace, monkeypatch
    ):
        """The file is judged by its ``tools``, not its bytes: a rewrite that keeps
        the list empty still confirms the ban."""
        _template(agents_dir)

        def reformat(prepared: projection.NativeSkillProjection) -> None:
            path = _alias_path(agents_dir, prepared)
            path.write_text(json.dumps(_read(path), indent=4), encoding="utf-8")

        made: list[acp_client.AcpClient] = []
        monkeypatch.setattr(
            llm_pool, "AcpClient", _client_factory(workspace, made, after_publication=reformat)
        )
        worker = AcpWorker(acp_backend=ACP_BACKEND_KIRO, sandbox_mode="auto")

        await worker.start()

        assert len(made) == 1 and worker._client is made[0]
        prepared = made[0]._native_skill_projection
        assert prepared is not None
        _assert_mounts_nothing(_read(_alias_path(agents_dir, prepared)))


class TestTheRepairRunsBeforeEverySpawn:
    @pytest.mark.asyncio
    async def test_the_repair_precedes_the_authored_read_and_the_spawn(self, monkeypatch):
        calls: list[str] = []
        client = AsyncMock()
        client.backend = ACP_BACKEND_KIRO
        client.authored_spec_declares_zero_tools = MagicMock(
            side_effect=lambda: calls.append("authored") or True
        )
        client.ensure_ready.side_effect = lambda: calls.append("ready")
        client.effective_spec_declares_zero_tools = MagicMock(
            side_effect=lambda **_kwargs: calls.append("effective") or True
        )
        monkeypatch.setattr(llm_pool, "_repair_agent_spec", lambda: calls.append("repair") or False)
        monkeypatch.setattr(llm_pool, "AcpClient", MagicMock(return_value=client))

        await AcpWorker(acp_backend=ACP_BACKEND_KIRO, sandbox_mode="auto").start()

        assert calls == ["repair", "authored", "ready", "effective"]

    @pytest.mark.asyncio
    async def test_a_mirrored_backend_spawn_is_repaired_too(self, monkeypatch):
        """opencode reads the same spec through its mirror, so it is repaired first too."""
        calls: list[str] = []
        client = AsyncMock()
        client.backend = ACP_BACKEND_OPENCODE
        client.spec_zero_tools = True
        client.ensure_ready.side_effect = lambda: calls.append("ready")
        monkeypatch.setattr(llm_pool, "_repair_agent_spec", lambda: calls.append("repair") or False)
        monkeypatch.setattr(llm_pool, "AcpClient", MagicMock(return_value=client))

        await AcpWorker(acp_backend=ACP_BACKEND_OPENCODE, sandbox_mode="auto").start()

        assert calls == ["repair", "ready"]
