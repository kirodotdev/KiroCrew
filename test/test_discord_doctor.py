"""Guards `kirocrew doctor`'s Discord section and the intent probe behind it.

Three things here are worth more than the line-by-line rendering:

* **The tri-state decode.** Discord reports each privileged intent as a PAIR of
  flag bits (unlimited / limited), and both mean the toggle is on. A decode
  that reads the unlimited bit alone reports every unverified install as "off",
  so the pairs are pinned per intent, including that an unrelated flag bit
  sitting between two pairs turns nothing on.
* **Degrading to "unknown".** The probe is a network call inside a diagnostic.
  Every failure has to become a reported unknown, never an exception, or
  `kirocrew doctor` produces no report at the moment the operator needs one.
* **The empty user allow-list.** The Discord transport fails closed, so an
  install that looks completely configured denies every message when
  ``allowed_user_ids`` is empty. That has to be called out as a blocking issue,
  not merely printed.
"""

from __future__ import annotations

import urllib.error
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import cli_doctor
from kiro_crew.config.loader import DiscordConfig, KiroCrewConfig
from kiro_crew.discord import intent_probe

_TOKEN = "MFYzNQ.qXpLwZ.vKjHgFdSaPoIuYtReWq"
_APP_ID = "123456789012345678"
_USER_ID = "234567890123456789"
_THREAD_ID = "345678901234567890"


# ── Intent flag decode ────────────────────────────────────────────────────────


#: field name -> (unlimited bit, limited bit), as Discord defines them.
_PAIRS: dict[str, tuple[int, int]] = {
    "message_content": (
        intent_probe.FLAG_GATEWAY_MESSAGE_CONTENT,
        intent_probe.FLAG_GATEWAY_MESSAGE_CONTENT_LIMITED,
    ),
    "server_members": (
        intent_probe.FLAG_GATEWAY_GUILD_MEMBERS,
        intent_probe.FLAG_GATEWAY_GUILD_MEMBERS_LIMITED,
    ),
    "presence": (
        intent_probe.FLAG_GATEWAY_PRESENCE,
        intent_probe.FLAG_GATEWAY_PRESENCE_LIMITED,
    ),
}


