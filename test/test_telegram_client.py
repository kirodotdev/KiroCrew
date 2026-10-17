"""Telegram Bot API client: transport configuration, delivery, and the bot token.

Covers ``TelegramClient`` (client.py): the one shared HTTP session, the configurable API
base and the file base derived from it, the loopback proxy bypass, refused redirects on
the token path, the plain-text retry after rejected markup, link-preview suppression,
the rich-message availability latch, non-JSON error bodies, the health callbacks, the
setMyCommands publish and close. Beside it: the dashboard's token-verify URL, which reads
the client's API-base resolver, and the bot token scrubbed from output by
``security.redact_credentials`` and masked in the dashboard's config response.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
import pytest
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401

from kiro_crew.telegram.client import TelegramClient
from kiro_crew.telegram.commands import bot_command_payload
from kiro_crew.telegram.renderer import TelegramApprovalDecider, TelegramRenderer
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES


class TestClientSession:
    def test_ensure_session_creates_single_shared_instance(self, monkeypatch: Any) -> None:
        # Concurrent _api callers (polling loop + handler tasks) must share ONE
        # ClientSession — the double-checked lock in _ensure_session prevents a
        # leaked duplicate.
        import kiro_crew.telegram.client as client_mod

        created = {"n": 0}

        class _FakeSession:
            def __init__(self) -> None:
                created["n"] += 1
                self.closed = False

        monkeypatch.setattr(client_mod.aiohttp, "ClientSession", _FakeSession)
        cli = client_mod.TelegramClient(token="x")

        async def _go() -> None:
            await asyncio.gather(
                cli._ensure_session(), cli._ensure_session(), cli._ensure_session()
            )

        asyncio.run(_go())
        assert created["n"] == 1


class TestConfigurableApiBase:
    """TELEGRAM_API_BASE_URL overrides the Bot API host for proxy setups.

    Unset keeps today's public host; set routes method calls and file
    downloads through the configured origin. The value is read at call time
    and validated (https-only, except loopback) because it carries the token.
    """

    # ── Call-time read: the var takes effect without a module reload ──

    def test_api_base_read_at_call_time_after_env_change(self, monkeypatch: Any) -> None:
        import kiro_crew.telegram.client as client_mod

        # The module is already imported with the var unset. Setting it now
        # (as load_credentials does, AFTER import) must change the result,
        # proving the value is read on each call rather than frozen at import.
        monkeypatch.delenv("TELEGRAM_API_BASE_URL", raising=False)
        assert client_mod._api_base() == "https://api.telegram.org/bot{token}/{method}"

        monkeypatch.setenv(
            "TELEGRAM_API_BASE_URL",
            "https://tg-proxy.example.com/bot{token}/{method}",
        )
        assert client_mod._api_base() == "https://tg-proxy.example.com/bot{token}/{method}"

    def test_unset_keeps_public_host(self, monkeypatch: Any) -> None:
        import kiro_crew.telegram.client as client_mod

        monkeypatch.delenv("TELEGRAM_API_BASE_URL", raising=False)
        assert client_mod._api_base() == "https://api.telegram.org/bot{token}/{method}"
        assert client_mod._file_base() == "https://api.telegram.org"

    # ── Scheme validation: https accepted, http refused unless loopback ──

    def test_https_proxy_accepted(self, monkeypatch: Any) -> None:
        import kiro_crew.telegram.client as client_mod

        monkeypatch.setenv(
            "TELEGRAM_API_BASE_URL",
            "https://tg-proxy.example.com/bot{token}/{method}",
        )
        assert client_mod._api_base() == "https://tg-proxy.example.com/bot{token}/{method}"

    def test_http_non_loopback_refused_falls_back_to_public(self, monkeypatch: Any) -> None:
        import kiro_crew.telegram.client as client_mod

        # Plaintext http to a remote host would send the bot token in clear
        # text — refused, falls back to the public https host.
        monkeypatch.setenv(
            "TELEGRAM_API_BASE_URL",
            "http://tg-proxy.example.com/bot{token}/{method}",
        )
        assert client_mod._api_base() == "https://api.telegram.org/bot{token}/{method}"

    def test_http_loopback_accepted(self, monkeypatch: Any) -> None:
        import kiro_crew.telegram.client as client_mod

        # http is allowed only for loopback, where the token never leaves the
        # machine (a local dev proxy on 127.0.0.1 / ::1 / localhost).
        for host in (
            "http://127.0.0.1:8080/bot{token}/{method}",
            "http://localhost/bot{token}/{method}",
            "http://[::1]:9000/bot{token}/{method}",
        ):
            monkeypatch.setenv("TELEGRAM_API_BASE_URL", host)
            assert client_mod._api_base() == host

    def test_missing_placeholder_refused(self, monkeypatch: Any) -> None:
        import kiro_crew.telegram.client as client_mod

        # A value lacking {token}/{method} formats to the same URL for every
        # method — refused, falls back to the public host.
        monkeypatch.setenv("TELEGRAM_API_BASE_URL", "https://proxy.example.com")
        assert client_mod._api_base() == "https://api.telegram.org/bot{token}/{method}"

    def test_unparseable_falls_back_to_public(self, monkeypatch: Any) -> None:
        import kiro_crew.telegram.client as client_mod

        monkeypatch.setenv("TELEGRAM_API_BASE_URL", "not-a-url")
        assert client_mod._api_base() == "https://api.telegram.org/bot{token}/{method}"
        assert client_mod._file_base() == "https://api.telegram.org"

    # ── File base keeps the template's path prefix ──

    def test_file_base_defaults_to_public_host(self, monkeypatch: Any) -> None:
        import kiro_crew.telegram.client as client_mod

        monkeypatch.delenv("TELEGRAM_API_BASE_URL", raising=False)
        assert client_mod._file_base() == "https://api.telegram.org"

    def test_file_base_derives_proxy_origin(self, monkeypatch: Any) -> None:
        import kiro_crew.telegram.client as client_mod

        # A root-mounted proxy template yields that proxy's origin.
        monkeypatch.setenv(
            "TELEGRAM_API_BASE_URL",
            "https://tg-proxy.example.com/bot{token}/{method}",
        )
        assert client_mod._file_base() == "https://tg-proxy.example.com"

    def test_file_base_keeps_path_prefix(self, monkeypatch: Any) -> None:
        import kiro_crew.telegram.client as client_mod

        # A path-prefixed template must keep its prefix so downloads stay
        # inside the proxied route (…/tg/file/bot…), not fall out to …/file/bot.
        monkeypatch.setenv(
            "TELEGRAM_API_BASE_URL",
            "https://proxy.example.com/tg/bot{token}/{method}",
        )
        assert client_mod._file_base() == "https://proxy.example.com/tg"
        # The full download URL built in download_file keeps the prefix.
        url = f"{client_mod._file_base()}/file/bot<TOKEN>/photos/f.jpg"
        assert url == "https://proxy.example.com/tg/file/bot<TOKEN>/photos/f.jpg"


class TestFileBaseFollowsApiBase:
    """The download base is derived from the template ``_api_base`` accepted.

    ``_api_base`` validates the parsed URL, which lowercases the scheme, and
    accepts any template carrying both placeholders. A file route can be
    derived only from one that ends in ``bot{token}/{method}``; for any other
    accepted template there is no download base, and no request is made.
    """

    @pytest.mark.parametrize(
        ("template", "expected"),
        [
            ("HTTPS://proxy.example.com/bot{token}/{method}", "https://proxy.example.com"),
            ("Https://proxy.example.com/tg/bot{token}/{method}", "https://proxy.example.com/tg"),
            ("HTTP://127.0.0.1:8080/bot{token}/{method}", "http://127.0.0.1:8080"),
        ],
    )
    def test_the_scheme_is_read_the_way_api_base_accepted_it(
        self, monkeypatch: Any, template: str, expected: str
    ) -> None:
        import kiro_crew.telegram.client as client_mod

        monkeypatch.setenv("TELEGRAM_API_BASE_URL", template)
        assert client_mod._api_base() == template
        assert client_mod._file_base() == expected

    @pytest.mark.parametrize(
        "template",
        [
            "https://proxy.example.com/tg/{token}/{method}",
            "https://proxy.example.com/bot{token}/{method}/",
            "https://proxy.example.com/api?token={token}&method={method}",
        ],
    )
    def test_a_template_without_the_method_tail_has_no_download_base(
        self, monkeypatch: Any, template: str
    ) -> None:
        import kiro_crew.telegram.client as client_mod

        monkeypatch.setenv("TELEGRAM_API_BASE_URL", template)
        # Method calls keep using it exactly as before ...
        assert client_mod._api_base() == template
        # ... but no download route can be derived from it.
        assert client_mod._file_base() is None

    def test_a_query_after_the_method_tail_has_no_download_base(self, monkeypatch: Any) -> None:
        import kiro_crew.telegram.client as client_mod

        template = "https://proxy.example.com/bot{token}/{method}?key=x"
        monkeypatch.setenv("TELEGRAM_API_BASE_URL", template)
        assert client_mod._api_base() == template
        assert client_mod._file_base() is None

    def _client(self, monkeypatch: Any, template: str | None) -> tuple[Any, dict[str, list[Any]]]:
        """A real client whose ``getFile`` call and session are fakes.

        Returns the client and a record of what it requested: the ``api``
        method names and the ``get`` URLs.
        """
        import kiro_crew.telegram.client as client_mod

        seen: dict[str, list[Any]] = {"api": [], "get": []}
        if template is None:
            monkeypatch.delenv("TELEGRAM_API_BASE_URL", raising=False)
        else:
            monkeypatch.setenv("TELEGRAM_API_BASE_URL", template)

        class _Content:
            async def iter_chunked(self, size: int) -> Any:
                yield b"file-bytes"

        class _FakeResp:
            status = 200
            content = _Content()

            async def __aenter__(self) -> "_FakeResp":
                return self

            async def __aexit__(self, *a: Any) -> None:
                return None

        class _FakeSession:
            def get(self, url: str, **kwargs: Any) -> _FakeResp:
                seen["get"].append(url)
                return _FakeResp()

        cli = client_mod.TelegramClient(token="123:ABC")

        async def _fake_api(method: str, params: dict, *a: Any, **k: Any) -> Any:
            seen["api"].append(method)
            return {"file_path": "photos/f.jpg"}

        async def _fake_ensure() -> _FakeSession:
            return _FakeSession()

        monkeypatch.setattr(cli, "_api", _fake_api)
        monkeypatch.setattr(cli, "_ensure_session", _fake_ensure)
        return cli, seen

    def test_download_goes_to_the_proxy_the_api_base_names(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        cli, seen = self._client(monkeypatch, "HTTPS://proxy.example.com/tg/bot{token}/{method}")
        dest = tmp_path / "out.bin"
        asyncio.run(cli.download_file("file-id", str(dest)))
        assert seen["get"] == ["https://proxy.example.com/tg/file/bot123:ABC/photos/f.jpg"]
        assert dest.read_bytes() == b"file-bytes"

    def test_download_without_the_override_requests_the_public_host(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        # Control: the unset default is unchanged.
        cli, seen = self._client(monkeypatch, None)
        asyncio.run(cli.download_file("file-id", str(tmp_path / "out.bin")))
        assert seen["get"] == ["https://api.telegram.org/file/bot123:ABC/photos/f.jpg"]

    def test_download_refuses_a_template_it_cannot_derive_a_route_from(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        cli, _ = self._client(monkeypatch, "https://proxy.example.com/tg/{token}/{method}")
        dest = tmp_path / "out.bin"
        with pytest.raises(ValueError) as excinfo:
            asyncio.run(cli.download_file("file-id", str(dest)))
        # The reason names the variable and never the token or the template.
        assert "TELEGRAM_API_BASE_URL" in str(excinfo.value)
        assert "123:ABC" not in str(excinfo.value)
        assert "proxy.example.com" not in str(excinfo.value)
        assert not dest.exists()

    def test_a_refused_download_makes_no_request(self, monkeypatch: Any, tmp_path: Any) -> None:
        # Refused before getFile: nothing is spent on a download that cannot happen.
        cli, seen = self._client(monkeypatch, "https://proxy.example.com/tg/{token}/{method}")
        with pytest.raises(ValueError):
            asyncio.run(cli.download_file("file-id", str(tmp_path / "out.bin")))
        assert seen == {"api": [], "get": []}


class TestLoopbackProxyBypass:
    """A loopback destination must bypass a configured forward proxy.

    The bot token travels in the request URL; sending a loopback http:// call
    through a remote HTTPS_PROXY would leak the token off-box in plaintext,
    defeating the loopback-http allowance.
    """

    def test_url_is_loopback_detects_loopback_hosts(self) -> None:
        from kiro_crew.telegram.client import _url_is_loopback

        assert _url_is_loopback("http://127.0.0.1:8080/botT/getMe")
        assert _url_is_loopback("http://localhost/botT/getMe")
        assert _url_is_loopback("http://[::1]:9000/botT/getMe")
        assert not _url_is_loopback("https://proxy.example.com/botT/getMe")
        assert not _url_is_loopback("https://api.telegram.org/botT/getMe")

    def test_proxy_bypassed_for_loopback_destination(self) -> None:
        from kiro_crew.telegram.client import TelegramClient

        client = TelegramClient(token="T", proxy="http://corp-proxy.example.com:3128")
        # A loopback request must not go through the remote proxy.
        assert client._proxy_for("http://127.0.0.1:8081/botT/getMe") is None
        assert client._proxy_for("http://localhost/botT/getMe") is None

    def test_proxy_kept_for_remote_destination(self) -> None:
        from kiro_crew.telegram.client import TelegramClient

        proxy = "http://corp-proxy.example.com:3128"
        client = TelegramClient(token="T", proxy=proxy)
        # A non-loopback request still uses the configured proxy.
        assert client._proxy_for("https://api.telegram.org/botT/getMe") == proxy
        assert client._proxy_for("https://proxy.example.com/botT/getMe") == proxy


class TestTelegramSettingsSharedBase:
    """The dashboard token-verify URL uses the client's shared resolver."""

    def test_validate_url_uses_shared_resolver_at_call_time(self, monkeypatch: Any) -> None:
        import asyncio

        import kiro_crew.dashboard.messaging_api.telegram_settings as settings_mod

        captured: dict[str, str] = {}

        class _FakeResp:
            async def json(self, content_type: Any = None) -> dict[str, Any]:
                return {"ok": True}

            async def __aenter__(self) -> "_FakeResp":
                return self

            async def __aexit__(self, *a: Any) -> None:
                return None

        class _FakeSession:
            def __init__(self, *a: Any, **k: Any) -> None:
                pass

            def get(self, url: str, **kwargs: Any) -> _FakeResp:
                captured["url"] = url
                captured["allow_redirects"] = kwargs.get("allow_redirects")
                return _FakeResp()

            async def __aenter__(self) -> "_FakeSession":
                return self

            async def __aexit__(self, *a: Any) -> None:
                return None

        import aiohttp

        monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
        monkeypatch.setattr(settings_mod, "_TOKEN_VERIFY_TIMEOUT", 1, raising=False)
        # Set the proxy var AFTER import — the dashboard must still pick it up,
        # proving it reads the same call-time resolver the client uses.
        monkeypatch.setenv(
            "TELEGRAM_API_BASE_URL",
            "https://tg-proxy.example.com/bot{token}/{method}",
        )

        asyncio.run(settings_mod._validate_telegram_token("123:ABC"))
        assert captured["url"] == "https://tg-proxy.example.com/bot123:ABC/getMe"
        # The verify URL carries the token, so a proxy redirect must not be
        # followed off-box.
        assert captured["allow_redirects"] is False


