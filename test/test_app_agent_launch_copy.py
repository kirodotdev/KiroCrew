"""On kiro-cli, a palette-opened app agent runs from a copy Crew writes, by a name Crew owns.

kiro-cli resolves a bare agent name project-first, so a checkout's same-named spec,
written after every check ran, could run in place of the app's own. The turn is
created under the app's launch copy instead (``apps.agent_launch``).
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source
from turn_harness import ScriptedProvider, SlotSpec, TurnScript, run_turn

from kiro_crew.acp import managed_agent_shadow
from kiro_crew.acp.session_handle import AcpRuntimeError
from kiro_crew.acp.skill_projection import project_agent_view
from kiro_crew.agent_discovery import is_internal_agent_spec, list_agents
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.apps import agent_launch, bridges, manager
from kiro_crew.apps.agent_launch import (
    AppAgentLaunchError,
    app_agent_launch_name,
    publish_app_agent_launch,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard.handlers.usage import read_effective_agent

_APP = "pr-bulk-ops"
_SPEC = {
    "name": "deploy",
    "prompt": "the app's prompt",
    "tools": ["fs_read", "@pr-bulk-ops:server"],
    "allowedTools": ["fs_read"],
    "mcpServers": {"pr-bulk-ops:server": {"command": "srv"}},
    "hooks": {"agentSpawn": [{"command": "echo hi"}]},
}


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    directory = tmp_path / "kiro-agents"
    directory.mkdir()
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", directory)
    return directory


def _register(agents_dir, agent="deploy", **extra):
    spec = {**_SPEC, "name": agent, **extra}
    (agents_dir / f"{_APP}--{agent}.json").write_text(json.dumps(spec))
    return spec


def _launch_file(agents_dir, agent="deploy"):
    return agents_dir / f"{app_agent_launch_name(_APP, agent)}.json"


# ── the copy ────────────────────────────────────────────────────────────────


def test_the_copy_is_the_registered_spec_under_the_reserved_name(agents_dir):
    source = _register(agents_dir)
    published = publish_app_agent_launch(_APP, "deploy")
    assert published == app_agent_launch_name(_APP, "deploy")
    assert published.startswith("kirocrew-app-launch-")
    copy = json.loads(_launch_file(agents_dir).read_text())
    assert copy["name"] == published
    assert agent_launch.is_owned_launch_spec(copy)
    # The copy is the registered spec's native skill view, nothing more: every
    # grant, tool, server, hook and prompt the app wrote is still there.
    view, _search, refusal = project_agent_view(
        source,
        agents_dir / f"{_APP}--deploy.json",
        "deploy",
        crew_home_id=data_home().absolute().as_posix(),
        inherits_skills=True,
        per_session_element=True,
    )
    assert refusal is None
    assert {k: v for k, v in copy.items() if k not in ("name", "description")} == {
        k: v for k, v in view.items() if k not in ("name", "description")
    }
    for key in ("prompt", "allowedTools", "hooks"):
        assert copy[key] == source[key]
    assert copy["mcpServers"]["pr-bulk-ops:server"] == source["mcpServers"]["pr-bulk-ops:server"]
    assert set(source["tools"]) <= set(copy["tools"])


def test_an_unchanged_spec_is_not_rewritten_and_a_changed_one_is(agents_dir):
    _register(agents_dir)
    first = publish_app_agent_launch(_APP, "deploy")
    target = _launch_file(agents_dir)
    # Read back after setting it: a filesystem stores mtime at its own resolution.
    os.utime(target, (1_000_000, 1_000_000))
    pinned = target.stat().st_mtime_ns
    assert publish_app_agent_launch(_APP, "deploy") == first
    assert target.stat().st_mtime_ns == pinned
    _register(agents_dir, prompt="a new prompt")
    assert publish_app_agent_launch(_APP, "deploy") == first
    assert json.loads(target.read_text())["prompt"] == "a new prompt"


def test_names_are_fixed_per_app_and_agent():
    assert app_agent_launch_name(_APP, "deploy") == app_agent_launch_name(_APP, "deploy")
    assert app_agent_launch_name(_APP, "deploy") != app_agent_launch_name(_APP, "review")
    assert app_agent_launch_name(_APP, "deploy") != app_agent_launch_name("other-app", "deploy")


def test_the_name_is_a_plain_hash_of_app_and_agent(tmp_path, monkeypatch):
    # No secret: kiro-cli never reads a checkout for the name (launch_process_cwd),
    # so two homes name the same agent the same way.
    names = []
    for home in ("one", "two"):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / home))
        names.append(app_agent_launch_name(_APP, "deploy"))
    assert names[0] == names[1]
    assert names[0].startswith("kirocrew-app-launch-")


class TestTheLaunchDirectory:
    """kiro-cli runs a launch copy from an empty directory no checkout spec can reach.

    kiro-cli finds project agents in its PROCESS directory and runs tools in the ACP
    session's, so starting it here keeps the session in the checkout while no
    ``.kiro/agents`` under the checkout is ever read for the launch name.
    """

    def test_it_is_created_empty_and_owner_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        path = agent_launch.launch_process_cwd()
        assert path == tmp_path / "home" / "app-agent-launch" / "cwd"
        assert list(path.iterdir()) == []
        if os.name != "nt":
            assert (path.stat().st_mode & 0o777) == 0o700
        assert agent_launch.launch_process_cwd() == path

    def test_a_spec_placed_in_it_refuses_the_launch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        path = agent_launch.launch_process_cwd()
        agents = path / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / f"{app_agent_launch_name(_APP, 'deploy')}.json").write_text("{}")
        with pytest.raises(AppAgentLaunchError) as raised:
            agent_launch.launch_process_cwd()
        assert raised.value.code == "unverifiable"

    def test_a_link_at_its_name_is_refused_not_followed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        (tmp_path / "elsewhere").mkdir()
        try:
            (tmp_path / "home" / "app-agent-launch").symlink_to(
                tmp_path / "elsewhere", target_is_directory=True
            )
        except OSError:
            pytest.skip("this host cannot create a directory symlink")
        if os.name != "nt":
            (tmp_path / "elsewhere").chmod(0o755)
        with pytest.raises(AppAgentLaunchError) as raised:
            agent_launch.launch_process_cwd()
        assert raised.value.code == "unverifiable"
        assert list((tmp_path / "elsewhere").iterdir()) == []
        if os.name != "nt":
            assert ((tmp_path / "elsewhere").stat().st_mode & 0o777) == 0o755

    def test_it_is_sealed_precreated_and_write_fenced(self):
        from kiro_crew import sandbox, security

        leaf = agent_launch._LAUNCH_DIR_LEAF
        assert leaf in sandbox._CREW_READONLY_LEAVES
        assert leaf not in sandbox._CREW_HIDDEN_LEAVES
        assert leaf in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
        assert leaf in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
        for prefix in (".kiro/crew", ".kirocrew"):
            assert security.is_sensitive_write_path(
                os.path.join(os.path.expanduser("~"), prefix, leaf, "cwd", "x.json")
            )

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="the namespace plan is POSIX-only")
    @pytest.mark.parametrize("mode", ("standard", "cc", "strict"))
    def test_the_linux_sandbox_seals_it_read_only(self, tmp_path, monkeypatch, mode):
        from kiro_crew import sandbox

        home = tmp_path / "crew-home"
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        home.mkdir()
        plan = sandbox._spawn_plan("namespace", mode)
        target = os.path.realpath(home / agent_launch._LAUNCH_DIR_LEAF)
        assert target in {os.path.realpath(p) for p in plan.readonly}
        assert target not in {os.path.realpath(p) for p in plan.sensitive_dirs}

    @pytest.mark.parametrize("mode", ("standard", "cc", "strict"))
    def test_macos_denies_writes_but_not_reads(self, tmp_path, monkeypatch, mode):
        from kiro_crew import sandbox

        home = tmp_path / "crew-home"
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        home.mkdir()
        from kiro_crew.config.paths import config_dir

        target = os.path.join(str(config_dir()), agent_launch._LAUNCH_DIR_LEAF)
        profile = sandbox._build_seatbelt_profile(mode)
        assert f'(deny file-write* (subpath "{target}"))' in profile
        assert f'(deny file-read* (subpath "{target}"))' not in profile


class TestReportedPathsPointAtTheSession:
    """kiro-cli runs the tool in the session's directory but reports the ``diff`` path
    against its process directory. ``locations`` carries the spelling the tool was
    given, so the diff follows that spelling; with none, a path under the launch
    directory moves to the session's, and anything else is left unanchored, which the
    edit gates refuse."""

    H = os.path.join(os.sep, "u", ".kiro", "crew")
    L = os.path.join(H, "app-agent-launch", "cwd")
    C = os.path.join(os.sep, "w", "a", "b")

    def _map(self, update, roots=None):
        agent_launch.map_process_cwd_paths(update, roots or (self.L,), self.C)
        return update

    def test_a_relative_write_is_judged_in_the_checkout(self):
        # Measured on kiro-cli 2.33.0: ``written.txt`` lands in the checkout while the
        # diff preview names <launch dir>/written.txt.
        update = self._map(
            {
                "content": [
                    {"type": "diff", "path": os.path.join(self.L, "written.txt")},
                    {"type": "content", "content": {"type": "text", "text": self.L}},
                ],
                "locations": [{"path": "written.txt", "line": 1}],
            }
        )
        assert update["content"][0]["path"] == os.path.join(self.C, "written.txt")
        assert update["locations"][0]["path"] == os.path.join(self.C, "written.txt")
        # Only a ``path`` key is a path; text that merely names the directory is left.
        assert update["content"][1]["content"]["text"] == self.L

    def test_a_parent_relative_write_is_judged_at_the_file_it_writes(self):
        # Measured: ``../sib/escape.txt`` lands beside the checkout, while the diff
        # preview names <launch parent>/sib/escape.txt.
        update = self._map(
            {
                "content": [
                    {"type": "diff", "path": os.path.join(self.H, "app-agent-launch", "sib", "x")}
                ],
                "locations": [{"path": "../../.ssh/authorized_keys", "line": 1}],
            }
        )
        target = os.path.join(os.sep, "w", ".ssh", "authorized_keys")
        assert update["content"][0]["path"] == target
        assert update["locations"][0]["path"] == target

    def test_a_home_relative_spelling_is_expanded(self, monkeypatch):
        monkeypatch.setenv("HOME", os.path.join(os.sep, "home", "op"))
        monkeypatch.setenv("USERPROFILE", os.path.join(os.sep, "home", "op"))
        update = self._map(
            {
                "content": [{"type": "diff", "path": os.path.join(self.L, "~", ".ssh", "k")}],
                "locations": [{"path": "~/.ssh/k"}],
            }
        )
        target = os.path.join(os.sep, "home", "op", ".ssh", "k")
        assert update["content"][0]["path"] == target

    def test_a_launch_root_reentry_is_judged_where_it_lands(self):
        # ``../cwd/x`` normalises back under the launch directory, yet the tool wrote
        # beside the checkout: the spelling wins over the directory mapping.
        update = self._map(
            {
                "content": [{"type": "diff", "path": os.path.join(self.L, "x")}],
                "locations": [{"path": "../cwd/x"}],
            }
        )
        assert update["content"][0]["path"] == os.path.join(os.sep, "w", "a", "cwd", "x")

    def test_a_variable_spelling_is_expanded(self, monkeypatch):
        monkeypatch.setenv("KC_PROBE_DIR", os.path.join(os.sep, "srv", "x"))
        update = self._map(
            {
                "content": [{"type": "diff", "path": os.path.join(self.L, "f")}],
                "locations": [{"path": "$KC_PROBE_DIR/f"}],
            }
        )
        assert update["content"][0]["path"] == os.path.join(os.sep, "srv", "x", "f")

    def test_several_diff_blocks_are_all_left_unanchored(self):
        # The edit gates judge only the first diff block, so none may look anchored.
        update = self._map(
            {
                "content": [
                    {"type": "diff", "path": os.path.join(self.L, "ok.txt")},
                    {"type": "diff", "path": os.path.join(self.H, "config.json")},
                ]
            }
        )
        assert not any(os.path.isabs(item["path"]) for item in update["content"])

    def test_an_absolute_argument_is_judged_as_given(self):
        target = os.path.join(os.sep, "etc", "hosts")
        update = self._map(
            {"content": [{"type": "diff", "path": target}], "locations": [{"path": target}]}
        )
        assert update["content"][0]["path"] == target
        assert update["locations"][0]["path"] == target

    def test_several_spellings_leave_the_diff_unanchored(self):
        update = self._map(
            {
                "content": [{"type": "diff", "path": os.path.join(self.L, "a")}],
                "locations": [{"path": "notes.txt"}, {"path": "/u/.ssh/authorized_keys"}],
            }
        )
        assert not os.path.isabs(update["content"][0]["path"])

    def test_without_locations_a_path_in_the_launch_directory_moves(self):
        update = self._map({"content": [{"type": "diff", "path": os.path.join(self.L, "f")}]})
        assert update["content"][0]["path"] == os.path.join(self.C, "f")

    def test_without_locations_a_climb_out_is_left_unanchored(self):
        for path in (os.path.join(self.H, ".ssh", "k"), os.path.join(os.sep, "etc", "x")):
            update = self._map({"content": [{"type": "diff", "path": path}]})
            assert not os.path.isabs(update["content"][0]["path"])

    def test_a_sibling_with_the_same_prefix_is_not_the_launch_directory(self):
        sibling = os.path.join(os.path.dirname(self.L), "cwd2", "f")
        update = self._map({"content": [{"type": "diff", "path": sibling}]})
        assert not os.path.isabs(update["content"][0]["path"])

    def test_the_per_frame_mapping_touches_no_filesystem(self, monkeypatch):
        # The reader loop maps every frame, so the spellings are resolved once at spawn.
        monkeypatch.setattr(
            agent_launch.os.path, "realpath", lambda *a, **k: pytest.fail("syscall per frame")
        )
        update = self._map({"content": [{"type": "diff", "path": os.path.join(self.L, "f")}]})
        assert update["content"][0]["path"] == os.path.join(self.C, "f")

    def test_the_resolved_spelling_of_a_linked_directory_maps_too(self, tmp_path):
        (tmp_path / "real" / "cwd").mkdir(parents=True)
        try:
            (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
        except OSError:
            pytest.skip("this host cannot create a directory symlink")
        linked = str(tmp_path / "linked" / "cwd")
        update = {
            "content": [{"type": "diff", "path": os.path.join(os.path.realpath(linked), "f")}]
        }
        agent_launch.map_process_cwd_paths(
            update, agent_launch.launch_cwd_roots(linked), str(tmp_path / "checkout")
        )
        assert update["content"][0]["path"] == os.path.join(str(tmp_path / "checkout"), "f")

    def test_the_runtime_maps_a_frame_for_its_session(self, tmp_path):
        from kiro_crew.acp.runtime import AcpRuntime

        runtime = AcpRuntime(work_dir=str(tmp_path / "work"), launch_agent="kirocrew-app-launch-x")
        launch_dir = str(tmp_path / "launch")
        runtime._launch_process_cwd = launch_dir
        runtime._launch_cwd_roots = (launch_dir,)
        runtime._session_cwds["s1"] = str(tmp_path / "checkout")
        diff = {"type": "diff", "path": os.path.join(launch_dir, "w.txt")}
        frame = {"params": {"sessionId": "s1", "update": {"content": [diff]}}}
        runtime._map_launch_cwd_paths(frame)
        assert diff["path"] == os.path.join(str(tmp_path / "checkout"), "w.txt")
        # An unknown session falls back to the runtime's own workspace.
        other = {"params": {"sessionId": "s2", "update": {"locations": [{"path": "x"}]}}}
        runtime._map_launch_cwd_paths(other)
        assert other["params"]["update"]["locations"][0]["path"] == str(tmp_path / "work" / "x")
        # A permission request carries the tool call under ``toolCall``.
        asked = {"params": {"sessionId": "s1", "toolCall": {"locations": [{"path": "y"}]}}}
        runtime._map_launch_cwd_paths(asked)
        target = str(tmp_path / "checkout" / "y")
        assert asked["params"]["toolCall"]["locations"][0]["path"] == target


class TestTheProcessStartsInTheLaunchDirectory:
    """kiro-cli for a launch copy starts in the sealed launch directory.

    Behind Crew's Linux launcher the process is created in its workspace and the
    launcher enters the directory itself, after its seals: a process created inside it
    would keep the writable mount underneath the seal (``/proc/<pid>/cwd``). Without
    that launcher it is created in the directory directly.
    """

    @staticmethod
    def _host(tmp_path, *, launch: bool, wrapped: bool):
        return SimpleNamespace(
            _launch_process_cwd=str(tmp_path / "launch") if launch else "",
            _spawn_work_dir=str(tmp_path / "work"),
            _bound_workspace_fd=7,
            _work_dir=tmp_path / "work",
            _sandbox_wrapped_by_crew=wrapped,
        )

    def test_crews_linux_launcher_enters_the_directory_itself(self, tmp_path, monkeypatch):
        from kiro_crew.acp import launch as launch_mod

        monkeypatch.setattr(launch_mod.sys, "platform", "linux")
        host = self._host(tmp_path, launch=True, wrapped=True)
        assert launch_mod.process_directory(host) == (str(tmp_path / "work"), 7)
        assert launch_mod.exec_cwd_options(host) == {"exec_cwd": str(tmp_path / "launch")}

    def test_without_crews_launcher_the_process_is_created_in_it(self, tmp_path, monkeypatch):
        from kiro_crew.acp import launch as launch_mod

        for platform, wrapped in (("linux", False), ("darwin", True), ("win32", False)):
            monkeypatch.setattr(launch_mod.sys, "platform", platform)
            host = self._host(tmp_path, launch=True, wrapped=wrapped)
            assert launch_mod.process_directory(host) == (str(tmp_path / "launch"), None)

    def test_every_other_process_starts_in_its_workspace(self, tmp_path):
        from kiro_crew.acp import launch as launch_mod

        host = self._host(tmp_path, launch=False, wrapped=True)
        assert launch_mod.process_directory(host) == (str(tmp_path / "work"), 7)
        assert launch_mod.exec_cwd_options(host) == {}

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="the namespace plan is POSIX-only")
    def test_the_plan_carries_the_directory_to_the_launcher(self, tmp_path, monkeypatch):
        from kiro_crew import sandbox, sandbox_plan

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-home"))
        (tmp_path / "crew-home").mkdir()
        target = str(tmp_path / "launch")
        plan = sandbox._spawn_plan("namespace", "standard", exec_cwd=target)
        assert sandbox_plan.namespace_payload(plan)["exec_cwd"] == target
        bare = sandbox._spawn_plan("namespace", "standard")
        assert sandbox_plan.namespace_payload(bare)["exec_cwd"] == ""

    @pytest.mark.skipif(not hasattr(os, "statvfs"), reason="POSIX launcher")
    def test_the_launcher_enters_only_a_sealed_empty_directory(self, tmp_path, monkeypatch):
        from kiro_crew import sandbox_launcher_program as program

        target = tmp_path / "launch"
        target.mkdir()
        monkeypatch.chdir(tmp_path)
        launch = SimpleNamespace(exec_cwd=str(target))
        real_statvfs = os.statvfs

        def sealed(path, *, read_only):
            result = real_statvfs(path)
            flags = (result.f_flag | os.ST_RDONLY) if read_only else (result.f_flag & ~os.ST_RDONLY)
            return SimpleNamespace(f_flag=flags)

        # Writable: the seal did not take, so nothing is exec'd from here.
        monkeypatch.setattr(program.os, "statvfs", lambda p: sealed(p, read_only=False))
        with pytest.raises(SystemExit, match="not sealed read-only"):
            program.enter_exec_cwd(launch)
        # Sealed and empty: the launcher now stands in it, through the seal.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(program.os, "statvfs", lambda p: sealed(p, read_only=True))
        program.enter_exec_cwd(launch)
        assert os.path.realpath(os.getcwd()) == os.path.realpath(target)
        # Something planted in it: refused.
        monkeypatch.chdir(tmp_path)
        (target / ".kiro").mkdir()
        with pytest.raises(SystemExit, match="not empty"):
            program.enter_exec_cwd(launch)
        # No directory named: the launcher stays where it was created.
        monkeypatch.chdir(tmp_path)
        program.enter_exec_cwd(SimpleNamespace(exec_cwd=""))
        assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path)


class TestCrewJudgesTheCopyItWrote:
    """Crew's own session-start reads take the copy by its path, not by a name lookup
    that a checkout spec of the copy's name could answer."""

    def test_the_copy_is_read_by_path_and_must_be_ours(self, agents_dir):
        _register(agents_dir)
        name = publish_app_agent_launch(_APP, "deploy")
        assert agent_launch.read_launch_copy(name)["name"] == name
        (agents_dir / f"{name}.json").write_text(json.dumps({"name": name}))
        with pytest.raises(AppAgentLaunchError) as raised:
            agent_launch.read_launch_copy(name)
        assert raised.value.code == "unverifiable"
        with pytest.raises(AppAgentLaunchError):
            agent_launch.read_launch_copy("kirocrew-app-launch-absent")

    def test_the_event_loop_lookups_never_wait_on_a_publication(self):
        # A publication holds the file lock across its reads and writes; the two
        # lookups the event loop makes must not take it.
        import threading

        answers = []
        with agent_launch._LAUNCH_FILE_LOCK:
            reader = threading.Thread(
                target=lambda: answers.append(
                    (
                        agent_launch.is_search_launch_copy("kirocrew-app-launch-x"),
                        agent_launch.launch_source_stem("kirocrew-app-launch-x"),
                    )
                )
            )
            reader.start()
            reader.join(timeout=5)
        assert answers == [(False, None)]

    def test_pooled_stubs_come_from_the_registered_file(self, agents_dir, tmp_path):
        from kiro_crew.acp.runtime import AcpRuntime

        _register(agents_dir)
        name = publish_app_agent_launch(_APP, "deploy")
        runtime = AcpRuntime(agent="deploy", work_dir=str(tmp_path), launch_agent=name)
        runtime._launch_copy_spec = {"name": name}
        assert runtime._pool_lookup("deploy") == (f"{_APP}--deploy", {"name": name})
        assert runtime._pool_lookup("other") == ("other", None)
        unpublished = AcpRuntime(
            agent="deploy", work_dir=str(tmp_path), launch_agent="kirocrew-app-launch-none"
        )
        assert unpublished._pool_lookup("deploy") == (None, None)
        plain = AcpRuntime(agent="deploy", work_dir=str(tmp_path))
        assert plain._pool_lookup("deploy") == ("deploy", None)

    def test_a_launch_copy_snapshot_is_never_read_by_name(self, tmp_path, monkeypatch):
        from kiro_crew.acp import runtime as setup

        seen = {}
        monkeypatch.setattr(
            setup,
            "pooled_session_servers",
            lambda overlay, agent, **scope: seen.update(agent=agent, **scope) or [],
        )
        monkeypatch.setattr(setup, "_ref_spec_snapshot", lambda *a: pytest.fail("read by name"))
        copy = {"name": "kirocrew-app-launch-x"}
        servers, ref = setup._pooled_session_servers_and_ref_spec(
            tmp_path, f"{_APP}--deploy", ACP_BACKEND_KIRO, tmp_path / "checkout", copy
        )
        assert (servers, ref) == ([], copy)
        assert seen["agent"] == f"{_APP}--deploy"
        assert seen.get("work_dir") is None


