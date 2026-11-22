"""The Slack app's name, its bot's ``@`` name and its ``/`` command are the user's.

Settings → Messaging Channels → Slack and ``kirocrew manifest`` render one manifest from three names
(``slack_manifest.AppNames``). A fresh install calls all three ``kirocrew``; an
existing app keeps the alias-based names it was created with, so
updating it never renames its bot. The exfil validator rebuilds the deep link
from the names it carries, so a link for exactly those names passes and any
other payload stays redacted.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.messaging as mod
from kiro_crew import cli_setup, slack_manifest
from kiro_crew.config.loader import CRED_SLACK_BOT_TOKEN, KiroCrewConfig
from kiro_crew.config.section_builders import _build_slack_config
from kiro_crew.security import scan_exfiltration_urls
from kiro_crew.security.exfil import _EXFIL_QUERY_MIN_LEN

AppNames = slack_manifest.AppNames


def _install(monkeypatch: pytest.MonkeyPatch, *, bot_token: str = "", **slack: object) -> None:
    """Make ``KiroCrewConfig.load`` return a config with *slack* and *bot_token*."""
    cfg = KiroCrewConfig()
    cfg.slack = _build_slack_config(dict(slack))
    creds = {CRED_SLACK_BOT_TOKEN: bot_token} if bot_token else {}
    monkeypatch.setattr(cfg, "load_credentials", lambda *a, **k: dict(creds), raising=False)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))


def _get(path: str) -> tuple[int, dict[str, Any]]:
    resp = asyncio.run(mod.api_slack_manifest(make_mocked_request("GET", path)))
    return resp.status, dict(json.loads(resp.text))


def _names(manifest: str) -> tuple[str, str, str]:
    parsed = yaml.safe_load(manifest)
    return (
        parsed["display_information"]["name"],
        parsed["features"]["bot_user"]["display_name"],
        parsed["features"]["slash_commands"][0]["command"],
    )


def _query(**names: str) -> str:
    return "/api/slack/manifest?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in names.items())


class TestDefaults:
    def test_a_fresh_install_is_kirocrew_everywhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch)
        status, body = _get("/api/slack/manifest")
        assert status == 200
        assert _names(body["manifest"]) == ("kirocrew", "kirocrew", "/kirocrew")
        assert (body["app_name"], body["bot_name"], body["command"]) == ("kirocrew",) * 3
        assert scan_exfiltration_urls(body["create_url"]) == []

    def test_empty_fields_fall_back_to_the_default(self) -> None:
        status, body = _get(_query(app_name="", bot_name=" ", command=""))
        assert status == 200
        assert _names(body["manifest"]) == ("kirocrew", "kirocrew", "/kirocrew")

    @pytest.mark.parametrize(
        "saved,existing,expected",
        [
            ("kirocrew", False, ("kirocrew", "kirocrew")),
            ("kirocrew", True, ("KiroCrew-kirocrew",) * 2),  # brand-ok: legacy name
            ("kirocrew-jdoe", False, ("KiroCrew-jdoe",) * 2),  # brand-ok: legacy name
            ("crew", False, ("KiroCrew-kirocrew",) * 2),  # brand-ok: legacy name
        ],
    )
    def test_unsaved_names_follow_the_install(
        self, saved: str, existing: bool, expected: tuple[str, str]
    ) -> None:
        names = slack_manifest.install_names(command=saved, existing=existing)
        assert (names.app_name, names.bot_name) == expected
        assert names.command == saved

    def test_saved_names_win_one_by_one(self) -> None:
        names = slack_manifest.install_names("Team Bot", "", "crew", existing=True)
        assert names == AppNames("Team Bot", "KiroCrew-kirocrew", "crew")  # brand-ok


class TestExistingAppsKeepTheirManifest:
    """An install with a Slack app and no saved names renders what it did before."""

    @pytest.mark.parametrize("saved", ["kirocrew", "kirocrew-jdoe", "crew"])
    def test_the_dashboard_renders_the_old_bytes(
        self, saved: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, bot_token="xoxb-test", command=saved)
        _, body = _get("/api/slack/manifest")
        alias = slack_manifest.alias_for_command(saved)
        # render(alias) is the pre-change render: its digest is pinned in
        # test_slack_manifest_features.py::test_switches_off_keeps_the_pre_toggle_payload.
        legacy = slack_manifest.render(alias).replace(
            f"/{slack_manifest.slash_command(alias)}\n", f"/{saved}\n"
        )
        assert body["manifest"] == legacy
        assert _names(body["manifest"])[:2] == (f"KiroCrew-{alias}",) * 2  # brand-ok

    @pytest.mark.parametrize("saved", ["OldCmd", "c" * 32])
    def test_a_command_saved_before_slacks_rule_still_renders(
        self, saved: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The save path keeps such a value (unchanged commands are not
        # re-checked), so the page must keep its manifest: the alias command,
        # as main rendered it, never a 400.
        _install(monkeypatch, bot_token="xoxb-test", command=saved)
        status, body = _get("/api/slack/manifest")
        assert status == 200
        assert body["manifest"] == slack_manifest.render(slack_manifest.alias_for_command(saved))
        _install(monkeypatch, bot_token="xoxb-test", command=saved, app_name="Team")
        cli_setup._manifest()
        assert _names(capsys.readouterr().out)[0] == "Team"

    def test_the_legacy_alias_path_is_unchanged(self) -> None:
        status, body = _get("/api/slack/manifest?alias=JDoe")
        assert status == 200
        legacy = slack_manifest.legacy_names("JDoe")
        assert legacy.app_name == legacy.bot_name == slack_manifest.LEGACY_NAME_PREFIX + "JDoe"
        assert _names(body["manifest"]) == (legacy.app_name, legacy.bot_name, "/kirocrew-jdoe")
        assert body["manifest"] == slack_manifest.render("JDoe")
        assert body["create_url"] == slack_manifest.deep_link("JDoe")

    def test_alias_and_names_together_are_refused(self) -> None:
        status, body = _get("/api/slack/manifest?alias=jdoe&app_name=x")
        assert status == 400
        assert "alias" in body["error"]


class TestBoundaryValidation:
    """Slack's own limits, checked server side; a bad name is refused, never rewritten."""

    @pytest.mark.parametrize(
        "field,value,ok",
        [
            ("app_name", "a" * slack_manifest.APP_NAME_MAX, True),
            ("app_name", "a" * (slack_manifest.APP_NAME_MAX + 1), False),
            ("bot_name", "b" * slack_manifest.BOT_NAME_MAX, True),
            ("bot_name", "b" * (slack_manifest.BOT_NAME_MAX + 1), False),
            ("command", "c" * (slack_manifest.SLASH_COMMAND_MAX - 1), True),
            ("command", "c" * slack_manifest.SLASH_COMMAND_MAX, False),
            ("command", "/crew", True),
            ("command", "Crew", False),
            ("command", "my crew", False),
            ("command", "crew🚀", False),
            ("command", "kirocrew", True),  # another install's command: allowed, hinted
            ("app_name", "Équipe 日本", True),
            ("app_name", "Crew🚀", True),
            ("app_name", "bad\u202eeman", False),  # bidi override
            ("app_name", "zero\u200bwidth", False),
            ("bot_name", "tab\tname", False),
        ],
    )
    def test_each_name(self, field: str, value: str, ok: bool) -> None:
        status, body = _get(_query(**{field: value}))
        assert (status == 200) is ok, body
        if ok:
            rendered = dict(zip(("app_name", "bot_name", "command"), _names(body["manifest"])))
            assert rendered[field] == (value if field != "command" else "/" + value.lstrip("/"))
        else:
            assert field.split("_")[0] in body["error"]

    def test_yaml_specials_round_trip_as_strings(self) -> None:
        for value in ("yes", "123", "a: b", "# x", '"q"', "__BOT_NAME__", "x\\y", "-dash"):
            text = slack_manifest.render(AppNames(app_name=value, bot_name=value))
            assert _names(text)[:2] == (value, value), value


