"""The toggle-aware Slack manifest: gating, emitters, requirements and the exfil exemption.

Every Slack Code switch combination must render a clean manifest (no gate
markers), every emitter must agree on it, the security redactor must exempt the
deep link of every combination exactly -- and nothing that mixes combinations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from urllib.parse import quote

import pytest
import yaml
from aiohttp.test_utils import make_mocked_request

from kiro_crew import cli_setup, slack_manifest
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.section_builders import _build_slack_config
from kiro_crew.security import scan_exfiltration_urls
from kiro_crew.slack.features import manifest_features

COMBOS = slack_manifest.feature_sets()
_CC = frozenset({slack_manifest.CODE_CHANNELS})
_AS = frozenset({slack_manifest.AGENT_SESSIONS})
#: A switch registered only for the test, the way a later PR registers one: an
#: entry in ``FEATURE_SCOPES`` plus its ``#@<name>`` lines in the template.
_FAKE = "fake_switch"
_FAKE_LINES = {
    "      - code_channels:manage  #@code_channels\n": (
        "      - code_channels:manage  #@code_channels\n" "      - fake:write  #@fake_switch\n"
    ),
    "      - agent_session_stopped  #@agent_sessions|code_channels\n": (
        "      - agent_session_stopped  #@agent_sessions|code_channels|fake_switch\n"
        "      - fake_event  #@fake_switch\n"
    ),
}


@pytest.fixture
def fake_switch(monkeypatch: pytest.MonkeyPatch) -> str:
    """Register ``fake_switch`` exactly as a new switch would be registered."""
    from kiro_crew.security import exfil

    raw = slack_manifest.raw_template()
    for old, new in _FAKE_LINES.items():
        assert old in raw, old
        raw = raw.replace(old, new)
    monkeypatch.setattr(slack_manifest, "raw_template", lambda: raw)
    monkeypatch.setitem(slack_manifest.FEATURE_SCOPES, _FAKE, "fake:write")
    monkeypatch.setattr(
        slack_manifest,
        "FEATURES",
        (*slack_manifest.FEATURE_SCOPES, slack_manifest.SLACK_MCP),
    )
    monkeypatch.setattr(exfil, "_slack_manifest_re_slot", [])
    return _FAKE


def _cfg(**slack: object) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.slack = _build_slack_config(dict(slack))
    return cfg


class TestRendering:
    def test_one_rendering_per_combination(self) -> None:
        assert len(COMBOS) == 2 ** len(slack_manifest.FEATURES)
        assert len({slack_manifest.render("x", features=c) for c in COMBOS}) == len(COMBOS)

    def test_features_are_the_registry(self) -> None:
        assert slack_manifest.FEATURES == (
            *slack_manifest.FEATURE_SCOPES,
            slack_manifest.SLACK_MCP,
        )
        # The Slack MCP switch has no bot scope, so the capability probe, the
        # latches and the runtime gate (all keyed on FEATURE_SCOPES) never see it.
        assert slack_manifest.SLACK_MCP not in slack_manifest.FEATURE_SCOPES
        assert slack_manifest.FEATURES == (
            slack_manifest.AGENT_SESSIONS,
            slack_manifest.CODE_CHANNELS,
            slack_manifest.SLACK_MCP,
        )
        # agent_view is one-way, so only the Agent sessions switch declares it.
        assert "agent_view" not in slack_manifest.render("x", features=_CC)
        assert "agent_view" in slack_manifest.render("x", features=_AS)

    @pytest.mark.parametrize("combo", COMBOS)
    def test_no_gate_survives_rendering(self, combo: frozenset[str]) -> None:
        for strip in (False, True):
            text = slack_manifest.render("someone", strip_comments=strip, features=combo)
            assert "#@" not in text and "##" not in text
            assert isinstance(yaml.safe_load(text), dict)

    def test_switches_off_keeps_the_pre_toggle_payload(self) -> None:
        # The stripped payload with no switch must not carry any Slack Code line:
        # an existing install's deep link stays the manifest it was created from.
        text = slack_manifest.stripped_template()
        for marker in (
            "agent_view",
            "code_channels",
            "assistant:write",
            "agent_session",
            "search:read.",
            "is_mcp_enabled",
            "pkce_enabled",
            "redirect_urls",
        ):
            assert marker not in text
        # Byte-pinned: the digest of the payload before the manifest became
        # toggle-aware. A change here changes every default install's deep link.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert digest == "bca5f538a73e541d77d4b00dbc5a39b48b499baa514358b3474010c36c984d11"

    def test_an_off_gate_renders_only_while_its_switch_is_off(self) -> None:
        # ``#@!slack_mcp`` marks a line the switch replaces: the legacy
        # ``search:read`` is in every manifest without the switch and none with.
        raw = slack_manifest.raw_template()
        assert "      - search:read  #@!slack_mcp\n" in raw
        for combo in COMBOS:
            text = slack_manifest.render("x", features=combo)
            assert ("      - search:read\n" in text) is (slack_manifest.SLACK_MCP not in combo)
            assert "#@" not in text

    def test_unknown_feature_names_are_ignored(self) -> None:
        assert slack_manifest.render("x", features={"bogus"}) == slack_manifest.render("x")

    def test_requirements_and_diff(self) -> None:
        change = slack_manifest.diff_requirements(
            slack_manifest.requirements(frozenset()), slack_manifest.requirements(_CC)
        )
        assert change.added_features == ("code_channels",)
        assert change.added_scopes == (
            "canvases:write",
            "channels:write.invites",
            "chat:write.customize",
            "code_channels:manage",
            "groups:write.invites",
            "pins:write",
        )
        assert change.added_events == (
            "agent_session_stopped",
            "channel_archive",
            "channel_rename",
            "channel_unarchive",
            "group_archive",
            "group_rename",
            "group_unarchive",
        )
        assert change.removed_scopes == () and not change.empty
        back = slack_manifest.diff_requirements(
            slack_manifest.requirements(_CC), slack_manifest.requirements(frozenset())
        )
        assert back.removed_features == ("code_channels",)
        assert slack_manifest.diff_requirements(
            slack_manifest.requirements(_CC), slack_manifest.requirements(_CC)
        ).empty

    def test_agent_sessions_requirements(self) -> None:
        """The agent view, its one scope and the two session events, nothing more."""
        change = slack_manifest.diff_requirements(
            slack_manifest.requirements(frozenset()), slack_manifest.requirements(_AS)
        )
        assert change.added_features == ("agent_view",)
        assert change.added_scopes == ("assistant:write",)
        assert change.added_events == ("agent_session_stopped", "agent_session_title_changed")
        both = slack_manifest.diff_requirements(
            slack_manifest.requirements(_CC), slack_manifest.requirements(_CC | _AS)
        )
        # Code channels already subscribe the Stop event; the title event is new.
        assert both.added_events == ("agent_session_title_changed",)
        assert "app_context_changed" not in slack_manifest.render("x", features=_AS)

    @pytest.mark.parametrize(
        "app_id,ok", [("A0123ABCD", True), ("a0123abcd", False), ("A1/../x", False), ("", False)]
    )
    def test_app_manifest_url(self, app_id: str, ok: bool) -> None:
        url = slack_manifest.app_manifest_url(app_id)
        assert (url is not None) is ok
        if ok:
            assert url == f"https://api.slack.com/apps/{app_id}/app-manifest"


class TestEmittersAgree:
    @pytest.mark.parametrize(
        "slack,expected",
        [
            ({}, frozenset()),
            ({"code_channels": True}, _CC),
            ({"code_channels": "true"}, frozenset()),
        ],
    )
    def test_config_to_features(self, slack: dict, expected: frozenset[str]) -> None:
        assert manifest_features(_cfg(**slack)) == expected

    @pytest.mark.parametrize("slack", [{}, {"code_channels": True}])
    def test_cli_and_endpoint_render_the_same_bytes(
        self, slack: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import kiro_crew.dashboard.handlers.messaging as mod

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: _cfg(**slack)))
        cli_setup._manifest(alias="someone")
        cli_out = capsys.readouterr().out
        resp = asyncio.run(
            mod.api_slack_manifest(make_mocked_request("GET", "/api/slack/manifest?alias=someone"))
        )
        body = json.loads(resp.text)
        assert cli_out.rstrip("\n") == body["manifest"].rstrip("\n")
        assert body["features"] == [
            f for f in slack_manifest.FEATURES if f in manifest_features(_cfg(**slack))
        ]
        expected = slack_manifest.deep_link("someone", manifest_features(_cfg(**slack)))
        assert body["create_url"] == expected


class TestExfilExemption:
    @pytest.mark.parametrize("combo", COMBOS)
    def test_every_combination_link_is_exempt(self, combo: frozenset[str]) -> None:
        url = slack_manifest.deep_link("someone", combo)
        assert len(url.split("?", 1)[1]) >= 200
        assert scan_exfiltration_urls(url) == []

    def test_a_variant_that_fails_to_build_costs_only_its_own_link(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.security import exfil

        real = slack_manifest.stripped_template

        def broken_for_code_channels(features: frozenset[str] = frozenset()) -> str:
            if slack_manifest.CODE_CHANNELS in features:
                raise OSError("variant unreadable")
            return real(features)

        monkeypatch.setattr(exfil, "_slack_manifest_re_slot", [])
        monkeypatch.setattr(slack_manifest, "stripped_template", broken_for_code_channels)
        # The stock link keeps its exemption; the one variant that did not
        # build is refused rather than accepted by a looser pattern.
        assert scan_exfiltration_urls(slack_manifest.deep_link("someone", frozenset())) == []
        monkeypatch.setattr(slack_manifest, "stripped_template", real)
        assert scan_exfiltration_urls(slack_manifest.deep_link("someone", _CC)) != []

    def test_a_mixed_combination_is_redacted(self) -> None:
        # The code-channels feature block WITHOUT its scope: no emitter renders
        # this, so the validator must not accept it even though every line is
        # legitimate.
        on = slack_manifest.stripped_template(_CC).splitlines()
        tampered = "\n".join(ln for ln in on if "code_channels:manage" not in ln) + "\n"
        payload = tampered.replace(slack_manifest.ALIAS_PLACEHOLDER, "someone").replace(
            slack_manifest.COMMAND_PLACEHOLDER, slack_manifest.slash_command("someone")
        )
        url = "https://api.slack.com/apps?new_app=1&manifest_yaml=" + quote(payload, safe="")
        assert scan_exfiltration_urls(url) != []

    def test_a_gate_marker_smuggled_into_the_payload_is_redacted(self) -> None:
        payload = slack_manifest.render("someone", strip_comments=True, features=_CC).replace(
            "code_channels:manage", "code_channels:manage  #@code_channels"
        )
        url = "https://api.slack.com/apps?new_app=1&manifest_yaml=" + quote(payload, safe="")
        assert scan_exfiltration_urls(url) != []


class TestExistingAppKeepsItsCommand:
    @pytest.mark.parametrize(
        "command,alias",
        [
            ("kirocrew", "kirocrew"),
            ("kirocrew-jdoe", "jdoe"),
            ("crew", "kirocrew"),
            ("kirocrew-", "kirocrew"),
        ],
    )
    def test_alias_for_command(self, command: str, alias: str) -> None:
        assert slack_manifest.alias_for_command(command) == alias

    def test_render_takes_the_saved_command_and_ignores_a_bad_one(self) -> None:
        assert "/crew\n" in slack_manifest.render("kirocrew", command="crew")
        assert "/kirocrew\n" in slack_manifest.render("kirocrew", command="Bad Command!")

    @pytest.mark.parametrize(
        "saved,name,slash",
        [
            ("kirocrew-jdoe", "KiroCrew-jdoe", "/kirocrew-jdoe"),  # brand-ok: manifest app name
            ("crew", "KiroCrew-kirocrew", "/crew"),  # brand-ok: manifest app name
        ],
    )
    def test_endpoint_renders_for_this_install(
        self, saved: str, name: str, slash: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.dashboard.handlers.messaging as mod

        cfg = _cfg(command=saved, code_channels=True)
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
        resp = asyncio.run(
            mod.api_slack_manifest(make_mocked_request("GET", "/api/slack/manifest"))
        )
        body = json.loads(resp.text)
        parsed = yaml.safe_load(body["manifest"])
        assert parsed["display_information"]["name"] == name
        assert parsed["features"]["slash_commands"][0]["command"] == slash
        assert body["command"] == slash.lstrip("/")
        assert "code_channels" in parsed["features"]
        # The create-app deep link stays one the exfil validator exempts.
        assert scan_exfiltration_urls(body["create_url"]) == []


class TestRegisteringASwitch:
    """A new switch needs only its registry entry, its template lines and its
    config field: every consumer below follows without a code change."""

    def test_render_and_combinations_follow(self, fake_switch: str) -> None:
        on = frozenset({fake_switch})
        combos = slack_manifest.feature_sets()
        assert combos[0] == frozenset()
        assert len(combos) == 2**4 == len(set(combos))
        assert {f for combo in combos for f in combo} == {
            slack_manifest.AGENT_SESSIONS,
            slack_manifest.CODE_CHANNELS,
            slack_manifest.SLACK_MCP,
            fake_switch,
        }
        text = slack_manifest.render("x", features=on)
        assert "fake:write" in text and "fake_event" in text and "#@" not in text
        assert "fake:write" not in slack_manifest.render("x", features=_CC)

    def test_requirements_follow(self, fake_switch: str) -> None:
        change = slack_manifest.diff_requirements(
            slack_manifest.requirements(frozenset()),
            slack_manifest.requirements(frozenset({fake_switch})),
        )
        assert change.added_scopes == ("fake:write",)
        assert change.added_events == ("agent_session_stopped", "fake_event")

    def test_the_exfil_exemption_follows(self, fake_switch: str) -> None:
        for combo in slack_manifest.feature_sets():
            assert scan_exfiltration_urls(slack_manifest.deep_link("someone", combo)) == []

    def test_config_and_the_settings_page_follow(self, fake_switch: str) -> None:
        from types import SimpleNamespace

        from kiro_crew.dashboard import slack_code_settings as scs

        cfg = SimpleNamespace(
            slack=SimpleNamespace(agent_sessions=False, code_channels=False, fake_switch=True)
        )
        assert manifest_features(cfg) == {fake_switch}
        changes = scs.feature_changes()
        assert set(changes) == {"agent_sessions", "code_channels", "slack_mcp", fake_switch}
        assert changes[fake_switch]["added_scopes"] == ["fake:write"]
        view = asyncio.run(scs.code_settings_view(cfg, None))
        assert view[fake_switch] is True and view["code_channels"] is False
        assert view[f"{fake_switch}_capability"]["scope"] == "fake:write"


#: The read-only user scopes the Slack MCP server needs, and nothing that writes.
#: The Slack Connections card asks for the same list (its ``recommended_scopes``).
_MCP_USER_SCOPES = (
    "search:read.public",
    "search:read.private",
    "search:read.mpim",
    "search:read.im",
    "search:read.files",
    "search:read.users",
    "channels:history",
    "groups:history",
    "mpim:history",
    "im:history",
    "channels:read",
    "groups:read",
    "im:read",
    "mpim:read",
    "users:read",
    "files:read",
    "canvases:read",
    "lists:read",
)
_MCP = frozenset({slack_manifest.SLACK_MCP})
_MCP_REDIRECT = "http://localhost:48106/callback"
#: The user scopes every manifest carried before the Slack MCP switch, in the
#: template's order. With the switch off the manifest still lists exactly these.
_LEGACY_USER_SCOPES = (
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "mpim:history",
    "mpim:read",
    "search:read",
    "users:read",
)


class TestSlackMcpSwitch:
    """Off: the manifest as it was before the switch existed, its user scopes
    included. On: exactly the read-only MCP user scopes, plus the MCP settings."""

    @pytest.mark.parametrize("combo", COMBOS)
    @pytest.mark.parametrize("strip", [False, True])
    def test_the_switch_alone_decides_the_mcp_lines(
        self, combo: frozenset[str], strip: bool
    ) -> None:
        parsed = yaml.safe_load(
            slack_manifest.render("someone", strip_comments=strip, features=combo)
        )
        oauth, settings = parsed["oauth_config"], parsed["settings"]
        user = oauth["scopes"]["user"]
        assert len(user) == len(set(user))  # the two lists never render together
        if slack_manifest.SLACK_MCP in combo:
            assert sorted(user) == sorted(_MCP_USER_SCOPES)
            assert oauth["pkce_enabled"] is True
            assert oauth["redirect_urls"] == [_MCP_REDIRECT]
            assert settings["is_mcp_enabled"] is True
        else:
            assert user == list(_LEGACY_USER_SCOPES)
            assert "pkce_enabled" not in oauth and "redirect_urls" not in oauth
            assert "is_mcp_enabled" not in settings

    def test_on_replaces_the_older_user_scope_list(self) -> None:
        # Nine scopes are in both lists; the switch drops the legacy
        # ``search:read`` and adds the nine the Slack MCP server needs.
        legacy, mcp = set(_LEGACY_USER_SCOPES), set(_MCP_USER_SCOPES)
        assert legacy - mcp == {"search:read"}
        assert len(mcp - legacy) == 9
        on = slack_manifest.requirements(_MCP).user_scopes
        assert "search:read" not in on and on == frozenset(mcp)

    def test_no_write_user_scope(self) -> None:
        user = slack_manifest.requirements(frozenset(slack_manifest.FEATURES)).user_scopes
        assert user == frozenset(_MCP_USER_SCOPES)
        assert not [s for s in user if ":write" in s or s.endswith(":manage")]

    def test_it_changes_no_bot_scope_event_or_feature(self) -> None:
        for combo in COMBOS:
            if slack_manifest.SLACK_MCP in combo:
                continue
            change = slack_manifest.diff_requirements(
                slack_manifest.requirements(combo), slack_manifest.requirements(combo | _MCP)
            )
            assert change.added_scopes == change.removed_scopes == ()
            assert change.added_events == change.removed_events == ()
            assert change.added_features == change.removed_features == ()
            assert change.added_user_scopes == tuple(
                sorted(set(_MCP_USER_SCOPES) - set(_LEGACY_USER_SCOPES))
            )
            # The one legacy scope the on-state drops is reported as removed,
            # so the update panel can show it leaving the manifest.
            assert change.removed_user_scopes == ("search:read",)
            assert change.added_settings == (
                "oauth_config.pkce_enabled: true",
                f"oauth_config.redirect_urls: {_MCP_REDIRECT}",
                "settings.is_mcp_enabled: true",
            )
            back = slack_manifest.diff_requirements(
                slack_manifest.requirements(combo | _MCP), slack_manifest.requirements(combo)
            )
            assert back.added_user_scopes == ("search:read",)
            assert back.removed_user_scopes == change.added_user_scopes
            assert back.added_settings == () and not back.empty

    def test_the_manifest_matches_the_slack_connections_card(self) -> None:
        """The rendered manifest asks Slack for exactly what the Slack Connections
        card signs in with, so one Slack app serves both: the card's own scope
        list, its redirect URL, and PKCE. Read from the registry entry itself,
        so a change on either side fails here."""
        from kiro_crew.connections import registry

        card = registry.get_provider("slack")
        assert card is not None, "the Slack Connections entry is gone"
        oauth = yaml.safe_load(slack_manifest.render("someone", features=_MCP))["oauth_config"]
        # Same set (Slack reads no meaning into the order; the template keeps
        # the order the off-state manifest already had).
        assert sorted(oauth["scopes"]["user"]) == sorted(card["recommended_scopes"])
        assert oauth["redirect_urls"] == [registry.redirect_uri(card)]
        assert oauth["pkce_enabled"] is card["l0_expectations"]["pkce"] is True
        # The pinned copies in this file say the same thing.
        assert list(_MCP_USER_SCOPES) == card["recommended_scopes"]
        assert _MCP_REDIRECT == registry.redirect_uri(card)

    @pytest.mark.parametrize(
        "slack,on",
        [
            ({}, False),
            ({"slack_mcp": True}, True),
            ({"slack_mcp": "true"}, False),
            ({"slack_mcp": 1}, False),
            ({"slack_mcp": False, "code_channels": True, "agent_sessions": True}, False),
            ({"slack_mcp": True, "code_channels": True, "agent_sessions": True}, True),
        ],
    )
    def test_only_a_real_true_turns_it_on(self, slack: dict, on: bool) -> None:
        assert (slack_manifest.SLACK_MCP in manifest_features(_cfg(**slack))) is on

    @pytest.mark.parametrize(
        "slack", [{"slack_mcp": True}, {"slack_mcp": True, "code_channels": True}]
    )
    def test_cli_and_endpoint_render_the_same_bytes(
        self, slack: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import kiro_crew.dashboard.handlers.messaging as mod

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: _cfg(**slack)))
        cli_setup._manifest(alias="someone")
        cli_out = capsys.readouterr().out
        resp = asyncio.run(
            mod.api_slack_manifest(make_mocked_request("GET", "/api/slack/manifest?alias=someone"))
        )
        body = json.loads(resp.text)
        assert cli_out.rstrip("\n") == body["manifest"].rstrip("\n")
        assert slack_manifest.SLACK_MCP in body["features"]
        assert yaml.safe_load(body["manifest"])["settings"]["is_mcp_enabled"] is True
        assert scan_exfiltration_urls(body["create_url"]) == []

    @pytest.mark.parametrize("alias", ["someone", "kirocrew", "A_b-9", "x", "a" * 32])
    def test_every_alias_link_is_exempt_with_it_on(self, alias: str) -> None:
        for combo in COMBOS:
            if slack_manifest.SLACK_MCP in combo:
                assert scan_exfiltration_urls(slack_manifest.deep_link(alias, combo)) == []

    @pytest.mark.parametrize(
        "drop", ["is_mcp_enabled", "pkce_enabled", "search:read.public", "localhost:48106"]
    )
    def test_a_partial_mcp_payload_is_redacted(self, drop: str) -> None:
        # Part of the MCP lines without the rest: no emitter renders it, so the
        # validator must not accept it.
        on = slack_manifest.stripped_template(_MCP).splitlines()
        tampered = "\n".join(ln for ln in on if drop not in ln) + "\n"
        payload = tampered.replace(slack_manifest.ALIAS_PLACEHOLDER, "someone").replace(
            slack_manifest.COMMAND_PLACEHOLDER, slack_manifest.slash_command("someone")
        )
        url = "https://api.slack.com/apps?new_app=1&manifest_yaml=" + quote(payload, safe="")
        assert scan_exfiltration_urls(url) != []

    def test_a_widened_mcp_payload_is_redacted(self) -> None:
        exact = slack_manifest.render("someone", strip_comments=True, features=_MCP)
        payload = exact.replace("      - lists:read\n", "      - lists:read\n      - chat:write\n")
        assert payload != exact
        url = "https://api.slack.com/apps?new_app=1&manifest_yaml=" + quote(payload, safe="")
        assert scan_exfiltration_urls(url) != []

    def test_a_payload_mixing_both_user_scope_lists_is_redacted(self) -> None:
        # The MCP manifest with the legacy ``search:read`` put back: no emitter
        # renders both lists at once, so the validator must not accept it.
        exact = slack_manifest.render("someone", strip_comments=True, features=_MCP)
        payload = exact.replace(
            "      - search:read.files\n", "      - search:read\n      - search:read.files\n"
        )
        assert payload != exact
        url = "https://api.slack.com/apps?new_app=1&manifest_yaml=" + quote(payload, safe="")
        assert scan_exfiltration_urls(url) != []

    def test_settings_page_reports_it_without_a_capability(self) -> None:
        from kiro_crew.dashboard import slack_code_settings as scs

        view = asyncio.run(scs.code_settings_view(_cfg(slack_mcp=True), None))
        assert view["slack_mcp"] is True
        assert "slack_mcp_capability" not in view
        assert "code_channels_capability" in view and "agent_sessions_capability" in view
        change = scs.feature_changes()["slack_mcp"]
        assert change["added_scopes"] == [] and change["added_events"] == []
        assert change["added_user_scopes"] == sorted(
            set(_MCP_USER_SCOPES) - set(_LEGACY_USER_SCOPES)
        )
        assert change["removed_user_scopes"] == ["search:read"]
        assert "removed_settings" not in change
        assert "settings.is_mcp_enabled: true" in change["added_settings"]

    @pytest.mark.parametrize(
        "provider,offered",
        [
            (None, False),
            ({"slug": "slack", "hidden": True}, False),
            ({"slug": "slack", "hidden": False}, True),
        ],
    )
    def test_settings_page_says_whether_connections_offers_the_slack_card(
        self, provider: dict | None, offered: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The page shows the switch only when the card it is for exists, by the
        # gallery's own rule (``registry.is_visible``).
        from kiro_crew.connections import registry
        from kiro_crew.dashboard import slack_code_settings as scs

        asked: list[str] = []

        def get_provider(slug: str) -> dict | None:
            asked.append(slug)
            return provider

        monkeypatch.setattr(registry, "get_provider", get_provider)
        monkeypatch.setattr(registry, "is_visible", lambda p: not p["hidden"])
        view = asyncio.run(scs.code_settings_view(_cfg(), None))
        assert view["slack_mcp_card"] is offered
        assert asked == ["slack"]

    def test_the_card_signal_reads_the_real_registry_and_gallery_switch(self) -> None:
        # The real Slack entry is shown (a pre-registered entry is, by
        # ``registry.is_visible``), so on a default config the switch is offered.
        from kiro_crew.connections import registry
        from kiro_crew.dashboard import slack_code_settings as scs

        card = registry.get_provider("slack")
        assert card is not None and registry.is_visible(card)
        view = asyncio.run(scs.code_settings_view(_cfg(), None))
        assert view["slack_mcp_card"] is True

    @pytest.mark.parametrize("gallery", [False, None, "true", 1])
    def test_no_card_when_the_connections_gallery_is_not_on(self, gallery: object) -> None:
        # ``connections_ui`` empties the Connections gallery: no Slack card is
        # reachable then, so the switch is not offered. Only a real ``True`` is
        # on, as the Connections page itself reads it.
        from kiro_crew.dashboard import slack_code_settings as scs

        cfg = _cfg()
        cfg.connections_ui = gallery  # type: ignore[assignment]
        view = asyncio.run(scs.code_settings_view(cfg, None))
        assert view["slack_mcp_card"] is False
