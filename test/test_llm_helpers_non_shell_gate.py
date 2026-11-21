"""A non-shell tool's document body is not a shell command line.

Handing EVERY string in ``tool_input`` to the three deny predicates for every
tool refuses a file write whose body merely quotes ``rm -rf /`` or names a
credential path although nothing in it executes. The edit gate (``test_llm_helpers_edit_gate.py``) already reroutes a
trusted ``edit``-kind call to its target path; these tests pin the scoping for
every OTHER non-shell tool with client-established provenance: the strings under
``platform.tool_paths.DOCUMENT_BODY_KEYS`` are skipped, every other string
(``command``, a path, a URL) still reaches the scan, a shell tool keeps the full
scan, and an unknown or untrusted-provenance tool keeps the full scan (fail
closed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew import llm_helpers, security
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
from kiro_crew.platform import tool_paths
from kiro_crew.platform.tool_paths import (
    DOCUMENT_BODY_KEYS,
    DOCUMENT_WRITING_TOOLS,
    command_shaped_strings,
)
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

# Prose that the full document scan refuses: the first sentence trips the
# ``rm -rf /`` deny glob, the second names a credential path.
_QUOTED_COMMAND = "rm -rf" + " /"
_PROSE = (
    "# Runbook\n\n"
    f"Never run `{_QUOTED_COMMAND}` on a shared host.\n"
    "The AWS profile lives in ~/.aws/credentials; do not read it from a script.\n"
)


class _RecordingProvider:
    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


def _event(
    params: dict | None,
    *,
    kind: str = "other",
    title: str = "Writing runbook.md",
    is_shell: bool = False,
    trusted: bool = True,
    identity: bool = True,
    mcp_server_name: str = "",
    tool_name: str = "fs_write",
) -> LLMEvent:
    """A permission event as the ACP client emits it after a tool_call frame.

    ``trusted`` sets the provenance the client derives from the preceding
    tool_call frame (``shell_classified`` + ``raw_params_trusted``); ``identity``
    sets ``mcp_identity_trusted`` (the tool_name / server caches hit). The
    ``tool_input`` is the rendered params, exactly what the document scan reads.
    """
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=title,
        request_id="r1",
        tool_kind=kind,
        tool_input=json.dumps(params, indent=2) if params is not None else "",
        raw_tool_params=params,
        raw_params_trusted=trusted and params is not None,
        shell_classified=trusted,
        is_shell=is_shell,
        mcp_identity_trusted=identity,
        mcp_server_name=mcp_server_name,
        tool_name=tool_name,
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


class TestABodyIsNotACommandLine:
    @pytest.mark.asyncio
    async def test_a_file_write_quoting_a_command_and_a_credential_path_is_allowed(
        self,
    ) -> None:
        params = {"command": "create", "path": "/tmp/proj/runbook.md", "content": _PROSE}
        approved, provider, rows = await _resolve(_event(params))
        assert approved is True, _error(rows)
        assert provider.approved == ["r1"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", sorted(DOCUMENT_BODY_KEYS))
    async def test_every_body_key_is_skipped(self, key: str) -> None:
        approved, _p, rows = await _resolve(_event({"path": "/tmp/proj/a.md", key: _PROSE}))
        assert approved is True, _error(rows)

    @pytest.mark.asyncio
    async def test_a_trusted_mcp_tool_body_is_still_scanned(self) -> None:
        # A resolved MCP identity is provenance, but not a document writer: the
        # server can execute whatever it calls ``content``, so every field of
        # an MCP tool keeps the full scan and a quoted command is refused.
        params = {"name": "runbook", "content": _PROSE}
        ev = _event(
            params,
            title="artifact_save",
            mcp_server_name="kirocrew-core",
            tool_name="artifact_save",
        )
        approved, _p, _rows = await _resolve(ev)
        assert approved is False

    @pytest.mark.asyncio
    async def test_an_mcp_tool_named_like_a_builtin_writer_is_still_scanned(self) -> None:
        params = {"path": "/tmp/proj/a.md", "content": _PROSE}
        ev = _event(params, mcp_server_name="some-server", tool_name="fs_write")
        approved, _p, _rows = await _resolve(ev)
        assert approved is False

    @pytest.mark.asyncio
    async def test_a_builtin_that_is_not_a_document_writer_is_still_scanned(self) -> None:
        params = {"path": "/tmp/proj/a.md", "content": _PROSE}
        ev = _event(params, tool_name="use_aws")
        approved, _p, _rows = await _resolve(ev)
        assert approved is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(DOCUMENT_WRITING_TOOLS))
    async def test_every_builtin_document_writer_skips_the_body(self, name: str) -> None:
        params = {"path": "/tmp/proj/a.md", "content": _PROSE}
        approved, _p, rows = await _resolve(_event(params, tool_name=name))
        assert approved is True, _error(rows)

    @pytest.mark.asyncio
    async def test_a_body_over_the_command_cap_is_not_refused_for_its_length(self) -> None:
        body = "x = 1\n" * (llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS // 4)
        assert len(body) > llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS
        params = {"path": "/tmp/proj/big.py", "content": body}
        approved, _p, rows = await _resolve(_event(params))
        assert approved is True, _error(rows)

    @pytest.mark.asyncio
    async def test_the_document_scan_is_not_run_on_the_body(self) -> None:
        seen: list[list[str]] = []
        real = llm_helpers._first_tool_input_denial

        def _spy(strings, denied, **kwargs):
            seen.append(list(strings))
            return real(strings, denied, **kwargs)

        params = {"command": "create", "path": "/tmp/proj/a.md", "content": _PROSE}
        with patch.object(llm_helpers, "_first_tool_input_denial", side_effect=_spy):
            approved, _p, _r = await _resolve(_event(params))
        assert approved is True
        assert seen == [["create", "/tmp/proj/a.md"]]


class TestEveryOtherStringStillReachesTheScan:
    @pytest.mark.asyncio
    async def test_the_same_text_as_a_shell_command_is_denied(self) -> None:
        params = {"command": _QUOTED_COMMAND}
        ev = _event(params, kind="execute", title="Bash", is_shell=True, tool_name="execute_bash")
        approved, provider, rows = await _resolve(ev)
        assert approved is False
        assert provider.rejected == ["r1"]
        assert "rm -rf" in _error(rows)
        assert rows[0]["metadata"]["mechanism"] == "always_deny_input"

    @pytest.mark.asyncio
    async def test_a_shell_tool_keeps_the_full_scan_over_a_body_key(self) -> None:
        # For a shell tool the body keys are not exempt: is_shell wins.
        params = {"command": "echo ok", "content": _QUOTED_COMMAND}
        ev = _event(params, kind="execute", title="Bash", is_shell=True, tool_name="execute_bash")
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "rm -rf" in _error(rows)

    @pytest.mark.asyncio
    async def test_a_command_field_on_a_non_shell_tool_is_still_scanned(self) -> None:
        # ``command`` is not a body key: a non-shell tool carrying the denied
        # text there is refused exactly as before.
        params = {"command": _QUOTED_COMMAND, "path": "/tmp/proj/a.md", "content": "ok\n"}
        approved, _p, rows = await _resolve(_event(params))
        assert approved is False
        assert "rm -rf" in _error(rows)

    @pytest.mark.asyncio
    async def test_a_credential_path_field_on_a_non_shell_tool_is_still_denied(self) -> None:
        params = {"command": "create", "path": "~/.aws/credentials", "content": "ok\n"}
        approved, _p, rows = await _resolve(_event(params))
        assert approved is False
        assert _error(rows).startswith("Blocked: sensitive path in tool_input: ")

    @pytest.mark.asyncio
    async def test_a_path_nested_under_a_body_key_is_still_scanned(self) -> None:
        # Only a STRING under a body key is skipped; a mapping under one is
        # walked, so the key above cannot hide a target.
        params = {"content": {"path": "~/.ssh/id_rsa"}}
        approved, _p, rows = await _resolve(_event(params))
        assert approved is False
        assert "id_rsa" in _error(rows)

    @pytest.mark.asyncio
    async def test_a_truncated_walk_is_denied_as_unverifiable(self) -> None:
        params = {"path": "/tmp/ok", "deep": [{"k": f"v{i}"} for i in range(6000)]}
        approved, provider, rows = await _resolve(_event(params))
        assert approved is False
        assert provider.rejected == ["r1"]
        assert "too large to security-scan" in _error(rows)
        assert rows[0]["metadata"]["mechanism"] == "always_deny_input"

    @pytest.mark.asyncio
    async def test_the_title_tier_still_runs_first(self) -> None:
        params = {"path": "/tmp/proj/a.md", "content": "ok\n"}
        ev = _event(params, title="env | grep AWS_SECRET_ACCESS_KEY")
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert rows[0]["metadata"]["mechanism"] == "always_deny"


class TestUnknownProvenanceKeepsTheFullScan:
    """Each flag the scoping keys on is client-derived; missing any one of them
    leaves the document scan in place, and the body is refused as before."""

    @pytest.mark.asyncio
    async def test_an_unknown_tool_is_still_denied(self) -> None:
        # No tool_call frame preceded this permission frame: nothing is
        # classified, nothing is trusted, so the payload is scanned whole.
        params = {"path": "/tmp/proj/a.md", "content": _PROSE}
        ev = _event(params, trusted=False, identity=False, tool_name="")
        approved, provider, rows = await _resolve(ev)
        assert approved is False
        assert provider.rejected == ["r1"]
        assert "rm -rf" in _error(rows)

    @pytest.mark.asyncio
    async def test_unresolved_identity_keeps_the_document_scan(self) -> None:
        # shell_classified + raw_params_trusted, but the identity caches missed:
        # built-in or MCP cannot be told apart, so the tool is unknown.
        params = {"path": "/tmp/proj/a.md", "content": _PROSE}
        approved, _p, rows = await _resolve(_event(params, identity=False))
        assert approved is False
        assert "rm -rf" in _error(rows)

    @pytest.mark.asyncio
    async def test_inline_params_without_cache_provenance_keep_the_document_scan(self) -> None:
        params = {"path": "/tmp/proj/a.md", "content": _PROSE}
        ev = _event(params)
        ev.raw_params_trusted = False
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "rm -rf" in _error(rows)

    @pytest.mark.asyncio
    async def test_unclassified_shell_state_keeps_the_document_scan(self) -> None:
        params = {"path": "/tmp/proj/a.md", "content": _PROSE}
        ev = _event(params)
        ev.shell_classified = False
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "rm -rf" in _error(rows)

    @pytest.mark.asyncio
    async def test_no_params_keeps_the_document_scan(self) -> None:
        ev = _event(None)
        ev.tool_input = json.dumps({"content": _PROSE})
        approved, _p, rows = await _resolve(ev)
        assert approved is False
        assert "rm -rf" in _error(rows)


class TestCommandShapedStringsUnit:
    def test_skips_body_strings_and_keeps_everything_else(self) -> None:
        params = {
            "command": "create",
            "path": "/tmp/a.md",
            "content": "body",
            "edits": [{"oldStr": "a", "newStr": "b", "path": "/tmp/b.md"}],
            "n": 3,
            "url": "https://example.com",
        }
        got = command_shaped_strings(params)
        assert list(got) == ["create", "/tmp/a.md", "/tmp/b.md", "https://example.com"]
        assert got.truncated is False

    def test_a_mapping_under_a_body_key_is_walked(self) -> None:
        assert list(command_shaped_strings({"content": {"path": "/x"}})) == ["/x"]
        assert list(command_shaped_strings({"content": ["a", "b"]})) == ["a", "b"]

    def test_non_mapping_input_is_empty(self) -> None:
        assert list(command_shaped_strings(None)) == []
        assert list(command_shaped_strings("x")) == []  # type: ignore[arg-type]
        assert list(command_shaped_strings({})) == []

    def test_the_work_cap_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tool_paths, "_TARGET_PATH_MAX_NODES", 5)
        got = command_shaped_strings({"a": ["1", "2", "3", "4", "5", "6"]})
        assert got.truncated is True


class TestProvenanceComesFromTheClient:
    """The identity flag the scoping keys on is set only when the tool_call
    frame's ``_meta.kiro`` caches HIT, exactly as ``acp._dispatch`` derives it."""

    @staticmethod
    def _frames(tool_call: dict, *, with_identity_caches: bool = True):
        from kiro_crew.acp import _dispatch
        from kiro_crew.acp.types import JsonRpcMessage

        caches: dict = dict(
            tool_input_cache={},
            shell_cache={},
            raw_params_cache={},
        )
        if with_identity_caches:
            caches["mcp_server_name_cache"] = {}
            caches["tool_name_cache"] = {}
        _dispatch._build_tool_call_event(tool_call, **caches)
        msg = JsonRpcMessage(
            id="p1",
            method="session/request_permission",
            params={
                "toolCall": {"toolCallId": "t1", "title": "x", "kind": "other"},
                "options": [{"optionId": "allow_once", "kind": "allow_once"}],
            },
        )
        ev, _ = _dispatch.build_permission_event(msg, **caches)
        return ev

    def test_a_built_in_write_carries_the_trusted_flags(self) -> None:
        raw = {"command": "create", "path": "/tmp/proj/a.md", "content": _PROSE}
        ev = self._frames(
            {
                "toolCallId": "t1",
                "title": "x",
                "kind": "other",
                "rawInput": raw,
                "_meta": {"kiro": {"toolName": "fs_write"}},
            }
        )
        assert ev.shell_classified is True and ev.is_shell is False
        assert ev.raw_params_trusted is True
        assert ev.mcp_identity_trusted is True
        assert ev.mcp_server_name == "" and ev.tool_name == "fs_write"

    def test_a_shell_frame_stays_a_shell_tool(self) -> None:
        ev = self._frames(
            {
                "toolCallId": "t1",
                "title": "x",
                "kind": "execute",
                "rawInput": {"command": _QUOTED_COMMAND},
                "_meta": {"kiro": {"toolName": "execute_bash"}},
            }
        )
        assert ev.is_shell is True and ev.shell_classified is True

    def test_missing_identity_caches_leave_the_tool_unknown(self) -> None:
        ev = self._frames(
            {"toolCallId": "t1", "title": "x", "kind": "other", "rawInput": {"content": "x"}},
            with_identity_caches=False,
        )
        assert ev.mcp_identity_trusted is False


