"""Crew 0.12 external ACP **protocol-only** proof of concept (R76).

Host-owned, stdlib-only subprocess bridge for one *operator-approved* backend ID.
It is intentionally NOT registered with Crew's selectable production backends:
ACP v1 handshake does not establish OS confinement or Worker/MCP tool enforcement.

The bridge exposes only initialize, session/new|load|close|cancel, and model
selection. It never sends session/prompt or supplies MCP servers, and denies all
server-originated requests. The API is stable across changing ACP executables;
future Crew integration must supply an independently verified execution gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import json
import os
import queue
import re
import subprocess
import threading
import time
from typing import Any, Mapping

_BACKEND_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_MAX_LINE = 1024 * 1024
_MAX_PENDING_NOTIFICATIONS = 128
_DEFAULT_TIMEOUT = 10.0
_ALLOWED_METHODS = frozenset({
    "initialize", "session/new", "session/load", "session/set_config_option",
    "session/set_model", "session/close",
})


class ProbeRefused(RuntimeError):
    """Untrusted executable, invalid ACP or forbidden operation."""


@dataclass(frozen=True)
class ApprovedAcpProcess:
    """HOST-owned approval, never constructed from an untrusted ACP manifest."""
    backend_id: str
    label: str
    executable: Path
    executable_sha256: str
    argv: tuple[str, ...]
    approval_root: Path
    cwd: Path
    env: Mapping[str, str]

    def validate(self, reserved_ids: frozenset[str]) -> None:
        if not _BACKEND_ID.fullmatch(self.backend_id) or self.backend_id in reserved_ids:
            raise ProbeRefused("invalid, non-canonical or reserved backend ID")
        if not self.label.strip() or len(self.label) > 96:
            raise ProbeRefused("invalid harness label")
        if not self.argv or len(self.argv) > 16 or any(
            not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 1024
            for arg in self.argv
        ):
            raise ProbeRefused("invalid ACP argv")
        if (not self.executable.is_absolute() or self.executable.is_symlink()
                or not self.executable.is_file()):
            raise ProbeRefused("executable must be an absolute regular non-symlink file")
        if (not self.approval_root.is_absolute()
                or not self.executable.resolve(strict=True).is_relative_to(
                    self.approval_root.resolve(strict=True))):
            raise ProbeRefused("executable outside the approved install root")
        if not self.cwd.is_absolute() or not self.cwd.is_dir():
            raise ProbeRefused("invalid working directory")
        if not re.fullmatch("[a-f0-9]{64}", self.executable_sha256):
            raise ProbeRefused("invalid operator-approved hash")
        stat = self.executable.stat()
        if stat.st_size <= 0 or stat.st_size > 200 * 1024 * 1024:
            raise ProbeRefused("executable size is out of bounds")
        digest = sha256()
        with self.executable.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != self.executable_sha256:
            raise ProbeRefused("executable changed since host approval")
        if not isinstance(self.env, Mapping) or any(
            not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
            or not isinstance(v, str) or "\x00" in v or len(v) > 4096
            for k, v in self.env.items()
        ):
            raise ProbeRefused("invalid explicit environment")


class ExternalAcpProbe:
    """Only the *control plane* of an operator-approved ACP child; never a tool host.

    This POC does not isolate its child from the OS. It MUST NOT be wired to
    sessions that issue prompts, privileged Worker actions or credentials.
    """

    def __init__(self, approved: ApprovedAcpProcess, *, reserved_ids: frozenset[str],
                 timeout: float = _DEFAULT_TIMEOUT):
        approved.validate(reserved_ids)
        if not 0.1 <= timeout <= 60:
            raise ProbeRefused("invalid timeout")
        self.approved = approved
        self.timeout = timeout
        self._next_id = 0
        self._closed = False
        self._events: queue.Queue[Any] = queue.Queue(maxsize=_MAX_PENDING_NOTIFICATIONS + 2)
        self._advertised_models: dict[str, frozenset[str]] = {}
        # Per-session selectable option vocabulary is read from the *current*
        # ACP response, never inferred from a model ID or vendor name.
        self._advertised_options: dict[str, dict[str, frozenset[str]]] = {}
        self._stderr_tail: list[str] = []
        # Only explicit host-supplied environment reaches the child. Do not inherit
        # arbitrary API keys/tokens from the Crew process into a third-party agent.
        self._process = subprocess.Popen(
            [str(approved.executable), *approved.argv],
            cwd=str(approved.cwd), env=dict(approved.env),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        try:
            while True:
                raw = self._process.stdout.readline(_MAX_LINE + 1)
                if not raw:
                    self._events.put(ProbeRefused("ACP subprocess closed stdout"))
                    return
                if len(raw) > _MAX_LINE or not raw.endswith(b"\n"):
                    self._events.put(ProbeRefused("oversized or unterminated ACP frame"))
                    return
                try:
                    data = json.loads(raw)
                except (UnicodeError, ValueError) as exc:
                    self._events.put(ProbeRefused(f"invalid JSONL ACP frame: {exc}"))
                    return
                if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
                    self._events.put(ProbeRefused("ACP frame must be a JSON-RPC 2.0 object"))
                    return
                self._events.put(data)
        except (OSError, ValueError) as exc:
            self._events.put(ProbeRefused(f"reading ACP pipe failed: {exc}"))

    def _drain_stderr(self) -> None:
        assert self._process.stderr is not None
        try:
            for line in iter(self._process.stderr.readline, b""):
                if len(self._stderr_tail) >= 8:
                    self._stderr_tail.pop(0)
                self._stderr_tail.append(line[:500].decode("utf-8", "replace"))
        except (OSError, ValueError):
            pass

    def _send(self, msg: dict[str, Any]) -> None:
        assert self._process.stdin is not None
        encoded = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(encoded) > _MAX_LINE:
            raise ProbeRefused("oversized outgoing ACP frame")
        try:
            self._process.stdin.write(encoded)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ProbeRefused(f"ACP write failed: {exc}") from exc

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise ProbeRefused("ACP child already closed")
        if method not in _ALLOWED_METHODS:
            raise ProbeRefused("ACP method is not permitted in protocol-only mode")
        if method in {"session/new", "session/load"} and (
            params.get("mcpServers") != [] or params.get("cwd") != str(self.approved.cwd)
        ):
            raise ProbeRefused("external ACP probing forbids MCP or unapproved cwd")
        if method in {"session/set_config_option", "session/set_model"}:
            sid = self._sid(params.get("sessionId"))
            if method == "session/set_config_option":
                option_id = params.get("configId")
                value = params.get("value")
                if not isinstance(option_id, str):
                    raise ProbeRefused("invalid ACP config option ID")
                if option_id == "model":
                    allowed = self._advertised_models.get(sid, frozenset())
                elif option_id in {"effortLevel", "thinkingMode", "host_tool_approval"}:
                    allowed = self._advertised_options.get(sid, {}).get(option_id, frozenset())
                else:
                    raise ProbeRefused("ACP option is outside the read-only capability negotiation allowlist")
                if option_id == "host_tool_approval" and value != "required": raise ProbeRefused("cannot downgrade host gate")
                if not isinstance(value, str) or value not in allowed:
                    raise ProbeRefused(f"ACP {option_id} value was not advertised for this session")
            elif params.get("modelId") not in self._advertised_models.get(sid, frozenset()):
                raise ProbeRefused("model ID was not advertised by this ACP session")
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        ignored = 0
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeRefused(f"ACP {method} timed out")
            try:
                event = self._events.get(timeout=remaining)
            except queue.Empty as exc:
                raise ProbeRefused(f"ACP {method} timed out") from exc
            if isinstance(event, Exception):
                raise event
            if "method" in event:
                if "id" in event:
                    if not (type(event["id"]) in (str, int) or event["id"] is None):
                        raise ProbeRefused("invalid server request ID")
                    # Never grant a tool/permission/credential request from this
                    # control-plane proof; explicit failure is safer than silence.
                    self._send({"jsonrpc": "2.0", "id": event["id"],
                                "error": {"code": -32601, "message": "probe host denies all agent requests"}})
                ignored += 1
                if ignored > _MAX_PENDING_NOTIFICATIONS:
                    raise ProbeRefused("ACP message flood")
                continue
            if event.get("id") == request_id:
                if "error" in event:
                    raise ProbeRefused(f"ACP {method} refused: {event['error']}")
                result = event.get("result")
                if not isinstance(result, dict):
                    raise ProbeRefused(f"ACP {method} returned no result object")
                # A model change invalidates the *old* effort choices even when
                # the agent did not return new configOptions (ACP v1 fallback).
                # Never retain stale permission to send an option for another model.
                if method in {"session/set_config_option", "session/set_model"}:
                    sid = self._sid(params["sessionId"])
                    changing_model = method == "session/set_model" or params.get("configId") == "model"
                    self._remember_options(sid, result, clear_if_missing=changing_model)
                return result
            ignored += 1
            if ignored > _MAX_PENDING_NOTIFICATIONS:
                raise ProbeRefused("ACP unrelated response flood")

    def initialize(self) -> dict[str, Any]:
        result = self._request("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                              "clientInfo": {"name": "crew-external-acp-probe", "version": "0.1"}})
        if type(result.get("protocolVersion")) is not int or result["protocolVersion"] != 1:
            raise ProbeRefused("unsupported ACP protocol version")
        return result

    def new_session(self) -> dict[str, Any]:
        result = self._request("session/new", {"cwd": str(self.approved.cwd), "mcpServers": []})
        if not isinstance(result.get("sessionId"), str) or not result["sessionId"]:
            raise ProbeRefused("ACP returned no sessionId")
        self._remember_models(result["sessionId"], result)
        return result

    @staticmethod
    def _model_ids(result: dict[str, Any]) -> frozenset[str]:
        ids: set[str] = set()
        models = result.get("models")
        if isinstance(models, dict) and isinstance(models.get("availableModels"), list):
            ids.update(row["modelId"] for row in models["availableModels"]
                       if isinstance(row, dict) and isinstance(row.get("modelId"), str))
        options = result.get("configOptions")
        if isinstance(options, list):
            for option in options:
                if isinstance(option, dict) and option.get("id") == "model":
                    rows = option.get("options", [])
                    if isinstance(rows, list):
                        ids.update(row["value"] for row in rows
                                   if isinstance(row, dict) and isinstance(row.get("value"), str))
        return frozenset(ids)

    def _remember_models(self, session_id: str, result: dict[str, Any]) -> None:
        self._advertised_models[session_id] = self._model_ids(result)
        self._remember_options(session_id, result, clear_if_missing=True)

    def _remember_options(self, session_id: str, result: dict[str, Any], *,
                          clear_if_missing: bool) -> None:
        """Reconcile only explicitly advertised, current ACP config options."""
        options = result.get("configOptions")
        if not isinstance(options, list):
            if clear_if_missing:
                self._advertised_options.pop(session_id, None)
            return
        allowed: dict[str, frozenset[str]] = {}
        for option in options:
            if not isinstance(option, dict):
                continue
            option_id = option.get("id")
            if not isinstance(option_id, str) or option_id not in {"effortLevel", "thinkingMode", "host_tool_approval"}:
                continue
            values = option.get("options")
            if option.get("type") != "select" or not isinstance(values, list):
                continue
            members = [item.get("value") for item in values if isinstance(item, dict)]
            if members and all(isinstance(v, str) and v and len(v) <= 128 for v in members) \
                    and len(members) == len(set(members)):
                allowed[option_id] = frozenset(members)
        self._advertised_options[session_id] = allowed

    def select_effort(self, session_id: str, value: str) -> dict[str, Any]:
        """Set an *advertised* effort; Type routing and unknown levels fail closed.

        Protocol-only: this does not authorize session/prompt, tools or MCP.
        """
        return self._request("session/set_config_option", {
            "sessionId": self._sid(session_id), "configId": "effortLevel", "value": value,
        })

    def select_thinking(self, session_id: str, value: str) -> dict[str, Any]:
        return self._request("session/set_config_option", {
            "sessionId": self._sid(session_id), "configId": "thinkingMode", "value": value,
        })

    def load_session(self, session_id: str) -> dict[str, Any]:
        sid = self._sid(session_id)
        result = self._request("session/load", {"cwd": str(self.approved.cwd),
                                                "mcpServers": [], "sessionId": sid})
        self._remember_models(sid, result)
        return result

    def select_model(self, session_id: str, model_id: str, *, v3: bool = True) -> dict[str, Any]:
        if not isinstance(model_id, str) or not model_id or len(model_id) > 512:
            raise ProbeRefused("invalid model ID")
        if model_id not in self._advertised_models.get(self._sid(session_id), frozenset()):
            raise ProbeRefused("model ID was not advertised by this ACP session")
        if v3:
            return self._request("session/set_config_option", {"sessionId": self._sid(session_id),
                                                                  "configId": "model", "value": model_id})
        return self._request("session/set_model", {"sessionId": self._sid(session_id), "modelId": model_id})

    def close_session(self, session_id: str) -> dict[str, Any]:
        sid = self._sid(session_id)
        result = self._request("session/close", {"sessionId": sid})
        self._advertised_models.pop(sid, None)
        self._advertised_options.pop(sid, None)
        return result

    def cancel_session(self, session_id: str) -> None:
        """Emit the ACP cancellation *notification*; the protocol returns no result.

        This does not prove interrupting a real model turn: prompting is disabled.
        """
        if self._closed:
            raise ProbeRefused("ACP child already closed")
        self._send({"jsonrpc": "2.0", "method": "session/cancel",
                    "params": {"sessionId": self._sid(session_id)}})

    @staticmethod
    def _sid(sid: str) -> str:
        if not isinstance(sid, str) or not sid or len(sid) > 256:
            raise ProbeRefused("invalid session ID")
        return sid

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.terminate()
            self._process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self._process.kill()
            self._process.wait(timeout=2)
        finally:
            if self._process.stdout:
                self._process.stdout.close()
            if self._process.stderr:
                self._process.stderr.close()

    def __enter__(self) -> "ExternalAcpProbe":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class AcpProbeSwitch:
    """Explicit backend selection; switching launches a NEW independent child.

    Does not transmute session IDs between harnesses. Production registration,
    Worker execution and MCP remain deliberately outside this experimental API.
    """

    def __init__(self, approved: Mapping[str, ApprovedAcpProcess],
                 *, reserved_ids: frozenset[str]):
        if len(approved) != len(set(approved)):
            raise ProbeRefused("duplicate IDs")
        self._approved = dict(approved)
        self.reserved_ids = reserved_ids
        for backend, spec in self._approved.items():
            if backend != spec.backend_id:
                raise ProbeRefused("descriptor ID differs from registry key")
            spec.validate(reserved_ids)
        self._active: ExternalAcpProbe | None = None
        self._active_backend: str | None = None

    def select(self, backend_id: str) -> ExternalAcpProbe:
        if backend_id not in self._approved:
            raise ProbeRefused("unknown or non-approved external backend")
        # Validate every time: revoking/changing the installed binary must prevent
        # a fresh spawn, including after a successful earlier selection.
        self._approved[backend_id].validate(self.reserved_ids)
        child = ExternalAcpProbe(self._approved[backend_id], reserved_ids=self.reserved_ids)
        try:
            child.initialize()
        except BaseException:
            child.close()
            raise
        old = self._active
        self._active = child
        self._active_backend = backend_id
        # Commit the switch only AFTER the next ACP child successfully initializes.
        # An invalid new binary cannot strand the currently selected backend.
        if old is not None:
            old.close()
        return child

    @property
    def selected_backend(self) -> str | None:
        return self._active_backend

    def close(self) -> None:
        if self._active is not None:
            self._active.close()
        self._active = None
        self._active_backend = None

    def __enter__(self) -> "AcpProbeSwitch":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
