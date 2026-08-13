#!/usr/bin/env python3
"""five_whys.py — deterministic mechanical core for the five-whys research mode.

The event log (append-only JSONL) is the single source of truth. This script owns
the mechanics that must NOT drift: appending validated events, allocating node ids,
referential integrity, and folding the log into projections (tree / frontier /
current / report). All judgment (what to ask, whether an answer is a real cause,
when a thread is done) stays with the skill/LLM.

Stdlib only, Python 3.8+. No network, no credentials, never rewrites history.

Free text (a question, answer, note, citation, title) is never passed as a shell
argument -- a value containing ``$(...)``, backticks or quotes would be executed or
mangled by the shell. Instead, pass ``--stdin-json`` and feed a single JSON object
of the free-text fields on stdin; the script reads them verbatim.

Usage:
  five_whys.py ask     <log> --parent ID|root --stage S (--q "..." | --stdin-json) [--origin proposed|user]
  five_whys.py answer  <log> --id ID (--a "..." | --stdin-json) [--source ...]
  five_whys.py focus   <log> --id ID
  five_whys.py done    <log> --id ID [--kind root|takeaway] [--note "..." | --stdin-json]
  five_whys.py discuss <log> --anchor ID (--text "..." | --stdin-json)
  five_whys.py prune   <log> --id ID [--reason "..." | --stdin-json]
  five_whys.py event   <log> (--json '{...}' | --stdin-json)   # plugin custom types
  five_whys.py view    <log>                                   # current path + open-branch count
  five_whys.py tree    <log>
  five_whys.py frontier<log>
  five_whys.py report  <log> [--title "..." | --stdin-json]    # folds to markdown
  five_whys.py validate<log>                                   # schema+integrity gate (exit 0/1)

  Free-text via stdin, e.g.:  five_whys.py ask <log> --parent 1 --stage what --stdin-json < fields.json
  where fields.json is {"q": "..."} written with a file tool (never interpolated).
"""

import argparse
import contextlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

CORE_TYPES = {"ask", "answer", "focus", "done", "discuss", "prune"}
STAGES = {"what", "example", "why-not", "benefits", "costs"}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reject_symlink(path):
    """Refuse to read or write through a symlink at the final path component.

    The log path is caller-supplied; a symlink planted there could make an
    append follow the link and corrupt an unintended target file.
    """
    if Path(path).is_symlink():
        raise SystemExit("refusing to follow symlink: %s" % path)


def _apply_stdin_json(a, *fields):
    """Override the named free-text fields from ONE JSON object read from stdin.

    All of a command's free text arrives in a single ``--stdin-json`` object
    (``{"a": "...", "source": "..."}``), so no free text is ever a shell argument
    and multiple untrusted fields share one read of the pipe (a per-field stdin
    sentinel could not -- a second read would drain empty). Only the named fields
    are consulted; each present value must be a string.
    """
    if not getattr(a, "stdin_json", False):
        return
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        raise SystemExit("invalid --stdin-json payload: %s" % e)
    if not isinstance(payload, dict):
        raise SystemExit("--stdin-json payload must be a JSON object")
    for f in fields:
        if f in payload:
            v = payload[f]
            if not isinstance(v, str):
                raise SystemExit("--stdin-json field %r must be a string" % f)
            setattr(a, f, v)


def _flock(fd, acquire):
    """Acquire/release an OS advisory lock on ``fd`` (auto-released on exit)."""
    if sys.platform == "win32":
        # msvcrt.locking needs a real byte range to lock; a freshly created
        # lock file is empty, so seed one byte before the first acquire or the
        # lock (and thus the first mutation) would fail.
        if acquire and os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX if acquire else fcntl.LOCK_UN)


@contextlib.contextmanager
def _log_lock(path):
    """Serialize a whole read -> allocate -> append transaction on one log.

    Two `ask` commands racing on the same log would load identical state,
    allocate the same node id, and append duplicates -- corrupting the tree and
    breaking validation. An advisory lock on a sibling ``<log>.lock`` makes each
    mutating command's load+append atomic across processes; the lock releases
    automatically when the process exits, so there is no stale lock to reap.
    """
    lock_path = str(path) + ".lock"
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink(lock_path)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _flock(fd, True)
        yield
    finally:
        try:
            _flock(fd, False)
        finally:
            os.close(fd)