def test_an_unregistered_agent_is_refused(agents_dir):
    with pytest.raises(AppAgentLaunchError) as raised:
        publish_app_agent_launch(_APP, "deploy")
    assert raised.value.code == "not_installed"
    assert not _launch_file(agents_dir).exists()


def test_a_registered_file_declaring_another_name_is_refused(agents_dir):
    (agents_dir / f"{_APP}--deploy.json").write_text(json.dumps({**_SPEC, "name": "other"}))
    with pytest.raises(AppAgentLaunchError) as raised:
        publish_app_agent_launch(_APP, "deploy")
    assert raised.value.code == "not_installed"


def test_a_foreign_file_at_the_copy_path_is_left_alone(agents_dir):
    _register(agents_dir)
    target = _launch_file(agents_dir)
    target.write_text(json.dumps({"name": target.stem, "prompt": "someone else's"}))
    with pytest.raises(AppAgentLaunchError) as raised:
        publish_app_agent_launch(_APP, "deploy")
    assert raised.value.code == "foreign"
    assert json.loads(target.read_text())["prompt"] == "someone else's"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_link_at_the_copy_path_is_not_written_through(agents_dir, tmp_path):
    _register(agents_dir)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}")
    _launch_file(agents_dir).symlink_to(elsewhere)
    with pytest.raises(AppAgentLaunchError) as raised:
        publish_app_agent_launch(_APP, "deploy")
    assert raised.value.code == "foreign"
    assert elsewhere.read_text() == "{}"


