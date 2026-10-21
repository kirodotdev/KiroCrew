#!/usr/bin/env python3
"""sweep_check.py - the push-time check of the class sweep, for kirocrew-prepare-pr.

push_guard.py --require-single-on-base runs this after its own checks. It reads
<git-dir>/prepare-pr-sweep.md (never committed, rewritten each fix round) and
proves one thing: the mate's own search pattern leaves no hit that no site line
accounts for. A sibling the pattern never names passes; the review lanes still
catch that. One entry per span:

    ## span=<12 hex>
    class: a git hook can come from a source other than core.hooksPath
    search: git grep -nE 'hooksPath|fsmonitor' -- src/kiro_crew
    risk: security                      (optional: security | data-loss | corruption)
    resolution: narrowed -- <20+ characters>   (optional, see the seam rules)
    sites:
    - src/kiro_crew/a.py:118  fixed
    - src/kiro_crew/b.py:240  covered-by test/test_a.py::test_submodule_hook
    - src/kiro_crew/c.py:12  not-in-class -- reads the name only, never runs it

A class with no usable pattern says `search: none -- <reason>` and lists
`branches:` rows (`<case>  fixed | covered-by <test id> | single -- <reason>`).

Usage:  python3 sweep_check.py --base BASE [--git-dir DIR]
Exit:   0 clean, no sweep file, or warn mode | 40 refused (stderr lists each
        finding and its fix) | 2 could not run (not a git repo, no base ref)

Rules, one finding each:
  - the pattern hits nothing in the tree before the fix (vacuous);
  - a hit in HEAD that no site line accounts for (the hit list is printed);
  - more than 25 hits (too broad: narrow the pattern or the pathspec);
  - `covered-by <test id>` names a test that is not in a changed test file that still
    exists in HEAD (a deleted test covers nothing);
  - `fixed` names a path the diff does not touch;
  - `not-in-class` or `single` with a reason under 20 characters;
  - `search: none` with fewer than two `branches:` rows;
  - a pattern that starts with `-` or holds a newline, a `git grep` flag outside
    -n -E -F -i -w, or both -E and -F in one search.
A site row `path:line` covers only that line of HEAD; a row `path` covers the file
(a leading `./` is dropped). Any whitespace separates a row's parts and a reason is
counted with its whitespace collapsed. `risk:` takes one or more of security,
data-loss, corruption, split on commas, bars or blanks; any other word is a finding.
A line ends at CR, LF or CRLF only.
The search line is parsed, never run through a shell: the pattern reaches
`git grep` as one argv element after `-e`. It means what it meant when the mate
ran it: git's basic regex unless the line says -E or -F, in the vacuity check
and in the residual search alike. Every call runs from the repository root.

Seam rules, from the rounds the PR thread records (writer dispositions whose
declared outcome is `fixed`, counted once per distinct judged head):
  - the 3rd fix of one span needs a `resolution:` line or a `branches:` table;
  - the 5th needs `resolution: removed|narrowed -- <reason>` or
    `resolution: deferred #<issue> pinned-by <test id>`; a class marked
    `risk: security|data-loss|corruption` is never deferred.
A PR whose rounds cannot be read skips these rules and says so. No rule asks a person.

SWEEP_ENFORCED is False while the check is in its warn period: the same findings
print and the exit is 0. Each run appends one JSON line to
<git-dir>/prepare-pr-metrics.jsonl so the warn period measures time and refusals.
Portable: stdlib only. Every git and gh call runs through push_guard.py's child
runner: executables from PATH's absolute entries only (never the checkout), and a
`.cmd`/`.bat` launch whose arguments hold a cmd.exe metacharacter is refused.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import sys
import time

SWEEP_ENFORCED = False
SWEEP_FILE = "prepare-pr-sweep.md"
METRICS_FILE = "prepare-pr-metrics.jsonl"

EXIT_OK = 0
EXIT_ENV = 2
EXIT_REFUSED = 40

MAX_HITS = 25
MIN_REASON = 20
GIT_TIMEOUT_S = 20
SEAM_REPEAT_FIXES = 2
SEAM_LIMIT_FIXES = 4
SENSITIVE_RISKS = ("security", "data-loss", "corruption")
# The only `git grep` flags a search line may carry. Anything else (-e, -O,
# --open-files-in-pager, --no-index, ...) either changes what runs or what is read.
GREP_FLAG_LETTERS = frozenset("nEFiw")
DEFAULT_PATHSPECS = (
    "src/kiro_crew",
    "website/src",
    "website/electron",
    ":(exclude,glob)**/test/**",
    ":(exclude,glob)**/tests/**",
    ":(exclude,glob)**/__tests__/**",
    ":(exclude,glob)**/locales/**",
    ":(exclude,glob)**/*.test.*",
)

ENTRY_RE = re.compile(r"^## span=([0-9a-f]{12})\s*$")
FIELD_RE = re.compile(r"^(class|search|risk|resolution|sites|branches):[ \t]*(.*?)\s*$")
# A site or branch row. The keyword and its argument are named groups, so no
# later step splits on a delimiter the pattern did not require (any whitespace
# separates the parts).
ROW_RE = re.compile(
    r"^-\s+(?P<what>.+?)\s+(?:(?P<fixed>fixed)"
    r"|covered-by\s+(?P<test>\S+)"
    r"|(?P<nic>not-in-class)\s+--\s+(?P<nic_reason>.+)"
    r"|(?P<single>single)\s+--\s+(?P<single_reason>.+))\s*$"
)
# One `git grep -z -n` row: `<rev>:<path> NUL <line> NUL <text> LF`. The path may
# hold any character but NUL, a newline included.
HIT_RE = re.compile(r"([^\0]+)\0(\d+)\0[^\n]*(?:\n|\Z)")
HEADING_LIKE_RE = re.compile(r"^#+\s*span\s*=", re.IGNORECASE)
RESOLUTION_RE = re.compile(
    r"^(?:(?P<kind>removed|narrowed)\s+--\s+(?P<reason>.+)"
    r"|(?P<defer>deferred)\s+#(?P<issue>\d+)\s+pinned-by\s+(?P<pin>\S+))$"
)
NONE_RE = re.compile(r"^none\s+--\s+(?P<reason>.+)$")


class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def set_data(self, path, data, *, _mode=0o666):
        return None


_SIBLINGS: dict = {}


def _sibling(filename):
    """A sibling script loaded once, without cwd, sys.path or bytecode side effects."""
    if filename not in _SIBLINGS:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        loader = _NoBytecodeSourceLoader("_prepare_pr_sweep_" + filename[:-3].lstrip("_"), path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        if spec is None:
            raise ImportError("cannot load " + path)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        _SIBLINGS[filename] = module
    return _SIBLINGS[filename]


def _run(args, cwd=None, timeout=GIT_TIMEOUT_S):
    """(rc, stdout, stderr) of one git or gh call, through push_guard's child runner.

    The runner resolves git and gh from PATH's absolute entries, refuses a
    `.cmd`/`.bat` launch whose arguments hold a cmd.exe metacharacter (126), and
    reports a missing executable as 127 and a bound hit as 124.
    """
    try:
        guard = _sibling("push_guard.py")
        env = guard.child_env()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_PAGER"] = "cat"
        res = guard.run_child(guard.tool_argv(args), env, timeout, cwd=cwd)
    except (OSError, ImportError, SyntaxError, ValueError) as exc:
        return 127, "", "{}: {}".format(args[0], exc)
    if res.timed_out:
        return 124, "", "{} {} timed out after {}s".format(args[0], args[1], timeout)
    return res.rc, res.out.decode("utf-8", "replace"), res.err.decode("utf-8", "replace")


def _git(args, cwd=None, timeout=GIT_TIMEOUT_S):
    return _run(["git", *args], cwd, timeout)


def _lines(text):
    """Lines split on CR, LF and CRLF only, by the contract module every prepare-pr script shares."""
    return _sibling("_review_contract.py").split_lines(text)


def _row_status(row):
    """(status, argument) of a matched ROW_RE row."""
    if row.group("fixed"):
        return "fixed", ""
    if row.group("test"):
        return "covered-by", row.group("test")
    if row.group("nic"):
        return "not-in-class", row.group("nic_reason").strip()
    return "single", row.group("single_reason").strip()


def parse_sweep(text):
    """(entries, problems): `problems` is [(line number, message)] for rows that do not parse."""
    entries: list = []
    problems: list = []
    cur = None
    section = None
    for number, raw in enumerate(_lines(text.lstrip("\ufeff")), 1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("<!--"):
            continue
        m = ENTRY_RE.match(line)
        if m:
            cur = {
                "span": m.group(1),
                "line": number,
                "class": "",
                "search": "",
                "risk": "",
                "resolution": "",
                "sites": [],
                "branches": [],
            }
            entries.append(cur)
            section = None
            continue
        if HEADING_LIKE_RE.match(line) and not ENTRY_RE.match(line):
            problems.append((number, "a span heading is exactly `## span=<12 lower-case hex>`"))
            continue
        if cur is None:
            if not line.startswith("#"):
                problems.append((number, "text before the first `## span=<12 hex>` heading"))
            continue
        f = FIELD_RE.match(line)
        if f:
            key, value = f.group(1), f.group(2)
            section = key if key in ("sites", "branches") else None
            if section is None:
                cur[key] = value
            continue
        if line.lstrip().startswith("-") and section:
            row = ROW_RE.match(line.strip())
            if not row:
                problems.append(
                    (
                        number,
                        "row does not end in fixed | covered-by <test id> | "
                        "not-in-class -- <reason> | single -- <reason>: " + line.strip()[:80],
                    )
                )
                continue
            status, arg = _row_status(row)
            cur[section].append(
                {"what": row.group("what"), "status": status, "arg": arg, "line": number}
            )
            continue
        problems.append((number, "unrecognised line: " + line.strip()[:80]))
    return entries, problems


def parse_search(value):
    """('none', reason) | ('grep', {'flags', 'pattern', 'paths'}) | ('error', message)."""
    m = NONE_RE.match(value.strip())
    if m:
        return "none", m.group("reason").strip()
    if not value.strip():
        return "error", "missing `search:` line (a `git grep` command, or `none -- <reason>`)"
    try:
        tokens = shlex.split(value)
    except ValueError as exc:
        return "error", "search line does not parse: {}".format(exc)
    if tokens[:2] != ["git", "grep"]:
        return "error", "search must be `git grep ...` or `none -- <reason>`"
    flags: set = set()
    pattern = None
    paths: list = []
    rest = tokens[2:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            paths = rest[i + 1 :]
            break
        if pattern is None and tok.startswith("-"):
            letters = set(tok[1:])
            if tok.startswith("--") or not letters or not letters <= GREP_FLAG_LETTERS:
                return "error", (
                    "search flag {!r} is not allowed (use -n -E -F -i -w); a pattern may "
                    "not start with '-'".format(tok)
                )
            flags |= letters
        elif pattern is None:
            pattern = tok
        else:
            return "error", "search takes one pattern, then `-- <pathspec>...`"
        i += 1
    if not pattern:
        return "error", "search has no pattern"
    if "\n" in pattern or "\0" in pattern:
        return "error", "a pattern with a newline is several patterns to git; give one per entry"
    if {"E", "F"} <= flags:
        return "error", "search carries both -E and -F; use one of them"
    for p in paths:
        if p.startswith("-") or p.startswith(":"):
            return "error", "pathspec {!r} may not start with '-' or ':'".format(p)
    return "grep", {"flags": flags, "pattern": pattern, "paths": paths}


def _grep(spec, rev, cwd):
    """(hits, error): [(path, line)] for the search at `rev`; error is a message or ''."""
    # git's own default (basic regex) unless the search line says -E or -F, so the
    # pattern means here what it meant when the mate ran it.
    args = ["grep", "-n", "-z", "-I", "--no-color"]
    args += [
        f for f, k in (("-E", "E"), ("-F", "F"), ("-i", "i"), ("-w", "w")) if k in spec["flags"]
    ]
    args += ["-e", spec["pattern"], rev, "--"] + (spec["paths"] or list(DEFAULT_PATHSPECS))
    rc, out, err = _git(args, cwd)
    if rc == 1:
        return [], ""
    if rc != 0:
        return [], "git grep failed: {}".format((err.strip().splitlines() or ["?"])[0])
    # With -z each row is `<rev>:<path> NUL <line> NUL <text>`: the path is verbatim
    # (no quoting of unusual names) and a colon in it cannot be mistaken for a line.
    prefix = rev + ":"
    hits = []
    for m in HIT_RE.finditer(out):
        path = m.group(1)
        hits.append((path[len(prefix) :] if path.startswith(prefix) else path, int(m.group(2))))
    return hits, ""


def _ref_exists(ref, cwd):
    return _git(["rev-parse", "--verify", "-q", ref + "^{commit}"], cwd)[0] == 0


def _anchored(spec, base_ref, branch_ref, cwd):
    """True when the pattern hits the tree before the fix: the base, or the branch as last pushed.

    Both are searched with the same flags as the residual search, so the two
    checks read the pattern the same way.
    """
    for ref in (base_ref, branch_ref):
        if ref and _ref_exists(ref, cwd) and _grep(spec, ref, cwd)[0]:
            return True
    return False


def _repo_path(path):
    """A repo-relative path without a leading `./` (only that is normalised)."""
    while path.startswith("./"):
        path = path[2:]
    return path


def _split_site(what):
    """(path, line or None) of a site row's `path` or `path:line`."""
    m = re.match(r"^(.*?):(\d+)$", what)
    path, line = (m.group(1), int(m.group(2))) if m else (what, None)
    return _repo_path(path), line


