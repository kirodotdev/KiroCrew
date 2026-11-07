"""The MiMo Code launch, end to end at its own seams: the read-back and version probe,
the deny rules found out of force and the re-projection, and the governance refusal.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import acp_tool_gate
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.client import AcpClient, AcpToolGateUnroutable
from kiro_crew.acp.harness import mimocode as mimocode_mod
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.agent_sdk.backends import ACP_BACKEND_MIMOCODE

# Copied from test_acp_mimocode_backend.py: a test module never imports a sibling.
_HOST_MIMO_ENV = (
    "MIMO_BIN",
    "MIMOCODE_HOME",
    "MIMOCODE_CONFIG_CONTENT",
    "MIMOCODE_CONFIG",
    "MIMOCODE_CONFIG_DIR",
    "MIMOCODE_AUTH_CONTENT",
)


def _isolate_mimocode_env(monkeypatch) -> None:
    """Remove the host's MiMo Code variables."""
    names = {
        *_HOST_MIMO_ENV,
        *mimocode_mod._MIMOCODE_STRIPPED_ENV,
        *(name for name, _value in mimocode_mod._MIMOCODE_PINNED_ENV),
        *(name for name, _why in mimocode_mod._MIMOCODE_REFUSED_ENV),
        *(name for name in os.environ if name.upper().startswith("MIMOCODE_")),
    }
    for name in sorted(names):
        monkeypatch.delenv(name, raising=False)


def _answer(stdout: str, returncode: int = 0):
    return type("_Completed", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()


# ── The launch, end to end at its own seams


class _MimoCodeLaunchRig:
    """``MimoCodeLaunch.resolve_spawn`` against fakes at the adapter's seams."""

    BIN = "/opt/bin/mimo"
    HIDDEN = ("/opt/creds/.aws",)
    SANDBOX = ["/opt/run/kirocrew_sandbox_launcher"]
    #: Launcher artifact per wrap; the launch must reclaim each.
    LAUNCHERS = {
        "wrap_version": "/opt/run/launcher-version.sh",
        "wrap_readback": "/opt/run/launcher-readback.sh",
    }

    def __init__(
        self,
        monkeypatch,
        tmp_path,
        *,
        routing=("", ""),
        probe_stdout="0.1.15",
        readback_stdout: str | None = None,
    ):
        """*readback_stdout*: real read-back on that document, not *routing*."""
        _isolate_mimocode_env(monkeypatch)
        #: ``(name, thread id, args)`` per seam, in reach order.
        self.events: list[tuple] = []
        self.loop_thread: int | None = None
        self.client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
        self.adapter = mimocode_mod.MimoCodeLaunch()
        self._tmp_path = tmp_path
        real_verify = mimocode_mod._verify_mimocode_routing
        reclaim_of = {
            path: name.replace("wrap_", "reclaim_") for name, path in self.LAUNCHERS.items()
        }
        _record = self.record

        def _reclaim(path):
            _record(reclaim_of.get(path, "reclaim_unknown"), path)

        monkeypatch.setattr(launch_mod, "_unlink_readback_launcher", _reclaim)

        async def _launch(backend):
            _record("resolve", backend)
            return self.BIN, [self.BIN, "acp"], "mimo acp", "mimo"

        async def _prepare_session_mcp():
            _record("session_mcp")
            # Warm as production does (``client_mcp_array._prepare_session_mcp``):
            # a test that wants servers placed sets the cache before running.
            if self.client._session_mcp_cache is None:
                self.client._session_mcp_cache = []

        async def _preflight(preflight, backend, mode):
            _record("preflight", preflight, backend, mode)
            return self.HIDDEN

        async def _wrap(argv, **kwargs):
            name = "wrap_version" if "--version" in argv else "wrap_readback"
            _record(name, list(argv), kwargs)
            return [*self.SANDBOX, *argv], self.LAUNCHERS[name]

        def _verify(session, backend, argv, config_content):
            _record("verify", list(argv), json.loads(config_content))
            if readback_stdout is None:
                return routing
            return real_verify(session, backend, argv, config_content)

        def _run(argv, **kwargs):
            if "--version" in argv:
                _record("version_probe", argv, kwargs)
                return _answer(probe_stdout)
            _record("readback_child", argv, kwargs)
            return _answer(readback_stdout or "")

        real_child_env = mimocode_mod._mimocode_child_env

        def _child_env(session):
            _record("child_env")
            return real_child_env(session)

        monkeypatch.setattr(mimocode_mod, "_mimocode_child_env", _child_env)
        monkeypatch.setattr(launch_mod, "resolve_self_served_launch", _launch)
        monkeypatch.setattr(self.client, "_prepare_session_mcp", _prepare_session_mcp)
        monkeypatch.setattr(launch_mod, "_run_preflight_bounded", _preflight)
        monkeypatch.setattr(mimocode_mod, "wrap_argv_async", _wrap)
        monkeypatch.setattr(mimocode_mod, "_verify_mimocode_routing", _verify)
        monkeypatch.setattr(mimocode_mod.subprocess_mod, "run", _run)

    def record(self, name: str, *args) -> None:
        self.events.append((name, threading.get_ident(), args))

    def names(self) -> list[str]:
        return [event[0] for event in self.events]

    def event(self, name: str) -> tuple:
        """The one event of *name*; fails if reached twice or never."""
        matches = [event for event in self.events if event[0] == name]
        assert len(matches) == 1, f"{name!r} was reached {len(matches)} times: {self.names()}"
        return matches[0]

    def reclaimed(self) -> list[str]:
        """Reclaimed launcher artifacts, in order."""
        return [event[2][0] for event in self.events if event[0].startswith("reclaim_")]

    def run(self):
        async def _go():
            self.loop_thread = threading.get_ident()
            return await self.adapter.resolve_spawn(
                SpawnContext(
                    agent="kirocrew",
                    work_dir=self._tmp_path,
                    model=None,
                    environ={},
                    home=self._tmp_path,
                    sandbox_mode="standard",
                    session=self.client,
                )
            )

        return asyncio.run(asyncio.wait_for(_go(), timeout=30))


def test_the_launch_reads_back_the_mimocode_seed_and_arms_the_tripwire(
    monkeypatch, tmp_path
) -> None:
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path)
    assert rig.client._unasked_shell_tripwire is False
    plan = rig.run()
    assert plan.argv == [rig.BIN, "acp"]
    _name, _thread, (argv, seed) = rig.event("verify")
    assert argv == [*rig.SANDBOX, rig.BIN, "debug", "config"]
    assert seed["permission"] == "ask"
    assert rig.client._unasked_shell_tripwire is True
    # Each launcher is reclaimed after its child exits.
    assert rig.reclaimed() == [rig.LAUNCHERS["wrap_version"], rig.LAUNCHERS["wrap_readback"]]