_URL = "https://example.com/docs/x"
_STALL_NOW = 1_000_000.0


def _fetch_event(
    url: str = _URL,
    *,
    tool_id: str = "claude#WebFetch",
    trusted: bool = True,
    **params: object,
) -> LLMEvent:
    """A claude permission event shaped as the client builds it from the live
    capture (``builtins-bash-edit-webfetch-live``): the WebFetch title, the trusted
    ``url`` params and the harness id the tool_call frame stamped."""
    raw = {"url": url, **params}
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=f"Fetch {url}",
        request_id="r1",
        tool_kind="fetch",
        tool_input=json.dumps(raw),
        raw_tool_params=raw,
        raw_params_trusted=trusted,
        harness_tool_id=tool_id,
    )


_CAPTURE = (
    Path(__file__).parent / "fixtures/acp_frames/claude/builtins-bash-edit-webfetch-live.jsonl"
)
_PROMPT = "What is the page title?"


def _captured_fetch_event() -> LLMEvent:
    """The capture's WebFetch, as sent (``url`` and ``prompt``), built by the client's
    own parser from its ``tool_call`` frames and ``session/request_permission``."""
    from kiro_crew.acp import _dispatch
    from kiro_crew.acp.types import JsonRpcMessage

    frames = [json.loads(line) for line in _CAPTURE.read_text().splitlines()[1:]]
    caches: dict = {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "harness_tool_name_cache": {},
        "tool_kind_cache": {},
    }
    for frame in frames:
        if frame.get("method") != "session/request_permission":
            _dispatch.parse_session_update(
                frame["params"]["update"], harness_backend="claude", **caches
            )
            continue
        if frame["params"]["toolCall"].get("name") != "WebFetch":
            continue
        msg = JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])
        event, _ = _dispatch.build_permission_event(msg, harness_backend="claude", **caches)
        assert event is not None
        return event
    raise AssertionError("the capture has no WebFetch permission request")