def _load(path):
    """Return list of event dicts. Missing file => empty log.

    Newline is the commit marker: a final line with no terminating newline is an
    uncommitted partial record (a prior append cut short by disk-full or a kill),
    so it is dropped rather than parsed -- a torn tail can never brick the log.
    """
    p = Path(path)
    _reject_symlink(p)
    if not p.exists():
        return []
    raw = p.read_text(encoding="utf-8")
    # Split ONLY on the "\n" that _append joins records with -- NOT str.splitlines(),
    # which also breaks on U+2028/U+2029/U+0085. json.dumps(ensure_ascii=False)
    # leaves those separators raw inside a free-text field, so splitlines() would
    # fragment such a record into invalid-JSON pieces and brick the log.
    lines = raw.split("\n")
    if lines and not raw.endswith("\n"):
        lines = lines[:-1]
    events = []
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as e:
            raise SystemExit("corrupt log at line %d: %s" % (i, e))
        events.append(ev)
    return events


def _repair_partial_tail(p):
    """Drop an unterminated final record (uncommitted, torn write) in place.

    Committed records end in a newline; a trailing partial line has none. Cut
    the file back to the last newline so the next append starts clean and does
    not concatenate onto the partial line. Runs under the mutating lock. Only
    ever removes an uncommitted tail -- never a committed (newline-terminated)
    record -- so it does not rewrite history.
    """
    if not p.exists() or p.stat().st_size == 0:
        return
    with p.open("rb") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) == b"\n":
            return
        f.seek(0)
        data = f.read()
    os.truncate(p, data.rfind(b"\n") + 1)


def _append(path, ev):
    """Append one event as a single JSON line. Append-only: never rewrites."""
    ev.setdefault("ts", _now())
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink(p)
    _repair_partial_tail(p)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


# ---- fold helpers -----------------------------------------------------------


def _asks(events):
    """id -> ask event (first definition wins; duplicates are integrity errors)."""
    out: dict = {}
    for e in events:
        if e.get("type") == "ask":
            out.setdefault(e["id"], e)
    return out


def _pruned(events, asks):
    """Set of pruned ids plus all their descendants."""
    roots = {e["id"] for e in events if e.get("type") == "prune"}
    pruned = set()
    for nid in asks:
        cur = nid
        while cur is not None:
            if cur in roots:
                pruned.add(nid)
                break
            cur = asks.get(cur, {}).get("parent")
    return pruned


def _last(events, etype, key="id"):
    val = None
    for e in events:
        if e.get("type") == etype:
            val = e.get(key)
    return val


def _sort_key(nid):
    """Total order over node ids that never raises on a non-numeric part.

    Tool-allocated ids are dotted integers; a hand-corrupted log could carry a
    non-numeric part. Rank numeric parts before non-numeric ones so a fold over
    an imperfect log sorts deterministically instead of aborting a projection
    with an unhandled ValueError. `validate` still flags the bad id.
    """
    key = []
    for part in str(nid).split("."):
        try:
            key.append((0, int(part), ""))
        except ValueError:
            key.append((1, 0, part))
    return tuple(key)


def _alloc_id(asks, parent):
    """Next child id under parent (None => root). Counts pruned ids to avoid reuse."""

    def _tail_num(nid):
        try:
            return int(str(nid).split(".")[-1])
        except ValueError:
            return 0

    sibs = [i for i, a in asks.items() if a.get("parent") == parent]
    nxt = 1 + max((_tail_num(i) for i in sibs), default=0)
    return str(nxt) if parent is None else "%s.%s" % (parent, nxt)


def _answers(events):
    out: dict = {}
    for e in events:
        if e.get("type") == "answer":
            out[e["id"]] = e.get("a", "")
    return out


def _dones(events):
    out: dict = {}
    for e in events:
        if e.get("type") == "done":
            out[e["id"]] = e
    return out


def _children(asks, pruned, parent):
    kids = [i for i, a in asks.items() if a.get("parent") == parent and i not in pruned]
    return sorted(kids, key=_sort_key)


