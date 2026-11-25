"""Sign-in from the Secrets vault: a credentials ask answered with a saved login.

The page, hub and routes are the ones ``test_browser_quick_ask`` drives; the
vault is a real :class:`SecretVault` under the test's ``KIROCREW_HOME``. The bar
is the quick ask's own: a value reaches only the bound element, and nothing the
agent can read carries it, or the username. Here also: a login is offered only
for its site, filled only on the person's pick, and a typed login is held in
memory only after the sign-in landed, stored only on the person's Save, and
never over an existing item.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_browser_live_routes import _app, _post
from test_browser_quick_ask import _home  # noqa: F401 - the autouse hub/home reset
from test_browser_quick_ask import CANARY, CREDS, KEY, FakePage, _answer, _asked, _login, _poll

from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.browser_harness import quick_ask as qa
from kiro_crew.browser_harness import vault_fill
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers import browser_ask
from kiro_crew.mcp_tools import browser as tool
from kiro_crew.secrets import SecretVault
from kiro_crew.secrets.items import decode_item

ORIGIN = "https://github.com"
USER = "octo-user-CANARY"
#: The real landing watch; the shared fixture stubs it out for every other test.
REAL_WAIT = vault_fill.wait_signed_in


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every SEL row the ask route and the vault fill write."""
    out: list[dict[str, Any]] = []
    fake = SimpleNamespace(log_api_access=lambda **kw: out.append(kw))
    monkeypatch.setattr(browser_ask, "_sel", lambda: fake)
    return out


async def _store(name: str, urls: list[str], username: str = USER, password: str = CANARY) -> None:
    await SecretVault(config_dir()).set_item_with_revision(
        name, "login", {"username": username, "password": password, "urls": urls}
    )


def _logins() -> dict[str, dict[str, Any]]:
    vault = SecretVault(config_dir())
    names = [r["name"] for r in vault.list_items() if r["type"] == "login"]
    return {
        n: decode_item(t, p.reveal()) for n, (t, p) in vault.get_items_readable(names)[0].items()
    }


@pytest.mark.parametrize(
    "url, page, matches",
    [
        # Base domain (eTLD+1): any host under it, any port.
        ("https://appleid.apple.com/sign-in", "https://apple.com", True),
        ("https://appleid.apple.com", "https://developer.apple.com", True),
        ("https://apple.com", "https://developer.apple.com:8443", True),
        ("https://apple.com.evil.example", "https://apple.com", False),
        # The private section counts: github.io is a public suffix.
        ("https://a.github.io", "https://b.github.io", False),
        ("https://a.github.io/x", "https://a.github.io", True),
        ("https://github.io", "https://a.github.io", False),
        ("https://www.bbc.co.uk", "https://news.bbc.co.uk", True),
        ("https://bbc.co.uk", "https://itv.co.uk", False),
        ("https://co.uk", "https://bbc.co.uk", False),
        # No registrable domain: exact host and port.
        ("http://localhost:3000", "http://localhost:3000", True),
        ("http://localhost:3000", "http://localhost:4000", False),
        ("http://127.0.0.1:8123", "http://127.0.0.1:8123", True),
        ("http://127.0.0.1:8123", "http://127.0.0.2:8123", False),
        ("http://127.0.0.1:8123", "http://localhost:8123", False),
        # Scheme: never down to http; up to https is fine.
        ("https://github.com", "http://github.com", False),
        ("http://github.com", "https://github.com", True),
        # IDN compares in punycode.
        ("https://bücher.de", "https://www.xn--bcher-kva.de", True),
        # Unparseable matches nothing.
        ("github.com", "https://github.com", False),
        ("https://[::1", "https://github.com", False),
        ("ftp://github.com", "https://github.com", False),
        ("", "https://github.com", False),
    ],
)
def test_a_url_matches_by_base_domain(url: str, page: str, matches: bool) -> None:
    assert vault_fill.url_matches(url, page) is matches


def test_a_saved_login_is_named_after_the_host() -> None:
    assert vault_fill.item_name_for("http://127.0.0.1:8123") == "127.0.0.1"
    assert vault_fill.item_name_for("http://[::1]:8123") == "[::1]"


