"""Section values come from the dataclass fields, and the declared departures stay pinned.

Three properties of the config load (``config.md`` "Defaults come from the DTO fields"):

* Through the read and build stages, a value of the wrong type for any section field
  builds that field's default, apart from the read-stage repairs the spec declares.
* Each build-time departure the spec declares builds its declared value, not the
  field default, when a malformed value reaches the builder.
* The builders, the DTO normalizers and the readers the section owners share take a
  default from the field at call time: moving a field's default moves what an
  omitted key and an unreadable value build. A second spelling of the default would
  keep the old value, and a home with no ``config.json`` (the bare dataclass) would
  then disagree with one whose file predates the key. A narrowing or safe value a
  reader keeps of its own, and the stub roster a legacy ``mcp_gateway`` section
  migrates to, do not move with it.
"""

from __future__ import annotations

import copy
import dataclasses
import itertools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_config_load_pipeline import _DECLARED_EXCEPTIONS, _document, _section_fields

from kiro_crew.acp_backends import selectable_backends
from kiro_crew.config import loader, sections, validation
from kiro_crew.instances.constants import DEFAULT_CONNECT_TIMEOUT_SECS, DEFAULT_MINT_TIMEOUT_SECS
from kiro_crew.monitoring.limits import DEFAULT_RUNTIME_CEILING_SECS
from kiro_crew.stt import models as stt_models

_BARE = loader.KiroCrewConfig()

#: Section fields whose wrong-typed value the READ stage repairs to something other
#: than the field default, on purpose. Each value is what the load builds.
_READ_STAGE_DEPARTURES = {
    # Fail closed: a present non-bool off-switch must not ride the missing-key
    # default (on) back in once validation removes it.
    ("agent", "session_control"): False,
    ("agent", "member_dispatch"): False,
    ("agent", "crew_panel"): False,
    ("skills", "project_skills_enabled"): False,
    # Resolved by the loader's own degradation rule before the enum check runs.
    ("stt", "provider"): "off",
    # An unreadable stored choice may have been a privacy choice: Temporary.
    ("dashboard", "default_memory_mode"): "temporary",
}


def _wrong_type(default: object) -> object:
    """A JSON value of a type the field cannot hold."""
    if isinstance(default, bool):
        return "maybe"
    if isinstance(default, (int, float)):
        return {"not": "a number"}
    if isinstance(default, str):
        return {"not": "a string"}
    if isinstance(default, (list, set)):
        return "not a list"
    if isinstance(default, dict) or dataclasses.is_dataclass(default):
        return "not an object"
    if default is None:
        return {"not": "a value"}
    raise AssertionError(f"no wrong-typed value for a {type(default).__name__} default")


#: Fields whose malformed-value handling another suite owns: ``decisions.bucket``'s
#: bounds are pinned by ``test_decisions_config.py``.
_OWNED_ELSEWHERE = {("decisions", "bucket")}


@pytest.fixture
def staged_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[dict], Any]:
    """Run the read and build stages over *document* written as a fresh home's config.json.

    A fresh directory per document, so the read stage's fingerprint-keyed cache can
    never serve one document's data for the next.
    """
    homes = itertools.count()

    def load(document: dict) -> loader.KiroCrewConfig:
        home = tmp_path / f"home{next(homes)}"
        home.mkdir()
        (home / "config.json").write_text(json.dumps(document), encoding="utf-8")
        monkeypatch.setattr(loader, "config_path", lambda: home / "config.json")
        monkeypatch.setattr(loader, "config_local_path", lambda: home / "config.local.json")
        monkeypatch.setattr(loader, "config_dir", lambda: home)
        cfg = loader.build_config(loader.read_config_document())
        # A degraded observation is sticky for the process; one document's must not
        # steer the next build in the same test.
        loader.reset_degraded_observations()
        return cfg

    return load


def _build(tmp_path: Path, document: dict) -> loader.KiroCrewConfig:
    return loader.build_config(_document(tmp_path, copy.deepcopy(document)))


