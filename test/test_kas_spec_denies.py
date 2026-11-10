"""A KAS spec that pairs a grant with ``deniedCommands`` / write ``deniedPaths``
runs what the grant covers and no deny names, as on kiro-cli.

The projection records the grants those denies held back, with the denies
(``kas_spec_denies``); the gate refuses what a deny names (``spec-denies``) and
grants the rest only after the deny check clears it (``spec-builtin-grant``).
Anything Crew cannot read keeps asking.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew.acp import kas_agents
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.kas_permissions import CREW_ANSWERED_COMMANDS_KEY, CREW_SPEC_DENIES_KEY
from kiro_crew.acp.kas_spec_denies import GATE_JUDGED_MARK, projected_spec_denies
from kiro_crew.acp.types import AcpEvent
from kiro_crew.hooks import (
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    HookManager,
    HooksConfig,
    hook_gate_kwargs,
)
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp


@pytest.fixture(autouse=True)
def _no_profiles(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _project(cwd: str = "", **spec: Any) -> tuple[dict[str, Any], Any]:
    out = kas_agents.to_client_custom_agent("a", {"name": "a", "prompt": "p", **spec}, "p")
    return out, projected_spec_denies([out], "a", cwd)


def _judge(event: AcpEvent, out: dict[str, Any], grant: Any):
    event.spec_granted_builtins = frozenset(out.get("_crewAnsweredBuiltins", ()))
    event.spec_granted_commands = tuple(out.get(CREW_ANSWERED_COMMANDS_KEY, ()))
    event.spec_denied_grant = grant
    return HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))


def _shell(command: str) -> AcpEvent:
    return _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": command}
    )


def _write(path: Path) -> AcpEvent:
    return _kas_builtin_flow(
        "fs_write", title="Write", kind="edit", raw_input={"path": str(path), "content": "x"}
    )


_SHELL_SETTINGS = {
    "shell": {
        "allowedCommands": ["make build", "npm publish.*", "make clean.*"],
        "deniedCommands": ["npm publish.*", "make clean"],
    }
}


# ── shell ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("beside", [{}, {"autoAllowReadonly": True}], ids=["alone", "readonly"])
def test_an_allowed_command_no_deny_names_runs_without_a_prompt(beside) -> None:
    out, grant = _project(toolsSettings={"shell": {**_SHELL_SETTINGS["shell"], **beside}})
    assert CREW_ANSWERED_COMMANDS_KEY not in out, "only granted once the denies are checked"
    verdict = _judge(_shell("make build"), out, grant)
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


@pytest.mark.parametrize(
    "command",
    ["npm publish --tag next", "make clean now"],
)
def test_an_allowed_command_a_deny_names_is_refused(command: str) -> None:
    """``make clean now``: a deny read as a prefix too, never narrower than kiro-cli."""
    out, grant = _project(toolsSettings=_SHELL_SETTINGS)
    verdict = _judge(_shell(command), out, grant)
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-denies")


@pytest.mark.parametrize("command", ["make cleanup", "make clean-all"])
def test_a_deny_is_never_a_hard_refusal_mid_word(command: str) -> None:
    """``make clean`` refuses ``make clean now``, never ``make cleanup``: the refusal
    reads a deny only up to a word end. The grant is still withheld, so it asks."""
    out, grant = _project(toolsSettings=_SHELL_SETTINGS)
    verdict = _judge(_shell(command), out, grant)
    assert verdict.tier != "spec-denies"
    assert verdict.action != TOOL_AUTO_APPROVE


@pytest.mark.parametrize(
    "command",
    ["npm test", "make build $(id)", "make build > out", "make build && npm publish x"],
)
def test_an_unmatched_or_unreadable_command_still_asks(command: str) -> None:
    out, grant = _project(toolsSettings=_SHELL_SETTINGS)
    verdict = _judge(_shell(command), out, grant)
    assert verdict.tier != "spec-builtin-grant"
    assert verdict.action != TOOL_AUTO_APPROVE


@pytest.mark.parametrize("beside", [{}, {"autoAllowReadonly": True}], ids=["alone", "readonly"])
def test_an_allowed_tools_shell_grant_with_denies_runs_what_no_deny_names(beside) -> None:
    out, grant = _project(
        allowedTools=["shell"], toolsSettings={"shell": {"deniedCommands": ["rm .*"], **beside}}
    )
    assert (_judge(_shell("make build"), out, grant).tier) == "spec-builtin-grant"
    assert _judge(_shell("rm notes.txt"), out, grant).tier == "spec-denies"


@pytest.mark.parametrize(
    "denied",
    [["rm \\p{N}+"], [7], "rm .*", ["make clean|npm publish"], ["\\<git.*"]],
    ids=["unreadable-entry", "not-a-string", "not-a-list", "alternation", "rust-word-boundary"],
)
def test_a_deny_crew_cannot_read_keeps_the_grant_at_the_prompt(denied: Any) -> None:
    settings = {"shell": {"allowedCommands": ["make build"], "deniedCommands": denied}}
    out, grant = _project(toolsSettings=settings)
    assert grant is None
    assert _judge(_shell("make build"), out, grant).action == TOOL_ALLOW


def test_a_deny_in_rust_syntax_is_checked_and_the_grant_answers_the_rest() -> None:
    """``\\d`` is read as Rust reads it on an all-ASCII command, so the deny is
    checked here and the grant beside it answered. A command with a non-ASCII
    character is one the ASCII reading cannot judge, so it is not granted."""
    settings = {
        "shell": {"allowedCommands": ["make build", "rm .*"], "deniedCommands": [r"rm \d+"]}
    }
    out, grant = _project(toolsSettings=settings)
    assert grant is not None
    assert _judge(_shell("make build"), out, grant).tier == "spec-builtin-grant"
    assert _judge(_shell("rm 42"), out, grant).tier == "spec-denies"
    assert _judge(_shell("rm notes"), out, grant).tier == "spec-builtin-grant"
    assert _judge(_shell("rm \u0663"), out, grant).action != TOOL_AUTO_APPROVE


@pytest.mark.parametrize("order", [1, -1])
def test_an_ascii_read_deny_never_lifts_the_refusal_another_deny_makes(order: int) -> None:
    """``make \\w+`` cannot judge a non-ASCII command, but ``npm publish( .*)?`` beside
    it still names ``npm publish ./caf\u00e9``, so the gate refuses it, as kiro-cli does."""
    denied = [r"npm publish( .*)?", r"make \w+"][::order]
    settings = {"shell": {"allowedCommands": ["npm .*"], "deniedCommands": denied}}
    out, grant = _project(toolsSettings=settings)
    assert grant is not None
    verdict = _judge(_shell("npm publish ./caf\u00e9"), out, grant)
    assert (verdict.tier, verdict.action) == ("spec-denies", TOOL_DENY)
    assert _judge(_shell("npm test \u00e9"), out, grant).action != TOOL_AUTO_APPROVE


@pytest.mark.parametrize(
    "beside",
    [{"denyByDefault": True}],
)
def test_another_restriction_beside_the_deny_keeps_the_grant_at_the_prompt(beside) -> None:
    settings = {"shell": {"allowedCommands": ["make build"], "deniedCommands": ["rm .*"], **beside}}
    out, grant = _project(toolsSettings=settings)
    assert grant is None or not grant.commands


def test_a_grant_beside_denied_commands_and_deny_by_default_still_asks() -> None:
    """Both readings at once: the ``allowedCommands`` answer keeps ``denyByDefault``
    out of the restriction but still reads ``deniedCommands`` as one, and the deny
    lift reads ``denyByDefault`` as a restriction. So neither answers the matched
    command, and it asks (fail toward ask)."""
    settings = {
        "shell": {
            "allowedCommands": ["make build"],
            "deniedCommands": ["rm .*"],
            "denyByDefault": True,
        }
    }
    out, grant = _project(toolsSettings=settings)
    assert CREW_ANSWERED_COMMANDS_KEY not in out
    assert grant is None or not grant.commands
    verdict = _judge(_shell("make build"), out, grant)
    assert verdict.action != TOOL_AUTO_APPROVE and verdict.tier != "spec-builtin-grant"
    # Without the deny, the same `denyByDefault` keeps the grant.
    plain, plain_grant = _project(
        toolsSettings={"shell": {"allowedCommands": ["make build"], "denyByDefault": True}}
    )
    assert plain[CREW_ANSWERED_COMMANDS_KEY] == ["make build"]
    plain_verdict = _judge(_shell("make build"), plain, plain_grant)
    assert (plain_verdict.action, plain_verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


# ── a deny KAS did not carry, with no grant beside it ─────────────────────────

#: Entries KAS gets no rule for (no glob spelling), each one Crew reads itself.
_UNCARRIED = {"shell": {"deniedCommands": [r"docker\s+push.*", r"rm -rf \w+", "npm publish.*"]}}


@pytest.mark.parametrize(
    "command", ["docker push img:1", "docker  push", "rm -rf build", "npm publish ."]
)
def test_an_uncarried_deny_refuses_what_it_names_without_a_grant(command: str) -> None:
    """No grant beside the deny, so nothing is lifted; the record only refuses, and
    the auto-approve floor (a cron job with ``approval_mode: auto``) refuses too."""
    from kiro_crew import permission_floor

    out, grant = _project(toolsSettings=_UNCARRIED)
    assert grant is not None and not grant.builtins and not grant.commands
    assert grant.denied_commands == (r"docker\s+push.*", r"rm -rf \w+", "npm publish.*")
    event = _shell(command)
    verdict = _judge(event, out, grant)
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-denies")
    reason = permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)
    assert reason == verdict.reason and "deniedCommands" in reason
    clear = _shell("docker build .")
    _judge(clear, out, grant)
    assert (
        permission_floor.refusal_for(clear, session_key="cron:probe", security_only=False) is None
    )


@pytest.mark.parametrize("command", ["docker build .", "rm notes.txt", "make build"])
def test_an_uncarried_deny_leaves_other_commands_as_they_were(command: str) -> None:
    out, grant = _project(toolsSettings=_UNCARRIED)
    verdict = _judge(_shell(command), out, grant)
    assert verdict.tier not in ("spec-denies", "spec-builtin-grant")
    assert verdict.action == TOOL_ALLOW


def test_a_deny_kas_carries_records_nothing() -> None:
    """``npm publish.*`` reaches KAS as a deny rule, so Crew adds no record."""
    out, grant = _project(toolsSettings={"shell": {"deniedCommands": ["npm publish.*"]}})
    assert CREW_SPEC_DENIES_KEY not in out and grant is None


def test_an_unreadable_uncarried_deny_refuses_nothing_and_is_named() -> None:
    """``a|b`` is outside what Crew reads as wide as kiro-cli: it refuses nothing, as
    before, and the notice names it as not refused. The readable entry beside it
    still refuses, and is not named."""
    from kiro_crew.agent_sdk.spec_hooks import spec_keys_notice

    spec = {
        "toolsSettings": {"shell": {"deniedCommands": ["curl|wget", r"docker\s+push.*"]}},
    }
    out, grant = _project(**spec)
    assert grant is not None and grant.denied_commands == (r"docker\s+push.*",)
    assert _judge(_shell("curl x"), out, grant).tier != "spec-denies"
    assert _judge(_shell("docker push x"), out, grant).tier == "spec-denies"
    lost = kas_agents.spec_keys_without_carrier(
        {"name": "a", **spec}, translates_tools_settings=True
    )
    assert "toolsSettings.shell.deniedCommands: curl|wget" in lost
    assert rf"toolsSettings.shell.deniedCommands: docker\s+push.*{GATE_JUDGED_MARK}" in lost
    notice = spec_keys_notice("a", lost)
    assert "curl|wget" in notice and "is not refused" in notice
    assert "It does not judge any other command" in notice
    assert "a newline, a trailing &, a tab, a ( ) group" in notice
    assert "so such a command runs without a refusal." in notice
    assert "docker" not in notice.split("have no effect")[0]


@pytest.mark.parametrize("regex", [r"^.*>.*\.kiro/.*$", ".*>.*", ".*<.*", "cat <.*"])
def test_a_redirect_deny_refuses_nothing_and_is_named(regex: str) -> None:
    """The row judges a plain command and each command of a chain, never a redirect,
    so a deny aimed at a redirect could never refuse: it is not recorded, and the notice names it as not refused.
    """
    from kiro_crew.acp.kas_spec_denies import uncarried_denied_commands
    from kiro_crew.agent_sdk.spec_hooks import spec_keys_notice

    spec = {"toolsSettings": {"execute_bash": {"deniedCommands": [regex]}}}
    assert uncarried_denied_commands(spec["toolsSettings"], None) == []
    lost = kas_agents.spec_keys_without_carrier(
        {"name": "a", **spec}, translates_tools_settings=True
    )
    assert lost == [f"toolsSettings.execute_bash.deniedCommands: {regex}"]
    assert "is not refused" in spec_keys_notice("a", lost)


def test_an_uncarried_deny_keeps_a_grant_already_in_the_record() -> None:
    """A ``web_fetch.trusted`` grant recorded first stays when the shell deny joins."""
    settings = {
        "web_fetch": {"trusted": ["https://docs\\.example\\.com/.*"]},
        "shell": {"deniedCommands": [r"docker\s+push.*"]},
    }
    out, grant = _project(toolsSettings=settings)
    assert grant is not None and grant.denied_commands == (r"docker\s+push.*",)
    assert grant.web_fetch_trusted == ("https://docs\\.example\\.com/.*",)
    assert _judge(_shell("docker push x"), out, grant).tier == "spec-denies"


def test_a_carried_dollar_anchored_deny_is_not_named_beside_an_uncarried_one() -> None:
    """``git push.*$`` reaches KAS as ``git push*``; only the entry neither KAS nor
    the gate enforces may be named."""
    spec = {
        "toolsSettings": {"shell": {"deniedCommands": ["git push.*$", "curl|wget"]}},
    }
    lost = kas_agents.spec_keys_without_carrier(
        {"name": "a", **spec}, translates_tools_settings=True
    )
    assert lost == ["toolsSettings.shell.deniedCommands: curl|wget"]


def test_readable_entries_past_the_bound_are_named_as_not_refused() -> None:
    """The record keeps the first 64; each readable entry after them is named."""
    denied = [rf"docker\s+push img{i}" for i in range(66)]
    spec = {"toolsSettings": {"shell": {"deniedCommands": denied}}}
    _, grant = _project(**spec)
    assert grant is not None and len(grant.denied_commands) == 64
    lost = kas_agents.spec_keys_without_carrier(
        {"name": "a", **spec}, translates_tools_settings=True
    )
    past = [key for key in lost if key.endswith("(past the first 64)")]
    assert past == [
        rf"toolsSettings.shell.deniedCommands: docker\s+push img{i} (past the first 64)"
        for i in (64, 65)
    ]
    assert len(lost) == 66


def test_denied_commands_under_a_non_shell_key_stays_named() -> None:
    """Only a shell key's ``deniedCommands`` is enforced here; any other stays lost."""
    spec = {"toolsSettings": {"write": {"deniedCommands": ["rm .*"]}}}
    lost = kas_agents.spec_keys_without_carrier(
        {"name": "a", **spec}, translates_tools_settings=True
    )
    assert "toolsSettings.write.deniedCommands" in lost