def _test_exists(test_id, changed, cwd):
    """'' when the id names a changed file (and a name found in it), else the reason."""
    path, _, name = test_id.partition("::")
    path = _repo_path(path)
    if path not in changed:
        return "test file {} is not in the diff".format(path)
    rc, out, _ = _git(["show", "HEAD:" + path], cwd)
    if rc != 0:
        return "test file {} does not exist in HEAD (a deleted test covers nothing)".format(path)
    # the parameter part `[a::b]` is cut first, so a `::` inside it is not a separator
    leaf = name.split("[", 1)[0].split("::")[-1]
    if name and not leaf:
        return "{} has no test named in {!r}".format(path, test_id)
    if name and not re.search(r"(?<![\w])" + re.escape(leaf) + r"(?![\w])", out):
        return "{} has no {}".format(path, leaf)
    return ""


def _reason_problem(kind, reason):
    if len(" ".join(reason.split())) < MIN_REASON:
        return "`{}` needs a reason of {}+ characters".format(kind, MIN_REASON)
    return ""


def _check_rows(rows, label, changed, cwd, require_fixed_in_diff):
    problems = []
    for row in rows:
        where = "{} line {}".format(label, row["line"])
        if row["status"] == "fixed" and require_fixed_in_diff:
            path = _split_site(row["what"])[0]
            if path not in changed:
                problems.append("{}: `fixed` but the diff does not touch {}".format(where, path))
        elif row["status"] == "covered-by":
            why = _test_exists(row["arg"], changed, cwd)
            if why:
                problems.append("{}: covered-by {}".format(where, why))
        elif row["status"] in ("not-in-class", "single"):
            why = _reason_problem(row["status"], row["arg"])
            if why:
                problems.append("{}: {}".format(where, why))
    return problems


