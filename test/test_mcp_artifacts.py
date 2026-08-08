"""Tests for the artifact MCP tool handlers in :mod:`kiro_crew.mcp_core`.

Covers the dispatch branches for ``artifact_save``, ``artifact_get``,
``artifact_update``, ``artifact_list``, ``artifact_versions`` and
``artifact_delete`` — verifying schema validation, payload construction,
HTTP call shape, and result formatting.
"""

from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest

from kiro_crew.mcp_core import _call_tool_inner

# Expected clickable-reference form the MCP layer emits for non-widget
# artifacts so the frontend renderer can linkify it into an openable anchor.
# Declared locally (not imported from source) so the test asserts the
# contract rather than tracking whatever the code happens to produce.
ARTIFACT_ROUTE_PREFIX = "/artifacts/"


class TestArtifactSaveSessionAttribution:
    """``artifact_save`` must attribute the save to its calling chat session.

    Without ``origin_session_key`` in the body the store persists
    ``session_key=""`` for every agent-authored artifact, so the in-session
    Artifacts tab — which scopes by session — cannot show the artifacts the
    agent just created in that session. The ``X-Session-Key`` header this call
    already carries only feeds the ``source`` bucket, so the value has to
    travel in the body.
    """

    def test_save_forwards_the_calling_session_key(self) -> None:
        saved: dict = {}

        def _capture(path, body, **kwargs):
            saved.update(body)
            return {"slug": "s", "version": 1, "name": "N", "kind": "widget"}

        with patch("kiro_crew.mcp_core._resolve_session_key", return_value="chat-5-123"):
            with patch("kiro_crew.mcp_core._post", side_effect=_capture):
                _call_tool_inner("artifact_save", {"name": "N", "content": "<div/>"})
        assert saved["origin_session_key"] == "chat-5-123"

    def test_save_omits_the_key_when_no_session_resolves(self) -> None:
        """A CLI/standalone caller has no slot — send nothing rather than junk."""
        saved: dict = {}

        def _capture(path, body, **kwargs):
            saved.update(body)
            return {"slug": "s", "version": 1, "name": "N", "kind": "widget"}

        with patch("kiro_crew.mcp_core._resolve_session_key", return_value=""):
            with patch("kiro_crew.mcp_core._post", side_effect=_capture):
                _call_tool_inner("artifact_save", {"name": "N", "content": "<div/>"})
        assert "origin_session_key" not in saved


