"""The read-only 1Password backend: the shared contract, the two ways in
(desktop-app sign-in and a service account token), item mapping.

The SDK is replaced at its import boundary (``onepassword.client`` and
``onepassword.desktop_core`` in ``sys.modules``) by a stand-in shaped like
onepassword-sdk 0.4.2's objects and raising its measured errors; no real
account or app is reached.
"""

from __future__ import annotations

import json
import sys
import threading
import types
from pathlib import Path
from typing import Any, Iterator, Optional

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


class DesktopSessionExpiredException(Exception):
    """Named like ``onepassword.errors.DesktopSessionExpiredException`` in 0.4.2."""


#: onepassword-sdk 0.4.2's own words when the app is not installed, measured on
#: a host without it (``desktop_core.find_1password_lib_path``).
APP_MISSING = "1Password desktop application not found"

ACCOUNT = "Smith Family"


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
        #: Which vault each item is in; unlisted items are in ``v1``.
        self.where: dict[str, str] = {}
        self.raise_on_auth: Optional[Exception] = None
        self.raise_on_call: Optional[Exception] = None
        #: The fake clock, also ``onepassword._now``; the app's approval lapses
        #: after ``APPROVAL_IDLE_SECS`` on it with no call.
        self.t = 1000.0
        #: The account the app approved, and when it was last used.
        self.account: Optional[str] = None
        self.seen = 0.0
        self.release: Optional[threading.Event] = None
        self.tokens: list[str] = []
        self.app_present = True
        #: Every desktop-app approval asked for, by account name.
        self.prompts: list[str] = []
        self.got: list[list[str]] = []

    def _client(self) -> types.SimpleNamespace:
        return _ns(
            vaults=_ns(list=self.list_vaults),
            items=_ns(list=self.list_items, get_all=self.get_all),
        )

    def module(self) -> types.ModuleType:
        fake = self

        class DesktopAuth:
            def __init__(self, account_name: str) -> None:
                self.account_name = account_name

        class Client:
            @classmethod
            async def authenticate(cls, auth: Any, integration_name: str, integration_version: str):
                if isinstance(auth, DesktopAuth):
                    if not fake.app_present:
                        raise FileNotFoundError(APP_MISSING)
                    fake.prompts.append(auth.account_name)
                    fake.account, fake.seen = auth.account_name, fake.t
                    if fake.raise_on_auth is not None:
                        raise fake.raise_on_auth
                    return fake._client()
                fake.tokens.append(auth)
                if fake.raise_on_auth is not None:
                    raise fake.raise_on_auth
                if auth != TOKEN:
                    raise Exception(UNKNOWN)
                return fake._client()

        mod = types.ModuleType("onepassword.client")
        mod.Client = Client  # type: ignore[attr-defined]
        mod.DesktopAuth = DesktopAuth  # type: ignore[attr-defined]
        return mod

    def desktop_core(self) -> types.ModuleType:
        fake = self

        def find_1password_lib_path() -> str:
            if not fake.app_present:
                raise FileNotFoundError(APP_MISSING)
            return "/opt/1Password/libop_sdk_ipc_client.so"

        mod = types.ModuleType("onepassword.desktop_core")
        mod.find_1password_lib_path = find_1password_lib_path  # type: ignore[attr-defined]
        return mod

    async def _work(self) -> None:
        if self.release is not None:
            # Blocks this call's own loop, on the core's worker thread: a hung SDK.
            assert self.release.wait(5)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        if self.account is not None and self.t - self.seen >= onepassword.APPROVAL_IDLE_SECS:
            # onepassword-sdk 0.4.2 InnerClient.invoke: an expired desktop session
            # is re-initialised in place (core.init_client), which shows the
            # app's Approve prompt; the call then goes on.
            self.prompts.append(self.account)
        self.seen = self.t

    async def list_vaults(self):
        await self._work()
        return self.vaults

    async def list_items(self, vault_id: str):
        await self._work()
        return [
            _ns(id=i.id, title=i.title, category=i.category, websites=i.websites)
            for i in self.items.values()
            if self.where.get(i.id, "v1") == vault_id
        ]

    async def get_all(self, vault_id: str, ids: list[str]):
        self.got.append(list(ids))
        return _ns(individual_responses=[_ns(content=self.items[i], error=None) for i in ids])


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeSdk]:
    fake = _FakeSdk()
    monkeypatch.setitem(sys.modules, "onepassword", types.ModuleType("onepassword"))
    monkeypatch.setitem(sys.modules, "onepassword.client", fake.module())
    monkeypatch.setitem(sys.modules, "onepassword.desktop_core", fake.desktop_core())
    monkeypatch.setattr(onepassword, "_now", lambda: fake.t)
    onepassword.forget_approvals()
    yield fake
    onepassword.forget_approvals()