def test_a_second_user_level_spec_declaring_the_reserved_name_is_refused(agents_dir):
    _register(agents_dir)
    (agents_dir / "squatter.json").write_text(
        json.dumps({"name": app_agent_launch_name(_APP, "deploy")})
    )
    with pytest.raises(AppAgentLaunchError) as raised:
        publish_app_agent_launch(_APP, "deploy")
    assert raised.value.code == "shadowed"


# ── the reserved name ───────────────────────────────────────────────────────


@pytest.mark.parametrize("by", ["stem", "declared"])
def test_a_checkout_spec_of_a_launch_name_does_not_refuse_the_kiro_spawn(tmp_path, by):
    # kiro-cli runs the copy from the empty launch directory, so a checkout spec of the
    # copy's name is never read for it: no reservation guards a lookup that cannot happen.
    name = app_agent_launch_name(_APP, "deploy")
    project = tmp_path / "checkout"
    (project / ".kiro" / "agents").mkdir(parents=True)
    filename = f"{name}.json" if by == "stem" else "innocent.json"
    (project / ".kiro" / "agents" / filename).write_text(json.dumps({"name": name}))
    assert managed_agent_shadow.managed_agent_shadow_refusal("kirocrew", project) is None


def test_a_launch_name_is_refused_as_an_app_agent_name(agents_dir):
    name = app_agent_launch_name(_APP, "deploy")
    refusal = manager.app_command_agent_refusal(_APP, name)
    assert refusal is not None and "reserves" in refusal


