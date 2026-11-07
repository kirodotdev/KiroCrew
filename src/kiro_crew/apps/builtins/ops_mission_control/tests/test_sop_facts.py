"""Ops Mission Control SOP text must match what the app ships.

The SOPs are read by cron agents, so a path the tool refuses or a schedule that
disagrees with ``app.json`` sends the agent the wrong way. Each check reads the
shipped files; none needs a gateway.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew.mcp_tools.apps import OPS_MISSION_CONTROL_ALLOWED_CALLS

_SOPS = (
    Path(kiro_crew.__file__).resolve().parent / "builtin_skills" / "ops-mission-control" / "sops"
)
_SKILL = _SOPS.parent / "SKILL.md"
_APP_JSON = Path(__file__).resolve().parents[1] / "app.json"
# repo_root/src/kiro_crew/apps/builtins/<app> -> repo_root
_OWNING_SPEC = (
    Path(__file__).resolve().parents[1].parents[4]
    / "docs"
    / "system-specs"
    / "modules"
    / "ops-mission-control.md"
)

#: The reconcile cron message, decoded. It defers to the SOP's evidence rule rather than
#: restating a weaker absence rule of its own.
_RECONCILE_MESSAGE = (
    "Follow the reconcile SOP at ~/.kiro/crew/skills/ops-mission-control/sops/reconcile.md. "
    "FIRST: GET /api/apps/ops-mission-control/state and if no provider has configured=true, "
    "produce NO output and stop - the app is not set up yet, so there is nothing to reconcile "
    "and this must cost nothing. Otherwise run the SOP Pass 1: GET "
    "/api/apps/ops-mission-control/signals and resolve an open incident (POST "
    "/incident/transition) ONLY on the evidence Pass 1 accepts. A signal missing from firing "
    "is never evidence on its own: a webhook (push) incident stays open until its cleared "
    "event arrives, which the dispatch heartbeat resolves itself. Recording the transition "
    "updates the incident's Slack message itself. Cap at 10 incidents per run. If nothing "
    "changed, exit silently as the SOP Rules say."
)

#: Sentences each SOP, the skill and the owning spec must carry verbatim.
_S1 = (
    "A push-only incident whose signal is merely absent stays open: the dispatch heartbeat "
    "resolves it itself when the sender delivers a cleared event (state ok) for that signal."
)
_S2 = (
    "If POST /incident/transition answers 409 with code recovery_evidence_required, leave that "
    "incident open and do not retry the resolve in this run; no cleared event is spooled for it."
)
_S3 = (
    "When you resolve a signal listed in cleared, start the resolution with: Resolved on an "
    "explicit cleared event:"
)
_S4 = (
    "For a webhook incident, resolve only after the sender delivered a cleared event; "
    "otherwise set needs_human with your diagnosis, because the server refuses the resolve "
    "with recovery_evidence_required."
)
_S5 = (
    "An automated resolve of a push-only (webhook) incident with no spooled cleared event is "
    "refused with 409 recovery_evidence_required; an operator can still resolve it from the "
    "dashboard."
)
_S6 = (
    "Server guard: an automated (internal_auth) resolve of a push-only incident with no "
    "spooled cleared event is refused with 409 `recovery_evidence_required`, and the dispatch "
    "heartbeat resolves a push-only incident itself when its sender delivers a cleared event."
)

#: The snapshot-source rule already in reconcile.md, which the push sentences must not
#: displace. Compared after collapsing whitespace, so the markdown wrap does not matter.
_P1 = (
    "So resolve an incident on absence **only when the poll for that signal's own source "
    'succeeded** — `poll_health["<source>"].ok == true`.'
)
_P2 = (
    "**`ok` is necessary and not sufficient. Also require "
    '`poll_health["<source>"].snapshot != false`.**'
)
_P3 = "**Never resolve an incident whose source failed to poll.**"
_P4 = (
    "# Only for an incident whose source polled ok with snapshot != false, or one listed in "
    "`cleared`."
)
_P4_OLD = "# Only for an incident whose source polled OK, or one listed in `cleared`."


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, starts: str, ends: tuple[str, ...]) -> str:
    """From the line starting with ``starts`` to the next line starting with any of ``ends``
    (or the end of the text)."""
    lines = text.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith(starts))
    last = next((i for i in range(first + 1, len(lines)) if lines[i].startswith(ends)), len(lines))
    return "\n".join(lines[first:last])


def _steps(section: str, markers: tuple[str, ...]) -> dict[str, str]:
    """``section`` cut at the lines starting with each marker, each piece whitespace-collapsed
    and running to the next marker line."""
    pieces: dict[str, list[str]] = {}
    current = ""
    for line in section.splitlines():
        hit = next((marker for marker in markers if line.startswith(marker)), "")
        if hit:
            current = hit
            pieces[current] = []
        if current:
            pieces[current].append(line)
    assert sorted(pieces) == sorted(markers), f"step markers missing: {sorted(pieces)}"
    return {marker: _collapse("\n".join(lines)) for marker, lines in pieces.items()}


def _reconcile_pass_one() -> dict[str, str]:
    text = (_SOPS / "reconcile.md").read_text(encoding="utf-8")
    return _steps(_section(text, "## Pass 1 —", ("## Pass 1a",)), ("3. ", "4. ", "5. "))


# A backticked `GET /x` or `POST /x` in SOP prose. Gateway routes outside the app
# base (`/api/...`) are plain routes, not tool paths, so they are excluded.
_CALL = re.compile(r"`(GET|POST) (/(?!api/)[a-z/_-]+)")


def _sops() -> list[Path]:
    return sorted(_SOPS.glob("*.md"))


@pytest.mark.parametrize("sop", _sops(), ids=lambda p: p.name)
def test_every_app_call_in_a_sop_is_reachable_through_the_tool(sop: Path) -> None:
    text = sop.read_text(encoding="utf-8")
    for method, path in _CALL.findall(text):
        assert (method, path.rstrip("/")) in OPS_MISSION_CONTROL_ALLOWED_CALLS, (
            f"{sop.name} tells the agent to call {method} {path}, which "
            "ops_mission_control_api refuses"
        )


def test_sop_cron_expr_matches_the_shipped_cron() -> None:
    crons = json.loads(_APP_JSON.read_text(encoding="utf-8"))["crons"]
    by_name = {c["name"]: c for c in crons}
    checked = 0
    for sop in _sops():
        text = sop.read_text(encoding="utf-8")
        name = re.search(r"^cron: ops-mission-control/(\S+)$", text, re.M)
        schedule = re.search(r'^schedule: "([^"]+)"$', text, re.M)
        if not (name and schedule):
            continue
        cron = by_name.get(name.group(1))
        if cron is None or "cron_expr" not in cron:
            continue
        assert schedule.group(1) == cron["cron_expr"], sop.name
        checked += 1
    assert checked, "no SOP with a cron_expr schedule was checked"


def test_dispatch_sop_runs_the_server_side_cycle() -> None:
    text = (_SOPS / "dispatch.md").read_text(encoding="utf-8")
    steps = text.split("## Steps", 1)[1].split("## Rules", 1)[0]
    first_step = steps.split("\n2. ", 1)[0]
    assert "`POST /dispatch`" in first_step
    # The manual board claim may be named only as the thing NOT to use.
    for line in steps.splitlines():
        if "POST /incident/claim" in line:
            assert "Do NOT" in steps.split("POST /incident/claim", 1)[0][-200:], line


def test_reconcile_cron_message_is_the_pinned_text() -> None:
    crons = json.loads(_APP_JSON.read_text(encoding="utf-8"))["crons"]
    message = next(c["message"] for c in crons if c["name"] == "reconcile")
    assert message == _RECONCILE_MESSAGE
    assert "no longer firing" not in message


def test_reconcile_sop_carries_the_pinned_push_sentences() -> None:
    """S1 and S2 in Pass 1 step 3, S3 in step 4 -- each piece cut at the next step marker,
    so a sentence appended to the wrong step does not count."""
    from kiro_crew.apps.builtins.ops_mission_control.backend.dispatch import (
        RECOVERY_EVIDENCE_REQUIRED,
    )

    steps = _reconcile_pass_one()
    assert _S1 in steps["3. "]
    assert _S2 in steps["3. "]
    assert _S3 in steps["4. "]
    assert RECOVERY_EVIDENCE_REQUIRED in _S2


def test_reconcile_sop_keeps_the_snapshot_source_rule() -> None:
    """The push sentences sit beside the snapshot-source rule; they must not replace it."""
    steps = _reconcile_pass_one()
    assert _P1 in steps["3. "]
    assert _P2 in steps["3. "]
    text = (_SOPS / "reconcile.md").read_text(encoding="utf-8")
    assert _P3 in _collapse(text.split("## Rules", 1)[1])


def test_reconcile_example_comment_is_the_pinned_line() -> None:
    stripped = [
        line.strip() for line in (_SOPS / "reconcile.md").read_text(encoding="utf-8").splitlines()
    ]
    assert _P4 in stripped
    assert _P4_OLD not in stripped


def test_investigate_and_skill_carry_the_pinned_refusal_sentences() -> None:
    investigate = (_SOPS / "investigate.md").read_text(encoding="utf-8")
    phase_four = _steps(_section(investigate, "## Phase 4", ("## ",)), ("1. ", "2. "))
    assert _S4 in phase_four["1. "]
    skill = _SKILL.read_text(encoding="utf-8")
    assert _S5 in _collapse(_section(skill, "## Board semantics", ("## ",)))


def test_owning_spec_names_the_guard_in_both_sections() -> None:
    """S6 on one physical line in EACH section, cut at the next ``##``/``###`` heading."""
    if not _OWNING_SPEC.exists():  # pragma: no cover — docs are absent in a wheel install
        pytest.skip("spec not present in this install")
    text = _OWNING_SPEC.read_text(encoding="utf-8")
    for heading in ("### 3b. ", "### Absence is not evidence (`registry.poll_health`)"):
        section = _section(text, heading, ("## ", "### "))
        assert section.splitlines()[0].startswith(heading)
        assert any(_S6 in line for line in section.splitlines()), heading
