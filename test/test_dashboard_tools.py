"""The six dashboard tools, and the preview/apply store behind four of them.

The user-facing flow is three phrases -- "show me another", "keep this one", "go back"
-- and this file holds the claims that make each of them safe to say to an agent:

* a PREVIEW records nothing. No version, no history row, and the current page is
  untouched, so "show me another" costs nothing when the answer is no;
* an APPLY installs the page that was STAGED. The tool takes no arguments, which is
  what makes the thing applied provably the thing the person looked at;
* only a page that SHIPPED with the product can be staged. A call carrying a manifest
  and html the agent wrote is refused, at the tool and again in the store, with a
  sentence that says custom templates come later -- because a rendered page runs its
  own script against the crewmate's task titles and summaries inside a frame that can
  navigate itself, so it could carry them out;
* "go back" reaches only a version still on disk, and the list it reads comes back on
  the fields call it was already going to make.

Every tool gets its real path and its refusal. A refusal with no test is a sentence
nobody has read: these tools are the first ones whose refusals an AGENT has to act on
without a person in the loop, so the refusal text is as much the product as the
success.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import dashboard_agentic
from kiro_crew.dashboard_templates import catalog, instance
from kiro_crew.mcp_panel import _call_tool_inner, _list_tools, _render_fields

SLUG = "fleet"
SESSION = "acp-dash-tools-1"

#: A verified caller, like ``test_mcp_panel_runtime``'s. Every tool here is behind the
#: strict identity gate, so a test that is not about the gate needs one or it exercises
#: the refusal instead of the behaviour it names.
GOOD_KEY = "dashboard:chat-1-100"

PAGE = '<div><b data-dashboard-field="credits"></b><i data-dashboard-field="phase"></i></div>'
#: The SECOND fixture template's page. One field, bound once, so a test that swaps
#: pages can tell which one it is looking at.
OTHER_PAGE = '<section><b data-dashboard-field="credits"></b></section>'
#: A page an agent wrote. Nothing here can stage it: it exists so the refusal that
#: says so has a real argument to refuse.
AUTHORED_PAGE = '<article><b data-dashboard-field="credits"></b></article>'
#: Binds a field no manifest here declares. The parity rule is about the PAIR, and a
#: hand-edited template directory is the way that pair can still come apart.
UNPAIRED_PAGE = (
    '<div><b data-dashboard-field="credits"></b><i data-dashboard-field="ghost"></i></div>'
)


def _builtin_manifest(**over: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "id": "fixture-board",
        "version": 1,
        "title": "Fixture board",
        "description": "A template this test owns.",
        "source": "builtin",
        "fields": {
            "credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}},
            "phase": {"type": "string", "source": {"agentic": True}},
        },
    }
    manifest.update(over)
    return manifest


def _other_builtin_manifest() -> dict[str, Any]:
    return _builtin_manifest(
        id="fixture-other",
        title="Fixture other",
        description="A second template this test owns.",
        fields={
            "credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}},
        },
    )


def _authored_manifest(**over: Any) -> dict[str, Any]:
    """A manifest an agent wrote. Only ever passed to something that refuses it."""
    manifest: dict[str, Any] = {
        "id": "my-page",
        "version": 1,
        "title": "My page",
        "description": "A page the agent wrote.",
        "source": "user",
        "fields": {
            "credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}},
        },
    }
    manifest.update(over)
    return manifest


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    """Own data home and two fixture built-ins, patched on the test-owned undo stack.

    Through ``_floor_monkeypatch`` and not the shared ``monkeypatch`` for the reason
    the sibling instance suites give (D11): an isolation patch on the shared stack is
    lifted by any body that calls ``monkeypatch.undo()``, which would hand the rest of
    that test the real data home and the real registry -- and this file writes instance
    records and template directories.

    TWO templates, because the staging and rollback claims are about swapping one page
    for another and the only pages that can be staged are the ones the catalog serves.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    root = tmp_path / "builtin"
    for manifest, page in (
        (_builtin_manifest(), PAGE),
        (_other_builtin_manifest(), OTHER_PAGE),
    ):
        directory = root / str(manifest["id"])
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (directory / "template.html").write_text(page, encoding="utf-8")
    _floor_monkeypatch.setattr(catalog, "builtin_dir", lambda: root)
    yield


@pytest.fixture
def _verified_caller() -> Any:
    with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=GOOD_KEY):
        yield


