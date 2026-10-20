"""Support shared by ``test_cli.py`` and the ``test_cli_<topic>.py`` parts split out of it.

``_doctor_reads_the_warm_probe_cache`` is ``test_cli.py``'s autouse fixture; its docstring's
"here" and "this file" mean ``test_cli.py`` and the doctor parts, whose doctor tests it serves.
Each of these files binds it by import, so every test of the family runs under the same autouse
stub. The doctor tests' helpers (``_noop_probe_server``, ``_write_agent_config``,
``_healthy_agent_file``, ``_pin_default_config``; the latter's "this file" means the same) serve
``test_cli.py`` and the two doctor parts, and ``_TestChatPermissionRequestHarness`` holds the
members both chat permission classes read.
"""

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest


async def _noop_probe_server(server):
    """Default probe stub for tests that call ``_doctor()`` but aren't
    specifically exercising the MCP handshake. Marks the target healthy
    so doctor renders the MCP section cleanly without spawning a real
    child process.

    Tests that care about specific probe outcomes (success with tool
    count, failure with stderr, etc.) build their own probe mocks via
    ``TestDoctorMcpTools._mock_probe``.
    """
    server.status = "ok"
    server.tools = []
    return server


def _write_agent_config(path: Path, *, tools: list[str], allowed: list[str], servers: dict) -> None:
    """Write a ``kirocrew.json`` agent config with the given managed
    servers + tool references. Typed keyword arguments make it obvious
    which fields each test cares about.
    """
    path.write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "tools": tools,
                "allowedTools": allowed,
                "mcpServers": servers,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _healthy_agent_file(path: Path) -> None:
    """Write a ``kirocrew.json`` whose managed MCP servers are all present
    so ``_doctor()``'s MCP section passes its static config check and only
    the (mocked) live probe remains. Used by doctor tests that exercise an
    unrelated section and must not trip the MCP exit path on an empty config.

    The server set is DERIVED from the same registry ``cli_doctor`` iterates
    (``mcp_cleanup.KIROCREW_BIN_MCP_SERVERS``) rather than hardcoded: doctor
    reports every managed server missing from ``mcpServers`` as an issue and
    exits 1, so a literal list here silently rots the moment a managed server is
    added — which is exactly how these fixtures broke when ``kirocrew-computer``
    landed.
    """
    from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

    refs = [f"@{name}" for name in KIROCREW_BIN_MCP_SERVERS]
    _write_agent_config(
        path,
        tools=refs,
        allowed=refs,
        servers={
            name: {
                # sys.executable, not a literal like /usr/local/bin/kirocrew:
                # doctor's dead-path scan stats every absolute command for real
                # (no shutil.which stub covers it), so the fixture's "healthy"
                # spec must name a binary that actually exists on the runner.
                "command": sys.executable,
                # The subcommand is the server name minus the "kirocrew-" prefix
                # ("kirocrew-core" -> "mcp-core"), matching the real invocation.
                "args": [f"mcp-{name.split('-', 1)[1]}"],
            }
            for name in KIROCREW_BIN_MCP_SERVERS
        },
    )


