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
_AS = frozenset({slack_manifest.AGENT_SESSIONS})
#: A switch registered only for the test, the way a later PR registers one: an
#: entry in ``FEATURE_SCOPES`` plus its ``#@<name>`` lines in the template.
_FAKE = "fake_switch"
_FAKE_LINES = {
    "      - assistant:write  #@agent_sessions\n": (
        "      - assistant:write  #@agent_sessions\n" "      - fake:write  #@fake_switch\n"
    ),
    "      - agent_session_title_changed  #@agent_sessions\n": (
        "      - agent_session_title_changed  #@agent_sessions\n"
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
    monkeypatch.setattr(slack_manifest, "FEATURES", tuple(slack_manifest.FEATURE_SCOPES))
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
        assert slack_manifest.FEATURES == tuple(slack_manifest.FEATURE_SCOPES)
        assert slack_manifest.FEATURES == (slack_manifest.AGENT_SESSIONS,)
        # agent_view is one-way, so only the Agent sessions switch declares it.
        assert "agent_view" not in slack_manifest.render("x")
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
        for marker in ("agent_view", "assistant:write", "agent_session"):
            assert marker not in text
        # Byte-pinned: the digest of the payload before the manifest became
        # toggle-aware. A change here changes every default install's deep link.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert digest == "bca5f538a73e541d77d4b00dbc5a39b48b499baa514358b3474010c36c984d11"

    def test_unknown_feature_names_are_ignored(self) -> None:
        assert slack_manifest.render("x", features={"bogus"}) == slack_manifest.render("x")

    def test_agent_sessions_requirements(self) -> None:
        """The agent view, its one scope and the two session events, nothing more."""
        change = slack_manifest.diff_requirements(
            slack_manifest.requirements(frozenset()), slack_manifest.requirements(_AS)
        )
        assert change.added_features == ("agent_view",)
        assert change.added_scopes == ("assistant:write",)
        assert change.added_events == ("agent_session_stopped", "agent_session_title_changed")
        assert change.removed_scopes == () and not change.empty
        assert "app_context_changed" not in slack_manifest.render("x", features=_AS)
        back = slack_manifest.diff_requirements(
            slack_manifest.requirements(_AS), slack_manifest.requirements(frozenset())
        )
        assert back.removed_features == ("agent_view",)
        assert back.removed_scopes == ("assistant:write",)
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
            ({"agent_sessions": "true"}, frozenset()),
        ],
    )
    def test_config_to_features(self, slack: dict, expected: frozenset[str]) -> None:
        assert manifest_features(_cfg(**slack)) == expected

    @pytest.mark.parametrize("slack", [{}, {"agent_sessions": True}])
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

        def broken_for_agent_sessions(features: frozenset[str] = frozenset()) -> str:
            if slack_manifest.AGENT_SESSIONS in features:
                raise OSError("variant unreadable")
            return real(features)

        monkeypatch.setattr(exfil, "_slack_manifest_re_slot", [])
        monkeypatch.setattr(slack_manifest, "stripped_template", broken_for_agent_sessions)
        # The stock link keeps its exemption; the one variant that did not
        # build is refused rather than accepted by a looser pattern.
        assert scan_exfiltration_urls(slack_manifest.deep_link("someone", frozenset())) == []
        monkeypatch.setattr(slack_manifest, "stripped_template", real)
        assert scan_exfiltration_urls(slack_manifest.deep_link("someone", _AS)) != []

    def test_a_mixed_combination_is_redacted(self) -> None:
        # The agent-view feature block WITHOUT its scope: no emitter renders
        # this, so the validator must not accept it even though every line is
        # legitimate.
        on = slack_manifest.stripped_template(_AS).splitlines()
        tampered = "\n".join(ln for ln in on if "assistant:write" not in ln) + "\n"
        payload = tampered.replace(slack_manifest.ALIAS_PLACEHOLDER, "someone").replace(
            slack_manifest.COMMAND_PLACEHOLDER, slack_manifest.slash_command("someone")
        )
        url = "https://api.slack.com/apps?new_app=1&manifest_yaml=" + quote(payload, safe="")
        assert scan_exfiltration_urls(url) != []

    def test_a_gate_marker_smuggled_into_the_payload_is_redacted(self) -> None:
        payload = slack_manifest.render("someone", strip_comments=True, features=_AS).replace(
            "assistant:write", "assistant:write  #@agent_sessions"
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

        cfg = _cfg(command=saved, agent_sessions=True)
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
        resp = asyncio.run(
            mod.api_slack_manifest(make_mocked_request("GET", "/api/slack/manifest"))
        )
        body = json.loads(resp.text)
        parsed = yaml.safe_load(body["manifest"])
        assert parsed["display_information"]["name"] == name
        assert parsed["features"]["slash_commands"][0]["command"] == slash
        assert body["command"] == slash.lstrip("/")
        assert "agent_view" in parsed["features"]
        # The create-app deep link stays one the exfil validator exempts.
        assert scan_exfiltration_urls(body["create_url"]) == []


class TestRegisteringASwitch:
    """A new switch needs only its registry entry, its template lines and its
    config field: every consumer below follows without a code change."""

    def test_render_and_combinations_follow(self, fake_switch: str) -> None:
        on = frozenset({fake_switch})
        assert slack_manifest.feature_sets() == (frozenset(), _AS, on, _AS | on)
        text = slack_manifest.render("x", features=on)
        assert "fake:write" in text and "fake_event" in text and "#@" not in text
        assert "fake:write" not in slack_manifest.render("x", features=_AS)

    def test_requirements_follow(self, fake_switch: str) -> None:
        change = slack_manifest.diff_requirements(
            slack_manifest.requirements(frozenset()),
            slack_manifest.requirements(frozenset({fake_switch})),
        )
        assert change.added_scopes == ("fake:write",)
        assert change.added_events == ("fake_event",)

    def test_the_exfil_exemption_follows(self, fake_switch: str) -> None:
        for combo in slack_manifest.feature_sets():
            assert scan_exfiltration_urls(slack_manifest.deep_link("someone", combo)) == []

    def test_config_and_the_settings_page_follow(self, fake_switch: str) -> None:
        from types import SimpleNamespace

        from kiro_crew.dashboard import slack_code_settings as scs

        cfg = SimpleNamespace(slack=SimpleNamespace(agent_sessions=False, fake_switch=True))
        assert manifest_features(cfg) == {fake_switch}
        changes = scs.feature_changes()
        assert set(changes) == {"agent_sessions", fake_switch}
        assert changes[fake_switch]["added_scopes"] == ["fake:write"]
        view = scs.code_settings_view(cfg)
        assert view[fake_switch] is True and view["agent_sessions"] is False
        assert view[f"{fake_switch}_capability"]["scope"] == "fake:write"
