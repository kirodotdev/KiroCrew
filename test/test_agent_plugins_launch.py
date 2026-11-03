"""The stdio launcher every Agent Plugin server starts through (spec §9).

Expansion is checked against the conformance core fixture's ``default`` server,
whose args and env spell out every case the suite probes: an exact
``${PLUGIN_ROOT}`` / ``${PLUGIN_DATA}``, an unknown ``${...}``, a bare ``$NAME``,
and ``${PLUGIN_ROOT_SUFFIX}``, which must not match ``${PLUGIN_ROOT}``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from agent_plugins_helpers import install, mcp

from kiro_crew.agent_plugins import consent, launch

FIXTURES = Path(__file__).parent / "fixtures" / "agent_plugins_conformance"
CORE = "agent-plugins-conformance-core"


@pytest.fixture
def crew_home(tmp_path, monkeypatch):
    home = tmp_path / "crew"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


def _turn_on(home: Path, name: str) -> None:
    consent.set_enabled(name, True)


@pytest.fixture
def core(tmp_path, crew_home):
    """The conformance core plugin, installed and turned on."""
    powers = tmp_path / "powers"
    root = powers / "installed" / CORE
    shutil.copytree(FIXTURES / CORE, root)
    for rel in ("bin/posix/probe token.sh", "bin/windows/probe token.cmd", "dist/probe.mjs"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("", encoding="utf-8")
    (root / "probe-workdir").mkdir()
    (powers / "installed.json").write_text(
        json.dumps({"installedPowers": [{"name": CORE}]}), encoding="utf-8"
    )
    _turn_on(powers, CORE)
    return powers


def _plan(powers: Path, server: str, environ: dict[str, str] | None = None) -> launch.Launch:
    env = {"PATH": os.environ.get("PATH", ""), **(environ or {})}
    return launch.plan_launch(CORE, server, env, home=powers)


class TestExpansion:
    def test_expand_is_one_pass_and_exact(self):
        assert launch.expand("${PLUGIN_ROOT}/x", "/r", "/d") == "/r/x"
        assert launch.expand("${PLUGIN_ROOT_SUFFIX}", "/r", "/d") == "${PLUGIN_ROOT_SUFFIX}"
        assert launch.expand("${APC_UNKNOWN}|$APC_VALUE", "/r", "/d") == "${APC_UNKNOWN}|$APC_VALUE"
        # A replacement is never scanned again.
        assert launch.expand("${PLUGIN_ROOT}", "${PLUGIN_DATA}", "/d") == "${PLUGIN_DATA}"

    @pytest.mark.skipif(shutil.which("node") is None, reason="the default server runs node")
    def test_default_server_matches_the_conformance_expectations(self, core):
        plan = _plan(core, "default")
        root = os.path.realpath(core / "installed" / CORE)
        data = os.path.abspath(core / "data" / CORE)
        assert plan.argv[1:] == [
            f"{root}/dist/probe.mjs",
            "default",
            "arg with spaces",
            "",
            root,
            data,
            "${APC_UNKNOWN}",
            "$APC_VALUE",
            "${PLUGIN_ROOT_SUFFIX}",
        ]
        assert plan.env["APC_VALUE"] == "fixture value with spaces"
        assert plan.env["APC_EXPANSION"] == f"{root}|{data}|{root}"
        assert plan.env["APC_LITERAL"] == "${APC_UNKNOWN}|$APC_VALUE|${PLUGIN_ROOT_SUFFIX}"


class TestEnvAndCwd:
    def _plugin(self, tmp_path, server: dict) -> Path:
        powers = tmp_path / "powers"
        install(
            powers,
            "p",
            **{"mcp.json": mcp({"s": server}), "bin__run": "#!/bin/sh\n", "work__keep": ""},
        )
        (powers / "installed" / "p" / "bin" / "run").chmod(0o755)
        _turn_on(powers, "p")
        return powers

    def test_declared_env_overrides_inherited_and_plugin_vars_are_set_last(
        self, tmp_path, crew_home
    ):
        powers = self._plugin(
            tmp_path, {"type": "stdio", "command": "./bin/run", "env": {"USER": "configured"}}
        )
        plan = launch.plan_launch(
            "p",
            "s",
            {"USER": "inherited", "PLUGIN_ROOT": "/bogus", "PLUGIN_DATA": "/bogus", "PATH": ""},
            home=powers,
        )
        root = os.path.realpath(powers / "installed" / "p")
        assert plan.env["USER"] == "configured"
        assert plan.env["PLUGIN_ROOT"] == root
        assert plan.env["PLUGIN_DATA"] == os.path.abspath(powers / "data" / "p")
        assert plan.executable == os.path.join(root, "bin", "run")

    def test_cwd_defaults_to_the_root_and_the_data_folder_is_created(self, tmp_path, crew_home):
        powers = self._plugin(tmp_path, {"type": "stdio", "command": "./bin/run"})
        assert not (powers / "data" / "p").exists()
        plan = launch.plan_launch("p", "s", {"PATH": ""}, home=powers)
        assert plan.cwd == os.path.realpath(powers / "installed" / "p")
        assert (powers / "data" / "p").is_dir()

    @pytest.mark.parametrize(
        ("cwd", "expected"),
        [("./work", "installed/p/work"), ("${PLUGIN_ROOT}/work", "installed/p/work")],
    )
    def test_relative_and_rooted_cwd(self, tmp_path, crew_home, cwd, expected):
        powers = self._plugin(tmp_path, {"type": "stdio", "command": "./bin/run", "cwd": cwd})
        plan = launch.plan_launch("p", "s", {"PATH": ""}, home=powers)
        assert plan.cwd == os.path.join(os.path.realpath(powers), *expected.split("/"))

    def test_a_bare_command_resolves_through_path(self, tmp_path, crew_home):
        powers = self._plugin(tmp_path, {"type": "stdio", "command": "run"})
        bindir = str(powers / "installed" / "p" / "bin")
        plan = launch.plan_launch("p", "s", {"PATH": bindir}, home=powers)
        assert plan.executable == os.path.join(bindir, "run")


class TestRefusals:
    def _plugin(self, tmp_path) -> Path:
        powers = tmp_path / "powers"
        install(
            powers,
            "p",
            **{
                "mcp.json": mcp(
                    {
                        "s": {"type": "stdio", "command": "./bin/run"},
                        "r": {"type": "streamable-http", "url": "https://example.com/mcp"},
                    }
                ),
                "bin__run": "#!/bin/sh\n",
            },
        )
        return powers

    def test_an_off_plugin_is_refused(self, tmp_path, crew_home):
        powers = self._plugin(tmp_path)
        with pytest.raises(launch.LaunchRefused, match="off, not on"):
            launch.plan_launch("p", "s", {}, home=powers)

    def test_a_plugin_turned_off_after_on_is_refused(self, tmp_path, crew_home):
        powers = self._plugin(tmp_path)
        _turn_on(powers, "p")
        consent.set_enabled("p", False)
        with pytest.raises(launch.LaunchRefused, match="off, not on"):
            launch.plan_launch("p", "s", {}, home=powers)

    def test_an_on_plugin_that_turned_invalid_is_refused(self, tmp_path, crew_home):
        powers = self._plugin(tmp_path)
        _turn_on(powers, "p")
        (powers / "installed" / "p" / "plugin.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(launch.LaunchRefused, match="invalid, not on"):
            launch.plan_launch("p", "s", {}, home=powers)

    def test_an_edited_on_plugin_still_launches(self, tmp_path, crew_home):
        powers = self._plugin(tmp_path)
        _turn_on(powers, "p")
        (powers / "installed" / "p" / "bin" / "run").write_text("#!/bin/sh\necho v2\n")
        plan = launch.plan_launch("p", "s", {"PATH": ""}, home=powers)
        assert plan.executable == os.path.join(
            os.path.realpath(powers / "installed" / "p"), "bin", "run"
        )

    def test_a_remote_or_unknown_server_is_refused(self, tmp_path, crew_home):
        powers = self._plugin(tmp_path)
        _turn_on(powers, "p")
        for server in ("r", "nope"):
            with pytest.raises(launch.LaunchRefused, match="no valid stdio server"):
                launch.plan_launch("p", server, {}, home=powers)

    def test_a_path_shaped_plugin_name_is_refused(self, tmp_path, crew_home):
        with pytest.raises(launch.LaunchRefused, match="plain folder name"):
            launch.plan_launch("../p", "s", {}, home=tmp_path)

    def test_main_prints_one_line_and_exits_78(self, tmp_path, crew_home, monkeypatch, capsys):
        monkeypatch.setenv("KIRO_POWERS_HOME", str(self._plugin(tmp_path)))
        with pytest.raises(SystemExit) as exc:
            launch.main(["p", "s"])
        assert exc.value.code == 78
        err = capsys.readouterr().err
        assert "plugin MCP server p:s was not started" in err and err.count("\n") == 1


@pytest.mark.skipif(os.name != "posix", reason="execs a POSIX shell script")
def test_the_launcher_execs_the_server_in_place(tmp_path, crew_home):
    """A real launch: the server runs in the root, with the spec's environment."""
    powers = tmp_path / "powers"
    script = '#!/bin/sh\nprintf "%s|%s|%s|%s" "$(pwd)" "$PLUGIN_ROOT" "$GREETING" "$1"\n'
    install(
        powers,
        "p",
        **{
            "mcp.json": mcp(
                {
                    "s": {
                        "type": "stdio",
                        "command": "./bin/run",
                        "args": ["${PLUGIN_DATA}"],
                        "env": {"GREETING": "hi ${PLUGIN_ROOT}"},
                    }
                }
            ),
            "bin__run": script,
        },
    )
    (powers / "installed" / "p" / "bin" / "run").chmod(0o755)
    _turn_on(powers, "p")
    env = {
        **os.environ,
        "KIRO_POWERS_HOME": str(powers),
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    out = subprocess.run(
        [sys.executable, "-m", launch.LAUNCH_MODULE, "p", "s"],
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    ).stdout
    root = os.path.realpath(powers / "installed" / "p")
    data = os.path.abspath(powers / "data" / "p")
    assert out == f"{root}|{root}|hi {root}|{data}"