def _pin_default_config(monkeypatch) -> None:
    """Make ``_doctor()``'s config read hermetic for doctor tests.

    ``_doctor()`` calls the real ``KiroCrewConfig.load()`` / ``load_credentials()``,
    which read the shared ``~/.kirocrew`` config at runtime. ``KiroCrewConfig.save()``
    writes that same shared path non-atomically, so under ``pytest -n auto`` a
    concurrent worker's config write races these reads: a polluted/foreign config
    flips a check and ``_doctor()`` exits 1. xdist worker interleaving differs per
    interpreter, so the flake surfaced only on python3.10. Pin both to a pristine
    default (Slack-less) so doctor runs are deterministic and isolated.

    Speech-to-Text is switched off on top of the defaults, where it ships ON. Its
    section reports the recogniser, which lives in the optional ``voice`` extra, so
    leaving it enabled makes every doctor test in this file depend on whether the
    runner installed that wheel, and on POSIX an absent one is a hard issue: the
    tests asserting ``_doctor()`` does not exit would then report the host rather
    than the behaviour they name. The STT arms pin it back on for themselves.

    The harness is pinned to kiro-cli: a fresh config defaults to KAS, whose
    engine check needs a kiro-cli with ``--agent-engine`` that the stubbed
    ``kiro-cli 1.0.0`` does not report.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
    from kiro_crew.config.loader import KiroCrewConfig

    def _pristine() -> KiroCrewConfig:
        cfg = KiroCrewConfig()
        cfg.stt.enabled = False
        cfg.agent.acp_backend = ACP_BACKEND_KIRO
        return cfg

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _pristine()))
    monkeypatch.setattr(KiroCrewConfig, "load_credentials", lambda self: {})


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no importing module calls monkeypatch.undo()
def _doctor_reads_the_warm_probe_cache(monkeypatch):
    """The doctor's blocking ``warm_backend()`` is a no-op here.

    ``_doctor()`` calls ``sandbox.warm_backend()`` before its MCP probes so a fresh
    CLI process has a settled backend verdict before anything spawns on the loop.
    In this suite the rootdir conftest already keeps ``sandbox._backend`` warm for
    every test (``pytest_runtest_setup``), yet the warm thread re-runs the real
    probe unconditionally -- a fork plus a ``sys.executable`` spawn per doctor
    test, 43 of them in this file, all against a verdict the conftest already
    holds. The doctor then reads the cache the conftest filled, which is the same
    thing a warmed boot gives it. Nothing in this file tests the warm itself
    (``test_sandbox_backend_cache.py`` does).
    """
    import kiro_crew.cli_doctor as _doc

    monkeypatch.setattr(_doc, "warm_backend", lambda timeout=None: None)


class _TestChatPermissionRequestHarness:
    """The members both permission-request classes read.

    The turn bound ``_TIMEOUT``, the autouse stdin-poisoning reset, the request builder
    ``_event``, the gated provider, the real ``HookManager`` gate, the shared seams
    ``_patch_env`` and the one-turn driver ``_drive``. No ``Test`` prefix, so pytest never
    collects it; ``TestChatPermissionRequest`` and ``TestChatPermissionRequestConsentSurface``
    inherit it.
    """

    #: Bounds a stalled turn so the suite fails instead of hanging. Never
    #: reached when the request is answered.
    _TIMEOUT = 10.0

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture moved verbatim; no test of these classes calls monkeypatch.undo()
    def _fresh_stdin_state(self, monkeypatch):
        """Poisoning is process-wide state; monkeypatch restores it per test."""
        import kiro_crew.cli_chat as cli_chat

        monkeypatch.setattr(cli_chat, "_stdin_poisoned", False)

    @staticmethod
    def _event(
        *,
        title="Terminal",
        command=None,
        tool_name="",
        mcp_server_name="",
        tool_kind=None,
        shell_classified=True,
        raw_params=None,
    ):
        """A permission request in the shape the wire delivers.

        ``tool_input`` rather than ``raw_tool_params`` carries the command,
        because that is where a permission_request puts it -- the fallback the
        gate depends on to recover what really executes.

        ``tool_kind`` defaults to ``execute`` only when a command is supplied.
        An execute-kind request with no recoverable command is the cache-miss
        anomaly ``_unverifiable_shell`` refuses outright, so it must not be the
        default shape for tests about display, teardown or stdin -- those need a
        request that actually reaches the prompt. Pass ``tool_kind`` explicitly
        to exercise the anomaly.

        ``shell_classified`` defaults True for the same reason: the normal wire
        shape is a preceding ``tool_call`` that carried a resolvable ``kind``, so
        the shell cache was populated and ``is_shell`` is a RESOLVED answer. The
        miss -- where nothing was classified and the gate must refuse rather than
        read the payload's own kind -- is opted into explicitly.
        """
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

        return LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=7,
            title=title,
            tool_kind=tool_kind if tool_kind is not None else ("execute" if command else "read"),
            is_shell=command is not None,
            shell_classified=shell_classified,
            tool_input=json.dumps({"command": command}) if command else "",
            tool_name=tool_name,
            raw_tool_params=raw_params,
            mcp_server_name=mcp_server_name,
            # Advertised by a real backend; the CLI must NOT read these -- option
            # ids are backend-specific and the ACP layer owns the mapping.
            options=[{"id": "allow", "label": "Allow Once"}],
        )

    class _GatedProvider:
        """Cannot finish its turn until the permission is answered.

        Provider calls and audit records land in ONE ``trace`` so their relative
        order is observable, not just their presence.
        """

        def __init__(self, event, trace):
            self._event, self.trace = event, trace
            self.answered = asyncio.Event()

        @property
        def calls(self):
            return [t for t in self.trace if t[0] in ("approve", "reject")]

        async def stream(self, message):
            from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="I'll check that. ")
            yield self._event
            # flake-ok: pre-existing wait on the stub's answered event, moved verbatim; the permission answer sets it
            await self.answered.wait()
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
            yield LLMEvent(kind=EVENT_COMPLETE)

        async def approve_tool(self, request_id, *, always: bool = False):
            self.trace.append(("approve", request_id, always))
            self.answered.set()
            return True

        async def reject_tool(self, request_id):
            self.trace.append(("reject", request_id, False))
            self.answered.set()

        def context_usage_pct(self):
            return 0.0

    @staticmethod
    def _gate():
        """A gate carrying the REAL HookManager: the built-in sensitive-path and
        denied-command rules are the thing under test in several cases."""
        import kiro_crew.cli_chat as cli_chat
        from kiro_crew.hooks import HookManager, HooksConfig

        return cli_chat._ToolGate(hooks=HookManager(HooksConfig.from_dict({})), agent="cli-tester")

    @staticmethod
    def _patch_env(monkeypatch, *, tty=True, trace=None, answer="d"):
        """Wire the seams every case shares. Returns the stdin-read record.

        The blocking read is patched at ``_read_line_blocking`` -- the single
        blocking seam -- NOT at the choice helper or the gate, so the exact-match
        rule, the daemon-thread plumbing, the gate call and the audit ordering
        all run as production code.
        """
        import kiro_crew.cli_chat as cli_chat

        monkeypatch.setattr(sys.stdin, "isatty", lambda: tty, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: tty, raising=False)
        monkeypatch.setattr(
            cli_chat,
            "sel",
            lambda: types.SimpleNamespace(
                log_tool_invocation=lambda **kw: (
                    trace.append(("sel", kw)) if trace is not None else None
                )
            ),
        )
        reads = {"n": 0, "prompts": []}

        def fake_read(prompt=""):
            reads["n"] += 1
            reads["prompts"].append(prompt)
            if answer is EOFError:
                raise EOFError
            return answer

        monkeypatch.setattr(cli_chat, "_read_line_blocking", fake_read)
        return reads

    async def _drive(self, monkeypatch, *, event=None, interactive=True, tty=True, answer="d"):
        """Run one gated turn. Returns (provider, sel records, stdin reads).

        ``interactive`` is the command mode the caller passes and ``tty`` is
        patched onto the real streams, so the production ``_can_prompt`` decides
        -- patching that helper would test the stub rather than the rule.
        """
        import kiro_crew.cli_chat as cli_chat

        trace: list = []
        provider = self._GatedProvider(event or self._event(), trace)
        reads = self._patch_env(monkeypatch, tty=tty, trace=trace, answer=answer)
        await asyncio.wait_for(
            cli_chat._send_and_print(
                provider, "run it", interactive=interactive, gate=self._gate()
            ),
            timeout=self._TIMEOUT,
        )
        return (
            provider,
            [t[1] for t in trace if t[0] == "sel" and t[1].get("outcome") != "approval_pending"],
            reads,
        )