class TestIntentFlagDecode:
    """One bit pair per intent, decoded as enabled / limited / disabled."""

    def test_flag_bits_are_discords_own_positions(self) -> None:
        # The pairing is per intent, not positional: presence owns the low
        # pair, members the middle, message content the high one.
        assert intent_probe.FLAG_GATEWAY_PRESENCE == 1 << 12
        assert intent_probe.FLAG_GATEWAY_PRESENCE_LIMITED == 1 << 13
        assert intent_probe.FLAG_GATEWAY_GUILD_MEMBERS == 1 << 14
        assert intent_probe.FLAG_GATEWAY_GUILD_MEMBERS_LIMITED == 1 << 15
        assert intent_probe.FLAG_GATEWAY_MESSAGE_CONTENT == 1 << 18
        assert intent_probe.FLAG_GATEWAY_MESSAGE_CONTENT_LIMITED == 1 << 19

    @pytest.mark.parametrize("field", sorted(_PAIRS))
    def test_unlimited_bit_is_enabled_and_isolated(self, field: str) -> None:
        grants = intent_probe.decode_intent_flags(_PAIRS[field][0])
        assert getattr(grants, field) == intent_probe.INTENT_ENABLED
        for other in _PAIRS:
            if other != field:
                assert getattr(grants, other) == intent_probe.INTENT_DISABLED

    @pytest.mark.parametrize("field", sorted(_PAIRS))
    def test_limited_bit_is_limited_and_isolated(self, field: str) -> None:
        grants = intent_probe.decode_intent_flags(_PAIRS[field][1])
        assert getattr(grants, field) == intent_probe.INTENT_LIMITED
        for other in _PAIRS:
            if other != field:
                assert getattr(grants, other) == intent_probe.INTENT_DISABLED

    def test_limited_counts_as_granted(self) -> None:
        # A limited grant delivers the data; only the server count is capped.
        assert intent_probe.INTENT_LIMITED in intent_probe.GRANTED_STATES
        assert intent_probe.INTENT_ENABLED in intent_probe.GRANTED_STATES
        assert intent_probe.INTENT_DISABLED not in intent_probe.GRANTED_STATES
        assert intent_probe.INTENT_UNKNOWN not in intent_probe.GRANTED_STATES

    def test_zero_flags_is_all_disabled(self) -> None:
        grants = intent_probe.decode_intent_flags(0)
        assert (grants.message_content, grants.server_members, grants.presence) == (
            intent_probe.INTENT_DISABLED,
        ) * 3
        assert grants.known is True
        assert grants.error == ""

    def test_every_bit_set_is_all_enabled(self) -> None:
        flags = 0
        for unlimited, limited in _PAIRS.values():
            flags |= unlimited | limited
        grants = intent_probe.decode_intent_flags(flags)
        assert (grants.message_content, grants.server_members, grants.presence) == (
            intent_probe.INTENT_ENABLED,
        ) * 3

    def test_unrelated_flag_bits_grant_nothing(self) -> None:
        # Bits 16 and 17 sit between the members and message-content pairs; an
        # off-by-one pairing would read them as a grant.
        grants = intent_probe.decode_intent_flags((1 << 16) | (1 << 17) | (1 << 6))
        assert (grants.message_content, grants.server_members, grants.presence) == (
            intent_probe.INTENT_DISABLED,
        ) * 3

    @pytest.mark.parametrize("bad", [None, "1024", True, False, -1, 1.5, [1 << 18], {}])
    def test_malformed_flags_decode_to_unknown_not_disabled(self, bad: object) -> None:
        # "Disabled" would send the operator to switch on an intent that may
        # already be on; unknown says what is actually true.
        grants = intent_probe.decode_intent_flags(bad)
        assert (grants.message_content, grants.server_members, grants.presence) == (
            intent_probe.INTENT_UNKNOWN,
        ) * 3
        assert grants.error
        assert grants.known is False


# ── The live probe ────────────────────────────────────────────────────────────


class _FakeResp:
    """Minimal aiohttp response: a status plus a JSON body."""

    def __init__(self, status: int, payload: object, *, json_error: Exception | None = None):
        self.status = status
        self._payload = payload
        self._json_error = json_error

    async def json(self, content_type: object = None) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    """Records every request; only ``get`` exists, so a write would AttributeError."""

    def __init__(self, resp: _FakeResp | None = None, *, get_error: Exception | None = None):
        self._resp = resp
        self._get_error = get_error
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResp:
        self.calls.append((url, dict(headers or {})))
        if self._get_error is not None:
            raise self._get_error
        assert self._resp is not None
        return self._resp

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _install_fake_aiohttp(monkeypatch, session: _FakeSession) -> dict[str, Any]:
    """Swap the probe's aiohttp for a recorder. Returns what it observed."""
    seen: dict[str, Any] = {"sessions": 0}

    def client_timeout(total: float | None = None) -> object:
        seen["total"] = total
        return object()

    def client_session(timeout: object = None) -> _FakeSession:
        seen["sessions"] += 1
        return session

    monkeypatch.setattr(
        intent_probe,
        "aiohttp",
        SimpleNamespace(ClientTimeout=client_timeout, ClientSession=client_session),
    )
    return seen


