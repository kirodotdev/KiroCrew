"""Render ``FEATURES.md`` from ``features.json`` -- the GUI user-test scenario backlog.

``features.json`` is the single source of truth: one record per user-visible
feature (route + flow), classified by ``feature`` slug, ``runnable`` tier and
``priority``. ``FEATURES.md`` is derived from it by this script so the two can
never disagree; ``test_features_catalog.py`` fails when the committed markdown
is stale or a record is malformed.

.. code-block:: bash

    python test/gui_user/features_catalog.py --check   # exit 1 when FEATURES.md is stale
    python test/gui_user/features_catalog.py --write   # regenerate FEATURES.md

Everything here is data-shaping over repo-authored JSON; no model output is
involved, so the only hygiene needed is table-cell safety.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FEATURES_JSON = HERE / "features.json"
FEATURES_MD = HERE / "FEATURES.md"

#: Product-area slugs in report order, slug -> title. Mirrors the closed
#: registry the scenario loader accepts; a record naming any other slug is a
#: validation error, so a mis-filed feature is caught before it reaches a YAML.
FEATURE_TITLES: dict[str, str] = {
    "chat": "Chat sessions",
    "side-panel": "Side panel tabs",
    "terminal": "Terminal panel",
    "sidebar": "Sessions sidebar & folders",
    "navigation": "Routing & redirects",
    "topbar": "Top bar",
    "search": "Search everywhere & command palette",
    "members": "Crew Members",
    "capabilities": "Agent capabilities (crews, templates, skills, prompts, steering, hooks, workflows)",
    "connections": "Connections (MCP servers & services)",
    "memory": "Memory, lessons & usage",
    "knowledge": "Knowledge library",
    "artifacts": "Artifacts",
    "files": "File viewer & project files",
    "browser-panel": "Browser panel",
    "apps": "Apps & App Store",
    "task-runner": "Task Runner",
    "worlds": "Worlds (3D scenes)",
    "dev-fleet": "Dev Fleet",
    "schedule": "Schedule (cron jobs)",
    "api": "Headless API surfaces",
    "webhooks": "Inbound webhooks",
    "channels": "Chat channel integrations",
    "voice": "Voice",
    "notifications": "Notifications",
    "computer-use": "Computer Use",
    "instances": "Multi-instance shell",
    "remote-instances": "Remote instances & cloud launch",
    "popout": "Popouts & embeds",
    "auth": "Authentication & sign-in",
    "onboarding": "Onboarding",
    "settings": "Settings",
    "themes": "Themes",
    "security": "Security & governance",
    "developer": "Developer tools",
}
RUNNABLE: tuple[str, ...] = ("smoke", "nightly", "native-only", "needs-secret", "excluded")
PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3")
REQUIRED_KEYS: tuple[str, ...] = (
    "id",
    "feature",
    "title",
    "user_story",
    "start_url",
    "entry_path",
    "preconditions",
    "runnable",
    "estimated_steps",
    "rationale",
    "source",
    "priority",
    "merged_from",
    "component",
)


class CatalogError(ValueError):
    """features.json is malformed."""


def validate(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the records of ``doc`` in catalog order, or raise :class:`CatalogError`."""
    if not isinstance(doc, dict) or not isinstance(doc.get("features"), list):
        raise CatalogError("top level must be a mapping with a 'features' list")
    records: list[dict[str, Any]] = doc["features"]
    seen: set[str] = set()
    problems: list[str] = []
    for i, r in enumerate(records):
        if not isinstance(r, dict):
            problems.append(f"record #{i}: must be a mapping, got {type(r).__name__}")
            continue
        rid = str(r.get("id", "?"))
        for k in REQUIRED_KEYS:
            if k not in r:
                problems.append(f"{rid}: missing {k}")
        if not isinstance(r.get("feature"), str) or r["feature"] not in FEATURE_TITLES:
            problems.append(f"{rid}: feature {r.get('feature')!r} is not a registry slug")
        if rid in seen:
            problems.append(f"duplicate id {rid}")
        seen.add(rid)
        url = str(r.get("start_url", ""))
        if not url.startswith("/") or "?" in url:
            problems.append(f"{rid}: start_url must be an absolute path without a query")
        if r.get("runnable") not in RUNNABLE:
            problems.append(f"{rid}: runnable {r.get('runnable')!r} not in {RUNNABLE}")
        if r.get("priority") not in PRIORITIES:
            problems.append(f"{rid}: priority {r.get('priority')!r} not in {PRIORITIES}")
        if not isinstance(r.get("estimated_steps"), int) or isinstance(
            r.get("estimated_steps"), bool
        ):
            problems.append(f"{rid}: estimated_steps must be an integer")
        if not isinstance(r.get("preconditions"), dict):
            problems.append(f"{rid}: preconditions must be a mapping")
        if not isinstance(r.get("source"), list) or not isinstance(r.get("merged_from"), list):
            problems.append(f"{rid}: source and merged_from must be lists")
        if not isinstance(r.get("component"), str):
            problems.append(
                f"{rid}: component must be a string ('' when the flow has no UI component)"
            )
    problems += cross_slug_duplicates(records)
    proposed = doc.get("proposed_features", [])
    if not isinstance(proposed, list) or not all(isinstance(pf, dict) for pf in proposed):
        problems.append("proposed_features must be a list of mappings")
    counts = doc.get("counts") or {}
    computed = compute_counts(records)
    if counts != computed:
        problems.append(f"counts {counts} do not match the records {computed}")
    if problems:
        raise CatalogError("\n".join(problems))
    order = {p: i for i, p in enumerate(PRIORITIES)}
    slugs = list(FEATURE_TITLES)
    expected = sorted(
        records, key=lambda r: (slugs.index(r["feature"]), order[r["priority"]], r["id"])
    )
    if [r["id"] for r in expected] != [r["id"] for r in records]:
        raise CatalogError("records must be ordered by feature (registry order), priority, id")
    return records


