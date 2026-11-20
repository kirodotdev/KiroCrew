"""``browser.report_changes``: click, type and select_option say what the page did.

The report is read from the walker's ``glance`` before and after the action, and
folded by ``browser_harness.change_report``. Pinned here:

* with the setting off (the default) no extra helper is called and no result key is
  added, so every existing caller sees the result it saw before;
* with it on, the result carries ``changes``: the new address (without its query),
  a changed title, dialogs that opened or closed, new alerts, the fields that
  failed validation (named, never quoted), and the controls that appeared or went away;
* a JavaScript dialog the action opened is reported without reading the page, which
  it would hold;
* a page that cannot be read leaves the action's own result, with a ``warning``;
* every piece of page text is one bounded, redacted line.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.browser_harness import change_report, ops
from kiro_crew.browser_harness.supervisor import HelperError
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.handlers import browser_ops
from kiro_crew.mcp_tools import browser as browser_tool
from kiro_crew.testing.synthetic_credentials import github_classic, synth
from kiro_crew.testing.wait import finished

#: A secret the redactor removes (an AWS key ID is an identifier it keeps visible),
#: built at run time so no source line carries a credential.
FAKE_KEY = github_classic()
_METHOD_RE = re.compile(r"\.(\w+)\((.*)\)$", re.DOTALL)


def census(**over: Any) -> dict[str, Any]:
    """One ``glance`` answer: a page with a button, a field and nothing else."""
    base: dict[str, Any] = {
        "url": "https://shop.example/cart?session=abc123",
        "title": "Cart",
        "controls": ["button|Pay", "textbox|Coupon"],
        "dialogs": [],
        "alerts": [],
        "invalid": [],
        "truncated": False,
    }
    base.update(over)
    return base


class Page:
    """A scripted page and supervisor: ``glances`` is answered in order, then repeats."""

    def __init__(self, tmp_path: Path, glances: list[Any]) -> None:
        self.calls: list[str] = []
        self.glances = list(glances)
        self.dialog: Any = None
        self.helpers: dict[str, Callable[..., Any]] = {}
        self.clock = 1000.0
        self.tmp = tmp_path

    async def call(self, helper: str, **kwargs: Any) -> Any:
        if helper in ("closed_secret_count", "frame_owner_count"):
            return 0
        if helper == "current_tab":
            return {"url": "https://shop.example/cart"}
        self.calls.append(helper)
        if helper in ("js", "js_isolated"):
            method = _METHOD_RE.search(kwargs["expression"][len(ops.WALKER_SOURCE) :])
            assert method
            name = method.group(1)
            self.calls[-1] = f"page:{name}"
            if name == "glance":
                nxt = self.glances.pop(0) if len(self.glances) > 1 else self.glances[0]
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
            return {
                "ok": True,
                "x": 10.0,
                "y": 20.0,
                "w": 5.0,
                "h": 5.0,
                "tag": "button",
                "focused": True,
                "selected": ["a"],
            }
        if helper == "page_info":
            return {"w": 800, "h": 600, "dialog": self.dialog}
        if helper in self.helpers:
            return self.helpers[helper](**kwargs)
        return None

    async def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def ctx(self, *, report: bool = True) -> ops.OpContext:
        extra: dict[str, Any] = {"report_changes": True} if report else {}
        return ops.OpContext(
            helper=self.call,
            sleep=self.sleep,
            now=lambda: self.clock,
            screenshot_dir=self.tmp / "shots",
            **extra,
        )


def run(op: str, args: dict[str, Any], page: Page, *, report: bool = True) -> dict[str, Any]:
    return asyncio.run(ops.run_op(op, args, page.ctx(report=report)))


def click(page: Page, **kw: Any) -> dict[str, Any]:
    return run("click", {"ref": "e1"}, page, **kw)


# --- off by default --------------------------------------------------------------


def test_off_the_result_and_the_helper_calls_are_what_they_were(tmp_path: Path) -> None:
    page = Page(tmp_path, [census(), census(title="Paid")])
    out = click(page, report=False)
    assert out == {"ok": True, "x": 10.0, "y": 20.0, "button": "left", "clicks": 1, "ref": "e1"}
    assert "page:glance" not in page.calls and "page_info" not in page.calls


def test_the_setting_defaults_off_on_the_context() -> None:
    assert ops.OpContext(helper=lambda *a, **k: None).report_changes is False


# --- what the report says ----------------------------------------------------------


def test_no_change_seen_is_said_so(tmp_path: Path) -> None:
    out = click(Page(tmp_path, [census()]))
    assert out["changes"] == {"no_change_seen": True}


def test_a_new_page_is_named_without_its_query_and_its_controls_are_left_to_a_snapshot(
    tmp_path: Path,
) -> None:
    after = census(
        url="https://user:pw@shop.example:8443/thanks?token=abc#top",
        title="Thanks",
        controls=["link|Home"],
    )
    out = click(Page(tmp_path, [census(), after]))
    assert out["changes"] == {
        "url": "https://shop.example:8443/thanks",
        "title": "Thanks",
        "controls": "a new page: take a snapshot for its refs",
    }
    assert "pw" not in json.dumps(out) and "abc" not in json.dumps(out)


def test_a_query_only_change_is_said_to_be_one(tmp_path: Path) -> None:
    out = click(Page(tmp_path, [census(), census(url="https://shop.example/cart?page=2")]))
    assert out["changes"] == {
        "url": "https://shop.example/cart (query, fragment or path parameters changed)"
    }


def test_dialog_alert_validation_and_controls_that_changed(tmp_path: Path) -> None:
    after = census(
        controls=["button|Pay", "button|Confirm", "textbox|Coupon", "link|Help"],
        dialogs=["Confirm payment"],
        alerts=["Card declined"],
        invalid=["Coupon"],
    )
    before = census(controls=["button|Pay", "textbox|Coupon", "button|Back"])
    out = click(Page(tmp_path, [before, after]))
    assert out["changes"] == {
        "dialog_opened": ["Confirm payment"],
        "alerts": ["Card declined"],
        "invalid": ["Coupon"],
        "appeared": ['button "Confirm"', 'link "Help"'],
        "disappeared": ['button "Back"'],
    }


def test_a_dialog_that_closed_is_reported(tmp_path: Path) -> None:
    out = click(Page(tmp_path, [census(dialogs=["Confirm"]), census()]))
    assert out["changes"] == {"dialog_closed": ["Confirm"]}


def test_an_alert_or_message_already_there_is_not_new(tmp_path: Path) -> None:
    same = census(alerts=["Saved"], invalid=["Name"])
    assert click(Page(tmp_path, [same, same]))["changes"] == {"no_change_seen": True}


def test_a_control_with_no_name_is_listed_by_its_role(tmp_path: Path) -> None:
    out = click(
        Page(tmp_path, [census(), census(controls=["button|Pay", "textbox|Coupon", "link|"])])
    )
    assert out["changes"] == {"appeared": ["link"]}


def test_a_page_with_more_controls_than_the_walker_reads_is_not_compared(tmp_path: Path) -> None:
    out = click(Page(tmp_path, [census(), census(controls=["button|New"], truncated=True)]))
    assert out["changes"] == {
        "controls": "not compared: the page has more controls than the report reads"
    }


@pytest.mark.parametrize(
    ("name", "what"),
    [
        ("dialogs", "open dialogs"),
        ("alerts", "alert and status messages"),
        ("invalid", "fields"),
    ],
)
@pytest.mark.parametrize("side", ["before", "after"])
def test_a_list_the_walker_cut_short_is_said_to_be_uncompared(
    tmp_path: Path, name: str, what: str, side: str
) -> None:
    # Ten messages read on both sides, and the new one is past the walker's cap.
    ten = [f"Notice {i}" for i in range(10)]
    reads = [census(**{name: ten}), census(**{name: ten})]
    reads[0 if side == "before" else 1]["capped"] = {name: True}
    out = click(Page(tmp_path, reads))
    assert out["changes"] == {name: f"not compared: the page has more {what} than the report reads"}


def test_a_cut_list_leaves_the_other_lists_compared(tmp_path: Path) -> None:
    ten = [f"Notice {i}" for i in range(10)]
    after = census(alerts=ten, dialogs=["Confirm"], capped={"alerts": True})
    out = click(Page(tmp_path, [census(alerts=ten), after]))
    assert out["changes"] == {
        "alerts": "not compared: the page has more alert and status messages than the report reads",
        "dialog_opened": ["Confirm"],
    }


@pytest.mark.parametrize("capped", ["alerts", ["alerts"], {"alerts": "true"}, {"other": True}])
def test_a_malformed_cut_marker_is_ignored(tmp_path: Path, capped: Any) -> None:
    out = click(Page(tmp_path, [census(), census(alerts=["Saved"], capped=capped)]))
    assert out["changes"] == {"alerts": ["Saved"]}


def test_an_ipv6_address_keeps_its_brackets(tmp_path: Path) -> None:
    before = census(url="http://[::1]:5173/cart")
    after = census(url="http://user:pw@[::1]:5173/thanks?t=1", controls=[])
    out = click(Page(tmp_path, [before, after]))
    assert out["changes"]["url"] == "http://[::1]:5173/thanks"
    upper = click(Page(tmp_path, [census(), census(url="https://[2001:DB8::1]/a", controls=[])]))
    assert upper["changes"]["url"] == "https://[2001:db8::1]/a"


def test_the_address_is_cleaned_by_the_one_url_scrubber_the_recordings_use(
    tmp_path: Path,
) -> None:
    """A ``;jsessionid=`` path parameter is a session token, like a query."""
    from kiro_crew.browser_harness import recording

    after = census(url="https://shop.example/cart;jsessionid=S3SS10N/thanks?x=1", controls=[])
    out = click(Page(tmp_path, [census(), after]))
    assert out["changes"]["url"] == "https://shop.example/cart/thanks"
    assert "S3SS10N" not in json.dumps(out)
    assert change_report.clean_url is recording.clean_url
    assert not hasattr(change_report, "shown_url")


def test_a_path_parameter_only_change_is_said_to_be_one(tmp_path: Path) -> None:
    before = census(url="https://shop.example/cart;jsessionid=A1")
    out = click(Page(tmp_path, [before, census(url="https://shop.example/cart;jsessionid=B2")]))
    assert out["changes"] == {
        "url": "https://shop.example/cart (query, fragment or path parameters changed)"
    }


def test_type_and_select_option_carry_the_report_and_other_ops_do_not(tmp_path: Path) -> None:
    changed = [census(), census(alerts=["Looks good"])]
    typed = run("type", {"ref": "e2", "text": "SAVE10"}, Page(tmp_path, changed))
    assert typed["changes"] == {"alerts": ["Looks good"]}
    picked = run("select_option", {"ref": "e3", "value": "a"}, Page(tmp_path, changed))
    assert picked["changes"] == {"alerts": ["Looks good"]}
    hovered = Page(tmp_path, changed)
    assert "changes" not in run("hover", {"ref": "e1"}, hovered)
    assert "page:glance" not in hovered.calls


def test_a_refused_action_is_left_alone(tmp_path: Path) -> None:
    out = run("click", {"ref": "nope"}, Page(tmp_path, [census()]))
    assert out["ok"] is False and "changes" not in out and "warning" not in out


class _Hooks:
    """A decision hook that answers by itself, or runs the action it is handed."""

    def __init__(self, *, runs: bool) -> None:
        self.runs = runs

    async def run(self, op: str, args: dict[str, Any], ctx: Any, handler: Any) -> dict[str, Any]:
        if self.runs:
            return await handler(ctx, args)  # type: ignore[no-any-return]
        return ops.needs_ref()


@pytest.mark.parametrize("runs", [True, False])
def test_the_page_is_read_only_when_the_action_runs(tmp_path: Path, runs: bool) -> None:
    page = Page(tmp_path, [census(), census(title="Paid")])
    ctx = replace(page.ctx(), decisions=_Hooks(runs=runs))
    out = asyncio.run(ops.run_op("click", {"ref": "e1"}, ctx))
    if runs:
        assert out["changes"] == {"title": "Paid"}
        assert page.calls.count("page:glance") == 2
    else:
        assert out == ops.needs_ref() and "page:glance" not in page.calls


def test_a_target_with_no_hook_to_pick_it_reads_nothing(tmp_path: Path) -> None:
    page = Page(tmp_path, [census()])
    out = run("click", {"target": "the pay button"}, page)
    assert out == ops.needs_ref() and "page:glance" not in page.calls


# --- a JavaScript dialog --------------------------------------------------------


def test_a_dialog_the_click_opened_is_reported_and_the_page_is_not_read_again(
    tmp_path: Path,
) -> None:
    page = Page(tmp_path, [census()])
    page.helpers["click"] = lambda **_kw: setattr(
        page, "dialog", {"type": "confirm", "message": "Delete?"}
    )
    out = click(page)
    assert out["ok"] is True
    assert out["changes"] == {
        "js_dialog": {"type": "confirm", "message": "Delete?"},
        "note": "the page is held by this dialog; call `dialog` to answer it",
    }
    assert page.calls.count("page:glance") == 1  # only the read before the click


def _open_on_click(page: Page, message: str) -> dict[str, Any]:
    page.helpers["click"] = lambda **_kw: setattr(
        page, "dialog", {"type": "alert", "message": message}
    )
    return click(page)


def test_a_dialog_message_is_redacted_before_any_cut(tmp_path: Path) -> None:
    """A credential the dialog view's 1,000-character cut would split is still removed."""
    lead = ops.DIALOG_MESSAGE_CAP - len(FAKE_KEY) // 2
    out = _open_on_click(Page(tmp_path, [census()]), " " * lead + FAKE_KEY)
    shown = json.dumps(out)
    for start in range(len(FAKE_KEY) - 5):
        assert FAKE_KEY[start : start + 6] not in shown


