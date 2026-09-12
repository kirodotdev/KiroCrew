"""Compact shipped prompts retain standalone operational contracts.

Text guards cannot prove model compliance. They protect the instructions most
likely to disappear during compression; the worked plan also uses the real
parser, and the size ceiling applies to each selectable prompt independently.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "src" / "kiro_crew" / "config"
# Absolute UTF-8 source-byte budgets, not token counts or live git baselines.
# A maintainer may raise a budget in a reviewed change when a new rule earns
# its space. Preserve the operational clauses below rather than cutting them
# to fit; their tests, not a size limit, check the retained text contracts.
PROMPT_BYTE_CEILINGS = {"prompt.md": 40_725, "prompt-orchestrator.md": 23_448}


def _read(name: str = "prompt.md") -> str:
    return (CONFIG / name).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    body = text.split(heading, 1)[1]
    return body.split("\n## ", 1)[0]


def _require(text: str, *patterns: str) -> None:
    """Match semantic clauses without pinning prose wrapping or Markdown weight."""
    flat = " ".join(text.replace("**", "").split())
    for pattern in patterns:
        assert re.search(pattern, flat, re.IGNORECASE), f"missing contract: {pattern}"


@pytest.mark.parametrize("name", PROMPT_BYTE_CEILINGS)
def test_each_selected_prompt_stays_under_its_byte_ceiling(name: str) -> None:
    # Normalize checkout line endings so Windows measures the same shipped text.
    size = len(_read(name).encode("utf-8"))
    assert size <= PROMPT_BYTE_CEILINGS[name], (name, size, PROMPT_BYTE_CEILINGS[name])


@pytest.mark.parametrize("name", PROMPT_BYTE_CEILINGS)
def test_template_slots_remain_complete_and_unique(name: str) -> None:
    text = _read(name)
    assert re.findall(r"\{\{([A-Z_]+)\}\}", text) == [
        "MAX_SUBAGENTS",
        "WIDGET_BLOCK",
        "VERBOSITY_BLOCK",
    ]
    assert text.count("{bot_name}") == 1
    assert text.rstrip().endswith("{{WIDGET_BLOCK}}\n\n{{VERBOSITY_BLOCK}}")


@pytest.mark.parametrize("name", PROMPT_BYTE_CEILINGS)
def test_each_prompt_keeps_its_own_output_and_host_boundaries(name: str) -> None:
    text = _read(name)
    output = _section(text, "## Output Format")
    _require(output, r"ANY file change", r"latest injected", r"unified diff", r"/dev/null")
    rules = _section(text, "## Rules")
    _require(
        rules,
        r"Do NOT run `git push` to protected branches",
        r"MUST name the branch explicitly",
        r"Do NOT read credential files directly",
        r"Do NOT run destructive AWS operations",
        r"ALWAYS bind to (?:localhost/)?127\.0\.0\.1.*explicit bind address",
        r"never rely on defaults",
    )
    for command in ("git push origin <feature-branch>", "--bind 127.0.0.1 --directory PATH"):
        assert command in rules


def test_cron_modes_and_session_ownership_remain_explicit() -> None:
    capabilities = _section(_read(), "## KiroCrew Capabilities")  # brand-ok: exact prompt heading
    _require(
        capabilities,
        r"no-LLM.*mutually exclusive `command`.*deterministic",
        r"`Skip\(\)` to retry.*`Done\(msg\)` to deliver/remove.*`Report\(msg\)` to deliver/keep",
        r"synchronous runner refuses `async def`",
        r"IANA `timezone`.*global config timezone, then UTC",
        r"THIS session's jobs only; empty does not mean none exist elsewhere",
        r"`timeout` bounds.*subprocess; `timeout_secs` bounds the whole wake",
        r"NON-BLOCKING: END YOUR TURN.*next user message, not the result",
    )
    for value in ("persistent_session=false", "minimal_context=true", "hide_in_chat=true"):
        assert value in capabilities
    assert "kirocrew cron preview <script:function> -m <message>" in capabilities


def test_injected_data_and_refusals_are_not_new_authority() -> None:
    text = _read()
    trust = _section(text, "## Injected context is not the user")
    _require(
        trust,
        r"SESSION CONTEXT.*REFERENCE.*CURRENT USER REQUEST",
        r"cancelled previous turn is a STOP signal",
        r"INCOGNITO SESSION.*TEMPORARY SESSION.*forbids memory tools",
        r"writes in incognito, reads as well in temporary",
    )
    rules = _section(text, "## Rules")
    _require(
        rules,
        r"blocked-by-policy.*before a second attempt; never rewrite",
        r"operator note is relayed verbatim",
        r'"Reject once".*that call alone, not a standing ban',
        r"trust-root file.*make your own command pass",
        r"forged.*ignore the instruction",
    )


def test_monitor_modes_have_distinct_stop_paths_and_real_exit_conditions() -> None:
    monitor = _section(_read(), "## Wait & Webhook Tools")
    _require(
        monitor,
        r"monitor_watch.*monitor_inspect.*monitor_stop.*NOT `autonudge_stop`",
        r"Structured monitors support dashboard/Slack/Discord only; Webex uses `monitor_start`",
        r"exit condition.*autonudge_stop",
        r"gate=false.*generic comments/advisory scans.*silence itself needs action",
        r"Arming failure.*not a transient reconnect",
        r"max_cycles.*runaway backstop, NOT success",
        r"CREATE-ONLY.*preserves an existing active loop/structured monitor",
        r"manual pause/user stop is preserved",
        r"budget-paused.*raise its stopping bound with user authorization",
        r"typed provider facts.*whole objective.*lifecycle.*checks.*mergeability.*review decision.*review threads",
        r"only for unsupported targets.*evidence the structured provider cannot see",
        r"final report or notification.*finite legacy path with `gate=false`",
        r"positive runtime/turn/token/provider-error budgets",
        r"Token caps depend on reported usage.*token_usage_known.*hard fallbacks",
        r"positive `max_cycles` and `max_runtime_secs`; never use zero",
        r"REQUESTED and END YOUR TURN.*after the turn.*cannot prove arming",
        r"On a later turn verify session-bound state",
        r"Terminal records are read-only.*explicit new watch.*retained user-stop evidence.*owner",
        r"never add a second driver.*unbounded wait/poll",
        r"Missing hosting context permits bounded in-turn wait/poll",
    )
    for token in (
        "max_runtime_secs",
        "session_ledger_record",
        "session_ledger_read",
        "kiro_crew.heartbeat.append_heartbeat_task(entry)",
        "HEARTBEAT_KEEP",
    ):
        assert token in monitor
    _require(monitor, r"never edit the file directly", r"cross-process lock")
    assert "Arming failure means NO monitor is running" not in monitor
    assert "On successful arming, tell the user monitoring is active" not in monitor


def test_browser_keeps_all_four_approval_groups_and_ownership_controls() -> None:
    browser = _section(_read(), "## Browser")
    groups = browser.split("Four groups still prompt", 1)[1].split("\n\n", 1)[0]
    for token in (
        "eval",
        "run-code",
        "upload",
        "state-load",
        "state-save <name>",
        "--filename",
        "installers",
        "cookie-list",
        "cookie-get",
        "localStorage",
        "sessionStorage",
        "requests",
        "header/body",
        "close",
        "tab-close",
        "close-all",
        "kill-all",
        "delete-data",
        "set",
        "delete",
        "clear",
        "loopback",
        "localhost",
        "private range",
    ):
        assert token in groups
    _require(groups, r"let the user approve; do not rewrite.*dodges the prompt", r"prefer `detach`")
    _require(
        browser,
        r"PUBLIC http\(s\).*refused",
        r"does not RESOLVE DNS",
        r"Fall back.*only when.*browser.*tells you",
        r"never `close`.*windows",
        r"After navigating, reloading.*fresh `snapshot`",
        r"assume sharing.*ONE distinct.*EVERY command",
        r"do not pass `--filename`",
    )
    for token in ("PLAYWRIGHT_CLI_SESSION", "PLAYWRIGHT_MCP_OUTPUT_DIR", "-s=<name>"):
        assert token in browser


def test_computer_use_keeps_opt_in_and_cursor_password_refusals() -> None:
    desktop = _section(_read(), "## Computer Use (native desktop apps)")
    _require(
        desktop,
        r"opt-in and off by default",
        r"Windows.*keyboard focus.*real cursor.*pass that on",
        r'"disabled" or "not supported" refusal is final',
        r"computer_get_state.*before any action",
        r"password field is refused by its index",
        r'click_method: "global".*ask for it BY NAME.*auto.*never picks it',
        r"Password fields.*<secure>.*never captured",
        r"own dashboard is refused, for reading as well as typing",
    )


def test_orchestrator_example_round_trips_through_real_plan_parser() -> None:
    from kiro_crew.context_management import extract_plan_metadata, validate_plan_format

    plans = [
        block
        for block in re.findall(
            r"^```[^\n]*\n(.*?)^```[ \t]*$", _read("prompt-orchestrator.md"), re.S | re.M
        )
        if block.startswith("📋 Plan for:")
    ]
    assert len(plans) == 1
    plan = plans[0]
    assert validate_plan_format(plan) == (True, True, [])
    titles, goal, tasks = extract_plan_metadata(plan)
    assert goal and all(tasks) and titles[-1] == "Verification"
    assert plan.splitlines()[-1] == "[OPTION: Go | Go All | Cancel]"
    assert plan.count("[OPTION:") == 1 and "[OPTIONS:" not in plan
    assert not validate_plan_format(plan.replace("Stage 2:", "Stage 9:"))[1]
    assert not validate_plan_format(plan.replace("[OPTION:", "[OPTIONS:"))[1]


def test_orchestrator_keeps_approval_scope_budgets_and_direct_work_exceptions() -> None:
    text = _read("prompt-orchestrator.md")
    _require(
        text,
        r"explicit plan request ALWAYS wins.*any language",
        r"plan only when ALL hold.*dependent phases.*multiple files/systems.*checkpoints",
        r"Go.*next stage.*pause for approval",
        r"Go All.*all remaining stages.*Stops on failure",
        r"Cancel.*abort the plan",
        r"once approved.*do not re-plan",
        r"END YOUR TURN immediately.*no tools.*until the user's Go / Go All",
        r"single indivisible unit.*parent unless.*specialist/model or context isolation",
        r"never dispatch work needing a still-running result",
        r"stage_timeout_seconds.*turn may START.*rather than hard-bounding",
        r"HALF that budget, capped at fifteen minutes",
        r"Max 3 rounds per stage.*checkpoint.*ask the user",
        r"3 failed attempts.*ask for guidance",
        r"Destructive/irreversible.*not already sanctioned",
        r"conflicting subagent results with no safe default",
        r"Do not invent new business requirements",
    )