class TestRedirectsRefusedOnTokenPath:
    """Token-bearing requests must not follow a proxy redirect (plaintext leak).

    A TLS-terminating proxy can answer a Bot API call with a 302 to a plain
    http:// Location; following it would send the token off-box in clear text.
    """

    def _drive(self, monkeypatch: Any, call: str) -> dict[str, Any]:
        import asyncio

        import kiro_crew.telegram.client as client_mod

        captured: dict[str, Any] = {}

        class _FakeResp:
            status = 200

            async def json(self, content_type: Any = None) -> dict[str, Any]:
                return {"ok": True, "result": {}}

            async def __aenter__(self) -> "_FakeResp":
                return self

            async def __aexit__(self, *a: Any) -> None:
                return None

        class _FakeSession:
            def post(self, url: str, **kwargs: Any) -> _FakeResp:
                captured["url"] = url
                captured["allow_redirects"] = kwargs.get("allow_redirects")
                return _FakeResp()

        cli = client_mod.TelegramClient(token="123:ABC")

        async def _fake_ensure() -> _FakeSession:
            return _FakeSession()

        monkeypatch.setattr(cli, "_ensure_session", _fake_ensure)

        async def _run() -> None:
            if call == "call_raw":
                await cli._call_raw("getMe", {})
            else:
                await cli._api_request("getMe", lambda: {"json": {}})

        asyncio.run(_run())
        return captured

    def test_call_raw_refuses_redirects(self, monkeypatch: Any) -> None:
        captured = self._drive(monkeypatch, "call_raw")
        assert captured["allow_redirects"] is False

    def test_api_request_refuses_redirects(self, monkeypatch: Any) -> None:
        captured = self._drive(monkeypatch, "api_request")
        assert captured["allow_redirects"] is False


