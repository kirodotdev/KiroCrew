"""The pi spawn path: two components, a gate loaded INTO the harness, and its proof.

Four things are pinned here, and they fail apart:

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
* **The envelope.** The adapter renders the extension's dialog, not the tool call,
  so the dispatch parser reads the tool call back out of the dialog's message.

The read-back's refusal vocabulary is exercised through
``acp_tool_gate.gate_extension_issue``, which owns it, rather than by asserting on
message wording at the call site.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp._dispatch import GATE_ENVELOPE_MARKER, build_permission_event, gate_envelope
from kiro_crew.acp.client import (
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
    _resolve_pi_acp_bin,
    _resolve_pi_bin,
    _seal_pi_gate_extension,
    pi_gate_extension_path,
)
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import (
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    Routing,
    gate_probe_command_for,
    routing_for,
)
from kiro_crew.acp_tool_gate import gate_extension_issue
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_ENV_PI_ACP_BIN = "PI_ACP_BIN"
_ENV_PI_COMMAND = "PI_ACP_PI_COMMAND"
PROBE = gate_probe_command_for(ACP_BACKEND_PI)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_ambient_pi_env(monkeypatch):
    """Neither override may leak in from the developer's own shell."""
    monkeypatch.delenv(_ENV_PI_ACP_BIN, raising=False)
    monkeypatch.delenv(_ENV_PI_COMMAND, raising=False)


def _registry(*entries: tuple[str, str | None]) -> list:
    """A pi ``get_commands`` list: ``(name, source path)`` pairs, ``None`` for built-ins."""
    out = []
    for name, path in entries:
        entry: dict = {"name": name, "description": ""}
        if path is not None:
            entry["source"] = "extension"
            entry["sourceInfo"] = {"path": path, "source": "cli"}
        out.append(entry)
    return out


def _response(commands: list, request_id: str = "kiro-crew-gate-readback") -> str:
    return json.dumps(
        {
            "id": request_id,
            "type": "response",
            "command": "get_commands",
            "success": True,
            "data": {"commands": commands},
        }
    )


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
        monkeypatch.setattr(acp_client, "_pi_gate_run_dir", lambda: str(run_dir))
        monkeypatch.setattr(acp_client, "_pi_gate_launcher_cache", {})
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
        monkeypatch.setattr(acp_client, "_pi_gate_run_dir", lambda: str(run_dir))
        monkeypatch.setattr(acp_client, "_pi_gate_launcher_cache", {})
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
        monkeypatch.setattr(acp_client, "pi_gate_extension_path", lambda: str(crlf_file))
        monkeypatch.setattr(acp_client, "_pi_gate_run_dir", lambda: str(run_dir))
        sealed = Path(_seal_pi_gate_extension())
        assert sealed.read_bytes() == lf
        # And a byte that is NOT a line ending still fails the digest.
        (tmp_path / "bad.ts").write_bytes(lf.replace(b"block: true", b"block: false", 1))
        monkeypatch.setattr(acp_client, "pi_gate_extension_path", lambda: str(tmp_path / "bad.ts"))
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
    """The harness loads a digest-verified copy in the run dir, never the package file."""

    def _run_dir(self, monkeypatch, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        monkeypatch.setattr(acp_client, "_pi_gate_run_dir", lambda: str(run_dir))
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
        monkeypatch.setattr(acp_client, "pi_gate_extension_path", lambda: str(tampered))
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

    def test_the_arm_loads_the_sealed_copy_not_the_package_file(self):
        body = _pi_arm()
        assert "_seal_pi_gate_extension" in body
        assert "pi_gate_extension_path()" not in body


class TestAMissingExtensionIsTheSameRefusal:
    def test_an_install_without_the_package_data_is_refused_by_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            acp_client, "pi_gate_extension_path", lambda: str(tmp_path / "absent.ts")
        )
        with pytest.raises(acp_client.PiGateExtensionTampered) as excinfo:
            _seal_pi_gate_extension()
        assert "cannot be read" in str(excinfo.value)