def test_no_roster_offers_a_launch_copy(agents_dir, monkeypatch):
    _register(agents_dir)
    publish_app_agent_launch(_APP, "deploy")
    monkeypatch.setattr("kiro_crew.agent_discovery.kiro_agents_dir", lambda: agents_dir)
    names = {a.name for a in list_agents(agents_dir=agents_dir)}
    assert "deploy" in names
    assert not any(n.startswith("kirocrew-app-launch-") for n in names)

    class _Row:
        name = app_agent_launch_name(_APP, "deploy")
        filename = f"{name}.json"
        description = ""

    assert is_internal_agent_spec(_Row())


def test_a_launch_copy_runtime_keeps_the_agent_name_governance_binds(tmp_path, monkeypatch):
    # The copy's name is wire-only: kiro-cli is asked for it on --agent and in
    # set_mode, and every other reader -- governance, audit, hooks, usage --
    # sees the agent's own name, so a task profile bound to it still applies.
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.platform import governance_profiles as gp

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "deploy-task.json").write_text(
        json.dumps(
            {
                "name": "deploy-task",
                "bind": {"type": "task", "id": "deploy"},
                "tools": {"mode": "allow", "allow": ["read"]},
            }
        )
    )
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    try:
        launch = app_agent_launch_name(_APP, "deploy")
        runtime = AcpRuntime(work_dir=tmp_path, agent="deploy", launch_agent=launch)
        assert runtime._agent == "deploy"
        assert runtime._wire_mode_id("deploy") == launch
        assert runtime._wire_mode_id("other") == "other"
        assert runtime.crew_agent_name(launch) == "deploy"
        assert runtime.crew_agent_name("other") == "other"
        prof = gp.resolve_active_scope("dashboard:slot1", agent=runtime._agent)
        assert prof is not None and prof.name == "deploy-task"

        class _Provider:
            _agent = runtime._agent
            app_launch_agent = launch

        assert read_effective_agent(_Provider()) == "deploy"
    finally:
        gp.reset_store()


