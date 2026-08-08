"""Tests for kiro_crew.validation — tool input/output validation."""

from __future__ import annotations

import pytest

from kiro_crew.validation import (
    ARTIFACT_SAVE_SCHEMA,
    CHANNEL_ID_RE,
    CRON_ADD_SCHEMA,
    LEARN_ADD_SCHEMA,
    SEND_MESSAGE_SCHEMA,
    SET_PROJECT_SCHEMA,
    SLACK_THREAD_TS_RE,
    SPAWN_RUN_SCHEMA,
    TASK_RUN_SCHEMA,
    FieldSpec,
    McpTextContent,
    ValidationError,
    build_tool_response,
    normalize_unicode,
    sanitize_response,
    sanitize_string,
    strip_hidden_unicode,
    validate_api_body,
    validate_field,
    validate_jsonrpc_request,
    validate_jsonrpc_response,
    validate_mcp_tool_arguments,
    validate_string_field,
    validate_tool_args,
)

# ── String Sanitization ──


class TestStripHiddenUnicode:
    def test_preserves_normal_text(self):
        assert strip_hidden_unicode("hello world\nfoo") == "hello world\nfoo"

    def test_strips_zero_width_space(self):
        assert strip_hidden_unicode("he\u200bllo") == "hello"

    def test_preserves_zero_width_joiner(self):
        # ZWJ is script-essential where it can actually shape: it welds emoji
        # sequences and is required by Arabic / Persian / Indic text. Between
        # two ASCII characters it shapes nothing, so it is dropped there (that
        # is the credential-redaction bypass — see the credential test below).
        assert (
            strip_hidden_unicode("\U0001f468\u200d\U0001f469")
            == "\U0001f468\u200d\U0001f469"
        )
        assert strip_hidden_unicode("\u0915\u200d\u0937") == "\u0915\u200d\u0937"
        assert strip_hidden_unicode("a\u200db") == "ab"

    def test_preserves_zwnj_and_variation_selector(self):
        assert strip_hidden_unicode("\u0645\u06cc\u200c\u062e") == "\u0645\u06cc\u200c\u062e"
        assert strip_hidden_unicode("\u2764\ufe0f") == "\u2764\ufe0f"

    def test_format_chars_are_deny_by_default(self):
        # Cf is fail-closed: only ZWNJ/ZWJ/LRM/RLM are allowed through, so a
        # format character nobody enumerated is stripped rather than silently
        # becoming an evasion vector.
        for ch in (
            "\u00ad",  # SOFT HYPHEN
            "\u180e",  # MONGOLIAN VOWEL SEPARATOR
            "\u2063",  # INVISIBLE SEPARATOR
            "\u2060",  # WORD JOINER
            "\ufff9",  # INTERLINEAR ANNOTATION ANCHOR
        ):
            assert ch not in strip_hidden_unicode(f"a{ch}b"), f"{ch!r} should be stripped"

    def test_shaping_marks_need_a_non_ascii_neighbour(self):
        # The four allowlisted marks shape NON-ASCII text, so between two ASCII
        # characters they have no rendering effect and are dropped.
        for ch in ("\u200c", "\u200d", "\u200e", "\u200f"):
            assert strip_hidden_unicode(f"a{ch}b") == "ab", f"{ch!r} between ASCII"
            assert strip_hidden_unicode(f"\u0645{ch}\u062e") == f"\u0645{ch}\u062e"
            assert strip_hidden_unicode(f"{ch}abc") == "abc", f"leading {ch!r}"
            assert strip_hidden_unicode(f"abc{ch}") == "abc", f"trailing {ch!r}"

    def test_marks_cannot_vouch_for_each_other(self):
        # "\u200d".isascii() is False, so a RUN of shaping marks would let each
        # one qualify as the next one's non-ASCII neighbour and all of them
        # survive inside an ASCII credential. The neighbour test skips over
        # adjacent marks, and runs after the other hidden characters are
        # already removed, so a stripped character cannot vouch either.
        clean = "AKIAIOSFODNN7EXAMPLE"
        for spike in (
            "\u200d\u200d",  # doubled ZWJ
            "\u200d\u200c\u200e",  # mixed run of three
            "\u200b\u200d",  # stripped ZWSP then ZWJ
            "\ufeff\u200c",  # stripped BOM then ZWNJ
            "\u202e\u200d",  # stripped bidi override then ZWJ
        ):
            assert strip_hidden_unicode(f"AKIA{spike}IOSFODNN7EXAMPLE") == clean, spike
        # A doubled joiner between real emoji is still legitimate and survives.
        emoji = "\U0001f468\u200d\u200d\U0001f469"
        assert strip_hidden_unicode(emoji) == emoji

    def test_invisible_cannot_hide_a_credential_from_redaction(self):
        # This sanitizer runs BEFORE credential redaction, so an invisible
        # character embedded in a credential must not survive to defeat the
        # redaction patterns and carry a recoverable secret into the dashboard
        # or the notification JSONL. Covers the allowlisted shaping marks too:
        # a credential is ASCII, so they have no neighbour that justifies them.
        clean = "AKIAIOSFODNN7EXAMPLE"
        for ch in (
            "\u200b",  # ZWSP
            "\u2063",  # invisible separator
            "\ufeff",  # BOM
            "\u00ad",  # soft hyphen
            "\u200c",  # ZWNJ — allowlisted, but not between ASCII
            "\u200d",  # ZWJ — same
            "\u200e",  # LRM — same
            "\u200f",  # RLM — same
        ):
            spiked = f"AKIA{ch}IOSFODNN7EXAMPLE"
            assert strip_hidden_unicode(spiked) == clean, f"{ch!r} survived"

    def test_strips_bom(self):
        assert strip_hidden_unicode("\ufeffhello") == "hello"

    def test_strips_directional_overrides(self):
        assert strip_hidden_unicode("a\u202eb\u202c") == "ab"

    def test_preserves_tab_and_newline(self):
        assert strip_hidden_unicode("a\tb\nc") == "a\tb\nc"

    def test_strips_null_byte(self):
        assert strip_hidden_unicode("a\x00b") == "ab"

    def test_preserves_emoji(self):
        assert strip_hidden_unicode("hello 🦞") == "hello 🦞"

    def test_preserves_cjk(self):
        assert strip_hidden_unicode("你好世界") == "你好世界"