@pytest.mark.asyncio
async def test_an_unreadable_vault_leaves_the_plain_card_and_refuses_a_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _store("GitHub", [ORIGIN])
    monkeypatch.setattr(vault_fill, "offer", lambda _origin: None)
    page = FakePage()
    _login(page)
    hub, _rec, ask = await _asked(page, dict(CREDS))
    assert "vault" not in lh.live_event(hub.get(KEY))["ask"]  # type: ignore[arg-type]
    status, out, _ = await _answer({"ask_id": ask.id, "action": "fill", "vault_item": "GitHub"})
    assert (status, out["error"]) == (400, "bad_action")
    assert page.inserts == []


@pytest.mark.asyncio
async def test_a_picked_login_fills_the_bound_fields_and_leaks_nowhere(
    rows: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await _store("GitHub", [f"{ORIGIN}/login"])
    await _store("Gist", ["https://gist.github.com"], password="other")
    await _store("GitLab", ["https://gitlab.com"], password="other")
    await _store("Pages", ["https://octo.github.io"], password="other")
    page = FakePage()
    _login(page)
    hub, rec, ask = await _asked(page, dict(CREDS))
    offered = lh.live_event(hub.get(KEY))["ask"]["vault"]  # type: ignore[arg-type]
    assert offered == {
        "items": [{"name": "Gist", "username": USER}, {"name": "GitHub", "username": USER}]
    }

    status, out, wire = await _answer({"ask_id": ask.id, "action": "fill", "vault_item": "GitHub"})
    assert (status, out) == (200, {"ok": True, "outcome": "filled"})
    assert page.inserts == [(page.refs["e3"], USER), (page.refs["e4"], CANARY)]
    polled = await _poll(ask.id)
    assert polled == {"done": True, "outcome": "filled"}
    for surface in (
        json.dumps(polled),
        tool._ask_text(polled["outcome"]),
        json.dumps(out),
        caplog.text,
        json.dumps(rows, default=str),
        json.dumps([e for _k, e in rec.events]),
    ):
        assert CANARY not in surface and USER not in surface
    # The owner's card shows the username to pick by; never a password.
    assert CANARY not in json.dumps(wire.events, default=str)
    assert "other" not in json.dumps(offered)
    fills = [r for r in rows if r["operation"] == vault_fill.OP_FILL]
    assert len(fills) == 1 and fills[0]["outcome"] == "filled"
    assert "item=GitHub" in fills[0]["resources"] and f"origin={ORIGIN}" in fills[0]["resources"]


@pytest.mark.asyncio
async def test_two_accounts_on_one_site_carry_their_own_usernames() -> None:
    await _store("GitHub", [ORIGIN], username="alice", password="pw-alice-CANARY")
    await _store("GitHub 2", [ORIGIN], username="bob", password="pw-bob-CANARY")
    page = FakePage()
    _login(page)
    hub, _rec, _ask = await _asked(page, dict(CREDS))
    event = lh.live_event(hub.get(KEY))  # type: ignore[arg-type]
    assert event["ask"]["vault"]["items"] == [
        {"name": "GitHub", "username": "alice"},
        {"name": "GitHub 2", "username": "bob"},
    ]
    assert "CANARY" not in json.dumps(event)


@pytest.mark.asyncio
async def test_matches_past_the_cap_are_counted_on_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vault_fill, "MAX_OFFERED", 2)
    for i in range(4):
        await _store(f"GitHub {i:02d}", [ORIGIN])
    page = FakePage()
    _login(page)
    hub, _rec, _ask = await _asked(page, dict(CREDS))
    wire = lh.live_event(hub.get(KEY))["ask"]["vault"]  # type: ignore[arg-type]
    assert len(wire["items"]) == 2 and wire["more"] == 2


@pytest.mark.asyncio
async def test_only_an_offered_login_still_matching_now_is_filled() -> None:
    await _store("GitHub", [ORIGIN])
    await _store("Other", ["https://example.com"])
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    status, out, _ = await _answer({"ask_id": ask.id, "action": "fill", "vault_item": "Other"})
    assert (status, out["error"]) == (400, "bad_action")
    # Moved to another site after the card was drawn: not filled, the ask stays open.
    await _store("GitHub", ["https://example.com"])
    status, out, _ = await _answer({"ask_id": ask.id, "action": "fill", "vault_item": "GitHub"})
    assert (status, out["error"]) == (409, "vault_unusable")
    assert page.inserts == [] and not ask.outcome