# ------------------------------------------------------------------ the store


class TestStagingRecordsNothing:
    """The claim "show me another" rests on: a preview is not a change."""

    def test_staging_a_catalog_template_writes_no_version(self) -> None:
        instance.adopt(SLUG, "fixture-board", session_id="")
        before = instance.read(SLUG)
        staged = instance.stage_preview(SLUG, template_id="fixture-board")
        after = instance.read(SLUG)
        assert staged.template_id == "fixture-board"
        # The RECORD did not move, and neither did the fold.
        assert after.instance_version == before.instance_version
        assert instance.versions(SLUG) == (before.instance_version,)
        assert len(instance.history(SLUG)) == 1

    def test_staging_replaces_the_previous_preview(self) -> None:
        """One page is offered at a time, because one question is asked at a time."""
        instance.stage_preview(SLUG, template_id="fixture-board")
        instance.stage_preview(SLUG, template_id="fixture-other")
        held = instance.staged_preview(SLUG)
        assert held is not None and held.template_id == "fixture-other"
        assert held.html == OTHER_PAGE

    def test_a_preview_older_than_the_window_reads_as_nothing_staged(self) -> None:
        """Past the window it is dropped, not applied.

        A "yes" to a page somebody looked at an hour ago is a yes to a page whose fold
        values have since moved, so applying it would install what they saw rather than
        what they agreed to.
        """
        instance.stage_preview(SLUG, template_id="fixture-board")
        stale = instance.MAX_PREVIEW_AGE_MS + 60_000
        with patch.object(instance, "_now_ms", return_value=instance._now_ms() + stale):
            assert instance.staged_preview(SLUG) is None

    def test_the_preview_wire_carries_the_link_and_never_the_page(self) -> None:
        """The html is the large half and the agent that staged it already holds it."""
        wire = instance.stage_preview(SLUG, template_id="fixture-board").wire()
        assert wire["preview_url"] == f"/api/members/{SLUG}/dashboard?preview=1"
        assert "html" not in wire and wire["html_bytes"] == len(PAGE.encode("utf-8"))


class TestAnAgentWrittenPageCannotBeStaged:
    """The F1 rule, at the layer that owns it.

    A dashboard page runs its own script against this crewmate's task titles and
    summaries, and the dashboard's own ``frame-src`` admits the hosts its artifact
    previews need -- so a page that runs can navigate itself to one of them with those
    values in the URL, and a closed ``connect-src`` does not stop that. Scripts ARE the
    format, so the only available signal is the author.

    The store keeps the ``html`` and ``manifest`` PARAMETERS and refuses them, rather
    than dropping them from the signature. That is what makes the refusal total: a
    second caller added later cannot stage an authored page by forgetting a check of its
    own, because the check is here.
    """

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param(
                {"manifest": _authored_manifest(), "html": AUTHORED_PAGE}, id="both-halves"
            ),
            pytest.param({"html": AUTHORED_PAGE}, id="page-only"),
            pytest.param({"manifest": _authored_manifest()}, id="manifest-only"),
            pytest.param(
                {"template_id": "fixture-board", "html": AUTHORED_PAGE}, id="beside-a-template-id"
            ),
            pytest.param(
                {"manifest": _authored_manifest(source="builtin"), "html": AUTHORED_PAGE},
                id="claiming-to-be-built-in",
            ),
        ],
    )
    def test_every_shape_of_an_authored_page_is_refused(self, kwargs: dict[str, Any]) -> None:
        """Including the half-supplied ones and the one claiming ``builtin``.

        A manifest claiming ``builtin`` is the case a source check alone would pass, so
        it is refused by the presence of the argument rather than by what it says.
        """
        with pytest.raises(instance.InstanceRefused) as refused:
            instance.stage_preview(SLUG, **kwargs)
        assert str(refused.value) == instance.AUTHORED_PAGE_REFUSAL
        assert "come later" in str(refused.value)
        assert instance.staged_preview(SLUG) is None, "a refused page was staged anyway"

    def test_the_refusal_says_what_to_call_instead(self) -> None:
        """An agent that cannot write a page has to be told where pages come from."""
        assert "dashboard_templates" in instance.AUTHORED_PAGE_REFUSAL
        assert "template_id" in instance.AUTHORED_PAGE_REFUSAL

    def test_only_a_built_in_source_is_renderable(self) -> None:
        """The set behind the adopt-time half of the same rule. ONE value."""
        assert instance.RENDERABLE_SOURCES == frozenset({catalog.BUILTIN_SOURCE})

    def test_a_user_sourced_record_is_refused_at_adopt_as_well(self) -> None:
        """Belt and braces, on the path that writes a version rather than stages one.

        ``_check`` runs for every committed version, so a record that reached disk
        under an older gateway cannot be rolled forward into a rendered page either.
        """
        with pytest.raises(instance.InstanceRefused) as refused:
            instance._check(_authored_manifest(), AUTHORED_PAGE)
        assert "shipped with the product" in str(refused.value)


