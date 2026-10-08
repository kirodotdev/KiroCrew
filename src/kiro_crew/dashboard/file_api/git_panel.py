"""The Git panel: bounded git runs and ``GET /api/project/git/status`` and ``/log``."""

from __future__ import annotations

import asyncio
import contextlib
import os
import posixpath
import stat as _stat_mod
import subprocess
import threading
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _GIT_FILTER_KEY_RE,
        _GIT_PROBE_STDERR_CAP,
        ConfigHookScanError,
        DashboardState,
        _match_known_project_for,
        _redact_project_path,
        _sel,
        _slot_git_repo_snapshot,
        config_hook_disable_args_sandboxed,
        is_sensitive_path,
        popen_limited,
        redact,
        redact_path_segments,
        sandboxed_spawn_argv,
        worktree_probe_failure_is_empty_scope,
    )


# Ceiling on captured git stdout for the Git-panel endpoints. Status output is
# repo-content-sized (an agent-authored repo can make it arbitrarily large) and
# these endpoints are POLLED by the dashboard, so an unbounded
# ``capture_output=True`` buffer is a memory-DoS surface. 8 MB comfortably
# holds the 500-file slice the responses return while bounding the worst case.
_GIT_PANEL_STDOUT_CAP = 8 * 1024 * 1024


def _project_directory_absent(path: str) -> bool:
    """Return whether *path* is missing or is not a directory.

    A permission or other operational failure is not absence: the caller keeps
    the original Git failure and returns 503 instead of claiming ``repo: false``.
    """
    try:
        return not _stat_mod.S_ISDIR(os.stat(path).st_mode)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return True
    except OSError:
        return False


def _probe_git_dir(base: str, env: dict) -> tuple[int, str]:
    """Ask sandboxed Git whether *base* belongs to a repository.

    ``rev-parse --git-dir`` owns repository discovery. Stdout is unused and
    discarded. Stderr is hard-capped before decoding so a repository cannot make
    this polling endpoint buffer an unbounded diagnostic.
    """
    rc, stderr, truncated = _run_git_bounded(
        ["git", "rev-parse", "--git-dir"],
        cwd=base,
        env=env,
        timeout=5,
        cap=_GIT_PROBE_STDERR_CAP,
        capture="stderr",
    )
    if truncated:
        return -9, ""
    return rc, stderr


def _is_not_a_repo_verdict(probe_stderr: str) -> bool:
    """True when a failed :func:`_probe_git_dir` is Git's own absence verdict.

    This is the ONE classification contract the status and log routes share:
    Git's English ``fatal: not a git repository`` line (the probe runs with
    ``LC_ALL=C``) is confirmed absence and answers ``repo: false``. Every other
    nonzero probe -- sandbox refusal, spawn failure, dubious ownership,
    permission failure, timeout, kill, corrupt metadata -- is an operational
    outage and answers 503, because an empty listing or an empty commit list is
    exactly what a clean or unborn repository legitimately returns, so spelling
    an outage that way is indistinguishable from a healthy answer.
    """
    return any(
        line.lstrip().startswith("fatal: not a git repository")
        for line in probe_stderr.lower().splitlines()
    )