class TestTelegramTokenRedaction:
    """#1 — a Telegram bot token echoed in output must be scrubbed."""

    def test_bot_token_is_redacted(self) -> None:
        from kiro_crew.security import redact_credentials

        token = "8412345678:" "AAExampleSecretTokenValue_1234567890abcd"
        cleaned, warnings = redact_credentials(f"my token is {token} ok")
        assert token not in cleaned
        assert "[REDACTED: credential]" in cleaned
        assert warnings  # at least one redaction warning recorded

    def test_benign_short_colon_pairs_not_redacted(self) -> None:
        from kiro_crew.security import redact_credentials

        # Too few digits (<6) and too-short suffix (<30) to match the token
        # shape — must not be over-redacted.
        text = "ratio 12:34, port 8080:abc, time 10:30:00"
        cleaned, _ = redact_credentials(text)
        assert cleaned == text


class TestConfigMasking:
    """#5 — sensitive config fields are masked in the API response only."""

    def test_bot_token_masked_in_response(self) -> None:
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK, _masked_config_dict

        class _Cfg:
            def to_dict(self) -> dict:
                return {
                    "telegram": {
                        "bot_token": "8412345678:AAsecretsecretsecret",
                        "enabled": True,
                        "allowed_user_ids": [7],
                    }
                }

        out = _masked_config_dict(_Cfg())  # type: ignore[arg-type]
        assert out["telegram"]["bot_token"] == _SENSITIVE_MASK  # secret hidden
        assert out["telegram"]["enabled"] is True  # non-sensitive untouched
        assert out["telegram"]["allowed_user_ids"] == [7]

    def test_empty_token_not_masked(self) -> None:
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK, _masked_config_dict

        class _Cfg:
            def to_dict(self) -> dict:
                return {"telegram": {"bot_token": "", "enabled": False}}

        out = _masked_config_dict(_Cfg())  # type: ignore[arg-type]
        # Unset stays empty (UI shows "not set"), never a fake mask sentinel.
        assert out["telegram"]["bot_token"] == ""
        assert _SENSITIVE_MASK not in str(out)


