"""The tool_input security scan must not run on the asyncio event loop.

``_resolve_permission`` inspects EVERY string ``_extract_tool_input_strings``
pulls out of the parsed tool input — including whole document bodies, which are
scanned as shell commands. The three predicates
(``sensitive_path_refusal`` / ``is_sensitive_bash_command`` / ``is_denied``) are
regex-heavy, so one long newline-free line held the loop past the 25s watchdog
and killed the gateway (Mesh-3693). The scan now happens in ONE
``asyncio.to_thread`` hop for the whole loop.

These tests pin three contracts:

1. Equivalence — the decision AND the reason/mechanism reaching the SEL row are
   byte-identical to the pre-offload behaviour, for a denied path, a denied bash
   command, a benign payload, a nested structure, and an empty input.
2. Off-loop — the predicates run on a thread that is not the loop's when they
   are applied to tool_input strings (the title-tier checks above still run on
   the loop, so calls are matched by their argument).
3. Liveness — a concurrent asyncio task keeps ticking while a 20 KB
   newline-free non-shell document body is scanned.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew import llm_helpers, security
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

# A title that survives every title-tier check, so the decision under test is
# decided by tool_input alone.
_BENIGN_TITLE = "Read"


class _RecordingProvider:
    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


def _event(tool_input: str, title: str = _BENIGN_TITLE, *, is_shell: bool = False) -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=title,
        request_id="r1",
        tool_input=tool_input,
        is_shell=is_shell,
    )


async def _resolve(
    tool_input: str, title: str = _BENIGN_TITLE, *, is_shell: bool = False
) -> tuple[bool, _RecordingProvider, list[dict]]:
    """Drive ``_resolve_permission`` and capture every SEL row it logged."""
    provider = _RecordingProvider()
    rows: list[dict] = []
    sel_stub = MagicMock()
    sel_stub.log_tool_invocation.side_effect = lambda **kw: rows.append(kw)
    with patch.object(sel_mod, "sel", lambda: sel_stub):
        approved = await _resolve_permission(
            provider,  # type: ignore[arg-type]
            _event(tool_input, title, is_shell=is_shell),
            ToolApprovalPolicy.AUTO_APPROVE,
            None,
        )
    return approved, provider, rows


def _decision(rows: list[dict]) -> tuple[str, str, str]:
    """(outcome, error, mechanism) of the single decision row."""
    assert len(rows) == 1, rows
    row = rows[0]
    return (
        row.get("outcome", ""),
        str(row.get("error") or ""),
        str((row.get("metadata") or {}).get("mechanism") or ""),
    )


class TestEquivalence:
    """The offloaded scan must decide exactly what the on-loop loop decided."""

    @pytest.mark.asyncio
    async def test_denied_sensitive_path_in_tool_input(self) -> None:
        payload = json.dumps({"path": "~/.aws/credentials"})
        approved, provider, rows = await _resolve(payload)
        assert approved is False
        assert provider.rejected == ["r1"]
        assert provider.approved == []
        outcome, error, mechanism = _decision(rows)
        assert outcome == "denied"
        assert error == "Blocked: sensitive path in tool_input: ~/.aws/credentials"
        assert mechanism == "always_deny_input"

    @pytest.mark.asyncio
    async def test_denied_bash_command_in_tool_input(self) -> None:
        payload = json.dumps({"command": "rm -rf /"})
        approved, provider, rows = await _resolve(payload)
        assert approved is False
        assert provider.rejected == ["r1"]
        outcome, error, mechanism = _decision(rows)
        assert outcome == "denied"
        assert error, "the bash reason text must still reach the SEL row"
        assert mechanism == "always_deny_input"

    @pytest.mark.asyncio
    async def test_benign_payload_is_approved(self) -> None:
        payload = json.dumps({"path": "README.md"})
        approved, provider, rows = await _resolve(payload)
        assert approved is True
        assert provider.approved == ["r1"]
        assert provider.rejected == []
        outcome, _error, _mechanism = _decision(rows)
        assert outcome == "auto_approved"

    @pytest.mark.asyncio
    async def test_nested_structure_is_still_scanned(self) -> None:
        """The recursive walk's reach must not narrow: a deny nested three
        levels down inside a list still denies, and still names that string."""
        payload = json.dumps(
            {"args": {"targets": ["README.md", {"file": "~/.ssh/id_rsa"}]}, "mode": "read"}
        )
        approved, provider, rows = await _resolve(payload)
        assert approved is False
        assert provider.rejected == ["r1"]
        outcome, error, mechanism = _decision(rows)
        assert outcome == "denied"
        assert error == "Blocked: sensitive path in tool_input: ~/.ssh/id_rsa"
        assert mechanism == "always_deny_input"

    @pytest.mark.asyncio
    async def test_first_denial_short_circuits_in_order(self) -> None:
        """Order is preserved: the FIRST denying string decides, and a later
        denying string never reaches the reason."""
        payload = json.dumps(["~/.aws/credentials", "~/.ssh/id_rsa"])
        _approved, _provider, rows = await _resolve(payload)
        _outcome, error, _mechanism = _decision(rows)
        assert error == "Blocked: sensitive path in tool_input: ~/.aws/credentials"

    @pytest.mark.asyncio
    async def test_empty_tool_input_skips_the_scan_entirely(self) -> None:
        with patch.object(
            llm_helpers, "_first_tool_input_denial", side_effect=AssertionError("scanned")
        ):
            approved, provider, rows = await _resolve("")
        assert approved is True
        assert provider.approved == ["r1"]
        outcome, _error, _mechanism = _decision(rows)
        assert outcome == "auto_approved"


class TestOffLoop:
    """The scan's predicates must not execute on the event loop's thread."""

    @pytest.mark.asyncio
    async def test_scan_predicates_run_on_a_worker_thread(self) -> None:
        loop_ident = threading.get_ident()
        probe_target = "kirocrew-offload-probe.md"
        seen: list[int] = []
        real = llm_helpers.sensitive_path_refusal

        def _probe(s: str, *a, **kw):
            if s == probe_target:
                seen.append(threading.get_ident())
            return real(s, *a, **kw)

        with patch.object(llm_helpers, "sensitive_path_refusal", _probe):
            approved, _provider, _rows = await _resolve(json.dumps({"path": probe_target}))

        assert approved is True
        assert seen, "the tool_input scan never ran"
        assert loop_ident not in seen, (
            "the tool_input scan ran on the event loop thread; a long payload "
            "there stalls the watchdog and takes the gateway down"
        )

    @pytest.mark.asyncio
    async def test_whole_loop_is_one_thread_hop(self) -> None:
        """One ``to_thread`` for the whole loop, not one per string."""
        loop_ident = threading.get_ident()
        strings = [f"offload-file-{i}.txt" for i in range(25)]
        seen: list[int] = []
        real = llm_helpers.sensitive_path_refusal

        def _probe(s: str, *a, **kw):
            if s.startswith("offload-file-"):
                seen.append(threading.get_ident())
            return real(s, *a, **kw)

        with patch.object(llm_helpers, "sensitive_path_refusal", _probe):
            await _resolve(json.dumps(strings))

        assert len(seen) == 25, seen
        assert len(set(seen)) == 1, "the scan hopped threads per string instead of once"
        assert loop_ident not in seen, "the single hop landed on the event loop thread"


class TestOversizeFailClosed:
    """A string too large to scan is DENIED, never skipped.

    The thread hop yields between strings but not within one -- ``re`` holds the
    GIL for a whole match call -- so one huge string could still hold the loop
    past the 25s watchdog and kill the gateway. The guard refuses such input.

    Fail-closed is the load-bearing property: skipping an unscannable string
    would let it past the deny surface, which trades a crash for a security
    hole. These tests pin the denial, not merely the absence of a stall.
    """

    def test_oversized_string_is_denied_not_skipped(self) -> None:
        cap = llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS
        # Deliberately BENIGN content: nothing here matches any predicate, so a
        # skip would return None and the call would be approved. Only the guard
        # can produce a denial.
        blob = "a" * (cap + 1)
        hit = llm_helpers._first_tool_input_denial([blob], None)
        assert hit is not None, "oversized input must be refused, not skipped"
        kind, reason, matched = hit
        assert kind == "oversize"
        assert str(cap) in reason and str(len(blob)) in reason
        assert len(matched) <= 64, "the echoed sample must stay bounded"

    def test_string_at_the_cap_is_still_scanned(self, monkeypatch) -> None:
        """The guard must not fire one character early.

        The cap is patched DOWN rather than scanning a real full-size string:
        the scan is superlinear, so a genuine at-the-cap scan costs seconds here
        and timed out at 120s on a loaded CI runner. The off-by-one this pins is
        a property of the comparison, not of the length.
        """
        monkeypatch.setattr(llm_helpers, "_MAX_SCANNABLE_TOOL_INPUT_CHARS", 64)
        assert llm_helpers._first_tool_input_denial(["a" * 64], None) is None
        assert llm_helpers._first_tool_input_denial(["a" * 65], None) is not None

    def test_a_real_denial_still_wins_over_size(self) -> None:
        """An earlier scannable string keeps its own, more specific reason."""
        cap = llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS
        hit = llm_helpers._first_tool_input_denial(["~/.aws/credentials", "a" * (cap + 1)], None)
        assert hit is not None
        assert hit[0] == "path"

    @pytest.mark.asyncio
    async def test_oversized_tool_input_is_rejected_end_to_end(self) -> None:
        cap = llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS
        payload = json.dumps({"content": "a" * (cap + 1)})
        approved, provider, rows = await _resolve(payload)
        assert approved is False
        assert provider.rejected == ["r1"]
        assert provider.approved == []
        outcome, error, mechanism = _decision(rows)
        assert outcome == "denied"
        assert "too large to security-scan" in error
        assert mechanism == "always_deny_input"

    @pytest.mark.asyncio
    async def test_the_guard_bounds_the_scan_far_under_the_watchdog(self) -> None:
        """A 1 MB body returns promptly instead of scanning for minutes."""
        payload = json.dumps({"content": "a" * (1024 * 1024)})
        started = time.perf_counter()
        approved, _provider, _rows = await _resolve(payload)
        elapsed = time.perf_counter() - started
        assert approved is False
        assert elapsed < 5.0, (
            f"oversized input took {elapsed:.2f}s -- the size guard is not "
            "short-circuiting before the regex predicates"
        )


class TestLiveness:
    """A 20 KB newline-free document body must not stall the loop."""

    @pytest.mark.timeout(300)
    @pytest.mark.asyncio
    async def test_loop_keeps_ticking_during_a_large_document_scan(self) -> None:
        # Newline-free, no shell metacharacters: a plain prose body, which is
        # exactly the payload that could be scanned as one giant command.
        body = ("the quick brown fox jumps over the lazy dog " * 500)[:20_000]
        assert "\n" not in body and len(body) >= 20_000

        ticks = 0
        stop = False

        async def _ticker() -> None:
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(_ticker())
        try:
            await asyncio.sleep(0.02)
            before = ticks
            started = time.monotonic()
            approved, _provider, _rows = await _resolve(json.dumps({"content": body}))
            elapsed = time.monotonic() - started
        finally:
            stop = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert approved is True
        # Generous: the scan itself legitimately costs tens of seconds on this
        # payload (is_sensitive_bash_command dominates). What the watchdog cares
        # about is that the LOOP stayed live throughout, asserted below.
        assert elapsed < 120.0, f"scan took {elapsed:.1f}s — far past any sane bound"
        assert ticks > before, (
            f"the event loop made no progress during the scan (ticks stuck at "
            f"{before}); the scan is still blocking the loop"
        )


class TestTitleTierOffLoop:
    """The title -- for a shell tool, the command itself -- is scanned on the
    same worker hop as tool_input, with the reasons and mechanisms the on-loop
    checks produced. The field crash was a ~9 KB shell TITLE parked on the loop
    for 25 s while only the tool_input tier was offloaded."""

    @pytest.mark.asyncio
    async def test_title_bash_denial_keeps_reason_and_mechanism(self) -> None:
        approved, provider, rows = await _resolve("", title="env | grep AWS_SECRET")
        assert approved is False
        assert provider.rejected == ["r1"]
        outcome, error, mechanism = _decision(rows)
        assert outcome == "denied"
        assert (
            error.splitlines()[0]
            == "Blocked: command reads AWS credentials from environment variables"
        )
        assert mechanism == "always_deny"

    @pytest.mark.asyncio
    async def test_title_path_denial_keeps_reason_and_mechanism(self) -> None:
        _approved, _provider, rows = await _resolve("", title="~/.ssh/id_rsa")
        outcome, error, mechanism = _decision(rows)
        assert outcome == "denied"
        assert error == "Blocked: sensitive path: ~/.ssh/id_rsa"
        assert mechanism == "always_deny"

    @pytest.mark.asyncio
    async def test_title_regex_denial_keeps_mechanism(self) -> None:
        _approved, _provider, rows = await _resolve("", title="rm -rf /")
        outcome, error, mechanism = _decision(rows)
        assert outcome == "denied"
        assert error
        assert mechanism == "always_deny"

    @pytest.mark.asyncio
    async def test_title_decides_before_tool_input(self) -> None:
        """Both tiers would deny; the title's reason wins, as it did when the
        title was checked inline before the tool_input loop ran."""
        payload = json.dumps({"path": "~/.ssh/id_rsa"})
        _approved, _provider, rows = await _resolve(payload, title="~/.aws/credentials")
        _outcome, error, mechanism = _decision(rows)
        assert error == "Blocked: sensitive path: ~/.aws/credentials"
        assert mechanism == "always_deny"

    @pytest.mark.asyncio
    async def test_title_predicates_run_off_the_loop_in_the_same_hop(self) -> None:
        loop_ident = threading.get_ident()
        title = "echo offload-title-probe"
        input_target = "offload-input-probe.md"
        seen: dict[str, int] = {}
        real_bash = llm_helpers.is_sensitive_bash_command
        real_path = llm_helpers.sensitive_path_refusal

        def _bash_probe(s: str, *a, **kw):
            if s == title:
                seen["title"] = threading.get_ident()
            return real_bash(s, *a, **kw)

        def _path_probe(s: str, *a, **kw):
            if s == input_target:
                seen["input"] = threading.get_ident()
            return real_path(s, *a, **kw)

        with (
            patch.object(llm_helpers, "is_sensitive_bash_command", _bash_probe),
            patch.object(llm_helpers, "sensitive_path_refusal", _path_probe),
        ):
            approved, _provider, _rows = await _resolve(
                json.dumps({"path": input_target}), title=title
            )

        assert approved is True
        assert set(seen) == {"title", "input"}, seen
        assert loop_ident not in seen.values(), (
            "a title-tier predicate ran on the event loop thread; a long shell "
            "title there is the crash path this offload exists to close"
        )
        assert seen["title"] == seen["input"], "title and tool_input scans took separate hops"

    @pytest.mark.asyncio
    async def test_missing_title_is_still_denied_on_the_loop(self) -> None:
        with patch.object(llm_helpers, "_title_denial", side_effect=AssertionError("scanned")):
            approved, provider, rows = await _resolve("", title="")
        assert approved is False
        assert provider.rejected == ["r1"]
        _outcome, error, mechanism = _decision(rows)
        assert error == "Blocked: missing tool title"
        assert mechanism == "always_deny"


class TestPathTierWording:
    """What the two path tiers SAY when the resolver stalls versus matches.

    Both tiers apply ``security.sensitive_path_refusal``. A match keeps each
    producer's historical wording; a stall passes the producer's unverifiable
    wording through unchanged, because that wording carries the anchor phrase
    ``deny_guidance`` classifies by.
    """

    def test_a_stalled_path_title_is_refused_as_unverifiable_not_as_sensitive(self) -> None:
        stalled = f"{security.UNVERIFIABLE_PATH_PREFIX}, so it is refused. Path: 'notes/todo.md'"
        with patch.object(llm_helpers, "sensitive_path_refusal", lambda *_a, **_k: stalled):
            hit = llm_helpers._title_denial("notes/todo.md", None)
        assert hit == ("path", stalled)

    def test_a_matched_path_title_keeps_this_producers_wording(self) -> None:
        with patch.object(
            llm_helpers,
            "sensitive_path_refusal",
            lambda p, *_a, **_k: f"Blocked: access to sensitive path: {p}",
        ):
            hit = llm_helpers._title_denial("~/.ssh/id_rsa", None)
        assert hit == ("path", "Blocked: sensitive path: ~/.ssh/id_rsa")

    def test_a_stalled_tool_input_string_is_refused_as_unverifiable(self) -> None:
        stalled = f"{security.UNVERIFIABLE_PATH_PREFIX}, so it is refused. Path: 'notes/todo.md'"
        with patch.object(llm_helpers, "sensitive_path_refusal", lambda *_a, **_k: stalled):
            hit = llm_helpers._first_tool_input_denial(["notes/todo.md"], None)
        assert hit == ("path", stalled, "notes/todo.md")

    def test_a_matched_tool_input_string_keeps_this_producers_wording(self) -> None:
        with patch.object(
            llm_helpers,
            "sensitive_path_refusal",
            lambda p, *_a, **_k: f"Blocked: access to sensitive path: {p}",
        ):
            hit = llm_helpers._first_tool_input_denial(["~/.ssh/id_rsa"], None)
        assert hit == (
            "path",
            "Blocked: sensitive path in tool_input: ~/.ssh/id_rsa",
            "~/.ssh/id_rsa",
        )


class TestShellCommandTextLeavesThePathTier:
    """The title and tool_input path tiers read PATHS; a shell tool's recovered
    COMMAND is command text. The funnel spares exactly that string the resolver
    (``hooks.on_tool_call`` makes the same exemption): no round-trip, and no stall
    refusal naming a command as a sensitive path. Every other string of a shell
    frame -- a structured ``use_aws`` argument naming a credential path -- stays
    path-gated, because in ``standard`` sandbox mode the sandbox is not the
    control for it; the path tier is.
    """

    CMD = "cd /x && grep -r TODO ."

    def test_the_recovered_command_never_reaches_the_path_tier(self) -> None:
        def never(*_a, **_k):
            raise AssertionError("the path tier ran on the recovered command text")

        with patch.object(llm_helpers, "sensitive_path_refusal", never):
            assert llm_helpers._title_denial(self.CMD, None, exempt_command=self.CMD) is None
            assert (
                llm_helpers._title_denial(f"Running: {self.CMD}", None, exempt_command=self.CMD)
                is None
            )
            assert (
                llm_helpers._first_tool_input_denial([self.CMD], None, exempt_command=self.CMD)
                is None
            )

    def test_a_title_that_is_not_the_command_still_pays_the_path_tier(self) -> None:
        seen: list[str] = []

        def record(path, *_a, **_k):
            seen.append(path)
            return None

        with patch.object(llm_helpers, "sensitive_path_refusal", record):
            assert llm_helpers._title_denial("notes/todo.md", None) is None
            assert llm_helpers._title_denial("list the repo", None, exempt_command=self.CMD) is None
        assert seen == ["notes/todo.md", "list the repo"]

    def test_a_matched_non_command_title_is_still_refused(self) -> None:
        with patch.object(
            llm_helpers, "sensitive_path_refusal", lambda *_a, **_k: "Blocked: sensitive path"
        ):
            hit = llm_helpers._title_denial("~/.ssh/id_rsa", None, exempt_command=self.CMD)
        assert hit == ("path", "Blocked: sensitive path: ~/.ssh/id_rsa")

    def test_other_payload_strings_of_a_shell_frame_still_pay_the_path_tier(self) -> None:
        """A shell-kind tool with structured parameters (kiro-cli ``use_aws``) can
        name a credential file as an ARGUMENT; that string is a path, not command
        text, and is refused exactly as before."""
        seen: list[str] = []

        def record(path, *_a, **_k):
            seen.append(path)
            return "Blocked: sensitive path" if path == "~/.aws/credentials" else None

        command = "aws s3api head-object --bucket b --key ~/.aws/credentials"
        with patch.object(llm_helpers, "sensitive_path_refusal", record):
            hit = llm_helpers._first_tool_input_denial(
                [command, "s3api", "head-object", "~/.aws/credentials"],
                None,
                exempt_command=command,
            )
        assert command not in seen, "the recovered command text must not be resolved"
        assert hit == (
            "path",
            "Blocked: sensitive path in tool_input: ~/.aws/credentials",
            "~/.aws/credentials",
        )

    def test_path_tier_exempt_is_the_recovered_command_of_a_sandboxed_shell_frame(self) -> None:
        shell = _event(
            json.dumps({"command": self.CMD}), title=f"Running: {self.CMD}", is_shell=True
        )
        assert llm_helpers._path_tier_exempt(shell) == self.CMD
        plain = _event(json.dumps({"path": "notes.md"}), title="Reading notes.md")
        assert llm_helpers._path_tier_exempt(plain) is None
        served = _event(json.dumps({"command": self.CMD}), title=self.CMD, is_shell=True)
        served.mcp_server_name = "local-files"
        assert llm_helpers._path_tier_exempt(served) is None
        unrecoverable = _event(json.dumps({"cwd": "/x"}), title="Bash", is_shell=True)
        assert llm_helpers._path_tier_exempt(unrecoverable) is None

    @pytest.mark.asyncio
    async def test_a_stalled_path_tier_cannot_refuse_a_shell_command_end_to_end(self) -> None:
        """Through ``_resolve_permission``: a path tier answering True for everything
        (a stall's fail-closed answer) does not refuse a shell command, because
        neither the command nor its ``Running:`` title is resolved."""
        with patch.object(
            llm_helpers, "sensitive_path_refusal", lambda *_a, **_k: "Blocked: sensitive path"
        ):
            approved, _provider, _rows = await _resolve(
                json.dumps({"command": self.CMD}), title=f"Running: {self.CMD}", is_shell=True
            )
        assert approved is True

    @pytest.mark.asyncio
    async def test_a_credential_argument_on_a_shell_frame_is_refused_end_to_end(self) -> None:
        params = {
            "service_name": "s3",
            "operation_name": "cp",
            "parameters": {"source": "~/.aws/credentials", "dest": "s3://bucket/x"},
        }
        approved, provider, rows = await _resolve(
            json.dumps(params), title="use_aws", is_shell=True
        )
        assert approved is False
        assert provider.rejected == ["r1"]
        assert "~/.aws/credentials" in _decision(rows)[1]

    @pytest.mark.asyncio
    async def test_an_mcp_served_shell_event_is_still_path_gated_end_to_end(self) -> None:
        """A shell-classified frame that also names an MCP server runs outside the
        sandbox; its title still pays the path tier and is refused."""
        with patch.object(
            llm_helpers, "sensitive_path_refusal", lambda *_a, **_k: "Blocked: sensitive path"
        ):
            provider = _RecordingProvider()
            rows: list[dict] = []
            sel_stub = MagicMock()
            sel_stub.log_tool_invocation.side_effect = lambda **kw: rows.append(kw)
            ev = _event(json.dumps({"command": "cat x"}), title="cat x", is_shell=True)
            ev.mcp_server_name = "local-files"
            with patch.object(sel_mod, "sel", lambda: sel_stub):
                approved = await _resolve_permission(
                    provider,  # type: ignore[arg-type]
                    ev,
                    ToolApprovalPolicy.AUTO_APPROVE,
                    None,
                )
        assert approved is False
        assert provider.rejected == ["r1"]

    def test_both_funnel_sites_carry_the_exemption_in_source(self) -> None:
        """Pinned in source, like ``test_deny_diff`` pins the hooks gate: a comment
        claiming the exemption is not the exemption. Each tier's body must gate its
        single path check on the exempt command text itself."""
        import inspect

        for tier in (llm_helpers._title_denial, llm_helpers._first_tool_input_denial):
            source = inspect.getsource(tier)
            assert "_is_exempt_command_text(" in source, tier.__name__
            assert source.count("sensitive_path_refusal(") == 1, tier.__name__
