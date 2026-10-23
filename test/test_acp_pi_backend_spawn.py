"""The pi spawn path at spawn time: where the gate is placed, and proof it runs there.

* **Placement.** ``PiLaunch.resolve_spawn``, driven against fakes at the adapter's
  own seams: the sandbox floor is checked and the read-back runs off the event loop
  before any child starts, and the adapter is told to run the verified launcher.
* **The envelope.** The adapter renders the extension's dialog, not the tool call,
  so the dispatch parser reads the tool call back out of the dialog's message.
* **The read-back for real.** Against the installed ``pi`` when both components
  are present, and against a fake harness on disk, with the gate artifacts still
  reachable inside the sandbox mask; a failed read-back refuses the session.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import acp_pi_backend_helpers
import pytest
from acp_pi_backend_helpers import (
    NONCE,
    PROBE,
    _envelope,
    _permission_frame,
    _PiLaunchRig,
    _registry,
    _response,
)

from conftest import make_dir_link
from kiro_crew import acp_tool_gate, sandbox, security
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp._dispatch import build_permission_event, gate_envelope
from kiro_crew.acp.client import (
    PI_INSTALL_COMMAND,
    AcpClient,
    AcpToolGateUnroutable,
    _ensure_pi_gate_launcher,
    _pi_commands_from_readback,
    _resolve_pi_bin,
    _seal_pi_gate_extension,
    pi_gate_extension_path,
)
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp_backends import (
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKEND_ROUTING,
    Routing,
    routing_for,
)
from kiro_crew.acp_tool_gate import gate_extension_issue
from kiro_crew.config.paths import config_dir
from kiro_crew.instances import run_marker
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_no_ambient_pi_env = acp_pi_backend_helpers._no_ambient_pi_env


# ── Placement ────────────────────────────────────────────────────────────────


def test_the_gate_read_back_runs_off_the_event_loop(monkeypatch, tmp_path) -> None:
    """The read-back starts a child and waits on it, so the loop must not."""
    rig = _PiLaunchRig(monkeypatch, tmp_path)
    rig.run()
    assert rig.ran_off_the_loop("verify"), "the gate read-back ran on the event loop"


def test_the_launcher_write_runs_off_the_event_loop(monkeypatch, tmp_path) -> None:
    rig = _PiLaunchRig(monkeypatch, tmp_path)
    rig.run()
    assert rig.ran_off_the_loop("launcher"), "the launcher write ran on the event loop"
    assert rig.ran_off_the_loop("seal"), "the seal ran on the event loop"


def test_the_sandbox_floor_is_checked_before_any_child_is_started(monkeypatch, tmp_path) -> None:
    """The refuse-then-mask preflight runs, for this harness and tier, before the read-back."""
    rig = _PiLaunchRig(monkeypatch, tmp_path)
    rig.run()
    names = rig.names()
    assert names.index("preflight") < names.index("wrap") < names.index("verify")
    preflight, backend, mode = rig.args_of("preflight")
    assert preflight is launch_mod._sandbox_preflight
    assert (backend, mode) == (ACP_BACKEND_PI, rig.MODE)


def test_the_read_back_child_is_sandbox_wrapped_with_the_adapter_mask(
    monkeypatch, tmp_path
) -> None:
    """The read-back is wrapped like the session, run wrapped, then its launcher removed."""
    rig = _PiLaunchRig(monkeypatch, tmp_path)
    plan = rig.run()
    wrapped, kwargs = rig.args_of("wrap")
    expose = acp_tool_gate.adapter_expose_files(ACP_BACKEND_PI, rig.HIDDEN)
    assert kwargs["mode"] == rig.MODE
    assert kwargs["extra_hidden_dirs"] == rig.HIDDEN
    assert kwargs["extra_expose_files"] == expose
    assert kwargs["strip_python_env"] is True
    assert rig.args_of("verify")[0] == [*rig.SANDBOX, *wrapped], "the read-back ran unwrapped"
    names = rig.names()
    assert rig.args_of("unlink") == (rig.CLEANUP,)
    assert names.index("verify") < names.index("unlink")
    # And the session itself is spawned under the same mask the read-back ran under.
    assert plan.extra_hidden_dirs == rig.HIDDEN
    assert plan.extra_expose_files == expose


def test_the_read_back_runs_exactly_what_the_adapter_will_spawn(monkeypatch, tmp_path) -> None:
    """The launcher plus the adapter's own arguments, so the process asked is the process served."""
    assert acp_client._PI_RPC_ARGS == ("--mode", "rpc", "--no-themes")
    rig = _PiLaunchRig(monkeypatch, tmp_path)
    rig.run()
    wrapped, _kwargs = rig.args_of("wrap")
    assert wrapped == [rig.LAUNCHER, *acp_client._PI_RPC_ARGS]
    assert rig.args_of("launcher") == (rig.PI_BIN, rig.SEALED)