class _PlainRetryResponse:
    """A Bot API response: ``body`` as JSON, or ``error`` raised on entering it."""

    def __init__(self, body: dict[str, Any] | None = None, error: BaseException | None = None):
        self._body = body
        self._error = error

    async def json(self, content_type: Any = None) -> dict[str, Any] | None:
        return self._body

    async def __aenter__(self) -> "_PlainRetryResponse":
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None


class _PlainRetrySession:
    """Records every ``sendMessage`` the server receives; answers ``first``, then ok."""

    closed = False

    def __init__(self, first: _PlainRetryResponse) -> None:
        self.received: list[dict[str, Any]] = []
        self._first = first

    def post(self, url: str, **kwargs: Any) -> _PlainRetryResponse:
        if not url.endswith("/sendMessage"):
            return _PlainRetryResponse({"ok": True, "result": True})
        self.received.append(dict(kwargs.get("json") or {}))
        if len(self.received) == 1:
            return self._first
        return _PlainRetryResponse({"ok": True, "result": {"message_id": 100 + len(self.received)}})


def _plain_retry_client(first: _PlainRetryResponse) -> tuple[TelegramClient, _PlainRetrySession]:
    client = TelegramClient(token="12345:testtoken")
    session = _PlainRetrySession(first)
    client._session = session  # type: ignore[assignment]
    return client, session