class TestStagingRefusals:
    """Each refusal names what the caller has to do instead, not a field."""

    def test_staging_nothing_is_refused(self) -> None:
        with pytest.raises(instance.InstanceRefused) as refused:
            instance.stage_preview(SLUG)
        assert "template_id" in str(refused.value)

    def test_a_blank_template_id_is_refused(self) -> None:
        with pytest.raises(instance.InstanceRefused) as refused:
            instance.stage_preview(SLUG, template_id="   ")
        assert "template_id" in str(refused.value)

    def test_a_template_whose_bindings_do_not_match_its_manifest_is_refused(self, tmp_path) -> None:
        """A hand-edited template directory, which is how the pair can still come apart.

        Refused at STAGING by the same check every committed version runs, so nobody is
        asked to look at a page this gateway was never going to install.
        """
        broken = catalog.builtin_dir() / "fixture-broken"
        broken.mkdir()
        (broken / "manifest.json").write_text(
            json.dumps(_builtin_manifest(id="fixture-broken")), encoding="utf-8"
        )
        (broken / "template.html").write_text(UNPAIRED_PAGE, encoding="utf-8")
        with pytest.raises(instance.InstanceRefused) as refused:
            instance.stage_preview(SLUG, template_id="fixture-broken")
        # The registry reports a directory it cannot serve rather than raising, so the
        # id is unknown here -- and the refusal names the ids that DO load.
        assert "fixture-board" in str(refused.value)

    def test_an_unknown_template_id_is_refused_naming_what_there_is(self) -> None:
        with pytest.raises(instance.InstanceRefused) as refused:
            instance.stage_preview(SLUG, template_id="no-such-template")
        assert "fixture-board" in str(refused.value)


class TestApplyingTheStagedPage:
    """The claim "keep this one" rests on: apply installs what was staged."""

    def test_applying_a_preview_commits_it_and_clears_the_preview(self) -> None:
        instance.stage_preview(SLUG, template_id="fixture-board")
        record = instance.apply_preview(SLUG, session_id="")
        assert record.instance_version == 1 and record.template_id == "fixture-board"
        assert record.html == PAGE, "apply installed a page other than the staged one"
        # DISCARDED, so "keep this one" cannot be answered twice and write two
        # versions of one page.
        assert instance.staged_preview(SLUG) is None

    def test_apply_writes_to_no_catalog(self) -> None:
        """Nothing is saved, because the only page that can be staged is already there.

        The registry's membership is the product's own shipped set, and an apply that
        could add to it would be the write that puts a page nobody reviewed where a
        later adopt can find it.
        """
        before = set(catalog.list_templates().by_id)
        instance.stage_preview(SLUG, template_id="fixture-other")
        instance.apply_preview(SLUG, session_id="")
        assert set(catalog.list_templates().by_id) == before

    def test_applying_with_nothing_staged_is_refused(self) -> None:
        with pytest.raises(instance.InstanceRefused) as refused:
            instance.apply_preview(SLUG, session_id="")
        assert "no page staged" in str(refused.value)

    def test_a_preview_file_naming_no_template_reads_as_nothing_staged(self) -> None:
        """What an older gateway's authored preview leaves behind on disk.

        It names no template, so there is nothing this gateway will install from it, and
        the answer is the one every other unusable preview gets: stage it again.
        """
        instance.stage_preview(SLUG, template_id="fixture-board")
        path = instance._preview_path(SLUG)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["template"] = {"id": "", "version": 0}
        raw["html"] = AUTHORED_PAGE
        raw["manifest"] = _authored_manifest()
        path.write_text(json.dumps(raw), encoding="utf-8")
        assert instance.staged_preview(SLUG) is None
        with pytest.raises(instance.InstanceRefused):
            instance.apply_preview(SLUG, session_id="")
        assert instance.read(SLUG).instance_version == 0

    def test_apply_is_recorded_as_adopted_so_the_history_vocabulary_stays_closed(
        self,
    ) -> None:
        """No fourth action value ships to describe a change the three already name.

        That matters because the entry type's ``action`` field is declared
        ``enum_closed``.
        """
        instance.stage_preview(SLUG, template_id="fixture-other")
        instance.apply_preview(SLUG, session_id="")
        rows = instance.history(SLUG)
        assert [row["action"] for row in rows] == ["adopted"]
        assert set(row["action"] for row in rows) <= set(instance.ACTIONS)


