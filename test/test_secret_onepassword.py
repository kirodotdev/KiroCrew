"""The read-only 1Password backend: the shared contract, vault choice, item mapping.

The SDK is replaced at its import boundary (``onepassword.client`` in
``sys.modules``) by a stand-in shaped like onepassword-sdk 0.4.2's objects and
raising its measured errors; no real account is reached.
"""

from __future__ import annotations

import json
import sys
import threading
import types
from pathlib import Path
from typing import Any, Optional

import pytest
from aiohttp.test_utils import TestClient, TestServer
from secret_backend_contract import Harness, SecretBackendContract

from kiro_crew.mcp_gateway.secret_uri import resolve_secret_uris
from kiro_crew.secrets import backend as core
from kiro_crew.secrets import onepassword
from kiro_crew.secrets.vault import SecretVault

TOKEN = "ops_test-token"

#: onepassword-sdk 0.4.2's own words for refused tokens, measured against the
#: real service with made-up tokens.
MALFORMED = (
    "invalid service account token, please make sure you provide a valid service account "
    "token as parameter:  service account token base64 decoding failed, please create "
    "another token"
)
UNKNOWN = (
    "invalid service account token, please make sure you provide a valid service account "
    "token as parameter:  service account deserialization failed, please create another token"
)


class RateLimitExceededException(Exception):
    """Named like ``onepassword.errors.RateLimitExceededException`` in 0.4.2."""