class TestGateArtifactsRefuseASharedDirectory:
    """The seal->exec window is a security property, so the tmpdir fallback is refused.

    ``sandbox._ensure_run_dir`` degrades to the system temp directory when the
    configured run directory cannot be created, with a warning. For a sandbox
    launcher that is a liveness matter; for the gate artifacts it is a bypass: a
    same-UID process can re-chmod and rewrite a file in a shared directory before
    the harness loads it. So the session is REFUSED, not degraded -- the posture the
    sandbox floor already takes when the credential mask cannot be applied.
    """

    def _point(self, monkeypatch, tmp_path, *, fallback: bool):
        cfg = tmp_path / "cfg"
        (cfg / "run").mkdir(parents=True)
        elsewhere = tmp_path / "tmpfallback"
        elsewhere.mkdir()
        # The driver binds both names at import, so the seam is the driver's own.
        monkeypatch.setattr(acp_client, "config_dir", lambda: cfg)
        monkeypatch.setattr(
            acp_client, "_ensure_run_dir", lambda: str(elsewhere if fallback else cfg / "run")
        )
        return cfg / "run", elsewhere

    def test_the_configured_run_dir_is_accepted(self, monkeypatch, tmp_path):
        run_dir, _ = self._point(monkeypatch, tmp_path, fallback=False)
        assert Path(acp_client._pi_gate_run_dir()) == run_dir

    def test_the_tempdir_fallback_refuses_the_session(self, monkeypatch, tmp_path):
        _, elsewhere = self._point(monkeypatch, tmp_path, fallback=True)
        with pytest.raises(AcpToolGateUnroutable) as excinfo:
            acp_client._pi_gate_run_dir()
        assert "run directory" in str(excinfo.value)
        assert list(elsewhere.iterdir()) == [], "nothing may be written to the fallback"

    def test_neither_artifact_is_written_under_the_fallback(self, monkeypatch, tmp_path):
        _, elsewhere = self._point(monkeypatch, tmp_path, fallback=True)
        monkeypatch.setattr(acp_client, "_pi_gate_launcher_cache", {})
        with pytest.raises(AcpToolGateUnroutable):
            _seal_pi_gate_extension()
        with pytest.raises(AcpToolGateUnroutable):
            _ensure_pi_gate_launcher(str(tmp_path / "pi"), str(tmp_path / "gate.ts"))
        assert list(elsewhere.iterdir()) == []

    def test_both_writers_go_through_the_strict_resolver(self):
        for fn in (_seal_pi_gate_extension, _ensure_pi_gate_launcher):
            source = inspect.getsource(fn)
            assert "_pi_gate_run_dir()" in source
            assert "_ensure_run_dir" not in source


class TestAdapterVersionIsNamedAtHandshake:
    """The gate contract was observed on one pi-acp release; any other is logged by name."""

    def _client(self, tmp_path, version, backend=ACP_BACKEND_PI):
        client = AcpClient(work_dir=tmp_path, acp_backend=backend)
        client._agent_version = version
        return client

    def test_the_verified_release_is_silent(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            self._client(tmp_path, acp_client.PI_ACP_VERIFIED_VERSION)._note_pi_adapter_version()
        assert not [r for r in caplog.records if "gate extension contract" in r.getMessage()]

    @pytest.mark.parametrize("version", ["0.0.34", ""])
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
        for reader in (
            AcpClient.send_message_stream,
            AcpClient._dispatch_events,
            AcpClient._read_prompt_response,
        ):
            assert "await self._tripwire_pi_gate(msg)" in inspect.getsource(reader), reader
        assert inspect.getsource(AcpClient).count("await self._tripwire_pi_gate(msg)") == 3


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

    def test_the_driver_normalizes_both_sides(self):
        source = inspect.getsource(AcpClient._verify_pi_gate)
        assert "_same_file_spelling_all(commands)" in source
        assert "_same_file_spelling(extension_path)" in source


class TestTheReadBackReportsFailureRatherThanAssuming:
    """A read-back that could not run must never read as "loaded"."""

    ARGV = ["/opt/run/kirocrew_pi_gate.sh", "--mode", "rpc", "--no-themes"]
    EXT = "/site/gate.ts"

    def _client(self, tmp_path, **kw):
        return AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI, **kw)

    def test_a_missing_launcher_is_an_issue_with_the_harness_remedy(self, tmp_path):
        issue, remedy = self._client(tmp_path)._verify_pi_gate(
            [str(tmp_path / "not-there"), "--mode", "rpc"], self.EXT
        )
        assert issue
        assert "get_commands" in remedy and PI_INSTALL_COMMAND in remedy

    def test_a_non_zero_exit_is_an_issue(self, tmp_path, monkeypatch):
        class _Completed:
            returncode = 3
            stdout = ""

        monkeypatch.setattr(acp_client.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, remedy = self._client(tmp_path)._verify_pi_gate(self.ARGV, self.EXT)
        assert "exit 3" in issue and "get_commands" in remedy

    def test_a_registry_without_the_gate_is_refused_with_the_gate_remedy(
        self, tmp_path, monkeypatch
    ):
        class _Completed:
            returncode = 0
            stdout = _response(_registry(("compact", None)))

        monkeypatch.setattr(acp_client.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, remedy = self._client(tmp_path)._verify_pi_gate(self.ARGV, self.EXT)
        assert PROBE in issue
        assert "gate extension" in remedy

    def test_the_gate_from_crews_file_is_in_force(self, tmp_path, monkeypatch):
        seen: dict = {}

        class _Completed:
            returncode = 0
            stdout = _response(_registry((PROBE, self.EXT)))

        def _fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _Completed()

        monkeypatch.setattr(acp_client.subprocess_mod, "run", _fake_run)
        assert self._client(tmp_path)._verify_pi_gate(self.ARGV, self.EXT) == ("", "")
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

        def _fake_run(argv, **kwargs):
            seen["env"] = kwargs["env"]
            return _Completed()

        monkeypatch.setattr(acp_client.subprocess_mod, "run", _fake_run)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-should-not-travel")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-travel")
        monkeypatch.setenv("KIRO_API_KEY", "should-not-travel")
        self._client(tmp_path)._verify_pi_gate(self.ARGV, self.EXT)
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

        def _fake_run(argv, **kwargs):
            seen["env"] = kwargs["env"]
            return _Completed()

        monkeypatch.setattr(acp_client.subprocess_mod, "run", _fake_run)
        monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
        client = self._client(
            tmp_path,
            extra_env={
                "PI_CODING_AGENT_DIR": str(tmp_path / "elsewhere"),
                "SLACK_BOT_TOKEN": "xoxb-overlay-must-not-bypass-the-scrub",
            },
        )
        client._verify_pi_gate(self.ARGV, self.EXT)
        assert seen["env"]["PI_CODING_AGENT_DIR"] == str(tmp_path / "elsewhere")
        assert "SLACK_BOT_TOKEN" not in seen["env"]


# ── Placement ────────────────────────────────────────────────────────────────


def _pi_arm() -> str:
    body = inspect.getsource(AcpClient._spawn).split("elif self._is_pi:", 1)[1]
    return body.split("        else:", 1)[0]


def test_the_gate_read_back_runs_off_the_event_loop() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(AcpClient._spawn)))
    offloaded = False
    bare_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "to_thread":
            for arg in node.args:
                if isinstance(arg, ast.Attribute) and arg.attr == "_verify_pi_gate":
                    offloaded = True
                if isinstance(arg, ast.Name) and arg.id == "_ensure_pi_gate_launcher":
                    pass
        elif isinstance(func, ast.Attribute) and func.attr == "_verify_pi_gate":
            bare_calls += 1
    assert offloaded, "the gate read-back must be handed to asyncio.to_thread"
    assert bare_calls == 0


