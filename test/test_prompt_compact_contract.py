"""Compact shipped prompts retain standalone operational contracts.

Text guards cannot prove model compliance. They protect the instructions most
likely to disappear during compression, and the size ceiling applies to each
selectable prompt independently.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew.context_assembly.sections import browser_section, computer_use_block
from kiro_crew.mcp_instructions import COMPUTER, CRON

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "src" / "kiro_crew" / "config"
# Absolute UTF-8 source-byte budgets, not token counts or live git baselines.
# A maintainer may raise a budget in a reviewed change when a new rule earns
# its space. Preserve the operational clauses below rather than cutting them
# to fit; their tests, not a size limit, check the retained text contracts.
# Measured on the prompt with its Computer Use slot filled the larger way: the
# pointer a session without `kirocrew-computer` gets (a mounted server carries
# its own usage text as MCP `instructions`, `kiro_crew.mcp_instructions`), and
# with its Browser section kept and its markers gone, as a session with browsing
# set up gets it. The
# `{{MAX_SUBAGENTS}}` and `{{WIDGET_BLOCK}}` slots are still counted as their
# raw tokens.
BABYSIT_SKILL_DIR = CONFIG.parent / "builtin_skills" / "kirocrew-dev" / "babysit"
PROMPT_BYTE_CEILINGS = {"prompt.md": 41_385}
_COMPUTER_USE_TOKEN = "{{COMPUTER_USE_BLOCK}}"


def _read(name: str = "prompt.md") -> str:
    return (CONFIG / name).read_text(encoding="utf-8")


def _resolved(name: str = "prompt.md", *, mounted: bool = True, browsing: bool = True) -> str:
    """The prompt with its Computer Use slot and Browser section resolved as a session gets them."""
    text = _read(name).replace(_COMPUTER_USE_TOKEN, computer_use_block(mounted))
    return browser_section(text, browsing)


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
    size = len(_resolved(name, mounted=False).encode("utf-8"))
    assert size <= PROMPT_BYTE_CEILINGS[name], (name, size, PROMPT_BYTE_CEILINGS[name])
    # Mounted, the slot is empty: the server's own instructions carry the text.
    assert len(_resolved(name, mounted=True).encode("utf-8")) < size
    assert len(_resolved(name, mounted=False, browsing=False).encode("utf-8")) < size


@pytest.mark.parametrize("name", PROMPT_BYTE_CEILINGS)
def test_template_slots_remain_complete_and_unique(name: str) -> None:
    text = _read(name)
    assert re.findall(r"\{\{([A-Z_]+)\}\}", text) == [
        "MAX_SUBAGENTS",
        "BROWSER_BEGIN",
        "BROWSER_END",
        "COMPUTER_USE_BLOCK",
        "WIDGET_BLOCK",
    ]
    assert text.count("{bot_name}") == 1
    assert text.rstrip().endswith("{{WIDGET_BLOCK}}")


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


def test_single_task_default_survives_prompt_compaction() -> None:
    """A compaction once dropped the explicit "one task stays in the parent"
    rule and left abstract value language plus a five-reason vocabulary. The
    model read the vocabulary as a menu of passing tokens and spawned one child
    per task. The concrete default, the fan-out threshold and the yield rule
    must survive every rewrite, and no reason vocabulary may come back."""
    for name in ("prompt.md",):
        text = _read(name)
        section = _section(text, "### Subagent Orchestration")
        _require(
            section,
            r"END YOUR TURN",
            r"still-running result",
            r"receipt says parent work is supported.*at most one minute",
        )
        if name == "prompt.md":
            _require(
                section,
                r"Do the task yourself by default",
                r"TWO OR MORE independent tasks",
                r"flood your context with bulk output",
                r"Never hand the whole request to one worker just to wait",
                r"ONE `spawn_run\(tasks=\[…\]\)` batch",
                r"Wait for the whole batch before spawning again",
            )
        else:
            _require(
                section,
                r"single indivisible unit stays in the parent",
                r"at least two independent tasks",
                r"Wait for all .*results before synthesizing",
            )
        for retired in (
            "solo_reason",
            "solo_details",
            "parent_parallel",
            "Solo reasons",
            "two workstreams",
            "Parent + one child",
            "Parent+child",
            "context isolation",
        ):
            assert retired.lower() not in section.lower(), (name, retired)


def test_cron_modes_and_session_ownership_remain_explicit() -> None:
    # The cron bullets are kirocrew-cron's own MCP instructions now.
    _require(
        CRON,
        r"no-LLM.*mutually exclusive `command`.*deterministic",
        r"`Skip\(\)` to retry.*`Done\(msg\)` to deliver/remove.*`Report\(msg\)` to deliver/keep",
        r"synchronous runner refuses `async def`",
        r"IANA `timezone`.*global config timezone, then UTC",
        r"THIS session's jobs only; empty does not mean none exist elsewhere",
        r"`timeout` bounds.*subprocess; `timeout_secs` bounds the whole wake",
    )
    for value in ("persistent_session=false", "minimal_context=true", "hide_in_chat=true"):
        assert value in CRON
    assert "kirocrew cron preview <script:function> -m <message>" in CRON
    capabilities = _section(_read(), "## KiroCrew Capabilities")  # brand-ok: exact prompt heading
    _require(
        capabilities,
        r"BLOCKING in a dashboard session: the call waits for the user and the answers are its result",
    )


def test_injected_data_and_refusals_are_not_new_authority() -> None:
    text = _read()
    trust = _section(text, "## Injected context is not the user")
    _require(
        trust,
        r"SESSION CONTEXT.*REFERENCE.*CURRENT USER REQUEST",
        r"cancelled previous turn is a STOP signal",
        r"compaction summary calling a tool call cancelled is not one when the call's own result shows it finished",
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


def test_a_compaction_summary_cancel_is_carved_out_of_the_stop_rule() -> None:
    """A compaction's "cancelled" is not a Stop; every other cancelled turn still is.

    A compaction can cut a finished tool call off from its result, and the
    harness then summarizes that call as cancelled by the user. Without the
    carve-out the STOP rule ends the session's own loop on that summary. The
    carve-out must ride on the STOP rule itself, so the general rule stays in
    force for every real cancel, marked or not, and it holds only when the
    call's own result shows it finished: a call the user really stopped has no
    such result, so its summary still reads as a cancel.
    """
    trust = " ".join(_section(_read(), "## Injected context is not the user").split())
    assert re.search(
        r"A cancelled previous turn is a STOP signal, not work to resume on your own; "
        r"a compaction summary calling a tool call cancelled is not one "
        r"when the call's own result shows it finished",
        trust,
    ), "the carve-out must qualify the STOP rule, and only for a call that finished"


def test_the_prompt_points_monitoring_at_the_babysit_skill() -> None:
    """The prompt names the skill by its real key and restates none of its rules."""
    monitor = _section(_read(), "## Wait & Webhook Tools")
    _require(
        monitor,
        r"core Kiro Crew feature",
        r"wait for, keep checking, keep an eye on, babysit",
        r"read the `kirocrew-dev/babysit` skill first, every time",
        r"required even when you are only asked to plan, describe or propose the call",
        r"Never choose a driver or write its arguments from memory",
        r"`wait`, `monitor_watch`, `monitor_start`, `cron_add`, `register_hook`",
    )
    assert (BABYSIT_SKILL_DIR / "SKILL.md").is_file()
    for restated in ("**Using monitor_start:**", "**Legacy timer:**", "gate=false", "max_cycles"):
        assert restated not in monitor, restated


def test_the_babysit_skill_keeps_the_monitor_contract() -> None:
    skill = " ".join((BABYSIT_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
    _require(
        skill,
        r"Stop with `monitor_stop\(reason=\.\.\.\)`.*`autonudge_stop` is a compatibility alias",
        r"Structured watches support dashboard, Slack, and Discord sessions",
        r"On Webex, use the finite legacy path",
        r"call `autonudge_stop` with a reason",
        r"A cap is a backstop, never successful completion",
        r"`monitor_start` is create-only",
        r"Manual pauses and user stops stay preserved",
        r"cap/budget-stopped legacy loop, only with user authorization",
        r"Typed providers do not observe generic issue/pull-request comments",
        r"finite legacy path with `gate=false`",
        r"`max_agent_turns` where 0 = no wake cap",
        r"token cap applies only when usage is reported.*`token_usage_known`",
        r"runtime always caps, turns only if set positive",
        r"never use `0` for unlimited work",
        r"pending application request, not proof that the monitor armed.*END THE TURN",
        r"Terminal records are read-only; restarting requires an explicit new watch",
        r"never add wait/poll beside it",
        r"Missing hosting context permits bounded in-turn wait/poll",
        r"three settled polls read the same progress key with no push",
        r'`watch="work-ledger"`',
        r"never edit the file directly.*cross-process lock",
    )
    for token in (
        "session_ledger_record",
        "session_ledger_read",
        "kiro_crew.heartbeat.append_heartbeat_task(entry)",
        "HEARTBEAT_KEEP",
    ):
        assert token in skill, token


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
    # What a session gets while its spec mounts `kirocrew-computer`: the server's
    # own MCP instructions. Its "off by default" and "turned off after this
    # session started" lines stayed behind: a mounted server is on, and a session
    # whose server goes away loses its instructions with it and gets the pointer.
    _require(
        COMPUTER,
        r"Windows.*keyboard focus.*real cursor.*pass that on",
        r'"disabled" or "not supported" refusal is final',
        r"computer_get_state.*before any action",
        r"password field is refused by its index",
        r'click_method: "global".*ask for it BY NAME.*auto.*never picks it',
        r"Password fields.*<secure>.*never captured",
        r"own dashboard is refused, for reading as well as typing",
    )
    assert "## Computer Use" not in _resolved(mounted=True)


def test_computer_use_pointer_names_the_setting_and_the_skill() -> None:
    # The same slot while the spec withholds the server: no tool instructions,
    # but a user asking for a desktop app is still pointed at the setting.
    desktop = _section(_resolved(mounted=False), "## Computer Use (native desktop apps)")
    _require(
        desktop,
        r"Not available in this session.*no `computer_\*` tools are mounted",
        r"point them to Settings → Computer Use",
        r"tools are in your tool list anyway.*read the `computer-use` skill",
    )
    for tool in ("computer_get_state", "computer_click", "computer_launch_app"):
        assert tool not in desktop