def test_a_dialog_message_too_long_to_read_whole_is_left_out(tmp_path: Path) -> None:
    message = "x" * change_report._INPUT_CAP + FAKE_KEY
    out = _open_on_click(Page(tmp_path, [census()]), message)
    assert out["changes"]["js_dialog"] == {"type": "alert", "message": ""}


def test_the_dialog_op_still_shows_the_capped_message(tmp_path: Path) -> None:
    page = Page(tmp_path, [census()])
    page.helpers["dialog"] = lambda **_kw: {"dialog": {"type": "alert", "message": "m" * 3000}}
    out = run("dialog", {}, page)
    assert out["dialog"]["message"] == "m" * ops.DIALOG_MESSAGE_CAP


def test_a_dialog_already_open_keeps_the_ops_own_outcome(tmp_path: Path) -> None:
    page = Page(tmp_path, [census()])
    page.dialog = {"type": "alert", "message": "Hi"}

    def stuck(**_kw: Any) -> None:
        raise HelperError("timed out")

    page.helpers["click"] = stuck
    out = click(page)
    assert out["code"] == "dialog_open"
    assert "page:glance" not in page.calls


# --- a page that cannot be read ---------------------------------------------------


def test_a_page_unreadable_after_the_action_keeps_the_action_and_says_so(tmp_path: Path) -> None:
    out = click(Page(tmp_path, [census(), HelperError("context gone")]))
    assert out["ok"] is True and out["ref"] == "e1"
    assert "changes" not in out
    assert out["warning"] == "`changes` left out: the page could not be read around the action"