_REJECTED_MARKUP = {"ok": False, "error_code": 400, "description": "Bad Request"}


class TestPlainRetryOnlyOnRejectedMarkup:
    """``send_message`` drops ``parse_mode`` on its retry only when Telegram rejected the send.

    After a 400 nothing was posted, so the retry goes out as plain text. After
    any other failure the first message may already be in the chat; the retry
    keeps the markup, so a copy renders like the first instead of showing the
    tags as text.
    """

    @pytest.mark.parametrize(
        "first",
        [
            _PlainRetryResponse(error=asyncio.TimeoutError()),
            _PlainRetryResponse(error=aiohttp.ServerDisconnectedError()),
            _PlainRetryResponse({"ok": False, "error_code": 500, "description": "Internal"}),
            _PlainRetryResponse({"ok": False, "error_code": 403, "description": "Forbidden"}),
        ],
        ids=["response-lost", "connection-reset", "server-error", "forbidden"],
    )
    def test_a_retry_after_an_unknown_outcome_keeps_the_markup(self, first) -> None:
        client, session = _plain_retry_client(first)

        result = asyncio.run(
            client.send_message(42, "\U0001f510 Approve <code>bash</code>?", parse_mode="HTML")
        )

        modes = [r.get("parse_mode") for r in session.received]
        assert modes == ["HTML", "HTML"], f"the retry shows the tags as text: {modes}"
        assert result == 102

    def test_a_rejected_markup_is_re_sent_once_as_plain_text(self) -> None:
        client, session = _plain_retry_client(_PlainRetryResponse(_REJECTED_MARKUP))

        result = asyncio.run(client.send_message(42, "<b>bad", parse_mode="HTML"))

        assert result == 102
        assert [r.get("parse_mode") for r in session.received] == ["HTML", None]
        for sent in session.received:
            assert sent["link_preview_options"] == {"is_disabled": True}

    def test_a_send_that_lands_is_sent_once(self) -> None:
        ok = _PlainRetryResponse({"ok": True, "result": {"message_id": 7}})
        client, session = _plain_retry_client(ok)

        assert asyncio.run(client.send_message(42, "<b>ok</b>", parse_mode="HTML")) == 7
        assert len(session.received) == 1

    def test_a_caller_that_opts_out_is_never_re_sent(self) -> None:
        client, session = _plain_retry_client(_PlainRetryResponse(_REJECTED_MARKUP))

        result = asyncio.run(
            client.send_message(42, "<b>bad", parse_mode="HTML", retry_plain=False)
        )

        assert result is None
        assert len(session.received) == 1

    def test_the_approval_card_is_not_refused_after_a_lost_response(self, monkeypatch) -> None:
        client, session = _plain_retry_client(_PlainRetryResponse(error=asyncio.TimeoutError()))
        refused: list[str] = []
        monkeypatch.setattr(
            TelegramApprovalDecider,
            "refuse_undelivered",
            classmethod(lambda cls, key: refused.append(key)),
        )
        renderer = TelegramRenderer(  # type: ignore[arg-type]
            client, 42, TELEGRAM_CAPABILITIES, session_key="telegram:42:0"
        )
        try:
            asyncio.run(renderer.on_prompt_choice([], request_id="rq-lost", tool_title="bash"))
        finally:
            TelegramApprovalDecider.retire(TelegramApprovalDecider.key("telegram:42:0", "rq-lost"))

        assert refused == [], "a lost response refused the approval"
        assert len(session.received) == 2


