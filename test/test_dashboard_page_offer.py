"""A crewmate on a template page asks once, on a card, before it replaces that page.

The card writes nothing: its yes wakes the crewmate to write, save and apply its page;
a no or an expiry writes nothing; all three leave the asked-once record. A crewmate
with nothing on its tab gets no card (``test_dashboard_default_by_slug``). That a
locked layout refuses the apply the wake makes is ``dashboard_apply``'s own rule,
pinned by ``test_dashboard_save_tool.test_a_locked_layout_refuses_the_apply_and_keeps_the_page``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import change_card_catalog as catalog
from kiro_crew import change_card_dashboard_page as card
from kiro_crew.dashboard import boot_id, page_offer

SLUG = "scout"
MEMBER = "Scout"


@pytest.fixture
def home(tmp_path: Path, _floor_monkeypatch: Any) -> Path:
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    return tmp_path


def _preview(replaces: str = "Goal board") -> dict[str, Any]:
    params = catalog.validate_params(catalog.KIND_CREWMATE_DASHBOARD_PAGE, {"member": MEMBER})
    snapshot = {"exists": True, "slug": SLUG, "member_id": "m1"}
    context = {"label": MEMBER, "goal": "Keep the docs current", "replaces": replaces}
    return catalog.build_preview(catalog.KIND_CREWMATE_DASHBOARD_PAGE, params, snapshot, context)


def test_the_card_offers_a_page_in_place_of_the_template_and_writes_nothing() -> None:
    preview = _preview()
    assert preview["title"] == card.TITLE
    (row,) = preview["changes"]
    assert row["before"] == "Goal board (template)"
    assert "Keep the docs current" in row["after"]
    # The one step is the yes: it starts a wake, it does not write a page.
    (step,) = preview["apply"]
    assert (step["method"], step["path"]) == ("POST", f"/api/members/{SLUG}/dashboard/page-offer")
    assert step["body"] == {"member": MEMBER}
    assert preview["undo_unavailable_reason"] == catalog.UNDO_NO_IDENTITY_CHECK


def test_only_the_crewmate_itself_raises_the_card() -> None:
    assert catalog.KIND_CREWMATE_DASHBOARD_PAGE in catalog.SELF_ONLY_KINDS
    assert catalog.KIND_CREWMATE_DASHBOARD_PAGE not in {k["id"] for k in catalog.list_kinds()}
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params(catalog.KIND_CREWMATE_DASHBOARD_PAGE, {"member": MEMBER, "x": 1})


def test_the_summary_reads_as_the_answer() -> None:
    p = {"member": MEMBER}
    kind = catalog.KIND_CREWMATE_DASHBOARD_PAGE
    assert "building its Dashboard page" in catalog.result_summary(kind, p, "applied", card.TITLE)
    for status in ("cancelled", "expired"):
        assert "keeps its current" in catalog.result_summary(kind, p, status, card.TITLE)


@pytest.mark.parametrize(
    ("krs", "target", "code"),
    [
        (False, {"replaces": "Goal board"}, "not_ready"),
        (True, None, "not_on_a_template"),
        (True, {"replaces": ""}, "not_on_a_template"),
    ],
    ids=["no-milestones", "own-page", "nothing-on-tab"],
)
def test_the_preview_refuses_a_card_the_crewmate_may_not_raise(
    _floor_monkeypatch: Any, krs: bool, target: Any, code: str
) -> None:
    from kiro_crew import dashboard_agentic, member_goals
    from kiro_crew.dashboard import change_cards_readers as readers

    rec = {
        "goals": [{"id": "g00000001", "text": "Docs"}],
        "key_results": [{"id": "k1"}] if krs else [],
    }
    _floor_monkeypatch.setattr(member_goals, "read_goals", lambda _s, _m: rec)
    _floor_monkeypatch.setattr(dashboard_agentic, "page_offer_target", lambda _s: target)
    _floor_monkeypatch.setattr(readers, "_crewmate_label", lambda _m: MEMBER)
    with pytest.raises(catalog.CardCatalogError) as exc:
        readers._page_offer_context(MEMBER, {"slug": SLUG})
    assert exc.value.code == code


def test_a_re_preview_of_the_raised_card_is_not_refused_as_asked(
    _floor_monkeypatch: Any,
) -> None:
    """The raise wrote the asked-once record, so the card's own Refresh (after a
    ``changed_since_preview`` yes) re-reads only readiness and the target."""
    from kiro_crew import dashboard_agentic, member_goals
    from kiro_crew.dashboard import change_cards_readers as readers

    rec = {"goals": [{"id": "g00000001", "text": "Docs"}], "key_results": [{"id": "k1"}]}
    _floor_monkeypatch.setattr(page_offer, "was_asked", lambda _s: True)
    _floor_monkeypatch.setattr(member_goals, "read_goals", lambda _s, _m: rec)
    _floor_monkeypatch.setattr(
        dashboard_agentic, "page_offer_target", lambda _s: {"replaces": "Goal board"}
    )
    _floor_monkeypatch.setattr(readers, "_crewmate_label", lambda _m: MEMBER)
    got = readers._page_offer_context(MEMBER, {"slug": SLUG})
    assert got["replaces"] == "Goal board"


def test_a_crewmate_asked_before_cannot_raise_the_card_again(
    home: Path, _floor_monkeypatch: Any
) -> None:
    import asyncio

    from kiro_crew.dashboard.change_cards import CardError
    from kiro_crew.dashboard.handlers import member_self

    _claim_env(_floor_monkeypatch)
    page_offer.record_asked(SLUG, "Goal board")
    with pytest.raises(CardError) as err:
        asyncio.run(member_self._page_offer_claim(MEMBER)())
    assert err.value.code == "already_asked"
    # The refusal leaves the first card's record in place.
    assert page_offer.was_asked(SLUG) is True


def test_a_ready_template_crewmate_gets_the_card_context(_floor_monkeypatch: Any) -> None:
    from kiro_crew import dashboard_agentic, member_goals
    from kiro_crew.dashboard import change_cards_readers as readers

    rec = {"goals": [{"id": "g00000001", "text": "Docs"}], "key_results": [{"id": "k1"}]}
    _floor_monkeypatch.setattr(page_offer, "was_asked", lambda _s: False)
    _floor_monkeypatch.setattr(member_goals, "read_goals", lambda _s, _m: rec)
    _floor_monkeypatch.setattr(
        dashboard_agentic, "page_offer_target", lambda _s: {"replaces": "Goal board"}
    )
    _floor_monkeypatch.setattr(readers, "_crewmate_label", lambda _m: MEMBER)
    got = readers._page_offer_context(MEMBER, {"slug": SLUG})
    assert got == {"label": MEMBER, "goal": "Docs", "replaces": "Goal board"}


def _claim_env(_floor_monkeypatch: Any) -> None:
    from kiro_crew import dashboard_agentic
    from kiro_crew import members as members_mod

    _floor_monkeypatch.setattr(members_mod, "member_slug", lambda _m, _c=None: SLUG)
    _floor_monkeypatch.setattr(
        dashboard_agentic, "page_offer_target", lambda _s: {"replaces": "Goal board"}
    )


def test_raising_the_card_records_asked_once_before_it_is_shown(
    home: Path, _floor_monkeypatch: Any
) -> None:
    import asyncio
    import json

    from kiro_crew.dashboard.handlers import member_self

    _claim_env(_floor_monkeypatch)
    assert page_offer.was_asked(SLUG) is False
    asyncio.run(member_self._page_offer_claim(MEMBER)())
    # Only the field a reader uses: the yes wake names the template it replaces.
    assert page_offer.read_record(SLUG) == {"replaces": "Goal board"}
    # Gateway-only: the record lives in crew-panels, not under the crewmate's own tree.
    assert page_offer.record_path(SLUG).parent.name == "crew-panels"
    assert json.loads(page_offer.record_path(SLUG).read_text(encoding="utf-8")) == {
        "replaces": "Goal board"
    }


def test_a_record_that_cannot_be_written_refuses_the_card(
    home: Path, _floor_monkeypatch: Any
) -> None:
    import asyncio

    from kiro_crew.dashboard.change_cards import CardError
    from kiro_crew.dashboard.handlers import member_self

    _claim_env(_floor_monkeypatch)

    def refuse(_slug: str, _replaces: str) -> None:
        raise OSError("read-only")

    _floor_monkeypatch.setattr(page_offer, "record_asked", refuse)
    with pytest.raises(CardError) as err:
        asyncio.run(member_self._page_offer_claim(MEMBER)())
    assert err.value.code == "page_offer_write_failed"


def test_a_card_that_is_not_published_takes_the_record_back(
    home: Path, _floor_monkeypatch: Any
) -> None:
    import asyncio

    from kiro_crew.dashboard.handlers import member_self

    _claim_env(_floor_monkeypatch)

    async def go() -> None:
        undo = await member_self._page_offer_claim(MEMBER)()
        assert page_offer.was_asked(SLUG) is True
        await undo()

    asyncio.run(go())
    assert page_offer.was_asked(SLUG) is False


def test_an_unreadable_record_still_counts_as_asked(home: Path) -> None:
    path = page_offer.record_path(SLUG)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert page_offer.was_asked(SLUG) is True


def test_the_yes_wake_writes_saves_and_applies_with_one_message() -> None:
    text = page_offer.write_prompt("Goal board")
    assert "saved with dashboard_save and then put live with dashboard_apply" in text
    assert "in place of the Goal board template" in text
    assert "one short message" in text and "dashboard_rollback" in text
    assert "locked the layout" in text and "stop" in text
    # No second card: the wake writes, it does not offer again.
    assert "offer_dashboard_page" not in text
    # The same look and variables the build line names.
    assert "a page worth returning to" in text and "var(--text)" in text and "var(--bg)" in text


async def _client(monkeypatch: Any, *, armed: bool, page: Any = None) -> TestClient:
    from kiro_crew.dashboard.handlers import member_page_offer as route

    async def owner(_req: Any, _op: str, member: Any, *, write: bool) -> Any:
        return SLUG, member, None

    monkeypatch.setattr(route, "_owner_member", owner)

    @web.middleware
    async def arm(request: web.Request, handler: Any) -> Any:
        if armed:
            request[route.REQ_CARD_PAGE_OFFER_APPLY] = True
            request[route.REQ_CARD_PAGE_OFFER_PAGE] = page
        return await handler(request)

    app = web.Application(middlewares=[arm])
    app["state"] = object()
    app.router.add_post("/api/members/{slug}/dashboard/page-offer", route.api_member_page_offer)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_the_yes_starts_one_write_wake_and_writes_nothing(
    home: Path, monkeypatch: Any
) -> None:
    import threading

    woken: list[tuple[str, str]] = []
    monkeypatch.setattr(
        page_offer, "schedule_write", lambda _st, slug, m, **_k: woken.append((slug, m))
    )
    marked_on: list[threading.Thread] = []
    real_mark = page_offer.record_yes

    def mark(slug: str) -> None:
        marked_on.append(threading.current_thread())
        real_mark(slug)

    monkeypatch.setattr(page_offer, "record_yes", mark)
    page_offer.record_asked(SLUG, "Goal board")
    client = await _client(monkeypatch, armed=True)
    try:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/page-offer", json={"member": MEMBER}
        )
        assert resp.status == 200, await resp.text()
    finally:
        await client.close()
    assert woken == [(SLUG, MEMBER)]
    # The yes only marks the record pending, and off the event loop: it is an fsync.
    assert page_offer.read_record(SLUG) == {
        "replaces": "Goal board",
        "pending": boot_id.current_boot_id(),
    }
    assert len(marked_on) == 1 and marked_on[0] is not threading.current_thread()


@pytest.mark.asyncio
async def test_only_the_card_step_can_start_the_wake(home: Path, monkeypatch: Any) -> None:
    woken: list[Any] = []
    monkeypatch.setattr(page_offer, "schedule_write", lambda *a: woken.append(a))
    client = await _client(monkeypatch, armed=False)
    try:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/page-offer", json={"member": MEMBER}
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "card_only"
    finally:
        await client.close()
    assert woken == []
    assert page_offer.read_record(SLUG) is None


def test_the_write_wake_is_one_waiting_nudge(monkeypatch: Any, home: Path) -> None:
    from kiro_crew.dashboard import goal_setup

    calls: list[tuple] = []
    monkeypatch.setattr(goal_setup, "_schedule", lambda *a, **k: calls.append((a, k)))
    from kiro_crew import dashboard_agentic

    monkeypatch.setattr(dashboard_agentic, "page_offer_target", lambda _s: {"replaces": "x"})
    page_offer.record_asked(SLUG, "Goal board")
    page_offer.schedule_write("state", SLUG, MEMBER)
    (((state, key, slug, member, build, label), kwargs),) = calls
    assert (state, key, slug, member, label) == (
        "state",
        f"{SLUG}:dashboard-page",
        SLUG,
        MEMBER,
        "dashboard page",
    )
    assert "in place of the Goal board template" in build({}, True)
    # A wake that never starts takes the asked-once record back.
    kwargs["on_dropped"]("skipped: timed out")
    assert not page_offer.was_asked(SLUG)


@pytest.mark.asyncio
async def test_a_wake_that_never_starts_runs_its_dropped_hook(monkeypatch: Any) -> None:
    from kiro_crew.dashboard import goal_setup

    async def timed_out(*_a: Any) -> str:
        return "skipped: timed out"

    monkeypatch.setattr(goal_setup, "_wait_and_send", timed_out)
    dropped: list[str] = []

    class _State:
        pass

    state = _State()
    goal_setup._schedule(state, "k", SLUG, MEMBER, lambda *_a: "x", "test", dropped.append)
    for task in list(goal_setup._tasks(state).values()):
        await task
    assert dropped == ["skipped: timed out"]


def test_the_hook_arms_the_yes_on_apply_only() -> None:
    from kiro_crew.dashboard.handlers import change_cards
    from kiro_crew.dashboard.handlers.member_page_offer import REQ_CARD_PAGE_OFFER_APPLY

    rec = {"kind": catalog.KIND_CREWMATE_DASHBOARD_PAGE, "params": {"member": MEMBER}}
    applied: dict[str, Any] = {}
    change_cards._arm_card_undo_cron_guard(applied, rec, catalog.OP_APPLY, {})  # type: ignore[arg-type]
    assert applied.get(REQ_CARD_PAGE_OFFER_APPLY) is True
    assert ("POST", "/api/members/{slug}/dashboard/page-offer") in catalog.HOOKED_ROUTES
    assert ("POST", "/api/members/{slug}/dashboard/page-offer") not in catalog.MEMORY_ROUTES


@pytest.mark.asyncio
async def test_propose_change_cannot_raise_the_offer(monkeypatch: Any) -> None:
    """Only offer_dashboard_page raises it, so the asked-once record is always kept."""
    from kiro_crew.dashboard.handlers import change_cards

    async def caller(_req: Any, _op: str) -> tuple[str, str]:
        return "member-scout", "sk"

    async def body(_req: Any) -> dict[str, Any]:
        return {"kind": catalog.KIND_CREWMATE_DASHBOARD_PAGE, "params": {"member": MEMBER}}

    proposed: list[Any] = []

    async def propose(*a: Any, **k: Any) -> Any:
        proposed.append(k)
        return web.json_response({})

    monkeypatch.setattr(change_cards, "_resolve_agent_caller", caller)
    monkeypatch.setattr(change_cards, "_json_body", body)
    monkeypatch.setattr(change_cards, "propose_for_caller", propose)
    resp = await change_cards.api_cards_agent_propose(object())  # type: ignore[arg-type]
    assert resp.status == 403
    assert proposed == []


def test_the_mate_tool_asks_for_the_offer() -> None:
    from kiro_crew import agent, mcp_mate
    from kiro_crew.dashboard.handlers import member_self

    assert mcp_mate._PROPOSALS["offer_dashboard_page"] == "dashboard_page"
    assert member_self.SELF_PROPOSALS["dashboard_page"] == catalog.KIND_CREWMATE_DASHBOARD_PAGE
    # A gateway wake may raise it: the offer rides the wake that was running.
    assert "dashboard_page" in member_self.OWN_GOAL_PROPOSALS
    assert "@kirocrew-mate/offer_dashboard_page" in agent._MATE_AUTO_GRANTS


def _propose_env(monkeypatch: Any, *, flush_fails: bool) -> list[str]:
    """``propose_for_caller`` with its build and store faked; returns the store's calls."""
    from kiro_crew.dashboard import change_cards as cards
    from kiro_crew.dashboard.change_cards import CardError
    from kiro_crew.dashboard.handlers import change_cards as routes

    calls: list[str] = []

    class _Store:
        def propose(self, **_kw: Any) -> dict[str, Any]:
            calls.append("propose")
            return {"id": "card-1"}

        async def flush(self, strict: bool = False) -> None:
            calls.append("flush")
            if flush_fails:
                raise CardError(503, "checkpoint_failed", "disk full")

        def discard(self, _card_id: str) -> None:
            calls.append("discard")

    async def build(*_a: Any, **_kw: Any) -> tuple[str, dict, dict]:
        return "preview", {}, {}

    async def settle(_request: Any) -> None:
        return None

    async def resolve(_kind: str, params: Any) -> Any:
        return params

    store = _Store()
    monkeypatch.setattr(catalog, "validate_params", lambda _k, p: p)
    monkeypatch.setattr(cards, "resolve_params", resolve)
    monkeypatch.setattr(cards, "check_param_text", lambda _p: None)
    monkeypatch.setattr(cards, "clean_reason", lambda r: r)
    monkeypatch.setattr(cards, "build", build)
    monkeypatch.setattr(routes, "_settle", settle)
    monkeypatch.setattr(routes, "_store", lambda _r: store)
    return calls