class TestNormalizeUnicode:
    def test_nfc_normalization(self):
        # é as combining sequence → single codepoint
        assert normalize_unicode("e\u0301") == "\u00e9"

    def test_already_nfc(self):
        assert normalize_unicode("café") == "café"


class TestSanitizeString:
    def test_full_pipeline(self):
        # BOM + zero-width + combining + trailing space
        result = sanitize_string("\ufeffhe\u200bllo\u0301 ")
        assert result == "helló"

    def test_empty_string(self):
        assert sanitize_string("") == ""

    def test_only_hidden_chars(self):
        # ZWSP is always dropped. ZWNJ/ZWJ shape neighbouring non-ASCII text,
        # and a string of nothing but marks has no such neighbour, so it
        # collapses to empty rather than keeping decorative invisibles.
        assert sanitize_string("\u200b") == ""
        assert sanitize_string("\u200b\u200c\u200d") == ""
        # Beside real script they survive.
        assert sanitize_string("\u0645\u200c\u062e") == "\u0645\u200c\u062e"


# ── Response Sanitization ──


class TestSanitizeResponse:
    def test_normal_response(self):
        assert sanitize_response("ok") == "ok"

    def test_truncation(self):
        long = "x" * 200
        result = sanitize_response(long, max_len=100)
        assert len(result) < 200
        assert "truncated" in result

    def test_strips_hidden_chars(self):
        assert sanitize_response("a\u200bb") == "ab"


# ── Field Validation ──


class TestValidateField:
    def test_required_missing(self):
        with pytest.raises(ValidationError, match="required"):
            validate_field(None, FieldSpec("x", str, required=True))

    def test_optional_missing_returns_default(self):
        assert validate_field(None, FieldSpec("x", str, default="hi")) == "hi"

    def test_wrong_type(self):
        with pytest.raises(ValidationError, match="expected str"):
            validate_field(123, FieldSpec("x", str))

    def test_string_max_len(self):
        with pytest.raises(ValidationError, match="max length"):
            validate_field("toolong", FieldSpec("x", str, max_len=3))

    def test_string_allowed(self):
        allowed = frozenset({"a", "b"})
        assert validate_field("a", FieldSpec("x", str, allowed=allowed)) == "a"
        with pytest.raises(ValidationError, match="must be one of"):
            validate_field("c", FieldSpec("x", str, allowed=allowed))

    def test_string_pattern(self):
        import re

        pat = re.compile(r"^[a-z]+$")
        assert validate_field("abc", FieldSpec("x", str, pattern=pat)) == "abc"
        with pytest.raises(ValidationError, match="invalid format"):
            validate_field("ABC", FieldSpec("x", str, pattern=pat))

    def test_numeric_min(self):
        with pytest.raises(ValidationError, match=">= 10"):
            validate_field(5, FieldSpec("x", int, min_val=10))

    def test_numeric_max(self):
        with pytest.raises(ValidationError, match="<= 100"):
            validate_field(200, FieldSpec("x", int, max_val=100))

    def test_sanitizes_string(self):
        result = validate_field("he\u200bllo", FieldSpec("x", str))
        assert result == "hello"

    def test_multi_type(self):
        assert validate_field(1, FieldSpec("x", (int, float))) == 1
        assert validate_field(1.5, FieldSpec("x", (int, float))) == 1.5


# ── Tool Schema Validation ──


