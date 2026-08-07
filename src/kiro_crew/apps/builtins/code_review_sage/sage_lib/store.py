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
import re
import shutil
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
        # Publish findings as a PENDING (draft) review on the PR itself.
        # OFF by default: a review is READ here, in the app, and writing to
        # someone else's pull request is a side effect the user has to ask for.
        # Turning it on restores the old behaviour (one pending review per PR,
        # bodies composed deterministically in Python and posted verbatim).
        "auto_post": False,
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


# Optional Kiro Crew redaction. Lives here rather than in `pipeline` because readers
# outside the posting path need it too, and `pipeline` imports `discovery`, so a
# reader in `discovery` cannot import `pipeline` back.
try:                                   # pragma: no cover - import shape
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls
except ImportError:                    # pragma: no cover - standalone fallback
    redact_credentials = redact_exfiltration_urls = None  # type: ignore


def redact_text(text: str) -> str:
    """Scrub credentials + exfiltration URLs from model-written text.

    Applied at every boundary where such text leaves this app -- the code-review
    system it posts to, and the dashboard it renders in. No-op when the Kiro Crew
    redaction lib is not importable (standalone use).
    """
    if redact_exfiltration_urls is None or redact_credentials is None:
        return text
    return redact_credentials(redact_exfiltration_urls(text)[0])[0]


try:                                  # pragma: no cover - import shape
    from kiro_crew import hooks
except Exception:                      # pragma: no cover - standalone fallback
    hooks = None  # type: ignore

# A record of this app's kind is a small JSON document; anything larger is not one.
JSON_MAX_BYTES = 4 * 1024 * 1024


def read_json_nolink(path: Path, within: Path) -> dict | None:
    """Read and parse a JSON object without following a link planted at `path`.

    Every file this app reads lives in a directory a review worker can reach, and
    runs are concurrent: a prompt-injected worker can replace a file with a symlink,
    and a plain `read_text` then dereferences it and hands the caller
    attacker-chosen JSON from anywhere the gateway can read.

    Returns None when the path is missing, is a plant, exceeds `JSON_MAX_BYTES`, or
    does not parse to an object. Schema validation is deliberately NOT done here --
    callers hold different schemas, so per-schema checks belong at the typed callers.
    """
    if hooks is None:  # pragma: no cover - standalone fallback
        try:
            raw: bytes | None = path.read_bytes()
        except OSError:
            return None
    else:
        raw = hooks.safe_read_file_bytes_nolink(
            str(path), str(within), max_bytes=JSON_MAX_BYTES)
    if raw is None:
        return None
    try:
        rec = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return rec if isinstance(rec, dict) else None


def read_text_nolink(path: Path, within: Path, *, max_bytes: int = JSON_MAX_BYTES) -> str | None:
    """Read a text file without following a link planted at `path`.

    The sibling of `read_json_nolink` for the app's non-JSON stores -- the learning
    catalogs are markdown, and they live in the same worker-reachable tree, so they
    need the same guard: a prompt-injected worker can replace one with a symlink to
    `~/.aws/credentials` and a plain `read_text` would dereference it straight into
    review data.

    Returns None when the path is missing, is a plant, exceeds `max_bytes`, or is not
    decodable as UTF-8. Every one of those reads as "no content", which is the same
    thing a caller does with an empty file.
    """
    if hooks is None:  # pragma: no cover - standalone fallback
        try:
            raw: bytes | None = path.read_bytes()
        except OSError:
            return None
    else:
        raw = hooks.safe_read_file_bytes_nolink(
            str(path), str(within), max_bytes=max_bytes)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def data_dir(root: Path | None = None) -> Path:
    return (root or app_root()) / "data"


# --- Per-run scratch ---------------------------------------------------------
# Every review run owns a private subtree, ``data/runs/<run-id>/``, holding its
# result records and its report. Runs used to share one ``data/results`` dir and
# one ``data/reports`` index, which forced the backend to serialize whole runs
# (an overlapping run would clear the records the other was still writing) and
# left only the newest report readable. Per-run isolation is what lets several
# reviews run at once AND keeps each finished report retrievable by run id.
#
# What stays GLOBAL (deliberately, do not move under a run): ``config.json``,
# ``reviewed.json`` (durable cross-run dedup index), and ``learnings/`` (the
# whole point of the learning store is that it outlives any single run).

_UNSAFE_RUN_ID = re.compile(r"[^A-Za-z0-9._-]+")


def safe_run_id(run_id: str) -> str:
    """Sanitize a run id into a single filesystem-safe path segment.

    Run ids are minted server-side (``uuid4().hex[:12]``), but this is the
    boundary where an id becomes a filesystem path, and ids also arrive from URL
    path params on the read endpoints — so sanitize unconditionally rather than
    trusting the caller.

    Two rules, both required:

    * Anything outside ``[A-Za-z0-9._-]`` collapses to ``_``, which makes
      separators (and therefore ``a/b``, ``../../etc``) unrepresentable.
    * A segment made ENTIRELY of dots is rejected outright. ``.`` and ``..`` are
      built from otherwise-safe characters, so the character filter alone lets
      them through untouched — and ``run_dir("..")`` would then resolve to the
      shared ``data/`` tree one level ABOVE ``runs/``. The dot rule is what
      actually closes containment.
    """
    stem = _UNSAFE_RUN_ID.sub("_", str(run_id)).strip("_")
    if not stem or set(stem) == {"."}:
        return "unknown"
    return stem


def runs_root(root: Path | None = None) -> Path:
    return data_dir(root) / "runs"


def run_dir(run_id: str, root: Path | None = None) -> Path:
    """The private subtree for one run. Always inside ``data/runs/``."""
    return runs_root(root) / safe_run_id(run_id)


def ensure_run_layout(run_id: str, root: Path | None = None) -> dict[str, str]:
    """Create one run's private ``results/`` + ``report/`` dirs. Idempotent."""
    rd = run_dir(run_id, root)
    results = rd / "results"
    reports = rd / "report"
    for d in (rd, results, reports):
        d.mkdir(parents=True, exist_ok=True)
    return {"runDir": str(rd), "resultsDir": str(results), "reportDir": str(reports)}


def remove_run_dir(run_id: str, root: Path | None = None) -> bool:
    """Delete one run's private subtree (called when a run is dismissed or ages
    out of the registry). Returns True when something was removed. Never raises
    — a run dir that is already gone, or unremovable, must not fail the caller."""
    rd = run_dir(run_id, root)
    # Containment assertion: rd is built from safe_run_id so it cannot escape,
    # but this is a recursive delete — verify the parent before removing.
    try:
        if rd.resolve().parent != runs_root(root).resolve():
            return False
    except OSError:  # pragma: no cover - defensive
        return False
    if not rd.exists():
        return False
    try:
        shutil.rmtree(rd)
        return True
    except OSError:  # pragma: no cover - defensive
        return False


def list_run_ids(root: Path | None = None) -> list[str]:
    """Run ids that currently have an on-disk subtree (used to reap orphans)."""
    rr = runs_root(root)
    if not rr.is_dir():
        return []
    return sorted(p.name for p in rr.iterdir() if p.is_dir())


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
    runs = data / "runs"          # per-run scratch: runs/<run-id>/{results,report}

    for d in (data, learnings, common, repos, namespaces, results, reports, runs):
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
