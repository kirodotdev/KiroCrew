"""The pi spawn path: two components, a gate loaded INTO the harness, and its proof.

Three things are pinned here, and they fail apart:

* **Resolution.** Two components resolved separately -- the ``pi-acp`` adapter on
  the Node-entry ladder and the ``pi`` agent on the plain-binary ladder -- so the
  not-found message names the half that is missing.
* **The gate launcher.** pi has no permission gate to configure, so Kiro Crew's own
  extension is loaded into it through a launcher the adapter is told to run in
  place of ``pi``. The launcher forwards every argument and appends the extension
  flag; nothing is written into the work dir or the operator's pi directory.
* **The read-back.** What makes this routing VERIFIED rather than declared: pi's
  own command registry is asked, off the event loop and before the first prompt,
  whether the probe command loaded from Crew's file.

The read-back's refusal vocabulary is exercised through
``acp_tool_gate.gate_extension_issue``, which owns it, rather than by asserting on
message wording at the call site.

Where the gate is placed at spawn, the envelope, and the read-back against a real
child are in ``test_acp_pi_backend_spawn.py``; a session's reasoning-effort channel,
its model pin, cancel and the signed-out error in ``test_acp_pi_backend_session.py``.
``acp_pi_backend_helpers.py`` holds what the three share.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import stat
from pathlib import Path

import acp_pi_backend_helpers
import pytest
from acp_pi_backend_helpers import (
    _ENV_PI_ACP_BIN,
    _ENV_PI_COMMAND,
    NONCE,
    PROBE,
    ROOT,
    _envelope,
    _permission_frame,
    _PiLaunchRig,
    _registry,
    _response,
)

from conftest import make_dir_link
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp._dispatch import GATE_ENVELOPE_MARKER
from kiro_crew.acp.client import (
    _READBACK_FAULT_MAX_SHAPES,
    _READBACK_FAULT_SHAPES,
    _READBACK_STDERR_SCAN_CHARS,
    PI_ACP_BIN,
    PI_BIN,
    PI_GATE_EXTENSION_SHA256,
    PI_INSTALL_COMMAND,
    PROTOCOL_VERSION_PI,
    AcpClient,
    AcpToolGateUnroutable,
    PiGateExtensionTampered,
    _ensure_pi_gate_launcher,
    _pi_commands_from_readback,
    _pi_gate_launcher_body,
    _readback_detail_with_diagnosis,
    _readback_stderr_diagnosis,
    _resolve_pi_acp_bin,
    _resolve_pi_bin,
    _seal_pi_gate_extension,
    pi_gate_extension_path,
)
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import (
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)
from kiro_crew.acp_tool_gate import gate_extension_issue
from kiro_crew.agent_sdk.drivers.acp import forget_cached_resolution

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_no_ambient_pi_env = acp_pi_backend_helpers._no_ambient_pi_env


@pytest.fixture
def fresh_pi_resolution():
    """pi's two resolutions start unresolved and are left unresolved.

    A test that injects a resolver must see it called, and must not leave its fake
    answer cached for the next test in the worker. The reset is the one the
    dashboard's re-check button uses, so the cache's shape stays the adapter's own.
    """
    forget_cached_resolution(ACP_BACKEND_PI)
    yield
    forget_cached_resolution(ACP_BACKEND_PI)


def _inject_pi_resolvers(monkeypatch, pi_bin: str) -> None:
    """Answer both of pi's resolvers at the adapter that owns them."""
    monkeypatch.setattr(pi_mod, "_resolve_pi_acp_bin", lambda: (["node", "pi-acp.js"], ""))
    monkeypatch.setattr(pi_mod, "_resolve_pi_bin", lambda: (pi_bin, ""))


# ── Resolution ───────────────────────────────────────────────────────────────


class TestPiResolutionLadder:
    """The agent's ladder: the ADAPTER'S override variable, then mise, then PATH."""

    def test_an_executable_override_wins(self, monkeypatch, tmp_path):
        binary = tmp_path / "pi"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(acp_client.platform_compat, "is_executable_file", lambda _p: True)
        monkeypatch.setenv(_ENV_PI_COMMAND, str(binary))
        monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: str(tmp_path / "never"))
        resolved, _searched = _resolve_pi_bin()
        assert resolved == (acp_client._normalize_exe_casing(str(binary)) or str(binary))

    def test_a_bare_override_name_is_looked_up_on_path(self, monkeypatch, tmp_path):
        """The adapter accepts a bare command name there, so this resolver does too."""
        monkeypatch.setattr(acp_client.platform_compat, "is_executable_file", lambda _p: False)
        monkeypatch.setenv(_ENV_PI_COMMAND, "my-pi")
        monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: None)
        target = str(tmp_path / "my-pi")
        monkeypatch.setattr(
            acp_client.shutil, "which", lambda name, path=None: target if name == "my-pi" else None
        )
        resolved, _searched = _resolve_pi_bin()
        assert resolved == (acp_client._normalize_exe_casing(target) or target)

    def test_mise_is_consulted_before_path(self, monkeypatch, tmp_path):
        mise_path = str(tmp_path / "mise" / "pi")
        monkeypatch.setattr(
            acp_client, "_mise_which", lambda tool: mise_path if tool == PI_BIN else None
        )
        monkeypatch.setattr(
            acp_client.shutil, "which", lambda *_a, **_kw: str(tmp_path / "path" / "pi")
        )
        resolved, _searched = _resolve_pi_bin()
        assert resolved == mise_path

    def test_absent_reports_the_path_it_searched(self, monkeypatch):
        monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: None)
        monkeypatch.setattr(acp_client.shutil, "which", lambda *_a, **_kw: None)
        resolved, searched = _resolve_pi_bin()
        assert resolved is None
        assert isinstance(searched, str)


class TestPiAcpResolutionLadder:
    """The adapter's ladder is codex's, so a Node entry script comes back with node."""

    def test_an_override_script_resolves_with_node(self, monkeypatch, tmp_path):
        script = tmp_path / "index.js"
        script.write_text("", encoding="utf-8")
        node = str(tmp_path / "node")
        monkeypatch.setenv(_ENV_PI_ACP_BIN, str(script))
        monkeypatch.setattr(acp_client, "_vendored_acp_roots", lambda *_a, **_kw: [])
        monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: None)
        monkeypatch.setattr(acp_client, "_resolve_node_for_script", lambda _s: node)
        argv, _searched = _resolve_pi_acp_bin()
        assert argv == [node, str(script.resolve())]

    def test_absent_reports_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(acp_client, "_vendored_acp_roots", lambda *_a, **_kw: [])
        monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: None)
        monkeypatch.setattr(acp_client, "_mise_node_installs_dir", lambda: tmp_path / "absent")
        monkeypatch.setattr(acp_client.shutil, "which", lambda *_a, **_kw: None)
        argv, _searched = _resolve_pi_acp_bin()
        assert argv is None

    def test_the_install_command_names_both_components(self):
        assert PI_ACP_BIN in PI_INSTALL_COMMAND
        assert "pi-coding-agent" in PI_INSTALL_COMMAND


def _npm_install(prefix: Path, package: str, version: str, entry: str) -> Path:
    """An npm global install of *package*: the package dir and the ``pi`` bin link to it."""
    package_dir = prefix / "lib" / "node_modules" / Path(*package.split("/"))
    script = package_dir / Path(*entry.split("/"))
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    (package_dir / "package.json").write_text(
        json.dumps({"name": package, "version": version, "bin": {"pi": entry}}),
        encoding="utf-8",
    )
    return script