class _Request(dict):
    app = {"state": object()}


@pytest.mark.asyncio
async def test_a_claim_that_fails_refuses_the_card_before_it_is_stored(monkeypatch: Any) -> None:
    from kiro_crew.dashboard.change_cards import CardError
    from kiro_crew.dashboard.handlers import change_cards as routes

    calls = _propose_env(monkeypatch, flush_fails=False)

    async def claim() -> Any:
        raise CardError(503, "page_offer_write_failed", "the page offer could not be recorded")

    resp = await routes.propose_for_caller(
        _Request(),
        "member-dm:atlas",
        "s",
        kind=catalog.KIND_CREWMATE_DASHBOARD_PAGE,
        params={"member": MEMBER},
        reason="",
        claim=claim,
    )
    assert resp.status == 503
    assert (json.loads(resp.text or "{}"))["code"] == "page_offer_write_failed"
    assert calls == []  # never stored, never shown


@pytest.mark.asyncio
async def test_a_card_that_fails_to_save_runs_the_claims_undo(monkeypatch: Any) -> None:
    from kiro_crew.dashboard.handlers import change_cards as routes

    calls = _propose_env(monkeypatch, flush_fails=True)

    async def claim() -> Any:
        calls.append("claim")

        async def undo() -> None:
            calls.append("undo")

        return undo

    resp = await routes.propose_for_caller(
        _Request(),
        "member-dm:atlas",
        "s",
        kind=catalog.KIND_CREWMATE_DASHBOARD_PAGE,
        params={"member": MEMBER},
        reason="",
        claim=claim,
    )
    assert resp.status == 503
    assert calls == ["claim", "propose", "flush", "discard", "undo"]