class TestValidateToolArgs:
    def test_spawn_run_valid(self):
        result = validate_tool_args({"task": "do stuff"}, SPAWN_RUN_SCHEMA)
        assert result["task"] == "do stuff"

    def test_spawn_run_tasks_array(self):
        result = validate_tool_args({"tasks": ["a", "b"]}, SPAWN_RUN_SCHEMA)
        assert result["tasks"] == ["a", "b"]

    def test_spawn_run_no_args_passes(self):
        # Neither task nor tasks is required at schema level;
        # _call_tool_inner validates at runtime
        result = validate_tool_args({}, SPAWN_RUN_SCHEMA)
        assert "task" not in result or result.get("task") is None

    def test_spawn_run_max_turns_zero_allowed(self):
        result = validate_tool_args({"task": "x", "max_turns": 0}, SPAWN_RUN_SCHEMA)
        assert result["max_turns"] == 0

    def test_spawn_run_max_turns_negative_rejected(self):
        with pytest.raises(ValidationError, match=">="):
            validate_tool_args({"task": "x", "max_turns": -1}, SPAWN_RUN_SCHEMA)

    def test_spawn_run_context_groups_accepted(self):
        result = validate_tool_args(
            {
                "task": "x",
                "include_memory": False,
                "include_lessons": True,
                "include_project": False,
            },
            SPAWN_RUN_SCHEMA,
        )
        assert result["include_memory"] is False
        assert result["include_lessons"] is True
        assert result["include_project"] is False

    def test_spawn_run_context_groups_omitted(self):
        """Absent flags must not materialize as False — omitted means all groups on."""
        result = validate_tool_args({"task": "x"}, SPAWN_RUN_SCHEMA)
        assert result.get("include_memory") is not False
        assert result.get("include_lessons") is not False
        assert result.get("include_project") is not False

    def test_spawn_run_context_group_non_bool_rejected(self):
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "include_memory": "no"}, SPAWN_RUN_SCHEMA)

    def test_learn_add_valid(self):
        result = validate_tool_args(
            {"rule": "use dark mode", "category": "preference"},
            LEARN_ADD_SCHEMA,
        )
        assert result["rule"] == "use dark mode"
        assert result["category"] == "preference"

    def test_learn_add_default_category(self):
        result = validate_tool_args({"rule": "use dark mode"}, LEARN_ADD_SCHEMA)
        assert result["category"] == "knowledge"

    def test_learn_add_bad_category(self):
        with pytest.raises(ValidationError, match="must be one of"):
            validate_tool_args(
                {"rule": "x", "category": "invalid"},
                LEARN_ADD_SCHEMA,
            )

    def test_learn_add_with_scope_and_workspace(self):
        result = validate_tool_args(
            {"rule": "use pytest-asyncio", "scope": "workspace", "workspace": "my-project"},
            LEARN_ADD_SCHEMA,
        )
        assert result["scope"] == "workspace"
        assert result["workspace"] == "my-project"

    def test_learn_add_scope_defaults_global(self):
        result = validate_tool_args({"rule": "use dark mode"}, LEARN_ADD_SCHEMA)
        assert result["scope"] == "global"

    def test_learn_add_bad_scope(self):
        with pytest.raises(ValidationError, match="must be one of"):
            validate_tool_args(
                {"rule": "x", "scope": "session"},
                LEARN_ADD_SCHEMA,
            )

    def test_learn_add_bad_workspace(self):
        with pytest.raises(ValidationError, match="invalid format"):
            validate_tool_args(
                {"rule": "x", "scope": "workspace", "workspace": "bad name!"},
                LEARN_ADD_SCHEMA,
            )

    def test_cron_add_valid(self):
        result = validate_tool_args(
            {"name": "check", "message": "check pipeline", "every": 300},
            CRON_ADD_SCHEMA,
        )
        assert result["name"] == "check"
        assert result["every"] == 300

    def test_cron_add_interval_too_low(self):
        with pytest.raises(ValidationError, match=">= 60"):
            validate_tool_args(
                {"name": "x", "message": "y", "every": 10},
                CRON_ADD_SCHEMA,
            )

    def test_cron_add_with_channel(self):
        result = validate_tool_args(
            {"name": "ops", "message": "check", "every": 300, "channel": "C0AP77JJSN6"},
            CRON_ADD_SCHEMA,
        )
        assert result["channel"] == "C0AP77JJSN6"

    def test_cron_add_invalid_channel(self):
        with pytest.raises(ValidationError, match="invalid format"):
            validate_tool_args(
                {"name": "ops", "message": "check", "every": 300, "channel": "not-a-channel"},
                CRON_ADD_SCHEMA,
            )

    def test_cron_add_accepts_windows_script_path(self):
        # A Windows absolute script path (drive letter + backslashes) must pass
        # the shape check — the old POSIX-only class rejected every path
        # Explorer / a file picker produces.
        win_path = "C:\\Users\\me\\.kiro\\crew\\crons\\job.py:run"
        result = validate_tool_args(
            {"name": "win", "script": win_path, "every": 300},
            CRON_ADD_SCHEMA,
        )
        assert result["script"] == win_path

    def test_cron_add_accepts_windows_forward_slash_script_path(self):
        result = validate_tool_args(
            {"name": "win", "script": "C:/Users/me/crons/job.py:run", "every": 300},
            CRON_ADD_SCHEMA,
        )
        assert result["script"] == "C:/Users/me/crons/job.py:run"

    def test_cron_add_still_accepts_posix_script_path(self):
        result = validate_tool_args(
            {"name": "p", "script": "~/.kiro/crew/crons/job.py:run", "every": 300},
            CRON_ADD_SCHEMA,
        )
        assert result["script"] == "~/.kiro/crew/crons/job.py:run"

    def test_cron_add_script_without_func_rejected(self):
        with pytest.raises(ValidationError, match="invalid format"):
            validate_tool_args(
                {"name": "x", "script": "C:\\crons\\job.py", "every": 300},
                CRON_ADD_SCHEMA,
            )

    def test_cron_add_accepts_windows_path_with_spaces(self):
        # "First Last" is the DEFAULT Windows account-name shape, and
        # config_dir() is rooted at %USERPROFILE%, so rejecting spaces made a
        # script cron impossible for a typical Windows user.
        spaced = "C:\\Users\\John Smith\\.kiro\\crew\\crons\\job.py:run"
        result = validate_tool_args(
            {"name": "s", "script": spaced, "every": 300}, CRON_ADD_SCHEMA
        )
        assert result["script"] == spaced

    def test_cron_add_rejects_unc_script_path(self):
        # A UNC path is not a local script, and resolving one triggers an
        # outbound SMB/DNS probe before the crons-root check can reject it.
        for unc in ("\\\\host\\share\\job.py:run", "//host/share/job.py:run"):
            with pytest.raises(ValidationError, match="invalid format"):
                validate_tool_args(
                    {"name": "x", "script": unc, "every": 300}, CRON_ADD_SCHEMA
                )

    def test_task_run_valid(self):
        result = validate_tool_args({"spec": "do things"}, TASK_RUN_SCHEMA)
        assert result["spec"] == "do things"

    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError, match="unknown field"):
            validate_tool_args(
                {"task": "x", "evil_field": "y"},
                SPAWN_RUN_SCHEMA,
            )

    def test_non_dict_args(self):
        with pytest.raises(ValidationError, match="must be a JSON object"):
            validate_tool_args("not a dict", SPAWN_RUN_SCHEMA)  # type: ignore[arg-type]

    def test_hidden_unicode_in_task(self):
        result = validate_tool_args(
            {"task": "do\u200b stuff"},
            SPAWN_RUN_SCHEMA,
        )
        assert result["task"] == "do stuff"