# ---------------------------------------------------------------------------
# A wrong-typed value, through the read and build stages.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestAWrongTypedValueLoadsTheFieldDefault:
    @pytest.mark.parametrize("section", [name for name, _ in _section_fields()])
    def test_every_field_of_the_section(self, staged_load, section):
        bare = getattr(_BARE, section)
        checked: list[str] = []
        drift: dict[str, tuple[str, str]] = {}
        departures: dict[str, object] = {}
        for spec in dataclasses.fields(bare):
            if spec.name.startswith("_") or (section, spec.name) in _DECLARED_EXCEPTIONS:
                continue
            if (section, spec.name) in _OWNED_ELSEWHERE:
                continue
            default = getattr(bare, spec.name)
            cfg = staged_load({section: {spec.name: _wrong_type(default)}})
            built = getattr(getattr(cfg, section), spec.name)
            checked.append(spec.name)
            if (section, spec.name) in _READ_STAGE_DEPARTURES:
                departures[spec.name] = built
            elif built != default:
                drift[spec.name] = (repr(built), repr(default))
        assert checked, f"{section} has no field to check"
        assert drift == {}
        assert departures == {
            name: value
            for (owner, name), value in _READ_STAGE_DEPARTURES.items()
            if owner == section
        }


def test_each_read_stage_departure_names_a_field_and_differs_from_its_default():
    fields_by_section = {name: dto for name, dto in _section_fields()}
    for (section, name), value in sorted(_READ_STAGE_DEPARTURES.items()):
        assert name in {f.name for f in dataclasses.fields(fields_by_section[section])}
        assert getattr(getattr(_BARE, section), name) != value, (section, name)


# ---------------------------------------------------------------------------
# The declared build-time departures.
# ---------------------------------------------------------------------------


def _row(
    key: str,
    document: dict,
    read: Callable[[loader.KiroCrewConfig], object],
    built: object,
    loaded: object,
):
    """A declared departure: what the build reads, and what a full load reads."""
    return pytest.param(document, read, built, loaded, id=key)


#: (document, read, value the build reads, value the read + build stages load). The
#: read stage removes a wrong-typed value at these keys before the build for the
#: rows whose last two values differ, so a load there reads the field default.
_BUILD_DEPARTURES = [
    _row("auto_update", {"auto_update": "maybe"}, lambda c: c.auto_update, False, False),
    _row(
        "instances.connect_timeout_secs",
        {"instances": {"connect_timeout_secs": "slow"}},
        lambda c: c.instances.connect_timeout_secs,
        DEFAULT_CONNECT_TIMEOUT_SECS,
        None,
    ),
    _row(
        "instances.mint_timeout_secs",
        {"instances": {"mint_timeout_secs": "slow"}},
        lambda c: c.instances.mint_timeout_secs,
        DEFAULT_MINT_TIMEOUT_SECS,
        None,
    ),
    _row(
        "dashboard.loop_stall_exit_after_secs",
        {"dashboard": {"loop_stall_exit_after_secs": "soon"}},
        lambda c: c.dashboard.loop_stall_exit_after_secs,
        sections.LOOP_STALL_EXIT_AFTER_DEFAULT,
        None,
    ),
    _row(
        "stt.provider",
        {"stt": {"provider": "not-a-recogniser"}},
        lambda c: c.stt.provider,
        "off",
        "off",
    ),
    _row(
        "dashboard.default_memory_mode",
        {"dashboard": {"default_memory_mode": "forever"}},
        lambda c: c.dashboard.default_memory_mode,
        "temporary",
        "temporary",
    ),
    _row(
        "slack.dm_activation",
        {"slack": {"dm_activation": "sometimes"}},
        lambda c: c.slack_dm_activation,
        "mention",
        "mention",
    ),
    _row(
        "telegram.forum_activation",
        {"telegram": {"forum_activation": "sometimes"}},
        lambda c: c.telegram.forum_activation,
        "mention",
        "mention",
    ),
    _row(
        "telemetry.beacon_endpoint",
        {"telemetry": {"beacon_endpoint": "http://beacon.example"}},
        lambda c: c.telemetry.beacon_endpoint,
        "",
        "",
    ),
    _row(
        "skills.project_skills_enabled",
        {"skills": {"project_skills_enabled": "maybe"}},
        lambda c: c.skills.project_skills_enabled,
        False,
        False,
    ),
    _row(
        "dashboard.import_onboarded-and-privacy_acked",
        {"dashboard": {"onboarded": True, "import_onboarded": "maybe", "privacy_acked": "maybe"}},
        lambda c: (c.dashboard.import_onboarded, c.dashboard.privacy_acked),
        (True, True),
        (True, True),
    ),
    _row(
        "mcp.honour_auto_approve",
        {"mcp": {"honour_auto_approve": "maybe"}},
        lambda c: c.mcp.honour_auto_approve,
        False,
        True,
    ),
]