def test_a_routing_refusal_is_translated_to_the_acp_layer_type(monkeypatch, tmp_path) -> None:
    """The gate module's refusal reaches the driver as the ACP layer's own type."""
    rig = _PiLaunchRig(monkeypatch, tmp_path, routing=("not loaded", "load it"))
    with pytest.raises(AcpToolGateUnroutable) as excinfo:
        rig.run()
    assert type(excinfo.value) is AcpToolGateUnroutable
    assert excinfo.value.__suppress_context__, "the gate module's exception leaks as context"
    # Refused AFTER the read-back's launcher was reclaimed, not instead of it.
    assert "unlink" in rig.names()


def test_the_adapter_is_told_to_run_the_launcher(monkeypatch, tmp_path) -> None:
    """The child's environment points the adapter at the verified launcher, with the nonce."""
    assert acp_client._ENV_PI_ACP_PI_COMMAND == "PI_ACP_PI_COMMAND"
    rig = _PiLaunchRig(monkeypatch, tmp_path)
    rig.run()
    env: dict[str, str] = {}
    rig.adapter.apply_spawn_env(env)
    nonce = rig.client._pi_gate_nonce
    assert re.fullmatch(r"[0-9a-f]{32}", nonce), "no nonce was recorded on the session"
    handoff, _payload = rig.client._pi_mcp_handoff
    assert handoff.endswith(f"_mcp_{nonce}.json")
    # The steer channel is the session's own, bound to the same nonce, and its key is
    # a fresh one, not the handoff's.
    steer = rig.client._pi_steer_channel
    assert steer is rig.adapter._steer and steer.nonce == nonce
    assert steer._stem().endswith(f"_steer_{nonce}_")
    assert len(steer.key) == 32 and steer.key != rig.adapter._mcp_key
    assert env == {
        "PI_ACP_PI_COMMAND": rig.LAUNCHER,
        "KIROCREW_PI_GATE_SESSION": nonce,
        "KIROCREW_PI_MCP_HANDOFF": handoff,
        "KIROCREW_PI_MCP_KEY": rig.adapter._mcp_key.hex(),
        "KIROCREW_PI_STEER": steer._stem(),
        "KIROCREW_PI_STEER_KEY": steer.key.hex(),
    }
    assert len(rig.adapter._mcp_key) == 32


def test_no_launcher_is_named_before_the_gate_is_verified() -> None:
    """An adapter that never resolved a plan sets nothing: no launcher, no nonce."""
    env: dict[str, str] = {"PATH": "/opt/bin"}
    pi_mod.PiLaunch().apply_spawn_env(env)
    assert env == {"PATH": "/opt/bin"}


def test_only_the_gated_session_hands_the_parser_a_nonce(tmp_path) -> None:
    """A session on any other harness passes ``None``, so no frame of its is an envelope."""
    source = inspect.getsource(AcpClient._build_permission_event)
    assert "gate_envelope_nonce=_gate_nonce or None" in source
    # Read compat-shaped, like the provenance caches beside it: an instance built
    # without ``__init__`` has no nonce, and no nonce trusts no envelope.
    assert '_gate_nonce = getattr(self, "_pi_gate_nonce", "")' in source
    for backend in (ACP_BACKEND_OPENCODE, ACP_BACKEND_PI):
        client = AcpClient(work_dir=tmp_path, acp_backend=backend)
        assert (
            client._pi_gate_nonce == ""
        ), "the nonce is minted in the spawn arm, not at construction"