def _risk_words(value):
    """The words of a `risk:` value, split on commas, bars and whitespace, lower-cased."""
    return [w for w in re.split(r"[\s,|]+", value.lower()) if w]


def _nth(n):
    return "{}{}".format(n, {1: "st", 2: "nd", 3: "rd"}.get(n if n < 20 else n % 10, "th"))


def _seam_problems(entry, prior, changed, cwd):
    """Findings from the 3rd/5th-fix rules; `prior` is the count of earlier fixed heads or None."""
    problems = []
    res = entry["resolution"].strip()
    parsed = RESOLUTION_RE.match(res) if res else None
    risky_words = [w for w in _risk_words(entry["risk"]) if w in SENSITIVE_RISKS]
    risky = bool(risky_words)
    if res and not parsed:
        problems.append(
            "resolution must read `removed -- <reason>`, `narrowed -- <reason>` or "
            "`deferred #<issue> pinned-by <test id>`"
        )
    elif parsed and parsed.group("kind"):
        why = _reason_problem(parsed.group("kind"), parsed.group("reason"))
        if why:
            problems.append("resolution: " + why)
    elif parsed:
        if risky:
            problems.append(
                "a {} class is never deferred: resolve it with `removed` or `narrowed`".format(
                    ", ".join(risky_words)
                )
            )
        why = _test_exists(parsed.group("pin"), changed, cwd)
        if why:
            problems.append("resolution pinned-by {}".format(why))
    if prior is None:
        return problems
    full_table = len(entry["branches"]) >= 2
    if prior >= SEAM_LIMIT_FIXES and not parsed:
        problems.append(
            "this is the {} fix of one span: narrow the scope or defer it with an issue "
            "and a pinning test, and record `resolution:` (see `sweep_check.py --help`)".format(
                _nth(prior + 1)
            )
        )
    elif prior >= SEAM_REPEAT_FIXES and not parsed and not full_table:
        problems.append(
            "this is the {} fix of one span: change the approach and record `resolution:`, "
            "or patch once with a `branches:` table of two or more rows".format(_nth(prior + 1))
        )
    return problems


