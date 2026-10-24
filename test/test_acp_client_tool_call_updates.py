"""Tests for ACP client: tool_call_update extraction and refinement, result content shapes and the
is_shell signal.
"""

import hashlib

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

import kiro_crew.acp.client as acp_client
from kiro_crew.acp.client import AcpClient

# ── _extract_tool_call_update tests ──


class TestExtractToolCallUpdate:
    """Tests for real-time tool result extraction from session updates."""

    def _make_msg(self, update):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(params={"update": update})

    def _client(self):
        return AcpClient()

    def test_ignores_non_tool_call_update(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = self._client()
        msg = JsonRpcMessage(params={"update": {"sessionUpdate": "other"}})
        assert client._extract_tool_call_update(msg) is None

    def test_ignores_missing_tool_call_id(self):
        client = self._client()
        msg = self._make_msg({"sessionUpdate": "tool_call_update", "toolCallId": ""})
        assert client._extract_tool_call_update(msg) is None

    def test_content_blocks_path(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-1",
                "content": [
                    {"content": {"type": "text", "text": "hello world"}},
                ],
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert event.kind == "tool_result"
        assert event.tool_call_id == "tc-1"
        assert "hello world" in event.tool_output

    @pytest.mark.parametrize(
        ("tool_call_id", "json_item", "expected"),
        [
            pytest.param(
                "tc-2", {"stdout": "ls output here"}, "ls output here", id="raw_output_stdout_path"
            ),
            pytest.param("tc-3", {"key": "value"}, "key", id="raw_output_json_fallback"),
        ],
    )
    def test_raw_output_json_item(self, tool_call_id, json_item, expected):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "rawOutput": {
                    "items": [{"Json": json_item}],
                },
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert expected in event.tool_output

    def test_flat_raw_output_is_not_read_as_no_output(self):
        """A captured KAS completion whose only carrier is a flat ``rawOutput``.

        Mirrors ``_dispatch._build_tool_result_event``'s third path. Returning
        None here also leaves the stall watchdog armed and PostToolUse unfired,
        so the dropped event costs more than the transcript text.
        """
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-kas",
                "status": "completed",
                "rawOutput": {"kind": "notEnabled", "retracted": False},
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert event.tool_call_id == "tc-kas"
        assert event.tool_final is True
        assert "notEnabled" in event.tool_output

    def test_flat_raw_output_loses_to_a_content_block(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-kas2",
                "status": "completed",
                "content": [{"content": {"type": "text", "text": "real output"}}],
                "rawOutput": {"output": "real output", "exitCode": 0},
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert event.tool_output == "real output"
        assert "exitCode" not in event.tool_output

    def test_outputless_terminal_updates_return_status_only_results(self):
        client = self._client()
        for status in ("completed", "failed"):
            for shape in ({"items": []}, {"items": [{"Text": ""}]}, {}):
                msg = self._make_msg(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc-empty",
                        "status": status,
                        "rawOutput": shape,
                    }
                )
                event = client._extract_tool_call_update(msg)
                assert (
                    event is not None
                ), f"terminal status {status} was discarded without a result event"
                assert event.tool_status == status
                assert event.tool_final is (status == "completed")
                assert event.tool_output == "", "a status-only result invented tool output"

    def test_outputless_nonterminal_update_returns_none(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-empty",
                "status": "in_progress",
                "rawOutput": {"items": []},
            }
        )
        assert client._extract_tool_call_update(msg) is None

    def test_credential_straddling_the_bound_is_still_redacted(self):
        """The 8000-char bound must be applied AFTER redaction, not before.

        A cut taken first splits a connection URI into fragments the credential
        prefilter (``://user:pass@``) does not match: the head slice keeps
        ``://admin:<password>`` and drops the ``@``, so the password would reach
        the dashboard in clear text. The padding here puts the ``@`` exactly on
        byte 8000 of the serialised output, which is the worst case.
        """
        import json as _json

        secret = "sUp3rS3cr3tPassw0rd"
        uri = f"postgres://admin:{secret}@db.internal:5432/prod"
        pad = 7900
        while True:
            raw = {"pad": "A" * pad, "conn": uri}
            if _json.dumps(raw, default=str).index("@") >= 8000:
                break
            pad += 1
        assert _json.dumps(raw, default=str).index("@") == 8000

        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-cred",
                "status": "completed",
                "rawOutput": raw,
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert secret not in event.tool_output
        assert len(event.tool_output) <= 8000

    def test_long_output_metadata_covers_full_redacted_text(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
        output = "A" * 8000 + "é-tail"
        full_redacted = acp_client.redact_text(output)
        full_bytes = full_redacted.encode("utf-8", "replace")
        prefix_bytes = full_redacted[:8000].encode("utf-8", "replace")

        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-long",
                "status": "completed",
                "rawOutput": {"items": [{"Text": output}]},
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert event.tool_output == full_redacted[:8000]
        assert event.tool_output_bytes == len(
            full_bytes
        ), f"parser recorded {event.tool_output_bytes} bytes from truncated output"
        assert event.tool_output_bytes != len(prefix_bytes)
        assert (
            event.tool_output_digest == hashlib.sha256(full_bytes).hexdigest()
        ), "parser digested truncated display prefix instead of full redacted output"

    def test_no_output_metadata_is_measured_while_the_ledger_is_off(self, monkeypatch):
        """With the ledger off the pair is absent, not a measurement of nothing.

        The digest and byte count have one consumer, the flag-gated emitter, and a
        tool result is as large as a file the model just printed -- so hashing one
        while nothing will read it is work the default path must not do. ``-1``
        distinguishes "not recorded" from a real zero-length output.
        """
        monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-off",
                "status": "completed",
                "rawOutput": {"items": [{"Text": "measured-only-when-on"}]},
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert event.tool_output == "measured-only-when-on"
        assert event.tool_output_bytes == -1, "the parser measured with the ledger off"
        assert event.tool_output_digest == "", "the parser digested with the ledger off"

    def test_raw_output_text_path(self):
        # fs_read / shell-style tools land their output in items[].Text —
        # this is the common case for native sub-agent read/summary tools.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-text",
                "status": "completed",
                "rawOutput": {
                    "items": [{"Text": "file contents line 1\nline 2"}],
                },
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert event.tool_call_id == "tc-text"
        assert "file contents line 1" in event.tool_output
        assert event.tool_final is True

    def test_raw_output_text_priority_over_json(self):
        # When an item carries both Text and Json, Text wins (it's the
        # human-readable rendering kiro-cli already produced).
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-both",
                "rawOutput": {
                    "items": [{"Text": "rendered text", "Json": {"stdout": "raw json"}}],
                },
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert "rendered text" in event.tool_output
        assert "raw json" not in event.tool_output

    def test_raw_output_skips_empty_text(self):
        # Empty Text must not short-circuit to a useless empty result; an
        # item with empty Text and no Json yields no output.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-empty",
                "rawOutput": {"items": [{"Text": ""}]},
            }
        )
        assert client._extract_tool_call_update(msg) is None

    def test_content_takes_priority_over_raw(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-4",
                "content": [{"content": {"type": "text", "text": "from content"}}],
                "rawOutput": {"items": [{"Json": {"stdout": "from raw"}}]},
            }
        )
        event = client._extract_tool_call_update(msg)
        assert "from content" in event.tool_output
        assert "from raw" not in event.tool_output

    def test_empty_content_falls_through_to_raw(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-5",
                "content": [],
                "rawOutput": {"items": [{"Json": {"stdout": "fallback"}}]},
            }
        )
        event = client._extract_tool_call_update(msg)
        assert "fallback" in event.tool_output

    def test_no_output_returns_none(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-6",
                "content": [],
                "rawOutput": {"items": []},
            }
        )
        assert client._extract_tool_call_update(msg) is None

    def test_output_truncated_to_8000(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-7",
                "content": [
                    {"content": {"type": "text", "text": "x" * 5000}},
                    {"content": {"type": "text", "text": "y" * 5000}},
                ],
            }
        )
        event = client._extract_tool_call_update(msg)
        assert len(event.tool_output) <= 8000

    def test_redaction_applied(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-8",
                "content": [
                    {"content": {"type": "text", "text": "key=AKIAIOSFODNN7EXAMPLE secret"}},
                ],
            }
        )
        event = client._extract_tool_call_update(msg)
        assert "AKIAIOSFODNN7EXAMPLE" not in event.tool_output

    def test_ignores_non_dict_content_blocks(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-9",
                "content": ["not a dict", None, 42],
            }
        )
        assert client._extract_tool_call_update(msg) is None

    def test_ignores_non_text_content_type(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-10",
                "content": [{"content": {"type": "image", "url": "http://x"}}],
            }
        )
        assert client._extract_tool_call_update(msg) is None

    def test_none_params(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = self._client()
        msg = JsonRpcMessage(params=None)
        assert client._extract_tool_call_update(msg) is None

    def test_bare_text_block_in_content_is_accepted(self):
        # ACP wraps the ContentBlock: {"type": "content", "content": {...}}.
        # Real backends also send the ContentBlock BARE, which is unambiguous —
        # dropping it left the dashboard with nothing and it fell through to
        # "No input or output captured for this tool call."
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-bare",
                "content": [{"type": "text", "text": "bare block output"}],
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert "bare block output" in event.tool_output

    def test_unrenderable_content_entries_are_logged_with_shape(self, caplog):
        # An unknown entry shape must not vanish with no diagnostic anywhere, or
        # the backend author has no way to see the mismatch. One warning naming
        # the shape — TYPE and KEY names only, never a value: this lands in the
        # log ring /api/logs serves and a tool result can carry a credential.
        import logging

        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-unknown",
                "content": [{"type": "resource_link", "uri": "file:///etc/secret.txt"}],
            }
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            assert client._extract_tool_call_update(msg) is None
        hits = [r.getMessage() for r in caplog.records if "could be rendered" in r.getMessage()]
        assert len(hits) == 1
        assert "resource_link" in hits[0]
        assert "secret.txt" not in hits[0]

    def test_content_free_frame_is_not_warned_about(self, caplog):
        # claude-agent-acp emits an initial tool_call with neither rawInput nor
        # content and refines it later; warning on that would fire on every
        # tool call of a healthy backend.
        import logging

        client = self._client()
        msg = self._make_msg({"sessionUpdate": "tool_call_update", "toolCallId": "tc-quiet"})
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            assert client._extract_tool_call_update(msg) is None
        assert [r for r in caplog.records if "could be rendered" in r.getMessage()] == []