BACKEND = onepassword.OnePasswordBackend()


def _connect(tmp_path: Path, how: str = "token") -> SecretVault:
    vault = SecretVault(tmp_path)
    conn = BACKEND.connect({"token": TOKEN} if how == "token" else {"account": ACCOUNT})
    vault._set_item_sync(core.connection_item(BACKEND.id), "note", core.connection_fields(conn))
    return vault


def _view(vault: SecretVault) -> dict[str, Any]:
    return next(v for v in core.backends_view(vault, []) if v["id"] == BACKEND.id)


# ── the shared contract, on the stubbed SDK ──


class OnePasswordHarness(Harness):
    def __init__(self, sdk: _FakeSdk, monkeypatch: pytest.MonkeyPatch) -> None:
        self.backend = BACKEND
        self.sdk = sdk
        self.monkeypatch = monkeypatch
        self.next_id = 0

    def good_values(self) -> dict[str, str]:
        return {"form": "fallback", "token": TOKEN}

    def bad_values(self) -> dict[str, str]:
        return {"form": "fallback", "token": "ops_not-known"}

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


# ── the two ways in ──


def test_the_main_form_asks_only_for_the_account_name() -> None:
    assert [(f.name, f.secret) for f in BACKEND.connect_form] == [("account", False)]
    assert [(f.name, f.secret) for f in BACKEND.fallback_form] == [("token", True)]


def test_app_sign_in_asks_the_app_and_stores_no_token(sdk: _FakeSdk) -> None:
    conn = BACKEND.connect({"account": ACCOUNT})
    assert conn == core.Connection(account=ACCOUNT, data={"account": ACCOUNT}, form="main")
    assert sdk.prompts == [ACCOUNT] and sdk.tokens == []


def test_a_token_connection_reads_every_vault_it_can_see(sdk: _FakeSdk, tmp_path: Path) -> None:
    sdk.items["w9"] = _password("w9", "WORK_KEY", "from-work")
    sdk.where["w9"] = "v2"
    vault = _connect(tmp_path)
    view = _view(vault)
    assert view["account"] == "Personal, Work" and view["connected_form"] == "fallback"
    assert "WORK_KEY" in [r["name"] for r in view["items"]]
    resolved, _ = resolve_secret_uris({"K": "secret://WORK_KEY"}, tmp_path)
    assert resolved == {"K": "from-work"}


def test_a_name_in_two_vaults_fails_closed(sdk: _FakeSdk, tmp_path: Path) -> None:
    _connect(tmp_path)
    sdk.items["w3"] = _password("w3", "SHARED", "other")
    sdk.where["w3"] = "v2"
    with pytest.raises(ValueError, match=r"\(ambiguous\)"):
        resolve_secret_uris({"X": "secret://SHARED"}, tmp_path)


def test_without_the_app_the_token_form_comes_first(sdk: _FakeSdk, tmp_path: Path) -> None:
    sdk.app_present = False
    view = _view(SecretVault(tmp_path))
    assert view["fallback_first"] is True
    assert [f["name"] for f in view["fallback_form"]] == ["token"]
    with pytest.raises(core.BackendError) as excinfo:
        BACKEND.connect({"account": ACCOUNT})
    assert excinfo.value.code == "app_missing"
    sdk.app_present = True
    assert _view(SecretVault(tmp_path))["fallback_first"] is False


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (Exception("the user declined"), "not_approved"),
        (RuntimeError("desktop app connection channel is closed"), "app_closed"),
        (DesktopSessionExpiredException("expired"), "approval_expired"),
    ],
)
def test_an_app_sign_in_that_fails_says_why(sdk: _FakeSdk, exc: Exception, code: str) -> None:
    sdk.raise_on_auth = exc
    with pytest.raises(core.BackendError) as excinfo:
        BACKEND.connect({"account": ACCOUNT})
    assert excinfo.value.code == code
    assert str(exc) not in str(excinfo.value)


def test_a_listing_never_prompts_and_a_lapsed_approval_asks_to_approve_again(
    sdk: _FakeSdk, tmp_path: Path
) -> None:
    vault = _connect(tmp_path, "app")
    assert _view(vault)["status"] == "connected"
    sdk.t += onepassword.APPROVAL_IDLE_SECS - 1
    assert _view(vault)["status"] == "connected"
    # Each use restarts the app's idle window.
    sdk.t += onepassword.APPROVAL_IDLE_SECS - 1
    assert _view(vault)["status"] == "connected"
    assert sdk.prompts == [ACCOUNT]
    sdk.t += onepassword.APPROVAL_IDLE_SECS
    view = _view(vault)
    assert view["status"] == "needs_approval" and view["error"]["code"] == "approval_expired"
    # The SDK was not called: had it been, its re-init would have shown a prompt.
    assert _view(vault)["status"] == "needs_approval"
    assert sdk.prompts == [ACCOUNT]