class TestTheDeclaredDepartures:
    @pytest.mark.parametrize(("document", "read", "built", "loaded"), _BUILD_DEPARTURES)
    def test_the_build_reads_the_declared_value(self, tmp_path, document, read, built, loaded):
        """What a builder does with a malformed value that reaches it.

        As one does whenever validation does not run: it is a no-op without
        ``jsonschema``, and the build reads the document it is handed.
        """
        assert read(_build(tmp_path, document)) == built
        assert read(_BARE) != built

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    @pytest.mark.parametrize(("document", "read", "built", "loaded"), _BUILD_DEPARTURES)
    def test_a_load_reads_the_declared_value_or_the_validated_default(
        self, staged_load, document, read, built, loaded
    ):
        assert read(staged_load(document)) == loaded
        assert loaded in (built, read(_BARE))


# ---------------------------------------------------------------------------
# Moving a field's default moves what the build reads.
# ---------------------------------------------------------------------------


def _move_default(monkeypatch: pytest.MonkeyPatch, dto: type, name: str, value: object) -> None:
    """Make *value* the default *dto* declares for field *name*, for this test only.

    Patches the ``Field`` itself, which ``fields.field_default`` reads, and with it
    ``SectionReader``. The class attribute of a plain default is left alone: no
    reader under test reads it.
    """
    [spec] = [f for f in dataclasses.fields(dto) if f.name == name]
    if spec.default_factory is not dataclasses.MISSING:
        assert value != spec.default_factory(), "a move must change the default"
        monkeypatch.setattr(spec, "default_factory", lambda: copy.deepcopy(value))
    else:
        assert value != spec.default, "a move must change the default"
        monkeypatch.setattr(spec, "default", value)


_SECTION_CHANNELS = {
    "slack": sections.SlackConfig,
    "telegram": sections.TelegramConfig,
    "weixin": sections.WeixinConfig,
    "whatsapp": sections.WhatsAppConfig,
    "discord": sections.DiscordConfig,
    "webex": sections.WebexConfig,
    "imessage": sections.IMessageConfig,
    "teams": sections.TeamsConfig,
    "wecom": sections.WeComConfig,
    "feishu": sections.FeishuConfig,
}


def _case(key: str, moves: list[tuple[type, str, object]], document: dict, read, expected):
    return pytest.param(moves, document, read, expected, id=key)