async def _resolve_with(
    event: LLMEvent, policy: ToolApprovalPolicy
) -> tuple[bool, _RecordingProvider, list[dict]]:
    from kiro_crew.hooks import HookManager, HooksConfig

    provider = _RecordingProvider()
    rows: list[dict] = []
    sel_stub = MagicMock()
    sel_stub.log_tool_invocation.side_effect = lambda **kw: rows.append(kw)
    with patch.object(sel_mod, "sel", lambda: sel_stub):
        approved = await _resolve_permission(
            provider,  # type: ignore[arg-type]
            event,
            policy,
            HookManager(HooksConfig()),
        )
    return approved, provider, rows


@pytest.fixture
def stalled_cwd(tmp_path, monkeypatch):
    """The issue's state: a real cooldown over the home prefix and no home anchors
    to serve, so their bounded rebuild refuses every path check fail-closed."""
    from kiro_crew.security import paths

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "_path_resolve_clock", lambda: _STALL_NOW)
    monkeypatch.setattr(paths, "_home_targets_cache", {})
    prefix = paths._stall_prefix(str(Path.home()))
    monkeypatch.setitem(paths._path_resolve_degraded, prefix, (_STALL_NOW + 30.0, 1))
    return tmp_path


_POLICIES = [ToolApprovalPolicy.AUTO_APPROVE, ToolApprovalPolicy.HOOK_BASED]

