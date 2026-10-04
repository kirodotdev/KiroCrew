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
    _render_fields,
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


# --------------------------------------------------------- dashboard read tool


class TestDashboardFields:
    """The read that makes a crewmate's first dashboard write usually correct.

    It reports which fields exist, which of them are the caller's to write, and
    which of the caller's own past writes were refused. Each of those is state
    about one crewmate, which is why the tool is behind the same strict gate as
    the publish.
    """

    def test_a_caller_the_gateway_cannot_name_is_refused_before_the_read(self) -> None:
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
            patch("kiro_crew.mcp_panel._get") as mock_get,
        ):
            out = _call_tool_inner("dashboard_fields", {})

        assert out.startswith("Error:")
        assert "subagent" in out.lower()
        mock_get.assert_not_called()

    def test_the_verified_key_reaches_the_gateway_unchanged(self) -> None:
        """The field list is keyed by crew, so re-resolving downstream would answer
        for a different crewmate than the one the gate vetted."""
        with patch("kiro_crew.mcp_panel._get", return_value={"template": None}) as mock_get:
            _call_tool_inner("dashboard_fields", {})

        assert mock_get.call_args.kwargs["session_key"] == GOOD_KEY

    def test_the_fields_come_back_as_prose_the_agent_can_act_on(self) -> None:
        """The payload is rendered rather than returned raw.

        The reader spends context on this, and the JSON's shape is not the
        message: which field is writable, and under what name, is.
        """
        with patch(
            "kiro_crew.mcp_panel._get",
            return_value={
                "template": {"id": "oncall", "version": 3},
                "instance_version": 7,
                "fields": [{"field": "open_items", "type": "number", "source": "agentic"}],
                "mistakes": [],
            },
        ):
            out = _call_tool_inner("dashboard_fields", {})

        assert "oncall" in out
        assert "open_items" in out
        assert "YOURS to write" in out

    def test_a_field_list_carrying_a_credential_comes_back_scrubbed(self) -> None:
        """The prose is built from gateway state this module does not author, so
        the whole rendered answer goes through the redactor, not just a refusal."""
        secret = "_".join(["ghp", "1234567890abcdefghijklmnopqrstuvwxyz"])
        with patch(
            "kiro_crew.mcp_panel._get",
            return_value={
                "template": {"id": "oncall", "version": 3},
                "instance_version": 1,
                "fields": [{"field": secret, "type": "string", "source": "agentic"}],
            },
        ):
            out = _call_tool_inner("dashboard_fields", {})

        assert secret not in out
        assert "REDACTED" in out


# -------------------------------------------------------- dashboard write tool


