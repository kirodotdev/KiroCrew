"""A file EDIT's tool_input is a document, and its gate is the target path.

``_dispatch.derive_edit_diff`` renders an edit's new content as a unified diff and
that text is what ``event.tool_input`` carries. Feeding it to the shell-command
scan refused writing a Markdown page that says ``git push origin main`` or a
docstring naming the gateway-restart command (both live ``is_denied`` regex
rules) -- and any body over the command-line size cap, for its length. A body
sentence naming ``~/.ssh`` is not refused either: there is no text-path fence. These tests pin the
replacement: an edit is judged by where it writes (``is_sensitive_write_path`` over
every accepted path spelling), the title tier still runs, and a non-edit tool
keeps the document scan byte for byte.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew import llm_helpers
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

_PROSE = (
    "# Sandbox notes\n\n"
    "The sandbox masks ~/.ssh and ~/.aws for the child process.\n"
    "Run git push origin main only after review.\n"
    "kirocrew restart is blocked by kiro-cli's filter.\n"
)


class _RecordingProvider:
    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


def _diff_for(path: str, body: str) -> str:
    lines = "\n".join(f"+{line}" for line in body.rstrip("\n").split("\n"))
    return f"--- /dev/null\n+++ {path}\n@@ -0,0 +1 @@\n{lines}\n"


def _edit_event(
    path: str,
    body: str = _PROSE,
    *,
    kind: str = "edit",
    params=...,
    trusted: bool = True,
    is_shell: bool = False,
    diff_path: str = "",
) -> LLMEvent:
    """A permission event as the ACP client emits one for a file edit.

    ``trusted`` sets the provenance flags the client derives from the preceding
    tool_call frame (``shell_classified`` + ``raw_params_trusted``); a frame that
    missed both caches carries neither. ``diff_path`` is the path the tool_call's
    diff content block named, cached by the client onto the permission event.
    """
    raw = {"command": "create", "path": path, "fileText": body} if params is ... else params
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Editing notes.md",
        request_id="r1",
        tool_kind=kind,
        tool_input=_diff_for(path, body),
        raw_tool_params=raw,
        raw_params_trusted=trusted and raw is not None,
        shell_classified=trusted,
        is_shell=is_shell,
        diff_path=diff_path,
    )


async def _resolve(event: LLMEvent) -> tuple[bool, _RecordingProvider, list[dict]]:
    provider = _RecordingProvider()
    rows: list[dict] = []
    sel_stub = MagicMock()
    sel_stub.log_tool_invocation.side_effect = lambda **kw: rows.append(kw)
    with patch.object(sel_mod, "sel", lambda: sel_stub):
        approved = await _resolve_permission(
            provider,  # type: ignore[arg-type]
            event,
            ToolApprovalPolicy.AUTO_APPROVE,
            None,
        )
    return approved, provider, rows


def _error(rows: list[dict]) -> str:
    assert len(rows) == 1, rows
    return str(rows[0].get("error") or "")


class TestEditContentIsNotACommandLine:
    @pytest.mark.asyncio
    async def test_prose_naming_fenced_paths_and_denied_commands_is_writable(self) -> None:
        # The document scan refused every one of these three sentences on its
        # own; the edit gate judges the target instead and this path is benign.
        approved, provider, rows = await _resolve(_edit_event("/tmp/proj/docs/notes.md"))
        assert approved is True, _error(rows)
        assert provider.approved == ["r1"]

    @pytest.mark.asyncio
    async def test_document_scan_is_never_run_on_an_edit(self) -> None:
        with patch.object(
            llm_helpers, "_first_tool_input_denial", side_effect=AssertionError("scanned")
        ):
            approved, _provider, _rows = await _resolve(_edit_event("/tmp/proj/a.py"))
        assert approved is True

    @pytest.mark.asyncio
    async def test_a_body_over_the_command_cap_is_not_refused_for_its_length(self) -> None:
        body = "x = 1\n" * (llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS // 4)
        assert len(body) > llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS
        approved, _provider, rows = await _resolve(_edit_event("/tmp/proj/big.py", body))
        assert approved is True, _error(rows)

    @pytest.mark.asyncio
    async def test_a_python_source_naming_a_denied_command_is_still_writable(self) -> None:
        # The document is not the fence; the sandbox and the cron vetter are.
        # An agent may WRITE a script whose text spells a denied command; RUNNING
        # it goes through the shell gate on its own title.
        body = 'subprocess.run(["git", "push", "origin", "main"])\n'
        approved, _provider, rows = await _resolve(_edit_event("/tmp/proj/x.py", body))
        assert approved is True, _error(rows)


class TestEditTargetIsTheGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "~/.ssh/authorized_keys",
            "~/.aws/credentials",
            "~/.kiro/crew/security_policy.json",
            "~/.kiro/crew/config.json",  # write-only tier
            "~/.kiro/agents/pwn.json",  # write-only tier
        ],
    )
    async def test_write_to_a_protected_path_is_denied(self, path: str) -> None:
        approved, provider, rows = await _resolve(_edit_event(path, "harmless\n"))
        assert approved is False
        assert provider.rejected == ["r1"]
        assert _error(rows).startswith("Blocked: write to protected path: ")
        assert rows[0]["metadata"]["mechanism"] == "always_deny_input"

    @pytest.mark.asyncio
    async def test_every_path_spelling_is_judged(self) -> None:
        # filePath alias, nested under a batch key -- target_paths walks them all.
        params = {"operations": [{"filePath": "~/.ssh/id_rsa", "text": "x"}]}
        approved, _p, rows = await _resolve(_edit_event("/tmp/ok", params=params))
        assert approved is False
        assert "id_rsa" in _error(rows)

    @pytest.mark.asyncio
    async def test_truncated_walk_is_denied_as_unverifiable(self) -> None:
        params = {"path": "/tmp/ok", "deep": [{"path": f"/tmp/f{i}"} for i in range(300)]}
        approved, _p, rows = await _resolve(_edit_event("/tmp/ok", params=params))
        assert approved is False
        assert "too large to verify" in _error(rows)

    @pytest.mark.asyncio
    async def test_title_tier_still_runs_first(self) -> None:
        # The title is a shell command the env-credential tier refuses. A credential
        # PATH in a title is not the producer (there is no text-path fence); an
        # env-credential read is.
        ev = _edit_event("/tmp/proj/a.md")
        ev.title = "env | grep AWS_SECRET_ACCESS_KEY"
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert rows[0]["metadata"]["mechanism"] == "always_deny"


class TestTheContentBlockPathIsATarget:
    """The params may carry no path key at all; the diff content block still
    names the file. The target set is the UNION, and an empty union is denied."""

    @pytest.mark.asyncio
    async def test_protected_path_named_only_by_the_diff_block_is_denied(self) -> None:
        # Trusted params without any accepted path spelling; the target lives in
        # the ``{"type": "diff", "path": ...}`` block the client cached.
        params = {"command": "create", "fileText": "harmless\n"}
        ev = _edit_event(
            "~/.kiro/agents/pwn.json",
            "harmless\n",
            params=params,
            diff_path="~/.kiro/agents/pwn.json",
        )
        approved, provider, rows = await _resolve(ev)
        assert approved is False
        assert provider.rejected == ["r1"]
        assert _error(rows) == "Blocked: write to protected path: ~/.kiro/agents/pwn.json"

    @pytest.mark.asyncio
    async def test_safe_path_named_only_by_the_diff_block_is_approved(self) -> None:
        params = {"command": "create", "fileText": _PROSE}
        ev = _edit_event("/tmp/proj/notes.md", params=params, diff_path="/tmp/proj/notes.md")
        approved, provider, rows = await _resolve(ev)
        assert approved is True
        assert provider.approved == ["r1"]

    @pytest.mark.asyncio
    async def test_both_sources_are_judged(self) -> None:
        # A safe params path does not launder a protected diff-block path, and
        # vice versa: any hit in the union denies.
        ev = _edit_event("/tmp/proj/ok.md", "x\n", diff_path="~/.ssh/authorized_keys")
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "authorized_keys" in _error(rows)
        ev = _edit_event("~/.aws/credentials", "x\n", diff_path="/tmp/proj/ok.md")
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "credentials" in _error(rows)

    @pytest.mark.asyncio
    async def test_trusted_params_naming_no_target_are_denied(self) -> None:
        # Trusted provenance, but neither the params nor a content block names a
        # file: nothing to judge, so deny -- never approve blind, and never fall
        # back to reading the document as a command line.
        ev = _edit_event("/tmp/proj/a.md", "harmless\n", params={"command": "create"})
        approved, provider, rows = await _resolve(ev)
        assert approved is False
        assert provider.rejected == ["r1"]
        assert _error(rows) == (
            "Blocked: file edit names no target path to verify (deny-by-default)"
        )
        assert rows[0]["metadata"]["mechanism"] == "always_deny_input"

    def test_edit_target_denial_unit(self) -> None:
        deny = llm_helpers._edit_target_denial
        assert deny({"command": "create"}) is not None
        assert deny({"command": "create"}, "")[1].startswith("Blocked: file edit names no")
        assert deny({"command": "create"}, "/tmp/ok.md") is None
        hit = deny({"command": "create"}, "~/.kiro/crew/config.json")
        assert hit is not None and hit[2] == "~/.kiro/crew/config.json"
        # The same path under both sources is one candidate, judged once.
        assert deny({"path": "/tmp/ok.md"}, "/tmp/ok.md") is None


class TestTheClientCarriesTheDiffPathOntoThePermissionEvent:
    """``_dispatch`` caches the diff block's path by scoped toolCallId and
    ``build_permission_event`` reads it, exactly like the sibling caches."""

    def _caches(self) -> dict:
        return dict(
            tool_input_cache={},
            shell_cache={},
            raw_params_cache={},
            diff_path_cache={},
            cache_scope="sess-1",
        )

    def _permission(self, caches: dict, tool_call_id: str = "tc-1"):
        from kiro_crew.acp import _dispatch
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id="req-1",
            method="session/request_permission",
            params={
                "sessionId": "sess-1",
                "toolCall": {"toolCallId": tool_call_id, "title": "Editing", "kind": "edit"},
                "options": [{"optionId": "allow_once", "kind": "allow_once"}],
            },
        )
        event, _ = _dispatch.build_permission_event(msg, **caches)
        return event

    def test_tool_call_diff_block_path_reaches_the_permission_event(self) -> None:
        from kiro_crew.acp import _dispatch

        caches = self._caches()
        _dispatch.parse_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc-1",
                "title": "Editing",
                "kind": "edit",
                "rawInput": {"command": "create", "fileText": "x"},
                "content": [
                    {
                        "type": "diff",
                        "path": "~/.kiro/agents/pwn.json",
                        "oldText": None,
                        "newText": "x",
                    }
                ],
            },
            **caches,
        )
        assert caches["diff_path_cache"] == {"sess-1|tc-1": "~/.kiro/agents/pwn.json"}
        ev = self._permission(caches)
        assert ev.diff_path == "~/.kiro/agents/pwn.json"
        assert ev.raw_params_trusted is True and ev.shell_classified is True
        assert llm_helpers._edit_target_denial(ev.raw_tool_params, ev.diff_path) is not None

    def test_refinement_diff_block_path_reaches_the_permission_event(self) -> None:
        # claude-agent-acp: the initial tool_call has no content; the refinement
        # carries the diff block.
        from kiro_crew.acp import _dispatch

        caches = self._caches()
        _dispatch.parse_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc-1",
                "title": "Editing",
                "kind": "edit",
            },
            **caches,
        )
        assert caches["diff_path_cache"] == {}
        _dispatch.parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-1",
                "rawInput": {"command": "create", "fileText": "x"},
                "content": [
                    {"type": "diff", "path": "/tmp/proj/a.md", "oldText": "", "newText": "x"}
                ],
            },
            **caches,
        )
        assert self._permission(caches).diff_path == "/tmp/proj/a.md"

    def test_cache_miss_leaves_diff_path_empty(self) -> None:
        caches = self._caches()
        assert self._permission(caches).diff_path == ""
        # A different origin scope cannot read another session's entry.
        caches["diff_path_cache"]["other|tc-1"] = "/tmp/x"
        assert self._permission(caches).diff_path == ""

    def test_missing_cache_is_tolerated(self) -> None:
        caches = self._caches()
        caches.pop("diff_path_cache")
        assert self._permission(caches).diff_path == ""


class TestProvenanceComesFromTheClientNotThePayload:
    """The flags the reroute keys on are what ``acp._dispatch`` derives from the
    tool_call frame that PRECEDES the permission frame, never from the permission
    payload's own ``kind``. Run both frames through the real builders."""

    @staticmethod
    def _frames(tool_call_kind: str, perm_kind: str, raw: dict):
        from kiro_crew.acp import _dispatch
        from kiro_crew.acp.types import JsonRpcMessage

        shell_cache: dict = {}
        raw_cache: dict = {}
        input_cache: dict = {}
        _dispatch._build_tool_call_event(
            {"toolCallId": "t1", "title": "x", "kind": tool_call_kind, "rawInput": raw},
            input_cache,
            shell_cache=shell_cache,
            raw_params_cache=raw_cache,
        )
        msg = JsonRpcMessage(
            id="p1",
            method="session/request_permission",
            params={
                "toolCall": {"toolCallId": "t1", "title": "x", "kind": perm_kind},
                "options": [{"optionId": "allow_once", "kind": "allow_once"}],
            },
        )
        ev, _ = _dispatch.build_permission_event(
            msg,
            tool_input_cache=input_cache,
            shell_cache=shell_cache,
            raw_params_cache=raw_cache,
        )
        return ev

    def test_a_genuine_edit_carries_the_trusted_flags(self) -> None:
        raw = {"command": "create", "path": "/tmp/proj/a.md", "fileText": _PROSE}
        ev = self._frames("edit", "edit", raw)
        assert ev.tool_kind == "edit"
        assert ev.shell_classified is True
        assert ev.is_shell is False
        assert ev.raw_params_trusted is True
        assert ev.raw_tool_params == raw

    def test_a_shell_call_claiming_edit_keeps_is_shell_from_the_cache(self) -> None:
        raw = {"command": "env | grep AWS_SECRET_ACCESS_KEY"}
        ev = self._frames("execute", "edit", raw)
        assert ev.tool_kind == "edit"  # the forged payload word
        assert ev.shell_classified is True
        assert ev.is_shell is True  # what the client established