class TestGoingBack:
    """ "go back" reaches a retained version, and says which one it landed on."""

    def test_rollback_moves_forward_and_the_old_page_returns(self) -> None:
        instance.adopt(SLUG, "fixture-board", session_id="")
        instance.stage_preview(SLUG, template_id="fixture-other")
        instance.apply_preview(SLUG, session_id="")
        assert instance.read(SLUG).html == OTHER_PAGE
        back = instance.rollback(SLUG, 1, session_id="")
        assert back.instance_version == 3, "a rollback writes a NEW version"
        assert back.html == PAGE


# ------------------------------------------------------------------- the tools


class TestTheToolSurface:
    def test_all_six_tools_are_advertised(self) -> None:
        """The set the agent spec and the skill both name.

        Pinned here as well as in ``test_mcp_panel_registration``, which checks the
        schema ratchet: this asserts the SIX by name, so a tool quietly dropped from
        the surface fails beside the behaviour tests rather than only in a count.
        """
        assert {t["name"] for t in _list_tools()} >= {
            "dashboard_fields",
            "dashboard_write",
            "dashboard_templates",
            "dashboard_preview",
            "dashboard_apply",
            "dashboard_rollback",
        }

    def test_apply_advertises_no_arguments(self) -> None:
        """The emptiness IS the safety property: apply cannot be pointed elsewhere."""
        apply_tool = next(t for t in _list_tools() if t["name"] == "dashboard_apply")
        assert apply_tool["inputSchema"].get("properties") == {}


class TestDashboardTemplatesTool:
    def test_it_lists_the_catalog_and_marks_the_current_page(self, _verified_caller: Any) -> None:
        body = {
            "templates": [
                {
                    "id": "fixture-board",
                    "version": 1,
                    "title": "Fixture board",
                    "description": "A template this test owns.",
                    "fields": ["credits", "phase"],
                    "current": True,
                }
            ],
            "current_template_id": "fixture-board",
            "query": "",
            "problems": [],
        }
        with patch("kiro_crew.mcp_panel._get", return_value=body) as mock_get:
            out = _call_tool_inner("dashboard_templates", {})
        assert "fixture-board" in out and "ON NOW" in out
        # The next step is named in the answer, because an agent that lists and then
        # applies has skipped the person.
        assert "dashboard_preview" in out
        assert mock_get.call_args[0][0] == "/api/agent-panel/dashboard/templates"

    def test_a_query_is_passed_through_url_encoded(self, _verified_caller: Any) -> None:
        with patch("kiro_crew.mcp_panel._get", return_value={"templates": []}) as mock_get:
            _call_tool_inner("dashboard_templates", {"query": "shows cost"})
        assert mock_get.call_args[0][0].endswith("?query=shows%20cost")

    def test_an_empty_result_tells_the_agent_what_to_do_instead(
        self, _verified_caller: Any
    ) -> None:
        with patch("kiro_crew.mcp_panel._get", return_value={"templates": [], "query": "cost"}):
            out = _call_tool_inner("dashboard_templates", {"query": "cost"})
        assert "No dashboard template matches" in out
        # Fewer words, or none at all -- NOT "write the page yourself", which this
        # gateway will not render.
        assert "fewer" in out and "no `query`" in out

    def test_a_non_string_query_is_refused_before_the_gateway(self, _verified_caller: Any) -> None:
        with patch("kiro_crew.mcp_panel._get") as mock_get:
            out = _call_tool_inner("dashboard_templates", {"query": 7})
        assert out.startswith("Error:")
        mock_get.assert_not_called()