#: The planted-link cases name a link ``https:`` and lean on POSIX collapsing ``//``;
#: NTFS cannot hold a ``:`` in a name, so the shape does not exist on Windows.
_POSIX_COLON_LINK = pytest.mark.skipif(
    sys.platform == "win32", reason="a link named 'https:' cannot exist on NTFS"
)


class TestAProvenFetchUrlIsNotAPath:
    """A proven web fetch's URL, and its ``Fetch <url>`` title, skip the path tier
    of both gates (the resolver's tiers and the hook gate's); nothing else does."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("policy", _POLICIES)
    async def test_a_fetch_is_not_refused_by_a_resolver_stall(self, stalled_cwd, policy) -> None:
        approved, provider, rows = await _resolve_with(_fetch_event(), policy)
        assert approved is True, rows
        assert provider.approved == ["r1"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("policy", _POLICIES)
    async def test_the_captured_webfetch_with_its_prompt_is_not_refused_by_a_stall(
        self, stalled_cwd, policy
    ) -> None:
        ev = _captured_fetch_event()
        assert ev.harness_tool_id == "claude#WebFetch"
        assert ev.raw_params_trusted is True
        assert ev.raw_tool_params == {"url": "https://example.com", "prompt": _PROMPT}
        approved, provider, rows = await _resolve_with(ev, policy)
        assert approved is True, rows
        assert provider.approved == [ev.request_id]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("policy", _POLICIES)
    async def test_a_file_url_on_a_fetch_is_still_refused(self, stalled_cwd, policy) -> None:
        approved, _p, rows = await _resolve_with(_fetch_event("file:///home/u/.ssh/id_rsa"), policy)
        assert approved is False
        assert security.is_unverifiable_path_refusal(_error(rows))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_id", ["claude#Read", "claude#Bash", "claude#Edit"])
    async def test_the_same_url_on_another_tool_is_still_refused(
        self, stalled_cwd, tool_id
    ) -> None:
        ev = _fetch_event(tool_id=tool_id)
        approved, _p, rows = await _resolve_with(ev, ToolApprovalPolicy.AUTO_APPROVE)
        assert approved is False
        assert _error(rows).endswith(f"Path: {f'Fetch {_URL}'!r}")

    @_POSIX_COLON_LINK
    @pytest.mark.asyncio
    async def test_a_url_through_a_planted_link_is_still_a_credential_on_a_read(
        self, tmp_path, monkeypatch
    ) -> None:
        # ``https://.aws/credentials`` is ``https:/.aws/credentials``: with an
        # ``https:`` link to $HOME in the base directory, the credential file.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "https:").symlink_to(Path.home(), target_is_directory=True)
        ev = _fetch_event("https://.aws/credentials", tool_id="claude#Read")
        approved, _p, rows = await _resolve_with(ev, ToolApprovalPolicy.AUTO_APPROVE)
        assert approved is False
        assert _error(rows).startswith("Blocked: sensitive path")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "unproven",
        [
            {"tool_id": ""},
            {"tool_id": "claude#WebSearch"},
            {"tool_id": "claude#mcp__web__fetch"},
            {"tool_id": "web_fetch"},
            {"trusted": False},
            {"url": "https://:80/docs"},
        ],
        ids=[
            "no-identity",
            "other-tool",
            "mcp-tool",
            "unqualified-id",
            "untrusted-params",
            "hostless-url",
        ],
    )
    async def test_an_unproven_fetch_gets_no_exemption(self, stalled_cwd, unproven) -> None:
        approved, _p, rows = await _resolve_with(
            _fetch_event(**unproven), ToolApprovalPolicy.AUTO_APPROVE
        )
        assert approved is False
        assert security.is_unverifiable_path_refusal(_error(rows))

    def test_a_malformed_event_proves_no_fetch(self) -> None:
        from kiro_crew.hooks import proven_fetch_url

        assert proven_fetch_url(MagicMock()) == ""

    @pytest.mark.parametrize(
        ("url", "kind", "tier"),
        [
            ("http://169.254.169.254/latest/meta-data/", "bash", "sensitive-bash"),
            ("https://x.example/a; rm -rf / ", "regex", "deny-rules"),
        ],
    )
    def test_the_command_tiers_still_read_a_fetch_url(self, url, kind, tier) -> None:
        from kiro_crew.hooks import HookManager, HooksConfig, proven_fetch_url

        assert proven_fetch_url(_fetch_event(url)) == url
        title_hit = llm_helpers._title_denial(f"Fetch {url}", None, exempt_url=url)
        input_hit = llm_helpers._first_tool_input_denial([url], None, path_rule=False)
        assert title_hit is not None and title_hit[0] == kind
        assert input_hit is not None and input_hit[0] == kind
        verdict = HookManager(HooksConfig()).on_tool_call(
            f"Fetch {url}", tool_kind="fetch", fetch_url=url
        )
        assert verdict.tier == tier

    @pytest.mark.asyncio
    async def test_a_credential_path_under_an_unlisted_key_is_still_refused(self) -> None:
        ev = _fetch_event(prompt=_PROMPT, extra="~/.ssh/id_rsa")
        approved, _p, rows = await _resolve_with(ev, ToolApprovalPolicy.AUTO_APPROVE)
        assert approved is False
        assert _error(rows) == "Blocked: sensitive path in tool_input: ~/.ssh/id_rsa"

    @_POSIX_COLON_LINK
    @pytest.mark.asyncio
    async def test_the_url_is_spared_by_key_not_by_value(self, tmp_path, monkeypatch) -> None:
        # The same string as the proven ``url``, under a key no row lists, is still
        # resolved: through the planted ``https:`` link it is the credential file.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "https:").symlink_to(Path.home(), target_is_directory=True)
        url = "https://.aws/credentials"
        ev = _fetch_event(url, prompt=_PROMPT, extra=url)
        approved, _p, rows = await _resolve_with(ev, ToolApprovalPolicy.AUTO_APPROVE)
        assert approved is False
        assert _error(rows) == f"Blocked: sensitive path in tool_input: {url}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prompt", [{"text": _PROMPT}, [_PROMPT]], ids=["mapping", "list"])
    async def test_a_prompt_that_is_not_a_string_is_still_path_scanned(
        self, stalled_cwd, prompt
    ) -> None:
        ev = _fetch_event(prompt=prompt)
        approved, _p, rows = await _resolve_with(ev, ToolApprovalPolicy.AUTO_APPROVE)
        assert approved is False
        assert _error(rows).endswith(f"Path: {_PROMPT!r}")

    @pytest.mark.asyncio
    async def test_another_harness_fetch_keeps_its_prompt_path_gated(self, stalled_cwd) -> None:
        # opencode's webfetch is a proven fetch, so its url and title pass; no captured
        # frame vets its other arguments, so the prompt is what the stall refuses.
        ev = _fetch_event(tool_id="opencode#webfetch", prompt=_PROMPT)
        approved, _p, rows = await _resolve_with(ev, ToolApprovalPolicy.AUTO_APPROVE)
        assert approved is False
        assert _error(rows).endswith(f"Path: {_PROMPT!r}")

    @_POSIX_COLON_LINK
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "frame",
        [
            {"trusted": False},
            {"tool_id": ""},
            {"tool_id": "claude#Read"},
            {"tool_id": "claude#Agent"},
        ],
        ids=["untrusted-params", "unproven", "non-fetch", "claude-agent"],
    )
    async def test_the_same_prompt_elsewhere_is_still_path_scanned(
        self, tmp_path, monkeypatch, frame
    ) -> None:
        # Through the planted ``https:`` link this prompt is the credential file, so it
        # is refused as that path only when the path tier reads the prompt.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "https:").symlink_to(Path.home(), target_is_directory=True)
        prompt = "https://.aws/credentials"
        approved, _p, rows = await _resolve_with(
            _fetch_event(prompt=prompt, **frame), ToolApprovalPolicy.AUTO_APPROVE
        )
        assert approved is False
        assert _error(rows) == f"Blocked: sensitive path in tool_input: {prompt}"

    @_POSIX_COLON_LINK
    @pytest.mark.asyncio
    async def test_a_claude_task_prompt_is_still_path_scanned(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "https:").symlink_to(Path.home(), target_is_directory=True)
        prompt = "https://.aws/credentials"
        params = {"description": "look", "prompt": prompt, "subagent_type": "general-purpose"}
        ev = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Task",
            request_id="r1",
            tool_kind="think",
            tool_input=json.dumps(params),
            raw_tool_params=params,
            raw_params_trusted=True,
            harness_tool_id="claude#Task",
        )
        approved, _p, rows = await _resolve_with(ev, ToolApprovalPolicy.AUTO_APPROVE)
        assert approved is False
        assert _error(rows) == f"Blocked: sensitive path in tool_input: {prompt}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("prompt", "kind"),
        [
            ("Summarise http://169.254.169.254/latest/meta-data/ for me", "bash"),
            ("Summarise it; rm -rf / ", "regex"),
        ],
        ids=["imds-bash-tier", "deny-tier"],
    )
    async def test_the_command_tiers_still_read_the_prompt(self, prompt, kind) -> None:
        ev = _fetch_event(prompt=prompt)
        approved, _p, rows = await _resolve_with(ev, ToolApprovalPolicy.AUTO_APPROVE)
        assert approved is False
        hit = llm_helpers._first_tool_input_denial([prompt], None, path_rule=False)
        assert hit is not None and hit[0] == kind
        assert _error(rows) == hit[1]

    @pytest.mark.asyncio
    async def test_an_oversize_prompt_is_still_refused(self) -> None:
        prompt = "x" * (llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        approved, _p, rows = await _resolve_with(
            _fetch_event(prompt=prompt), ToolApprovalPolicy.AUTO_APPROVE
        )
        assert approved is False
        assert "too large to security-scan" in _error(rows)

    @pytest.mark.asyncio
    async def test_the_node_cap_still_refuses_a_fetch(self, monkeypatch) -> None:
        monkeypatch.setattr(tool_paths, "_TARGET_PATH_MAX_NODES", 3)
        ev = _fetch_event(prompt=_PROMPT, extra=["a", "b", "c", "d"])
        approved, _p, rows = await _resolve_with(ev, ToolApprovalPolicy.AUTO_APPROVE)
        assert approved is False
        assert (
            _error(rows) == "Blocked: tool arguments too large to security-scan (deny-by-default)"
        )