def test_a_navigation_that_swallows_the_first_read_is_read_again(tmp_path: Path) -> None:
    after = census(url="https://shop.example/done", title="Done", controls=[])
    out = click(Page(tmp_path, [census(), HelperError("navigating"), after]))
    assert out["changes"]["url"] == "https://shop.example/done"


def test_a_page_unreadable_before_the_action_still_runs_the_action(tmp_path: Path) -> None:
    out = click(Page(tmp_path, [HelperError("busy")]))
    assert out["ok"] is True and "warning" in out and "changes" not in out


def test_a_malformed_answer_is_dropped_not_trusted(tmp_path: Path) -> None:
    out = click(Page(tmp_path, [census(), "not a census"]))
    assert out["ok"] is True and "changes" not in out and "warning" in out


class Stalling(Page):
    """A page whose ``page_info`` never answers on one side of the click."""

    def __init__(self, tmp_path: Path, *, after: bool) -> None:
        super().__init__(tmp_path, [census(), census(title="Paid")])
        self.stalled = not after
        self.helpers["click"] = self._clicked

    def _clicked(self, **_kw: Any) -> None:
        self.stalled = not self.stalled

    async def call(self, helper: str, **kwargs: Any) -> Any:
        if helper == "page_info" and self.stalled:
            await asyncio.Event().wait()  # never set: only the report's bound ends it
        return await super().call(helper, **kwargs)


