"""``view.html``: the package shape for a page its owner WROTE, rather than composed.

A ``kind="dashboard"`` package has carried one kind of view -- ``view.blocks``, a list of
block types out of a closed catalog, drawn by host renderers. That shape cannot hold the
pages this surface exists for: a dispatch tree, a chart, a 3D scene are layouts no fixed
renderer knows, and a catalog that grew a type per picture would be the host writing every
page anyway.

So a view is EITHER a block list or one HTML page, never both and never neither. The page
is agent-authored and may run JS, and what keeps that safe is not a check on the markup --
it is :mod:`kiro_crew.dashboard_frame`: text-only bindings, ``connect-src 'none'``,
``allow-scripts`` and nothing else, over an opaque origin. The checks here are the ones
that catch an author's mistake while the author is still present:

* a page that ships its own ``<html>`` would nest documents inside the host's;
* a binding naming a field the model does not declare is a cell that stays blank forever,
  and the model is also the push allowlist, so the typo is silent twice;
* a page over the view cap is one the frame would refuse to draw.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.artifacts import ArtifactValidationError

BOUND = "crewmate:mate"

#: The two fields every page below binds, and the only two its model declares.
TYPES: dict[str, Any] = {
    "boards": {
        "type": "number",
        "label": "Boards",
        "source": {"fold": "worktree", "path": "boards"},
    },
    "conductor": {
        "type": "text",
        "label": "Conductor",
        "source": {"fold": "work", "path": "conductor"},
    },
}


def package(**over: Any) -> dict[str, Any]:
    """A package whose view is one HTML page, with each part overridable."""
    view = over.pop("view", {"html": '<p data-dashboard-field="boards"></p>'})
    return {
        "kind": "dashboard",
        "bound_to": over.pop("bound_to", BOUND),
        "model": {"types": over.pop("types", TYPES)},
        "view": view,
        "theme": over.pop("theme", {"tokens": {}}),
        **over,
    }


def refusal(raw: dict[str, Any]) -> str:
    with pytest.raises(ArtifactValidationError) as caught:
        dp.validate_package(raw)
    return str(caught.value)


class TestOneViewOrTheOther:
    def test_an_html_page_is_a_view(self) -> None:
        out = dp.validate_package(package())
        assert out["view"] == {"html": '<p data-dashboard-field="boards"></p>'}
        assert "blocks" not in out["view"]

    def test_a_block_list_is_still_a_view(self) -> None:
        """The shape every stored package uses today. A new view kind that invalidated
        the old one would make this change a migration rather than an addition."""
        out = dp.validate_package(
            package(view={"blocks": [{"id": "b", "type": "stat", "fields": ["boards"]}]})
        )
        assert out["view"]["blocks"][0]["id"] == "b"

    def test_both_at_once_is_refused(self) -> None:
        """Two views is a package with no answer to "what does this page look like".
        Drawing one and ignoring the other would pick for the author silently, and the
        half that was ignored is the half they were editing."""
        said = refusal(
            package(
                view={
                    "html": "<p>x</p>",
                    "blocks": [{"id": "b", "type": "stat", "fields": ["boards"]}],
                }
            )
        )
        assert "view" in said
        assert "html" in said and "blocks" in said

    def test_neither_is_refused(self) -> None:
        said = refusal(package(view={}))
        assert "html" in said and "blocks" in said


class TestThePageItself:
    def test_a_page_carrying_a_document_skeleton_is_refused(self) -> None:
        """The host supplies ``<!doctype>``, ``<html>``, ``<head>`` and ``<body>`` and
        owns the page's security policy. A page shipping its own nests documents, and it
        could carry a second CSP ``<meta>`` -- not a bypass, since policies intersect,
        but a page that renders wrong for a reason its author cannot see."""
        for skeleton in ("<!doctype html><p>x</p>", "<html><p>x</p>", "<body><p>x</p>"):
            said = refusal(package(view={"html": skeleton}))
            assert "view.html" in said

    def test_a_blank_page_is_refused(self) -> None:
        for blank in ("", "   \n"):
            assert "view.html" in refusal(package(view={"html": blank}))

    def test_a_non_string_page_is_refused(self) -> None:
        assert "view.html" in refusal(package(view={"html": 42}))

    def test_a_page_over_the_view_cap_is_refused(self) -> None:
        """The refusal names the limit, so the author knows how much to take out."""
        said = refusal(package(view={"html": "<p>" + "x" * dp.MAX_VIEW_HTML_BYTES + "</p>"}))
        assert str(dp.MAX_VIEW_HTML_BYTES) in said

    def test_a_page_may_run_its_own_script(self) -> None:
        """JS is the contract, not a loophole. A page that could only print text cannot
        draw a tree -- and the containment is the frame's CSP and sandbox, which hold
        whatever the page's script does."""
        html = (
            '<div id="tree"></div>'
            "<script>window.kirocrew.subscribe('boards', (n) => draw(n));</script>"
        )
        assert dp.validate_package(package(view={"html": html}))["view"]["html"] == html

    def test_a_page_may_bind_every_declared_field_and_none_other(self) -> None:
        html = '<p data-dashboard-field="boards"></p>' "<p data-dashboard-field='conductor'></p>"
        assert dp.validate_package(package(view={"html": html}))["view"]["html"] == html