class TestTemplateSearchMatching:
    """Search matches what the template SHOWS, not only what its author called it."""

    @staticmethod
    def _listing() -> dict[str, Any]:
        return {
            "id": "fixture-board",
            "title": "Fixture board",
            "description": "A template this test owns.",
            "fields": ["credits", "phase"],
            "folds": ["usage"],
            "paths": ["usage.cost_usd"],
        }

    def test_a_word_only_in_a_fold_path_still_matches(self) -> None:
        """ "shows cost" has to find the page that reads a cost out of a usage fold.

        Matching only the prose would answer "there is none" for a template that does
        exactly the thing the person asked for, because the author never put the word
        in the title.
        """
        from kiro_crew.dashboard.handlers.agent_panel import _template_matches

        assert _template_matches(self._listing(), "cost")

    def test_every_word_has_to_match(self) -> None:
        from kiro_crew.dashboard.handlers.agent_panel import _template_matches

        assert _template_matches(self._listing(), "cost usage")
        assert not _template_matches(self._listing(), "cost latency")

    def test_matching_is_case_folded(self) -> None:
        from kiro_crew.dashboard.handlers.agent_panel import _template_matches

        assert _template_matches(self._listing(), "COST")


class TestDashboardPreviewTool:
    def test_it_hands_back_the_link_and_says_nothing_changed(self, _verified_caller: Any) -> None:
        body = {
            "ok": True,
            "preview": {
                "template_id": "fixture-board",
                "template_version": 1,
                "title": "Fixture board",
                "fields": ["credits", "phase"],
                "html_bytes": 120,
                "staged_ms": 1,
                "preview_url": "/api/members/fleet/dashboard?preview=1",
            },
        }
        with patch("kiro_crew.mcp_panel._post", return_value=body):
            out = _call_tool_inner("dashboard_preview", {"template_id": "fixture-board"})
        assert "NOTHING has changed" in out
        assert "/api/members/fleet/dashboard?preview=1" in out
        # The instruction to ASK is unconditional: this tool's whole value is that it
        # changes nothing, and an agent that previews and applies in one breath has
        # spent the staging step without letting anybody look.
        assert "ASK the person" in out

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(
                {"manifest": _authored_manifest(), "html": AUTHORED_PAGE}, id="both-halves"
            ),
            pytest.param({"html": AUTHORED_PAGE}, id="page-only"),
            pytest.param({"manifest": _authored_manifest()}, id="manifest-only"),
            pytest.param(
                {"template_id": "fixture-board", "manifest": _authored_manifest()},
                id="beside-a-template-id",
            ),
        ],
    )
    def test_a_page_the_agent_wrote_is_refused_without_reaching_the_gateway(
        self, _verified_caller: Any, args: dict[str, Any]
    ) -> None:
        """Answered locally, with the SAME sentence the store would have given.

        Two wordings of one rule is how an agent ends up retrying the other one, so
        this tool does not phrase its own; and refusing before the post means an agent
        that wrote a page learns why without spending a round trip on it.
        """
        with patch("kiro_crew.mcp_panel._post") as mock_post:
            out = _call_tool_inner("dashboard_preview", args)
        assert out == f"Error: {instance.AUTHORED_PAGE_REFUSAL}"
        assert "come later" in out
        mock_post.assert_not_called()

    def test_previewing_nothing_is_refused_locally(self, _verified_caller: Any) -> None:
        with patch("kiro_crew.mcp_panel._post") as mock_post:
            out = _call_tool_inner("dashboard_preview", {})
        assert out.startswith("Error:") and "nothing to preview" in out
        mock_post.assert_not_called()

    def test_preview_advertises_one_argument(self) -> None:
        """The surface an agent reads before it tries to write a page.

        A schema still offering ``manifest`` and ``html`` would invite exactly the call
        the store refuses, and the refusal is a worse teacher than never asking.
        """
        tool = next(t for t in _list_tools() if t["name"] == "dashboard_preview")
        assert set(tool["inputSchema"]["properties"]) == {"template_id"}
        assert "CANNOT preview a page you wrote" in tool["description"]

    def test_a_refusal_from_the_gateway_comes_back_whole(self, _verified_caller: Any) -> None:
        """A parity refusal names every unpaired field in both directions.

        That list is what somebody fixes a hand-edited template from, so shortening it
        to a generic failure costs exactly the cycle the message exists to save.
        """
        refusal = (
            "this page and manifest do not load: template.html binds 'ghost', which "
            "the manifest does not declare"
        )
        with patch("kiro_crew.mcp_panel._post", return_value={"error": refusal}):
            out = _call_tool_inner("dashboard_preview", {"template_id": "fixture-broken"})
        assert "ghost" in out and "does not declare" in out


