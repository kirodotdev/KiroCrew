# SPDX-License-Identifier: Apache-2.0
"""A Bot's worker carries the agent's own tool grants, as an ordinary KAS session does.

Adding an agent's abilities to a Bot copies its ``toolsSettings`` into the Bot's
file. A read or write ``allowedPaths``, shell ``denyByDefault`` and
``autoAllowReadonly``, and ``subagent.trustedAgents`` now land in the worker's
merged spec, and the same projection, session record and gate that answer an
ordinary KAS session answer the worker's requests. A ``deniedCommands`` list is
carried whole; when an entry of it is one neither KAS nor the gate can judge, the
worker's gate refuses every shell command that is not read-only, on every approval
policy, Trust included. These tests drive the real Bot-file read, merge,
projection, session record and gate on recorded-shape KAS requests.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_kas_spec_builtin_grant import _handle, _permission
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew import mate_spec_policy, mate_worker
from kiro_crew.acp import kas_agents
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.harness.kas import KasHarness, mate_worker_projection
from kiro_crew.acp.kas_permissions import CREW_ONLY_ENTRY_KEYS, CREW_UNJUDGED_DENIES_KEY
from kiro_crew.acp.kas_spec_denies import GATE_JUDGED_MARK, with_unjudged_denies
from kiro_crew.acp.types import AcpEvent
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.config import paths
from kiro_crew.hooks import (
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    HookManager,
    HooksConfig,
    ToolHookResult,
    hook_gate_kwargs,
)
from kiro_crew.mate_spec_policy import SpecPolicyUncarried
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp

WORKER = "kirocrew-mate-worker"

WORKER_BASE = {
    "name": WORKER,
    "prompt": "You are the worker.",
    "tools": ["*"],
    "allowedTools": [],
    "mcpServers": {},
}

#: A reported agent's ``deniedCommands``, as written. The three ``rm`` entries hold
#: ``|`` inside a group, so neither KAS (globs only) nor the gate can judge them.
#: The final three entries are checked per command because they use ASCII classes.
MIXED_DENIED = [
    ".*rm\\s+-r(f)?\\s+/([^/\\s]+)?/?(\\s|$).*",
    ".*rm\\s+-r(f)?\\s+~($|\\s|/).*",
    ".*rm\\s+-r(f)?\\s+\\.($|\\s).*",
    ".*mkfs.*",
    ".*dd\\s+if=.*",
    ".*git\\s+push.*",
    ".*git\\s+clean\\s+-[^n]*[dxf].*",
]

#: The fields the session handle stamps on its own agent's KAS request.
_STAMPED = (
    "spec_granted_builtins",
    "spec_granted_commands",
    "spec_shell_allowlist",
    "spec_write_allowlist",
    "spec_write_cwd",
    "spec_denied_grant",
    "spec_unjudged_denies",
    "spec_shell_readonly",
    "spec_granted_paths",
    "spec_granted_read_paths",
)


@pytest.fixture(autouse=True)
def _ungoverned(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    """No governance profile and no ceiling: those are their own modules' tests."""
    from kiro_crew.platform import governance

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    _floor_monkeypatch.setattr(
        governance, "sanitize_agent_config_governance", lambda spec, **_k: None
    )
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """The worker session's directory, with ``inside/in.txt`` and ``outside/out.txt``."""
    root = Path(os.path.realpath(tmp_path)) / "work"
    for name, text in (("inside/in.txt", "in"), ("outside/out.txt", "out")):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Writes the Bot ``pixel``'s own file, whose worker session is ``worker-1``."""
    user, mates = tmp_path / "agents", tmp_path / "mates" / "agents"
    user.mkdir()
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id, config=None: "pixel")
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key: SimpleNamespace(member_id="pixel"),
    )

    def write(settings: dict[str, Any], **extra: Any) -> None:
        spec = {"name": "pixel", "prompt": "", "toolsSettings": settings, **extra}
        (mates / "pixel.json").write_text(json.dumps(spec), encoding="utf-8")

    return write


def _worker_batch(tmp_path: Path) -> list[dict[str, Any]]:
    """The batch a worker session registers: the Bot file read and merged
    (``mate_worker_projection``), projected, and recorded as the KAS harness does."""
    spec = mate_worker_projection(dict(WORKER_BASE), tmp_path, "worker-1")
    batch = kas_agents.build_kas_custom_agents(tmp_path, WORKER, spec, session_key="worker-1")
    return with_unjudged_denies(batch, WORKER, spec)