class TestTheModelIsTheAllowlist:
    def test_a_binding_the_model_does_not_declare_is_refused(self) -> None:
        """Silent twice over, which is why it is worth refusing at save time: the cell
        stays blank forever, and the model is also the list of fields the controller
        pushes, so nothing will ever arrive to fill it either."""
        said = refusal(package(view={"html": '<p data-dashboard-field="bords"></p>'}))
        assert "bords" in said
        assert "boards" in said, "the refusal does not list the names that would work"

    def test_a_single_quoted_binding_is_checked_too(self) -> None:
        """An author writes whichever quote their editor inserts, and a check that reads
        only one of them turns the other into the spelling that skips validation."""
        assert "bords" in refusal(package(view={"html": "<p data-dashboard-field='bords'></p>"}))

    def test_a_page_that_binds_nothing_is_allowed(self) -> None:
        """A page may draw entirely from ``window.kirocrew.subscribe`` and carry no text
        binding at all. Requiring one would refuse exactly the pages this view kind was
        added for -- a canvas, a scene, an SVG tree."""
        html = "<canvas id=t></canvas><script>window.kirocrew.subscribe('boards', f);</script>"
        assert dp.validate_package(package(view={"html": html}))["view"]["html"] == html


_HEAD = '<p data-dashboard-field="boards"></p><script>'
_TAIL = "</script>"


def _page_of(size: int, fill: str = "x") -> str:
    """A page of exactly *size* bytes of UTF-8, binding a declared field, *fill* inlined."""
    return _HEAD + fill * (size - len(_HEAD) - len(_TAIL)) + _TAIL


def _save(raw: dict[str, Any]) -> str:
    """Through THE write gate, the one that also measures the stored form."""
    return dp.canonical_package_content(json.dumps(raw, ensure_ascii=False))