def test_the_backend_declares_the_verified_mechanism() -> None:
    from kiro_crew.agent_sdk import tool_gate

    assert routing_for(ACP_BACKEND_PI) is Routing.VERIFIED_GATE_EXTENSION
    assert tool_gate.is_enforced(ACP_BACKEND_PI)
    verdict, reason = tool_gate.routing_verdict(ACP_BACKEND_PI)
    assert verdict is tool_gate.Verdict.ROUTED and PROBE in reason
    assert PROBE in tool_gate.remediation_for(ACP_BACKEND_PI)


# ── The envelope ─────────────────────────────────────────────────────────────


def _build(frame, nonce=NONCE):
    return build_permission_event(frame, gate_envelope_nonce=nonce)


class TestGateEnvelope:
    def test_crews_dialog_is_read_as_the_tool_call_it_asks_about(self):
        event, recorded = _build(_permission_frame(_envelope()))
        assert event.title == "bash"
        assert event.tool_kind == "execute"
        assert event.tool_call_id == "call_v0prcvqi"
        assert event.is_shell is True and event.shell_classified is True
        assert event.raw_tool_params == {"command": "echo gate-check"}
        assert event.raw_params_trusted is True
        assert "echo gate-check" in event.tool_input
        assert recorded == {"once": "yes", "always": "yes", "reject": "no"}

    def test_a_read_is_not_shell(self):
        event, _ = _build(
            _permission_frame(
                _envelope(tool="read", kind="read", input={"path": "a.txt"}), title="read"
            )
        )
        assert event.is_shell is False and event.shell_classified is True
        assert event.raw_tool_params == {"path": "a.txt"}

    def test_a_truncated_envelope_keeps_its_keys_but_earns_no_durable_trust(self):
        """A cut file body still carries ``path``, so the path checks judge it."""
        cut = {"path": "~/.kiro/crew/config.json", "content": "x" * 40 + "…[9000 chars omitted]"}
        event, _ = _build(
            _permission_frame(
                _envelope(tool="write", kind="edit", input=cut, truncated=True), title="write"
            )
        )
        assert event.raw_tool_params == cut
        assert event.raw_params_trusted is False
        assert "config.json" in event.tool_input

    def test_another_extensions_dialog_is_left_alone(self):
        event, _ = _build(_permission_frame("Allow rm -rf?", title="Dangerous!"))
        assert event.title == "Dangerous!"
        assert event.tool_kind == "other"
        assert event.tool_call_id == "pi-ui-5646e0bd"
        assert event.raw_params_trusted is False

    def test_a_session_without_a_gate_extension_never_reads_an_envelope(self):
        """The forgery every other harness would otherwise be open to.

        On claude, codex and opencode a permission frame's ``rawInput`` IS the
        model's tool arguments, so a model could call any tool with
        ``{method: "confirm", message: <envelope>}`` and have the gate judge the
        benign call it described. Those sessions pass no nonce, and no nonce means
        the envelope is never consulted.
        """
        event, _ = build_permission_event(_permission_frame(_envelope()))
        assert event.title == "bash" and event.tool_kind == "other"
        assert event.tool_call_id == "pi-ui-5646e0bd"
        assert event.raw_tool_params is None and event.raw_params_trusted is False
        assert event.shell_classified is False

    def test_an_envelope_with_another_sessions_nonce_is_a_dialog(self):
        event, _ = _build(_permission_frame(_envelope(nonce="0" * 32)))
        assert event.tool_call_id == "pi-ui-5646e0bd" and event.raw_tool_params is None

    def test_an_envelope_whose_tool_is_not_the_dialogs_title_is_a_dialog(self):
        """Another extension relaying model text controls the message, not the title."""
        event, _ = _build(_permission_frame(_envelope(tool="read", kind="read"), title="ask_user"))
        assert event.title == "ask_user" and event.raw_tool_params is None

    @pytest.mark.parametrize(
        "message",
        [
            "kiro-crew-gate",
            '{"kiro-crew-gate": 2, "tool": "bash"}',
            '["kiro-crew-gate"]',
            '{"kiro-crew-gate": 1, "tool": 5}',
        ],
    )
    def test_a_message_that_is_not_the_envelope_is_none(self, message):
        raw = {"method": "confirm", "title": "bash", "message": message}
        assert gate_envelope({"rawInput": raw}, NONCE) is None

    def test_a_non_confirm_dialog_is_none(self):
        raw = {"method": "select", "title": "bash", "message": _envelope()}
        assert gate_envelope({"rawInput": raw}, NONCE) is None
        assert gate_envelope({"rawInput": None}, NONCE) is None
        raw = {"method": "confirm", "title": "bash", "message": _envelope()}
        assert gate_envelope({"rawInput": raw}, None) is None
        assert gate_envelope({"rawInput": raw}, "") is None


