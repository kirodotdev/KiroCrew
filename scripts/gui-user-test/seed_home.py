#!/usr/bin/env python3
"""Populate a throwaway KIROCREW_HOME for the GUI user-test target.

Copies a named fixture (``kiro_crew.seed``, the same code path as
``kirocrew gateway --seed``) and then patches ``config.json`` so the dashboard
renders as a fully onboarded install with a crew roster:

* ``dashboard.onboarded`` / ``import_onboarded`` -> true (no setup wizard);
* ``agents.default`` first, then ``agents.<slug>`` for every ``--member`` (the
  Crew Members page reads the roster from ``config.agents``; the fixtures ship
  none). ``default`` is written explicitly because the fixtures' seeded sessions
  name it as their agent: the config loader only materializes it when
  ``agents`` is empty, and adding a member here makes it non-empty, so without
  this row every seeded session fails to send with "Crew Member 'default' is
  unavailable";
* with ``--project DIR``, the pinned starter session
  (``sessions/dashboard_starter.jsonl``) gets ``DIR`` as its project directory,
  written into the transcript's metadata line exactly where the dashboard
  persists a slot's ``project`` -- the chat's Files view lists that tree. The
  fixture cannot carry the path itself (it only exists on the machine the seed
  runs on), and only this one session gets it so the other seeded chats keep
  reading as project-less conversations.

Run with ``KIROCREW_HOME`` pointing at an EMPTY scratch directory. The seed
module refuses the real data home, so this cannot touch an operator's install.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _display_name(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--fixture", default="rich")
    p.add_argument(
        "--member", action="append", default=[], help="crew member slug to add (repeatable)"
    )
    p.add_argument(
        "--project",
        default="",
        help="absolute directory to set as the pinned starter session's project",
    )
    p.add_argument(
        "--print-fake-backend",
        action="store_true",
        help="print the path of the packaged fake ACP backend (for KIROCREW_KIRO_BIN) and exit",
    )
    args = p.parse_args(argv)

    if args.print_fake_backend:
        from kiro_crew.testing import fake_acp_backend

        print(fake_acp_backend.__file__)
        return 0

    home = os.environ.get("KIROCREW_HOME")
    if not home:
        print("KIROCREW_HOME must point at the scratch home to seed", file=sys.stderr)
        return 2

    from kiro_crew.seed import SeedError, seed

    try:
        seed(args.fixture)
    except SeedError as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 2

    cfg_path = Path(home).expanduser() / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    dashboard = cfg.setdefault("dashboard", {})
    dashboard["onboarded"] = True
    cfg["import_onboarded"] = True
    agents = cfg.setdefault("agents", {})
    default_kiro_agent = cfg.get("agent", {}).get("default_agent", "kirocrew")
    agents.setdefault(
        "default",
        {
            "kiro_agent": default_kiro_agent,
            "workspace": cfg.get("default_workspace", "default"),
            "memory_store": "default",
        },
    )
    for slug in args.member:
        agents.setdefault(
            slug,
            {
                "kiro_agent": default_kiro_agent,
                "workspace": cfg.get("default_workspace", "default"),
                "description": f"{_display_name(slug)} -- seeded crew member for the GUI user test.",
            },
        )
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    if args.project:
        if not os.path.isabs(args.project) or not os.path.isdir(args.project):
            print(
                f"--project must be an existing absolute directory: {args.project}", file=sys.stderr
            )
            return 2
        _set_session_project(Path(home).expanduser() / "sessions" / STARTER_SESSION, args.project)
    print(
        f"seeded {args.fixture} into {home} with members {args.member or '[]'}"
        + (f" and project {args.project}" if args.project else "")
    )
    return 0


# The rich fixture's pinned starter transcript: the one session the Files
# scenario opens, so the one that carries the staged sample project.
STARTER_SESSION = "dashboard_starter.jsonl"


def _set_session_project(transcript: Path, project: str) -> None:
    """Write ``project`` into the metadata line of a seeded transcript, if present.

    The first line of a session file is its ``_type: metadata`` record; the
    dashboard's persistence loaders restore ``slot.project`` from that record's
    ``project`` key, so this is the same shape a live slot saves.
    """
    if not transcript.is_file():
        # A fixture without the starter transcript has no session for the Files
        # scenario to open; the seed still succeeds, the project is just unused.
        print(f"{transcript.name} not in this fixture; --project not applied", file=sys.stderr)
        return
    lines = transcript.read_text(encoding="utf-8").splitlines(keepends=True)
    if not lines:
        raise SystemExit(f"{transcript}: empty transcript, no metadata line to patch")
    meta = json.loads(lines[0])
    if meta.get("_type") != "metadata":
        raise SystemExit(f"{transcript}: first line is not the metadata record")
    meta["project"] = project
    lines[0] = json.dumps(meta) + "\n"
    transcript.write_text("".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