# ── JSON-RPC Validation ──


class TestValidateJsonrpcRequest:
    def test_valid_request(self):
        method, rid, params = validate_jsonrpc_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}}
        )
        assert method == "tools/call"
        assert rid == 1
        assert params == {"name": "x"}

    def test_missing_method(self):
        method, rid, params = validate_jsonrpc_request({"jsonrpc": "2.0", "id": 1})
        assert method == ""

    def test_non_dict_rejected(self):
        with pytest.raises(ValidationError, match="must be a JSON object"):
            validate_jsonrpc_request("not a dict")  # type: ignore[arg-type]

    def test_non_string_method(self):
        with pytest.raises(ValidationError, match="must be a string"):
            validate_jsonrpc_request({"method": 123})

    def test_non_dict_params_defaults(self):
        _, _, params = validate_jsonrpc_request({"method": "x", "params": "bad"})
        assert params == {}


# ── Response Schema ──


class TestBuildToolResponse:
    def test_normal_response(self):
        result = build_tool_response("hello")
        assert result == {"content": [{"type": "text", "text": "hello"}]}

    def test_sanitizes_hidden_chars(self):
        result = build_tool_response("a\u200bb")
        assert result["content"][0]["text"] == "ab"

    def test_truncates_oversized(self):
        result = build_tool_response("x" * 200_000)
        text = result["content"][0]["text"]
        assert len(text) < 200_000
        assert "truncated" in text

    def test_content_is_list_of_one_text(self):
        result = build_tool_response("test")
        assert isinstance(result["content"], list)
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "text"


class TestMcpTextContent:
    def test_to_dict(self):
        c = McpTextContent(type="text", text="hello")
        assert c.to_dict() == {"type": "text", "text": "hello"}


class TestValidateJsonrpcResponse:
    def test_valid_result(self):
        resp = validate_jsonrpc_response({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        assert resp["id"] == 1
        assert resp["result"] == {"ok": True}

    def test_valid_error(self):
        resp = validate_jsonrpc_response(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32601, "message": "not found"},
            }
        )
        assert resp["error"]["code"] == -32601

    def test_missing_id(self):
        with pytest.raises(ValidationError, match="missing id"):
            validate_jsonrpc_response({"jsonrpc": "2.0", "result": {}})

    def test_missing_result_and_error(self):
        with pytest.raises(ValidationError, match="must have result or error"):
            validate_jsonrpc_response({"jsonrpc": "2.0", "id": 1})

    def test_non_dict(self):
        with pytest.raises(ValidationError, match="must be a JSON object"):
            validate_jsonrpc_response("bad")  # type: ignore[arg-type]


# ── API Body Validation ──


class TestValidateApiBody:
    def test_valid_body(self):
        assert validate_api_body({"key": "val"}) == {"key": "val"}

    def test_non_dict_rejected(self):
        with pytest.raises(ValidationError, match="must be a JSON object"):
            validate_api_body([1, 2, 3])

    def test_oversized_rejected(self):
        with pytest.raises(ValidationError, match="exceeds max size"):
            validate_api_body({"x": "a" * 200}, max_size=100)


