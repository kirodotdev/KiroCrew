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
  unavailable".

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
    print(f"seeded {args.fixture} into {home} with members {args.member or '[]'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