def _run_git_bounded(
    args: list[str],
    cwd: str,
    env: dict,
    timeout: float,
    cap: int = _GIT_PANEL_STDOUT_CAP,
    decode_errors: str = "replace",
    capture: str = "stdout",
) -> tuple[int, str, bool]:
    """Run git capturing at most ``cap`` bytes from one output stream.

    ``capture`` is ``"stdout"`` or ``"stderr"``; the other stream is discarded.
    Returns ``(returncode, captured_text, truncated)``. When the process
    outlives ``timeout`` or overflows ``cap`` it is killed and reported as
    truncated with a nonzero returncode -- callers already treat nonzero as
    "no data", which is the safe degraded answer for a pathological repo.

    ``decode_errors`` is ``"replace"`` for display-bound output. A caller
    whose output names a filesystem path fed to an ``os`` call passes
    ``"surrogateescape"`` so non-UTF-8 path bytes round-trip through
    ``os.fsencode`` (see :func:`kiro_crew.subprocess_utf8.utf8_path_stdout`).
    """
    if capture not in ("stdout", "stderr"):
        raise ValueError(f"unsupported capture stream: {capture}")

    # OS-sandbox + credential-scrubbed env chokepoint (worktree.py's _run_git
    # pattern): the repository content is agent-influenced, and git filter
    # drivers (filter.<name>.clean/process from .git/config) can run during
    # status re-hashing -- ``-c`` flags cannot neutralize arbitrary driver
    # names, so isolation, not argument hygiene, is the containment. Fail
    # CLOSED: no sandbox backend means no data, not an unisolated spawn.
    cleanup: str | None = None
    try:
        argv, env, cleanup = sandboxed_spawn_argv(args, mode="strict", env=env)
    except RuntimeError:
        return -9, "", False
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        try:
            proc = popen_limited(
                argv,
                cwd=cwd,
                env=env,
                stdout=(subprocess.PIPE if capture == "stdout" else subprocess.DEVNULL),
                stderr=(subprocess.PIPE if capture == "stderr" else subprocess.DEVNULL),
            )
        except OSError:
            # The cwd (project dir) can vanish between the handler's isdir
            # check and this spawn, and the git binary itself can be absent.
            # Both are "no data", never a 500 out of a polling endpoint.
            return -9, "", False
        buf = bytearray()
        overflow = False

        def _drain() -> None:
            nonlocal overflow
            stream = proc.stdout if capture == "stdout" else proc.stderr
            assert stream is not None
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                if len(buf) + len(chunk) > cap:
                    buf.extend(chunk[: cap - len(buf)])
                    overflow = True
                    return
                buf.extend(chunk)

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()
        reader.join(timeout)
        timed_out = reader.is_alive()
        if timed_out or overflow:
            proc.kill()
            reader.join(5)
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = -9
        if timed_out or overflow:
            rc = rc or -9
        return rc, bytes(buf).decode("utf-8", decode_errors), timed_out or overflow
    finally:
        if cleanup:
            with contextlib.suppress(OSError):
                os.unlink(cleanup)


def _porcelain_unquote(path: str) -> str:
    """Decode a C-quoted porcelain v1 path (``"foo \\"bar\\""`` -> ``foo "bar"``).

    Porcelain v1 wraps a path in double quotes and backslash-escapes it when it
    contains quotes, backslashes, or control characters (``core.quotePath=false``
    already keeps plain non-ASCII raw). Returning the quoted display form would
    point the row -- and a subsequent open/save -- at a file that does not
    exist. Decode failures fall back to the raw string rather than raising.
    """
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    body = path[1:-1]
    out = bytearray()
    i = 0
    escapes = {"n": 10, "t": 9, "r": 13, "a": 7, "b": 8, "f": 12, "v": 11, "\\": 92, '"': 34}
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.extend(ch.encode("utf-8"))
            i += 1
            continue
        if i + 1 >= len(body):
            return path  # dangling escape: not valid quoting, keep raw
        nxt = body[i + 1]
        if nxt in escapes:
            out.append(escapes[nxt])
            i += 2
        elif nxt.isdigit() and i + 3 < len(body) + 1 and body[i + 1 : i + 4].isdigit():
            out.append(int(body[i + 1 : i + 4], 8) & 0xFF)
            i += 4
        else:
            return path
    return out.decode("utf-8", "replace")


def _worktree_probe_failure_is_empty_scope(git_cmd: list[str], base: str, env: dict) -> bool:
    """True when a failed ``--worktree`` probe hit the empty scope git creates lazily.

    Called only AFTER ``git config --worktree ...`` exited non-zero — never to
    gate whether that probe runs. Resolves ``$GIT_DIR`` through this handler's
    own bounded runner and feeds it to
    :func:`kiro_crew.git_worktree_scope.worktree_probe_failure_is_empty_scope`,
    the one shared classification all four filter-driver guards use. See that
    module's docstring for why the probe-first order is the contract.
    """
    # surrogateescape, not the display default: this answer is handed to the
    # classifier's ``os.lstat``, so a non-UTF-8 byte in the real path must
    # survive as a PEP 383 surrogate ``os.fsencode`` restores byte-exactly --
    # a U+FFFD from ``"replace"`` would miss an existing ``config.worktree``
    # and clear a scope git still reads.
    gitdir_rc, gitdir_out, _ = _run_git_bounded(
        [*git_cmd, "rev-parse", "--absolute-git-dir"],
        cwd=base,
        env=env,
        timeout=5,
        decode_errors="surrogateescape",
    )
    return worktree_probe_failure_is_empty_scope(gitdir_out if gitdir_rc == 0 else "", base)