class TestLinkPreviewSuppression:
    def test_send_message_disables_previews_on_initial_send_and_plain_retry(
        self, monkeypatch
    ) -> None:
        client = TelegramClient(token="12345:testtoken")
        calls: list[tuple[str, dict[str, Any]]] = []

        async def _api(method, params, timeout=30, *, record=True, err_out=None):
            calls.append((method, dict(params)))
            return None if len(calls) == 1 else {"message_id": 7}

        monkeypatch.setattr(client, "_api", _api)
        result = asyncio.run(client.send_message(1, "<b>hello</b>", parse_mode="HTML"))

        assert result == 7
        assert [method for method, _params in calls] == ["sendMessage", "sendMessage"]
        for _method, params in calls:
            assert params["link_preview_options"] == {"is_disabled": True}

    def test_streaming_edit_disables_previews_on_both_attempts(self, monkeypatch) -> None:
        """A URL appearing mid-stream must not gain a preview on the edit
        path that the send path already denies (round-8 gpt finding)."""
        client = TelegramClient(token="12345:testtoken")
        calls: list[tuple[str, dict[str, Any]]] = []

        async def _api(method, params, timeout=30, *, record=True, err_out=None):
            calls.append((method, dict(params)))
            return None if len(calls) == 1 else {"message_id": 7}

        monkeypatch.setattr(client, "_api", _api)
        ok = asyncio.run(
            client.edit_message(1, 7, "<b>https://evil.example</b>", parse_mode="HTML")
        )

        assert ok is True
        assert [m for m, _p in calls] == ["editMessageText", "editMessageText"]
        for _method, params in calls:
            assert params["link_preview_options"] == {"is_disabled": True}

    def test_draft_and_rich_paths_disable_previews(self, monkeypatch) -> None:
        client = TelegramClient(token="12345:testtoken")
        calls: list[tuple[str, dict[str, Any]]] = []

        async def _api(method, params, timeout=30, *, record=True, err_out=None):
            calls.append((method, dict(params)))
            return {"message_id": 7}

        monkeypatch.setattr(client, "_api", _api)
        asyncio.run(client.send_message_draft(1, "d1", "streaming…"))
        asyncio.run(client.send_rich_message(1, "# heading"))

        assert [m for m, _p in calls] == ["sendMessageDraft", "sendRichMessage"]
        for _method, params in calls:
            assert params["link_preview_options"] == {"is_disabled": True}


class TestRichMessageAvailabilityLatch:
    """sendRichMessage learns whether the server implements the method."""

    def _client(self):
        from kiro_crew.telegram.client import TelegramClient

        return TelegramClient(token="12345:testtoken")

    def _stub(self, client, monkeypatch, codes: list[int | None]) -> list[str]:
        """Make _api fail with each code in turn; record the methods called."""
        calls: list[str] = []
        seq = list(codes)

        async def _api(method, params, timeout=30, *, record=True, err_out=None):
            calls.append(method)
            code = seq.pop(0) if seq else None
            if code is None:
                return {"message_id": 7}
            if err_out is not None:
                err_out["error_code"] = code
                err_out["description"] = "stub failure"
            return None

        monkeypatch.setattr(client, "_api", _api)
        return calls

    def test_a_404_latches_immediately_and_stops_re_probing(self, monkeypatch) -> None:
        # A server without the method rejects every call the same way, so
        # re-probing per table would waste a round-trip forever.
        c = self._client()
        calls = self._stub(c, monkeypatch, [404])
        assert asyncio.run(c.send_rich_message(1, "| a |\n| - |")) is None
        assert c._rich_unsupported is True
        # Second call must short-circuit without touching the API at all.
        assert asyncio.run(c.send_rich_message(1, "| a |\n| - |")) is None
        assert calls == ["sendRichMessage"], "latched: no second request"

    def test_a_429_never_latches(self, monkeypatch) -> None:
        # Rate limiting is transient. Disabling rich rendering for the process
        # because of one 429 would be a permanent penalty for a momentary limit.
        c = self._client()
        calls = self._stub(c, monkeypatch, [429, None])
        assert asyncio.run(c.send_rich_message(1, "| a |\n| - |")) is None
        assert c._rich_unsupported is False
        assert asyncio.run(c.send_rich_message(1, "| a |\n| - |")) == 7
        assert len(calls) == 2, "still probing after a transient failure"

    def test_one_400_does_not_latch_but_a_streak_does(self, monkeypatch) -> None:
        # 400 is ambiguous: a wrong payload shape fails EVERY call, while one
        # oversized table fails only itself. Latch on the streak so a single bad
        # message cannot disable rich rendering for the whole process.
        from kiro_crew.telegram.client import _RICH_400_LATCH

        c = self._client()
        self._stub(c, monkeypatch, [400] * _RICH_400_LATCH)
        for _ in range(_RICH_400_LATCH - 1):
            assert asyncio.run(c.send_rich_message(1, "| a |\n| - |")) is None
            assert c._rich_unsupported is False, "one bad table must not latch"
        assert asyncio.run(c.send_rich_message(1, "| a |\n| - |")) is None
        assert c._rich_unsupported is True, "a persistent 400 latches"

    def test_a_successful_send_clears_the_400_streak(self, monkeypatch) -> None:
        # Two unrelated bad tables spread over a session must not accumulate
        # into a latch when good tables send in between.
        from kiro_crew.telegram.client import _RICH_400_LATCH

        c = self._client()
        self._stub(c, monkeypatch, [400, None] * _RICH_400_LATCH)
        for _ in range(_RICH_400_LATCH):
            asyncio.run(c.send_rich_message(1, "| a |\n| - |"))  # 400
            asyncio.run(c.send_rich_message(1, "| a |\n| - |"))  # success
        assert c._rich_unsupported is False, "streak reset by each success"


_GATEWAY_PAGE = "<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>"


class _NonJsonResponse:
    """A response whose body ``text`` is parsed the way aiohttp's ``json()`` parses it."""

    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    async def json(self, content_type: Any = None) -> Any:
        return json.loads(self._text)

    async def __aenter__(self) -> "_NonJsonResponse":
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None