class TestDashboardWrite:
    """One type-checked value into the caller's own dashboard cell."""

    def test_a_missing_field_name_is_refused_before_the_gateway(self) -> None:
        with patch("kiro_crew.mcp_panel._post") as mock_post:
            out = _call_tool_inner("dashboard_write", {"value": 3})

        assert out.startswith("Error:")
        assert "field" in out
        mock_post.assert_not_called()

    @pytest.mark.parametrize("bad", ["", "   ", None, 42])
    def test_a_field_name_that_names_nothing_is_refused(self, bad: Any) -> None:
        with patch("kiro_crew.mcp_panel._post") as mock_post:
            out = _call_tool_inner("dashboard_write", {"field": bad, "value": 3})

        assert out.startswith("Error:")
        mock_post.assert_not_called()

    def test_an_absent_value_is_refused_rather_than_sent_as_null(self) -> None:
        """A write with no value and a write of ``None`` are different intents.

        Defaulting the absent one to ``None`` would send a null the manifest's
        type check then refuses, spending a round trip on a mistake that is
        answerable here.
        """
        with patch("kiro_crew.mcp_panel._post") as mock_post:
            out = _call_tool_inner("dashboard_write", {"field": "open_items"})

        assert out.startswith("Error:")
        assert "`value` is required" in out
        mock_post.assert_not_called()

    def test_an_explicit_null_reaches_the_gateway(self) -> None:
        """``None`` is a value the caller chose, so the manifest decides it."""
        with patch("kiro_crew.mcp_panel._post", return_value={"written": {}}) as mock_post:
            _call_tool_inner("dashboard_write", {"field": "risk_note", "value": None})

        sent = mock_post.call_args[0][1]
        assert sent == {"field": "risk_note", "value": None}

    def test_a_caller_the_gateway_cannot_name_is_refused_before_the_write(self) -> None:
        """The refusal matters more here than on the read: a write lands in the
        crewmate's own crew log, so a caller resolved to its parent's slot would
        fill a cell on a dashboard its parent never asked it to touch."""
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
            patch("kiro_crew.mcp_panel._post") as mock_post,
        ):
            out = _call_tool_inner("dashboard_write", {"field": "open_items", "value": 3})

        assert out.startswith("Error:")
        assert "subagent" in out.lower()
        mock_post.assert_not_called()

    def test_the_verified_key_reaches_the_gateway_unchanged(self) -> None:
        with patch("kiro_crew.mcp_panel._post", return_value={"written": {}}) as mock_post:
            _call_tool_inner("dashboard_write", {"field": "open_items", "value": 3})

        assert mock_post.call_args.kwargs["session_key"] == GOOD_KEY

    def test_the_target_dashboard_is_never_taken_from_the_arguments(self) -> None:
        """Ownership comes from the vetted session, as it does for a publish."""
        with patch("kiro_crew.mcp_panel._post", return_value={"written": {}}) as mock_post:
            _call_tool_inner(
                "dashboard_write",
                {"field": "open_items", "value": 3, "crew": "someone-else", "slug": "other"},
            )

        sent = mock_post.call_args[0][1]
        assert set(sent) == {"field", "value"}, f"caller-controlled identity in {sent}"

    def test_a_refusal_comes_back_whole(self) -> None:
        """The refusal sentence is the feature.

        It names the valid fields, the type that was wanted, and how many times
        this mistake has been made before. A caller handed a generic failure
        instead guesses again, which is the cycle the mistake book ends.
        """
        refusal = (
            "unknown_field: 'open' is not a field of template 'oncall' (version 3). "
            "Writable fields: open_items, risk_note. You have made this mistake 3 times."
        )
        with patch("kiro_crew.mcp_panel._post", return_value={"error": refusal}):
            out = _call_tool_inner("dashboard_write", {"field": "open", "value": 3})

        assert "unknown_field" in out
        assert "open_items, risk_note" in out
        assert "3 times" in out
        assert "REDACTED" not in out

    def test_a_refusal_carrying_a_credential_is_scrubbed(self) -> None:
        secret = "".join(["AKIA", "IOSFODNN7", "EXAMPLE"])
        with patch(
            "kiro_crew.mcp_panel._post",
            return_value={"error": f"wrong_type: 'risk_note' wants string, got {secret}"},
        ):
            out = _call_tool_inner("dashboard_write", {"field": "risk_note", "value": 1})

        assert secret not in out
        assert "REDACTED" in out

    def test_a_successful_write_reports_the_field_and_type_that_landed(self) -> None:
        """The gateway's own answer, not an echo of the request.

        The manifest decides the stored type, so reporting the argument back
        would hide a value that landed as something other than what was sent.
        """
        with patch(
            "kiro_crew.mcp_panel._post",
            return_value={"ok": True, "written": {"field": "open_items", "type": "number"}},
        ):
            out = _call_tool_inner("dashboard_write", {"field": "open_items", "value": 3})

        assert "open_items" in out
        assert "number" in out
        assert not out.startswith("Error:")

    def test_a_write_that_answers_an_earlier_refusal_says_so(self) -> None:
        """The correction is what tells a crewmate its mistake book shrank."""
        with patch(
            "kiro_crew.mcp_panel._post",
            return_value={
                "ok": True,
                "written": {"field": "open_items", "type": "number"},
                "corrected": True,
            },
        ):
            out = _call_tool_inner("dashboard_write", {"field": "open_items", "value": 3})

        assert "corrected an earlier refused write" in out

    def test_an_ordinary_write_claims_no_correction(self) -> None:
        with patch(
            "kiro_crew.mcp_panel._post",
            return_value={"ok": True, "written": {"field": "open_items", "type": "number"}},
        ):
            out = _call_tool_inner("dashboard_write", {"field": "open_items", "value": 3})

        assert "corrected" not in out

    def test_a_gateway_answer_with_no_written_block_still_names_the_field(self) -> None:
        """The request's own field is the fallback, so a thin answer still tells
        the caller which cell it filled rather than reporting a blank."""
        with patch("kiro_crew.mcp_panel._post", return_value={"ok": True}):
            out = _call_tool_inner("dashboard_write", {"field": "open_items", "value": 3})

        assert "open_items" in out


# ------------------------------------------------------------ field rendering


