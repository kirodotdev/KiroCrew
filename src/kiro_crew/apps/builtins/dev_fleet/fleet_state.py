"""PR, context, resource, and fleet-state projections for Dev Fleet."""

from __future__ import annotations

import asyncio
import functools
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.dev_fleet import live, repository, runtime
from kiro_crew.executors import subprocess_executor
from kiro_crew.loop_lock import LoopBoundLock

# --- build-pending detection (server-side truth) ---
_START_EPOCH = time.time()


def _build_pending() -> bool:
    """True when MAIN_REPO's built SPA dist is NEWER than this process's start.

    The dist inspected is the one Pull+Build actually writes — the MAIN CHECKOUT's
    — not the dist of the installation this backend happens to be running from.
    Those coincide only when the gateway runs from that same source tree. With the
    packaged desktop app the running installation's dist lives inside the .app
    bundle and its mtime never changes, so the previous ``parents[3]`` lookup
    could never fire: a completed Pull+Build reported nothing pending and the
    dashboard never told the user there was a build to apply.
    """
    try:
        # Path("") is Path("."), which would stat this process's own tree —
        # no checkout means nothing can be pending.
        dist = Path(repository._repo()) / "src" / "kiro_crew" / "static" / "dist"
        if not dist.exists():
            return False
        # stat() follows a symlink on purpose: a source-tree install points
        # static/dist at website/dist, and the rebuild time we care about is the
        # target's.
        return dist.stat().st_mtime > _START_EPOCH
    except (OSError, repository.RepoUnavailable):
        return False


# --- GitHub PR status (TTL-cached, best-effort) ---
_PR_CACHE: dict[str, dict] = {}
_PR_TTL = 55

_OWNER_REPO: str | None = None
_OWNER_REPO_RETRY_AT: float = 0.0  # monotonic deadline before retrying a failed lookup


async def _repo_owner_name() -> str | None:
    """Derive owner/repo from the upstream remote URL."""
    remote = await repository._upstream_remote()
    try:
        repo = repository._repo()
    except repository.RepoUnavailable:
        # Best-effort helper: an unresolved checkout is "not derivable", the
        # same answer every other failure below produces. Without the accessor
        # this ran `git -C ""` and could report whatever repository the
        # backend's working directory happened to be.
        return None
    rc, stdout, _ = await runtime._run_cmd(
        ["git", "-C", repo, "remote", "get-url", remote], timeout=5
    )
    if rc != 0:
        return None
    url = stdout.strip()
    m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


async def _get_owner_repo() -> str | None:
    """Resolve owner/repo once. Only SUCCESS is cached permanently; a failed
    lookup (transient network/gh error) is retried after a short TTL so PR
    status and merged-worktree pruning recover without a gateway restart."""
    global _OWNER_REPO, _OWNER_REPO_RETRY_AT
    if _OWNER_REPO:
        return _OWNER_REPO
    now = time.monotonic()
    if now < _OWNER_REPO_RETRY_AT:
        return None
    val = await _repo_owner_name()
    if val:
        _OWNER_REPO = val
        return val
    _OWNER_REPO_RETRY_AT = now + 60.0
    return None


async def _pr_query_one(owner_repo: str, branch: str) -> dict | None:
    # `title` is a display field carried into the payload; `body` is fetched in
    # the SAME query (no extra gh call, so no added rate cost per refresh) and
    # kept INTERNAL (moved to `_body`) — it feeds issue-ref parsing but is
    # dropped from the payload by _redact_pr (which skips `_`-prefixed keys).
    rc, stdout, _ = await runtime._run_cmd(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            owner_repo,
            "--head",
            branch,
            "--json",
            "number,state,url,isDraft,title,body,headRefOid",
            "--state",
            "all",
            "--limit",
            "1",
        ],
        timeout=15,
    )
    if rc != 0:
        return None
    try:
        prs = json.loads(stdout)
        pr = prs[0] if prs else None
    except (json.JSONDecodeError, IndexError):
        return None
    if pr is not None:
        pr["_repo"] = owner_repo
        pr["_head_oid"] = pr.pop("headRefOid", None)
        if "body" in pr:
            pr["_body"] = pr.pop("body") or ""
    return pr


async def _fetch_pr_status(branch: str) -> dict | None:
    """Query GitHub PR via gh CLI: upstream repo first, then ancestor-verified
    legacy-remote repos (pre-rename PRs stay visible and prunable)."""
    owner_repo = await _get_owner_repo()
    if not owner_repo or not branch:
        return None
    pr = await _pr_query_one(owner_repo, branch)
    if pr is not None:
        return pr
    for repo in repository._FALLBACK_REPOS or []:
        pr = await _pr_query_one(repo, branch)
        if pr is not None:
            return pr
    return None


async def _head_contained_in_pr(path: str, branch_oid: str, pr_head_oid: str) -> bool:
    """True when the worktree HEAD is the PR head or an ANCESTOR of it.

    A merged PR whose head gained remote-side commits before merge leaves the
    local branch strictly BEHIND the PR head — all local content is contained
    in the merge, so removal is safe. Only commits the PR head does NOT
    contain (local HEAD not an ancestor) are unmerged work.
    """
    if branch_oid.strip() == pr_head_oid.strip():
        return True
    rc, _, _err = await runtime._run_cmd(
        ["git", "-C", path, "merge-base", "--is-ancestor", branch_oid.strip(), pr_head_oid.strip()],
        timeout=10,
    )
    return rc == 0


async def _fetch_pr_head_oid(branch: str, repo: str | None = None) -> str | None:
    """Fetch the headRefOid of the PR for *branch* — FRESH and MERGED-gated.

    Destructive callers (prune/removal) rely on this as the authoritative
    check: the state and head OID come from the SAME live response, and a
    non-MERGED state returns None. A stale cached MERGED verdict for a
    reused branch name can therefore never authorize removing the new
    branch's worktree — the fresh state here is OPEN and we refuse.
    """
    owner_repo = repo or await _get_owner_repo()
    if not owner_repo or not branch:
        return None
    rc, stdout, _ = await runtime._run_cmd(
        ["gh", "pr", "view", branch, "--repo", owner_repo, "--json", "headRefOid,state"],
        timeout=15,
    )
    if rc != 0:
        return None
    try:
        data = json.loads(stdout)
        if data.get("state") != "MERGED":
            return None
        return data.get("headRefOid")
    except ValueError:
        return None