# ---- commands ---------------------------------------------------------------


def cmd_ask(a):
    _apply_stdin_json(a, "q", "stage")
    if not a.q:
        raise SystemExit("ask requires --q or a 'q' field in --stdin-json")
    if a.stage not in STAGES:
        raise SystemExit(
            "ask requires --stage one of: %s (got %r)" % (", ".join(sorted(STAGES)), a.stage)
        )
    events = _load(a.log)
    asks = _asks(events)
    parent = None if a.parent in (None, "", "root", "null") else a.parent
    if parent is not None and parent not in asks:
        raise SystemExit("parent %r does not exist" % parent)
    if parent is not None and parent in _pruned(events, asks):
        raise SystemExit(
            "parent %r is pruned; a child added under it would be dropped from "
            "every projection" % parent
        )
    nid = _alloc_id(asks, parent)
    _append(
        a.log,
        {
            "type": "ask",
            "id": nid,
            "parent": parent,
            "stage": a.stage,
            "q": a.q,
            "origin": a.origin,
        },
    )
    print(nid)


def _require(a, asks):
    if a.id not in asks:
        raise SystemExit("node %r does not exist" % a.id)


def _require_live(a, events, asks):
    _require(a, asks)
    if a.id in _pruned(events, asks):
        raise SystemExit("node %r is pruned" % a.id)


def cmd_answer(a):
    _apply_stdin_json(a, "a", "source")
    if not a.a:
        raise SystemExit("answer requires --a or an 'a' field in --stdin-json")
    events = _load(a.log)
    asks = _asks(events)
    _require_live(a, events, asks)
    _append(a.log, {"type": "answer", "id": a.id, "a": a.a, "source": a.source})
    print("ok")


def cmd_focus(a):
    events = _load(a.log)
    asks = _asks(events)
    _require_live(a, events, asks)
    _append(a.log, {"type": "focus", "id": a.id})
    print("ok")


def cmd_done(a):
    _apply_stdin_json(a, "note")
    events = _load(a.log)
    asks = _asks(events)
    _require_live(a, events, asks)
    _append(a.log, {"type": "done", "id": a.id, "kind": a.kind, "note": a.note})
    print("ok")


def cmd_discuss(a):
    _apply_stdin_json(a, "text")
    if not a.text:
        raise SystemExit("discuss requires --text or a 'text' field in --stdin-json")
    asks = _asks(_load(a.log))
    if a.anchor not in asks:
        raise SystemExit("anchor %r does not exist" % a.anchor)
    _append(a.log, {"type": "discuss", "anchor": a.anchor, "text": a.text})
    print("ok")


def cmd_prune(a):
    _apply_stdin_json(a, "reason")
    events = _load(a.log)
    asks = _asks(events)
    _require(a, asks)
    cur = _last(events, "focus")
    if cur is not None and (cur == a.id or cur.startswith(a.id + ".")):
        raise SystemExit(
            "%r is the current focus or an ancestor of it; focus elsewhere "
            "first so resume cannot land on a pruned node" % a.id
        )
    _append(a.log, {"type": "prune", "id": a.id, "reason": a.reason})
    print("ok")


def cmd_event(a):
    if getattr(a, "stdin_json", False):
        try:
            ev = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            raise SystemExit("invalid --stdin-json payload: %s" % e)
    elif a.json is not None:
        try:
            ev = json.loads(a.json)
        except json.JSONDecodeError as e:
            raise SystemExit("invalid --json: %s" % e)
    else:
        raise SystemExit("event requires --json or --stdin-json")
    if not isinstance(ev, dict) or "type" not in ev:
        raise SystemExit("event must be a JSON object with a 'type' field")
    if not isinstance(ev["type"], str):
        raise SystemExit("event 'type' must be a string")
    if ev["type"] in CORE_TYPES:
        raise SystemExit(
            "event type %r is a core type; use its dedicated subcommand so the "
            "required fields are validated" % ev["type"]
        )
    _append(a.log, ev)
    print("ok")