def test_both_children_run_bounded_in_the_session_work_dir_without_a_shell(
    monkeypatch, tmp_path
) -> None:
    """Each child gets an argv list, a timeout and the session work dir (config root)."""
    seed_doc = '{"permission": {"*": "ask"}}'
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path, readback_stdout=seed_doc)
    rig.run()
    for name, timeout in (
        ("version_probe", mimocode_mod._MIMOCODE_VERSION_TIMEOUT_S),
        ("readback_child", mimocode_mod._MIMOCODE_READBACK_TIMEOUT_S),
    ):
        _name, _thread, (argv, kwargs) = rig.event(name)
        assert isinstance(argv, list) and argv[0] == rig.SANDBOX[0], name
        assert kwargs["timeout"] == timeout and timeout > 0, name
        assert kwargs["cwd"] == rig.client._spawn_work_dir, name
        assert Path(kwargs["cwd"]).resolve() == tmp_path.resolve(), name
        assert kwargs.get("shell", False) is False, name
    _name, _thread, (_argv, readback_kwargs) = rig.event("readback_child")
    _name, _thread, (_argv, seed) = rig.event("verify")
    assert json.loads(readback_kwargs["env"]["MIMOCODE_CONFIG_CONTENT"]) == seed
    _name, _thread, (_argv, version_kwargs) = rig.event("version_probe")
    assert "MIMOCODE_CONFIG_CONTENT" not in version_kwargs["env"], "the version child got a seed"
    assert rig.client._unasked_shell_tripwire is True