def cross_slug_duplicates(records: list[Any]) -> list[str]:
    """One UI component at one route with one seed belongs to ONE feature slug.

    The inventory was merged per feature, so the same flow could survive twice
    under two slugs (a settings panel filed as ``settings`` by one reader and
    as ``voice`` by another). Same ``start_url`` + same seed + same
    ``component`` reached from two slugs is that duplicate, whatever the ids
    say. Records with no UI component (``component == ""``) are exempt: they
    are page-level rows from the feature map and cannot be told apart
    mechanically.
    """
    owners: dict[tuple[str, str, str], dict[str, list[str]]] = {}
    for r in records:
        if not isinstance(r, dict) or not r.get("component"):
            continue
        # ``validate`` reports a non-mapping ``preconditions`` as a problem but
        # keeps going so every problem is listed at once; guard here so that
        # malformed shape surfaces as the clean report, not an AttributeError.
        pre = r.get("preconditions")
        seed = pre.get("seed") if isinstance(pre, dict) else None
        key = (
            str(r.get("start_url")),
            str(seed),
            str(r["component"]),
        )
        owners.setdefault(key, {}).setdefault(str(r.get("feature")), []).append(str(r.get("id")))
    out: list[str] = []
    for (url, seed, component), by_slug in sorted(owners.items()):
        if len(by_slug) > 1:
            spelled = "; ".join(
                f"{slug}: {', '.join(ids)}" for slug, ids in sorted(by_slug.items())
            )
            out.append(
                f"cross-slug duplicate at {url} (seed {seed}, {component}): {spelled} -- "
                "merge into one record or move the flow to the slug that owns the component"
            )
    return out


def compute_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        t: sum(1 for r in records if isinstance(r, dict) and r.get("runnable") == t)
        for t in RUNNABLE
    }
    counts["total"] = len(records)
    return counts


def _cell(text: Any, *, max_chars: int = 400) -> str:
    s = str(text or "").replace("|", "/").replace("\n", " ").strip()
    return s if len(s) <= max_chars else s[: max_chars - 1] + "…"


