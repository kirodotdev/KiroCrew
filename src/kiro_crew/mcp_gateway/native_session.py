"""Route the final native ACP tool roster through the outside Windows broker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.mcp_gateway.hashing import STUB_FLAGS_FLAG, encode_target_args, hash_command


def broker_entries(
    entries: list[dict[str, Any]],
    *,
    agent_name: str,
    work_dir: str | Path,
    socket_path: Path,
) -> list[dict[str, Any]]:
    """Wrap only canonical managed launches already granted by the session."""
    from kiro_crew.agent import managed_mcp_spec_entry

    result = []
    for entry in entries:
        name = entry.get("name")
        if name not in KIROCREW_BIN_MCP_SERVERS or "kiro_crew.mcp_gateway.stub" in entry.get(
            "args", []
        ):
            result.append(entry)
            continue
        managed = managed_mcp_spec_entry(name, include_opt_in=True)
        if not managed or hash_command(
            entry.get("command", ""), entry.get("args", [])
        ) != hash_command(managed["command"], managed.get("args", [])):
            raise PermissionError("Native MCP managed launch does not match its host declaration")
        flags = [
            "--server",
            name,
            "--agent",
            agent_name,
            "--target-command",
            managed["command"],
            "--target-args-b64",
            encode_target_args(managed.get("args", [])),
            "--work-dir",
            str(work_dir),
            "--socket",
            str(socket_path),
            "--env-json",
            json.dumps(managed.get("env", {}), sort_keys=True),
        ]
        # Managed env contains the host's routing paths. Session authority is
        # attached separately, and no declaration supplied by the agent is used.
        argv = platform_compat.isolated_python_argv(
            "-P",
            "-m",
            "kiro_crew.mcp_gateway.stub",
            f"{STUB_FLAGS_FLAG}={encode_target_args(flags)}",
        )
        result.append({**entry, "command": argv[0], "args": argv[1:], "env": []})
    return result


async def finalize(
    runtime: Any,
    entries: list[dict[str, Any]],
    *,
    agent_name: str,
    work_dir: str | Path,
    session_key: str,
    token: str,
) -> tuple[list[dict[str, Any]], str]:
    """Bind authority after member and panel entries have joined the array."""
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.config.paths import data_home
    from kiro_crew.mcp_gateway.claim import send_claim
    from kiro_crew.mcp_gateway.native_scope import attach
    from kiro_crew.mcp_gateway.session_servers import attach_stub_session_token

    if not any(entry.get("name") in KIROCREW_BIN_MCP_SERVERS for entry in entries):
        return list(entries), token

    def prepare() -> tuple[list[dict[str, Any]], Path]:
        cfg = KiroCrewConfig.load()
        if any(
            entry.get("name") in KIROCREW_BIN_MCP_SERVERS
            and entry["name"] not in cfg.mcp_gateway.stub_servers
            for entry in entries
        ):
            raise PermissionError(
                "Native MCP broker routing must be enabled for managed runtime tools"
            )
        socket = (
            Path(cfg.mcp_gateway.socket_path).expanduser()
            if cfg.mcp_gateway.socket_path
            else data_home() / "mcp-gateway/gateway.sock"
        )
        return (
            broker_entries(entries, agent_name=agent_name, work_dir=work_dir, socket_path=socket),
            socket,
        )

    prepared, socket = await asyncio.to_thread(prepare)
    if not token:
        prepared, token = await runtime._own_stub_session(prepared, session_key)
    else:
        prepared = attach_stub_session_token(prepared, token)
    if not token:
        raise PermissionError("Native MCP session authority is unavailable")
    # The base roster can be empty when its token is minted. Late mounts
    # therefore need their claim here, even when they already carry that token.
    if session_key:
        await send_claim(str(socket), runtime.pid, session_key, stub_session_token=token)
    sealed = await asyncio.to_thread(attach, prepared, Path(runtime._scratch_dir))
    return sealed, token