def test_the_read_back_and_the_version_probe_run_wrapped_and_off_the_loop(
    monkeypatch, tmp_path
) -> None:
    """Both run the harness binary: behind the sandbox and credential mask, on a worker
    thread, since inline either stalls every dashboard tab for a session start."""
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path)
    rig.run()
    expose = acp_tool_gate.adapter_expose_files(ACP_BACKEND_MIMOCODE, rig.HIDDEN)
    for name, argv in (
        ("wrap_version", [rig.BIN, "--version"]),
        ("wrap_readback", [rig.BIN, "debug", "config"]),
    ):
        _name, _thread, (wrapped, kwargs) = rig.event(name)
        assert wrapped == argv
        assert kwargs["extra_hidden_dirs"] == rig.HIDDEN, f"{name} lost the session's mask"
        assert kwargs["extra_expose_files"] == expose
        assert kwargs["strip_python_env"] is True
    _name, probe_thread, (probe_argv, _kwargs) = rig.event("version_probe")
    assert probe_argv == [*rig.SANDBOX, rig.BIN, "--version"]
    assert probe_thread != rig.loop_thread, "the version child ran on the event loop"
    _name, verify_thread, _args = rig.event("verify")
    assert verify_thread != rig.loop_thread, "the routing read-back ran on the event loop"
    # Child env reads agent.env and the agent socket from disk, so off the loop.
    env_threads = [event[1] for event in rig.events if event[0] == "child_env"]
    assert env_threads, "no child environment was built"
    assert rig.loop_thread not in env_threads, "a child environment was built on the loop"
    for name in ("reclaim_version", "reclaim_readback"):
        _name, reclaim_thread, _args = rig.event(name)
        assert reclaim_thread != rig.loop_thread, f"{name} unlinked on the event loop"


def test_the_floor_and_the_version_gate_precede_the_read_back(monkeypatch, tmp_path) -> None:
    """Mask before any child; untested binary never reaches the read-back; each launcher
    reclaimed after its child, before the next wrap."""
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path)
    rig.run()
    names = rig.names()
    assert (
        names.index("session_mcp")
        < names.index("preflight")
        < names.index("wrap_version")
        < names.index("version_probe")
        < names.index("reclaim_version")
        < names.index("wrap_readback")
        < names.index("verify")
        < names.index("reclaim_readback")
    )
    _name, _thread, (preflight, backend, mode) = rig.event("preflight")
    assert preflight is launch_mod._sandbox_preflight
    assert (backend, mode) == (ACP_BACKEND_MIMOCODE, "standard")


def _assert_raised_without_context(excinfo) -> None:
    """Raised ``from None``: the gate's exception is not chained."""
    assert excinfo.value.__suppress_context__ is True
    assert excinfo.value.__cause__ is None


def test_a_relocated_home_is_refused_before_any_child_runs(monkeypatch, tmp_path) -> None:
    """``MIMOCODE_HOME`` moves the credential past the mask; not even ``--version`` runs."""
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path)
    monkeypatch.setenv("MIMOCODE_HOME", str(tmp_path / "elsewhere"))
    with pytest.raises(AcpToolGateUnroutable, match="MIMOCODE_HOME is set") as excinfo:
        rig.run()
    assert "Unset MIMOCODE_HOME in the gateway's environment" in str(excinfo.value)
    _assert_raised_without_context(excinfo)
    assert not {"wrap_version", "version_probe", "wrap_readback", "verify"} & set(rig.names())
    assert rig.reclaimed() == [], "no child ran, so nothing was there to reclaim"
    assert rig.client._unasked_shell_tripwire is False


def test_the_launch_records_the_memory_root_it_armed_for(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path)
    rig.run()
    assert rig.client._mimocode_memory_root == str(tmp_path / "xdg" / "mimocode" / "memory")


def test_a_refused_routing_leaves_the_tripwire_unarmed(monkeypatch, tmp_path) -> None:
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path, routing=("permission is allow", "set ask"))
    with pytest.raises(AcpToolGateUnroutable) as excinfo:
        rig.run()
    message = str(excinfo.value)
    assert "would not reach Kiro Crew's security gate (permission is allow)" in message
    assert message.endswith(" set ask")
    _assert_raised_without_context(excinfo)
    assert rig.reclaimed() == [rig.LAUNCHERS["wrap_version"], rig.LAUNCHERS["wrap_readback"]]
    assert rig.client._unasked_shell_tripwire is False


def test_an_untested_line_is_refused_before_the_read_back(monkeypatch, tmp_path) -> None:
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path, probe_stdout="0.2.0")
    with pytest.raises(AcpToolGateUnroutable, match="launch refused") as excinfo:
        rig.run()
    assert "reports 0.2" in str(excinfo.value) and "@mimo-ai/cli@0.1" in str(excinfo.value)
    _assert_raised_without_context(excinfo)
    assert not {"wrap_readback", "verify"} & set(rig.names())
    assert rig.reclaimed() == [rig.LAUNCHERS["wrap_version"]]
    assert rig.client._unasked_shell_tripwire is False


# ── A deny rule the read-back finds out of force


#: A seeded deny kept in force, and the same deny outranked by an agent rule for that
#: tool id (appended after the top-level rules, so it wins).
_DENY_HELD = '{"permission": {"*": "ask", "docs_search": "deny"}}'
_DENY_LOST = (
    '{"permission": {"*": "ask", "docs_search": "deny"}, '
    '"agent": {"build": {"permission": {"docs_search": "ask"}}}%s}'
)