def test_the_launcher_write_runs_off_the_event_loop() -> None:
    body = _pi_arm()
    assert "asyncio.to_thread(\n                _ensure_pi_gate_launcher" in body or (
        "to_thread(_ensure_pi_gate_launcher" in body
    )


def test_the_sandbox_floor_is_checked_before_any_child_is_started() -> None:
    body = _pi_arm()
    preflight_at = body.find("_sandbox_preflight")
    readback_at = body.find("_verify_pi_gate")
    assert preflight_at != -1 and readback_at != -1
    assert preflight_at < readback_at


def test_the_read_back_child_is_sandbox_wrapped_with_the_adapter_mask() -> None:
    body = _pi_arm()
    wrap_at = body.find("wrap_argv_async")
    readback_at = body.find("_verify_pi_gate")
    assert wrap_at != -1 and wrap_at < readback_at
    assert "extra_hidden_dirs=adapter_hidden_dirs" in body
    assert "readback_cleanup" in body


def test_the_read_back_runs_exactly_what_the_adapter_will_spawn() -> None:
    """The launcher plus the adapter's own arguments, so the process asked is the process served."""
    body = _pi_arm()
    assert "[self._pi_gate_launcher, *_PI_RPC_ARGS]" in body
    assert acp_client._PI_RPC_ARGS == ("--mode", "rpc", "--no-themes")


def test_a_routing_refusal_is_translated_to_the_acp_layer_type() -> None:
    body = _pi_arm()
    assert "except acp_tool_gate.ToolGateUnroutable" in body
    assert "raise AcpToolGateUnroutable" in body


def test_the_adapter_is_told_to_run_the_launcher() -> None:
    """The env section sets the adapter's own override to the verified launcher."""
    source = inspect.getsource(AcpClient._spawn)
    assert "env[_ENV_PI_ACP_PI_COMMAND] = self._pi_gate_launcher" in source
    assert acp_client._ENV_PI_ACP_PI_COMMAND == "PI_ACP_PI_COMMAND"
    assert "env[_ENV_PI_GATE_SESSION] = self._pi_gate_nonce" in source


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


def _permission_frame(message: str, title: str = "bash") -> JsonRpcMessage:
    """pi-acp's rendering of a confirm dialog, as captured live."""
    return JsonRpcMessage(
        id=0,
        method="session/request_permission",
        params={
            "sessionId": "s",
            "toolCall": {
                "toolCallId": "pi-ui-5646e0bd",
                "title": title,
                "kind": "other",
                "status": "pending",
                "rawInput": {"method": "confirm", "title": title, "message": message},
            },
            "options": [
                {"optionId": "yes", "name": "Yes", "kind": "allow_once"},
                {"optionId": "no", "name": "No", "kind": "reject_once"},
            ],
        },
    )


NONCE = "f00dfeed" * 4


def _envelope(**overrides) -> str:
    body = {
        GATE_ENVELOPE_MARKER: 1,
        "nonce": NONCE,
        "toolCallId": "call_v0prcvqi",
        "tool": "bash",
        "kind": "execute",
        "input": {"command": "echo gate-check"},
    }
    body.update(overrides)
    return json.dumps(body)


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
    monkeypatch.setattr(acp_client, "_pi_gate_run_dir", lambda: str(run_dir))
    monkeypatch.setattr(acp_client, "_pi_gate_launcher_cache", {})
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
    monkeypatch.setattr(acp_client, "_pi_gate_launcher_cache", {})
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
