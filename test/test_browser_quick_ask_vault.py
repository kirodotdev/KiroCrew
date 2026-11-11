"""Sign-in from the Secrets vault: a credentials ask answered with a saved login.

The page, hub and routes are the ones ``test_browser_quick_ask`` drives; the
vault is a real :class:`SecretVault` under the test's ``KIROCREW_HOME``. The bar
is the quick ask's own: a value reaches only the bound element, and nothing the
agent can read carries it, or the username. Here also: a login is offered only
for its exact origin, filled only on the person's pick, and a typed login is
stored only when the person ticked save, never over an existing item.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from test_browser_quick_ask import _home  # noqa: F401 - the autouse hub/home reset
from test_browser_quick_ask import CANARY, CREDS, KEY, FakePage, _answer, _asked, _login, _poll

from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.browser_harness import vault_fill
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers import browser_ask
from kiro_crew.mcp_tools import browser as tool
from kiro_crew.secrets import SecretVault
from kiro_crew.secrets.items import decode_item

ORIGIN = "https://github.com"
USER = "octo-user-CANARY"


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every SEL row the ask route and the vault fill write."""
    out: list[dict[str, Any]] = []
    fake = SimpleNamespace(log_api_access=lambda **kw: out.append(kw))
    monkeypatch.setattr(browser_ask, "_sel", lambda: fake)
    return out


@pytest.fixture
def preview_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault_fill, "enabled", lambda: True)


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
async def test_without_the_preview_the_card_and_the_route_are_as_before() -> None:
    await _store("GitHub", [ORIGIN])
    page = FakePage()
    _login(page)
    hub, _rec, ask = await _asked(page, dict(CREDS))
    assert "vault" not in lh.live_event(hub.get(KEY))["ask"]  # type: ignore[arg-type]
    status, out, _ = await _answer({"ask_id": ask.id, "action": "fill", "vault_item": "GitHub"})
    assert (status, out["error"]) == (400, "bad_action")
    assert page.inserts == []


@pytest.mark.asyncio
async def test_a_picked_login_fills_the_bound_fields_and_leaks_nowhere(
    preview_on: None, rows: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
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
async def test_two_accounts_on_one_site_carry_their_own_usernames(preview_on: None) -> None:
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
    preview_on: None, monkeypatch: pytest.MonkeyPatch
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
async def test_only_an_offered_login_still_matching_now_is_filled(preview_on: None) -> None:
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


@pytest.mark.asyncio
async def test_typed_values_are_saved_only_when_ticked_and_never_over_an_item(
    preview_on: None, rows: list[dict[str, Any]]
) -> None:
    await _store("github.com", ["https://example.com"], password="kept")
    page = FakePage()
    _login(page)
    hub, _rec, ask = await _asked(page, dict(CREDS))
    assert lh.live_event(hub.get(KEY))["ask"]["vault"] == {"items": []}  # type: ignore[arg-type]
    typed = {"username": USER, "password": CANARY}

    status, out, _ = await _answer(
        {"ask_id": ask.id, "action": "fill", "values": typed, "save": True}
    )
    assert (status, out) == (200, {"ok": True, "outcome": "filled", "saved": True})
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


@pytest.mark.asyncio
async def test_a_save_never_takes_a_name_the_secrets_route_reserves(
    preview_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard.handlers import secrets as secrets_mod

    # A remote caller, with a channel's .env line pointing at secret://github.com.
    monkeypatch.setattr(secrets_mod, "is_direct_local_request", lambda _request: False)
    monkeypatch.setattr(secrets_mod, "_channel_referenced_names", lambda: frozenset({"github.com"}))
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    typed = {"username": USER, "password": CANARY}
    status, out, _ = await _answer(
        {"ask_id": ask.id, "action": "fill", "values": typed, "save": True}
    )
    assert (status, out) == (200, {"ok": True, "outcome": "filled", "saved": True})
    assert sorted(_logins()) == ["github.com (2)"]


@pytest.mark.asyncio
async def test_no_free_name_writes_nothing() -> None:
    vault = SecretVault(config_dir())
    with pytest.raises(ValueError):
        await vault.add_item_under_free_name(
            "github.com", "login", {"password": "pw"}, lambda _name: True
        )
    assert vault.list_items() == []


@pytest.mark.asyncio
async def test_an_untick_saves_nothing_and_another_account_saves_beside_a_match(
    preview_on: None,
) -> None:
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    typed = {"username": USER, "password": CANARY}
    status, out, _ = await _answer({"ask_id": ask.id, "action": "fill", "values": typed})
    assert (status, out) == (200, {"ok": True, "outcome": "filled"})
    assert _logins() == {}

    await _store("GitHub", [ORIGIN])
    lh.reset_live_hub()
    page = FakePage()
    _login(page)
    _hub, _rec, ask = await _asked(page, dict(CREDS))
    other = {"username": "second-user", "password": "pw-second"}
    status, out, _ = await _answer(
        {"ask_id": ask.id, "action": "fill", "values": other, "save": True}
    )
    assert (status, out) == (200, {"ok": True, "outcome": "filled", "saved": True})
    logins = _logins()
    assert logins["GitHub"]["password"] == CANARY  # the match is untouched
    assert logins["github.com"]["username"] == "second-user"