class TestArtifactReferenceLink:
    """The MCP layer surfaces a clickable ``[<name>](/artifacts/<slug>)``
    markdown link for non-widget artifacts (markdown/html/text) so they can
    be opened from the chat transcript. Widget artifacts already round-trip
    via ``<mcwidget>`` and intentionally do NOT get the link.
    """

    def test_save_non_widget_emits_markdown_link(self) -> None:
        # given a saved markdown artifact
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={
                "slug": "release-notes",
                "version": 1,
                "name": "Release Notes",
                "kind": "markdown",
            },
        ):
            # when the save tool result is rendered
            result = _call_tool_inner(
                "artifact_save",
                {"name": "Release Notes", "content": "# notes", "kind": "markdown"},
            )
        # then it carries the clickable link form: [<name>](/artifacts/<slug>)
        assert "[Release Notes](/artifacts/release-notes)" in result

    def test_save_widget_omits_markdown_link(self) -> None:
        # given a saved widget artifact (round-trips via <mcwidget>)
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={
                "slug": "dash",
                "version": 1,
                "name": "Dash",
                "kind": "widget",
            },
        ):
            # when the save tool result is rendered
            result = _call_tool_inner(
                "artifact_save",
                {"name": "Dash", "content": "<div/>", "kind": "widget"},
            )
        # then no /artifacts/<slug> link is emitted — the widget re-emit hint
        # is the artifact's surfacing mechanism instead.
        assert ARTIFACT_ROUTE_PREFIX not in result
        assert "<mcwidget" in result

    def test_get_non_widget_emits_markdown_link(self) -> None:
        # given a fetched markdown artifact
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "slug": "doc",
                "name": "My Doc",
                "kind": "markdown",
                "version": 2,
                "content": "# hi",
            },
        ):
            # when the get tool result is rendered
            result = _call_tool_inner("artifact_get", {"slug": "doc"})
        # then the clickable reference is appended
        assert "[My Doc](/artifacts/doc)" in result

    def test_get_widget_omits_markdown_link(self) -> None:
        # given a fetched widget artifact
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "slug": "w",
                "name": "W",
                "kind": "widget",
                "version": 1,
                "content": "<div/>",
            },
        ):
            # when the get tool result is rendered
            result = _call_tool_inner("artifact_get", {"slug": "w"})
        # then no link form — widget round-trips via the re-emit tag
        assert ARTIFACT_ROUTE_PREFIX not in result
        assert "<mcwidget" in result

    def test_update_non_widget_emits_markdown_link(self) -> None:
        # given an updated text artifact
        with patch("kiro_crew.mcp_core.loopback_urlopen") as urlopen_mock:
            urlopen_mock.return_value.__enter__.return_value.read.return_value = (
                b'{"slug": "log", "version": 5, "name": "Run Log", "kind": "text"}'
            )
            # when the update tool result is rendered
            result = _call_tool_inner("artifact_update", {"slug": "log", "content": "new"})
        # then the clickable reference is appended
        assert "[Run Log](/artifacts/log)" in result

    def test_link_falls_back_to_slug_when_name_missing(self) -> None:
        # given an updated non-widget artifact whose response omits 'name'
        with patch("kiro_crew.mcp_core.loopback_urlopen") as urlopen_mock:
            urlopen_mock.return_value.__enter__.return_value.read.return_value = (
                b'{"slug": "anon-doc", "version": 1, "kind": "markdown"}'
            )
            # when the update tool result is rendered
            result = _call_tool_inner("artifact_update", {"slug": "anon-doc", "content": "x"})
        # then the slug is used as the link text so the link is never empty
        assert "[anon-doc](/artifacts/anon-doc)" in result

    def test_link_label_redacts_credential_in_name(self) -> None:
        # given a non-widget artifact whose LLM-provided name embeds a
        # credential pattern (the name becomes the visible link text)
        leaked_credential = "AKIAIOSFODNN7EXAMPLE"
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={
                "slug": "doc",
                "name": f"My {leaked_credential} Doc",
                "kind": "markdown",
                "version": 1,
            },
        ):
            # when the save tool result is rendered
            result = _call_tool_inner(
                "artifact_save",
                {
                    "name": f"My {leaked_credential} Doc",
                    "content": "# notes",
                    "kind": "markdown",
                },
            )
        # then the clickable reference is emitted to the system-generated slug
        # with the credential scrubbed out of the visible label
        link_start = result.index("[")
        link_end = result.index(")", result.index(ARTIFACT_ROUTE_PREFIX)) + 1
        link = result[link_start:link_end]
        assert link.endswith(f"]({ARTIFACT_ROUTE_PREFIX}doc)")
        assert leaked_credential not in link

    def test_link_url_sanitizes_markdown_breaking_slug(self) -> None:
        # given a save whose server-returned slug carries markdown-breaking
        # characters that could close the link and inject arbitrary markdown
        # (the slug is reflected from the API response into the link URL)
        crafted_slug = "evil)[x](http://attacker.test"
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={
                "slug": crafted_slug,
                "name": "Doc",
                "kind": "markdown",
                "version": 1,
            },
        ):
            # when the save tool result is rendered
            result = _call_tool_inner(
                "artifact_save",
                {"name": "Doc", "content": "# notes", "kind": "markdown"},
            )
        # then the slug embedded in the URL is constrained to the safe slug
        # charset, so the crafted ')'/'[' can't break out of the link
        assert f"[Doc]({ARTIFACT_ROUTE_PREFIX}evilxhttpattackertest)" in result

    def test_link_url_redacts_credential_in_slug(self) -> None:
        # given a save whose server-returned slug carries a credential pattern
        leaked_credential = "AKIAIOSFODNN7EXAMPLE"
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={
                "slug": leaked_credential,
                "name": "Doc",
                "kind": "markdown",
                "version": 1,
            },
        ):
            # when the save tool result is rendered
            result = _call_tool_inner(
                "artifact_save",
                {"name": "Doc", "content": "# notes", "kind": "markdown"},
            )
        # then the credential is scrubbed out of the link URL before the
        # charset filter (redaction runs first, same as the label)
        link_start = result.index("[Doc](")
        link = result[link_start : result.index(")", link_start) + 1]
        assert leaked_credential.lower() not in link

    def test_empty_slug_after_sanitize_emits_plain_text(self) -> None:
        # given a non-widget artifact whose slug reduces to nothing once the
        # slug charset filter runs (here a slug of only filtered-out chars) —
        # an empty slug would otherwise produce a dangling /artifacts/ href
        all_filtered_slug = "???"
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "slug": all_filtered_slug,
                "name": "Orphan Doc",
                "kind": "markdown",
                "version": 1,
                "content": "# hi",
            },
        ):
            # when the get tool result is rendered
            result = _call_tool_inner("artifact_get", {"slug": "doc"})
        # then it degrades to plain text: the name surfaces with no broken link
        assert "Orphan Doc" in result
        assert f"]({ARTIFACT_ROUTE_PREFIX}" not in result

    def test_name_with_newline_collapses_to_single_line_link(self) -> None:
        # given a non-widget artifact whose name carries a literal newline (a
        # newline in the label would split the markdown anchor across lines and
        # break the clickable link)
        newline_name = "Quarterly\nReport"
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "slug": "q-report",
                "name": newline_name,
                "kind": "markdown",
                "version": 1,
                "content": "# hi",
            },
        ):
            # when the get tool result is rendered
            result = _call_tool_inner("artifact_get", {"slug": "q-report"})
        # then the link label has the newline collapsed to a space, keeping the
        # whole anchor on a single line so it linkifies correctly
        assert "[Quarterly Report](/artifacts/q-report)" in result
        # and no link label spans two lines: the raw newline form must never
        # appear inside a markdown link's bracket text (the artifact metadata
        # echo elsewhere in the result may still carry the verbatim name, so we
        # scope this assertion to the bracketed link label only)
        assert "[Quarterly\nReport]" not in result