def test_a_crewmate_without_mate_tools_gets_no_offer_line(monkeypatch: Any) -> None:
    from kiro_crew import dashboard_agentic
    from kiro_crew.dashboard import goal_reminder

    rec = {"goals": [{"id": "g00000001", "text": "Docs"}], "key_results": [{"id": "k1"}]}
    monkeypatch.setattr(dashboard_agentic, "bound_package", lambda _s: "")
    monkeypatch.setattr(dashboard_agentic, "tab_shows_suggestions", lambda _s: False)
    monkeypatch.setattr(page_offer, "offer_line", lambda _s, _r: "OFFER")
    assert goal_reminder.dashboard_line(SLUG, rec) == "OFFER"
    # offer_dashboard_page is a kirocrew-mate tool: without it the line could never be acted on.
    assert goal_reminder.dashboard_line(SLUG, rec, has_mate_tools=False) == ""


def test_the_offer_snapshot_names_the_page_so_a_newer_template_refuses_the_yes(
    monkeypatch: Any,
) -> None:
    """A template adopted after the card was raised changes the snapshot the yes is
    compared against, so the yes is refused instead of replacing the newer page."""
    from kiro_crew import dashboard_agentic
    from kiro_crew.dashboard import change_cards_readers as readers

    monkeypatch.setattr(
        readers, "_crewmate_identity", lambda m: {"exists": True, "slug": SLUG, "member_id": "x"}
    )
    page = {"template_id": "goal-board", "replaces": "Goal board"}
    monkeypatch.setattr(dashboard_agentic, "page_offer_target", lambda slug: dict(page))
    raised = readers._page_offer_snapshot(MEMBER)
    page.update(template_id="office", replaces="Office")
    assert readers._page_offer_snapshot(MEMBER) != raised
    assert raised["page"]["template_id"] == "goal-board"


