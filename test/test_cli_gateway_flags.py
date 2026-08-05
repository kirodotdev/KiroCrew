"""Tests for `kirocrew gateway` composable CLI flags.

Covers argparse parsing, --test-mode bundle expansion with override
semantics, and the --approval yolo safety rail.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from kiro_crew.cli import _resolve_gateway_args

# ─── Helpers ─────────────────────────────────────────────────────────────


def _ns(**kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace with gateway-flag defaults filled in.

    Mirrors what the CLI parser produces for `kirocrew gateway`. Tests
    override only the fields they exercise.
    """
    defaults = {
        "command": "gateway",
        "slack_only": False,
        "no_crons": False,
        "seed": None,
        "seed_replace": False,
        "no_open": False,
        "port": None,
        "json_ready": False,
        "approval": None,
        "test_mode": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ─── _resolve_gateway_args: bundle expansion + override semantics ────────


class TestNoFlags:
    """Without any new flags, current behavior is preserved byte-for-byte."""

    def test_defaults_pass_through(self):
        result = _resolve_gateway_args(_ns())
        assert result == {
            "no_dashboard": False,
            "no_crons": False,
            "no_open": False,
            "port_override": None,
            "json_ready": False,
            "approval_mode": None,
            "test_mode": False,
        }

    def test_legacy_flags_pass_through(self):
        result = _resolve_gateway_args(_ns(slack_only=True, no_crons=True, no_open=True))
        assert result["no_dashboard"] is True
        assert result["no_crons"] is True
        assert result["no_open"] is True
        # New flags untouched.
        assert result["port_override"] is None
        assert result["json_ready"] is False
        assert result["approval_mode"] is None
        assert result["test_mode"] is False


class TestTestModeBundle:
    """`--test-mode` expands to the documented bundle."""

    def test_bundle_defaults(self):
        result = _resolve_gateway_args(_ns(test_mode=True))
        assert result["port_override"] == "auto"
        assert result["json_ready"] is True
        assert result["no_open"] is True
        assert result["approval_mode"] == "reads"
        assert result["test_mode"] is True

    def test_explicit_approval_overrides_bundle(self, tmp_path, monkeypatch):
        # yolo bundle override needs the safety rail to pass; isolate home.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        result = _resolve_gateway_args(_ns(test_mode=True, approval="yolo"))
        assert result["approval_mode"] == "yolo"
        # Other bundle defaults still apply.
        assert result["port_override"] == "auto"
        assert result["json_ready"] is True
        assert result["no_open"] is True

    def test_explicit_port_overrides_bundle(self):
        result = _resolve_gateway_args(_ns(test_mode=True, port="9999"))
        assert result["port_override"] == "9999"
        assert result["approval_mode"] == "reads"
        assert result["json_ready"] is True
        assert result["no_open"] is True

    def test_explicit_interactive_overrides_bundle(self):
        result = _resolve_gateway_args(_ns(test_mode=True, approval="interactive"))
        assert result["approval_mode"] == "interactive"

    def test_explicit_reads_redundant_but_accepted(self):
        # Same as the bundle default — should be a no-op, not an error.
        result = _resolve_gateway_args(_ns(test_mode=True, approval="reads"))
        assert result["approval_mode"] == "reads"


class TestStandaloneFlags:
    """Each new flag works without --test-mode."""

    def test_port_int(self):
        result = _resolve_gateway_args(_ns(port="9999"))
        assert result["port_override"] == "9999"
        assert result["json_ready"] is False  # not set by --port

    def test_port_auto_alone(self):
        result = _resolve_gateway_args(_ns(port="auto"))
        assert result["port_override"] == "auto"
        assert result["json_ready"] is False

    def test_port_auto_uppercase_canonicalized(self):
        # Case-insensitive auto — common typo, should accept.
        result = _resolve_gateway_args(_ns(port="AUTO"))
        assert result["port_override"] == "auto"

    def test_port_auto_mixedcase_canonicalized(self):
        result = _resolve_gateway_args(_ns(port="Auto"))
        assert result["port_override"] == "auto"

    def test_port_int_canonicalized_to_string(self):
        # Integer string passes through unchanged so downstream
        # comparison with "auto" works without type-juggling.
        result = _resolve_gateway_args(_ns(port="1234"))
        assert result["port_override"] == "1234"

    def test_json_ready_alone(self):
        result = _resolve_gateway_args(_ns(json_ready=True))
        assert result["json_ready"] is True
        assert result["port_override"] is None  # not set by --json-ready

    def test_approval_reads_alone(self):
        result = _resolve_gateway_args(_ns(approval="reads"))
        assert result["approval_mode"] == "reads"

    def test_approval_interactive_alone(self):
        result = _resolve_gateway_args(_ns(approval="interactive"))
        assert result["approval_mode"] == "interactive"


class TestPortValidation:
    """`--port` rejects garbage at parse time, not deep in startup."""

    def test_non_numeric_string_rejected(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _resolve_gateway_args(_ns(port="abc"))
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "must be an integer or 'auto'" in captured.err

    def test_float_string_rejected(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _resolve_gateway_args(_ns(port="99.5"))
        assert exc.value.code == 2

    def test_negative_port_rejected(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _resolve_gateway_args(_ns(port="-1"))
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "out of range" in captured.err

    def test_zero_port_rejected(self, capsys):
        # Port 0 means "ephemeral" only via the `auto` keyword. Bare 0 is a typo.
        with pytest.raises(SystemExit) as exc:
            _resolve_gateway_args(_ns(port="0"))
        assert exc.value.code == 2

    def test_port_above_65535_rejected(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _resolve_gateway_args(_ns(port="70000"))
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "out of range" in captured.err

    def test_port_max_accepted(self):
        result = _resolve_gateway_args(_ns(port="65535"))
        assert result["port_override"] == "65535"

    def test_port_min_accepted(self):
        result = _resolve_gateway_args(_ns(port="1"))
        assert result["port_override"] == "1"


# ─── Safety rail: --approval yolo ────────────────────────────────────────


class TestApprovalYoloSafetyRail:
    """`--approval yolo` refuses to run against the default home."""

    def test_yolo_refused_without_kirocrew_home(self, monkeypatch, capsys):
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        with pytest.raises(SystemExit) as exc:
            _resolve_gateway_args(_ns(approval="yolo"))
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "KIROCREW_HOME must be explicitly set" in captured.err

    def test_yolo_refused_when_kirocrew_home_empty_string(self, monkeypatch, capsys):
        monkeypatch.setenv("KIROCREW_HOME", "")
        with pytest.raises(SystemExit) as exc:
            _resolve_gateway_args(_ns(approval="yolo"))
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "KIROCREW_HOME must be explicitly set" in captured.err

    def test_yolo_refused_when_resolves_to_default_home(self, monkeypatch, capsys):
        # Point KIROCREW_HOME at the literal default; rail must catch it.
        monkeypatch.setenv("KIROCREW_HOME", str(Path.home() / ".kiro" / "crew"))
        with pytest.raises(SystemExit) as exc:
            _resolve_gateway_args(_ns(approval="yolo"))
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "main gateway home" in captured.err

    def test_yolo_refused_via_tilde_expansion(self, monkeypatch, capsys):
        # `~/.kiro/crew` expands then resolves to the same path as
        # Path.home() / .kiro / crew.
        monkeypatch.setenv("KIROCREW_HOME", "~/.kiro/crew")
        with pytest.raises(SystemExit) as exc:
            _resolve_gateway_args(_ns(approval="yolo"))
        assert exc.value.code == 2
        # Confirm we hit the same-as-default branch (not the resolve-failure branch).
        captured = capsys.readouterr()
        assert "main gateway home" in captured.err

    def test_yolo_refused_when_resolves_to_legacy_home(self, monkeypatch, capsys):
        # GPT 5.6 HIGH regression: after the data-home move, the rail must reject the
        # LEGACY ~/.kirocrew too, not just ~/.kiro/crew. On an unmigrated/downgraded
        # install the legacy home still holds the LIVE data, so yolo against it is
        # exactly as destructive as against the new home.
        monkeypatch.setenv("KIROCREW_HOME", str(Path.home() / ".kirocrew"))
        with pytest.raises(SystemExit) as exc:
            _resolve_gateway_args(_ns(approval="yolo"))
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "main gateway home" in captured.err

    def test_yolo_accepted_with_isolated_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        result = _resolve_gateway_args(_ns(approval="yolo"))
        assert result["approval_mode"] == "yolo"

    def test_yolo_accepted_via_test_mode_bundle_with_isolated_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        result = _resolve_gateway_args(_ns(test_mode=True, approval="yolo"))
        assert result["approval_mode"] == "yolo"
        assert result["port_override"] == "auto"

    def test_reads_mode_skips_safety_rail(self):
        # Rail only applies to yolo; reads should pass with default home.
        result = _resolve_gateway_args(_ns(approval="reads"))
        assert result["approval_mode"] == "reads"

    def test_interactive_mode_skips_safety_rail(self):
        result = _resolve_gateway_args(_ns(approval="interactive"))
        assert result["approval_mode"] == "interactive"


# ─── _is_read_only_tool helper ───────────────────────────────────────────


class TestIsReadOnlyTool:
    """Tool-name classification used by --approval reads."""

    @pytest.mark.parametrize(
        "title",
        [
            "read_file",
            "Read foo.txt",
            "list_directory",
            "ls /tmp",
            "get_status",
            "search foo",
            "find x",
            "describe table",
            "show config",
            "view file",
            "fetch url",
            "query db",
            "grep -r foo",
            "cat file",
            "head -n 5",
            "tail -f log",
        ],
    )
    def test_known_read_verbs_match(self, title):
        from kiro_crew.slack.gateway import _is_read_only_tool

        assert _is_read_only_tool(title) is True

    @pytest.mark.parametrize(
        "title",
        [
            "write_file",
            "delete record",
            "create table",
            "rm -rf /",
            "shell: rm",
            "execute_command",
            "post_message",
            "update record",
        ],
    )
    def test_write_verbs_do_not_match(self, title):
        from kiro_crew.slack.gateway import _is_read_only_tool

        assert _is_read_only_tool(title) is False

    @pytest.mark.parametrize(
        "title",
        [
            "read_or_write",  # read prefix masking write
            "read_and_delete",  # read prefix masking delete
            "find_and_replace",  # find prefix masking replace
            "search_replace",  # search prefix masking replace
            "get_or_create",  # get prefix masking create
            "list_and_remove",  # list prefix masking remove
            "fetch_and_update",  # fetch prefix masking update
            "query_and_modify",  # query prefix masking modify
        ],
    )
    def test_compound_read_write_verbs_rejected(self, title):
        """Denylist catches tools whose read-verb prefix masks a write capability."""
        from kiro_crew.slack.gateway import _is_read_only_tool

        assert _is_read_only_tool(title) is False

    def test_empty_string_not_match(self):
        from kiro_crew.slack.gateway import _is_read_only_tool

        assert _is_read_only_tool("") is False

    def test_whitespace_only_not_match(self):
        from kiro_crew.slack.gateway import _is_read_only_tool

        assert _is_read_only_tool("   ") is False
        assert _is_read_only_tool("\t\n") is False

    def test_handles_punctuation_separators(self):
        from kiro_crew.slack.gateway import _is_read_only_tool

        # First token before space/colon/underscore/dash/paren counts.
        assert _is_read_only_tool("read(file.txt)") is True
        assert _is_read_only_tool("LIST: stuff") is True

    def test_substring_inside_token_does_not_match(self):
        """`set` token-equality check must not match the longer token `setter`."""
        from kiro_crew.slack.gateway import _is_read_only_tool

        # `read_setter_field` has read prefix; tokens are
        # ["read", "setter", "field"]. None equal an entry in
        # _WRITE_INDICATORS (which lists "set", not "setter").
        assert _is_read_only_tool("read_setter_field") is True
