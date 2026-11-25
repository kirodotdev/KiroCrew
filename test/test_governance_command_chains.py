"""``command_chains: each`` checks each command of a shell chain against ``commands``.

Under an allow-mode ``commands`` list, ``*`` in ``git *`` also matches ``&&``, ``;``
and ``|``, so the list read as one string admits ``git status && python3 -c x``. The
opt-in ordinal ``command_chains`` (``whole`` < ``each``) makes a policy or a profile
ask ``commands`` about every command of the chain too, at the tool-call gate (any
title shape) and for a cron ``command`` body, and it only ever refuses more.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_kas_allowed_commands import (  # noqa: F401 - _no_profiles is an autouse fixture
    _install_governance,
    _judge,
    _no_profiles,
    _shell,
)

from kiro_crew import mcp_cron
from kiro_crew.acp._dispatch import build_permission_event, parse_session_update
from kiro_crew.acp.types import AcpEvent, JsonRpcMessage
from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.hooks import TOOL_DENY
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import assert_governance_floor, parse_policy, parse_profile

_SESSION = "dashboard:chains"
_GIT_ONLY = {"commands": {"mode": "allow", "allow": ["git *"]}}


def _kiro_cli_shell(command: str) -> AcpEvent:
    """A kiro-cli shell call as the wire carries it: titled ``Running: <command>``."""
    caches: dict[str, Any] = {
        name: {}
        for name in (
            "tool_input_cache",
            "shell_cache",
            "raw_params_cache",
            "mcp_server_name_cache",
            "tool_name_cache",
            "tool_kind_cache",
        )
    }
    title = f"Running: {command}"
    parse_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-cli",
            "title": title,
            "kind": "execute",
            "status": "pending",
            "rawInput": {"command": command},
        },
        **caches,
    )
    msg = JsonRpcMessage(
        id=7,
        method="session/request_permission",
        params={
            "sessionId": "s",
            "toolCall": {"toolCallId": "tc-cli", "status": "pending", "title": title},
            "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
        },
    )
    event, _ = build_permission_event(msg, **caches)
    assert event.shell_command == command
    return event


def _refused_by_governance(event: AcpEvent) -> bool:
    verdict = _judge(event, [], session_key=_SESSION)
    return (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def _bind_dashboard_profile(tmp_path: Any, body: dict[str, Any]) -> None:
    profile = {"name": "chains", "bind": {"type": "surface", "id": "dashboard"}, **body}
    (tmp_path / "profiles" / "chains.json").write_text(json.dumps(profile), encoding="utf-8")
    gp.reset_store()


def _bind_cron_profile(tmp_path: Any, body: dict[str, Any]) -> None:
    profile = {"name": "cron-chains", "bind": {"type": "surface", "id": "cron"}, **body}
    (tmp_path / "profiles" / "cron-chains.json").write_text(json.dumps(profile), encoding="utf-8")
    gp.reset_store()


def _cron_job(command: str) -> CronJob:
    return CronJob(
        id="cc1",
        name="chains",
        message="",
        schedule=CronSchedule(kind="every", every_secs=3600),
        command=command,
    )


def _cron_refusal(command: str) -> tuple[str | None, str | None]:
    """The authoring and the fire-time verdict on a cron ``command`` body."""
    return mcp_cron._vet_shell_command(command), mcp_cron.vet_job_at_fire_time(_cron_job(command))


# ── the tool-call gate, a title that carries the command (kiro-cli) ───────────


def test_each_refuses_a_chained_unlisted_command_in_a_kiro_cli_title() -> None:
    _install_governance({**_GIT_ONLY, "command_chains": "each"})
    assert _refused_by_governance(_kiro_cli_shell("git status && python3 -c x"))
    assert not _refused_by_governance(_kiro_cli_shell("git status && git log"))
    assert _refused_by_governance(_kiro_cli_shell('git status && echo "$(id)"'))


def test_each_names_the_refusing_command() -> None:
    _install_governance({**_GIT_ONLY, "command_chains": {"value": "each"}})
    verdict = _judge(_kiro_cli_shell("git status && python3 -c x"), [], session_key=_SESSION)
    assert verdict.reason.startswith("Blocked by governance policy: ")
    assert "'python3 -c x' not in allow-set" in verdict.reason


@pytest.mark.parametrize("chains", [None, "whole"])
def test_whole_or_unnamed_reads_a_kiro_cli_title_as_one_string(chains: str | None) -> None:
    _install_governance({**_GIT_ONLY, **({"command_chains": chains} if chains else {})})
    assert not _refused_by_governance(_kiro_cli_shell("git status && python3 -c x"))
    assert not _refused_by_governance(_kiro_cli_shell("git status && git log"))
    assert not _refused_by_governance(_kiro_cli_shell('git status && echo "$(id)"'))


# ── the tool-call gate, a title that does not carry it (KAS ``Run Command``) ──


def test_each_refuses_an_unsplittable_command_an_allow_all_list_admits() -> None:
    _install_governance({"commands": {"mode": "allow", "allow": ["*"]}, "command_chains": "each"})
    assert _refused_by_governance(_shell('git log "$(id)"'))
    assert not _refused_by_governance(_shell("git status && git log"))


def test_whole_admits_what_an_allow_all_list_admitted() -> None:
    _install_governance({"commands": {"mode": "allow", "allow": ["*"]}})
    assert not _refused_by_governance(_shell('git log "$(id)"'))


# ── a profile turns it on, and it only refuses more ───────────────────────────


def test_a_profile_each_under_a_whole_policy_refuses_the_chain(tmp_path: Any) -> None:
    _install_governance(_GIT_ONLY)
    _bind_dashboard_profile(tmp_path, {"command_chains": "each"})
    assert _refused_by_governance(_kiro_cli_shell("git status && python3 -c x"))
    assert not _refused_by_governance(_kiro_cli_shell("git status && git log"))
    # Strictly additive: what the list refuses on its own is still refused.
    assert _refused_by_governance(_kiro_cli_shell("python3 -c x"))


# ── deny mode ─────────────────────────────────────────────────────────────────


_DENY_RM = {"commands": {"mode": "deny", "deny": ["rm *"]}}


def test_each_lets_a_deny_glob_meet_a_chained_command_at_the_gate() -> None:
    _install_governance({**_DENY_RM, "command_chains": "each"})
    assert _refused_by_governance(_kiro_cli_shell("git status && rm -rf x"))


def test_whole_deny_glob_does_not_meet_a_chained_command_at_the_gate() -> None:
    _install_governance(_DENY_RM)
    assert not _refused_by_governance(_kiro_cli_shell("git status && rm -rf x"))


def test_each_lets_a_deny_glob_meet_a_chained_command_in_a_cron_body() -> None:
    _install_governance({**_DENY_RM, "command_chains": "each"})
    reason = mcp_cron._vet_command_governance("git status && rm -rf x")
    assert reason is not None and "blocked by governance policy" in reason


def test_whole_deny_glob_does_not_meet_a_chained_command_in_a_cron_body() -> None:
    _install_governance(_DENY_RM)
    assert mcp_cron._vet_command_governance("git status && rm -rf x") is None


# ── the cron ``command`` body, at authoring and at fire time ──────────────────


def test_each_refuses_a_chained_unlisted_cron_command_at_authoring_and_fire() -> None:
    _install_governance({**_GIT_ONLY, "command_chains": "each"})
    authoring, fire = _cron_refusal("git status && python3 -c x")
    assert authoring is not None and "cron command blocked by governance policy" in authoring
    assert fire is not None and "cron command blocked by governance policy" in fire


@pytest.mark.parametrize("command", ["git status && git log", "git status \\\n&& git log"])
def test_each_accepts_a_cron_chain_whose_every_command_is_listed(command: str) -> None:
    _install_governance({**_GIT_ONLY, "command_chains": "each"})
    assert _cron_refusal(command) == (None, None)


def test_whole_accepts_a_chained_unlisted_cron_command() -> None:
    _install_governance(_GIT_ONLY)
    assert _cron_refusal("git status && python3 -c x") == (None, None)


def test_a_cron_profile_each_refuses_a_chained_unlisted_cron_command(tmp_path: Any) -> None:
    _install_governance(_GIT_ONLY)
    _bind_cron_profile(tmp_path, {"command_chains": "each"})
    reason = mcp_cron._vet_command_governance("git status && python3 -c x")
    assert reason is not None and "blocked by governance policy" in reason
    # The profile loaded (an unreadable one would refuse everything).
    assert mcp_cron._vet_command_governance("git status && git log") is None


# ── composition ───────────────────────────────────────────────────────────────


def test_a_profile_whole_under_a_policy_each_fails_the_floor() -> None:
    ceiling = parse_policy({"version": 1, "boot": {}, "command_chains": "each"})
    with pytest.raises(PlatformCompositionError):
        assert_governance_floor(ceiling, parse_profile({"name": "p", "command_chains": "whole"}))
    assert_governance_floor(ceiling, parse_profile({"name": "p", "command_chains": "each"}))


def test_an_unknown_command_chains_value_is_refused() -> None:
    assert parse_policy({"version": 1, "boot": {}, "command_chains": "whole"}) is not None
    with pytest.raises(PlatformCompositionError):
        parse_policy({"version": 1, "boot": {}, "command_chains": "parts"})