@pytest.mark.asyncio
@pytest.mark.parametrize("launch", ["kirocrew-app-launch-x", ""], ids=["copy", "plain"])
async def test_the_session_control_plane_is_read_from_the_spec_kiro_cli_runs(
    tmp_path, monkeypatch, launch
):
    # The per-session control-plane elements replace the spec's own declarations,
    # so they are derived from the copy kiro-cli runs, never from a view of the
    # bare name (which a checkout's same-named spec could have fed).
    from types import SimpleNamespace

    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.runtime import AcpRuntime

    seen = []

    def fake_mount(agent, *, work_dir, existing_names, spec_override=None):
        seen.append((agent, spec_override))
        return SimpleNamespace(elements=[], withheld={})

    monkeypatch.setattr(session_mcp, "kiro_control_plane_servers", fake_mount)
    runtime = AcpRuntime(work_dir=tmp_path, agent="deploy", launch_agent=launch)
    bare_view = {"name": "deploy", "prompt": "a checkout's view"}
    runtime._native_skill_projection = SimpleNamespace(
        specs={"deploy": bare_view}, search_agents=set()
    )
    copy = {"name": launch, "prompt": "the copy kiro-cli runs"}
    if launch:
        # Read by its path at spawn; never a name lookup, with or without a projection.
        with pytest.raises(AcpRuntimeError):
            await runtime._unpooled_control_planes([], "deploy", tmp_path)
        runtime._launch_copy_spec = copy
        runtime._native_skill_projection = None
        await runtime._unpooled_control_planes([], "deploy", tmp_path)
        assert seen == [(launch, copy)]
    else:
        await runtime._unpooled_control_planes([], "deploy", tmp_path)
        assert seen == [("deploy", bare_view)]


@pytest.mark.asyncio
async def test_a_search_copy_without_its_core_element_refuses_without_a_projection(
    tmp_path, monkeypatch
):
    # The copy's skills come only through kirocrew-core's skill_search, so a session
    # whose mount withheld that element is refused even when no projection runs.
    from types import SimpleNamespace

    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.runtime import AcpRuntime

    launch = "kirocrew-app-launch-x"
    monkeypatch.setattr(
        session_mcp,
        "kiro_control_plane_servers",
        lambda agent, *, work_dir, existing_names, spec_override=None: SimpleNamespace(
            elements=[], withheld={}
        ),
    )
    monkeypatch.setattr(agent_launch, "_SEARCH_COPIES", {launch})
    runtime = AcpRuntime(work_dir=tmp_path, agent="deploy", launch_agent=launch)
    runtime._launch_copy_spec = {"name": launch}
    runtime._native_skill_projection = None
    with pytest.raises(AcpRuntimeError):
        await runtime._unpooled_control_planes([], "deploy", tmp_path)
    monkeypatch.setattr(agent_launch, "_SEARCH_COPIES", set())
    assert await runtime._unpooled_control_planes([], "deploy", tmp_path) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("launch", ["kirocrew-app-launch-x", ""], ids=["copy", "plain"])
async def test_reselecting_the_agent_mid_session_names_the_copy(tmp_path, launch):
    # A mid-session set_mode for the runtime's own agent goes out as the copy,
    # past the projection that would map the bare name to a view built by name.
    from types import SimpleNamespace

    from kiro_crew.acp.runtime import AcpRuntime

    runtime = AcpRuntime(work_dir=tmp_path, agent="deploy", launch_agent=launch)
    runtime._process = SimpleNamespace(stdin=object())
    runtime._native_skill_projection = SimpleNamespace(
        request=lambda method, params: {**params, "modeId": "kirocrew-skill-view-view"}
    )
    sent = []

    async def capture(data, request_id, method):
        sent.append(json.loads(data))

    runtime._write_request_bounded = capture
    await runtime.send_request("session/set_mode", {"sessionId": "s1", "modeId": "deploy"})
    expected = launch or "kirocrew-skill-view-view"
    assert sent[0]["params"]["modeId"] == expected


@pytest.mark.parametrize("launch", ["kirocrew-app-launch-x", ""], ids=["copy", "plain"])
def test_a_rebuilt_session_is_asked_for_the_same_copy(launch):
    # A reset successor and a compaction restart rebuild the session from
    # allocation_identity; it carries the copy, so the rebuild never falls back
    # to the bare name kiro-cli would resolve project-first.
    from types import SimpleNamespace

    from kiro_crew.session_lifecycle_parts.parked_queue import allocation_identity

    owner = SimpleNamespace(get_channel=lambda key: None)
    session = SimpleNamespace(
        agent="deploy",
        approval_policy="",
        provider=SimpleNamespace(cwd=None),
        requested_model="",
        capability_member="",
        app_launch_agent=launch,
    )
    identity = allocation_identity(owner, "dashboard:slot1", session)
    assert identity["agent"] == "deploy"
    if launch:
        assert identity["app_launch_agent"] == launch
    else:
        assert "app_launch_agent" not in identity


def test_only_a_kiro_provider_hands_kiro_cli_the_copy():
    from kiro_crew.providers.acp_runtime_start import _launch_agent

    class _Provider:
        app_launch_agent = "kirocrew-app-launch-x"
        is_kiro_backend = True

    assert _launch_agent(_Provider()) == "kirocrew-app-launch-x"
    _Provider.is_kiro_backend = False
    assert _launch_agent(_Provider()) == ""