class TestArtifactSave:
    def test_minimal_save(self) -> None:
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={"slug": "my-widget", "version": 1, "name": "My Widget"},
        ) as post:
            result = _call_tool_inner(
                "artifact_save", {"name": "My Widget", "content": "<p>hi</p>"}
            )
        path, body = post.call_args.args
        assert path == "/api/artifacts"
        assert body["name"] == "My Widget"
        assert body["content"] == "<p>hi</p>"
        # Optional fields not in args don't appear.
        assert "slug" not in body
        assert "tags" not in body
        assert "Saved artifact" in result
        assert "my-widget" in result

    def test_optional_fields_passed(self) -> None:
        with patch("kiro_crew.mcp_core._post", return_value={"slug": "x", "version": 1}) as post:
            _call_tool_inner(
                "artifact_save",
                {
                    "name": "X",
                    "content": "<x/>",
                    "slug": "explicit",
                    "kind": "html",
                    "source": "manual",
                    "description": "desc",
                    "tags": ["a", "b"],
                },
            )
        body = post.call_args.args[1]
        assert body["slug"] == "explicit"
        assert body["kind"] == "html"
        assert body["source"] == "manual"
        assert body["description"] == "desc"
        assert body["tags"] == ["a", "b"]

    def test_error_propagated(self) -> None:
        with patch("kiro_crew.mcp_core._post", return_value={"error": "duplicate"}):
            result = _call_tool_inner("artifact_save", {"name": "x", "content": "a"})
        assert "Error: duplicate" in result

    def test_dedup_hint_when_existing_widget_with_same_name(self) -> None:
        # When the agent's about to artifact_save a widget whose name
        # matches an existing chat-source widget, the response includes
        # a duplicate-warning hint pointing at the existing slug. The
        # save still proceeds — we only WARN; we don't block.
        with (
            patch(
                "kiro_crew.mcp_core._get",
                return_value={
                    "artifacts": [
                        {
                            "slug": "rules-of-fight-club",
                            "name": "Rules of Fight Club",
                            "updated_at": "2026-05-29T03:24:00Z",
                        },
                    ],
                },
            ),
            patch(
                "kiro_crew.mcp_core._post",
                return_value={"slug": "rules-of-fight-club-2", "version": 1},
            ),
        ):
            result = _call_tool_inner(
                "artifact_save",
                {"name": "Rules of Fight Club", "content": "<div>v2</div>"},
            )
        assert "Possible duplicate" in result
        assert "rules-of-fight-club" in result
        assert "artifact_update" in result

    def test_dedup_hint_nfc_normalizes_name_match(self) -> None:
        # Agent emits "Café" in NFD form; existing artifact stored in
        # NFC form. Without normalization the byte-different titles
        # would compare as distinct and the hint would silently miss.
        nfd = "Cafe\u0301"  # decomposed
        nfc = "Caf\u00e9"  # composed (= "Café")
        assert nfd != nfc  # sanity
        with (
            patch(
                "kiro_crew.mcp_core._get",
                return_value={
                    "artifacts": [
                        {"slug": "cafe", "name": nfc, "updated_at": "2026-05-29T03:00:00Z"},
                    ],
                },
            ),
            patch(
                "kiro_crew.mcp_core._post",
                return_value={"slug": "cafe-2", "version": 1},
            ),
        ):
            result = _call_tool_inner(
                "artifact_save",
                {"name": nfd, "content": "<div>x</div>"},
            )
        assert "Possible duplicate" in result
        assert "cafe" in result

    def test_dedup_hint_skipped_when_explicit_slug_provided(self) -> None:
        # Agent passing explicit slug=foo means it knows what it's
        # doing — typically re-saving a known artifact. Don't surface
        # a hint that would just be noise. Probe shouldn't even fire.
        with (
            patch("kiro_crew.mcp_core._get") as get,
            patch(
                "kiro_crew.mcp_core._post",
                return_value={"slug": "foo", "version": 1},
            ),
        ):
            result = _call_tool_inner(
                "artifact_save",
                {"name": "Rules of Fight Club", "content": "<div>x</div>", "slug": "foo"},
            )
        assert "Possible duplicate" not in result
        get.assert_not_called()

    def test_dedup_hint_skipped_for_default_widget_title(self) -> None:
        # name=="Widget" is the autogenerated fallback used when the
        # agent doesn't set a title — too collision-prone to dedup
        # against (every title-less widget would bind to the first
        # such artifact in the library).
        with (
            patch("kiro_crew.mcp_core._get") as get,
            patch(
                "kiro_crew.mcp_core._post",
                return_value={"slug": "x", "version": 1},
            ),
        ):
            result = _call_tool_inner(
                "artifact_save",
                {"name": "Widget", "content": "<div>x</div>"},
            )
        assert "Possible duplicate" not in result
        get.assert_not_called()

    def test_dedup_hint_skipped_when_kind_is_not_widget(self) -> None:
        # Same-named markdown / html / json artifacts are a different
        # use case — they're often per-source-file or per-document
        # snapshots where collision is normal, not a sign of a mistake.
        with (
            patch("kiro_crew.mcp_core._get") as get,
            patch(
                "kiro_crew.mcp_core._post",
                return_value={"slug": "x", "version": 1},
            ),
        ):
            _call_tool_inner(
                "artifact_save",
                {"name": "Notes", "content": "# notes", "kind": "markdown"},
            )
        get.assert_not_called()

    def test_dedup_hint_swallows_probe_failure_silently(self) -> None:
        # If the artifact_list probe fails (network blip, store error),
        # we proceed with the save without the hint rather than letting
        # a transient observability concern block legitimate saves.
        with (
            patch(
                "kiro_crew.mcp_core._get",
                side_effect=RuntimeError("probe boom"),
            ),
            patch(
                "kiro_crew.mcp_core._post",
                return_value={"slug": "x", "version": 1},
            ),
        ):
            result = _call_tool_inner(
                "artifact_save",
                {"name": "Rules of Fight Club", "content": "<div>x</div>"},
            )
        assert "Possible duplicate" not in result
        assert "Saved artifact" in result