def test_the_yes_wake_carries_the_page_look_and_no_motion_rule_of_its_own() -> None:
    """The yes wake and dashboard_save's own description reach the crewmate in one turn,
    so the wake carries :data:`PAGE_LOOK` itself rather than a motion rule that could
    contradict it, and no met / open judgement the fold does not make."""
    from kiro_crew.dashboard_templates.page_look import PAGE_LOOK

    prompt = page_offer.write_prompt("Goal board")
    assert PAGE_LOOK in prompt
    low = " ".join(prompt.split()).lower()
    assert "nothing looping" not in low and "none under prefers-reduced-motion" not in low
    assert "met / open" not in low


def test_a_template_picked_after_the_yes_sends_no_write_wake(monkeypatch: Any, home: Path) -> None:
    """The yes was over one page; a template the person picked while the thread was
    busy is the newer choice, so the delayed wake is not sent and the record goes back."""
    from kiro_crew import dashboard_agentic
    from kiro_crew.dashboard import goal_setup

    calls: list[tuple] = []
    monkeypatch.setattr(goal_setup, "_schedule", lambda *a, **k: calls.append((a, k)))
    page_offer.record_asked(SLUG, "Goal board", "goal-board")
    assert page_offer.read_record(SLUG) == {"replaces": "Goal board", "template_id": "goal-board"}
    page_offer.schedule_write("state", SLUG, MEMBER)
    (((_state, _key, _slug, _member, build, _label), _kwargs),) = calls
    page = {"replaces": "Goal board", "template_id": "goal-board"}
    monkeypatch.setattr(dashboard_agentic, "page_offer_target", lambda _s: page)
    assert "in place of the Goal board template" in build({}, True)
    page = {"replaces": "Sprint board", "template_id": "sprint-board"}
    monkeypatch.setattr(dashboard_agentic, "page_offer_target", lambda _s: page)
    assert build({}, True) is None
    monkeypatch.setattr(dashboard_agentic, "page_offer_target", lambda _s: None)
    assert build({}, True) is None


