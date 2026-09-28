"""ACP (Agent Client Protocol) adapter for the Antigravity CLI (agy).

Translates ACP JSON-RPC 2.0 over stdio onto agy's stream-json protocol:
    agy --input-format stream-json --output-format stream-json
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger("kiro_crew.acp.adapters.agy")


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


class AgySession:
    """Manages an active agy stream-json subprocess session."""

    def __init__(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
        cwd: str,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> None:
        self.session_id = session_id
        self.proc = proc
        self.cwd = cwd
        self.model = model
        self.effort = effort
        self.active_prompt_task: Optional[asyncio.Task] = None

    async def cancel(self) -> None:
        """Cancel the active prompt turn and signal the agy subprocess."""
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
        if self.active_prompt_task and not self.active_prompt_task.done():
            self.active_prompt_task.cancel()

    async def close(self) -> None:
        """Terminate the agy subprocess."""
        if self.active_prompt_task and not self.active_prompt_task.done():
            self.active_prompt_task.cancel()
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


class AgyAcpServer:
    """ACP JSON-RPC 2.0 stdio server bridging to agy."""

    def __init__(self) -> None:
        self.sessions: Dict[str, AgySession] = {}
        self.agy_bin: Optional[str] = None
        self.default_model: Optional[str] = None
        self.default_effort: Optional[str] = None

    def _write_json(self, payload: Dict[str, Any]) -> None:
        """Write a JSON-RPC message to stdout and flush."""
        line = json.dumps(payload) + "\n"
        sys.stdout.write(line)
        sys.stdout.flush()

    def _write_response(self, req_id: Any, result: Any) -> None:
        self._write_json({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _write_error(self, req_id: Any, code: int, message: str) -> None:
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
                    "loadSession": True,
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
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
        )

        assert proc.stdout is not None
        # Read the 'init' event line from agy
        init_line = await proc.stdout.readline()
        if not init_line:
            raise RuntimeError(
                f"agy process exited with code {proc.returncode} without emitting init event"
            )

        init_data = json.loads(init_line.decode("utf-8"))
        actual_id = init_data.get("conversation_id")
        if not actual_id:
            raise RuntimeError(f"agy init event did not carry conversation_id: {init_data}")

        return proc, actual_id

    async def handle_session_new(self, req_id: Any, params: Dict[str, Any]) -> None:
        """Handle ACP 'session/new' request."""
        cwd = params.get("cwd", os.getcwd())
        model = params.get("model")
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
        if not session_id:
            self._write_error(req_id, -32602, "missing sessionId")
            return

        try:
            proc, loaded_id = await self._spawn_agy_process(
                cwd=cwd,
                conversation_id=session_id,
            )
            session = AgySession(
                session_id=loaded_id,
                proc=proc,
                cwd=cwd,
            )
            self.sessions[loaded_id] = session
            self._write_response(req_id, {"sessionId": loaded_id})
        except Exception as exc:
            self._write_error(req_id, -32000, str(exc))

    async def handle_session_prompt(self, req_id: Any, params: Dict[str, Any]) -> None:
        """Handle ACP 'session/prompt' request."""
        session_id = params.get("sessionId")
        session = self.sessions.get(session_id or "")
        if not session or not session.proc or session.proc.returncode is not None:
            self._write_error(req_id, -32001, f"session {session_id} not active")
            return

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
                line = await session.proc.stdout.readline()
                if not line:
                    exit_code = session.proc.returncode
                    if exit_code is None:
                        try:
                            exit_code = await asyncio.wait_for(session.proc.wait(), timeout=1.0)
                        except (asyncio.TimeoutError, Exception):
                            exit_code = session.proc.returncode
                    self._write_error(
                        req_id,
                        -32000,
                        f"agy process exited with code {exit_code}",
                    )
                    return
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
        except asyncio.CancelledError:
            self._write_response(req_id, {"stopReason": "cancelled"})
            raise
        except Exception as exc:
            self._write_error(req_id, -32000, f"prompt turn failed: {exc}")
        finally:
            if session.active_prompt_task == asyncio.current_task():
                session.active_prompt_task = None

    async def handle_session_cancel(self, req_id: Any, params: Dict[str, Any]) -> None:
        """Handle ACP 'session/cancel'."""
        session_id = params.get("sessionId")
        session = self.sessions.get(session_id or "")
        if session:
            await session.cancel()
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
        session_id = params.get("sessionId")
        session = self.sessions.get(session_id or "")
        if config_id == "model":
            self.default_model = str(value)
            if session and session.model != str(value):
                session.model = str(value)
                try:
                    await session.close()
                    proc, _ = await self._spawn_agy_process(
                        cwd=session.cwd,
                        conversation_id=session.session_id,
                        model=session.model,
                        effort=session.effort,
                    )
                    session.proc = proc
                except Exception as exc:
                    logger.warning("Failed to respawn agy process with model %s: %s", value, exc)
                    self._write_error(
                        req_id, -32000, f"failed to respawn agy process with model {value}: {exc}"
                    )
                    return
        elif config_id == "effort":
            self.default_effort = str(value)
            if session and session.effort != str(value):
                session.effort = str(value)
                try:
                    await session.close()
                    proc, _ = await self._spawn_agy_process(
                        cwd=session.cwd,
                        conversation_id=session.session_id,
                        model=session.model,
                        effort=session.effort,
                    )
                    session.proc = proc
                except Exception as exc:
                    logger.warning("Failed to respawn agy process with effort %s: %s", value, exc)
                    self._write_error(
                        req_id, -32000, f"failed to respawn agy process with effort {value}: {exc}"
                    )
                    return
        self._write_response(req_id, {})

    async def handle_set_model(self, req_id: Any, params: Dict[str, Any]) -> None:
        """Handle ACP 'session/set_model'."""
        model_id = params.get("modelId") or params.get("model")
        session_id = params.get("sessionId")
        session = self.sessions.get(session_id or "")
        if model_id:
            self.default_model = str(model_id)
            if session and session.model != str(model_id):
                session.model = str(model_id)
                try:
                    await session.close()
                    proc, _ = await self._spawn_agy_process(
                        cwd=session.cwd,
                        conversation_id=session.session_id,
                        model=session.model,
                        effort=session.effort,
                    )
                    session.proc = proc
                except Exception as exc:
                    logger.warning("Failed to respawn agy process with model %s: %s", model_id, exc)
                    self._write_error(
                        req_id,
                        -32000,
                        f"failed to respawn agy process with model {model_id}: {exc}",
                    )
                    return
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
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                message = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue

            if isinstance(message, dict):
                method = message.get("method")
                if method == "session/prompt":
                    task = asyncio.create_task(self.dispatch_request(message))
                    session_id = message.get("params", {}).get("sessionId")
                    session = self.sessions.get(session_id or "")
                    if session:
                        session.active_prompt_task = task
                else:
                    await self.dispatch_request(message)

        # Cleanup all sessions on exit
        for session in list(self.sessions.values()):
            await session.close()


def main() -> None:
    """CLI entry point for agy-acp adapter."""
    asyncio.run(AgyAcpServer().run())


if __name__ == "__main__":
    main()
