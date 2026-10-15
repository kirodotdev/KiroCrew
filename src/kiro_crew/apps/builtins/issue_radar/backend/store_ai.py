"""What Issue Radar's AI features keep per repo.

The per-issue triage and per-PR summary caches, the label recommendations, the
tagging-suggestion queue and the investigation records, with the clock that stamps
them. Callers reach every name here through :mod:`kiro_crew.apps.builtins.issue_radar.backend.store`.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write

from .store_repos import repo_data_dir

# ── AI triage cache (summary + suggested labels) ─────────────────────────────
#
# The AI summary + suggested-labels for one issue are computed by a single LLM
# call and cached per issue (``issue-{n}-ai.json``), mirroring the cache-first
# philosophy of the issue/detail/labels caches: the (relatively expensive) model
# call is paid once per issue and served instantly on re-open; ``refresh=1`` on
# the route bypasses it, and a label edit drops it (the applied label changes
# what counts as "already on the issue", so suggestions must be recomputed).


def issue_ai_cache_path(
    owner: str, repo: str, number: int, root: Path | None = None, *, ui_language: str = ""
) -> Path:
    """Where one issue's AI triage result lives, PARTITIONED by output language.

    The summary and each suggested label's `reason` are prose written in one
    language, and the language can differ per-browser. One slot per issue would
    therefore thrash: two browsers resolving different languages would each read
    the other's entry as unusable, regenerate over it, and pay a model call on
    every single open, so the cache would stop working for the install entirely.
    A partition per language makes each browser's summary durable.

    ``""`` -- no language known, which is every install that never configured one
    -- keeps the ORIGINAL filename, so caches written before partitioning are
    still found and no migration is needed.

    The tag is safe in a filename by construction: it only ever arrives via
    :func:`kiro_crew.context.normalize_ui_language_tag`, which admits nothing
    outside a fixed set of shipped catalog names.
    """
    suffix = f"-{ui_language}" if ui_language else ""
    return repo_data_dir(owner, repo, root) / f"issue-{int(number)}-ai{suffix}.json"


def _cache_generated_at(data: dict, path: Path) -> str | None:
    """The stamped ``generated_at``, falling back to the file's mtime.

    Caches written before the field existed carry no stamp, and the UI would then
    show no age at all until the user manually regenerated. The mtime is when the
    cache was written, which IS when the summary was generated — so it is the
    right answer, not a guess.
    """
    stamped = data.get("generated_at")
    if isinstance(stamped, str) and stamped:
        return stamped
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return (
        datetime.fromtimestamp(mtime, timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def write_issue_ai_cache(
    owner: str,
    repo: str,
    number: int,
    payload: dict,
    *,
    root: Path | None = None,
    ui_language: str = "",
) -> None:
    """Cache one issue's AI triage result (``{summary, suggested_labels}``).

    Stamped with ``generated_at`` so the UI can show how old the summary is —
    without it a cached card gives no hint whether it was written minutes or
    months ago.

    ``ui_language`` selects the partition to write (see
    :func:`issue_ai_cache_path`); it is deliberately NOT also stored inside the
    document, because the path already carries it and a second copy could only
    ever disagree with it."""
    atomic_write(
        issue_ai_cache_path(owner, repo, number, root, ui_language=ui_language),
        json.dumps(
            {
                "owner": owner,
                "repo": repo,
                "number": int(number),
                "summary": payload.get("summary", ""),
                "suggested_labels": payload.get("suggested_labels", []),
                "generated_at": _now_iso(),
            },
            indent=2,
        ),
    )


def read_issue_ai_cache(
    owner: str, repo: str, number: int, root: Path | None = None, *, ui_language: str = ""
) -> dict | None:
    """Return ``{"summary", "suggested_labels", "generated_at"}``, or None.

    Reads only the partition for ``ui_language``, so a summary written in another
    language is a miss rather than something served with foreign prose -- and,
    unlike a shared slot, is left intact for the browser that generated it.
    Caches written before the ``generated_at`` stamp existed fall back to the
    file's mtime (see _cache_generated_at)."""
    path = issue_ai_cache_path(owner, repo, number, root, ui_language=ui_language)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "summary": data.get("summary", ""),
        "suggested_labels": data.get("suggested_labels", []),
        "generated_at": _cache_generated_at(data, path),
    }


