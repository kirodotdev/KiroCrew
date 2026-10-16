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


def _cfg(**slack: object) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.slack = _build_slack_config(dict(slack))
    return cfg


class TestRendering:
    def test_four_distinct_combinations(self) -> None:
        assert len(COMBOS) == 4
        assert len({slack_manifest.render("x", features=c) for c in COMBOS}) == 4

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
        for marker in ("agent_view", "code_channels", "assistant:write", "agent_session"):
            assert marker not in text
        # Byte-pinned: the digest of the payload before the manifest became
        # toggle-aware. A change here changes every default install's deep link.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert digest == "bca5f538a73e541d77d4b00dbc5a39b48b499baa514358b3474010c36c984d11"

    def test_unknown_feature_names_are_ignored(self) -> None:
        assert slack_manifest.render("x", features={"bogus"}) == slack_manifest.render("x")

    def test_requirements_and_diff(self) -> None:
        change = slack_manifest.diff_requirements(
            slack_manifest.requirements(frozenset()), slack_manifest.requirements(_CC | _AS)
        )
        assert change.added_features == ("agent_view", "code_channels")
        assert change.added_scopes == (
            "assistant:write",
            "canvases:write",
            "channels:write.invites",
            "chat:write.customize",
            "code_channels:manage",
            "groups:write.invites",
        )
        assert change.added_events == (
            "agent_session_stopped",
            "agent_session_title_changed",
            "app_context_changed",
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
            slack_manifest.requirements(_AS), slack_manifest.requirements(_AS)
        ).empty

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
            ({"agent_sessions": True}, _AS),
            ({"code_channels": True}, _CC),
            ({"agent_sessions": True, "code_channels": True}, _AS | _CC),
        ],
    )
    def test_config_to_features(self, slack: dict, expected: frozenset[str]) -> None:
        assert manifest_features(_cfg(**slack)) == expected

    @pytest.mark.parametrize("slack", [{}, {"code_channels": True, "agent_sessions": True}])
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
        # Agent-view feature block WITHOUT its scope: no emitter renders this, so
        # the validator must not accept it even though every line is legitimate.
        on = slack_manifest.stripped_template(_AS).splitlines()
        tampered = "\n".join(ln for ln in on if "assistant:write" not in ln) + "\n"
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