def _ns(**kw: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(**kw)


def _field(fid: str, value: str) -> types.SimpleNamespace:
    return _ns(id=fid, value=value)


def _password(item_id: str, title: str, value: str) -> types.SimpleNamespace:
    return _ns(
        id=item_id,
        title=title,
        category=_ns(value="Password"),
        fields=[_field("password", value)],
        notes="",
        websites=[],
    )


_ITEMS: dict[str, types.SimpleNamespace] = {
    "i1": _ns(
        id="i1",
        title="AWS_CONSOLE",
        category=_ns(value="Login"),
        fields=[_field("username", "zezhen@example.com"), _field("password", "op-pw")],
        notes="",
        websites=[_ns(url="https://console.aws.amazon.com")],
    ),
    "i2": _ns(
        id="i2",
        title="STRIPE_LIVE_KEY",
        category=_ns(value="ApiCredentials"),
        fields=[_field("credential", "sk_live_1")],
        notes="",
        websites=[],
    ),
    "i3": _password("i3", "SHARED", "from-1password"),
    "i4": _ns(
        id="i4", title="CARD", category=_ns(value="CreditCard"), fields=[], notes="", websites=[]
    ),
}


class _FakeSdk:
    """A stand-in for ``onepassword.client.Client`` and what it returns."""

    def __init__(self) -> None:
        self.vaults = [_ns(id="v1", title="Personal"), _ns(id="v2", title="Work")]
        self.items = dict(_ITEMS)
        self.raise_on_auth: Optional[Exception] = None
        self.raise_on_call: Optional[Exception] = None
        self.release: Optional[threading.Event] = None
        self.tokens: list[str] = []
        self.got: list[list[str]] = []

    def module(self) -> types.ModuleType:
        fake = self

        class Client:
            @classmethod
            async def authenticate(cls, auth: str, integration_name: str, integration_version: str):
                fake.tokens.append(auth)
                if fake.raise_on_auth is not None:
                    raise fake.raise_on_auth
                if auth != TOKEN:
                    raise Exception(UNKNOWN)
                return _ns(
                    vaults=_ns(list=fake.list_vaults),
                    items=_ns(list=fake.list_items, get_all=fake.get_all),
                )

        mod = types.ModuleType("onepassword.client")
        mod.Client = Client  # type: ignore[attr-defined]
        return mod

    async def _work(self) -> None:
        if self.release is not None:
            # Blocks this call's own loop, on the core's worker thread: a hung SDK.
            assert self.release.wait(5)
        if self.raise_on_call is not None:
            raise self.raise_on_call

    async def list_vaults(self):
        await self._work()
        return self.vaults

    async def list_items(self, vault_id: str):
        await self._work()
        return [
            _ns(id=i.id, title=i.title, category=i.category, websites=i.websites)
            for i in self.items.values()
        ]

    async def get_all(self, vault_id: str, ids: list[str]):
        self.got.append(list(ids))
        return _ns(individual_responses=[_ns(content=self.items[i], error=None) for i in ids])


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> _FakeSdk:
    fake = _FakeSdk()
    monkeypatch.setitem(sys.modules, "onepassword", types.ModuleType("onepassword"))
    monkeypatch.setitem(sys.modules, "onepassword.client", fake.module())
    _preview(monkeypatch, True)
    return fake


def _preview(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    """Set the ``secrets_vault`` preview for the resolver and the dashboard routes."""
    import kiro_crew.dashboard.handlers.secrets as handlers

    monkeypatch.setattr(core, "preview_on", lambda: on)
    monkeypatch.setattr(handlers, "_secrets_vault_preview_on", lambda: on)


BACKEND = onepassword.OnePasswordBackend()


def _connect(tmp_path: Path, vault_name: str = "Personal") -> SecretVault:
    vault = SecretVault(tmp_path)
    conn = BACKEND.connect({"token": TOKEN, "vault": vault_name})
    vault._set_item_sync(core.connection_item(BACKEND.id), "note", core.connection_fields(conn))
    return vault


# ── the shared contract, on the stubbed SDK ──


class OnePasswordHarness(Harness):
    def __init__(self, sdk: _FakeSdk, monkeypatch: pytest.MonkeyPatch) -> None:
        self.backend = BACKEND
        self.sdk = sdk
        self.monkeypatch = monkeypatch
        self.next_id = 0

    def good_values(self) -> dict[str, str]:
        return {"token": TOKEN, "vault": "Personal"}

    def bad_values(self) -> dict[str, str]:
        return {"token": "ops_not-known", "vault": "Personal"}

    def add_secret(self, name: str, value: str) -> None:
        self.next_id += 1
        item_id = f"c{self.next_id}"
        self.sdk.items[item_id] = _password(item_id, name, value)

    def fail_with(self, exc: Exception) -> None:
        self.sdk.raise_on_call = exc

    def hang_until(self, release: threading.Event) -> None:
        self.sdk.release = release

    def make_unavailable(self) -> None:
        self.monkeypatch.setitem(sys.modules, "onepassword.client", None)


class TestOnePasswordContract(SecretBackendContract):
    @pytest.fixture
    def harness(self, sdk: _FakeSdk, monkeypatch: pytest.MonkeyPatch) -> Harness:
        return OnePasswordHarness(sdk, monkeypatch)


# ── vault choice ──


@pytest.mark.parametrize(
    ("vaults", "wanted", "code"),
    [
        ([{"id": "v1", "name": "Personal"}, {"id": "v2", "name": "Work"}], "", "choose_vault"),
        ([{"id": "v1", "name": "Personal"}], "Nope", "vault_not_found"),
        ([{"id": "a", "name": "Dup"}, {"id": "b", "name": "Dup"}], "Dup", "ambiguous_vault"),
        ([], "", "no_vaults"),
    ],
)
def test_vault_choice_fails_closed_and_names_the_vaults(
    vaults: list, wanted: str, code: str
) -> None:
    with pytest.raises(core.BackendError) as excinfo:
        onepassword.pick_vault(vaults, wanted)
    assert excinfo.value.code == code
    assert excinfo.value.message_key == f"settings.secrets.backend_op_err_{code}"
    assert excinfo.value.params == {"vaults": ", ".join(sorted(v["name"] for v in vaults))}


def test_an_empty_vault_choice_takes_the_only_vault(sdk: _FakeSdk) -> None:
    sdk.vaults = [_ns(id="v1", title="Personal")]
    assert BACKEND.connect({"token": TOKEN, "vault": ""}).data["vault_id"] == "v1"
    sdk.vaults = [_ns(id="v1", title="Personal"), _ns(id="v2", title="Work")]
    assert BACKEND.connect({"token": TOKEN, "vault": "v2"}).account == "Work"


def test_connect_refuses_a_non_token_before_the_sdk_sees_it(sdk: _FakeSdk) -> None:
    for bad in ("sk-123", "ops_ with space"):
        with pytest.raises(core.BackendError) as excinfo:
            BACKEND.connect({"token": bad, "vault": "Personal"})
        assert excinfo.value.code == "invalid_token"
    assert sdk.tokens == []


@pytest.mark.parametrize("message", [MALFORMED, UNKNOWN])
def test_a_token_the_sdk_refuses_reads_as_unauthorized(sdk: _FakeSdk, message: str) -> None:
    sdk.raise_on_auth = Exception(message)
    with pytest.raises(core.BackendError) as excinfo:
        BACKEND.connect({"token": TOKEN, "vault": "Personal"})
    assert excinfo.value.code == "unauthorized"
    assert "invalid service account" not in str(excinfo.value)


def test_a_throttled_call_reads_as_rate_limited(sdk: _FakeSdk, tmp_path: Path) -> None:
    vault = _connect(tmp_path)
    sdk.raise_on_call = RateLimitExceededException("slow down")
    view = next(v for v in core.backends_view(vault, []) if v["id"] == BACKEND.id)
    assert view["status"] == "unavailable" and view["error"]["code"] == "rate_limited"


# ── listing ──


def test_listing_maps_categories_and_shows_no_value(sdk: _FakeSdk, tmp_path: Path) -> None:
    vault = _connect(tmp_path)
    view = next(v for v in core.backends_view(vault, []) if v["id"] == BACKEND.id)
    rows = view["items"]
    assert view["account"] == "Personal"
    assert [(r["name"], r["type"]) for r in rows] == [
        ("AWS_CONSOLE", "login"),
        ("SHARED", "secret"),
        ("STRIPE_LIVE_KEY", "secret"),
    ]
    assert rows[0]["fields"] == {
        "username": "zezhen@example.com",
        "urls": ["https://console.aws.amazon.com"],
    }
    assert "op-pw" not in json.dumps(view) and "sk_live_1" not in json.dumps(view)
    assert TOKEN not in json.dumps(view)
    assert sdk.got == [["i1"]]


# ── resolution ──


def test_a_reference_missing_locally_resolves_from_1password(
    sdk: _FakeSdk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _connect(tmp_path)
    vault.set_sync("SHARED", "from-local")
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "kiro_crew.sel.sel",
        lambda: type("S", (), {"log_api_access": lambda _s, **kw: rows.append(kw)})(),
    )
    env = {
        "PW": "secret://AWS_CONSOLE",
        "USER": "secret://AWS_CONSOLE#username",
        "KEY": "secret://STRIPE_LIVE_KEY",
        "SHARED": "secret://SHARED",
    }
    resolved, keys = resolve_secret_uris(env, tmp_path)
    assert resolved == {
        "PW": "op-pw",
        "USER": "zezhen@example.com",
        "KEY": "sk_live_1",
        "SHARED": "from-local",
    }
    assert keys == set(env)
    assert sorted(sdk.got[-1]) == ["i1", "i2"]
    assert rows == [
        {
            "caller": "gateway",
            "operation": "secrets_backend_resolve",
            "outcome": "ok",
            "source": "gateway",
            "resources": "1password:AWS_CONSOLE,AWS_CONSOLE#username,STRIPE_LIVE_KEY",
            "error": "",
        }
    ]


def test_an_unreachable_1password_fails_closed(sdk: _FakeSdk, tmp_path: Path) -> None:
    _connect(tmp_path)
    sdk.raise_on_call = Exception("error sending request for url")
    with pytest.raises(ValueError) as excinfo:
        resolve_secret_uris({"PW": "secret://AWS_CONSOLE"}, tmp_path)
    assert "(unreachable)" in str(excinfo.value)
    assert "AWS_CONSOLE" not in str(excinfo.value)


def test_a_name_two_items_share_fails_closed(sdk: _FakeSdk, tmp_path: Path) -> None:
    _connect(tmp_path)
    sdk.items["i5"] = _ns(**{**vars(_ITEMS["i3"]), "id": "i5"})
    with pytest.raises(ValueError, match=r"\(ambiguous\)"):
        resolve_secret_uris({"X": "secret://SHARED"}, tmp_path)


def test_a_unique_whole_title_resolves_beside_a_shared_base_title(
    sdk: _FakeSdk, tmp_path: Path
) -> None:
    _connect(tmp_path)
    sdk.items["h1"] = _password("h1", "API#1", "whole")
    sdk.items["h2"] = _password("h2", "API", "a")
    sdk.items["h3"] = _password("h3", "API", "b")
    resolved, _ = resolve_secret_uris({"K": "secret://API#1"}, tmp_path)
    assert resolved == {"K": "whole"}


def test_a_field_of_a_title_holding_a_hash_resolves(sdk: _FakeSdk, tmp_path: Path) -> None:
    _connect(tmp_path)
    sdk.items["w1"] = _ns(
        id="w1",
        title="AWS#work",
        category=_ns(value="Login"),
        fields=[_field("username", "worker@example.com"), _field("password", "pw")],
        notes="",
        websites=[],
    )
    resolved, _ = resolve_secret_uris({"U": "secret://AWS#work#username"}, tmp_path)
    assert resolved == {"U": "worker@example.com"}


def test_a_name_neither_holds_and_an_unsupported_category_fail_closed(
    sdk: _FakeSdk, tmp_path: Path
) -> None:
    _connect(tmp_path)
    for ref in ("secret://NOPE", "secret://CARD"):
        with pytest.raises(ValueError, match="does not exist"):
            resolve_secret_uris({"X": ref}, tmp_path)


def test_with_the_preview_off_the_sdk_is_never_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = SecretVault(tmp_path)
    conn = core.Connection(account="Personal", data={"token": TOKEN, "vault_id": "v1"})
    vault._set_item_sync(core.connection_item(BACKEND.id), "note", core.connection_fields(conn))
    _preview(monkeypatch, False)
    monkeypatch.delitem(sys.modules, "onepassword", raising=False)
    monkeypatch.delitem(sys.modules, "onepassword.client", raising=False)
    with pytest.raises(ValueError, match="does not exist"):
        resolve_secret_uris({"PW": "secret://AWS_CONSOLE"}, tmp_path)
    assert "onepassword" not in sys.modules and "onepassword.client" not in sys.modules


# ── where the SDK is absent ──


@pytest.mark.parametrize(("wheel", "code"), [(True, "sdk_missing"), (False, "sdk_unsupported")])
def test_without_the_sdk_the_backend_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wheel: bool, code: str
) -> None:
    monkeypatch.setitem(sys.modules, "onepassword.client", None)  # import raises ImportError
    monkeypatch.setattr(onepassword, "_wheel_platform", lambda: wheel)
    _preview(monkeypatch, True)
    view = next(v for v in core.backends_view(SecretVault(tmp_path), []) if v["id"] == BACKEND.id)
    assert view["status"] == "unavailable" and view["connected"] is False
    assert view["error"]["code"] == code
    with pytest.raises(core.BackendError) as excinfo:
        core.call(BACKEND, lambda: BACKEND.connect({"token": TOKEN, "vault": ""}))
    assert excinfo.value.code == code


@pytest.mark.parametrize(
    ("system", "machine", "libc", "py", "expected"),
    [
        ("Linux", "x86_64", ("glibc", "2.35"), (3, 12), True),
        ("Linux", "aarch64", ("glibc", "2.31"), (3, 12), False),
        ("Linux", "x86_64", ("", ""), (3, 12), False),  # musl
        ("Darwin", "arm64", ("", ""), (3, 13), True),
        ("Windows", "AMD64", ("", ""), (3, 12), True),
        ("Windows", "ARM64", ("", ""), (3, 12), False),
        ("Linux", "x86_64", ("glibc", "2.39"), (3, 14), False),
    ],
)
def test_wheel_platform_matches_the_published_wheels(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    libc: tuple,
    py: tuple,
    expected: bool,
) -> None:
    monkeypatch.setattr(onepassword.platform, "system", lambda: system)
    monkeypatch.setattr(onepassword.platform, "machine", lambda: machine)
    monkeypatch.setattr(onepassword.platform, "libc_ver", lambda: libc)
    monkeypatch.setattr(onepassword.sys, "version_info", py)
    assert onepassword._wheel_platform() is expected


# ── dashboard API ──


@pytest.mark.asyncio
async def test_connect_names_the_vaults_to_choose_from_over_the_api(
    sdk: _FakeSdk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_secret_items import _app

    import kiro_crew.dashboard.handlers.secrets as handlers

    monkeypatch.setattr(handlers, "config_dir", lambda: str(tmp_path))
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/secrets/backends/1password", json={"token": TOKEN})
        body = await resp.json()
    assert resp.status == 409 and body["code"] == "choose_vault"
    assert body["message_key"] == "settings.secrets.backend_op_err_choose_vault"
    assert body["params"] == {"vaults": "Personal, Work"}
    assert TOKEN not in json.dumps(body)


@pytest.mark.asyncio
async def test_a_missing_sdk_shows_on_the_connection_and_offers_the_install_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_secret_items import _app

    import kiro_crew.dashboard.handlers.secrets as handlers
    from kiro_crew import extras

    vault = SecretVault(tmp_path)
    conn = core.Connection(account="Personal", data={"token": TOKEN, "vault_id": "v1"})
    vault._set_item_sync(core.connection_item(BACKEND.id), "note", core.connection_fields(conn))
    monkeypatch.setitem(sys.modules, "onepassword.client", None)
    monkeypatch.setattr(onepassword, "_wheel_platform", lambda: True)
    _preview(monkeypatch, True)
    monkeypatch.setattr(extras, "pip_install_channel_available", lambda: True)
    monkeypatch.setattr(handlers, "config_dir", lambda: str(tmp_path))
    async with TestClient(TestServer(_app())) as client:
        listed = await (await client.get("/api/secrets")).json()
    view = next(v for v in listed["backends"] if v["id"] == BACKEND.id)
    assert view["connected"] is True and view["status"] == "unavailable"
    assert view["error"]["code"] == "sdk_missing"
    assert "onepassword-sdk==0.4.2" in view["error"]["install_command"]
    assert TOKEN not in json.dumps(listed)


# ── WakaTime's sign-in tokens entry (a LOCAL vault entry main's WakaTime flow owns) ──

_WAKA = "WAKATIME_OAUTH_TOKENS"


def test_wakatimes_own_token_read_never_reaches_1password(
    sdk: _FakeSdk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.wakatime import oauth

    _connect(tmp_path)
    sdk.items["i9"] = _password("i9", _WAKA, "from-1password")
    monkeypatch.setattr(oauth, "config_dir", lambda: tmp_path)
    calls = len(sdk.tokens)
    assert oauth.read_tokens() is None
    assert len(sdk.tokens) == calls


@pytest.mark.asyncio
async def test_the_wakatime_pane_is_offered_only_for_the_local_entry(
    sdk: _FakeSdk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_secret_items import _app

    import kiro_crew.dashboard.handlers.secrets as handlers

    # The SDK call runs its own event loop, which cannot start inside this
    # test's, so the connection is stored directly.
    vault = SecretVault(tmp_path)
    conn = core.Connection(account="Personal", data={"token": TOKEN, "vault_id": "v1"})
    vault._set_item_sync(core.connection_item(BACKEND.id), "note", core.connection_fields(conn))
    sdk.items["i9"] = _password("i9", _WAKA, "from-1password")
    monkeypatch.setattr(handlers, "config_dir", lambda: str(tmp_path))
    async with TestClient(TestServer(_app())) as client:
        listed = await (await client.get("/api/secrets")).json()
    assert all(entry["name"] != _WAKA for entry in listed["managed"])
    view = next(v for v in listed["backends"] if v["id"] == BACKEND.id)
    row = next(r for r in view["items"] if r["name"] == _WAKA)
    assert row["shadowed"] is True


@pytest.mark.parametrize("slug", ["github", "1password", "backend.1password", "my-app"])
def test_crew_oauth_entry_names_can_never_be_a_backend_connection(slug: str) -> None:
    # ``set_many_sync`` (owner-added MCP servers on Crew-owned OAuth) writes only
    # these two names, and a write through it bypasses the generic route's
    # guard, so the names themselves must never reach a connection entry.
    from kiro_crew.connections.crew_oauth import token_entry_name
    from kiro_crew.connections.crew_oauth_dcr import client_entry_name

    for name in (client_entry_name(slug), token_entry_name(slug)):
        assert not core.is_connection_item(name)
        assert core.local_only(name)