def test_a_readable_uncarried_deny_is_named_as_refused_by_the_gate() -> None:
    """The gate refuses what such an entry names, so the notice names it with what
    the gate does not judge, never as having no effect."""
    from kiro_crew.agent_sdk.spec_hooks import spec_keys_notice

    lost = kas_agents.spec_keys_without_carrier(
        {"name": "a", **{"toolsSettings": _UNCARRIED}}, translates_tools_settings=True
    )
    # `npm publish.*` reaches KAS as a rule, so only the other two are named.
    assert sorted(lost) == sorted(
        f"toolsSettings.shell.deniedCommands: {entry}{GATE_JUDGED_MARK}"
        for entry in (r"docker\s+push.*", r"rm -rf \w+")
    )
    notice = spec_keys_notice("a", lost)
    assert "no effect" not in notice and "is not refused" not in notice
    assert "refuses a plain command they name, and each command of a plain chain" in notice


@pytest.mark.parametrize(
    ("command", "refused"),
    [
        ("cd app && docker push x", True),
        ("ls; docker push x", True),
        ("echo $(docker push x)", False),
        ("echo `docker push x`", False),
        ("docker push x > out", False),
        ("cd app\ndocker push x", False),
        ("docker push x &", False),
        ("docker\tpush x", False),
        ("(docker push x)", False),
    ],
)
def test_an_uncarried_deny_refuses_each_command_of_a_chain_not_a_substitution(
    command: str, refused: bool
) -> None:
    """What the notice says, measured at the gate: with the uncarried record and no
    grant, a chain is refused piece by piece; a substitution or a redirect is not."""
    out, grant = _project(toolsSettings=_UNCARRIED)
    tier = _judge(_shell(command), out, grant).tier
    assert (tier == "spec-denies") is refused