def delete_issue_ai_cache(owner: str, repo: str, number: int, root: Path | None = None) -> None:
    """Drop a cached AI result (called after a label edit so it recomputes).

    Clears EVERY language partition, not just one. The applied label changes what
    counts as "already on the issue", so the suggestions are stale in whatever
    language they were written in -- dropping only the caller's language would
    leave another browser reading advice that predates the edit."""
    for path in issue_ai_cache_paths(owner, repo, number, root):
        path.unlink(missing_ok=True)


def issue_ai_cache_paths(
    owner: str, repo: str, number: int, root: Path | None = None
) -> list[Path]:
    """Every existing language partition of one issue's AI cache.

    Globs rather than iterating the shipped catalog set so a partition left behind
    by a language that has since been removed from the registry is still cleaned
    up instead of lingering forever.
    """
    unsuffixed = issue_ai_cache_path(owner, repo, number, root)
    found = [unsuffixed] if unsuffixed.is_file() else []
    # `issue-7-ai-*.json` cannot collide with another issue: the number is
    # followed by the literal `-ai`, so `issue-7-ai-de.json` matches and
    # `issue-70-ai.json` does not.
    found.extend(sorted(unsuffixed.parent.glob(f"issue-{int(number)}-ai-*.json")))
    return found


# ── PR AI summary cache ──────────────────────────────────────────────────────
#
# Unlike an issue's triage result, a PR summary goes stale on its own: it reads
# the description, EVERY comment/review, and the check state, all of which move
# while the PR is open. So the cache is keyed by a FINGERPRINT of those inputs
# (see routes._pr_ai_fingerprint) and a mismatch reads as a miss — a new comment
# or a flipped check silently earns a fresh summary, with no user action and no
# repeated model call while nothing has changed.


def pr_ai_cache_path(
    owner: str, repo: str, number: int, root: Path | None = None, *, ui_language: str = ""
) -> Path:
    """Where one PR's AI summary lives, PARTITIONED by output language.

    Same reasoning as :func:`issue_ai_cache_path`: the summary is prose in one
    language, the language can differ per-browser, and a single slot per PR would
    make two browsers overwrite each other's summary on every open. The
    fingerprint alone cannot solve this -- it decides whether the PR has MOVED,
    and one file holds one fingerprint, so the second browser's write evicts the
    first. ``""`` keeps the ORIGINAL filename so existing caches survive.
    """
    suffix = f"-{ui_language}" if ui_language else ""
    return repo_data_dir(owner, repo, root) / f"pull-{int(number)}-ai{suffix}.json"


def write_pr_ai_cache(
    owner: str,
    repo: str,
    number: int,
    payload: dict,
    *,
    root: Path | None = None,
    ui_language: str = "",
) -> None:
    """Cache one PR's AI summary together with the fingerprint it was built from."""
    atomic_write(
        pr_ai_cache_path(owner, repo, number, root, ui_language=ui_language),
        json.dumps(
            {
                "owner": owner,
                "repo": repo,
                "number": int(number),
                "summary": payload.get("summary", ""),
                "fingerprint": payload.get("fingerprint", ""),
                "generated_at": _now_iso(),
            },
            indent=2,
        ),
    )


def read_pr_ai_cache(
    owner: str,
    repo: str,
    number: int,
    root: Path | None = None,
    *,
    fingerprint: str | None = None,
    ui_language: str = "",
) -> dict | None:
    """Return ``{"summary", "generated_at"}`` for a cached PR summary, or None.

    A stored fingerprint that does not match ``fingerprint`` is a MISS: the PR has
    moved (new comment, new push, check flipped) since the summary was written.
    ``ui_language`` selects the partition, so another language's summary is absent
    here rather than evicted.
    """
    path = pr_ai_cache_path(owner, repo, number, root, ui_language=ui_language)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    # A syntactically VALID but non-object root (``[]``, a bare string) would blow
    # up on .get() and keep failing every request until the file is deleted by
    # hand. Treat it as a miss and let the route rewrite it.
    if not isinstance(data, dict):
        return None
    if fingerprint is not None and data.get("fingerprint") != fingerprint:
        return None
    return {"summary": data.get("summary", ""), "generated_at": _cache_generated_at(data, path)}


# ── AI label recommendations (per-repo taxonomy proposal) ────────────────────
#
# One cache per repo (NOT per issue): the "what NEW labels should this repo add"
# result computed over the repo's existing labels + a sample of open issues.
# Generated on explicit user action (the settings "Recommend labels" button),
# so it is cached until the user regenerates.


