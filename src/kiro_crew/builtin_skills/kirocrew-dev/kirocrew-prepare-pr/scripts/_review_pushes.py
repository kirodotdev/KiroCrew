"""The PR-level blocked-push count, read from GitHub with GET calls only.

``_review_contract.py`` loads this module and re-exports every name here, and
callers import them from there. ``same_head`` comes from ``_review_rounds.py``.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re


class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def set_data(self, path, data, *, _mode=0o666):
        return None


def _load_sibling(filename, name):
    """Load a sibling script without cwd, sys.path, or bytecode side effects."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    loader = _NoBytecodeSourceLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot import kirocrew-prepare-pr sibling script: " + path)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _load_owner(stem):
    """Load ``<stem>.py`` beside this script, named under this module's own package."""
    package = __name__.rpartition(".")[0]
    return _load_sibling(stem + ".py", package + "." + stem if package else stem)


_rounds = _load_owner("_review_rounds")
same_head = _rounds.same_head


# The PR-level seam trigger: pushes after a BLOCKED head, read from GitHub alone.
# A disposition marker is bookkeeping an author may never write; a push and the
# check or review that blocked the head before it are always on the PR.
BLOCKED_PUSH_REPEAT = 3
BLOCKED_PUSH_LIMIT = 5
LANE_CHECK_RE = re.compile(
    r"^(?:(?:Opus|GPT) [0-9][0-9.]* Review|Design Review|UX Review|First Principles Review)$"
)
_MAX_TIMELINE_PAGES = 50
_MAX_CHECK_PAGES = 10


def _api_json(run_command, url):
    rc, out, _ = run_command(["gh", "api", url])
    if rc != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def _timeline_events(repo, number, run_command):
    """The PR timeline's events across pages, or None on a failed page or the page cap."""
    events: list = []
    for page in range(1, _MAX_TIMELINE_PAGES + 1):
        batch = _api_json(
            run_command,
            "repos/{}/issues/{}/timeline?per_page=100&page={}".format(repo, number, page),
        )
        if not isinstance(batch, list):
            return None
        events.extend(e for e in batch if isinstance(e, dict))
        if len(batch) < 100:
            return events
    return None


def _pushed_heads(events):
    """([head sha, ...] in push order, {sha with a live CHANGES_REQUESTED review}).

    A force push names its new head. A `committed` event is a new head only when it
    sits on the current head, so the commits a force push replaced, and the older
    commits of a multi-commit push, add nothing. A request for changes is held per
    (reviewer, head) and ends when the same reviewer later APPROVES that head or the
    review is dismissed; a comment does not end it, and another reviewer's request on
    the head stands. Events are read in timeline order, so a request after an approval
    blocks again. A force push event names only the head it created, so when no commit
    event came first, the head it replaced is recovered from a review that precedes the
    first push; a first head with no review and no commit event cannot be seen.
    """
    heads: list = []
    reviews: dict = {}  # (reviewer login, head sha) -> id of the CHANGES_REQUESTED review
    for event in events:
        kind = event.get("event")
        if kind == "head_ref_force_pushed":
            sha = event.get("commit_id")
        elif kind == "committed":
            sha = event.get("sha")
            parents = [p.get("sha") for p in event.get("parents") or [] if isinstance(p, dict)]
            if heads and (not parents or parents[0] != heads[-1]):
                continue
        elif kind == "reviewed":
            who = str((event.get("user") or {}).get("login") or "").lower()
            key = (who, str(event.get("commit_id") or "").lower())
            state = str(event.get("state") or "").lower()
            if key[1] and not heads:
                # The timeline names a force push's NEW head only, so the head it
                # replaced first shows up here: a review before any push names it.
                heads.append(key[1])
            if state == "changes_requested":
                reviews[key] = event.get("id")
            elif state == "approved":
                reviews.pop(key, None)
            continue
        elif kind == "review_dismissed":
            gone = (event.get("dismissed_review") or {}).get("review_id")
            for key in [k for k, rid in reviews.items() if gone is not None and rid == gone]:
                del reviews[key]
            continue
        else:
            continue
        sha = str(sha or "").lower()
        if sha and (not heads or heads[-1] != sha):
            heads.append(sha)
    return heads, {head for _, head in reviews}


def _lane_failed(repo, sha, run_command):
    """True when a review lane's latest check run on ``sha`` failed; None if unreadable.

    Only the lanes named by LANE_CHECK_RE and only `failure`: a cancelled, timed-out
    or skipped run is not a verdict, and a test shard is not a lane. Every page is
    read and, per lane name, only the newest run (highest id, then later in the
    list) speaks, so a green re-run of a failed lane clears the head even if the
    host returned both runs.
    """
    newest: dict = {}
    seen = 0
    for page in range(1, _MAX_CHECK_PAGES + 1):
        data = _api_json(
            run_command,
            "repos/{}/commits/{}/check-runs?filter=latest&per_page=100&page={}".format(
                repo, sha, page
            ),
        )
        runs = data.get("check_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            return None
        for r in runs:
            name = str(r.get("name") or "") if isinstance(r, dict) else ""
            if LANE_CHECK_RE.match(name):
                rank = r.get("id") if isinstance(r.get("id"), int) else 0
                if name not in newest or rank >= newest[name][0]:
                    newest[name] = (rank, r.get("conclusion"))
        seen += len(runs)
        if not runs or seen >= (data.get("total_count") or 0):
            return any(c == "failure" for _, c in newest.values())
    return None


def blocked_push_count(repo, number, run, since="", stop_at=None):
    """Pushes whose previous head was BLOCKED, or None when GitHub cannot be read.

    A head is blocked when a review lane's latest check run on it failed or a
    CHANGES_REQUESTED review names it. A push counts once, only when the head it
    replaced was blocked, so a rebase or a flaky test shard on a green head adds
    nothing. GET only; ``run(args)`` returns (rc, stdout, stderr), so a test needs
    no network. ``since`` is a head sha (any 7-40 character spelling): only pushes
    that land after it count, which restarts the count after a recorded
    `resolution:`. A head the PR returned to by force push is taken at its LAST
    visit, so a reset never brings back the pushes that came before it.
    ``stop_at`` returns early once the count reaches it.
    """
    events = _timeline_events(repo, number, run) if repo else None
    if events is None:
        return None
    heads, reviewed = _pushed_heads(events)
    visits = [i + 1 for i, h in enumerate(heads) if since and same_head(h, since)]
    first = visits[-1] if visits else 0
    count = 0
    for i in range(max(first, 1), len(heads)):
        sha = heads[i - 1]
        blocked = sha in reviewed or _lane_failed(repo, sha, run)
        if blocked is None:
            return None
        count += 1 if blocked else 0
        if stop_at and count >= stop_at:
            break
    return count