# ── Live, when both components are installed ────────────────────────────────


def _pi_installed() -> bool:
    return bool(shutil.which("pi")) and bool(shutil.which("pi-acp"))


@pytest.mark.skipif(not _pi_installed(), reason="pi and pi-acp are not installed on this host")
def test_live_the_shipped_extension_loads_and_the_read_back_sees_it(tmp_path, monkeypatch):
    """Real ``pi``, real launcher, real registry: the whole read-back chain, unwrapped.

    Run under a scratch agent directory so nothing of the operator's is read or
    written, and offline so no update check runs. The negative half plants a copy
    of the extension elsewhere and requires the read-back to refuse it: same probe
    name, wrong file.
    """
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", lambda: str(run_dir))
    monkeypatch.setattr(pi_mod, "_pi_gate_launcher_cache", {})
    pi_bin, _searched = _resolve_pi_bin()
    assert pi_bin
    extension = pi_gate_extension_path()
    launcher = _ensure_pi_gate_launcher(pi_bin, extension)
    # The operator's own environment, as the spawn would carry it (a mise-managed
    # ``pi`` needs its HOME to resolve), with only pi's agent directory redirected.
    env = {
        **os.environ,
        "PATH": acp_client.augmented_path(os.environ.get("PATH", "")),
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "PI_OFFLINE": "1",
        # pi compiles the .ts extension through a loader that caches under the
        # temp directory; keep that residue inside this test's own tree.
        "TMPDIR": str(tmp_path),
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
    }
    completed = subprocess.run(
        [launcher, *acp_client._PI_RPC_ARGS],
        cwd=str(tmp_path),
        env=env,
        input=json.dumps(acp_client._PI_READBACK_REQUEST) + "\n",
        capture_output=True,
        timeout=60,
        **UTF8_TEXT,
    )
    commands = _pi_commands_from_readback(completed.stdout)
    assert commands is not None, completed.stderr[-2000:]
    assert gate_extension_issue(ACP_BACKEND_PI, commands, extension) == ""

    # Mutation: the same file under another name is refused as not Crew's.
    elsewhere = tmp_path / "mine.ts"
    elsewhere.write_text(Path(extension).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(pi_mod, "_pi_gate_launcher_cache", {})
    other = _ensure_pi_gate_launcher(pi_bin, str(elsewhere))
    completed = subprocess.run(
        [other, *acp_client._PI_RPC_ARGS],
        cwd=str(tmp_path),
        env=env,
        input=json.dumps(acp_client._PI_READBACK_REQUEST) + "\n",
        capture_output=True,
        timeout=60,
        **UTF8_TEXT,
    )
    commands = _pi_commands_from_readback(completed.stdout)
    assert commands is not None
    assert gate_extension_issue(ACP_BACKEND_PI, commands, extension)


# ── The gate artifacts survive the sandbox mask ───────────────────────────────


class TestTheGateArtifactsStayReachableInsideTheSandbox:
    """The credential-bearing run directory stays hidden while pi's gate can run."""

    def _hidden(self, backend: str = ACP_BACKEND_PI) -> tuple[str, ...]:
        return acp_tool_gate.adapter_hidden_credential_dirs(backend)

    def _run_dir(self) -> str:
        return os.path.normpath(str(config_dir() / "run"))

    def _artifact_dir(self) -> str:
        return os.path.normpath(str(config_dir() / "pi-gate"))

    def _launcher_lists(self, monkeypatch: pytest.MonkeyPatch) -> tuple[list, list]:
        """The masked and the read-only lists the Linux launcher is handed for pi."""
        # A namespace plan asks the host's ``ssh -V`` (once per process, cached) for the
        # accept-new flag. Which flag the launcher carries is not what these lists are
        # about, so the probe is pinned rather than run: no host binary, no cache-order
        # dependence on which test in the worker got there first.
        monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)
        hidden = self._hidden()
        plan = sandbox._spawn_plan(
            "namespace",
            "standard",
            strip_python_env=True,
            extra_hidden_dirs=hidden,
            extra_expose_files=acp_tool_gate.adapter_expose_files(ACP_BACKEND_PI, hidden),
        )
        return list(plan.sensitive_dirs), list(plan.readonly)

    def _is_masked(self, path: str, targets: tuple[str, ...] | list[str]) -> bool:
        normalized = os.path.normpath(path)
        return any(
            os.path.commonpath((normalized, os.path.normpath(target))) == os.path.normpath(target)
            for target in targets
        )

    def test_pi_gate_is_excluded_from_the_child_mask_but_remains_on_the_floor(self):
        hidden = self._hidden()
        assert not any(Path(path).name == "pi-gate" for path in hidden)
        assert any(Path(leaf).name == "pi-gate" for leaf in security.sensitive_home_dirs())

    def test_pi_mask_keeps_run_and_the_gateway_secret_parent_hidden(self):
        hidden = self._hidden()
        normalized = {os.path.normpath(path) for path in hidden}
        assert self._run_dir() in normalized
        credential_parent = str(run_marker.secret_path(32145).parent)
        assert self._is_masked(credential_parent, hidden)

    def test_gate_artifact_exclusion_is_per_backend(self):
        backend = next(
            backend
            for backend, routing in ACP_BACKEND_ROUTING.items()
            if backend != ACP_BACKEND_PI and routing in acp_tool_gate.ENFORCED_ROUTINGS
        )
        normalized = {os.path.normpath(path) for path in self._hidden(backend)}
        assert self._artifact_dir() in normalized

    def test_gate_artifact_leaf_is_created_and_sealed_on_the_shared_walk(self):
        """The leaf is materialized and sealed like every other governance ceiling.

        It has to be: ``mount(2)`` cannot seal an absent path, so a leaf left off the
        precreate list stays WRITABLE in the sandbox on every install that has not run
        pi yet -- which is the ordinary install. The nofollow list matters for the same
        reason it matters for ``playwright-cli``: the gateway later execs out of this
        name, so the mounted name must stay the real directory.
        """
        assert "pi-gate" in sandbox._CREW_READONLY_LEAVES
        assert "pi-gate" in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
        assert "pi-gate" in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
        assert set(sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES) <= set(
            sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
        )

    @pytest.mark.parametrize("leaf_name", ["pi-gate", "playwright-cli"])
    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink creation")
    def test_a_squat_refuses_the_spawn_with_no_per_leaf_exception(
        self, monkeypatch, tmp_path, leaf_name
    ):
        """The shared walk carries no per-adapter branch: both leaves refuse alike.

        Parametrized over the pi leaf and a pre-existing one so a later exemption for
        either has to change this test rather than pass quietly. ``pi-gate`` sharing the
        seam is the point: the walk stays one code path for every backend.
        """
        leaf = tmp_path / leaf_name
        leaf.symlink_to(tmp_path / "nowhere")
        monkeypatch.setattr(sandbox, "_sealable_absent_ceilings", lambda: ([str(leaf)], []))
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

    @pytest.mark.parametrize(
        "squat",
        ["dangling-symlink", "symlink-to-dir", "regular-file"],
    )
    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink creation")
    def test_the_resolver_refuses_every_squat_the_shared_walk_used_to_catch(
        self, monkeypatch, tmp_path, squat
    ):
        """The checks moved to the resolver, so the resolver must still make them.

        These are the three states ``_refuse_if_dangling_symlink``,
        ``_refuse_if_symlink_leaf`` and ``_require_real_dir_nofollow`` covered while the
        leaf sat on the shared lists. Each one must refuse the session here instead.
        """
        monkeypatch.setattr(pi_mod, "config_dir", lambda: tmp_path)
        leaf = tmp_path / "pi-gate"
        if squat == "dangling-symlink":
            leaf.symlink_to(tmp_path / "nowhere")
        elif squat == "symlink-to-dir":
            elsewhere = tmp_path / "elsewhere"
            elsewhere.mkdir()
            leaf.symlink_to(elsewhere, target_is_directory=True)
        else:
            leaf.write_text("not a directory", encoding="utf-8")
        with pytest.raises(AcpToolGateUnroutable):
            pi_mod._pi_gate_artifact_dir()

    def test_the_resolver_tightens_a_loose_preexisting_leaf(self, monkeypatch, tmp_path):
        """A real directory left group-readable is narrowed to owner-only, not refused.

        ``0o750`` rather than a wider mode on purpose: what is under test is that the
        resolver removes access it did not grant, and one group bit proves that as well
        as seven bits would while keeping the fixture off the insecure-permissions rule.
        """
        monkeypatch.setattr(pi_mod, "config_dir", lambda: tmp_path)
        leaf = tmp_path / "pi-gate"
        leaf.mkdir(mode=0o750)
        os.chmod(leaf, 0o750)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- a deliberately LOOSE fixture: what is under test is that the resolver NARROWS a pre-existing directory to owner-only, so it has to start wider than 0o700. One group-read bit under tmp_path, never published. lockdown-ok.  # noqa: E501  # fmt: skip
        assert stat.S_IMODE(leaf.stat().st_mode) != 0o700, "the fixture must start loose"
        assert Path(pi_mod._pi_gate_artifact_dir()) == leaf
        if not acp_client.platform_compat.IS_WINDOWS:
            assert stat.S_IMODE(leaf.stat().st_mode) == 0o700

    @pytest.mark.skipif(
        not acp_client.platform_compat.IS_POSIX,
        reason="the namespace plan requires os.getuid",
    )
    def test_linux_launcher_masks_run_and_voice_but_exposes_and_seals_gate_artifacts(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        masked, readonly = self._launcher_lists(monkeypatch)
        masked_set = {os.path.normpath(path) for path in masked}
        readonly_set = {os.path.normpath(path) for path in readonly}
        assert self._run_dir() in masked_set
        assert self._artifact_dir() not in masked_set
        assert self._artifact_dir() in readonly_set
        voice_runtime = {os.path.normpath(path) for path in sandbox._voice_runtime_sandbox_paths()}
        assert voice_runtime and voice_runtime <= masked_set

    @pytest.mark.skipif(
        not acp_client.platform_compat.IS_POSIX,
        reason="_build_seatbelt_profile shares launcher state that requires os.getuid",
    )
    def test_seatbelt_masks_run_but_exposes_and_seals_gate_artifacts(self):
        profile = sandbox._build_seatbelt_profile("standard", extra_hidden_dirs=self._hidden())
        assert f'(deny file-read* (subpath "{self._run_dir()}"))' in profile
        assert f'(deny file-read* (subpath "{self._artifact_dir()}"))' not in profile
        assert f'(deny file-write* (subpath "{self._artifact_dir()}"))' in profile

    def test_both_gate_artifacts_use_the_strict_artifact_resolver(self, monkeypatch, tmp_path):
        artifact_dir = tmp_path / "pi-gate"
        artifact_dir.mkdir()
        monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", lambda: str(artifact_dir))
        monkeypatch.setattr(pi_mod, "_pi_gate_launcher_cache", {})
        sealed = _seal_pi_gate_extension()
        launcher = _ensure_pi_gate_launcher("/usr/bin/pi", sealed)
        assert Path(sealed).parent == artifact_dir
        assert Path(launcher).parent == artifact_dir

    def test_every_file_written_to_the_artifact_leaf_is_swept(self, monkeypatch, tmp_path):
        """The leaf's invariant is a ratchet, not a comment.

        The leaf is excluded from the pi child's OS mask, so it is the one directory
        under the data home that an enforced harness can read. That is safe only while
        nothing but Crew's own gate artifacts lands there. A comment saying so is what
        this change's own pattern harvest calls the defect class, so the property is
        asserted: every name the writers produce is matched by the sweep family, which
        means a future writer dropping a differently-named file fails here rather than
        leaving an unswept, child-readable file behind.
        """
        artifact_dir = tmp_path / "pi-gate"
        artifact_dir.mkdir()
        monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", lambda: str(artifact_dir))
        monkeypatch.setattr(pi_mod, "_pi_gate_launcher_cache", {})
        sealed = _seal_pi_gate_extension()
        _ensure_pi_gate_launcher("/usr/bin/pi", sealed)
        families = sandbox._PI_GATE_DIR_ARTIFACTS
        written = sorted(entry.name for entry in artifact_dir.iterdir())
        assert written, "the writers produced nothing to check"
        for name in written:
            prefix = next((p for p in families if name.startswith(p)), None)
            assert (
                prefix is not None
            ), f"{name} is written to the leaf but no sweep family claims it"
            assert any(
                name.endswith(suffix) for suffix in families[prefix]
            ), f"{name} carries a suffix the sweep family does not reclaim"

    def test_the_strict_artifact_resolver_uses_the_dedicated_owner_only_leaf(
        self, monkeypatch, tmp_path
    ):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        monkeypatch.setattr(pi_mod, "config_dir", lambda: cfg)
        artifact_dir = Path(pi_mod._pi_gate_artifact_dir())
        assert artifact_dir == cfg / "pi-gate"
        if not acp_client.platform_compat.IS_WINDOWS:
            assert stat.S_IMODE(artifact_dir.stat().st_mode) == 0o700

    def test_stale_pi_gate_artifacts_are_swept_from_the_dedicated_directory(
        self, monkeypatch, tmp_path
    ):
        artifact_dir = tmp_path / "pi-gate"
        artifact_dir.mkdir()
        stale = artifact_dir / "kirocrew_pi_gate_999999_gate.ts"
        stale.write_text("gate", encoding="utf-8")
        monkeypatch.setattr(sandbox.platform_compat, "pid_exists", lambda _pid: False)
        removed = sandbox.cleanup_stale_sandbox_profiles(
            data_home=tmp_path, legacy_dir=str(tmp_path / "absent")
        )
        assert removed == 1
        assert not stale.exists()

    def test_sweep_refuses_a_linked_pi_gate_artifact_directory(self, monkeypatch, tmp_path):
        target = tmp_path / "outside-pi-gate"
        target.mkdir()
        stale = target / "kirocrew_pi_gate_999999_gate.ts"
        stale.write_text("gate", encoding="utf-8")
        make_dir_link(tmp_path / "pi-gate", target)
        monkeypatch.setattr(sandbox.platform_compat, "pid_exists", lambda _pid: False)
        removed = sandbox.cleanup_stale_sandbox_profiles(
            data_home=tmp_path, legacy_dir=str(tmp_path / "absent")
        )
        assert removed == 0
        assert stale.exists()

    def test_sweep_refuses_a_linked_run_artifact_directory(self, monkeypatch, tmp_path):
        target = tmp_path / "outside-run"
        target.mkdir()
        stale = target / "kirocrew_sandbox_999999.py"
        stale.write_text("launcher", encoding="utf-8")
        make_dir_link(tmp_path / "run", target)
        monkeypatch.setattr(sandbox.platform_compat, "pid_exists", lambda _pid: False)
        removed = sandbox.cleanup_stale_sandbox_profiles(
            data_home=tmp_path, legacy_dir=str(tmp_path / "absent")
        )
        assert removed == 0
        assert stale.exists()


# ── The read-back against a real child ───────────────────────────────────────


class TestTheReadBackAgainstARealChild:
    """A fake harness on disk, so the parse, the status and the refusal are all real."""

    EXT = "/site/gate.ts"

    def _fake(self, tmp_path: Path, body: str) -> list[str]:
        script = tmp_path / "fake_pi.sh"
        script.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        script.chmod(0o700)
        return [str(script), *acp_client._PI_RPC_ARGS]

    def _verify(self, tmp_path: Path, argv: list[str], extension_path: str = "") -> tuple:
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        return pi_mod._verify_pi_gate(client, ACP_BACKEND_PI, argv, extension_path or self.EXT)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX shell launcher")
    def test_a_launcher_the_os_will_not_run_reports_the_bare_exit(self, tmp_path):
        """A silent child reports only its exit status."""
        argv = self._fake(tmp_path, "exec 2>/dev/null\nexit 126\n")
        issue, remedy = self._verify(tmp_path, argv)
        assert "the harness's command registry could not be read back" in issue
        assert issue.endswith("(exit 126)")
        assert PI_INSTALL_COMMAND in remedy

    @pytest.mark.skipif(os.name == "nt", reason="POSIX shell launcher")
    def test_a_registry_carrying_crews_probe_is_no_issue(self, tmp_path):
        sealed = str(tmp_path / "gate.ts")
        Path(sealed).write_text("// gate\n")
        payload = _response(_registry((PROBE, sealed))).replace("'", "'\\''")
        argv = self._fake(tmp_path, f"cat >/dev/null\nprintf '%s\\n' '{payload}'\n")
        assert self._verify(tmp_path, argv, sealed) == ("", "")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX shell launcher")
    def test_a_response_shape_this_gateway_cannot_read_is_refused(self, tmp_path):
        """Protocol drift reads as "not established", never as "loaded"."""
        drifted = json.dumps(
            {
                "id": "kiro-crew-gate-readback",
                "type": "response",
                "command": "get_commands",
                "success": True,
                "data": {"slashCommands": [{"name": PROBE}]},
            }
        ).replace("'", "'\\''")
        argv = self._fake(tmp_path, f"cat >/dev/null\nprintf '%s\\n' '{drifted}'\n")
        issue, remedy = self._verify(tmp_path, argv)
        assert "could not be read back" in issue
        assert "no response" in issue
        assert PI_INSTALL_COMMAND in remedy


# ── The refusal is a refusal ─────────────────────────────────────────────────


class TestAReadBackFailureRefusesTheSession:
    """Not a warning, and not a config the operator can switch off."""

    def test_this_harness_is_enforced(self):
        assert acp_tool_gate.is_enforced(ACP_BACKEND_PI)
        assert routing_for(ACP_BACKEND_PI) is Routing.VERIFIED_GATE_EXTENSION

    def test_the_readback_issue_raises_rather_than_returning(self):
        with pytest.raises(acp_tool_gate.ToolGateUnroutable) as excinfo:
            acp_tool_gate.enforce_runtime_routing(
                ACP_BACKEND_PI,
                "the harness's command registry could not be read back (exit 126)",
                remedy="reinstall it",
            )
        message = str(excinfo.value)
        assert "would not reach Kiro Crew's security gate" in message
        assert acp_tool_gate.UNENFORCED_CONTROLS in message
        assert "reinstall it" in message

    @pytest.mark.parametrize(
        "issue",
        [
            "the harness's command registry could not be read back (exit 126)",
            f"{PROBE} is not registered",
        ],
    )
    def test_the_arm_raises_on_any_routing_issue_before_the_first_prompt(
        self, monkeypatch, tmp_path, issue
    ):
        """Any read-back issue ends the launch with a refusal; no plan is returned."""
        rig = _PiLaunchRig(monkeypatch, tmp_path, routing=(issue, "the remedy"))
        with pytest.raises(AcpToolGateUnroutable) as excinfo:
            rig.run()
        assert "the remedy" in str(excinfo.value)
        assert rig.plan is None