def _own_request(event: AcpEvent, batch: list[dict[str, Any]], cwd: Path) -> AcpEvent:
    """*event* as the worker's session handle stamps its own agent's request."""
    handle = _handle(ACP_BACKEND_KAS)
    handle._bound_cwd = str(cwd)
    KasHarness.record_session_projection(object.__new__(KasHarness), handle, batch, WORKER)
    stamped = handle._build_permission_event(_permission("own-sid"))
    assert stamped is not None
    for field in _STAMPED:
        setattr(event, field, getattr(stamped, field))
    return event


def _read(path: str) -> AcpEvent:
    """A KAS ``read_file`` request, shaped like the captured frame
    (``consent.capability`` = ``fs_read``, ``resource`` = the relative path)."""
    return _kas_builtin_flow("read_file", title="Read File", kind="read", raw_input={"path": path})


def _shell(command: str) -> AcpEvent:
    return _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": command}
    )


def _write(path: str) -> AcpEvent:
    raw = {"path": path, "command": "create"}
    return _kas_builtin_flow("fs_write", title="Write", kind="edit", raw_input=raw)


def _judge(event: AcpEvent) -> ToolHookResult:
    return HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))


# ── 1-3. the worker merge carries the keys ───────────────────────────────────


@pytest.mark.parametrize(
    "settings",
    [
        {"shell": {"denyByDefault": True, "allowedCommands": ["^echo allowed$"]}},
        {"shell": {"autoAllowReadonly": True}},
        {"read": {"allowedPaths": ["inside/**"]}},
        {"write": {"allowedPaths": ["inside/**"]}},
        {"subagent": {"trustedAgents": ["gpu-coder"]}},
        {"shell": {"deniedCommands": MIXED_DENIED}},
    ],
)
def test_each_key_is_carried_onto_the_worker_as_written(settings: dict[str, Any]) -> None:
    merged = dict(WORKER_BASE)
    mate_spec_policy.layer_policy(merged, {"toolsSettings": settings}, worker=True)
    assert merged["toolsSettings"] == settings


@pytest.mark.parametrize(
    ("settings", "named"),
    [
        ({"use_subagent": {"trustedAgents": ["x"]}}, "toolsSettings.use_subagent.trustedAgents"),
        ({"fs_write": {"fallbackAction": "deny"}}, "toolsSettings.fs_write.fallbackAction"),
        ({"web_fetch": {"trusted": ["docs"]}}, "toolsSettings.web_fetch.trusted"),
    ],
)
def test_every_other_key_is_still_refused_by_name(settings: dict[str, Any], named: str) -> None:
    with pytest.raises(SpecPolicyUncarried) as caught:
        mate_spec_policy.layer_policy(dict(WORKER_BASE), {"toolsSettings": settings}, worker=True)
    assert named in caught.value.fields


def test_a_deny_list_with_unjudged_entries_is_carried_and_they_are_named() -> None:
    spec = {"toolsSettings": {"shell": {"deniedCommands": MIXED_DENIED}}}
    assert mate_spec_policy.unjudged_denies(spec) == MIXED_DENIED[:3]
    judged = {"toolsSettings": {"shell": {"deniedCommands": MIXED_DENIED[3:]}}}
    assert mate_spec_policy.unjudged_denies(judged) == []


# ── 4. the gate reads the worker's merged spec ───────────────────────────────