async def _pr_status_cached(branch: str, head_oid: str | None = None) -> dict | None:
    """Return cached PR status for a branch.

    *head_oid* is the full current worktree HEAD commit.  When provided and
    the cached entry records a MERGED verdict whose stored head OID
    differs from *head_oid*, the entry is treated as stale and a fresh lookup is
    performed.  This prevents a permanently-cached MERGED result from surviving
    a branch name being reused for a new head commit.  Callers that do not have
    the head OID readily available may omit *head_oid*; the cache then degrades
    to the previous behaviour (MERGED is terminal, non-MERGED expires via TTL).
    """
    if not branch or branch == repository.BASE_BRANCH:
        return None
    now = time.time()
    ent = _PR_CACHE.get(branch)
    if ent:
        # Only MERGED is permanently terminal — a CLOSED PR can be reopened,
        # so its cache entry must expire via the normal TTL.
        is_terminal = (ent.get("data") or {}).get("state") == "MERGED"
        if is_terminal:
            # Invalidate a MERGED entry when the caller supplies a head OID
            # that differs from the one recorded at cache-write time.  A changed
            # head means the branch was reused for new work; the old MERGED
            # verdict no longer describes the current commits.
            cached_head = ent.get("cached_head")
            if head_oid and cached_head != head_oid:
                # Head changed (or entry was written without a head OID) —
                # fall through to a fresh fetch below.
                pass
            else:
                return ent.get("data")
        elif (now - ent["ts"]) < _PR_TTL:
            return ent.get("data")
    data = await _fetch_pr_status(branch)
    if data and data.get("state") == "MERGED" and head_oid and data.get("_head_oid") != head_oid:
        # GitHub may return the old merged PR when a branch name is reused
        # before a replacement PR exists. A local head contained in the PR
        # head is still fully shipped (for example, remote commits landed
        # before merge); only a divergent head means this verdict is stale.
        pr_head_oid = data.get("_head_oid")
        if not pr_head_oid or not await _head_contained_in_pr(
            repository._repo(), head_oid, pr_head_oid
        ):
            data = None
    _PR_CACHE[branch] = {"data": data, "ts": time.time(), "cached_head": head_oid}
    return data


def _is_pr_merged(pr: dict | None) -> bool:
    return (pr or {}).get("state") == "MERGED"


def _is_pr_closed(pr: dict | None) -> bool:
    """True when the PR was CLOSED without merging.

    Distinct from ``_is_pr_merged``: a merged PR's content is on the base branch
    by definition, so its worktree is safe to delete. A closed-unmerged PR was
    declined or superseded, and its worktree can still hold the only copy of
    work that never landed — so a closed worktree is prunable ONLY through the
    manual path, with a dirty-tree refusal and a loss summary. GitHub reports a
    merged PR as ``MERGED`` (never ``CLOSED``), so this check is unambiguous.
    """
    return (pr or {}).get("state") == "CLOSED"


# --- per-worktree context: issue/ticket links + purpose one-liner ---
#
# Best-effort and TTL-cached per branch exactly like the PR-state cache. Every
# field degrades to empty/None on any git/gh failure so context resolution can
# never break the fleet payload.

# GitHub issue references: keyworded (Fixes/Closes/Resolves #N) OR bare #N. The
# bare form is a superset, so a single "#<digits>" match — guarded by a
# trailing non-alphanumeric lookahead that rejects colour hexes (#1a2b, #fff)
# and version-ish tokens — covers both; dedup collapses the keyworded overlap.
_ISSUE_REF_RE = re.compile(r"#(\d{1,7})(?![0-9A-Za-z])")
# Ticket IDs (JIRA / Taskei style): PROJECT-1234.
_TICKET_ID_RE = re.compile(r"\b[A-Z][A-Za-z]{1,15}-\d{1,6}\b")
# Subjects that are pure version bumps — skipped when picking the one-liner.
_VERSION_BUMP_RE = re.compile(
    r"^\s*(?:chore(?:\([^)]*\))?:\s*)?"
    r"(?:bump\b|release\b|bump version\b|version bump\b|v?\d+\.\d+\.\d+\s*$)",
    re.IGNORECASE,
)
# Payload growth caps (keep fleet rows modest).
_CTX_MAX_ISSUES = 8
_CTX_MAX_TICKETS = 5