class TestArtifactGet:
    def test_get_current(self) -> None:
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "slug": "x",
                "name": "X",
                "kind": "widget",
                "version": 3,
                "updated_at": "2026-05-21T16:00:00.000000+00:00",
                "tags": ["foo"],
                "description": "desc",
                "content": "<p>hello</p>",
            },
        ) as get:
            result = _call_tool_inner("artifact_get", {"slug": "x"})
        assert get.call_args.args[0] == "/api/artifacts/x"
        assert "slug: x" in result
        assert "version: 3" in result
        assert "<p>hello</p>" in result

    def test_get_specific_version(self) -> None:
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={"slug": "x", "name": "X", "version": 1, "content": "v1"},
        ) as get:
            _call_tool_inner("artifact_get", {"slug": "x", "version": 1})
        assert get.call_args.args[0] == "/api/artifacts/x/versions/1"

    def test_redacts_credentials_in_content(self) -> None:
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "slug": "x",
                "name": "X",
                "version": 1,
                "content": "<p>AKIAIOSFODNN7EXAMPLE</p>",
            },
        ):
            result = _call_tool_inner("artifact_get", {"slug": "x"})
        # Raw credential pattern must not appear.
        assert "AKIAIOSFODNN7EXAMPLE" not in result


class TestArtifactList:
    def test_no_filter(self) -> None:
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "artifacts": [
                    {
                        "slug": "a",
                        "name": "A",
                        "kind": "widget",
                        "version": 2,
                        "tags": ["t1"],
                    },
                    {
                        "slug": "b",
                        "name": "B",
                        "kind": "markdown",
                        "version": 1,
                        "tags": [],
                    },
                ]
            },
        ) as get:
            result = _call_tool_inner("artifact_list", {})
        assert get.call_args.args[0] == "/api/artifacts"
        assert "a  v2  widget  [t1]  A" in result
        assert "b  v1  markdown  B" in result

    def test_with_filter(self) -> None:
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={"artifacts": []},
        ) as get:
            _call_tool_inner("artifact_list", {"tag": "ops", "kind": "widget"})
        url = get.call_args.args[0]
        assert "tag=ops" in url
        assert "kind=widget" in url

    def test_empty(self) -> None:
        with patch("kiro_crew.mcp_core._get", return_value={"artifacts": []}):
            result = _call_tool_inner("artifact_list", {})
        assert result == "No artifacts saved."


class TestArtifactVersions:
    def test_versions(self) -> None:
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={"slug": "x", "versions": [1, 2, 5]},
        ):
            result = _call_tool_inner("artifact_versions", {"slug": "x"})
        assert "v1" in result
        assert "v2" in result
        assert "v5" in result

    def test_no_versions(self) -> None:
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={"slug": "x", "versions": []},
        ):
            result = _call_tool_inner("artifact_versions", {"slug": "x"})
        assert "No versions" in result


class TestArtifactDelete:
    def test_delete(self) -> None:
        with patch("kiro_crew.mcp_core._delete", return_value={"ok": True}) as delete:
            result = _call_tool_inner("artifact_delete", {"slug": "x"})
        assert delete.call_args.args[0] == "/api/artifacts/x"
        assert "Deleted artifact: x" in result

    def test_delete_error(self) -> None:
        with patch("kiro_crew.mcp_core._delete", return_value={"error": "not found"}):
            result = _call_tool_inner("artifact_delete", {"slug": "x"})
        assert "Error: not found" in result


class TestArtifactUpdate:
    def test_no_op_update_rejected(self) -> None:
        # No content/name/description/tags → error message.
        result = _call_tool_inner("artifact_update", {"slug": "x"})
        assert "nothing to update" in result.lower()