class TestValidateStringField:
    def test_valid(self):
        assert validate_string_field({"name": "hello"}, "name", required=True) == "hello"

    def test_missing_required(self):
        with pytest.raises(ValidationError, match="required"):
            validate_string_field({}, "name", required=True)

    def test_missing_optional(self):
        assert validate_string_field({}, "name") == ""

    def test_wrong_type(self):
        with pytest.raises(ValidationError, match="must be a string"):
            validate_string_field({"name": 123}, "name")

    def test_max_len(self):
        with pytest.raises(ValidationError, match="max length"):
            validate_string_field({"name": "toolong"}, "name", max_len=3)

    def test_sanitizes(self):
        assert validate_string_field({"name": "he\u200bllo"}, "name") == "hello"

    def test_allowed(self):
        allowed = frozenset({"a", "b"})
        with pytest.raises(ValidationError, match="must be one of"):
            validate_string_field({"x": "c"}, "x", allowed=allowed)


# ── Channel ID Regex ──


@pytest.mark.parametrize("channel_id,valid", [
    ("C01ABC23DEF", True),   # standard channel
    ("G01JWUKTY10", True),   # legacy private channel
    ("D01ABC23DEF", True),   # DM channel
    ("W01ABC23DEF", True),   # Slack Connect shared channel
    ("X01ABC23DEF", False),  # invalid prefix
    ("C", False),            # too short
    ("c01abc", False),       # lowercase rejected
    ("", False),             # empty
])
def test_channel_id_re(channel_id, valid):
    assert bool(CHANNEL_ID_RE.match(channel_id)) == valid


# ── Slack thread_ts Regex (gates an authorization decision) ──


@pytest.mark.parametrize("session_key,valid", [
    ("1781215864.487849", True),       # canonical Slack thread_ts
    ("1712793600.123456", True),       # 10-digit epoch + 6-digit subsecond
    ("17812158640.4878490", True),     # 11 digits / 7 subsecond digits OK
    ("123.45", False),                 # too few digits on both sides
    ("1781215864", False),             # no subsecond component
    ("1781215864.4878", False),        # subsecond < 6 digits
    ("dashboard:chat-1", False),       # prefixed dashboard key
    ("١٧٨١٢١٥٨٦٤.٤٨٧٨٤٩", False),       # Arabic-Indic digits: \d would match, [0-9] must not
    ("१७८१२१५८६४.४८७८४९", False),       # Devanagari digits rejected
    ("", False),                       # empty
])
def test_slack_thread_ts_re_ascii_only(session_key, valid):
    """The pattern must accept ASCII-digit Slack timestamps and reject
    everything else — including Unicode-digit lookalikes, since the match
    gates channel_namespace authorization in api_lessons_create."""
    assert bool(SLACK_THREAD_TS_RE.match(session_key)) == valid


class TestSendMessageSchema:
    def test_thread_ts_valid(self):
        result = validate_tool_args(
            {"text": "hi", "thread_ts": "1712793600.123456"}, SEND_MESSAGE_SCHEMA
        )
        assert result["thread_ts"] == "1712793600.123456"

    def test_thread_ts_rejects_garbage(self):
        with pytest.raises(ValidationError):
            validate_tool_args(
                {"text": "hi", "thread_ts": "not-a-ts"}, SEND_MESSAGE_SCHEMA
            )

    def test_reply_broadcast_valid(self):
        result = validate_tool_args(
            {"text": "hi", "thread_ts": "1.2", "reply_broadcast": True},
            SEND_MESSAGE_SCHEMA,
        )
        assert result["reply_broadcast"] is True

    def test_reply_broadcast_rejects_non_bool(self):
        with pytest.raises(ValidationError):
            validate_tool_args(
                {"text": "hi", "reply_broadcast": "yes"}, SEND_MESSAGE_SCHEMA
            )


class TestSetProjectSchema:
    def test_absolute_path_accepted(self):
        result = validate_tool_args({"path": "/home/me/work"}, SET_PROJECT_SCHEMA)
        assert result["path"] == "/home/me/work"

    def test_clear_with_empty_path(self):
        result = validate_tool_args({"path": "", "clear": True}, SET_PROJECT_SCHEMA)
        assert result["path"] == ""
        assert result["clear"] is True

    def test_empty_path_without_clear_rejected(self):
        with pytest.raises(ValidationError, match="required.*clear=true"):
            validate_tool_args({"path": ""}, SET_PROJECT_SCHEMA)

    def test_missing_path_rejected(self):
        with pytest.raises(ValidationError, match="required.*clear=true"):
            validate_tool_args({}, SET_PROJECT_SCHEMA)

    def test_clear_with_non_empty_path_rejected(self):
        with pytest.raises(ValidationError, match="path must be empty when clear=true"):
            validate_tool_args({"path": "/foo", "clear": True}, SET_PROJECT_SCHEMA)

    def test_relative_path_rejected(self):
        with pytest.raises(ValidationError, match="invalid format"):
            validate_tool_args({"path": "relative/path"}, SET_PROJECT_SCHEMA)

    def test_non_string_rejected(self):
        with pytest.raises(ValidationError, match="expected str"):
            validate_tool_args({"path": 42}, SET_PROJECT_SCHEMA)

    def test_oversized_rejected(self):
        too_long = "/" + "a" * 4096
        with pytest.raises(ValidationError, match="max length"):
            validate_tool_args({"path": too_long}, SET_PROJECT_SCHEMA)

    def test_strips_hidden_unicode(self):
        result = validate_tool_args({"path": "/home/me\x00/x"}, SET_PROJECT_SCHEMA)
        assert "\x00" not in result["path"]