class TestProbeIntentGrants:
    """The probe: read-only, bounded, and never raising."""

    @pytest.mark.asyncio
    async def test_reads_the_app_record_with_a_bot_token_and_decodes_it(self, monkeypatch) -> None:
        session = _FakeSession(
            _FakeResp(
                200,
                {
                    "id": _APP_ID,
                    "flags": intent_probe.FLAG_GATEWAY_MESSAGE_CONTENT_LIMITED,
                },
            )
        )
        seen = _install_fake_aiohttp(monkeypatch, session)

        grants = await intent_probe.probe_intent_grants(_TOKEN)

        assert grants.message_content == intent_probe.INTENT_LIMITED
        assert grants.application_id == _APP_ID
        assert grants.error == ""
        # One GET, on the app's own record, authenticated as a bot. Nothing
        # else: this must not be able to change the install it reports on.
        assert session.calls == [(intent_probe.APPLICATION_URL, {"Authorization": f"Bot {_TOKEN}"})]
        assert seen["total"] == intent_probe.PROBE_TIMEOUT_SECS

    @pytest.mark.asyncio
    async def test_honours_a_caller_supplied_timeout(self, monkeypatch) -> None:
        session = _FakeSession(_FakeResp(200, {"id": _APP_ID, "flags": 0}))
        seen = _install_fake_aiohttp(monkeypatch, session)

        await intent_probe.probe_intent_grants(_TOKEN, timeout=0.25)

        assert seen["total"] == 0.25

    @pytest.mark.asyncio
    async def test_no_token_answers_unknown_without_calling_discord(self, monkeypatch) -> None:
        session = _FakeSession(_FakeResp(200, {"id": _APP_ID, "flags": 0}))
        seen = _install_fake_aiohttp(monkeypatch, session)

        grants = await intent_probe.probe_intent_grants("")

        assert grants.known is False
        assert grants.error == "no bot token"
        assert seen["sessions"] == 0
        assert session.calls == []

    @pytest.mark.asyncio
    async def test_rejected_token_is_unknown_and_names_the_status(self, monkeypatch) -> None:
        _install_fake_aiohttp(
            monkeypatch, _FakeSession(_FakeResp(401, {"message": "401: Unauthorized"}))
        )

        grants = await intent_probe.probe_intent_grants(_TOKEN)

        assert grants.known is False
        assert "401" in grants.error

    @pytest.mark.asyncio
    async def test_server_error_is_unknown_not_disabled(self, monkeypatch) -> None:
        _install_fake_aiohttp(monkeypatch, _FakeSession(_FakeResp(503, None)))

        grants = await intent_probe.probe_intent_grants(_TOKEN)

        assert grants.message_content == intent_probe.INTENT_UNKNOWN
        assert grants.error == "HTTP 503"

    @pytest.mark.asyncio
    async def test_error_never_carries_the_token(self, monkeypatch) -> None:
        _install_fake_aiohttp(monkeypatch, _FakeSession(get_error=OSError("boom")))

        grants = await intent_probe.probe_intent_grants(_TOKEN)

        assert grants.error == "OSError"
        assert _TOKEN not in grants.error

    @pytest.mark.asyncio
    async def test_transport_failure_answers_instead_of_raising(self, monkeypatch) -> None:
        _install_fake_aiohttp(monkeypatch, _FakeSession(get_error=TimeoutError()))

        grants = await intent_probe.probe_intent_grants(_TOKEN)

        assert grants.known is False
        assert grants.error == "TimeoutError"

    @pytest.mark.asyncio
    async def test_undecodable_body_answers_instead_of_raising(self, monkeypatch) -> None:
        _install_fake_aiohttp(
            monkeypatch, _FakeSession(_FakeResp(200, None, json_error=ValueError("not json")))
        )

        grants = await intent_probe.probe_intent_grants(_TOKEN)

        assert grants.known is False
        assert grants.error == "ValueError"

    @pytest.mark.asyncio
    async def test_non_object_body_is_unknown(self, monkeypatch) -> None:
        _install_fake_aiohttp(monkeypatch, _FakeSession(_FakeResp(200, [1, 2, 3])))

        grants = await intent_probe.probe_intent_grants(_TOKEN)

        assert grants.known is False
        assert grants.error == "unexpected response body"

    @pytest.mark.asyncio
    async def test_missing_flags_key_is_unknown_with_the_app_id_kept(self, monkeypatch) -> None:
        _install_fake_aiohttp(monkeypatch, _FakeSession(_FakeResp(200, {"id": _APP_ID})))

        grants = await intent_probe.probe_intent_grants(_TOKEN)

        assert grants.known is False
        assert grants.application_id == _APP_ID

    @pytest.mark.asyncio
    async def test_an_id_that_is_not_a_snowflake_is_dropped(self, monkeypatch) -> None:
        # The id is remote input that ends up in a printed install URL.
        _install_fake_aiohttp(
            monkeypatch, _FakeSession(_FakeResp(200, {"id": "1234\x1b[31m", "flags": 0}))
        )

        grants = await intent_probe.probe_intent_grants(_TOKEN)

        assert grants.application_id == ""
        assert grants.message_content == intent_probe.INTENT_DISABLED