def _move_cases() -> list:
    agent, dashboard, stt = sections.AgentConfig, sections.DashboardConfig, sections.SttConfig
    tailscale, instances = sections.TailscaleConfig, sections.InstancesConfig
    backend = sorted(b for b in selectable_backends() if b)[0]
    account = {"telegram": {"accounts": {"ops": {"bot_token": "t", "soft_threshold_pct": "x"}}}}
    groups = [{"jid": "g@g.us"}]
    return [
        # Readers in ``config.sections``.
        _case(
            "session.archive_retention_days-omitted",
            [(sections.SessionConfig, "archive_retention_days", 7)],
            {"session": {}},
            lambda c: c.session.archive_retention_days,
            7,
        ),
        _case(
            "session.archive_retention_days-malformed",
            [(sections.SessionConfig, "archive_retention_days", 7)],
            {"session": {"archive_retention_days": "a while"}},
            lambda c: c.session.archive_retention_days,
            7,
        ),
        _case(
            "slack.channels-omitted",
            [
                (sections.ChannelConfig, "activation", "always"),
                (sections.ChannelConfig, "agent", "reviewer"),
                (sections.ChannelConfig, "thread_follow", False),
            ],
            {"slack": {"channels": {"C1": {}}}},
            lambda c: dataclasses.astuple(c.slack_channels["C1"]),
            ("always", "reviewer", False),
        ),
        _case(
            "workspaces.dir-omitted",
            [(sections.WorkspaceConfig, "dir", "elsewhere")],
            {"workspaces": {"w": {}}},
            lambda c: c.workspaces["w"].dir,
            "elsewhere",
        ),
        _case(
            "dashboard.tailscale-omitted",
            [
                (tailscale, "enabled", True),
                (tailscale, "trust_identity", True),
                (tailscale, "allowed_logins", ["owner@example.com"]),
                (tailscale, "pin_scope", "login"),
                (tailscale, "bind_refresh_chains", False),
                (tailscale, "keep_awake", False),
            ],
            {"dashboard": {"tailscale": {}}},
            lambda c: dataclasses.astuple(c.dashboard.tailscale),
            (True, True, ["owner@example.com"], "login", False, False),
        ),
        _case(
            "dashboard.tailscale.keep_awake-malformed",
            [(tailscale, "keep_awake", False)],
            {"dashboard": {"tailscale": {"keep_awake": "no"}}},
            lambda c: c.dashboard.tailscale.keep_awake,
            False,
        ),
        _case(
            "decisions.nudge_wake-omitted",
            [
                (sections.NudgeWakeConfig, "provider", "llm"),
                (sections.NudgeWakeConfig, "llm_model", "fixture-model"),
                (sections.NudgeWakeConfig, "quiet_streak_floor", 3),
            ],
            {"decisions": {"nudge_wake": {}}},
            lambda c: dataclasses.astuple(c.decisions.nudge_wake),
            ("llm", "fixture-model", 3),
        ),
        _case(
            "decisions.nudge_wake-malformed",
            [
                (sections.NudgeWakeConfig, "provider", "llm"),
                (sections.NudgeWakeConfig, "quiet_streak_floor", 3),
            ],
            {"decisions": {"nudge_wake": {"provider": "oracle", "quiet_streak_floor": "x"}}},
            lambda c: (c.decisions.nudge_wake.provider, c.decisions.nudge_wake.quiet_streak_floor),
            ("llm", 3),
        ),
        _case(
            "decisions.history_budget_chars-omitted",
            [(sections.DecisionsConfig, "history_budget_chars", 500)],
            {"decisions": {}},
            lambda c: c.decisions.history_budget_chars,
            500,
        ),
        _case(
            "decisions.history_budget_chars-malformed",
            [(sections.DecisionsConfig, "history_budget_chars", 500)],
            {"decisions": {"history_budget_chars": "lots"}},
            lambda c: c.decisions.history_budget_chars,
            500,
        ),
        _case(
            "telegram.accounts.entry-omitted-and-malformed",
            [
                (sections.TelegramAccountConfig, "allow_forum", True),
                (sections.TelegramAccountConfig, "soft_threshold_pct", 60),
                (sections.TelegramAccountConfig, "allowed_user_ids", [7]),
                (sections.TelegramAccountConfig, "allowed_forum_chat_ids", [-100]),
            ],
            account,
            lambda c: (
                c.telegram.accounts["ops"].allow_forum,
                c.telegram.accounts["ops"].soft_threshold_pct,
                c.telegram.accounts["ops"].allowed_user_ids,
                c.telegram.accounts["ops"].allowed_forum_chat_ids,
            ),
            (True, 60, [7], [-100]),
        ),
        _case(
            "telegram.accounts-omitted",
            [
                (
                    sections.TelegramConfig,
                    "accounts",
                    {"ops": sections.TelegramAccountConfig(bot_token="t")},
                )
            ],
            {"telegram": {}},
            lambda c: c.telegram.accounts,
            {"ops": sections.TelegramAccountConfig(bot_token="t")},
        ),
        _case(
            "agent.fallback_model-malformed",
            [(agent, "fallback_model", "fixture-model")],
            {"agent": {"fallback_model": 7}},
            lambda c: c.agent.fallback_model,
            "fixture-model",
        ),
        _case(
            "agent.yolo_duration-omitted",
            [(agent, "yolo_duration", "12h")],
            {"agent": {}},
            lambda c: c.agent.yolo_duration,
            "12h",
        ),
        _case(
            "agent.yolo_duration-invalid",
            [(agent, "yolo_duration", "12h")],
            {"agent": {"yolo_duration": "a fortnight"}},
            lambda c: c.agent.yolo_duration,
            "12h",
        ),
        _case(
            "agent.dangerously_skip_permissions-omitted",
            [(agent, "dangerously_skip_permissions", True)],
            {"agent": {}},
            lambda c: c.agent.dangerously_skip_permissions,
            True,
        ),
        _case(
            "stt.provider-null",
            [(stt, "provider", "apple")],
            {"stt": {"provider": None}},
            lambda c: c.stt.provider,
            "apple",
        ),
        _case(
            "stt.model-empty",
            [(stt, "model", "small")],
            {"stt": {"model": ""}},
            lambda c: c.stt.model,
            "small",
        ),
        _case(
            "stt.language_code-blank",
            [(stt, "language_code", "de-DE")],
            {"stt": {"language_code": "  "}},
            lambda c: c.stt.language_code,
            "de-DE",
        ),
        _case(
            "stt.transcribe_vocabulary-omitted",
            [(stt, "transcribe_vocabulary", "fixture-vocabulary")],
            {"stt": {}},
            lambda c: c.stt.transcribe_vocabulary,
            "fixture-vocabulary",
        ),
        _case(
            "imessage.service-invalid",
            [(sections.IMessageConfig, "service", "sms")],
            {"imessage": {"service": "pigeon"}},
            lambda c: c.imessage.service,
            "sms",
        ),
        # Call sites in ``config.section_builders``.
        _case(
            "monitoring.max_runtime_secs-omitted",
            [(sections.MonitoringConfig, "max_runtime_secs", 3600)],
            {"monitoring": {}},
            lambda c: c.monitoring.max_runtime_secs,
            3600,
        ),
        *[
            _case(
                f"{channel}.session_folder-omitted",
                [(dto, "session_folder", "Inbox")],
                {channel: {}},
                lambda c, channel=channel: getattr(c, channel).session_folder,
                "Inbox",
            )
            for channel, dto in sorted(_SECTION_CHANNELS.items())
        ],
        _case(
            "skills.extra_paths-omitted",
            [(sections.SkillsConfig, "extra_paths", ["/opt/skills"])],
            {"skills": {}},
            lambda c: c.skills.extra_paths,
            ["/opt/skills"],
        ),
        _case(
            "slack.trusted_bot_ids-and-reactions-omitted",
            [
                (sections.SlackConfig, "trusted_bot_ids", {"B1"}),
                (sections.SlackConfig, "reactions", {"done": "white_check_mark"}),
            ],
            {"slack": {}},
            lambda c: (c.slack.trusted_bot_ids, c.slack.reactions),
            ({"B1"}, {"done": "white_check_mark"}),
        ),
        _case(
            "whatsapp.groups-omitted",
            [(sections.WhatsAppConfig, "groups", groups)],
            {"whatsapp": {}},
            lambda c: c.whatsapp.groups,
            sections._coerce_whatsapp_groups(groups),
        ),
        _case(
            "channel-allowlists-omitted",
            [
                (sections.WebexConfig, "allowed_room_ids", ["room"]),
                (sections.IMessageConfig, "allowed_handles", ["handle"]),
                (sections.WeComConfig, "allowed_users", [{"userid": "u"}]),
            ],
            {"webex": {}, "imessage": {}, "wecom": {}},
            lambda c: (c.webex.allowed_room_ids, c.imessage.allowed_handles, c.wecom.allowed_users),
            (["room"], ["handle"], [{"userid": "u"}]),
        ),
        _case(
            "channel-id-lists-omitted",
            [
                (sections.TelegramConfig, "allowed_user_ids", [7]),
                (sections.TelegramConfig, "allowed_forum_chat_ids", [-100]),
                (sections.WeixinConfig, "allowed_user_ids", ["wxid_a"]),
                (sections.WhatsAppConfig, "allowed_wa_ids", ["15550001"]),
                (sections.DiscordConfig, "allowed_user_ids", ["1"]),
                (sections.DiscordConfig, "allowed_thread_ids", ["2"]),
                (sections.DiscordConfig, "allowed_channel_ids", ["3"]),
                (sections.FeishuConfig, "allowed_open_ids", ["ou_a"]),
                (sections.FeishuConfig, "allowed_group_ids", ["oc_a"]),
            ],
            {"telegram": {}, "weixin": {}, "whatsapp": {}, "discord": {}, "feishu": {}},
            lambda c: (
                c.telegram.allowed_user_ids,
                c.telegram.allowed_forum_chat_ids,
                c.weixin.allowed_user_ids,
                c.whatsapp.allowed_wa_ids,
                c.discord.allowed_user_ids,
                c.discord.allowed_thread_ids,
                c.discord.allowed_channel_ids,
                c.feishu.allowed_open_ids,
                c.feishu.allowed_group_ids,
            ),
            ([7], [-100], ["wxid_a"], ["15550001"], ["1"], ["2"], ["3"], ["ou_a"], ["oc_a"]),
        ),
        _case(
            "teams.app_password",
            [(sections.TeamsConfig, "app_password", "from-the-environment")],
            {"teams": {"app_password": "from-config-json"}},
            lambda c: c.teams.app_password,
            "from-the-environment",
        ),
        # Call sites in ``config.loader``.
        _case(
            "agent.role-maps-omitted",
            [
                (agent, "role_models", {"subagent": "fixture-model"}),
                (agent, "role_efforts", {"subagent": "high"}),
                (agent, "deepseek_env", {"DEEPSEEK_API_KEY": "secret://deepseek"}),
            ],
            {"agent": {}},
            lambda c: (c.agent.role_models, c.agent.role_efforts, c.agent.deepseek_env),
            (
                {"subagent": "fixture-model"},
                {"subagent": "high"},
                {"DEEPSEEK_API_KEY": "secret://deepseek"},
            ),
        ),
        _case(
            "agent.acp_backend-omitted",
            [(agent, "acp_backend", backend)],
            {"agent": {}},
            lambda c: c.agent.acp_backend,
            backend,
        ),
        _case(
            "agent.subagent_timeout_secs-malformed",
            [(agent, "subagent_timeout_secs", 1800)],
            {"agent": {"subagent_timeout_secs": "a while"}},
            lambda c: c.agent.subagent_timeout_secs,
            1800,
        ),
        _case(
            "session.compact_wait_secs-malformed",
            [(sections.SessionConfig, "compact_wait_secs", 120.0)],
            {"session": {"compact_wait_secs": "a while"}},
            lambda c: c.session.compact_wait_secs,
            120.0,
        ),
        _case(
            "dashboard.folder_sort-unknown",
            [(dashboard, "folder_sort", "name")],
            {"dashboard": {"folder_sort": "by colour"}},
            lambda c: c.dashboard.folder_sort,
            "name",
        ),
        _case(
            "dashboard.title_refresh_every_turns-malformed",
            [(dashboard, "title_refresh_every_turns", 8)],
            {"dashboard": {"title_refresh_every_turns": "often"}},
            lambda c: c.dashboard.title_refresh_every_turns,
            8,
        ),
        _case(
            "dashboard.lists-omitted",
            [
                (dashboard, "model_picker_hidden_models", ["fixture-model"]),
                (dashboard, "gitlab_hosts", ["gitlab.example.com"]),
                (dashboard, "jira_hosts", ["jira.example.com"]),
            ],
            {"dashboard": {}},
            lambda c: (
                c.dashboard.model_picker_hidden_models,
                c.dashboard.gitlab_hosts,
                c.dashboard.jira_hosts,
            ),
            (["fixture-model"], ["gitlab.example.com"], ["jira.example.com"]),
        ),
        _case(
            "dashboard.dto-lists-omitted",
            [
                (dashboard, "jira_auth", [sections.JiraAuthEntry(host="jira.example.com")]),
                (
                    dashboard,
                    "link_patterns",
                    [sections.LinkPatternRule(pattern="ENG-\\d+", url="https://x.example/$0")],
                ),
            ],
            {"dashboard": {}},
            lambda c: (c.dashboard.jira_auth, c.dashboard.link_patterns),
            (
                [sections.JiraAuthEntry(host="jira.example.com")],
                [sections.LinkPatternRule(pattern="ENG-\\d+", url="https://x.example/$0")],
            ),
        ),
        _case(
            "dashboard.loop_stall_exit_after_secs-omitted",
            [(dashboard, "loop_stall_exit_after_secs", 60)],
            {"dashboard": {}},
            lambda c: c.dashboard.loop_stall_exit_after_secs,
            60,
        ),
        _case(
            "publish.allowed_destinations-omitted",
            [(sections.PublishConfig, "allowed_destinations", ["github"])],
            {"publish": {}},
            lambda c: c.publish.allowed_destinations,
            ["github"],
        ),
        _case(
            "agents.avatar-omitted",
            [(sections.KiroCrewAgentConfig, "avatar", {"kind": "image"})],
            {"agents": {"crew": {}}},
            lambda c: c.agents["crew"].avatar,
            {"kind": "image"},
        ),
        # Normalizers and shared readers in the section owners.
        _case(
            "instances.out-of-range",
            [
                (instances, "tunnel_base_port", 9000),
                (instances, "max_recovery_attempts", 5),
                (instances, "recover_backoff_max_secs", 10.0),
                (instances, "probe_failure_threshold", 2),
            ],
            {
                "instances": {
                    "tunnel_base_port": 70000,
                    "max_recovery_attempts": 0,
                    "recover_backoff_max_secs": 0,
                    "probe_failure_threshold": 0,
                }
            },
            lambda c: (
                c.instances.tunnel_base_port,
                c.instances.max_recovery_attempts,
                c.instances.recover_backoff_max_secs,
                c.instances.probe_failure_threshold,
            ),
            (9000, 5, 10.0, 2),
        ),
        _case(
            "instances.timeouts-omitted",
            [(instances, "connect_timeout_secs", 20.0), (instances, "mint_timeout_secs", 40.0)],
            {"instances": {}},
            lambda c: (c.instances.connect_timeout_secs, c.instances.mint_timeout_secs),
            (20.0, 40.0),
        ),
        _case(
            "instances.timeouts-below-floor",
            [(instances, "connect_timeout_secs", 20.0), (instances, "mint_timeout_secs", 40.0)],
            {"instances": {"connect_timeout_secs": 0.5, "mint_timeout_secs": 5.0}},
            lambda c: (c.instances.connect_timeout_secs, c.instances.mint_timeout_secs),
            (20.0, 40.0),
        ),
        _case(
            "mcp_gateway.stub_overrides-omitted",
            [(sections.McpGatewayConfig, "stub_overrides", {"extra": True})],
            {"mcp_gateway": {}},
            lambda c: (c.mcp_gateway.stub_overrides, c.mcp_gateway.stub_servers),
            ({"extra": True}, ["extra"]),
        ),
        _case(
            "memory.embedding_provider-any",
            [(sections.MemoryConfig, "embedding_provider", "fixture-provider")],
            {"memory": {"embedding_provider": "ollama"}},
            lambda c: c.memory.embedding_provider,
            "fixture-provider",
        ),
        _case(
            "knowledge.auto_add_documents-omitted",
            [(sections.KnowledgeConfig, "auto_add_documents", True)],
            {"knowledge": {}},
            lambda c: c.knowledge.auto_add_documents,
            True,
        ),
        _case(
            "skills.auto_similarity_threshold-out-of-range",
            [(sections.SkillsConfig, "auto_similarity_threshold", 0.5)],
            {"skills": {"auto_similarity_threshold": 1.5}},
            lambda c: c.skills.auto_similarity_threshold,
            0.5,
        ),
        _case(
            "messaging.queue_mode-unknown",
            [(sections.MessagingConfig, "queue_mode", "queue")],
            {"messaging": {"queue_mode": "later"}},
            lambda c: c.messaging.queue_mode,
            "queue",
        ),
    ]


