"""New-user friction: what confused the tester, reported beside (not inside) the verdict.

The harness gives the model one extra tool, ``report_friction``. Whenever the
persona (``scenarios.PERSONAS``) stalls -- cannot find a control, clicks the
wrong thing, does not understand a label or an icon, does not know what just
happened -- it files one structured entry and carries on with the task. A
scenario can PASS with a dozen friction entries and FAIL with none: the two
channels never touch each other.

This module owns everything about those entries once the model has spoken:

* :data:`FRICTION_TOOL` -- the tool schema the harness advertises;
* :func:`validate_entry` -- the only way an entry gets into ``summary.json``
  (every field typed, bounded and stripped; the model cannot smuggle Markdown,
  fences or mentions into a bot-authored comment);
* :func:`entry_key` -- the cross-night identity ``(feature, element,
  what_confused)`` after normalization, so the same confusion seen on
  consecutive nights is one row with a count, not a new row each night;
* :func:`merge_ledger` -- folds a run into the ledger the workflow carries
  from night to night as an artifact;
* :func:`render_section` -- the "New-user friction" block for ``verdict.md``,
  the run summary and the nightly issue, grouped by feature, ordered by
  severity;
* :func:`plan_issues` / :func:`file_issues` -- the GitHub issues: at most
  ``ISSUE_CAP`` new ones per night for non-cosmetic rows that have no issue yet
  (new tonight, or held over by an earlier night's cap),
  one "again on <date>" comment for a recurrence that already has an issue;
* :func:`ledger_source_runs` / :func:`ledger_artifact_id` / :func:`pick_ledger`
  -- WHERE the previous ledger may come from. The ledger feeds a job that writes
  issues with the repository token, so it is never looked up by artifact name
  across the repository (a fork pull request can upload an artifact of any
  name): the source is this run's own earlier attempt when there is one, else
  the newest completed scheduled run of THIS workflow on the default branch,
  and the artifact is taken from that run alone.

CLI (what the workflow calls)::

    python test/gui_user/friction.py pick-ledger --repo owner/name --branch main \
        --self-run $GITHUB_RUN_ID --out prev/pick.json   # {"run_id": ..., "artifact_id": ...} or nulls
    python test/gui_user/friction.py merge  --summary results/summary.json \
        --ledger prev/friction_ledger.json --out results/friction.json \
        --out-ledger results/friction_ledger.json --date YYYY-MM-DD \
        --run-url ... --artifact-url ... --head-sha ...
    python test/gui_user/friction.py issues --ledger results/friction_ledger.json \
        --repo owner/name --date YYYY-MM-DD [--dry-run]
    python test/gui_user/friction.py snapshot --ledger results/friction_ledger.json \
        --out results/friction.json --date YYYY-MM-DD     # after issues: rows now carry numbers
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

if __package__ in (None, ""):  # ``python test/gui_user/friction.py``
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui_user.scenarios import FEATURES  # noqa: E402

SEVERITIES: tuple[str, ...] = ("blocker", "slows-down", "cosmetic")
#: Longest text the model may put in one field; longer is cut, never rejected,
#: so a verbose tester does not lose the entry.
FIELD_MAX = 240
#: Entries one attempt may file; past this the tool answers "log full" and the
#: task continues. Bounds the cost of a tester who narrates every pixel.
ATTEMPT_CAP = 12
#: New issues one night may open; the rest stay in the run summary.
ISSUE_CAP = 5
LEDGER_VERSION = 1
LEDGER_FILE = "friction_ledger.json"
LEDGER_ARTIFACT = "gui-user-test-friction-ledger"
WORKFLOW_PATH = ".github/workflows/gui-user-test.yml"
LEDGER_EVENT = "schedule"
# Which finished runs may supply the ledger. A FAIL verdict, a cancellation or a
# timeout all come AFTER the ledger was written and uploaded (the upload step runs
# whenever the merge step succeeded, and an artifact either finalized or does not
# exist), so a ledger found on any of them is a whole night; rejecting a cancelled
# run would drop the issue numbers it filed and re-file them. Only conclusions
# under which the job never ran are out (skipped, action_required, stale, ...).
LEDGER_CONCLUSIONS: frozenset[str] = frozenset({"success", "failure", "cancelled", "timed_out"})
# Bound on the rendered "New-user friction" section: it rides inside an issue
# body / comment whose ceiling is 65,536 characters alongside the scenario table,
# and a run can produce many maximum-length rows. Rows past the budget are
# counted, not shown; every row is in the artifact's friction.json.
SECTION_MAX_CHARS = 30_000
# Pages of completed nightlies the ledger lookup will walk (100 runs each) before
# concluding no ledger exists: ~500 nights, far past the 90-day artifact retention.
MAX_RUN_PAGES = 5
FRICTION_FILE = "friction.json"
ISSUE_MARKER = "<!-- gui-user-friction "
LABEL_UX = "ux"
LABEL_CHANNEL = "channel: gui-user-test"
#: ``feature`` slug -> the repo's ``area:`` label, for the slugs whose surface is
#: not the dashboard shell. Every key must be a ``FEATURES`` slug (a unit test
#: holds it); anything unlisted is the dashboard.
AREA_LABELS: dict[str, str] = {
    "apps": "area: apps",
    "schedule": "area: cron",
}
DEFAULT_AREA_LABEL = "area: dashboard"

FRICTION_TOOL: dict[str, Any] = {
    "name": "report_friction",
    "description": (
        "Record ONE moment where the app confused you as a first-time user: you paused for more "
        "than a glance to find something, could not find a control, clicked the wrong thing, did "
        "not understand a label, icon or message, did not know what was happening, or the layout "
        "hid the main action. Call it the moment it happens, then continue the task. Say it in "
        "your own words. This never changes the task or its verdict."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "surface": {
                "type": "string",
                "maxLength": FIELD_MAX,
                "description": "Page or panel you were on (as a user would name it).",
            },
            "element": {
                "type": "string",
                "maxLength": FIELD_MAX,
                "description": "The control, text or area involved, described so someone else could find it.",
            },
            "what_confused": {
                "type": "string",
                "maxLength": FIELD_MAX,
                "description": "One sentence, first person: what confused you.",
            },
            "expected": {
                "type": "string",
                "maxLength": FIELD_MAX,
                "description": "What you expected to see or happen.",
            },
            "actual": {
                "type": "string",
                "maxLength": FIELD_MAX,
                "description": "What actually was there or happened.",
            },
            "severity": {
                "type": "string",
                "enum": list(SEVERITIES),
                "description": (
                    "blocker = you could not continue without guessing; slows-down = you got there "
                    "but lost time; cosmetic = it looked wrong but did not slow you down."
                ),
            },
        },
        "required": ["surface", "element", "what_confused", "expected", "actual", "severity"],
        "additionalProperties": False,
    },
}

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^a-z0-9 ]+")


class FrictionError(ValueError):
    """A friction entry or ledger is malformed."""


# --------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------


def _clean(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise FrictionError(f"{what} must be a string")
    kept = "".join(ch for ch in value if ch.isprintable() or ch in "\n\t")
    text = _WS.sub(" ", kept).strip()
    if not text:
        raise FrictionError(f"{what} must not be empty")
    return text if len(text) <= FIELD_MAX else text[: FIELD_MAX - 1] + "…"


def normalize(text: str) -> str:
    """Case-, whitespace- and punctuation-insensitive form used for the key."""
    return _WS.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


def entry_key(feature: str, element: str, what_confused: str) -> str:
    raw = "\n".join([feature, normalize(element), normalize(what_confused)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def validate_entry(
    raw: Any, *, feature: str, scenario: str, screenshot: str, step: int
) -> dict[str, Any]:
    """Turn a ``report_friction`` tool input into a stored entry, or raise."""
    if not isinstance(raw, dict):
        raise FrictionError("friction input must be a mapping")
    unknown = set(raw) - set(FRICTION_TOOL["input_schema"]["properties"])
    if unknown:
        raise FrictionError(f"unknown friction fields {sorted(unknown)}")
    severity = raw.get("severity")
    if severity not in SEVERITIES:
        raise FrictionError(f"severity must be one of {SEVERITIES}")
    if feature not in FEATURES:
        raise FrictionError(f"feature {feature!r} is not a registry slug")
    entry = {
        "feature": feature,
        "scenario": scenario,
        "surface": _clean(raw.get("surface"), "surface"),
        "element": _clean(raw.get("element"), "element"),
        "what_confused": _clean(raw.get("what_confused"), "what_confused"),
        "expected": _clean(raw.get("expected"), "expected"),
        "actual": _clean(raw.get("actual"), "actual"),
        "severity": severity,
        "screenshot": str(screenshot),
        "step": int(step),
    }
    entry["key"] = entry_key(feature, entry["element"], entry["what_confused"])
    return entry


def collect(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Every entry of a run, one per key, in first-sighting order.

    A key seen twice in one run -- the retry of a scenario reporting the same
    confusion, two scenarios stumbling on the same control -- is folded the way
    :func:`merge_ledger` folds nights: the newest sighting's evidence (surface,
    wording, screenshot) with the WORST severity either sighting gave it. A
    first attempt that called something cosmetic must not hide the retry that
    found it a blocker: cosmetic rows never reach :func:`plan_issues`.
    """
    by_key: dict[str, dict[str, Any]] = {}
    for sc in summary.get("scenarios") or []:
        if not isinstance(sc, dict):
            continue
        for att in sc.get("attempts") or []:
            if not isinstance(att, dict):
                continue
            for e in att.get("friction") or []:
                if not isinstance(e, dict) or e.get("severity") not in SEVERITIES:
                    continue
                key = str(e.get("key") or "")
                if not key:
                    continue
                prior = by_key.get(key)
                merged = dict(e)
                if prior is not None and severity_rank(prior["severity"]) < severity_rank(
                    merged["severity"]
                ):
                    merged["severity"] = prior["severity"]
                by_key[key] = merged
    return list(by_key.values())