class TestDoctorIntentGrantsWrapper:
    """`_discord_intent_grants` runs the probe on its own loop and never raises."""

    def test_returns_the_probe_result(self, monkeypatch) -> None:
        async def fake(token: str) -> intent_probe.IntentGrants:
            assert token == _TOKEN
            return intent_probe.IntentGrants(message_content=intent_probe.INTENT_ENABLED)

        monkeypatch.setattr(intent_probe, "probe_intent_grants", fake)

        assert (
            cli_doctor._discord_intent_grants(_TOKEN).message_content == intent_probe.INTENT_ENABLED
        )

    def test_a_probe_that_cannot_even_be_awaited_is_reported_not_raised(self, monkeypatch) -> None:
        monkeypatch.setattr(intent_probe, "probe_intent_grants", lambda token: "not awaitable")

        grants = cli_doctor._discord_intent_grants(_TOKEN)

        assert grants.known is False
        assert grants.error == "ValueError"


# ── The doctor section ────────────────────────────────────────────────────────


def _cfg(**kwargs: Any) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.discord = DiscordConfig(**kwargs)
    return cfg


def _render(
    monkeypatch,
    capsys,
    *,
    cfg: KiroCrewConfig,
    token: str = "",
    grants: intent_probe.IntentGrants | None = None,
    live: dict[str, object] | None = None,
    port: int | None = 8765,
) -> tuple[str, list[str]]:
    """Render the Discord section with the two live probes stubbed out."""
    probed: list[str] = []

    def fake_grants(tok: str) -> intent_probe.IntentGrants:
        probed.append(tok)
        return grants if grants is not None else intent_probe.IntentGrants()

    monkeypatch.setattr(cli_doctor, "_discord_intent_grants", fake_grants)
    monkeypatch.setattr(cli_doctor, "_discord_live_state", lambda p: live)
    issues: list[str] = []
    creds = {"DISCORD_BOT_TOKEN": token} if token else {}
    cli_doctor._doctor_discord(cfg, creds, port, issues)
    out = capsys.readouterr().out
    if not cfg.discord.enabled:
        assert probed == [], "a disabled channel must not be probed over the network"
    return out, issues