class TestATooOldPiIsRefusedByName:
    """pi-acp cannot drive an old pi, and without this check the chat just spins.

    Measured, not assumed: pi-acp 0.0.34 fails ``session/new`` on pi 0.80.x and
    waits forever on 0.73.1 (the last release under the old npm name), and pi-acp
    0.0.33 never ends a turn on 0.80.3. The gate read-back passes on every one of
    them, so nothing else names the cause.
    """

    def _linked_bin(self, tmp_path, package, version, entry="dist/cli.js"):
        """The ``pi`` bin the way a global npm install lays it out on THIS platform.

        POSIX: a symlink in ``bin`` into the package. Windows: a ``pi.cmd`` shim in
        the prefix, with the package under the prefix's ``node_modules``.
        """
        if acp_client.platform_compat.IS_WINDOWS:
            package_dir = tmp_path / "node_modules" / Path(*package.split("/"))
            package_dir.mkdir(parents=True)
            (package_dir / "package.json").write_text(
                json.dumps({"name": package, "version": version}), encoding="utf-8"
            )
            shim = tmp_path / "pi.cmd"
            shim.write_text("@echo off\r\n", encoding="utf-8")
            return str(shim)
        script = _npm_install(tmp_path, package, version, entry)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        link = bin_dir / "pi"
        link.symlink_to(script)
        return str(link)

    def test_the_version_is_read_through_the_npm_bin_link(self, tmp_path):
        pi_bin = self._linked_bin(tmp_path, acp_client.PI_NPM_PKG, "0.87.1", "dist/bundle/cli.js")
        assert acp_client._pi_installed_version(pi_bin) == ((0, 87, 1), acp_client.PI_NPM_PKG)

    def test_the_windows_shim_finds_the_package_under_its_node_modules(self, tmp_path):
        package_dir = tmp_path / "node_modules" / "@earendil-works" / "pi-coding-agent"
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text(
            json.dumps({"name": acp_client.PI_NPM_PKG, "version": "0.80.5"}), encoding="utf-8"
        )
        shim = tmp_path / "pi.cmd"
        shim.write_text("@echo off\r\n", encoding="utf-8")
        assert acp_client._pi_installed_version(str(shim)) == ((0, 80, 5), acp_client.PI_NPM_PKG)

    def test_a_project_local_windows_shim_finds_the_package_beside_bin(self, tmp_path):
        """``node_modules/.bin/pi.cmd``: the package is a sibling of ``.bin``, not below it."""
        modules = tmp_path / "project" / "node_modules"
        (modules / "@mariozechner" / "pi-coding-agent").mkdir(parents=True)
        (modules / "@mariozechner" / "pi-coding-agent" / "package.json").write_text(
            json.dumps({"name": "@mariozechner/pi-coding-agent", "version": "0.73.1"}),
            encoding="utf-8",
        )
        (modules / ".bin").mkdir()
        (modules / ".bin" / "pi.cmd").write_text("@echo off\r\n", encoding="utf-8")
        assert "0.73.1" in acp_client._pi_version_issue(str(modules / ".bin" / "pi.cmd"))

    def test_a_manifest_of_another_package_is_not_read_as_pis(self, tmp_path):
        pi_bin = self._linked_bin(tmp_path, "some-other-cli", "0.1.0")
        assert acp_client._pi_installed_version(pi_bin) is None
        assert acp_client._pi_version_issue(pi_bin) == ""

    def test_an_unknown_install_is_let_through(self, tmp_path):
        wrapper = tmp_path / "pi"
        wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
        assert acp_client._pi_installed_version(str(wrapper)) is None
        assert acp_client._pi_version_issue(str(wrapper)) == ""

    def test_the_floor_itself_passes(self, tmp_path):
        floor = ".".join(str(part) for part in acp_client.PI_MIN_VERSION)
        pi_bin = self._linked_bin(tmp_path, acp_client.PI_NPM_PKG, floor)
        assert acp_client._pi_version_issue(pi_bin) == ""

    def test_a_release_below_the_floor_names_the_upgrade(self, tmp_path):
        pi_bin = self._linked_bin(tmp_path, acp_client.PI_NPM_PKG, "0.80.5")
        issue = acp_client._pi_version_issue(pi_bin)
        assert "0.80.5" in issue and "0.99.2" in issue
        assert f"npm i -g {acp_client.PI_NPM_PKG}" in issue
        assert "npm rm" not in issue

    def test_a_release_before_the_mcp_tool_spelling_is_below_the_floor(self, tmp_path):
        """0.99.0 added built-in MCP; 0.99.2 is the release whose MCP tool names the
        gate's shadow refusal matches, so a pi below it would hold no usable Crew tools."""
        assert acp_client.PI_MIN_VERSION == (0, 99, 2)
        pi_bin = self._linked_bin(tmp_path, acp_client.PI_NPM_PKG, "0.99.1")
        issue = acp_client._pi_version_issue(pi_bin)
        assert "0.99.1" in issue and "0.99.2" in issue

    def test_the_old_package_name_is_removed_first(self, tmp_path):
        """Both names install a ``pi`` bin, so npm refuses the new one until the old one goes."""
        pi_bin = self._linked_bin(tmp_path, "@mariozechner/pi-coding-agent", "0.73.1")
        issue = acp_client._pi_version_issue(pi_bin)
        assert "0.73.1" in issue
        assert issue.index("npm rm -g @mariozechner/pi-coding-agent") < issue.index(
            f"npm i -g {acp_client.PI_NPM_PKG}"
        )

    def test_the_spawn_refuses_before_any_child_starts(
        self, tmp_path, monkeypatch, fresh_pi_resolution
    ):
        pi_bin = self._linked_bin(tmp_path, acp_client.PI_NPM_PKG, "0.80.5")
        _inject_pi_resolvers(monkeypatch, pi_bin)

        async def no_child(*_a, **_kw):
            raise AssertionError("a child was prepared for a pi the adapter cannot drive")

        monkeypatch.setattr(launch_mod, "_run_preflight_bounded", no_child)
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        with pytest.raises(acp_client.AcpError, match="too old"):
            asyncio.run(client._spawn())

    def test_a_current_pi_reaches_the_rest_of_the_spawn(
        self, tmp_path, monkeypatch, fresh_pi_resolution
    ):
        pi_bin = self._linked_bin(tmp_path, acp_client.PI_NPM_PKG, "1.2.0")
        _inject_pi_resolvers(monkeypatch, pi_bin)

        class Reached(Exception):
            pass

        async def reached(*_a, **_kw):
            raise Reached

        monkeypatch.setattr(launch_mod, "_run_preflight_bounded", reached)
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        with pytest.raises(Reached):
            asyncio.run(client._spawn())


def test_the_handshake_is_the_spec_dialect():
    """Integer ``1``, from the per-harness table the shared handshake reads."""
    assert PROTOCOL_VERSION_PI == 1
    assert acp_client._PROTOCOL_VERSION_BY_BACKEND[ACP_BACKEND_PI] is PROTOCOL_VERSION_PI


# ── The gate launcher ────────────────────────────────────────────────────────