def severity_rank(severity: str) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)


def sort_entries(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    slugs = list(FEATURES)

    def rank(e: dict[str, Any]) -> tuple[int, int, str]:
        f = str(e.get("feature", ""))
        return (
            slugs.index(f) if f in slugs else len(slugs),
            severity_rank(e["severity"]),
            e["key"],
        )

    return sorted(entries, key=rank)


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------


def empty_ledger() -> dict[str, Any]:
    return {"version": LEDGER_VERSION, "entries": {}}


def load_ledger(path: Optional[Path]) -> dict[str, Any]:
    if path is None or not path.exists():
        return empty_ledger()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FrictionError(f"ledger {path}: {exc}") from exc
    if (
        not isinstance(doc, dict)
        or doc.get("version") != LEDGER_VERSION
        or not isinstance(doc.get("entries"), dict)
    ):
        raise FrictionError(f"ledger {path}: unexpected shape")
    for key, row in doc["entries"].items():
        if not isinstance(row, dict) or row.get("severity") not in SEVERITIES:
            raise FrictionError(f"ledger {path}: entry {key} is malformed")
    return doc


def merge_ledger(
    ledger: dict[str, Any],
    entries: Iterable[dict[str, Any]],
    *,
    date: str,
    run_url: str = "",
    artifact_url: str = "",
    head_sha: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """Fold one run into the ledger; return ``(ledger, keys that are new tonight)``.

    A recurrence bumps ``count`` and ``last_seen`` and takes the newest sighting
    whole (surface, wording, expected/actual, screenshot) plus the worst severity
    of old and new, so a blocker never downgrades to cosmetic because one
    night's tester shrugged. Runs are told apart by ``run_url``: the same run
    merged twice is a no-op, a different run on the same day refreshes the
    evidence but does not count the day twice.
    """
    rows: dict[str, Any] = ledger["entries"]
    new_keys: list[str] = []
    for e in entries:
        key = e["key"]
        row = rows.get(key)
        if row is None:
            rows[key] = {
                "feature": e["feature"],
                "scenario": e["scenario"],
                "surface": e["surface"],
                "element": e["element"],
                "what_confused": e["what_confused"],
                "expected": e["expected"],
                "actual": e["actual"],
                "severity": e["severity"],
                "screenshot": e["screenshot"],
                "first_seen": date,
                "last_seen": date,
                "count": 1,
                "issue": None,
                "run_url": run_url,
                "artifact_url": artifact_url,
                "head_sha": head_sha,
            }
            new_keys.append(key)
            continue
        if row.get("run_url") == run_url:
            # The same run merged twice -- a re-run of the night, even one that
            # crossed midnight UTC and so carries a later ``date``: attempt 1
            # already counted it, filed it and uploaded its evidence. Not a new
            # night, not a recurrence, nothing to refresh.
            continue
        same_day = row.get("last_seen") == date
        if not same_day:
            # A night counts once, however many runs saw the row that day.
            row["count"] = int(row.get("count", 1)) + 1
            row["last_seen"] = date
        # Take the newest sighting whole -- surface, wording and evidence together --
        # so a row never pairs an old location with a new screenshot, and a
        # same-day run other than the one recorded (a dispatch after the nightly)
        # reports ITS evidence under ITS artifact, not the earlier run's. The key
        # is normalized, so ``element`` / ``what_confused`` may differ in spelling.
        for field in ("scenario", "surface", "element", "what_confused", "expected", "actual"):
            row[field] = e[field]
        row["screenshot"] = e["screenshot"]
        row["run_url"] = run_url
        row["artifact_url"] = artifact_url
        row["head_sha"] = head_sha
        if severity_rank(e["severity"]) < severity_rank(row["severity"]):
            row["severity"] = e["severity"]
    return ledger, new_keys


def tonight(ledger: dict[str, Any], date: str) -> list[dict[str, Any]]:
    """Ledger rows seen on ``date``, as entries carrying their key, count and issue."""
    out = []
    for key, row in ledger["entries"].items():
        if row.get("last_seen") == date:
            out.append({**row, "key": key})
    return sort_entries(out)


def rows_for(ledger: dict[str, Any], keys: Iterable[str]) -> list[dict[str, Any]]:
    """Ledger rows for exactly these keys (one run's own sightings), in catalog order.

    ``friction.json`` is scoped this way rather than by date: a dispatch on the
    same day as the nightly must report its own rows, not the nightly's, or its
    screenshot links would point into the wrong artifact.
    """
    wanted = set(keys)
    return sort_entries(
        [{**row, "key": key} for key, row in ledger["entries"].items() if key in wanted]
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


_SCHEME = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*):(//)")
_WWW = re.compile(r"(?i)\bwww\.")


def _cell(text: Any, *, max_chars: int = FIELD_MAX) -> str:
    """Model-authored text rendered inert in a Markdown table cell / issue body.

    Pipes, backticks, angle and square brackets lose their Markdown meaning, an
    ``@`` cannot mention, and a URL is defanged (zero-width space after the
    scheme colon and inside ``www.``) so GitHub never autolinks a URL the tester
    merely read off the screen -- an injected page must not become a live link
    in a bot-authored issue.
    """
    s = str(text or "")
    kept = "".join(ch for ch in s if ch.isprintable())
    kept = kept.replace("|", "/").replace("`", "'").replace("@", "@\u200b")
    # "#123" / "owner/repo#123" would autolink and notify that issue's thread.
    kept = re.sub(r"#(?=\d)", "#\u200b", kept)
    kept = kept.replace("<", "‹").replace(">", "›").replace("[", "(").replace("]", ")")
    kept = _SCHEME.sub("\\1:\u200b\\2", kept)
    kept = _WWW.sub("www\u200b.", kept)
    kept = _WS.sub(" ", kept).strip()
    return kept if len(kept) <= max_chars else kept[: max_chars - 1] + "…"


def _shot_link(entry: dict[str, Any], artifact_url: Optional[str]) -> str:
    shot = _cell(entry.get("screenshot"), max_chars=120)
    if not shot:
        return "—"
    url = artifact_url or entry.get("artifact_url") or ""
    return f"[`{shot}`]({url})" if url else f"`{shot}`"


def render_section(
    entries: Iterable[dict[str, Any]],
    *,
    artifact_url: Optional[str] = None,
    max_chars: int = SECTION_MAX_CHARS,
) -> str:
    """The "New-user friction" block: one table per feature, worst severity first.

    Never longer than ``max_chars``: once the budget is spent the remaining rows
    (already sorted, so the worst are the ones shown) are summarised in one
    line, because the block is embedded in issue bodies and comments GitHub caps
    at 65,536 characters.
    """
    rows = sort_entries(entries)
    lines = ["## New-user friction", ""]
    if not rows:
        lines.append("_The tester reported nothing confusing in this run._")
        return "\n".join(lines) + "\n"
    counts = {s: sum(1 for r in rows if r["severity"] == s) for s in SEVERITIES}
    lines.append(
        f"_{len(rows)} moment(s) where a first-time user stalled: "
        + " · ".join(f"{counts[s]} {s}" for s in SEVERITIES)
        + ". Reported by the tester persona beside the verdict; a scenario can PASS and still list friction._"
    )
    current = None
    used = sum(len(line) + 1 for line in lines)
    shown = 0
    for r in rows:
        chunk: list[str] = []
        if r["feature"] != current:
            chunk += [
                "",
                f"### {FEATURES.get(r['feature'], r['feature'])} (`{r['feature']}`)",
                "",
                "| Severity | What confused me | Where | Expected → actual | Seen | Screenshot |",
                "|---|---|---|---|---|---|",
            ]
        seen = f"{int(r.get('count', 1))}× · last {r.get('last_seen', '')}".strip(" ·")
        if int(r.get("count", 1)) == 1:
            seen = "new"
        issue = r.get("issue")
        if issue:
            seen += f" · #{int(issue)}"
        chunk.append(
            f"| {r['severity']} | {_cell(r['what_confused'])} | {_cell(r['surface'])} → {_cell(r['element'])} "
            f"| {_cell(r['expected'])} → {_cell(r['actual'])} | {seen} | {_shot_link(r, artifact_url)} |"
        )
        cost = sum(len(line) + 1 for line in chunk)
        if used + cost > max_chars - 160:  # keep room for the closing line
            break
        lines += chunk
        used += cost
        shown += 1
        current = r["feature"]
    if shown < len(rows):
        lines += [
            "",
            f"_… {len(rows) - shown} more row(s) not shown to keep this report within GitHub's "
            "size limit; every row is in the run artifact's `friction.json`._",
        ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Issues
# --------------------------------------------------------------------------


def issue_title(row: dict[str, Any]) -> str:
    text = _cell(row["what_confused"], max_chars=90)
    return f"ux({row['feature']}): {text}"


def issue_body(key: str, row: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"{ISSUE_MARKER}{key} -->",
            f"A first-time user drove `{row.get('scenario', '')}` (feature `{row['feature']}`) in the nightly "
            f"GUI user test and stalled here. Severity **{row['severity']}**. Opened by the lane; the wording "
            "below is the tester's, in the first person.",
            "",
            f"- **Where:** {_cell(row['surface'])} → {_cell(row['element'])}",
            f"- **What confused me:** {_cell(row['what_confused'])}",
            f"- **Expected:** {_cell(row['expected'])}",
            f"- **Actual:** {_cell(row['actual'])}",
            f"- **First seen:** {row.get('first_seen', '')} · **seen** {int(row.get('count', 1))}×"
            + (
                " · earlier issue(s): " + ", ".join(f"#{int(n)}" for n in row["closed_issues"])
                if row.get("closed_issues")
                else ""
            ),
            f"- **Screenshot:** {_shot_link(row, None)} · [run]({row.get('run_url', '')}) · `{row.get('head_sha', '')}`",
            "",
            "Recurrences are appended as comments by the lane; close when the confusion is gone (the lane "
            "reopens nothing -- a sighting after the close opens a new issue that links back here).",
            "",
            "Lane: `.github/workflows/gui-user-test.yml` · docs: `docs/build/gui-user-test.md` · tracking: #9578",
        ]
    )


def plan_issues(ledger: dict[str, Any], *, date: str, cap: int = ISSUE_CAP) -> dict[str, list[str]]:
    """Which ledger keys get a new issue tonight, which get a recurrence comment.

    Cosmetic rows never leave the summary. Every non-cosmetic row in the ledger
    that has no issue yet is a candidate -- tonight's sightings first, then rows
    an earlier night's cap held over, each group in registry-then-severity
    order -- and the first ``cap`` are opened. A row seen tonight that already
    has an issue and a count above one gets an "again on <date>" comment, once
    per date (``last_commented`` makes a same-day rerun idempotent).
    """
    seen_tonight = {r["key"] for r in tonight(ledger, date)}
    unissued = [
        {**row, "key": key}
        for key, row in ledger["entries"].items()
        if row["severity"] != "cosmetic" and not row.get("issue")
    ]
    ordered = sort_entries([r for r in unissued if r["key"] in seen_tonight]) + sort_entries(
        [r for r in unissued if r["key"] not in seen_tonight]
    )
    create = [r["key"] for r in ordered[:cap]]
    comment = [
        r["key"]
        for r in tonight(ledger, date)
        if r["severity"] != "cosmetic"
        and r.get("issue")
        and int(r.get("count", 1)) > 1
        and r.get("last_commented") != date  # a same-day rerun must not post the comment twice
    ]
    return {"create": create, "comment": comment}


Runner = Callable[[list[str]], str]


def _full_name(repo_block: Any) -> Optional[str]:
    """``owner/name`` from an API repository block, None when it is missing or malformed."""
    if not isinstance(repo_block, dict):
        return None
    name = repo_block.get("full_name")
    return name if isinstance(name, str) else None


def ledger_source_run(
    run: Any,
    *,
    repo: str,
    branch: str,
    workflow_path: str = WORKFLOW_PATH,
    event: str = LEDGER_EVENT,
    require_completed: bool = True,
) -> Optional[int]:
    """The run's id if every provenance field says it may supply the ledger, else None.

    ``run`` is one Actions API run object. It qualifies only when it is a run OF
    this workflow file, fired BY the trusted event, ON the trusted branch, whose
    head and base repository are both this repository (never a fork), and --
    unless ``require_completed`` is off, which only the current run's own prior
    attempt earns -- that completed with a conclusion in
    :data:`LEDGER_CONCLUSIONS`. Anything malformed is None, never guessed at.
    """
    if not isinstance(run, dict):
        return None
    run_id = run.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool):
        return None
    if run.get("path") != workflow_path or run.get("event") != event:
        return None
    if run.get("head_branch") != branch:
        return None
    if require_completed and (
        run.get("status") != "completed" or run.get("conclusion") not in LEDGER_CONCLUSIONS
    ):
        return None
    if _full_name(run.get("repository")) != repo or _full_name(run.get("head_repository")) != repo:
        return None
    return run_id


def ledger_source_runs(
    runs: Iterable[Any],
    *,
    repo: str,
    branch: str,
    workflow_path: str = WORKFLOW_PATH,
    event: str = LEDGER_EVENT,
    exclude_run_id: Optional[int] = None,
) -> list[int]:
    """Ids of the completed runs that may supply the previous ledger, newest first.

    ``runs`` is the ``workflow_runs`` list of the Actions API; each is judged by
    :func:`ledger_source_run`. ``exclude_run_id`` (the current run) is left out
    here because :func:`pick_ledger` consults it separately, and first.
    """
    picked: list[tuple[str, int]] = []
    for run in runs:
        run_id = ledger_source_run(
            run, repo=repo, branch=branch, workflow_path=workflow_path, event=event
        )
        if run_id is None or run_id == exclude_run_id:
            continue
        picked.append((str(run.get("created_at") or ""), run_id))
    picked.sort(reverse=True)
    return [run_id for _, run_id in picked]


def ledger_artifact_id(
    artifacts: Iterable[Any], *, run_id: int, branch: str, name: str = LEDGER_ARTIFACT
) -> Optional[int]:
    """The id of the live ledger artifact that ``run_id`` itself produced, else None.

    ``artifacts`` is the ``artifacts`` list of ``GET /actions/runs/{id}/artifacts``.
    The artifact's own ``workflow_run`` block must name the trusted run and
    branch -- the listing was scoped to the run, but the check is repeated here
    so a wrong listing (or a wrong caller) cannot slip a foreign artifact in.
    """
    for art in artifacts:
        if not isinstance(art, dict) or art.get("name") != name or art.get("expired"):
            continue
        wf = art.get("workflow_run")
        if not isinstance(wf, dict) or wf.get("id") != run_id or wf.get("head_branch") != branch:
            continue
        art_id = art.get("id")
        if isinstance(art_id, int) and not isinstance(art_id, bool):
            return art_id
    return None


def _ledger_of_run(
    run_id: int, *, branch: str, fetch: Callable[[str], Any]
) -> Optional[dict[str, Optional[int]]]:
    arts = fetch(f"actions/runs/{run_id}/artifacts?name={LEDGER_ARTIFACT}")
    items = arts.get("artifacts") if isinstance(arts, dict) else None
    if not isinstance(items, list):
        raise FrictionError(f"artifact listing for run {run_id} has no artifacts list")
    art_id = ledger_artifact_id(items, run_id=run_id, branch=branch)
    return None if art_id is None else {"run_id": run_id, "artifact_id": art_id}


def pick_ledger(
    *,
    repo: str,
    branch: str,
    self_run_id: Optional[int],
    fetch: Callable[[str], Any],
    per_page: int = 100,
) -> dict[str, Optional[int]]:
    """Resolve the previous ledger's (run id, artifact id) through ``fetch``.

    ``fetch(path)`` returns the decoded JSON of ``GET /repos/{repo}/{path}`` and
    raises on any API failure -- a failure propagates, because a retrieval error
    is not a first night (the caller must refuse to start a fresh ledger). Only a
    complete answer with NO eligible run or artifact yields ``{"run_id": None,
    "artifact_id": None}``.

    The current run (``self_run_id``) is consulted FIRST, by fetching its own
    run object and judging it like any other except that it may still be in
    progress: on a re-run of a scheduled night the earlier attempt already
    filed issues and uploaded the ledger that records them, and reading last
    night's ledger instead would file them all again. A ledger is uploaded only
    after the merge step succeeded, so a prior attempt's artifact is a whole
    night, never a half-written one. A current run that is not itself a trusted
    scheduled run (a dispatch, a pull request) never wrote a ledger and is
    skipped. Then the completed candidates are walked newest first and the
    first run that still holds a live ledger artifact wins, so a night whose
    merge step failed (and uploaded nothing) is skipped rather than treated as
    a reset.
    """
    if self_run_id is not None:
        me = fetch(f"actions/runs/{self_run_id}")
        if ledger_source_run(me, repo=repo, branch=branch, require_completed=False) == self_run_id:
            mine = _ledger_of_run(self_run_id, branch=branch, fetch=fetch)
            if mine is not None:
                return mine
    # Walk the completed nightlies page by page, newest first, until one holds a
    # ledger or the listing runs dry. A single page would turn a long streak of
    # merge-step failures into a false "first night" while an older ledger still
    # sits inside its retention window; MAX_RUN_PAGES bounds the walk well past
    # that window (nightly cadence, 90-day artifact retention).
    for page in range(1, MAX_RUN_PAGES + 1):
        listing = fetch(
            f"actions/workflows/{Path(WORKFLOW_PATH).name}/runs"
            f"?event={LEDGER_EVENT}&branch={branch}&status=completed"
            f"&per_page={per_page}&page={page}"
        )
        runs = listing.get("workflow_runs") if isinstance(listing, dict) else None
        if not isinstance(runs, list):
            raise FrictionError("workflow runs listing has no workflow_runs list")
        for run_id in ledger_source_runs(
            runs, repo=repo, branch=branch, exclude_run_id=self_run_id
        ):
            found = _ledger_of_run(run_id, branch=branch, fetch=fetch)
            if found is not None:
                return found
        if len(runs) < per_page:
            break
    return {"run_id": None, "artifact_id": None}


def _gh(args: list[str]) -> str:
    proc = subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return proc.stdout


Persist = Callable[[dict[str, Any]], None]


def _existing_issue(run: Runner, *, repo: str, key: str) -> Optional[int]:
    """The number of an OPEN issue whose body carries ``key``'s marker, else None.

    Searches by the key (a hex digest, so the search is exact enough) and then
    checks the literal marker in each body, so a stray mention of the digest in
    prose cannot be adopted. Raises on a ``gh`` failure or an unreadable answer.
    """
    out = run(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--label",
            LABEL_UX,
            "--search",
            f"{key} in:body",
            "--json",
            "number,body",
            "--limit",
            "20",
        ]
    )
    marker = f"{ISSUE_MARKER}{key} -->"
    for item in json.loads(out):
        if not isinstance(item, dict) or marker not in str(item.get("body") or ""):
            continue
        number = item.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            return number
    return None


def file_issues(
    ledger: dict[str, Any],
    *,
    repo: str,
    date: str,
    run: Runner = _gh,
    cap: int = ISSUE_CAP,
    persist: Optional[Persist] = None,
) -> dict[str, Any]:
    """Open / comment the planned issues via ``gh``; record issue numbers in the ledger.

    ``persist`` (the ledger writer) is called after EVERY successful ``gh``
    mutation, so a failure partway through a batch never loses the numbers of
    the issues already opened -- a rerun would otherwise open them twice. A
    failed create or comment is recorded under ``failed`` and the batch goes on.
    """
    rows = ledger["entries"]
    failed: list[dict[str, str]] = []

    def save() -> None:
        if persist is not None:
            persist(ledger)

    # A recurrence whose issue a human has since CLOSED is a new sighting, not a
    # comment on a closed thread: forget the mapping (kept under ``closed_issues``)
    # so the plan below opens a fresh issue. Unknown state (gh error) keeps the
    # mapping and the comment path -- never lose a number on a transient failure.
    for row in tonight(ledger, date):
        issue = row.get("issue")
        if not issue:
            continue
        try:
            state = json.loads(
                run(["issue", "view", str(issue), "--repo", repo, "--json", "state"])
            ).get("state")
        except (subprocess.CalledProcessError, ValueError):
            continue
        if state == "CLOSED":
            live = rows[row["key"]]
            live.setdefault("closed_issues", []).append(int(issue))
            live["issue"] = None
            live.pop("last_commented", None)
            save()

    plan = plan_issues(ledger, date=date, cap=cap)

    for label, color, desc in (
        (LABEL_UX, "D4C5F9", "New-user confusion reported by the GUI user-test persona"),
        (LABEL_CHANNEL, "0E8A16", "Filed by the nightly GUI user-test lane"),
    ):
        try:
            run(
                [
                    "label",
                    "create",
                    label,
                    "--repo",
                    repo,
                    "--color",
                    color,
                    "--description",
                    desc,
                    "--force",
                ]
            )
        except subprocess.CalledProcessError:
            pass  # the label exists or labels are not ours to make; create still works
    opened: list[int] = []
    adopted: list[int] = []
    for key in plan["create"]:
        row = rows[key]
        # The ledger may have lost this key's number (the run that opened the
        # issue was cancelled before it uploaded): an OPEN issue already carrying
        # the key's marker is adopted, never duplicated. A failed lookup skips
        # the key for tonight -- a duplicate is worse than a night's delay.
        try:
            existing = _existing_issue(run, repo=repo, key=key)
        except (subprocess.CalledProcessError, ValueError) as exc:
            failed.append({"key": key, "op": "reconcile", "error": str(exc)})
            continue
        if existing is not None:
            row["issue"] = existing
            adopted.append(existing)
            save()
            continue
        try:
            out = run(
                [
                    "issue",
                    "create",
                    "--repo",
                    repo,
                    "--title",
                    issue_title(row),
                    "--body",
                    issue_body(key, row),
                    "--label",
                    LABEL_UX,
                    "--label",
                    LABEL_CHANNEL,
                    "--label",
                    AREA_LABELS.get(row["feature"], DEFAULT_AREA_LABEL),
                ]
            )
        except subprocess.CalledProcessError as exc:
            failed.append({"key": key, "op": "create", "error": str(exc)})
            continue
        m = re.search(r"/issues/(\d+)", out)
        if not m:
            failed.append({"key": key, "op": "create", "error": "no issue URL in gh output"})
            continue
        row["issue"] = int(m.group(1))
        opened.append(row["issue"])
        save()
    commented: list[int] = []
    for key in plan["comment"]:
        row = rows[key]
        body = (
            f"Again on {date} ({int(row.get('count', 1))}× so far, severity {row['severity']}): "
            f"{_cell(row['what_confused'])} — {_shot_link(row, None)} · [run]({row.get('run_url', '')})"
        )
        try:
            run(["issue", "comment", str(row["issue"]), "--repo", repo, "--body", body])
        except subprocess.CalledProcessError as exc:
            failed.append({"key": key, "op": "comment", "error": str(exc)})
            continue
        row["last_commented"] = date
        commented.append(int(row["issue"]))
        save()
    return {"opened": opened, "adopted": adopted, "commented": commented, "failed": failed}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _write(path: Path, doc: Any) -> None:
    """Write ``doc`` as JSON, atomically.

    The ledger is written after every ``gh`` mutation and then uploaded by a
    step that only checks the file exists, so a write cut short (disk full, a
    killed runner) must never leave a truncated file under the canonical name:
    the next night would fail to load it, upload nothing, and keep failing until
    someone deleted the artifact by hand. The document goes to a sibling
    temporary file first and is renamed over the target only once it is fully
    on disk; ``os.replace`` is atomic on every platform the lane runs on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="new-user friction: merge, render, file issues")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("merge", help="fold summary.json into the ledger; write friction.json")
    m.add_argument("--summary", type=Path, required=True)
    m.add_argument("--ledger", type=Path, help="previous night's ledger (optional)")
    m.add_argument("--out", type=Path, required=True, help="friction.json for this run")
    m.add_argument("--out-ledger", type=Path, required=True)
    m.add_argument("--date", required=True, help="YYYY-MM-DD of this run")
    m.add_argument("--run-url", default="")
    m.add_argument("--artifact-url", default="")
    m.add_argument("--head-sha", default="")

    r = sub.add_parser(
        "snapshot", help="rewrite friction.json (tonight's rows) from the ledger, e.g. after issues"
    )
    r.add_argument("--ledger", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True, help="the friction.json `merge` wrote")
    r.add_argument("--date", required=True)

    i = sub.add_parser("issues", help="open / comment GitHub issues for tonight's ledger rows")
    i.add_argument("--ledger", type=Path, required=True)
    i.add_argument("--repo", required=True)
    i.add_argument("--date", required=True)
    i.add_argument(
        "--artifact-url",
        default="",
        help="this run's artifact URL, stamped on tonight's rows so issue screenshot links resolve",
    )
    i.add_argument(
        "--friction",
        type=Path,
        help="this run's friction.json: only ITS rows (run_keys) get --artifact-url stamped",
    )
    i.add_argument("--dry-run", action="store_true")

    k = sub.add_parser(
        "pick-ledger",
        help="resolve the previous ledger: newest scheduled run of this workflow on the trusted branch",
    )
    k.add_argument("--repo", required=True)
    k.add_argument("--branch", required=True, help="the trusted branch (the default branch)")
    k.add_argument(
        "--self-run",
        type=int,
        default=None,
        help="this run's id: a prior attempt's ledger is preferred, the run is otherwise skipped",
    )
    k.add_argument(
        "--out", type=Path, required=True, help='where to write {"run_id": ..., "artifact_id": ...}'
    )

    args = p.parse_args(argv)
    try:
        if args.cmd == "merge":
            summary = json.loads(args.summary.read_text(encoding="utf-8"))
            ledger = load_ledger(args.ledger)
            entries = collect(summary)
            ledger, new_keys = merge_ledger(
                ledger,
                entries,
                date=args.date,
                run_url=args.run_url,
                artifact_url=args.artifact_url,
                head_sha=args.head_sha,
            )
            run_keys = [e["key"] for e in entries]
            rows = rows_for(ledger, run_keys)
            _write(
                args.out,
                {
                    "date": args.date,
                    "head_sha": args.head_sha,
                    "run_url": args.run_url,
                    "artifact_url": args.artifact_url,
                    "run_keys": run_keys,
                    "new_keys": new_keys,
                    "entries": rows,
                },
            )
            _write(args.out_ledger, ledger)
            print(
                f"{len(rows)} friction entr{'y' if len(rows) == 1 else 'ies'}, {len(new_keys)} new"
            )
        elif args.cmd == "snapshot":
            ledger = load_ledger(args.ledger)
            if not args.out.exists():
                raise FrictionError(f"{args.out}: snapshot needs the friction.json `merge` wrote")
            prior = json.loads(args.out.read_text(encoding="utf-8"))
            run_keys = [str(k) for k in prior.get("run_keys") or []]
            rows = rows_for(ledger, run_keys)
            _write(
                args.out,
                {
                    "date": args.date,
                    "head_sha": prior.get("head_sha", ""),
                    "run_url": prior.get("run_url", ""),
                    "artifact_url": prior.get("artifact_url", ""),
                    "run_keys": run_keys,
                    "new_keys": prior.get("new_keys", []),
                    "entries": rows,
                },
            )
            print(f"{len(rows)} friction entr{'y' if len(rows) == 1 else 'ies'} snapshotted")
        elif args.cmd == "pick-ledger":
            picked = pick_ledger(
                repo=args.repo,
                branch=args.branch,
                self_run_id=args.self_run,
                fetch=lambda path: json.loads(_gh(["api", f"repos/{args.repo}/{path}"])),
            )
            # To a file, not stdout: the workflow reads it with jq, and nothing
            # that came back from the API is echoed into the job log.
            _write(args.out, picked)
            print("no previous ledger" if picked["run_id"] is None else "previous ledger found")
        else:
            ledger = load_ledger(args.ledger)
            if args.artifact_url:
                # Only the rows THIS run observed: a re-run that did not see an
                # earlier attempt's row must not point that row's screenshot at
                # an artifact which does not hold it. Without the run's
                # friction.json nothing is stamped.
                if args.friction is None:
                    raise FrictionError("--artifact-url needs --friction (the run's own keys)")
                own = json.loads(args.friction.read_text(encoding="utf-8"))
                for key in own.get("run_keys") or []:
                    row = ledger["entries"].get(str(key))
                    if row is not None:
                        row["artifact_url"] = args.artifact_url
            if args.dry_run:
                print(json.dumps(plan_issues(ledger, date=args.date), indent=2))
                return 0
            _write(args.ledger, ledger)  # the artifact-url stamp, before any gh call
            result = file_issues(
                ledger,
                repo=args.repo,
                date=args.date,
                persist=lambda led: _write(args.ledger, led),
            )
            print(json.dumps(result))
            if result["failed"]:
                print(f"friction: {len(result['failed'])} gh call(s) failed", file=sys.stderr)
                return 2
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"friction: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
