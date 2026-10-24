#!/usr/bin/env python3
"""crewmate_prompt_preview.py -- read every crewmate prompt as the gateway assembles it, offline.

    python3 scripts/crewmate_prompt_preview.py --fixture all-met
    python3 scripts/crewmate_prompt_preview.py --fixture all-met --piece heartbeat --member Atlas
    python3 scripts/crewmate_prompt_preview.py --fixture all-met --diff     # against the golden
    python3 scripts/crewmate_prompt_preview.py --fixture all-met --write    # refresh the golden
    python3 scripts/crewmate_prompt_preview.py --list

Every piece of text comes from ``src/kiro_crew/crewmate_prompts.json``; this script
runs the real assembly code over a FIXTURE (a JSON file: the goal, milestones met or
open, whether a milestones card waits, the worker's servers and what the gateway has
beyond them, the waiting items, a worker report) in a throwaway data home, and prints
each assembled prompt under a ``=== <piece> ===`` header. No gateway, no model, no
pod: edit the JSON, run this, read the result.

A fixture is a name under ``test/fixtures/crewmate_prompts/`` (``<name>.json``) or a
path. Its golden is ``<name>.golden.txt`` beside it; ``--diff`` prints a unified diff
of the current output against it and exits 1 when they differ, ``--write`` rewrites it.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "test" / "fixtures" / "crewmate_prompts"
sys.path.insert(0, str(ROOT / "src"))

#: Every piece this script assembles, in print order.
PIECES = (
    "conductor",
    "operating_mode",
    "worker",
    "heartbeat",
    "daily",
    "weekly",
    "monthly",
    "goal_setup",
    "milestones_confirmed",
    "goal_removed",
    "worker_report",
    "first_welcome",
    "goal_welcome",
    "upgrade_notice",
    "dispatch_note",
)


def _fixture_path(name: str) -> Path:
    path = Path(name)
    if path.suffix == ".json" and path.exists():
        return path
    return FIXTURES / f"{name}.json"


def _record(fx: dict[str, Any], slug: str, member: str) -> dict[str, Any]:
    from kiro_crew import member_goals

    member_goals.set_goal(slug, member, fx["goal"], by="user")
    milestones = fx.get("milestones") or []
    if milestones:
        member_goals.set_key_results(
            slug, member, [{"text": m["text"], "target": m["target"]} for m in milestones]
        )
        rec = member_goals.read_goals(slug, member)
        for kr, m in zip(rec["key_results"], milestones):
            for reading in m.get("readings") or []:
                member_goals.record_key_result(slug, member, kr["id"], reading)
    member_goals.set_schedules(
        slug,
        member,
        {"heartbeat": True, "interval": fx.get("heartbeat_minutes", 30), "daily": True},
    )
    return member_goals.read_goals(slug, member)


def assemble(fx: dict[str, Any], member: str) -> dict[str, str]:
    """Each piece's assembled text for fixture *fx*, keyed by piece name."""
    from kiro_crew import context as ctx
    from kiro_crew import crewmate_dynamic_capabilities
    from kiro_crew import mate_capability_catalog as cc
    from kiro_crew import mate_upgrade_notice, mate_worker, members
    from kiro_crew.context_assembly import member as cm
    from kiro_crew.dashboard import (
        goal_reminder,
        goal_setup,
        mate_thread_wakes,
        mate_welcome,
    )

    # The preview is of the crewmate prompts with Dynamic capabilities on,
    # whatever the host config says.
    crewmate_dynamic_capabilities._override = True
    slug = members.slug_for_name(member)
    rec = _record(fx, slug, member)
    tools = fx.get("has_mate_tools", True)
    binding = SimpleNamespace(
        description=fx.get("description", ""),
        triggers="",
        kiro_agent="",
        workspace="",
        member_id="",
    )
    cfg = ctx.KiroCrewConfig()
    cfg.agents[member] = binding
    ctx.KiroCrewConfig.load = staticmethod(lambda *a, **k: cfg)  # type: ignore[method-assign]
    import kiro_crew.member_mate_tools as mmt

    mmt.binding_has_mate_tools = lambda _b: tools  # type: ignore[assignment]
    worker = fx.get("worker_servers") or []
    lacks = fx.get("gateway_has_beyond_worker") or []
    cc.worker_has = lambda _s, _c: {"servers": [{"name": n} for n in worker]}
    cc.gateway_lacks = lambda _s, _c: {"servers": [{"name": n} for n in lacks]}
    entry = SimpleNamespace(display_name=member, description=fx.get("description", ""))
    gid = rec["goals"][0]["id"]
    out = {
        "conductor": cm.build_member_section(member),
        "operating_mode": cm.operating_mode_block(member),
        "worker": mate_worker.MATE_WORKER_PROMPT,
        "heartbeat": goal_reminder.heartbeat_prompt(
            rec,
            has_session_tools=fx.get("has_session_tools", True),
            milestones_waiting=fx.get("milestones_card_waiting", False),
        ),
        "daily": goal_reminder.reminder_prompt(rec, "daily"),
        "weekly": goal_reminder.reminder_prompt(rec, "weekly", finished=fx.get("finished") or []),
        "monthly": goal_reminder.reminder_prompt(rec, "monthly", finished=fx.get("finished") or []),
        "goal_setup": goal_setup.kickoff_prompt(rec, gid, has_mate_tools=tools) or "",
        "milestones_confirmed": goal_setup.milestones_prompt(rec, has_mate_tools=tools) or "",
        "goal_removed": "",
        "worker_report": (
            mate_thread_wakes.report_prompt(fx["worker_reports"])
            if fx.get("worker_reports")
            else ""
        ),
        "first_welcome": mate_welcome.welcome_kickoff(
            entry, has_other_crewmates=True, has_mate_tools=tools
        ),
        "goal_welcome": mate_welcome.goal_kickoff(entry, member, "", has_mate_tools=tools),
        "upgrade_notice": mate_upgrade_notice.NOTICE_PROMPT,
        "dispatch_note": cc.dispatch_summary({}, {}),
    }
    return {piece: out[piece] for piece in PIECES}