def _repo_filter_refusal_cause(git_cmd: list[str], base: str, env: dict) -> str:
    """Why this repo's checks are refused: ``"declared"``, ``"unreadable"``, or ``""``.

    ``"declared"`` means repo-supplied config names a content-filter driver.
    ``"unreadable"`` means the probe could not prove one absent, so nothing is
    known about a driver at all. Both refuse -- neither can be proven safe --
    but they are different facts and the reader is told the one that applies:
    collapsing them onto one message made the common case (an LFS repo) read as
    a disjunction the caller had already resolved.

    Mirrors ``worktree.py::_checkout_filter``: drivers can only come from a
    config file the repository supplies — ``--local`` (``.git/config``) and,
    when ``extensions.worktreeConfig`` is on, ``--worktree``
    (``$GIT_DIR/config.worktree``). The worktree scope is PROBED FIRST and a
    failure classified AFTERWARDS: git creates ``config.worktree`` lazily, so
    a probe that failed because the file is genuinely absent is the empty
    scope, not an unreadable one — while an existence pre-check would drop
    the scope on a stale fact and never look at a file git goes on to read.
    ``--includes`` is mandatory: a specific-scope
    query defaults include-following OFF, so a driver reached through
    ``include.path`` would be invisible to the probe yet still execute.
    Global/system config is deliberately not probed (the user's own machine
    setup, e.g. ``git lfs install``, is not repository-supplied). Any other
    probe failure refuses: an unreadable scope cannot be proven filter-free.
    The probe itself is safe — ``git config`` reads files and never runs
    drivers.
    """
    scopes = ["--local"]
    # --local is load-bearing: git takes the extension from the REPO config
    # only, while a merged read lets a worktree-scoped
    # extensions.worktreeConfig=false win the chain and hide the very scope it
    # lives in. --bool folds every git-true spelling (yes/on/1/valueless).
    ext_rc, ext_out, _ = _run_git_bounded(
        [
            *git_cmd,
            "config",
            "--local",
            "--includes",
            "--bool",
            "--get",
            "extensions.worktreeConfig",
        ],
        cwd=base,
        env=env,
        timeout=5,
    )
    if ext_rc == 0 and ext_out.strip() == "true":
        scopes.append("--worktree")
    for scope in scopes:
        rc, out, _ = _run_git_bounded(
            [*git_cmd, "config", scope, "--includes", "--name-only", "--list"],
            cwd=base,
            env=env,
            timeout=5,
        )
        if rc != 0:
            if scope == "--worktree" and _worktree_probe_failure_is_empty_scope(git_cmd, base, env):
                continue
            return "unreadable"
        for key in out.splitlines():
            if _GIT_FILTER_KEY_RE.match(key.strip()):
                return "declared"
    return ""