def test_a_launch_name_is_a_valid_agent_name():
    from kiro_crew.validation import is_registered_agent_name

    assert is_registered_agent_name(app_agent_launch_name(_APP, "deploy"))


def test_an_agent_dropped_from_the_manifest_loses_its_copy(tmp_path, app_env):
    from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
    from kiro_crew.apps.manifest import AppManifest

    install_app(_make_app_source(tmp_path))
    app_root = app_env["home"] / "apps" / "test-app"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    copies = {}
    for agent in ("my-agent", "dropped"):
        path = app_env["kiro_agents"] / f"{app_agent_launch_name('test-app', agent)}.json"
        path.write_text(json.dumps({"name": path.stem, "description": agent_launch.OWNER_MARKER}))
        copies[agent] = path
    bridges._prune_stale_app_resources("test-app", manifest, app_root)
    assert copies["my-agent"].exists()
    assert not copies["dropped"].exists()


def test_deregistering_the_app_removes_only_its_own_copies(agents_dir):
    _register(agents_dir)
    publish_app_agent_launch(_APP, "deploy")
    other = agents_dir / f"{app_agent_launch_name('other-app', 'deploy')}.json"
    other.write_text(json.dumps({"name": other.stem, "description": agent_launch.OWNER_MARKER}))
    bridges._deregister_agents(_APP)
    assert not _launch_file(agents_dir).exists()
    assert not (agents_dir / f"{_APP}--deploy.json").exists()
    assert other.exists()


# ── the turn ────────────────────────────────────────────────────────────────


class _KiroProvider(ScriptedProvider):
    """kiro-cli capabilities; kiro-cli asked for what the allocation stamped (the copy)."""

    capabilities = capabilities_for(ACP_BACKEND_KIRO)

    @property
    def _agent(self) -> str:
        return str(self._ctx.slot.agent)

    @property
    def app_launch_agent(self) -> str:
        owner_app, _, owner_agent = (self._ctx.slot.app_agent_owner or "").partition("/")
        return app_agent_launch_name(owner_app, owner_agent)


class _BareKiroProvider(ScriptedProvider):
    """kiro-cli capabilities, but started with no launch copy (the bare name)."""

    capabilities = capabilities_for(ACP_BACKEND_KIRO)

    @property
    def _agent(self) -> str:
        return str(self._ctx.slot.agent)


class _KasProvider(ScriptedProvider):
    capabilities = capabilities_for(ACP_BACKEND_KAS)


def _bind(ctx):
    agent = KiroCrewConfig.load().default_agent
    ctx.slot.agent = agent
    ctx.slot.app_agent_owner = f"{_APP}/{agent}"


def _bind_in(project):
    """``_bind``, with the session working in the checkout at *project*."""

    def bind(ctx):
        _bind(ctx)
        ctx.slot.project = str(project)

    return bind


def _streamed(record):
    return [call for call in record.provider_calls if call.name.startswith("stream")]


def _allocated_agents(record):
    return [call.kwargs.get("agent") for call in record.allocations]


def _allocated_launches(record):
    return [call.kwargs.get("app_launch_agent") for call in record.allocations]


@pytest.mark.asyncio
async def test_a_project_spec_written_between_check_and_spawn_is_not_the_one_launched(
    agents_dir, tmp_path, monkeypatch
):
    # The bare-name check passes, and only then does the checkout gain a spec
    # declaring the agent's bare name -- the check-to-spawn window. The session is
    # still created under the copy's name, whose file holds the app's own spec, and
    # the checkout's spec does not claim that name.
    agent = KiroCrewConfig.load().default_agent
    _register(agents_dir, agent)
    project = tmp_path / "checkout"
    (project / ".kiro" / "agents").mkdir(parents=True)
    real_check = manager.app_command_agent_refusal

    def check_then_shadow(owner, name):
        verdict = real_check(owner, name)
        (project / ".kiro" / "agents" / f"{name}.json").write_text(
            json.dumps({"name": name, "prompt": "the checkout's prompt", "allowedTools": ["*"]})
        )
        return verdict

    monkeypatch.setattr(manager, "app_command_agent_refusal", check_then_shadow)
    record = await run_turn(
        TurnScript(message="deploy it", setup=_bind, provider=_KiroProvider),
        slot=SlotSpec(),
        config={"agent": {"acp_backend": "kiro"}},
    )
    # The session keeps the agent's own name; kiro-cli is asked for the copy.
    assert _allocated_agents(record) == [KiroCrewConfig.load().agents[agent].kiro_agent]
    [launch] = _allocated_launches(record)
    assert launch.startswith("kirocrew-app-launch-")
    assert _streamed(record)
    launched = json.loads((agents_dir / f"{launch}.json").read_text())
    assert launched["prompt"] == "the app's prompt"
    assert launched["allowedTools"] == ["fs_read"]
    assert not list((project / ".kiro" / "agents").glob(f"{launch}*"))


@pytest.mark.asyncio
async def test_a_checkout_spec_of_a_launch_name_does_not_refuse_the_turn(
    agents_dir, tmp_path, monkeypatch
):
    # No per-turn check of the copy's name against the checkout: kiro-cli never reads
    # a checkout spec for it, so a turn in such a checkout runs as the copy.
    agent = KiroCrewConfig.load().default_agent
    _register(agents_dir, agent)
    project = tmp_path / "checkout"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "agents" / "kirocrew-app-launch-squat.json").write_text(
        json.dumps({"name": "kirocrew-app-launch-squat", "allowedTools": ["*"]})
    )
    record = await run_turn(
        TurnScript(message="deploy it", setup=_bind_in(project), provider=_KiroProvider),
        slot=SlotSpec(),
        config={"agent": {"acp_backend": "kiro"}},
    )
    assert len(_allocated_launches(record)) == 1
    assert _streamed(record)
    assert not any("app_agent_refused" in str(row) for row in record.window)