def render(doc: dict[str, Any]) -> str:
    records = validate(doc)
    counts = doc["counts"]
    pri = {p: sum(1 for r in records if r["priority"] == p) for p in PRIORITIES}
    sha = str(doc.get("generated_from_sha", ""))[:12]
    lines = [
        "# GUI user-test feature inventory",
        "",
        "The backlog for `.github/workflows/gui-user-test.yml`: every user-visible feature of the",
        "dashboard, written as the scenario the lane would drive, so scenario batches can be cut",
        "by feature and by priority instead of by whoever remembered a page. This file is RENDERED",
        "from `features.json` by `features_catalog.py` -- edit the JSON, then run",
        "`python test/gui_user/features_catalog.py --write`; `test_features_catalog.py` fails when",
        "the two disagree. Generated from `docs/feature-map/README.md`, `website/src/pages/**`,",
        "`website/src/surfaces/builtins.tsx`, the settings registry, the builtin apps and",
        f"`docs/system-specs/modules/**` at commit `{sha}`, then deduped by route + flow. It is an",
        "inventory, not a contract: a row says what a person can do and where, the scenario YAML",
        "under `scenarios/` says how the lane checks it.",
        "",
        "## How to read a row",
        "",
        "- **Priority**: P0 = newly merged UI or a core daily path, first scenario batch; P1 = other",
        "  smoke-tier flows; P2 = nightly-tier flows; P3 = not generated (see *Not generated*).",
        "- **Runnable**: `smoke` = core path in about five model actions (PR + nightly); `nightly` =",
        "  runs on the lane's target (Xvfb + Chromium, fake ACP backend, no login, no network);",
        "  `native-only` = needs the Electron shell; `needs-secret` = needs a real external account",
        "  or credential; `excluded` = not observable through pixels or would take the target down.",
        "- **Seed**: the `KIROCREW_HOME` fixture the target boots from (`kirocrew gateway --seed`).",
        "- **Steps**: estimated model actions; the scenario's `max_steps` should sit a little above.",
        "",
        "## Counts",
        "",
        "| | Total | smoke | nightly | native-only | needs-secret | excluded |",
        "|---|---|---|---|---|---|---|",
        f"| **All features** | {counts['total']} | {counts['smoke']} | {counts['nightly']} "
        f"| {counts['native-only']} | {counts['needs-secret']} | {counts['excluded']} |",
    ]
    for slug, title in FEATURE_TITLES.items():
        group = [r for r in records if r["feature"] == slug]
        c = compute_counts(group)
        lines.append(
            f"| {title} (`{slug}`) | {c['total']} | {c['smoke']} | {c['nightly']} "
            f"| {c['native-only']} | {c['needs-secret']} | {c['excluded']} |"
        )
    raw = doc.get("raw_record_count")
    lines += [
        "",
        f"Priorities: P0 {pri['P0']} · P1 {pri['P1']} · P2 {pri['P2']} · P3 {pri['P3']}."
        + (f" Deduped from {raw} raw records." if raw else ""),
    ]
    for slug, title in FEATURE_TITLES.items():
        rows = [
            r for r in records if r["feature"] == slug and r["runnable"] in ("smoke", "nightly")
        ]
        if not rows:
            continue
        lines += ["", f"## {title} (`{slug}`)", ""]
        lines += [
            "| Priority | Id | User story | Start URL | Seed | Runnable | Steps |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            seed = r["preconditions"].get("seed", "rich")
            lines.append(
                f"| {r['priority']} | `{r['id']}` | {_cell(r['user_story'])} | `{r['start_url']}` "
                f"| {seed} | {r['runnable']} | {r['estimated_steps']} |"
            )
    lines += [
        "",
        "## Not generated",
        "",
        "Features the lane cannot drive on its target. Listed so the gap is a decision, not an omission.",
        "",
        "| Tier | Id | Feature | Title | Why |",
        "|---|---|---|---|---|",
    ]
    for tier in ("native-only", "needs-secret", "excluded"):
        for r in records:
            if r["runnable"] == tier:
                lines.append(
                    f"| {tier} | `{r['id']}` | {r['feature']} | {_cell(r['title'])} "
                    f"| {_cell(r['rationale'], max_chars=220)} |"
                )
    lines += ["", "## Proposed new feature slugs", ""]
    proposed = doc.get("proposed_features") or []
    if not proposed:
        lines.append(
            "None outstanding: every area the readers proposed is a slug in the `FEATURES` registry"
            " in `scenarios.py` (mirrored by `FEATURE_TITLES` here), and its records are filed there."
        )
    else:
        lines += [
            "Areas the readers could not place in the closed registry; each was filed under the",
            "closest existing slug for now. Adding a slug is a one-line change to the `FEATURES`",
            "registry in `scenarios.py` (mirrored by `FEATURE_TITLES` here) plus a row in",
            "`docs/build/gui-user-test.md`.",
            "",
            "| Slug | Title | Why |",
            "|---|---|---|",
        ]
        for pf in proposed:
            lines.append(
                f"| `{_cell(pf.get('slug'))}` | {_cell(pf.get('title'))} | {_cell(pf.get('reason'))} |"
            )
    return "\n".join(lines) + "\n"


def load(path: Path | None = None) -> dict[str, Any]:
    # Resolved at call time (not as a default bound at import) so callers and tests
    # that repoint the module-level FEATURES_JSON are honoured.
    return json.loads((path or FEATURES_JSON).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="render FEATURES.md from features.json")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 when FEATURES.md is stale")
    mode.add_argument("--write", action="store_true", help="regenerate FEATURES.md")
    args = p.parse_args(argv)
    try:
        md = render(load())
    except (OSError, ValueError) as exc:
        print(f"features.json: {exc}", file=sys.stderr)
        return 2
    if args.write:
        FEATURES_MD.write_text(md, encoding="utf-8")
        print(f"wrote {FEATURES_MD}")
        return 0
    current = FEATURES_MD.read_text(encoding="utf-8") if FEATURES_MD.exists() else ""
    if current != md:
        print(
            "FEATURES.md is stale: run `python test/gui_user/features_catalog.py --write`",
            file=sys.stderr,
        )
        return 1
    print("FEATURES.md is up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