def _put(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: dict[str, Any], stored: dict
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"slack": stored}), encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _run() -> tuple[int, dict[str, Any]]:
        app = web.Application()
        app.router.add_put("/api/slack/config", mod.api_slack_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/slack/config", json=body)
            return resp.status, await resp.json()

    status, reply = asyncio.run(_run())
    return status, reply, json.loads(cfg.read_text(encoding="utf-8"))["slack"]


class TestSaving:
    def test_names_save_and_clear(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        body = {"app_name": " Team Bot ", "bot_name": "teambot", "command": "/team"}
        status, reply, slack = _put(monkeypatch, tmp_path, body, {})
        assert status == 200 and reply["restart_required"] is False
        assert (slack["app_name"], slack["bot_name"], slack["command"]) == (
            "Team Bot",
            "teambot",
            "team",
        )
        # Cleared: back to the install's default, which the save pins.
        status, _, slack = _put(monkeypatch, tmp_path, {"app_name": ""}, {"app_name": "x"})
        assert status == 200 and slack["app_name"] == "kirocrew"

    def test_a_save_pins_the_names_the_page_rendered(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A fresh install's first save names no name: it stores kirocrew, so
        # the token it brings cannot turn the app into an "existing" one that
        # renders the alias-based name.
        status, _, slack = _put(monkeypatch, tmp_path, {"show_thinking": True}, {})
        assert status == 200
        assert (slack["app_name"], slack["bot_name"]) == ("kirocrew", "kirocrew")
        # An existing app (saved custom command) pins its legacy name.
        _, _, slack = _put(
            monkeypatch, tmp_path, {"show_thinking": True}, {"command": "kirocrew-jdoe"}
        )
        assert slack["app_name"] == slack["bot_name"] == "KiroCrew-jdoe"  # brand-ok

    def test_a_save_that_changes_the_command_pins_the_names_rendered_before_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Fresh (default command, no token): the page showed kirocrew, so the
        # new command does not turn the pin into an alias-based name.
        status, _, slack = _put(monkeypatch, tmp_path, {"command": "team"}, {})
        assert status == 200 and slack["command"] == "team"
        assert (slack["app_name"], slack["bot_name"]) == ("kirocrew", "kirocrew")

    def test_unreadable_credentials_pin_the_existing_app_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def _boom(*a: object, **k: object) -> dict:
            raise OSError("vault unavailable")

        monkeypatch.setattr(KiroCrewConfig, "load_credentials", _boom)
        status, _, slack = _put(monkeypatch, tmp_path, {"show_thinking": True}, {})
        assert status == 200
        assert slack["app_name"] == slack["bot_name"] == "KiroCrew-kirocrew"  # brand-ok

    def test_a_save_without_names_keeps_names_stored_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stored = {"app_name": "From CLI", "bot_name": "cli"}
        status, _, slack = _put(monkeypatch, tmp_path, {"show_thinking": True}, stored)
        assert status == 200
        assert (slack["app_name"], slack["bot_name"]) == ("From CLI", "cli")

    @pytest.mark.parametrize(
        "body",
        [
            {"app_name": "a" * 36},
            {"bot_name": "b" * 81},
            {"bot_name": 7},
            {"command": "Upper"},
            {"command": "x" * 32},
        ],
    )
    def test_a_bad_value_is_refused_and_nothing_is_written(
        self, body: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        status, _, slack = _put(monkeypatch, tmp_path, body, {"command": "keep"})
        assert status == 400
        assert slack == {"command": "keep"}

    def test_an_old_command_still_saves_when_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The form sends the command on every save; a value stored before the
        # lowercase rule must not block saving the other fields.
        status, _, slack = _put(
            monkeypatch, tmp_path, {"command": "OldCmd", "bot_name": "b"}, {"command": "OldCmd"}
        )
        assert status == 200 and slack["bot_name"] == "b" and slack["command"] == "OldCmd"

    def test_the_gateway_answers_the_saved_command(self) -> None:
        assert slack_manifest.is_app_command("/team", "team")
        assert not slack_manifest.is_app_command("/team", "kirocrew")


class TestCliParity:
    def test_saved_names_render_what_the_dashboard_renders(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _install(monkeypatch, app_name="Team Bot", bot_name="teambot", command="team")
        monkeypatch.setenv("USER", "hostaccount")
        cli_setup._manifest()
        out = capsys.readouterr().out
        _, body = _get("/api/slack/manifest")
        assert out.rstrip("\n") == body["manifest"].rstrip("\n")
        assert _names(out) == ("Team Bot", "teambot", "/team")
        assert "hostaccount" not in out
        cli_setup._manifest(url=True)
        assert body["create_url"] in capsys.readouterr().out

    def test_no_saved_names_keeps_the_alias_default(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _install(monkeypatch)
        monkeypatch.setenv("USER", "jdoe")
        cli_setup._manifest()
        assert capsys.readouterr().out.rstrip("\n") == slack_manifest.render("jdoe").rstrip("\n")

    def test_the_alias_path_says_how_to_keep_its_names(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A dashboard save may already have pinned kirocrew; the printed lines
        # are what keep the app this link creates on its alias name.
        _install(monkeypatch)
        cli_setup._manifest(alias="bob", url=True)
        out = capsys.readouterr().out
        legacy = slack_manifest.legacy_names("bob")
        assert f"kirocrew config set slack.app_name {legacy.app_name}" in out
        assert f"kirocrew config set slack.bot_name {legacy.bot_name}" in out
        _install(monkeypatch, app_name="Team", bot_name="team")
        cli_setup._manifest(url=True)
        assert "slack.app_name" not in capsys.readouterr().out

    def test_a_bad_saved_name_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, app_name="a" * 40)
        with pytest.raises(SystemExit) as exc:
            cli_setup._manifest()
        assert exc.value.code == 1


class TestCliTokenSetupKeepsTheCardName:
    """An app created from the panel's card as kirocrew, tokens stored with
    `kirocrew setup --slack`: the manifest must still name it kirocrew."""

    @pytest.fixture
    def home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        home = tmp_path / "crew-home"
        home.mkdir()
        env, cfg = home / ".env", home / "config.json"
        env.write_text("", encoding="utf-8")
        cfg.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        for target in (loader, cli_setup):
            monkeypatch.setattr(target, "env_path", lambda: env, raising=False)
            monkeypatch.setattr(target, "config_path", lambda: cfg, raising=False)
        for var in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "KIROCREW_OWNER_ID"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(cli_setup, "_verify_slack_secret", lambda key, value: (True, ""))
        monkeypatch.setattr(cli_setup, "_verify_slack_owner_id", lambda bot, owner: (True, ""))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
        return home

    def _setup_slack(self, monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
        # The two steps `kirocrew setup --slack` runs, in its order and wiring.
        monkeypatch.setattr("builtins.input", lambda _p="": answers.pop(0))
        had_app = cli_setup._setup_slack_tokens()
        cli_setup._setup_slash_command(had_app)

    def test_the_card_name_survives_the_cli_token_save(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        status, before = _get("/api/slack/manifest")
        assert status == 200 and _names(before["manifest"])[:2] == ("kirocrew", "kirocrew")
        # Configure, app token, bot token, owner, then keep the slash command.
        self._setup_slack(monkeypatch, ["y", "xapp-ok", "xoxb-ok", "U03T18B4Y23", ""])
        assert "xoxb-ok" in (home / ".env").read_text(encoding="utf-8")
        status, after = _get("/api/slack/manifest")
        assert status == 200
        assert _names(after["manifest"]) == ("kirocrew", "kirocrew", "/kirocrew")

    def test_the_step_says_which_names_it_stored(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # An app made by `kirocrew manifest` with its config-set lines skipped
        # looks like a card-created one here, so the user is told what to change.
        self._setup_slack(monkeypatch, ["y", "xapp-ok", "xoxb-ok", "U03T18B4Y23", ""])
        out = capsys.readouterr().out
        assert "Slack app names stored (app_name kirocrew, bot_name kirocrew)" in out
        assert "kirocrew config set slack.app_name <name>" in out

    def test_an_alias_command_typed_at_setup_keeps_its_alias_name(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The CLI path: an app made by `kirocrew manifest --alias jdoe`.
        self._setup_slack(monkeypatch, ["y", "xapp-ok", "xoxb-ok", "U03T18B4Y23", "kirocrew-jdoe"])
        _, after = _get("/api/slack/manifest")
        assert after["manifest"] == slack_manifest.render("jdoe")

    def test_an_app_whose_token_is_only_in_the_environment_keeps_its_name(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An existing app: its token is exported, not in .env, so the manifest
        # already renders the alias-based name. Storing the token must not
        # rename it to kirocrew.
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
        _, before = _get("/api/slack/manifest")
        assert _names(before["manifest"])[0] == "KiroCrew-kirocrew"  # brand-ok
        self._setup_slack(monkeypatch, ["y", "xapp-ok", "xoxb-ok", "U03T18B4Y23", ""])
        _, after = _get("/api/slack/manifest")
        assert after["manifest"] == before["manifest"]

    def test_eof_at_the_command_prompt_still_keeps_the_card_name(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answers = ["y", "xapp-ok", "xoxb-ok", "U03T18B4Y23"]

        def _answer(_prompt: str = "") -> str:
            if not answers:
                raise EOFError
            return answers.pop(0)

        monkeypatch.setattr("builtins.input", _answer)
        had_app = cli_setup._setup_slack_tokens()
        with pytest.raises(cli_setup._SetupAborted):
            cli_setup._setup_slash_command(had_app)
        _, after = _get("/api/slack/manifest")
        assert _names(after["manifest"]) == ("kirocrew", "kirocrew", "/kirocrew")

    def test_a_name_already_stored_is_kept(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (home / "config.json").write_text(json.dumps({"slack": {"app_name": "Mine"}}), "utf-8")
        self._setup_slack(monkeypatch, ["y", "xapp-ok", "xoxb-ok", "U03T18B4Y23", ""])
        slack = json.loads((home / "config.json").read_text(encoding="utf-8"))["slack"]
        assert (slack["app_name"], slack["bot_name"]) == ("Mine", "kirocrew")


class TestDeepLinkValidator:
    """The link for exactly the entered names passes; any other payload does not."""

    @pytest.mark.parametrize(
        "names",
        [
            AppNames(),
            AppNames("Team Bot", "teambot", "team"),
            AppNames("Équipe: 日本", "ü bot", "x"),
            AppNames("a" * 35, "b c " * 19 + "dd", "c" * 31),
            AppNames('say "hi"', "back\\slash", "q"),
        ],
    )
    def test_every_switch_combination_passes(self, names: AppNames) -> None:
        for combo in slack_manifest.feature_sets():
            assert scan_exfiltration_urls(slack_manifest.deep_link(names, combo)) == [], combo

    def _link(self, payload: str) -> str:
        return "https://api.slack.com/apps?new_app=1&manifest_yaml=" + quote(payload, safe="")

    def test_a_payload_unlike_the_render_is_refused(self) -> None:
        exact = slack_manifest.render(AppNames("Team Bot", "tb", "team"), strip_comments=True)
        assert scan_exfiltration_urls(self._link(exact)) == []
        for tampered in (
            exact.replace('name: "Team Bot"', "name: Team Bot"),  # unquoted space
            exact.replace("display_name: tb", "display_name: tb\n    extra: 1"),
            exact.replace("display_name: tb", 'display_name: "tb"'),
            exact.replace("- command: /team", "- command: /Team"),
            exact.replace('name: "Team Bot"', 'name: "Team\\u0042ot"'),  # second spelling
            exact.replace('name: "Team Bot"', 'name: "' + "x" * 36 + '"'),
            exact + "\n",
        ):
            assert tampered != exact
            assert scan_exfiltration_urls(self._link(tampered)) != [], tampered

    def test_a_broken_quoted_name_is_refused(self) -> None:
        exact = slack_manifest.render(AppNames(app_name="a b"), strip_comments=True)
        assert 'name: "a b"\n' in exact
        assert slack_manifest.scalar_value('"a b') is None
        assert slack_manifest.scalar_value('"\\u0041"') == "A"
        assert scan_exfiltration_urls(self._link(exact.replace('"a b"', '"a b'))) != []

    def test_a_template_with_a_repeated_placeholder_builds_no_pattern(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.security import exfil

        real = slack_manifest.stripped_template
        monkeypatch.setattr(
            slack_manifest,
            "stripped_template",
            lambda combo=None: real(combo) + f"x: {slack_manifest.APP_NAME_PLACEHOLDER}\n",
        )
        monkeypatch.setattr(exfil, "_slack_manifest_re_slot", [])
        # Every variant refused, so even the stock link loses its exemption.
        assert scan_exfiltration_urls(slack_manifest.deep_link(AppNames())) != []

    def test_the_names_together_stay_under_the_free_query_length(self) -> None:
        # The residual the validator states: names that look like no credential
        # carry no more than any short query anywhere already can.
        widest = slack_manifest.APP_NAME_MAX + slack_manifest.BOT_NAME_MAX
        assert widest + (slack_manifest.SLASH_COMMAND_MAX - 1) + 2 < _EXFIL_QUERY_MIN_LEN

    def test_a_long_non_ascii_name_still_meets_the_percent_encoding_check(self) -> None:
        # Unconditional and untouched: 20+ consecutive percent octets are
        # refused before the carve-out, so a long CJK name's link is redacted in
        # chat text. The dashboard's Create button reads it from JSON instead.
        link = slack_manifest.deep_link(AppNames(app_name="日本語のアプリです"))
        assert scan_exfiltration_urls(link) != []


def test_unreadable_credentials_keep_the_existing_app_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)
    cfg = KiroCrewConfig.load()

    def _boom(*a: object, **k: object) -> dict:
        raise OSError("vault unavailable")

    monkeypatch.setattr(cfg, "load_credentials", _boom, raising=False)
    status, body = _get("/api/slack/manifest")
    assert status == 200 and body["app_name"] == "KiroCrew-kirocrew"  # brand-ok


def test_no_host_account_name_in_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, bot_token="xoxb-secret-value")
    monkeypatch.setenv("USER", "hostaccount")
    _, body = _get("/api/slack/manifest")
    text = json.dumps(body)
    assert "hostaccount" not in text
    assert "xoxb-secret-value" not in text