@pytest.mark.asyncio
async def test_on_kas_the_turn_keeps_the_bare_name(agents_dir, monkeypatch):
    # KAS takes no agent name: Crew reads the spec it sends, so nothing changes there.
    agent = KiroCrewConfig.load().default_agent
    _register(agents_dir, agent)
    monkeypatch.setattr(manager, "app_command_agent_refusal", lambda *a: None)
    record = await run_turn(
        TurnScript(message="deploy it", setup=_bind, provider=_KasProvider),
        slot=SlotSpec(),
        config={"agent": {"acp_backend": "kas"}},
    )
    assert _allocated_launches(record) == [None]
    assert _streamed(record)
    assert not list(agents_dir.glob("kirocrew-app-launch-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_an_admitted_turn_binds_its_session_before_the_claim(
    agents_dir, monkeypatch, refused
):
    # The KAS projection reads the registered file by path for a bound session
    # (acp.harness.kas), so the binding exists by the time the session is allocated.
    from kiro_crew.dashboard.chat_runner import effective_session_key

    bindings: dict = {}
    monkeypatch.setattr(agent_launch, "_KAS_BINDINGS", bindings)
    agent = KiroCrewConfig.load().default_agent
    if not refused:
        _register(agents_dir, agent)
    seen: list = []

    def bind(ctx):
        _bind(ctx)
        seen.append(effective_session_key(ctx.slot))

    record = await run_turn(
        TurnScript(message="deploy it", setup=bind, provider=_KasProvider),
        slot=SlotSpec(),
        config={"agent": {"acp_backend": "kas"}},
    )
    if refused:
        assert not bindings and not record.allocations
    else:
        assert bindings == {seen[0]: (_APP, agent)}
        assert _streamed(record)


@pytest.mark.asyncio
async def test_a_turn_not_bound_to_the_app_drops_the_keys_binding(agents_dir, monkeypatch):
    # A slot recreated under a deleted app slot's name (or switched to another
    # agent) runs its own agent: the old binding must not decide its projection.
    from kiro_crew.dashboard.chat_runner import effective_session_key

    bindings: dict = {"another-session": (_APP, "deploy")}
    monkeypatch.setattr(agent_launch, "_KAS_BINDINGS", bindings)
    seen: list = []

    def stale_binding(ctx):
        key = effective_session_key(ctx.slot)
        seen.append(key)
        bindings[key] = (_APP, str(ctx.slot.agent))

    record = await run_turn(
        TurnScript(message="hello", setup=stale_binding, provider=_KasProvider),
        slot=SlotSpec(),
        config={"agent": {"acp_backend": "kas"}},
    )
    assert _streamed(record)
    assert seen and seen[0] not in bindings
    assert bindings == {"another-session": (_APP, "deploy")}


@pytest.mark.asyncio
async def test_a_turn_whose_copy_cannot_be_written_is_refused(agents_dir, monkeypatch):
    # Nothing registered: no copy, so the turn is refused before any session.
    monkeypatch.setattr(manager, "app_command_agent_refusal", lambda *a: None)
    record = await run_turn(
        TurnScript(message="deploy it", setup=_bind, provider=_KiroProvider),
        slot=SlotSpec(),
        config={"agent": {"acp_backend": "kiro"}},
    )
    assert not record.allocations
    assert not _streamed(record)
    assert any("app_agent_refused" in str(row) for row in record.window)


@pytest.mark.asyncio
async def test_a_claimed_kiro_session_started_as_the_bare_name_is_refused_and_reset(
    agents_dir, monkeypatch
):
    agent = KiroCrewConfig.load().default_agent
    _register(agents_dir, agent)
    monkeypatch.setattr(manager, "app_command_agent_refusal", lambda *a: None)
    record = await run_turn(
        TurnScript(message="deploy it", setup=_bind, provider=_BareKiroProvider),
        slot=SlotSpec(),
        config={"agent": {"acp_backend": "kiro"}},
    )
    assert record.allocations
    assert not _streamed(record)
    assert any("app_agent_refused" in str(row) for row in record.window)
    assert [call for call in record.session_calls if call.name == "reset"]


@pytest.mark.asyncio
async def test_an_unbound_kiro_turn_keeps_its_own_agent(agents_dir):
    record = await run_turn(
        TurnScript(message="hi"),
        slot=SlotSpec(),
        config={"agent": {"acp_backend": "kiro"}},
    )
    assert record.allocations and _allocated_launches(record) == [None]
    assert not list(agents_dir.glob("kirocrew-app-launch-*"))


# ── the copy's skill view ───────────────────────────────────────────────────


@pytest.fixture
def clean_pins(monkeypatch):
    from kiro_crew import skill_scope_pin

    pins: dict = {}
    monkeypatch.setattr(skill_scope_pin, "_PINNED", pins)
    return pins


def test_the_copy_is_a_skill_view_with_no_skill_resources(agents_dir):
    # Like every Kiro launch: kiro-cli loads no skill:// resource natively, and the
    # agent reads its skills through skill_search instead.
    source = _register(agents_dir, resources=["skill://skills/**/SKILL.md", "file://README.md"])
    publish_app_agent_launch(_APP, "deploy")
    copy = json.loads(_launch_file(agents_dir).read_text())
    assert copy["resources"] == ["file://README.md"]
    assert "kirocrew-core" in copy["mcpServers"]
    assert "@kirocrew-core/skill_search" in copy["tools"]
    assert copy["prompt"] == source["prompt"]
    assert copy["allowedTools"] == source["allowedTools"]


def test_a_spec_that_forbids_skill_search_refuses_the_turn(agents_dir):
    # The same refusal a projected Kiro spawn makes for this spec.
    _register(
        agents_dir,
        resources=["skill://skills/**/SKILL.md"],
        mcpServers={"kirocrew-core": {"command": "x", "disabledTools": ["skill_search"]}},
    )
    with pytest.raises(AppAgentLaunchError) as raised:
        publish_app_agent_launch(_APP, "deploy")
    assert raised.value.code == "no_skill_view"
    assert not _launch_file(agents_dir).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend, provider", [("kiro", _KiroProvider), ("kas", _KasProvider)], ids=["kiro", "kas"]
)
async def test_a_checkout_spec_of_the_bare_name_neither_refuses_nor_scopes_the_turn(
    agents_dir, tmp_path, clean_pins, backend, provider
):
    # The checkout declares the agent's bare name with its own grants and skills
    # before the turn starts. The turn is not refused, and on each allowed backend
    # the session's skill scope comes from the app's registered file, for the
    # skill_search route, dollar skills and the injected directory alike.
    from kiro_crew import context
    from kiro_crew.agent_discovery import session_skill_globs
    from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP

    agent = KiroCrewConfig.load().default_agent
    _register(agents_dir, agent, resources=["skill://app-skills/**/SKILL.md"])
    project = tmp_path / "checkout"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "agents" / f"{agent}.json").write_text(
        json.dumps(
            {
                "name": agent,
                "allowedTools": ["*"],
                "resources": ["skill://checkout-skills/**/SKILL.md"],
            }
        )
    )
    record = await run_turn(
        TurnScript(message="deploy it", setup=_bind_in(project), provider=provider),
        slot=SlotSpec(),
        config={"agent": {"acp_backend": backend}},
    )
    assert _streamed(record)
    assert not any("app_agent_refused" in str(row) for row in record.window)
    [(session_key, pinned)] = clean_pins.items()
    assert pinned == agents_dir / f"{_APP}--{agent}.json"

    for globs in (
        session_skill_globs(session_key, agent, project_dir=project, provider_type=PROVIDER_ACP),
        context._skills_injection_plan(
            agent,
            is_cc=False,
            project_dir=project,
            provider_type=PROVIDER_ACP,
            session_key=session_key,
        )[1],
    ):
        assert globs and all("app-skills" in g for g in globs)
        assert not any("checkout-skills" in g for g in globs)
    # Without the pin the same name resolves project-first, to the checkout's spec.
    unpinned = session_skill_globs("other", agent, project_dir=project, provider_type=PROVIDER_ACP)
    assert unpinned and all("checkout-skills" in g for g in unpinned)


