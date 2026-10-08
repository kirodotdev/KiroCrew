"""ACP (Agent Client Protocol) adapter for the Antigravity CLI (agy).

Translates ACP JSON-RPC 2.0 over stdio onto agy's stream-json protocol:
    agy --input-format stream-json --output-format stream-json --dangerously-skip-permissions
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger("kiro_crew.acp.adapters.agy")


STREAM_BUFFER_LIMIT = 64 * 1024 * 1024  # 64MB buffer for large JSON-RPC lines and tool outputs


def find_agy_bin() -> str:
    """Find the agy executable on the system."""
    override = os.environ.get("AGY_BIN")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override

    on_path = shutil.which("agy")
    if on_path:
        return on_path

    local_bin = os.path.expanduser("~/.local/bin/agy")
    if os.path.isfile(local_bin) and os.access(local_bin, os.X_OK):
        return local_bin

    raise FileNotFoundError(
        "agy binary not found on PATH or ~/.local/bin/agy. " "Set AGY_BIN to the executable path."
    )


def _setup_mcp_servers(
    cwd: str,
    mcp_servers: list[dict[str, Any]],
    gemini_config_path: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], list[str]]:
    """Format and provision MCP servers in cwd/.mcp.json and ~/.gemini/config/mcp_config.json for agy.

    Returns (mcp_file_path, original_content_or_None, added_gemini_keys).
    """
    if not mcp_servers:
        return None, None, []

    mcp_path = os.path.join(cwd, ".mcp.json")
    orig_content: Optional[str] = None
    existing_servers: dict[str, Any] = {}

    if os.path.isfile(mcp_path):
        try:
            with open(mcp_path, "r", encoding="utf-8") as f:
                content = f.read()
                orig_content = content
                parsed = json.loads(content)
                if isinstance(parsed, dict) and isinstance(parsed.get("mcpServers"), dict):
                    existing_servers = dict(parsed["mcpServers"])
        except Exception:
            orig_content = None

    servers_dict: dict[str, Any] = dict(existing_servers)
    for s in mcp_servers:
        name = s.get("name")
        if not name:
            continue
        cmd = s.get("command")
        args = s.get("args", [])
        raw_env = s.get("env", {})
        env: dict[str, str] = {}
        if isinstance(raw_env, list):
            for item in raw_env:
                if isinstance(item, dict) and "name" in item:
                    env[item["name"]] = str(item.get("value", ""))
        elif isinstance(raw_env, dict):
            env = {str(k): str(v) for k, v in raw_env.items()}

        entry: dict[str, Any] = {}
        if cmd:
            entry["command"] = cmd
        if args:
            entry["args"] = args
        if env:
            entry["env"] = env
        servers_dict[name] = entry

    mcp_file: Optional[str] = mcp_path
    try:
        with open(mcp_path, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": servers_dict}, f, indent=2)
    except OSError:
        mcp_file = None

    added_gemini_keys: list[str] = []
    target_gemini_cfg = (
        gemini_config_path
        or os.environ.get("GEMINI_MCP_CONFIG")
        or os.path.expanduser("~/.gemini/config/mcp_config.json")
    )
    try:
        if os.path.isfile(target_gemini_cfg):
            with open(target_gemini_cfg, "r", encoding="utf-8") as f:
                gemini_data = json.load(f)
            if isinstance(gemini_data, dict):
                gemini_servers = gemini_data.setdefault("mcpServers", {})
                for name, entry in servers_dict.items():
                    if name not in gemini_servers:
                        added_gemini_keys.append(name)
                    gemini_servers[name] = entry
                with open(target_gemini_cfg, "w", encoding="utf-8") as f:
                    json.dump(gemini_data, f, indent=2)
    except Exception as exc:
        logger.debug("Failed updating gemini mcp_config: %s", exc)

    return mcp_file, orig_content, added_gemini_keys


class AgySession:
    """Manages an active agy stream-json subprocess session."""

    def __init__(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
        cwd: str,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        mcp_file: Optional[str] = None,
        orig_mcp_content: Optional[str] = None,
        added_gemini_keys: Optional[list[str]] = None,
        gemini_config_path: Optional[str] = None,
    ) -> None:
        self.session_id = session_id
        self.proc = proc
        self.cwd = cwd
        self.model = model
        self.effort = effort
        self.mcp_file = mcp_file
        self.orig_mcp_content = orig_mcp_content
        self.added_gemini_keys = added_gemini_keys or []
        self.gemini_config_path = (
            gemini_config_path
            or os.environ.get("GEMINI_MCP_CONFIG")
            or os.path.expanduser("~/.gemini/config/mcp_config.json")
        )
        self.cancelled = False

    async def close(self) -> None:
        """Terminate the agy subprocess and clean up session files."""
        if self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=3.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self.proc.kill()
                    await self.proc.wait()
                except ProcessLookupError:
                    pass
        if self.mcp_file and os.path.exists(self.mcp_file):
            try:
                if self.orig_mcp_content is not None:
                    with open(self.mcp_file, "w", encoding="utf-8") as f:
                        f.write(self.orig_mcp_content)
                else:
                    os.remove(self.mcp_file)
            except OSError:
                pass
        if self.added_gemini_keys and self.gemini_config_path:
            try:
                if os.path.isfile(self.gemini_config_path):
                    with open(self.gemini_config_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
                        for k in self.added_gemini_keys:
                            data["mcpServers"].pop(k, None)
                        with open(self.gemini_config_path, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2)
            except Exception:
                pass


class AgyAcpServer:
    """ACP JSON-RPC 2.0 stdio server bridging to agy."""

    def __init__(self) -> None:
        self.sessions: Dict[str, AgySession] = {}
        self.agy_bin: Optional[str] = None
        self.default_model: Optional[str] = None
        self.default_effort: Optional[str] = None

    def _write_json(self, payload: Dict[str, Any]) -> None:
        """Write a JSON-RPC message to stdout and flush."""
        try:
            line = json.dumps(payload) + "\n"
            sys.stdout.write(line)
            sys.stdout.flush()
        except (BrokenPipeError, OSError) as exc:
            logger.debug("Failed writing to stdout: %s", exc)

    def _write_response(self, req_id: Any, result: Any) -> None:
        if req_id is not None:
            self._write_json({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _write_error(self, req_id: Any, code: int, message: str) -> None:
        if req_id is not None:
            self._write_json(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": code, "message": message},
                }
            )

    def _write_notification(self, method: str, params: Any) -> None:
        self._write_json({"jsonrpc": "2.0", "method": method, "params": params})

    async def handle_initialize(self, req_id: Any, params: Dict[str, Any]) -> None:
        """Handle ACP 'initialize' request."""
        self._write_response(
            req_id,
            {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "load": True,
                },
                "agentInfo": {
                    "name": "agy",
                    "version": "1.2.11",
                },
            },
        )

    async def _spawn_agy_process(
        self,
        cwd: str,
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> tuple[asyncio.subprocess.Process, str]:
        """Spawn agy in stream-json mode and read the initial 'init' event."""
        if not self.agy_bin:
            self.agy_bin = find_agy_bin()

        cmd = [
            self.agy_bin,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
        ]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        selected_model = model or self.default_model
        if selected_model:
            cmd.extend(["--model", selected_model])
        selected_effort = effort or self.default_effort
        if selected_effort:
            cmd.extend(["--effort", selected_effort])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            limit=STREAM_BUFFER_LIMIT,
        )

        assert proc.stdout is not None
        # Read the 'init' event line from agy
        init_line = await proc.stdout.readline()
        if not init_line:
            err = ""
            if proc.stderr:
                err = (await proc.stderr.read()).decode("utf-8", errors="replace")
            raise RuntimeError(f"agy process exited without emitting init event: {err}")

        init_data = json.loads(init_line.decode("utf-8"))
        actual_id = init_data.get("conversation_id")
        if not actual_id:
            raise RuntimeError(f"agy init event did not carry conversation_id: {init_data}")

        return proc, actual_id

    async def handle_session_new(self, req_id: Any, params: Dict[str, Any]) -> None:
        """Handle ACP 'session/new' request."""
        cwd = params.get("cwd", os.getcwd())
        model = params.get("model")
        mcp_servers = params.get("mcpServers", [])
        mcp_file, orig_content, added_keys = _setup_mcp_servers(cwd, mcp_servers)
        try:
            proc, session_id = await self._spawn_agy_process(
                cwd=cwd,
                model=model,
            )
            session = AgySession(
                session_id=session_id,
                proc=proc,
                cwd=cwd,
                model=model,
                mcp_file=mcp_file,
                orig_mcp_content=orig_content,
                added_gemini_keys=added_keys,
            )
            self.sessions[session_id] = session
            self._write_response(
                req_id,
                {
                    "sessionId": session_id,
                    "modes": {
                        "currentModeId": "default",
                        "availableModes": [{"id": "default", "name": "Default"}],
                    },
                },
            )
        except Exception as exc:
            self._write_error(req_id, -32000, str(exc))

    async def handle_session_load(self, req_id: Any, params: Dict[str, Any]) -> None:
        """Handle ACP 'session/load' request."""
        session_id = params.get("sessionId")
        cwd = params.get("cwd", os.getcwd())
        mcp_servers = params.get("mcpServers", [])
        if not session_id:
            self._write_error(req_id, -32602, "missing sessionId")
            return

        mcp_file, orig_content, added_keys = _setup_mcp_servers(cwd, mcp_servers)
        try:
            proc, loaded_id = await self._spawn_agy_process(
                cwd=cwd,
                conversation_id=session_id,
            )
            session = AgySession(
                session_id=loaded_id,
                proc=proc,
                cwd=cwd,
                mcp_file=mcp_file,
                orig_mcp_content=orig_content,
                added_gemini_keys=added_keys,
            )
            self.sessions[loaded_id] = session
            self._write_response(
                req_id,
                {
                    "sessionId": loaded_id,
                    "modes": {
                        "currentModeId": "default",
                        "availableModes": [{"id": "default", "name": "Default"}],
                    },
                },
            )
        except Exception as exc:
            self._write_error(req_id, -32000, str(exc))

    async def handle_session_prompt(self, req_id: Any, params: Dict[str, Any]) -> None:
        """Handle ACP 'session/prompt' request."""
        session_id = params.get("sessionId")
        session = self.sessions.get(session_id or "")

        # Auto-reconnect if session process is dead or not found in memory
        if not session or not session.proc or session.proc.returncode is not None:
            if session_id:
                try:
                    logger.info(
                        "Session %s inactive; auto-reconnecting via --conversation", session_id
                    )
                    cwd = session.cwd if session else (params.get("cwd") or os.getcwd())
                    model = session.model if session else params.get("model")
                    effort = session.effort if session else params.get("effort")
                    mcp_file = session.mcp_file if session else None
                    orig_content = session.orig_mcp_content if session else None
                    added_keys = session.added_gemini_keys if session else []
                    gemini_cfg = session.gemini_config_path if session else None

                    proc, recovered_id = await self._spawn_agy_process(
                        cwd=cwd,
                        conversation_id=session_id,
                        model=model,
                        effort=effort,
                    )
                    if session:
                        session.proc = proc
                        session.cancelled = False
                    else:
                        session = AgySession(
                            session_id=recovered_id,
                            proc=proc,
                            cwd=cwd,
                            model=model,
                            effort=effort,
                            mcp_file=mcp_file,
                            orig_mcp_content=orig_content,
                            added_gemini_keys=added_keys,
                            gemini_config_path=gemini_cfg,
                        )
                        self.sessions[recovered_id] = session
                except Exception as exc:
                    logger.exception("Failed to reconnect session %s: %s", session_id, exc)
                    self._write_error(req_id, -32001, f"session {session_id} not active: {exc}")
                    return
            else:
                self._write_error(req_id, -32001, f"session {session_id} not active")
                return

        session.cancelled = False

        prompt_blocks = params.get("prompt", [])
        prompt_text = "".join(
            block.get("text", "")
            for block in prompt_blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if not prompt_text:
            prompt_text = str(params.get("prompt", ""))

        msg_payload = {"event": "user", "message": {"content": prompt_text}}
        assert session.proc.stdin is not None
        assert session.proc.stdout is not None

        try:
            session.proc.stdin.write((json.dumps(msg_payload) + "\n").encode("utf-8"))
            await session.proc.stdin.drain()

            has_emitted_text = False
            session_tool_calls: dict[str, str] = {}
            while True:
                if session.cancelled:
                    self._write_response(req_id, {"stopReason": "cancelled"})
                    return

                try:
                    line = await session.proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    logger.warning("Dropped oversize line from agy stdout: %s", exc)
                    continue

                if not line:
                    break
                try:
                    data = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                event = data.get("event")
                if event == "step_update":
                    update = data.get("step_update", {})
                    step_type = update.get("step_type")
                    if step_type == "agent_response" and "text_delta" in update:
                        delta = update["text_delta"]
                        if delta:
                            has_emitted_text = True
                            self._write_notification(
                                "session/update",
                                {
                                    "sessionId": session_id,
                                    "update": {
                                        "sessionUpdate": "agent_message_chunk",
                                        "type": "agent_message_chunk",
                                        "content": {
                                            "type": "text",
                                            "text": delta,
                                        },
                                    },
                                },
                            )
                    elif step_type == "tool":
                        raw_name = update.get("tool_name") or update.get("tool_info", {}).get(
                            "name", "tool"
                        )
                        call_id = f"call_{update.get('step_index', 0)}"
                        tool_info = update.get("tool_info", {})
                        params_obj = tool_info.get("parameters", {})

                        tool_name = raw_name
                        tool_input = params_obj
                        if raw_name in ("call_mcp_tool", "mcp") and isinstance(params_obj, dict):
                            server = (
                                params_obj.get("ServerName")
                                or params_obj.get("server_name")
                                or params_obj.get("server")
                            )
                            tool = (
                                params_obj.get("ToolName")
                                or params_obj.get("tool_name")
                                or params_obj.get("tool")
                            )
                            if server and tool:
                                tool_name = f"mcp__{server}__{tool}"
                                tool_input = (
                                    params_obj.get("Arguments")
                                    or params_obj.get("arguments")
                                    or params_obj.get("args")
                                    or {}
                                )

                        state = update.get("state")
                        if state == "ACTIVE":
                            session_tool_calls[call_id] = tool_name
                            self._write_notification(
                                "session/update",
                                {
                                    "sessionId": session_id,
                                    "update": {
                                        "sessionUpdate": "tool_call",
                                        "type": "tool_call",
                                        "toolCallId": call_id,
                                        "title": tool_name,
                                        "name": tool_name,
                                        "input": tool_input,
                                    },
                                },
                            )
                        elif state == "DONE":
                            tool_name = session_tool_calls.get(call_id, tool_name)
                            output = tool_info.get("output", "")
                            self._write_notification(
                                "session/update",
                                {
                                    "sessionId": session_id,
                                    "update": {
                                        "sessionUpdate": "tool_call_update",
                                        "type": "tool_call_update",
                                        "toolCallId": call_id,
                                        "title": tool_name,
                                        "name": tool_name,
                                        "status": "completed",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": str(output),
                                            }
                                        ],
                                    },
                                },
                            )
                elif event == "result":
                    res_obj = data.get("result", {})
                    resp_text = res_obj.get("response")
                    if not has_emitted_text and resp_text:
                        self._write_notification(
                            "session/update",
                            {
                                "sessionId": session_id,
                                "update": {
                                    "sessionUpdate": "agent_message_chunk",
                                    "type": "agent_message_chunk",
                                    "content": {
                                        "type": "text",
                                        "text": resp_text,
                                    },
                                },
                            },
                        )
                    usage = res_obj.get("usage", {})
                    if usage:
                        self._write_notification(
                            "session/update",
                            {
                                "sessionId": session_id,
                                "update": {
                                    "sessionUpdate": "usage_update",
                                    "used": usage.get("total_tokens") or usage.get("input_tokens"),
                                    "size": 1000000,
                                },
                            },
                        )
                    self._write_response(req_id, {"stopReason": "end_turn"})
                    return

            # Subprocess ended without emitting result event
            if session.cancelled:
                self._write_response(req_id, {"stopReason": "cancelled"})
                return

            ret_code = session.proc.returncode
            err_msg = ""
            if session.proc.stderr:
                try:
                    err_bytes = await session.proc.stderr.read()
                    err_msg = err_bytes.decode("utf-8", errors="replace").strip()
                except Exception:
                    pass
            logger.error(
                "agy subprocess ended before result for session %s (returncode=%s): %s",
                session_id,
                ret_code,
                err_msg,
            )
            self._write_error(
                req_id,
                -32000,
                f"agy process ended unexpectedly (code={ret_code}): {err_msg or 'stdout closed'}",
            )
        except Exception as exc:
            self._write_error(req_id, -32000, f"prompt turn failed: {exc}")

    async def handle_session_steer(self, req_id: Any, params: Dict[str, Any]) -> None:
        """Handle ACP '_session/steer' (mid-turn steer)."""
        session_id = params.get("sessionId")
        message = params.get("message", "")
        session = self.sessions.get(session_id or "")
        if not session or not session.proc or session.proc.returncode is not None:
            self._write_error(req_id, -32001, f"session {session_id} not active")
            return

        self._write_notification(
            "session/update",
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "steering_queued",
                    "content": message,
                },
            },
        )

        if session.proc.stdin:
            msg_payload = {"event": "user", "message": {"content": message}}
            try:
                session.proc.stdin.write((json.dumps(msg_payload) + "\n").encode("utf-8"))
                await session.proc.stdin.drain()
            except Exception as exc:
                logger.warning("Failed writing steer to agy stdin: %s", exc)

        self._write_notification(
            "session/update",
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "steering_consumed",
                    "content": message,
                },
            },
        )
        if req_id is not None:
            self._write_response(req_id, {"queued": True})

    async def handle_session_cancel(self, req_id: Any, params: Dict[str, Any]) -> None:
        """Handle ACP 'session/cancel'."""
        session_id = params.get("sessionId")
        session = self.sessions.get(session_id or "")
        if session:
            session.cancelled = True
            if session.proc and session.proc.returncode is None:
                try:
                    session.proc.terminate()
                except Exception:
                    pass
        if req_id is not None:
            self._write_response(req_id, {})

    async def handle_session_close(self, req_id: Any, params: Dict[str, Any]) -> None:
        """Handle ACP 'session/close'."""
        session_id = params.get("sessionId")
        if session_id and session_id in self.sessions:
            session = self.sessions.pop(session_id)
            await session.close()
        self._write_response(req_id, {})

    async def handle_set_config_option(self, req_id: Any, params: Dict[str, Any]) -> None:
        """Handle ACP 'session/set_config_option'."""
        config_id = params.get("configId")
        value = params.get("value")
        if config_id == "model":
            self.default_model = str(value)
        elif config_id == "effort":
            self.default_effort = str(value)
        self._write_response(req_id, {})

    async def handle_set_model(self, req_id: Any, params: Dict[str, Any]) -> None:
        """Handle ACP 'session/set_model'."""
        model_id = params.get("modelId") or params.get("model")
        if model_id:
            self.default_model = str(model_id)
        self._write_response(req_id, {})

    async def dispatch_request(self, message: Dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message."""
        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params", {})

        if method == "initialize":
            await self.handle_initialize(req_id, params)
        elif method == "session/new":
            await self.handle_session_new(req_id, params)
        elif method == "session/load" or method == "session/resume":
            await self.handle_session_load(req_id, params)
        elif method == "session/prompt":
            await self.handle_session_prompt(req_id, params)
        elif method in ("_session/steer", "session/steer"):
            await self.handle_session_steer(req_id, params)
        elif method == "session/cancel":
            await self.handle_session_cancel(req_id, params)
        elif method == "session/close":
            await self.handle_session_close(req_id, params)
        elif method == "session/set_config_option":
            await self.handle_set_config_option(req_id, params)
        elif method == "session/set_model":
            await self.handle_set_model(req_id, params)
        elif method == "notifications/initialized":
            # Client notification: ignore
            pass
        else:
            if req_id is not None:
                self._write_error(req_id, -32601, f"method {method!r} not found")

    async def run(self) -> None:
        """Main event loop reading JSON-RPC messages from stdin."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=STREAM_BUFFER_LIMIT)
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            try:
                line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                logger.warning("Dropped oversize line from stdin: %s", exc)
                continue
            except Exception as exc:
                logger.error("Error reading stdin: %s", exc)
                break

            if not line:
                break
            try:
                message = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue

            if isinstance(message, dict):
                try:
                    await self.dispatch_request(message)
                except Exception as exc:
                    logger.exception("Error dispatching ACP request: %s", exc)
                    req_id = message.get("id")
                    if req_id is not None:
                        try:
                            self._write_error(req_id, -32603, f"Internal error: {exc}")
                        except Exception:
                            pass

        # Cleanup all sessions on exit
        for session in list(self.sessions.values()):
            await session.close()


def main() -> None:
    """CLI entry point for agy-acp adapter."""
    asyncio.run(AgyAcpServer().run())


if __name__ == "__main__":
    main()