def _extract_issue_refs(text: str) -> list[int]:
    """Ordered-unique GitHub issue numbers referenced in *text*."""
    seen: list[int] = []
    for m in _ISSUE_REF_RE.finditer(text or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def _extract_ticket_ids(text: str) -> list[str]:
    """Ordered-unique ticket IDs (PROJECT-1234) referenced in *text*."""
    seen: list[str] = []
    for m in _TICKET_ID_RE.finditer(text or ""):
        t = m.group(0)
        if t not in seen:
            seen.append(t)
    return seen


def _render_ticket_url(template: str, tid: str) -> str | None:
    """Render a ticket URL from *template* ({id} placeholder). Returns None
    when the template is empty or has no {id} — chips then render unlinked."""
    if not template or "{id}" not in template:
        return None
    return template.replace("{id}", tid)


def _is_version_bump(subject: str) -> bool:
    return bool(_VERSION_BUMP_RE.match(subject or ""))


def _pick_summary(subjects: list[str]) -> str | None:
    """Latest non-merge commit subject, skipping trivial version bumps; falls
    back to the latest subject when every candidate is a bump."""
    for s in subjects:
        if s and not _is_version_bump(s):
            return s
    return subjects[0] if subjects else None


def _issue_url(base: str | None, n: int) -> str | None:
    return f"{base}/issues/{n}" if base else None


def _parse_html_repo_base(remote_url: str) -> str | None:
    """Derive the repo's browser (html) base URL from a git remote URL,
    normalising scp-style and scheme URLs to https. None when unparseable."""
    url = (remote_url or "").strip()
    if not url:
        return None
    # scp-like: [user@]host:owner/repo(.git)  — (?!/) rejects a scheme "://".
    m = re.match(r"^(?:[^@/]+@)?([\w.\-]+):(?!/)(.+?)(?:\.git)?/?$", url)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    # scheme://[user@]host[:port]/owner/repo(.git)
    m = re.match(
        r"^[A-Za-z][\w+.\-]*://(?:[^@/]+@)?([\w.\-]+)(?::\d+)?/(.+?)(?:\.git)?/?$",
        url,
    )
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return None


_HTML_BASE: str | None = None


async def _html_repo_base() -> str | None:
    """Cached browser base URL of the upstream repo (for issue links). Derived
    from the remote URL; falls back to github.com/<owner/repo> (PR resolution
    is gh/github-only anyway)."""
    global _HTML_BASE
    if _HTML_BASE:
        return _HTML_BASE
    remote = await repository._upstream_remote()
    try:
        rc, out, _ = await runtime._run_cmd(
            ["git", "-C", repository._repo(), "remote", "get-url", remote], timeout=5
        )
    except repository.RepoUnavailable:
        # Same degrade as a failed get-url: fall through to the cached
        # owner/repo lookup (which answers None in this state too).
        rc, out = 1, ""
    if rc == 0:
        base = _parse_html_repo_base(out.strip())
        if base:
            _HTML_BASE = base
            return base
    owner_repo = await _get_owner_repo()
    if owner_repo:
        _HTML_BASE = f"https://github.com/{owner_repo}"
    return _HTML_BASE


# per-branch context cache (mirrors _PR_CACHE / _PR_TTL)
_CTX_CACHE: dict[str, dict] = {}
_CTX_TTL = 55


async def _resolve_context(
    branch: str | None, subjects: list[str], bodies: list[str], pr_body: str | None
) -> dict:
    """Assemble {issues, tickets, summary} from pre-extracted text. The html
    base is resolved ONLY when issue refs exist and the ticket template ONLY
    when ticket IDs exist — so a refless worktree triggers no extra lookup (and
    no owner/repo cache write), keeping callers side-effect-free."""
    issue_nums = _extract_issue_refs("\n".join([pr_body or "", *subjects, *bodies]))
    ticket_ids = _extract_ticket_ids("\n".join([branch or "", *subjects]))
    html_base = await _html_repo_base() if issue_nums else None
    tpl = (
        str(repository._load_dev_fleet_cfg().get("ticket_url_template") or "") if ticket_ids else ""
    )
    summary = _pick_summary(subjects)
    return {
        "issues": [
            {"number": n, "url": _issue_url(html_base, n)} for n in issue_nums[:_CTX_MAX_ISSUES]
        ],
        "tickets": [
            {"id": t, "url": _render_ticket_url(tpl, t)} for t in ticket_ids[:_CTX_MAX_TICKETS]
        ],
        "summary": runtime._redact(summary) if summary else None,
    }


async def _build_context(branch: str, path: str, pr: dict | None) -> dict:
    """Resolve {issues, tickets, summary} for a worktree branch. Best-effort:
    the PR body comes from the already-cached PR dict (no NEW gh call) and the
    commit subjects/bodies from a local `git log`; any failure yields empties."""
    remote = await repository._upstream_remote()
    subjects: list[str] = []
    bodies: list[str] = []
    # Subject + body of the last ~10 non-merge commits, record-separated by
    # 0x1e (bodies contain newlines, so newline can't delimit records).
    log = await repository._git(
        path,
        "log",
        f"{remote}/{repository.BASE_BRANCH}..HEAD",
        "--no-merges",
        "-10",
        "--format=%s%x1f%b%x1e",
        timeout=12,
    )
    if log:
        for rec in log.split("\x1e"):
            rec = rec.strip("\n")
            if not rec.strip():
                continue
            subj, _sep, body = rec.partition("\x1f")
            subj = subj.strip()
            if subj:
                subjects.append(subj)
            if body.strip():
                bodies.append(body)
    return await _resolve_context(branch, subjects, bodies, (pr or {}).get("_body"))


async def _context_cached(branch: str | None, path: str, pr: dict | None) -> dict:
    """TTL-cached per-branch context (same approach as _pr_status_cached)."""
    empty: dict = {"issues": [], "tickets": [], "summary": None}
    if not branch or branch == repository.BASE_BRANCH:
        return empty
    now = time.time()
    ent = _CTX_CACHE.get(branch)
    if ent and (now - ent["ts"]) < _CTX_TTL:
        return ent["data"]
    try:
        data = await _build_context(branch, path, pr)
    except Exception:  # noqa: BLE001 — context is best-effort, never break fleet
        data = empty
    _CTX_CACHE[branch] = {"data": data, "ts": time.time()}
    return data


# --- fleet cache ---
_FLEET_TTL = 10.0
_FLEET_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
# The single in-flight rebuild. A rebuild costs one `gh pr` round-trip per
# branch, so the background revalidate and every `fresh=1` request coalesce onto
# the SAME task instead of each starting their own. Holding the reference here
# also keeps a fire-and-forget rebuild from being garbage-collected mid-flight.
_FLEET_INFLIGHT: asyncio.Task[dict] | None = None
# Worktrees evicted by `_fleet_forget`, keyed to an eviction counter. A rebuild
# that started BEFORE an eviction still reads the worktree from git, so storing
# its result would resurrect the row the eviction just dropped. Entries are
# reaped by the first build that started after them.
_FLEET_EPOCH = 0
_FLEET_TOMBSTONES: dict[str, int] = {}


def _drop_worktrees(data: dict, names: set[str]) -> dict:
    """A copy of ``data`` without the named worktrees.

    Copies rather than mutates: a concurrent response may still hold the old
    dict while aiohttp serializes it.
    """
    wts = data.get("worktrees")
    if not isinstance(wts, list):
        return data
    kept = [w for w in wts if w.get("name") not in names]
    if len(kept) == len(wts):
        return data
    return {**data, "worktrees": kept}


async def _fleet_build() -> dict:
    global _FLEET_TOMBSTONES
    started = _FLEET_EPOCH
    data = await _build_fleet()
    # Removals that landed DURING this build are invisible to it — the git state
    # it read predates them. Re-apply them so a slow build cannot put back a row
    # an eviction already removed.
    data = _drop_worktrees(data, {n for n, e in _FLEET_TOMBSTONES.items() if e > started})
    # Evictions that predate this build's start need no tombstone: `_fleet_forget`
    # runs only after git has removed the worktree, so no later build can see it.
    _FLEET_TOMBSTONES = {n: e for n, e in _FLEET_TOMBSTONES.items() if e > started}
    _FLEET_CACHE["data"] = data
    _FLEET_CACHE["ts"] = time.monotonic()
    return data


def _fleet_rebuild_task() -> asyncio.Task[dict]:
    """The in-flight rebuild, starting one if none is running."""
    global _FLEET_INFLIGHT
    task = _FLEET_INFLIGHT
    if task is None or task.done():
        task = asyncio.ensure_future(_fleet_build())
        _FLEET_INFLIGHT = task
    return task


async def _fleet_refresh() -> dict:
    # shield: a client disconnecting mid-request must not cancel a rebuild that
    # other waiters (and the cache) depend on. Coalescing onto a build already in
    # flight is safe even for a `fresh=1` request that raced a removal:
    # `_fleet_build` re-applies any eviction that landed mid-build.
    return await asyncio.shield(_fleet_rebuild_task())


def _fleet_forget(name: str) -> None:
    """Evict one worktree from the cached snapshot and mark it for rebuild.

    ``_fleet_cached`` is stale-while-revalidate: once past the TTL it serves the
    PREVIOUS snapshot and only schedules a rebuild behind it. Without this hook a
    just-removed worktree keeps rendering for the length of a full rebuild, so
    the UI shows rows that no longer exist and refreshing does not help. Evicting
    the row makes the very next response truthful at zero rebuild latency, and
    zeroing the timestamp schedules the rebuild that refreshes the rest.
    """
    global _FLEET_EPOCH
    _FLEET_EPOCH += 1
    _FLEET_TOMBSTONES[name] = _FLEET_EPOCH
    data = _FLEET_CACHE["data"]
    if isinstance(data, dict):
        _FLEET_CACHE["data"] = _drop_worktrees(data, {name})
    _FLEET_CACHE["ts"] = 0.0


def _log_fleet_rebuild_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        runtime.logger.warning("dev-fleet: background fleet rebuild failed: %s", exc)


async def _fleet_cached() -> dict:
    data, ts = _FLEET_CACHE["data"], _FLEET_CACHE["ts"]
    if data is None:
        return await _fleet_refresh()
    if time.monotonic() - ts > _FLEET_TTL:
        task = _fleet_rebuild_task()
        # This caller does not await the rebuild, so consume its exception here
        # or asyncio reports it as never-retrieved.
        if not task.done():
            task.add_done_callback(_log_fleet_rebuild_failure)
    return data


#: Memoized ``(key, reason)``. The comparison needs real path resolution —
#: a symlinked checkout otherwise reads as a mismatch — and that is filesystem
#: IO, which on a network-backed checkout can stall. So it runs on the executor,
#: once per distinct key, rather than on the event loop for every ``/fleet`` poll.
_SERVING_REASON: "tuple[tuple[str, tuple[str, ...]], str | None] | None" = None


def _is_checkout(path: str) -> bool:
    """Whether *path* is a real git checkout. Blocking — executor only.

    ``.git`` is tested rather than mere existence, and as a path rather than a
    directory, because a linked worktree's ``.git`` is a FILE. An empty or absent
    directory is not something Dev Fleet can be said to manage.
    """
    if not path:
        return False
    try:
        return (Path(path) / ".git").exists()
    except (OSError, RuntimeError, ValueError):
        return False


def _serving_install_reason_sync(main_repo: str, managed: "tuple[str, ...]") -> str | None:
    """Why the install serving this dashboard is not one Dev Fleet manages.

    Blocking — resolves paths. Call it through an executor, never on the loop.

    Dev Fleet drives a set of checkouts, but the backend answering these routes
    can be a different install altogether — a published desktop bundle, say,
    whose own Pull+Build predates the dist-staging step. Every control then keeps
    reporting success while doing an incomplete job: the checkout fast-forwards,
    the bundle it serves does not, and a Restart button whose eligibility that
    older backend computes never appears at all. Saying nothing is what turns a
    one-line diagnosis into a long session chasing the downstream symptoms.

    Every discovered worktree counts as managed, not just the primary checkout:
    Make live deliberately points the gateway at a linked worktree that lives
    outside it, and warning about a state this app just created would train the
    user to dismiss the one signal built for the takeover case.
    """
    # __file__ is the strongest available evidence of which install is RUNNING:
    # it is this very module, so it cannot disagree with the process the way a
    # PATH-resolved binary can.
    pkg = Path(__file__).resolve().parents[3]
    for candidate in (main_repo, *managed):
        if not candidate:
            continue
        try:
            root = Path(candidate).resolve()
        except (OSError, RuntimeError, ValueError):
            # An unusable entry is skipped, not raised: /fleet is a read and every
            # other field still describes the fleet correctly. RuntimeError is in
            # the tuple because a symlink cycle surfaces as one rather than as an
            # OSError — the same reason beacon.is_default_home() catches it.
            continue
        if pkg == root or root in pkg.parents:
            return None
    if not _is_checkout(main_repo):
        # Nothing is actually being managed: a desktop-bundle or pip install with
        # no source checkout — the out-of-the-box case — would otherwise get a
        # permanent warning whose remedy ("start the gateway from <path>") names a
        # directory that is not there. A dead-end instruction on every visit is
        # how a signal gets trained away.
        return None
    return (
        "This dashboard is served by a different install than the checkout you are "
        "managing, so Pull+Build here does not change the code that runs. "
        f"Start the gateway from {runtime._redact(main_repo)}, or Make live onto it. "
        f"Serving now: {runtime._redact(str(pkg))} — an install older than the pulled "
        "revision may not refresh the dashboard bundle."
    )


async def _serving_install_reason(worktrees: "list[dict]") -> str | None:
    global _SERVING_REASON
    managed = tuple(sorted(str(wt["path"]) for wt in worktrees if wt.get("path")))
    # Reached only with a built fleet in hand, so the accessor cannot raise
    # here; it exists to keep the path build off the bare global.
    repo = repository._repo()
    key = (repo, managed)
    if _SERVING_REASON is not None and _SERVING_REASON[0] == key:
        return _SERVING_REASON[1]
    loop = asyncio.get_running_loop()
    reason = await loop.run_in_executor(
        subprocess_executor(),
        functools.partial(_serving_install_reason_sync, repo, managed),
    )
    _SERVING_REASON = (key, reason)
    return reason


async def _provision_reattach_ids() -> dict[str, str]:
    """Provision run ids the UI can reattach to after a page reload.

    A run id is exposed while the run is still executing, or when it finished
    unsuccessfully (the failed stepper + log must survive a reload). A failed
    id persists until the UI dismisses it (POST /api/pod/provision/dismiss
    forgets the id server-side, so a reload after dismissing does NOT re-show
    the failure), a newer provision for the same checkout overwrites it, the
    run is evicted from the bounded registry, or the gateway restarts.
    Successful runs are omitted: the refreshed fleet row already
    reports the built state, so there is nothing to reattach to. Run ids
    evicted from the bounded run registry are omitted too — the UI could not
    fetch their output anyway.
    """
    inflight = dict(_PROVISION_INFLIGHT)
    out: dict[str, str] = {}
    async with runtime._RUNS_LOCK:
        for name, rid in inflight.items():
            run = runtime._RUNS.get(rid)
            if not run:
                continue
            if run.get("status") == "running" or run.get("exit_code") not in (None, 0):
                out[name] = rid
    return out


# Per-pod system resources (memory / CPU / tasks / home size).
#
# The pod units already have CPUAccounting/MemoryAccounting on and systemd
# tracks MemoryCurrent / CPUUsageNSec / TasksCurrent per unit, plus the
# unit's own MemoryMax ceiling. We PLUMB that through; we do not collect it.
# Everything here is best-effort and Linux-only: on macOS (launchd, which
# emits no MemoryMax/CPUQuota) or any host where the probe fails, the fields
# are ABSENT (None), never fabricated zeros — the UI hides the readout when a
# field is absent so a blank pod never reads as "0 B used / 0%".
# --------------------------------------------------------------------------- #

# Properties fetched in the single batched ``systemctl --user show`` call.
_POD_RES_PROPS = (
    # Id first: it is what matches each emitted block back to the unit it
    # describes. Without it the only pairing available is positional, and a unit
    # systemd does not know emits no block -- shifting every later record onto
    # the wrong pod.
    "Id",
    "MemoryCurrent",
    "MemoryMax",
    "CPUUsageNSec",
    "TasksCurrent",
    "MemoryAccounting",
    "CPUAccounting",
    # Changes on every unit start, so a CPU sample can be tied to the exact
    # invocation it was taken from -- see _cpu_percent.
    "InvocationID",
)

# CPU% needs two samples: CPUUsageNSec is a monotonic counter, so a percentage
# is (Δcpu_ns / Δwall_ns) * 100. We keep the PREVIOUS (wall_ns, cpu_ns) per pod
# unit and report null on the first observation rather than a fake 0% — a
# fabricated 0 on a busy pod is worse than a blank. Keyed by unit name so a
# pod that stops and a new pod that reuses the name do not cross samples
# (the unit name carries the worktree name, which is the pod identity).
_POD_CPU_SAMPLES: dict[str, tuple[float, int, str]] = {}

# ``du`` over a multi-GB pod HOME is far too expensive to run on every poll,
# and the fleet payload is polled repeatedly by an open dashboard. Cache the
# size behind a TTL keyed by unit name: (measured_at_monotonic, size_bytes).
_POD_HOME_SIZE_CACHE: dict[str, tuple[float, int]] = {}
_POD_HOME_SIZE_TTL = 60.0  # seconds


def _parse_systemctl_records(text: str) -> list[dict[str, str]]:
    """Split a batched ``systemctl show`` dump into one dict per unit.

    ``systemctl show a b c -p ...`` emits the property block for each unit in
    argument order, separated by a blank line. Records are returned in that
    same order so the caller can zip them back to the unit list it asked for.
    Lines without ``=`` (there should be none) are ignored.
    """
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        key, sep, val = line.partition("=")
        if sep:
            current[key.strip()] = val.strip()
    if current:
        records.append(current)
    return records


def _coerce_uint(raw: str | None) -> int | None:
    """A non-negative int from a systemd property value, else None.

    systemd reports an unset/unknown numeric property as the sentinel
    ``[not set]`` or the max-uint ``18446744073709551615`` (``infinity`` for
    MemoryMax). Those are NOT measurements, so they collapse to None — the
    caller renders "no ceiling" / absent rather than an absurd byte count.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not raw or not raw.lstrip("-").isdigit():
        return None
    val = int(raw)
    if val < 0 or val >= 0xFFFFFFFFFFFFFFFF:
        return None
    return val


def _pod_resources_sync(cfg: Any, running_names: list[str]) -> dict[str, dict]:
    """Batched per-pod resource probe for the RUNNING pods only.

    ONE ``systemctl --user show`` invocation covering every running pod unit,
    not one subprocess per row. Returns ``{worktree_name: {mem_current,
    mem_max, cpu_pct, tasks, home_bytes}}`` where any field the host cannot
    measure is ``None``.

    Linux-only: ``rt.systemctl`` calls ``require_systemd`` which raises off
    Linux, so the whole probe degrades to ``{}`` (all rows absent) there. Any
    other failure degrades the same way — never partial fabricated data.
    """
    if not running_names:
        # No pods running is still liveness information: every cached sample now
        # belongs to a pod that is gone. Returning early WITHOUT pruning left
        # both caches holding every entry forever -- the CPU samples would then
        # be compared against a restarted pod's counter, and a stale home size
        # would keep feeding the fleet total. Prune first, then answer.
        _POD_CPU_SAMPLES.clear()
        _POD_HOME_SIZE_CACHE.clear()
        return {}
    units = [runtime.rt.pod_unit(cfg, n) for n in running_names]
    prop_args: list[str] = []
    for prop in _POD_RES_PROPS:
        prop_args.extend(["-p", prop])
    try:
        cp = runtime.rt.systemctl("show", *units, *prop_args, timeout=10)
    except Exception:  # noqa: BLE001 — off-Linux (require_systemd) or probe error
        return {}
    records = _parse_systemctl_records(cp.stdout or "")
    now = time.monotonic()
    out: dict[str, dict] = {}
    # Match each record to its unit by the Id systemd echoes back, NOT by
    # position. `systemctl show` emits blocks in argument order, but a unit it
    # does not know contributes no block -- so a positional zip would shift every
    # later record onto the wrong pod and publish one pod's memory and CPU under
    # another pod's name. Misattributed resource figures are worse than absent
    # ones: they read as measured. A record whose Id is missing or unknown is
    # dropped, leaving that pod's fields absent.
    by_unit = {rec["Id"]: rec for rec in records if rec.get("Id")}
    for name, unit in zip(running_names, units):
        rec = by_unit.get(unit)
        if rec is None:
            continue
        mem_current = (
            _coerce_uint(rec.get("MemoryCurrent")) if rec.get("MemoryAccounting") == "yes" else None
        )
        mem_max = _coerce_uint(rec.get("MemoryMax"))
        tasks = _coerce_uint(rec.get("TasksCurrent"))
        cpu_pct = _cpu_percent(unit, rec, now)
        out[name] = {
            "mem_current": mem_current,
            "mem_max": mem_max,
            "cpu_pct": cpu_pct,
            "tasks": tasks,
            # Filled in by the caller: the pod-HOME `du` goes through the async
            # routed chokepoint (`_run_cmd`), which cannot be awaited from this
            # sync probe. Absent until then, never a fabricated 0.
            "home_bytes": None,
        }
    # Drop CPU samples for pods no longer running so a stopped-then-restarted
    # pod starts fresh (null on its first new observation) and the dict cannot
    # grow without bound across a long-lived gateway.
    for stale in set(_POD_CPU_SAMPLES) - set(units):
        _POD_CPU_SAMPLES.pop(stale, None)
    # Same for the home-size cache, which was previously left to expire on its
    # TTL. A worktree evicted mid-TTL kept a cached size that went on being
    # summed into the fleet total, so the header reported disk for a pod that no
    # longer existed -- and the dict grew unbounded besides. Dropping it here
    # ties both to the same liveness signal.
    for stale in set(_POD_HOME_SIZE_CACHE) - set(units):
        _POD_HOME_SIZE_CACHE.pop(stale, None)
    return out


def _cpu_percent(unit: str, rec: dict[str, str], now: float) -> float | None:
    """CPU% from two CPUUsageNSec samples; None on the first observation.

    Reports None (not 0) the first time a pod is seen and whenever accounting
    is off or the counter is unreadable, so the UI shows a blank rather than a
    fabricated 0% on a busy pod.

    Each sample is tied to the unit's ``InvocationID``, which systemd changes on
    every start. Keying on the unit NAME alone is not enough: a pod that stops
    and restarts between two polls keeps its name while ``CPUUsageNSec`` restarts
    from zero, and the backwards-counter guard below only catches that when the
    new invocation has not yet burned past the old total. A fast restart with
    heavy startup work passes that guard and yields a positive delta spanning two
    different processes -- a number that looks like a measurement and is not. A
    changed invocation discards the previous sample instead.
    """
    invocation = rec.get("InvocationID") or ""
    if rec.get("CPUAccounting") != "yes":
        _POD_CPU_SAMPLES.pop(unit, None)
        return None
    cpu_ns = _coerce_uint(rec.get("CPUUsageNSec"))
    if cpu_ns is None:
        _POD_CPU_SAMPLES.pop(unit, None)
        return None
    prev = _POD_CPU_SAMPLES.get(unit)
    _POD_CPU_SAMPLES[unit] = (now, cpu_ns, invocation)
    if prev is None:
        return None
    prev_wall, prev_cpu, prev_invocation = prev
    # A different (or newly unknown) invocation means the counter belongs to a
    # different process than the one we sampled. Not comparable.
    if prev_invocation != invocation:
        return None
    wall_delta_ns = (now - prev_wall) * 1e9
    cpu_delta_ns = cpu_ns - prev_cpu
    # A non-positive wall delta (clock jump / same instant) or a counter that
    # went backwards (restart the invocation check somehow missed) is not a
    # measurement.
    if wall_delta_ns <= 0 or cpu_delta_ns < 0:
        return None
    return round(cpu_delta_ns / wall_delta_ns * 100.0, 1)


async def _pod_home_size(cfg: Any, name: str, unit: str, now: float) -> int | None:
    """Pod HOME size in bytes, cached behind a TTL.

    ``du`` over a multi-GB tree is too expensive to run every poll, so the
    result is cached per pod for ``_POD_HOME_SIZE_TTL`` seconds. The HOME path
    is resolved through the existing ``rt.pod_home`` — never hardcoded. Any
    failure (missing tree, du error) yields None, not 0.

    The ``du`` itself goes through ``_run_cmd``, the same routed chokepoint the
    module's two other ``du`` calls use. That is deliberately not a bare
    ``subprocess.run``: routing is what vets the binary instead of trusting the
    service PATH (which leads with agent-writable directories), pins the child's
    PATH and encoding, and keeps the spawn inside the sandbox chokepoint the
    repo's spawn audit requires. Doing it by hand needed three separate
    exceptions and still would not have been the module's own pattern.
    """
    cached = _POD_HOME_SIZE_CACHE.get(unit)
    if cached is not None and (now - cached[0]) < _POD_HOME_SIZE_TTL:
        return cached[1]
    try:
        home = runtime.rt.pod_home(cfg, name)
    except Exception:  # noqa: BLE001
        return cached[1] if cached is not None else None
    rc, stdout, _ = await runtime._run_cmd(["du", "-sb", str(home)], timeout=20)
    if rc != 0:
        return cached[1] if cached is not None else None
    try:
        size = int(stdout.split()[0])
    except (ValueError, IndexError):
        return cached[1] if cached is not None else None
    _POD_HOME_SIZE_CACHE[unit] = (now, size)
    return size


# Fleet-level totals for the page header ("this needs cleaning" legibility).
#
# Worktree disk is deliberately NOT computed here. The dashboard already shows
# it, sourced from the pre-existing ``/disk`` endpoint, and that endpoint is
# async out-of-band on purpose: a ``du`` over every worktree is slow (tens of
# GB across tens of trees), so it runs as a background task behind an
# idle/computing/done handshake rather than inside a request. Measuring it a
# second time in ``_build_fleet`` would put that same ``du`` on the POLLED
# payload path -- paid again on every TTL expiry, for a figure the page is
# already displaying from another source. Two independently-computed values
# under one label is also a number an operator cannot trust. So this leaves
# worktree disk to its existing owner and carries only what is genuinely new
# and cheap: pod-home disk (already measured per running pod for the row
# readout, so summing it is free) and the orphan count (a directory scan).


def _orphan_count_sync(cfg: Any) -> int | None:
    """Count of pod HOMEs left on disk with no live pod. None on failure.

    Reuses ``rt.orphan_homes`` (a cheap directory scan, no ``du``) — the same
    predicate the prune flow uses — so the header's "N to clean" agrees with
    what a prune would actually reclaim.
    """
    try:
        return len(runtime.rt.orphan_homes(cfg))
    except Exception:  # noqa: BLE001
        return None


async def _build_fleet() -> dict:
    live_path = await live._live_worktree_path()
    staged_path = live._staged_target()
    worktrees = await repository._discover_worktrees()
    cfg = runtime._load_cfg()
    legacy_prefixes = tuple(
        f"{r.split('/')[-1].lower()}-wt-" for r in (repository._FALLBACK_REPOS or [])
    )
    wts = []
    for wt in worktrees:
        path = wt.get("path", "")
        branch = wt.get("branch")
        is_main = wt.get("is_main", False)
        g = await repository._git_info(path)
        pr = (await _pr_status_cached(branch, g.get("head_oid"))) if branch else None
        name = Path(path).name if not is_main else repository.BASE_BRANCH

        # Pod status (best-effort)
        running = False
        port = None
        health = None
        has_venv = False
        has_dist = False
        loop = asyncio.get_running_loop()
        # Build state is a plain filesystem check (``.venv`` binary present /
        # ``static/dist`` directory present) and is therefore knowable on EVERY
        # platform — report it even where pods cannot run, so the Fleet view
        # still tells the truth about which worktrees are built. That includes
        # the MAIN checkout (#8058): during a cutover the panel is exactly the
        # surface a human checks, and reporting main as unprovisioned reads as
        # "the cutover failed". The ``not is_main`` restriction belongs to the
        # POD-state check below (pods never run on main), not to these probes.
        if runtime._POD_IMPORTED:
            try:
                has_venv = await loop.run_in_executor(
                    subprocess_executor(), runtime.prov.has_venv, Path(path)
                )
                has_dist = await loop.run_in_executor(
                    subprocess_executor(), runtime.prov.has_dist, Path(path)
                )
            except Exception:  # noqa: BLE001
                pass
        # Pod state, by contrast, only exists where pods can run.
        if runtime._POD_AVAILABLE and cfg and not is_main:
            try:
                active = await loop.run_in_executor(
                    subprocess_executor(), runtime.rt.active_names, cfg
                )
                running = name in active
                if running:
                    port = await loop.run_in_executor(
                        subprocess_executor(), runtime.rt.derive_port, cfg, name
                    )
                    # Identity-gated: ``rt.health`` takes the pod NAME as well as
                    # the port because a derived port can be held by another pod
                    # or by the live gateway, and a bare port probe would report
                    # that squatter's 200 as this worktree's health — the row
                    # would show a healthy dot for a pod that never bound its
                    # port. A foreign responder comes back as
                    # ``rt.HEALTH_FOREIGN`` and renders as unhealthy.
                    health = await loop.run_in_executor(
                        subprocess_executor(), runtime.rt.health, cfg, name, port, 2
                    )
            except Exception:  # noqa: BLE001
                pass

        ahead = await repository._git_ahead(path)
        # "shipped" drives the UI's "safe to remove" affordance — require a
        # POSITIVELY clean worktree (dirty is False, not merely unknown), so
        # the confirm dialog never promises a removal the backend will refuse.
        shipped = (
            _is_pr_merged(pr)
            and (ahead is not None and ahead == 0)
            and g["dirty"] is False
            and not is_main
        )

        # Per-worktree context (issue/ticket links + purpose one-liner). Skipped
        # for the main checkout (no feature context). Best-effort: never raises.
        ctx = (
            await _context_cached(branch, path, pr)
            if branch and not is_main
            else {"issues": [], "tickets": [], "summary": None}
        )

        wts.append(
            {
                # "name" doubles as the opaque identifier for follow-up actions
                # (validated against the discovered set on every call); display
                # fields sourced from git/gh output are redacted.
                "name": name,
                "path": runtime._redact(path),
                "is_main": is_main,
                "running": running,
                "port": port,
                "health": health,
                "is_live": live_path is not None and repository._same_path(path, live_path),
                "is_staged": staged_path is not None and repository._same_path(path, staged_path),
                "has_venv": has_venv,
                "has_dist": has_dist,
                "branch": runtime._redact(g["branch"] or branch or ""),
                "head": g["head"] or wt.get("head", "")[:7],
                "dirty": g["dirty"],
                "behind": g["behind"],
                "pr": runtime._redact_pr(pr),
                "shipped": shipped,
                "issues": ctx["issues"],
                "tickets": ctx["tickets"],
                "summary": ctx["summary"],
                "legacy": bool(legacy_prefixes)
                and not is_main
                and name.lower().startswith(legacy_prefixes),
                "last_updated_at": g["last_updated_at"],
                # Per-pod system resources (memory/CPU/tasks/home size). Filled in
                # by one batched probe after the loop for running pods on Linux;
                # stays None for stopped pods, the main row, and off Linux — the
                # UI hides the readout entirely when it is absent.
                "pod_resources": None,
            }
        )
    # ONE batched resource probe for every running pod, off the row loop so the
    # payload never spawns a subprocess per row per poll. Best-effort and
    # Linux-only (the probe itself degrades to {} off Linux / on failure), so a
    # miss simply leaves ``pod_resources`` None and the UI hides the readout.
    if runtime._POD_AVAILABLE and cfg:
        # Rows are keyed by the worktree's BASENAME, and a pod is identified by
        # that same name -- so two worktrees under different parents that share a
        # basename resolve to ONE pod unit. Probing under that name would then
        # publish that single pod's memory, CPU and disk on BOTH rows, as though
        # each had its own. We cannot tell which row owns the pod (this module
        # already treats a basename collision as unattributable -- see
        # `_reclaim_pod_locked`), so a collided name is withheld from the probe
        # entirely and both rows keep `pod_resources` absent. Duplicated figures
        # would read as measured; absent ones are honest.
        live_row_names = [w["name"] for w in wts if w.get("running") and not w.get("is_main")]
        collided = {nm for nm in live_row_names if live_row_names.count(nm) > 1}
        running_names = [nm for nm in live_row_names if nm not in collided]
        if collided:
            runtime.logger.warning(
                "dev-fleet: withholding pod resources for %d ambiguous worktree "
                "basename(s) -- a shared basename cannot be attributed to one pod",
                len(collided),
            )
        if running_names:
            try:
                res = await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    functools.partial(_pod_resources_sync, cfg, running_names),
                )
            except Exception:  # noqa: BLE001
                res = {}
            # Pod-HOME size is the one figure the sync probe cannot take: its
            # `du` goes through the async routed chokepoint. TTL-cached, so this
            # is a no-op on most polls.
            now = time.monotonic()
            for nm in running_names:
                if nm not in res:
                    continue
                try:
                    res[nm]["home_bytes"] = await _pod_home_size(
                        cfg, nm, runtime.rt.pod_unit(cfg, nm), now
                    )
                except Exception:  # noqa: BLE001
                    res[nm]["home_bytes"] = None
            for w in wts:
                if w["name"] in res:
                    w["pod_resources"] = res[w["name"]]
    # Fleet-level totals for the header: worktree disk (batched du, TTL-cached),
    # pod-home disk (summed from the resource probe above), and orphan count.
    # All best-effort — a field stays None and the header omits it rather than
    # rendering a fabricated 0.
    fleet_totals: dict[str, Any] = {
        "pod_home_bytes": None,
        "orphan_pods": None,
    }
    if runtime._POD_AVAILABLE and cfg:
        # A TOTAL has to cover everything it claims to. Summing only the pods
        # that reported would publish a partial figure under a total's label --
        # the operator reads "Pod-home disk: 2.1GB" and cannot tell that a pod
        # whose `du` failed is missing from it. That is the same fabrication the
        # per-row fields are careful to avoid, one level up. So the total is
        # published only when EVERY running pod measured; otherwise it is absent
        # and the header omits it.
        measured = [
            w["pod_resources"]["home_bytes"]
            for w in wts
            if w.get("pod_resources") and w["pod_resources"].get("home_bytes") is not None
        ]
        # `expected` is the number of pods actually RUNNING -- not the number that
        # happened to come back with a record. Counting only rows that already
        # have `pod_resources` made the check self-consistent instead of true: a
        # pod dropped by the record matching, or withheld for an ambiguous
        # basename, has no `pod_resources` and so vanished from both sides of the
        # comparison, letting a total publish while a running pod was missing
        # from it.
        expected = sum(1 for w in wts if w.get("running") and not w.get("is_main"))
        if measured and len(measured) == expected:
            fleet_totals["pod_home_bytes"] = sum(measured)
        try:
            fleet_totals["orphan_pods"] = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                functools.partial(_orphan_count_sync, cfg),
            )
        except Exception:  # noqa: BLE001
            pass
    # The run pointers a reloaded page reattaches to -- `sync_run_id` and each
    # row's `provision_run_id` -- are deliberately NOT set here. This snapshot is
    # cached and served stale-while-revalidate, so a pointer written at build
    # time is a frozen answer to a live question; `_with_live_run_pointers`
    # overlays both at request time instead. One owner, so no reader can pick up
    # a stale id.
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "worktrees": wts,
        "main_repo": runtime._redact(repository._repo()),
        "main_repo_inferred": repository.MAIN_REPO_INFERRED,
        "base_branch": repository.BASE_BRANCH,
        "build_pending": _build_pending(),
        "gateway_service_active": await live._gateway_service_active(),
        # Non-null while a cutover is staged but not yet running: the UI renders a
        # persistent pending-restart state from this, so the instruction outlives
        # the toast that announced it.
        "staged_target": runtime._redact(staged_path) if staged_path else None,
        # Whether the pointer-only cancel of that stage would be accepted (see
        # _staged_cancel_available). Only probed while a stage exists; false
        # otherwise so the dashboard's cancel control stays hidden.
        "staged_cancel_available": (
            staged_path is not None and await live._staged_cancel_available()
        ),
        "manual_restart": live._manual_restart_command(),
        # WHY the gateway cannot be restarted/repointed from here, when it
        # cannot. Same lesson as pods_unavailable_reason below: the previous
        # behaviour was to hide Restart and Make live with no explanation, so a
        # macOS user saw a Pull+Build succeed with no way to apply it and
        # nothing on screen saying why. ``None`` when the service is drivable.
        "gateway_service_reason": await live._gateway_service_reason(),
        # Non-null when the backend answering this request is NOT the checkout
        # below. Reported for the same reason as gateway_service_reason: the
        # controls stay clickable and keep succeeding, so nothing else on screen
        # would ever reveal that the managed code is not the running code.
        "serving_install_reason": await _serving_install_reason(worktrees),
        # Whether pods can run on THIS host, and if not, why. Previously
        # _POD_ERROR was computed and then never read by anything, so a
        # non-Linux user saw pod controls that silently failed with no
        # explanation. The UI uses these to disable those controls and say why.
        "pods_available": runtime._POD_AVAILABLE,
        "fleet_totals": fleet_totals,
        "pods_unavailable_reason": runtime._POD_ERROR or None,
    }


async def _worktree_detail(name: str) -> dict:
    """Lazy per-worktree detail."""
    wt, err = await repository._find_worktree(name)
    if wt is None:
        return {"error": err}
    path = wt["path"]
    branch = wt.get("branch")
    is_main = wt.get("is_main", False)
    g = await repository._git_info(path)
    pr = (await _pr_status_cached(branch, g.get("head_oid"))) if branch else None
    own_commits = await repository._own_commits_count(path)

    remote = await repository._upstream_remote()
    commits: list[dict] = []
    if not is_main:
        log = await repository._git(
            path,
            "log",
            f"{remote}/{repository.BASE_BRANCH}..HEAD",
            "-12",
            "--format=%h\x1f%s\x1f%cr",
        )
        if log:
            for line in log.splitlines():
                parts = line.split("\x1f")
                if len(parts) == 3:
                    commits.append(
                        {"hash": parts[0], "subject": runtime._redact(parts[1]), "when": parts[2]}
                    )

    design_docs: list[str] = []
    if not is_main:
        diff_out = await repository._git(
            path,
            "diff",
            "--name-only",
            f"{remote}/{repository.BASE_BRANCH}...HEAD",
            timeout=15,
        )
        if diff_out:
            seen: set[str] = set()
            for line in diff_out.splitlines():
                line = line.strip()
                if not line or line in seen:
                    continue
                seen.add(line)
                low = line.lower()
                if low.startswith("docs/") or "/docs/" in low or "design" in low:
                    design_docs.append(runtime._redact(line))
                if len(design_docs) >= 12:
                    break

    disk_mb = None
    try:
        rc, stdout, _ = await runtime._run_cmd(["du", "-sm", path], timeout=15)
        if rc == 0:
            disk_mb = int(stdout.split()[0])
    except (ValueError, IndexError):
        pass

    pod_running = False
    pod_port = None
    cfg = runtime._load_cfg()
    if runtime._POD_AVAILABLE and cfg and not is_main:
        try:
            loop = asyncio.get_running_loop()
            active = await loop.run_in_executor(subprocess_executor(), runtime.rt.active_names, cfg)
            pod_running = name in active
            if pod_running:
                pod_port = await loop.run_in_executor(
                    subprocess_executor(), runtime.rt.derive_port, cfg, name
                )
        except Exception:
            pass

    # Context (issue/ticket links + purpose one-liner) assembled from the
    # commits already fetched above (their subjects) + the PR body — no extra
    # git log, so the detail endpoint keeps to a single own-commits log call.
    ctx = (
        await _resolve_context(branch, [c["subject"] for c in commits], [], (pr or {}).get("_body"))
        if branch and not is_main
        else {"issues": [], "tickets": [], "summary": None}
    )
    # `real_dirty` keeps its exact prior meaning and its own query, so the
    # authoritative "is there any dirt" answer is unchanged. The breakdown is
    # additive and computed only when there IS dirt -- a clean tree has nothing
    # to describe, and an unverifiable one (None) has nothing trustworthy to say.
    real_dirty = await repository._real_dirty(path)
    dirt_tracked, dirt_untracked = await repository._dirty_split(path) if real_dirty else (None, [])
    return {
        "name": name,
        "path": runtime._redact(path),
        "branch": runtime._redact(g["branch"] or branch or ""),
        "head": g["head"],
        "dirty": g["dirty"],
        "own_commits": own_commits,
        "real_dirty": real_dirty,
        **repository._dirt_fields(dirt_tracked, dirt_untracked),
        "pr": runtime._redact_pr(pr),
        "pr_merged": _is_pr_merged(pr),
        "issues": ctx["issues"],
        "tickets": ctx["tickets"],
        "summary": ctx["summary"],
        "commits": commits,
        "design_docs": design_docs,
        "disk_mb": disk_mb,
        "behind": g["behind"],
        "is_main": is_main,
        "pod_running": pod_running,
        "pod_port": pod_port,
    }


# Per-worktree provisioning single-flight: name -> run id. Repeated POSTs
# must not concurrently recreate .venv / dist for the same checkout.
_PROVISION_INFLIGHT: dict[str, str] = {}
_PROVISION_LOCK = LoopBoundLock()


# --- disk aggregation ---
_DISK: dict = {"status": "idle", "total_mb": None, "per": {}}
_DISK_COMPUTING = False


async def _disk() -> dict:
    global _DISK_COMPUTING
    if _DISK["status"] == "computing":
        return dict(_DISK)
    if _DISK["status"] == "done":
        snap = dict(_DISK)
        _DISK["status"] = "idle"
        return snap
    _DISK["status"] = "computing"
    _DISK_COMPUTING = True

    async def work() -> None:
        global _DISK_COMPUTING
        try:
            per: dict = {}
            total = 0
            for w in await repository._discover_worktrees():
                nm = Path(w["path"]).name
                try:
                    rc, stdout, _ = await runtime._run_cmd(["du", "-sm", w["path"]], timeout=60)
                    if rc == 0:
                        mb = int(stdout.split()[0])
                        per[nm] = mb
                        total += mb
                except (ValueError, IndexError):
                    pass
            _DISK.update({"status": "done", "total_mb": total, "per": per})
        except Exception:  # noqa: BLE001
            _DISK.update({"status": "done", "total_mb": None, "per": {}})
        finally:
            _DISK_COMPUTING = False

    asyncio.create_task(work())
    return {"status": "computing", "total_mb": None, "per": {}}


__all__ = (
    "_CTX_CACHE",
    "_CTX_MAX_ISSUES",
    "_CTX_MAX_TICKETS",
    "_CTX_TTL",
    "_DISK",
    "_DISK_COMPUTING",
    "_FLEET_CACHE",
    "_FLEET_EPOCH",
    "_FLEET_INFLIGHT",
    "_FLEET_TOMBSTONES",
    "_FLEET_TTL",
    "_HTML_BASE",
    "_ISSUE_REF_RE",
    "_OWNER_REPO",
    "_OWNER_REPO_RETRY_AT",
    "_POD_CPU_SAMPLES",
    "_POD_HOME_SIZE_CACHE",
    "_POD_HOME_SIZE_TTL",
    "_POD_RES_PROPS",
    "_PROVISION_INFLIGHT",
    "_PROVISION_LOCK",
    "_PR_CACHE",
    "_PR_TTL",
    "_SERVING_REASON",
    "_START_EPOCH",
    "_TICKET_ID_RE",
    "_VERSION_BUMP_RE",
    "_build_context",
    "_build_fleet",
    "_build_pending",
    "_coerce_uint",
    "_context_cached",
    "_cpu_percent",
    "_disk",
    "_drop_worktrees",
    "_extract_issue_refs",
    "_extract_ticket_ids",
    "_fetch_pr_head_oid",
    "_fetch_pr_status",
    "_fleet_build",
    "_fleet_cached",
    "_fleet_forget",
    "_fleet_rebuild_task",
    "_fleet_refresh",
    "_get_owner_repo",
    "_head_contained_in_pr",
    "_html_repo_base",
    "_is_checkout",
    "_is_pr_closed",
    "_is_pr_merged",
    "_is_version_bump",
    "_issue_url",
    "_log_fleet_rebuild_failure",
    "_orphan_count_sync",
    "_parse_html_repo_base",
    "_parse_systemctl_records",
    "_pick_summary",
    "_pod_home_size",
    "_pod_resources_sync",
    "_pr_query_one",
    "_pr_status_cached",
    "_provision_reattach_ids",
    "_render_ticket_url",
    "_repo_owner_name",
    "_resolve_context",
    "_serving_install_reason",
    "_serving_install_reason_sync",
    "_worktree_detail",
)