class TestDashboardApplyTool:
    def test_it_reports_the_new_version(self, _verified_caller: Any) -> None:
        body = {"ok": True, "instance_version": 4, "template_id": "fixture-board"}
        with patch("kiro_crew.mcp_panel._post", return_value=body) as mock_post:
            out = _call_tool_inner("dashboard_apply", {})
        assert "version 4" in out and "fixture-board" in out
        # NO BODY, which is the property the tool's description advertises.
        assert mock_post.call_args[0][1] == {}

    def test_apply_promises_no_saved_template(self, _verified_caller: Any) -> None:
        """Nothing is saved, so the answer must not claim a reusable template.

        An agent told it had one would quote an id to the person that names nothing.
        """
        tool = next(t for t in _list_tools() if t["name"] == "dashboard_apply")
        assert "reusable template" not in tool["description"]
        body = {"ok": True, "instance_version": 1, "template_id": "fixture-other"}
        with patch("kiro_crew.mcp_panel._post", return_value=body):
            out = _call_tool_inner("dashboard_apply", {})
        assert "saved" not in out

    def test_a_refusal_with_nothing_staged_comes_back(self, _verified_caller: Any) -> None:
        refusal = "there is no page staged to keep; preview one first"
        with patch("kiro_crew.mcp_panel._post", return_value={"error": refusal}):
            out = _call_tool_inner("dashboard_apply", {})
        assert out.startswith("Error:") and "preview one first" in out


class TestTheReplyNamesTheFeaturePreview:
    """The Dashboard tab draws this page only for readers with the preview on.

    With the 'Dynamic Dashboard' Feature Preview off, which is the default, the tab draws the
    panel_publish record instead. An agent told only "your page is now version 4"
    believes a person can see it, so the replies that touch the page say who can.
    """

    @staticmethod
    def _note() -> str:
        import kiro_crew.mcp_panel as mcp_panel

        note = getattr(mcp_panel, "DASHBOARD_PREVIEW_NOTE", None)
        assert note is not None, "mcp_panel must declare the feature-preview sentence"
        return str(note)

    def test_the_sentence_names_the_preview_and_the_fallback(self) -> None:
        note = self._note()
        # The switch's own label, so a person told it can find the row.
        assert "'Dynamic Dashboard' Feature Preview" in note
        assert "panel_publish" in note

    def test_apply_says_who_can_see_the_page(self, _verified_caller: Any) -> None:
        body = {"ok": True, "instance_version": 4, "template_id": "fixture-board"}
        with patch("kiro_crew.mcp_panel._post", return_value=body):
            out = _call_tool_inner("dashboard_apply", {})
        assert self._note() in out

    def test_a_refused_apply_does_not_carry_it(self, _verified_caller: Any) -> None:
        with patch("kiro_crew.mcp_panel._post", return_value={"error": "nothing staged"}):
            out = _call_tool_inner("dashboard_apply", {})
        assert self._note() not in out

    def test_write_says_who_can_see_the_page(self, _verified_caller: Any) -> None:
        body = {"ok": True, "written": {"field": "credits", "type": "number"}}
        with patch("kiro_crew.mcp_panel._post", return_value=body):
            out = _call_tool_inner("dashboard_write", {"field": "credits", "value": 3})
        assert self._note() in out

    def test_fields_says_who_can_see_the_page(self) -> None:
        out = _render_fields(
            {"template": {"id": "fixture-board", "version": 1}, "instance_version": 2}
        )
        assert self._note() in out


class TestDashboardRollbackTool:
    def test_it_reports_the_version_it_landed_on_not_the_one_asked_for(
        self, _verified_caller: Any
    ) -> None:
        """Those are two different numbers.

        A caller that reported the one it asked for would tell a person they are on a
        version nobody is on.
        """
        body = {"ok": True, "instance_version": 3, "restored_from": 1, "template_id": "x"}
        with patch("kiro_crew.mcp_panel._post", return_value=body):
            out = _call_tool_inner("dashboard_rollback", {"to_version": 1})
        assert "Restored version 1 as version 3" in out

    def test_version_zero_is_refused_before_the_gateway(self, _verified_caller: Any) -> None:
        with patch("kiro_crew.mcp_panel._post") as mock_post:
            out = _call_tool_inner("dashboard_rollback", {"to_version": 0})
        assert out.startswith("Error:") and "dashboard_fields" in out
        mock_post.assert_not_called()

    def test_a_dropped_version_refusal_names_the_ones_still_kept(
        self, _verified_caller: Any
    ) -> None:
        refusal = "instance version 2 is no longer kept; kept versions: [7, 8, 9]"
        with patch("kiro_crew.mcp_panel._post", return_value={"error": refusal}):
            out = _call_tool_inner("dashboard_rollback", {"to_version": 2})
        assert "[7, 8, 9]" in out