def _reprojecting_rig(monkeypatch, tmp_path, readback: str, unhonoured: frozenset[str]):
    """Projects the ``docs_search`` deny; fake re-projection records *unhonoured*."""
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path, readback_stdout=readback)
    rig.client._session_harness_deny_rules = ("docs_search",)

    def _resolve_session_mcp_servers():
        rig.record("reproject", rig.client._mimocode_denies_in_force)
        rig.client._session_mcp_unhonoured = unhonoured
        return [{"name": "kept"}]

    monkeypatch.setattr(rig.client, "_resolve_session_mcp_servers", _resolve_session_mcp_servers)
    return rig


def test_a_deny_the_read_back_finds_out_of_force_re_projects_the_session(
    monkeypatch, tmp_path, caplog
) -> None:
    """The lost rule's server is withheld whole; re-projected off the loop, held rules only."""
    rig = _reprojecting_rig(monkeypatch, tmp_path, _DENY_LOST % "", frozenset({"docs"}))
    with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
        rig.run()
    _name, _thread, (_argv, seed) = rig.event("verify")
    assert seed["permission"] == {"*": "ask", "docs_search": "deny"}
    assert rig.client._mimocode_denies_unenforced == frozenset({"docs_search"})
    # Re-projection sees that no seeded deny held.
    _name, reproject_thread, (in_force_seen,) = rig.event("reproject")
    assert in_force_seen == frozenset()
    assert reproject_thread != rig.loop_thread, "the re-projection read the spec on the loop"
    assert rig.names().index("reclaim_readback") < rig.names().index("reproject")
    assert rig.client._session_mcp_cache == [{"name": "kept"}]
    assert rig.client._session_mcp_unhonoured == frozenset({"docs"})
    assert "1 per-tool deny rule(s) are not in force" in caplog.text
    assert "docs_search" in caplog.text
    assert rig.client._unasked_shell_tripwire is True


def test_a_deny_the_read_back_finds_in_force_projects_nothing_again(monkeypatch, tmp_path) -> None:
    rig = _reprojecting_rig(monkeypatch, tmp_path, _DENY_HELD, frozenset({"docs"}))
    rig.run()
    assert rig.client._mimocode_denies_in_force == frozenset({"docs_search"})
    assert "reproject" not in rig.names()
    # The warm step's array stands; no re-projection replaced it.
    assert rig.client._session_mcp_cache == []
    assert rig.client._unasked_shell_tripwire is True


def test_a_re_projection_that_withholds_a_natively_mounted_server_refuses(
    monkeypatch, tmp_path
) -> None:
    """Re-projection can withhold a server the read-back never judged; a native mount
    keeps its tools, so refusal follows the re-projection."""
    readback = _DENY_LOST % ', "mcp": {"other": {}}'
    rig = _reprojecting_rig(monkeypatch, tmp_path, readback, frozenset({"docs", "other"}))
    with pytest.raises(AcpToolGateUnroutable, match="MiMo Code mounts that server") as excinfo:
        rig.run()
    assert "'other'" in str(excinfo.value) and "'docs'" not in str(excinfo.value)
    assert "stop mounting the server in" in str(excinfo.value)
    # Claude Code's ``.claude.json`` is pinned out of every child, so the remedy names
    # the harness's own files alone.
    assert "mimocode.json" in str(excinfo.value) and ".claude.json" not in str(excinfo.value)
    _assert_raised_without_context(excinfo)
    rig.event("reproject")
    assert rig.client._mimocode_native_mounts == frozenset({"other"})
    assert rig.reclaimed() == [rig.LAUNCHERS["wrap_version"], rig.LAUNCHERS["wrap_readback"]]
    assert rig.client._unasked_shell_tripwire is False


def test_a_native_mount_of_a_server_held_at_the_read_back_refuses_there(
    monkeypatch, tmp_path
) -> None:
    """A native mount whose tool the lost rule names: read-back refuses, no re-projection."""
    readback = _DENY_LOST % ', "mcp": {"docs": {}}'
    rig = _reprojecting_rig(monkeypatch, tmp_path, readback, frozenset({"docs"}))
    with pytest.raises(AcpToolGateUnroutable, match="MiMo Code mounts that server") as excinfo:
        rig.run()
    assert "'docs'" in str(excinfo.value)
    _assert_raised_without_context(excinfo)
    assert "reproject" not in rig.names()
    assert rig.reclaimed() == [rig.LAUNCHERS["wrap_version"], rig.LAUNCHERS["wrap_readback"]]
    assert rig.client._unasked_shell_tripwire is False


