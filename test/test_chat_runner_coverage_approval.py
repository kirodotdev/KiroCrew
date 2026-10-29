"""Coverage tests for ``kiro_crew.dashboard.chat_runner``: the approval ladder of a turn.

Covers the auto-approve rungs a ``_run_chat`` turn walks between the interactive
prompt and the session-trust flag (trusted patterns and their deadlines, read-only
bash, name grants) and the interactive approval window. The pure trust
checks are in ``test_chat_runner_coverage_trust.py``.
``test_chat_runner_coverage.py`` describes the two harnesses the family uses; the
shared builders are in ``chat_runner_coverage_helpers.py``.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from chat_runner_coverage_helpers import (
    _answer_card,
    _complete,
    _drive,
    _errors,
    _permission,
    _runner_state,
    _set_stream,
    _settle,
    _slot,
    _write_recorder,
)

from kiro_crew import name_grant
from kiro_crew.dashboard import chat_runner
from kiro_crew.trust_patterns import (
    GrantDeadline,
    base_trust_patterns,
    canonical_non_shell_trust_key,
    exact_trust_pattern,
)


def _card_outcomes(slot) -> list:
    """How each approval card the slot shows was resolved, in order."""
    return [
        json.loads(m["cls"]).get("resolved") for m in slot.messages if m.get("role") == "permission"
    ]


def _tool_rows(slot) -> list[str]:
    return [m.get("content", "") for m in slot.messages if m.get("role") == "tool"]


# ── _run_chat: auto-approve rungs ─────────────────────────────────────────


class TestRunChatAutoApproveRungs:
    @pytest.fixture(autouse=True)
    def _vouch_for_the_program(self):
        """Let these tests exercise the RUNG, not the host's program layout.

        Each rung now re-judges its own auto-approve by asking whether the
        command's program names still identify the programs they name
        (``name_grant``), which resolves against the real ``PATH`` and reads the
        file behind each name. That made these tests depend on the machine: they
        use ``ls -la``, which passes on Linux because ``ls`` lives in a trusted
        system directory and FAILS on Windows, where there is no such ``ls`` --
        so the rung declined, `approve_tool` was never called, and the assertions
        below broke on one platform only. The thread hop the real check needs also
        outlives these tests' event loop and crashed the xdist worker.

        Stubbing it keeps each test measuring what its name claims -- does the
        rung approve, reject, and render -- while the check itself is covered
        directly in ``test/test_name_grant.py``. It patches the ONE off-loop
        entry point every rung goes through; patching only the event-shaped
        wrapper left two rungs spawning real threads, which is why the Windows
        workers kept crashing after the first attempt at this.
        """

        with patch.object(
            chat_runner, "_name_grant_refusal_off_loop", new=AsyncMock(return_value=None)
        ):
            yield

    @pytest.mark.asyncio
    async def test_a_non_shell_approval_mints_no_shell_witness(self, tmp_path):
        """GPT 5.6 round-22: a non-shell approval must not pin a shell program.

        `extract_bash_command` reads a `command` key out of ANY structured input,
        so a non-shell MCP call carrying `{"command": "gh ..."}` would otherwise
        record a durable identity for `gh` from an approval that was never about
        running `gh`.
        """
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(
            client,
            [
                _permission(
                    title="Looking up the record",
                    tool_input='{"command": "gh repo view"}',
                    tool_kind="other",
                    is_shell=False,
                    tool_name="read_record",
                    mcp_server_name="records:primary",
                ),
                _complete(),
            ],
        )

        def _allow_once_when_registered():
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _allow_once_when_registered
        with patch.object(chat_runner, "pin_human_approval") as pin:
            await _drive(state, slot)

        # The branch really was reached -- otherwise the assertion below would
        # pass for the wrong reason, which is exactly how a guard like this gets
        # a test that cannot fail.
        client.approve_tool.assert_awaited_once_with("req-cov-1")
        pin.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_declined_name_grant_is_audited(self, tmp_path):
        """GPT 5.6 round-19: declining a grant is a security decision, so it is logged.

        Without this the audit trail shows a command arriving at the interactive
        card and never says a name grant was withheld, or why.
        """
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trust_reads = True
        _set_stream(
            client,
            [_permission(title="Running: ls -la", tool_input='{"command": "ls -la"}'), _complete()],
        )
        refusal = name_grant.Refusal(name_grant.SHADOWED, "ls resolves to /writable/ls")
        slot._empty_response_retries = 2
        with (
            patch.object(chat_runner, "sel") as mock_sel,
            patch.object(
                chat_runner, "_name_grant_refusal_off_loop", new=AsyncMock(return_value=refusal)
            ),
            patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        ):
            audit = MagicMock()
            mock_sel.return_value = audit
            await chat_runner._run_chat(state, slot, "hello")
        await _settle(slot)

        declined = [
            c.kwargs
            for c in audit.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "auto_approve_declined"
        ]
        assert declined, audit.log_tool_invocation.call_args_list
        assert declined[0]["metadata"]["code"] == name_grant.SHADOWED
        assert declined[0]["metadata"]["tier"] == "trust_reads"
        # The CODE, never the detail: the detail names resolved paths, and an
        # audit sink is exactly where that becomes a disclosure.
        assert "/writable/ls" not in json.dumps(declined[0], default=str)

    @pytest.mark.asyncio
    async def test_non_shell_trust_matches_cached_server_tool_identity(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        canonical = canonical_non_shell_trust_key("records:primary", "read_record")
        slot._trusted_patterns = {exact_trust_pattern(canonical)}
        _set_stream(
            client,
            [
                _permission(
                    title="Looking up the record",
                    tool_kind="other",
                    is_shell=False,
                    tool_name="read_record",
                    mcp_server_name="records:primary",
                ),
                _complete(),
            ],
        )

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        assert _card_outcomes(slot) == []
        assert _tool_rows(slot) == ["🔧 Looking up the record"]

    @pytest.mark.asyncio
    async def test_trusted_pattern_append_persists_tool_meta_untruncated(self, tmp_path):
        """The trusted-pattern rung's persisted meta must be `_tool_meta` exactly.

        A hand-rolled meta dict at this rung diverges from every other
        approval rung: a shorter purpose cap cuts restored transcripts
        mid-sentence while live rows (broadcast at `_MAX_TOOL_PURPOSE`)
        show the whole purpose, and a dict without `input`/`kind` or the
        `tool_call_id` redaction breaks the live↔replay join. One builder,
        one cap.
        """
        state, client = _runner_state(tmp_path)
        slot = _slot()
        canonical = canonical_non_shell_trust_key("records:primary", "read_record")
        slot._trusted_patterns = {exact_trust_pattern(canonical)}
        purpose = (
            "Get the PMET record header fields (Program/Marketplace/Operation) "
            "exactly as the LS writes them, from the small current-hour file "
            "(0 replicas, so few records), to query MWS with the right "
            "dimensions, then fold the result back into the incident brief"
        )
        assert len(purpose) > 200, "purpose long enough that a divergent short cap cuts it"
        perm = _permission(
            title="Looking up the record",
            tool_kind="other",
            is_shell=False,
            tool_name="read_record",
            mcp_server_name="records:primary",
        )
        perm.tool_call_id = "call-cov-1"
        perm.tool_purpose = purpose
        _set_stream(client, [perm, _complete()])

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        (tool_msg,) = [m for m in slot.messages if m.get("role") == "tool"]
        meta = tool_msg.get("meta") or {}
        assert meta.get("purpose") == purpose
        # Everything `_tool_meta` builds must be persisted verbatim; `append`
        # additionally stamps its own `mid`, which is not part of the contract.
        assert chat_runner._tool_meta(perm).items() <= meta.items()

    @pytest.mark.asyncio
    async def test_structured_non_shell_reprompt_never_matches_tool_identity_trust(self, tmp_path):
        """Consumed display input must not erase argument provenance."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        canonical = canonical_non_shell_trust_key("records:primary", "read_record")
        slot._trusted_patterns = {exact_trust_pattern(canonical)}
        permission = _permission(
            title="Looking up the record",
            tool_input="",
            tool_kind="other",
            is_shell=False,
            tool_name="read_record",
            mcp_server_name="records:primary",
            raw_tool_params={"record_id": "sensitive-record"},
        )
        _set_stream(client, [permission, _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_awaited_once_with("req-cov-1")
        (card,) = [m for m in slot.messages if m.get("role") == "permission"]
        meta = json.loads(card["cls"])
        assert "full_command" not in meta
        assert "trust_command_key" not in meta
        assert "trust_command_grantable" not in meta
        # The session-wide grant names no command, so it survives an
        # underivable one: only the command-scoped tiers are withheld.
        assert meta["trust_grantable"] == "1"

    @pytest.mark.asyncio
    async def test_reused_non_shell_title_cannot_match_another_tools_trust(self, tmp_path):
        """The title is model prose; only the cached ACP identity may match."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {
            exact_trust_pattern(canonical_non_shell_trust_key("records:primary", "read_record"))
        }
        _set_stream(
            client,
            [
                _permission(
                    title="Looking up the record",
                    tool_kind="other",
                    is_shell=False,
                    tool_name="delete_record",
                    mcp_server_name="records:primary",
                ),
                _complete(),
            ],
        )

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_awaited_once_with("req-cov-1")
        assert _card_outcomes(slot) == ["rejected"]

    @pytest.mark.asyncio
    async def test_non_shell_identity_collision_cannot_auto_approve_other_tool(self, tmp_path):
        """One wire-shaped display identity may not imply one trust identity."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        granted = canonical_non_shell_trust_key("github", "repo__delete")
        colliding = canonical_non_shell_trust_key("github__repo", "delete")
        assert granted != colliding
        slot._trusted_patterns = {exact_trust_pattern(granted)}
        _set_stream(
            client,
            [
                _permission(
                    title="Repository action",
                    tool_kind="other",
                    is_shell=False,
                    tool_name="delete",
                    mcp_server_name="github__repo",
                ),
                _complete(),
            ],
        )

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_non_shell_identity_cache_miss_stays_allow_once_or_reject(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"*"}
        _set_stream(
            client,
            [
                _permission(
                    title="Shared model-authored title",
                    tool_kind="other",
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _allow_once_when_registered():
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _allow_once_when_registered
        await _drive(state, slot)

        (card,) = [m for m in slot.messages if m.get("role") == "permission"]
        meta = json.loads(card["cls"])
        assert "full_command" not in meta
        assert "trust_command_key" not in meta
        assert "trust_command_grantable" not in meta
        assert meta["trust_grantable"] == "1"
        client.approve_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_redacted_command_never_matches_existing_trust_pattern(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        displayed = "echo [REDACTED: credential]"
        # This exact pattern can legitimately be granted for a command whose
        # literal argument is the display marker.  It must not cover a different
        # command that only became identical after transport redaction.
        slot._trusted_patterns = {exact_trust_pattern(displayed)}
        permission = _permission(tool_input=json.dumps({"command": displayed}))
        permission.tool_input_redacted = True
        _set_stream(client, [permission, _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_awaited_once_with("req-cov-1")
        assert _card_outcomes(slot) == ["rejected"]

    @pytest.mark.asyncio
    async def test_browser_cli_presence_does_not_skip_shell_approval(self, tmp_path, monkeypatch):
        from kiro_crew.browser_cli import install

        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(
            client,
            [
                _permission(
                    title="Running: playwright-cli snapshot",
                    tool_input=json.dumps({"command": "playwright-cli snapshot"}),
                ),
                _complete(),
            ],
        )
        # Pin the capability as present without consulting or touching the host.
        monkeypatch.setattr(install, "cli_path", lambda: "/agent-writable/playwright-cli")

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_awaited_once_with("req-cov-1")
        assert _card_outcomes(slot) == ["rejected"]

    @pytest.mark.asyncio
    async def test_browser_cli_explicit_trusted_pattern_still_auto_approves(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"playwright-cli*"}
        _set_stream(
            client,
            [
                _permission(
                    title="Running: playwright-cli snapshot",
                    tool_input=json.dumps({"command": "playwright-cli snapshot"}),
                ),
                _complete(),
            ],
        )

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        assert _card_outcomes(slot) == []
        assert _tool_rows(slot) == ["🔧 Running: playwright-cli snapshot"]

    @pytest.mark.asyncio
    async def test_trusted_pattern_auto_approves_a_matching_command(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"ls*"}
        _set_stream(
            client,
            [
                _permission(tool_input=json.dumps({"command": "ls -la"})),
                _complete(),
            ],
        )

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        assert any(
            m.get("role") == "tool" and m.get("content", "").startswith("🔧") for m in slot.messages
        )

    @pytest.mark.asyncio
    async def test_a_base_grant_is_not_applied_to_a_write_redirect(self, tmp_path):
        """``gh *`` vouches for gh, not for the file the shell opens for it."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"gh", "gh *"}
        _set_stream(
            client,
            [
                _permission(
                    title="Running: gh pr view 42 > /tmp/x",
                    tool_input=json.dumps({"command": "gh pr view 42 > /tmp/x"}),
                ),
                _complete(),
            ],
        )
        # The notice is posted only beside a card someone can answer, so the
        # card is answered (rejected) rather than born declined by a zero budget.
        state.push_slots_update.side_effect = lambda: _answer_card(slot, "rejected")
        slot._empty_response_retries = 2
        with patch.object(chat_runner, "sel") as mock_sel:
            audit = MagicMock()
            mock_sel.return_value = audit
            await chat_runner._run_chat(state, slot, "hello")
        await _settle(slot)

        client.approve_tool.assert_not_awaited()
        declined = [
            c.kwargs
            for c in audit.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "auto_approve_declined"
        ]
        assert declined, audit.log_tool_invocation.call_args_list
        assert declined[0]["metadata"] == {
            "reason": "redirect_outside_grant",
            "tier": "trusted_pattern",
        }
        # The redirect check also fires on a quoted ``>`` that writes nothing,
        # so both texts say the command MAY write, not that it does.
        assert (
            declined[0]["error"]
            == "the command may write through a redirect the grant does not cover"
        )
        notices = [
            m.get("content", "")
            for m in slot.messages
            if "Trusted pattern not applied" in m.get("content", "")
        ]
        assert notices, slot.messages
        assert "may write through a redirect, which the grant does not cover" in notices[0]
        assert "writes to a file" not in notices[0]

    @pytest.mark.asyncio
    async def test_a_card_born_declined_gets_no_declined_grant_notice(self, tmp_path):
        """No turn budget left: the card is created rejected, so nobody is asked."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"ls*"}
        _set_stream(
            client,
            [_permission(title="Running: ls -la", tool_input='{"command": "ls -la"}'), _complete()],
        )
        refusal = name_grant.Refusal(name_grant.SHADOWED, "ls resolves to /writable/ls")
        with (
            patch.object(
                chat_runner, "_name_grant_refusal_off_loop", new=AsyncMock(return_value=refusal)
            ) as pattern_check,
            patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        ):
            await _drive(state, slot)

        # The pattern rung really declined; otherwise no notice proves nothing.
        pattern_check.assert_awaited_once()
        client.approve_tool.assert_not_awaited()
        (card,) = [m for m in slot.messages if m.get("role") == "permission"]
        assert json.loads(card["cls"]).get("resolved") == "rejected"
        assert not any("not applied" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_one_shadowed_name_refused_by_two_rungs_is_one_notice(self, tmp_path):
        from kiro_crew.hooks import TOOL_AUTO_APPROVE, ToolHookResult

        context_builder = MagicMock()
        context_builder.hooks.on_tool_call.return_value = ToolHookResult(action=TOOL_AUTO_APPROVE)
        context_builder.build_message.return_value = ("hello", None)
        state, client = _runner_state(tmp_path, context_builder=context_builder)
        slot = _slot()
        slot._trusted_patterns = {"python3*"}
        state.push_slots_update.side_effect = lambda: _answer_card(slot, "rejected")
        _set_stream(
            client,
            [
                _permission(title="Running: python3 -V", tool_input='{"command": "python3 -V"}'),
                _complete(),
            ],
        )
        refusal = name_grant.Refusal(name_grant.SHADOWED, "python3 resolves to /shims/python3")
        with (
            patch.object(
                chat_runner, "_name_grant_refusal_for", new=AsyncMock(return_value=refusal)
            ) as hook_check,
            patch.object(
                chat_runner, "_name_grant_refusal_off_loop", new=AsyncMock(return_value=refusal)
            ) as pattern_check,
        ):
            await _drive(state, slot)

        # Both rungs really declined; otherwise one notice proves nothing.
        hook_check.assert_awaited_once()
        pattern_check.assert_awaited_once()
        shown = [
            m.get("role")
            for m in slot.messages
            if m.get("role") == "permission" or "not applied" in m.get("content", "")
        ]
        assert shown == ["system", "permission"], slot.messages

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grant", ["trust", "yolo"])
    async def test_a_declined_pattern_approved_by_a_later_rung_asks_nothing(self, tmp_path, grant):
        """No "approve explicitly" notice when session trust or YOLO approves anyway."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"ls*"}
        if grant == "trust":
            slot._trust = True
        else:
            state.enable_yolo()
        _set_stream(
            client,
            [_permission(title="Running: ls -la", tool_input='{"command": "ls -la"}'), _complete()],
        )
        refusal = name_grant.Refusal(name_grant.SHADOWED, "ls resolves to /writable/ls")
        with patch.object(
            chat_runner, "_name_grant_refusal_off_loop", new=AsyncMock(return_value=refusal)
        ) as pattern_check:
            await _drive(state, slot)

        # The pattern rung really declined; otherwise no notice proves nothing.
        pattern_check.assert_awaited_once()
        client.approve_tool.assert_awaited_once_with("req-cov-1")
        assert not any("not applied" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_a_base_grant_still_covers_a_discard_redirect(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"gh", "gh *"}
        _set_stream(
            client,
            [
                _permission(
                    title="Running: gh pr view 42 2>/dev/null",
                    tool_input=json.dumps({"command": "gh pr view 42 2>/dev/null"}),
                ),
                _complete(),
            ],
        )

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        assert _card_outcomes(slot) == []
        assert _tool_rows(slot) == ["🔧 Running: gh pr view 42 2>/dev/null"]

    @pytest.mark.asyncio
    async def test_trusted_pattern_rejects_an_invalid_tool_name(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"ls*"}
        _set_stream(
            client,
            [
                _permission(tool_input=json.dumps({"command": "ls -la"})),
                _complete(),
            ],
        )

        with patch.object(
            chat_runner, "_validate_tool_name", side_effect=ValueError("name too long")
        ):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")
        assert any("invalid: name too long" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_unrecognised_tool_input_skips_pattern_matching(self, tmp_path):
        """Deny-by-default: a non-bash tool_input must not reach fnmatch."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"*"}
        slot._trust = True  # falls through to the trust rung instead
        _set_stream(client, [_permission(tool_input="{}"), _complete()])

        with patch.object(chat_runner, "_matches_trusted_pattern") as matcher:
            await _drive(state, slot)

        matcher.assert_not_called()
        client.approve_tool.assert_awaited_once()
        # Session trust approves without a card and without a tool row.
        assert _card_outcomes(slot) == []

    @pytest.mark.asyncio
    async def test_trust_reads_auto_approves_a_read_only_command(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trust_reads = True
        _set_stream(
            client,
            [_permission(tool_input=json.dumps({"command": "cat README.md"})), _complete()],
        )

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        assert _card_outcomes(slot) == []
        assert _tool_rows(slot) == ["🔧 Running: ls -la"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command", ["git status", "git log --oneline -5", "ls && git diff", "/usr/bin/GIT status"]
    )
    async def test_trust_reads_never_auto_approves_git(self, tmp_path, command):
        """git reads ``.git/config`` on every run, and ``core.fsmonitor`` /
        ``core.hooksPath`` name a program there that no text classifier sees, so a
        read-only git command still goes to the card under trust-reads."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trust_reads = True
        _set_stream(
            client,
            [_permission(tool_input=json.dumps({"command": command})), _complete()],
        )
        slot._empty_response_retries = 2
        with (
            patch.object(chat_runner, "sel") as mock_sel,
            patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        ):
            audit = MagicMock()
            mock_sel.return_value = audit
            await chat_runner._run_chat(state, slot, "hello")
        await _settle(slot)

        client.approve_tool.assert_not_awaited()
        # The card does not offer a trust-reads grant that would not cover git.
        cards = [json.loads(m["cls"]) for m in slot.messages if m.get("role") == "permission"]
        assert cards and all(not card.get("is_read_only") for card in cards), cards
        assert not [
            c.kwargs
            for c in audit.log_tool_invocation.call_args_list
            if c.kwargs.get("metadata", {}).get("reason") == "trust_reads"
            and c.kwargs.get("outcome") == "auto_approved"
        ]

    # ── Reads mode and Kiro Crew's own read-only MCP tools ───────────────────

    @staticmethod
    def _real_gate_state(tmp_path):
        """A runner whose context builder carries a REAL ``HookManager``, so the
        permission walks the gate's tiers rather than a scripted verdict."""
        from kiro_crew.hooks import HookManager

        context_builder = MagicMock()
        context_builder.hooks = HookManager()
        context_builder.build_message.return_value = ("hello", None)
        return _runner_state(tmp_path, context_builder=context_builder)

    @staticmethod
    def _core_read(
        server: str = "kirocrew-core", tool: str = "memory_recall", *, trusted: bool = True
    ):
        """A permission request for a first-party MCP read, as kiro-cli raises
        it: the trusted ``_meta.kiro`` pair, no ``kind``, a model-authored title."""
        return _permission(
            title=f"Running: @{server}/{tool}",
            tool_input=json.dumps({"query": "what did we decide"}),
            tool_kind="",
            is_shell=False,
            tool_name=tool,
            mcp_server_name=server,
            mcp_identity_trusted=trusted,
        )

    @pytest.mark.asyncio
    async def test_reads_mode_auto_approves_a_first_party_read_only_mcp_tool(self, tmp_path):
        state, client = self._real_gate_state(tmp_path)
        slot = _slot()
        slot._trust_reads = True
        _set_stream(client, [self._core_read(), _complete()])

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        assert _card_outcomes(slot) == []
        assert not _errors(slot)

    @pytest.mark.asyncio
    async def test_normal_mode_still_prompts_for_the_same_first_party_read(self, tmp_path):
        """The proof is Reads mode's: a Normal slot shows the card as before."""
        state, client = self._real_gate_state(tmp_path)
        slot = _slot()
        assert slot._trust_reads is False
        state.push_slots_update.side_effect = lambda: _answer_card(slot, "rejected")
        _set_stream(client, [self._core_read(), _complete()])

        await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        assert _card_outcomes(slot) == ["rejected"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "server,tool",
        [
            ("evil-server", "memory_recall"),  # same name, another server
            ("kirocrew-core", "learn_add"),  # a core WRITE tool
            ("kirocrew-core", "list_sessions"),  # a core tool kept off the list
        ],
    )
    async def test_reads_mode_prompts_for_every_other_mcp_call(self, tmp_path, server, tool):
        state, client = self._real_gate_state(tmp_path)
        slot = _slot()
        slot._trust_reads = True
        state.push_slots_update.side_effect = lambda: _answer_card(slot, "rejected")
        _set_stream(client, [self._core_read(server, tool), _complete()])

        await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        assert _card_outcomes(slot) == ["rejected"]

    @pytest.mark.asyncio
    async def test_reads_mode_prompts_when_the_identity_is_not_trusted(self, tmp_path):
        """The pair without the provenance flag is prose, not identity: an
        event that merely NAMES the core server and tool prompts."""
        state, client = self._real_gate_state(tmp_path)
        slot = _slot()
        slot._trust_reads = True
        state.push_slots_update.side_effect = lambda: _answer_card(slot, "rejected")
        _set_stream(client, [self._core_read(trusted=False), _complete()])

        await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        assert _card_outcomes(slot) == ["rejected"]

    @pytest.mark.asyncio
    async def test_trust_reads_rejects_an_invalid_tool_name(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trust_reads = True
        _set_stream(
            client,
            [_permission(tool_input=json.dumps({"command": "cat README.md"})), _complete()],
        )

        with patch.object(chat_runner, "_validate_tool_name", side_effect=ValueError("bad name")):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")
        assert any("invalid: bad name" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_live_timed_pattern_auto_approves_a_matching_command(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        # flake-ok: pre-existing real-clock grant deadline 600 s from the turn, moved verbatim; tracked for a de-flake pass
        deadline = GrantDeadline(wall=time.time() + 600, mono=time.monotonic() + 600)
        slot._trusted_pattern_deadlines = {"ls*": deadline}
        _set_stream(
            client,
            [_permission(tool_input=json.dumps({"command": "ls -la"})), _complete()],
        )

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1", deadline=deadline)
        assert set(slot._trusted_pattern_deadlines) == {"ls*"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("timed_pattern", ["git*", "ls*"])
    async def test_chat_long_pattern_approval_carries_no_deadline(self, tmp_path, timed_pattern):
        """The chat-long grant alone covers the call, so it is approved exactly as before."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"ls*"}
        slot._trusted_pattern_deadlines = {
            # flake-ok: pre-existing real-clock grant deadline 600 s from the turn, moved verbatim; tracked for a de-flake pass
            timed_pattern: GrantDeadline(wall=time.time() + 600, mono=time.monotonic() + 600)
        }
        _set_stream(
            client,
            [_permission(tool_input=json.dumps({"command": "ls -la"})), _complete()],
        )

        await _drive(state, slot)

        assert client.approve_tool.await_args_list == [call("req-cov-1")]
        assert _card_outcomes(slot) == []
        assert _tool_rows(slot) == ["🔧 Running: ls -la"]

    @pytest.mark.asyncio
    async def test_timed_grant_ending_before_the_send_prompts_and_sends_nothing(
        self, tmp_path, monkeypatch
    ):
        """Approved while the grant was live, the answer would land after its deadline: ask."""
        from kiro_crew import permission_floor, trust_patterns
        from kiro_crew.acp.client import AcpClient

        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_pattern_deadlines = {
            # flake-ok: pre-existing real-clock grant deadline 600 s from the turn, moved verbatim; tracked for a de-flake pass
            "ls*": GrantDeadline(wall=time.time() + 600, mono=time.monotonic() + 600)
        }
        event = _permission(tool_input=json.dumps({"command": "ls -la"}))
        # The real transport answers the runner, and its floor check lasts an
        # hour on the clocks the deadline check reads: a host suspended mid-check.
        order: list[str] = []
        transport = AcpClient(work_dir=tmp_path)
        transport._send_response = AsyncMock(side_effect=_write_recorder(order))
        transport._permission_gate_events[event.request_id] = event
        client.approve_tool.side_effect = transport.approve_tool

        class _Clock:
            offset = 0.0

            def time(self):
                # flake-ok: pre-existing offset clock over the real one, advanced by hand in the test, moved verbatim; tracked for a de-flake pass
                return time.time() + self.offset

            def monotonic(self):
                # flake-ok: pre-existing offset clock over the real one, advanced by hand in the test, moved verbatim; tracked for a de-flake pass
                return time.monotonic() + self.offset

            def __getattr__(self, name):
                return getattr(time, name)

        clock = _Clock()

        def _hour_long_floor(_event):
            clock.offset += 3600
            return None

        monkeypatch.setattr(permission_floor, "refusal_for", _hour_long_floor)
        monkeypatch.setattr(trust_patterns, "time", clock)

        def _allow_once_when_registered():
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                order.append("prompted")
                future.set_result("approved")

        state.push_slots_update.side_effect = _allow_once_when_registered
        _set_stream(client, [event, _complete()])

        await _drive(state, slot)

        assert order == ["prompted", "sent"]

    @pytest.mark.asyncio
    async def test_expired_timed_pattern_is_dropped_before_matching(self, tmp_path):
        """Fail closed: a grant past its deadline never reaches the matcher."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"git status"}
        # Past on the wall clock only, as after a suspended host wakes.
        slot._trusted_pattern_deadlines = {
            # flake-ok: pre-existing real-clock grant deadline, past on the wall clock only, moved verbatim; tracked for a de-flake pass
            "ls*": GrantDeadline(wall=time.time() - 1, mono=time.monotonic() + 600)
        }
        # Session trust lets the turn finish; the matcher is what is observed.
        slot._trust = True
        _set_stream(
            client,
            [_permission(tool_input=json.dumps({"command": "ls -la"})), _complete()],
        )

        with patch.object(
            chat_runner, "_matches_trusted_pattern", wraps=chat_runner._matches_trusted_pattern
        ) as matcher:
            await _drive(state, slot)

        matcher.assert_called_once()
        assert matcher.call_args.args[1] == {"git status"}
        assert slot._trusted_patterns == {"git status"}
        assert slot._trusted_pattern_deadlines == {}

    @pytest.mark.asyncio
    async def test_only_expired_timed_patterns_skip_matching_entirely(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_pattern_deadlines = {
            # flake-ok: pre-existing real-clock grant deadline, past on the monotonic clock only, moved verbatim; tracked for a de-flake pass
            "ls*": GrantDeadline(wall=time.time() + 600, mono=time.monotonic() - 1)
        }
        slot._trust = True
        _set_stream(
            client,
            [_permission(tool_input=json.dumps({"command": "ls -la"})), _complete()],
        )

        with patch.object(chat_runner, "_matches_trusted_pattern") as matcher:
            await _drive(state, slot)

        matcher.assert_not_called()
        assert slot._trusted_pattern_deadlines == {}

    @pytest.mark.asyncio
    async def test_timed_grant_expiring_during_the_program_check_prompts(
        self, tmp_path, monkeypatch
    ):
        """A grant that ends during the awaited check is not answered: the call prompts."""
        from kiro_crew import permission_floor
        from kiro_crew.acp.client import AcpClient

        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_pattern_deadlines = {
            # flake-ok: pre-existing real-clock grant deadline 600 s from the turn, moved verbatim; tracked for a de-flake pass
            "ls*": GrantDeadline(wall=time.time() + 600, mono=time.monotonic() + 600)
        }
        event = _permission(tool_input=json.dumps({"command": "ls -la"}))
        order: list[str] = []
        transport = AcpClient(work_dir=tmp_path)
        transport._send_response = AsyncMock(side_effect=_write_recorder(order))
        transport._permission_gate_events[event.request_id] = event
        client.approve_tool.side_effect = transport.approve_tool
        monkeypatch.setattr(permission_floor, "refusal_for", lambda _event: None)

        async def _expire_while_checking(_command):
            # The executor hop lasts past the deadline (a slow pool, a host
            # suspended mid-call): the grant is live when matched, gone after.
            slot._trusted_pattern_deadlines["ls*"] = GrantDeadline(wall=0.0, mono=0.0)
            return None

        def _allow_once_when_registered():
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                order.append("prompted")
                future.set_result("approved")

        state.push_slots_update.side_effect = _allow_once_when_registered
        _set_stream(client, [event, _complete()])

        with patch.object(
            chat_runner,
            "_name_grant_refusal_off_loop",
            new=AsyncMock(side_effect=_expire_while_checking),
        ):
            await _drive(state, slot)

        assert order == ["prompted", "sent"]
        assert client.approve_tool.await_args_list[0] == call(
            "req-cov-1", deadline=GrantDeadline(wall=0.0, mono=0.0)
        )

    @staticmethod
    def _redirect_permissions(command: str) -> list:
        """The same write-redirect command asked twice, then the turn's end."""
        return [
            _permission(
                title=f"Running: {command}",
                tool_input=json.dumps({"command": command}),
                request_id=request_id,
            )
            for request_id in ("req-cov-1", "req-cov-2")
        ] + [_complete()]

    @pytest.mark.asyncio
    async def test_timed_exact_grant_covers_its_own_write_redirect(self, tmp_path):
        """A timed exact grant spells the redirect, so every repeat is approved unprompted."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        # flake-ok: pre-existing real-clock grant deadline 600 s from the turn, moved verbatim; tracked for a de-flake pass
        deadline = GrantDeadline(wall=time.time() + 600, mono=time.monotonic() + 600)
        slot._trusted_pattern_deadlines = {exact_trust_pattern("echo hi > out.txt"): deadline}
        _set_stream(client, self._redirect_permissions("echo hi > out.txt"))

        # A prompt would time out at once and reject, so any prompt fails the test.
        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        assert client.approve_tool.await_args_list == [
            call("req-cov-1", deadline=deadline),
            call("req-cov-2", deadline=deadline),
        ]
        client.reject_tool.assert_not_awaited()
        assert not any("Trusted pattern not applied" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_chat_long_exact_grant_covers_its_own_write_redirect(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {exact_trust_pattern("echo hi > out.txt")}
        _set_stream(client, self._redirect_permissions("echo hi > out.txt"))

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        assert client.approve_tool.await_args_list == [call("req-cov-1"), call("req-cov-2")]
        client.reject_tool.assert_not_awaited()
        assert not any("Trusted pattern not applied" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_timed_redirect_grant_under_a_chat_long_base_grant_carries_its_deadline(
        self, tmp_path
    ):
        """The chat-long ``echo *`` matches, but only the timed grant covers the redirect."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = base_trust_patterns("echo")
        # flake-ok: pre-existing real-clock grant deadline 600 s from the turn, moved verbatim; tracked for a de-flake pass
        deadline = GrantDeadline(wall=time.time() + 600, mono=time.monotonic() + 600)
        slot._trusted_pattern_deadlines = {exact_trust_pattern("echo hi > out.txt"): deadline}
        _set_stream(client, self._redirect_permissions("echo hi > out.txt"))

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        assert client.approve_tool.await_args_list == [
            call("req-cov-1", deadline=deadline),
            call("req-cov-2", deadline=deadline),
        ]
        client.reject_tool.assert_not_awaited()
        assert _card_outcomes(slot) == []
        assert _tool_rows(slot) == ["🔧 Running: echo hi > out.txt"] * 2


class TestRunChatApprovalWindow:
    @pytest.mark.asyncio
    async def test_non_shell_card_separates_display_from_collision_free_trust_key(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(
            client,
            [
                _permission(
                    title="Repository action",
                    tool_input="",
                    tool_kind="other",
                    is_shell=False,
                    tool_name="repo__delete",
                    mcp_server_name="github",
                ),
                _complete(),
            ],
        )

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        (card,) = [m for m in slot.messages if m.get("role") == "permission"]
        meta = json.loads(card["cls"])
        # Preserve the ACP-compatible UI label while keeping authority in the
        # injective server-only field.
        assert meta["full_command"] == "mcp__github__repo__delete"
        assert meta["trust_command_key"] == canonical_non_shell_trust_key("github", "repo__delete")
        assert meta["trust_command_key"] != canonical_non_shell_trust_key("github__repo", "delete")
        assert meta["trust_command_grantable"] == "1"
        client.reject_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_redacted_command_still_allows_one_time_approval(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        permission = _permission(tool_input=json.dumps({"command": "echo [REDACTED: credential]"}))
        permission.tool_input_redacted = True
        _set_stream(client, [permission, _complete()])

        def _approve_when_registered():
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _approve_when_registered

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        client.reject_tool.assert_not_awaited()
        assert _card_outcomes(slot) == ["approved"]
        assert _tool_rows(slot) == ["✅ Running: ls -la"]

    @pytest.mark.asyncio
    async def test_redacted_command_is_allow_once_only(self, tmp_path):
        """Different hidden commands collapse to the same display marker.

        The transport provenance therefore suppresses every durable command
        scope while leaving the ordinary approval card live.
        """
        state, client = _runner_state(tmp_path)
        slot = _slot()
        permission = _permission(tool_input=json.dumps({"command": "echo [REDACTED: credential]"}))
        permission.tool_input_redacted = True
        _set_stream(client, [permission, _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        (card,) = [m for m in slot.messages if m.get("role") == "permission"]
        meta = json.loads(card["cls"])
        assert meta["tool_input"] == '{"command": "echo [REDACTED: credential]"}'
        assert "full_command" not in meta
        assert "trust_command_key" not in meta
        assert "base_command" not in meta
        assert "trust_command_grantable" not in meta
        assert "trust_base_grantable" not in meta
        # Redaction hides the bytes a COMMAND grant would name, so those tiers
        # go.  Trusting the session names no bytes, so that tier stays.
        assert meta["trust_grantable"] == "1"
        client.reject_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_no_remaining_budget_declines_without_waiting(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_permission(), _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")
        assert _errors(slot)

    @pytest.mark.asyncio
    async def test_unanswered_prompt_times_out_and_names_the_cause(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_permission(), _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.01):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")
        assert slot._approval_futures == {}

    @pytest.mark.asyncio
    async def test_unattended_slot_is_told_to_ask_instead_of_retrying(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._app = "worker-app"
        slot._human_seen = False
        assert slot.unattended is True
        _set_stream(client, [_permission(), _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.01):
            await _drive(state, slot)

        assert any(
            "running unattended" in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "assistant"
        )