def _own_value_cases() -> list:
    """A present value the reader cannot use keeps the reader's value, moved default or not.

    Each is a narrowing or safe value, or one of the two constants ``config.md``
    names: a typo must not widen what the config grants when a default changes.
    """
    agent, tailscale = sections.AgentConfig, sections.TailscaleConfig
    return [
        _case(
            "slack.channels.activation-invalid",
            [(sections.ChannelConfig, "activation", "always")],
            {"slack": {"channels": {"C1": {"activation": "sometimes"}}}},
            lambda c: c.slack_channels["C1"].activation,
            "mention",
        ),
        _case(
            "dashboard.tailscale.bind_refresh_chains-malformed",
            [(tailscale, "bind_refresh_chains", False)],
            {"dashboard": {"tailscale": {"bind_refresh_chains": "yes"}}},
            lambda c: c.dashboard.tailscale.bind_refresh_chains,
            True,
        ),
        _case(
            "dashboard.tailscale.pin_scope-unrecognised",
            [(tailscale, "pin_scope", "login")],
            {"dashboard": {"tailscale": {"pin_scope": "everywhere"}}},
            lambda c: c.dashboard.tailscale.pin_scope,
            "node",
        ),
        _case(
            "agent.jail-invalid",
            [(agent, "jail", "on")],
            {"agent": {"jail": "sideways"}},
            lambda c: c.agent.jail,
            "auto",
        ),
        _case(
            "agent.dangerously_skip_permissions-malformed",
            [(agent, "dangerously_skip_permissions", True)],
            {"agent": {"dangerously_skip_permissions": "yes"}},
            lambda c: c.agent.dangerously_skip_permissions,
            False,
        ),
        _case(
            "messaging.dm_scope-unknown",
            [(sections.MessagingConfig, "dm_scope", "unified")],
            {"messaging": {"dm_scope": "everyone"}},
            lambda c: c.messaging.dm_scope,
            "per-channel-peer",
        ),
        _case(
            "slack.trusted_bot_ids-malformed",
            [(sections.SlackConfig, "trusted_bot_ids", {"B1"})],
            {"slack": {"trusted_bot_ids": "B2"}},
            lambda c: c.slack.trusted_bot_ids,
            set(),
        ),
        _case(
            "mcp.extra_path_dirs-malformed",
            [(sections.McpConfig, "extra_path_dirs", ["/opt/bin"])],
            {"mcp": {"extra_path_dirs": "/usr/bin"}},
            lambda c: c.mcp.extra_path_dirs,
            [],
        ),
        _case(
            "dashboard.browser_view_port-out-of-range",
            [(sections.DashboardConfig, "browser_view_port", 8080)],
            {"dashboard": {"browser_view_port": 70000}},
            lambda c: c.dashboard.browser_view_port,
            0,
        ),
        _case(
            "agents.session_color-malformed",
            [(sections.KiroCrewAgentConfig, "session_color", "#123456")],
            {"agents": {"crew": {"session_color": "not a colour"}}},
            lambda c: c.agents["crew"].session_color,
            "",
        ),
        _case(
            "stt.provider-retired",
            [(sections.SttConfig, "provider", "apple")],
            {"stt": {"provider": "whisper"}},
            lambda c: c.stt.provider,
            "local",
        ),
        _case(
            "stt.model-unrecognised",
            [(sections.SttConfig, "model", "small")],
            {"stt": {"model": "enormous"}},
            lambda c: c.stt.model,
            stt_models.DEFAULT_MODEL,
        ),
        _case(
            "monitoring.max_runtime_secs-malformed",
            [(sections.MonitoringConfig, "max_runtime_secs", 3600)],
            {"monitoring": {"max_runtime_secs": 0}},
            lambda c: c.monitoring.max_runtime_secs,
            DEFAULT_RUNTIME_CEILING_SECS,
        ),
    ]