@pytest.fixture
def landed(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[bool]]:
    """The landing watch answers what the test sets; each call is counted."""
    said = [True]

    async def _watch(*_a: Any, **_k: Any) -> bool:
        return said[0]

    monkeypatch.setattr(vault_fill, "wait_signed_in", _watch)
    yield said
    for held in list(vault_fill._offers):
        vault_fill.drop_offer(held)


async def _save_offer(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    app, _wire = _app()
    async with TestClient(TestServer(app)) as client:
        return await _post(client, "/api/browser/ask/save-offer", {"session_key": KEY, **body})


def _files_holding(root: Path, needle: str) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and needle.encode() in p.read_bytes()]


@pytest.mark.asyncio
async def test_a_typed_sign_in_that_landed_waits_in_memory_until_save(
    rows: list[dict[str, Any]], landed: list[bool], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await _store("github.com", ["https://example.com"], password="kept")
    page = FakePage()
    _login(page)
    _hub, rec, ask = await _asked(page, dict(CREDS))
    typed = {"username": USER, "password": CANARY}
    status, out, wire = await _answer({"ask_id": ask.id, "action": "fill", "values": typed})
    assert status == 200 and out["outcome"] == "filled"
    offer = out["save_offer"]
    assert (offer["site"], offer["username"]) == ("github.com", USER)
    # Asked, not saved: the login is in gateway memory only, never on disk.
    assert sorted(_logins()) == ["github.com"]
    assert _files_holding(tmp_path, CANARY) == []
    polled = await _poll(ask.id)
    assert polled == {"done": True, "outcome": "filled"}
    for surface in (
        json.dumps(out),
        json.dumps(polled),
        caplog.text,
        json.dumps(rows, default=str),
        json.dumps([e for _k, e in rec.events]),
        json.dumps(wire.events, default=str),
    ):
        assert CANARY not in surface

    assert await _save_offer({"offer_id": offer["id"], "action": "save"}) == (
        200,
        {"ok": True, "saved": True},
    )
    logins = _logins()
    assert logins["github.com"]["password"] == "kept"
    assert logins["github.com (2)"] == {
        "username": USER,
        "password": CANARY,
        "urls": [ORIGIN],
        "notes": "",
    }
    saves = [r for r in rows if r["operation"] == vault_fill.OP_SAVE]
    assert len(saves) == 1 and "item=github.com (2)" in saves[0]["resources"]
    assert CANARY not in json.dumps(rows) and USER not in json.dumps(rows)
    # Answered once: the held login is gone.
    status, out = await _save_offer({"offer_id": offer["id"], "action": "save"})
    assert (status, out["error"]) == (409, "stale")
    assert vault_fill._offers == {}


@pytest.mark.asyncio
async def test_not_now_and_the_timeout_drop_the_held_login(landed: list[bool]) -> None:
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    typed = {"username": USER, "password": CANARY}
    _status, out, _ = await _answer({"ask_id": ask.id, "action": "fill", "values": typed})
    held = vault_fill._offers[KEY]
    assert await _save_offer({"offer_id": out["save_offer"]["id"], "action": "drop"}) == (
        200,
        {"ok": True, "saved": False},
    )
    assert _logins() == {} and held.values == {} and vault_fill._offers == {}

    # Unanswered: the timer armed for SAVE_OFFER_TTL_S drops it.
    shown = vault_fill.hold_offer(KEY, ORIGIN, typed)
    held = vault_fill._offers[KEY]
    assert held.timer is not None
    left = held.timer.when() - asyncio.get_running_loop().time()
    assert 0 < left <= vault_fill.SAVE_OFFER_TTL_S
    vault_fill._expire(KEY, held)
    assert held.values == {} and vault_fill._offers == {}
    status, out = await _save_offer({"offer_id": shown["id"], "action": "save"})
    assert (status, out["error"]) == (409, "stale")
    assert _logins() == {}


@pytest.mark.asyncio
async def test_a_newer_offer_drops_the_older_one(landed: list[bool]) -> None:
    first = vault_fill.hold_offer(KEY, ORIGIN, {"username": "a", "password": "pw-a"})
    old = vault_fill._offers[KEY]
    vault_fill.hold_offer(KEY, ORIGIN, {"username": "b", "password": "pw-b"})
    assert old.values == {} and old.timer is None
    status, out = await _save_offer({"offer_id": first["id"], "action": "save"})
    assert (status, out["error"]) == (409, "stale")


@pytest.mark.asyncio
async def test_a_sign_in_that_did_not_land_offers_nothing(landed: list[bool]) -> None:
    landed[0] = False
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    typed = {"username": USER, "password": CANARY}
    status, out, _ = await _answer({"ask_id": ask.id, "action": "fill", "values": typed})
    assert (status, out) == (200, {"ok": True, "outcome": "filled"})
    assert vault_fill._offers == {} and _logins() == {}


@pytest.mark.asyncio
async def test_a_login_the_vault_holds_is_not_offered_and_another_account_is(
    landed: list[bool],
) -> None:
    await _store("GitHub", [ORIGIN])
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    status, out, _ = await _answer({"ask_id": ask.id, "action": "fill", "vault_item": "GitHub"})
    assert (status, out) == (200, {"ok": True, "outcome": "filled"})

    lh.reset_live_hub()
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    typed = {"username": USER, "password": "pw-typed-again"}
    status, out, _ = await _answer({"ask_id": ask.id, "action": "fill", "values": typed})
    assert (status, out) == (200, {"ok": True, "outcome": "filled"})

    lh.reset_live_hub()
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    other = {"username": "second-user", "password": "pw-second"}
    status, out, _ = await _answer({"ask_id": ask.id, "action": "fill", "values": other})
    assert out["save_offer"]["username"] == "second-user"
    assert await _save_offer({"offer_id": out["save_offer"]["id"], "action": "save"}) == (
        200,
        {"ok": True, "saved": True},
    )
    logins = _logins()
    assert logins["GitHub"]["password"] == CANARY  # the match is untouched
    assert logins["github.com"]["username"] == "second-user"


@pytest.mark.asyncio
async def test_a_save_never_takes_a_name_the_secrets_route_reserves(
    monkeypatch: pytest.MonkeyPatch, landed: list[bool]
) -> None:
    from kiro_crew.dashboard.handlers import secrets as secrets_mod

    # A remote caller, with a channel's .env line pointing at secret://github.com.
    monkeypatch.setattr(secrets_mod, "is_direct_local_request", lambda _request: False)
    monkeypatch.setattr(secrets_mod, "_channel_referenced_names", lambda: frozenset({"github.com"}))
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    typed = {"username": USER, "password": CANARY}
    _status, out, _ = await _answer({"ask_id": ask.id, "action": "fill", "values": typed})
    assert await _save_offer({"offer_id": out["save_offer"]["id"], "action": "save"}) == (
        200,
        {"ok": True, "saved": True},
    )
    assert sorted(_logins()) == ["github.com (2)"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"offer_id": "x", "action": "never"},
        {"offer_id": 7, "action": "save"},
        {"action": "save"},
        {"offer_id": "x", "action": "save", "session_key": "chat-2"},
    ],
)
async def test_the_save_offer_route_refuses_a_bad_body(body: dict[str, Any]) -> None:
    status, out = await _save_offer(body)
    assert status in (400, 404) and out["ok"] is False