# ── non-canonical tool-call content shapes on the dispatch path ──


class TestDispatchToolResultContentShapes:
    """`_build_tool_result_event` walks the same `content` array as
    `AcpClient._extract_tool_call_update`, so it must be equally lenient —
    a backend's shape is not a function of which of the two loops read it."""

    def test_bare_text_block_in_content_is_accepted(self):
        from kiro_crew.acp._dispatch import _build_tool_result_event

        event = _build_tool_result_event(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-bare-dispatch",
                "content": [{"type": "text", "text": "bare on the dispatch path"}],
            }
        )
        assert event is not None
        assert "bare on the dispatch path" in event.tool_output

    def test_unrenderable_content_entries_are_logged_with_shape(self, caplog):
        import logging

        from kiro_crew.acp._dispatch import _build_tool_result_event

        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp._dispatch"):
            assert (
                _build_tool_result_event(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc-unknown-dispatch",
                        "content": [{"kind": "text", "value": "wrong key names"}],
                    }
                )
                is None
            )
        hits = [r.getMessage() for r in caplog.records if "could be rendered" in r.getMessage()]
        assert len(hits) == 1
        assert "kind" in hits[0]
        assert "wrong key names" not in hits[0]

    def test_shape_diagnostics_and_tool_id_are_redacted(self, caplog):
        """The warning lands in the ``/api/logs`` ring, which outlives the
        transcript's own redaction. ``type`` and the tool id are frame-supplied
        VALUES, not just key names, so a hostile or careless backend can put a
        credential in either -- both must be scrubbed before they are logged."""
        import logging

        from kiro_crew.acp._dispatch import _build_tool_result_event

        # Split so this file holds no literal-looking key; matches
        # (?:AKIA|ASIA)[A-Z0-9]{16}, the same shape as
        # test_core_path_redact_before_bound.
        token = "AKIA" + "SHAPELEAK0123456"

        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp._dispatch"):
            assert (
                _build_tool_result_event(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": f"tc-{token}",
                        "content": [{"type": token, "text": "unrecognised entry type"}],
                    }
                )
                is None
            )
        hits = [r.getMessage() for r in caplog.records if "could be rendered" in r.getMessage()]
        assert len(hits) == 1
        assert token not in hits[0]
        assert "[REDACTED" in hits[0]

    def test_a_newline_bearing_tool_id_cannot_forge_a_log_line(self, caplog):
        """The id is frame-supplied, so a newline in it would let a backend append
        its own lines to the gateway log the dashboard renders. ``%r`` keeps the
        whole id on one line with the newline escaped."""
        import logging

        from kiro_crew.acp._dispatch import _build_tool_result_event

        forged = "tc-1\nWARNING kiro_crew: drive deleted by owner"

        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp._dispatch"):
            assert (
                _build_tool_result_event(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": forged,
                        "content": [{"kind": "no-renderable-entry"}],
                    }
                )
                is None
            )
        hits = [r.getMessage() for r in caplog.records if "could be rendered" in r.getMessage()]
        assert len(hits) == 1
        # The escaped form is present and the raw newline is not, so the forged
        # text cannot start a line of its own.
        assert "\\n" in hits[0]
        assert "drive deleted by owner" not in hits[0].splitlines()[1:]
        assert "\n" not in hits[0]

    def test_every_documented_known_content_type_is_renderable_or_silent(self):
        """The constant's docstring names four entry types as understood. If one is
        dropped from the frozenset a real backend's frame starts warning, so pin
        the documented set rather than trusting the comment above it."""
        from kiro_crew.acp._dispatch import _KNOWN_TOOL_CONTENT_TYPES, unrenderable_content_shapes

        assert _KNOWN_TOOL_CONTENT_TYPES == frozenset({"content", "diff", "terminal", "text"})
        for known in sorted(_KNOWN_TOOL_CONTENT_TYPES):
            assert unrenderable_content_shapes([{"type": known}]) == "", known
        assert unrenderable_content_shapes([{"type": "not-a-known-type"}]) != ""

    def test_a_numeric_tool_id_does_not_crash_the_turn(self, caplog):
        """``toolCallId`` is whatever JSON the backend sent, so it need not be a
        string. Passing an int straight into the redactor's regexes raises
        ``TypeError`` -- and this is a diagnostic warning, which must never be able
        to abort the turn it is reporting on."""
        import logging

        from kiro_crew.acp._dispatch import _build_tool_result_event, _loggable_request_id

        assert _loggable_request_id(1234) == "1234"

        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp._dispatch"):
            assert (
                _build_tool_result_event(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": 1234,
                        "content": [{"kind": "unrecognised"}],
                    }
                )
                is None
            )
        assert [r for r in caplog.records if "could be rendered" in r.getMessage()]

    def test_both_diagnostics_are_bounded(self):
        """Both strings are unbounded backend input feeding a RETAINED log-ring
        entry, so a frame that pads them cannot be held in memory in full. Bounds
        are applied AFTER redaction, never before -- a cut taken first can split a
        credential into fragments no pattern matches."""
        from kiro_crew.acp._dispatch import (
            _REQUEST_ID_LOG_CAP,
            _loggable_request_id,
            unrenderable_content_shapes,
        )

        # Under the input cap and left intact by the redactor (a single repeated
        # letter matches no credential pattern), so the display slice is what
        # bounds it -- pinned by equality.
        assert len(_loggable_request_id("t" * 3000)) == _REQUEST_ID_LOG_CAP
        # Over the input cap the value is replaced by the length-only marker.
        assert _loggable_request_id("t" * 100_000).startswith("<id too long: ")
        padded = [{"type": f"x{i}", "pad": "y" * 200} for i in range(2000)]
        assert len(unrenderable_content_shapes(padded)) == 4000

    def test_collection_stops_early_rather_than_building_the_whole_join(self):
        """The final ``[:4000]`` alone still VISITS every entry and allocates the
        full join before cutting, so a near-limit frame could OOM the gateway on the
        way to a bounded string.

        Asserted by counting entry visits, not by inspecting the output: a
        length/content assertion passes either way, because the tail is cut off
        regardless of whether the loop broke early. Counting is what discriminates.
        """
        from kiro_crew.acp._dispatch import unrenderable_content_shapes

        visits = 0

        class _CountingEntry(dict):
            def get(self, *args, **kwargs):  # type: ignore[override]
                nonlocal visits
                visits += 1
                return super().get(*args, **kwargs)

        blocks = [_CountingEntry({"type": f"unknown-{i:06d}"}) for i in range(50_000)]
        out = unrenderable_content_shapes(blocks)
        assert len(out) <= 4000
        assert "unknown-000000" in out
        # ~4000 chars of shapes at ~40 chars each is a couple of hundred entries;
        # anything near 50_000 means the loop consumed the whole frame.
        assert visits < 1000, f"visited {visits} entries — the budget did not stop collection"

    def test_an_object_valued_type_never_renders_its_nested_values(self):
        """``type`` is frame-supplied and need not be a string. ``repr()`` of a dict
        prints its nested VALUES, so a credential parked one level down would reach
        the log ring through the very diagnostic that promises names only. A
        non-string type renders by CLASS instead."""
        from kiro_crew.acp._dispatch import unrenderable_content_shapes

        token = "AKIA" + "NESTEDLEAK012345"
        out = unrenderable_content_shapes([{"type": {"secret": token}, "other": 1}])
        assert token not in out
        assert "secret" not in out
        assert "type=<dict>" in out
        # Key NAMES are still reported -- that is the diagnostic's whole purpose.
        assert "other" in out

    def test_a_key_flood_is_truncated_structurally_not_mid_name(self):
        """Per-entry key rendering is capped by DROPPING whole names, never by
        cutting through one: a cut ahead of redaction could leave half a credential
        that no pattern matches."""
        from kiro_crew.acp._dispatch import unrenderable_content_shapes

        entry = {"type": "unknown"} | {f"k{i:04d}": 1 for i in range(500)}
        out = unrenderable_content_shapes([entry])
        assert "more" in out
        assert "k0499" not in out

    def test_shapes_are_joined_with_one_separator(self):
        """Two unrecognised entries render as two shapes in one string.

        Deliberately does NOT claim to prove the redact-then-join ORDERING: a
        separator count passes for a per-shape implementation too, so an assertion
        of that shape discriminates nothing. The ordering is enforced by the
        function returning a single joined string, not by a test.
        """
        from kiro_crew.acp._dispatch import unrenderable_content_shapes

        joined = unrenderable_content_shapes([{"type": "one"}, {"type": "two"}])
        assert "type='one'" in joined and "type='two'" in joined

    def test_a_wrapped_entry_with_a_malformed_inner_block_is_reported(self):
        """A known outer ``type`` is not evidence the entry renders.

        ``{"type": "content", "content": {"kind": "text", ...}}`` -- ``kind`` where
        the reader wants ``type`` -- renders nothing, and checked only at the outer
        level it said nothing either: the same undiagnosable blank the shape
        diagnostic exists to eliminate, one level down.
        """
        from kiro_crew.acp._dispatch import unrenderable_content_shapes

        out = unrenderable_content_shapes(
            [{"type": "content", "content": {"kind": "text", "text": "AKIAIOSFODNN7SECRET"}}]
        )
        assert out, "a wrapped entry with an unreadable inner block must be reported"
        assert "inner_type=<NoneType>" in out
        assert "'kind'" in out and "'text'" in out
        # Key NAMES only at the inner level too -- this lands in the log ring.
        assert "AKIAIOSFODNN7SECRET" not in out

    def test_a_wrapped_inner_block_carrying_no_text_stays_silent(self):
        """The negative control for the inner check, and the reason it is an
        allowlist rather than "anything but text".

        An image, audio or resource block renders no text and is NOT a defect, so
        reporting it would fire this warning on a healthy backend -- the exact
        false positive `test_content_free_frame_is_not_warned_about` guards at the
        frame level.
        """
        from kiro_crew.acp._dispatch import unrenderable_content_shapes

        for kind in ("text", "image", "audio", "resource", "resource_link"):
            entry = {"type": "content", "content": {"type": kind}}
            assert unrenderable_content_shapes([entry]) == "", f"{kind} must stay silent"

    def test_a_wrapper_with_no_inner_block_stays_silent_but_an_explicit_null_does_not(self):
        """The line the inner check draws, pinned in both directions.

        A ``content`` key that is ABSENT asserts no block, so it stays silent --
        that is the bare wrapper `_KNOWN_TOOL_CONTENT_TYPES` documents as
        understood, and it is why the check cannot be a plain ``.get("content")``,
        which would collapse this case into the null one below.
        """
        from kiro_crew.acp._dispatch import unrenderable_content_shapes

        assert unrenderable_content_shapes([{"type": "content"}]) == ""
        assert unrenderable_content_shapes([{"type": "content", "content": None}]) != ""

    def test_a_wrapped_non_dict_inner_renders_by_class_not_value(self):
        """``{"type": "content", "content": "<secret>"}`` is unreadable too, and the
        inner is not a dict, so there are no key names -- only its CLASS may be
        named, never the string itself."""
        from kiro_crew.acp._dispatch import unrenderable_content_shapes

        out = unrenderable_content_shapes([{"type": "content", "content": "AKIAIOSFODNN7SECRET"}])
        assert "inner=<str>" in out
        assert "AKIAIOSFODNN7SECRET" not in out