# ── webapp_metadata bounded validation tests ──

class TestWebappMetadataBoundedValidation:
    """Test the nested webapp_metadata validator added for item 5."""

    def test_valid_metadata_accepted(self):
        args = {
            "name": "test", "content": "<h1>hi</h1>", "kind": "webapp",
            "webapp_metadata": {
                "deploy_target": {
                    "public_url": "https://example.com/demo",
                    "profile": "my-profile",
                    "region": "us-west-2",
                    "slug": "my-demo",
                },
                "lifecycle": {"status": "live", "expires_at": "2026-12-31T23:59:59Z"},
                "cost": {"monthly_estimate": ["$0.50"]},
            },
        }
        result = validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)
        assert result["webapp_metadata"]["deploy_target"]["slug"] == "my-demo"

    def test_invalid_public_url_rejected(self):
        args = {
            "name": "t", "content": "x", "kind": "webapp",
            "webapp_metadata": {"deploy_target": {"public_url": "ftp://nope"}},
        }
        with pytest.raises(ValidationError, match="http\\(s\\) URL"):
            validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)

    def test_invalid_lifecycle_status_rejected(self):
        args = {
            "name": "t", "content": "x", "kind": "webapp",
            "webapp_metadata": {"lifecycle": {"status": "banana"}},
        }
        with pytest.raises(ValidationError, match="must be one of"):
            validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)

    def test_invalid_expires_at_rejected(self):
        args = {
            "name": "t", "content": "x", "kind": "webapp",
            "webapp_metadata": {"lifecycle": {"expires_at": "not-a-date"}},
        }
        with pytest.raises(ValidationError, match="ISO-8601"):
            validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)

    def test_oversized_list_rejected(self):
        args = {
            "name": "t", "content": "x", "kind": "webapp",
            "webapp_metadata": {"cost": {"items": ["x"] * 51}},
        }
        with pytest.raises(ValidationError, match="exceeds 50"):
            validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)

    def test_absent_fields_tolerated(self):
        """Absent nested fields don't trigger validation errors."""
        args = {
            "name": "t", "content": "x", "kind": "webapp",
            "webapp_metadata": {},  # all fields absent
        }
        result = validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)
        assert result["webapp_metadata"] == {}

    def test_invalid_profile_format_rejected(self):
        args = {
            "name": "t", "content": "x", "kind": "webapp",
            "webapp_metadata": {"deploy_target": {"profile": "evil;rm -rf"}},
        }
        with pytest.raises(ValidationError, match="invalid profile"):
            validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)

    def test_invalid_slug_format_rejected(self):
        args = {
            "name": "t", "content": "x", "kind": "webapp",
            "webapp_metadata": {"deploy_target": {"slug": "HAS_UPPERCASE"}},
        }
        with pytest.raises(ValidationError, match="invalid slug"):
            validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)

    # --- item 4 R18: empty string public_url for drafts ---

    def test_empty_public_url_accepted_for_drafts(self):
        """deploy_target.public_url="" is valid (documented draft state)."""
        args = {
            "name": "t", "content": "x", "kind": "webapp",
            "webapp_metadata": {
                "deploy_target": {"public_url": "", "profile": "p", "slug": "my-app"},
                "lifecycle": {"status": "draft"},
            },
        }
        result = validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)
        assert result["webapp_metadata"]["deploy_target"]["public_url"] == ""

    def test_javascript_url_still_rejected(self):
        """javascript: URLs are still rejected (XSS vector)."""
        args = {
            "name": "t", "content": "x", "kind": "webapp",
            "webapp_metadata": {"deploy_target": {"public_url": "javascript:alert(1)"}},
        }
        with pytest.raises(ValidationError, match="http\\(s\\) URL"):
            validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)


# --- deploy_artifact schema tests (item 6 R18) ---

class TestDeployArtifactSchema:
    """Test the deploy_artifact MCP tool schema validation."""

    def test_schema_accepts_valid_artifact_slug(self):
        from kiro_crew.validation import DEPLOY_ARTIFACT_SCHEMA

        # confirm/override_scan were REMOVED from the schema (round-19: the MCP
        # tool is preview-only; human confirmation happens in the dashboard).
        args = {"site_id": "my-app", "artifact_slug": "my-demo"}
        result = validate_tool_args(args, DEPLOY_ARTIFACT_SCHEMA)
        assert result["site_id"] == "my-app"
        assert result["artifact_slug"] == "my-demo"
        assert "confirm" not in result

    def test_schema_accepts_valid_local_dir(self):
        from kiro_crew.validation import DEPLOY_ARTIFACT_SCHEMA
        args = {"site_id": "my-app", "local_dir": "/home/user/app/public"}
        result = validate_tool_args(args, DEPLOY_ARTIFACT_SCHEMA)
        assert result["local_dir"] == "/home/user/app/public"

    def test_schema_rejects_missing_site_id(self):
        from kiro_crew.validation import DEPLOY_ARTIFACT_SCHEMA
        with pytest.raises(ValidationError, match="site_id"):
            validate_tool_args({"artifact_slug": "x"}, DEPLOY_ARTIFACT_SCHEMA)

    def test_schema_accepts_ttl_hours(self):
        from kiro_crew.validation import DEPLOY_ARTIFACT_SCHEMA
        args = {"site_id": "s", "artifact_slug": "a", "ttl_hours": 48}
        result = validate_tool_args(args, DEPLOY_ARTIFACT_SCHEMA)
        assert result["ttl_hours"] == 48