class TestTheFieldsReadCarriesTheHistory:
    """There is no history tool, and this is why there does not need to be one."""

    def test_the_history_and_the_reachable_versions_are_in_the_fields_payload(
        self,
    ) -> None:
        manifest = dashboard_agentic.TemplateManifest  # imported shape, not constructed
        assert manifest is not None
        payload = dashboard_agentic.fields_for_agent(
            None,
            None,
            history=[{"instance_version": 2, "action": "adopted", "template_id": "x", "at_ms": 9}],
            rollback_versions=[1, 2],
        )
        assert payload["history"] == [
            {"instance_version": 2, "action": "adopted", "template_id": "x", "at_ms": 9}
        ]
        assert payload["rollback_versions"] == [1, 2]

    def test_the_rows_are_trimmed_to_the_cap_keeping_the_recent_ones(self) -> None:
        rows = [
            {"instance_version": n, "action": "adopted", "template_id": "x", "at_ms": n}
            for n in range(1, dashboard_agentic.HISTORY_SHOWN + 6)
        ]
        payload = dashboard_agentic.fields_for_agent(None, None, history=rows)
        kept = payload["history"]
        assert len(kept) == dashboard_agentic.HISTORY_SHOWN
        # The END of the fold's order, because the fold keeps newest last and the rows
        # worth showing are the recent ones.
        assert kept[-1]["instance_version"] == rows[-1]["instance_version"]

    def test_the_rendered_read_names_the_versions_a_rollback_can_reach(self) -> None:
        """A shorter list than the history, and the one that prevents a sure refusal."""
        out = _render_fields(
            {
                "template": {"id": "fixture-board", "version": 1},
                "instance_version": 9,
                "fields": [],
                "mistakes": [],
                "history": [
                    {"instance_version": 8, "action": "adopted", "template_id": "x", "at_ms": 1}
                ],
                "rollback_versions": [8, 9],
            }
        )
        assert "v8: adopted" in out
        assert "dashboard_rollback can still reach: 8, 9" in out


# ------------------------------------------------- the refusal an agent reads