def recommendations_cache_path(
    owner: str, repo: str, root: Path | None = None, *, ui_language: str = ""
) -> Path:
    """Where a repo's label recommendations live, PARTITIONED by output language.

    Each recommendation carries `rationale` prose written in one language, so a set
    is only meaningful for the language it was generated in. Partitioning rather
    than gating one shared document matters because the language can differ
    per-browser: with a single slot, two browsers resolving different languages
    would each read the other's set as absent and each regenerate would overwrite
    it, silently discarding paid model output back and forth. Separate files remove
    the eviction outright, and switching a language back finds the earlier set
    still there instead of destroyed.

    ``""`` -- no language known, which is every install that never configured one
    -- keeps the ORIGINAL unsuffixed filename, so existing caches are still found
    after the upgrade and no migration is needed.

    The tag is safe in a filename by construction: it only ever arrives via
    :func:`kiro_crew.context.normalize_ui_language_tag`, which admits nothing
    outside a fixed set of shipped catalog names.
    """
    name = (
        "recommendations-cache.json"
        if not ui_language
        else (f"recommendations-cache-{ui_language}.json")
    )
    return repo_data_dir(owner, repo, root) / name


def write_recommendations_cache(
    owner: str, repo: str, payload: dict, *, root: Path | None = None, ui_language: str = ""
) -> None:
    """Cache a repo's AI label recommendations (``{recommendations, generated_at}``).

    ``ui_language`` selects the partition to write (see
    :func:`recommendations_cache_path`); it is deliberately NOT also stored inside
    the document, because the path already carries it and a second copy could only
    ever disagree with it."""
    atomic_write(
        recommendations_cache_path(owner, repo, root, ui_language=ui_language),
        json.dumps(
            {
                "owner": owner,
                "repo": repo,
                "recommendations": payload.get("recommendations", []),
                "generated_at": payload.get("generated_at", ""),
            },
            indent=2,
        ),
    )


def read_recommendations_cache(
    owner: str, repo: str, root: Path | None = None, *, ui_language: str = ""
) -> dict | None:
    """Return ``{"recommendations", "generated_at"}`` for a repo, or None.

    Reads only the partition for ``ui_language``, so a set generated in another
    language reads as absent rather than being served with foreign prose -- and,
    unlike a shared slot, is left intact for whoever generated it."""
    path = recommendations_cache_path(owner, repo, root, ui_language=ui_language)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "recommendations": data.get("recommendations", []),
        "generated_at": data.get("generated_at", ""),
    }


# ── tagging suggestions (which EXISTING labels an untagged issue should get) ──
#
# One cache per repo, keyed by issue number. Distinct from the per-issue AI cache
# (issue-ai-{n}.json), which holds a summary for ONE issue the user opened: this
# is the Tagging dashboard's queue, produced by analysing many untagged issues in
# a single batched model call.
#
# Written INCREMENTALLY. The dashboard analyses untagged issues in bounded
# batches, so each generate merges its batch into whatever is already cached
# rather than replacing the document, and applying a suggestion prunes just that
# issue's entry. Both are read-modify-write over the whole file, hence the lock.

TAGGING_CACHE_SCHEMA = 1


def tagging_cache_path(owner: str, repo: str, root: Path | None = None) -> Path:
    return repo_data_dir(owner, repo, root) / "tagging-cache.json"


@contextlib.contextmanager
def _tagging_cache_lock(owner: str, repo: str, root: Path | None = None):
    """Serialize the tagging cache's read-modify-write across threads AND
    processes. ``atomic_write`` prevents a torn file but not a lost update: a
    merge (generate) overlapping a prune (apply) would replace the whole document
    with its own stale copy. Same reasoning as :func:`_config_lock`."""
    path = tagging_cache_path(owner, repo, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".json.lock")
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            yield


def _normalize_tagging(raw: Any) -> dict[str, list[dict]]:
    """Coerce a suggestions map to ``{"<number>": [{name, reason}]}``.

    Deliberately tolerant: a partially-written or hand-edited document should
    degrade to "fewer suggestions", never break the dashboard. Keys are
    normalized through ``int`` so ``"12"`` and ``12`` can't both be present."""
    out: dict[str, list[dict]] = {}
    if not isinstance(raw, dict):
        return out
    for key, items in raw.items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        if number <= 0 or not isinstance(items, list):
            continue
        rows: list[dict] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            rows.append({"name": name, "reason": str(item.get("reason") or "").strip()})
        out[str(number)] = rows
    return out