# ── _extract_tool_call_refinement tests ──


class TestExtractToolCallRefinement:
    """claude-agent-acp emits a follow-up tool_call_update once the streamed
    tool input is complete; the refinement carries title / kind / rawInput."""

    def _make_msg(self, update):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(params={"update": update})

    def _client(self):
        return AcpClient()

    def test_ignores_non_tool_call_update(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = self._client()
        msg = JsonRpcMessage(params={"update": {"sessionUpdate": "other"}})
        assert client._extract_tool_call_refinement(msg) is None

    @pytest.mark.parametrize(
        "tool_call_id",
        [
            pytest.param("", id="ignores_missing_tool_call_id"),
            # Empty update with just tool_call_id should not emit a refinement.
            pytest.param("tc-6", id="no_refinement_fields_returns_none"),
        ],
    )
    def test_no_refinement(self, tool_call_id):
        client = self._client()
        msg = self._make_msg({"sessionUpdate": "tool_call_update", "toolCallId": tool_call_id})
        assert client._extract_tool_call_refinement(msg) is None

    def test_pure_output_update_returns_none(self):
        # tool_call_update carrying ONLY content (no title/kind/rawInput) is
        # the result-only path handled by _extract_tool_call_update.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-1",
                "content": [{"content": {"type": "text", "text": "out"}}],
            }
        )
        assert client._extract_tool_call_refinement(msg) is None

    def test_refines_title_and_kind(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2",
                "title": "ls /tmp",
                "kind": "execute",
                "rawInput": {"command": "ls /tmp"},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.kind == "tool_call_update"
        assert event.tool_call_id == "tc-2"
        assert event.title == "ls /tmp"
        assert event.tool_kind == "execute"
        assert "ls /tmp" in event.tool_input

    def test_prefers_rawinput_description_over_title(self):
        # Bash tool emits both `command` and `description` — the description
        # is the human-readable purpose ("List Kiro Crew dashboard module
        # files"), and that's what we surface on the pill.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2b",
                "title": "ls /workplace/.../dashboard/",
                "kind": "execute",
                "rawInput": {
                    "command": "ls /workplace/.../dashboard/",
                    "description": "List Kiro Crew dashboard module files",
                },
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.title == "List Kiro Crew dashboard module files"

    def test_generic_shell_title_yields_the_command(self):
        # A backend whose shell `title` is a kind label ("Run Command") names no
        # command; every pill in the transcript would read the same. The command
        # is the ground truth of the call, so it is what the pill shows.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2d",
                "title": "Run Command",
                "kind": "execute",
                "rawInput": {"command": "git status"},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.title == "git status"

    def test_kindless_refinement_inherits_the_shell_classification(self):
        # `kind` is optional on an update. Reading its absence as "not shell"
        # repainted the pill with the generic title the initial tool_call had
        # already resolved to a command, so the cached classification decides.
        client = self._client()
        call = self._make_msg(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc-2e",
                "title": "Run Command",
                "kind": "execute",
                "rawInput": {"command": "git status"},
            }
        )
        assert client._extract_tool_event(call).title == "git status"
        refinement = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2e",
                "title": "Run Command",
                "rawInput": {"command": "git status"},
            }
        )
        event = client._extract_tool_call_refinement(refinement)
        assert event is not None
        assert event.title == "git status"

    def test_title_only_refinement_reads_the_cached_params(self):
        # A refinement can repeat the title without resending rawInput; the
        # command then has to come from the params the initial call cached.
        client = self._client()
        call = self._make_msg(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc-2f",
                "title": "Run Command",
                "kind": "execute",
                "rawInput": {"command": "git status"},
            }
        )
        client._extract_tool_event(call)
        refinement = self._make_msg(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-2f", "title": "Run Command"}
        )
        event = client._extract_tool_call_refinement(refinement)
        assert event is not None
        assert event.title == "git status"

    def test_blank_description_falls_back_to_title(self):
        # Whitespace-only description shouldn't override a useful title.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2c",
                "title": "ls /tmp",
                "rawInput": {"command": "ls /tmp", "description": "   "},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.title == "ls /tmp"

    def test_caches_input_for_permission_lookup(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-3",
                "title": "grep foo",
                "rawInput": {"pattern": "foo"},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        # The refined input is also cached so a later permission request
        # for the same tool_call_id can pick it up.
        assert client._tool_call_inputs.get("tc-3") == event.tool_input

    def test_diff_content_block_replaces_raw_input(self):
        # Edit-style tools send the diff in content; the refinement should
        # surface the unified diff instead of the raw input dict.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-4",
                "title": "Edit foo.py",
                "kind": "edit",
                "rawInput": {"old_string": "old", "new_string": "new", "file_path": "foo.py"},
                "content": [
                    {
                        "type": "diff",
                        "path": "foo.py",
                        "oldText": "old",
                        "newText": "new",
                    },
                ],
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        # _make_unified_diff prefixes file headers
        assert "foo.py" in event.tool_input
        assert event.diff_path == "foo.py"

    def test_redacts_credentials_in_input(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-5",
                "title": "Bash",
                "rawInput": {"command": "echo AKIAIOSFODNN7EXAMPLE"},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert "AKIAIOSFODNN7EXAMPLE" not in event.tool_input

    def test_kind_only_emits_refinement(self):
        # Even a lone `kind` update is worth surfacing — avoids losing the
        # Bash/Edit distinction if the upstream renders it without a title.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-7",
                "kind": "search",
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.tool_kind == "search"

    def test_carries_the_purpose_from_raw_input(self):
        # The refinement's rawInput is the complete params object, so it holds
        # the reserved purpose argument. Dropping it loses the purpose whenever
        # the initial tool_call streamed an empty rawInput, and makes consumers
        # that fall back on an empty purpose paint the raw command instead.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-8",
                "title": "ls /tmp",
                "kind": "execute",
                "rawInput": {"command": "ls /tmp", "__tool_use_purpose": "List the temp dir"},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.tool_purpose == "List the temp dir"

    def test_kindless_refinement_without_purpose_reports_empty(self):
        # Consumers read an empty purpose as "keep what the initial tool_call
        # supplied", so a refinement carrying no params must not invent one.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-9",
                "title": "ls /tmp",
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.tool_purpose == ""

    def test_purpose_is_redacted(self):
        # Asserts the value is POPULATED as well as scrubbed — an empty purpose
        # would satisfy a bare "no credential in it" check on its own.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-10",
                "rawInput": {
                    "command": "aws s3 ls",
                    "__tool_use_purpose": "Use AKIAIOSFODNN7EXAMPLE to list buckets",
                },
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.tool_purpose.startswith("Use ")
        assert event.tool_purpose.endswith("to list buckets")
        assert "AKIAIOSFODNN7EXAMPLE" not in event.tool_purpose


class TestAcpClientIsShellSignal:
    """The canonical is_shell flow: the ACP shell kind ("execute") must be
    captured at the tool_call boundary and inherited by the later
    permission_request event (which carries no reliable kind), so the dashboard
    exempts long shell command titles from the 256-char cap. Without the
    inherited kind the permission path rejects long shell commands as
    "Tool name exceeds max length 256".
    """

    def _tool_call_msg(self, kind, tool_call_id="tc-1", command="echo hi"):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": tool_call_id,
                    "title": "Running a very long command",
                    "kind": kind,
                    "rawInput": {"command": command},
                }
            },
        )

    def _permission_msg(self, tool_call_id="tc-1", kind=None):
        from kiro_crew.acp.types import JsonRpcMessage

        tool_call = {"toolCallId": tool_call_id, "title": "x" * 400}
        # By default no "kind" on the permission payload — mirrors live
        # behaviour where the request_permission toolCall carries an empty
        # kind. Pass kind=... to exercise the payload-kind fallback branch.
        if kind is not None:
            tool_call["kind"] = kind
        return JsonRpcMessage(
            id=42,
            method="session/request_permission",
            params={
                "toolCall": tool_call,
                "options": [{"optionId": "allow_once", "name": "Allow once"}],
            },
        )

    def _refinement_msg(self, kind, tool_call_id="tc-1", command="echo hi"):
        from kiro_crew.acp.types import JsonRpcMessage

        update = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": tool_call_id,
            "title": "refined title",
            "rawInput": {"command": command},
        }
        # kind is optional on a refinement; only include it when provided so
        # tests can exercise the no-clobber path (kind absent).
        if kind is not None:
            update["kind"] = kind
        return JsonRpcMessage(method="session/update", params={"update": update})

    def test_execute_kind_marks_event_is_shell(self, tmp_path):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        ev = client._extract_tool_event(self._tool_call_msg("execute"))
        assert ev is not None
        assert ev.is_shell is True
        assert client._tool_call_is_shell.get("tc-1") is True

    def test_non_shell_kind_not_is_shell(self, tmp_path):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        ev = client._extract_tool_event(self._tool_call_msg("edit"))
        assert ev is not None
        assert ev.is_shell is False

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            pytest.param("execute", True, id="permission_event_inherits_is_shell_from_tool_call"),
            pytest.param("edit", False, id="permission_event_non_shell_stays_false"),
        ],
    )
    def test_permission_event_is_shell_from_the_tool_call(self, tmp_path, kind, expected):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        # 1. tool_call notification arrives first and carries the kind
        client._extract_tool_event(self._tool_call_msg(kind))
        # 2. permission request for the SAME toolCallId carries no kind, so the
        # verdict comes from the toolCallId cache
        perm = client._build_permission_event(self._permission_msg())
        assert perm.is_shell is expected

    def test_permission_event_inherits_trusted_tool_identity(self, tmp_path):
        # The client caches the trusted _meta.kiro identity (mcpServerName +
        # toolName) from the tool_call so the later permission event (which
        # carries no _meta) can rebuild mcp__<server>__<tool> for the
        # app-own-server auto-approve's per-tool governance. Without the
        # tool_name cache the permission event's tool_name is always "" and the
        # auto-approve fails closed (never fires).
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        tc = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-1",
                    "title": "Doing app work",  # prose, not the canonical mcp__ form
                    "kind": "other",
                    "rawInput": {},
                    "_meta": {
                        "kiro": {
                            "toolName": "list_intakes",
                            "mcpServerName": "beehive:beehive-mcp",
                        }
                    },
                }
            },
        )
        client._extract_tool_event(tc)
        perm = client._build_permission_event(self._permission_msg())
        assert perm.tool_name == "list_intakes"
        assert perm.mcp_server_name == "beehive:beehive-mcp"

    def test_permission_event_does_not_promote_title_on_identity_cache_miss(self, tmp_path):
        """Legacy per-session transport fails closed without ``_meta.kiro``."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage
        from kiro_crew.trust_patterns import approval_command

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-1",
                        "title": "mcp__beehive:beehive-mcp__list_intakes",
                        "kind": "other",
                        "rawInput": {},
                    }
                },
            )
        )

        perm = client._build_permission_event(self._permission_msg())

        assert perm.tool_name == ""
        assert perm.mcp_server_name == ""
        assert (
            approval_command(
                perm.tool_input,
                is_shell=perm.is_shell,
                tool_name=perm.tool_name,
                mcp_server_name=perm.mcp_server_name,
            )
            == ""
        )

    def test_structured_non_shell_reprompt_keeps_argument_provenance(self, tmp_path):
        """A repeated MCP permission retains both display and raw provenance."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage
        from kiro_crew.trust_patterns import approval_command

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-1",
                        "title": "Looking up the record",
                        "kind": "other",
                        "rawInput": {"record_id": "sensitive-record"},
                        "_meta": {
                            "kiro": {
                                "toolName": "read_record",
                                "mcpServerName": "records:primary",
                            }
                        },
                    }
                },
            )
        )

        first = client._build_permission_event(self._permission_msg())
        repeated = client._build_permission_event(self._permission_msg())

        assert first.tool_input
        assert repeated.tool_input == first.tool_input
        assert repeated.raw_tool_params == {"record_id": "sensitive-record"}
        assert (
            approval_command(
                repeated.tool_input,
                is_shell=repeated.is_shell,
                tool_name=repeated.tool_name,
                mcp_server_name=repeated.mcp_server_name,
                raw_tool_params=repeated.raw_tool_params,
            )
            == ""
        )

    @pytest.mark.parametrize("raw_input", ["/etc/secret", ["/etc/secret"]])
    def test_non_dict_non_shell_reprompt_cannot_become_durable_tool_trust(
        self, tmp_path, raw_input
    ):
        """String/list rawInput must not become argument-free after one prompt."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage
        from kiro_crew.trust_patterns import approval_command

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-1",
                        "title": "Reading a path",
                        "kind": "read",
                        "rawInput": raw_input,
                        "_meta": {"kiro": {"toolName": "read_path", "mcpServerName": "files"}},
                    }
                },
            )
        )

        first = client._build_permission_event(self._permission_msg())
        repeated = client._build_permission_event(self._permission_msg())

        assert repeated.tool_input == first.tool_input
        assert repeated.tool_input
        assert (
            approval_command(
                repeated.tool_input,
                is_shell=repeated.is_shell,
                tool_name=repeated.tool_name,
                mcp_server_name=repeated.mcp_server_name,
                raw_tool_params=repeated.raw_tool_params,
            )
            == ""
        )

    def test_end_to_end_long_shell_title_passes_validation(self, tmp_path):
        """The full regression: a 400-char shell title validates only because
        is_shell propagated from tool_call → permission → _validate_tool_name."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.dashboard.chat_utils import _validate_tool_name

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(self._tool_call_msg("execute"))
        perm = client._build_permission_event(self._permission_msg())
        # Mirrors the chat_runner call site.
        assert _validate_tool_name(perm.title, is_shell=perm.is_shell) == perm.title

    @pytest.mark.parametrize(
        ("initial_kind", "refinement_kind"),
        [
            # A refinement update that omits `kind` must NOT overwrite the
            # is_shell=True cached by the initial tool_call notification (kind is
            # optional on updates). This pins the no-clobber invariant.
            pytest.param("execute", None, id="refinement_kindless_does_not_clobber_cached_shell"),
            # A refinement that DOES carry a kind refreshes the cached signal: the
            # edit tool_call cached False, and kind="execute" must flip it to True.
            pytest.param("edit", "execute", id="refinement_with_kind_refreshes_shell_signal"),
        ],
    )
    def test_refinement_leaves_a_shell_verdict(self, tmp_path, initial_kind, refinement_kind):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(self._tool_call_msg(initial_kind))
        ev = client._extract_tool_call_refinement(self._refinement_msg(refinement_kind))
        assert ev is not None
        assert ev.is_shell is True
        assert client._tool_call_is_shell.get("tc-1") is True

    def test_permission_payload_kind_is_not_trusted_on_cache_miss(self, tmp_path):
        """SECURITY (deny-by-default): a cache miss must NOT be rescued by the
        permission payload's own kind. The payload is agent/LLM-influenced, so
        trusting kind="execute" there would let a malicious agent waive the
        length cap on the very name being validated. With no preceding
        tool_call to populate the cache, is_shell must stay False even when the
        payload claims kind="execute"."""
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        # No _extract_tool_event call → cache miss; payload tries kind="execute".
        perm = client._build_permission_event(self._permission_msg(kind="execute"))
        assert perm.is_shell is False

    def test_permission_event_does_not_pop_shell_cache(self, tmp_path):
        """The permission event must read the cached signal with .get(), not
        .pop(): a later tool_call_update refinement for the same toolCallId
        reads the same cache, so popping would make it wrongly see is_shell=
        False. The read must use .get(), not a .pop() that consumes the entry."""
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(self._tool_call_msg("execute"))
        perm = client._build_permission_event(self._permission_msg())
        assert perm.is_shell is True
        # Entry survives → a post-permission refinement still resolves True.
        assert client._tool_call_is_shell.get("tc-1") is True
        ev = client._extract_tool_call_refinement(self._refinement_msg(None))
        assert ev is not None and ev.is_shell is True

    def test_is_shell_kind_is_none_safe(self):
        """_is_shell_kind tolerates a non-str/None kind without raising — the
        ACP payload can carry "kind": null (JSON-legal), and equality keeps the
        classifier crash-free where a `.lower()`-based predicate would not."""
        from kiro_crew.acp.client import _is_shell_kind

        assert _is_shell_kind(None) is False
        assert _is_shell_kind("") is False
        assert _is_shell_kind("edit") is False
        assert _is_shell_kind("execute") is True

    def test_permission_event_null_kind_does_not_crash(self, tmp_path):
        """A request_permission payload carrying "kind": null (JSON-legal, and
        emitted by real ACP/MCP servers) must not crash event construction, and
        the resulting title must still validate. Guards the chat-runner event
        loop against a null-kind AttributeError on the length-cap path."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage
        from kiro_crew.dashboard.chat_utils import _validate_tool_name

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(
            id=7,
            method="session/request_permission",
            params={
                "toolCall": {"toolCallId": "tc-null", "title": "ReadFile", "kind": None},
                "options": [{"optionId": "allow_once", "name": "Allow once"}],
            },
        )
        perm = client._build_permission_event(msg)
        # No cached tool_call → deny-by-default; short non-shell name validates.
        assert perm.is_shell is False
        assert _validate_tool_name(perm.title, is_shell=perm.is_shell) == "ReadFile"

    def test_is_shell_propagates_through_to_llm_event(self):
        """AcpProvider._to_llm_event re-wraps an event field-by-field; the
        is_shell flag must survive that copy or the dashboard (which consumes
        the re-wrapped LLMEvent) would never see it on the live ACP path."""
        from kiro_crew.acp.types import AcpEvent
        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST

        src = AcpEvent(kind=EVENT_PERMISSION_REQUEST, title="x" * 400, is_shell=True)
        wrapped = AcpProvider._to_llm_event(src)
        assert wrapped.is_shell is True
        # And the default carries through as False when not a shell tool.
        not_shell = AcpProvider._to_llm_event(AcpEvent(kind=EVENT_PERMISSION_REQUEST))
        assert not_shell.is_shell is False
