#!/usr/bin/env python3
"""Re-run one finding's proof of concept and record the verdict.

Hallucinated vulnerabilities are the dominant noise source in agentic security
review, so every finding gets an independent second pass whose job is REJECTING
it. This script is that pass's only writer: the conductor reads the verdict it
records and never reads a verifier's prose and decides for itself.

Usage::

    python3 verify_finding.py [--db PATH] --finding-id N --worktree DIR \\
        [--timeout SECONDS]

Exit codes, which are the interface::

    0   confirmed    -- the proof reproduces
    10  rejected     -- it does not reproduce
    20  needs-human  -- it cannot be settled here: refused, timed out, or the
                        proof did not actually run
    2   invalid input -- no such finding, a bad timeout, an unrunnable PoC
                        shape; NOTHING is recorded, because a verdict about a
                        finding that is not there has nothing to attach to

stdout is one JSON object: ``{"finding_id": N, "verdict": V, "reason": R}``.
Every verdict this script reaches is appended through
``ledger.record_verdict(role="verifier", ...)``, so the disagreement with the
auditor stays readable instead of being overwritten by whoever wrote last.

The two proof shapes::

    pytest::<nodeid>     a unit-level test that FAILS while the defect is present
    cmd::<argv ...>      a command that exits NONZERO while the defect is present

``cmd::`` is split with ``shlex`` and run WITHOUT a shell, so a pipe, a
redirection or a substitution in a PoC is an argument rather than an operator.

**Neither lane may read its verdict off an exit status alone**, and both for the
same reason: a nonzero exit means "this did not succeed", which covers "the
defect reproduced" AND "the proof never ran". Confirming a finding on a proof
that never ran is the exact false positive this pass exists to catch.

- ``pytest::`` takes its verdict from a **JUnit XML report written outside the
  worktree**, keyed to the requested nodeid, never from the run's stdout or exit
  status. A run that exits 1 because collection failed, or because a DIFFERENT
  test failed, is not a reproduction; the report says which test ran and what it
  did. A missing, unparseable or unrelated report is ``needs-human``, so the
  failure direction is toward a human rather than toward a confirmation.
- ``cmd::`` treats **launch-failure shapes as ``needs-human``**: a spawn error,
  a death by signal, and exit 126/127 all mean the command did not run. So is
  exit 1, the uncaught-crash status of every common interpreter, which cannot
  be told from a reproduction. Only a command that ran and exited a deliberate
  status in 2..125 confirms.

**The refusal screen.** A PoC is refused, unrun, as ``needs-human`` when it
carries a shape the rules of engagement forbid outright, or when ``--worktree``
is not a git checkout -- "in a scratch checkout" is the entire blast-radius
bound, and an unverified directory is not one. The screened shapes:

- network egress (``curl``, ``wget``, ``nc``, ``ssh``) and ``token``, applied to
  a ``cmd::`` PoC's argv only. A ``pytest::`` nodeid is a test SELECTOR, not a
  program: it cannot itself reach the network, so word-screening it buys nothing
  -- what the selected test DOES was never visible to a string screen -- while
  refusing real nodeids from this codebase's dominant finding class, wherever a
  screened word stands alone: a parametrised id (``test_token[query]``), a module
  (``token.py``), a directory (``ssh/``).
- credential paths (``~/.aws``, ``~/.ssh``, the crew config), applied to both
  shapes, since no legitimate nodeid names one.
- a ``cmd::`` argument that is an absolute path or contains a ``..`` segment --
  the write-outside-the-worktree shape.

This screen is a NAMED-SHAPE CHECK on the AUDITOR'S text, not a sandbox. The
RFC's trust boundary is explicit: the auditor's finding is model-authored and
untrusted, so its PoC is screened before it runs; the target checkout is the
operator's own and is trusted. Containment is the disposable worktree plus the
deadline. This script does not attempt to defend the operator from code they
chose to check out and run as themselves -- there is no privilege drop to
defend, and Kiro Crew's namespace sandbox is Linux-only, which a cross-platform
verifier cannot take on.

What the child process gets: the PoC's argv, ``cwd`` set to the worktree, a
minimal environment, and a deadline. ``HOME`` points AT the worktree, so a
``~``-relative credential path resolves inside the throwaway checkout instead of
into the operator's real home.

Reads and writes one SQLite file through ``ledger.py``, and runs exactly one
subprocess. No network of its own.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Buffer
from pathlib import Path
from typing import Any

CONFIRMED = "confirmed"
REJECTED = "rejected"
NEEDS_HUMAN = "needs-human"

EXIT_CODES = {CONFIRMED: 0, REJECTED: 10, NEEDS_HUMAN: 20}
EXIT_INVALID = 2

DEFAULT_TIMEOUT = 120
# How long to wait for a killed proof to be reaped. Short and bounded: the verdict
# is already decided by the time this runs, so the only thing at stake is whether
# the verifier leaves a zombie behind, and blocking on an unkillable child forever
# would be worse than the zombie.
REAP_SECONDS = 5

PYTEST_PREFIX = "pytest::"
CMD_PREFIX = "cmd::"
POC_PREFIXES = (PYTEST_PREFIX, CMD_PREFIX)

# Programs whose presence in a cmd:: PoC means network egress. Matched as whole
# words over the whole argv rather than only as the leading program, so a proof
# that hides one inside `sh -c "..."` is refused too.
EGRESS_PROGRAMS = ("curl", "wget", "nc", "ssh")
# Credential shapes, matched as substrings: these are paths, not program names.
CREDENTIAL_SUBSTRINGS = ("~/.aws", "~/.ssh", ".kiro/crew/config.json")
_EGRESS_RE = re.compile(r"\b(" + "|".join(EGRESS_PROGRAMS) + r")\b", re.IGNORECASE)
# `token` as a whole word, so it does not fire on `tokenize`.
_TOKEN_RE = re.compile(r"\btoken\b", re.IGNORECASE)
# A parent-directory reference ANYWHERE inside an argument, in either spelling of
# a separator. Deliberately not anchored to the argument's start: the shapes that
# matter are quoted (`-c "open('../sibling','w')"`) and flag-attached
# (`--config=../outside.cfg`), and a boundary-anchored pattern missed both while
# looking correct. `...` in prose is still not a parent reference.
_PARENT_SEGMENT_RE = re.compile(r"\.\.[\\/]|[\\/]\.\.(?=[\\/]|$)|^\.\.$")
# An absolute path reference anywhere inside an argument: a `/` that begins a
# path (so `1/2` and `a / b` are arithmetic, not paths), a drive qualifier, or a
# UNC prefix. The screen errs toward refusal, which routes to a human rather
# than to a verdict.
_ABSOLUTE_REF_RE = re.compile(r"""(?:^|['"=,(\s])(?:/[\w.]|[A-Za-z]:[\\/]|\\\\[\w.])""")

# Exit statuses that mean the command never ran. 126 is "found but not
# executable", 127 is "not found" (what a shell inside a PoC reports where a
# direct spawn would have raised OSError instead).
LAUNCH_FAILURE_CODES = (126, 127)
# The uncaught-exception status of every common interpreter and shell.
AMBIGUOUS_CRASH_CODE = 1
# The largest status a proof exits ON PURPOSE. 128+N is a shell reporting signal N.
MAX_DELIBERATE_CODE = 125


class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname: str) -> Any:
        path = self.get_filename(fullname)
        source = self.get_data(path)
        return self.source_to_code(source, path)

    def set_data(self, path: str, data: Buffer, *, _mode: int = 0o666) -> None:
        return None


def load_ledger() -> Any:
    """Load the sibling ledger without cwd, sys.path, or bytecode side effects.

    Mirrors ``prepare-pr/scripts/pr_status.py``: a skill's scripts are synced out
    of the package tree and run as bare files, so ``ledger`` is a file beside
    this one rather than an importable module, and importing it the ordinary way
    would drop a ``__pycache__`` entry into the checked-out tree.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.py")
    name = "_security_conductor_ledger"
    loader = _NoBytecodeSourceLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot import security conductor ledger: " + path)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


_LEDGER: Any = None


def ledger() -> Any:
    """The ledger module, loaded once on first use.

    LAZY on purpose. ``finding_entry.py`` loads this script for the two PoC
    shapes it validates against, and a module-level load would execute the
    ledger a second time for a caller that only wanted two constants.
    """
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = load_ledger()
    return _LEDGER


def poc_body(poc: str) -> tuple[str, str] | None:
    """``(kind, payload)`` for a known PoC shape, else ``None``."""
    if poc.startswith(PYTEST_PREFIX):
        return "pytest", poc[len(PYTEST_PREFIX) :].strip()
    if poc.startswith(CMD_PREFIX):
        return "cmd", poc[len(CMD_PREFIX) :].strip()
    return None


def nodeid_file_part(nodeid: str) -> str:
    """The file a nodeid selects -- everything before the first ``::``."""
    return nodeid.split("::", 1)[0].strip()


def refusal_reason(poc: str) -> str | None:
    """Why this PoC must not be run at all, or ``None`` when it may be."""
    for needle in CREDENTIAL_SUBSTRINGS:
        if needle in poc:
            return (
                f"the proof of concept names {needle!r}, credential material the rules of"
                " engagement forbid reading; a human decides how to settle this finding"
            )
    parsed = poc_body(poc)
    if parsed is None:
        return None
    if parsed[0] != "cmd":
        # The WORD screens below do not apply to a pytest nodeid: it is a selector,
        # not a program, and they would refuse any nodeid where a screened word
        # stands alone -- a parametrised `test_token[query]`, a `token.py`, an
        # `ssh/` directory -- which in this codebase is the dominant real finding
        # class.
        #
        # The PATH screen does apply, and only to the FILE part. A nodeid's file
        # part IS a path that pytest resolves against the worktree, so `..` or a
        # root/drive prefix there selects code the scratch checkout does not
        # contain -- the same escape the cmd lane is screened for, spelled as a
        # selector instead of as an argument. The test-name part is excluded
        # because a parametrised id legitimately carries arbitrary text.
        selector = nodeid_file_part(parsed[1])
        if _PARENT_SEGMENT_RE.search(selector) or _ABSOLUTE_REF_RE.search(selector):
            return (
                f"the proof of concept selects {selector!r}, which is outside the scratch"
                " worktree; a proof may only run code the audited checkout contains"
            )
        return None
    body = parsed[1]
    egress = _EGRESS_RE.search(body)
    if egress is not None:
        return (
            f"the proof of concept names {egress.group(1)!r}, a network-egress shape the rules"
            " of engagement forbid; a human decides how to settle this finding"
        )
    if _TOKEN_RE.search(body) is not None:
        return (
            "the proof of concept names a token, which the rules of engagement forbid"
            " handling; a human decides how to settle this finding"
        )
    escape = _escaping_argument(body)
    if escape is not None:
        return (
            f"the proof-of-concept command names {escape!r}, which points outside the scratch"
            " worktree; the rules of engagement bound every write to that checkout, so a"
            " human decides how to settle this finding"
        )
    return None


def _escaping_argument(body: str) -> str | None:
    """The first ``cmd::`` argument that points outside the worktree, if any.

    An absolute path or a ``..`` reference is the write-outside-the-worktree
    shape. Screened per argument, and per argument INTERIOR rather than only at
    its start: the shapes that matter are quoted (``-c "open('../x','w')"``) and
    flag-attached (``--config=../x``), and a boundary-anchored check missed both
    while reading as correct.
    """
    try:
        arguments = shlex.split(body)
    except ValueError:
        # Unbalanced quoting: poc_argv refuses it as an unreadable shape, so
        # there is nothing here to screen.
        return None
    for argument in arguments:
        if _PARENT_SEGMENT_RE.search(argument) or _ABSOLUTE_REF_RE.search(argument):
            return argument
    return None


def is_git_worktree(directory: Path) -> bool:
    """Is this a git checkout -- a clone (``.git/`` directory) or a worktree?

    A linked worktree carries a ``.git`` FILE holding a gitdir pointer, not a
    directory, so an ``is_dir()`` check would reject exactly the layout the
    verifier brief asks for.
    """
    if not directory.is_dir():
        return False
    marker = directory / ".git"
    return marker.is_dir() or marker.is_file()


def is_own_checkout(worktree: Path) -> bool:
    """Is this the very tree these scripts are running from?

    The narrow, checkable half of "the worktree must be disposable". Provenance in
    general is NOT checkable here -- the caller chooses the path and therefore
    controls anything inside it, so no marker the verifier reads is unforgeable by
    the one party that could forge it -- but this case is decidable without trusting
    the caller at all, and it is the one an operator reaches by accident: pointing
    the verifier at the repository it lives in, where a relative write in a PoC
    lands on real source instead of on a throwaway copy.
    """
    try:
        here = Path(__file__).resolve()
        root = worktree.resolve()
    except OSError:  # pragma: no cover - unresolvable path, caller's own check reports it
        return False
    return root == here or root in here.parents


def child_env(worktree: Path) -> dict[str, str]:
    """The minimal environment a PoC runs in.

    Inherits nothing but ``PATH`` (a PoC needs an interpreter), the locale, and
    the three names a Windows process needs in order to start at all.
    ``HOME`` is the worktree itself, so a ``~``-relative path in a PoC resolves
    inside the throwaway checkout rather than into the operator's real home, and
    a tool that wants a cache directory writes it where the blast radius already
    is. ``TMPDIR`` follows for the same reason.

    The ``PATH`` fallback is ``os.defpath`` rather than a POSIX literal: a
    hardcoded ``/usr/bin:/bin`` names nothing on Windows, where the same
    fallback has to be a Windows search path.
    """
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(worktree),
        "TMPDIR": str(worktree),
        # Windows spells the scratch directory ``TEMP``/``TMP``, and that is what
        # ``tempfile`` reads there, so pinning ``TMPDIR`` alone put a PoC's scratch
        # files outside the throwaway checkout on that platform -- the one place the
        # blast radius is bounded.
        "TEMP": str(worktree),
        "TMP": str(worktree),
        "PYTHONDONTWRITEBYTECODE": "1",
        # Deterministic text decoding of whatever the child prints.
        "PYTHONIOENCODING": "utf-8",
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    # Windows needs three more names to start a process at all, and withholding
    # them hardens nothing: ``SYSTEMROOT`` is where CPython finds the crypto
    # provider it seeds ``os.urandom`` from, so a child without it dies during
    # interpreter startup and never runs the proof; ``PATHEXT`` and ``COMSPEC`` are
    # how a bare program name resolves to an executable there. Each is forwarded
    # only when the host defines it, so on POSIX this loop adds nothing rather than
    # branching on the platform.
    for name in ("SYSTEMROOT", "PATHEXT", "COMSPEC"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def poc_argv(poc: str, report_path: Path | None = None) -> tuple[list[str], str] | None:
    """``(argv, kind)`` for a runnable PoC, or ``None`` when the shape is unknown."""
    parsed = poc_body(poc)
    if parsed is None:
        return None
    kind, body = parsed
    if not body:
        return None
    if kind == "pytest":
        # A selector that names only a file is unverifiable: the report it produces
        # is keyed to no single test, so ANY failure in that file would read as the
        # claimed defect reproducing. Refusing the SHAPE is what keeps that out of
        # the ledger entirely -- `finding_entry.validate_poc` runs this same parser,
        # so an unkeyed selector is refused at filing time and not only at run time.
        if nodeid_test_name(body) is None:
            return None
        argv = [
            sys.executable,
            "-m",
            "pytest",
            # No cache and no header: the run must depend on the worktree's own
            # test, not on state beside it.
            "-p",
            "no:cacheprovider",
            "--no-header",
            "-q",
            "--tb=no",
        ]
        if report_path is not None:
            argv.append(f"--junitxml={report_path}")
        argv.append(body)
        return argv, kind
    try:
        argv = shlex.split(body)
    except ValueError:
        return None
    return (argv, kind) if argv else None


def nodeid_test_name(nodeid: str) -> str | None:
    """The test name a nodeid selects, or ``None`` when it names only a file."""
    if "::" not in nodeid:
        return None
    return nodeid.rsplit("::", 1)[1].strip() or None


def read_report(report_path: Path) -> str | None:
    """The report's text, or ``None`` when there is no readable file there.

    Read as text rather than parsed as XML only because the three questions asked
    of it -- which cases ran, what each was named, which outcome child it carries
    -- are answered by flat start tags, and a tree buys nothing for that.
    """
    try:
        return report_path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def report_cases(text: str) -> list[str]:
    """One chunk of report text per ``<testcase``, in document order.

    ``testcase`` elements are flat inside ``testsuite``, so splitting on the
    start tag gives each case its own text -- enough to see which outcome child
    it carries without building a tree.
    """
    return text.split("<testcase")[1:]


def _has_element(chunk: str, tag: str) -> bool:
    """Is ``<tag`` present as MARKUP in this chunk?

    Requires the tag name to be followed by whitespace, ``>`` or ``/``, so
    ``<failures>`` does not count as ``<failure``. A raw ``<`` can only be
    markup: XML escapes one inside an attribute value or text as ``&lt;``, so a
    test NAMED ``test_<failure/>`` cannot forge an outcome element here.
    """
    return bool(re.search(r"<" + tag + r"(?=[\s/>])", chunk))


def case_name(chunk: str) -> str | None:
    """The ``name`` attribute of the testcase this chunk starts.

    Two bounds, both load-bearing. The attribute name is anchored, because
    ``name="`` is a SUBSTRING of ``classname="`` and an unanchored search
    returned the module name for every case. And only the START TAG is searched
    -- text up to the first ``>`` -- so a nested ``<failure message="...">``
    cannot supply a name either.
    """
    start_tag = chunk.split(">", 1)[0]
    match = re.search(r'(?<![\w-])name="([^"]*)"', start_tag)
    return None if match is None else match.group(1)


def judge_report(report_path: Path, nodeid: str) -> tuple[str, str]:
    """Map pytest's JUnit report onto a verdict. Fails closed to ``needs-human``.

    The report is the channel: written to a path outside the worktree and keyed
    to the nodeid that was asked for, so an exit status that means "collection
    failed" or "a different test failed" cannot read as a reproduction.
    """
    if not report_path.exists():
        return (
            NEEDS_HUMAN,
            "pytest wrote no result report, so the proof did not run to completion;"
            " it neither reproduces nor refutes the finding",
        )
    text = read_report(report_path)
    if text is None:
        return (
            NEEDS_HUMAN,
            "pytest's result report could not be read; a report the verifier cannot read"
            " is not evidence either way",
        )
    cases = report_cases(text)
    if not cases:
        return (
            NEEDS_HUMAN,
            "pytest's result report names no test, so the proof of concept selected nothing",
        )
    wanted = nodeid_test_name(nodeid)
    if wanted is None:
        return (
            NEEDS_HUMAN,
            "the proof of concept names a file rather than one test, so no result in the"
            " report can be attributed to the defect this finding claims",
        )
    # ONLY the requested case decides. A run can report tests beside the one the
    # finding cites -- a module-level fixture, a parametrised sibling, whatever else
    # the selector's file collected -- and folding those in let an UNRELATED failure
    # confirm this finding. Naming the test was never enough on its own: the verdict
    # has to be read off that test's own result.
    requested = [chunk for chunk in cases if case_name(chunk) == wanted]
    if not requested:
        return (
            NEEDS_HUMAN,
            f"pytest's result report does not name {wanted!r}, so the test this finding"
            " cites did not run",
        )
    failed = [chunk for chunk in requested if _has_element(chunk, "failure")]
    errored = [chunk for chunk in requested if _has_element(chunk, "error")]
    skipped = [chunk for chunk in requested if _has_element(chunk, "skipped")]
    if failed:
        return (
            CONFIRMED,
            f"the proof of concept failed as the finding claims"
            f" ({len(failed)} of {len(requested)} run(s) of {wanted!r})",
        )
    if errored:
        return (
            NEEDS_HUMAN,
            f"the proof of concept errored rather than failing ({len(errored)});"
            " it did not run, so it neither reproduces nor refutes the finding",
        )
    if skipped and len(skipped) == len(requested):
        return NEEDS_HUMAN, "the proof of concept was skipped, so nothing was demonstrated"
    return (
        REJECTED,
        f"the proof of concept passed ({len(requested)} run(s) of {wanted!r}), so the"
        " defect it claims does not reproduce",
    )


def judge_cmd(returncode: int) -> tuple[str, str]:
    """Map a command PoC onto a verdict.

    Nonzero means the defect is present -- EXCEPT for the three statuses that say
    the command never ran: a death by signal, exit 126 (found, not executable)
    and exit 127 (not found). Those are not evidence about the finding.

    Exit 1 is the fourth. It is the uncaught-exception status of every common
    interpreter -- a Python PoC that dies on a hallucinated import exits 1, and so
    does a shell script whose first command is mistyped -- so it cannot be told
    apart from a PoC that ran and demonstrated the defect. A model-authored PoC
    crashing is the ORDINARY input here, not the rare one, and confirming on it
    persisted a false verdict into the ledger. A ``cmd::`` proof that wants to be
    confirmable exits a deliberate status in 2..125; exit 1 is handed to a human
    with the reason. A spawn failure is caught earlier, in :func:`run_poc`, as
    ``LaunchFailed``.
    """
    if returncode == 0:
        return (
            REJECTED,
            "the proof-of-concept command exited 0, so the defect it claims does not reproduce",
        )
    if returncode < 0:
        return (
            NEEDS_HUMAN,
            f"the proof-of-concept command was killed by signal {-returncode}, so it did not"
            " run to a verdict",
        )
    if returncode in LAUNCH_FAILURE_CODES:
        return (
            NEEDS_HUMAN,
            f"the proof-of-concept command exited {returncode}, which means it was not found"
            " or not executable rather than that the defect reproduced",
        )
    if returncode == AMBIGUOUS_CRASH_CODE:
        return (
            NEEDS_HUMAN,
            "the proof-of-concept command exited 1, which is also what an uncaught crash"
            " exits, so it cannot be told apart from a proof that never ran; a confirmable"
            " command proof exits a deliberate status in 2..125",
        )
    if returncode > MAX_DELIBERATE_CODE:
        # 128+N is a shell wrapper reporting its child's death by signal N, and a
        # Windows crash is a large positive status: neither is a status the proof
        # CHOSE, so neither is a claim about the defect.
        return (
            NEEDS_HUMAN,
            f"the proof-of-concept command exited {returncode}, which is above the range a"
            " proof exits on purpose and is the shape of a signal death or a crash",
        )
    return (
        CONFIRMED,
        f"the proof-of-concept command exited {returncode} as the finding claims",
    )


class Timeout:
    """The deadline was hit."""


class LaunchFailed:
    """The child could not be started at all."""

    def __init__(self, message: str) -> None:
        self.message = message


def reap(process: subprocess.Popen[bytes]) -> None:
    """Kill the proof if it is still running, then wait for it. Safe to call twice.

    The direct child only. A proof that detaches a grandchild is the operator's
    own checkout leaving a process on the operator's own machine, which the RFC's
    trust boundary places outside this script: there is no privilege drop to
    defend, and a portable process-tree kill does not exist in the standard
    library. The verdict is already decided when this runs, so a bounded wait
    beats blocking the conductor on an unreapable child.
    """
    process.kill()
    try:
        process.wait(timeout=REAP_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def run_poc(argv: list[str], worktree: Path, timeout: int) -> int | Timeout | LaunchFailed:
    """The child's return code, or why there is no return code.

    The child's OUTPUT is discarded, not captured. No verdict reads it -- the
    pytest lane reads the report file and the command lane reads the status --
    and capturing it through a pipe buffered the whole stream in this process,
    so a PoC that prints without stopping exhausted the verifier's memory before
    any verdict was written. ``timeout`` bounds wall time, never bytes.

    """
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(worktree),
            env=child_env(worktree),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # No shell, no inherited stdin: a PoC that waits on input must hit
            # the deadline rather than hang on a terminal nobody is watching.
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        # A mistyped or hallucinated program name is the verifier's ordinary
        # adversarial input, and it raises here rather than returning a status.
        # Letting it propagate killed the process outside the documented exit
        # contract with no verdict recorded at all.
        return LaunchFailed(str(exc))
    # No pipes are open, so waiting cannot deadlock on an unread buffer.
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        reap(process)
        return Timeout()
    return returncode


def read_finding(conn: Any, finding_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, surface, title, poc FROM findings WHERE id = ?", (finding_id,)
    ).fetchone()
    return None if row is None else {key: row[key] for key in row.keys()}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Re-run one finding's proof of concept and record the verdict"
    )
    parser.add_argument("--db", default=None, help="ledger path (default: data home)")
    parser.add_argument("--finding-id", required=True, type=int)
    parser.add_argument("--worktree", required=True, help="the scratch checkout to run in")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    return parser


def decide(finding: dict[str, Any], worktree: Path, timeout: int) -> tuple[str, str]:
    """The verdict and its reason, having run the proof of concept if allowed."""
    poc = str(finding.get("poc") or "")
    refusal = refusal_reason(poc)
    if refusal is not None:
        return NEEDS_HUMAN, refusal
    if not is_git_worktree(worktree):
        return (
            NEEDS_HUMAN,
            f"{worktree} is not a git worktree, and a scratch checkout is the whole"
            " blast-radius bound a proof of concept runs inside",
        )
    if is_own_checkout(worktree):
        return (
            NEEDS_HUMAN,
            f"{worktree} is the checkout these scripts are running from, so a proof of"
            " concept would run against real source rather than a throwaway copy",
        )
    parsed = poc_body(poc)
    if parsed is None:  # pragma: no cover - guarded by the caller's shape check
        raise ValueError(f"unrunnable proof-of-concept shape: {poc!r}")
    kind, body = parsed
    # The report lands OUTSIDE the worktree, so the audited checkout is not
    # writing into the directory the verdict is read from by accident.
    report_dir = Path(tempfile.mkdtemp(prefix="secc-verify-"))
    try:
        report_path = report_dir / "result.xml" if kind == "pytest" else None
        argv_and_kind = poc_argv(poc, report_path)
        if argv_and_kind is None:  # pragma: no cover - guarded by the caller
            raise ValueError(f"unrunnable proof-of-concept shape: {poc!r}")
        argv, _ = argv_and_kind
        outcome = run_poc(argv, worktree, timeout)
        if isinstance(outcome, Timeout):
            return (
                NEEDS_HUMAN,
                f"the proof of concept did not finish within {timeout}s; a proof that cannot be"
                " re-run inside the deadline is not evidence either way",
            )
        if isinstance(outcome, LaunchFailed):
            return (
                NEEDS_HUMAN,
                f"the proof of concept could not be started ({outcome.message}), so it says"
                " nothing about the finding",
            )
        if kind == "pytest":
            assert report_path is not None
            return judge_report(report_path, body)
        return judge_cmd(outcome)
    finally:
        shutil.rmtree(report_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.timeout <= 0:
        print("--timeout must be a positive number of seconds", file=sys.stderr)
        return EXIT_INVALID

    module = ledger()
    db_path = Path(args.db) if args.db else module.default_db_path()
    worktree = Path(args.worktree).expanduser()

    if not db_path.exists():
        # Verifying never CREATES a ledger. A finding has to have been filed to
        # be verified, and filing is what creates the database -- so a missing
        # one means a mistyped --db, and creating it there left an empty ledger
        # behind that later reads as "zero rules" to scope_check.
        print(
            f"no ledger at {db_path}; a finding must be filed before it can be verified",
            file=sys.stderr,
        )
        return EXIT_INVALID

    conn = module.connect(db_path)
    try:
        try:
            finding = read_finding(conn, args.finding_id)
        except Exception as exc:  # sqlite3.Error: not a ledger, or no findings table
            print(f"cannot read findings from {db_path}: {exc}", file=sys.stderr)
            return EXIT_INVALID
        if finding is None:
            print(f"no finding with id {args.finding_id}", file=sys.stderr)
            return EXIT_INVALID
        poc = str(finding.get("poc") or "")
        if refusal_reason(poc) is None and poc_argv(poc) is None:
            # An unreadable proof shape is an input defect in the RECORD, not a
            # verdict about the defect it claims, so nothing is recorded here.
            print(
                f"finding {args.finding_id} carries no runnable proof of concept"
                f" ({poc!r}); expected {PYTEST_PREFIX}<nodeid> or {CMD_PREFIX}<argv>",
                file=sys.stderr,
            )
            return EXIT_INVALID
        verdict, reason = decide(finding, worktree, args.timeout)
        module.record_verdict(
            conn,
            finding_id=args.finding_id,
            role="verifier",
            verdict=verdict,
            reason=reason,
        )
    finally:
        conn.close()

    print(
        json.dumps(
            {"finding_id": args.finding_id, "verdict": verdict, "reason": reason}, sort_keys=True
        )
    )
    return EXIT_CODES[verdict]


if __name__ == "__main__":
    sys.exit(main())