# ── MCP inputSchema validation (fail-closed boundary for app-originated calls) ──


class TestValidateMcpToolArguments:
    """validate_mcp_tool_arguments: the shared fail-closed check applied to
    UNTRUSTED tools/call arguments (e.g. from an MCP App iframe)."""

    def _v(self, args, schema):
        from kiro_crew.validation import validate_mcp_tool_arguments
        validate_mcp_tool_arguments(args, schema)

    def _raises(self, args, schema, fragment):
        from kiro_crew.validation import ValidationError, validate_mcp_tool_arguments
        with pytest.raises(ValidationError) as exc:
            validate_mcp_tool_arguments(args, schema)
        assert fragment in str(exc.value)

    def test_no_schema_allows_only_empty(self):
        self._v({}, None)
        self._raises({"a": 1}, None, "no inputSchema")

    def test_top_level_must_be_object(self):
        self._raises("nope", {"type": "object"}, "must be a JSON object")
        self._raises([1], {"type": "object"}, "must be a JSON object")

    def test_typed_properties_enforced(self):
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}, "count": {"type": "integer"}},
            "required": ["path"],
        }
        self._v({"path": "/tmp/x", "count": 3}, schema)
        self._raises({"count": 3}, schema, "required")
        self._raises({"path": 42}, schema, "expected string")
        # bool must not satisfy integer.
        self._raises({"path": "x", "count": True}, schema, "expected integer")

    def test_unknown_fields_rejected_fail_closed(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        self._raises({"a": "x", "smuggled": 1}, schema, "unknown field")

    def test_additional_properties_false_without_properties_map(self):
        """Regression: `{"type":"object","additionalProperties":false}` — a
        valid schema meaning "accepts only {}" — must reject every key even
        though it declares NO properties map."""
        schema = {"type": "object", "additionalProperties": False}
        self._v({}, schema)
        self._raises({"x": 1}, schema, "unknown field")
        # Nested position too.
        nested = {"type": "object",
                  "properties": {"o": {"type": "object", "additionalProperties": False}}}
        self._v({"o": {}}, nested)
        self._raises({"o": {"x": 1}}, nested, "unknown field")

    def test_additional_properties_opt_in(self):
        loose = {"type": "object", "properties": {"a": {"type": "string"}},
                 "additionalProperties": True}
        self._v({"a": "x", "extra": 1}, loose)
        typed = {"type": "object", "properties": {},
                 "additionalProperties": {"type": "integer"}}
        self._v({"n": 5}, typed)
        self._raises({"n": "s"}, typed, "expected integer")

    def test_enum_and_ranges(self):
        schema = {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["r", "w"]},
                "n": {"type": "number", "minimum": 0, "maximum": 10},
                "s": {"type": "string", "maxLength": 3},
            },
        }
        self._v({"mode": "r", "n": 5, "s": "ab"}, schema)
        self._raises({"mode": "x"}, schema, "not in enum")
        self._raises({"n": 11}, schema, "must be <=")
        self._raises({"s": "abcd"}, schema, "exceeds maxLength")

    def test_array_items(self):
        schema = {
            "type": "object",
            "properties": {"xs": {"type": "array", "items": {"type": "integer"},
                                  "maxItems": 2}},
        }
        self._v({"xs": [1, 2]}, schema)
        self._raises({"xs": [1, 2, 3]}, schema, "exceeds maxItems")
        self._raises({"xs": ["s"]}, schema, "expected integer")

    def test_unknown_schema_type_rejects(self):
        schema = {"type": "object",
                  "properties": {"a": {"type": "no-such-type"}}}
        self._raises({"a": 1}, schema, "expected no-such-type")

    def test_unsupported_validation_keywords_reject_fail_closed(self):
        """A constraint the subset can't enforce must never be silently
        dropped — oneOf/$ref/multipleOf/patternProperties reject outright."""
        for kw, val in [
            ("oneOf", [{"type": "string"}]),
            ("$ref", "#/defs/x"),
            ("multipleOf", 2),
            ("patternProperties", {"^x": {}}),
            ("if", {"type": "string"}),
        ]:
            schema = {"type": "object", "properties": {"a": {kw: val}}}
            self._raises({"a": 1}, schema, "unsupported validation keyword")
        # Also rejected at the top level.
        self._raises({}, {"type": "object", "allOf": []},
                     "unsupported validation keyword")

    def test_annotation_keywords_are_ignored(self):
        schema = {
            "type": "object", "title": "T", "description": "d", "$schema": "s",
            "properties": {"a": {"type": "string", "format": "uri",
                                 "default": "x", "examples": ["y"]}},
        }
        self._v({"a": "anything"}, schema)

    def test_const_and_pattern(self):
        schema = {
            "type": "object",
            "properties": {
                "mode": {"const": "fixed"},
                "path": {"type": "string", "pattern": r"^/safe/"},
            },
        }
        self._v({"mode": "fixed", "path": "/safe/file"}, schema)
        self._raises({"mode": "other"}, schema, "does not equal const")
        self._raises({"path": "/etc/passwd"}, schema, "does not match pattern")

    def test_pattern_redos_is_bounded_fail_closed(self):
        """A pathological server-controlled pattern + app-controlled
        near-match must not spin the validator: the bounded matcher times
        out (or size-caps) and the call fails closed."""
        import time as _time

        evil = {"type": "object",
                "properties": {"s": {"type": "string", "pattern": r"(a+)+$"}}}
        start = _time.monotonic()
        self._raises({"s": "a" * 3000 + "b"}, evil,
                     "could not be safely evaluated")
        assert _time.monotonic() - start < 5.0  # bounded, not exponential
        # Oversized inputs are also refused without running the regex.
        self._raises({"s": "x" * 5000},
                     {"type": "object",
                      "properties": {"s": {"type": "string", "pattern": "x+"}}},
                     "could not be safely evaluated")

    def test_exclusive_bounds_min_items_unique(self):
        schema = {
            "type": "object",
            "properties": {
                "n": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 10},
                "xs": {"type": "array", "minItems": 1, "uniqueItems": True},
            },
        }
        self._v({"n": 5, "xs": [1, 2]}, schema)
        self._raises({"n": 0}, schema, "must be > 0")
        self._raises({"n": 10}, schema, "must be < 10")
        self._raises({"xs": []}, schema, "fewer than minItems")
        self._raises({"xs": [1, 1]}, schema, "items not unique")

    def test_depth_bomb_rejected(self):
        # 20 levels of nesting under a schema with no properties map — the
        # hard depth cap must stop it.
        payload: dict = {"k": 1}
        for _ in range(20):
            payload = {"k": payload}
        self._raises({"deep": payload},
                     {"type": "object", "additionalProperties": True},
                     "max nesting depth")


