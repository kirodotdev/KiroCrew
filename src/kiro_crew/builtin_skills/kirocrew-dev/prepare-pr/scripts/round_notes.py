#!/usr/bin/env python3
"""round_notes.py - the per-PR round log the prepare-pr loop reads before each round.

The loop has no memory between rounds beyond the PR's own comment thread, and
that thread is written for the reviewers, not for the author. So each round
re-derives the situation from scratch: it sees this round's findings, not the
fact that the code they land in was itself added two rounds ago to satisfy an
earlier finding. That is how a PR grows 2-6x under review while every single
change was "a fix" - nothing ever looks back at the whole.

This script keeps ONE markdown file per PR branch, outside the repository, that
the loop appends to every round and reads at the top of the next. The file
carries what a retrospective needs and nothing else:

  * the PR's original intent, copied once at round 0 and never edited;
  * one entry per round: head, diff size and its delta, every finding with its
    span, whether it landed in code THIS PR added in an earlier round, and the
    disposition taken;
  * a running list of the mechanisms the PR has grown (one line each, with the
    round that introduced it), maintained by the agent as it goes.

On ``show`` the script also prints the span recurrence table it derives from
the entries, so a span that has drawn findings in three rounds is visible as a
number rather than a recollection. The agent decides what to do with that -
this script recommends nothing and never gates anything.

The file lives under ``<KIROCREW_HOME>/prepare-pr/<owner>-<repo>/<branch>.md``
(``KIROCREW_HOME`` defaults to ``~/.kiro/crew``), never inside the worktree, so
it can neither dirty the tree nor land in the diff. ``rm`` deletes one file
(the loop runs it at Phase 4), ``prune`` deletes files untouched for N days
(Phase 0 runs it) - a note outlives its PR only by accident, never by design.

Stdlib only; Python 3.10+, like its sibling scripts. Read-write, but only under
the notes directory named above.

Usage:
    python3 round_notes.py init  [--intent TEXT | --intent-file PATH]
    python3 round_notes.py add   --head SHA --additions N --deletions N
                                 [--finding 'span=ID | title | self-added:yes|no | disposition']...
                                 [--mechanism 'one-line description'] [--note TEXT]
    python3 round_notes.py show  [--json]
    python3 round_notes.py rm
    python3 round_notes.py prune [--days N]   (default 14)

Every subcommand resolves the note from the current git repository and branch;
pass ``--repo OWNER/NAME`` / ``--branch NAME`` to override (``prune`` needs
neither).

Exit:
    0  done
    2  environment / usage error (not a git repo, on the base branch, bad args)
    20 `show` on a branch that has no note yet
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROUND_RE = re.compile(r"^## Round (\d+) — head ([0-9a-f]+) — \+(\d+)/-(\d+)", re.MULTILINE)
FINDING_RE = re.compile(
    r"^- span=(?P<span>[0-9a-f]+) \| (?P<title>.*?) \| self-added:(?P<self>yes|no) \| (?P<disp>[\w-]+)\s*$",
    re.MULTILINE,
)
MECHANISM_HEADER = "## Mechanisms this PR has grown"
INTENT_HEADER = "## Intent (round 0 — never edited)"
BASE_BRANCHES = ("main", "master", "mainline")


def notes_root() -> Path:
    home = os.environ.get("KIROCREW_HOME", "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".kiro" / "crew"
    return root / "prepare-pr"


def run(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return 127, ""
    return proc.returncode, proc.stdout.strip()


def detect_repo() -> str | None:
    rc, url = run(["git", "config", "--get", "remote.origin.url"])
    if rc != 0 or not url:
        return None
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", url)
    return "{}/{}".format(m.group(1), m.group(2)) if m else None


def detect_branch() -> str | None:
    rc, branch = run(["git", "branch", "--show-current"])
    return branch if rc == 0 and branch else None


def safe_component(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "unnamed"


def note_path(repo: str, branch: str) -> Path:
    return notes_root() / safe_component(repo.replace("/", "-")) / (safe_component(branch) + ".md")


def resolve(args: argparse.Namespace) -> tuple[Path, str, str]:
    repo = args.repo or detect_repo()
    branch = args.branch or detect_branch()
    if not repo or not branch:
        sys.stderr.write(
            "round_notes: cannot resolve the note - not in a git repository with an "
            "origin remote and a checked-out branch (pass --repo and --branch).\n"
        )
        sys.exit(2)
    if branch in BASE_BRANCHES:
        sys.stderr.write("round_notes: refusing to keep a note for the base branch {!r}.\n".format(branch))
        sys.exit(2)
    return note_path(repo, branch), repo, branch


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    path, repo, branch = resolve(args)
    if path.exists() and not args.force:
        print("note already exists: {}".format(path))
        return 0
    if args.intent_file:
        intent = Path(args.intent_file).read_text(encoding="utf-8").strip()
    elif args.intent:
        intent = args.intent.strip()
    else:
        rc, intent = run(["git", "log", "-1", "--format=%s%n%n%b"])
        intent = intent.strip() if rc == 0 else ""
    if not intent:
        sys.stderr.write("round_notes: no intent - pass --intent or --intent-file.\n")
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "# prepare-pr round notes\n\n"
        "repo: {repo}\nbranch: {branch}\ncreated: {ts}\n\n"
        "{intent_header}\n\n{intent}\n\n"
        "{mech_header}\n\n(none yet)\n\n"
        "## Rounds\n"
    ).format(
        repo=repo,
        branch=branch,
        ts=now(),
        intent_header=INTENT_HEADER,
        intent=intent,
        mech_header=MECHANISM_HEADER,
    )
    path.write_text(body, encoding="utf-8")
    print("initialised {}".format(path))
    return 0


def parse_finding(raw: str) -> str:
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 4 or not parts[0].startswith("span="):
        sys.stderr.write(
            "round_notes: --finding must be 'span=ID | title | self-added:yes|no | disposition', "
            "got {!r}\n".format(raw)
        )
        sys.exit(2)
    span, title, self_added, disp = parts
    self_added = self_added.lower().replace(" ", "")
    if self_added not in ("self-added:yes", "self-added:no"):
        sys.stderr.write("round_notes: self-added must be 'self-added:yes' or 'self-added:no'\n")
        sys.exit(2)
    return "- {} | {} | {} | {}".format(span, title, self_added, disp)


def cmd_add(args: argparse.Namespace) -> int:
    path, _, _ = resolve(args)
    if not path.exists():
        sys.stderr.write("round_notes: no note for this branch - run `round_notes.py init` first.\n")
        return 2
    text = path.read_text(encoding="utf-8")
    rounds = ROUND_RE.findall(text)
    n = len(rounds)
    delta = ""
    if rounds:
        prev_add, prev_del = int(rounds[-1][2]), int(rounds[-1][3])
        delta = " (Δ {:+d}/{:+d})".format(args.additions - prev_add, args.deletions - prev_del)
    lines = [
        "",
        "## Round {} — head {} — +{}/-{}{} — {}".format(
            n, args.head[:12], args.additions, args.deletions, delta, now()
        ),
        "### Findings",
    ]
    if args.finding:
        lines.extend(parse_finding(f) for f in args.finding)
    else:
        lines.append("- (none)")
    if args.note:
        lines.append("### Note\n" + args.note.strip())
    text = text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    for mech in args.mechanism or []:
        text = _append_mechanism(text, n, mech.strip())
    path.write_text(text, encoding="utf-8")
    print("recorded round {} in {}".format(n, path))
    return 0


def _append_mechanism(text: str, round_no: int, mech: str) -> str:
    start = text.index(MECHANISM_HEADER) + len(MECHANISM_HEADER)
    end = text.index("\n## ", start)
    entries = [line for line in text[start:end].splitlines() if line.startswith("- ")]
    entries.append("- r{}: {}".format(round_no, mech))
    return text[:start] + "\n\n" + "\n".join(entries) + "\n" + text[end:]


def summarise(text: str) -> dict:
    rounds = [
        {"round": int(r), "head": h, "additions": int(a), "deletions": int(d)}
        for r, h, a, d in ROUND_RE.findall(text)
    ]
    spans: dict[str, dict] = {}
    self_added_total = 0
    for block in re.split(r"^## Round ", text, flags=re.MULTILINE)[1:]:
        rno = int(block.split(" ", 1)[0])
        for m in FINDING_RE.finditer(block):
            entry = spans.setdefault(
                m.group("span"), {"rounds": [], "title": m.group("title"), "self_added": 0}
            )
            entry["rounds"].append(rno)
            if m.group("self") == "yes":
                entry["self_added"] += 1
                self_added_total += 1
    mechanisms: list[str] = []
    mech_start = text.find(MECHANISM_HEADER)
    if mech_start != -1:
        mech_end = text.find("\n## ", mech_start + len(MECHANISM_HEADER))
        mechanisms = [
            line[2:].strip()
            for line in text[mech_start:mech_end].splitlines()
            if line.startswith("- ")
        ]
    intent = ""
    if INTENT_HEADER in text:
        s = text.index(INTENT_HEADER) + len(INTENT_HEADER)
        intent = text[s : text.find("\n## ", s)].strip()
    return {
        "intent": intent,
        "rounds": rounds,
        "growth": (
            {"first": rounds[0]["additions"], "last": rounds[-1]["additions"]} if rounds else None
        ),
        "mechanisms": mechanisms,
        "spans": spans,
        "recurring_spans": sorted(
            ((s, len(v["rounds"])) for s, v in spans.items() if len(v["rounds"]) >= 3),
            key=lambda x: -x[1],
        ),
        "self_added_findings": self_added_total,
    }


def cmd_show(args: argparse.Namespace) -> int:
    path, _, _ = resolve(args)
    if not path.exists():
        sys.stderr.write("round_notes: no note for this branch yet ({}).\n".format(path))
        return 20
    text = path.read_text(encoding="utf-8")
    summary = summarise(text)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print(text.rstrip("\n"))
    print("\n---")
    print("rounds: {}".format(len(summary["rounds"])))
    if summary["growth"]:
        g = summary["growth"]
        print("additions: {} → {} ({:+d})".format(g["first"], g["last"], g["last"] - g["first"]))
    print("mechanisms grown: {}".format(len(summary["mechanisms"])))
    print("findings in self-added code: {}".format(summary["self_added_findings"]))
    if summary["recurring_spans"]:
        print("recurring spans (≥3 rounds):")
        for span, count in summary["recurring_spans"]:
            print("  {} ×{} — {}".format(span, count, summary["spans"][span]["title"]))
    else:
        print("recurring spans (≥3 rounds): none")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    path, _, _ = resolve(args)
    if path.exists():
        path.unlink()
        print("removed {}".format(path))
    else:
        print("no note at {}".format(path))
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    root = notes_root()
    if not root.exists():
        print("nothing to prune")
        return 0
    cutoff = time.time() - args.days * 86400
    removed = 0
    for path in root.rglob("*.md"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    print("pruned {} note(s) older than {} day(s)".format(removed, args.days))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", help="OWNER/NAME override")
    ap.add_argument("--branch", help="branch override")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--intent")
    p.add_argument("--intent-file")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("add")
    p.add_argument("--head", required=True)
    p.add_argument("--additions", type=int, required=True)
    p.add_argument("--deletions", type=int, required=True)
    p.add_argument("--finding", action="append")
    p.add_argument("--mechanism", action="append")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("show")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("rm")
    p.set_defaults(fn=cmd_rm)

    p = sub.add_parser("prune")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(fn=cmd_prune)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