def test_raising_the_card_records_the_page_it_was_raised_over(
    home: Path, _floor_monkeypatch: Any
) -> None:
    import asyncio

    from kiro_crew import dashboard_agentic
    from kiro_crew.dashboard.handlers import member_self

    _claim_env(_floor_monkeypatch)
    _floor_monkeypatch.setattr(
        dashboard_agentic,
        "page_offer_target",
        lambda _s: {"replaces": "Goal board", "template_id": "goal-board"},
    )
    asyncio.run(member_self._page_offer_claim(MEMBER)())
    assert page_offer.read_record(SLUG) == {"replaces": "Goal board", "template_id": "goal-board"}


@pytest.mark.asyncio
async def test_the_yes_names_the_page_the_card_last_previewed(home: Path, monkeypatch: Any) -> None:
    """A card refreshed after the person adopted another template carries that page; the
    yes wakes over it, not over the page the card was first raised over."""
    from kiro_crew import dashboard_agentic
    from kiro_crew.dashboard import goal_setup

    calls: list[tuple] = []
    monkeypatch.setattr(goal_setup, "_schedule", lambda *a, **k: calls.append((a, k)))
    page_offer.record_asked(SLUG, "Goal board", "goal-board")
    refreshed = {"replaces": "Sprint board", "template_id": "sprint-board"}
    client = await _client(monkeypatch, armed=True, page=refreshed)
    try:
        resp = await client.post(
            f"/api/members/{SLUG}/dashboard/page-offer", json={"member": MEMBER}
        )
        assert resp.status == 200, await resp.text()
    finally:
        await client.close()
    (((_state, _key, _slug, _member, build, _label), _kwargs),) = calls
    monkeypatch.setattr(dashboard_agentic, "page_offer_target", lambda _s: refreshed)
    assert "in place of the Sprint board template" in build({}, True)