# ── MCP Apps arg-validation hardening (PR #339 round 7) ──

def test_boolean_false_subschema_rejects():
    with pytest.raises(ValidationError):
        validate_mcp_tool_arguments(
            {"x": 1}, {"type": "object", "properties": {"x": False}}
        )


def test_boolean_true_subschema_accepts_anything():
    validate_mcp_tool_arguments(
        {"x": {"any": [1, 2, 3]}}, {"type": "object", "properties": {"x": True}}
    )


def test_enum_rejects_bool_for_number():
    # Python True == 1, but JSON keeps bool and number distinct.
    with pytest.raises(ValidationError, match="enum"):
        validate_mcp_tool_arguments(
            {"x": True}, {"type": "object", "properties": {"x": {"enum": [1]}}}
        )


def test_const_rejects_bool_for_number():
    with pytest.raises(ValidationError, match="const"):
        validate_mcp_tool_arguments(
            {"x": True}, {"type": "object", "properties": {"x": {"const": 1}}}
        )


def test_malformed_type_keyword_rejected():
    with pytest.raises(ValidationError, match="malformed"):
        validate_mcp_tool_arguments(
            {"x": 1}, {"type": "object", "properties": {"x": {"type": 7}}}
        )


def test_malformed_property_subschema_rejected_fail_closed():
    # GPT 5.6 finding: a property VALUE that is neither a subschema (object)
    # nor a boolean (e.g. the string "false") must NOT recurse to the
    # "no subschema → return" branch and admit the field unvalidated. The
    # whole schema is rejected fail-closed.
    with pytest.raises(ValidationError, match="malformed"):
        validate_mcp_tool_arguments(
            {"path": "/etc/shadow"},
            {"type": "object", "properties": {"path": "false"}},
        )
    # additionalProperties with a malformed (non-dict, non-bool) shape too.
    with pytest.raises(ValidationError, match="malformed"):
        validate_mcp_tool_arguments(
            {"x": 1},
            {"type": "object", "additionalProperties": "nope"},
        )
    # A boolean subschema (`false`) is still VALID shape — it rejects the value
    # on VALUE grounds, not as a malformed schema.
    with pytest.raises(ValidationError, match="forbidden"):
        validate_mcp_tool_arguments(
            {"path": "x"},
            {"type": "object", "properties": {"path": False}},
        )


def test_unique_items_distinguishes_bool_from_number():
    # [True, 1] are DISTINCT in JSON, so uniqueItems must accept them.
    validate_mcp_tool_arguments(
        {"x": [True, 1]},
        {"type": "object", "properties": {"x": {"type": "array", "uniqueItems": True}}},
    )
    with pytest.raises(ValidationError, match="unique"):
        validate_mcp_tool_arguments(
            {"x": [1, 1]},
            {"type": "object",
             "properties": {"x": {"type": "array", "uniqueItems": True}}},
        )
