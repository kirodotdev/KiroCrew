"""``kiro_crew.slack.features``: the Slack Code switches and code-channel defaults.

Pins the shared contract other work codes against: the switch readers fail
closed, the defaults normalize, and the resolution order for a new code-channel
session is explicit default > folder > Slack's agent (agent) and folder > origin
thread (project).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.section_builders import _build_slack_config
from kiro_crew.slack import features
from kiro_crew.testing.wait import async_wait_until

_FOLDERS = [
    {"id": "aaaaaaaaaaaa", "name": "Dev", "parent_id": "", "project_dir": "/repo/dev"},
    {
        "id": "bbbbbbbbbbbb",
        "name": "Slack Code",
        "parent_id": "aaaaaaaaaaaa",
        "default_agent": "builder",
    },
    {"id": "cccccccccccc", "name": "Twin", "parent_id": ""},
    {"id": "dddddddddddd", "name": "twin", "parent_id": ""},
]


#: A second switch registered only for the test, the way a later PR registers
#: one: an entry in ``slack_manifest.FEATURE_SCOPES``. Nothing else in
#: ``features`` names a switch, so registering it is all the reader needs.
_FAKE = "fake_switch"
_FAKE_SCOPE = "fake:write"


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
        assert features.code_channels_enabled(cfg) is False
        assert features.code_channel_defaults(cfg) == features.CodeChannelDefaults()
        assert features.code_channel_defaults(cfg).is_empty

    @pytest.mark.parametrize("raw", ["true", 1, "yes", None, [], {}])
    def test_only_a_real_true_turns_a_switch_on(self, raw: object) -> None:
        slack = _build_slack_config({"code_channels": raw})
        assert features.code_channels_toggle(slack) is False
        assert features.toggle(_slack(code_channels=raw), "code_channels") is False

    def test_true_turns_each_switch_on_independently(self, fake_switch: str) -> None:
        slack = _slack(code_channels=True, fake_switch=False)
        assert features.code_channels_toggle(slack) is True
        assert features.toggle(slack, fake_switch) is False
        assert features.manifest_features(slack) == {"code_channels"}
        assert features.manifest_features(_slack(fake_switch=True)) == {fake_switch}

    def test_an_unregistered_switch_is_off_even_when_set(self) -> None:
        assert features.toggle(_slack(not_registered=True), "not_registered") is False
        assert features.manifest_features(_slack(not_registered=True)) == frozenset()

    def test_reader_accepts_full_config_or_slack_section(self) -> None:
        cfg = KiroCrewConfig()
        cfg.slack = _build_slack_config({"code_channels": True})
        assert features.code_channels_toggle(cfg) is True
        assert features.code_channels_toggle(cfg.slack) is True

    def test_a_reader_handed_something_unrelated_is_off(self) -> None:
        assert features.code_channels_toggle(object()) is False
        assert features.code_channels_enabled(object()) is False
        assert features.code_channel_defaults(object()) == features.CodeChannelDefaults()


class TestDefaultsNormalization:
    def test_strings_are_trimmed_and_blank_is_unset(self) -> None:
        slack = _build_slack_config(
            {
                "code_channel_defaults": {
                    "folder": "  Dev/Slack Code ",
                    "agent": "   ",
                    "model": "claude-opus-4.8",
                    "reasoning_effort": "high",
                }
            }
        )
        assert features.code_channel_defaults(slack) == features.CodeChannelDefaults(
            folder="Dev/Slack Code", agent=None, model="claude-opus-4.8", reasoning_effort="high"
        )

    @pytest.mark.parametrize("effort", ["HIGH", "extreme", "", 3, None])
    def test_effort_outside_the_ladder_is_unset(self, effort: object) -> None:
        slack = _build_slack_config({"code_channel_defaults": {"reasoning_effort": effort}})
        assert features.code_channel_defaults(slack).reasoning_effort is None

    @pytest.mark.parametrize("raw", ["Dev", 7, ["x"], None])
    def test_non_object_defaults_read_as_unset(self, raw: object) -> None:
        slack = _build_slack_config({"code_channel_defaults": raw})
        assert features.code_channel_defaults(slack).is_empty

    def test_over_long_value_is_unset(self) -> None:
        slack = _build_slack_config({"code_channel_defaults": {"agent": "a" * 257}})
        assert features.code_channel_defaults(slack).agent is None


class TestFindFolder:
    def test_by_id(self) -> None:
        assert features.find_folder(_FOLDERS, "bbbbbbbbbbbb")["name"] == "Slack Code"

    def test_by_path_case_and_space_insensitive(self) -> None:
        hit = features.find_folder(_FOLDERS, " dev / slack code ")
        assert hit is not None and hit["id"] == "bbbbbbbbbbbb"

    def test_ambiguous_path_matches_nothing(self) -> None:
        assert features.find_folder(_FOLDERS, "Twin") is None

    def test_dangling_id_never_falls_back_to_a_name(self) -> None:
        folders = [*_FOLDERS, {"id": "eeeeeeeeeeee", "name": "ffffffffffff", "parent_id": ""}]
        assert features.find_folder(folders, "ffffffffffff") is None

    def test_parent_cycle_does_not_hang(self) -> None:
        cyc = [
            {"id": "111111111111", "name": "A", "parent_id": "222222222222"},
            {"id": "222222222222", "name": "B", "parent_id": "111111111111"},
        ]
        assert features.find_folder(cyc, "nope") is None
        assert features.folder_project_dir(cyc, "111111111111") is None


class TestResolution:
    def test_explicit_default_agent_wins(self) -> None:
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(folder="bbbbbbbbbbbb", agent="reviewer"),
            _FOLDERS,
            slack_agent="slackbot",
        )
        assert (got.agent, got.agent_source) == ("reviewer", "default")

    def test_folder_agent_beats_slack_agent(self) -> None:
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(folder="Dev/Slack Code"), _FOLDERS, slack_agent="slackbot"
        )
        assert (got.agent, got.agent_source) == ("builder", "folder")
        assert got.folder_id == "bbbbbbbbbbbb"

    def test_slack_agent_is_the_last_agent_fallback(self) -> None:
        # The auto folder exists but its default agent was cleared.
        folders = [*_FOLDERS, {"id": "cccccccccccc", "name": "Code channels", "parent_id": ""}]
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(), folders, slack_agent="slackbot"
        )
        assert (got.agent, got.agent_source) == ("slackbot", "slack")

    def test_folder_project_is_inherited_from_ancestor_and_beats_origin(self) -> None:
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(folder="bbbbbbbbbbbb"),
            _FOLDERS,
            origin_project_dir="/repo/origin",
        )
        assert (got.project_dir, got.project_source) == ("/repo/dev", "folder")
        assert got.folder_path == "Dev/Slack Code"
        assert got.folder_auto_create is False

    def test_origin_project_when_no_folder_project(self) -> None:
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(), _FOLDERS, origin_project_dir="/repo/origin"
        )
        assert (got.project_dir, got.project_source) == ("/repo/origin", "origin")

    def test_nothing_configured_runs_the_conductor_in_the_auto_folder(self) -> None:
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(), [], slack_agent="slackbot"
        )
        assert got.folder_id is None
        assert got.folder_path == features.AUTO_FOLDER_NAME == "Code channels"
        assert got.folder_auto_create is True
        assert (got.agent, got.agent_source) == ("kirocrew-conductor", "folder")
        assert features.AUTO_FOLDER_AGENT == "kirocrew-conductor"
        assert (got.project_dir, got.folder_missing) == (None, False)

    def test_an_existing_auto_folder_is_used_and_not_recreated(self) -> None:
        folders = [
            *_FOLDERS,
            {
                "id": "cccccccccccc",
                "name": "code channels",
                "parent_id": "",
                "default_agent": "builder",
                "project_dir": "/repo/cc",
            },
        ]
        got = features.resolve_code_channel_session(features.CodeChannelDefaults(), folders)
        assert (got.folder_id, got.folder_auto_create) == ("cccccccccccc", False)
        assert got.folder_path == "code channels"
        # Its own settings apply once it exists, conductor or not.
        assert (got.agent, got.agent_source) == ("builder", "folder")
        assert (got.project_dir, got.project_source) == ("/repo/cc", "folder")

    def test_explicit_agent_beats_the_auto_folder_agent(self) -> None:
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(agent="reviewer"), []
        )
        assert (got.agent, got.agent_source) == ("reviewer", "default")
        assert got.folder_auto_create is True

    def test_a_nested_or_duplicated_auto_name_is_not_the_auto_folder(self) -> None:
        nested = [
            *_FOLDERS,
            {"id": "cccccccccccc", "name": "Code channels", "parent_id": "aaaaaaaaaaaa"},
        ]
        assert features.find_auto_folder(nested) is None
        twice = [
            {"id": "cccccccccccc", "name": "Code channels", "parent_id": ""},
            {"id": "dddddddddddd", "name": "Code Channels", "parent_id": ""},
        ]
        assert features.find_auto_folder(twice) is None
        assert len(features.auto_folder_matches(twice)) == 2
        # Through the resolver: two duplicates are not "missing", so nothing is
        # created and the session is unfiled with Slack's agent, not filed into
        # one duplicate as if it were a fresh conductor folder.
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(), twice, slack_agent="slackbot"
        )
        assert (got.folder_id, got.folder_path, got.folder_auto_create) == (None, None, False)
        assert (got.agent, got.agent_source) == ("slackbot", "slack")
        # A nested folder of that name is not the auto folder, so it is still created.
        got = features.resolve_code_channel_session(features.CodeChannelDefaults(), nested)
        assert got.folder_auto_create is True

    def test_deleted_folder_is_flagged_and_other_defaults_still_apply(self) -> None:
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(folder="999999999999", model="m1", reasoning_effort="low"),
            _FOLDERS,
            slack_agent="slackbot",
            origin_project_dir="/repo/origin",
        )
        assert got.folder_missing is True
        assert got.folder_id is None
        assert (got.folder_path, got.folder_auto_create) == (None, False)
        assert (got.agent_source, got.project_source) == ("slack", "origin")
        assert (got.model, got.reasoning_effort) == ("m1", "low")


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
    def __init__(self, scopes: str | None, *, auth_error: str = "", views_error: str = "") -> None:
        self.scopes, self.auth_error, self.views_error = scopes, auth_error, views_error
        self.calls: list[str] = []

    async def auth_test(self) -> _Resp:
        self.calls.append("auth.test")
        if self.auth_error:
            raise _SlackErr(self.auth_error)
        headers = {} if self.scopes is None else {"x-oauth-scopes": self.scopes}
        return _Resp({"ok": True}, headers)

    async def api_call(self, method: str, **kw: object) -> _Resp:
        self.calls.append(method)
        if self.views_error:
            raise _SlackErr(self.views_error)
        return _Resp({"ok": True})


@pytest.fixture()
def caps():
    features.reset_capabilities()
    features.set_capability_notifier(None)
    yield features
    features.reset_capabilities()
    features.set_capability_notifier(None)


def _on():
    return _slack(code_channels=True, fake_switch=True)


class TestCapability:
    def test_toggle_off_is_off_whatever_the_install_can_do(self, caps) -> None:
        caps.record_granted_scopes({"code_channels:manage"})
        off = _build_slack_config({})
        assert not caps.code_channels_enabled(off)
        assert caps.code_channels_available()

    def test_toggle_on_before_the_probe_is_off(self, caps) -> None:
        assert caps.availability("code_channels") == (False, "scopes_unknown")
        assert not caps.code_channels_enabled(_on())

    def test_toggle_on_with_scopes_missing_is_off(self, caps, fake_switch: str) -> None:
        caps.record_granted_scopes({"chat:write"})
        assert caps.availability("code_channels") == (False, "missing_scope")
        assert not caps.code_channels_enabled(_on())
        # The manifest still asks for the scope: it is how the install gains it.
        assert caps.manifest_features(_on()) == {"code_channels", fake_switch}

    def test_toggle_on_with_scopes_granted_is_on_independently(
        self, caps, fake_switch: str
    ) -> None:
        caps.record_granted_scopes({_FAKE_SCOPE})
        assert caps.enabled(_on(), fake_switch)
        assert not caps.code_channels_enabled(_on())

    @pytest.mark.parametrize(
        "error", ["feature_disabled", "missing_scope", "not_allowed_token_type"]
    )
    def test_a_refusal_latches_the_feature_off(self, caps, fake_switch: str, error: str) -> None:
        caps.record_granted_scopes({_FAKE_SCOPE, "code_channels:manage"})
        assert caps.latch_unavailable("code_channels", error) is True
        assert not caps.code_channels_enabled(_on())
        assert caps.availability("code_channels") == (False, error)
        assert caps.enabled(_on(), _FAKE)  # the other feature is untouched

    @pytest.mark.parametrize("error", ["ratelimited", "channel_not_found", "", "internal_error"])
    def test_other_errors_do_not_latch(self, caps, error: str) -> None:
        caps.record_granted_scopes({"code_channels:manage"})
        assert caps.latch_unavailable("code_channels", error) is False
        assert caps.code_channels_available()

    def test_latch_notifies_once_per_process(self, caps) -> None:
        seen: list[tuple[str, str]] = []
        caps.set_capability_notifier(lambda f, e: seen.append((f, e)))
        caps.latch_unavailable("code_channels", "feature_disabled")
        caps.latch_unavailable("code_channels", "feature_disabled")
        caps.clear_latch()  # a reconnect re-probes ...
        caps.latch_unavailable("code_channels", "feature_disabled")  # ... and re-latches quietly
        assert seen == [("code_channels", "feature_disabled")]

    def test_a_reinstall_lifts_a_missing_scope_latch(self, caps) -> None:
        caps.record_granted_scopes({"chat:write"})
        caps.latch_unavailable("code_channels", "missing_scope")
        caps.record_granted_scopes({"chat:write", "code_channels:manage"})
        assert caps.code_channels_available()

    def test_an_unregistered_feature_never_latches(self, caps) -> None:
        assert caps.latch_unavailable("not_registered", "feature_disabled") is False
        assert caps.latched() == {}

    def test_feature_disabled_survives_a_scope_refresh(self, caps) -> None:
        caps.record_granted_scopes({"code_channels:manage"})
        caps.latch_unavailable("code_channels", "feature_disabled")
        caps.record_granted_scopes({"code_channels:manage"})
        assert caps.availability("code_channels") == (False, "feature_disabled")


class TestConnectProbe:
    def _run(self, client: _Client) -> None:
        import asyncio

        from kiro_crew.slack import scope_probe

        asyncio.run(scope_probe.refresh_capabilities(client, probe_code_channels=True))

    def test_reads_granted_scopes_from_auth_test(self, caps) -> None:
        self._run(_Client("chat:write,code_channels:manage", views_error="not_agent_channel"))
        assert caps.code_channels_available()
        assert caps.granted_scopes() == {"chat:write", "code_channels:manage"}

    def test_workspace_without_code_channels_latches_them(self, caps) -> None:
        seen: list[tuple[str, str]] = []
        caps.set_capability_notifier(lambda f, e: seen.append((f, e)))
        self._run(_Client("code_channels:manage", views_error="feature_disabled"))
        assert caps.availability("code_channels") == (False, "feature_disabled")
        assert seen == [("code_channels", "feature_disabled")]

    def test_no_feature_probe_without_the_scope(self, caps) -> None:
        client = _Client("chat:write", views_error="feature_disabled")
        self._run(client)
        assert client.calls == ["auth.test"]
        assert caps.availability("code_channels") == (False, "missing_scope")

    def test_unreadable_scopes_leave_it_off(self, caps) -> None:
        self._run(_Client(None))
        assert caps.availability("code_channels") == (False, "scopes_unknown")

    def test_wrong_token_type_latches_every_registered_feature(self, caps, fake_switch) -> None:
        self._run(_Client("", auth_error="not_allowed_token_type"))
        assert caps.availability(fake_switch) == (False, "not_allowed_token_type")
        assert caps.availability("code_channels") == (False, "not_allowed_token_type")

    def test_a_reconnect_clears_old_latches_and_re_probes(self, caps) -> None:
        caps.record_granted_scopes({"code_channels:manage"})
        caps.latch_unavailable("code_channels", "missing_scope")
        self._run(_Client("code_channels:manage", views_error="not_agent_channel"))
        assert caps.code_channels_available()


class TestReconnectHook:
    def test_every_hello_re_probes_and_other_messages_do_not(self, caps, monkeypatch) -> None:
        import asyncio
        from types import SimpleNamespace

        from kiro_crew.slack import events, scope_probe

        calls: list[object] = []

        async def _probe(client: object, *, probe_code_channels: bool = False) -> None:
            calls.append(client)
            probes.append(probe_code_channels)

        probes: list[bool] = []
        cfg = KiroCrewConfig()
        cfg.slack = _build_slack_config({"code_channels": True})
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
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
            # Each probe reads the live config off the loop first.
            await async_wait_until(lambda: len(calls) == 2, describe=lambda: calls)

        asyncio.run(_drive())
        assert calls == [web, web]
        # The workspace-feature probe follows the live Code channels toggle.
        assert probes == [True, True]
        cfg.slack = _build_slack_config({})
        calls.clear()
        probes.clear()
        asyncio.run(_drive())
        assert probes == [False, False]