# ---- the landing watch ---------------------------------------------------------


class _Tab:
    """``top_frame`` answers, one per read; the last one repeats."""

    def __init__(self, *tops: tuple[str, str, str]) -> None:
        self.tops = list(tops)

    async def top_frame(self) -> tuple[str, str, str]:
        return self.tops.pop(0) if len(self.tops) > 1 else self.tops[0]


DOC = ("F1", "L1", ORIGIN)


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch) -> list[bool | None]:
    """What the bound site shows per read (a password box, none, unreadable)."""
    out: list[bool | None] = []

    async def _shown(*_a: Any) -> bool | None:
        return out.pop(0) if len(out) > 1 else out[0]

    monkeypatch.setattr(vault_fill, "password_shown", _shown)
    monkeypatch.setattr(qa, "STEP_POLL_S", 0.0)
    monkeypatch.setattr(vault_fill, "LAND_QUIET_S", 0.0)
    return out


async def _watch(
    tab: _Tab,
    stop: bool = False,
    *,
    stop_after: int = 0,
    folded: bool = False,
    base: tuple[str, str, str] | None = None,
) -> bool:
    ask = SimpleNamespace(top_origin=ORIGIN)
    asked = [0]

    def _stop() -> bool:
        asked[0] += 1
        return stop or (stop_after > 0 and asked[0] > stop_after)

    return await REAL_WAIT(tab, ask, None, _stop, lambda: folded, base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_watch_lands_when_the_password_box_goes(shown: list[bool | None]) -> None:
    shown[:] = [True, None, False]
    assert await _watch(_Tab(DOC, DOC, ("F1", "L2", ORIGIN))) is True


@pytest.mark.asyncio
async def test_the_watch_lands_when_the_tab_leaves_the_site(shown: list[bool | None]) -> None:
    shown[:] = [True]
    assert await _watch(_Tab(DOC, DOC, ("F1", "L2", "https://app.example.com"))) is True


@pytest.mark.asyncio
async def test_a_new_page_asking_for_the_password_again_has_not_landed(
    shown: list[bool | None],
) -> None:
    shown[:] = [True]
    assert await _watch(_Tab(DOC, DOC, ("F1", "L2", ORIGIN))) is False


@pytest.mark.asyncio
async def test_the_watch_gives_up_at_its_bound_and_on_stop(
    shown: list[bool | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    shown[:] = [True]
    # The bound already spent is the case under test.
    monkeypatch.setattr(vault_fill, "LAND_WAIT_S", 0.0)  # flake-ok: bound under test
    assert await _watch(_Tab(DOC)) is False
    # No password box, but not for LAND_QUIET_S yet when time is up.
    monkeypatch.setattr(vault_fill, "LAND_QUIET_S", 3600.0)  # flake-ok: never reached
    shown[:] = [False]
    assert await _watch(_Tab(DOC)) is False
    shown[:] = [False]
    monkeypatch.setattr(vault_fill, "LAND_QUIET_S", 0.0)
    assert await _watch(_Tab(DOC), stop=True) is False


@pytest.mark.asyncio
async def test_no_free_name_writes_nothing() -> None:
    vault = SecretVault(config_dir())
    with pytest.raises(ValueError):
        await vault.add_item_under_free_name(
            "github.com", "login", {"password": "pw"}, lambda _name: True
        )
    assert vault.list_items() == []


def test_a_locked_password_box_still_counts_as_not_signed_in() -> None:
    # A form that disables its fields while it checks the password has not landed:
    # the landing read counts every laid-out password box, unlike the fill's finder.
    js = vault_fill._PASSWORD_PRESENT_JS
    assert "type" in js and "password" in js
    assert "disabled" not in js and "readOnly" not in js
    assert "disabled" in qa._FIND_JS


@pytest.mark.asyncio
async def test_a_turn_that_ends_mid_watch_lands_only_on_a_new_page_with_no_password_box(
    shown: list[bool | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The agent finishes inside the quiet window: the hub drops the stream, so the
    # watch stops early and its last read decides.
    monkeypatch.setattr(vault_fill, "LAND_QUIET_S", 3600.0)  # flake-ok: never reached
    shown[:] = [False]
    new_page = _Tab(DOC, ("F1", "L2", ORIGIN))
    assert await _watch(new_page, stop_after=1, folded=True) is True
    assert await _watch(_Tab(DOC, ("F1", "L2", ORIGIN)), stop_after=1, folded=False) is False
    # Same document, or a new one still asking for the password: not landed.
    assert await _watch(_Tab(DOC), stop_after=1, folded=True) is False
    shown[:] = [None]
    assert await _watch(_Tab(DOC, ("F1", "L2", ORIGIN)), stop_after=1, folded=True) is False


@pytest.mark.asyncio
async def test_a_page_that_moved_on_during_the_fill_counts_as_new_against_the_pre_fill_read(
    shown: list[bool | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault_fill, "LAND_QUIET_S", 3600.0)  # flake-ok: never reached
    shown[:] = [False]
    success = ("F1", "L2", ORIGIN)
    # The success page was already up when the watch began; the turn then ends.
    assert await _watch(_Tab(success), stop_after=1, folded=True, base=DOC) is True
    assert await _watch(_Tab(success), stop_after=1, folded=True) is False


@pytest.mark.asyncio
async def test_the_offer_tells_the_sheet_how_long_it_is_held() -> None:
    shown = vault_fill.hold_offer(KEY, ORIGIN, {"username": "a", "password": "pw-a"})
    assert shown["expires_in_s"] == int(vault_fill.SAVE_OFFER_TTL_S)