def test_the_yes_wake_puts_the_template_back_by_id() -> None:
    """A page named for the crewmate that it never adopted has no version before the
    write, so the way back is the template by id, not a rollback."""
    text = page_offer.write_prompt("Goal board", "goal-board")
    assert 'dashboard_apply(template_id="goal-board")' in text
    assert "dashboard_rollback" not in text


def test_the_yes_marks_the_record_pending_until_its_wake_starts(
    monkeypatch: Any, home: Path
) -> None:
    """The yes waits in memory for an idle thread; the record says so until it starts."""
    from kiro_crew.dashboard import goal_setup

    calls: list[tuple] = []
    monkeypatch.setattr(goal_setup, "_schedule", lambda *a, **k: calls.append((a, k)))
    page_offer.record_asked(SLUG, "Goal board", "goal-board")
    page_offer.record_yes(SLUG)
    page_offer.schedule_write("state", SLUG, MEMBER)
    assert page_offer.read_record(SLUG) == {
        "replaces": "Goal board",
        "template_id": "goal-board",
        "pending": boot_id.current_boot_id(),
    }
    # Still asked in this process: the wake is waiting, not lost.
    assert page_offer.was_asked(SLUG) is True
    ((_args, kwargs),) = calls
    kwargs["on_started"]()
    assert page_offer.read_record(SLUG) == {"replaces": "Goal board", "template_id": "goal-board"}


