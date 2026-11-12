"""Log lines that echo stored, model-written or child-process text redact it first.

Each sink here can carry a credential the log filter's narrow patterns do not
know (a GitHub token, a signed URL), so the line runs the module's own redactor,
or names only the size of what failed.
"""

from __future__ import annotations

import json
import logging

import pytest

TOKEN = "ghp_" + "a1B2" * 9


class _FakeStream:
    def __init__(self, lines):
        self._lines = [f"{line}\n".encode() for line in lines]

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeProcess:
    def __init__(self, lines):
        self.stderr = _FakeStream(lines)


def _runtime(lines):
    from kiro_crew.acp.runtime import AcpRuntime

    runtime = AcpRuntime.__new__(AcpRuntime)
    runtime.recording_allowed = True
    runtime._stderr_lines = []
    runtime._saw_auth_failure = False
    runtime._saw_sandbox_init_failure = False
    runtime._saw_delegate_launch_failure = False
    runtime._initialized = False
    runtime._first_session_ready = False
    runtime._process = _FakeProcess(lines)
    return runtime


class TestChildStderrDebugLine:
    @pytest.mark.parametrize(
        "line",
        [
            f"auth failed: header Authorization: token {TOKEN}",
            f"fetch https://x:{TOKEN}@github.com/o/r.git refused",
            # A long prefix puts the token across the 200-char cut.
            "x" * 190 + f" {TOKEN}",
        ],
        ids=["auth-header", "url-userinfo", "across-the-cut"],
    )
    @pytest.mark.asyncio
    async def test_the_stderr_line_is_redacted(self, caplog, line):
        runtime = _runtime([line])
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp"):
            await runtime._drain_stderr()
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stderr: ")]
        assert lines, "the stderr debug line was not logged"
        # Even a token prefix cut at 200 chars must not survive.
        assert all("ghp_" not in got for got in lines), lines


class TestFailedJsonParseLine:
    @pytest.mark.parametrize(
        "reply",
        [
            f"Sure! The user's key is {TOKEN} and nothing parses here",
            f'{{"note": "{TOKEN}", broken',
        ],
        ids=["prose", "broken-json"],
    )
    def test_the_reply_text_is_not_logged(self, caplog, reply):
        from kiro_crew.llm_helpers import parse_llm_json

        with caplog.at_level(logging.DEBUG, logger="kiro_crew"):
            assert parse_llm_json(reply) is None
        lines = [
            r.getMessage() for r in caplog.records if "Failed to parse LLM JSON" in r.getMessage()
        ]
        assert lines, "the parse-failure line was not logged"
        assert all(TOKEN not in got for got in lines), lines
        assert all(str(len(reply)) in got for got in lines), lines


class TestMalformedLoopRowLine:
    @pytest.mark.parametrize(
        "row",
        [
            # no message: the constructor raises
            {"id": "bad-1", "slot_key": "chat-1-1", "banner": f"tok {TOKEN}", "idle_secs": "x"},
            # a monitor that is not an object raises before the banner/message scrub
            {"id": "bad-2", "slot_key": "chat-1-1", "message": f"use {TOKEN}", "monitor": "bad"},
        ],
    )
    def test_the_row_is_redacted(self, tmp_path, caplog, row):
        from kiro_crew.autonudge import AutoNudgeService

        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew"):
                svc._load()
        finally:
            svc.stop()
        lines = [r.getMessage() for r in caplog.records if "malformed loop entry" in r.getMessage()]
        assert lines, "the malformed row was not logged"
        assert all(TOKEN not in got for got in lines), lines
        assert row in svc._store.unparsed_rows, "the row itself must still be kept"
