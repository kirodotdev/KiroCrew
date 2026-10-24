"""Typed vault items: encoding, references, and the vault/API seams."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.secrets import setup_secrets_routes
from kiro_crew.mcp_gateway.secret_uri import resolve_secret_uris
from kiro_crew.secrets import items
from kiro_crew.secrets.vault import SecretVault, entry_item_type

_LOGIN = {
    "username": "zezhexu",
    "password": "pw-1",
    "urls": ["https://github.com"],
    "notes": "",
}


# ── items: validation and encoding ──


def test_secret_item_encodes_to_its_bare_value() -> None:
    assert items.encode_item("secret", {"value": " sk-1 "}) == " sk-1 "


def test_login_encodes_to_canonical_json_and_decodes_back() -> None:
    plaintext = items.encode_item("login", {**_LOGIN, "extra": "dropped"})
    fields = items.decode_item("login", plaintext)
    assert fields == _LOGIN
    assert "extra" not in plaintext


@pytest.mark.parametrize(
    ("item_type", "fields", "code"),
    [
        ("login", {"username": "u"}, "missing_field"),
        ("login", {"password": "p", "urls": ["ftp://x"]}, "invalid_url"),
        ("login", {"password": "p", "urls": ["https://u:pw@example.com/"]}, "invalid_url"),
        ("login", {"password": "p", "urls": ["https://u@example.com/"]}, "invalid_url"),
        ("login", {"password": "p", "urls": [{"href": "https://a"}]}, "invalid_field_type"),
        ("login", {"password": 1}, "invalid_field_type"),
        ("note", {"text": "   "}, "missing_field"),
        ("note", {"text": "x" * (items.MAX_FIELD_CHARS + 1)}, "field_too_long"),
        ("login", {"password": "p", "urls": ["https://[::1"]}, "invalid_url"),
        ("card", {"number": "1"}, "unknown_type"),
    ],
)
def test_validate_item_refuses_with_a_stable_code(item_type: str, fields: dict, code: str) -> None:
    with pytest.raises(items.ItemError) as excinfo:
        items.validate_item(item_type, fields)
    assert excinfo.value.code == code


def test_a_plain_secret_keeps_no_length_bound() -> None:
    long_value = "x" * (items.MAX_FIELD_CHARS + 1)
    assert items.encode_item("secret", {"value": long_value}) == long_value


def test_public_view_shows_username_and_urls_but_never_a_credential() -> None:
    view = items.public_view("login", items.encode_item("login", _LOGIN))
    assert view == {"username": "zezhexu", "urls": _LOGIN["urls"]}
    # A URL that carries userinfo is never shown, even one stored by hand.
    stored = json.dumps({**_LOGIN, "urls": ["https://u:pw@example.com/", "https://a.example"]})
    assert items.public_view("login", stored)["urls"] == ["https://a.example"]
    assert items.public_view("secret", "sk-1") == {}
    assert items.public_view("note", items.encode_item("note", {"text": "t"})) == {}


# ── items: references ──


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("GITHUB", ("GITHUB", None)),
        ("GITHUB#username", ("GITHUB", "username")),
        ("GITHUB#", ("GITHUB", "")),
        ("A#b#c", ("A", "b#c")),
    ],
)
def test_parse_reference_splits_on_the_first_separator(ref: str, expected: tuple) -> None:
    assert items.parse_reference(ref) == expected


def test_bare_reference_means_the_default_field() -> None:
    plaintext = items.encode_item("login", _LOGIN)
    assert items.resolve_field("login", plaintext, None) == "pw-1"
    assert items.resolve_field("login", plaintext, "username") == "zezhexu"
    assert items.resolve_field("login", plaintext, "url") == "https://github.com"
    assert items.resolve_field("secret", "sk-1", None) == "sk-1"
    assert items.resolve_field("note", items.encode_item("note", {"text": "t"}), None) == "t"


def test_unknown_field_fails_closed() -> None:
    with pytest.raises(items.ItemError) as excinfo:
        items.resolve_field("secret", "sk-1", "username")
    assert excinfo.value.code == "unknown_field"


# ── vault seam ──


def test_vault_round_trips_a_login_and_binds_its_type_into_the_ciphertext(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    revision = asyncio.run(vault.set_item_with_revision("GITHUB", "login", _LOGIN))
    assert {row["name"]: row["revision"] for row in vault.list_items()} == {"GITHUB": revision}
    got = vault.get_items_many(["GITHUB"])["GITHUB"]
    assert got is not None
    item_type, plaintext = got
    assert item_type == "login"
    assert items.decode_item("login", plaintext.reveal()) == _LOGIN
    # The plain reader answers the default field, so an existing consumer keeps working.
    assert vault.get("GITHUB").reveal() == "pw-1"  # type: ignore[union-attr]
    assert vault.get_many(["GITHUB"])["GITHUB"].reveal() == "pw-1"  # type: ignore[union-attr]
    # Relabelling the entry's type in the store does not read it as another type.
    store = tmp_path / ".vault" / "secrets.enc"
    envelope = json.loads(store.read_text())
    envelope["entries"]["GITHUB"]["type"] = "note"
    store.write_text(json.dumps(envelope))
    with pytest.raises(Exception):
        SecretVault(tmp_path).get_items_many(["GITHUB"])


def test_updating_a_login_keeps_the_fields_the_request_leaves_out(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    asyncio.run(vault.set_item_with_revision("GITHUB", "login", {**_LOGIN, "notes": "n"}))
    # An edit form that cannot show the password or notes sends neither.
    edit = {"username": "octocat", "urls": ["https://github.com/login"]}
    asyncio.run(vault.set_item_with_revision("GITHUB", "login", edit))
    stored = vault.get_items_many(["GITHUB"])["GITHUB"]
    assert stored is not None
    assert items.decode_item("login", stored[1].reveal()) == {
        **edit,
        "password": "pw-1",
        "notes": "n",
    }
    # Sent as "", a field is cleared.
    asyncio.run(vault.set_item_with_revision("GITHUB", "login", {**edit, "notes": ""}))
    stored = vault.get_items_many(["GITHUB"])["GITHUB"]
    assert stored is not None
    assert items.decode_item("login", stored[1].reveal())["notes"] == ""
    # A NEW login still needs a password.
    with pytest.raises(items.ItemError):
        asyncio.run(vault.set_item_with_revision("OTHER", "login", edit))
    assert [row["name"] for row in vault.list_items()] == ["GITHUB"]


def test_a_plain_secret_entry_keeps_its_two_field_shape(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    asyncio.run(vault.set_item_with_revision("API", "secret", {"value": "sk-1"}))
    asyncio.run(vault.set_with_revision("API2", "sk-2"))
    entries = json.loads((tmp_path / ".vault" / "secrets.enc").read_text())["entries"]
    assert set(entries["API"]) == set(entries["API2"]) == {"nonce", "ct"}
    assert entry_item_type(entries["API"]) == "secret"
    rows = {row["name"]: row for row in vault.list_items()}
    assert rows["API"]["type"] == "secret" and "updated_at" not in rows["API"]


def test_list_items_reads_type_and_updated_at_without_decrypting(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    asyncio.run(vault.set_item_with_revision("N", "note", {"text": "hello"}))
    (row,) = vault.list_items()
    assert row["name"] == "N" and row["type"] == "note" and row["updated_at"]
    assert vault.get_items_many(["N", "missing"])["missing"] is None


# ── secret_uri seam ──


def test_resolve_secret_uris_picks_fields_and_defaults(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    asyncio.run(vault.set_item_with_revision("GITHUB", "login", _LOGIN))
    vault.set_sync("PLAIN", "sk-1")
    env = {
        "GH_USER": "secret://GITHUB#username",
        "GH_PASS": "secret://GITHUB",
        "API": "secret://PLAIN",
        "KEEP": "literal",
    }
    resolved, keys = resolve_secret_uris(env, tmp_path)
    assert resolved == {"GH_USER": "zezhexu", "GH_PASS": "pw-1", "API": "sk-1", "KEEP": "literal"}
    assert keys == {"GH_USER", "GH_PASS", "API"}


def test_a_stored_name_holding_hash_still_resolves_whole(tmp_path: Path) -> None:
    # Stored before field references existed (POST now refuses such a name).
    SecretVault(tmp_path).set_sync("A#B", "legacy")
    resolved, _ = resolve_secret_uris({"K": "secret://A#B"}, tmp_path)
    assert resolved == {"K": "legacy"}


def test_a_whole_name_match_never_decrypts_the_base_name(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    vault.set_sync("A#B", "legacy")
    vault.set_sync("A", "base")
    store = tmp_path / ".vault" / "secrets.enc"
    envelope = json.loads(store.read_text())
    envelope["entries"]["A"]["type"] = "note"  # A now fails to decrypt
    store.write_text(json.dumps(envelope))
    resolved, _ = resolve_secret_uris({"K": "secret://A#B"}, tmp_path)
    assert resolved == {"K": "legacy"}


def test_resolve_secret_uris_refuses_an_unknown_field_naming_only_the_key(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    vault.set_sync("PLAIN", "sk-1")
    with pytest.raises(ValueError) as excinfo:
        resolve_secret_uris({"API": "secret://PLAIN#username"}, tmp_path)
    message = str(excinfo.value)
    assert "'API'" in message and "unknown_field" in message
    assert "PLAIN" not in message and "sk-1" not in message


# ── dashboard API seam ──


class _FakeState:
    owner_id = ""


def _app() -> web.Application:
    @web.middleware
    async def _identity(request, handler):
        request["user"] = "local-app"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    setup_secrets_routes(app)
    return app


@pytest.mark.asyncio
async def test_post_typed_item_then_list_shows_public_fields_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.dashboard.handlers.secrets as handlers

    monkeypatch.setattr(handlers, "config_dir", lambda: str(tmp_path))
    async with TestClient(TestServer(_app())) as client:
        await _exercise_typed_round_trip(client)


async def _exercise_typed_round_trip(client: TestClient) -> None:
    resp = await client.post(
        "/api/secrets", json={"name": "GITHUB", "type": "login", "fields": _LOGIN}
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["type"] == "login" and body["revision"]

    resp = await client.post("/api/secrets", json={"name": "PLAIN", "value": "sk-1"})
    assert resp.status == 200

    resp = await client.get("/api/secrets")
    listing = await resp.json()
    assert listing["names"] == ["GITHUB", "PLAIN"]
    by_name = {item["name"]: item for item in listing["items"]}
    assert by_name["GITHUB"]["type"] == "login"
    assert by_name["GITHUB"]["fields"] == {
        "username": "zezhexu",
        "urls": _LOGIN["urls"],
    }
    assert by_name["PLAIN"]["type"] == "secret" and by_name["PLAIN"].get("fields") is None
    text = json.dumps(listing)
    assert "pw-1" not in text and "sk-1" not in text


@pytest.mark.asyncio
async def test_an_item_that_no_longer_decrypts_is_listed_as_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.dashboard.handlers.secrets as handlers

    vault = SecretVault(tmp_path)
    await vault.set_item_with_revision("GITHUB", "login", _LOGIN)
    await vault.set_item_with_revision("NOTE", "note", {"text": "t"})
    store = tmp_path / ".vault" / "secrets.enc"
    envelope = json.loads(store.read_text())
    envelope["entries"]["GITHUB"]["type"] = "note"
    store.write_text(json.dumps(envelope))
    monkeypatch.setattr(handlers, "config_dir", lambda: str(tmp_path))
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get("/api/secrets")
        assert resp.status == 200
        by_name = {item["name"]: item for item in (await resp.json())["items"]}
    assert by_name["GITHUB"]["corrupt"] is True and by_name["GITHUB"]["fields"] == {}
    assert "corrupt" not in by_name["NOTE"]


@pytest.mark.asyncio
async def test_a_missing_key_fails_the_list_instead_of_marking_items_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.dashboard.handlers.secrets as handlers

    vault = SecretVault(tmp_path)
    await vault.set_item_with_revision("GITHUB", "login", _LOGIN)
    # A restored backup without its key file: every entry is intact.
    vault._key_path.unlink()
    await _assert_list_fails_unreadable(tmp_path, monkeypatch, handlers)


@pytest.mark.asyncio
async def test_a_truncated_key_fails_the_list_instead_of_marking_items_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.dashboard.handlers.secrets as handlers

    vault = SecretVault(tmp_path)
    await vault.set_item_with_revision("GITHUB", "login", _LOGIN)
    vault._key_path.write_bytes(vault._key_path.read_bytes()[:31])
    await _assert_list_fails_unreadable(tmp_path, monkeypatch, handlers)


@pytest.mark.asyncio
async def test_another_full_length_key_fails_the_list_instead_of_marking_items_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.dashboard.handlers.secrets as handlers

    vault = SecretVault(tmp_path)
    await vault.set_item_with_revision("GITHUB", "login", _LOGIN)
    await vault.set_item_with_revision("NOTE", "note", {"text": "t"})
    await vault.set("PLAIN", "sk-1")
    # A restored store next to this install's own key: every entry is intact.
    vault._key_path.write_bytes(bytes(range(32)))
    await _assert_list_fails_unreadable(tmp_path, monkeypatch, handlers)


async def _assert_list_fails_unreadable(tmp_path: Path, monkeypatch, handlers) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(handlers, "config_dir", lambda: str(tmp_path))
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get("/api/secrets")
        assert resp.status == 500
        body = await resp.json()
    assert body["code"] == "vault_unreadable"
    assert "corrupt" not in json.dumps(body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"name": "X", "type": "login", "value": "p"}, "missing_fields"),
        ({"name": "X", "type": "login", "fields": {"username": "u"}}, "missing_field"),
        ({"name": "X", "type": "card", "fields": {}}, "invalid_type"),
        ({"name": "X", "type": "note", "fields": "text"}, "invalid_fields"),
        ({"name": "A#B", "value": "v"}, "invalid_name"),
    ],
)
async def test_post_refuses_a_bad_typed_body_with_a_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict, code: str
) -> None:
    import kiro_crew.dashboard.handlers.secrets as handlers

    monkeypatch.setattr(handlers, "config_dir", lambda: str(tmp_path))
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/secrets", json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == code
    assert not (tmp_path / ".vault" / "secrets.enc").exists()