def _stub_roster_migration_cases() -> list:
    """The stub roster a legacy ``mcp_gateway`` section migrates to, moved defaults or not.

    ``_resolve_stub_roster`` reproduces the stub set that install was already running,
    so with ``stub_servers`` absent an omitted ``enabled`` reads off, and an omitted
    ``poolable_servers`` an empty roster, whatever the fields declare. Each case reads
    the effective set and the configured roster.
    """
    gateway = sections.McpGatewayConfig
    moves = [(gateway, "enabled", True), (gateway, "stub_servers", ["moved"])]

    def roster(c: loader.KiroCrewConfig) -> tuple[list[str], list[str]]:
        return c.mcp_gateway.stub_servers, c.mcp_gateway.stub_roster

    return [
        _case(
            "mcp_gateway.stub-roster-enabled-omitted",
            moves,
            {"mcp_gateway": {"poolable_servers": ["fs"]}},
            roster,
            ([], []),
        ),
        _case(
            "mcp_gateway.stub-roster-enabled-false",
            moves,
            {"mcp_gateway": {"enabled": False, "poolable_servers": ["fs"]}},
            roster,
            ([], []),
        ),
        _case(
            "mcp_gateway.stub-roster-enabled-true",
            moves,
            {"mcp_gateway": {"enabled": True, "poolable_servers": ["fs"]}},
            roster,
            (["fs"], ["fs"]),
        ),
        _case(
            "mcp_gateway.stub-roster-stub_servers-present",
            moves,
            {"mcp_gateway": {"stub_servers": ["a"]}},
            roster,
            (["a"], ["a"]),
        ),
        _case(
            "mcp_gateway.stub-roster-poolable_servers-omitted",
            [*moves, (gateway, "poolable_servers", ["moved"])],
            {"mcp_gateway": {"enabled": True}},
            roster,
            ([], []),
        ),
    ]


