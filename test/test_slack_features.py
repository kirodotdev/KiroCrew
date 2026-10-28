"""``kiro_crew.slack.features``: the Slack Code switches and the install's capability.

Pins the shared contract other work codes against: the switch readers fail
closed, a switch runs only while the installed app can use it, and Slack's
refusals latch a feature off until the next reconnect re-probes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.section_builders import _build_slack_config
from kiro_crew.slack import features
from kiro_crew.testing.wait import async_wait_until

#: A second switch registered only for the test, the way a later PR registers
#: one: an entry in ``slack_manifest.FEATURE_SCOPES``. Nothing else in
#: ``features`` names a switch, so registering it is all the reader needs.
_FAKE = "fake_switch"
_FAKE_SCOPE = "fake:write"

_AS = "agent_sessions"
_AS_SCOPE = "assistant:write"


@pytest.fixture
def fake_switch(monkeypatch: pytest.MonkeyPatch) -> str:
    from kiro_crew import slack_manifest

    monkeypatch.setitem(slack_manifest.FEATURE_SCOPES, _FAKE, _FAKE_SCOPE)
    monkeypatch.setattr(slack_manifest, "FEATURES", tuple(slack_manifest.FEATURE_SCOPES))
    return _FAKE


def _slack(**switches: object) -> SimpleNamespace:
    """A Slack section carrying arbitrary switch fields (a registered fake one too)."""
    return SimpleNamespace(**switches)


class TestSwitches:
    def test_defaults_are_off(self) -> None:
        cfg = KiroCrewConfig()
        assert features.agent_sessions_enabled(cfg) is False
        assert features.toggle(cfg, _AS) is False
        assert features.manifest_features(cfg) == frozenset()

    @pytest.mark.parametrize("raw", ["true", 1, "yes", None, [], {}])
    def test_only_a_real_true_turns_a_switch_on(self, raw: object) -> None:
        slack = _build_slack_config({_AS: raw})
        assert features.toggle(slack, _AS) is False
        assert features.toggle(_slack(agent_sessions=raw), _AS) is False

    def test_true_turns_each_switch_on_independently(self, fake_switch: str) -> None:
        slack = _slack(agent_sessions=True, fake_switch=False)
        assert features.toggle(slack, _AS) is True
        assert features.toggle(slack, fake_switch) is False
        assert features.manifest_features(slack) == {_AS}
        assert features.manifest_features(_slack(fake_switch=True)) == {fake_switch}

    def test_an_unregistered_switch_is_off_even_when_set(self) -> None:
        assert features.toggle(_slack(not_registered=True), "not_registered") is False
        assert features.manifest_features(_slack(not_registered=True)) == frozenset()

    def test_reader_accepts_full_config_or_slack_section(self) -> None:
        cfg = KiroCrewConfig()
        cfg.slack = _build_slack_config({_AS: True})
        assert features.toggle(cfg, _AS) is True
        assert features.toggle(cfg.slack, _AS) is True

    def test_a_reader_handed_something_unrelated_is_off(self) -> None:
        assert features.toggle(object(), _AS) is False
        assert features.agent_sessions_enabled(object()) is False
        assert features.manifest_features(object()) == frozenset()


# ── Capability: toggle AND available ──


class _Resp(dict):
    def __init__(self, data: dict, headers: dict | None = None) -> None:
        super().__init__(data)
        self.headers = headers or {}


class _SlackErr(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.response = {"ok": False, "error": error}


class _Client:
    def __init__(self, scopes: str | None, *, auth_error: str = "") -> None:
        self.scopes, self.auth_error = scopes, auth_error
        self.calls: list[str] = []

    async def auth_test(self) -> _Resp:
        self.calls.append("auth.test")
        if self.auth_error:
            raise _SlackErr(self.auth_error)
        headers = {} if self.scopes is None else {"x-oauth-scopes": self.scopes}
        return _Resp({"ok": True}, headers)

    async def api_call(self, method: str, **kw: object) -> _Resp:
        self.calls.append(method)
        return _Resp({"ok": True})


@pytest.fixture()
def caps():
    features.reset_capabilities()
    features.set_capability_notifier(None)
    yield features
    features.reset_capabilities()
    features.set_capability_notifier(None)


def _on():
    return _slack(agent_sessions=True, fake_switch=True)


class TestCapability:
    def test_toggle_off_is_off_whatever_the_install_can_do(self, caps) -> None:
        caps.record_granted_scopes({_AS_SCOPE})
        off = _build_slack_config({})
        assert not caps.agent_sessions_enabled(off)
        assert caps.available(_AS)

    def test_toggle_on_before_the_probe_is_off(self, caps) -> None:
        assert caps.availability(_AS) == (False, "scopes_unknown")
        assert not caps.agent_sessions_enabled(_on())

    def test_toggle_on_with_scopes_missing_is_off(self, caps, fake_switch: str) -> None:
        caps.record_granted_scopes({"chat:write"})
        assert caps.availability(_AS) == (False, "missing_scope")
        assert not caps.agent_sessions_enabled(_on())
        # The manifest still asks for the scope: it is how the install gains it.
        assert caps.manifest_features(_on()) == {_AS, fake_switch}

    def test_toggle_on_with_scopes_granted_is_on_independently(
        self, caps, fake_switch: str
    ) -> None:
        caps.record_granted_scopes({_FAKE_SCOPE})
        assert caps.enabled(_on(), fake_switch)
        assert not caps.agent_sessions_enabled(_on())

    @pytest.mark.parametrize(
        "error", ["feature_disabled", "missing_scope", "not_allowed_token_type"]
    )
    def test_a_refusal_latches_the_feature_off(self, caps, fake_switch: str, error: str) -> None:
        caps.record_granted_scopes({_FAKE_SCOPE, _AS_SCOPE})
        assert caps.latch_unavailable(_AS, error) is True
        assert not caps.agent_sessions_enabled(_on())
        assert caps.availability(_AS) == (False, error)
        assert caps.enabled(_on(), _FAKE)  # the other feature is untouched

    @pytest.mark.parametrize("error", ["ratelimited", "channel_not_found", "", "internal_error"])
    def test_other_errors_do_not_latch(self, caps, error: str) -> None:
        caps.record_granted_scopes({_AS_SCOPE})
        assert caps.latch_unavailable(_AS, error) is False
        assert caps.available(_AS)

    def test_latch_notifies_once_per_process(self, caps) -> None:
        seen: list[tuple[str, str]] = []
        caps.set_capability_notifier(lambda f, e: seen.append((f, e)))
        caps.latch_unavailable(_AS, "feature_disabled")
        caps.latch_unavailable(_AS, "feature_disabled")
        caps.clear_latch()  # a reconnect re-probes ...
        caps.latch_unavailable(_AS, "feature_disabled")  # ... and re-latches quietly
        assert seen == [(_AS, "feature_disabled")]

    def test_a_reinstall_lifts_a_missing_scope_latch(self, caps) -> None:
        caps.record_granted_scopes({"chat:write"})
        caps.latch_unavailable(_AS, "missing_scope")
        caps.record_granted_scopes({"chat:write", _AS_SCOPE})
        assert caps.available(_AS)

    def test_an_unregistered_feature_never_latches(self, caps) -> None:
        assert caps.latch_unavailable("not_registered", "feature_disabled") is False
        assert caps.latched() == {}

    def test_feature_disabled_survives_a_scope_refresh(self, caps) -> None:
        caps.record_granted_scopes({_AS_SCOPE})
        caps.latch_unavailable(_AS, "feature_disabled")
        caps.record_granted_scopes({_AS_SCOPE})
        assert caps.availability(_AS) == (False, "feature_disabled")


class TestConnectProbe:
    def _run(self, client: _Client) -> None:
        import asyncio

        from kiro_crew.slack import scope_probe

        asyncio.run(scope_probe.refresh_capabilities(client))

    def test_reads_granted_scopes_from_auth_test(self, caps) -> None:
        client = _Client(f"chat:write,{_AS_SCOPE}")
        self._run(client)
        assert caps.available(_AS)
        assert caps.granted_scopes() == {"chat:write", _AS_SCOPE}
        # Agent sessions have no read-only probe: auth.test is the only call.
        assert client.calls == ["auth.test"]

    def test_a_missing_scope_reads_as_missing(self, caps) -> None:
        self._run(_Client("chat:write"))
        assert caps.availability(_AS) == (False, "missing_scope")

    def test_unreadable_scopes_leave_it_off(self, caps) -> None:
        self._run(_Client(None))
        assert caps.availability(_AS) == (False, "scopes_unknown")

    def test_wrong_token_type_latches_every_registered_feature(self, caps, fake_switch) -> None:
        self._run(_Client("", auth_error="not_allowed_token_type"))
        assert caps.availability(fake_switch) == (False, "not_allowed_token_type")
        assert caps.availability(_AS) == (False, "not_allowed_token_type")

    @pytest.mark.parametrize("error", ["ratelimited", "internal_error", "fatal_error"])
    def test_a_probe_that_fails_keeps_the_scopes_last_read(self, caps, error: str) -> None:
        # A reconnect whose auth.test fails for a reason other than the token must
        # not switch agent sessions off in the middle of a turn.
        caps.record_granted_scopes({_AS_SCOPE})
        self._run(_Client("", auth_error=error))
        assert caps.available(_AS)
        assert caps.granted_scopes() == {_AS_SCOPE}

    def test_a_probe_that_cannot_reach_slack_keeps_the_scopes_last_read(self, caps) -> None:
        class _Down(_Client):
            async def auth_test(self) -> _Resp:
                raise ConnectionError("offline")

        caps.record_granted_scopes({_AS_SCOPE})
        self._run(_Down(""))
        assert caps.available(_AS)

    @pytest.mark.parametrize("error", ["invalid_auth", "token_revoked", "account_inactive"])
    def test_a_refused_token_forgets_the_scopes(self, caps, error: str) -> None:
        caps.record_granted_scopes({_AS_SCOPE})
        self._run(_Client("", auth_error=error))
        assert caps.availability(_AS) == (False, "scopes_unknown")

    def test_a_reconnect_clears_old_latches_and_re_probes(self, caps) -> None:
        caps.record_granted_scopes({_AS_SCOPE})
        caps.latch_unavailable(_AS, "missing_scope")
        self._run(_Client(_AS_SCOPE))
        assert caps.available(_AS)


class TestReconnectHook:
    def test_every_hello_re_probes_and_other_messages_do_not(self, caps, monkeypatch) -> None:
        import asyncio

        from kiro_crew.slack import events, scope_probe

        calls: list[object] = []

        async def _probe(client: object) -> None:
            calls.append(client)

        monkeypatch.setattr(scope_probe, "refresh_capabilities", _probe)
        orch = SimpleNamespace(
            dashboard_state=None, _socket_client=SimpleNamespace(message_listeners=[])
        )
        web = object()
        events._wire_slack_code_capability_probe(orch, web)  # type: ignore[arg-type]
        (listener,) = orch._socket_client.message_listeners

        async def _drive() -> None:
            await listener(None, {"type": "hello"}, "")
            await listener(None, {"type": "events_api"}, "")
            await listener(None, {"type": "hello"}, "")  # a reconnect
            await async_wait_until(lambda: len(calls) == 2, describe=lambda: calls)

        asyncio.run(_drive())
        assert calls == [web, web]