class TestArtifactRevert:
    def test_revert_fetches_target_then_patches_with_reverted_event(self) -> None:
        # The revert tool should: (1) GET the target version's content, then
        # (2) PATCH the artifact with that content, event_type='reverted',
        # and from_version pinned to the target. The handler enforces
        # snapshot=True for reverted updates so the revert always lands as
        # a new version on the timeline.
        target_content = "# v2 content"
        with (
            patch(
                "kiro_crew.mcp_core._get",
                return_value={"slug": "doc", "version": 2, "content": target_content},
            ) as get_mock,
            patch("kiro_crew.mcp_core.loopback_urlopen") as urlopen_mock,
        ):
            urlopen_mock.return_value.__enter__.return_value.read.return_value = (
                b'{"slug": "doc", "version": 4}'
            )
            result = _call_tool_inner("artifact_revert", {"slug": "doc", "target_version": 2})
        # Step 1: GET the target version
        get_mock.assert_called_once_with("/api/artifacts/doc/versions/2")
        # Step 2: PATCH with reverted event metadata
        req = urlopen_mock.call_args.args[0]
        assert req.method == "PATCH"
        assert req.full_url.endswith("/api/artifacts/doc")
        body = json.loads(req.data)
        assert body["content"] == target_content
        assert body["event_type"] == "reverted"
        assert body["from_version"] == 2
        # Result is human-readable summary.
        assert "Reverted doc to v2" in result
        assert "Live state is now v4" in result

    def test_revert_propagates_get_error(self) -> None:
        # If the target version doesn't exist, the GET fails and we report it.
        with patch("kiro_crew.mcp_core._get", return_value={"error": "version not found"}):
            result = _call_tool_inner("artifact_revert", {"slug": "doc", "target_version": 99})
        assert "cannot fetch version 99" in result

    def test_revert_validates_target_version(self) -> None:
        # Schema enforces target_version is a positive integer.
        from kiro_crew.mcp_core import _call_tool

        result = _call_tool("artifact_revert", {"slug": "doc", "target_version": 0})
        assert "error" in result.lower() or "target_version" in result.lower()

    def test_revert_surfaces_source_path_for_diff_headers(self) -> None:
        # When the artifact is file-backed, the response should include
        # source_path so the calling agent can build a unified-diff header
        # that activates the dashboard's Open file affordance.
        with (
            patch(
                "kiro_crew.mcp_core._get",
                return_value={"slug": "doc", "version": 2, "content": "v2"},
            ),
            patch("kiro_crew.mcp_core.loopback_urlopen") as urlopen_mock,
        ):
            urlopen_mock.return_value.__enter__.return_value.read.return_value = (
                b'{"slug": "doc", "version": 4, "source_path": "/home/u/notes/doc.md"}'
            )
            result = _call_tool_inner("artifact_revert", {"slug": "doc", "target_version": 2})
        assert "source_path: /home/u/notes/doc.md" in result
        # Guidance for emitting a proper diff is included so the agent
        # surfaces an operable Open file button to the user.
        assert "--- /home/u/notes/doc.md" in result and "+++ /home/u/notes/doc.md" in result

    def test_revert_no_source_path_omits_diff_guidance(self) -> None:
        # Chat-backed artifacts (no source_path) shouldn't get the diff
        # guidance — there's no file to open in the side panel.
        with (
            patch(
                "kiro_crew.mcp_core._get",
                return_value={"slug": "doc", "version": 2, "content": "v2"},
            ),
            patch("kiro_crew.mcp_core.loopback_urlopen") as urlopen_mock,
        ):
            urlopen_mock.return_value.__enter__.return_value.read.return_value = (
                b'{"slug": "doc", "version": 4}'  # no source_path
            )
            result = _call_tool_inner("artifact_revert", {"slug": "doc", "target_version": 2})
        assert "source_path" not in result
        assert "--- " not in result


class TestSchemas:
    """Confirm validation rejects bad inputs at the dispatcher."""

    def test_save_rejects_missing_required(self) -> None:
        from kiro_crew.mcp_core import _call_tool

        # Missing 'content' → validation error
        result = _call_tool("artifact_save", {"name": "x"})
        assert "content" in result.lower() or "error" in result.lower()

    def test_save_rejects_invalid_slug(self) -> None:
        from kiro_crew.mcp_core import _call_tool

        result = _call_tool("artifact_save", {"name": "x", "content": "a", "slug": "Has Spaces"})
        assert "error" in result.lower() or "invalid" in result.lower()

    def test_get_rejects_invalid_slug(self) -> None:
        from kiro_crew.mcp_core import _call_tool

        result = _call_tool("artifact_get", {"slug": "BAD/PATH"})
        assert "error" in result.lower() or "invalid" in result.lower()


class TestArtifactGetCommentsFullBody:
    """Body text is returned in full — no [:200] truncation."""

    def test_short_body_returned_in_full(self) -> None:
        body = "Fix the typo in section 3."
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "comments": [
                    {"author": "alice", "body": body, "status": "open"},
                ]
            },
        ) as get:
            result = _call_tool_inner("artifact_get_comments", {"slug": "my-doc"})
        assert get.call_args.args[0] == "/api/artifacts/my-doc/comments"
        assert body in result

    def test_long_body_not_truncated(self) -> None:
        # 500-char body — previously would be cut to 200
        body = "A" * 250 + " MIDDLE " + "B" * 242
        assert len(body) == 500
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "comments": [
                    {"author": "bob", "body": body, "status": "open"},
                ]
            },
        ):
            result = _call_tool_inner("artifact_get_comments", {"slug": "doc"})
        # Full body present, including the middle marker
        assert " MIDDLE " in result
        assert "B" * 242 in result

    def test_very_long_body_preserved(self) -> None:
        # Near the ARTIFACT_COMMENT_TEXT_MAX (10_000) ceiling
        body = "X" * 5000
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "comments": [
                    {"author": "eve", "body": body, "status": "open"},
                ]
            },
        ):
            result = _call_tool_inner("artifact_get_comments", {"slug": "doc"})
        assert "X" * 5000 in result