class TestOnlyEditKindWithParamsIsRerouted:
    @pytest.mark.asyncio
    async def test_non_edit_kind_keeps_the_document_scan(self) -> None:
        # Same prose, non-edit kind: the document scan still runs and the
        # ``git push origin main`` sentence trips the git-publish regex rule.
        ev = _edit_event("/tmp/proj/a.md", kind="read")
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "git-publish-push-protected-branch-name" in _error(rows)

    @pytest.mark.asyncio
    async def test_edit_without_params_falls_back_to_the_document_scan(self) -> None:
        # No params at all (raw_params_trusted cannot be earned) -> the reroute
        # is not taken and the document scan stays as the fail-closed path.
        ev = _edit_event("/tmp/proj/a.md", params=None)
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "git-publish-push-protected-branch-name" in _error(rows)

    @pytest.mark.asyncio
    async def test_forged_edit_kind_on_a_shell_call_keeps_the_command_scan(self) -> None:
        # The payload's ``kind`` is agent-influenced. A shell call that claims
        # ``kind="edit"`` still carries the client's cached is_shell=True, so the
        # reroute is refused and the command scan runs on the input.
        ev = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Bash",
            request_id="r1",
            tool_kind="edit",
            tool_input=json.dumps({"command": "env | grep AWS_SECRET_ACCESS_KEY"}),
            raw_tool_params={
                "path": "/tmp/harmless.md",
                "command": "env | grep AWS_SECRET_ACCESS_KEY",
            },
            raw_params_trusted=True,
            shell_classified=True,
            is_shell=True,
        )
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "AWS credentials from environment" in _error(rows)

    @pytest.mark.asyncio
    async def test_unclassified_edit_keeps_the_document_scan(self) -> None:
        # No shell-cache hit for this toolCallId: the kind cannot be trusted, so
        # the document scan stays even though the payload says edit.
        ev = _edit_event("/tmp/proj/a.md", trusted=False)
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "git-publish-push-protected-branch-name" in _error(rows)

    @pytest.mark.asyncio
    async def test_inline_params_without_cache_provenance_keep_the_document_scan(self) -> None:
        # shell_classified but the params came from the inline fallback: a target
        # the agent authored is not a target to judge by.
        ev = _edit_event("/tmp/proj/a.md")
        ev.raw_params_trusted = False
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "git-publish-push-protected-branch-name" in _error(rows)

    @pytest.mark.asyncio
    async def test_bash_tool_input_is_unchanged(self) -> None:
        ev = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Bash",
            request_id="r1",
            tool_kind="execute",
            tool_input=json.dumps({"command": "env | grep AWS_SECRET_ACCESS_KEY"}),
            raw_tool_params={"command": "env | grep AWS_SECRET_ACCESS_KEY"},
        )
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "AWS credentials from environment" in _error(rows)
