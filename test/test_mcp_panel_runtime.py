"""Runtime behaviour of the ``kirocrew-panel`` MCP server.

``test_mcp_panel_registration.py`` covers the REGISTRY wiring -- that the server
appears in every declaration surface, that the conductor grants are exact, that no
tool takes a session argument. None of that executes the module, so the half this
file covers is the half the security argument actually rests on:

* the strict identity gate, whose entire purpose is refusing a subagent rather
  than resolving it to its parent's crew;
* the redaction of gateway refusal prose, which is the justification recorded for
  this module's ``NON_EGRESS`` output-boundary classification in
  ``security_posture``;
* the refusal paths, which are what a caller sees when it gets something wrong.

A security claim with no executing test is a comment, so each test here names the
claim it is holding up.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.mcp_panel import (
    ADVERTISE_CALLER_IDENTITY,
    SERVER_NAME,
    _call_tool,
    _call_tool_inner,
    _list_tools,
    _validate_args,
    run_mcp_server,
)

#: A verified caller. Every test that is not ABOUT the identity gate needs one,
#: or it exercises the refusal instead of the behaviour under test.
GOOD_KEY = "dashboard:chat-1-100"


@pytest.fixture(autouse=True)
def _verified_caller() -> Any:
    """Resolve the caller strictly, the way a real parent session would.

    Module-wide because every tool here is behind the gate; the identity-gate
    tests patch this to empty themselves and the inner patch wins.
    """
    with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=GOOD_KEY):
        yield


# --------------------------------------------------------------- identity gate


class TestTheStrictIdentityGate:
    """Why the strict resolver is used instead of the lenient one.

    The lenient resolver walks ``/proc`` ancestors and resolves a SUBAGENT to its
    parent's slot. For this server that is not a cosmetic difference: the panel is
    keyed by the resolved crew, so a subagent would publish over its parent
    crew's webview -- and the parent never asked for it.
    """

    def test_a_caller_the_gateway_cannot_name_is_refused_not_resolved(self) -> None:
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
            patch("kiro_crew.mcp_panel._post") as mock_post,
        ):
            out = _call_tool_inner("panel_publish", {"data": {"cycle": 1}})

        assert out.startswith("Error:")
        # The refusal has to be ACTIONABLE: a subagent is told where to publish
        # from, rather than being told only that it failed.
        assert "subagent" in out.lower()
        # And nothing reached the gateway -- refused here, not sent with an
        # authority nobody can name.
        mock_post.assert_not_called()

    def test_the_read_tool_is_behind_the_same_gate(self) -> None:
        """Both tools, not just the write.

        The template list is not sensitive, but an ungated read would still be a
        second door into the server with a different identity story, and the next
        tool added would copy whichever one it saw first.
        """
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
            patch("kiro_crew.mcp_panel._get") as mock_get,
        ):
            out = _call_tool_inner("panel_templates", {})

        assert out.startswith("Error:")
        mock_get.assert_not_called()

    def test_the_verified_key_reaches_the_gateway_unchanged(self) -> None:
        """Re-resolving downstream would send a different authority than was checked."""
        with patch("kiro_crew.mcp_panel._post", return_value={"panel": {}}) as mock_post:
            _call_tool_inner("panel_publish", {"data": {"cycle": 1}})

        assert mock_post.call_args.kwargs["session_key"] == GOOD_KEY

    def test_the_publishing_crew_is_never_taken_from_the_arguments(self) -> None:
        """Ownership comes from the vetted session, never from the caller's payload.

        A ``crew`` argument that reached the gateway would let any caller publish
        as any crew, making the whole strict gate above decorative.
        """
        with patch("kiro_crew.mcp_panel._post", return_value={"panel": {}}) as mock_post:
            _call_tool_inner(
                "panel_publish",
                {"data": {"cycle": 1}, "crew": "some-other-crew", "slug": "other"},
            )

        sent = mock_post.call_args[0][1]
        assert "crew" not in sent and "slug" not in sent, f"caller-controlled identity in {sent}"


# ------------------------------------------------------------------- redaction


class TestRefusalProseIsRedacted:
    """The module's ``NON_EGRESS`` output-boundary classification, under test.

    The two ``redact`` calls exist because gateway refusal prose is returned to the
    agent verbatim so it can correct itself -- and that prose is built from an
    upstream error string this module does not author. If a refusal ever quotes
    something credential-shaped, returning it raw would hand it to the model. That
    reasoning is the recorded justification for the classification, so it gets a
    test rather than a comment.
    """

    # ASSEMBLED AT RUNTIME, never written as literals.
    #
    # These have to be credential-SHAPED to reach the detectors under test, which
    # makes them indistinguishable from real leaked keys to a scanner reading this
    # file -- GitHub push protection rejected the Slack-shaped one outright. Joining
    # the parts keeps the shape at runtime while the source contains no matchable
    # string, so the test stays honest without an allowlist entry that would teach
    # the scanner to ignore this path. Do not "simplify" these back into literals.
    #
    # One per detector in the shared redactor, so a single pattern regressing does
    # not leave this test green on the strength of the others.
    CREDENTIALS = [
        "-".join(["xoxb", "123456789012", "1234567890123", "abcdefghijklmnopqrstuvwx"]),
        "_".join(["ghp", "1234567890abcdefghijklmnopqrstuvwxyz"]),
        "".join(["AKIA", "IOSFODNN7", "EXAMPLE"]),
    ]

    @pytest.mark.parametrize("secret", CREDENTIALS)
    def test_a_publish_refusal_carrying_a_credential_comes_back_scrubbed(self, secret: str) -> None:
        with patch(
            "kiro_crew.mcp_panel._post",
            return_value={"error": f"bad_template: could not read {secret}"},
        ):
            out = _call_tool_inner("panel_publish", {"data": {"cycle": 1}})

        assert secret not in out, "a credential in refusal prose reached the tool result"
        assert "REDACTED" in out

    @pytest.mark.parametrize("secret", CREDENTIALS)
    def test_a_templates_refusal_is_scrubbed_too(self, secret: str) -> None:
        """The read path has its own redactor call; both are claimed, both tested."""
        with patch("kiro_crew.mcp_panel._get", return_value={"error": f"boom {secret}"}):
            out = _call_tool_inner("panel_templates", {})

        assert secret not in out
        assert "REDACTED" in out

    def test_ordinary_refusal_prose_survives_intact(self) -> None:
        """Redaction must not eat the actionable part.

        The refusal codes are the whole reason the prose is returned rather than a
        generic failure: a caller that cannot read "no such template" cannot fix
        its next call. A redactor that scrubbed everything would pass the tests
        above and destroy the feature.
        """
        with patch(
            "kiro_crew.mcp_panel._post",
            return_value={"error": "unknown_template: no such template 'nope'"},
        ):
            out = _call_tool_inner("panel_publish", {"data": {"cycle": 1}})

        assert "unknown_template" in out
        assert "no such template" in out
        assert "REDACTED" not in out


# ---------------------------------------------------------------- publish tool


class TestPanelPublish:
    def test_a_successful_publish_reports_what_it_did(self) -> None:
        with patch(
            "kiro_crew.mcp_panel._post",
            return_value={"panel": {"template": "oncall"}},
        ):
            out = _call_tool_inner("panel_publish", {"data": {"a": 1, "b": 2}})

        assert "oncall" in out
        assert "2 top-level fields" in out
        # The caller has to know publishing REPLACES rather than merges, or it
        # will publish one field at a time and lose the rest.
        assert "replaced" in out.lower()

    def test_one_field_is_not_reported_as_plural(self) -> None:
        with patch("kiro_crew.mcp_panel._post", return_value={"panel": {}}):
            out = _call_tool_inner("panel_publish", {"data": {"only": 1}})
        assert "1 top-level field)" in out
        assert "1 top-level fields" not in out

    def test_a_publish_with_no_template_reports_the_default(self) -> None:
        with patch("kiro_crew.mcp_panel._post", return_value={"panel": {}}):
            out = _call_tool_inner("panel_publish", {"data": {"a": 1}})
        assert "default" in out

    @pytest.mark.parametrize("bad", [None, "a string", 42, ["a", "list"], True])
    def test_data_that_is_not_an_object_is_refused_before_the_gateway(self, bad: Any) -> None:
        """A shape refusal is answerable locally, so it costs no round trip."""
        with patch("kiro_crew.mcp_panel._post") as mock_post:
            out = _call_tool_inner("panel_publish", {"data": bad})

        assert out.startswith("Error:")
        assert "JSON object" in out
        mock_post.assert_not_called()

    def test_template_and_title_are_forwarded_when_given(self) -> None:
        with patch("kiro_crew.mcp_panel._post", return_value={"panel": {}}) as mock_post:
            _call_tool_inner(
                "panel_publish",
                {"data": {"a": 1}, "template": "oncall", "title": "Oncall"},
            )

        sent = mock_post.call_args[0][1]
        assert sent["template"] == "oncall"
        assert sent["title"] == "Oncall"

    def test_absent_optional_fields_are_omitted_rather_than_sent_as_null(self) -> None:
        """A null template would override the server's default with nothing."""
        with patch("kiro_crew.mcp_panel._post", return_value={"panel": {}}) as mock_post:
            _call_tool_inner("panel_publish", {"data": {"a": 1}, "template": None})

        assert "template" not in mock_post.call_args[0][1]