class TestArtifactGetCommentsAnchorFormatting:
    """Anchor quotes: short shown in full, long bookended with TRUNCATED marker."""

    def test_short_anchor_shown_in_full(self) -> None:
        quote = "Callers / Sources"
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "comments": [
                    {
                        "author": "alice",
                        "body": "comment",
                        "status": "open",
                        "anchor": {"quote": quote, "start_offset": 42, "end_offset": 59},
                    },
                ]
            },
        ):
            result = _call_tool_inner("artifact_get_comments", {"slug": "doc"})
        # Full quote present
        assert f'"{quote}"' in result
        # Offsets included
        assert "chars 42:59" in result
        # No TRUNCATED marker
        assert "TRUNCATED" not in result

    def test_anchor_at_300_chars_shown_in_full(self) -> None:
        quote = "Z" * 300
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "comments": [
                    {
                        "author": "bob",
                        "body": "lgtm",
                        "status": "open",
                        "anchor": {"quote": quote, "start_offset": 0, "end_offset": 300},
                    },
                ]
            },
        ):
            result = _call_tool_inner("artifact_get_comments", {"slug": "doc"})
        # 300 chars exactly = still shown in full (threshold is <=300)
        assert "Z" * 300 in result
        assert "TRUNCATED" not in result

    def test_anchor_over_300_chars_is_bookended(self) -> None:
        # 500-char anchor — should be truncated with bookending
        head = "H" * 100
        middle = "M" * 300
        tail = "T" * 100
        quote = head + middle + tail
        assert len(quote) == 500
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "comments": [
                    {
                        "author": "carol",
                        "body": "needs work",
                        "status": "open",
                        "anchor": {"quote": quote, "start_offset": 100, "end_offset": 600},
                    },
                ]
            },
        ):
            result = _call_tool_inner("artifact_get_comments", {"slug": "doc"})
        # Bookended: first 100 and last 100 chars present
        assert "H" * 100 in result
        assert "T" * 100 in result
        # Middle NOT fully present (it's 300 chars, omitted)
        assert "M" * 300 not in result
        # TRUNCATED marker with char count
        assert "TRUNCATED" in result
        assert "300 chars omitted" in result
        # Offsets included
        assert "chars 100:600" in result

    def test_anchor_without_offsets(self) -> None:
        quote = "some text"
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "comments": [
                    {
                        "author": "dave",
                        "body": "fix this",
                        "status": "open",
                        "anchor": {"quote": quote},
                    },
                ]
            },
        ):
            result = _call_tool_inner("artifact_get_comments", {"slug": "doc"})
        assert f'"{quote}"' in result
        # No offset info when not provided
        assert "chars" not in result

    def test_long_anchor_without_offsets(self) -> None:
        quote = "A" * 400
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "comments": [
                    {
                        "author": "eve",
                        "body": "too long",
                        "status": "open",
                        "anchor": {"quote": quote},
                    },
                ]
            },
        ):
            result = _call_tool_inner("artifact_get_comments", {"slug": "doc"})
        # Still bookended
        assert "TRUNCATED" in result
        assert "200 chars omitted" in result
        # No "chars X:Y" offset info when offsets not provided
        assert not re.search(r"chars \d+:\d+", result)


