"""``kirocrew chat`` answering a permission request, and two neighbours of that turn.

``TestChatPermissionRequest`` holds the security gate, the audit record, the human's
decision, the shell command the prompt shows and the stdin reader's lifecycle; it runs on
``_TestChatPermissionRequestHarness`` from ``cli_helpers.py``, which it shares with
``TestChatPermissionRequestConsentSurface`` in ``test_cli_chat_consent_surface.py``.
``TestDenialNoticeEncoding`` runs the automatic-denial notice in a child interpreter whose
stderr is not UTF-8, and ``TestChatProviderShutdown`` pins that every exit from a chat turn
shuts the provider down.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import types

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
    _TestChatPermissionRequestHarness,
)


class TestChatPermissionRequest(_TestChatPermissionRequestHarness):
    """`kirocrew chat` must ANSWER a permission request, and answering one is an
    authorization decision.

    Every case drives the real ``_send_and_print`` against a provider whose
    stream cannot finish until the request is answered, and against a real
    ``HookManager`` -- not a stub returning the verdict the test wants.
    """

    # ── The security gate ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_a_benign_title_cannot_hide_a_sensitive_command(self, monkeypatch, capsys):
        """The gate judges what executes, not what the model called it.

        ``title`` for a shell tool is an LLM-authored description, so an IMDS
        credential fetch labelled "List project files" is the bypass that keying
        on the title alone would let through. The user is never even asked.
        """
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(
                title="List project files",
                command="curl http://169.254.169.254/latest/meta-data/",
            ),
            answer="a",  # the user WOULD have allowed it
        )
        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0
        # A stable code, not the gate's reason: the reason names the very path
        # being protected, and an audit record must not restate it.
        assert sels[0]["error"] == "hook_deny"
        assert "169.254.169.254" not in json.dumps(sels[0])
        # The reason still reaches the terminal, and it has to be the REAL one:
        # `is_shell` with no command also denies, via the gate's deny-by-default
        # backstop, so "it was denied" would pass just as well when the command
        # is never forwarded at all.
        err = capsys.readouterr().err
        assert "IMDS endpoint" in err
        assert "could not be verified" not in err

    @pytest.mark.asyncio
    async def test_a_denied_command_is_not_the_users_to_override(self, monkeypatch):
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="Tidy the workspace", command="rm -rf /"),
            answer="a",
        )
        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0
        assert [s["outcome"] for s in sels] == ["denied"]

    # ── The audit record ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event_kw,answer,order,code",
        [
            (dict(title="x", command="rm -rf /"), "a", ["sel", "reject"], "hook_deny"),
            ({}, "a", ["sel", "approve", "sel"], ""),
            ({}, "d", ["sel", "reject"], "user_denied"),
        ],
    )
    async def test_the_audit_precedes_the_transport(
        self, monkeypatch, event_kw, answer, order, code
    ):
        """A transport failure must not erase the decision, and the code stays a
        stable token so the log is queryable and quotes nothing.

        Asserting the interleaved trace, rather than two separate lists, is what
        makes this an ordering test.
        """
        provider, sels, _ = await self._drive(
            monkeypatch, event=self._event(**event_kw), answer=answer
        )
        assert [t[0] for t in provider.trace] == order
        assert sels[0]["error"] == code

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event_kw,expected,forbidden",
        [
            # The canonical `_meta.kiro` identity wins over LLM prose.
            (
                dict(title="do something friendly", tool_name="mcp__files__read"),
                "mcp__files__read",
                "friendly",
            ),
            # No canonical name: the title is all there is, so scrub it -- SEL
            # does not redact for its callers.
            (dict(title="deploy with AKIAIOSFODNN7EXAMPLE"), None, "AKIAIOSFODNN7EXAMPLE"),
        ],
    )
    async def test_the_audited_identity_is_not_model_authored(
        self, monkeypatch, event_kw, expected, forbidden
    ):
        _, sels, _ = await self._drive(monkeypatch, event=self._event(**event_kw), answer="a")
        if expected is not None:
            assert sels[0]["tool_name"] == expected
        assert forbidden not in sels[0]["tool_name"]

    # ── The human decision ───────────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer,call,outcome",
        [("a", ("approve", 7, False), "allowed"), ("d", ("reject", 7, False), "denied")],
    )
    async def test_the_turn_completes_whatever_the_human_answers(
        self, monkeypatch, capsys, answer, call, outcome
    ):
        """Denial answers the gate; it must not abandon the turn."""
        provider, sels, reads = await self._drive(monkeypatch, answer=answer)
        assert provider.calls == [call]
        assert reads["n"] == 1
        assert [s["outcome"] for s in sels] == [outcome]
        assert "done" in capsys.readouterr().out  # text after the gate reached stdout

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer,allows",
        # A prefix match reads `abort` as an allow -- the opposite of intent.
        # The other rejects are what a person types when a single-key prompt did
        # not register, plus the do-nothing answers.
        [(a, True) for a in ("a", "A", " a ", "a\n")]
        + [(a, False) for a in ("abort", "allow", "always", "wait", "ad", "x", "", EOFError)],
    )
    async def test_only_the_exact_allow_token_approves(self, monkeypatch, answer, allows):
        provider, sels, _ = await self._drive(monkeypatch, answer=answer)
        assert provider.calls == [("approve", 7, False) if allows else ("reject", 7, False)]
        assert [s["outcome"] for s in sels] == ["allowed" if allows else "denied"]

    @pytest.mark.asyncio
    async def test_no_answer_grants_a_persistent_approval(self, monkeypatch):
        """There is no "always allow": a backend that records one stops sending
        permission requests for matching calls, and a request never sent is a
        call this ladder never runs and never audits."""
        for answer in ("a", "w", "always"):
            provider, _, reads = await self._drive(monkeypatch, answer=answer)
            assert all(not always for kind, _, always in provider.calls if kind == "approve")
        # The prompt must not advertise an option the responder cannot honour.
        assert "always" not in reads["prompts"][0].lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "interactive,tty",
        [
            # `-m` is documented as non-interactive, so a terminal does not
            # license a prompt: a script under a pty would block on a question
            # nobody is watching for.
            (False, True),
            # And a prompt nobody can see is a hang, not consent.
            (True, False),
        ],
    )
    async def test_a_prompt_nobody_can_answer_denies_without_reading_stdin(
        self, monkeypatch, capsys, interactive, tty
    ):
        provider, sels, reads = await self._drive(
            monkeypatch, interactive=interactive, tty=tty, answer="a"
        )
        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0
        assert sels[0]["error"] == "noninteractive"
        captured = capsys.readouterr()
        assert "done" in captured.out  # the turn still finished
        assert "Denied automatically" in captured.err
        # The notice's own wording stays ASCII, which is a legibility choice: a
        # redirected stream encodes with the locale codec, and an escaped
        # character reads as noise mid-sentence. This case supplies an ASCII
        # title, so the whole captured notice is ASCII.
        #
        # It is NOT a safety property, and the title is not covered by it — see
        # TestDenialNoticeEncoding, which drives a real child process to pin what
        # actually protects an arbitrary-Unicode title.
        captured.err.encode("ascii")

    # ── What the human is shown ──────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command,expected,absent",
        [
            ("git status --short", "Command: git status --short", None),
            # Collapsed to one line: a heredoc must not reflow the question.
            ("printf 'a\\n\\tb'\nwc -l", "Command: printf 'a\\n\\tb' wc -l", None),
            # Capped, with the cut made explicit.
            ("echo " + "x" * 400, "... [truncated]", "x" * 400),
            # Redacted on the way to the screen.
            ("deploy --key AKIAIOSFODNN7EXAMPLE", None, "AKIAIOSFODNN7EXAMPLE"),
            # Nothing to show for a non-shell call.
            (None, None, "Command:"),
        ],
    )
    async def test_a_shell_prompt_shows_what_will_actually_run(
        self, monkeypatch, capsys, command, expected, absent
    ):
        """Approving on an LLM-authored title alone is consent to a description.

        The gate already keys on ``shell_command``; the human deciding needs the
        same ground truth.
        """
        await self._drive(
            monkeypatch,
            event=self._event(title="Run a helpful script", command=command),
            answer="a",
        )
        out = capsys.readouterr().out
        # Non-vacuous control: for a shell call the line must have been printed
        # at all, or an "absent" assertion passes for a request that never
        # reached the prompt.
        assert ("Command:" in out) is (command is not None)
        if expected:
            assert expected in out
        if absent:
            assert absent not in out

    # ── stdin lifecycle ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_the_event_loop_keeps_running_while_the_prompt_waits(self, monkeypatch):
        """The turn is parked INSIDE an active stream, not at an idle REPL.

        The blocking read does not return until a loop-side ticker has advanced,
        so a synchronous implementation deadlocks and this times out rather than
        passing quietly.
        """
        import kiro_crew.cli_chat as cli_chat

        ticks, released = {"n": 0}, threading.Event()

        async def _ticker():
            while True:
                # The prompt thread waits on the event this ticker sets.
                await asyncio.sleep(
                    0.01
                )  # flake-ok: the loop-side ticker's interval is the measurement
                ticks["n"] += 1
                if ticks["n"] >= 3:
                    released.set()

        def blocking_read(prompt=""):
            # Waits for the LOOP to progress: only reachable off the loop thread.
            assert released.wait(timeout=self._TIMEOUT), "event loop was blocked"
            return "a"

        self._patch_env(monkeypatch)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", blocking_read)
        provider = self._GatedProvider(self._event(), [])
        ticker = asyncio.create_task(_ticker())
        try:
            await asyncio.wait_for(
                cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate()),
                timeout=self._TIMEOUT,
            )
        finally:
            ticker.cancel()
        assert provider.calls == [("approve", 7, False)]

    @pytest.mark.asyncio
    async def test_a_poisoned_session_never_starts_a_second_reader(self, monkeypatch):
        """No later entry point may race the abandoned reader for keystrokes --
        not a second permission prompt, and not the REPL."""
        import kiro_crew.cli_chat as cli_chat

        self._patch_env(monkeypatch)
        monkeypatch.setattr(cli_chat, "_stdin_poisoned", True)

        def never(prompt=""):
            raise AssertionError("a poisoned session read stdin")

        monkeypatch.setattr(cli_chat, "_read_line_blocking", never)
        monkeypatch.setattr("builtins.input", never)

        provider = self._GatedProvider(self._event(), [])
        with pytest.raises(cli_chat.StdinPoisonedError):
            await cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate())
        with pytest.raises(cli_chat.StdinPoisonedError):
            await cli_chat._interactive(provider, types.SimpleNamespace())

    @pytest.mark.asyncio
    async def test_teardown_never_awaits_the_backend(self, monkeypatch):
        """Cancelling frees the coroutine, not the reader thread, and a wedged
        transport must not swallow the cancellation being delivered.

        The abandoned reader stays parked and takes the next line the user types
        -- measured -- so stdin has to be marked unusable. And ``CancelledError``
        has already been raised once with nothing to re-deliver it, so awaiting a
        ``reject_tool`` that never returns would leave the Ctrl-C that asked for
        this teardown unable to land.
        """
        import kiro_crew.cli_chat as cli_chat

        entered = threading.Event()

        class _HungReject(self._GatedProvider):
            async def reject_tool(self, request_id):
                self.trace.append(("reject", request_id, False))
                # flake-ok: pre-existing never-set event in the hung-reject stub, moved verbatim; teardown never calls it and the wait_for on the cancelled task bounds a regression
                await asyncio.Event().wait()  # never returns

        def blocking_read(prompt=""):
            entered.set()
            # flake-ok: pre-existing bounded park of the prompt thread, moved verbatim; the test cancels the turn while it is parked
            threading.Event().wait(timeout=self._TIMEOUT)
            return "a"

        trace: list = []
        self._patch_env(monkeypatch, trace=trace)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", blocking_read)
        provider = _HungReject(self._event(), trace)
        task = asyncio.create_task(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate())
        )
        # flake-ok: pre-existing 5 s bounded wait for the prompt thread to start, moved verbatim
        await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5.0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=self._TIMEOUT)
        assert cli_chat._stdin_poisoned is True
        assert provider.calls == []  # nothing was awaited on the way out
        assert [s[1]["error"] for s in trace if s[0] == "sel"] == ["session_aborted"]

    @pytest.mark.asyncio
    async def test_the_teardown_audit_also_runs_off_the_event_loop(self, monkeypatch):
        """The exit path is the one where blocking the loop hurts most: the loop
        still owns the ACP reader and stderr-drain tasks, and cold ``sel()``
        initialization replays the audit log to recover the HMAC chain. Auditing
        synchronously here would relocate the freeze this path exists to end.

        Shielded, so the record still lands if another cancellation arrives
        mid-write -- ``session_aborted`` is the outcome an audit reader can least
        afford to lose, because it is what distinguishes a died-unanswered turn
        from a user who said no.
        """
        import kiro_crew.cli_chat as cli_chat

        entered = threading.Event()
        threads: list = []
        trace: list = []

        def blocking_read(prompt=""):
            entered.set()
            # flake-ok: pre-existing bounded park of the prompt thread, moved verbatim; the test cancels the turn while it is parked
            threading.Event().wait(timeout=self._TIMEOUT)
            return "a"

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", blocking_read)

        def _record(**kw):
            threads.append(threading.current_thread())
            trace.append(("sel", kw))

        monkeypatch.setattr(
            cli_chat, "sel", lambda: types.SimpleNamespace(log_tool_invocation=_record)
        )

        provider = self._GatedProvider(self._event(), trace)
        task = asyncio.create_task(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate())
        )
        # flake-ok: pre-existing 5 s bounded wait for the prompt thread to start, moved verbatim
        await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5.0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=self._TIMEOUT)

        assert [s[1]["error"] for s in trace if s[0] == "sel"] == ["session_aborted"]
        assert threads[0] is not threading.main_thread(), (
            "the teardown SEL write ran on the event loop thread; a slow audit "
            "store would stall ACP draining while the turn is being torn down"
        )

    @pytest.mark.asyncio
    async def test_an_approval_that_cannot_be_audited_is_refused(self, monkeypatch):
        """AUDIT-OR-DENY on the allow path. An unwritable SEL log otherwise means
        the tool RUNS while the only record that a human authorized it is dropped
        by the background writer -- and on this surface the consent was a
        keystroke, so nothing else can reconstruct it afterwards.

        Refused rather than raised: letting the failure escape would abandon the
        request unanswered, and the backend holds the turn open until it is
        answered, which is the hang this path exists to end.
        """
        import kiro_crew.cli_chat as cli_chat

        trace: list = []

        def _explode(**kw):
            trace.append(("sel", kw))
            if kw.get("critical"):
                raise OSError("SEL log is read-only")

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", lambda prompt="": "a")
        monkeypatch.setattr(
            cli_chat, "sel", lambda: types.SimpleNamespace(log_tool_invocation=_explode)
        )

        provider = self._GatedProvider(self._event(), trace)
        await asyncio.wait_for(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate()),
            timeout=self._TIMEOUT,
        )

        assert provider.calls == [("reject", 7, False)], "the tool must not run unaudited"
        outcomes = [(s[1]["outcome"], s[1].get("error", "")) for s in trace if s[0] == "sel"]
        assert (
            "approval_pending",
            "",
        ) in outcomes, "the critical pending record must have been attempted"
        assert ("denied", "audit_unwritable") in outcomes, (
            "the downgrade must be recorded under its OWN code -- the operator said "
            "yes, so an audit reader must not be told they refused"
        )

    @pytest.mark.asyncio
    async def test_the_allow_audit_is_critical_and_the_deny_audit_is_not(self, monkeypatch):
        """The asymmetry is deliberate. Fail-closed only has meaning where the
        alternative is EXECUTION: a deny is already refusing, so a lost record
        cannot authorize anything, and making it audit-or-deny would turn a
        refusal into an error for no security gain."""
        import kiro_crew.cli_chat as cli_chat

        seen: dict = {}

        async def _drive(answer, key):
            trace: list = []
            self._patch_env(monkeypatch, trace=trace, answer=answer)
            provider = self._GatedProvider(self._event(), trace)
            await asyncio.wait_for(
                cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate()),
                timeout=self._TIMEOUT,
            )
            seen[key] = [s[1].get("critical", False) for s in trace if s[0] == "sel"]

        await _drive("a", "allow")
        await _drive("d", "deny")

        assert seen["allow"] == [
            True,
            False,
        ], "the pending record must be audit-or-deny and the result must follow the wire"
        assert seen["deny"] == [False], "a refusal must not be gated on its own audit"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("approval_sent", [True, False])
    async def test_approval_audit_records_pending_then_transport_result(
        self, monkeypatch, approval_sent
    ):
        import kiro_crew.cli_chat as cli_chat

        trace: list = []

        class _Provider(self._GatedProvider):
            async def approve_tool(self, request_id, *, always: bool = False):
                self.trace.append(("approve", request_id, always))
                self.answered.set()
                return approval_sent

        self._patch_env(monkeypatch, trace=trace, answer="a")
        provider = _Provider(self._event(), trace)
        await asyncio.wait_for(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate()),
            timeout=self._TIMEOUT,
        )

        outcomes = [entry[1]["outcome"] for entry in trace if entry[0] == "sel"]
        expected = "allowed" if approval_sent else cli_chat.OUTCOME_REJECTED_TRANSPORT_FLOOR
        assert outcomes == [cli_chat.OUTCOME_PENDING_APPROVAL, expected]
        assert provider.calls == [("approve", 7, False)]
        if not approval_sent:
            assert "allowed" not in outcomes

    @pytest.mark.asyncio
    async def test_the_prompt_and_the_gate_share_one_set_of_path_spellings(self, monkeypatch):
        """A ``filePath`` target must not be DISPLAYED as though vetted while the
        keystone never reads that key. Both sides resolve through
        ``hooks.target_paths``, so the parity is structural rather than asserted in
        a comment -- and a sensitive value under any spelling is refused by the
        gate before a human is ever asked.
        """
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(
                title="Tidy up the notes",
                tool_name="fs_write",
                tool_kind="edit",
                raw_params={"filePath": "~/.ssh/id_rsa"},
            ),
            answer="a",
        )
        assert provider.calls == [("reject", 7, False)], "the keystone must refuse it"
        assert reads["n"] == 0, "a policy denial is not the user's to override"
        assert [s["error"] for s in sels] == ["hook_deny"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer,tty,interactive,expected_error",
        [
            ("d", True, True, "user_denied"),
            ("a", False, True, "noninteractive"),
        ],
    )
    async def test_a_refusal_survives_its_own_audit_failing(
        self, monkeypatch, answer, tty, interactive, expected_error
    ):
        """The audit is bookkeeping; ``reject_tool`` is what ends the turn. If a
        raising audit skipped the rejection, the backend would hold the turn open
        on an unanswered request -- the hang this path exists to end, reached
        through the bookkeeping instead of the decision.

        Parametrised across two different refusal reasons so this pins the CLASS,
        not the single call site: every refusal path shares one helper.
        """
        import kiro_crew.cli_chat as cli_chat

        trace: list = []

        def _explode(**kw):
            trace.append(("sel", kw))
            raise ValueError("invalid SEL key")

        monkeypatch.setattr(sys.stdin, "isatty", lambda: tty, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: tty, raising=False)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", lambda prompt="": answer)
        monkeypatch.setattr(
            cli_chat, "sel", lambda: types.SimpleNamespace(log_tool_invocation=_explode)
        )

        provider = self._GatedProvider(self._event(), trace)
        await asyncio.wait_for(
            cli_chat._send_and_print(
                provider, "run it", interactive=interactive, gate=self._gate()
            ),
            timeout=self._TIMEOUT,
        )

        assert provider.calls == [("reject", 7, False)], (
            "the audit failure swallowed the rejection; the backend would wait "
            "forever on an unanswered permission request"
        )
        assert [s[1].get("error") for s in trace if s[0] == "sel"] == [expected_error]

    @pytest.mark.asyncio
    async def test_a_policy_denial_survives_its_own_audit_failing(self, monkeypatch):
        """The gate-deny path is the one a prompt-injected agent is most likely to
        reach, so it must not be the one where a broken audit sink turns a refusal
        into an abandoned turn."""
        import kiro_crew.cli_chat as cli_chat

        trace: list = []

        def _explode(**kw):
            trace.append(("sel", kw))
            raise ValueError("invalid SEL key")

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", lambda prompt="": "a")
        monkeypatch.setattr(
            cli_chat, "sel", lambda: types.SimpleNamespace(log_tool_invocation=_explode)
        )

        event = self._event(
            title="Tidy up",
            tool_name="fs_write",
            tool_kind="edit",
            raw_params={"path": "~/.ssh/id_rsa"},
        )
        provider = self._GatedProvider(event, trace)
        await asyncio.wait_for(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate()),
            timeout=self._TIMEOUT,
        )

        assert provider.calls == [("reject", 7, False)]
        assert [s[1].get("error") for s in trace if s[0] == "sel"] == ["hook_deny"]

    @pytest.mark.asyncio
    async def test_a_gate_exception_is_explicitly_rejected(self, monkeypatch, capsys):
        """A broken authorization gate cannot become an unanswered request."""
        import kiro_crew.cli_chat as cli_chat

        trace: list = []
        event = self._event(title="Check it", tool_name="fs_read")
        provider = self._GatedProvider(event, trace)
        reads = self._patch_env(monkeypatch, tty=True, trace=trace, answer="a")
        gate = self._gate()

        def broken_gate(*args, **kwargs):
            raise ValueError("malformed tool input")

        monkeypatch.setattr(gate.hooks, "on_tool_call", broken_gate)
        await asyncio.wait_for(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=gate),
            timeout=self._TIMEOUT,
        )

        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0
        assert [s[1].get("error") for s in trace if s[0] == "sel"] == ["gate_failed"]
        captured = capsys.readouterr()
        assert "done" in captured.out
        assert "security gate could not verify" in captured.err

    # ── Unchanged behaviour ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_stream_without_a_permission_request_is_unchanged(self, capsys):
        import kiro_crew.cli_chat as cli_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        class _PlainProvider(self._GatedProvider):
            async def stream(self, message):
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="hello ")
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="world")
                yield LLMEvent(kind=EVENT_COMPLETE)

        provider = _PlainProvider(self._event(), [])
        await cli_chat._send_and_print(provider, "hi")
        assert capsys.readouterr().out == "hello world\n"
        assert provider.calls == []


class TestDenialNoticeEncoding:
    """The automatic-denial notice interpolates a title the model or the tool
    chose, and is written to stderr precisely when stderr is REDIRECTED — where
    the stream encodes with the locale codec rather than UTF-8.

    Run in a CHILD PROCESS on purpose. The contract under test is a property of
    the real interpreter's standard streams, and in-process substitutes do not
    have it: a ``TextIOWrapper`` or ``StringIO`` built by a test carries whatever
    error handler the test chose, so a strict one would manufacture a failure
    this code path cannot actually produce, and pytest's own capture replaces
    ``sys.stderr`` outright.
    """

    #: Han the cp950 codec can encode, plus an emoji no legacy codepage can.
    _TITLE = "刪除全部 \U0001f4a3 rm -rf"

    _CHILD = """