def check_entry(entry, base_ref, branch_ref, changed, prior, cwd):
    """Every finding for one entry, as strings."""
    problems = []
    if not entry["class"].strip():
        problems.append("missing `class:` sentence")
    unknown = [w for w in _risk_words(entry["risk"]) if w not in SENSITIVE_RISKS]
    if unknown:
        problems.append(
            "risk must name security, data-loss or corruption, not {}".format(", ".join(unknown))
        )
    kind, spec = parse_search(entry["search"])
    if kind == "error":
        problems.append(spec)
    elif kind == "none":
        if len(entry["branches"]) < 2:
            problems.append("`search: none` needs a `branches:` table of two or more rows")
        if any(r["status"] == "not-in-class" for r in entry["branches"]):
            problems.append("branch rows end in fixed | covered-by <test id> | single -- <reason>")
    else:
        if not _anchored(spec, base_ref, branch_ref, cwd):
            problems.append("pattern matches nothing in the tree before the fix (vacuous)")
        hits, bad = _grep(spec, "HEAD", cwd)
        if bad:
            problems.append(bad)
        elif len(hits) > MAX_HITS:
            problems.append(
                "pattern has {} hits (limit {}): too broad, narrow the pattern or the "
                "pathspec".format(len(hits), MAX_HITS)
            )
        else:
            sites = [_split_site(r["what"]) for r in entry["sites"]]
            whole_files = {p for p, n in sites if n is None}
            one_line = {(p, n) for p, n in sites if n is not None}
            loose = [
                "{}:{}".format(p, n)
                for p, n in hits
                if p not in whole_files and (p, n) not in one_line
            ]
            if loose:
                problems.append(
                    "{} hit(s) no site line accounts for: {}".format(len(loose), ", ".join(loose))
                )
    problems += _check_rows(entry["sites"], "site", changed, cwd, True)
    problems += _check_rows(entry["branches"], "branch", changed, cwd, False)
    problems += _seam_problems(entry, prior, changed, cwd)
    return problems