class TestARefusalReachesTheAgentWhole:
    """The stored row is capped; the sentence the agent reads is not.

    The 240-character cap bounds the crew-log entry. Applying it to the 400 body as
    well cut the sentence from the END, and the end is where the remedy is: the
    valid field names come first and the "you have made this mistake N times before,
    and 'x' worked" note last. A manifest with about five fields therefore pushed
    exactly the useful half past the cap, and an agent was told what it got wrong
    without being told what to do instead -- which is the cycle the mistake book
    exists to end.
    """

    @staticmethod
    def _long_refusal() -> dashboard_agentic.WriteRefused:
        """A refusal whose sentence runs well past the stored cap."""
        return dashboard_agentic.WriteRefused(
            "unknown_field",
            "ghost",
            "no dashboard field 'ghost'. Agentic fields: "
            + ", ".join(f"field_number_{n}" for n in range(12))
            + ". You have made this mistake 4 times before; 'phase' worked.",
        )

    def test_the_sentence_is_handed_back_whole(self) -> None:
        refused = self._long_refusal()
        sentence = dashboard_agentic.refusal_sentence(refused)
        assert len(sentence) > 240, "this case needs a sentence past the cap to be about it"
        # The REMEDY, which is the tail and therefore the first thing a cut drops.
        assert sentence.endswith("'phase' worked.")

    def test_the_stored_row_is_a_prefix_of_that_sentence(self) -> None:
        """One wording, cut in one place.

        Two wordings of one rule is how an agent ends up unsure which it broke, so
        the record is not allowed to be a second sentence -- only a shorter one.
        """
        refused = self._long_refusal()
        stored = dashboard_agentic.refusal_entry(refused)["reason"]
        assert len(stored) == 240
        assert dashboard_agentic.refusal_sentence(refused).startswith(stored)

    def test_both_halves_run_through_the_same_scrub(self) -> None:
        """The cap is the only difference between them.

        A page binding the mistake book draws these as text, so a credential-shaped
        field name reaching either one unscrubbed would be rendered verbatim on the
        dashboard.
        """
        leaked = dashboard_agentic.WriteRefused(
            "unknown_field",
            "tok",
            "no dashboard field 'tok'. Try AKIAIOSFODNN7EXAMPLE instead.",
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in dashboard_agentic.refusal_sentence(leaked)
        assert "AKIAIOSFODNN7EXAMPLE" not in dashboard_agentic.refusal_entry(leaked)["reason"]


# ------------------------------------------- the discard happens under the lock


def test_the_consumed_preview_is_discarded_inside_the_instance_lock() -> None:
    """Read off the function's own syntax tree, because the race is not reachable.

    With the discard outside the lock, an apply commits, releases, and only then
    deletes whatever preview is staged at that moment -- so an apply racing a
    preview throws away the page that was just staged, and a second apply arriving
    in the same window consumes the first one's preview before it is deleted at all.
    Neither sequence can be driven from a test: the window is between two real
    syscalls. What CAN be checked exactly is the thing that closes it, which is the
    call sitting inside the ``with _locked(...)`` body.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(instance.apply_preview)))
    withs = [n for n in ast.walk(tree) if isinstance(n, ast.With)]
    assert withs, "apply_preview no longer takes the instance lock at all"
    inside = {
        node.func.id
        for w in withs
        for node in ast.walk(w)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "discard_preview" in inside, (
        "the consumed preview is discarded outside the instance lock, so an apply "
        "racing a preview can delete the page that was just staged"
    )


def test_applying_twice_cannot_install_the_same_page_twice() -> None:
    """The behaviour the discard is for, checked on the path a caller can drive."""
    instance.stage_preview(SLUG, template_id="fixture-board")
    instance.apply_preview(SLUG, session_id="")
    with pytest.raises(instance.InstanceRefused):
        instance.apply_preview(SLUG, session_id="")
    assert instance.read(SLUG).instance_version == 1


# --------------------------------------- redaction keeps every row it scrubs


class TestRedactionDoesNotLoseRows:
    """Two keys that scrub to one placeholder are suffixed, not collapsed.

    The collapse was recorded as an acceptable trade on the grounds that such keys
    must not be displayed anyway. The label is indeed unreadable either way -- but
    each key carries a VALUE the agent wrote and meant to store, so a two-row write
    landing as one row loses a row with nothing raised and nothing in the mistake
    book. `member_dashboard._page_safe` already suffixes for exactly this reason.
    """

    @staticmethod
    def _two_colliding_keys() -> dict[str, str]:
        """Two DIFFERENT credential-shaped keys that redact to the same text.

        Assembled at runtime, like every other credential in this suite: the
        internal-content scan reads this change's own diff. Do not join them up.
        """
        head = "AKIA" + "IOSFODNN7"
        return {f"{head}EXAMPLE": "first row", f"{head}SAMPLE2": "second row"}

    def test_both_values_survive_a_key_collision(self) -> None:
        raw = self._two_colliding_keys()
        assert len(raw) == 2, "the fixture no longer carries two distinct keys"
        out = dashboard_agentic._redacted(raw)
        assert len(out) == 2, f"a row was lost to a redacted-key collision: {out}"
        assert sorted(out.values()) == ["first row", "second row"]

    def test_the_second_key_takes_a_numbered_suffix(self) -> None:
        """The same shape ``_page_safe`` gives a colliding fold key."""
        out = dashboard_agentic._redacted(self._two_colliding_keys())
        suffixed = [k for k in out if k.endswith(" (2)")]
        assert len(suffixed) == 1, f"no suffixed key: {sorted(out)}"

    def test_a_third_collision_keeps_counting(self) -> None:
        head = "AKIA" + "IOSFODNN7"
        raw = {f"{head}EXAMPLE": 1, f"{head}SAMPLE2": 2, f"{head}SAMPLE3": 3}
        out = dashboard_agentic._redacted(raw)
        assert len(out) == 3, f"a row was lost: {out}"
        assert sorted(out.values()) == [1, 2, 3]

    def test_keys_that_do_not_collide_are_untouched(self) -> None:
        """A control: the suffix is not applied to ordinary keys."""
        out = dashboard_agentic._redacted({"alpha": 1, "beta": 2})
        assert out == {"alpha": 1, "beta": 2}