def test_a_reference_asks_the_app_once_then_reuses_the_approval(
    sdk: _FakeSdk, tmp_path: Path
) -> None:
    _connect(tmp_path, "app")
    onepassword.forget_approvals()
    for _ in range(2):
        resolved, _ = resolve_secret_uris({"K": "secret://STRIPE_LIVE_KEY"}, tmp_path)
        assert resolved == {"K": "sk_live_1"}
    assert sdk.prompts == [ACCOUNT, ACCOUNT]


def test_a_reference_after_the_approval_lapses_asks_the_app_again(
    sdk: _FakeSdk, tmp_path: Path
) -> None:
    _connect(tmp_path, "app")
    sdk.t += onepassword.APPROVAL_IDLE_SECS
    resolved, _ = resolve_secret_uris({"K": "secret://STRIPE_LIVE_KEY"}, tmp_path)
    assert resolved == {"K": "sk_live_1"}
    assert sdk.prompts == [ACCOUNT, ACCOUNT]


def test_a_reference_through_the_app_waits_as_long_as_a_connect(
    sdk: _FakeSdk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Resolving may raise the app's Approve prompt; 20 s would fail a person who
    # approves at second 30.
    _connect(tmp_path, "app")
    waits: list[Optional[float]] = []
    real = core.call

    def spy(backend: core.SecretBackend, work: Any, timeout: Optional[float] = None) -> Any:
        waits.append(timeout)
        return real(backend, work, timeout)

    monkeypatch.setattr(core, "call", spy)
    resolve_secret_uris({"K": "secret://STRIPE_LIVE_KEY"}, tmp_path)
    assert waits == [core.CONNECT_TIMEOUT_SECS]


def test_only_an_app_connection_can_be_approved_again(sdk: _FakeSdk) -> None:
    app = BACKEND.connect({"account": ACCOUNT})
    assert core.approve_values(BACKEND, app) == {"account": ACCOUNT}
    token = BACKEND.connect({"token": TOKEN})
    with pytest.raises(core.BackendError):
        core.approve_values(BACKEND, token)


def test_a_body_names_its_form(sdk: _FakeSdk) -> None:
    assert core.form_values(BACKEND, {"account": ACCOUNT, "token": TOKEN}) == {"account": ACCOUNT}
    assert core.form_values(BACKEND, {"form": "fallback", "token": TOKEN}) == {"token": TOKEN}
    with pytest.raises(core.BackendError):
        core.form_values(BACKEND, {"form": "other"})


def test_connect_refuses_a_non_token_before_the_sdk_sees_it(sdk: _FakeSdk) -> None:
    for bad in ("sk-123", "ops_ with space"):
        with pytest.raises(core.BackendError) as excinfo:
            BACKEND.connect({"token": bad})
        assert excinfo.value.code == "invalid_token"
    assert sdk.tokens == []


@pytest.mark.parametrize("message", [MALFORMED, UNKNOWN])
def test_a_token_the_sdk_refuses_reads_as_unauthorized(sdk: _FakeSdk, message: str) -> None:
    sdk.raise_on_auth = Exception(message)
    with pytest.raises(core.BackendError) as excinfo:
        BACKEND.connect({"token": TOKEN})
    assert excinfo.value.code == "unauthorized"
    assert "invalid service account" not in str(excinfo.value)


def test_a_token_that_reads_no_vault_is_refused(sdk: _FakeSdk) -> None:
    sdk.vaults = []
    with pytest.raises(core.BackendError) as excinfo:
        BACKEND.connect({"token": TOKEN})
    assert excinfo.value.code == "no_vaults"
    assert excinfo.value.message_key == "settings.secrets.backend_op_err_no_vaults"


def test_a_throttled_call_reads_as_rate_limited(sdk: _FakeSdk, tmp_path: Path) -> None:
    vault = _connect(tmp_path)
    sdk.raise_on_call = RateLimitExceededException("slow down")
    view = _view(vault)
    assert view["status"] == "unavailable" and view["error"]["code"] == "rate_limited"


# ── listing ──


def test_listing_maps_categories_and_shows_no_value(sdk: _FakeSdk, tmp_path: Path) -> None:
    vault = _connect(tmp_path)
    view = next(v for v in core.backends_view(vault, []) if v["id"] == BACKEND.id)
    rows = view["items"]
    assert view["account"] == "Personal, Work"
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


def test_with_no_connection_a_reference_never_imports_the_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    view = next(v for v in core.backends_view(SecretVault(tmp_path), []) if v["id"] == BACKEND.id)
    assert view["status"] == "unavailable" and view["connected"] is False
    assert view["error"]["code"] == code
    with pytest.raises(core.BackendError) as excinfo:
        core.call(BACKEND, lambda: BACKEND.connect({"token": TOKEN}))
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
async def test_connect_and_approve_again_over_the_api_store_no_token(
    sdk: _FakeSdk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_secret_items import _app

    import kiro_crew.dashboard.handlers.secrets as handlers

    monkeypatch.setattr(handlers, "config_dir", lambda: str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.handlers.secret_backends.data_home", lambda: tmp_path)
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/secrets/backends/1password", json={"account": ACCOUNT})
        assert resp.status == 200 and (await resp.json())["account"] == ACCOUNT
        onepassword.forget_approvals()
        listed = await (await client.get("/api/secrets")).json()
        view = next(v for v in listed["backends"] if v["id"] == BACKEND.id)
        assert view["status"] == "needs_approval" and view["connected_form"] == "main"
        again = await client.post("/api/secrets/backends/1password/approve")
        assert again.status == 200
        listed = await (await client.get("/api/secrets")).json()
    view = next(v for v in listed["backends"] if v["id"] == BACKEND.id)
    assert view["status"] == "connected"
    assert sdk.prompts == [ACCOUNT, ACCOUNT] and sdk.tokens == []
    conn = core.read_connection(SecretVault(tmp_path), BACKEND)
    assert conn is not None and conn.data == {"account": ACCOUNT}


@pytest.mark.asyncio
async def test_without_the_app_connect_says_app_missing_over_the_api(
    sdk: _FakeSdk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_secret_items import _app

    import kiro_crew.dashboard.handlers.secrets as handlers

    sdk.app_present = False
    monkeypatch.setattr(handlers, "config_dir", lambda: str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.handlers.secret_backends.data_home", lambda: tmp_path)
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/secrets/backends/1password", json={"account": ACCOUNT})
        body = await resp.json()
        token = await client.post(
            "/api/secrets/backends/1password", json={"form": "fallback", "token": TOKEN}
        )
        approve = await client.post("/api/secrets/backends/1password/approve")
    assert resp.status == 409 and body["code"] == "app_missing"
    assert APP_MISSING not in json.dumps(body)
    assert token.status == 200
    # A token connection has nothing to approve again.
    assert approve.status == 400


@pytest.mark.asyncio
async def test_a_token_connect_never_echoes_the_token_over_the_api(
    sdk: _FakeSdk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_secret_items import _app

    import kiro_crew.dashboard.handlers.secrets as handlers

    monkeypatch.setattr(handlers, "config_dir", lambda: str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.handlers.secret_backends.data_home", lambda: tmp_path)
    async with TestClient(TestServer(_app())) as client:
        bad = await client.post(
            "/api/secrets/backends/1password", json={"form": "fallback", "token": "ops_nope"}
        )
        body = await bad.json()
        good = await client.post(
            "/api/secrets/backends/1password", json={"form": "fallback", "token": TOKEN}
        )
        good_body = await good.json()
    assert bad.status == 409 and body["code"] == "unauthorized"
    assert good.status == 200 and good_body["account"] == "Personal, Work"
    assert TOKEN not in json.dumps(good_body) and "ops_nope" not in json.dumps(body)


@pytest.mark.asyncio
async def test_a_missing_sdk_shows_on_the_connection_and_offers_the_install_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_secret_items import _app

    import kiro_crew.dashboard.handlers.secrets as handlers
    from kiro_crew import extras

    vault = SecretVault(tmp_path)
    conn = core.Connection(account="Personal", data={"token": TOKEN})
    vault._set_item_sync(core.connection_item(BACKEND.id), "note", core.connection_fields(conn))
    monkeypatch.setitem(sys.modules, "onepassword.client", None)
    monkeypatch.setattr(onepassword, "_wheel_platform", lambda: True)
    monkeypatch.setattr(extras, "pip_install_channel_available", lambda: True)
    monkeypatch.setattr(handlers, "config_dir", lambda: str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.handlers.secret_backends.data_home", lambda: tmp_path)
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
    conn = core.Connection(account="Personal", data={"token": TOKEN})
    vault._set_item_sync(core.connection_item(BACKEND.id), "note", core.connection_fields(conn))
    sdk.items["i9"] = _password("i9", _WAKA, "from-1password")
    monkeypatch.setattr(handlers, "config_dir", lambda: str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.handlers.secret_backends.data_home", lambda: tmp_path)
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