def fix_counts_from(records, bodies, skip_head=""):
    """{span: distinct earlier heads that fixed it} from writer disposition records.

    Read by `_review_contract.disposition_rounds` and `fix_rounds`, the reader
    `pr_findings.py --rounds` uses: a fix is a disposition whose first declared
    outcome is `fixed`, `removed` or `reverted`, read from the first quoted lead
    line or a label line that starts the record; every span of a fixing record is
    kept, a head counts once per span whether spelled with 7 characters or 40,
    and a plain `resolution:` line in a disposition restarts the span's count.
    The count never looks at a title or a `class:` line. `skip_head` leaves out
    the head being fixed now, whose disposition may be posted before the push.
    """
    contract = _sibling("_review_contract.py")
    order, rounds = contract.disposition_rounds(records, bodies)
    if skip_head:
        order = [k for k in order if not contract.same_head(rounds[k]["head"], skip_head)]
    return {span: len(found) for span, found in contract.fix_rounds(order, rounds).items()}


def _prior_fix_counts(cwd):
    """{span: earlier fixed heads} from the PR thread, or None when it cannot be read.

    Reads the thread with the same record rules as `pr_findings.py --rounds`
    (writer-authored dispositions only), through the safe gh runner.
    """
    rc, out, _ = _run(["gh", "pr", "view", "--json", "number,url,headRefOid"], cwd)
    if rc != 0:
        return None
    try:
        pr = json.loads(out)
        m = re.match(r"https?://[^/]+/([^/]+)/([^/]+)/pull/\d+", pr.get("url") or "")
        if m is None:
            return None
        repo = "{}/{}".format(m.group(1), m.group(2))
        contract = _sibling("_review_contract.py")
        comments = contract.fetch_disposition_comments(repo, pr["number"], _run)
        if comments is None:
            return None
        records = contract.writer_disposition_records(repo, comments, _run)
        if records is None:
            return None
        bodies = {c.get("id"): (c.get("body") or "") for c in comments}
        return fix_counts_from(records, bodies, pr.get("headRefOid") or "")
    except (ValueError, KeyError, TypeError, ImportError, SyntaxError, AttributeError):
        return None


