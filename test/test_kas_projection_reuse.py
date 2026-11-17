"""KAS projection: work a new session repeats is reused, and the answer does not move.

Four reuses, each with the projection's output pinned byte-identical:

* the resource-glob matcher's answers are memoized (``batch_guard._read_glob_matches``,
  a pure function of two strings; its purity is pinned in ``test_batch_guard_glob``);
* one projection reads the MCP config scopes once (``mcp_cleanup.mcp_scope_muted_aliases``)
  and hands that reading to every part that asks;
* a shared launch snapshot that already holds the exact bytes is not written again,
  and one that differs, is missing or is not owner-only still is;
* one session start reads each agent spec, skill file and installed app once for its
  own agent and all its sub-agent targets (``build_reuse``).
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import mcp_cleanup
from kiro_crew.acp import batch_guard, kas_env_launch
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.kas_agents import build_kas_custom_agents, to_client_custom_agent
from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.config import paths as paths_mod
from kiro_crew.mcp_gateway import session_servers as session_servers_mod

SESSION = "dashboard_chat-projection-reuse-1"


@pytest.fixture
def home(tmp_path, monkeypatch):
    data_home = tmp_path / "crew-home"
    data_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    return data_home


def _stdio(i: int) -> dict:
    return {
        "command": sys.executable,
        "args": [f"server_{i}.py"],
        "env": {"TOKEN_VALUE": f"v-{i}-zq7Xk"},
    }


def _cp64_spec() -> dict:
    """64 declared stdio servers with an env value the wire withholds, tool globs the
    auto-approval and hook coverage match against every server, and one muted server."""
    servers = {f"s{i}": _stdio(i) for i in range(64)}
    servers["s63"]["disabled"] = True
    return {
        "name": "kirocrew",
        "prompt": "p",
        "tools": ["*"],
        "allowedTools": ["@s0/*", "@s1/read_?", "@s2/**", "@s3/tool_*"],
        "hooks": {"preToolUse": [{"matcher": "@s4/*", "command": "true"}]},
        "mcpServers": servers,
    }


def _shared_writes(monkeypatch) -> list[str]:
    written: list[str] = []
    real = kas_env_launch.atomic_write

    def counting(path, *args, **kwargs):
        written.append(os.path.basename(str(path)))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(kas_env_launch, "atomic_write", counting)
    return written


def _snapshots(home) -> dict[str, tuple[bytes, int]]:
    root = home / "kas-mcp-launch"
    return {
        p.name: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.mark.skipif(
    os.name != "posix", reason="the content check is POSIX-only; Windows always writes"
)
class TestSharedSnapshotWrite:
    def test_a_second_projection_of_the_same_launch_does_not_rewrite_it(self, home, monkeypatch):
        written = _shared_writes(monkeypatch)
        spec = {"name": "a", "prompt": "p", "tools": ["*"], "mcpServers": {"s": _stdio(0)}}
        first = to_client_custom_agent("a", spec, "p", session_key=SESSION)
        before = _snapshots(home)
        second = to_client_custom_agent("a", spec, "p", session_key=SESSION)
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
        assert len(written) == 1, written
        assert _snapshots(home) == before
        assert {mode for _, mode in before.values()} == {0o600}

    def test_a_fifo_at_the_name_is_refused_without_waiting_for_a_writer(self, tmp_path):
        import threading

        fifo = tmp_path / "snapshot.json"
        os.mkfifo(fifo, 0o600)
        answers: list[bool] = []
        probe = threading.Thread(
            target=lambda: answers.append(kas_env_launch._holds_exactly(fifo, b"{}")),
            daemon=True,
        )
        probe.start()
        probe.join(timeout=10)
        if probe.is_alive():
            # Release the blocked open so the thread ends with the test.
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            probe.join(timeout=10)
        assert answers == [False]

    @pytest.mark.parametrize("damage", ["different", "missing", "group-readable"])
    def test_a_snapshot_that_is_not_exactly_right_is_written_again(self, home, monkeypatch, damage):
        written = _shared_writes(monkeypatch)
        spec = {"name": "a", "prompt": "p", "tools": ["*"], "mcpServers": {"s": _stdio(0)}}
        to_client_custom_agent("a", spec, "p", session_key=SESSION)
        ((name, (good, _)),) = _snapshots(home).items()
        path = next((home / "kas-mcp-launch").rglob(name))
        if damage == "different":
            path.write_bytes(good.replace(b"zq7Xk", b"XXXXX"))
        elif damage == "missing":
            path.unlink()
        else:
            path.chmod(0o640)
        to_client_custom_agent("a", spec, "p", session_key=SESSION)
        assert len(written) == 2, written
        assert _snapshots(home) == {name: (good, 0o600)}


class TestOneScopeReadPerProjection:
    @pytest.mark.asyncio
    async def test_a_projection_reads_the_config_scopes_once(self, tmp_path, monkeypatch, home):
        reads: list[int] = []

        def scope_muted():
            reads.append(1)
            return frozenset({"s5"})

        monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
        monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
        monkeypatch.setattr(
            session_servers_mod, "injection_server_names", lambda overlay, agent: frozenset()
        )
        monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: tmp_path)
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
        monkeypatch.setattr(mcp_cleanup, "mcp_scope_muted_aliases", scope_muted)
        (tmp_path / "kirocrew.json").write_text(json.dumps(_cp64_spec()), encoding="utf-8")
        work = tmp_path / "work"
        work.mkdir()
        extras = await harness_for(ACP_BACKEND_KAS).session_extras("kirocrew", work_dir=str(work))
        (entry,) = [a for a in extras.custom_agents if a.get("id") == "kirocrew"]
        assert "s5" not in entry["mcpServers"], "the one reading still mutes"
        assert len(reads) == 1


class TestTheProjectionDoesNotMove:
    def test_a_cp64_shaped_projection_is_byte_identical_with_and_without_reuse(
        self, home, monkeypatch
    ):
        """'Before' is emulated in-process: no memo, a write every time, and each part
        reading the scopes itself. 'After' is the code as it runs: warm memo, files
        already in place, one scope reading handed down. Same bytes."""
        spec = _cp64_spec()
        real_holds = kas_env_launch._holds_exactly
        real_read = batch_guard._read_glob_matches

        monkeypatch.setattr(kas_env_launch, "_holds_exactly", lambda path, data: False)
        monkeypatch.setattr(
            batch_guard, "_read_glob_matches", lambda p, r: batch_guard._glob_match_steps(p, r)[0]
        )
        before = build_kas_custom_agents(home, "kirocrew", spec, session_key=SESSION)

        monkeypatch.setattr(kas_env_launch, "_holds_exactly", real_holds)
        monkeypatch.setattr(batch_guard, "_read_glob_matches", real_read)
        real_read.cache_clear()
        build_kas_custom_agents(home, "kirocrew", spec, session_key=SESSION)  # warms memo and files
        after = build_kas_custom_agents(
            home,
            "kirocrew",
            spec,
            session_key=SESSION,
            scope_muted=mcp_cleanup.mcp_scope_muted_aliases(),
        )
        assert real_read.cache_info().hits > 0, "the memo was exercised"
        assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)
        assert len(before[0]["mcpServers"]) == 63, "the muted server is not declared"


class TestOneStartReadsEachFileOnce:
    """A session start whose agent names several sub-agent targets reads each agent
    spec, skill file and installed app once (``build_reuse``), and every target's
    entry is the one a start without that reuse projects."""

    TARGETS = ("t1", "t2", "t3", "t4")

    @pytest.fixture
    def kiro(self, tmp_path, monkeypatch, home):
        root = tmp_path / "kiro"
        agents = root / "agents"
        agents.mkdir(parents=True)
        monkeypatch.setenv("KIRO_HOME", str(root))
        monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents)
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
        monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
        for skill in ("alpha", "beta"):
            folder = root / "skills" / skill
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(
                f"---\nname: {skill}\ndescription: the {skill} skill\n---\nbody\n",
                encoding="utf-8",
            )
        app = home / "apps" / "some-app"
        app.mkdir(parents=True)
        (app / "installed.json").write_text("{}", encoding="utf-8")
        parent = {
            "name": "lead",
            "prompt": "p",
            "tools": ["fs_read", "use_subagent"],
            "toolsSettings": {"subagent": {"availableAgents": list(self.TARGETS)}},
        }
        specs = [parent, *({"name": t, "prompt": t, "tools": ["fs_read"]} for t in self.TARGETS)]
        for spec in specs:
            (agents / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")
        return root

    async def _start(self) -> str:
        extras = await harness_for(ACP_BACKEND_KAS).session_extras("lead", work_dir=None)
        assert [a["id"] for a in extras.custom_agents] == ["lead", *self.TARGETS]
        return json.dumps(extras.custom_agents, sort_keys=True)

    @pytest.mark.asyncio
    async def test_each_file_is_read_once_and_the_entries_do_not_move(self, kiro, monkeypatch):
        from kiro_crew import build_reuse, hooks
        from kiro_crew.apps import manager_lookup

        spec_reads: list[str] = []
        skill_reads: list[str] = []
        app_reads: list[str] = []
        real_spec = agent_mod._read_spec_capped
        real_skill = hooks.safe_read_prefix
        real_app = manager_lookup._declared_agents

        def spec_spy(path):
            spec_reads.append(path.name)
            return real_spec(path)

        def skill_spy(path, *args, **kwargs):
            skill_reads.append(path)
            return real_skill(path, *args, **kwargs)

        def app_spy(entry):
            app_reads.append(entry.name)
            return real_app(entry)

        monkeypatch.setattr(agent_mod, "_read_spec_capped", spec_spy)
        monkeypatch.setattr(hooks, "safe_read_prefix", skill_spy)
        monkeypatch.setattr(manager_lookup, "_declared_agents", app_spy)

        real_reuse = build_reuse.reuse
        monkeypatch.setattr(build_reuse, "reuse", lambda key, compute, **_: compute())
        before = await self._start()
        assert len(app_reads) > 1 and len(skill_reads) > 2, "without reuse each target reads"
        assert len(spec_reads) > len(set(spec_reads)), "without reuse each target scans"
        targets = [f"{t}.json" for t in self.TARGETS]
        assert all(spec_reads.count(t) > 2 for t in targets), spec_reads
        spec_reads.clear()
        skill_reads.clear()
        app_reads.clear()

        monkeypatch.setattr(build_reuse, "reuse", real_reuse)
        after = await self._start()

        assert after == before
        assert app_reads == ["some-app"]
        assert sorted(skill_reads) == sorted(set(skill_reads)) and len(skill_reads) == 2
        # The name scan reads each spec once for the whole start; each target's own
        # projection then loads its own spec, once, as before.
        assert [spec_reads.count(t) for t in targets] == [2] * len(targets), spec_reads


class TestOneSpawnReadsEachInstalledAppOnce:
    """A KAS process spawned for a custom agent asks which installed app owns the
    agent when it reads the spec, and again for the servers Crew added to it; the
    MCP switch-off then judges the spec the handshake read. One spawn reads each
    installed app once, and what it sends does not move."""

    APPS = ("app-a", "app-b")

    @pytest.fixture
    def spawn(self, tmp_path, monkeypatch, home):
        from kiro_crew.acp import kas_secret_storage
        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp import runtime_spawn_launch
        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.agent_sdk.tool_search import ToolSearchSettings
        from kiro_crew.apps import manager_lookup

        agents = tmp_path / "kiro" / "agents"
        agents.mkdir(parents=True)
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
        monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents)
        monkeypatch.setattr(runtime_mod, "ensure_agent_materialized", lambda agent: None)
        monkeypatch.setattr(kas_secret_storage, "persistence_enabled", lambda: False)
        monkeypatch.setattr(
            agent_mod, "managed_mcp_spec_entry", lambda name, **_: {"command": name}
        )
        monkeypatch.setattr(
            runtime_spawn_launch, "_known_server_tool_names", lambda server: ["memory_recall"]
        )
        for app in self.APPS:
            (home / "apps" / app).mkdir(parents=True)
            (home / "apps" / app / "installed.json").write_text("{}", encoding="utf-8")
        spec = {"name": "mine", "prompt": "p", "tools": ["fs_read", "@own/one"]}
        (agents / "mine.json").write_text(json.dumps(spec), encoding="utf-8")
        app_reads: list[str] = []
        real_app = manager_lookup._declared_agents

        def app_spy(entry):
            app_reads.append(entry.name)
            return real_app(entry)

        monkeypatch.setattr(manager_lookup, "_declared_agents", app_spy)
        work = tmp_path / "work"
        work.mkdir()
        sent: list[tuple[str, dict]] = []

        async def send_notification(method: str, params: dict) -> None:
            sent.append((method, params))

        rt = AcpRuntime(acp_backend=ACP_BACKEND_KAS, work_dir=str(work))
        rt._agent = "mine"
        rt._tool_search = ToolSearchSettings(True, 5, 50_000)
        rt.send_notification = send_notification  # type: ignore[method-assign]

        async def start() -> dict:
            """The two spec judgements of one spawn, in ``_spawn_admitted``'s order."""
            caps = await runtime_spawn_launch._handshake_client_capabilities(rt)
            await runtime_spawn_launch._switch_off_mcp_for_toolless_spawn(rt)
            assert sent == [] and rt._mcp_switched_off is False
            return caps["_meta"]["kiro"]["settings"]["toolSearch"]

        return start, app_reads

    @pytest.mark.asyncio
    async def test_each_app_is_read_once_and_the_handshake_does_not_move(self, spawn, monkeypatch):
        from kiro_crew import build_reuse

        start, app_reads = spawn
        real_reuse = build_reuse.reuse
        monkeypatch.setattr(build_reuse, "reuse", lambda key, compute, **_: compute())
        before = await start()
        assert before["enabled"] is False
        # Crew's added servers stay behind the loader; the author's own grant is direct.
        assert before["neverDefer"] == ["kirocrew-core::mcp_server_wait", "own::one"]
        assert app_reads == [*self.APPS, *self.APPS], "without reuse each ask reads"
        app_reads.clear()

        monkeypatch.setattr(build_reuse, "reuse", real_reuse)
        after = await start()

        assert after == before
        assert app_reads == list(self.APPS)

    @pytest.mark.asyncio
    async def test_the_next_spawn_reads_the_apps_and_the_spec_again(self, spawn, tmp_path):
        """Nothing is kept past one spawn: an app or a spec changed between two
        spawns of one runtime is read fresh by the second."""
        start, app_reads = spawn
        first = await start()
        spec = {"name": "mine", "prompt": "p", "tools": ["fs_read", "@other/two"]}
        (tmp_path / "kiro" / "agents" / "mine.json").write_text(json.dumps(spec), encoding="utf-8")
        second = await start()
        assert app_reads == [*self.APPS, *self.APPS]
        assert first["neverDefer"] == ["kirocrew-core::mcp_server_wait", "own::one"]
        assert second["neverDefer"] == ["kirocrew-core::mcp_server_wait", "other::two"]

    def test_each_spawn_starts_with_no_judged_spec(self):
        """The spec the switch-off reuses is this spawn's handshake's: the spawn
        clears it before the handshake, so one whose handshake reads none (no Tool
        Search settings) never judges an earlier spawn's."""
        import inspect

        from kiro_crew.acp import runtime_spawn_launch

        src = inspect.getsource(runtime_spawn_launch._spawn_admitted)
        cleared = src.find("self._spawn_spec = None")
        assert 0 <= cleared < src.index("await self._handshake_client_capabilities()")