class TestArtifactGetCommentsEdgeCases:
    """Edge cases: no comments, errors, agent comments, replies."""

    def test_no_comments_message(self) -> None:
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={"comments": []},
        ) as get:
            result = _call_tool_inner("artifact_get_comments", {"slug": "empty"})
        assert get.call_args.args[0] == "/api/artifacts/empty/comments"
        assert "No comments" in result

    def test_error_response(self) -> None:
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={"error": "artifact not found"},
        ):
            result = _call_tool_inner("artifact_get_comments", {"slug": "gone"})
        assert "Error" in result
        assert "artifact not found" in result

    def test_agent_comment_prefix(self) -> None:
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "comments": [
                    {
                        "author": "kiro",
                        "body": "auto-review",
                        "status": "open",
                        "is_agent": True,
                    },
                ]
            },
        ):
            result = _call_tool_inner("artifact_get_comments", {"slug": "doc"})
        # Agent provenance renders as a plain-text marker (no emoji), derived from
        # the structured is_agent field — not persisted into the body.
        from kiro_crew.validation import ARTIFACT_AGENT_MARKER

        assert ARTIFACT_AGENT_MARKER in result
        assert "\U0001f916" not in result

    def test_reply_indent(self) -> None:
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "comments": [
                    {"author": "alice", "body": "parent", "status": "open"},
                    {
                        "author": "bob",
                        "body": "child reply",
                        "status": "open",
                        "parent_id": "c1",
                    },
                ]
            },
        ):
            result = _call_tool_inner("artifact_get_comments", {"slug": "doc"})
        assert "↳" in result

    def test_comment_count_in_header(self) -> None:
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "comments": [
                    {"author": "a", "body": "one", "status": "open"},
                    {"author": "b", "body": "two", "status": "open"},
                    {"author": "c", "body": "three", "status": "resolved"},
                ]
            },
        ):
            result = _call_tool_inner("artifact_get_comments", {"slug": "doc"})
        assert "(3)" in result

    def test_comment_id_surfaced(self) -> None:
        # The id is the handle artifact_mark_review / artifact_delete_comment
        # need; omitting it left those follow-up tools uncallable from a result.
        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "comments": [
                    {"id": "c-42", "author": "alice", "body": "fix", "status": "open"},
                ]
            },
        ):
            result = _call_tool_inner("artifact_get_comments", {"slug": "doc"})
        assert "id=c-42" in result

    def test_agent_comment_body_stored_verbatim_no_emoji(self) -> None:
        # Bodies are stored verbatim (no emoji stamped in); the CLI marker is a
        # plain-text prefix derived from is_agent, applied once at render time.
        from kiro_crew.validation import ARTIFACT_AGENT_MARKER

        with patch(
            "kiro_crew.mcp_core._get",
            return_value={
                "comments": [
                    {
                        "id": "c1",
                        "author": "agent",
                        "body": "auto-review note",
                        "status": "open",
                        "is_agent": True,
                    },
                ]
            },
        ):
            result = _call_tool_inner("artifact_get_comments", {"slug": "doc"})
        # Exactly one plain-text marker, no emoji.
        assert result.count(ARTIFACT_AGENT_MARKER) == 1
        assert "\U0001f916" not in result
        assert "auto-review note" in result


class TestArtifactPostCommentTool:
    """The artifact_post_comment MCP tool POSTs the comment route with the body
    verbatim + is_agent flag, redacting the body before sending (no emoji)."""

    def test_post_comment_hits_comment_route_verbatim(self) -> None:
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={"comment": {"id": "c9", "sync_state": "local"}},
        ) as post:
            result = _call_tool_inner(
                "artifact_post_comment",
                {"slug": "doc", "text": "looks wrong in section 2"},
            )
        post.assert_called_once()
        path, payload = post.call_args.args
        assert path == "/api/artifacts/doc/comments"
        # Body stored verbatim (no emoji watermark); provenance is is_agent.
        assert payload["text"] == "looks wrong in section 2"
        assert "\U0001f916" not in payload["text"]
        assert payload["is_agent"] is True
        assert payload["author"] == "agent"
        assert payload["scope"] == "private"
        assert "c9" in result

    def test_post_comment_shared_scope_passed(self) -> None:
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={"comment": {"id": "c1", "sync_state": "pending"}},
        ) as post:
            _call_tool_inner(
                "artifact_post_comment",
                {"slug": "doc", "text": "sync me", "scope": "shared"},
            )
        _, payload = post.call_args.args
        assert payload["scope"] == "shared"

    def test_post_comment_redacts_credentials_in_body(self) -> None:
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={"comment": {"id": "c1", "sync_state": "local"}},
        ) as post:
            _call_tool_inner(
                "artifact_post_comment",
                {"slug": "doc", "text": "key AKIAIOSFODNN7EXAMPLE leaked"},
            )
        _, payload = post.call_args.args
        assert "AKIAIOSFODNN7EXAMPLE" not in payload["text"]

    def test_post_comment_surfaces_backend_error(self) -> None:
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={"error": "artifact not found"},
        ):
            result = _call_tool_inner(
                "artifact_post_comment",
                {"slug": "gone", "text": "hi"},
            )
        assert result.startswith("Error:")

    def test_post_comment_rejects_invalid_scope(self) -> None:
        from kiro_crew.validation import ValidationError

        with patch("kiro_crew.mcp_core._post") as post:
            with pytest.raises(ValidationError):
                _call_tool_inner(
                    "artifact_post_comment",
                    {"slug": "doc", "text": "hi", "scope": "public"},
                )
        post.assert_not_called()

    def test_post_comment_text_within_handler_cap(self) -> None:
        # The body is stored verbatim (no watermark), so the MCP schema's text cap
        # equals the HTTP handler's cap exactly — no schema-valid input can be
        # rejected server-side.
        from kiro_crew.validation import (
            ARTIFACT_COMMENT_TEXT_MAX,
            ARTIFACT_POST_COMMENT_SCHEMA,
        )

        text_max = next(f.max_len for f in ARTIFACT_POST_COMMENT_SCHEMA.fields if f.name == "text")
        assert text_max <= ARTIFACT_COMMENT_TEXT_MAX
        # The maximum-length accepted text POSTs a body within the handler cap.
        max_text = "x" * text_max
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={"comment": {"id": "c1", "sync_state": "local"}},
        ) as post:
            _call_tool_inner("artifact_post_comment", {"slug": "doc", "text": max_text})
        _, payload = post.call_args.args
        assert len(payload["text"]) <= ARTIFACT_COMMENT_TEXT_MAX


