"""The per-call edit row's diff for kiro-cli's v3 replace tool, whose arguments name no command.

The v3 engine sends ``str_replace`` as ``{path, oldStr, newStr, replace_all}``. Its
first ``tool_call`` frame carries ``kind: "edit"``; the ``tool_call_update`` frames
that re-send the arguments before the permission request carry no kind, and one
of them holds an empty placeholder diff block. The frames below copy the shapes a
live kiro-cli 2.35.1 ``--agent-engine v3`` session wrote (only the path and ids
are changed); ``test_file_change_snapshots_kiro_v3.py`` replays the same frames
for the end-of-turn card.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

import kiro_crew.sel as sel_mod
from kiro_crew.acp._dispatch_parts.session_update_parser import parse_session_update
from kiro_crew.acp._dispatch_parts.surfaced_text import DIFF_TRUNCATION_MARK, derive_edit_diff
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE, JsonRpcMessage
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
from kiro_crew.llm_helpers_parts import tool_input_denials
from kiro_crew.llm_helpers_parts.tool_input_denials import (
    _MAX_SCANNABLE_TOOL_INPUT_CHARS,
    _extract_tool_input_strings,
    _first_tool_input_denial,
)
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

PATH = "/work/a.txt"
ARGS = {"path": PATH, "oldStr": "two", "newStr": "TWO", "replace_all": False}
REPLACE_DIFF = f"--- {PATH}\n+++ {PATH}\n@@ -1 +1 @@\n-two\n+TWO"


def _pending_frames() -> list[dict[str, Any]]:
    """What arrives for one replace before its permission request."""
    return [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-replace",
            "title": "Replace in File",
            "kind": "edit",
            "status": "pending",
            "rawInput": {"path": PATH, "oldStr": "two", "newStr": "TWO"},
            "locations": [{"path": PATH}],
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-replace",
            "rawInput": ARGS,
            "content": [{"type": "diff", "path": "", "oldText": "", "newText": ""}],
        },
        {"sessionUpdate": "tool_call_update", "toolCallId": "call-replace", "rawInput": ARGS},
    ]


class TestDeriveEditDiff:
    def test_a_commandless_replace_pair_renders_as_a_replace_hunk(self):
        assert derive_edit_diff(ARGS) == REPLACE_DIFF

    def test_it_matches_the_strreplace_command_shape(self):
        assert derive_edit_diff(ARGS) == derive_edit_diff({**ARGS, "command": "strReplace"})

    def test_a_commandless_write_or_append_derives_nothing(self):
        # {path, text} is a whole-file write or an append; the hunk would be a guess.
        assert derive_edit_diff({"path": PATH, "text": "four"}) == ""

    def test_a_non_string_half_derives_nothing(self):
        assert derive_edit_diff({"path": PATH, "oldStr": "two", "newStr": 3}) == ""

    def test_another_command_is_not_read_as_a_replace(self):
        assert derive_edit_diff({**ARGS, "command": "view"}) == ""

    def test_a_replace_all_derives_nothing(self):
        # One hunk would show a single occurrence of an approval for every one.
        assert derive_edit_diff({**ARGS, "replace_all": True}) == ""


class TestRedactionOfADerivedDiff:
    # A fake PEM key body; only the block rule, which needs its markers, matches it.
    LINES = [f"MIIE{n}" + "q" * 56 for n in range(8)]

    # Assembled at run time so the source holds no key marker for a scanner to flag.
    KIND = " ".join(("RSA", "PRIVATE", "KEY"))

    def _key(self, lines: list[str]) -> str:
        dashes = "-" * 5
        return (
            f"{dashes}BEGIN {self.KIND}{dashes}\n"
            + "\n".join(lines)
            + f"\n{dashes}END {self.KIND}{dashes}\n"
        )

    def _args(self) -> dict[str, Any]:
        edited = list(self.LINES)
        edited[4] = "MIIEX" + "z" * 55
        return {"path": PATH, "oldStr": self._key(self.LINES), "newStr": self._key(edited)}

    def test_an_interior_key_line_is_redacted_from_the_diff(self):
        diff = derive_edit_diff(self._args())
        for line in (*self.LINES, "MIIEX" + "z" * 55):
            assert line not in diff

    def test_the_row_is_marked_redacted(self):
        frames = _pending_frames()
        frames[-1] = {**frames[-1], "rawInput": self._args()}
        events = [e for f in frames for e in parse_session_update(f, tool_input_cache={})]
        assert events[-1].tool_input_redacted is True
        client = AcpClient()
        TestAcpClientPath()._replay(client, frames)
        assert client._tool_call_input_redacted["call-replace"] is True


class TestDocumentScanOfARenderedDiff:
    # The approval path's document scan reads the rendered input. The JSON
    # arguments handed it the target path as its own string; the diff must too.
    @pytest.mark.parametrize("target", ["~/.ssh/config", "~/.aws/credentials"])
    def test_a_sensitive_target_is_refused_from_the_diff_as_from_the_json(self, target):
        args = {"path": target, "oldStr": "a", "newStr": "b"}
        as_json = _first_tool_input_denial(
            _extract_tool_input_strings(json.dumps(args, indent=2)), None
        )
        as_diff = _first_tool_input_denial(
            _extract_tool_input_strings(derive_edit_diff(args)), None
        )
        assert as_json is not None and as_json[0] == "path"
        assert as_diff is not None and as_diff[0] == "path"

    def test_the_diff_adds_path_hunk_old_and_new_after_itself(self):
        assert _extract_tool_input_strings(REPLACE_DIFF) == [
            REPLACE_DIFF,
            PATH,
            "@@ -1 +1 @@",
            "two",
            "TWO",
        ]

    @pytest.mark.parametrize("field", ["oldStr", "newStr"])
    def test_a_sensitive_path_opening_a_side_is_refused(self, field):
        args = {"path": PATH, "oldStr": "a", "newStr": "b", field: "~/.ssh/id_rsa\nc"}
        denial = _first_tool_input_denial(_extract_tool_input_strings(derive_edit_diff(args)), None)
        assert denial is not None and denial[0] == "path"

    def _oversize_replace(self) -> str:
        # Each argument is under the scan ceiling, as it was in the JSON; the
        # whole diff is not.
        lines = _MAX_SCANNABLE_TOOL_INPUT_CHARS // 8
        args = {"path": PATH, "oldStr": "old\n" * lines, "newStr": "new\n" * lines}
        diff = derive_edit_diff(args)
        assert len(diff) > _MAX_SCANNABLE_TOOL_INPUT_CHARS
        return diff

    def test_a_classified_edit_whose_halves_each_fit_is_not_refused_for_size(self):
        strings = _extract_tool_input_strings(self._oversize_replace(), split_oversize_diff=True)
        assert _first_tool_input_denial(strings, None) is None

    def test_an_unclassified_call_keeps_the_oversize_refusal(self):
        denial = _first_tool_input_denial(
            _extract_tool_input_strings(self._oversize_replace()), None
        )
        assert denial is not None and denial[0] == "oversize"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("classified", "approved"), [(True, True), (False, False)])
    async def test_the_approval_path_splits_only_a_classified_call(self, classified, approved):
        provider = MagicMock()
        provider.approve_tool = AsyncMock()
        provider.reject_tool = AsyncMock()
        event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Replace in File",
            request_id="r1",
            tool_input=self._oversize_replace(),
            is_shell=False,
            shell_classified=classified,
        )
        with patch.object(sel_mod, "sel", lambda: MagicMock()):
            result = await _resolve_permission(
                provider, event, ToolApprovalPolicy.AUTO_APPROVE, None
            )
        assert result is approved

    def test_a_cut_diff_stays_one_candidate_and_is_refused(self):
        lines = 70_000 // 4
        diff = derive_edit_diff({"path": PATH, "oldStr": "o\n" * lines, "newStr": "n\n" * lines})
        assert diff.endswith("\n" + DIFF_TRUNCATION_MARK)
        assert getattr(tool_input_denials, "_DIFF_TRUNCATION_MARK", None) == DIFF_TRUNCATION_MARK
        strings = _extract_tool_input_strings(diff, split_oversize_diff=True)
        assert strings == [diff]
        denial = _first_tool_input_denial(strings, None)
        assert denial is not None and denial[0] == "oversize"

    def test_a_crafted_diff_shape_keeps_its_raw_string(self):
        # Text that only looks like a diff may be what runs: the raw string,
        # line continuation included, is still judged whole.
        text = "--- a\n+++ a\nrm \\\n-rf ~"
        strings = _extract_tool_input_strings(text)
        assert strings[0] == text
        assert _first_tool_input_denial(strings, None) is not None

    def test_a_line_of_another_shape_reaches_both_sides(self):
        text = "--- a\n+++ a\n@@ -1 +1 @@\nrm -rf /\n-x\n+y"
        assert _extract_tool_input_strings(text)[1:] == [
            "a",
            "@@ -1 +1 @@",
            "rm -rf /\nx",
            "rm -rf /\ny",
        ]

    def test_sides_never_join_across_a_hunk_gap(self):
        # Lines 4 and 12 are far apart in the file; one side joining two hunks
        # would put them next to each other as a command neither argument holds.
        doc = [f"line {n}" for n in range(1, 16)]
        doc[3], doc[11] = "rm \\", "-rf /"
        before = "\n".join(doc) + "\n"
        after = before.replace("line 1\n", "LINE 1\n", 1).replace("line 15\n", "LINE 15\n")
        diff = derive_edit_diff({"path": PATH, "oldStr": before, "newStr": after})
        assert diff.count("\n@@ ") == 2
        strings = _extract_tool_input_strings(diff, split_oversize_diff=True)
        assert _first_tool_input_denial(strings[1:], None) is None

    def test_text_that_is_not_a_diff_stays_one_candidate(self):
        assert _extract_tool_input_strings("--- not a diff") == ["--- not a diff"]


class TestSessionHandlePath:
    # kiro-cli's session handle passes no kind cache (only KAS gets one), so
    # the replay does not either.
    def _replay(self, frames: list[dict[str, Any]]) -> tuple[list[str], dict[str, str]]:
        inputs: dict[str, str] = {}
        rendered = []
        for frame in frames:
            for event in parse_session_update(frame, tool_input_cache=inputs):
                if event.kind in (EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE):
                    rendered.append(event.tool_input)
        return rendered, inputs

    def test_every_pending_frame_renders_the_diff(self):
        rendered, inputs = self._replay(_pending_frames())
        assert rendered == [REPLACE_DIFF] * 3
        # The cached input is what the permission request's row shows.
        assert list(inputs.values()) == [REPLACE_DIFF]

    def test_the_completed_block_still_wins(self):
        completed = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-replace",
            "status": "completed",
            "rawInput": ARGS,
            "content": [
                {
                    "type": "diff",
                    "path": "file://" + PATH,
                    "oldText": "one\ntwo\nthree\n",
                    "newText": "one\nTWO\nthree\n",
                }
            ],
        }
        rendered, _ = self._replay([*_pending_frames(), completed])
        assert rendered[-1].endswith("@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three")

    def test_a_replace_all_update_shows_its_arguments(self):
        frames = _pending_frames()
        frames[-1] = {**frames[-1], "rawInput": {**ARGS, "replace_all": True}}
        rendered, inputs = self._replay(frames)
        assert json.loads(rendered[-1])["replace_all"] is True
        assert json.loads(next(iter(inputs.values())))["replace_all"] is True

    def test_a_kindless_update_of_a_non_edit_call_keeps_its_arguments(self):
        call = {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-x",
            "title": "Some tool",
            "kind": "other",
            "rawInput": {"path": PATH},
        }
        update = {"sessionUpdate": "tool_call_update", "toolCallId": "call-x", "rawInput": ARGS}
        rendered, _ = self._replay([call, update])
        assert json.loads(rendered[-1]) == ARGS


class TestAcpClientPath:
    def _replay(self, client: AcpClient, frames: list[dict[str, Any]]) -> list[str]:
        rendered = []
        for frame in frames:
            msg = JsonRpcMessage(params={"update": frame})
            if frame["sessionUpdate"] == "tool_call":
                event = client._extract_tool_event(msg)
            else:
                event = client._extract_tool_call_refinement(msg)
            assert event is not None
            rendered.append(event.tool_input)
        return rendered

    def test_every_pending_frame_renders_the_diff(self):
        client = AcpClient()
        assert self._replay(client, _pending_frames()) == [REPLACE_DIFF] * 3
        assert client._tool_call_inputs["call-replace"] == REPLACE_DIFF

    def test_a_kindless_update_of_a_non_edit_call_keeps_its_arguments(self):
        client = AcpClient()
        call = {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-x",
            "title": "Some tool",
            "kind": "other",
            "rawInput": {"path": PATH},
        }
        update = {"sessionUpdate": "tool_call_update", "toolCallId": "call-x", "rawInput": ARGS}
        assert json.loads(self._replay(client, [call, update])[-1]) == ARGS