class TestDoctorDiscordSection:
    """The rendered section, one failure mode at a time."""

    def test_disabled_channel_says_how_to_enable_it_and_blocks_nothing(
        self, monkeypatch, capsys
    ) -> None:
        out, issues = _render(monkeypatch, capsys, cfg=_cfg(enabled=False))

        assert "Discord Integration" in out
        assert "not enabled (optional)" in out
        assert "Settings → Discord" in out
        assert "DISCORD_BOT_TOKEN" in out
        assert issues == []

    def test_enabled_without_a_token_is_a_blocking_issue_with_a_fix(
        self, monkeypatch, capsys
    ) -> None:
        out, issues = _render(
            monkeypatch, capsys, cfg=_cfg(enabled=True, allowed_user_ids=[_USER_ID])
        )

        assert "token:       ❌" in out
        assert "the channel never starts" in out
        assert "kirocrew restart" in out
        assert "discord: enabled without a bot token" in issues

    def test_empty_user_allowlist_is_called_out_as_denying_everything(
        self, monkeypatch, capsys
    ) -> None:
        out, issues = _render(monkeypatch, capsys, cfg=_cfg(enabled=True), token=_TOKEN)

        assert "users:       ❌" in out
        assert "EVERY message is denied" in out
        assert "Copy User ID" in out
        assert "discord: empty user allow-list denies every message" in issues

    def test_a_configured_channel_reports_its_allowlists(self, monkeypatch, capsys) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_cfg(enabled=True, allowed_user_ids=[_USER_ID, "999"]),
            token=_TOKEN,
        )

        assert "status:      ✅ enabled" in out
        assert "token:       ✅ present" in out
        assert "users:       ✅ 2 allow-listed" in out
        assert issues == []

    def test_the_bot_token_is_never_printed_even_in_part(self, monkeypatch, capsys) -> None:
        out, _ = _render(
            monkeypatch,
            capsys,
            cfg=_cfg(enabled=True, allowed_user_ids=[_USER_ID]),
            token=_TOKEN,
            # The masked preview the live endpoint really returns: four
            # bullets plus the token's last four characters.
            live={"connected": True, "bot_token_preview": f"••••{_TOKEN[-4:]}"},
        )

        assert _TOKEN not in out
        for start in range(len(_TOKEN) - 3):
            assert _TOKEN[start : start + 4] not in out
        # A diagnostic an operator pastes into an issue has no business
        # repeating even the masked preview.
        assert "••••" not in out

    def test_no_thread_or_channel_ids_reads_as_dms_only(self, monkeypatch, capsys) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_cfg(enabled=True, allowed_user_ids=[_USER_ID]),
            token=_TOKEN,
        )

        assert "servers:     ⏹ none, DMs only" in out
        assert issues == []

    def test_thread_and_channel_counts_are_reported(self, monkeypatch, capsys) -> None:
        out, _ = _render(
            monkeypatch,
            capsys,
            cfg=_cfg(
                enabled=True,
                allowed_user_ids=[_USER_ID],
                allowed_thread_ids=[_THREAD_ID],
                allowed_channel_ids=["1", "2"],
            ),
            token=_TOKEN,
            grants=intent_probe.IntentGrants(message_content=intent_probe.INTENT_ENABLED),
        )

        assert "servers:     ✅ 1 thread(s), 2 channel(s)" in out
        # The allow-list is what makes the privileged intent mandatory, so the
        # line that reports it says so.
        assert "Message Content required" in out


def _thread_cfg() -> KiroCrewConfig:
    return _cfg(
        enabled=True,
        allowed_user_ids=[_USER_ID],
        allowed_thread_ids=[_THREAD_ID],
    )


class TestDoctorMessageContentIntent:
    """Message Content severity follows the allow-lists, not the grant alone."""

    def test_off_with_threads_allowlisted_is_a_blocking_issue(self, monkeypatch, capsys) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_thread_cfg(),
            token=_TOKEN,
            grants=intent_probe.IntentGrants(
                message_content=intent_probe.INTENT_DISABLED,
                server_members=intent_probe.INTENT_DISABLED,
                presence=intent_probe.INTENT_DISABLED,
            ),
        )

        assert "msg content: ❌ OFF" in out
        assert "4014" in out
        assert "Developer Portal" in out
        assert "discord: Message Content Intent off with threads allow-listed" in issues

    def test_granted_with_threads_allowlisted_passes(self, monkeypatch, capsys) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_thread_cfg(),
            token=_TOKEN,
            grants=intent_probe.IntentGrants(message_content=intent_probe.INTENT_ENABLED),
        )

        assert "msg content: ✅ granted" in out
        assert "capped" not in out
        assert issues == []

    def test_limited_grant_passes_and_says_what_limited_means(self, monkeypatch, capsys) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_thread_cfg(),
            token=_TOKEN,
            grants=intent_probe.IntentGrants(message_content=intent_probe.INTENT_LIMITED),
        )

        assert "msg content: ✅ granted" in out
        assert "100 servers" in out
        assert issues == []

    def test_unverifiable_grant_warns_but_never_blocks(self, monkeypatch, capsys) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_thread_cfg(),
            token=_TOKEN,
            grants=intent_probe.IntentGrants(error="TimeoutError"),
        )

        assert "msg content: ⚠️" in out
        assert "cannot verify (TimeoutError)" in out
        assert issues == []

    def test_dm_only_install_does_not_need_the_intent(self, monkeypatch, capsys) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_cfg(enabled=True, allowed_user_ids=[_USER_ID]),
            token=_TOKEN,
            grants=intent_probe.IntentGrants(message_content=intent_probe.INTENT_DISABLED),
        )

        assert "msg content: ⏭  not needed" in out
        assert issues == []

    def test_dm_only_install_with_the_intent_on_says_it_is_unused(
        self, monkeypatch, capsys
    ) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_cfg(enabled=True, allowed_user_ids=[_USER_ID]),
            token=_TOKEN,
            grants=intent_probe.IntentGrants(message_content=intent_probe.INTENT_ENABLED),
        )

        assert "unused by a DM-only install" in out
        assert issues == []