def render(pieces: dict[str, str], only: str | None) -> str:
    chosen = [only] if only else list(PIECES)
    return "".join(f"=== {p} ===\n{pieces[p]}\n\n" for p in chosen)


def run(fixture: Path, member: str, only: str | None) -> str:
    """The whole preview for *fixture*, assembled in a throwaway data home."""
    fx = json.loads(fixture.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="crewmate-prompt-preview-") as home:
        os.environ["KIROCREW_HOME"] = home
        os.environ["HOME"] = home
        text = render(assemble(fx, member or fx.get("member", "Atlas")), only)
        for spelling in sorted({os.path.realpath(home), home}, key=len, reverse=True):
            text = text.replace(spelling, "<HOME>")
    # Goal and milestone ids are minted fresh per record; a golden pins the words.
    return re.sub(r"\b([gk])[0-9a-f]{8}\b", r"\1<id>", text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--fixture", default="all-met")
    parser.add_argument("--member", default="")
    parser.add_argument("--piece", choices=PIECES)
    parser.add_argument("--diff", action="store_true", help="diff against the fixture's golden")
    parser.add_argument("--write", action="store_true", help="rewrite the fixture's golden")
    parser.add_argument("--list", action="store_true", help="list the fixtures and pieces")
    args = parser.parse_args(argv)
    if args.list:
        print("fixtures:", " ".join(sorted(p.stem for p in FIXTURES.glob("*.json"))))
        print("pieces:", " ".join(PIECES))
        return 0
    fixture = _fixture_path(args.fixture)
    text = run(fixture, args.member, None if (args.diff or args.write) else args.piece)
    golden = fixture.with_suffix(".golden.txt")
    if args.write:
        golden.write_text(text, encoding="utf-8")
        print(f"wrote {golden.relative_to(ROOT)}")
        return 0
    if args.diff:
        old = golden.read_text(encoding="utf-8") if golden.exists() else ""
        diff = list(
            difflib.unified_diff(
                old.splitlines(True), text.splitlines(True), str(golden.name), "current"
            )
        )
        sys.stdout.writelines(diff)
        return 1 if diff else 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
