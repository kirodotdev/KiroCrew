"""Tests for kiro_crew.validation — tool input/output validation."""

from __future__ import annotations

import pytest

from kiro_crew.validation import (
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
    validate_string_field,
    validate_tool_args,
)

# ── String Sanitization ──


class TestStripHiddenUnicode:
    def test_preserves_normal_text(self):
        assert strip_hidden_unicode("hello world\nfoo") == "hello world\nfoo"

    def test_strips_zero_width_space(self):
        assert strip_hidden_unicode("he\u200bllo") == "hello"

    def test_strips_zero_width_joiner(self):
        assert strip_hidden_unicode("a\u200db") == "ab"

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
        assert sanitize_string("\u200b\u200c\u200d") == ""


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
    gates slack_namespace authorization in api_lessons_create."""
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