class TestDoctorUnusedIntents:
    """Server Members and Presence: reported only when they are granted."""

    @pytest.mark.parametrize(
        ("field", "label"),
        [("server_members", "members:"), ("presence", "presence:")],
    )
    @pytest.mark.parametrize("state", [intent_probe.INTENT_ENABLED, intent_probe.INTENT_LIMITED])
    def test_a_granted_unused_intent_is_flagged_with_how_to_drop_it(
        self, monkeypatch, capsys, field: str, label: str, state: str
    ) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_thread_cfg(),
            token=_TOKEN,
            grants=intent_probe.IntentGrants(
                message_content=intent_probe.INTENT_ENABLED, **{field: state}
            ),
        )

        assert f"{label} " in out
        assert "on but unused" in out
        assert "Turn it off" in out
        # Over-granting is a hardening note, never a broken install.
        assert issues == []

    @pytest.mark.parametrize("state", [intent_probe.INTENT_DISABLED, intent_probe.INTENT_UNKNOWN])
    def test_an_ungranted_unused_intent_stays_quiet(self, monkeypatch, capsys, state: str) -> None:
        out, _ = _render(
            monkeypatch,
            capsys,
            cfg=_thread_cfg(),
            token=_TOKEN,
            grants=intent_probe.IntentGrants(
                message_content=intent_probe.INTENT_ENABLED,
                server_members=state,
                presence=state,
            ),
        )

        assert "on but unused" not in out
        assert "members:" not in out
        assert "presence:" not in out


class TestDoctorLiveConnection:
    """The live connected state, read from the running gateway."""

    def test_connected_reports_green(self, monkeypatch, capsys) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_cfg(enabled=True, allowed_user_ids=[_USER_ID]),
            token=_TOKEN,
            live={"connected": True, "connect_error": ""},
        )

        assert "connection:  ✅ connected" in out
        assert issues == []

    def test_connect_error_is_shown_with_the_close_code_fixes(self, monkeypatch, capsys) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_cfg(enabled=True, allowed_user_ids=[_USER_ID]),
            token=_TOKEN,
            live={"connected": False, "connect_error": "gateway close 4014"},
        )

        assert "connection:  ❌ not connected" in out
        assert "gateway close 4014" in out
        assert "4004 = reset the bot" in out
        assert "discord: channel not connected" in issues

    def test_a_control_sequence_in_the_reason_is_shown_inert(self, monkeypatch, capsys) -> None:
        out, _ = _render(
            monkeypatch,
            capsys,
            cfg=_cfg(enabled=True, allowed_user_ids=[_USER_ID]),
            token=_TOKEN,
            live={"connected": False, "connect_error": "close \x1b[2Kfaked line"},
        )

        assert "\x1b" not in out
        assert "\\x1b[2Kfaked line" in out

    def test_not_connected_without_a_reason_points_at_a_restart(self, monkeypatch, capsys) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_cfg(enabled=True, allowed_user_ids=[_USER_ID]),
            token=_TOKEN,
            live={"connected": False, "connect_error": ""},
        )

        assert "connection:  ⚠️" in out
        assert "kirocrew" in out and "restart" in out
        assert issues == []

    def test_unreachable_gateway_is_not_a_discord_fault(self, monkeypatch, capsys) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_cfg(enabled=True, allowed_user_ids=[_USER_ID]),
            token=_TOKEN,
            live=None,
        )

        assert "connection:  ⏹ live state unavailable" in out
        assert issues == []


