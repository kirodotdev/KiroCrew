"""``GET /api/file-search``: the name search behind the ``@`` picker."""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _SEARCH_LIMIT_CEILING,
        _WALK_MAX_DIRS_VISITED,
        _WALK_MAX_SCAN_SCOPED,
        _WALK_MAX_SCAN_UNSCOPED,
        _WALK_SKIP_DIRS,
        DashboardState,
        _PathProbeBusy,
        _probe_busy_response,
        _run_path_probe,
        _sel,
        data_home,
        logger,
        platform_compat,
        require_owner_dashboard_request,
    )


def _resolve_search_root(raw: str) -> tuple[str, bool]:
    """Canonicalize a caller-supplied search root; say whether it is a directory.

    Blocking (``realpath`` then ``isdir``) -- callers run it on a worker thread.
    An empty *raw* means "the caller named no root", which the browse endpoints
    answer with ``$HOME``; the search endpoint never passes one.
    """
    root = os.path.realpath(os.path.expanduser(raw or "~"))
    return root, os.path.isdir(root)


def _subsequence_run(q: str, haystack: str) -> tuple[int, int]:
    """Greedily match ``q`` as a subsequence of ``haystack``.

    Returns how many of ``q``'s characters were consumed in order, and the
    longest run of matches that landed on consecutive ``haystack`` positions
    within that single greedy pass -- NOT the longest contiguous occurrence of
    ``q``, since the scan never backtracks over an earlier isolated match
    (``q="ab"`` against ``"axxab"`` consumes both chars but reports a run of
    1). A consumed count below ``len(q)`` means ``haystack`` does not contain
    ``q`` as a subsequence at all; the caller normalizes the run length by
    ``len(q)`` into the contiguity term of the fuzzy score.
    """
    qi = 0
    consecutive = 0
    max_run = 0
    for ch in haystack:
        if qi < len(q) and ch == q[qi]:
            qi += 1
            consecutive += 1
            max_run = max(max_run, consecutive)
        else:
            consecutive = 0
    return qi, max_run


def _fuzzy_score(q: str, name: str, rel: str) -> float:
    """Score a file match. Higher = better. Returns 0 for no match."""
    nl = name.lower()
    rl = rel.lower()
    score = 0.0

    # Exact filename match (sans extension)
    stem = nl.rsplit(".", 1)[0] if "." in nl else nl
    if q == nl or q == stem:
        score += 100.0
    elif nl.startswith(q):
        score += 50.0
    elif q in nl:
        score += 30.0
    elif q in rl:
        score += 10.0
    else:
        # Fuzzy: check whether the query chars appear in order in the
        # filename, falling back to the search-root-relative path when the
        # filename alone does not carry the query as an in-order subsequence.
        matched_on_name = True
        qi, max_run = _subsequence_run(q, nl)
        if qi < len(q):
            matched_on_name = False
            qi, max_run = _subsequence_run(q, rl)
        if qi < len(q):
            return 0.0  # not all query chars found
        # Score based on coverage ratio and longest consecutive run
        matched_len = len(nl) if matched_on_name else len(rl)
        coverage = len(q) / max(matched_len, 1)
        score += 5.0 + 15.0 * (max_run / len(q)) + 5.0 * coverage

    # Bonus: shorter filenames are more relevant
    score += max(0.0, 5.0 - len(nl) * 0.1)
    return score


async def _audit_file_search_exit(caller: str, resources: str, error: str = "") -> None:
    """Record one file-search outcome without blocking the loop or raising.

    Two properties this endpoint needs and a bare ``_sel()`` call does not give:

    * The singleton is warmed at gateway start, but a FAILED warm leaves
      construction to the first caller -- key load and a tail read of the log --
      and this runs on the event loop. Same gate and hop as
      ``handlers/decisions._audit`` and ``server._audit_middleware_denial``: two
      attribute reads on the healthy path, a worker thread on the degraded one
      (``no-blocking-call-on-event-loop``).
    * Best-effort. These calls sit on EARLY-EXIT paths that answered cleanly
      before, so an audit that raised would turn a 200 or a 404 into a 500. The
      record is what degrades, never the response.
    """
    from kiro_crew.sel import sel_is_warm

    def _write() -> None:
        _sel().log_api_access(
            caller=caller,
            operation="file_search",
            outcome="allowed",
            resources=resources,
            error=error,
        )

    try:
        if sel_is_warm():
            _write()
        else:
            await asyncio.to_thread(_write)
    except Exception:  # noqa: BLE001 - the record degrades, not the answer
        logger.warning("SEL logging failed for file_search", exc_info=True)