def cmd_tree(a):
    events = _load(a.log)
    asks = _asks(events)
    pruned = _pruned(events, asks)
    answers = _answers(events)
    dones = _dones(events)
    cur = _last(events, "focus")
    lines = []

    def walk(nid, depth):
        node = asks[nid]
        if nid in dones:
            mark = " ✅"
        elif nid == cur:
            mark = " ⏳"
        else:
            mark = ""
        ans = answers.get(nid)
        tail = " → %s" % ans if ans else ""
        lines.append(
            "%s- [%s] (%s) %s%s%s"
            % ("  " * depth, nid, node.get("stage", ""), node.get("q", ""), tail, mark)
        )
        for kid in _children(asks, pruned, nid):
            walk(kid, depth + 1)

    for root in _children(asks, pruned, None):
        walk(root, 0)
    print("\n".join(lines) if lines else "(empty)")


def _frontier(events):
    asks = _asks(events)
    pruned = _pruned(events, asks)
    answers = _answers(events)
    dones = _dones(events)
    cur = _last(events, "focus")
    return [
        i
        for i in sorted(asks, key=_sort_key)
        if i not in pruned and i not in answers and i not in dones and i != cur
    ]


def cmd_frontier(a):
    events = _load(a.log)
    asks = _asks(events)
    fr = _frontier(events)
    if not fr:
        print("(none)")
        return
    for i in fr:
        print("- [%s] %s" % (i, asks[i].get("q", "")))


def cmd_view(a):
    events = _load(a.log)
    asks = _asks(events)
    answers = _answers(events)
    cur = _last(events, "focus")
    fr = _frontier(events)
    if cur is None:
        print("(no current focus)")
        print("open branches: %d" % len(fr))
        return
    parts = cur.split(".")
    chain = [".".join(parts[: k + 1]) for k in range(len(parts))]
    print("current path:")
    for depth, nid in enumerate(chain):
        node = asks.get(nid, {})
        ans = answers.get(nid)
        tail = " → %s" % ans if ans else ""
        star = "  ← current" if nid == cur else ""
        print(
            "%s- [%s] (%s) %s%s%s"
            % ("  " * depth, nid, node.get("stage", ""), node.get("q", ""), tail, star)
        )
    print("open branches: %d" % len(fr))


def cmd_report(a):
    _apply_stdin_json(a, "title")
    events = _load(a.log)
    asks = _asks(events)
    pruned = _pruned(events, asks)
    answers = _answers(events)
    dones = _dones(events)
    roots = _children(asks, pruned, None)
    title = a.title or (asks[roots[0]].get("q") if roots else "5 Whys")
    out = [
        "# 5 Whys report — %s" % title,
        "_Generated: %s · %d events_" % (_now(), len(events)),
        "",
    ]
    out.append("## Starting point")
    for r in roots:
        out.append("- %s" % asks[r].get("q", ""))
    out += ["", "## Tree (What -> Example -> Why-not -> Benefits -> Costs)"]

    def walk(nid, depth):
        node = asks[nid]
        ans = answers.get(nid)
        tail = " → %s" % ans if ans else ""
        if dones.get(nid, {}).get("kind") == "root":
            flag = "  **[root]**"
        elif nid in dones:
            flag = "  _[takeaway]_"
        else:
            flag = ""
        out.append(
            "%s- **[%s]** (%s) %s%s%s"
            % ("  " * depth, nid, node.get("stage", ""), node.get("q", ""), tail, flag)
        )
        for kid in _children(asks, pruned, nid):
            walk(kid, depth + 1)

    for r in roots:
        walk(r, 0)

    out += ["", "## Roots / key takeaways"]
    dn = [e for e in events if e.get("type") == "done" and e["id"] not in pruned]
    if dn:
        for e in dn:
            out.append("- **[%s]** (%s) %s" % (e["id"], e.get("kind", ""), e.get("note", "")))
    else:
        out.append("- (none yet)")

    disc = [e for e in events if e.get("type") == "discuss"]
    if disc:
        out += ["", "## Side discussions"]
        for e in disc:
            out.append("- @[%s] %s" % (e.get("anchor", ""), e.get("text", "")))
    print("\n".join(out))