class TestDoctorLiveStateReader:
    """`_discord_live_state`: loopback, bounded, and silent on every failure."""

    def test_no_port_never_touches_the_network(self, monkeypatch) -> None:
        # Recorded rather than raised: the reader swallows every exception by
        # design, so a raising stub would be silently absorbed and the test
        # would pass against a reader that DID make the request.
        attempts: list[object] = []

        def record(req: Any, timeout: float | None = None) -> None:
            attempts.append(req)
            raise urllib.error.URLError("unreachable")

        monkeypatch.setattr(cli_doctor.urllib.request, "urlopen", record)

        assert cli_doctor._discord_live_state(None) is None
        assert attempts == []

    def test_reads_the_loopback_endpoint(self, monkeypatch) -> None:
        seen: list[str] = []

        class _Resp:
            def read(self) -> bytes:
                return b'{"connected": true}'

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        def fake_urlopen(req: Any, timeout: float | None = None) -> "_Resp":
            seen.append(req.full_url)
            return _Resp()

        monkeypatch.setattr(cli_doctor.urllib.request, "urlopen", fake_urlopen)

        assert cli_doctor._discord_live_state(8765) == {"connected": True}
        assert seen == ["http://127.0.0.1:8765/api/discord/config"]

    @pytest.mark.parametrize(
        "failure",
        [
            urllib.error.URLError("down"),
            urllib.error.HTTPError("u", 401, "unauthorized", {}, None),  # type: ignore[arg-type]
            OSError("reset"),
        ],
    )
    def test_every_failure_reads_as_unavailable(self, monkeypatch, failure: Exception) -> None:
        def fake_urlopen(req: Any, timeout: float | None = None) -> None:
            raise failure

        monkeypatch.setattr(cli_doctor.urllib.request, "urlopen", fake_urlopen)

        assert cli_doctor._discord_live_state(8765) is None

    def test_a_non_object_body_reads_as_unavailable(self, monkeypatch) -> None:
        class _Resp:
            def read(self) -> bytes:
                return b"[1, 2]"

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(cli_doctor.urllib.request, "urlopen", lambda req, timeout=None: _Resp())

        assert cli_doctor._discord_live_state(8765) is None


class TestDoctorInstallUrl:
    """Discord's install surface: the authorize URL, printed for this install."""

    def test_a_thread_install_prints_the_documented_permissions(self, monkeypatch, capsys) -> None:
        out, _ = _render(
            monkeypatch,
            capsys,
            cfg=_thread_cfg(),
            token=_TOKEN,
            grants=intent_probe.IntentGrants(
                message_content=intent_probe.INTENT_ENABLED, application_id=_APP_ID
            ),
        )

        assert f"install URL: https://discord.com/oauth2/authorize?client_id={_APP_ID}" in out
        assert "scope=bot+applications.commands" in out
        assert "permissions=309237711936" in out
        assert "thread-capable" in out

    def test_a_dm_only_install_prints_the_no_permission_url(self, monkeypatch, capsys) -> None:
        out, _ = _render(
            monkeypatch,
            capsys,
            cfg=_cfg(enabled=True, allowed_user_ids=[_USER_ID]),
            token=_TOKEN,
            grants=intent_probe.IntentGrants(
                message_content=intent_probe.INTENT_DISABLED, application_id=_APP_ID
            ),
        )

        assert "permissions=0" in out
        assert "309237711936" not in out
        assert "DM-only" in out

    def test_without_an_app_id_it_points_at_the_doc_instead_of_a_dead_url(
        self, monkeypatch, capsys
    ) -> None:
        out, issues = _render(
            monkeypatch,
            capsys,
            cfg=_cfg(enabled=True, allowed_user_ids=[_USER_ID]),
            token=_TOKEN,
            grants=intent_probe.IntentGrants(error="TimeoutError"),
        )

        assert "install URL: ⏭  needs the app id" in out
        assert "discord.com/oauth2/authorize" not in out
        assert issues == []