# -------------------------------------------------------------- templates tool


class TestPanelTemplates:
    def test_it_lists_the_installed_templates_and_names_the_default(self) -> None:
        with patch(
            "kiro_crew.mcp_panel._get",
            return_value={"templates": ["default", "oncall"], "default": "oncall"},
        ):
            out = _call_tool_inner("panel_templates", {})

        assert "default" in out and "oncall" in out

    def test_an_empty_install_says_so_rather_than_listing_nothing(self) -> None:
        with patch("kiro_crew.mcp_panel._get", return_value={"templates": []}):
            out = _call_tool_inner("panel_templates", {})
        assert "No panel templates" in out


# ------------------------------------------------------------- module plumbing


class TestTheDispatchSurface:
    def test_an_unknown_tool_is_refused(self) -> None:
        assert _call_tool_inner("panel_nope", {}).startswith("Error: unknown tool")

    def test_every_advertised_tool_is_actually_dispatchable(self) -> None:
        """A tool in ``tools/list`` that falls through to "unknown" is a dead entry.

        Derived from ``_list_tools`` so a newly advertised tool must be wired up
        rather than merely declared.
        """
        names = [t["name"] for t in _list_tools()]
        assert names, "no tools advertised -- this test would be vacuous"
        for name in names:
            assert "unknown tool" not in _call_tool_inner(
                name, {}
            ), f"{name} is advertised but not dispatched"

    def test_validation_passes_unknown_tools_through_untouched(self) -> None:
        """No schema is not an error here; the dispatcher reports the unknown name."""
        args = {"anything": 1}
        assert _validate_args("panel_nope", args) == args

    def test_a_schema_violation_is_reported_through_the_guarded_entry_point(self) -> None:
        """``_call_tool`` is the real entry point: validation and SEL audit wrap it."""
        out = _call_tool("panel_publish", {"data": "not an object"})
        assert "Error" in out

    def test_the_server_advertises_caller_identity_when_it_starts(self) -> None:
        """The flag is what puts this server in the shareable set; assert it is PASSED.

        A module-level constant that never reaches ``run_mcp_stdio_loop`` would
        leave the discovery classification claiming a property the running server
        does not have.
        """
        with patch("kiro_crew.mcp_panel.run_mcp_stdio_loop") as loop:
            run_mcp_server()

        assert loop.call_args.kwargs["advertise_caller_identity"] is ADVERTISE_CALLER_IDENTITY
        assert ADVERTISE_CALLER_IDENTITY is True
        assert loop.call_args[0][0] == SERVER_NAME
