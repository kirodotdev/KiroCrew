"""pi's Node.js engine guard: which Node pi will run under, against pi's own floor.

Driven against real files: a fake npm prefix holding pi's package (manifest, a
``#!/usr/bin/env node`` script, the ``bin/pi`` link) and fake ``node`` executables
that print a fixed version, so the PATH lookup, the shebang read, the manifest read
and the ``node --version`` probe all run as they do on a host.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from acp_pi_backend_helpers import _no_ambient_pi_env, _PiLaunchRig  # noqa: F401

from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp.harness import pi_node
from kiro_crew.acp.harness.pi_node import (
    PiNodeChoice,
    choose_pi_node,
    node_engine_floor,
)
from kiro_crew.acp.transport_errors import AcpError

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX shebang launch; Windows is unmanaged"
)

_FLOOR = (22, 19, 0)


def _fake_node(directory: Path, version: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    node = directory / "node"
    node.write_text(f"#!/bin/sh\necho v{version}\n", encoding="utf-8")
    node.chmod(0o755)
    return str(node)


def _fake_pi(prefix: Path, *, engines: object = None, shebang: str = "#!/usr/bin/env node") -> str:
    """An npm global install of pi under *prefix*; returns the ``bin/pi`` link."""
    package = prefix / "lib" / "node_modules" / "@earendil-works" / "pi-coding-agent"
    (package / "dist").mkdir(parents=True)
    manifest: dict = {"name": pi_mod.PI_NPM_PKG, "version": "1.3.0"}
    if engines is not None:
        manifest["engines"] = engines
    (package / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    script = package / "dist" / "cli.js"
    script.write_text(f"{shebang}\nconsole.log('pi')\n", encoding="utf-8")
    script.chmod(0o755)
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    link = prefix / "bin" / "pi"
    link.symlink_to(script)
    return str(link)


def _path(*dirs: Path) -> str:
    return os.pathsep.join(str(d) for d in dirs)


@pytest.mark.parametrize(
    ("spec", "floor"),
    [
        (">=22.19.0", (22, 19, 0)),
        ("^22.19 || >=24", (22, 19, 0)),
        ("22.x", (22, 0, 0)),
        (">=20 <24", (20, 0, 0)),
        ("20.1 - 24", (20, 1, 0)),
        (">22", None),
        (">=22.19 or so", None),
        ("22.19.0-beta.1", None),
        ("*", None),
        ("", None),
        (None, None),
        (22, None),
    ],
)
def test_the_engine_floor_is_the_lowest_version_the_range_admits(spec, floor) -> None:
    assert node_engine_floor(spec) == floor


def test_a_node_below_the_floor_with_none_newer_refuses_naming_both(tmp_path) -> None:
    pi_bin = _fake_pi(tmp_path / "prefix", engines={"node": ">=22.19.0"})
    old = _fake_node(tmp_path / "usr-local-bin", "18.20.4")
    _fake_node(tmp_path / "later", "22.18.9")
    choice = choose_pi_node(pi_bin, _FLOOR, _path(tmp_path / "usr-local-bin", tmp_path / "later"))
    assert choice.argv == ()
    assert old in choice.issue
    assert "v18.20.4" in choice.issue
    assert "22.19.0" in choice.issue
    assert "'engines' floor" in choice.issue


def test_no_node_on_the_path_and_none_beside_pi_refuses(tmp_path) -> None:
    pi_bin = _fake_pi(tmp_path / "prefix")
    choice = choose_pi_node(pi_bin, _FLOOR, _path(tmp_path / "empty"))
    assert "no 'node' is on the session's PATH" in choice.issue


def test_a_node_at_the_floor_launches_pi_unchanged(tmp_path) -> None:
    pi_bin = _fake_pi(tmp_path / "prefix")
    _fake_node(tmp_path / "usr-local-bin", "22.19.0")
    assert choose_pi_node(pi_bin, _FLOOR, _path(tmp_path / "usr-local-bin")) == PiNodeChoice()


def test_a_node_beside_pi_is_chosen_over_an_older_one_first_on_the_path(tmp_path) -> None:
    prefix = tmp_path / "prefix"
    pi_bin = _fake_pi(prefix)
    _fake_node(tmp_path / "usr-local-bin", "18.20.4")
    beside = _fake_node(prefix / "bin", "24.18.0")
    choice = choose_pi_node(pi_bin, _FLOOR, _path(tmp_path / "usr-local-bin", prefix / "bin"))
    assert choice == PiNodeChoice(argv=(beside, os.path.realpath(pi_bin)))


def test_a_newer_node_later_on_the_path_is_chosen(tmp_path) -> None:
    pi_bin = _fake_pi(tmp_path / "prefix")
    _fake_node(tmp_path / "a", "20.11.1")
    later = _fake_node(tmp_path / "b", "22.20.0")
    choice = choose_pi_node(pi_bin, _FLOOR, _path(tmp_path / "a", tmp_path / "b"))
    assert choice.argv == (later, os.path.realpath(pi_bin))


def test_a_node_in_pis_npm_prefix_is_found_when_pi_is_linked_from_elsewhere(tmp_path) -> None:
    prefix = tmp_path / "prefix"
    script = os.path.realpath(_fake_pi(prefix))
    in_prefix = _fake_node(prefix / "bin", "24.0.0")
    elsewhere = tmp_path / "links"
    elsewhere.mkdir()
    (elsewhere / "pi").symlink_to(script)
    _fake_node(tmp_path / "old", "18.0.0")
    choice = choose_pi_node(str(elsewhere / "pi"), _FLOOR, _path(tmp_path / "old"))
    assert choice.argv == (in_prefix, script)


def test_an_absolute_node_shebang_is_checked_too(tmp_path) -> None:
    old = _fake_node(tmp_path / "opt", "16.0.0")
    pi_bin = _fake_pi(tmp_path / "prefix", shebang=f"#!{old}")
    choice = choose_pi_node(pi_bin, _FLOOR, _path(tmp_path / "empty"))
    assert old in choice.issue and "v16.0.0" in choice.issue


@pytest.mark.parametrize(
    "shebang", ["#!/bin/sh", "#!/usr/bin/env -S node --no-warnings", "console.log(1)"]
)
def test_a_pi_that_is_not_a_plain_node_script_is_left_alone(tmp_path, shebang) -> None:
    pi_bin = _fake_pi(tmp_path / "prefix", shebang=shebang)
    _fake_node(tmp_path / "old", "18.0.0")
    assert choose_pi_node(pi_bin, _FLOOR, _path(tmp_path / "old")) == PiNodeChoice()


def test_a_node_whose_version_cannot_be_read_is_left_to_the_read_back(tmp_path) -> None:
    pi_bin = _fake_pi(tmp_path / "prefix")
    broken = tmp_path / "broken" / "node"
    broken.parent.mkdir()
    broken.write_text("#!/bin/sh\necho nonsense\n", encoding="utf-8")
    broken.chmod(0o755)
    assert choose_pi_node(pi_bin, _FLOOR, _path(broken.parent)) == PiNodeChoice()


def test_the_floor_is_read_from_pis_manifest(tmp_path) -> None:
    pi_bin = _fake_pi(tmp_path / "prefix", engines={"node": ">=24.1.0"})
    assert pi_mod._pi_node_floor(pi_bin) == (24, 1, 0)


@pytest.mark.parametrize("engines", [None, {}, {"node": ">22"}, "node>=22"])
def test_an_absent_or_unreadable_engine_is_no_floor(tmp_path, engines) -> None:
    pi_bin = _fake_pi(tmp_path / "prefix", engines=engines)
    assert pi_mod._pi_node_floor(pi_bin) is None


class _Session:
    """The three attributes ``_pi_node_choice`` reads off a session."""

    def __init__(self, path: str, work_dir: str) -> None:
        self._extra_env = {"PATH": path}
        self._spawn_work_dir = work_dir

    def _agent_env_for_spawn(self) -> dict:
        return {}


def test_without_a_declared_floor_pi_is_left_to_the_read_back(tmp_path) -> None:
    """No floor is guessed: an old Node is not probed, let alone refused."""
    pi_bin = _fake_pi(tmp_path / "prefix")
    _fake_node(tmp_path / "old", "18.0.0")
    probed: list = []

    @contextmanager
    def _wrap(argv):
        probed.append(argv)
        yield argv

    session = _Session(_path(tmp_path / "old"), str(tmp_path))
    assert pi_mod._pi_node_choice(session, pi_bin, _wrap) == PiNodeChoice()
    assert probed == []


def test_the_version_probe_gets_no_gateway_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NODE_OPTIONS", "--require /nonexistent.js")
    monkeypatch.setenv("KIROCREW_PROBE_SECRET", "s3cret")
    node = tmp_path / "bin" / "node"
    node.parent.mkdir()
    node.write_text(
        '#!/bin/sh\n[ -z "$NODE_OPTIONS$KIROCREW_PROBE_SECRET" ] && echo v22.19.0\n',
        encoding="utf-8",
    )
    node.chmod(0o755)
    assert pi_node._node_version(str(node), str(node.parent), "") == (22, 19, 0)


def test_the_launcher_runs_pi_under_the_chosen_node() -> None:
    body = pi_mod._pi_gate_launcher_body(
        "/usr/local/bin/pi", "/run/gate.ts", ("/opt/node 24/bin/node", "/opt/pi/cli.js")
    )
    assert body == (
        "#!/bin/sh\nexec '/opt/node 24/bin/node' /opt/pi/cli.js \"$@\" --extension /run/gate.ts\n"
    )


def test_a_launcher_per_program(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", lambda: str(tmp_path))
    pi_bin, gate = str(tmp_path / "pi"), str(tmp_path / "gate.ts")
    plain = pi_mod._ensure_pi_gate_launcher(pi_bin, gate)
    chosen = pi_mod._ensure_pi_gate_launcher(pi_bin, gate, ("/n/node", "/p/cli.js"))
    assert plain != chosen
    assert "exec /n/node /p/cli.js " in Path(chosen).read_text(encoding="utf-8")


def test_the_spawn_refuses_on_the_node_issue_before_any_child(monkeypatch, tmp_path) -> None:
    rig = _PiLaunchRig(monkeypatch, tmp_path, node_choice=PiNodeChoice(issue="pi needs Node"))
    with pytest.raises(AcpError, match="pi needs Node"):
        rig.run()
    assert rig.names().index("version") < rig.names().index("node")
    assert not {"seal", "launcher", "wrap", "verify"} & set(rig.names())


def test_the_spawn_hands_the_chosen_node_to_the_launcher(monkeypatch, tmp_path) -> None:
    program = ("/opt/node/bin/node", "/opt/pi/cli.js")
    rig = _PiLaunchRig(monkeypatch, tmp_path, node_choice=PiNodeChoice(argv=program))
    rig.run()
    assert rig.args_of("launcher") == (rig.PI_BIN, rig.SEALED, program)
    assert rig.ran_off_the_loop("node"), "the node probe ran on the event loop"


def test_the_node_probe_runs_after_the_sandbox_floor_under_its_mask(monkeypatch, tmp_path) -> None:
    rig = _PiLaunchRig(monkeypatch, tmp_path)
    wrapped: list = []

    def _wrap_argv(argv, **kwargs):
        wrapped.append((list(argv), kwargs))
        return ["/sandbox", *argv], "/run/probe-launcher"

    unlinked: list = []
    monkeypatch.setattr(pi_mod, "wrap_argv", _wrap_argv)
    monkeypatch.setattr(pi_mod.launch_mod, "_unlink_readback_launcher", unlinked.append)
    rig.run()
    names = rig.names()
    assert names.index("preflight") < names.index("node") < names.index("seal")
    _pi_bin, wrap = rig.args_of("node")
    unlinked.clear()  # the read-back's own launcher, reclaimed during the run
    with wrap(["/usr/local/bin/node", "--version"]) as argv:
        assert argv == ["/sandbox", "/usr/local/bin/node", "--version"]
        assert unlinked == [], "the launcher was removed before the child ran"
    assert unlinked == ["/run/probe-launcher"]
    assert wrapped == [
        (
            ["/usr/local/bin/node", "--version"],
            {
                "mode": rig.MODE,
                "strip_python_env": True,
                "extra_hidden_dirs": rig.HIDDEN,
                "extra_expose_files": pi_mod.acp_tool_gate.adapter_expose_files(
                    pi_mod.ACP_BACKEND_PI, rig.HIDDEN
                ),
            },
        )
    ]


def test_every_version_child_runs_through_the_wrap(tmp_path) -> None:
    prefix = tmp_path / "prefix"
    pi_bin = _fake_pi(prefix)
    old = _fake_node(tmp_path / "old", "18.0.0")
    beside = _fake_node(prefix / "bin", "24.0.0")
    seen: list[list[str]] = []

    @contextmanager
    def _wrap(argv):
        seen.append(list(argv))
        yield argv

    choice = choose_pi_node(pi_bin, _FLOOR, _path(tmp_path / "old"), wrap=_wrap)
    assert choice.argv[0] == beside
    assert seen == [[old, "--version"], [beside, "--version"]]
