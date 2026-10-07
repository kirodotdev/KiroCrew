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
from typing import Any

import pytest

from kiro_crew import agent_panel, dashboard_frame


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

    def test_the_cap_is_measured_in_bytes_not_characters(self) -> None:
        page = "<p>" + "\u00e9" * (dashboard_frame.MAX_PAGE_BYTES // 2)
        assert len(page) < dashboard_frame.MAX_PAGE_BYTES
        with pytest.raises(dashboard_frame.PageError) as caught:
            dashboard_frame.validate_page(page)
        assert caught.value.code == "page_too_large"

    def test_the_cap_is_dsh_visualizes_own_default(self) -> None:
        assert dashboard_frame.MAX_PAGE_BYTES == 1_000_000


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
        # Every `sandbox=` attribute in the file must be the constant, so a grant
        # cannot be widened by one frame spelling its own. Checked on the
        # ATTRIBUTES rather than on the prose: the constant's own comment names the
        # withheld grants in order to explain why they are withheld.
        attributes = [line.strip() for line in source.splitlines() if "sandbox=" in line]
        assert attributes, "no sandboxed frame found; did the component change shape?"
        for line in attributes:
            assert line == "sandbox={CREW_DASHBOARD_SANDBOX}", line


class TestTheComposedDocument:
    def page(self) -> str:
        return (
            '<h1 data-dashboard-field="risk_note"></h1>'
            '<span data-dashboard-field="entries"></span>'
            "<canvas id=c></canvas>"
            "<script>var s = window.kirocrew.fields.entries;</script>"
        )

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
        assert doc.count("configurable: true") == 2

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
        }

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