@pytest.mark.parametrize("entry", [r"^docker\s+push.*$", r"\Adocker\s+push.*\z"])
def test_an_anchored_uncarried_deny_is_recorded_and_refuses(entry: str) -> None:
    """kiro-cli anchors every entry itself, so an explicit anchor is no redirect or
    separator: the entry is recorded and refuses what it names."""
    out, grant = _project(toolsSettings={"shell": {"deniedCommands": [entry]}})
    assert grant is not None and grant.denied_commands == (entry,)
    assert _judge(_shell("docker push x"), out, grant).tier == "spec-denies"


# ── write paths ───────────────────────────────────────────────────────────────


def _write_spec(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    return {
        "allowedTools": ["write"],
        "toolsSettings": {
            "write": {
                "allowedPaths": [f"{tmp_path}/autopilot/**"],
                "deniedPaths": [f"{tmp_path}/autopilot/secret"],
            }
        },
        **extra,
    }


def test_an_allowed_write_no_deny_covers_runs_without_a_prompt(tmp_path: Path) -> None:
    (tmp_path / "autopilot").mkdir()
    out, grant = _project(str(tmp_path), **_write_spec(tmp_path))
    verdict = _judge(_write(tmp_path / "autopilot" / "run-report.md"), out, grant)
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


def test_a_write_a_deny_covers_is_refused(tmp_path: Path) -> None:
    (tmp_path / "autopilot" / "secret").mkdir(parents=True)
    out, grant = _project(str(tmp_path), **_write_spec(tmp_path))
    verdict = _judge(_write(tmp_path / "autopilot" / "secret" / "key.txt"), out, grant)
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-denies")


def test_a_write_through_a_link_into_a_denied_dir_is_refused(tmp_path: Path) -> None:
    (tmp_path / "autopilot" / "secret").mkdir(parents=True)
    (tmp_path / "autopilot" / "door").symlink_to(tmp_path / "autopilot" / "secret")
    out, grant = _project(str(tmp_path), **_write_spec(tmp_path))
    verdict = _judge(_write(tmp_path / "autopilot" / "door" / "key.txt"), out, grant)
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-denies")


def test_an_ungranted_write_still_asks(tmp_path: Path) -> None:
    (tmp_path / "autopilot").mkdir()
    spec = _write_spec(tmp_path)
    spec["allowedTools"] = ["read"]
    out, grant = _project(str(tmp_path), **spec)
    verdict = _judge(_write(tmp_path / "autopilot" / "run-report.md"), out, grant)
    assert verdict.action == TOOL_ALLOW


def test_two_spellings_of_one_deny_still_grant(tmp_path: Path) -> None:
    """``secret/**`` and ``./secret/**`` anchor to one glob: both entries check, so
    the grant holds and the write they name is refused."""
    (tmp_path / "secret").mkdir()
    spec = _write_spec(tmp_path)
    spec["toolsSettings"]["write"]["allowedPaths"] = [f"{tmp_path}/**"]
    spec["toolsSettings"]["write"]["deniedPaths"] = ["secret/**", "./secret/**"]
    out, grant = _project(str(tmp_path), **spec)
    assert grant is not None and len(grant.denied_write_paths) == 1
    assert _judge(_write(tmp_path / "secret" / "k"), out, grant).tier == "spec-denies"


def test_only_the_alias_kiro_cli_keeps_is_read() -> None:
    """With both shell spellings set, kiro-cli keeps one; a deny under the other is
    not one it enforces, so the gate does not refuse on it either."""
    from kiro_crew.platform.tools_settings_aliases import canonical_tools_settings

    settings = {
        "shell": {"deniedCommands": ["rm .*"]},
        "execute_bash": {"deniedCommands": ["make build"]},
    }
    kept, dropped = canonical_tools_settings(settings)
    assert len(dropped) == 1
    out, grant = _project(allowedTools=["shell"], toolsSettings=settings)
    assert grant is not None
    kept_key = next(k for k in ("shell", "execute_bash") if k in kept)
    assert list(grant.denied_commands) == kept[kept_key]["deniedCommands"]


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows strips a trailing space from a directory name"
)
def test_a_blank_looking_deny_still_refuses_what_it_names(tmp_path: Path) -> None:
    """``" "`` is a real relative path to kiro-cli; it is kept and checked, never
    dropped, so the write it covers is refused rather than granted."""
    (tmp_path / " ").mkdir()
    spec = _write_spec(tmp_path)
    spec["toolsSettings"]["write"]["allowedPaths"] = [f"{tmp_path}/**"]
    spec["toolsSettings"]["write"]["deniedPaths"] = [" ", "protected"]
    out, grant = _project(str(tmp_path), **spec)
    assert grant is not None
    assert _judge(_write(tmp_path / " " / "k"), out, grant).tier == "spec-denies"