class TestTheFieldRendering:
    """The prose an agent reads before it writes.

    Exercised directly because each branch is a sentence a reader acts on: a
    missing dashboard sends it to the human, a fold-sourced field tells it not to
    write that one, and the mistake rows tell it what to write instead.
    """

    def test_a_crewmate_with_no_dashboard_is_sent_to_the_human(self) -> None:
        out = _render_fields({"template": None, "fields": []})

        assert "no dashboard yet" in out
        assert "Ask the human" in out
        # No mistake-book line: there is no dashboard for a write to have been
        # refused against, so claiming an empty book would invent a state.
        assert "mistake book" not in out

    @pytest.mark.parametrize("bad", [None, "oncall", 42, ["oncall"]])
    def test_a_template_that_is_not_an_object_reads_as_no_dashboard(self, bad: Any) -> None:
        assert "no dashboard yet" in _render_fields({"template": bad})

    def test_the_header_names_the_template_and_both_versions(self) -> None:
        """The instance version is the crewmate's own copy, which can trail the
        template's -- a reader that saw only one number could not tell."""
        out = _render_fields(
            {"template": {"id": "oncall", "version": 3}, "instance_version": 7, "fields": []}
        )

        assert "oncall" in out
        assert "version 3" in out
        assert "7" in out

    def test_an_agentic_field_is_marked_as_the_callers_to_write(self) -> None:
        out = _render_fields(
            {
                "template": {"id": "oncall", "version": 3},
                "instance_version": 1,
                "fields": [{"field": "open_items", "type": "number", "source": "agentic"}],
            }
        )

        assert "open_items (number) -- YOURS to write" in out

    def test_a_fold_sourced_field_names_its_fold_and_path(self) -> None:
        """Listed rather than hidden, and with its source.

        A crewmate that cannot see the field has no way to know the number is
        already recorded, and will either try to write it or duplicate it under
        an agentic name so the page draws the same quantity twice.
        """
        out = _render_fields(
            {
                "template": {"id": "oncall", "version": 3},
                "instance_version": 1,
                "fields": [
                    {
                        "field": "entries",
                        "type": "number",
                        "source": "fold",
                        "fold": "work",
                        "path": "count",
                    }
                ],
            }
        )

        assert "entries (number)" in out
        assert "work fold at count" in out
        assert "YOURS to write" not in out

    def test_a_malformed_field_row_is_skipped_rather_than_crashing_the_read(self) -> None:
        """The payload comes from the gateway, so one bad row must not cost the
        caller the rows beside it."""
        out = _render_fields(
            {
                "template": {"id": "oncall", "version": 3},
                "instance_version": 1,
                "fields": [
                    "not a row",
                    {"field": "open_items", "type": "number", "source": "agentic"},
                ],
            }
        )

        assert "open_items" in out

    def test_an_empty_field_list_renders_no_field_section(self) -> None:
        out = _render_fields(
            {"template": {"id": "oncall", "version": 3}, "instance_version": 1, "fields": []}
        )

        assert "Fields:" not in out

    def test_a_single_refusal_reads_as_once_rather_than_a_count(self) -> None:
        out = _render_fields(
            {
                "template": {"id": "oncall", "version": 3},
                "instance_version": 1,
                "fields": [],
                "mistakes": [
                    {
                        "count": 1,
                        "code": "unknown_field",
                        "field": "open",
                        "use_instead": "open_items",
                    }
                ],
            }
        )

        assert "once: unknown_field on `open` -- use `open_items` instead" in out
        assert "1 times" not in out

    def test_a_repeated_refusal_leads_with_how_often_it_happened(self) -> None:
        """The count leads so the mistake made five times is the one read first."""
        out = _render_fields(
            {
                "template": {"id": "oncall", "version": 3},
                "instance_version": 1,
                "fields": [],
                "mistakes": [
                    {
                        "count": 5,
                        "code": "wrong_type",
                        "field": "risk_note",
                        "reason": "wants a string",
                    }
                ],
            }
        )

        assert "5 times: wrong_type on `risk_note` -- wants a string" in out

    def test_a_malformed_mistake_row_is_skipped(self) -> None:
        out = _render_fields(
            {
                "template": {"id": "oncall", "version": 3},
                "instance_version": 1,
                "fields": [],
                "mistakes": [None, {"count": 1, "code": "wrong_type", "field": "risk_note"}],
            }
        )

        assert "once: wrong_type on `risk_note`" in out

    def test_an_empty_mistake_book_is_reported_rather_than_left_silent(self) -> None:
        """Silence reads as "the book did not load". Saying it is empty tells the
        caller its past writes were accepted."""
        out = _render_fields(
            {
                "template": {"id": "oncall", "version": 3},
                "instance_version": 1,
                "fields": [],
                "mistakes": [],
            }
        )

        assert "mistake book is empty" in out

    def test_the_retry_budget_tells_the_caller_when_to_stop_guessing(self) -> None:
        out = _render_fields(
            {
                "template": {"id": "oncall", "version": 3},
                "instance_version": 1,
                "fields": [],
                "retry_budget": 3,
            }
        )

        assert "after 3 tries, ask the human" in out

    def test_no_budget_leaves_out_the_retry_advice(self) -> None:
        out = _render_fields({"template": None, "retry_budget": 0})

        assert "Ask the human" in out, "the no-dashboard advice stands on its own"
        assert "tries" not in out


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