async def api_project_git_status(request: web.Request) -> web.Response:
    """GET /api/project/git/status?path=... - working tree status for a project dir.

    Returns staged/unstaged/untracked files with per-file line-change counts.
    Path must match a known project directory or a git root a session's tool
    calls touched (``_slot_git_repo_snapshot``).
    """
    state: DashboardState = request.app["state"]
    caller = request.get("user", "dashboard")
    raw = request.query.get("path", "").strip()
    if not raw:
        return web.json_response({"error": "path required", "code": "path_required"}, status=400)
    allowed = _slot_git_repo_snapshot(state, request.get("app", ""))
    project = await asyncio.to_thread(_match_known_project_for, allowed, raw)
    if project is None:
        _sel().log_api_access(
            caller=caller,
            operation="project_git_status",
            outcome="denied",
            resources=raw,
            error="not a known project directory",
        )
        return web.json_response(
            {"error": "Unknown project directory", "code": "unknown_project_dir"}, status=403
        )

    base = await asyncio.to_thread(lambda: os.path.realpath(os.path.expanduser(project)))
    # Both probes stat the filesystem (a stalled network mount would block the
    # event loop), so they run in a worker thread like the realpath above.
    if await asyncio.to_thread(is_sensitive_path, base):
        _sel().log_api_access(
            caller=caller,
            operation="project_git_status",
            outcome="denied",
            resources=base,
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied", "code": "access_denied"}, status=403)
    # Log the allow decision here (not after _run) so every authorized access
    # is audited, including the not-a-directory / not-a-repo early answers.
    _sel().log_api_access(
        caller=caller, operation="project_git_status", outcome="allowed", resources=base
    )
    if not await asyncio.to_thread(os.path.isdir, base):
        return web.json_response({"repo": False, "files": []})

    def _run() -> dict:
        _git_cmd = [
            "git",
            "-c",
            "diff.textconv=",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=",
            # Repo-local .gitattributes is still consulted despite the
            # attributesFile override, so keep driver escape hatches shut and
            # emit non-ASCII paths raw (UTF-8) instead of C-quoted so the
            # panel can open them.
            "-c",
            "core.quotePath=false",
        ]
        _env = {
            **os.environ,
            "GIT_ATTR_NOSYSTEM": "1",
            "LC_ALL": "C",
            "LANGUAGE": "C",
        }

        # Git owns repository discovery inside the sandbox. Its English
        # not-a-repository verdict is confirmed absence. Every other probe
        # failure remains an operational outage unless the directory vanished.
        probe_rc, probe_err = _probe_git_dir(base, _env)
        if probe_rc != 0:
            if _is_not_a_repo_verdict(probe_err):
                return {"repo": False, "files": []}
            return {"_status_unavailable": True}

        # A hook defined in config (`hook.<name>.command`, git 2.54+) is not reached by
        # `core.hooksPath`, and `status` runs `post-index-change` when it refreshes the
        # index. Each name git can see is disabled. See `kiro_crew.git_config_hooks`.
        try:
            _git_cmd = [
                *_git_cmd,
                *config_hook_disable_args_sandboxed(
                    base, spawn_argv=sandboxed_spawn_argv, mode="strict", env=_env
                ),
            ]
        except (ConfigHookScanError, RuntimeError):
            return {"_status_unavailable": True}

        # ``rev-parse --git-dir`` proves this is a repository, not that HEAD is
        # usable. Keep this separate from status: with
        # ``HEAD = ref: refs/heads/bad.lock``, the exact
        # ``git status --porcelain=v1 -b`` command exits 0 with bogus output
        # ``## A  a.txt``, while ``git branch --show-current`` exits 128. The
        # ``test_lock_suffix_head_ref_matches_git_probe`` regression pins this
        # state. Detached and unborn repositories both return success.
        head_rc, _head_out, _ = _run_git_bounded(
            [*_git_cmd, "branch", "--show-current"],
            cwd=base,
            env=_env,
            timeout=5,
        )
        if head_rc != 0:
            return {"_status_unavailable": True}

        # Refuse repos whose own config names a content-filter driver: status
        # re-hashes modified files through ``filter.<name>.clean``, which would
        # execute that program on every 5s poll.
        #
        # The refusal reports UNAVAILABLE, never an empty file list.
        # ``{"repo": True, "files": []}`` is what a genuinely clean repository
        # returns, so a refusal wearing that shape is indistinguishable from
        # health: the panel draws its green "clean" pill over a working tree it
        # holds no status for, on any repo configured by ``git lfs install
        # --local``, on every poll. A panel whose one job is surfacing
        # uncommitted changes is more wrong when it claims none than when it
        # admits it has no answer. The guard also refuses when its own config
        # probe cannot prove a driver absent, and that state is likewise unknown
        # rather than clean -- it answers a distinct cause so the reader is told
        # which of the two actually happened.
        #
        # It carries its OWN code, distinct from the outage code its four
        # siblings use. This condition is not an outage: it is a standing policy
        # decision with a knowable cause. For the `declared` cause it is also
        # permanent rather than transient -- no retry clears it while that
        # config stands -- which is why only the declared copy promises
        # permanence and only the declared cause makes the panel's refresh
        # control inert. The `unreadable` cause can clear on its own, so it
        # promises nothing about permanence. Spelling either as an outage
        # would tell an LFS user their repository is broken, every poll, forever.
        _status_refusal = _repo_filter_refusal_cause(_git_cmd, base, _env)
        if _status_refusal:
            return {"_status_filter_refused": _status_refusal}

        # Get repo root and branch info
        root_rc, root_out, _ = _run_git_bounded(
            [*_git_cmd, "rev-parse", "--show-toplevel"],
            cwd=base,
            env=_env,
            timeout=5,
        )
        repo_root = root_out.strip()
        if root_rc != 0 or not repo_root:
            return {"_status_unavailable": True}

        # Branch + ahead/behind via status -b
        status_rc, status_out, _ = _run_git_bounded(
            [*_git_cmd, "status", "--porcelain=v1", "-b", "--untracked-files=all"],
            cwd=base,
            env=_env,
            timeout=10,
        )
        if status_rc != 0:
            return {"_status_unavailable": True}

        lines = status_out.splitlines()
        branch = None
        ahead = 0
        behind = 0

        # Parse the branch header line: ## branch...tracking [ahead N, behind M]
        if lines and lines[0].startswith("## "):
            header = lines[0][3:]
            # Extract branch name (before ... or end)
            dot_idx = header.find("...")
            if dot_idx >= 0:
                branch = header[:dot_idx]
            else:
                # Could be "## branch" or "## No commits yet on branch"
                if header.startswith("No commits yet on "):
                    branch = header[len("No commits yet on ") :]
                else:
                    branch = header.split()[0] if header else None
            # Parse ahead/behind
            bracket_idx = header.find("[")
            if bracket_idx >= 0:
                info = header[bracket_idx + 1 : header.find("]")]
                for part in info.split(","):
                    part = part.strip()
                    if part.startswith("ahead "):
                        try:
                            ahead = int(part[6:])
                        except ValueError:
                            pass
                    elif part.startswith("behind "):
                        try:
                            behind = int(part[7:])
                        except ValueError:
                            pass

        # Parse file entries
        files: list[dict] = []
        for line in lines[1:]:
            if len(line) < 4:
                continue
            x = line[0]  # index status
            y = line[1]  # worktree status
            filepath = line[3:]

            # Rename entries quote each side separately ("old" -> "new"), so
            # split BEFORE unquoting would see the arrow inside quotes; the
            # porcelain arrow separator is never itself quoted, so splitting
            # first and unquoting each side is correct for both forms.

            # Handle renames/copies: "R  old -> new". Gate on the status
            # letters -- a plain modified file legitimately named
            # "foo -> bar" must NOT be split, or its row would point at an
            # unrelated file and clicking it edits the wrong one.
            if (x in ("R", "C") or y in ("R", "C")) and " -> " in filepath:
                filepath = filepath.split(" -> ", 1)[1]
            filepath = _porcelain_unquote(filepath)

            # Determine status code and staged flag
            if x == "?" and y == "?":
                files.append({"path": filepath, "status": "?", "staged": False})
            elif x == "!" and y == "!":
                continue  # ignored
            else:
                # If X is non-space/non-?, there's a staged change
                if x not in (" ", "?", "!"):
                    files.append({"path": filepath, "status": x, "staged": True})
                # If Y is non-space, there's an unstaged change
                if y not in (" ", "?", "!"):
                    files.append({"path": filepath, "status": y, "staged": False})

        # Merge numstat for line counts (staged + unstaged vs HEAD)
        try:
            numstat_rc, numstat_out, _ = _run_git_bounded(
                [*_git_cmd, "diff", "--numstat", "--no-textconv", "--no-ext-diff", "HEAD"],
                cwd=base,
                env=_env,
                timeout=10,
            )
            if numstat_rc == 0:
                stats: dict[str, tuple[int | None, int | None]] = {}
                for ns_line in numstat_out.splitlines():
                    parts = ns_line.split("\t", 2)
                    if len(parts) == 3:
                        add_s, del_s, ns_path = parts
                        adds = int(add_s) if add_s != "-" else None
                        dels = int(del_s) if del_s != "-" else None
                        # numstat C-quotes the same class of paths status does;
                        # unquote so the merge key matches the parsed rows.
                        stats[_porcelain_unquote(ns_path)] = (adds, dels)
                for f in files:
                    if f["path"] in stats:
                        adds, dels = stats[f["path"]]
                        if adds is not None:
                            f["additions"] = adds
                        if dels is not None:
                            f["deletions"] = dels
        except FileNotFoundError:
            pass

        result: dict = {"repo": True, "repoRoot": repo_root, "files": files[:500]}
        # Status paths are repo-root-relative; when the project directory sits
        # below the repo root, every path starts with this prefix.
        rel = os.path.relpath(base, repo_root)
        result["_prefix"] = (
            "" if rel in (".", "") or rel.startswith("..") else rel.replace(os.sep, posixpath.sep)
        )
        if len(files) > 500:
            result["truncated"] = True
        if branch:
            result["branch"] = branch
        if ahead:
            result["ahead"] = ahead
        if behind:
            result["behind"] = behind
        return result

    result = await asyncio.to_thread(_run)
    # A project directory can vanish after the initial directory check and
    # surface from process creation as ENOENT/ENOTDIR (FileNotFoundError or
    # NotADirectoryError), including Windows errors 2, 3, and 267. Re-check the
    # authoritative path once here so every spawn/status stage has the same
    # classification: absence is a normal no-repository result; only a failure
    # while the directory still exists is an operational outage.
    if await asyncio.to_thread(_project_directory_absent, base):
        return web.json_response({"repo": False, "files": []})
    _status_refusal = result.pop("_status_filter_refused", "")
    if _status_refusal:
        return web.json_response(
            {
                # One cause per body. "declares a filter driver, or could not be
                # read" made every LFS repo -- the common case by far -- read a
                # disjunction this function had already resolved.
                "error": (
                    "Checks are off for this repository: its Git config declares a "
                    "filter driver, so they are refused by policy."
                    if _status_refusal == "declared"
                    else "Checks are off for this repository: its Git config could not be "
                    "read, so they are refused by policy."
                ),
                "code": "git_status_filter_refused",
                "cause": _status_refusal,
            },
            status=503,
        )
    if result.pop("_status_unavailable", False):
        return web.json_response(
            {
                "error": "Couldn't read the repository status.",
                "code": "git_status_unavailable",
            },
            status=503,
        )
    # Egress redaction: repo content (paths, branch label, repo root) is
    # agent-influenceable and this response body is rendered by the dashboard,
    # so it goes through the same redaction as api_project_git. Normal values
    # pass through unchanged. ``repoRoot`` is an absolute path and takes the same
    # path-aware wrapper that endpoint uses, so the two stay consistent; the
    # branch label and the repo-relative file paths keep the bare detector.
    if result.get("repoRoot"):
        result["repoRoot"] = _redact_project_path(result["repoRoot"])
    if result.get("branch"):
        result["branch"] = redact(result["branch"])
    # Redact each file path with redact_path_segments over the same
    # context-aware redact(): each path is redacted segment-wise, and every
    # redacted segment carries an opaque label keyed per gateway process, so two
    # genuinely-different paths that collapse to the same tag stay two entries
    # instead of one placeholder -- the whole-string redact() is still the
    # floor, never less. The label is stable across responses within this
    # process, which is what lets the dashboard join this listing with the
    # tree listing by path.
    # Then drop entries that duplicate an earlier one (preserving order and
    # first occurrence): this is the fallback for a collision the helper does
    # not separate. This list feeds GitPanel, which keys its rows on
    # `${path}:${staged}` and takes its file total from files.length, so a
    # collision would render two indistinguishable rows under one React key and
    # overstate the count. (It cannot reach @pierre/trees as a duplicate the
    # way api_project_tree's list can: the tree's "changed" mode already
    # collapses status entries by path before handing them over.) The
    # files[:500] cap was already applied to the raw listing above, so this
    # only removes collisions.
    #
    # The key is (path, status, staged), NOT path alone: one file with both
    # staged and unstaged changes ("MM", "AM", "MD") legitimately yields two
    # entries sharing a path but differing in status/staged, and GitPanel
    # renders them as separate rows (identical originals redact identically, so
    # the pair still shares its path). Keying on path alone would drop the
    # unstaged lane and undercount the file total. A real redaction collision
    # has an identical tuple, so it still collapses.
    # The project directory's own repo-relative prefix is redacted the same
    # way the tree root and repoRoot are (whole-string, unlabelled), so a
    # credential-shaped project directory reads identically on both sides of
    # the dashboard join; only the part beneath it is labelled per segment.
    prefix = str(result.pop("_prefix", "") or "")
    prefix_slash = posixpath.join(prefix, "") if prefix else ""
    redacted_prefix = redact(prefix) if prefix else ""

    def _redact_status_path(path: str) -> str:
        if prefix_slash and path.startswith(prefix_slash):
            below = redact_path_segments(path[len(prefix_slash) :], redact)
            joined = posixpath.join(redacted_prefix, below)
            # Same floor redact_path_segments applies to its own assembly: the
            # prefix and the part beneath it are redacted separately, so a
            # token that straddles the joining slash is matched by neither
            # half. The joined result must be a fixed point of the redactor;
            # when it is not, the whole-path result wins, exactly as it does
            # inside the helper.
            return joined if redact(joined) == joined else redact(path)
        if prefix and path == prefix:
            return redacted_prefix
        return redact_path_segments(path, redact)

    deduped_files: list[dict] = []
    seen_keys: set[tuple[str, str | None, bool | None]] = set()
    files = result.get("files", [])
    for f in files:
        f["path"] = _redact_status_path(f["path"])
        key = (f["path"], f.get("status"), f.get("staged"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped_files.append(f)
    if "files" in result:
        result["files"] = deduped_files
    return web.json_response(result)


async def api_project_git_log(request: web.Request) -> web.Response:
    """GET /api/project/git/log?path=...&limit=N - recent commit log for a project dir.

    Returns short sha, subject, author, date (ISO), and isHead flag.
    Path must match a known project directory or a git root a session's tool
    calls touched (``_slot_git_repo_snapshot``).
    """
    state: DashboardState = request.app["state"]
    caller = request.get("user", "dashboard")
    raw = request.query.get("path", "").strip()
    if not raw:
        return web.json_response({"error": "path required", "code": "path_required"}, status=400)

    limit_s = request.query.get("limit", "20")
    try:
        limit = max(1, min(100, int(limit_s)))
    except (ValueError, TypeError):
        limit = 20

    allowed = _slot_git_repo_snapshot(state, request.get("app", ""))
    project = await asyncio.to_thread(_match_known_project_for, allowed, raw)
    if project is None:
        _sel().log_api_access(
            caller=caller,
            operation="project_git_log",
            outcome="denied",
            resources=raw,
            error="not a known project directory",
        )
        return web.json_response(
            {"error": "Unknown project directory", "code": "unknown_project_dir"}, status=403
        )

    base = await asyncio.to_thread(lambda: os.path.realpath(os.path.expanduser(project)))
    # Both probes stat the filesystem (a stalled network mount would block the
    # event loop), so they run in a worker thread like the realpath above.
    if await asyncio.to_thread(is_sensitive_path, base):
        _sel().log_api_access(
            caller=caller,
            operation="project_git_log",
            outcome="denied",
            resources=base,
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied", "code": "access_denied"}, status=403)
    # Log the allow decision here (not after _run) so every authorized access
    # is audited, including the not-a-directory / not-a-repo early answers.
    _sel().log_api_access(
        caller=caller, operation="project_git_log", outcome="allowed", resources=base
    )
    if not await asyncio.to_thread(os.path.isdir, base):
        return web.json_response({"repo": False, "commits": []})

    def _run() -> dict:
        _git_cmd = [
            "git",
            "-c",
            "diff.textconv=",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=",
            # Repo-local .gitattributes is still consulted despite the
            # attributesFile override, so keep driver escape hatches shut and
            # emit non-ASCII paths raw (UTF-8) instead of C-quoted so the
            # panel can open them.
            "-c",
            "core.quotePath=false",
        ]
        _env = {
            **os.environ,
            "GIT_ATTR_NOSYSTEM": "1",
            # The probe's verdict match reads Git's English diagnostic.
            "LC_ALL": "C",
            "LANGUAGE": "C",
        }

        # Same discovery boundary as the status route: Git's not-a-repository
        # verdict is confirmed absence; any other probe failure is an outage,
        # not an empty history.
        probe_rc, probe_err = _probe_git_dir(base, _env)
        if probe_rc != 0:
            if _is_not_a_repo_verdict(probe_err):
                return {"repo": False, "commits": []}
            return {"_log_unavailable": True}

        # Same config-defined hook disable as the status route.
        try:
            _git_cmd = [
                *_git_cmd,
                *config_hook_disable_args_sandboxed(
                    base, spawn_argv=sandboxed_spawn_argv, mode="strict", env=_env
                ),
            ]
        except (ConfigHookScanError, RuntimeError):
            return {"_log_unavailable": True}

        # Same filter-driver refusal as the status handler (defense in depth:
        # ``git log`` does not run clean filters, but one uniform invariant --
        # no git subcommand runs against a repo that names a driver -- is
        # auditable; per-subcommand carve-outs are not). Reported as
        # unavailable for the same reason status is: an empty commit list is
        # what a brand-new repository legitimately returns, so a refusal
        # spelled that way is indistinguishable from "no history yet". It
        # carries the filter-specific code, since the cause is the repo's own
        # config rather than an outage.
        _log_refusal = _repo_filter_refusal_cause(_git_cmd, base, _env)
        if _log_refusal:
            return {"_log_filter_refused": _log_refusal}

        # Get HEAD sha for isHead marking
        head_rc, head_out, _ = _run_git_bounded(
            [*_git_cmd, "rev-parse", "--short", "HEAD"],
            cwd=base,
            env=_env,
            timeout=5,
        )
        head_sha = head_out.strip() if head_rc == 0 else ""

        # Separator unlikely in commit data
        sep = "\x1f"
        fmt = f"%h{sep}%s{sep}%an{sep}%aI"
        log_rc, log_out, _ = _run_git_bounded(
            [*_git_cmd, "log", f"--pretty=format:{fmt}", f"-{limit}"],
            cwd=base,
            env=_env,
            timeout=15,
        )
        if log_rc != 0:
            return {"repo": True, "commits": []}

        commits: list[dict] = []
        for line in log_out.splitlines():
            parts = line.split(sep, 3)
            if len(parts) < 4:
                continue
            sha, message, author, date = parts
            commits.append(
                {
                    "sha": sha,
                    "message": message,
                    "author": author,
                    "date": date,
                    "isHead": sha == head_sha,
                }
            )
        return {"repo": True, "commits": commits}

    result = await asyncio.to_thread(_run)
    # Same vanished-directory re-check as the status route: a project directory
    # deleted between the isdir gate and the spawn is absence, not an outage.
    if await asyncio.to_thread(_project_directory_absent, base):
        return web.json_response({"repo": False, "commits": []})
    _log_refusal = result.pop("_log_filter_refused", "")
    if _log_refusal:
        return web.json_response(
            {
                # One cause per body, same as the status route.
                "error": (
                    "History is off for this repository: its Git config declares a "
                    "filter driver, so this check is refused by policy."
                    if _log_refusal == "declared"
                    else "History is off for this repository: its Git config could not be "
                    "read, so this check is refused by policy."
                ),
                "code": "git_log_filter_refused",
                "cause": _log_refusal,
            },
            status=503,
        )
    if result.pop("_log_unavailable", False):
        return web.json_response(
            {
                "error": "Couldn't read the commit history.",
                "code": "git_log_unavailable",
            },
            status=503,
        )
    # Egress redaction: commit subjects and author names are repo content the
    # agent can author, and this body is rendered by the dashboard.
    for c in result.get("commits", []):
        c["message"] = redact(c["message"])
        c["author"] = redact(c["author"])
    return web.json_response(result)