# ── governance's mcp scope cannot reach a fused title: refused before a mount ──

_MCP_CEILING = {
    "version": 1,
    "boot": {"fail_closed": True},
    "mcp": {"mode": "deny", "deny": ["@ops/repo.delete"]},
}


@pytest.fixture
def governed_host(monkeypatch, tmp_path):
    """An isolated profile store and platform context; ``install(body)`` sets the ceiling."""
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.governance import parse_policy

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    base = build_default_context(KiroCrewConfig.load())

    def install(body: dict | None) -> None:
        ceiling = parse_policy(body) if body is not None else None
        ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))

    install(None)
    try:
        yield SimpleNamespace(install=install, profiles=profiles, gp=gp)
    finally:
        gp.reset_store()
        ctx_mod.reset_context()


def _mounting_rig(monkeypatch, tmp_path, placed: list[str], native: dict | None = None):
    """A launch whose read-back holds; Crew places *placed*, the harness mounts *native*."""
    readback = (
        _DENY_HELD
        if native is None
        else ('{"permission": {"*": "ask", "docs_search": "deny"}, "mcp": %s}' % json.dumps(native))
    )
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path, readback_stdout=readback)
    rig.client._session_harness_deny_rules = ("docs_search",)
    rig.client._session_mcp_cache = [{"name": name} for name in placed]
    return rig


def test_an_mcp_control_refuses_a_session_that_would_mount_a_server(
    governed_host, monkeypatch, tmp_path
) -> None:
    """A ceiling denying ``@ops/repo.delete`` is a rule the fused title can never meet,
    so a MiMo session that would mount any server is refused, naming the servers."""
    from kiro_crew.platform.governance import mcp_scope_governed_now

    governed_host.install(_MCP_CEILING)
    assert mcp_scope_governed_now() is True
    rig = _mounting_rig(monkeypatch, tmp_path, ["kirocrew-core"], native={"ops": {}})
    with pytest.raises(AcpToolGateUnroutable, match="carries an 'mcp' control") as excinfo:
        rig.run()
    message = str(excinfo.value)
    assert "'kirocrew-core'" in message and "'ops'" in message
    assert "fused '<server>_<tool>' title" in message
    assert "pick another harness" in message
    assert rig.client._unasked_shell_tripwire is not True


def test_an_mcp_control_lets_a_session_with_no_server_start(
    governed_host, monkeypatch, tmp_path
) -> None:
    """With nothing to mount there is no MCP call the control could miss."""
    governed_host.install(_MCP_CEILING)
    rig = _mounting_rig(monkeypatch, tmp_path, [])
    rig.run()
    assert rig.client._unasked_shell_tripwire is True


def test_without_an_mcp_control_a_mounting_session_starts(
    governed_host, monkeypatch, tmp_path
) -> None:
    """Narrows only: a host whose policy says nothing about ``mcp`` is unchanged."""
    from kiro_crew.platform.governance import mcp_scope_governed_now

    governed_host.install({"version": 1, "boot": {"fail_closed": True}})
    assert mcp_scope_governed_now() is False
    rig = _mounting_rig(monkeypatch, tmp_path, ["kirocrew-core"], native={"ops": {}})
    rig.run()
    assert rig.client._unasked_shell_tripwire is True


def test_a_profile_s_mcp_control_counts_without_a_ceiling(governed_host) -> None:
    """Governance is policy ∩ profile: a profile-only ``mcp`` rule is a control too."""
    from kiro_crew.platform.governance import Profile, ScopedRuleset, mcp_scope_governed_now

    gp = governed_host.gp
    assert mcp_scope_governed_now() is False
    narrow = Profile(name="narrow", controls={"mcp": ScopedRuleset(mode="deny", deny=("@x",))})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gp._STORE, "resolved", lambda: True)
        mp.setattr(gp._STORE, "all_profiles", lambda: [narrow])
        assert mcp_scope_governed_now() is True
        # A deny-mode profile with no patterns constrains nothing.
        open_profile = Profile(name="open", controls={"mcp": ScopedRuleset(mode="deny", deny=())})
        mp.setattr(gp._STORE, "all_profiles", lambda: [open_profile])
        assert mcp_scope_governed_now() is False
        # An unresolved store cannot confirm that no profile constrains: governed.
        mp.setattr(gp._STORE, "resolved", lambda: False)
        assert mcp_scope_governed_now() is True
