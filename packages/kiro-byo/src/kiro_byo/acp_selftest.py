"""Non-destructive, real-stdio ACP compatibility check for the installed BYO.

This is not a KiroCrew permission, Worker, or UI test: it runs a BYO child
against an isolated *sanitized metadata-only* provider config. It never sends
session/prompt, tool calls, MCP servers or requests to model providers.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from .acp_preflight import ApprovedAcpProcess, ExternalAcpProbe, ProbeRefused
from .config import config_path


_SECRET_PARTS = ("api_key", "apikey", "secret", "credential", "authorization", "token", "headers", "password")


def _safe_metadata(value: Any) -> Any:
    """Strip secrets recursively before copying a user's config to a test home."""
    if isinstance(value, dict):
        return {key: _safe_metadata(item) for key, item in value.items()
                if isinstance(key, str) and not any(part in key.lower() for part in _SECRET_PARTS)}
    if isinstance(value, list):
        return [_safe_metadata(v) for v in value]
    return value


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _metadata_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"credential_schema_version": 1, "providers": []}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("providers.json must be a JSON object")
    safe = _safe_metadata(data)
    safe["credential_schema_version"] = 1
    return safe


def _approved_process(temp: Path, mode: str, config: dict[str, Any]) -> ApprovedAcpProcess:
    home = temp / mode / "byo-home"
    workspace = temp / mode / "workspace"
    (home / "config").mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    configured = dict(config)
    configured["mode"] = {"id": mode, "type": "multi" if mode == "tiga" else "flash"}
    (home / "config" / "providers.json").write_text(json.dumps(configured, ensure_ascii=False), encoding="utf-8")
    exe = Path(sys.executable).resolve(strict=True)
    env = {
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "KIRO_BYO_HOME": str(home),
        "KIROCREW_HOME": str(temp / mode / "crew-home"),
        "HOME": str(home),
    }
    if os.name == "nt":
        # These are OS runtime requirements, not inherited model credentials.
        for name in ("SYSTEMROOT", "WINDIR", "PATHEXT"):
            if name in os.environ:
                env[name] = os.environ[name]
        env["APPDATA"] = str(home)
        env["LOCALAPPDATA"] = str(home)
    return ApprovedAcpProcess(
        backend_id="kiro-byo", label="Kiro BYO", executable=exe,
        executable_sha256=_sha(exe), approval_root=exe.parent,
        argv=("-m", "kiro_byo", "acp"), cwd=workspace, env=env,
    )


def check_acp(*, models: tuple[str, ...] = (), config_file: Path | None = None) -> dict[str, Any]:
    """Validate two Mode→Type→fixed-model ACP paths with real child processes.

    `models` is an explicit model display-name allowlist when supplied; missing
    names are errors rather than silently using a different vendor's model.
    """
    report: dict[str, Any] = {
        "ok": False,
        "verification_level": "BYO real ACP subprocess (isolated metadata only)",
        "crew_host_registration": "not_verified",
        "crew_tool_worker_mcp_enforcement": "not_verified",
        "dashboard_selection_capabilities": "not_verified",
        "windows_desktop_end_to_end": "not_verified",
        "provider_network_calls": 0,
        "mcp_servers_sent": 0,
        "modes": [],
    }
    try:
        cfg = _metadata_config(config_file or config_path())
        with tempfile.TemporaryDirectory(prefix="kiro-byo-acp-selftest-") as tmp:
            base = Path(tmp)
            encountered: set[str] = set()
            for mode, default_type in (("tiga", "multi"), ("dyna", "flash")):
                spec = _approved_process(base, mode, cfg)
                with ExternalAcpProbe(spec, reserved_ids=frozenset({"kas", "codex"}), timeout=15) as child:
                    initialized = child.initialize()
                    created = child.new_session()
                    sid = created["sessionId"]
                    type_model = f"kiro-byo-type:{mode}:{default_type}"
                    if created.get("models", {}).get("currentModelId") != type_model:
                        raise AssertionError(f"{mode} Type default not advertised as expected")
                    # For this Type, effort is automatic: do not ever advertise a
                    # stale strength selector from a prior fixed-model session.
                    try:
                        child.select_effort(sid, "high")
                    except ProbeRefused:
                        pass
                    else:
                        raise AssertionError("Type offered a manual effort selector")
                    model_ids = child._model_ids(created)
                    target_models = [name for name in models if name in model_ids] if models else [
                        name for name in sorted(model_ids)
                        if not name.startswith("kiro-byo-type:") and not name.startswith("kiro-byo/")
                    ][:20]
                    if models:
                        encountered.update(target_models)
                    effort_checks = []
                    for name in target_models:
                        response = child.select_model(sid, name)
                        options = child._advertised_options.get(sid, {})
                        levels = sorted(options.get("effortLevel", ()))
                        if levels:
                            # A concrete advertised value must round-trip through
                            # the real agent. This does not invoke model providers.
                            value = "max" if "max" in levels else levels[-1]
                            changed = child.select_effort(sid, value)
                            chosen = next((x for x in changed.get("configOptions", [])
                                           if x.get("id") == "effortLevel"), {})
                            if chosen.get("currentValue") != value:
                                raise AssertionError(f"{name}: effort {value!r} not applied")
                        effort_checks.append({"model": name, "effort_levels": levels,
                                              "effort_round_trip": bool(levels)})
                    child.select_model(sid, type_model)
                    if child._advertised_options.get(sid, {}).get("effortLevel"):
                        raise AssertionError("stale fixed-model effort after Type switch")
                    child.cancel_session(sid)  # ACP notification, no active model turn
                    child.close_session(sid)
                    restored = child.load_session(sid)
                    if restored.get("models", {}).get("currentModelId") != type_model:
                        raise AssertionError("session/load did not restore Type selection")
                    try:
                        child._request("session/prompt", {"sessionId": sid, "prompt": []})
                    except ProbeRefused:
                        pass
                    else:
                        raise AssertionError("protocol-only preflight must deny prompts")
                    report["modes"].append({
                        "mode": mode, "protocol_version": initialized["protocolVersion"],
                        "type": type_model, "fixed_models_tested": effort_checks,
                        "session_load": True, "cancel_notification": True,
                        "prompt_denied_by_preflight": True,
                    })
            missing = sorted(set(models) - encountered)
            if missing:
                raise ValueError("requested models not advertised: " + ", ".join(missing))
        report["ok"] = True
    except (OSError, ValueError, ProbeRefused, AssertionError, TimeoutError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report