@pytest.mark.asyncio
async def test_a_turn_that_is_not_the_launch_copy_unpins_its_session(agents_dir, clean_pins):
    # The pin is re-decided on every turn of the key: an unbound turn drops it, and
    # an unrelated key keeps its own.
    from kiro_crew.dashboard.chat_runner import effective_session_key

    seen: list[str] = []

    def pin_before(ctx):
        key = effective_session_key(ctx.slot)
        seen.append(key)
        clean_pins[key] = agents_dir / "stale.json"

    clean_pins["another-session"] = agents_dir / "other.json"
    await run_turn(
        TurnScript(message="hello", setup=pin_before, provider=_KiroProvider),
        slot=SlotSpec(),
        config={"agent": {"acp_backend": "kiro"}},
    )
    assert seen and seen[0] not in clean_pins
    assert clean_pins["another-session"] == agents_dir / "other.json"


@pytest.mark.asyncio
async def test_a_turn_refused_before_the_claim_keeps_the_running_turns_pin(
    agents_dir, monkeypatch, clean_pins
):
    # A turn that never takes the session (here: its copy cannot be written) must
    # not touch the pin the turn holding the session reads its skills by.
    from kiro_crew.dashboard.chat_runner import effective_session_key

    keys: list[str] = []

    def bind_and_pin(ctx):
        _bind(ctx)
        key = effective_session_key(ctx.slot)
        keys.append(key)
        clean_pins[key] = agents_dir / "running.json"

    agent = KiroCrewConfig.load().default_agent
    _register(agents_dir, agent)

    def unwritable(target, data):
        raise OSError("read-only file system")

    monkeypatch.setattr("kiro_crew.agent._atomic_json_write", unwritable)
    record = await run_turn(
        TurnScript(message="deploy it", setup=bind_and_pin, provider=_KiroProvider),
        slot=SlotSpec(),
        config={"agent": {"acp_backend": "kiro"}},
    )
    assert not record.allocations
    assert clean_pins[keys[0]] == agents_dir / "running.json"


def test_the_injected_agent_prompt_is_read_from_the_pinned_file(agents_dir, tmp_path, clean_pins):
    # Crew's [AGENT SYSTEM PROMPT] for a pinned session comes from the app's
    # registered file, not from a checkout spec declaring the same bare name.
    from kiro_crew import context
    from kiro_crew.skill_scope_pin import pin_session_skill_spec, pinned_session_skill_spec

    _register(agents_dir, "deploy", prompt="the app's prompt")
    project = tmp_path / "checkout"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "agents" / "deploy.json").write_text(
        json.dumps({"name": "deploy", "prompt": "the checkout's prompt"})
    )
    load = context.ContextBuilder._load_agent_prompt
    assert load("deploy", str(project)) == "the checkout's prompt"
    pin_session_skill_spec("app-session", agents_dir / f"{_APP}--deploy.json")
    pinned = pinned_session_skill_spec("app-session")
    assert load("deploy", str(project), spec_path=pinned) == "the app's prompt"


@pytest.mark.parametrize("launch", ["kirocrew-app-launch-x", ""], ids=["copy", "plain"])
def test_a_newer_projection_keeps_the_copy_the_spawn_held(tmp_path, launch):
    # A set_mode refresh adopts a fresh preparation, which lists no copy; the
    # copy's view and search verdict carry over, and nothing else changes.
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    runtime = AcpRuntime(work_dir=tmp_path, agent="deploy", launch_agent=launch)
    held = NativeSkillProjection({}, specs={"kirocrew-app-launch-x": {"name": "copy"}})
    held.search_agents.add("kirocrew-app-launch-x")
    assert runtime._adopt_skill_projection(held, 1)
    fresh = NativeSkillProjection({"kirocrew": "kirocrew-skill-view-a"})
    assert runtime._adopt_skill_projection(fresh, 2)
    if launch:
        assert fresh.specs[launch] == {"name": "copy"}
        assert launch in fresh.search_agents
    else:
        assert fresh.specs == {} and fresh.search_agents == set()


def test_the_copy_is_a_search_agent_exactly_when_its_view_is(agents_dir, monkeypatch):
    # The projection's own verdict decides, not the presence of kirocrew-core: an
    # unmapped agent whose authored kirocrew-core the mount would hold back keeps
    # its view without search, so its sessions are not refused for it.
    monkeypatch.setattr(agent_launch, "_SEARCH_COPIES", set())
    _register(
        agents_dir,
        mcpServers={"kirocrew-core": {"command": "x", "disabledTools": ["learn_add"]}},
    )
    name = publish_app_agent_launch(_APP, "deploy")
    assert not agent_launch.is_search_launch_copy(name)
    _register(agents_dir, resources=["skill://skills/**/SKILL.md"])
    assert publish_app_agent_launch(_APP, "deploy") == name
    assert agent_launch.is_search_launch_copy(name)


class TestTheCopyRunsTheLaunchSteeringDecision:
    """A launch-copy spawn makes the same inherit decision every Kiro launch makes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("inherits", [False, True], ids=["opted-out", "inherits"])
    async def test_the_decision_runs_on_the_copys_view(self, tmp_path, monkeypatch, inherits):
        from test_acp_spawn_offload import TestRuntimeProjectedLaunchDocuments as harness

        from kiro_crew.acp import runtime as runtime_facade

        agents = tmp_path / "copy-agents"
        agents.mkdir()
        launch = "kirocrew-app-launch-x"
        (agents / f"{launch}.json").write_text(
            json.dumps(
                {
                    "name": launch,
                    "description": agent_launch.OWNER_MARKER,
                    "resources": ["file://.kiro/steering/**/*.md"],
                    "mcpServers": {"kirocrew-core": {"command": "managed"}},
                }
            )
        )
        # Through the facade: every owner holding the name sees the patch.
        monkeypatch.setattr(runtime_facade, "kiro_agents_dir", lambda: agents)
        monkeypatch.setattr(agent_launch, "_SEARCH_COPIES", {launch})

        def as_copy(runtime):
            runtime._launch_agent = launch

        runtime, _calls = await harness._drive(
            monkeypatch, tmp_path, inherits=inherits, before_spawn=as_copy
        )
        projection = runtime._native_skill_projection
        assert projection.specs[launch]["name"] == launch
        assert launch in projection.search_agents
        guide = tmp_path / "workspace" / ".kiro" / "steering" / "guide.md"
        if inherits:
            assert runtime._native_launch_sources == {}
            assert runtime._native_launch_view_alias is None
        else:
            assert runtime._native_launch_sources == {str(guide): ""}
            assert runtime._native_launch_view_alias == launch