def test_an_empty_deny_entry_keeps_the_grant_at_the_prompt(tmp_path: Path) -> None:
    spec = _write_spec(tmp_path)
    spec["toolsSettings"]["write"]["deniedPaths"] = ["", "protected"]
    _, grant = _project(str(tmp_path), **spec)
    assert grant is None


def test_a_relative_deny_with_no_session_directory_grants_nothing(tmp_path: Path) -> None:
    spec = _write_spec(tmp_path)
    spec["toolsSettings"]["write"]["deniedPaths"] = ["secret/**"]
    out, grant = _project("", **spec)
    assert grant is None


# ── plumbing ──────────────────────────────────────────────────────────────────


def test_the_record_never_reaches_the_wire() -> None:
    out, _ = _project(toolsSettings=_SHELL_SETTINGS)
    assert CREW_SPEC_DENIES_KEY in out
    params: dict[str, Any] = {}
    attach_kas_custom_agents(params, [out])
    (wire,) = params["_meta"]["kiro"]["customAgents"]
    assert CREW_SPEC_DENIES_KEY not in wire


def test_a_malformed_record_grants_nothing() -> None:
    out, _ = _project(toolsSettings=_SHELL_SETTINGS)
    out[CREW_SPEC_DENIES_KEY]["deniedCommands"] = "npm publish.*"
    assert projected_spec_denies([out], "a", "") is None