def test_a_yes_a_restart_lost_is_offered_again(monkeypatch: Any, home: Path) -> None:
    """A gateway that exits while the yes waits leaves a mark naming an older process:
    the crewmate is not counted as asked, and raising the card again clears the mark."""
    import asyncio

    from kiro_crew.dashboard import goal_setup
    from kiro_crew.dashboard.handlers import member_self

    monkeypatch.setattr(goal_setup, "_schedule", lambda *a, **k: None)
    page_offer.record_asked(SLUG, "Goal board", "goal-board")
    page_offer.record_yes(SLUG)
    page_offer.schedule_write("state", SLUG, MEMBER)
    monkeypatch.setattr(boot_id, "_boot_id", "a-later-gateway")
    assert page_offer.was_asked(SLUG) is False
    _claim_env(monkeypatch)
    asyncio.run(member_self._page_offer_claim(MEMBER)())
    assert page_offer.read_record(SLUG) == {"replaces": "Goal board"}
    assert page_offer.was_asked(SLUG) is True


def test_a_started_wake_stays_asked_after_a_restart(monkeypatch: Any, home: Path) -> None:
    from kiro_crew.dashboard import goal_setup

    calls: list[tuple] = []
    monkeypatch.setattr(goal_setup, "_schedule", lambda *a, **k: calls.append((a, k)))
    page_offer.record_asked(SLUG, "Goal board")
    page_offer.record_yes(SLUG)
    page_offer.schedule_write("state", SLUG, MEMBER)
    ((_args, kwargs),) = calls
    kwargs["on_started"]()
    monkeypatch.setattr(boot_id, "_boot_id", "a-later-gateway")
    assert page_offer.was_asked(SLUG) is True


@pytest.mark.asyncio
async def test_a_wake_that_starts_runs_its_started_hook_only(monkeypatch: Any) -> None:
    from kiro_crew.dashboard import goal_setup

    async def started(*_a: Any) -> str:
        return "started"

    monkeypatch.setattr(goal_setup, "_wait_and_send", started)
    seen: list[str] = []

    class _State:
        pass

    state = _State()
    goal_setup._schedule(
        state,
        "k",
        SLUG,
        MEMBER,
        lambda *_a: "x",
        "test",
        on_dropped=lambda outcome: seen.append(f"dropped:{outcome}"),
        on_started=lambda: seen.append("started"),
    )
    for task in list(goal_setup._tasks(state).values()):
        await task
    assert seen == ["started"]