async def api_file_search(request: web.Request) -> web.Response:
    """GET /api/file-search?q=... — fuzzy filename search for the @-mention file picker.

    OWNER-ONLY, like every other reader in the file API (``file_read``,
    ``file_grep``, ``browse_dirs``, ``browse_files`` and the rest). The gate
    matters more here than on any of them, because this is the one path reader
    that takes an ARBITRARY root: ``?project=`` names any directory on the host
    and only ``is_sensitive_path`` is consulted, so without the gate a non-owner
    dashboard session could walk the host outside the credential set and read
    back real names, sizes and mtimes. ``/api/path-complete`` answers the same
    picker and is not in that position: it resolves the SERVER-HELD value its
    ``path`` matched against the known project directories.
    """
    # Re-imported at call time (not reused from the module-level binding) so a
    # test that stubs ``kiro_crew.security.is_sensitive_path`` is observed by the
    # project-root rejection below.
    from kiro_crew.security import is_sensitive_path  # noqa: F811

    owner_denied = await require_owner_dashboard_request(request, "file_search")
    if owner_denied is not None:
        return owner_denied
    caller = request.get("user", "dashboard")
    query = request.query.get("q", "").strip().lower()
    if len(query) < 2:
        # Audited like every other exit of this handler. The shared gate records
        # only denials, so an exit that answers without an audit of its own leaves
        # a SUCCESSFUL authorization with no SEL event at all -- the access was
        # granted and nothing says so. Same idiom as ``api_file_diff``'s early
        # ``allowed`` events: the ordinary outcome vocabulary, distinguished by
        # ``resources``, rather than a marker only this handler emits.
        await _audit_file_search_exit(caller, "short_query")
        return web.json_response({"results": []})

    # Result page size. Default mirrors SEARCH_RESULT_CAP in FolderPanel.tsx;
    # the caller may raise it via ``limit`` (the folder panel's expand control),
    # clamped to ``_SEARCH_LIMIT_CEILING`` server-side. Non-integer input falls
    # back to the default, mirroring how ``kinds`` handles unknown values.
    try:
        max_results = int(request.query.get("limit", "15"))
    except ValueError:
        max_results = 15
    max_results = max(1, min(max_results, _SEARCH_LIMIT_CEILING))

    # kinds: "all" (default) returns both files and directories; "files" or
    # "dirs" restricts the result set. Unknown values fall back to "all".
    kinds = request.query.get("kinds", "all").strip().lower()
    if kinds not in ("all", "files", "dirs"):
        kinds = "all"
    want_files = kinds in ("all", "files")
    want_dirs = kinds in ("all", "dirs")

    # Scope search to project (arbitrary path) or workspace
    project = request.query.get("project", "")
    ws_name = request.query.get("workspace", "")
    search_roots: list[str] = []
    if project:
        # Off-loop: realpath on a caller-supplied root, then its isdir probe.
        # ``?project=`` names any path on the host, so an unresponsive mount
        # would stall the loop here, before the already-offloaded walk is
        # reached.
        try:
            project, project_is_dir = await _run_path_probe(_resolve_search_root, project)
        except _PathProbeBusy:
            return _probe_busy_response(resource=project, operation="file_search", caller=caller)
        if is_sensitive_path(project):
            _sel().log_api_access(
                caller=caller,
                operation="file_search",
                outcome="denied",
                resources=project,
                error="sensitive path",
            )
            return web.json_response(
                {"error": "Access denied", "code": "access_denied"}, status=403
            )
        if project_is_dir:
            search_roots.append(project)
        else:
            # Audited for the same reason as the short-query exit above: the
            # authorization succeeded, so the record must not end at the gate.
            await _audit_file_search_exit(caller, f"project={project}", error="not a directory")
            return web.json_response(
                {
                    "results": [],
                    "error": "Project directory not found",
                    "code": "project_not_found",
                },
                status=404,
            )
    elif ws_name:
        from kiro_crew.config.loader import workspace_dir_for  # noqa: F811

        ws_path = str(workspace_dir_for(ws_name))
        try:
            ws_is_dir = await _run_path_probe(os.path.isdir, ws_path)
        except _PathProbeBusy:
            return _probe_busy_response(resource=ws_path, operation="file_search", caller=caller)
        if ws_is_dir:
            search_roots.append(ws_path)

    scoped = bool(search_roots)

    if not search_roots:
        # Fallback: project dir, then the kirocrew workspace.
        #
        # Bare $HOME is deliberately NOT a fallback root. Walking it reaches
        # every TCC-gated folder macOS knows about, and each one costs a
        # separate consent dialog -- paid on an unscoped keystroke the user
        # never pointed anywhere. The results did not justify it either: the
        # walk stops at max_scan entries in os.walk order, so an unscoped home
        # search returned whichever files happened to be reached first rather
        # than the best matches. Callers that genuinely want home can still
        # ask for it explicitly with ?project=$HOME, which is scoped and
        # searched in full.
        proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
        mc_workspace = str(data_home() / "workspace")

        def _probe_fallback_roots() -> tuple[bool, bool]:
            return bool(proj) and os.path.isdir(proj), os.path.isdir(mc_workspace)

        # Off-loop: two isdir probes on operator-configured paths, either of
        # which may sit on a network mount.
        try:
            proj_is_dir, workspace_is_dir = await _run_path_probe(_probe_fallback_roots)
        except _PathProbeBusy:
            return _probe_busy_response(resource=proj, operation="file_search", caller=caller)
        if proj_is_dir:
            search_roots.append(proj)
        if workspace_is_dir:
            search_roots.append(mc_workspace)

    # Filter out sensitive roots
    safe_roots: list[str] = []
    for r in search_roots:
        if is_sensitive_path(r):
            _sel().log_api_access(
                caller=caller,
                operation="file_search",
                outcome="denied",
                resources=r,
                error="sensitive path",
            )
        else:
            safe_roots.append(r)

    # Fast path: use in-memory index when available for a single scoped project
    state: DashboardState = request.app["state"]
    if scoped and len(safe_roots) == 1:
        idx = state.file_indexes.get(safe_roots[0])
        if idx and idx.is_ready and not idx.truncated:
            results = await asyncio.to_thread(idx.search, query, _fuzzy_score, max_results, kinds)
            trimmed = [{k: v for k, v in r.items() if k != "_score"} for r in results]
            _sel().log_api_access(
                caller=caller,
                operation="file_search",
                outcome="allowed",
                resources=f"q={query} kinds={kinds} indexed=true entries={idx.entry_count} results={len(trimmed)}",
            )
            return web.json_response({"results": trimmed, "root": safe_roots[0]})

    # Fallback: walk filesystem per request
    # Dot-prefixed FILES stay excluded (startswith(".") guard in _collect).
    # Dot-prefixed DIRECTORIES (.github, .kiro, .claude) ARE offered as
    # candidates; only skip_dirs below are dropped from both descent and results.
    # skip_dirs is the SAME shared set the indexed fast path uses (imported from
    # file_index), so the two paths of this endpoint cannot diverge on which
    # directories are suppressed.
    skip_dirs = _WALK_SKIP_DIRS

    max_scan = _WALK_MAX_SCAN_SCOPED if scoped else _WALK_MAX_SCAN_UNSCOPED
    max_collect = max_results * 10  # collect enough candidates for good scoring, then stop

    def _walk_file_search() -> list[dict]:
        """Blocking file-system walk — offloaded via asyncio.to_thread.

        Files and directories are collected into SEPARATE candidate lists, each
        with its own ``max_collect`` allowance. A shared list would let a burst
        of matching directories fill the cap before the files in the same
        directory are even examined, dropping the likely target before the
        file-before-dir tie-break ever runs. Files are also scanned first at each
        level, so under a tight scan budget the file candidates are the ones that
        survive.

        An independent ``_WALK_MAX_DIRS_VISITED`` ceiling bounds how many
        directories the walk descends into, so no request can traverse a whole
        large tree.
        """
        found: dict[str, list[dict]] = {"file": [], "dir": []}
        walked: dict[str, int] = {"file": 0, "dir": 0}
        dirs_visited = 0
        wanted = {"file": want_files, "dir": want_dirs}

        def _done(kind: str) -> bool:
            return not wanted[kind] or walked[kind] >= max_scan or len(found[kind]) >= max_collect

        def _full() -> bool:
            return dirs_visited >= _WALK_MAX_DIRS_VISITED or (_done("file") and _done("dir"))

        def _collect(kind: str, dirpath: str, names: list[str], root_dir: str) -> None:
            """Score and collect one kind of entry from a single directory level."""
            for name in names:
                if _done(kind):
                    return
                walked[kind] += 1
                if kind == "file" and name.startswith("."):
                    continue
                full = os.path.join(dirpath, name)
                score = _fuzzy_score(query, name, os.path.relpath(full, root_dir))
                if score <= 0:
                    continue
                # Resolve symlinks before the sensitivity check so a link into a
                # sensitive tree cannot slip through.
                if is_sensitive_path(os.path.realpath(full)):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                found[kind].append(
                    {
                        "path": full,
                        "name": name,
                        "kind": kind,
                        "size": st.st_size if kind == "file" else 0,
                        "mtime": int(st.st_mtime),
                        "_score": score,
                    }
                )

        for root_dir in safe_roots:
            if _full():
                break
            # macOS: prune the TCC-gated folders. Reaching into them would pop
            # one consent modal PER folder. ``scoped`` means the user NAMED
            # this root (?project= / ?workspace=), so even ``project=$HOME``
            # is deliberate and is searched in full.
            for dirpath, dirnames, filenames in os.walk(root_dir):
                # Bounds the traversal; the per-kind counters stop advancing once
                # their kind is done.
                dirs_visited += 1
                # A dot-prefixed directory (.github, .kiro, .claude) should be
                # OFFERED as a candidate even though we must not DESCEND into it.
                #
                # Build the candidate list (offered AND stat'd) first, then
                # derive the narrower descent list from it. Both drop skip_dirs
                # (.git, node_modules, ...). On an UNSCOPED root the TCC-gated
                # folders (Downloads, Desktop, Library, ... from a $HOME root on
                # macOS) must also be dropped from candidates -- merely offering
                # one means os.stat-ing it, which pops a consent modal; a scoped
                # root is deliberate and is never TCC-pruned, matching the
                # descent rule below. Only the leading-dot rule differs: a dot-
                # dir is a valid candidate but is removed from the descent list.
                base_dirs = [d for d in dirnames if d not in skip_dirs]
                if scoped:
                    candidate_dirs = base_dirs
                else:
                    candidate_dirs = platform_compat.tcc_prune_walk_dirs(
                        root_dir, dirpath, base_dirs
                    )
                dirnames[:] = [d for d in candidate_dirs if not d.startswith(".")]
                # Files first: under a tight scan budget the file candidates are
                # the ones that survive.
                _collect("file", dirpath, filenames, root_dir)
                _collect("dir", dirpath, candidate_dirs, root_dir)
                if _full():
                    break
        return found["file"] + found["dir"]

    # The walk is filesystem work on a caller-supplied root, so it takes a
    # probe slot too: a walk into a dead mount would otherwise pin a
    # default-executor worker exactly as an unbounded stat does.
    try:
        results = await _run_path_probe(_walk_file_search, transfer=True)
    except _PathProbeBusy:
        return _probe_busy_response(resource=f"q={query}", operation="file_search", caller=caller)

    # Sort by score descending, files before dirs on a tie, then shorter name, then recency
    now = time.time()
    results.sort(
        key=lambda r: (
            -r["_score"],
            r["kind"] == "dir",
            len(r["name"]),
            now - r["mtime"],
        )
    )

    # Strip internal scoring field before response
    trimmed = [{k: v for k, v in r.items() if k != "_score"} for r in results[:max_results]]

    _sel().log_api_access(
        caller=caller,
        operation="file_search",
        outcome="allowed",
        resources=f"q={query} kinds={kinds} roots={len(safe_roots)} results={len(trimmed)}",
    )
    return web.json_response(
        {
            "results": trimmed,
            "root": safe_roots[0] if scoped and safe_roots else "",
        }
    )
