#!/usr/bin/env python3
"""Code Review Sage — app-local store + self-heal data layout.

This module is the deterministic, token-free backbone of the app. It owns the
on-disk layout under ``<config_dir>/apps/code-review-sage/data/`` (i.e.
``~/.kiro/crew/apps/code-review-sage/data/`` by default) and is safe to run on
every action (idempotent self-heal).

Layout:

    data/
      learnings/
        common/learned-patterns.md          # cross-repo (warm start)
        repos/<host>/<org>/<repo>/
            learned-patterns.md
            checkpoint.json
      results/<change-id>.json               # one result record per change
      reports/index.json                     # latest run pointer (UI reads)
      config.json                            # resolved paths, globs, caps, rule packs

Run ``python3 sage_lib/store.py --ensure`` to create/repair the layout and seed
``config.json`` without overwriting any user edits.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# Canonical KiroCrew data-root accessor. Imported at module top but kept guarded
# so the store stays importable standalone (outside the KiroCrew runtime) — the
# fallback mirrors ``config_dir()``'s default of ``~/.kiro/crew``, honoring
# ``KIROCREW_HOME`` when it is set.
try:
    from kiro_crew.config.paths import config_dir as _config_dir
except ImportError:  # pragma: no cover - standalone fallback
    _config_dir = None  # type: ignore[assignment]

APP_NAME = "code-review-sage"


def crew_home() -> Path:
    """Resolve the active KiroCrew data root.

    Delegates to ``kiro_crew.config.paths.config_dir()`` when the runtime is
    importable (so it follows the ``~/.kiro/crew`` root and honors
    ``KIROCREW_HOME`` uniformly, including the one-time legacy-home migration).
    Falls back to a standalone resolution when run outside the KiroCrew package.
    """
    if _config_dir is not None:
        return _config_dir()
    home = os.environ.get("KIROCREW_HOME")
    return Path(home) if home else Path.home() / ".kiro" / "crew"


# Sensitive-path globs feed the deterministic blast-radius extractor. Kept here
# so the single config is the tunable source of truth.
DEFAULT_SENSITIVE_GLOBS: list[str] = [
    "**/auth/**", "**/*auth*", "**/login*", "**/session*", "**/token*",
    "**/*cred*", "**/secret*", "**/*.pem", "**/*.key",
    "**/csp*", "**/cors*", "**/network/**", "**/*proxy*",
    "**/migrations/**", "**/*migration*", "**/schema*", "**/models/**",
    "**/infra/**", "**/*.tf", "**/cdk/**", "**/cloudformation/**",
    "**/gateway*", "**/server.py", "**/lifecycle*", "**/startup*",
]

# Rule-pack pointers: repo-identity -> rule pack skill name/path. Read-only reuse,
# composed at review time. Empty by default (OSS); users can map their own repos.
DEFAULT_RULE_PACKS: dict[str, str] = {}

DEFAULT_CONFIG: dict[str, object] = {
    "schema": "code-review-sage-config",
    "version": 1,
    # Triage thresholds — tunable *guidance* the report AI weighs.
    "triage": {
        "critical_blast": "LARGE",
        "medium_blast": "MEDIUM",
        "yellow_min_yellow_findings": 2,
    },
    # Learning-store governance caps.
    "caps": {
        "common_max_patterns": 60,
        "repo_max_patterns": 120,
    },
    # Review settings — model, effort, and active namespaces.
    "review": {
        "model": None,         # None = inherit the system/agent default model
        "effort": "",          # "" = inherit the model/provider default effort
        "active_namespaces": ["default"],  # which namespaces to load during review
        "max_concurrent": 5,     # max reviews running at once on the shared runtime
                                 # (clamped to [1, 30]); "review all" can raise it.
    },
    "sensitive_globs": DEFAULT_SENSITIVE_GLOBS,
    "rule_packs": DEFAULT_RULE_PACKS,
    # Settled-change filtering defaults.
    "exclude_settled_by_default": True,
}


def app_root() -> Path:
    """Resolve the installed app root under the KiroCrew home dir.

    Derives from ``crew_home()`` (``config_dir()`` → ``~/.kiro/crew`` by
    default, honoring ``KIROCREW_HOME``); ``crew_home`` keeps a standalone
    fallback so the store stays importable outside the KiroCrew runtime."""
    return crew_home() / "apps" / APP_NAME


def data_dir(root: Path | None = None) -> Path:
    return (root or app_root()) / "data"


def ensure_layout(root: Path | None = None) -> dict[str, str]:
    """Create the full data layout if missing. Idempotent — never clobbers.

    Returns a dict of the key resolved paths (consumed by config.json + the UI).
    """
    data = data_dir(root)
    learnings = data / "learnings"
    common = learnings / "common"   # learned-patterns.md (canonical) + .candidate.md (staging)
    repos = learnings / "repos"            # reserved for per-repo learning files
    namespaces = learnings / "namespaces"  # user-created namespaces
    results = data / "results"
    reports = data / "reports"

    for d in (data, learnings, common, repos, namespaces, results, reports):
        d.mkdir(parents=True, exist_ok=True)

    # Warm-start common layer (empty but present so brand-new repos inherit it).
    common_patterns = common / "learned-patterns.md"
    if not common_patterns.exists():
        common_patterns.write_text(
            "# Common learned patterns (cross-repo, warm start)\n\n"
            "<!-- Promoted from per-repo layers via human-approved generalization. -->\n",
            encoding="utf-8",
        )

    # Reports pointer the UI polls.
    index = reports / "index.json"
    if not index.exists():
        index.write_text(
            json.dumps({"report_slug": None, "bands": {"red": 0, "yellow": 0, "green": 0},
                        "generated_at": None}, indent=2),
            encoding="utf-8",
        )

    _seed_config(data)

    return {
        "dataDir": str(data),
        "learningsCommon": str(common_patterns),
        "reposDir": str(repos),
        "resultsDir": str(results),
        "reportsIndex": str(index),
        "configPath": str(data / "config.json"),
    }


def _seed_config(data: Path) -> None:
    """Write config.json once, merging in any missing top-level keys on upgrade."""
    cfg_path = data / "config.json"
    if not cfg_path.exists():
        cfg = dict(DEFAULT_CONFIG)
        cfg["resolved_paths"] = {
            "results": str(data / "results"),
            "reports": str(data / "reports"),
            "learnings": str(data / "learnings"),
        }
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return

    # Upgrade path: add any new default keys without overwriting user edits.
    try:
        existing = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    changed = False
    for key, val in DEFAULT_CONFIG.items():
        if key not in existing:
            existing[key] = val
            changed = True
    # Always ensure resolved_paths exists (not in DEFAULT_CONFIG but required by UI).
    if "resolved_paths" not in existing:
        existing["resolved_paths"] = {
            "results": str(data / "results"),
            "reports": str(data / "reports"),
            "learnings": str(data / "learnings"),
        }
        changed = True
    if changed:
        cfg_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def load_config(root: Path | None = None) -> dict:
    cfg_path = data_dir(root) / "config.json"
    if not cfg_path.exists():
        ensure_layout(root)
    return json.loads((data_dir(root) / "config.json").read_text(encoding="utf-8"))


def _main() -> int:
    ap = argparse.ArgumentParser(description="Code Review Sage store / self-heal")
    ap.add_argument("--ensure", action="store_true",
                    help="Create/repair the data layout and seed config.json")
    ap.add_argument("--print-config", action="store_true",
                    help="Print the resolved config.json")
    ap.add_argument("--root", default=None,
                    help="Override app root (testing); defaults to the installed path")
    args = ap.parse_args()

    root = Path(args.root) if args.root else None

    if args.ensure or not args.print_config:
        paths = ensure_layout(root)
        print(json.dumps({"ok": True, "paths": paths}, indent=2))
    if args.print_config:
        print(json.dumps(load_config(root), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
