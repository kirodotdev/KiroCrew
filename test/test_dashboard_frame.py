"""The frame: what a template's page must be, and what the minted document grants.

A template's `template.html` is an inert body, never a document. This file pins
both halves of that: `validate_page` refuses anything that is already a document
or is too large, and `compose` mints the body into a page whose own policy closes
egress while allowing the page's script to run.

Kept apart from the write path's tests because the frame knows nothing about who
filled a field -- it draws whatever payload it is handed.
"""

from __future__ import annotations

import json
import random
import re
from typing import Any

import pytest

from kiro_crew import agent_panel, dashboard_frame


def _page_of(size: int) -> str:
    """A page of exactly *size* bytes of UTF-8: markup around one inlined script."""
    head, tail = '<div id="scene"></div><script>', "</script>"
    return head + "x" * (size - len(head) - len(tail)) + tail


class TestThePageContract:
    def test_an_empty_page_is_refused(self) -> None:
        with pytest.raises(dashboard_frame.PageError) as caught:
            dashboard_frame.validate_page("  \n ")
        assert caught.value.code == "page_empty"

    @pytest.mark.parametrize(
        "skeleton", ["<!doctype html><div>x</div>", "<html><p>x", "<div></div><body>", "<HEAD>"]
    )
    def test_a_document_skeleton_is_refused(self, skeleton: str) -> None:
        with pytest.raises(dashboard_frame.PageError) as caught:
            dashboard_frame.validate_page(skeleton)
        assert caught.value.code == "page_has_skeleton"

    @pytest.mark.parametrize(
        "page",
        [
            '<div>x</div><script>var s="<body class=x>";</script>',
            '<p>chart</p><style>p::before{content:"<html"}</style>',
            '<script>var a="<head>",b="<!doctype html>";</script><p>ok</p>',
        ],
    )
    def test_a_skeleton_looking_string_inside_a_raw_text_element_is_allowed(
        self, page: str
    ) -> None:
        """A ``<script>``/``<style>`` element's content is text, not markup -- so
        the ``"<body"`` that minified ECharts carries in a string literal is a
        string, and the page that inlines it is still a single inert body."""
        assert dashboard_frame.validate_page(page) == len(page.encode("utf-8"))

    @pytest.mark.parametrize(
        "page",
        [
            "<div>x</div><script>var a=1;</script><body>",
            "<p>x</p><style>b{}</style><html>",
            '<script>var s="<!--";</script><body>',
        ],
    )
    def test_a_real_skeleton_tag_after_a_raw_text_element_is_still_refused(self, page: str) -> None:
        """Skipping a script's content must not blind the scan to a real document
        tag that follows the closing tag. The ``<!--`` case pins that a comment
        start inside a script does not run on past ``</script>`` to hide a later
        ``<body>``."""
        with pytest.raises(dashboard_frame.PageError) as caught:
            dashboard_frame.validate_page(page)
        assert caught.value.code == "page_has_skeleton"

    def test_an_unterminated_raw_text_element_grants_no_cover(self) -> None:
        """Fail-safe: an unclosed ``<script>`` must not swallow a real document
        tag. When the boundary is in doubt we refuse rather than under-refuse."""
        with pytest.raises(dashboard_frame.PageError) as caught:
            dashboard_frame.validate_page('<script>var s="<body>"')
        assert caught.value.code == "page_has_skeleton"

    def test_the_cap_is_measured_in_bytes_not_characters(self) -> None:
        page = "<p>" + "\u00e9" * (dashboard_frame.MAX_PAGE_BYTES // 2)
        assert len(page) < dashboard_frame.MAX_PAGE_BYTES
        with pytest.raises(dashboard_frame.PageError) as caught:
            dashboard_frame.validate_page(page)
        assert caught.value.code == "page_too_large"

    def test_the_cap_holds_a_page_that_inlines_a_library(self) -> None:
        """3 MiB: a client-side library and the page's own scene code, inlined."""
        assert dashboard_frame.MAX_PAGE_BYTES == 3 * 1024 * 1024

    def test_a_page_exactly_at_the_cap_is_accepted(self) -> None:
        page = _page_of(dashboard_frame.MAX_PAGE_BYTES)
        assert dashboard_frame.validate_page(page) == dashboard_frame.MAX_PAGE_BYTES

    def test_one_byte_over_the_cap_is_refused_naming_the_cap(self) -> None:
        with pytest.raises(dashboard_frame.PageError) as caught:
            dashboard_frame.validate_page(_page_of(dashboard_frame.MAX_PAGE_BYTES + 1))
        assert caught.value.code == "page_too_large"
        assert f"over the {dashboard_frame.MAX_PAGE_BYTES}-byte limit" in str(caught.value)


class TestTheFramesSecurityPolicy:
    def test_egress_is_closed(self) -> None:
        """This page holds real numbers from the operator's crew log, including a
        work ledger and a mistake book. A page that can both read those and open a
        channel is an exfiltration path with a data source attached."""
        assert "connect-src 'none'" in dashboard_frame.FRAME_CSP

    def test_nothing_is_allowed_by_default(self) -> None:
        assert dashboard_frame.FRAME_CSP.startswith("default-src 'none'")

    @pytest.mark.parametrize(
        "directive",
        ["frame-src 'none'", "object-src 'none'", "base-uri 'none'", "form-action 'none'"],
    )
    def test_the_closed_directives_stay_closed(self, directive: str) -> None:
        assert directive in dashboard_frame.FRAME_CSP

    def test_no_dynamic_execution_primitive_is_granted(self) -> None:
        assert "unsafe-eval" not in dashboard_frame.FRAME_CSP

    def test_the_cdn_allowlist_matches_the_browser_wrapper(self) -> None:
        """THE ONE DIRECTIVE THE TWO POLICIES MUST AGREE ON. They intersect, so an
        origin named only here is still blocked and the page fails to load its chart
        library with no clue why. Read out of that file rather than restated."""
        import re
        from pathlib import Path

        wrapper = (
            Path(__file__).resolve().parents[1] / "website" / "src" / "lib" / "widgetSrcdoc.ts"
        )
        policy = wrapper.read_text(encoding="utf-8")
        policy = policy.split("const cspFor")[1].split("const BASE_BODY_CSS")[0]
        assert set(dashboard_frame.RESOURCE_ORIGINS) == set(
            re.findall(r"https://[a-z0-9.\-]+", policy)
        )

    def test_the_sandbox_grant_is_scripts_only(self) -> None:
        """Read out of the component, so a grant added there reddens here."""
        from pathlib import Path

        component = (
            Path(__file__).resolve().parents[1]
            / "website"
            / "src"
            / "pages"
            / "members"
            / "CrewDynamicDashboard.tsx"
        )
        source = component.read_text(encoding="utf-8")
        assert "export const CREW_DASHBOARD_SANDBOX = 'allow-scripts'" in source
        # The static mount is the empty attribute, which is every restriction
        # applied; a non-empty value here would be a second grant set.
        assert "export const CREW_DASHBOARD_SANDBOX_STATIC = ''" in source
        # The one chooser between the two, and the only thing it may read is the
        # flag the SERVER sent: a page that could influence this value would be a
        # page that could grant itself script.
        assert (
            "export function crewDashboardSandbox(scripts: boolean): string {\n"
            "  return scripts ? CREW_DASHBOARD_SANDBOX : CREW_DASHBOARD_SANDBOX_STATIC\n"
            "}"
        ) in source
        # Every `sandbox=` attribute in the file must go through that chooser, so a
        # grant cannot be widened by one frame spelling its own. Checked on the
        # ATTRIBUTES rather than on the prose: the constants' own comments name the
        # withheld grants in order to explain why they are withheld.
        attributes = [line.strip() for line in source.splitlines() if "sandbox=" in line]
        assert attributes, "no sandboxed frame found; did the component change shape?"
        for line in attributes:
            assert line.startswith("sandbox={crewDashboardSandbox("), line
            assert line.endswith(")}"), line


class TestTheComposedDocument:
    def page(self) -> str:
        return (
            '<h1 data-dashboard-field="risk_note"></h1>'
            '<span data-dashboard-field="entries"></span>'
            "<canvas id=c></canvas>"
            "<script>var s = window.kirocrew.fields.entries;</script>"
        )

    @staticmethod
    def island(doc: str) -> str:
        return doc.split('id="kirocrew-dashboard-data">')[1].split("</script>")[0]

    def doc(self, **over: Any) -> str:
        read = dashboard_frame.read_payload(
            over.pop("fields", {"risk_note": "two blocked", "entries": 3}), **over
        )
        return dashboard_frame.compose(self.page(), read, title="Atlas")

    def test_the_host_writes_the_whole_document(self) -> None:
        doc = self.doc()
        assert doc.startswith("<!doctype html>")
        assert doc.rstrip().endswith("</html>")
        assert '<meta name="referrer" content="no-referrer">' in doc

    def test_the_page_is_placed_verbatim(self) -> None:
        """The template owns the body. Rewriting it would be the host quietly editing
        a page it then presents as the template's."""
        assert self.page() in self.doc()

    def test_the_data_island_precedes_the_page(self) -> None:
        """A page's own inline script runs during parse, so a bootstrap placed after
        it would leave the first script reading ``undefined`` -- and a chart that
        cannot see its data on first paint draws nothing."""
        doc = self.doc()
        assert doc.index("kirocrew-dashboard-data") < doc.index("<h1 data-dashboard-field")

    def test_the_ready_beacon_follows_the_page(self) -> None:
        """So a page's own scripts have run by the time readiness is decided."""
        doc = self.doc()
        assert doc.index("<h1 data-dashboard-field") < doc.index(dashboard_frame.READY_MESSAGE_TYPE)

    def test_the_init_guard_is_installed_before_the_page(self) -> None:
        """It has to see an error in the page's FIRST script.

        Separate scripts are independent, so a throw aborts only the script it
        happened in and every later one still runs: reaching the beacon says nothing
        about the page. The guard installed ahead of the page is what makes the
        difference observable at all, and installed after it would miss exactly the
        failure that matters most -- a library missing at the top of the page.
        """
        doc = self.doc()
        flag = dashboard_frame._INIT_FAILED_FLAG
        assert doc.index(flag) < doc.index("<h1 data-dashboard-field"), (
            "the init guard is installed after the page, so an error in the page's "
            "own first script is never recorded"
        )

    def test_the_beacon_withholds_readiness_when_initialization_failed(self) -> None:
        """Otherwise a blank page is promoted over the page that worked.

        The host keeps the last page that beaconed, so a crashed page that beacons
        anyway replaces a working one AND takes its recovery band with it: the reader
        is left with nothing and no statement that anything went wrong.
        """
        doc = self.doc()
        flag = dashboard_frame._INIT_FAILED_FLAG
        beacon = doc[doc.index(dashboard_frame.READY_MESSAGE_TYPE) - 400 :]
        assert flag in beacon, "the beacon posts without reading the init-failure flag"

    def test_the_refill_probe_sits_between_the_bootstrap_and_the_page(self) -> None:
        """The host refills in place only for a page that said it redraws.

        After the bootstrap, so the bootstrap's own refill listener is not counted as
        the page's; before the page, so the page's listeners are.
        """
        doc = self.doc()
        flag = dashboard_frame._REFILL_FLAG
        probe = doc.index(flag)
        assert (
            doc.index("JSON.parse(el.textContent)") < probe < doc.index("<h1 data-dashboard-field")
        )

    def test_the_band_does_not_count_as_a_page_that_refills(self) -> None:
        """The band script runs after the page and listens for the refill event. Through
        the wrapped ``addEventListener`` it would mark every page refill-capable, and a
        page that does not redraw would then show stale values instead of reloading."""
        assert "EventTarget.prototype.addEventListener.call(window" in dashboard_frame._BAND_JS

    def test_the_beacon_reports_whether_the_page_refills(self) -> None:
        beacon = dashboard_frame._BEACON_JS
        assert f"refill: window[{dashboard_frame._REFILL_FLAG!r}] === true" in beacon

    def test_the_guard_ignores_a_resource_error(self) -> None:
        """A missing image must not blank the page.

        A resource error reaches window with ``target`` set to the element rather
        than to window, and withholding the whole page over a broken image would be
        a worse answer than drawing it.
        """
        guard = dashboard_frame._INIT_GUARD_JS
        assert (
            "event.target !== window" in guard
        ), "the guard counts resource errors, so a missing image suppresses the page"
        assert (
            "unhandledrejection" in guard
        ), "a template that initializes in a promise fails there, not at top level"

    def test_a_value_cannot_end_the_island_and_open_a_tag(self) -> None:
        """Inside a document whose body is already template-authored, which is
        exactly where a second injection point must not exist."""
        doc = self.doc(fields={"risk_note": "</script><img src=x onerror=alert(1)>"})
        assert "</script><img" not in doc
        assert "\\u003c/script\\u003e" in doc

    def test_the_escaped_island_is_still_the_same_json(self) -> None:
        value = {"t": "<a>&</a>", "n": 1}
        blob = json.dumps(value)
        assert json.loads(dashboard_frame.escape_json_for_html(blob)) == value

    def test_values_are_bound_as_text_and_never_as_markup(self) -> None:
        """THE CONTAINMENT LINE of this surface. A fold value can hold any text the
        log recorded -- an issue title, a review comment, a command's output."""
        doc = self.doc()
        assert "textContent" in doc
        assert "innerHTML" not in doc

    def test_the_binding_attribute_matches_parity(self) -> None:
        """The dev-time gate that checks a template's bindings and the frame that
        fills them must not disagree about what a binding is."""
        from kiro_crew.dashboard_templates import parity

        assert dashboard_frame.BINDING_ATTRIBUTE == parity._BINDING

    def test_the_global_is_frozen_so_a_page_cannot_rewrite_its_numbers(self) -> None:
        doc = self.doc()
        assert "Object.freeze" in doc
        assert "writable: false" in doc

    def test_the_global_stays_redefinable_so_a_refill_can_replace_it(self) -> None:
        """Measured on a pod, not reasoned about: a non-configurable definition made
        the refill's own redefine throw, and made a second document in one window
        render with no bindings filled at all. The deep freeze is what protects the
        values; the descriptor only decides whether a refill is possible."""
        doc = self.doc()
        assert "configurable: false" not in doc
        defines = doc.count("Object.defineProperty(window")
        assert defines == 1, (
            "the global is defined from more than one site, so each one is its own "
            "chance to drop the descriptor the refill needs"
        )
        assert doc.count("configurable: true") == defines
        # The refill reaches that ONE definition rather than carrying its own, which is
        # what keeps the count above worth asserting.
        assert doc.count("publish(read);") == 2, "first load and the refill must both publish"

    def test_a_title_cannot_carry_markup_into_the_head(self) -> None:
        read = dashboard_frame.read_payload({})
        doc = dashboard_frame.compose("<p>x</p>", read, title="</title><script>evil()")
        assert "<script>evil()" not in doc
        assert "&lt;/title&gt;&lt;script&gt;evil()" in doc

    def test_the_stale_band_is_host_markup_above_the_page(self) -> None:
        """A template cannot suppress it: it is not in the fragment, and the page has
        no way to remove an element it did not create before the reader sees it."""
        doc = self.doc(stale=True, missing=["entries"])
        assert doc.index("kirocrew-stale-band") < doc.index("kirocrew-dashboard-data")

    def test_the_agentic_cells_are_marked(self) -> None:
        """A number the crewmate asserted and a number the log recorded are different
        kinds of claim, and a reader deciding whether to act is entitled to know
        which."""
        assert "data-dashboard-agentic" in self.doc(agentic=["risk_note"])

    def test_a_read_that_will_not_serialize_composes_an_empty_stale_one(self) -> None:
        """A value this module could not encode must not blank somebody's
        dashboard."""
        doc = dashboard_frame.compose("<p>x</p>", {"fields": {"a": object()}})
        island = doc.split('id="kirocrew-dashboard-data">')[1].split("</script>")[0]
        assert json.loads(island)["stale"] is True

    def test_an_unresolved_field_keeps_its_last_value_rather_than_blanking(self) -> None:
        """Blanking would turn one unresolved path into a page of em dashes and make a
        stale dashboard look like an empty one."""
        assert "data-dashboard-missing" in self.doc(stale=True, missing=["entries"])

    def test_the_read_payload_is_built_one_way_for_the_document_and_the_refill(self) -> None:
        """Two builders could describe the same read differently, which a reader
        cannot detect: a page filled once and refilled later looks the same."""
        read = dashboard_frame.read_payload({"a": 1}, agentic=["a"], seq=4, missing=["b"])
        assert read == {
            "fields": {"a": 1},
            "agentic": ["a"],
            "seq": 4,
            "stale": False,
            "missing": ["b"],
            # Empty rather than absent: a page reads this map unconditionally, and a
            # missing key would make a template branch on whether the host is new.
            "written_at": {},
            # English when the caller names no language, for the same reason.
            "locale": "en",
            # Whose page it is; a crewmate's unless the host says otherwise.
            "subject": "crewmate",
            # Empty rather than absent, like `written_at`: no stamp to give.
            "last_user_at": "",
        }

    def test_the_read_says_when_the_person_last_spoke_and_nothing_else_there(self) -> None:
        """The stamp telling a page the person wrote in the chat after a card was written.

        Passed through as the host's own string; anything that is not a string reads as
        no stamp, so a page never meets a number or a null where it expects text."""
        stamp = "2026-10-13T23:49:04+00:00"
        assert dashboard_frame.read_payload({}, last_user_at=stamp)["last_user_at"] == stamp
        for odd in (None, 0, 1760399344.0, ["2026-10-13T23:49:04+00:00"]):
            assert dashboard_frame.read_payload({}, last_user_at=odd)["last_user_at"] == ""
        doc = self.doc(last_user_at=stamp)
        assert json.loads(self.island(doc))["last_user_at"] == stamp

    def test_the_document_language_comes_from_the_read(self) -> None:
        """A page in the reader's language needs a document that says which one."""
        doc = self.doc(locale="zh-CN")
        assert '<html lang="zh-CN">' in doc
        assert json.loads(self.island(doc))["locale"] == "zh-CN"

    @pytest.mark.parametrize(
        "raw",
        ["xx-YY", "zh-TW", "zh-cn", "", None, 7, 'en"><script>alert(1)</script>', "en-XA"],
    )
    def test_a_language_the_app_does_not_ship_falls_back_to_english(self, raw: Any) -> None:
        """The same gate as the persisted UI language: only a shipped catalog's tag."""
        doc = self.doc(locale=raw)
        assert '<html lang="en">' in doc
        assert json.loads(self.island(doc))["locale"] == "en"

    def test_a_hand_built_read_cannot_put_an_unchecked_tag_in_markup(self) -> None:
        read = dashboard_frame.read_payload({})
        read["locale"] = '"><b>x</b>'
        doc = dashboard_frame.compose(self.page(), read)
        assert '<html lang="en">' in doc

    def test_the_bootstrap_sets_the_language_for_a_host_that_writes_its_own_html(self) -> None:
        """``compose_body`` drops ``<html>``, so the bootstrap carries the language."""
        body = dashboard_frame.compose_body(
            self.page(), dashboard_frame.read_payload({}, locale="zh-CN")
        )
        assert "<html" not in body
        assert "document.documentElement.lang = read.locale" in body

    def test_the_refill_replaces_the_global_wholesale(self) -> None:
        """A page holding a reference to the old object keeps reading consistent
        numbers from it, and nothing half-updates."""
        doc = self.doc()
        assert dashboard_frame.DATA_MESSAGE_TYPE in doc
        assert dashboard_frame.PAGE_EVENT in doc

    def test_a_refill_from_anyone_but_the_minting_window_is_dropped(self) -> None:
        """The one sender entitled to refill this page is the window that minted it.

        This frame runs with ``allow-scripts`` and so does every other frame the
        dashboard embeds, so a sibling running script of its own reaches
        ``parent.frames[i]`` and can post here. Unchecked, it would replace every
        recorded cell with values it chose and the page would present them exactly as
        it presents the host's -- which is the single property the fold-sourced fields
        exist to guarantee.
        """
        doc = self.doc()
        assert "event.source !== parent" in doc

    def test_the_sender_is_checked_before_the_message_type(self) -> None:
        """Order, not just presence.

        A sender test placed after the type and shape checks still drops the message,
        but it reads as one more validation of a message the page has already begun to
        trust. Asserting the order is what makes the check's PURPOSE -- this sender is
        not allowed to be here at all -- survive a later edit to the branches below it.
        """
        doc = self.doc()
        sender = doc.index("event.source !== parent")
        typed = doc.index(f"data.type !== {dashboard_frame.DATA_MESSAGE_TYPE!r}".replace("'", '"'))
        assert sender < typed, "the sender check must come first"

    def test_the_island_escapes_match_the_panel_store(self) -> None:
        assert dashboard_frame._JSON_HTML_ESCAPES == agent_panel._JSON_HTML_ESCAPES


def test_compose_body_carries_the_rules_its_own_markup_needs():
    """The host builds the HEAD, so a body-only compose must bring its stylesheet.

    These rules are not decoration. They are what DIMS a missing cell, underlines an
    agentic one, and makes the stale band a band rather than a line of plain text -- so
    a body composed without them renders a page whose own marks say nothing. The slice
    takes what sits between `<body>` and `</body>`, which is why the head's stylesheet
    needed carrying explicitly.

    Checked by selector rather than by substring of the whole sheet, so the pin names
    the three marks that have to keep working instead of freezing the file's text.
    """
    body = dashboard_frame.compose_body(
        "<p data-dashboard-field='entries'>x</p>",
        dashboard_frame.read_payload({"entries": 3}, agentic=[], seq=1, stale=True, missing=["a"]),
    )
    assert "<style>" in body, "the body-only compose carries no stylesheet at all"
    for selector in (
        "[data-dashboard-missing='true']",
        "[data-dashboard-agentic='true']",
        "#kirocrew-stale-band",
    ):
        assert selector in body, (
            f"{selector} has no rule in the composed body, so that mark renders "
            "unstyled on a page the host wrapped"
        )
    # The stylesheet leads, so the rules are parsed before the markup they apply to.
    assert body.index("<style>") < body.index(
        'kirocrew-stale-band"'
    ), "the stylesheet follows the band it styles"


class TestTheCodeTextOfAPage:
    """Which stretches of a page are ``<script>`` and ``<style>`` text (``code_spans``).

    A span licenses a weaker rule over that text, so the question each case asks is what a
    browser would read as code: anything it would read as markup or prose must get no span.
    """

    @staticmethod
    def texts(html: str) -> list[str]:
        return [html[start:end] for start, end in dashboard_frame.code_spans(html)]

    def test_a_page_with_neither_element_has_no_span(self) -> None:
        assert self.texts('<p data-dashboard-field="goal"></p><b onclick="f()">x</b>') == []

    def test_the_span_is_the_text_and_the_tags_stay_markup(self) -> None:
        html = '<p>a</p><script type="module">let x = 1;</script><b>b</b><style>.a{c:red}</style>'
        assert self.texts(html) == ["let x = 1;", ".a{c:red}"]

    def test_the_names_fold_case_and_an_end_tag_may_carry_whitespace(self) -> None:
        assert self.texts("<SCRIPT>a</Script ><Style>b</STYLE\n>") == ["a", "b"]

    def test_a_longer_name_is_not_one_of_them(self) -> None:
        assert self.texts("<scripted>x</scripted><styles>y</styles>") == []

    def test_an_empty_element_has_no_span(self) -> None:
        assert self.texts("<script></script><style></style>") == []

    def test_an_end_tag_must_end_its_name(self) -> None:
        assert self.texts("<script>a</scripts>b</script>") == ["a</scripts>b"]

    def test_the_first_end_tag_ends_the_element_as_the_parser_reads_it(self) -> None:
        assert self.texts("<script>var s = '</script>';</script>") == ["var s = '"]

    def test_a_style_inside_a_script_is_script_text(self) -> None:
        assert self.texts("<script>var a = '<style>x</style>';</script>") == [
            "var a = '<style>x</style>';"
        ]

    def test_a_limit_stops_the_scan_after_that_many(self) -> None:
        html = "<script>a</script><style>b</style><script>c</script>"
        assert [html[a:b] for a, b in dashboard_frame.code_spans(html, 2)] == ["a", "b"]
        assert dashboard_frame.code_spans(html, 0) == []
        assert len(dashboard_frame.code_spans(html, 99)) == 3

    # -- what is NOT an element, however much it looks like one ---------------------

    def test_a_tag_inside_a_comment_opens_nothing(self) -> None:
        assert self.texts("<!-- old <script> tag removed --><p>x</p>") == []
        assert self.texts("<!-- <script> --><script>a</script>") == ["a"]

    def test_the_comments_that_end_early_or_late(self) -> None:
        assert self.texts("<!--><script>a</script>") == ["a"]
        assert self.texts("<!---><script>a</script>") == ["a"]
        assert self.texts("<!----><script>a</script>") == ["a"]
        assert self.texts("<!-- x --!><script>a</script>") == ["a"]
        assert self.texts("<!-- <script>a</script>") == [], "an unended comment holds the rest"

    def test_a_tag_inside_an_attribute_value_opens_nothing(self) -> None:
        assert self.texts('<p title="<style>">x</p>') == []
        assert self.texts("<p title='<script>'>x</p><script>a</script>") == ["a"]
        assert self.texts('<a href="x" data-t="<script>">t</a><style>b</style>') == ["b"]

    def test_a_greater_than_inside_a_quoted_value_does_not_end_the_start_tag(self) -> None:
        assert self.texts("<script data-note=\">\" data-k='>'>var x</script><p>t</p>") == ["var x"]

    @pytest.mark.parametrize("name", ["textarea", "title", "xmp", "iframe", "noembed", "noframes"])
    def test_the_content_of_a_raw_text_element_is_text(self, name: str) -> None:
        assert self.texts(f"<{name}><script>x</script></{name}><p>y</p>") == []
        assert self.texts(f"<{name}><style></{name}><style>a</style>") == ["a"]

    def test_noscript_ends_the_scan(self) -> None:
        """Its content is text with scripting on and markup with it off, and a frame runs either.

        With scripting off, ``<noscript><textarea></noscript><script>K</script></textarea>``
        shows the script's text inside the textarea.
        """
        assert self.texts(
            "<script>a</script><noscript><script>x</script></noscript><script>b</script>"
        ) == ["a"]
        assert (
            self.texts("<noscript><textarea></noscript><script>K</script></textarea></noscript>")
            == []
        )
        assert self.texts("<NoScript ><script>a</script>") == []

    def test_template_ends_the_scan(self) -> None:
        """A template has insertion modes of its own; a column group in one ignores a style."""
        assert self.texts("<template><col><style></template><p>K</p></style>") == []
        assert self.texts(
            "<style>a</style><template><style>b</style></template><style>c</style>"
        ) == ["a"]
        assert self.texts("<TEMPLATE ><script>a</script>") == []

    def test_noscript_named_inside_text_opens_nothing_and_ends_nothing(self) -> None:
        assert self.texts('<p title="<noscript>">x</p><script>a</script>') == ["a"]
        assert self.texts("<!-- <noscript> --><script>a</script>") == ["a"]
        assert self.texts("<textarea><noscript></textarea><script>a</script>") == ["a"]

    def test_a_doctype_a_cdata_section_and_a_processing_instruction_end_at_the_first_gt(
        self,
    ) -> None:
        assert self.texts("<![CDATA[<script>x</script>]]><script>a</script>") == ["a"]
        assert self.texts("<?xml <script> ?><script>a</script>") == ["a"]
        assert self.texts("</ <script>><script>a</script>") == ["a"]

    def test_a_lone_less_than_is_text(self) -> None:
        assert self.texts("1 < 2 and 3 <4 <script>a</script>") == ["a"]

    # -- names are read as the tokenizer reads them, not as Python folds them ---------

    @pytest.mark.parametrize(
        "name", ["\u017fcript", "scr\u0131pt", "script\u00a0", "\u212aeyframes", "scr\u0130pt"]
    )
    def test_a_look_alike_name_is_not_a_script(self, name: str) -> None:
        assert self.texts(f"<{name}>a</{name}>") == []

    def test_only_ascii_white_space_ends_a_name_and_an_end_tag(self) -> None:
        assert self.texts("<script>a</script\u00a0>b</script>") == ["a</script\u00a0>b"]
        assert self.texts("<script\t\n\f\r >a</script\t>") == ["a"]

    # -- script data has escape states: "<!--" and "<script" move where the element ends --

    def test_a_nested_script_end_tag_does_not_end_the_element(self) -> None:
        """The first ``</script>`` closes the nested ``<script>``; resuming markup there is unsafe."""
        page = '<script><!--<script></script><style></script><p title="K">K</p></style>'
        assert self.texts(page) == ["<!--<script></script><style>"]

    @pytest.mark.parametrize(
        ("page", "expected"),
        [
            ("<script><!-- x </script><p>K</p>", ["<!-- x "]),
            ("<script><!--<script></script>x</script>y", ["<!--<script></script>x"]),
            ("<script><!--<script>--></script>y", ["<!--<script>-->"]),
            ("<script><!--> </script>", ["<!--> "]),
            ("<script><!---> </script>", ["<!---> "]),
            ("<script><!--<SCRIPT ></SCRIPT\n>x</script>y", ["<!--<SCRIPT ></SCRIPT\n>x"]),
            ("<script><!--<scripts></script>", ["<!--<scripts>"]),
            ("<script>a<script>b</script>", ["a<script>b"]),
            ("<script><!--\nvar a;\n//--></script><p>K</p><script>b</script>", ["<!--\nvar a;\n//-->", "b"]),
            ("<script><!--<script>", []),
        ],
    )  # fmt: skip
    def test_the_escape_states_place_the_end_where_the_tokenizer_does(
        self, page: str, expected: list[str]
    ) -> None:
        assert self.texts(page) == expected

    # -- elements the tree builder decides about: svg, select and math -------------------

    def test_a_simple_svg_or_select_is_read_whole_and_the_page_goes_on(self) -> None:
        svg = '<svg viewBox="0 0 1 1"><path d="M0 0"/><circle r=1></svg>'
        assert self.texts(svg + "<script>a</script>") == ["a"]
        select = (
            "<select><option value=a>A</option><optgroup><option>B</option></optgroup></select>"
        )
        assert self.texts(select + "<style>b</style>") == ["b"]
        assert self.texts("<SVG><G></G></SVG><SELECT></SELECT><script>a</script>") == ["a"]

    @pytest.mark.parametrize(
        "page",
        [
            "<svg><script>x</script></svg><script>a</script>",
            "<svg><style>x</style></svg><script>a</script>",
            "<svg><title>t</title></svg><script>a</script>",
            "<svg><desc>d</desc></svg><script>a</script>",
            "<svg><foreignObject></foreignObject></svg><script>a</script>",
            "<svg><template></template></svg><script>a</script>",
            "<svg><svg></svg></svg><script>a</script>",
            "<svg><!-- c --></svg><script>a</script>",
            "<svg><![CDATA[x]]></svg><script>a</script>",
            "<svg><script/></svg><script>a</script>",
            "<svg/><script>a</script>",
            "<svg><circle/><script>a</script>",
            "<svg><iframe></svg><script>a</script>",
            "<select><script>x</script></select><script>a</script>",
            "<select><style></select><p>K</p></style>",
            "<select><svg></svg></select><script>a</script>",
            "<math><mi>x</mi></math><script>a</script>",
            "<math></svg><style><mtext>K</mtext></style></math>",
            '<svg><foreignObject><div></svg></div></foreignObject><script><a title="K">x</a></script>',
        ],
    )
    def test_inside_any_other_svg_select_or_math_the_scan_stops(self, page: str) -> None:
        """Only the tree builder knows how these read, so nothing past one is a span."""
        assert self.texts(page) == []

    def test_a_stray_end_tag_of_one_of_them_is_ignored_and_a_look_alike_name_is_ordinary(
        self,
    ) -> None:
        assert self.texts("</svg></select></math></template><script>a</script>") == ["a"]
        assert self.texts("<svgfoo><script>a</script></svgfoo>") == ["a"]
        assert self.texts("<templates><script>a</script></templates>") == ["a"]

    # -- a bare attribute value runs on through quotes, "=" and "<" ---------------------

    def test_a_bare_value_that_holds_quotes_is_read_to_white_space_or_gt(self) -> None:
        """The tag ends at the first ``>``, so what follows it is markup, not an attribute."""
        assert self.texts('<p a=x"b="c>d" e<textarea x><script>K</script></textarea>') == []
        assert self.texts("<p a=x<!-- y><script>K</script>") == ["K"]
        assert self.texts('<p a=x<b="c>KEY<script>y</script>') == ["y"]

    # -- what the scan will not guess at ----------------------------------------------

    def test_an_element_that_is_never_closed_has_no_span(self) -> None:
        assert self.texts("<p>x</p><script>var a = 1;") == []
        assert self.texts("<style>a{}<p>y</p>") == []

    def test_a_start_tag_that_never_ends_has_no_span(self) -> None:
        assert self.texts("<p>x</p><script src=x") == []
        assert self.texts('<p>x</p><script src="x>y</script>') == []

    def test_a_self_closing_script_outside_svg_ends_the_scan(self) -> None:
        """HTML ignores the slash and opens a script; a value ending in ``/`` looks the same."""
        assert self.texts("<p><script/></p><script>a</script>") == []
        assert self.texts("<script src=a/><script>a</script>") == []

    def test_plaintext_ends_the_scan(self) -> None:
        assert self.texts("<plaintext><script>a</script>") == []

    def test_an_attribute_name_the_scan_cannot_read_ends_the_scan(self) -> None:
        assert self.texts("<a =x><script>a</script>") == []

    def test_a_page_that_needs_more_steps_than_the_bound_gets_no_more_spans(self) -> None:
        reads = dashboard_frame._MAX_SCAN_STEPS
        assert self.texts("<!---->" * (reads // 2) + "<script>a</script>") == ["a"]
        assert self.texts("<!---->" * (reads + 5) + "<script>a</script>") == []

    # -- cost and agreement -------------------------------------------------------------

    def test_unclosed_start_tags_are_read_once_each(self) -> None:
        """Quadratic here would stop the gateway: the scrub runs on every served page."""
        assert dashboard_frame.code_spans("<script " * 100_000) == []
        assert dashboard_frame.code_spans("<script>" * 50_000) == []
        assert dashboard_frame.code_spans("<p title='" * 100_000 + "<script>a</script>") == []
        assert len(dashboard_frame.code_spans("<script></script>" * 5_000)) == 0
        assert len(dashboard_frame.code_spans("<style>x</style>" * 3_000)) == 3_000
        tail = "<script>a</script>"
        assert dashboard_frame.code_spans('<p x="' * 100_000 + tail) == []
        assert dashboard_frame.code_spans("<select>" + "<option>x" * 100_000 + "<script>") == []
        assert self.texts("<svg>" + "<p>" * 100_000 + "</svg>" + tail) == ["a"]
        assert self.texts("<a " + "b=c " * 100_000 + tail) == [], "the script is inside the tag"
        reads = dashboard_frame._MAX_SCAN_STEPS
        assert dashboard_frame.code_spans("<svg></svg>" * (reads + 10) + tail) == []
        assert self.texts("<svg></svg>" * (reads // 2) + tail) == ["a"]

    def test_a_page_of_ordinary_tags_before_a_script_is_read_in_bulk(self) -> None:
        html = "<a href=\"x\" class=y data-q='1' hidden>t</a><br/>" * 100_000 + "<script>a</script>"
        assert self.texts(html) == ["a"]

    PAGES = (
        "<p>a</p><script>b</script><i>c</i><style>d</style>",
        "<a href=\"x\" class=y data-q='1' hidden>t</a><br/><script>b</script>",
        "<!-- c --><p title='<script>'>t</p><textarea><style></textarea><script>x</script>",
        "<p a=b\"c d='e>f'>g</p><script>h</script><div =x><script>i</script>",
        "<script src=a/><script>x</script>",
        "<?x?><!doctype html><svg><style>s</style><script/></svg><script>y</script>",
        "</p x='<script>'><SCRIPT\n>z</SCRIPT\n>",
        "<svg><script><p>K</p></script></svg><script>a</script>",
        "<svg><!-- </svg> --><script>x</script></svg><script>a</script>",
        "<svg><![CDATA[ </svg> ]]><script>x</script></svg><math><style>m</style></math><style>b</style>",
        '<p a=x"b="c>d" e<textarea x><script>K</script></textarea>',
        '<script><!--<script></script><style></script><p title="K">K</p></style>',
        "<script><!--\nvar a;\n//--></script><p>K</p><script>b</script>",
        "<p a=x<!-- y><script>K</script>",
        '<svg viewBox="0 0 1 1"><path d="M0 0"/></svg><select><option>a</option></select><script>s</script>',
        "<select><style></select><p>K</p></style><script>t</script>",
    )

    @pytest.mark.parametrize("html", PAGES)
    def test_the_bulk_run_finds_what_the_construct_by_construct_read_finds(
        self, html: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_PLAIN_RUN`` only makes the common page fast: with its tags off the answer is the same."""
        with_bulk = dashboard_frame.code_spans(html)
        monkeypatch.setattr(dashboard_frame, "_PLAIN_RUN", re.compile(r"[^<]++"))
        assert dashboard_frame.code_spans(html) == with_bulk

    @pytest.mark.parametrize("html", PAGES)
    def test_every_span_is_a_whole_nonempty_element_text(self, html: str) -> None:
        spans = dashboard_frame.code_spans(html)
        assert spans == sorted(spans)
        for start, end in spans:
            assert 0 < start < end <= len(html)
            assert html[start - 1] == ">", "a span starts where its start tag ends"
            assert re.match(r"</(?:script|style)[\t\n\f\r />]", html[end:], re.IGNORECASE)


class TestTheScanAgreesWithItselfOnRandomPages:
    """The bulk run is an accelerator, so on any page it must not change what the scan finds."""

    PIECES = (
        "<script>", "</script>", "<style>", "</style>", "<!--", "-->", "<p>", "</p>", "<br/>",
        "<textarea>", "</textarea>", "<title>", "</title>", "<svg>", "</svg>", "<select>",
        "</select>", "<option>", "<math>", "<![CDATA[", "<a ", "<p title=\"", "x='", "a=x\"b=\"c",
        "\"", "'", "=", ">", "<", "/", " ", "\n", "\u00a0", "K", "<script/>", "<noscript>",
        "</noscript>", "<iframe>", "</iframe>", "<!doctype html>", "<?x?>", "</", "<!",
        "<script>", "</script>", "<style>", "</style>", "<script>", "</script>", "K", "x",
        "<!--<script>", "--></script>", "<script/", "</SCRIPT\n", "-->", "--",
    )  # fmt: skip

    def pages(self, count: int = 3000):
        rng = random.Random(20815)
        for _ in range(count):
            yield "".join(rng.choice(self.PIECES) for _ in range(rng.randint(1, 18)))

    def test_with_the_bulk_run_off_the_answer_is_the_same(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pages = list(self.pages())
        with_bulk = [dashboard_frame.code_spans(page) for page in pages]
        # Text alone is still skipped in bulk: every tag is read construct by construct.
        monkeypatch.setattr(dashboard_frame, "_PLAIN_RUN", re.compile(r"[^<]++"))
        for page, expected in zip(pages, with_bulk):
            assert dashboard_frame.code_spans(page) == expected, page

    def test_every_span_is_the_whole_text_of_a_closed_element(self) -> None:
        closer = re.compile(r"</(?:script|style)[\t\n\f\r />]", re.IGNORECASE | re.ASCII)
        checked = 0
        for page in self.pages():
            spans = dashboard_frame.code_spans(page)
            assert spans == sorted(spans)
            last_end = 0
            for start, end in spans:
                assert last_end <= start < end <= len(page), page
                assert page[start - 1] == ">" and closer.match(page, end), page
                last_end = end
                checked += 1
        assert checked > 100, "the corpus no longer produces spans, so this proves nothing"


class TestMappingAPageByKind:
    """``map_page_text`` is the markup function's page, with code's result in script and style text.

    The markup function runs over the whole page first and its answer is the baseline. The one
    change allowed is to put the code function's result for an element's text where the markup
    function's was, and the result is checked before it is served.
    """

    PAGES = (
        "",
        "<p>x</p>",
        "<script>a</script>",
        "<script></script><i>c</i>",
        "<p>a</p><script>b</script><i>c</i><style>d</style>",
        "<p>a</p><style>b{}</style><script>unclosed",
    )

    def test_two_identity_functions_give_the_page_back(self) -> None:
        for html in self.PAGES:
            assert dashboard_frame.map_page_text(html, lambda s: s, lambda s: s) == html

    def test_script_and_style_text_gets_the_code_function_and_the_rest_the_markup_function(
        self,
    ) -> None:
        html = "<p>KC</p><script>KC</script><i>KC</i><style>KC</style>"
        out = dashboard_frame.map_page_text(
            html, lambda s: s.replace("K", "#"), lambda s: s.replace("C", "-")
        )
        assert out == "<p>#C</p><script>K-</script><i>#C</i><style>K-</style>"

    def test_the_code_function_sees_only_the_text_of_each_element(self) -> None:
        seen: list[str] = []

        def code(piece: str) -> str:
            seen.append(piece)
            return piece

        html = "<p>a</p><script>b</script><i>c</i><style>d</style>"
        dashboard_frame.map_page_text(html, lambda s: s.upper(), code)
        assert seen == ["b", "d"]

    def test_a_page_with_no_code_reaches_the_markup_function_whole(self) -> None:
        seen: list[str] = []

        def markup(piece: str) -> str:
            seen.append(piece)
            return piece

        def not_code(piece: str) -> str:
            raise AssertionError(f"{piece!r} is not code")

        html = "<p>x</p>"
        assert dashboard_frame.map_page_text(html, markup, not_code) == html
        assert seen == [html]

    def test_a_page_the_markup_function_leaves_alone_never_reaches_the_code_function(self) -> None:
        def not_code(piece: str) -> str:
            raise AssertionError(f"{piece!r} was handed over although nothing was scrubbed")

        html = "<p>x</p><script>a</script>"
        assert dashboard_frame.map_page_text(html, lambda s: s, not_code) == html

    def test_a_run_that_the_markup_function_reads_whole_is_not_cut_by_the_code_function(
        self,
    ) -> None:
        """A key id glued to a partial key is one run to the markup function.

        Letting the code function rewrite the prose first would leave the markup function a
        tail too short to match, and the tail would be served.
        """
        html = "<p>AAAAKKKK</p><style>a{}</style>"

        def markup(piece: str) -> str:
            return re.sub(r"[AK]{8,}", "#", piece)

        def code(piece: str) -> str:
            return piece.replace("AAAA", "[R]")

        out = dashboard_frame.map_page_text(html, markup, code)
        assert out == markup(html) == "<p>#</p><style>a{}</style>"

    def test_a_rule_that_reaches_across_a_start_tag_gives_the_page_to_the_markup_function(
        self,
    ) -> None:
        """The pieces scrubbed one by one must add up to the page scrubbed whole."""
        html = "<p>1</p>x<script>K</script><i>K</i>"

        def markup(piece: str) -> str:
            return piece.replace("x<script>K</script>", "#").replace("K", "k")

        out = dashboard_frame.map_page_text(html, markup, lambda s: s)
        assert out == markup(html) == "<p>1</p>#<i>k</i>"

    def test_text_that_would_end_early_once_the_code_function_is_done_is_not_kept_as_code(
        self,
    ) -> None:
        """Dropping a nested script opener moves where the element ends; what follows is prose."""
        html = "<script>A<!--<script>B</script>C</script><p>K</p>"

        def code(piece: str) -> str:
            return piece.replace("<!--<script>", "")

        def markup(piece: str) -> str:
            return piece.replace("K", "k").replace("C", "c")

        out = dashboard_frame.map_page_text(html, markup, code)
        assert out == markup(html) == "<script>A<!--<script>B</script>c</script><p>k</p>"

    def test_a_markup_function_that_moves_where_an_element_ends_gets_the_page_whole(self) -> None:
        html = "<script>K</script><p>K</p>"

        def markup(piece: str) -> str:
            return piece.replace("</script>", "").replace("K", "k")

        out = dashboard_frame.map_page_text(html, markup, lambda s: s)
        assert out == markup(html) == "<script>k<p>k</p>", "the script text was kept as code"

    def test_a_markup_function_that_leaves_the_elements_alone_keeps_the_code_text(self) -> None:
        html = "<p>K</p><script>K</script><p>K</p><style>K</style>"
        out = dashboard_frame.map_page_text(html, lambda s: s.replace("K", "k"), lambda s: s)
        assert out == "<p>k</p><script>K</script><p>k</p><style>K</style>"

    def test_the_number_of_calls_is_bounded_however_many_elements_the_page_holds(self) -> None:
        """Each stretch between elements is a call; thousands of tiny elements must not cost thousands."""
        extra = 40
        bound = dashboard_frame.MAX_CODE_PIECES
        html = "<script>a</script>" * (bound + extra)
        calls = {"markup": 0, "code": 0}

        def count(kind: str):
            def fn(piece: str) -> str:
                calls[kind] += 1
                return piece.replace("a", "b") if kind == "markup" else piece

            return fn

        dashboard_frame.map_page_text(html, count("markup"), count("code"))
        assert calls["code"] == bound
        # the whole page, then each stretch outside (one more than the elements) and each text
        assert calls["markup"] == 1 + (bound + 1) + bound

    def test_an_element_past_the_bound_goes_to_the_stricter_function(self) -> None:
        html = "<script>a</script>" * (dashboard_frame.MAX_CODE_PIECES + 1)
        out = dashboard_frame.map_page_text(
            html, lambda s: s.replace("a", "="), lambda s: s.replace("a", "-")
        )
        assert out.count("-") == dashboard_frame.MAX_CODE_PIECES
        assert out.endswith("<script>=</script>"), "the last element was kept as code"

    def test_neither_function_is_called_with_an_empty_piece(self) -> None:
        markup_seen: list[str] = []
        code_seen: list[str] = []

        def markup(piece: str) -> str:
            markup_seen.append(piece)
            return piece.replace("a", "x")

        def code(piece: str) -> str:
            code_seen.append(piece)
            return piece

        html = "<script>a</script><style>b</style><script>c"
        dashboard_frame.map_page_text(html, markup, code)
        assert code_seen == ["a", "b"]
        assert all(markup_seen), markup_seen