import asyncio, json, sys
import kiro_crew.cli_chat as cli_chat
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

print(f"encoding={sys.stderr.encoding}")
print(f"errors={sys.stderr.errors}")


class P:
    async def approve_tool(self, rid):
        print("approve")

    async def reject_tool(self, rid):
        print("reject")


event = LLMEvent(
    kind=EVENT_PERMISSION_REQUEST,
    request_id=7,
    title=%(title)r,
    tool_kind="read",
    is_shell=False,
    tool_input=json.dumps({}),
    tool_name="",
    options=[{"id": "allow", "label": "Allow Once"}],
)
# interactive=False takes the automatic-denial branch, whose notice goes to
# stderr with the title interpolated into it.
asyncio.run(cli_chat._answer_permission(P(), event, interactive=False))
print("survived")
"""

    @pytest.mark.parametrize("io_encoding", ["cp950", "cp950:strict"])
    def test_denial_notice_survives_a_non_utf8_redirected_stderr(self, tmp_path, io_encoding):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = io_encoding
        # Keep the SEL audit write inside the test's own tree.
        env["KIROCREW_HOME"] = str(tmp_path / "home")
        out_path = tmp_path / "child.out"
        err_path = tmp_path / "child.err"
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            # flake-ok: pre-existing real child interpreter run without a timeout, moved verbatim; tracked for a de-flake pass
            rc = subprocess.call(
                [sys.executable, "-c", self._CHILD % {"title": self._TITLE}],
                stdout=out,
                stderr=err,  # a real OS handle, so the stream is redirected
                env=env,
                # Anchor the child OUTSIDE the checkout. It imports the installed
                # package, so CWD is not on its import path, and any relative
                # artifact it or a library writes then lands in the test's own tree
                # instead of surviving in the repo.
                cwd=tmp_path,
            )
        stdout = out_path.read_text(encoding="utf-8", errors="replace")

        assert rc == 0, f"the denial notice killed the CLI:\n{stdout}"
        assert "survived" in stdout
        assert "reject" in stdout
        # The premise a reviewer keeps reaching for is that a locale codec makes
        # this strict. It does not: CPython pins stderr to backslashreplace, and
        # the ``:strict`` half of PYTHONIOENCODING is ignored for this stream.
        assert "encoding=cp950" in stdout
        assert "errors=backslashreplace" in stdout
        # And the unencodable character is escaped rather than raised.
        written = err_path.read_bytes().decode("cp950", errors="replace")
        assert "\\U0001f4a3" in written


class TestChatProviderShutdown:
    """``provider.start()`` hands back a live backend process, so every exit from
    the turn -- return, error, or cancellation -- has to run ``shutdown()``."""

    class _FakeProvider:
        def __init__(self):
            self.started = 0
            self.shutdowns = 0
            self.child_fidelity_aware = False
            self.fidelity_aware_at_start: bool | None = None

        async def start(self):
            self.started += 1
            # Record the flag as start() sees it: the opt-in must precede the
            # backend spawn, or an early child permission frame races the gate.
            self.fidelity_aware_at_start = self.child_fidelity_aware

        async def shutdown(self):
            self.shutdowns += 1

        def context_usage_pct(self):
            return 0.0

    def _patch(self, monkeypatch, provider):
        import kiro_crew.cli_chat as cli_chat
        from kiro_crew.config import KiroCrewConfig

        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: KiroCrewConfig()))
        monkeypatch.setattr(
            cli_chat, "build_provider_factory", lambda cfg: (lambda *a, **k: provider)
        )
        monkeypatch.setattr(cli_chat, "_build_tool_gate", lambda agent: None)

    @pytest.mark.asyncio
    async def test_shutdown_runs_when_the_turn_is_cancelled(self, monkeypatch):
        # A permission prompt cancelled at the terminal raises through the turn
        # by design. The backend must still be torn down, and the cancellation
        # must still reach the caller.
        import kiro_crew.cli_chat as cli_chat

        provider = self._FakeProvider()
        self._patch(monkeypatch, provider)

        async def cancelled(*a, **k):
            raise asyncio.CancelledError

        monkeypatch.setattr(cli_chat, "_send_and_print", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await cli_chat._chat("hello", None)
        assert provider.shutdowns == 1

    @pytest.mark.asyncio
    async def test_shutdown_runs_when_the_turn_raises(self, monkeypatch):
        import kiro_crew.cli_chat as cli_chat

        provider = self._FakeProvider()
        self._patch(monkeypatch, provider)

        async def boom(*a, **k):
            raise RuntimeError("backend died")

        monkeypatch.setattr(cli_chat, "_send_and_print", boom)
        with pytest.raises(RuntimeError, match="backend died"):
            await cli_chat._chat("hello", None)
        assert provider.shutdowns == 1

    @pytest.mark.asyncio
    async def test_chat_opts_into_the_child_fidelity_contract_before_start(self, monkeypatch):
        """The CLI implements the low-fidelity child downgrade (hook gate ->
        _unverifiable_shell fail-close -> trusted-fields-only human prompt), so
        _chat must opt in -- and BEFORE start(), or an early child permission
        frame races the handle's fail-close gate and is rejected as
        `child_low_fidelity_unaware_consumer`, the auto-deny this fix ends."""
        import kiro_crew.cli_chat as cli_chat

        provider = self._FakeProvider()
        self._patch(monkeypatch, provider)

        async def done(*a, **k):
            return None

        monkeypatch.setattr(cli_chat, "_send_and_print", done)
        await cli_chat._chat("hello", None)
        assert provider.fidelity_aware_at_start is True

    @pytest.mark.asyncio
    async def test_shutdown_runs_exactly_once_on_the_normal_path(self, monkeypatch):
        import kiro_crew.cli_chat as cli_chat

        provider = self._FakeProvider()
        self._patch(monkeypatch, provider)

        async def ok(*a, **k):
            return None

        monkeypatch.setattr(cli_chat, "_send_and_print", ok)
        await cli_chat._chat("hello", None)
        assert provider.shutdowns == 1

    @pytest.mark.asyncio
    async def test_gc_runs_even_when_shutdown_raises(self, monkeypatch):
        # gc.collect() collects the subprocess transports while the loop is
        # still open; a failing shutdown must not skip it, and must not replace
        # the exception already propagating.
        import kiro_crew.cli_chat as cli_chat

        class _BadShutdown(self._FakeProvider):
            async def shutdown(self):
                self.shutdowns += 1
                raise RuntimeError("shutdown failed")

        provider = _BadShutdown()
        self._patch(monkeypatch, provider)
        collected = {"n": 0}
        monkeypatch.setattr(cli_chat.gc, "collect", lambda: collected.__setitem__("n", 1))

        async def cancelled(*a, **k):
            raise asyncio.CancelledError

        monkeypatch.setattr(cli_chat, "_send_and_print", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await cli_chat._chat("hello", None)
        assert provider.shutdowns == 1
        assert collected["n"] == 1