class TestGateLauncher:
    """Forwards the adapter's arguments, appends the extension, quotes what it names."""

    def test_the_posix_body_quotes_both_paths(self, monkeypatch):
        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", False)
        body = _pi_gate_launcher_body("/opt/my pi/pi", "/site/gate's.ts")
        assert body.startswith("#!/bin/sh\n")
        assert "'/opt/my pi/pi'" in body
        assert '"$@"' in body, "every argument the adapter passes must be forwarded"
        assert "--extension" in body
        assert body.rstrip().endswith("'/site/gate'\"'\"'s.ts'"), body

    def test_the_windows_body_is_a_cmd_that_forwards_arguments(self, monkeypatch):
        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
        body = _pi_gate_launcher_body("C:\\tools\\pi.cmd", "C:\\site\\gate.ts")
        assert body.startswith("@echo off")
        assert "%*" in body
        assert '"C:\\tools\\pi.cmd"' in body and '"C:\\site\\gate.ts"' in body

    def test_the_launcher_is_written_once_and_lives_in_the_run_dir(self, monkeypatch, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", lambda: str(run_dir))
        monkeypatch.setattr(pi_mod, "_pi_gate_launcher_cache", {})
        first = _ensure_pi_gate_launcher(str(tmp_path / "pi"), str(tmp_path / "gate.ts"))
        second = _ensure_pi_gate_launcher(str(tmp_path / "pi"), str(tmp_path / "gate.ts"))
        assert first == second
        assert Path(first).parent == run_dir
        assert Path(first).name.startswith("kirocrew_pi_gate_")
        assert str(tmp_path / "gate.ts") in Path(first).read_text(encoding="utf-8")
        # Different inputs are a different launcher, not a stale one.
        other = _ensure_pi_gate_launcher(str(tmp_path / "pi2"), str(tmp_path / "gate.ts"))
        assert other != first

    def test_nothing_is_written_into_the_work_dir_or_the_pi_directory(self, monkeypatch, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", lambda: str(run_dir))
        monkeypatch.setattr(pi_mod, "_pi_gate_launcher_cache", {})
        _ensure_pi_gate_launcher(str(tmp_path / "pi"), str(tmp_path / "gate.ts"))
        assert list(work.iterdir()) == []
        assert not (tmp_path / ".pi").exists()

    def test_the_shipped_extension_is_package_data(self):
        """The file exists beside agent_sdk, and both packaging manifests ship it."""
        path = Path(pi_gate_extension_path())
        assert path.is_file()
        assert path.suffix == ".ts"
        rel = path.relative_to(ROOT / "src" / "kiro_crew").as_posix()
        setup_cfg = (ROOT / "setup.cfg").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        assert "agent_sdk/gate_extensions/*/*.ts" in setup_cfg, rel
        assert "src/kiro_crew/agent_sdk/gate_extensions *.ts" in manifest, rel

    def test_the_extension_and_the_host_agree_on_the_vocabulary(self):
        """The probe name and the envelope marker are spelled once each, on both sides."""
        source = Path(pi_gate_extension_path()).read_text(encoding="utf-8")
        probe = re.search(r'const PROBE_COMMAND = "([^"]+)"', source)
        marker = re.search(r'const ENVELOPE_MARKER = "([^"]+)"', source)
        nonce_env = re.search(r'const NONCE_ENV = "([^"]+)"', source)
        assert probe and probe.group(1) == PROBE
        assert marker and marker.group(1) == GATE_ENVELOPE_MARKER
        assert nonce_env and nonce_env.group(1) == acp_client._ENV_PI_GATE_SESSION
        assert 'pi.on("tool_call"' in source
        assert "block: true" in source, "a denied dialog must block the call"

    def test_the_shipped_extension_matches_the_pinned_digest(self):
        """Editing the gate is a deliberate two-file edit: the bytes and this pin."""
        payload = acp_client._pi_gate_extension_bytes(Path(pi_gate_extension_path()).read_bytes())
        assert hashlib.sha256(payload).hexdigest() == PI_GATE_EXTENSION_SHA256

    def test_the_digest_is_stable_under_both_line_endings(self, tmp_path, monkeypatch):
        """A CRLF checkout of the same file is the same gate, not a tampered one.

        Windows CI showed the raw-bytes digest of the CRLF rendering; with it every
        Windows install would refuse every pi session as tampered. Normalizing to LF
        before hashing (and sealing the normalized bytes) is what makes the property
        hold however the file arrived; the ``.gitattributes`` pin is the second belt.
        """
        lf = Path(pi_gate_extension_path()).read_bytes()
        assert b"\r\n" not in lf, "the shipped file is LF"
        crlf = lf.replace(b"\n", b"\r\n")
        assert crlf != lf
        assert hashlib.sha256(crlf).hexdigest() != PI_GATE_EXTENSION_SHA256, "raw bytes differ"
        for payload in (lf, crlf):
            normalized = acp_client._pi_gate_extension_bytes(payload)
            assert hashlib.sha256(normalized).hexdigest() == PI_GATE_EXTENSION_SHA256
        # End to end: a CRLF package file seals, and the sealed copy is the LF form.
        crlf_file = tmp_path / "crlf.ts"
        crlf_file.write_bytes(crlf)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        monkeypatch.setattr(pi_mod, "pi_gate_extension_path", lambda: str(crlf_file))
        monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", lambda: str(run_dir))
        sealed = Path(_seal_pi_gate_extension())
        assert sealed.read_bytes() == lf
        # And a byte that is NOT a line ending still fails the digest.
        (tmp_path / "bad.ts").write_bytes(lf.replace(b"block: true", b"block: false", 1))
        monkeypatch.setattr(pi_mod, "pi_gate_extension_path", lambda: str(tmp_path / "bad.ts"))
        with pytest.raises(acp_client.PiGateExtensionTampered):
            _seal_pi_gate_extension()

    def test_the_checkout_is_pinned_lf_as_well(self):
        """Belt two: git is told not to translate the file, so the bytes match on every clone."""
        attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        assert "src/kiro_crew/agent_sdk/gate_extensions/**/*.ts text eol=lf" in attrs

    def test_the_extension_never_drops_the_arguments_it_forwards(self):
        """An oversize call is bounded value by value or refused, never sent without its keys.

        The keys the host's path checks read (``path``, ``file_path``) must survive,
        and a shell command is never cut: the host's deny rules read its text, so it
        is forwarded whole and judged on its content like on every other harness,
        and only an envelope too large to carry at all is refused -- a deny rule
        cannot judge text it did not see.
        """
        source = Path(pi_gate_extension_path()).read_text(encoding="utf-8")
        assert "input: null" not in source
        assert 'bash: new Set(["command"])' in source, "the shell command is forwarded whole"
        assert 'write: new Set(["content"])' in source, "a document body is forwarded whole"
        assert 'edit: new Set(["oldText", "newText"])' in source
        assert "shellCommandIsTooLong" not in source, "a long command is judged, not refused"
        assert "too large to judge" in source


class TestSealedExtension:
    """The harness loads a digest-verified copy in the gate artifact dir, never the package file."""

    def _run_dir(self, monkeypatch, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", lambda: str(run_dir))
        return run_dir

    def test_the_shipped_bytes_are_sealed_read_only_in_the_run_dir(self, monkeypatch, tmp_path):
        run_dir = self._run_dir(monkeypatch, tmp_path)
        sealed = Path(_seal_pi_gate_extension())
        assert sealed.parent == run_dir and sealed.suffix == ".ts"
        assert sealed.read_bytes() == Path(pi_gate_extension_path()).read_bytes()
        if not acp_client.platform_compat.IS_WINDOWS:
            assert (sealed.stat().st_mode & 0o777) == 0o400
        assert Path(_seal_pi_gate_extension()) == sealed, "the same bytes are not rewritten"

    def test_a_rewritten_package_file_is_refused_before_any_child_starts(
        self, monkeypatch, tmp_path
    ):
        self._run_dir(monkeypatch, tmp_path)
        tampered = tmp_path / "kiro_crew_tool_gate.ts"
        tampered.write_text("export default function () {}\n", encoding="utf-8")
        monkeypatch.setattr(pi_mod, "pi_gate_extension_path", lambda: str(tampered))
        with pytest.raises(PiGateExtensionTampered):
            _seal_pi_gate_extension()

    def test_a_tampered_sealed_copy_is_replaced(self, monkeypatch, tmp_path):
        run_dir = self._run_dir(monkeypatch, tmp_path)
        sealed = Path(_seal_pi_gate_extension())
        sealed.chmod(0o600)
        sealed.write_text("// not the gate\n", encoding="utf-8")
        assert (
            Path(_seal_pi_gate_extension()).read_bytes()
            == Path(pi_gate_extension_path()).read_bytes()
        )
        assert sealed.parent == run_dir

    def test_the_arm_loads_the_sealed_copy_not_the_package_file(self, monkeypatch, tmp_path):
        """The launcher and the read-back both name the copy the seal returned."""
        rig = _PiLaunchRig(monkeypatch, tmp_path)
        rig.run()
        assert rig.args_of("launcher") == (rig.PI_BIN, rig.SEALED)
        assert rig.args_of("verify")[1] == rig.SEALED
        assert pi_gate_extension_path() not in (rig.args_of("launcher")[1], rig.SEALED)


class TestAMissingExtensionIsTheSameRefusal:
    def test_an_install_without_the_package_data_is_refused_by_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pi_mod, "pi_gate_extension_path", lambda: str(tmp_path / "absent.ts"))
        with pytest.raises(acp_client.PiGateExtensionTampered) as excinfo:
            _seal_pi_gate_extension()
        assert "cannot be read" in str(excinfo.value)


class TestGateArtifactsRefuseAnUnsafeDirectory:
    """The seal-to-exec window requires a real owner-only artifact directory."""

    def _point(self, monkeypatch, tmp_path):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        monkeypatch.setattr(pi_mod, "config_dir", lambda: cfg)
        return cfg / "pi-gate"

    def test_the_dedicated_directory_is_created_owner_only(self, monkeypatch, tmp_path):
        expected = self._point(monkeypatch, tmp_path)
        assert Path(pi_mod._pi_gate_artifact_dir()) == expected
        if not acp_client.platform_compat.IS_WINDOWS:
            assert stat.S_IMODE(expected.stat().st_mode) == 0o700

    def test_a_linked_artifact_directory_refuses_the_session(self, monkeypatch, tmp_path):
        expected = self._point(monkeypatch, tmp_path)
        elsewhere = tmp_path / "shared"
        elsewhere.mkdir()
        make_dir_link(expected, elsewhere)
        with pytest.raises(AcpToolGateUnroutable) as excinfo:
            pi_mod._pi_gate_artifact_dir()
        assert "not a real directory" in str(excinfo.value)
        assert list(elsewhere.iterdir()) == []

    def test_neither_writer_uses_a_linked_artifact_directory(self, monkeypatch, tmp_path):
        expected = self._point(monkeypatch, tmp_path)
        elsewhere = tmp_path / "shared"
        elsewhere.mkdir()
        make_dir_link(expected, elsewhere)
        monkeypatch.setattr(pi_mod, "_pi_gate_launcher_cache", {})
        with pytest.raises(AcpToolGateUnroutable):
            _seal_pi_gate_extension()
        with pytest.raises(AcpToolGateUnroutable):
            _ensure_pi_gate_launcher(str(tmp_path / "pi"), str(tmp_path / "gate.ts"))
        assert list(elsewhere.iterdir()) == []

    def test_both_writers_go_through_the_strict_resolver(self, monkeypatch, tmp_path):
        """Each writer asks the strict resolver for its directory, every time it writes."""
        artifact_dir = tmp_path / "pi-gate"
        artifact_dir.mkdir()
        asked: list[str] = []

        def _strict() -> str:
            asked.append("asked")
            return str(artifact_dir)

        monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", _strict)
        monkeypatch.setattr(pi_mod, "_pi_gate_launcher_cache", {})
        sealed = _seal_pi_gate_extension()
        assert len(asked) == 1 and Path(sealed).parent == artifact_dir
        launcher = _ensure_pi_gate_launcher(str(tmp_path / "pi"), sealed)
        assert len(asked) == 2 and Path(launcher).parent == artifact_dir


class TestAdapterVersionIsNamedAtHandshake:
    """The gate contract was observed on one pi-acp release; any other is logged by name."""

    @pytest.fixture(autouse=True)
    def _fresh_process_memory(self, monkeypatch):
        monkeypatch.setattr(acp_client, "_pi_adapter_versions_noted", set())

    def _client(self, tmp_path, version, backend=ACP_BACKEND_PI):
        client = AcpClient(work_dir=tmp_path, acp_backend=backend)
        client._agent_version = version
        return client

    def test_a_release_is_named_once_per_process(self, tmp_path, caplog):
        """A gateway on a newer adapter is told once, not on every session."""
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            self._client(tmp_path, "0.0.35")._note_pi_adapter_version()
            self._client(tmp_path, "0.0.35")._note_pi_adapter_version()
            self._client(tmp_path, "0.0.36")._note_pi_adapter_version()
        hits = [r for r in caplog.records if "gate extension contract" in r.getMessage()]
        assert len(hits) == 2

    def test_the_verified_release_is_silent(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            self._client(tmp_path, acp_client.PI_ACP_VERIFIED_VERSION)._note_pi_adapter_version()
        assert not [r for r in caplog.records if "gate extension contract" in r.getMessage()]

    @pytest.mark.parametrize("version", ["0.0.35", ""])
    def test_another_or_unknown_release_is_logged_with_both_versions(
        self, tmp_path, caplog, version
    ):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            self._client(tmp_path, version)._note_pi_adapter_version()
        hits = [
            r.getMessage() for r in caplog.records if "gate extension contract" in r.getMessage()
        ]
        assert len(hits) == 1
        assert acp_client.PI_ACP_VERIFIED_VERSION in hits[0]
        assert (version or "unknown") in hits[0]

    def test_other_harnesses_are_untouched(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            self._client(tmp_path, "9.9.9", ACP_BACKEND_OPENCODE)._note_pi_adapter_version()
        assert not caplog.records

    def test_it_runs_at_the_handshake(self):
        source = inspect.getsource(AcpClient._initialize_session)
        assert "self._agent_version = agent_version_from_init(init_resp)" in source
        assert "self._note_pi_adapter_version()" in source

    def test_the_verified_version_is_the_corpus_version(self):
        readme = (ROOT / "test" / "fixtures" / "acp_frames" / "pi" / "README.md").read_text(
            encoding="utf-8"
        )
        assert acp_client.PI_ACP_VERIFIED_VERSION in readme

    def test_the_verified_version_is_the_lane_pin(self):
        """The corpus is captured at the lane's pin, so the constant tracks it."""
        from real_adapter_gate import MEASURED_PI_ACP_VERSION

        assert acp_client.PI_ACP_VERIFIED_VERSION == MEASURED_PI_ACP_VERSION


class TestGateTripwire:
    """A completed tool call the gate never asked about ends the session."""

    def _update(self, tool_call_id, status="completed"):
        return JsonRpcMessage(
            method="session/update",
            params={
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tool_call_id,
                    "status": status,
                },
            },
        )

    def _client(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        client._pi_gate_nonce = NONCE
        killed: list = []

        async def _kill(*, force=False):
            killed.append(force)

        monkeypatch.setattr(client, "_kill_process", _kill)
        return client, killed

    def test_a_completed_call_the_gate_never_saw_kills_the_harness(self, tmp_path, monkeypatch):
        client, killed = self._client(tmp_path, monkeypatch)
        with pytest.raises(AcpToolGateUnroutable):
            asyncio.run(client._tripwire_pi_gate(self._update("call_unseen")))
        assert killed == [True]

    def test_a_completed_call_the_gate_asked_about_passes(self, tmp_path, monkeypatch):
        client, killed = self._client(tmp_path, monkeypatch)
        client._build_permission_event(_permission_frame(_envelope(toolCallId="call_seen")))
        asyncio.run(client._tripwire_pi_gate(self._update("call_seen")))
        assert killed == []

    def test_the_auto_approve_path_also_records_the_asked_call(self, tmp_path, monkeypatch):
        """``_handle_permission`` builds no event without a deny set; the id is noted anyway.

        Otherwise every pi tool call answered on the auto-approve path would trip
        the wire on completion and kill a harness that did ask.
        """
        client, killed = self._client(tmp_path, monkeypatch)
        approved: list = []

        async def _approve(request_id):
            approved.append(request_id)

        monkeypatch.setattr(client, "approve_tool", _approve)
        assert not client._spec_denied_tools
        asyncio.run(client._handle_permission(_permission_frame(_envelope(toolCallId="call_auto"))))
        assert approved
        asyncio.run(client._tripwire_pi_gate(self._update("call_auto")))
        assert killed == []

    def test_a_denied_call_that_completes_kills_the_harness(self, tmp_path, monkeypatch):
        """Third link: pi must honour the extension's block; a denied call may never complete."""
        client, killed = self._client(tmp_path, monkeypatch)
        sent: list = []

        async def _send(request_id, payload):
            sent.append((request_id, payload))

        monkeypatch.setattr(client, "_send_response", _send)
        frame = _permission_frame(_envelope(toolCallId="call_denied"))
        client._build_permission_event(frame)
        asyncio.run(client.reject_tool(frame.id))
        assert sent, "the reject was answered on the wire"
        assert "call_denied" in client._pi_gate_denied_ids
        with pytest.raises(AcpToolGateUnroutable) as excinfo:
            asyncio.run(client._tripwire_pi_gate(self._update("call_denied")))
        assert killed == [True]
        assert "DENIED" in str(excinfo.value)

    def test_a_denied_call_that_fails_is_the_expected_outcome(self, tmp_path, monkeypatch):
        client, killed = self._client(tmp_path, monkeypatch)

        async def _send(request_id, payload):
            pass

        monkeypatch.setattr(client, "_send_response", _send)
        frame = _permission_frame(_envelope(toolCallId="call_d2"))
        client._build_permission_event(frame)
        asyncio.run(client.reject_tool(frame.id))
        asyncio.run(client._tripwire_pi_gate(self._update("call_d2", status="failed")))
        assert killed == []

    def test_an_approved_call_is_not_marked_denied(self, tmp_path, monkeypatch):
        client, killed = self._client(tmp_path, monkeypatch)

        async def _send(request_id, payload):
            pass

        monkeypatch.setattr(client, "_send_response", _send)
        frame = _permission_frame(_envelope(toolCallId="call_ok"))
        client._build_permission_event(frame)
        asyncio.run(client.approve_tool(frame.id))
        assert "call_ok" not in client._pi_gate_denied_ids
        asyncio.run(client._tripwire_pi_gate(self._update("call_ok")))
        assert killed == []

    def test_a_re_asked_call_is_judged_on_its_fresh_verdict(self, tmp_path, monkeypatch):
        """A DENY is scoped to the ask it answered, not to the id for the session's life.

        Nothing makes a tool-call id unique for a whole session, so the same id can be
        denied once and then legitimately asked about again. Were the denial to survive
        the second ask, the approved call's own ``completed`` frame would trip
        ``ran_despite_deny`` and kill a harness that behaved correctly throughout.
        """
        client, killed = self._client(tmp_path, monkeypatch)

        async def _send(request_id, payload):
            pass

        monkeypatch.setattr(client, "_send_response", _send)
        first = _permission_frame(_envelope(toolCallId="call_reask"))
        client._build_permission_event(first)
        asyncio.run(client.reject_tool(first.id))
        assert "call_reask" in client._pi_gate_denied_ids

        second = _permission_frame(_envelope(toolCallId="call_reask"))
        client._build_permission_event(second)
        assert "call_reask" not in client._pi_gate_denied_ids
        asyncio.run(client.approve_tool(second.id))

        asyncio.run(client._tripwire_pi_gate(self._update("call_reask")))
        assert killed == []

    def test_a_completed_call_consumes_its_ask_so_a_reused_id_is_asked_again(
        self, tmp_path, monkeypatch
    ):
        """The mirror of the stale-deny case: an approval must not outlive its call.

        Ids recur within a session, so an ask left behind by a completed call would
        vouch for the next call wearing the same id -- one the gate never saw. The
        terminal frame consumes the id's state; a reuse is unasked until a fresh
        dialog names it, and a fresh dialog earns it a fresh, trusted verdict.
        """
        client, killed = self._client(tmp_path, monkeypatch)

        async def _send(request_id, payload):
            pass

        monkeypatch.setattr(client, "_send_response", _send)
        first = _permission_frame(_envelope(toolCallId="call_reuse"))
        client._build_permission_event(first)
        asyncio.run(client.approve_tool(first.id))
        asyncio.run(client._tripwire_pi_gate(self._update("call_reuse")))
        assert killed == []
        assert "call_reuse" not in client._pi_gate_asked_ids

        with pytest.raises(AcpToolGateUnroutable) as excinfo:
            asyncio.run(client._tripwire_pi_gate(self._update("call_reuse")))
        assert "without asking" in str(excinfo.value)
        assert killed == [True]

        fresh, fresh_killed = self._client(tmp_path, monkeypatch)
        monkeypatch.setattr(fresh, "_send_response", _send)
        for _ in range(2):
            frame = _permission_frame(_envelope(toolCallId="call_reuse"))
            fresh._build_permission_event(frame)
            asyncio.run(fresh.approve_tool(frame.id))
            asyncio.run(fresh._tripwire_pi_gate(self._update("call_reuse")))
        assert fresh_killed == []

    def test_a_failed_call_does_not_trip(self, tmp_path, monkeypatch):
        """Schema rejections and gate denials both fail before the gate could ask."""
        client, killed = self._client(tmp_path, monkeypatch)
        asyncio.run(client._tripwire_pi_gate(self._update("call_x", status="failed")))
        asyncio.run(client._tripwire_pi_gate(self._update("call_x", status="in_progress")))
        assert killed == []

    def test_a_session_without_the_gate_is_untouched(self, tmp_path, monkeypatch):
        client, killed = self._client(tmp_path, monkeypatch)
        client._pi_gate_nonce = ""
        asyncio.run(client._tripwire_pi_gate(self._update("call_unseen")))
        assert killed == []

    def test_the_tripwire_runs_on_every_reader(self):
        """The three loops that read session updates each run the tripwire."""
        import sys

        for reader in (
            AcpClient.send_message_stream,
            AcpClient._dispatch_events,
            AcpClient._read_prompt_response,
        ):
            assert "await self._tripwire_pi_gate(msg)" in inspect.getsource(reader), reader
        family = "".join(
            inspect.getsource(sys.modules[name])
            for name in (acp_client.__name__, *acp_client._CLIENT_OWNERS)
        )
        assert family.count("await self._tripwire_pi_gate(msg)") == 3


# ── The read-back ────────────────────────────────────────────────────────────


class TestReadbackParsing:
    """The response is found by id among whatever else pi writes to stdout."""

    def test_the_matching_response_is_found_after_other_lines(self):
        stdout = "\n".join(
            [
                json.dumps({"type": "extension_ui_request", "id": "x", "method": "notify"}),
                "not json at all",
                _response(_registry((PROBE, "/site/gate.ts"))),
            ]
        )
        commands = _pi_commands_from_readback(stdout)
        assert isinstance(commands, list) and commands[0]["name"] == PROBE

    def test_a_stray_line_before_the_response_is_skipped(self):
        """A line nested past the decoder raised ``RecursionError``, which the
        ``ValueError`` arm missed, and failed the session's spawn."""
        from stray_line_helpers import STRAY_LINES

        strays = [make().decode("utf-8", "replace").rstrip("\n") for make in STRAY_LINES.values()]
        stdout = "\n".join([*strays, _response(_registry((PROBE, "/site/gate.ts")))])
        commands = _pi_commands_from_readback(stdout)
        assert isinstance(commands, list) and commands[0]["name"] == PROBE

    def test_a_response_to_another_request_is_not_taken(self):
        assert _pi_commands_from_readback(_response([], request_id="other")) is None

    def test_a_failed_response_is_none(self):
        frame = json.loads(_response([]))
        frame["success"] = False
        assert _pi_commands_from_readback(json.dumps(frame)) is None

    def test_empty_output_is_none(self):
        assert _pi_commands_from_readback("") is None


class TestTheIssueVocabulary:
    """``gate_extension_issue`` owns what counts as loaded."""

    EXT = "/site/kiro_crew/agent_sdk/gate_extensions/pi/kiro_crew_tool_gate.ts"

    def test_the_probe_from_crews_file_is_no_issue(self):
        assert (
            gate_extension_issue(
                ACP_BACKEND_PI, _registry(("compact", None), (PROBE, self.EXT)), self.EXT
            )
            == ""
        )

    def test_an_absent_probe_is_an_issue(self):
        issue = gate_extension_issue(ACP_BACKEND_PI, _registry(("compact", None)), self.EXT)
        assert PROBE in issue and "unasked" in issue

    def test_the_probe_from_another_file_is_an_issue(self):
        """An operator extension registering the same name must not read as the gate."""
        issue = gate_extension_issue(
            ACP_BACKEND_PI, _registry((PROBE, "/srv/pi-extensions/mine.ts")), self.EXT
        )
        assert issue and "not Kiro Crew's" in issue

    def test_an_unreadable_registry_is_an_issue(self):
        assert gate_extension_issue(ACP_BACKEND_PI, None, self.EXT)

    def test_a_harness_on_another_mechanism_has_no_issue(self):
        assert gate_extension_issue(ACP_BACKEND_OPENCODE, None, self.EXT) == ""


class TestTheReadBackComparesFilesNotStrings:
    """pi reports the path it loaded from in its own spelling; the sealed copy must still pass."""

    def test_a_symlinked_spelling_of_the_sealed_copy_passes(self, tmp_path):
        """Node's realpath through a symlinked install is the same file, not another one."""
        real_dir = tmp_path / "run"
        real_dir.mkdir()
        sealed = real_dir / "kirocrew_pi_gate_1.ts"
        sealed.write_text("// gate\n", encoding="utf-8")
        alias = tmp_path / "alias"
        try:
            alias.symlink_to(real_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable here")
        reported = str(alias / "kirocrew_pi_gate_1.ts")
        assert reported != str(sealed)
        # Raw strings: refused. Same-file spelling: passes.
        assert gate_extension_issue(ACP_BACKEND_PI, _registry((PROBE, reported)), str(sealed))
        assert (
            gate_extension_issue(
                ACP_BACKEND_PI,
                acp_client._same_file_spelling_all(_registry((PROBE, reported))),
                acp_client._same_file_spelling(str(sealed)),
            )
            == ""
        )

    def test_another_file_still_fails_after_normalizing(self, tmp_path):
        sealed = tmp_path / "kirocrew_pi_gate_1.ts"
        sealed.write_text("// gate\n", encoding="utf-8")
        other = tmp_path / "mine.ts"
        other.write_text("// gate\n", encoding="utf-8")
        assert gate_extension_issue(
            ACP_BACKEND_PI,
            acp_client._same_file_spelling_all(_registry((PROBE, str(other)))),
            acp_client._same_file_spelling(str(sealed)),
        )

    def test_an_unparseable_registry_keeps_its_shape(self):
        """Normalizing must not turn a refusable shape into a list the decision accepts."""
        assert acp_client._same_file_spelling_all(None) is None
        assert acp_client._same_file_spelling_all("nope") == "nope"
        odd = [{"name": PROBE, "sourceInfo": "not-a-dict"}, 7]
        assert acp_client._same_file_spelling_all(odd) == odd

    @pytest.mark.parametrize("respelled", ["registry", "extension"])
    def test_the_driver_normalizes_both_sides(self, tmp_path, monkeypatch, respelled):
        """The read-back passes when EITHER side names the sealed copy another way.

        ``..`` is a spelling every platform resolves, so this needs no symlink: the
        string differs, the file does not.
        """
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        sealed = run_dir / "kirocrew_pi_gate_1.ts"
        sealed.write_text("// gate\n", encoding="utf-8")
        other_spelling = str(run_dir / ".." / "run" / sealed.name)
        assert other_spelling != str(sealed)
        reported, expected = (
            (other_spelling, str(sealed))
            if respelled == "registry"
            else (str(sealed), other_spelling)
        )

        class _Completed:
            returncode = 0
            stdout = _response(_registry((PROBE, reported)))
            stderr = ""

        monkeypatch.setattr(pi_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        argv = ["/opt/run/kirocrew_pi_gate.sh", *acp_client._PI_RPC_ARGS]
        assert pi_mod._verify_pi_gate(client, ACP_BACKEND_PI, argv, expected) == ("", "")


#: A 40-char run of the base64 alphabet: the AWS secret-access-key shape the
#: redactors' bare-secret detector is built for. Not a real key.
_AWS_SECRET_SHAPE = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


class TestTheReadBackStderrDiagnosis:
    """What a refused read-back tells the operator about the child's failure."""

    #: One stderr per vocabulary phrase. Keyed by phrase so a shape added to
    #: :data:`_READBACK_FAULT_SHAPES` without a sample here fails the coverage test
    #: below rather than going unexercised.
    SAMPLES = {
        "its shebang interpreter could not be run": (
            "/bin/sh: /opt/pi/bin/pi: /usr/bin/node: bad interpreter: No such file or directory"
        ),
        "it is built for a different CPU or executable format": (
            "/bin/sh: /opt/pi/bin/pi: Bad CPU type in executable"
        ),
        "the OS killed it over its code signature": "/opt/pi/bin/pi: code signature invalid",
        "a shared library it needs is missing": ("dyld: Library not loaded: @rpath/libnode.dylib"),
        "the gateway passed it a flag this harness version does not accept": (
            "pi: unknown flag --extension"
        ),
        "its configuration could not be parsed": "Error: cannot parse config at line 3",
        "the OS denied the operation, as a sandbox, quarantine or privacy policy does": (
            "/bin/sh: /opt/pi/bin/pi: Operation not permitted"
        ),
        "the OS refused to execute it": "/bin/sh: /opt/pi/bin/pi: Permission denied",
        "the file was still being written": "/bin/sh: /opt/pi/bin/pi: Text file busy",
        "the path is a directory, not a program": "/bin/sh: /opt/pi/bin/pi: Is a directory",
        "its path loops through symlinks": (
            "/bin/sh: /opt/pi/bin/pi: Too many levels of symbolic links"
        ),
        "the path does not exist": "/bin/sh: /opt/pi/bin/pi: No such file or directory",
    }

    @staticmethod
    def _phrases():
        return [phrase for _pattern, phrase in _READBACK_FAULT_SHAPES]

    def test_the_exec_refusal_that_exit_126_cannot_distinguish_is_named(self):
        # exit 126 is the shell refusing an exec; only the child's message says
        # whether the OS denied it or the shebang could not be resolved, and those
        # take different fixes.
        denied = _readback_stderr_diagnosis("/bin/sh: /opt/pi/bin/pi: Permission denied\n")
        shebang = _readback_stderr_diagnosis(
            "/bin/sh: /opt/pi/bin/pi: /usr/bin/node: bad interpreter: No such file or directory\n"
        )
        assert denied == "the OS refused to execute it"
        assert shebang.startswith("its shebang interpreter could not be run")
        assert denied != shebang

    def test_a_shebang_fault_names_the_interpreter_before_the_missing_file(self):
        # The two shapes co-occur in one line and the order carries the meaning:
        # the interpreter is the cause, the missing file only its symptom.
        out = _readback_stderr_diagnosis(
            "/bin/sh: /opt/pi/bin/pi: /usr/bin/node: bad interpreter: No such file or directory\n"
        )
        assert out.split("; ") == [
            "its shebang interpreter could not be run",
            "the path does not exist",
        ]

    def test_at_most_two_shapes_are_reported(self):
        crowded = (
            "Permission denied\nbad interpreter\nText file busy\nIs a directory\n"
            "No such file or directory\nBad CPU type in executable\n"
        )
        assert len(_readback_stderr_diagnosis(crowded).split("; ")) <= _READBACK_FAULT_MAX_SHAPES

    def test_every_vocabulary_shape_has_a_sample_and_reports_itself_first(self):
        # Coverage in both directions: a phrase with no sample, and a sample whose
        # phrase is not reported first, both fail here.
        assert sorted(self.SAMPLES) == sorted(self._phrases())
        for phrase, sample in self.SAMPLES.items():
            assert _readback_stderr_diagnosis(sample).split("; ")[0] == phrase, phrase

    def test_the_platform_wording_a_launcher_chooses_does_not_matter(self):
        # Same fault, four spellings a shell, dyld, cmd.exe or Node might use.
        for text in (
            "permission denied",
            "PERMISSION DENIED",
            "pi: Permission denied (os error 13)",
            "Error: spawn /opt/pi/bin/pi EACCES: Permission denied",
        ):
            assert _readback_stderr_diagnosis(text) == "the OS refused to execute it", text

    def test_nothing_the_child_wrote_is_ever_published(self):
        """The structural guarantee: output is drawn from the vocabulary, or empty.

        This is what makes the credential question unanswerable rather than
        answered. A scheme that echoes the child's bytes has to show no credential
        survives any rejoining of them, and the redactors' patterns need contiguity
        and label anchors that a single inserted byte destroys. Matching instead of
        echoing means there is no path from a child byte to published text, so a
        hostile stderr cannot produce one whatever it contains.
        """
        allowed = set(self._phrases())
        secrets = (
            "glpat-" + "aB3xY7zQ9wE2rT5yU8iO",
            "AKIA" + "IOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "xoxb-" + "123456789012-abcdefghijklmnop",
            "s3cr3tOpaqueTok3nValue",
        )
        splitters = ("", "\n", " ", "\x1b[31m", "\u2028", "\u00a0", "\u200b", "-", ".", "\r\n")
        hostile = []
        for secret in secrets:
            for splitter in splitters:
                for offset in range(0, len(secret), 3):
                    spliced = secret[:offset] + splitter + secret[offset:]
                    hostile.append(f"/bin/sh: /opt/pi/bin/pi: Permission denied\ntoken={spliced}\n")
                    hostile.append(f"Authorization: Bearer {spliced}\nText file busy\n")
        # The label pushed clean out of the scan window, and a capture far larger
        # than the window.
        far = "x" * (_READBACK_STDERR_SCAN_CHARS + 500)
        hostile.append(f"Authorization: Bearer{far}\n{secrets[-1]}\nPermission denied\n")
        hostile.append("y" * 5_000_000 + "\nPermission denied\ntoken=" + secrets[0] + "\n")

        for text in hostile:
            out = _readback_stderr_diagnosis(text)
            if not out:
                continue
            assert all(part in allowed for part in out.split("; ")), out
            for secret in secrets:
                assert secret not in out
                assert secret.split("-", 1)[-1] not in out

    def test_a_colour_code_inside_a_token_body_publishes_no_token(self):
        # The composition that defeats every normalising scheme: rejoining the run
        # strips the `-` a prefixed pattern anchors on, and not rejoining leaves the
        # `[31m` residue inside the run. Matching a vocabulary is indifferent to it.
        token = "glpat-" + "aB3xY7zQ9wE2rT5yU8iO"
        body = token[len("glpat-") :]
        allowed = set(self._phrases())
        for sgr in ("\x1b[31m", "\x1b[0m", "\x1b[1;32m", "\x1b[m", "\x1b[38;5;196m"):
            for sgr_at in range(len(body) + 1):
                for wrap_at in range(0, len(body) + 1, 3):
                    if wrap_at == sgr_at:
                        continue
                    lo, hi = sorted((sgr_at, wrap_at))
                    first, second = (sgr, "\n") if lo == sgr_at else ("\n", sgr)
                    spliced = "glpat-" + body[:lo] + first + body[lo:hi] + second + body[hi:]
                    out = _readback_stderr_diagnosis(
                        f"auth failed token={spliced}\n/bin/sh: pi: Permission denied\n"
                    )
                    assert out == "the OS refused to execute it"
                    assert all(part in allowed for part in out.split("; "))
                    assert body not in out

    def test_a_label_beyond_the_scan_window_publishes_no_token(self):
        token = "s3cr3tOpaqueTok3nValue"
        gap = "x" * (_READBACK_STDERR_SCAN_CHARS + 500)
        out = _readback_stderr_diagnosis(
            f"Authorization: Bearer{gap}\n{token}\n/bin/sh: pi: Permission denied\n"
        )
        assert out == "the OS refused to execute it"
        assert token not in out

    def test_only_the_tail_of_a_large_stderr_is_read(self):
        # The window bounds the matching work, and it is the TAIL because a harness
        # writes its banner first and fails last.
        filler = "b" * (_READBACK_STDERR_SCAN_CHARS * 2)
        assert (
            _readback_stderr_diagnosis(
                "Text file busy\n" + filler + "\n/bin/sh: pi: Permission denied\n"
            )
            == "the OS refused to execute it"
        )
        # The same shape left far enough back is outside the window and unread.
        assert (
            _readback_stderr_diagnosis(
                "/bin/sh: pi: Permission denied\n" + filler + "\nText file busy\n"
            )
            == "the file was still being written"
        )

    def test_an_unreadable_or_silent_stderr_answers_nothing(self):
        assert _readback_stderr_diagnosis(None) == ""
        assert _readback_stderr_diagnosis(b"Permission denied") == ""
        assert _readback_stderr_diagnosis("") == ""
        assert _readback_stderr_diagnosis("   \n\t ") == ""
        assert _readback_stderr_diagnosis("a harness banner and nothing else") == ""

    def test_the_detail_separates_a_recognised_fault_from_an_unreadable_one(self):
        # Three answers the operator needs apart: a named fault, a harness that
        # explained itself in words this gateway has no shape for, and silence.
        assert _readback_detail_with_diagnosis("exit 126", "pi: Permission denied\n") == (
            "exit 126: the OS refused to execute it"
        )
        assert _readback_detail_with_diagnosis("exit 126", "harness gave up\n") == (
            "exit 126, and its stderr holds no message this gateway recognises"
        )
        assert _readback_detail_with_diagnosis("exit 126", "") == "exit 126"
        assert _readback_detail_with_diagnosis("exit 126", "  \n ") == "exit 126"
        assert _readback_detail_with_diagnosis("no response", None) == "no response"

    def test_an_unrecognised_stderr_is_described_and_never_quoted(self):
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        out = _readback_detail_with_diagnosis("exit 126", f"mystery failure key={secret}\n")
        assert out == "exit 126, and its stderr holds no message this gateway recognises"
        assert secret not in out
        assert "mystery" not in out


class TestTheReadBackReportsFailureRatherThanAssuming:
    """A read-back that could not run must never read as "loaded"."""

    ARGV = ["/opt/run/kirocrew_pi_gate.sh", "--mode", "rpc", "--no-themes"]
    EXT = "/site/gate.ts"

    def _client(self, tmp_path, **kw):
        return AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI, **kw)

    @staticmethod
    def _verify(client, argv, extension_path):
        return pi_mod._verify_pi_gate(client, ACP_BACKEND_PI, argv, extension_path)

    def test_a_missing_launcher_is_an_issue_with_the_harness_remedy(self, tmp_path):
        issue, remedy = self._verify(
            self._client(tmp_path), [str(tmp_path / "not-there"), "--mode", "rpc"], self.EXT
        )
        assert issue
        assert "get_commands" in remedy and PI_INSTALL_COMMAND in remedy

    def test_a_non_zero_exit_is_an_issue(self, tmp_path, monkeypatch):
        class _Completed:
            returncode = 3
            stdout = ""
            stderr = ""

        monkeypatch.setattr(pi_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, remedy = self._verify(self._client(tmp_path), self.ARGV, self.EXT)
        assert "exit 3" in issue and "get_commands" in remedy

    def test_the_childs_own_reason_reaches_the_refusal(self, tmp_path, monkeypatch):
        """An exit code alone names a verdict, not a cause.

        The launcher is /bin/sh exec'ing the resolved harness binary, so its message
        is what tells an exec the OS refused apart from a shebang it cannot resolve.
        The refusal carries which of the two it was, in the gateway's own words, and
        the child's bytes stay out of it -- including the operator's home path.
        """

        class _Completed:
            returncode = 126
            stdout = ""
            stderr = "/bin/sh: /Users/me/.local/bin/pi: Permission denied\n"

        monkeypatch.setattr(pi_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, _remedy = self._verify(self._client(tmp_path), self.ARGV, self.EXT)
        assert "exit 126" in issue
        assert "the OS refused to execute it" in issue
        assert "/Users/me" not in issue

    def test_a_silent_child_is_reported_as_the_bare_exit(self, tmp_path, monkeypatch):
        """No placeholder: "the child said nothing" must not look like "Crew hid it"."""

        class _Completed:
            returncode = 126
            stdout = ""
            stderr = "   \n\t\n"

        monkeypatch.setattr(pi_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, _remedy = self._verify(self._client(tmp_path), self.ARGV, self.EXT)
        assert issue.endswith("(exit 126)")

    def test_a_response_that_never_came_still_reports_the_childs_reason(
        self, tmp_path, monkeypatch
    ):
        """A zero exit with no parseable answer is the other half of the same path."""

        class _Completed:
            returncode = 0
            stdout = "not json at all"
            stderr = "pi: unknown flag --extension\n"

        monkeypatch.setattr(pi_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, _remedy = self._verify(self._client(tmp_path), self.ARGV, self.EXT)
        assert "no response" in issue
        assert "a flag this harness version does not accept" in issue

    def test_a_secret_in_the_childs_stderr_is_not_republished(self, tmp_path, monkeypatch):
        """A refusal reaches the dashboard and the chat card; the child is foreign.

        Both halves matter: a secret beside an UNRECOGNISED message, where there is
        nothing to report, and a secret beside a RECOGNISED one, where the fault is
        still named. Naming a fault publishes a phrase from the gateway's own
        vocabulary, so it cannot carry a secret either way.
        """

        def _run(stderr):
            class _Completed:
                returncode = 126
                stdout = ""

            _Completed.stderr = stderr
            monkeypatch.setattr(pi_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
            issue, _remedy = self._verify(self._client(tmp_path), self.ARGV, self.EXT)
            return issue

        unknown = _run(f"mystery: key={_AWS_SECRET_SHAPE} rejected\n")
        assert _AWS_SECRET_SHAPE not in unknown
        assert unknown.endswith("recognises)"), unknown

        known = _run(f"key={_AWS_SECRET_SHAPE}\n/bin/sh: pi: Permission denied\n")
        assert _AWS_SECRET_SHAPE not in known
        assert "the OS refused to execute it" in known, "a secret must not cost the diagnosis"

    def test_a_registry_without_the_gate_is_refused_with_the_gate_remedy(
        self, tmp_path, monkeypatch
    ):
        class _Completed:
            returncode = 0
            stdout = _response(_registry(("compact", None)))
            stderr = ""

        monkeypatch.setattr(pi_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, remedy = self._verify(self._client(tmp_path), self.ARGV, self.EXT)
        assert PROBE in issue
        assert "gate extension" in remedy

    def test_the_gate_from_crews_file_is_in_force(self, tmp_path, monkeypatch):
        seen: dict = {}

        class _Completed:
            returncode = 0
            stdout = _response(_registry((PROBE, self.EXT)))
            stderr = ""

        def _fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _Completed()

        monkeypatch.setattr(pi_mod.subprocess_mod, "run", _fake_run)
        assert self._verify(self._client(tmp_path), self.ARGV, self.EXT) == ("", "")
        # Used VERBATIM: the caller hands over an argv already through the sandbox
        # wrapper, so anything rebuilt here would run unwrapped.
        assert seen["argv"] == self.ARGV
        assert json.loads(seen["kwargs"]["input"])["type"] == "get_commands"
        assert seen["kwargs"]["timeout"] > 0
        assert seen["kwargs"]["cwd"] == str(tmp_path)
        assert "shell" not in seen["kwargs"]
        assert seen["kwargs"]["encoding"] == "utf-8"

    def test_the_child_environment_is_scrubbed_like_the_spawns(self, tmp_path, monkeypatch):
        """A FOREIGN harness binary, started before the spawn's own scrub runs."""
        seen: dict = {}

        class _Completed:
            returncode = 0
            stdout = _response(_registry((PROBE, self.EXT)))
            stderr = ""

        def _fake_run(argv, **kwargs):
            seen["env"] = kwargs["env"]
            return _Completed()

        monkeypatch.setattr(pi_mod.subprocess_mod, "run", _fake_run)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-should-not-travel")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-travel")
        monkeypatch.setenv("KIRO_API_KEY", "should-not-travel")
        self._verify(self._client(tmp_path), self.ARGV, self.EXT)
        env = seen["env"]
        for leaked in ("SLACK_BOT_TOKEN", "AWS_SECRET_ACCESS_KEY", "KIRO_API_KEY"):
            assert leaked not in env, f"{leaked} reached the harness's read-back child"
        assert env.get("PATH")
        assert env["PI_OFFLINE"] == "1", "the probe must not wait on pi's startup network work"

    def test_the_child_sees_the_session_overlay_the_spawn_applies(self, tmp_path, monkeypatch):
        """pi reads its agent directory from the environment, so the overlay must reach it."""
        seen: dict = {}

        class _Completed:
            returncode = 0
            stdout = _response(_registry((PROBE, self.EXT)))
            stderr = ""

        def _fake_run(argv, **kwargs):
            seen["env"] = kwargs["env"]
            return _Completed()

        monkeypatch.setattr(pi_mod.subprocess_mod, "run", _fake_run)
        monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
        client = self._client(
            tmp_path,
            extra_env={
                "PI_CODING_AGENT_DIR": str(tmp_path / "elsewhere"),
                "SLACK_BOT_TOKEN": "xoxb-overlay-must-not-bypass-the-scrub",
            },
        )
        self._verify(client, self.ARGV, self.EXT)
        assert seen["env"]["PI_CODING_AGENT_DIR"] == str(tmp_path / "elsewhere")
        assert "SLACK_BOT_TOKEN" not in seen["env"]