class TestTheSizeCaps:
    """The page cap and the package cap, each at its boundary, through the write gate.

    A page that inlines a client-side library with its own scene code is a few MB of
    layout with no data in it, so both caps are sized for it: the page cap is the
    frame's own, and the package cap leaves room for that page once JSON escapes it.
    """

    def test_a_page_exactly_at_the_cap_saves(self) -> None:
        page = _page_of(dp.MAX_VIEW_HTML_BYTES)
        assert len(page.encode("utf-8")) == dp.MAX_VIEW_HTML_BYTES
        stored = _save(package(view={"html": page}))
        assert dp.parse_package(stored)["view"]["html"] == page

    def test_one_byte_over_the_cap_is_refused_naming_the_cap(self) -> None:
        with pytest.raises(ArtifactValidationError) as caught:
            _save(package(view={"html": _page_of(dp.MAX_VIEW_HTML_BYTES + 1)}))
        said = str(caught.value)
        assert "view.html" in said
        assert f"is {dp.MAX_VIEW_HTML_BYTES + 1} bytes" in said
        assert f"over the {dp.MAX_VIEW_HTML_BYTES}-byte limit for a page" in said

    def test_a_page_at_the_cap_that_doubles_once_stored_still_saves(self) -> None:
        """The worst case for the stored form, with a model and a theme beside it.

        JSON writes each quote in the page as two bytes, so a page made of them takes
        twice its size in the package. The package cap is sized for exactly that: a
        page the page cap allows is never refused by the package cap instead.
        """
        page = _page_of(dp.MAX_VIEW_HTML_BYTES, fill='"')
        theme = {"tokens": {"--accent": "#0af"}, "css": "#scene { inset: 0; }"}
        stored = _save(package(view={"html": page}, theme=theme))
        assert len(stored.encode("utf-8")) > 2 * dp.MAX_VIEW_HTML_BYTES - 1024
        assert dp.parse_package(stored)["view"]["html"] == page

    def test_a_stored_package_at_its_cap_saves_and_one_byte_more_is_refused(self) -> None:
        """The package cap's own boundary, measured on the STORED form.

        A page at the page cap reaches it only with more than 128 KiB beside it, so the
        model carries forty enum fields. Swapping one ``x`` in the page for a quote adds
        exactly one stored byte, which is what lands the package on the cap exactly.
        """
        filler = {
            f"pick_{i:02d}": {
                "type": "enum",
                "label": "Pick",
                "choices": [f"choice {j:02d} " + "c" * 100 for j in range(32)],
                "source": {"agentic": True},
            }
            for i in range(40)
        }
        types = {**TYPES, **filler}
        room = dp.MAX_VIEW_HTML_BYTES - len(_HEAD) - len(_TAIL)

        def at(quotes: int) -> dict[str, Any]:
            html = _HEAD + '"' * quotes + "x" * (room - quotes) + _TAIL
            return package(types=types, view={"html": html})

        plain = len(_save(at(0)).encode("utf-8"))
        quotes = dp.MAX_PACKAGE_BYTES - plain
        assert 0 < quotes < room, "the filler does not put the cap in reach"
        assert len(_save(at(quotes)).encode("utf-8")) == dp.MAX_PACKAGE_BYTES
        with pytest.raises(ArtifactValidationError) as caught:
            _save(at(quotes + 1))
        said = str(caught.value)
        assert f"is {dp.MAX_PACKAGE_BYTES + 1} bytes once stored" in said
        assert f"over the {dp.MAX_PACKAGE_BYTES}-byte limit" in said


class TestVersioningAndRevertSeeAPage:
    def test_editing_the_page_is_a_layout_change(self) -> None:
        """The page IS the layout now, so a version has to be cut for an edit to it --
        otherwise the one thing an owner would want to roll back is the one thing no
        version records."""
        before = dp.dump_package(dp.validate_package(package()))
        after = dp.dump_package(
            dp.validate_package(package(view={"html": '<h1 data-dashboard-field="boards"></h1>'}))
        )
        assert dp.layout_changed(before, after) is True

    def test_re_storing_the_same_page_is_not(self) -> None:
        content = dp.dump_package(dp.validate_package(package()))
        assert dp.layout_changed(content, content) is False

    def test_a_revert_restores_the_page_and_keeps_the_live_binding(self) -> None:
        """A version from before a rebind carries the OLD binding, so restoring content
        wholesale would hand this crewmate's page to whoever that stale binding names."""
        target = dp.dump_package(
            dp.validate_package(package(bound_to="crewmate:old", view={"html": "<p>target</p>"}))
        )
        live = dp.dump_package(dp.validate_package(package(bound_to="crewmate:now")))
        back = dp.parse_package(dp.revert_package(live, target))
        assert back["view"] == {"html": "<p>target</p>"}
        assert back["bound_to"] == "crewmate:now"


def test_the_subscribed_fields_are_read_off_the_model() -> None:
    """ONE function the controller asks "what may this page be sent", so the push gate
    and the save gate read one document.

    Spelled as ``fold:<path>`` and ``custom:<key>`` -- the brief's two source kinds,
    mapped onto the two the package grammar already admits (``{fold, path}`` and
    ``{agentic: true}``) rather than a third spelling of the same thing.
    """
    out = dp.validate_package(package())
    assert dp.subscribed_fields(out) == {
        "boards": "fold:worktree.boards",
        "conductor": "fold:work.conductor",
    }


def test_an_agentic_field_is_a_custom_source() -> None:
    types = dict(TYPES)
    types["verdict"] = {"type": "text", "label": "Verdict", "source": {"agentic": True}}
    out = dp.validate_package(package(types=types))
    assert dp.subscribed_fields(out)["verdict"] == "custom:verdict"