def _log_metrics(git_dir, started, outcome, entries, findings):
    row = {
        "ts": int(time.time()),
        "step": "sweep_check",
        "seconds": round(time.monotonic() - started, 2),
        "outcome": outcome,
        "entries": entries,
        "findings": findings,
        "enforced": SWEEP_ENFORCED,
    }
    try:
        with open(os.path.join(git_dir, METRICS_FILE), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def run(base, git_dir=None, cwd=None, prior_counts=None):
    """Check the sweep file; return EXIT_OK, EXIT_REFUSED or EXIT_ENV.

    `prior_counts` is a callable returning {span: earlier fixed heads} or None;
    the default reads the PR thread.
    """
    started = time.monotonic()
    # Sweep searches are repository-relative, so every call runs from the
    # repository root, wherever the caller stands (website/, a subdirectory).
    rc, top, err = _git(["rev-parse", "--show-toplevel"], cwd)
    if rc != 0 or not top.strip():
        print("ERROR: not a git repository: {}".format(err.strip()), file=sys.stderr)
        return EXIT_ENV
    cwd = top.strip()
    if git_dir is None:
        rc, out, err = _git(["rev-parse", "--absolute-git-dir"], cwd)
        if rc != 0:
            print("ERROR: not a git repository: {}".format(err.strip()), file=sys.stderr)
            return EXIT_ENV
        git_dir = out.strip()
    path = os.path.join(git_dir, SWEEP_FILE)
    if not os.path.isfile(path):
        _log_metrics(git_dir, started, "no-file", 0, 0)
        return EXIT_OK
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        print("ERROR: cannot read {}: {}".format(path, exc), file=sys.stderr)
        return EXIT_ENV
    base_ref = "refs/remotes/origin/" + base
    if not _ref_exists(base_ref, cwd):
        print("ERROR: {} does not exist; fetch the base first.".format(base_ref), file=sys.stderr)
        return EXIT_ENV
    rc, branch, _ = _git(["symbolic-ref", "--short", "-q", "HEAD"], cwd)
    branch_ref = "refs/remotes/origin/" + branch.strip() if rc == 0 and branch.strip() else ""
    rc, names, _ = _git(["diff", "--name-only", "-z", base_ref, "HEAD"], cwd)
    changed = {n for n in names.split("\0") if n}

    entries, parse_problems = parse_sweep(text)
    findings = ["{} line {}: {}".format(SWEEP_FILE, n, msg) for n, msg in parse_problems]
    counts = (prior_counts or (lambda: _prior_fix_counts(cwd)))()
    if counts is None:
        print(
            "sweep_check: the PR's rounds could not be read; seam rules skipped.", file=sys.stderr
        )
    for entry in entries:
        prior = None if counts is None else counts.get(entry["span"], 0)
        for msg in check_entry(entry, base_ref, branch_ref, changed, prior, cwd):
            findings.append("span={}: {}".format(entry["span"], msg))
    if not findings:
        print(
            "sweep_check: {} sweep entr{} checked, no findings.".format(
                len(entries), "y" if len(entries) == 1 else "ies"
            )
        )
        _log_metrics(git_dir, started, "ok", len(entries), 0)
        return EXIT_OK
    head = "REFUSED" if SWEEP_ENFORCED else "WARNING (warn period, not refused)"
    print("sweep_check {}: {} finding(s) in {}".format(head, len(findings), path), file=sys.stderr)
    for f in findings:
        print("  - " + f, file=sys.stderr)
    print(
        "  Fix the sweep file or the code, then push again. Format: sweep_check.py --help",
        file=sys.stderr,
    )
    _log_metrics(
        git_dir, started, "refused" if SWEEP_ENFORCED else "warned", len(entries), len(findings)
    )
    return EXIT_REFUSED if SWEEP_ENFORCED else EXIT_OK


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return EXIT_OK if args else 64
    base = git_dir = None
    i = 0
    while i < len(args):
        if args[i] == "--base" and i + 1 < len(args):
            base = args[i + 1]
            i += 2
        elif args[i] == "--git-dir" and i + 1 < len(args):
            git_dir = args[i + 1]
            i += 2
        else:
            print("usage: sweep_check.py --base BASE [--git-dir DIR]", file=sys.stderr)
            return 64
    if not base:
        print("usage: sweep_check.py --base BASE [--git-dir DIR]", file=sys.stderr)
        return 64
    return run(base, git_dir)


if __name__ == "__main__":
    sys.exit(main())
