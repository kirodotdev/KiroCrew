"""What ``kirocrew chat``'s permission prompt shows, and the identity it audits.

``TestChatPermissionRequestConsentSurface`` complements ``TestChatPermissionRequest`` in
``test_cli_chat_permission.py`` and runs on the same harness from ``cli_helpers.py``: terminal
controls and hostile text never reach the terminal, a request the gate cannot verify is
refused instead of shown, the prompt discloses targets and MCP identity, the direct client's
cached parameters stay authoritative, and the canonical MCP identity reaches the shared gate.
"""

import asyncio
import sys
import types

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
    _TestChatPermissionRequestHarness,
)


class TestChatPermissionRequestConsentSurface(_TestChatPermissionRequestHarness):
    """What a permission request shows the person, and the identity it is audited under.

    Terminal controls and hostile text on the prompt, refusal of requests the gate cannot verify,
    what the prompt discloses, the direct client, child fidelity and the canonical MCP identity at
    the shared gate. It runs on the harness it shares with ``TestChatPermissionRequest``.
    """

    class _StrictTextStream:
        """A TTY-like stream that raises on anything its codec cannot encode."""

        errors = "strict"

        def __init__(self, encoding):
            self.encoding = encoding
            self._chunks = []

        def write(self, text):
            text.encode(self.encoding, errors=self.errors)
            self._chunks.append(text)
            return len(text)

        def flush(self):
            return None

        def isatty(self):
            return True

        def getvalue(self):
            return "".join(self._chunks)

    @staticmethod
    def _direct_tool_call(client, *, path="notes.md", tool_call_id="tc-direct"):
        """Populate the real direct-client provenance caches for an edit call."""
        from kiro_crew.acp.types import JsonRpcMessage

        event = client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": tool_call_id,
                        "title": "Edit notes",
                        "kind": "edit",
                        "rawInput": {"path": path, "content": "updated"},
                        "_meta": {"kiro": {"toolName": "fs_write"}},
                    }
                },
            )
        )
        assert event is not None

    @staticmethod
    def _direct_permission_message(*, tool_call_id="tc-direct", inline_path=None):
        from kiro_crew.acp.types import JsonRpcMessage

        tool_call: dict[str, object] = {
            "toolCallId": tool_call_id,
            "title": "Edit notes",
            "kind": "edit",
        }
        if inline_path is not None:
            tool_call["input"] = {"path": inline_path, "content": "inline"}
        return JsonRpcMessage(
            id="req-direct",
            method="session/request_permission",
            params={
                "toolCall": tool_call,
                "options": [
                    {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
                ],
            },
        )

    # ── Terminal controls on the consent surface ─────────────────────────

    @pytest.mark.asyncio
    async def test_osc52_title_cannot_reach_the_terminal(self, monkeypatch, capsys):
        # An OSC 52 sequence in a model-authored title writes the user's
        # clipboard if it reaches the terminal. Neither ESC nor BEL -- the
        # sequence's introducer and terminator -- may survive to the prompt.
        # The prompt's own terminal reset is fixed, trusted bytes rather than
        # anything a title can influence, so it is subtracted before asserting
        # that NOTHING else escaped: that keeps this a whole-output guarantee
        # instead of narrowing it to one line.
        import kiro_crew.cli_chat as cli_chat

        title = "Read file\x1b]52;c;aGVsbG8=\x07 please"
        await self._drive(monkeypatch, event=self._event(title=title))
        out = capsys.readouterr().out
        untrusted = out.replace(cli_chat._PROMPT_TERMINAL_RESET, "")
        assert "\x1b" not in untrusted and "\x07" not in untrusted
        assert "]52;c;aGVsbG8=" in out  # neutralised to inert text, not dropped
        assert "Read file" in out

    @pytest.mark.asyncio
    async def test_csi_sequence_cannot_reach_the_terminal(self, monkeypatch, capsys):
        # CSI can move the cursor and erase what is already drawn, so a title
        # could repaint the question the user is answering. The newline is part
        # of the same class: a multi-line title pushes the prompt off screen.
        title = "Delete\x1b[2K\x1b[1Anothing\nimportant"
        await self._drive(monkeypatch, event=self._event(title=title))
        out = capsys.readouterr().out
        line = next(ln for ln in out.splitlines() if ln.startswith("Permission required:"))
        assert "\x1b" not in line
        assert line == "Permission required: Delete [2K [1Anothing important"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("encoding", ["utf-8", "cp1252"])
    async def test_malicious_unicode_title_and_tool_input_are_answered(self, monkeypatch, encoding):
        """Strict UTF-8 rejects lone surrogates; strict cp1252 rejects wider Unicode.

        Drive the whole pending-request flow through a strict destination stream
        so either case raises before ``approve_tool`` when the render boundary is
        missing. ``tool_input`` is used because that is the permission-request
        wire shape ``shell_command`` decodes.
        """
        stream = self._StrictTextStream(encoding)
        monkeypatch.setattr(sys, "stdout", stream)
        bomb = chr(0x1F4A3)
        command = f"printf '{chr(0xDFFF)} {bomb}'"
        event = self._event(title=f"Run safely {chr(0xD800)} 刪除 {bomb}", command=command)
        provider, sels, reads = await self._drive(monkeypatch, event=event, answer="a")

        assert provider.calls == [("approve", 7, False)]
        assert reads["n"] == 1
        assert [record["outcome"] for record in sels] == ["allowed"]
        rendered = stream.getvalue()
        rendered.encode(encoding, errors="strict")
        assert "\ud800" not in rendered and "\udfff" not in rendered
        assert "Permission required:" in rendered and "Command:" in rendered
        if encoding == "cp1252":
            assert "\\u522a\\u9664" in rendered
            assert "\\U0001f4a3" in rendered
        else:
            assert "刪除" in rendered and bomb in rendered

    @pytest.mark.asyncio
    async def test_a_prompt_render_failure_is_explicitly_rejected(self, monkeypatch, capsys):
        """A render bug is not allowed to escape between request and response."""
        import kiro_crew.cli_chat as cli_chat

        async def broken_prompt(event):
            raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogate")

        monkeypatch.setattr(cli_chat, "_prompt_answer", broken_prompt)
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="Render \ud800", tool_name="fs_write"),
            answer="a",
        )

        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0
        assert [record["error"] for record in sels] == ["prompt_failed"]
        captured = capsys.readouterr()
        assert "done" in captured.out
        assert "could not be rendered or read" in captured.err

    @pytest.mark.asyncio
    async def test_control_characters_in_a_command_are_neutralised(self, monkeypatch, capsys):
        # The shell command is untrusted display text on the same surface, and
        # keeps its existing one-line collapse contract.
        event = self._event(title="Run it", command="echo \x1b]52;c;x\x07hi\nls")
        await self._drive(monkeypatch, event=event)
        out = capsys.readouterr().out
        line = next(ln for ln in out.splitlines() if ln.startswith("Command:"))
        assert "\x1b" not in line and "\x07" not in line
        assert line == "Command: echo ]52;c;x hi ls"

    @pytest.mark.asyncio
    async def test_an_unverifiable_execute_request_is_never_put_to_the_user(
        self, monkeypatch, capsys
    ):
        """``is_shell`` comes only from the trusted preceding-tool_call cache.

        On a cache miss it stays False, and ``shell_command`` returns None
        whenever it is False -- so a real command would be gated on nothing but
        its LLM-authored title. The request is refused instead of asked about,
        even though the user would have allowed it.
        """
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="List project files", tool_kind="execute"),
            answer="a",  # the user WOULD have allowed it
        )
        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0  # never prompted
        assert sels[0]["outcome"] == "denied"
        # Its own code: the gate did not reject this, we refused to ask.
        assert sels[0]["error"] == "unverified_shell"
        assert "could not be verified" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_an_unclassified_request_is_not_said_to_run_a_command(self, monkeypatch, capsys):
        """A request nothing classified claimed no command, so the notice must not
        say it did -- that sends the reader after the wrong defect."""
        provider, sels, _ = await self._drive(
            monkeypatch,
            event=self._event(title="Sub-agent: my-research", tool_kind="", shell_classified=False),
        )
        assert provider.calls == [("reject", 7, False)]
        assert sels[0]["error"] == "unverified_shell"
        err = capsys.readouterr().err
        assert "claims to run a command" not in err
        assert "could not be identified" in err

    @pytest.mark.asyncio
    async def test_a_cosmetic_kind_variant_still_refuses(self, monkeypatch):
        # Widening a fail-closed test is safe, so casing/padding must not be a
        # way to present an unverifiable command as an ordinary tool call.
        provider, sels, _ = await self._drive(
            monkeypatch,
            event=self._event(title="List files", tool_kind="  Execute "),
            answer="a",
        )
        assert provider.calls == [("reject", 7, False)]
        assert sels[0]["error"] == "unverified_shell"

    @pytest.mark.asyncio
    async def test_a_non_string_kind_still_gets_answered(self, monkeypatch):
        # ACP relays ``toolCall.kind`` verbatim, so a backend can send ``kind: 1``.
        # The gate reads the kind as text, so an unguarded value raises inside the
        # hook and the turn ends with the request UNANSWERED -- the hang this
        # whole path exists to end. The request must still be decided, and an
        # unusable kind must not qualify for the read-only allow-list.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="List files", tool_kind=1),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        assert reads["n"] == 1
        assert [s["outcome"] for s in sels] == ["allowed"]

    @pytest.mark.asyncio
    async def test_a_builtin_call_shows_its_trusted_tool_name(self, monkeypatch, capsys):
        # A builtin has no MCP server, so the prompt otherwise carries only the
        # model-authored title and a file write reaches the human as whatever
        # prose the model chose. ``tool_name`` is the trusted ``_meta.kiro``
        # identity, so consent covers what runs rather than its description.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="Tidy up the notes", tool_name="fs_write"),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        assert "fs_write" in capsys.readouterr().out

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["path", "file_path", "filePath"])
    async def test_a_file_write_discloses_its_target_path(self, monkeypatch, capsys, key):
        # The trusted identity says WHICH tool runs, not what it runs AGAINST.
        # ``fs_write`` under a benign title is consent to a verb, and the gate's
        # sensitive-path deny covers only the named-dangerous paths -- so what is
        # left undisclosed is exactly the ordinary valuable file no rule speaks
        # for. Every spelling the gate accepts must be read here too, or the
        # prompt and the gate disagree about which value is the target.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(
                title="Tidy up the notes",
                tool_name="fs_write",
                raw_params={key: "/home/tester/thesis.md"},
            ),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        assert "/home/tester/thesis.md" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_a_multi_target_call_discloses_the_count(self, monkeypatch, capsys):
        # Now that extraction is nesting-aware, a batch call yields every target,
        # and showing only the first would have the human consent to one file
        # while approving the whole batch. The first path plus a count keeps the
        # prompt honest without turning it into a manifest.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(
                title="Read the notes",
                tool_name="read",
                raw_params={
                    "operations": [
                        {"mode": "Line", "path": "/home/tester/thesis.md"},
                        {"mode": "Line", "path": "/home/tester/notes.md"},
                        {"mode": "Line", "path": "/home/tester/refs.md"},
                    ]
                },
            ),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        out = capsys.readouterr().out
        assert "/home/tester/thesis.md" in out
        assert "(+2 more)" in out

    @pytest.mark.asyncio
    async def test_a_pathless_call_is_still_asked_about(self, monkeypatch):
        # Absence of a path is NOT a refusal. Most builtin calls legitimately act
        # on no file, so denying whenever a path cannot be found would refuse
        # them all to close a gap that only exists for tools which name a file.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="Remember this", tool_name="memory_write"),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        assert reads["n"] == 1, "a call with no path must still reach the human"
        assert [s["outcome"] for s in sels] == ["allowed"]

    @pytest.mark.asyncio
    async def test_a_long_path_cannot_push_the_question_off_screen(self, monkeypatch, capsys):
        # The path is attacker-influenceable text on an authorization prompt, so
        # it is capped for the same reason the command line is.
        import kiro_crew.cli_chat as cli_chat

        flood = "/tmp/" + "a" * (cli_chat._MAX_COMMAND_DISPLAY * 3)
        await self._drive(
            monkeypatch,
            event=self._event(title="Tidy up", tool_name="fs_write", raw_params={"path": flood}),
            answer="a",
        )
        out = capsys.readouterr().out
        assert "[truncated]" in out
        assert flood not in out

    @pytest.mark.asyncio
    async def test_a_non_string_path_argument_does_not_break_the_prompt(self, monkeypatch):
        # ``raw_tool_params`` is relayed from the backend, so a value need not be
        # a string. Raising here would leave the permission request unanswered --
        # the hang this whole path exists to end.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(
                title="Tidy up",
                tool_name="fs_write",
                raw_params={"path": {"nested": 1}},
            ),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        assert [s["outcome"] for s in sels] == ["allowed"]

    @pytest.mark.asyncio
    async def test_the_audit_write_never_runs_on_the_event_loop(self, monkeypatch):
        """``sel()`` opens the audit log and replays it to recover the HMAC chain,
        so the first permission of a fresh chat pays a filesystem cost inside the
        call -- unbounded on slow or corrupt storage. This coroutine shares its
        loop with the ACP reader and stderr-drain tasks, so a blocking write here
        stops draining the backend and freezes the turn the audit is about."""
        import threading

        import kiro_crew.cli_chat as cli_chat

        threads: list = []
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", lambda prompt="": "a")
        monkeypatch.setattr(
            cli_chat,
            "sel",
            lambda: types.SimpleNamespace(
                log_tool_invocation=lambda **kw: threads.append(threading.current_thread())
            ),
        )

        class _P:
            def __init__(self):
                self.calls = []

            async def approve_tool(self, request_id, *, always: bool = False):
                self.calls.append(("approve", request_id))

            async def reject_tool(self, request_id):
                self.calls.append(("reject", request_id))

        provider = _P()
        await asyncio.wait_for(
            cli_chat._answer_permission(
                provider,
                self._event(title="Tidy up", tool_name="fs_write"),
                interactive=True,
                gate=self._gate(),
            ),
            timeout=self._TIMEOUT,
        )
        assert provider.calls == [("approve", 7)]
        assert threads, "nothing was audited"
        assert threads[0] is not threading.main_thread(), (
            "the SEL write ran on the event loop thread; a slow audit store would "
            "stall ACP draining and freeze the turn"
        )

    @pytest.mark.asyncio
    async def test_the_prompt_resets_terminal_modes_first(self, monkeypatch, capsys):
        # Streamed model output is printed raw, so a turn can leave the terminal
        # in conceal mode and the prompt would draw invisibly into it --
        # sanitising the prompt's own strings cannot undo inherited state. The
        # reset has to precede the question, or it does not protect it.
        await self._drive(
            monkeypatch,
            event=self._event(title="Tidy up the notes", tool_name="fs_write"),
            answer="a",
        )
        out = capsys.readouterr().out
        assert "\x1b[0m" in out and "\x1b[?25h" in out
        assert out.index("\x1b[0m") < out.index("Permission required")

    @pytest.mark.asyncio
    async def test_the_mcp_audit_record_names_the_server(self, monkeypatch):
        # An MCP tool name is unique only within its server, so the audit record
        # must carry both halves or two servers exposing the same name produce
        # indistinguishable records.
        _, sels, _ = await self._drive(
            monkeypatch,
            event=self._event(
                title="Tidy up", tool_name="write_file", mcp_server_name="filesystem"
            ),
            answer="d",
        )
        assert sels[0]["tool_name"] == "@filesystem/write_file"

    @pytest.mark.asyncio
    async def test_the_mcp_audit_identity_cannot_collide_on_underscores(self, monkeypatch):
        # `mcp__<server>__<tool>` re-splits on the LAST `__`, so server "a__b"
        # with tool "c" and server "a" with tool "b__c" would compose to the same
        # string. The `/`-joined reference keeps them distinct, because neither an
        # MCP server nor an MCP tool name contains a slash.
        _, first, _ = await self._drive(
            monkeypatch,
            event=self._event(title="One", tool_name="c", mcp_server_name="a__b"),
            answer="d",
        )
        _, second, _ = await self._drive(
            monkeypatch,
            event=self._event(title="Two", tool_name="b__c", mcp_server_name="a"),
            answer="d",
        )
        assert first[0]["tool_name"] == "@a__b/c"
        assert second[0]["tool_name"] == "@a/b__c"
        assert first[0]["tool_name"] != second[0]["tool_name"]

    @pytest.mark.asyncio
    async def test_a_non_mcp_audit_record_keeps_the_plain_tool_name(self, monkeypatch):
        # The negative control: without an MCP server there is nothing to
        # qualify, so the record must not gain an `@` prefix.
        _, sels, _ = await self._drive(
            monkeypatch,
            event=self._event(title="Read it", tool_name="fs_read"),
            answer="d",
        )
        assert sels[0]["tool_name"] == "fs_read"

    @pytest.mark.asyncio
    async def test_the_reset_closes_an_open_string_before_resetting_modes(
        self, monkeypatch, capsys
    ):
        # An OSC/DCS sequence the model left UNTERMINATED puts the terminal in
        # string-consuming mode: everything after it is swallowed as that
        # sequence's payload. A mode reset sent into that state is itself eaten,
        # and so is the prompt. So the abort must come FIRST -- once it discards
        # the string, the rest of the reset is interpreted again.
        await self._drive(
            monkeypatch,
            event=self._event(title="Tidy up the notes", tool_name="fs_write"),
            answer="a",
        )
        out = capsys.readouterr().out
        can = out.index("\x18")
        assert can < out.index("\x1b[0m"), "the abort must precede the mode reset"
        assert can < out.index("Permission required")

    @pytest.mark.asyncio
    async def test_the_reset_aborts_rather_than_terminates_a_pending_string(
        self, monkeypatch, capsys
    ):
        # A String Terminator would COMPLETE the pending string, handing an
        # unterminated OSC 52 from model output to the terminal as a finished
        # command -- which sets the user's clipboard from attacker-controlled
        # text. The reset must abort the sequence, so it must not contain ST.
        await self._drive(
            monkeypatch,
            event=self._event(title="Tidy up the notes", tool_name="fs_write"),
            answer="a",
        )
        out = capsys.readouterr().out
        assert "\x18" in out
        assert "\x1b\\" not in out, "ST completes the pending string instead of discarding it"

    @pytest.mark.asyncio
    async def test_the_reset_does_not_beep(self, monkeypatch, capsys):
        # BEL also ends an OSC string, but it beeps audibly when no string is
        # open -- on every single prompt. CAN is silent and, unlike BEL, discards
        # the pending payload rather than delivering it, so the reset must not
        # fall back to BEL.
        await self._drive(
            monkeypatch,
            event=self._event(title="Tidy up the notes", tool_name="fs_write"),
            answer="a",
        )
        assert "\x07" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_the_reset_is_withheld_when_stdout_is_not_a_terminal(self, monkeypatch):
        # The negative control, exercised directly: driving a full turn with
        # tty=False never renders the prompt at all, so asserting on its output
        # would pass no matter what this branch does. Escape bytes written to a
        # pipe or a redirected file are literal noise in someone's log.
        import kiro_crew.cli_chat as cli_chat

        monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
        assert cli_chat._terminal_reset() == ""
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        assert cli_chat._terminal_reset() == cli_chat._PROMPT_TERMINAL_RESET

        def boom():
            raise ValueError("detached")

        # A closed or detached stream must not let the check itself break the
        # prompt the user is waiting on.
        monkeypatch.setattr(sys.stdout, "isatty", boom, raising=False)
        assert cli_chat._terminal_reset() == ""

    @pytest.mark.asyncio
    async def test_a_verified_shell_call_is_still_asked_about(self, monkeypatch):
        # The negative control: the refusal must key on the MISSING trusted
        # signal, not on execute-kind itself, or every shell call would die
        # here and the test above would pass for the wrong reason.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="Run the tests", command="pytest -q"),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        assert reads["n"] == 1
        assert [s["outcome"] for s in sels] == ["allowed"]

    @pytest.mark.asyncio
    async def test_direct_client_non_shell_permission_can_be_allowed(self, monkeypatch, tmp_path):
        """The Claude/direct transport must distinguish cached False from a miss.

        This drives the production ``AcpClient`` parser before the CLI consumer;
        hand-building an event with ``shell_classified=True`` would not catch a
        transport that dropped the provenance flag while copying the cache.
        """
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        self._direct_tool_call(client, path=str(tmp_path / "notes.md"))
        event = client._build_permission_event(self._direct_permission_message())

        assert event.is_shell is False
        assert event.shell_classified is True
        assert event.raw_params_trusted is True
        provider, sels, reads = await self._drive(monkeypatch, event=event, answer="a")
        assert provider.calls == [("approve", "req-direct", False)]
        assert reads["n"] == 1
        assert [s["outcome"] for s in sels] == ["allowed"]

    @pytest.mark.asyncio
    async def test_direct_client_relative_edit_target_reaches_the_prompt(
        self, monkeypatch, tmp_path
    ):
        """An ordinary relative edit target is the routine shape on every
        backend. Its params spelling is judged verbatim as it always was and
        the request goes to the human, exactly like the absolute sibling above;
        only a DELETE's relative target (the call's sole evidence) is refused
        as unverifiable (``tool_paths.PARAMS_ONLY_TARGET_KINDS``)."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        self._direct_tool_call(client, path="notes.md")
        event = client._build_permission_event(self._direct_permission_message())

        assert event.raw_params_trusted is True
        provider, sels, reads = await self._drive(monkeypatch, event=event, answer="a")
        assert provider.calls == [("approve", "req-direct", False)]
        assert reads["n"] == 1
        assert [s["outcome"] for s in sels] == ["allowed"]

    @pytest.mark.asyncio
    async def test_direct_client_repeat_keeps_cached_params_authoritative(
        self, monkeypatch, tmp_path
    ):
        """A repeated request cannot replace trusted args with inline prose."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        self._direct_tool_call(client, path="~/.ssh/id_rsa")
        first = client._build_permission_event(self._direct_permission_message())
        repeat = client._build_permission_event(
            self._direct_permission_message(inline_path="notes.md")
        )

        assert first.raw_tool_params == repeat.raw_tool_params
        assert repeat.raw_tool_params is not None
        assert repeat.raw_tool_params["path"] == "~/.ssh/id_rsa"
        assert repeat.raw_params_trusted is True
        assert repeat.shell_classified is True
        provider, _, reads = await self._drive(monkeypatch, event=repeat, answer="a")
        assert provider.calls == [("reject", "req-direct", False)]
        assert reads["n"] == 0

    @pytest.mark.asyncio
    async def test_direct_client_cache_miss_stays_fail_closed(self, monkeypatch, tmp_path):
        """Inline permission data cannot manufacture trusted provenance."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        # Absolute so the hook gate has nothing to refuse on the target itself;
        # what this pins is that inline data earns no provenance, and the
        # refusal is the unverified-shell one, not a target denial.
        event = client._build_permission_event(
            self._direct_permission_message(
                tool_call_id="uncached", inline_path=str(tmp_path / "notes.md")
            )
        )

        assert event.raw_params_trusted is False
        assert event.shell_classified is False
        provider, sels, reads = await self._drive(monkeypatch, event=event, answer="a")
        assert provider.calls == [("reject", "req-direct", False)]
        assert reads["n"] == 0
        assert sels[0]["error"] == "unverified_shell"

    @pytest.mark.asyncio
    async def test_kindless_mcp_tool_reaches_the_prompt_instead_of_auto_denying(self, monkeypatch):
        """A backend that omits `kind` on an MCP tool_call must not cost the user
        the tool. The frame's `_meta.kiro` transport identity earns the
        _unverifiable_shell escape -- WITHOUT minting a shell classification, so
        the low-fidelity gates elsewhere keep treating the title as unverified --
        and the request reaches the normal approval prompt.

        Drives the shared-runtime parser rather than hand-building the event: the
        auto-deny came from the cache write being skipped, which an event built
        with trusted identity fields would hide.
        """
        from kiro_crew.acp._dispatch import _build_tool_call_event, build_permission_event
        from kiro_crew.acp.types import JsonRpcMessage

        shell_cache: dict[str, bool] = {}
        raw_cache: dict[str, dict] = {}
        server_cache: dict[str, str] = {}
        name_cache: dict[str, str] = {}
        _build_tool_call_event(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc-kindless",
                "title": "Asking the knowledge service",
                "rawInput": {"question": "why"},
                "_meta": {"kiro": {"mcpServerName": "kb", "toolName": "ask"}},
            },
            None,
            shell_cache=shell_cache,
            raw_params_cache=raw_cache,
            mcp_server_name_cache=server_cache,
            tool_name_cache=name_cache,
        )
        event, _ = build_permission_event(
            JsonRpcMessage(
                id="req-kindless",
                method="session/request_permission",
                params={
                    "toolCall": {
                        "toolCallId": "tc-kindless",
                        "title": "Asking the knowledge service",
                    },
                    "options": [
                        {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
                    ],
                },
            ),
            shell_cache=shell_cache,
            raw_params_cache=raw_cache,
            mcp_server_name_cache=server_cache,
            tool_name_cache=name_cache,
        )

        assert event.shell_classified is False  # no classification was minted
        assert event.is_shell is False
        assert event.mcp_identity_trusted is True  # the escape's actual carrier
        provider, sels, reads = await self._drive(monkeypatch, event=event, answer="a")
        assert provider.calls == [("approve", "req-kindless", False)]
        assert reads["n"] == 1
        assert [s["outcome"] for s in sels] == ["allowed"]

    @pytest.mark.asyncio
    async def test_low_fidelity_child_without_identity_is_rejected_not_prompted(
        self, monkeypatch, capsys
    ):
        """The admission boundary of the child-fidelity opt-in: a child event
        whose params never reached the cache AND whose MCP identity is
        unverified must be rejected before any prompt. The prompt for such an
        event would carry only the agent-authored title -- for an edit, no Path
        line -- so approval would consent to an undisclosed write."""
        from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

        event = AcpEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=9,
            title="Tidying up the notes file",
            sub_session_id="child-a",
            tool_kind="edit",
            is_shell=False,
            shell_classified=False,
            raw_params_trusted=False,
            options=[{"id": "allow", "label": "Allow Once"}],
        )
        assert event.child_low_fidelity is True
        assert event.child_mcp_identity_trusted is False
        provider, sels, reads = await self._drive(monkeypatch, event=event, answer="a")
        assert provider.calls == [("reject", 9, False)]
        assert reads["n"] == 0  # the human was never asked
        assert sels[0]["error"] == "child_unverified_context"
        assert "without verifiable security context" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_identity_trusted_low_fidelity_child_still_reaches_the_prompt(
        self, monkeypatch, capsys
    ):
        """The one admission through that boundary: a child MCP call whose
        `_meta.kiro` identity survived the cache is presented -- the prompt
        shows the non-forgeable server/tool pair, the same args-blind consent
        contract the dashboard's interactive card provides."""
        from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

        event = AcpEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=10,
            title="Asking the knowledge service",
            sub_session_id="child-a",
            tool_kind="fetch",
            is_shell=False,
            shell_classified=False,
            raw_params_trusted=False,
            mcp_server_name="kb",
            tool_name="ask",
            mcp_identity_trusted=True,
            options=[{"id": "allow", "label": "Allow Once"}],
        )
        assert event.child_low_fidelity is True
        assert event.child_mcp_identity_trusted is True
        provider, sels, reads = await self._drive(monkeypatch, event=event, answer="a")
        assert provider.calls == [("approve", 10, False)]
        assert reads["n"] == 1
        assert "MCP tool: kb / ask" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_ordinary_title_and_command_display_unchanged(self, monkeypatch, capsys):
        # The control-stripping must not disturb ordinary text: this is the
        # control for the three cases above.
        event = self._event(title="Run the test suite", command="pytest -q test/test_cli.py")
        await self._drive(monkeypatch, event=event)
        out = capsys.readouterr().out
        assert "Permission required: Run the test suite" in out
        assert "Command: pytest -q test/test_cli.py" in out
        # A shell call has no MCP identity to disclose, so the line is absent
        # rather than empty -- the negative control for the MCP cases below.
        assert "MCP tool:" not in out

    @pytest.mark.asyncio
    async def test_mcp_identity_is_disclosed_not_just_the_title(self, monkeypatch, capsys):
        # An MCP call shows no command, so before this the human saw ONLY the
        # model-authored title: a benign description could win consent for an
        # undisclosed tool. The trusted _meta identity must reach the prompt.
        event = self._event(
            title="Tidy up the workspace",
            tool_name="delete_everything",
            mcp_server_name="filesystem",
        )
        await self._drive(monkeypatch, event=event)
        out = capsys.readouterr().out
        assert "MCP tool: filesystem / delete_everything" in out

    @pytest.mark.asyncio
    async def test_mcp_identity_is_neutralised_and_survives_a_nameless_tool(
        self, monkeypatch, capsys
    ):
        # The identity is server-supplied text on the consent surface, so it
        # gets the same one-line control-stripping as the title and command. A
        # missing tool name still discloses the server rather than printing a
        # bare separator that reads as though nothing was withheld.
        event = self._event(
            title="Tidy up",
            tool_name="",
            mcp_server_name="fs\x1b[2Kspoof\nserver",
        )
        await self._drive(monkeypatch, event=event)
        line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("MCP tool:"))
        assert "\x1b" not in line
        assert line == "MCP tool: fs [2Kspoof server / unnamed tool"

    # ── Canonical MCP identity reaches the shared gate ───────────────────

    @pytest.mark.asyncio
    async def test_canonical_mcp_identity_is_forwarded_and_denies(self, monkeypatch):
        # The wiring guard: a permission_request carrying the trusted _meta.kiro
        # server + tool names must reach HookManager as those fields, so a
        # governance rule naming the canonical mcp__server__tool denies the call
        # -- the backend is rejected and the human is never asked.
        import kiro_crew.cli_chat as cli_chat
        import kiro_crew.hooks as hooks_mod
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

        seen: list[str] = []

        def fake_gov(ctx, name, *a, **k):
            # One question carries every identity for the call (title, trusted
            # tool name, canonical MCP reference), so match against all of them.
            targets = [name, *k.get("extra_titles", ()), k.get("mcp_ref", "")]
            for target in targets:
                if target and target not in seen:
                    seen.append(target)
            if "@weather:srv/wipe_disk" in targets:
                return "Blocked by governance policy: denied"
            return None

        monkeypatch.setattr(hooks_mod, "_governance_denial", fake_gov)
        event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=7,
            title="Check the forecast",  # benign model-authored prose
            tool_kind="other",
            mcp_server_name="weather:srv",
            tool_name="wipe_disk",
            options=[{"id": "allow", "label": "Allow Once"}],
        )
        trace: list = []
        provider = self._GatedProvider(event, trace)
        reads = self._patch_env(monkeypatch, tty=True, trace=trace, answer="a")
        await asyncio.wait_for(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate()),
            timeout=self._TIMEOUT,
        )

        assert "@weather:srv/wipe_disk" in seen  # forwarded, not just the title
        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0  # the human was never asked
        assert [s[1]["error"] for s in trace if s[0] == "sel"] == ["hook_deny"]

    @pytest.mark.asyncio
    async def test_gate_verdict_is_obtained_off_the_event_loop(self, monkeypatch):
        # The gate resolves the governance profile, which stats and reads
        # ``profiles/``. This coroutine shares its loop with the ACP reader and
        # drain tasks, so that walk must not run on the loop: on slow or network
        # storage a synchronous call stalls the whole session. Pinned by asserting
        # the call lands on a DIFFERENT thread than the loop's.
        import threading

        import kiro_crew.cli_chat as cli_chat
        import kiro_crew.hooks as hooks_mod
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

        loop_thread = threading.get_ident()
        call_threads: list[int] = []
        real = hooks_mod.HookManager.on_tool_call

        def recording(self, *a, **k):
            call_threads.append(threading.get_ident())
            return real(self, *a, **k)

        monkeypatch.setattr(hooks_mod.HookManager, "on_tool_call", recording)
        event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=11,
            title="Read a file",
            tool_kind="read",
            options=[{"id": "allow", "label": "Allow Once"}],
        )
        trace: list = []
        provider = self._GatedProvider(event, trace)
        self._patch_env(monkeypatch, tty=True, trace=trace, answer="a")
        await asyncio.wait_for(
            cli_chat._send_and_print(provider, "go", interactive=True, gate=self._gate()),
            timeout=self._TIMEOUT,
        )

        assert call_threads, "the gate was never consulted"
        assert loop_thread not in call_threads, (
            "on_tool_call ran on the event-loop thread; its profile walk must be "
            "offloaded via asyncio.to_thread"
        )

    def test_an_unclassified_request_is_refused_whatever_kind_it_claims(self):
        # THE VECTOR. A real shell call whose preceding tool_call carried no
        # resolvable kind leaves the cache empty, so is_shell is the MISS default
        # rather than a resolved "not a shell tool". Labelling it ``read`` must not
        # buy it a trip to the human prompt, where the only thing on display would
        # be a title with no command behind it.
        import kiro_crew.cli_chat as cli_chat

        event = self._event(title="Read a file", tool_kind="read", shell_classified=False)
        assert cli_chat._unverifiable_shell(event) is True

    def test_a_resolved_non_shell_request_is_not_refused(self):
        # The negative control that keeps the deny narrow: a RESOLVED non-shell
        # classification is a real answer, and must still reach the prompt.
        import kiro_crew.cli_chat as cli_chat

        event = self._event(title="Read a file", tool_kind="read")
        assert cli_chat._unverifiable_shell(event) is False

    def test_audit_scrubs_a_credential_out_of_the_tool_identity(self, monkeypatch):
        # The audited identity comes from the backend, and SEL does not redact for
        # its callers, so a credential riding in a tool name would be persisted and
        # served over /api/sel/events.
        import types as _types

        import kiro_crew.cli_chat as cli_chat

        secret = "AKIAIOSFODNN7EXAMPLE"
        trace: list = []
        gate = self._gate()
        monkeypatch.setattr(
            cli_chat,
            "sel",
            lambda: _types.SimpleNamespace(
                log_tool_invocation=lambda **kw: trace.append(("sel", kw))
            ),
        )
        event = self._event(title="Terminal", tool_name=f"fetch_{secret}")
        cli_chat._audit(gate, event, "approved")

        logged = [s[1]["tool_name"] for s in trace if s[0] == "sel"]
        assert logged, "nothing was audited"
        assert secret not in logged[0], f"credential survived into the audit: {logged[0]!r}"