def test_the_binding_attribute_matches_the_frame() -> None:
    """The gate that checks a page's bindings and the frame that fills them cannot
    disagree about what a binding IS.

    Mirrored rather than imported: this module is on the artifact write path and the
    frame is the panel read path's. A drift either way is silent in the worst direction
    -- the gate would pass a page whose bindings the frame never fills, or refuse a
    field name the frame would have filled.
    """
    from kiro_crew import dashboard_frame

    assert dashboard_frame.BINDING_ATTRIBUTE in dp._HTML_BINDING_RE.pattern


def test_the_view_cap_is_under_the_package_cap() -> None:
    """A page that could fill the whole package would leave no room for the model that
    says what may be pushed into it -- and a package with a page and no model is one its
    own reader refuses."""
    assert dp.MAX_VIEW_HTML_BYTES < dp.MAX_PACKAGE_BYTES


def test_the_page_cap_is_the_frames_own() -> None:
    """A page a package can hold is a page the frame will draw.

    Mirrored rather than imported, for the binding attribute's reason above. A page cap
    over the frame's would let a package save a page its own reader then refuses, and
    the tab would show the refusal where the page should be.
    """
    from kiro_crew import dashboard_frame

    assert dp.MAX_VIEW_HTML_BYTES == dashboard_frame.MAX_PAGE_BYTES