class TestAMovedFieldDefaultMovesTheBuild:
    @pytest.mark.parametrize(("moves", "document", "read", "expected"), _move_cases())
    def test_the_build_follows_the_field(
        self, tmp_path, monkeypatch, moves, document, read, expected
    ):
        for dto, name, value in moves:
            _move_default(monkeypatch, dto, name, value)
        assert read(_build(tmp_path, document)) == expected

    @pytest.mark.parametrize(
        ("moves", "document", "read", "expected"),
        [*_own_value_cases(), *_stub_roster_migration_cases()],
    )
    def test_a_value_of_its_own_holds_when_the_default_moves(
        self, tmp_path, monkeypatch, moves, document, read, expected
    ):
        for dto, name, value in moves:
            _move_default(monkeypatch, dto, name, value)
        assert read(_build(tmp_path, document)) == expected

    def test_a_null_transcribe_vocabulary_reads_the_field_too(self, tmp_path, monkeypatch):
        _move_default(monkeypatch, sections.SttConfig, "transcribe_vocabulary", "fixture-vocab")
        cfg = _build(tmp_path, {"stt": {"transcribe_vocabulary": None}})
        assert cfg.stt.transcribe_vocabulary == "fixture-vocab"

    def test_a_move_is_undone_with_its_monkeypatch(self, tmp_path):
        [spec] = [f for f in dataclasses.fields(sections.SessionConfig) if f.name == "pool_agent"]
        shipped = spec.default
        with pytest.MonkeyPatch.context() as patched:
            _move_default(patched, sections.SessionConfig, "pool_agent", "fixture-agent")
            assert _build(tmp_path, {"session": {}}).session.pool_agent == "fixture-agent"
        assert (spec.default, sections.SessionConfig.pool_agent) == (shipped, shipped)
        assert _build(tmp_path, {"session": {}}).session.pool_agent == shipped
