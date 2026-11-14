"""The third-party secret backend core: registry, routes, and a fake provider on the contract."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, ClassVar, Optional
from urllib.parse import quote

import pytest
from aiohttp.test_utils import TestClient, TestServer
from secret_backend_contract import Harness, SecretBackendContract

from kiro_crew.secrets import backend as core
from kiro_crew.secrets import items
from kiro_crew.secrets.vault import SecretValue, SecretVault

FAKE_TOKEN = "fake-token-123"


class FakeBackend(core.SecretBackend):
    """An in-memory provider: what a second provider looks like, end to end."""

    id: ClassVar[str] = "fakepm"
    display_name: ClassVar[str] = "FakePM"
    capabilities: ClassVar[core.Capabilities] = core.Capabilities(
        item_types=(items.ITEM_TYPE_SECRET,)
    )
    connect_form: ClassVar[tuple[core.FormField, ...]] = (
        core.FormField("token", "settings.secrets.backend_op_token_label", secret=True),
    )

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.fail: Optional[Exception] = None
        self.release: Optional[threading.Event] = None
        self.available = True

    def _work(self) -> None:
        if self.release is not None:
            assert self.release.wait(5)
        if self.fail is not None:
            raise self.fail

    def availability(self) -> Optional[core.BackendError]:
        return None if self.available else core.BackendError("sdk_missing", "FakePM is missing.")

    def connect(self, values: Mapping[str, str]) -> core.Connection:
        if values["token"] != FAKE_TOKEN:
            raise core.BackendError("unauthorized", "FakePM refused the token.")
        return core.Connection(account="Team", data={"token": values["token"]})

    def list_items(self, conn: Mapping[str, str]) -> list[dict[str, Any]]:
        self._work()
        return [{"name": n, "type": "secret", "fields": {}} for n in self.store]

    def get_items_many(
        self, conn: Mapping[str, str], names: list[str]
    ) -> dict[str, Optional[tuple[str, SecretValue]]]:
        self._work()
        return {
            n: ("secret", SecretValue(self.store[n])) if n in self.store else None for n in names
        }


class FakeHarness(Harness):
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend

    def good_values(self) -> dict[str, str]:
        return {"token": FAKE_TOKEN}

    def bad_values(self) -> dict[str, str]:
        return {"token": "wrong"}

    def add_secret(self, name: str, value: str) -> None:
        self.backend.store[name] = value  # type: ignore[attr-defined]

    def fail_with(self, exc: Exception) -> None:
        self.backend.fail = exc  # type: ignore[attr-defined]

    def hang_until(self, release: threading.Event) -> None:
        self.backend.release = release  # type: ignore[attr-defined]

    def make_unavailable(self) -> None:
        self.backend.available = False  # type: ignore[attr-defined]


@pytest.fixture
def fake() -> Iterator[FakeBackend]:
    backend = FakeBackend()
    core.register_secret_backend(backend)
    try:
        yield backend
    finally:
        core.reset_registered_for_tests()


class TestFakeBackendContract(SecretBackendContract):
    @pytest.fixture
    def harness(self, fake: FakeBackend) -> Harness:
        return FakeHarness(fake)


# ── registry ──


def test_registry_refuses_what_the_core_cannot_serve(fake: FakeBackend) -> None:
    with pytest.raises(ValueError, match="already registered"):
        core.register_secret_backend(FakeBackend())

    class UnknownType(FakeBackend):
        id = "unknowntype"
        capabilities = core.Capabilities(item_types=("card",))

    class BadId(FakeBackend):
        id = "Bad Id"

    class BadKey(FakeBackend):
        id = "badkey"
        connect_form = (core.FormField("token", "nav.home", secret=True),)

    class NoForm(FakeBackend):
        id = "noform"
        connect_form = ()

    for cls in (UnknownType, BadId, BadKey, NoForm):
        with pytest.raises(ValueError):
            core.register_secret_backend(cls())
    assert [b.id for b in core.registered()] == ["1password", "fakepm"]


def test_every_builtin_passes_registration_checks() -> None:
    for backend in core.registered():
        core._validate(backend)


# ── resolution order across backends ──


def test_the_first_backend_holding_a_name_answers_it(
    fake: FakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Second(FakeBackend):
        id = "second"

    second = Second()
    core.register_secret_backend(second)
    monkeypatch.setattr(core, "preview_on", lambda: True)
    vault = SecretVault(tmp_path)
    for backend in (fake, second):
        conn = backend.connect({"token": FAKE_TOKEN})
        vault._set_item_sync(core.connection_item(backend.id), "note", core.connection_fields(conn))
    fake.store["X"] = "first"
    second.store.update({"X": "second", "Y": "only-second"})
    asked: list[tuple[str, list[str]]] = []
    found = core.resolve_missing(vault, ["X", "Y"], lambda b, n, o, c: asked.append((b.id, n)))
    assert {k: v[1].reveal() for k, v in found.items()} == {"X": "first", "Y": "only-second"}
    assert asked == [("fakepm", ["X", "Y"]), ("second", ["Y"])]
    views = {v["id"]: v for v in core.backends_view(vault, [])}
    shadowed = {r["name"]: r["shadowed"] for r in views["second"]["items"]}
    assert shadowed == {"X": True, "Y": False}


def test_a_provider_answering_a_wrong_shape_reads_as_missing(
    fake: FakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "preview_on", lambda: True)
    vault = SecretVault(tmp_path)
    conn = fake.connect({"token": FAKE_TOKEN})
    vault._set_item_sync(core.connection_item(fake.id), "note", core.connection_fields(conn))
    monkeypatch.setattr(fake, "get_items_many", lambda c, n: {"X": ("login", "plain str")})
    assert core.resolve_missing(vault, ["X"], lambda *a: None) == {}


def test_a_provider_title_holding_a_hash_resolves_as_its_whole_text(
    fake: FakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.mcp_gateway.secret_uri import resolve_secret_uris

    monkeypatch.setattr(core, "preview_on", lambda: True)
    vault = SecretVault(tmp_path)
    conn = fake.connect({"token": FAKE_TOKEN})
    vault._set_item_sync(core.connection_item(fake.id), "note", core.connection_fields(conn))
    fake.store.update({"API#1": "whole", "API": "name-part"})
    resolved, _ = resolve_secret_uris({"K": "secret://API#1"}, tmp_path)
    assert resolved == {"K": "whole"}


def test_a_title_a_local_name_part_answers_is_listed_as_taken(
    fake: FakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A local GITHUB answers secret://GITHUB#personal#username, so the
    # provider's GITHUB#personal can never be reached and must say so.
    monkeypatch.setattr(core, "preview_on", lambda: True)
    vault = SecretVault(tmp_path)
    conn = fake.connect({"token": FAKE_TOKEN})
    vault._set_item_sync(core.connection_item(fake.id), "note", core.connection_fields(conn))
    fake.store.update({"GITHUB#personal": "p", "OTHER#x": "o"})
    view = next(v for v in core.backends_view(vault, ["GITHUB"]) if v["id"] == fake.id)
    assert {r["name"]: r["shadowed"] for r in view["items"]} == {
        "GITHUB#personal": True,
        "OTHER#x": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["undecryptable", "not_a_connection"])
async def test_an_unreadable_connection_degrades_and_can_be_disconnected(
    fake: FakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    from kiro_crew.mcp_gateway.secret_uri import resolve_secret_uris

    vault = SecretVault(tmp_path)
    vault.set_sync("LOCAL_ONE", "v")
    name = core.connection_item(fake.id)
    vault._set_item_sync(name, "note", {"text": "not json"})
    if damage == "undecryptable":
        real = SecretVault.get_items_readable

        def readable(self: SecretVault, names: list[str]) -> Any:
            found, bad = real(self, names)
            return {k: v for k, v in found.items() if k != name}, bad | ({name} & set(names))

        monkeypatch.setattr(SecretVault, "get_items_readable", readable)
    app = _app_with(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="connection_unreadable"):
        resolve_secret_uris({"K": "secret://REMOTE_ONLY"}, tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/secrets")
        listed = await resp.json()
        view = next(v for v in listed["backends"] if v["id"] == fake.id)
        gone = await client.delete(f"/api/secrets/backends/{fake.id}")
    assert resp.status == 200 and listed["names"] == ["LOCAL_ONE"]
    assert view["connected"] is True and view["status"] == "unavailable"
    assert view["error"]["code"] == "connection_unreadable"
    assert gone.status == 200 and name not in vault.list_names()


def test_no_name_a_person_can_store_is_a_connection_name() -> None:
    # The person-facing write refuses ``#`` in a name, and every connection
    # name holds one, so an item someone saved can never be read as one.
    from kiro_crew.secrets import items as vault_items

    assert vault_items.REFERENCE_FIELD_SEPARATOR in core.CONNECTION_PREFIX
    assert not core.is_connection_item("kirocrew.backend.1password")


def test_a_shared_title_is_one_ambiguous_row_and_a_padded_title_is_not_listed(
    fake: FakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = SecretVault(tmp_path)
    conn = fake.connect({"token": FAKE_TOKEN})
    vault._set_item_sync(core.connection_item(fake.id), "note", core.connection_fields(conn))
    rows = [
        {"name": "Github", "type": "secret", "fields": {"username": "a"}},
        {"name": "Github", "type": "secret", "fields": {"username": "b"}},
        {"name": " API key", "type": "secret", "fields": {}},
        {"name": "Solo", "type": "secret", "fields": {}},
    ]
    monkeypatch.setattr(fake, "list_items", lambda c: rows)
    view = next(v for v in core.backends_view(vault, []) if v["id"] == fake.id)
    assert [(r["name"], r["ambiguous"], r["fields"]) for r in view["items"]] == [
        ("Github", True, {}),
        ("Solo", False, {}),
    ]


def test_form_values_drop_unknown_keys_and_refuse_control_characters(fake: FakeBackend) -> None:
    assert core.form_values(fake, {"token": "t", "extra": "x"}) == {"token": "t"}
    for bad in ("a\x00b", 7, "x" * (core.MAX_FORM_VALUE_CHARS + 1)):
        with pytest.raises(core.BackendError) as excinfo:
            core.form_values(fake, {"token": bad})
        assert excinfo.value.code == "invalid_input"


# ── routes ──


def _app_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, on: bool = True):
    from test_secret_items import _app

    import kiro_crew.dashboard.handlers.secrets as handlers

    monkeypatch.setattr(handlers, "_secrets_vault_preview_on", lambda: on)
    monkeypatch.setattr(core, "preview_on", lambda: on)
    monkeypatch.setattr(handlers, "config_dir", lambda: str(tmp_path))
    return _app()


@pytest.mark.asyncio
async def test_connect_list_disconnect_through_the_generic_routes(
    fake: FakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.store["TEAM_KEY"] = "team-value"
    async with TestClient(TestServer(_app_with(monkeypatch, tmp_path))) as client:
        resp = await client.post("/api/secrets/backends/fakepm", json={"token": "wrong"})
        assert resp.status == 409 and (await resp.json())["code"] == "unauthorized"

        resp = await client.post("/api/secrets/backends/fakepm", json={"token": FAKE_TOKEN})
        assert resp.status == 200 and await resp.json() == {"ok": True, "account": "Team"}

        listed = await (await client.get("/api/secrets")).json()
        assert all(not core.is_connection_item(n) for n in listed["names"])
        view = next(v for v in listed["backends"] if v["id"] == "fakepm")
        assert view["status"] == "connected" and view["account"] == "Team"
        assert [r["name"] for r in view["items"]] == ["TEAM_KEY"]
        assert FAKE_TOKEN not in json.dumps(listed) and "team-value" not in json.dumps(listed)

        resp = await client.post(
            "/api/secrets", json={"name": core.connection_item("fakepm"), "value": "x"}
        )
        assert resp.status == 400 and (await resp.json())["code"] == "invalid_name"
        resp = await client.delete("/api/secrets/" + quote(core.connection_item("fakepm"), safe=""))
        assert resp.status == 409 and (await resp.json())["code"] == "managed_by_backend"

        assert (await client.delete("/api/secrets/backends/fakepm")).status == 200
        listed = await (await client.get("/api/secrets")).json()
        view = next(v for v in listed["backends"] if v["id"] == "fakepm")
        assert view["connected"] is False and "items" not in view

        assert (await client.post("/api/secrets/backends/nope", json={})).status == 404


@pytest.mark.asyncio
async def test_a_failing_backend_keeps_the_local_listing(
    fake: FakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = SecretVault(tmp_path)
    vault.set_sync("LOCAL_ONE", "v")
    conn = fake.connect({"token": FAKE_TOKEN})
    vault._set_item_sync(core.connection_item(fake.id), "note", core.connection_fields(conn))
    fake.fail = RuntimeError("boom")
    async with TestClient(TestServer(_app_with(monkeypatch, tmp_path))) as client:
        resp = await client.get("/api/secrets")
        listed = await resp.json()
    assert resp.status == 200 and listed["names"] == ["LOCAL_ONE"]
    view = next(v for v in listed["backends"] if v["id"] == "fakepm")
    assert view["status"] == "unavailable" and view["error"]["code"] == "unreachable"


@pytest.mark.asyncio
async def test_preview_off_equals_main_on_the_wire(
    fake: FakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = SecretVault(tmp_path)
    vault.set_sync("PLAIN", "v")
    conn = fake.connect({"token": FAKE_TOKEN})
    vault._set_item_sync(core.connection_item(fake.id), "note", core.connection_fields(conn))
    async with TestClient(TestServer(_app_with(monkeypatch, tmp_path, on=False))) as client:
        body = await (await client.get("/api/secrets")).json()
        refused = await client.post("/api/secrets/backends/fakepm", json={"token": FAKE_TOKEN})
        gone = await client.delete("/api/secrets/backends/fakepm")
    assert set(body) == {"names", "managed"} and body["names"] == ["PLAIN"]
    assert refused.status == 404 and gone.status == 404


def test_a_connection_is_never_a_grantable_cron_secret() -> None:
    import inspect

    from kiro_crew.dashboard.handlers import cron_secret_grants

    assert "is_connection_item" in inspect.getsource(cron_secret_grants)
    assert core.is_connection_item(core.connection_item("anything"))


# ── the two readers of the one switch ──


@pytest.mark.parametrize(
    ("previews", "expected"),
    [
        ({"secrets_vault": True}, True),
        ({"secrets_vault": False}, False),
        ({}, False),
        (RuntimeError("config unreadable"), False),
    ],
    ids=["on", "off", "absent", "unreadable"],
)
def test_the_resolver_and_the_dashboard_read_the_preview_the_same_way(
    monkeypatch: pytest.MonkeyPatch, previews: object, expected: bool
) -> None:
    # ``core.preview_on`` gates the MCP start path, which may not import the
    # dashboard; ``_secrets_vault_preview_on`` gates the dashboard routes and is
    # the same function. A split would let MCP start ask a provider while the
    # panel says off.
    import types

    import kiro_crew.dashboard.handlers.secrets as handlers
    from kiro_crew.config.loader import KiroCrewConfig

    assert handlers._secrets_vault_preview_on is core.preview_on
    assert core.FLAG_KEY == handlers.SECRETS_VAULT_PREVIEW

    def load(*_a: Any, **_k: Any) -> Any:
        if isinstance(previews, Exception):
            raise previews
        return types.SimpleNamespace(dashboard=types.SimpleNamespace(feature_previews=previews))

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(load))
    assert core.preview_on() is expected
    assert handlers._secrets_vault_preview_on() is expected