class TestAScriptsOffPageStillShowsItsValues:
    """THE BOOTSTRAP IS WHAT FILLS A BINDING, so a page mounted without it filled none.

    With ``scripts: false`` the frame mounts under an empty sandbox and no script runs.
    Every bound cell was drawn EMPTY and the stale band stayed hidden, so the page read
    as a current one showing nothing -- the one thing this surface must not do.

    Filled server-side instead, ESCAPED, which keeps the frame's own promise that a
    value can never become markup: the same guarantee the ``textContent`` assignment
    makes, made in Python.
    """

    @staticmethod
    def _read(**fields: Any) -> dict[str, Any]:
        from kiro_crew import dashboard_frame

        return dashboard_frame.read_payload(dict(fields))

    def test_a_quoted_cell_is_filled(self) -> None:
        from kiro_crew import dashboard_frame as frame

        out = frame.fill_bindings_as_text(
            '<p data-dashboard-field="goal"></p>', self._read(goal="land it")
        )
        assert ">land it<" in out

    def test_an_unquoted_cell_is_filled_too(self) -> None:
        """The attribute's value need not be quoted for a browser to read it, so it
        must not need quoting for this to find it either."""
        from kiro_crew import dashboard_frame as frame

        out = frame.fill_bindings_as_text(
            "<span data-dashboard-field=goal></span>", self._read(goal="land it")
        )
        assert ">land it<" in out

    def test_a_value_cannot_become_markup(self) -> None:
        """The promise this path has to keep. A value is TEXT, so it is escaped before
        it is placed -- otherwise the scripts-off page would be the one place a value
        could inject."""
        from kiro_crew import dashboard_frame as frame

        out = frame.fill_bindings_as_text(
            '<p data-dashboard-field="goal"></p>', self._read(goal="<img onerror=x>")
        )
        assert "<img" not in out
        assert "&lt;img" in out

    def test_a_cell_holding_markup_is_left_alone(self) -> None:
        """A binding is a text cell by definition, so an element holding tags was never
        one; rewriting it would be this fill guessing at a structure the contract does
        not describe."""
        from kiro_crew import dashboard_frame as frame

        page = '<b data-dashboard-field="goal"><i>nested</i></b>'
        assert frame.fill_bindings_as_text(page, self._read(goal="land it")) == page

    def test_a_field_the_read_does_not_carry_keeps_its_own_text(self) -> None:
        """The model is the allowlist on this path as on every other: a cell naming a
        field the read does not carry is not filled with a dash by this function, it is
        left exactly as the author wrote it."""
        from kiro_crew import dashboard_frame as frame

        page = '<p data-dashboard-field="other">author text</p>'
        assert frame.fill_bindings_as_text(page, self._read(goal="land it")) == page

    @pytest.mark.timeout(60)
    @pytest.mark.parametrize(
        ("prefix", "unit", "suffix"),
        [
            pytest.param("", "<a ", "", id="opening-brackets-only"),
            pytest.param("", "<a data-dashboard-field=x ", "", id="bound-tags-never-closed"),
            pytest.param("<", "a-", "", id="hyphenated-tag-name"),
            pytest.param("<a ", "data-dashboard-field=gp ", "", id="one-tag-many-attributes"),
            pytest.param(
                "<a ",
                "data-dashboard-field=x ",
                ">t</b>",
                id="one-tag-many-attributes-wrong-close",
            ),
        ],
    )
    def test_a_page_built_to_stall_the_scan_is_filled_in_linear_time(
        self, prefix: str, unit: str, suffix: str
    ) -> None:
        """EACH START TAG IS READ ONCE, so no page stalls the fill however it is cut.

        One page per way a scan can re-read what it has read: every ``<a`` runs on to
        the end of a page with no ``>`` after it; one tag name is cut again at every
        ``-``; one start tag names the attribute many times, each reading on to the
        same ``>`` and each failing at the same wrong close. A quadratic form takes
        about a second at 16-64 KiB, and the cost grows with the square, so a page at
        the cap holds the GIL for hours, which the ``_run`` offload cannot help. Each
        page below is at the cap. A quadratic form does not finish it, and the
        per-test timeout is what fails a regression: no time is asserted.
        """
        from kiro_crew import dashboard_frame as frame

        cell = '<p data-dashboard-field="goal"></p>'
        room = frame.MAX_PAGE_BYTES - len(prefix) - len(suffix) - len(cell)
        page = prefix + unit * (room // len(unit)) + suffix + cell
        assert len(page.encode("utf-8")) <= frame.MAX_PAGE_BYTES
        out = frame.fill_bindings_as_text(page, self._read(goal="land it"))
        assert out.endswith('<p data-dashboard-field="goal">land it</p>')

    def test_a_start_tag_is_not_read_across_the_next_tag(self) -> None:
        """The price of the linear scan, pinned: a literal ``<`` inside a start tag
        ends the search, so the cell keeps the author's own text."""
        from kiro_crew import dashboard_frame as frame

        page = '<p title="a<b" data-dashboard-field="goal">author text</p>'
        assert frame.fill_bindings_as_text(page, self._read(goal="land it")) == page

    @pytest.mark.parametrize(
        ("name", "wrong_close"),
        [
            pytest.param("p-", "p", id="name-ends-in-a-hyphen"),
            pytest.param("my-el", "my", id="custom-element"),
        ],
    )
    def test_a_hyphenated_tag_name_is_read_whole(self, name: str, wrong_close: str) -> None:
        """``<my-el>`` is an element named ``my-el``, so only ``</my-el>`` closes it.
        Reading the name as ``my`` and then closing it with ``</my>`` is what cut the
        name at every ``-``, and a browser does not read it that way either."""
        from kiro_crew import dashboard_frame as frame

        read = self._read(goal="land it")
        closed = f"<{name} data-dashboard-field=goal>author text</{name}>"
        assert frame.fill_bindings_as_text(closed, read) == (
            f"<{name} data-dashboard-field=goal>land it</{name}>"
        )
        unclosed = f"<{name} data-dashboard-field=goal>author text</{wrong_close}>"
        assert frame.fill_bindings_as_text(unclosed, read) == unclosed

    def test_the_route_fills_only_when_script_is_withheld(self) -> None:
        """Source-level, because the ORDER is the mechanism: a page that will run its
        own bootstrap must not also be filled here, or the two would disagree about one
        read for as long as it takes the script to run."""
        import inspect

        from kiro_crew.dashboard.handlers import member_dashboard_fields as fields

        src = inspect.getsource(fields.render_package_page)
        assert "if not scripts:" in src
        assert "fill_bindings_as_text" in src

    def test_a_quoted_gt_after_the_binding_does_not_cut_the_tag(self) -> None:
        """A ``>`` inside a double-quoted value AFTER the binding attribute is part of
        the value, not the end of the tag. Read with ``[^>]`` the tag ended at the
        quoted ``>``, so the fill overwrote the rest of the attribute and the original
        text -- a corrupted page served as a current one."""
        from kiro_crew import dashboard_frame as frame

        page = '<span data-dashboard-field="goal" title="a>b">old</span>'
        out = frame.fill_bindings_as_text(page, self._read(goal="land it"))
        assert out == '<span data-dashboard-field="goal" title="a>b">land it</span>'

    def test_a_quoted_gt_before_the_binding_still_finds_the_cell(self) -> None:
        """A ``>`` inside a double-quoted value BEFORE the binding attribute is part of
        the value too. Read with ``[^>]`` the tag ended before the binding attribute was
        reached, so the cell was silently left unfilled -- an empty cell on a page that
        reads as current."""
        from kiro_crew import dashboard_frame as frame

        page = '<span title="a>b" data-dashboard-field="goal">old</span>'
        out = frame.fill_bindings_as_text(page, self._read(goal="land it"))
        assert out == '<span title="a>b" data-dashboard-field="goal">land it</span>'

    def test_a_single_quoted_gt_after_the_binding_does_not_cut_the_tag(self) -> None:
        """The same for a single-quoted value: the two quoting styles are units alike."""
        from kiro_crew import dashboard_frame as frame

        page = "<span data-dashboard-field='goal' title='a>b'>old</span>"
        out = frame.fill_bindings_as_text(page, self._read(goal="land it"))
        assert out == "<span data-dashboard-field='goal' title='a>b'>land it</span>"

    def test_a_single_quoted_gt_before_the_binding_still_finds_the_cell(self) -> None:
        """And a single-quoted ``>`` before the binding attribute is part of the value."""
        from kiro_crew import dashboard_frame as frame

        page = "<span title='a>b' data-dashboard-field='goal'>old</span>"
        out = frame.fill_bindings_as_text(page, self._read(goal="land it"))
        assert out == "<span title='a>b' data-dashboard-field='goal'>land it</span>"

    def test_a_value_that_reads_like_the_binding_attribute_is_not_the_binding(self) -> None:
        """A quoted value may contain the literal text ``data-dashboard-field=`` without
        being the binding attribute. The reader must see it as one value and find the
        real binding attribute that follows, not stop inside the decoy."""
        from kiro_crew import dashboard_frame as frame

        page = '<span title="data-dashboard-field=x" data-dashboard-field="goal">old</span>'
        out = frame.fill_bindings_as_text(page, self._read(goal="land it"))
        assert out == (
            '<span title="data-dashboard-field=x" data-dashboard-field="goal">land it</span>'
        )

    def test_a_cell_with_a_quoted_gt_and_nested_markup_is_still_left_alone(self) -> None:
        """The no-nested-markup rule holds through a quoted ``>``: an element whose start
        tag carries a quoted ``>`` but whose body holds tags is not a text cell, so it is
        left exactly as the author wrote it rather than rewritten."""
        from kiro_crew import dashboard_frame as frame

        page = '<b data-dashboard-field="goal" title="a>b"><i>nested</i></b>'
        assert frame.fill_bindings_as_text(page, self._read(goal="land it")) == page


class TestThePageWearsItsTheme:
    """A package's ``theme`` is drawn on its page: tokens on ``:root``, then its css.

    The gate accepts both halves and the save tool tells the author to set the look
    there, so a renderer that dropped them served a page in the host's plain look while
    its author had been told the style was live.
    """

    @staticmethod
    def _body(theme: dict[str, Any]) -> str:
        from kiro_crew import dashboard_frame
        from kiro_crew.dashboard.handlers import member_dashboard_fields as fields

        read = dashboard_frame.read_payload({"boards": 2})
        return fields.render_package_page(dp.validate_package(package(theme=theme)), read)

    def test_the_tokens_and_the_css_reach_the_page(self) -> None:
        body = self._body(
            {"tokens": {"--panel-bg": "#20262a"}, "css": ".board{font-family:cursive}"}
        )
        assert ":root{--panel-bg:#20262a;}" in body
        assert ".board{font-family:cursive}" in body
        # After the host's stylesheet, so a token the package sets wins over the default.
        assert body.index("--panel-bg:#20262a") > body.index("<style>")
        assert body.index(".board{font-family:cursive}") < body.index(
            '<p data-dashboard-field="boards">'
        )

    def test_an_empty_theme_adds_no_style(self) -> None:
        assert self._body({"tokens": {}}).count("<style>") == 1

    def test_a_theme_the_gate_would_refuse_draws_nothing(self) -> None:
        from kiro_crew import dashboard_frame
        from kiro_crew.dashboard.handlers import member_dashboard_fields as fields

        stored = dp.validate_package(package())
        # Written under an older validator: a css that closes the element.
        stored["theme"] = {"tokens": {}, "css": "</style><script>alert(1)</script>"}
        body = fields.render_package_page(stored, dashboard_frame.read_payload({"boards": 2}))
        assert "alert(1)" not in body