class _NonJsonSession:
    """Answers ``method`` calls from ``replies`` in turn, everything else with ok."""

    closed = False

    def __init__(self, *replies: _NonJsonResponse, method: str = "sendMessage") -> None:
        self._replies = list(replies)
        self._method = method
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    def post(self, url: str, **kwargs: Any) -> _NonJsonResponse:
        method = url.rsplit("/", 1)[-1]
        payload = dict(kwargs.get("json") or {})
        if method == self._method and self._replies:
            self.calls.append((method, payload, False))
            return self._replies.pop(0)
        self.calls.append((method, payload, True))
        sends = ("sendMessage", "sendRichMessage")
        result: Any = {"message_id": 100 + len(self.calls)} if method in sends else True
        return _NonJsonResponse(200, json.dumps({"ok": True, "result": result}))


class TestNonJsonErrorBody:
    """A response body that is not JSON (an HTML error page) is a failed call.

    ``_api_request`` returns None for it, as for any other failed call, and
    reports its HTTP status as the error code, so callers take their failure
    paths instead of meeting a decode error.
    """

    @staticmethod
    def _client(session: _NonJsonSession) -> TelegramClient:
        client = TelegramClient(token="12345:testtoken")
        client._session = session  # type: ignore[assignment]
        return client

    def test_a_502_html_page_is_a_failed_call_with_its_status(self) -> None:
        client = self._client(_NonJsonSession(_NonJsonResponse(502, _GATEWAY_PAGE)))
        err: dict[str, Any] = {}

        result = asyncio.run(client._api("sendMessage", {"chat_id": 42}, err_out=err))

        assert result is None
        assert err["error_code"] == 502

    def test_send_message_returns_none_for_a_502_html_page(self) -> None:
        client = self._client(_NonJsonSession(_NonJsonResponse(502, _GATEWAY_PAGE)))

        assert asyncio.run(client.send_message(42, "hello")) is None

    def test_the_answer_still_reaches_the_chat_after_a_502_html_page(self) -> None:
        session = _NonJsonSession(_NonJsonResponse(502, _GATEWAY_PAGE))
        renderer = TelegramRenderer(  # type: ignore[arg-type]
            self._client(session), 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0"
        )
        answer = "The build passed on all three targets."

        async def _turn() -> None:
            await renderer.on_turn_start()
            await renderer.on_text_chunk(answer)
            await renderer._seal_current()

        asyncio.run(_turn())

        landed = [
            payload.get("text", "")
            for method, payload, ok in session.calls
            if ok and method in ("sendMessage", "editMessageText")
        ]
        assert any(answer in text for text in landed), session.calls

    @pytest.mark.parametrize("status", [400, 502], ids=["400", "502"])
    def test_a_json_error_body_is_reported_as_before(self, status) -> None:
        body = {"ok": False, "error_code": status, "description": "Bad Request: chat not found"}
        client = self._client(_NonJsonSession(_NonJsonResponse(status, json.dumps(body))))
        err: dict[str, Any] = {}

        result = asyncio.run(client._api("sendMessage", {"chat_id": 42}, err_out=err))

        assert result is None
        assert err == {"error_code": status, "description": "Bad Request: chat not found"}

    def test_a_successful_send_is_unchanged(self) -> None:
        session = _NonJsonSession()

        assert asyncio.run(self._client(session).send_message(42, "hello")) == 101
        assert [method for method, _payload, _ok in session.calls] == ["sendMessage"]

    def test_one_proxy_error_page_does_not_turn_rich_messages_off(self) -> None:
        # A Bot API server without sendRichMessage answers every call with a JSON
        # 4xx, which latches rich rendering off. One HTML 403 page from an edge or
        # a proxy is not that server's verdict: the next rich send still goes out.
        page = "<html><head><title>403 Forbidden</title></head><body>blocked</body></html>"
        session = _NonJsonSession(_NonJsonResponse(403, page), method="sendRichMessage")
        client = self._client(session)
        table = "| a |\n|---|\n| 1 |"

        async def scenario() -> tuple[Any, Any]:
            first = await client.send_rich_message(42, table)
            return first, await client.send_rich_message(42, table)

        first, second = asyncio.run(scenario())

        assert first is None
        assert second is not None, (
            "one non-JSON 403 page from a proxy turned sendRichMessage off for the "
            f"process: server calls {[method for method, _payload, _ok in session.calls]}"
        )
        assert client._rich_unsupported is False
        assert [method for method, _payload, _ok in session.calls] == ["sendRichMessage"] * 2