@pytest.mark.parametrize("bound", ["CHANGE_BEFORE_S", "CHANGE_AFTER_S"])
def test_a_page_that_stalls_the_report_keeps_the_action_inside_its_bound(
    bound: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reads around the action are bounded as a whole, not only the walker call:
    the route's budget counts on it, so a clicked page that stalls never turns the
    click into a timeout the agent would answer by clicking again."""
    # The stalled read never answers, so the bound always expires: its value only
    # sets how long the test takes.
    monkeypatch.setattr(ops, bound, 0.05, raising=False)
    page = Stalling(tmp_path, after=bound == "CHANGE_AFTER_S")
    out = asyncio.run(finished(ops.run_op("click", {"ref": "e1"}, page.ctx()), "the click"))
    assert out["ok"] is True and out["ref"] == "e1" and "changes" not in out
    assert out["warning"] == "`changes` left out: the page could not be read around the action"


class Redirecting(Page):
    """A page the click sends to an unlisted loopback address while the report waits."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path, [census()])
        self.url = "https://shop.example/cart"
        self.clicked = False
        self.helpers["click"] = lambda **_kw: setattr(self, "clicked", True)

    async def sleep(self, seconds: float) -> None:
        await super().sleep(seconds)
        if self.clicked and seconds == ops.CHANGE_SETTLE_S and self.url != "about:blank":
            self.url = "http://127.0.0.1:5173/admin"

    async def call(self, helper: str, **kwargs: Any) -> Any:
        if helper == "current_tab":
            return {"url": self.url}
        if helper == "goto":
            self.calls.append("goto")
            self.url = kwargs["url"]
            return None
        return await super().call(helper, **kwargs)


@pytest.mark.parametrize("report", [True, False])
def test_a_redirect_refused_while_the_report_waits_is_the_answer(
    report: bool, tmp_path: Path
) -> None:
    """The dialog read finds the refused landing first; its refusal is the op's answer,
    as it is with the setting off, never a report read off the blanked tab."""
    page = Redirecting(tmp_path)
    if not report:
        # With the setting off nothing waits: the page moves as the click lands.
        page.helpers["click"] = lambda **_kw: setattr(page, "url", "http://127.0.0.1:5173/admin")
    ctx = replace(page.ctx(report=report), url_allowed=lambda url: "127.0.0.1" not in url)
    out = asyncio.run(ops.run_op("click", {"ref": "e1"}, ctx))
    assert out["ok"] is False and out["code"] == "redirect_refused", out
    assert "changes" not in out and page.url == "about:blank"


def test_the_ops_own_warning_is_kept_beside_the_reports() -> None:
    result: dict[str, Any] = {"ok": True, "warning": "`clicks` ignored"}
    ops._left_out(result)
    assert result["warning"] == (
        "`clicks` ignored; `changes` left out: the page could not be read around the action"
    )


def test_the_route_budget_counts_the_report_only_while_it_is_on() -> None:
    from kiro_crew.browser_harness import budgets

    on = ops.OpContext(helper=lambda *a, **k: None, report_changes=True)
    off = ops.OpContext(helper=lambda *a, **k: None)
    plain = budgets.OP_TIMEOUT_S + budgets.DECISION_HEADROOM_S
    assert browser_ops._op_budget("click", off) == plain
    assert browser_ops._op_budget("hover", on) == plain
    assert browser_ops._op_budget("navigate", on) == budgets.SLOW_OP_TIMEOUT_S + (
        budgets.DECISION_HEADROOM_S
    )
    for op in ops.REPORT_OPS:
        assert browser_ops._op_budget(op, on) == plain + budgets.CHANGE_REPORT_S
    assert budgets.CHANGE_REPORT_S == budgets.CHANGE_BEFORE_S + budgets.CHANGE_AFTER_S
    # The tool still waits longer than the gateway's worst case for such an op.
    worst = (
        budgets.ENSURE_TIMEOUT_S
        + budgets.LOCK_WAIT_S
        + budgets.CREDENTIAL_WORST_S
        + budgets.OP_TIMEOUT_S
        + browser_ops._op_budget("click", on)
    )
    assert browser_tool.FAST_COMMAND_TIMEOUT_SECS > worst


# --- page text is untrusted -------------------------------------------------------


def test_page_text_is_one_bounded_redacted_line(tmp_path: Path) -> None:
    hostile = "Oops\n\x1b[31mred\x1b[0m\x07 " + "x" * 500 + " " + FAKE_KEY
    after = census(alerts=[hostile], dialogs=["\x1b]0;owned\x07Title"], title="T\r\nitle")
    out = click(Page(tmp_path, [census(), after]))["changes"]
    (alert,) = out["alerts"]
    assert "\n" not in alert and "\x1b" not in alert and "\x07" not in alert
    assert len(alert) <= change_report.TEXT_CAP
    assert out["dialog_opened"] == ["Title"]
    assert out["title"] == "T itle"


def test_a_credential_in_page_text_is_redacted() -> None:
    text = change_report.clean(f"token {FAKE_KEY} issued", change_report.TEXT_CAP)
    assert FAKE_KEY not in text


def test_each_list_is_capped_and_the_rest_counted(tmp_path: Path) -> None:
    many = [f"button|B{i}" for i in range(20)]
    out = click(Page(tmp_path, [census(controls=[]), census(controls=many)]))["changes"]
    assert len(out["appeared"]) == change_report.LIST_CAP + 1
    assert out["appeared"][-1] == "... and 12 more"
    assert out["appeared"][0] == 'button "B0"'


def test_a_name_is_cut_at_its_cap() -> None:
    assert len(change_report.clean("n" * 500, change_report.NAME_CAP)) == change_report.NAME_CAP


@pytest.mark.parametrize("lead", [49, 55, 150, 158])
@pytest.mark.parametrize("cap", [change_report.NAME_CAP, change_report.TEXT_CAP])
def test_page_text_is_redacted_before_it_is_cut_for_display(lead: int, cap: int) -> None:
    """A display cut that lands inside a credential leaves no piece of it behind."""
    text = change_report.clean("x" * lead + " " + FAKE_KEY, cap)
    assert len(text) <= cap
    for start in range(len(FAKE_KEY) - 5):
        assert FAKE_KEY[start : start + 6] not in text


def test_a_value_longer_than_the_input_cap_is_left_out_not_cut() -> None:
    """Cutting the input could split a credential below the redactor's match length."""
    cap = change_report._INPUT_CAP
    assert change_report.clean("x" * (cap + 1), change_report.TEXT_CAP) == ""
    assert change_report.clean("x" * (cap - 10) + FAKE_KEY, change_report.TEXT_CAP) == ""
    assert change_report.clean("x" * cap, change_report.TEXT_CAP).startswith("xxx")


def test_an_address_too_long_to_read_is_said_to_be_new(tmp_path: Path) -> None:
    after = census(url="https://shop.example/" + "p" * 3000, controls=[])
    out = click(Page(tmp_path, [census(), after]))["changes"]
    assert out["url"] == "an address too long to show"
    assert out["controls"] == "a new page: take a snapshot for its refs"


@pytest.mark.parametrize("gap", [" ", "\n", " \t ", "\u00a0\u2003\n"])
@pytest.mark.parametrize("lead", ["", f"Saved {FAKE_KEY} and "])
def test_a_token_split_by_white_space_is_left_out(gap: str, lead: str) -> None:
    """Markup can put white space inside a token the page draws whole: the pieces
    match no rule, so the text is read again with its white space removed."""
    split = FAKE_KEY[:10] + gap + FAKE_KEY[10:]
    assert change_report.clean(lead + split, change_report.TEXT_CAP) == ""


def test_an_alert_left_out_is_said_to_be_there(tmp_path: Path) -> None:
    split = FAKE_KEY[:10] + "\n" + FAKE_KEY[10:]
    out = click(Page(tmp_path, [census(), census(alerts=[f"Your key {split}"])]))["changes"]
    assert out["alerts"] == ["a message that could not be shown"]


@pytest.mark.parametrize(
    "sentence",
    [
        "Your changes were saved.",
        "Saved 2 items to the cart",
        "The browser is sent to http://127.0.0.1:48102/callback?code=..., which is unreachable",
        "comment 6841699202: an app gets what it asked for",
        "Run npm_config_cache to set where the cache folder lives",
    ],
)
def test_ordinary_sentences_pass_the_white_space_check(sentence: str) -> None:
    """Only rules that start with a fixed prefix read the text run together: a link
    and a run of digits and a colon also match run-together prose."""
    assert change_report.clean(sentence, change_report.TEXT_CAP) == sentence


def test_a_whole_token_is_redacted_not_left_out() -> None:
    assert change_report.clean(f"token {FAKE_KEY} was made", change_report.TEXT_CAP) == (
        "token [REDACTED: credential] was made"
    )


def _row_token(row: Any) -> str:
    """A token in *row*'s published format, built at run time: never a literal."""
    from kiro_crew.security.redaction_structured.charsets import ALPHABETS

    if row.get("checksum"):
        return github_classic(row["prefixes"][0])
    body = ""
    for part in row["body"]:
        if "literal" in part:
            body += part["literal"][0]
        else:
            size = part.get("length") or (part.get("lengths") or [0])[0] or part["min"]
            body += synth(size, ALPHABETS[part["alphabet"]].replace("-", "").replace("_", ""))
    return row["prefixes"][0] + body


def _secret_token_rows() -> list[Any]:
    """The secret token formats with a fixed prefix: only those read run-together text.

    Telegram and Discord tokens start with any digit or letter, so a split one in page
    text is a known gap of the run-together read; whole, the redaction replaces it.
    """
    from kiro_crew.security.redaction_structured import ROWS, SECRET

    return [
        r
        for r in ROWS
        if r["detector"] == "token_format"
        and r["tier"] == SECRET
        and all(len(p) >= change_report._MIN_FIXED_PREFIX for p in r["prefixes"])
    ]


def test_a_split_google_api_key_is_left_out() -> None:
    """A Google API key's record names no prefixed rule: its format still counts."""
    key = "".join(["AI", "za"]) + synth(35)
    assert key not in change_report.clean(f"Key: {key}", change_report.TEXT_CAP)
    for text in (f"Key: {key[:12]} {key[12:]}", f"Your key {key[:12]}\n{key[12:]} is ready"):
        assert change_report.clean(text, change_report.TEXT_CAP) == "", text


@pytest.mark.parametrize("row", _secret_token_rows(), ids=lambda r: r["id"])
def test_every_secret_token_format_split_by_white_space_is_left_out(row: Any) -> None:
    """Every format the detectors know a secret token by, whatever its record's rule."""
    token = _row_token(row)
    assert token not in change_report.clean(f"Key: {token}", change_report.TEXT_CAP)
    cut = len(token) // 2
    for text in (
        f"Key: {token[:cut]} {token[cut:]}",
        f"Your key {token[:cut]} {token[cut:]} is ready",
    ):
        assert change_report.clean(text, change_report.TEXT_CAP) == "", text


def test_a_hostile_census_cannot_grow_the_report() -> None:
    raw = census(controls=[f"button|{i}" for i in range(5000)], alerts=["a"] * 5000)
    read = change_report.census(raw)
    assert read is not None and len(read["controls"]) <= 700 and len(read["alerts"]) <= 700


# --- the walker ----------------------------------------------------------------


def test_glance_is_a_walker_method_read_in_the_isolated_world() -> None:
    assert "glance: function" in ops.WALKER_SOURCE
    assert "glance" not in ops.PAGE_WORLD_METHODS


def _glance_source() -> str:
    source = ops.WALKER_PATH.read_text(encoding="utf-8")
    return source[source.index("glance: function") :]


def test_glance_leaves_a_secret_field_out_of_the_invalid_list_whole() -> None:
    """A wrapping label's text holds a prefilled textarea's value; a secret one is never named."""
    body = _glance_source()
    scan = body[body.index('querySelectorAll("input,select,textarea,[aria-invalid=\\"true\\"]")') :]
    assert (
        "if (!flagged || !fieldish(field, 0) || !shown(field) || secretish(field) ||\n"
        "            inField(field.parentElement)) continue;" in scan
    )
    assert scan.index("secretish(field)") < scan.index("nameOf(field")


def test_glance_reads_no_name_from_an_element_inside_a_field() -> None:
    """A chip in an editor is part of the editor's value: neither its text nor its
    attributes name anything, and a name built from elements is read whole or not at all."""
    body = _glance_source()
    controls = body[body.index("var role = roleFor(el);") :]
    assert controls.index("if (inField(el.parentElement)) continue;") < controls.index("nameOf(el")
    assert "if (!shown(found[d]) || inField(found[d].parentElement)) continue;" in body
    ids = body[body.index("function idsText(") :]
    assert "if (ids.length > GLANCE_ID_CAP) return null;" in ids
    assert "var part = inField(n) ? null : ownText(n, GLANCE_TEXT_CAP);" in ids
    assert "if (part === null) return null;" in ids


def test_glance_leaves_out_text_too_long_to_read_whole() -> None:
    """The walker never cuts page text: a cut could split a credential the gateway would redact."""
    body = _glance_source()
    assert ".slice(" not in body and ".substring(" not in body and ".substr(" not in body
    assert "return text.length <= cap ? text : null;" in body


def test_glance_reads_no_name_or_message_from_inside_a_field() -> None:
    """Every label, labelledby, describedby, heading and alert text is read without the fields it wraps.

    A field's text is its value, so the one text reader skips the whole subtree of
    anything the one field test names, and reads nothing from a node that sits inside
    a field. The generic readers (``textContent``, ``innerText``, the snapshot's
    ``accName``) are not used on a container.
    """
    body = _glance_source()
    tags = re.search(r"var NOT_TEXT = \{([^}]*)\}", body)
    assert tags is not None
    for tag in ("script", "style", "template", "noscript", "datalist"):
        assert f"{tag}: 1" in tags.group(1)
    assert "if (n.nodeType !== 1 || skipped(n) || n.hidden) continue;" in body
    assert 'if (style.display === "none") continue;' in body
    # Text the page does not draw adds nothing, and its drawn children are read.
    assert 'return style.visibility !== "hidden" && style.visibility !== "collapse";' in body
    assert "if (!ink) continue;" in body
    # Text nodes are joined as the page shows them: a space only where the page
    # holds one or a new line starts, so a credential split across inline
    # elements reaches redaction whole.
    assert 'text += (gap ? " " : "") + n.nodeValue;' in body
    assert 'clip(parts.join(" "), cap)' not in body
    assert "for (var up = n; up; up = up.parentElement) if (skipped(up)) return true;" in body
    assert 'if (inField(node)) return "";' in body
    for generic in (
        r"labels\[0\]\.textContent",
        r"\.textContent",
        r"\.innerText",
        r"\baccName\(",
        r"VALUE_SELECTOR",
        r"holdsValue",
    ):
        assert re.search(generic, body) is None, generic
    # An alert region is read only through the field-free reader.
    assert "var said = ownText(live[a], GLANCE_TEXT_CAP);" in body


def test_glance_has_one_field_test_for_the_controls_and_every_text_read() -> None:
    """A custom ``role="textbox"`` (a one-time code in an alert) is a field like a native one.

    The controls scan and the text reader ask the same ``fieldish``: native fields,
    editable regions, every input role as any token of ``role``, and a shadow host
    whose open root holds a field. Nothing depends on a field's name looking secret.
    """
    body = _glance_source()
    roles = re.search(r"var FIELD_ROLES = \[([^\]]*)\];", body)
    assert roles is not None
    assert {r.strip().strip('"') for r in roles.group(1).split(",")} == {
        "textbox",
        "searchbox",
        "combobox",
        "spinbutton",
        "slider",
    }
    assert 'if (tag === "textarea" || tag === "select" || n.isContentEditable) return true;' in body
    assert "if (fieldRole(n)) return true;" in body
    assert "var tokens = spaceWords(explicitRole(n));" in body
    assert 'n.shadowRoot.querySelectorAll("*")' in body
    assert "holds = fieldish(all[i], depth + 1);" in body
    # A shadow tree too deep or too large to look through counts as a field.
    assert "var holds = depth > 3;" in body and "if (shadowBudget < 0) holds = true;" in body
    assert "return !!NOT_TEXT[n.tagName.toLowerCase()] || fieldish(n, 0);" in body
    assert "var isField = fieldish(el, 0);" in body
    assert "EDITABLE_ROLES" not in body and "SECRET_NAME_RE" not in body


def test_glance_names_an_invalid_field_and_reads_no_validation_message() -> None:
    """A page's message can repeat what was typed in any form, so none is read.

    An invalid field is listed by the name the controls list gives it, and nothing
    else: no ``validationMessage``, no ``aria-describedby`` or ``aria-errormessage``
    text, and no echo filter that could miss a reworded copy of the value.
    """
    body = _glance_source()
    for read in ("validationMessage", "aria-describedby", "aria-errormessage", "reason("):
        assert read not in body, read
    assert 'out.invalid.push(clip(nameOf(field, true), GLANCE_NAME_CAP) || "field");' in body


# --- the setting and the tool text ------------------------------------------------


@pytest.mark.parametrize("flag", [True, False])
def test_the_gateway_reads_the_setting_for_each_op(
    flag: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = KiroCrewConfig()
    cfg.browser.report_changes = flag
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls: cfg))
    monkeypatch.setattr(browser_ops, "config_dir", lambda: tmp_path)
    assert browser_ops.build_context("s", MagicMock(), MagicMock()).report_changes is flag


def test_an_unreadable_setting_leaves_the_report_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_cls: Any) -> Any:
        raise OSError("config unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(broken))
    monkeypatch.setattr(browser_ops, "config_dir", lambda: tmp_path)
    assert browser_ops.build_context("s", MagicMock(), MagicMock()).report_changes is False


def test_the_tool_text_carries_the_report_and_redacts_it_again() -> None:
    result = {
        "ok": True,
        "ref": "e1",
        "changes": {"alerts": [f"key {FAKE_KEY}"], "url": "https://shop.example/done"},
    }
    text = browser_tool._result_text("click", result)
    assert "https://shop.example/done" in text and "alerts" in text
    assert FAKE_KEY not in text