def test_a_read_inside_the_allowed_paths_is_granted_and_one_outside_is_not(
    bot, work, tmp_path
) -> None:
    """Outside the paths the grant does not answer; the read goes on to the rows
    below it, as on an ordinary KAS session (``read-only`` settles a plain read)."""
    bot({"read": {"allowedPaths": ["inside/**"]}})
    batch = _worker_batch(tmp_path)
    inside = _judge(_own_request(_read("inside/in.txt"), batch, work))
    assert (inside.action, inside.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")
    outside = _judge(_own_request(_read("outside/out.txt"), batch, work))
    assert outside.tier != "spec-builtin-grant"


def test_without_the_key_the_read_inside_is_not_granted(bot, work, tmp_path) -> None:
    bot({})
    verdict = _judge(_own_request(_read("inside/in.txt"), _worker_batch(tmp_path), work))
    assert verdict.tier != "spec-builtin-grant"


def test_a_write_inside_the_allowed_paths_runs_and_one_outside_asks(bot, work, tmp_path) -> None:
    bot({"write": {"allowedPaths": ["inside/**"]}})
    batch = _worker_batch(tmp_path)
    inside = _judge(_own_request(_write("inside/w.txt"), batch, work))
    assert (inside.action, inside.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")
    outside = _judge(_own_request(_write("outside/w.txt"), batch, work))
    assert outside.action == TOOL_ALLOW


def test_deny_by_default_refuses_touch_and_auto_allow_readonly_runs_ls(bot, work, tmp_path) -> None:
    bot(
        {
            "shell": {
                "denyByDefault": True,
                "autoAllowReadonly": True,
                "allowedCommands": ["^echo allowed$"],
            }
        }
    )
    batch = _worker_batch(tmp_path)
    touch = _judge(_own_request(_shell(f"touch {work}/t.txt"), batch, work))
    assert (touch.action, touch.tier) == (TOOL_DENY, "spec-shell-allowlist")
    ls = _judge(_own_request(_shell(f"ls {work}"), batch, work))
    assert ls.action == TOOL_AUTO_APPROVE
    echo = _judge(_own_request(_shell("echo allowed"), batch, work))
    assert (echo.action, echo.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


def test_trusted_agents_land_as_a_subagent_allow_on_the_wire(bot, tmp_path) -> None:
    bot({"subagent": {"trustedAgents": ["gpu-coder"]}})
    params: dict[str, Any] = {}
    attach_kas_custom_agents(params, _worker_batch(tmp_path))
    rules = params["_meta"]["kiro"]["customAgents"][0]["permissions"]["rules"]
    assert {"capability": "subagent", "match": ["gpu-coder"], "effect": "allow"} in rules


# ── 5-6. entries nobody judges hold the worker's shell ───────────────────────


def test_unjudged_denies_refuse_a_command_that_is_not_read_only(bot, work, tmp_path) -> None:
    bot({"shell": {"deniedCommands": MIXED_DENIED}})
    batch = _worker_batch(tmp_path)
    record = batch[0][CREW_UNJUDGED_DENIES_KEY]
    assert [item["entry"] for item in record] == [*MIXED_DENIED[:3], *MIXED_DENIED[4:]]
    assert [item["conditional"] for item in record] == [False] * 3 + [True] * 3
    rm = _judge(_own_request(_shell("rm -rf /tmp/x"), batch, work))
    assert (rm.action, rm.tier) == (TOOL_DENY, "spec-unjudged-denies")
    assert "Kiro Crew cannot check some of its deniedCommands" in rm.reason
    ls = _judge(_own_request(_shell(f"ls {work}"), batch, work))
    assert ls.action == TOOL_AUTO_APPROVE


@pytest.mark.parametrize(
    ("pattern", "command"),
    [
        (r"touch \w+", "touch résumé"),
        (r"touch \d+", "touch ١"),
        (r"touch\s+file", "touch\u00a0file"),
        (r"touch \b.+", "touch résumé"),
        (r"(?i)touch k", "touch K"),
    ],
)
def test_ascii_only_denies_hold_unicode_commands_under_trust(
    bot, work, tmp_path, pattern, command
) -> None:
    from kiro_crew.acp.kas_spec_denies import GATE_JUDGED_MARK, unread_denied_commands
    from kiro_crew.platform import allowed_commands

    settings = {"shell": {"deniedCommands": [pattern]}}
    bot(settings)
    parsed = allowed_commands.parse_deny(pattern)
    assert parsed is not None and parsed.ascii_only
    assert allowed_commands.denied_match([pattern], command) is None
    assert unread_denied_commands(settings) == [
        f"shell.deniedCommands: {pattern}{GATE_JUDGED_MARK}"
    ]
    batch = _worker_batch(tmp_path)
    event = _own_request(_shell(command), batch, work)
    trusted = HookManager(HooksConfig(auto_approve_tools=["*"]))
    verdict = trusted.on_tool_call(event.title, **hook_gate_kwargs(event))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-unjudged-denies")
    record = batch[0][CREW_UNJUDGED_DENIES_KEY]
    assert len(record) == 1 and record[0]["entry"] == pattern
    assert record[0]["conditional"] is True
    assert event.spec_unjudged_denies == tuple(record)
    assert mate_spec_policy.unjudged_denies({"toolsSettings": settings}) == []


@pytest.mark.parametrize("pattern", [r"touch \w+", r"touch\s+file"])
def test_ascii_commands_keep_normal_deny_judgement(bot, work, tmp_path, pattern) -> None:
    settings = {"shell": {"deniedCommands": [pattern]}}
    bot(settings)
    batch = _worker_batch(tmp_path)
    trusted = HookManager(HooksConfig(auto_approve_tools=["*"]))
    for command, expected in (
        ("touch file", (TOOL_DENY, "spec-denies")),
        ("touch file && true", (TOOL_DENY, "spec-denies")),
        ("mkdir folder", (TOOL_AUTO_APPROVE, "operator-grants")),
        ("mkdir folder && true", (TOOL_AUTO_APPROVE, "operator-grants")),
    ):
        event = _own_request(_shell(command), batch, work)
        verdict = trusted.on_tool_call(event.title, **hook_gate_kwargs(event))
        assert (verdict.action, verdict.tier) == expected
    assert mate_spec_policy.unjudged_denies({"toolsSettings": settings}) == []


def test_authored_marker_cannot_disguise_a_truly_unjudged_deny(bot, work, tmp_path) -> None:
    pattern = GATE_JUDGED_MARK + r"touch \w+ > out"
    bot({"shell": {"deniedCommands": [pattern]}})
    event = _own_request(_shell("mkdir folder"), _worker_batch(tmp_path), work)
    verdict = HookManager(HooksConfig(auto_approve_tools=["*"])).on_tool_call(
        event.title, **hook_gate_kwargs(event)
    )
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-unjudged-denies")


@pytest.mark.parametrize(
    ("pattern", "conditional"),
    [("touch résumé", False), ("touch [a-z]+", True), ("touch .*", False)],
)
def test_fully_judged_denies_do_not_hold_unrelated_worker_commands(
    bot, work, tmp_path, pattern, conditional
) -> None:
    settings = {"shell": {"deniedCommands": [pattern]}}
    bot(settings)
    batch = _worker_batch(tmp_path)
    record = batch[0].get(CREW_UNJUDGED_DENIES_KEY, [])
    assert [item["entry"] for item in record] == ([pattern] if conditional else [])
    assert all(item["conditional"] for item in record)
    assert mate_spec_policy.unjudged_denies({"toolsSettings": settings}) == []
    event = _own_request(_shell("mkdir folder"), batch, work)
    verdict = HookManager(HooksConfig(auto_approve_tools=["*"])).on_tool_call(
        event.title, **hook_gate_kwargs(event)
    )
    assert verdict.action == TOOL_AUTO_APPROVE


@pytest.mark.parametrize(
    ("pattern", "ascii_only"),
    [
        (".*mkfs.*", False),
        ("rm -rf /", False),
        (".*touch [a-z]+.*", False),
        (r"rm\s+-rf\s+/", True),
        (r".*git\s+push\s+--force.*", True),
        (r"sudo\s+.*", True),
    ],
)
def test_which_deny_entries_are_judged_only_for_ascii_commands(
    bot, work, tmp_path, pattern, ascii_only
) -> None:
    """KAS-carried denies need no record; every gate-only deny is conditional."""
    from kiro_crew.platform import allowed_commands

    parsed = allowed_commands.parse_deny(pattern)
    assert parsed is not None and parsed.ascii_only is ascii_only
    settings = {"shell": {"deniedCommands": [pattern]}}
    bot(settings)
    batch = _worker_batch(tmp_path)
    assert mate_spec_policy.unjudged_denies({"toolsSettings": settings}) == []
    from kiro_crew.acp.kas_permissions import shell_deny_carried, shell_deny_globs

    if shell_deny_carried(pattern, shell_deny_globs(batch[0].get("permissions"))):
        assert CREW_UNJUDGED_DENIES_KEY not in batch[0]
    else:
        record = batch[0][CREW_UNJUDGED_DENIES_KEY]
        assert [item["entry"] for item in record] == [pattern]
        assert record[0]["conditional"] is True
    event = _own_request(_shell("mkdir folder"), batch, work)
    verdict = HookManager(HooksConfig(auto_approve_tools=["*"])).on_tool_call(
        event.title, **hook_gate_kwargs(event)
    )
    assert verdict.action == TOOL_AUTO_APPROVE


@pytest.mark.parametrize(
    ("pattern", "command"),
    [
        (".*touch [a-z]+.*", "touch file > log"),
        (r".*touch\s+file.*", "touch file # comment && true"),
    ],
)
def test_worker_holds_commands_the_shared_deny_judge_declines(
    bot, work, tmp_path, pattern, command
) -> None:
    settings = {"shell": {"deniedCommands": [pattern]}}
    bot(settings)
    batch = _worker_batch(tmp_path)
    trusted = HookManager(HooksConfig(auto_approve_tools=["*"]))
    for text, expected in (
        (command, (TOOL_DENY, "spec-unjudged-denies")),
        ("touch file", (TOOL_DENY, "spec-denies")),
        ("mkdir folder", (TOOL_AUTO_APPROVE, "operator-grants")),
    ):
        event = _own_request(_shell(text), batch, work)
        verdict = trusted.on_tool_call(event.title, **hook_gate_kwargs(event))
        assert (verdict.action, verdict.tier) == expected
    assert mate_spec_policy.unjudged_denies({"toolsSettings": settings}) == []

    # Ordinary sessions still decline these shapes instead of adding a refusal.
    spec = {"name": "probe", "prompt": "p", "toolsSettings": settings}
    ordinary = [kas_agents.to_client_custom_agent("probe", spec, "p")]
    handle = _handle(ACP_BACKEND_KAS)
    handle._bound_cwd = str(work)
    KasHarness.record_session_projection(object.__new__(KasHarness), handle, ordinary, "probe")
    event = _shell(command)
    stamped = handle._build_permission_event(_permission("own-sid"))
    assert stamped is not None
    for field in _STAMPED:
        setattr(event, field, getattr(stamped, field))
    assert event.spec_denied_grant is not None
    assert event.spec_unjudged_denies == ()
    verdict = trusted.on_tool_call(event.title, **hook_gate_kwargs(event))
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "operator-grants")


def test_an_ordinary_session_with_the_same_spec_is_unchanged(work) -> None:
    """The record is set for a Bot's worker only: an ordinary session asks, as on main."""
    spec = {
        "name": "probe",
        "prompt": "p",
        "toolsSettings": {"shell": {"deniedCommands": MIXED_DENIED}},
    }
    batch = [kas_agents.to_client_custom_agent("probe", spec, "p")]
    assert CREW_UNJUDGED_DENIES_KEY not in batch[0]
    event = _shell("rm -rf /tmp/x")
    handle = _handle(ACP_BACKEND_KAS)
    handle._bound_cwd = str(work)
    KasHarness.record_session_projection(object.__new__(KasHarness), handle, batch, "probe")
    stamped = handle._build_permission_event(_permission("own-sid"))
    assert stamped is not None and stamped.spec_unjudged_denies == ()
    assert _judge(event).action == TOOL_ALLOW


def test_the_unjudged_record_never_reaches_the_wire(bot, tmp_path) -> None:
    assert CREW_UNJUDGED_DENIES_KEY in CREW_ONLY_ENTRY_KEYS
    bot({"shell": {"deniedCommands": MIXED_DENIED}})
    params: dict[str, Any] = {}
    attach_kas_custom_agents(params, _worker_batch(tmp_path))
    assert CREW_UNJUDGED_DENIES_KEY not in json.dumps(params)


# ── step 0: nothing forces a person's answer under Trust ─────────────────────


def test_the_gate_has_no_result_a_trusted_session_must_hand_to_a_person() -> None:
    """The operator confirm list only withholds a grant: its match is an ordinary
    ask (``TOOL_ALLOW``), which Trust approves. So an unjudged deny must refuse."""
    assert {f.name for f in dataclasses.fields(ToolHookResult)} == {
        "action",
        "reason",
        "security_deny",
        "identity_grant",
        "read_only",
        "tier",
    }
    call = _shell("rm -rf /tmp/x")
    confirmed = HookManager(
        HooksConfig(auto_approve_tools=["*"], confirm_tools=["*"])
    ).on_tool_call(call.title, **hook_gate_kwargs(call))
    assert confirmed.action == TOOL_ALLOW


@pytest.mark.parametrize(
    "alias", ["shell", "execute_bash", "executeBash", "executeCmd", "execute_cmd"]
)
@pytest.mark.parametrize(
    "pattern",
    [
        "(touch|mkdir).*" + GATE_JUDGED_MARK,
        GATE_JUDGED_MARK + "(touch|mkdir).*",
        "(touch|mkdir).* (past the first 64)",
    ],
)
def test_authored_notice_text_never_classifies_a_deny(bot, work, tmp_path, alias, pattern):
    settings = {alias: {"deniedCommands": [pattern]}}
    bot(settings)
    event = _own_request(
        _shell("touch file # refused by Kiro Crew's gate"), _worker_batch(tmp_path), work
    )
    verdict = HookManager(HooksConfig(auto_approve_tools=["*"])).on_tool_call(
        event.title, **hook_gate_kwargs(event)
    )
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-unjudged-denies")
    assert mate_spec_policy.unjudged_denies({"toolsSettings": settings}) == [pattern]


def test_notice_renders_structured_denies_without_reclassifying_authored_text():
    from kiro_crew.agent_sdk.spec_hooks import spec_keys_notice

    raw = "(touch|mkdir).*" + GATE_JUDGED_MARK
    lost = kas_agents.spec_keys_without_carrier(
        {"toolsSettings": {"shell": {"deniedCommands": [raw]}}},
        translates_tools_settings=True,
    )
    record = json.loads(lost[0])["deniedCommand"]
    assert record["entry"] == raw and record["conditional"] is False
    notice = spec_keys_notice("probe", lost)
    assert raw in notice and "is not refused" in notice
    assert "refuses a plain command" not in notice


@pytest.mark.parametrize("value", [[{}], [{"entry": "touch", "conditional": True}], "bad"])
def test_malformed_worker_deny_record_cannot_remove_a_hold(value):
    from kiro_crew.agent_sdk.kas_tool_view import unjudged_denies_for_command, worker_deny_records

    assert unjudged_denies_for_command(worker_deny_records(value), "mkdir folder")


@pytest.mark.parametrize("entries", [[r"docker\s+push.*"], [MIXED_DENIED[0]]])
def test_a_bot_workers_notice_says_its_gate_refuses_what_the_denies_hold(entries):
    """On a Bot's worker the gate holds a command these entries cannot be checked
    against (``spec-unjudged-denies``), so its notice never says they have no effect
    or that such a command runs. An ordinary session keeps today's wording."""
    from kiro_crew.agent_sdk.spec_hooks import spec_keys_notice

    spec = {"toolsSettings": {"shell": {"deniedCommands": entries}}}
    lost = kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True)
    worker = spec_keys_notice(WORKER, lost)
    assert entries[0] in worker
    for said in ("no effect", "runs without a refusal", "is not refused"):
        assert said not in worker
    assert "this Bot's worker refuses" in worker
    ordinary = spec_keys_notice("probe", lost)
    assert "this Bot's worker" not in ordinary
    assert ("runs without a refusal" in ordinary) or ("is not refused" in ordinary)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [WORKER, "ordinary-deny-agent"])
async def test_session_extras_attaches_unjudged_denies_only_to_the_worker(
    bot, work, monkeypatch, agent
) -> None:
    from kiro_crew import agent as agent_mod
    from kiro_crew.agent_sdk import kas_model_catalog

    settings = {"shell": {"deniedCommands": ["(touch|mkdir).*"]}}
    bot(settings)
    spec = {**WORKER_BASE, "name": agent}
    if agent != WORKER:
        spec["toolsSettings"] = settings
    (paths.kiro_agents_dir() / f"{agent}.json").write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    monkeypatch.setattr(kas_model_catalog, "vault_identity", lambda: None)

    extras = await KasHarness().session_extras(agent, work_dir=work, session_key="worker-1")

    own = next(entry for entry in extras.custom_agents if entry["id"] == agent)
    if agent == WORKER:
        record = own[CREW_UNJUDGED_DENIES_KEY]
        assert [item["entry"] for item in record] == ["(touch|mkdir).*"]
        assert record[0]["conditional"] is False
    else:
        assert CREW_UNJUDGED_DENIES_KEY not in own