def read_tagging_cache(owner: str, repo: str, root: Path | None = None) -> dict | None:
    """Return ``{"suggestions", "generated_at", "ui_language"}`` for a repo, or None
    when nothing has been generated yet (an unreadable / stale-schema file is a
    miss, same guard as the other caches).

    ``ui_language`` is the BCP-47 tag the cached ``reason`` text was WRITTEN in, so
    a caller can tell a servable entry from one that predates a language switch.
    Absent reads as ``""`` — identical to the unconfigured sentinel — which is why
    adding it did not need a schema bump: a document written before this field is
    still valid, and an install that never set a language keeps every cached
    suggestion across the upgrade. Bumping the schema would have discarded them
    all, which is a worse trade for a field whose absence is meaningful."""
    path = tagging_cache_path(owner, repo, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("schema") != TAGGING_CACHE_SCHEMA:
        return None
    return {
        "suggestions": _normalize_tagging(data.get("suggestions")),
        "generated_at": str(data.get("generated_at") or ""),
        "ui_language": str(data.get("ui_language") or ""),
    }


def _write_tagging_cache_unlocked(
    owner: str,
    repo: str,
    suggestions: dict[str, list[dict]],
    generated_at: str,
    root: Path | None = None,
    ui_language: str = "",
) -> None:
    atomic_write(
        tagging_cache_path(owner, repo, root),
        json.dumps(
            {
                "schema": TAGGING_CACHE_SCHEMA,
                "owner": owner,
                "repo": repo,
                "suggestions": suggestions,
                "generated_at": generated_at,
                "ui_language": ui_language,
            },
            indent=2,
        ),
    )


def merge_tagging_suggestions(
    owner: str,
    repo: str,
    batch: dict[str, list[dict]],
    *,
    verify_language: Callable[[], str],
    root: Path | None = None,
    ui_language: str = "",
) -> dict:
    """Merge one generated batch into the repo's cached suggestions; return the
    merged document, or the untouched one when the batch is refused.

    The batch WINS for the issues it covers — a regenerate must replace a stale
    proposal — while every issue outside the batch keeps its existing entry, so
    analysing the queue in slices accumulates instead of overwriting.

    EXCEPT across a language change. Each cached entry carries `reason` prose, so
    accumulating a batch generated in one language on top of entries generated in
    another would leave the queue showing two languages at once, with nothing to
    say which rows are which. When the stored tag differs from ``ui_language`` the
    surviving entries are dropped and the batch starts a fresh document: a
    regenerate the user can trigger is a better state than a permanently mixed
    one. Same reasoning as the issue-ai cache, which refuses a hit whose tag no
    longer matches.

    ``verify_language`` is what makes that safe under concurrency, and it has to
    be checked HERE rather than by the caller. The caller's own pre-write check
    and this write would not be atomic, so a switch landing between them lets a
    stale generation replace a newer-language one that already landed — the replace
    semantics above turn a lost race into lost data. Re-reading the configured
    language inside the lock closes that: the check and the write it authorises
    cannot be separated. REQUIRED, not optional: there is one caller and it always
    has a language to verify against, so a default would only offer a way to write
    without the guard that makes writing safe. Refusing returns the CURRENT
    document with ``stale_language`` set, so the caller can report that nothing was
    written rather than claiming a batch it did not persist. The stored language is
    deliberately NOT echoed back: no caller reads it, and the one place that needs
    it -- the two tagging routes' servable-cache gate -- reads it from
    :func:`read_tagging_cache`, which is where it lives.

    Deliberately a callable rather than a compare-and-set on the stored tag: a CAS
    would also refuse the ordinary case of two same-language batches accumulating
    slice by slice, where the second read the document before the first wrote. What
    makes a write valid is that the CONFIGURED language is still the one the batch
    was generated under, not what other writers did in the meantime."""
    with _tagging_cache_lock(owner, repo, root):
        if verify_language() != ui_language:
            current = read_tagging_cache(owner, repo, root)
            return {
                "suggestions": (current or {}).get("suggestions") or {},
                "generated_at": (current or {}).get("generated_at") or "",
                "stale_language": True,
            }
        current = read_tagging_cache(owner, repo, root)
        if current is not None and str(current.get("ui_language") or "") == ui_language:
            merged = dict(current["suggestions"])
        else:
            merged = {}
        merged.update(_normalize_tagging(batch))
        generated_at = _now_iso()
        _write_tagging_cache_unlocked(
            owner, repo, merged, generated_at, root, ui_language=ui_language
        )
    return {"suggestions": merged, "generated_at": generated_at, "stale_language": False}


def drop_tagging_suggestions(
    owner: str, repo: str, numbers: list[int], *, root: Path | None = None
) -> dict:
    """Forget the cached suggestions for ``numbers`` (applied or dismissed) and
    return what remains. No-op when nothing is cached."""
    with _tagging_cache_lock(owner, repo, root):
        current = read_tagging_cache(owner, repo, root)
        if current is None:
            return {"suggestions": {}, "generated_at": ""}
        drop = {int(n) for n in numbers}
        remaining = {k: v for k, v in current["suggestions"].items() if int(k) not in drop}
        # Carries the stored tag through: this rewrites the document, and dropping
        # the tag here would make every surviving entry read as "generated with no
        # language configured" and become servable again after a switch.
        _write_tagging_cache_unlocked(
            owner,
            repo,
            remaining,
            current["generated_at"],
            root,
            ui_language=str(current.get("ui_language") or ""),
        )
        return {"suggestions": remaining, "generated_at": current["generated_at"]}


# ── investigation records (the "Investigate" button) ─────────────────────────
#
# Clicking "Investigate" on an issue opens a Kiro Crew chat session, seeds it
# with an investigation prompt, and files it into a per-repo "Issue Radar -
# <repo>" chat folder. There is NO shared, git-backed,
# CLI-driven ledger; instead each investigated issue gets ONE small local
# record, keyed by number like the detail/AI caches, so Issue Radar can:
#   * RESUME the same session on a repeat click (via ``slot_key``) instead of
#     spawning a duplicate;
#   * badge the issue's investigation ``status``;
#   * retain ``findings`` the investigating agent (or the user) writes back.
# The record lives under the repo's cache dir, so ``remove_connected_repo``'s
# ``rmtree`` cleans it up on disconnect — nothing on GitHub is ever touched.

_INVESTIGATION_STATUSES = ("investigating", "resolved", "archived")


def _now_iso() -> str:
    """UTC timestamp, microsecond precision, ``Z`` suffix — stable, sortable, and
    fine-grained enough that rapid successive writes order deterministically
    when investigation records are sorted on ``last_opened_at``."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# Public alias: the routes layer stamps freshly-computed AI results with the same
# clock the caches use, so a "generated N minutes ago" label reads identically
# whether the response came from cache or was just computed.
now_iso = _now_iso


def investigation_path(
    owner: str, repo: str, number: int, root: Path | None = None, *, kind: str = "issue"
) -> Path:
    """Path of one item's investigation record.

    ``kind`` is the record's NAMESPACE, not the item's type, and the caller is
    responsible for having folded it (see ``provider.investigation_kind``). It
    exists because a number does not always identify an item on its own: GitHub
    draws issues and pull requests from ONE sequence, so ``#5`` is unique, while
    GitLab keeps two -- issue ``#5`` and merge request ``!5`` are unrelated items.
    Sharing one file between them would make "Review MR !5" resume issue #5's
    session and overwrite its findings.

    ``"issue"`` deliberately keeps the historical filename, so every existing
    record (all of which are GitHub's, where the namespace is shared) is found
    exactly where it was written and nothing needs migrating.
    """
    suffix = "" if kind == "issue" else f"{kind}-"
    return repo_data_dir(owner, repo, root) / f"investigation-{suffix}{int(number)}.json"


def read_investigation(
    owner: str, repo: str, number: int, root: Path | None = None, *, kind: str = "issue"
) -> dict | None:
    """Return an item's investigation record, or None if never investigated."""
    path = investigation_path(owner, repo, number, root, kind=kind)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _normalize_findings(raw: Any) -> dict[str, Any] | None:
    """Coerce a (client- or agent-supplied) findings blob into the known schema,
    or None. Strings are trimmed; ``suggested_labels`` is a de-duplicated string
    list; unknown keys are dropped. An all-empty result collapses to None so
    "no findings yet" stays null rather than a hollow object."""
    if not isinstance(raw, dict):
        return None

    def _s(key: str) -> str | None:
        v = raw.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None

    labels: list[str] = []
    labels_raw = raw.get("suggested_labels")
    if isinstance(labels_raw, list):
        seen: set[str] = set()
        for item in labels_raw:
            if isinstance(item, str) and item.strip() and item.strip() not in seen:
                seen.add(item.strip())
                labels.append(item.strip())

    findings = {
        "verdict": _s("verdict"),
        "root_cause": _s("root_cause"),
        "suggested_labels": labels,
        "next_action": _s("next_action"),
        "summary": _s("summary"),
    }
    if not any(findings.values()):
        return None
    return findings


def _merge_findings(existing: Any, raw: Any) -> dict[str, Any] | None:
    """Merge a (possibly partial) findings patch over the stored findings.

    The record's other fields merge per-field, and ``findings`` must behave the
    same way: replacing the whole object meant a second write that carried only
    a ``verdict`` silently DESTROYED the ``root_cause``/``summary``/labels an
    earlier write had stored. That is reachable from the
    ``issue_radar_record_investigation`` MCP tool, whose whole contract is "a
    partial update is fine", and the loss is permanent (the record is the only
    copy).

    Semantics, so callers can rely on them:

    * ``raw is None`` — explicit null CLEARS the whole findings object. This is
      the UI's clear path (``putInvestigation`` types findings as
      ``Partial<InvestigationFindings> | null``).
    * ``raw`` is a dict — each supplied, non-empty value overrides its key;
      omitted or empty values keep what is stored. ``suggested_labels`` is a
      whole value: a non-empty list replaces the stored list, an empty or
      absent one keeps it (a recommendation set is not additive). An empty dict
      is therefore a no-op, not a wipe.
    * ``raw`` is neither — malformed input keeps the stored findings rather than
      destroying them; the route and the tool schema both reject it upstream.

    There is deliberately NO per-field clear: an empty string means "leave this
    alone", which is what makes a partial patch safe for an LLM writer. Clear
    everything with an explicit null and re-write what should remain.

    All of the above is the contract for writes WITHIN one investigation run.
    Across a run boundary it is the wrong contract, and
    :func:`write_investigation` decides that question before calling here — see
    :func:`_findings_run_key`.
    """
    if raw is None:
        return None
    base = _normalize_findings(existing) or {}
    if not isinstance(raw, dict):
        return _normalize_findings(base)
    combined: dict[str, Any] = dict(base)
    for key in ("verdict", "root_cause", "next_action", "summary"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            combined[key] = value
    labels = raw.get("suggested_labels")
    if isinstance(labels, list) and any(isinstance(x, str) and x.strip() for x in labels):
        combined["suggested_labels"] = labels
    # Re-normalize so the merged object gets the same trimming / de-duplication /
    # unknown-key drop / all-empty-collapses-to-None treatment as a fresh write.
    return _normalize_findings(combined)


def _findings_run_key(existing: dict[str, Any]) -> str | None:
    """Which run's session the STORED findings were written under, or None when
    that is not known.

    ``findings_slot_key`` is stamped by :func:`write_investigation` on every
    write, so a record written by this build always answers for itself. A record
    written BEFORE the field existed carries findings but no stamp, and the
    honest backfill is the record's own ``slot_key``: those findings were
    written by whatever session the record pointed at at the time. That reading
    keeps a mid-run partial update on an upgraded record merging exactly as it
    did before, and still moves the boundary when the slot later changes,
    because this is read from the PRE-patch record — before a new ``slot_key``
    overwrites the old one.
    """
    raw = (
        existing.get("findings_slot_key")
        if "findings_slot_key" in existing
        else existing.get("slot_key")
    )
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def write_investigation(
    owner: str,
    repo: str,
    number: int,
    patch: dict[str, Any],
    *,
    root: Path | None = None,
    kind: str = "issue",
) -> dict[str, Any]:
    """Upsert an issue's investigation record, MERGING ``patch`` into any
    existing record (last-writer-wins per field). ``started_at`` is stamped once
    on first create; ``last_opened_at`` is refreshed on every write. Only known,
    validated fields are applied — ``slot_key``/``folder_id`` (strings; ``""``
    clears to None), ``status`` (one of ``_INVESTIGATION_STATUSES``), and
    ``findings`` (merged per key by :func:`_merge_findings`, so a patch carrying
    only ``verdict`` keeps the stored ``root_cause``/``summary``/labels; an
    explicit ``None`` clears them). A partial patch (even ``{}``, which just bumps
    the open stamp) is valid. Returns the stored record.

    Per-key merging is right WITHIN one investigation run and wrong ACROSS runs:
    a re-run (Issue Radar's "Start over", or any path that opens a replacement
    session) that records a ``verdict`` and ``summary`` but no ``root_cause``
    would inherit the PREVIOUS run's ``root_cause``, leaving the record — the
    only copy — holding a verdict assembled from two investigations with nothing
    marking which parts came from which.

    So the run boundary is owned HERE rather than by callers, because only here
    is it atomic with the write. The record remembers the session its findings
    were written under (``findings_slot_key``), and the FIRST findings write
    under a different session REPLACES rather than merges; later writes from that
    same session merge as before. The previous verdict therefore survives right
    up until a new one exists and never blends with it — which is why the clear
    is not done when the replacement session opens (the record is the only copy,
    so an abandoned re-run would lose the prior verdict permanently).

    A boundary is only crossed when BOTH sessions are known and differ. An
    unknown owner (findings recorded through the MCP tool for an item with no
    session linked) falls back to merging: the store cannot tell those findings
    from ones the about-to-be-linked session just wrote, and merging is the
    non-destructive reading. Clearing the link (``slot_key: ""``) is not a new
    run either."""
    number = int(number)
    now = _now_iso()
    lock_path = investigation_path(owner, repo, number, root, kind=kind).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as fd:
        with platform_compat.file_lock(fd.fileno(), exclusive=True):
            existing = read_investigation(owner, repo, number, root, kind=kind) or {}
            # Read the findings' owning session from the PRE-patch record: the
            # patch below may replace slot_key, and the comparison needs the old
            # one.
            findings_run_key = _findings_run_key(existing)

            record: dict[str, Any] = {
                "owner": owner,
                "repo": repo,
                "number": number,
                "slot_key": existing.get("slot_key"),
                "folder_id": existing.get("folder_id"),
                "status": (
                    existing.get("status")
                    if existing.get("status") in _INVESTIGATION_STATUSES
                    else "investigating"
                ),
                "started_at": existing.get("started_at") or now,
                "last_opened_at": now,
                "findings": existing.get("findings"),
                # Only meaningful while findings are stored; re-stamped below
                # whenever they change.
                "findings_slot_key": findings_run_key if existing.get("findings") else None,
            }

            if "slot_key" in patch and isinstance(patch["slot_key"], str):
                record["slot_key"] = patch["slot_key"].strip() or None
            if "folder_id" in patch and isinstance(patch["folder_id"], str):
                record["folder_id"] = patch["folder_id"].strip() or None
            if "status" in patch:
                st = str(patch.get("status") or "").strip().lower()
                if st in _INVESTIGATION_STATUSES:
                    record["status"] = st
            if "findings" in patch:
                # Runs AFTER slot_key is applied: a patch may carry both, and the
                # session it names is the run this write belongs to.
                raw = patch.get("findings")
                # None (the explicit clear), a non-dict, and a dict with nothing
                # in it all normalize to None — so this is "the patch carries a
                # real verdict", which is the only thing that may cross a run
                # boundary.
                fresh = _normalize_findings(raw)
                crossed_runs = bool(
                    findings_run_key
                    and record["slot_key"]
                    and findings_run_key != record["slot_key"]
                )
                if fresh is not None:
                    if crossed_runs:
                        # First findings of a new run: REPLACE. Nothing from the
                        # previous run survives into the new verdict.
                        record["findings"] = fresh
                    else:
                        record["findings"] = _merge_findings(existing.get("findings"), raw)
                    # A real verdict was written, so the current session owns it.
                    # The merge above always keeps `fresh`, so this is never a
                    # stamp on an empty findings object.
                    record["findings_slot_key"] = record["slot_key"]
                elif raw is None:
                    # The explicit clear. No findings, so no owning run.
                    record["findings"] = None
                    record["findings_slot_key"] = None
                # else: nothing to write — an empty dict, a whitespace-only value,
                # or a malformed one. Findings AND their owning run are both left
                # exactly as the pre-patch record had them. Advancing the stamp
                # here would silently re-home the PREVIOUS run's findings onto the
                # new session, so the next real write would merge into them
                # instead of replacing them — the very blend this boundary
                # exists to stop, reachable through a no-op.

            atomic_write(
                investigation_path(owner, repo, number, root, kind=kind),
                json.dumps(record, indent=2),
            )
    return record