class TestArtifactReplyCommentTool:
    """The artifact_reply_comment MCP tool POSTs the parent thread's reply route
    with the body verbatim + is_agent flag, redacting the body before sending
    (no emoji — provenance rides on the structured flag)."""

    def test_reply_comment_posts_to_parent_thread(self) -> None:
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={"comment": {"id": "c2", "sync_state": "local_only"}},
        ) as post:
            result = _call_tool_inner(
                "artifact_reply_comment",
                {"slug": "doc", "parent_id": "c1", "text": "done"},
            )
        post.assert_called_once()
        path, payload = post.call_args.args
        assert path == "/api/artifacts/doc/comments/c1/reply"
        # Body stored verbatim (no emoji watermark); provenance is is_agent.
        assert payload["text"] == "done"
        assert "\U0001f916" not in payload["text"]
        assert payload["is_agent"] is True
        assert payload["author"] == "agent"
        assert "posted" in result.lower()

    def test_reply_comment_redacts_credentials_in_body(self) -> None:
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={"comment": {"id": "c2", "sync_state": "local"}},
        ) as post:
            _call_tool_inner(
                "artifact_reply_comment",
                {"slug": "doc", "parent_id": "c1",
                 "text": "key AKIAIOSFODNN7EXAMPLE leaked"},
            )
        _, payload = post.call_args.args
        assert "AKIAIOSFODNN7EXAMPLE" not in payload["text"]

    def test_reply_comment_surfaces_backend_error(self) -> None:
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={"error": "parent comment not found"},
        ):
            result = _call_tool_inner(
                "artifact_reply_comment",
                {"slug": "doc", "parent_id": "gone", "text": "hi"},
            )
        assert result.startswith("Error:")
        assert "parent comment not found" in result

    def test_reply_comment_rejects_bad_parent_id(self) -> None:
        from kiro_crew.validation import ValidationError

        with patch("kiro_crew.mcp_core._post") as post:
            with pytest.raises(ValidationError):
                _call_tool_inner(
                    "artifact_reply_comment",
                    {"slug": "doc", "parent_id": "../etc/passwd", "text": "hi"},
                )
        post.assert_not_called()


class TestArtifactMarkReviewTool:
    """The artifact_mark_review MCP tool POSTs the review route — agents may
    advance a thread to REVIEW but never resolve it."""

    def test_mark_review_hits_review_route(self) -> None:
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={"status": "review"},
        ) as post:
            result = _call_tool_inner(
                "artifact_mark_review",
                {"slug": "doc", "comment_id": "c1"},
            )
        post.assert_called_once()
        path, payload = post.call_args.args
        assert path == "/api/artifacts/doc/comments/c1/review"
        assert payload == {}
        assert "REVIEW" in result

    def test_mark_review_surfaces_backend_error(self) -> None:
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={"error": "comment not found"},
        ):
            result = _call_tool_inner(
                "artifact_mark_review",
                {"slug": "doc", "comment_id": "missing"},
            )
        assert result.startswith("Error:")
        assert "comment not found" in result

    def test_mark_review_rejects_bad_comment_id(self) -> None:
        from kiro_crew.validation import ValidationError

        with patch("kiro_crew.mcp_core._post") as post:
            with pytest.raises(ValidationError):
                _call_tool_inner(
                    "artifact_mark_review",
                    {"slug": "doc", "comment_id": "../etc/passwd"},
                )
        post.assert_not_called()


class TestArtifactDeleteCommentTool:
    """The artifact_delete_comment MCP tool DELETEs the comment route with the
    required reason in the body (redacted before sending — it lands in the SEL
    audit and activity feed)."""

    def test_delete_comment_hits_delete_route_with_reason(self) -> None:
        with patch(
            "kiro_crew.mcp_core._delete",
            return_value={"deleted": True},
        ) as dl:
            result = _call_tool_inner(
                "artifact_delete_comment",
                {"slug": "doc", "comment_id": "c1", "reason": "applied in v3: typo fixed"},
            )
        dl.assert_called_once()
        path, payload = dl.call_args.args
        assert path == "/api/artifacts/doc/comments/c1"
        assert payload["reason"] == "applied in v3: typo fixed"
        assert "deleted" in result.lower()

    def test_delete_comment_surfaces_backend_error(self) -> None:
        with patch(
            "kiro_crew.mcp_core._delete",
            return_value={"error": "agents cannot delete provider-synced comments"},
        ):
            result = _call_tool_inner(
                "artifact_delete_comment",
                {"slug": "doc", "comment_id": "c1", "reason": "applied"},
            )
        assert result.startswith("Error:")
        assert "provider-synced" in result

    def test_delete_comment_requires_reason(self) -> None:
        from kiro_crew.validation import ValidationError

        with patch("kiro_crew.mcp_core._delete") as dl:
            with pytest.raises(ValidationError):
                _call_tool_inner(
                    "artifact_delete_comment",
                    {"slug": "doc", "comment_id": "c1"},
                )
        dl.assert_not_called()