class TestClientHealth:
    """get_me auth gate + on_status polling-health callback."""

    def _client(self):
        from kiro_crew.telegram.client import TelegramClient

        return TelegramClient(token="12345:testtoken")

    def test_get_me_returns_identity(self, monkeypatch) -> None:
        client = self._client()

        async def _call_raw(method, params, timeout=15):
            assert method == "getMe"
            return {"ok": True, "result": {"id": 42, "username": "kirocrew_bot"}}

        monkeypatch.setattr(client, "_call_raw", _call_raw)
        result = asyncio.run(client.get_me())
        assert result["username"] == "kirocrew_bot"

    def test_get_me_raises_auth_error_on_rejection(self, monkeypatch) -> None:
        from kiro_crew.telegram.client import TelegramAuthError

        client = self._client()

        async def _call_raw(method, params, timeout=15):
            return {"ok": False, "error_code": 401, "description": "Unauthorized"}

        monkeypatch.setattr(client, "_call_raw", _call_raw)
        try:
            asyncio.run(client.get_me())
            raise AssertionError("expected TelegramAuthError")
        except TelegramAuthError as exc:
            # The message must stay token-free: it is surfaced in settings.
            assert "testtoken" not in str(exc)
            assert "Unauthorized" in str(exc)

    def test_get_me_propagates_transport_errors(self, monkeypatch) -> None:
        """Offline is NOT a bad token: transport errors must not become
        TelegramAuthError, so the gateway can degrade instead of failing."""
        import aiohttp

        client = self._client()

        async def _call_raw(method, params, timeout=15):
            raise aiohttp.ClientConnectionError("network down")

        monkeypatch.setattr(client, "_call_raw", _call_raw)
        try:
            asyncio.run(client.get_me())
            raise AssertionError("expected ClientConnectionError")
        except aiohttp.ClientConnectionError:
            pass

    def test_notify_status_swallows_callback_errors(self) -> None:
        client = self._client()

        def _boom(healthy, reason):
            raise RuntimeError("callback bug")

        client.on_status = _boom
        client._notify_status(False, "x")  # must not raise

    def test_polling_loop_reports_persistent_failure_and_recovery(self, monkeypatch) -> None:
        """3 consecutive getUpdates failures -> unhealthy; next success -> healthy."""
        client = self._client()
        transitions: list[tuple[bool, str]] = []
        client.on_status = lambda healthy, reason: transitions.append((healthy, reason))

        results: list[Any] = [None, None, None, []]  # 3 failures then success

        async def _get_updates():
            if not results:
                client._closed = True
                return []
            return results.pop(0)

        async def _no_sleep(_delay):
            return None

        monkeypatch.setattr(client, "_get_updates", _get_updates)
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        asyncio.run(client._polling_loop())
        assert transitions[0][0] is False  # reported unhealthy at threshold
        assert "getUpdates" in transitions[0][1]
        assert transitions[1] == (True, "")  # recovered on next success

    def test_polling_loop_reports_recovery_from_offline_boot(self, monkeypatch) -> None:
        """When the gateway seeded unhealthy (offline at startup), the FIRST
        successful poll must flip to healthy — no failure threshold applies."""
        client = self._client()
        client._last_status = False  # gateway boot state: unreachable
        transitions: list[tuple[bool, str]] = []
        client.on_status = lambda healthy, reason: transitions.append((healthy, reason))

        results: list[Any] = [[]]  # immediate success

        async def _get_updates():
            if not results:
                client._closed = True
                return []
            return results.pop(0)

        monkeypatch.setattr(client, "_get_updates", _get_updates)
        asyncio.run(client._polling_loop())
        assert transitions and transitions[0] == (True, "")
        assert len(transitions) == 1  # deduped: repeat successes don't re-fire


class TestSetMyCommands:
    def test_empty_list_is_refused_so_the_menu_is_never_wiped(self) -> None:
        # Telegram reads an empty array as "this bot has no commands" and clears
        # the menu, so an empty payload must not reach the wire.
        client = TelegramClient(token="t")
        calls: list = []

        async def _api(method: str, params: dict, *a: Any, **kw: Any) -> Any:
            calls.append(method)
            return True

        client._api = _api  # type: ignore[assignment]
        assert asyncio.run(client.set_my_commands([])) is False
        assert calls == []

    def test_publishes_the_catalogue(self) -> None:
        client = TelegramClient(token="t")
        sent: list = []

        async def _api(method: str, params: dict, *a: Any, **kw: Any) -> Any:
            sent.append((method, params))
            return True

        client._api = _api  # type: ignore[assignment]
        assert asyncio.run(client.set_my_commands(bot_command_payload())) is True
        assert sent[0][0] == "setMyCommands"
        assert {"command": "model", "description": "Choose the model from a list"} in sent[0][1][
            "commands"
        ]


class TestClientClose:
    def test_close_closes_session_even_when_task_died_with_a_bug(self) -> None:
        """A polling task already dead from an uncaught, non-CancelledError
        exception makes ``task.cancel()`` a no-op, and re-``await``ing it
        re-raises that exception -- which must not skip the session close."""

        class _FakeSession:
            def __init__(self) -> None:
                self.closed = False
                self.close_calls = 0

            async def close(self) -> None:
                self.close_calls += 1
                self.closed = True

        async def _run() -> None:
            client = TelegramClient(token="t")
            session = _FakeSession()
            client._session = session  # type: ignore[assignment]

            async def _buggy_loop() -> None:
                raise ValueError("malformed update")

            client._task = asyncio.create_task(_buggy_loop())
            # flake-ok: pre-existing zero-length yield so the failing task ends before close(), moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)  # let the task actually finish before close()

            try:
                await client.close()
                raise AssertionError("close() must propagate the task's exception")
            except ValueError as exc:
                assert "malformed update" in str(exc)

            assert client._task is None
            assert session.close_calls == 1
            assert client._session is None

        asyncio.run(_run())