def cmd_validate(a):
    events = _load(a.log)
    issues = []
    asks: dict = {}
    for n, e in enumerate(events, 1):
        if not isinstance(e, dict):
            issues.append("line %d: not a JSON object" % n)
            continue
        if "type" not in e or "ts" not in e:
            issues.append("line %d: missing type/ts" % n)
            continue
        t = e["type"]
        if t == "ask":
            aid = e.get("id")
            if (
                not isinstance(aid, str)
                or not aid
                or not all(part.isdigit() for part in aid.split("."))
            ):
                issues.append("line %d: ask has invalid id %r" % (n, aid))
                continue
            if "q" not in e:
                issues.append("line %d: ask missing q" % n)
                continue
            if e.get("stage") not in STAGES:
                issues.append("line %d: ask has out-of-schema stage %r" % (n, e.get("stage")))
            if aid in asks:
                issues.append("line %d: duplicate ask id %r" % (n, aid))
            p = e.get("parent")
            if p is not None and (not isinstance(p, str) or p not in asks):
                issues.append("line %d: ask parent %r invalid or not yet defined" % (n, p))
            asks[aid] = e
        elif t in ("answer", "focus", "done", "prune"):
            if not isinstance(e.get("id"), str) or e.get("id") not in asks:
                issues.append("line %d: %s references unknown id %r" % (n, t, e.get("id")))
        elif t == "discuss":
            if not isinstance(e.get("anchor"), str) or e.get("anchor") not in asks:
                issues.append("line %d: discuss anchor %r unknown" % (n, e.get("anchor")))
        # unknown (plugin) types are allowed and ignored
    if issues:
        print("\n".join(issues))
        sys.exit(1)
    print("OK — %d events, %d nodes" % (len(events), len(asks)))


def main():
    # Folded output (tree markers, arrows) is non-ASCII; a Windows cp1252 console
    # would raise UnicodeEncodeError on print. Make stdout tolerant before any
    # output is written so no command can crash on the console encoding.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    ap = argparse.ArgumentParser(description="five-whys event-log mechanical core")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn):
        p = sub.add_parser(name)
        p.add_argument("log")
        p.set_defaults(fn=fn)
        return p

    p = add("ask", cmd_ask)
    p.add_argument("--parent", default=None)
    p.add_argument("--stage", default="")
    p.add_argument("--q", default="")
    p.add_argument("--origin", default="proposed", choices=["proposed", "user"])
    p.add_argument("--stdin-json", action="store_true", dest="stdin_json")

    p = add("answer", cmd_answer)
    p.add_argument("--id", required=True)
    p.add_argument("--a", default="")
    p.add_argument("--source", default="ai")
    p.add_argument("--stdin-json", action="store_true", dest="stdin_json")

    p = add("focus", cmd_focus)
    p.add_argument("--id", required=True)

    p = add("done", cmd_done)
    p.add_argument("--id", required=True)
    p.add_argument("--kind", default="root", choices=["root", "takeaway"])
    p.add_argument("--note", default="")
    p.add_argument("--stdin-json", action="store_true", dest="stdin_json")

    p = add("discuss", cmd_discuss)
    p.add_argument("--anchor", required=True)
    p.add_argument("--text", default="")
    p.add_argument("--stdin-json", action="store_true", dest="stdin_json")

    p = add("prune", cmd_prune)
    p.add_argument("--id", required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--stdin-json", action="store_true", dest="stdin_json")

    p = add("event", cmd_event)
    p.add_argument("--json", default=None)
    p.add_argument("--stdin-json", action="store_true", dest="stdin_json")

    add("view", cmd_view)
    add("tree", cmd_tree)
    add("frontier", cmd_frontier)

    p = add("report", cmd_report)
    p.add_argument("--title", default="")
    p.add_argument("--stdin-json", action="store_true", dest="stdin_json")

    add("validate", cmd_validate)

    args = ap.parse_args()
    # Mutating commands read the log, derive state (e.g. the next id) and append;
    # hold an exclusive lock across that whole transaction so two of them racing
    # on one log cannot allocate the same id and corrupt the tree.
    mutating = {"ask", "answer", "focus", "done", "discuss", "prune", "event"}
    if args.cmd in mutating:
        with _log_lock(args.log):
            args.fn(args)
    else:
        args.fn(args)


if __name__ == "__main__":
    main()
