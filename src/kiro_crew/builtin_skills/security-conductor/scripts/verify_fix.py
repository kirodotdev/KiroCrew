#!/usr/bin/env python3
"""Check that a security fix killed the finding and broke nothing legitimate.

A fix that makes the proof of concept stop reproducing has done half a job. The
other half is the half a security change actually gets rejected for: the deny
fence grew a rule that also refuses ``gh pr view --json``, or a path guard now
rejects the operator's own worktree, and the tool the fix protected became one
nobody can use. Two failure modes -- **the tool became unusable** and **platform
lock-in** -- and the committed ``golden-paths.json`` beside this skill is their
corpus: the legitimate operations every security fix must keep alive. Both are
nameable from this repository's own history rather than from any clause:
read-only ``gh pr view --json`` shapes have been refused by a widened rule, and a
guard written against one host's interpreter path has cost the other host's test
lane.

So this is a two-step gate, and both steps must hold::

    python3 verify_fix.py [--db PATH] --finding-id N --worktree DIR [--timeout SECONDS]

1. ``verify_finding.py`` re-runs the proof. The fix holds ONLY when that pass
   comes back ``rejected`` -- the proof does not reproduce any more.
   ``confirmed`` means the fix did not land, and anything else means the question
   was not settled.
2. Every ``shell`` golden path in the committed corpus whose platform matches
   the host is re-classified against the FIXED code. None of them may be refused.

Exit codes, which are the interface::

    0   holds        -- the proof is dead and every shell golden path is permitted
    10  reproduces   -- the proof still reproduces; the fix did not land
    30  broken       -- at least one golden path is refused (the rows are
                        printed); this is the "tool became unusable" rejection
    20  unverifiable -- something this script owns could not be settled: the
                        verifier is absent, the deny composite is not readable, or
                        the committed corpus is absent, does not load, or is empty
    2   invalid input -- a bad argument, or a worktree that is not a checkout

Precedence when several apply is ``10 > 30 > 20 > 0``, and it is not arbitrary. A
proof that still reproduces means the fix does not exist yet, so what it did to
the golden paths is not yet a question. A broken golden path outranks an
unverifiable one because it is the actionable verdict: a named row, a named
reason, something to change. And **0 is unreachable while any check this script
owns went unsettled** -- an unclassified golden path is not a permitted one, and
reporting it as one is how a fix that broke the tool ships green. The same rule
covers the corpus as a whole: a corpus file that is missing, does not load, or
holds no row checked zero rows, and zero rows checked is 20, not a pass -- it is
a broken installation, exactly like an absent verifier or a sibling ``ledger.py``
that will not load. Every one of those is reported through the same JSON payload
and exit 20, never a traceback. "Corpus present, none for this host" is the
different case and passes on the rows that do apply.

stdout is one JSON object, carrying ``broken``, ``unverifiable`` and
``needs_human`` as lists of rows, so a reviewer gets the specific operations
rather than a count. A row is named by ``entry``, its position in the corpus
file, which is the name ``ledger.py`` gives a malformed one too.

**The corpus is the committed file, not the ledger's ``golden_paths`` table.**
The RFC rules it: "both gates read the file and nothing else, and a row that is
not in the committed export does not gate." Two reasons, and either alone would
decide it. The ledger is per-host and invisible to a CI runner, so a gate that read
it could not be enforced in the one place it is declared blocking. And the table is
mutable by anything that can reach the database -- ``ledger.py`` says outright that
its CLI is not an authentication boundary -- so a gate that read it could be steered
by a ledger write: retire the one row the fix broke, and the gate goes green
without a line of the fix changing. The committed file is reviewed text; changing
what the gate checks means changing a file in a pull request, where it shows. It is
read from beside THIS script -- the skill's own copy, not the worktree under
review -- so the change being judged cannot rewrite the gate that judges it. The
table keeps its job as the editing surface: an audit proposes a row, a human
approves it, and it reaches the gate when it lands in this file through review.

**No argument can shrink the corpus.** The file is the one beside this script and
the platform is the host's own, and there is deliberately no flag for either: a
``--corpus`` would let the caller hand the gate a smaller file, and a
``--platform`` would let it drop this host's rows by naming the other host. Both
are the same move as a ledger write, one step removed -- the fixer running the
gate chooses what the gate checks -- so neither exists. What the caller names is
the finding and the worktree; what gets checked is decided here.

**The fence classified against is the worktree's own, proven, not assumed.** The
probe puts ``<worktree>/src`` at the head of ``PYTHONPATH``, but a prepend is a
preference, not a guarantee: a checkout that does not carry ``kiro_crew/security``
would import the INSTALLED package instead, and every golden path would be
classified against rules that are not the ones under review. So the probe checks
where ``kiro_crew.security`` actually came from and reports the composite
unavailable (exit 20) unless that file resolves beneath the worktree's ``src``.
A fence borrowed from somewhere else is not a fence that agreed.

**Nothing out of the corpus is ever executed.** That is the load-bearing rule
here, and it is what decides how each kind is treated:

``shell``
    Classified by the tool gate's deny composite, never run. The claim a shell row
    makes is "the gate must not refuse this", so classification answers it exactly
    and running the command would answer a different question. "Refused" means
    refused by ANY of the four checks ``hooks.on_tool_call`` applies to a shell
    command, in its order -- the path fence, the sensitive-command tier, the
    exfiltration auditor, and the deny-rule catalog (:data:`TIERS`, the same table
    ``scripts/deny_diff.py`` declares). Measuring the catalog alone would go green
    on a fix that tightened any of the other three, which is the specific way this
    gate could ship a meaningless pass. The verdict names the tier that refused,
    because "the path fence refused it" and "a catalog rule matched it" need
    different fixes. The composite is called in a CHILD process with the worktree's own ``src`` ahead of
    everything on ``PYTHONPATH``, because the point is to classify against the
    FIXED code: importing it in this process would bind whatever copy of the
    package the interpreter already loaded, which for a test runner inside the
    repository is the unfixed one. When the import fails, or the tree lacks one of
    the four checks, every shell row is ``unverifiable`` and the verdict is 20 -- a
    fence that cannot be read is not a fence that agreed, and a tier that is missing
    is coverage silently lost, not a tier that permitted. The probe's argv is FIXED -- this script re-entered with
    ``--classify-stdin`` -- and there is deliberately no flag to substitute another
    program: one would execute caller-supplied argv with the operator's access,
    outside the tool gate the outer invocation passed.

``flow`` and ``cron``
    Recorded, reported, and left to a human. Neither is executed and neither
    moves the exit code, because neither is a check this script can make:

    * A corpus row is not authorization. It is text in a JSON file, and the
      same text once imported sits in a table whose CLI ``ledger.py`` states
      plainly is not an authentication boundary -- ``--approved-by`` is an
      unverified caller assertion -- so a row is data written by whoever could
      edit the file or reach the database. Every other consumer only READS such a
      row; running one as argv would turn a file edit into command execution with
      the operator's access, which is a privilege escalation no approver field
      gates. No containment fixes that: the escalation is in treating the row as
      permission, not in how the child is confined.
    * A schedule is worse than unhelpful to execute -- firing one has effects
      outside the worktree that no deadline bounds -- and parsing it here would
      settle nothing either, because a parse in THIS process never consults the
      fixed code, so its answer is a constant no fix can change.

    Their value is the corpus, not a verdict: they name the operations
    (``monitor_start``, a chat turn, a shipped cron) a human has to exercise, and
    they are printed for exactly that. The shipped corpus's own well-formedness is
    asserted in the test suite, at review time, where a corpus-authoring mistake
    belongs.

Trust boundary. The target checkout is the operator's own code, which they chose
to run as themselves; containment for the verifier child is that disposable
checkout plus the deadline, not a sandbox. What this script adds on top is the
rule above: the corpus is data, never an instruction.

Reads the committed corpus through ``ledger.py``'s loader, resolves the ledger
path only to hand it to the verifier, and runs two kinds of child process -- the
sibling verifier and its own classifier probe. No network of its own.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HOLDS = "holds"
REPRODUCES = "reproduces"
BROKEN = "broken"
UNVERIFIABLE = "unverifiable"

EXIT_CODES = {HOLDS: 0, REPRODUCES: 10, UNVERIFIABLE: 20, BROKEN: 30}
EXIT_INVALID = 2

#: Strongest verdict first. :func:`fold_verdict` walks this, so the precedence
#: documented above lives in ONE place and cannot drift from a chain of ``if``
#: statements that happen to be written in some order.
VERDICT_PRECEDENCE = (REPRODUCES, BROKEN, UNVERIFIABLE, HOLDS)

#: The kind whose claim this script can settle by itself. Every other kind is
#: corpus for a human, so this is the whole of what the exit code covers.
CHECKED_KIND = "shell"

#: The committed export, one directory up from ``scripts/`` -- beside the skill,
#: where ``scope_check.py`` finds ``rules-of-engagement.json`` for the same reason.
CORPUS_FILENAME = "golden-paths.json"

DEFAULT_TIMEOUT = 120
#: How long to wait for a killed child to be reaped. Bounded for the reason
#: ``verify_finding.py`` bounds its own: the verdict is already decided, so the
#: only thing at stake is a leftover process, and blocking the conductor forever
#: on an unkillable child is worse than that.
REAP_SECONDS = 5

#: ``verify_finding.py``'s exit codes, which are its interface. Mapped rather than
#: re-derived: this script reads that contract and must not grow a second opinion
#: about what ``confirmed`` means.
VERIFIER_CONFIRMED = 0
VERIFIER_REJECTED = 10
VERIFIER_NEEDS_HUMAN = 20
VERIFIER_INVALID = 2


class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname: str) -> Any:
        path = self.get_filename(fullname)
        source = self.get_data(path)
        return self.source_to_code(source, path)

    def set_data(self, path: str, data: Any, *, _mode: int = 0o666) -> None:
        return None


def script_dir() -> Path:
    return Path(os.path.dirname(os.path.abspath(__file__)))


def load_ledger() -> Any:
    """Load the sibling ledger without cwd, sys.path, or bytecode side effects.

    Mirrors ``verify_finding.py``: a skill's scripts are synced out of the package
    tree and run as bare files, so ``ledger`` is a file beside this one rather
    than an importable module, and importing it the ordinary way would drop a
    ``__pycache__`` entry into the checked-out tree.
    """
    path = str(script_dir() / "ledger.py")
    name = "_security_conductor_ledger_for_fix"
    loader = _NoBytecodeSourceLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot import security conductor ledger: " + path)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def corpus_path() -> Path:
    """The export beside the skill, one directory up from ``scripts/``.

    The ONLY corpus this script ever reads. Not a default that a flag overrides --
    a caller who could name another file could name a smaller one.
    """
    return script_dir().parent / CORPUS_FILENAME


def load_corpus(ledger: Any, path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """The committed corpus, every row numbered by its position in the file.

    Returns ``(rows, problem)``. ``problem`` is set, and ``rows`` empty, when the
    file is absent, unreadable, does not validate, or holds no row at all; the
    caller reports that as ``unverifiable``, because a gate whose corpus cannot be
    read agreed to nothing, and a corpus of zero rows checked nothing -- the same
    refusal ``scripts/deny_diff.py`` makes of an empty corpus. Validation is
    ``ledger.py``'s own -- the same check ``import-golden-paths`` applies -- so the
    file the gate reads and the file the ledger loads cannot be judged by two
    different rules.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        return [], f"the committed corpus is not readable: {path} ({reason})"
    try:
        rows = ledger.load_golden_path_corpus(text)
    except ValueError as exc:
        return [], f"the committed corpus does not load: {path}: {exc}"
    if not rows:
        return [], f"the committed corpus holds no golden path: {path}"
    for index, row in enumerate(rows):
        row["entry"] = index
    return rows, None


def host_platform() -> str:
    """The concrete host name a golden path's ``platform`` column is matched to.

    Derived from the host and from nothing else: a caller who could name the other
    host would drop this host's rows from the check. Never ``any`` -- that is a
    property of a ROW (it applies everywhere), not a host a check could run on.
    """
    return "windows" if os.name == "nt" else "posix"


def rows_for_host(rows: list[dict[str, Any]], platform: str) -> list[dict[str, Any]]:
    """The rows one host must keep alive: its own and the ``any`` rows.

    The other host's shape is ABSENT rather than present-and-skipped: reporting a
    Windows-only command as broken on Linux would be a false rejection of a fix.
    """
    return [row for row in rows if row["platform"] in ("any", platform)]


def is_git_worktree(directory: Path) -> bool:
    """Is this a git checkout -- a clone (``.git/`` directory) or a linked worktree?

    The same screen ``verify_finding.py`` applies, and for the same reason: "in a
    scratch checkout" is the whole blast-radius bound for the proof this script
    is about to re-run, and a directory nobody has established is one is not it.
    Screened HERE too so the answer is a named exit 2 rather than a 20 relayed out
    of the child.

    A linked worktree carries a ``.git`` FILE holding a gitdir pointer rather than
    a directory, so an ``is_dir()`` check would reject exactly the layout the
    conductor's brief asks for.
    """
    if not directory.is_dir():
        return False
    marker = directory / ".git"
    return marker.is_dir() or marker.is_file()


def child_env(worktree: Path, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The closed environment a child runs in.

    Inherits nothing but ``PATH`` (a child needs an interpreter), the locale, and
    the names a Windows process needs to start at all. ``HOME`` and every scratch
    directory spelling point AT the worktree, so a ``~``-relative path or a tool's
    cache lands inside the throwaway checkout instead of the operator's real home.
    Kept deliberately identical in shape to ``verify_finding.py``'s, because "the
    same containment as the proof" is the promise, and two almost-equal allowlists
    would be two things to keep aligned.
    """
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(worktree),
        "TMPDIR": str(worktree),
        # Windows spells the scratch directory ``TEMP``/``TMP``, and that is what
        # ``tempfile`` reads there, so pinning ``TMPDIR`` alone would put a
        # child's scratch files outside the one place the blast radius is bounded.
        "TEMP": str(worktree),
        "TMP": str(worktree),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    # Three names Windows needs in order to start a process at all, and
    # withholding them hardens nothing: ``SYSTEMROOT`` is where CPython finds the
    # crypto provider it seeds ``os.urandom`` from, and ``PATHEXT``/``COMSPEC``
    # are how a bare program name resolves there. Forwarded only when the host
    # defines them, so on POSIX this loop adds nothing rather than branching.
    for name in ("SYSTEMROOT", "PATHEXT", "COMSPEC"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    if extra:
        env.update(extra)
    return env


def reap(process: subprocess.Popen[bytes]) -> None:
    """Kill a child that hit the deadline, then wait once. Safe to call twice.

    The DIRECT child only, matching ``verify_finding.py`` exactly. A process-tree
    kill has no portable spelling in the standard library -- every one available is
    POSIX-only -- and a verifier whose own teardown works on a single host would be
    the PLATFORM LOCK-IN this corpus exists to catch. The two children spawned here
    are a checked-in script and this script itself, so a detached grandchild is the
    operator's own code on the operator's own machine, inside the boundary the RFC
    already draws there.
    """
    try:
        process.kill()
    except OSError:  # pragma: no cover - already reaped
        pass
    try:
        process.wait(timeout=REAP_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def run_child(
    argv: list[str],
    worktree: Path,
    timeout: int,
    *,
    env_extra: dict[str, str] | None = None,
    stdin_text: str | None = None,
    capture: bool = False,
) -> tuple[str, int, str]:
    """Run one child. Returns ``(outcome, returncode, text)``.

    ``outcome`` is ``ran``, ``timeout`` or ``launch-failed``, so a caller never has
    to read one sentinel return code as three different things -- the mistake
    ``verify_finding.py`` documents at length for proofs.

    Output is captured ONLY when the caller needs to parse it. The verifier's
    output is discarded through ``DEVNULL`` rather than a pipe, because nothing
    reads it and a child that prints without stopping would otherwise buffer its
    whole stream in this process before any verdict is written.
    """
    pipe = subprocess.PIPE if capture else subprocess.DEVNULL
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(worktree),
            env=child_env(worktree, extra=env_extra),
            stdout=pipe,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        )
    except OSError as exc:
        # A missing interpreter raises here rather than returning a status, and
        # letting it propagate would end the run outside the documented exit
        # contract with no verdict written at all.
        return "launch-failed", 0, str(exc)
    try:
        payload = None if stdin_text is None else stdin_text.encode("utf-8")
        out, _ = process.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        reap(process)
        return "timeout", 0, ""
    text = "" if not out else out.decode("utf-8", errors="replace")
    return "ran", int(process.returncode), text


# --------------------------------------------------------------------- step 1


def verifier_path() -> Path:
    return script_dir() / "verify_finding.py"


def run_verifier(
    *, db: Path, finding_id: int, worktree: Path, timeout: int
) -> tuple[str, str, int]:
    """Re-run the proof through ``verify_finding.py``. ``(verdict, reason, exit)``.

    The sibling is invoked BY PATH rather than imported, and its exit status is the
    whole contract this reads. That keeps one implementation of what ``confirmed``
    means: copying its judgement here would give the harness two verifiers that can
    disagree about the same proof, and the one a reviewer reads would be whichever
    they happened to run.

    The sibling ships in this same skill bundle, so an absent one is a BROKEN
    INSTALLATION rather than a state to accommodate. It is still ``unverifiable``
    and never a pass -- a proof that was not re-run says nothing about the fix -- and
    it is reported as one more invocation that could not run, which is what it is.

    The check is here rather than left to the spawn: the interpreter exists, so a
    nonexistent script argument makes the CHILD exit 2, which is the verifier's own
    code for "I rejected your input". Reading that as a rejected finding-id would
    turn a broken install into a lie about the caller's arguments.
    """
    script = verifier_path()
    if not script.is_file():
        return (
            UNVERIFIABLE,
            f"verify_finding.py could not be invoked: {script} is not a file."
            " It ships beside this script, so this is a broken installation;"
            " the proof was not re-run, and nothing about this fix is settled",
            0,
        )
    # ``--db`` is always passed, never left to the child's own default. The child
    # runs with ``HOME`` pointed at the worktree, and the default resolves under
    # ``HOME`` -- so omitting it made the verifier read a DIFFERENT ledger from the
    # one this script reads golden paths out of, and report "no such finding" for a
    # finding that is right there.
    argv = [
        sys.executable,
        str(script),
        "--db",
        str(db),
        "--finding-id",
        str(finding_id),
        "--worktree",
        str(worktree),
        "--timeout",
        str(timeout),
    ]
    # The verifier's own deadline bounds the proof; this outer one only bounds the
    # verifier's bookkeeping around it, so it gets room past the inner one rather
    # than racing it and reporting a timeout the proof did not cause.
    outcome, code, _ = run_child(argv, worktree, timeout + REAP_SECONDS + 30)
    if outcome != "ran":
        return UNVERIFIABLE, f"verify_finding.py did not complete ({outcome})", 0
    if code == VERIFIER_REJECTED:
        return HOLDS, "the proof does not reproduce any more", code
    if code == VERIFIER_CONFIRMED:
        return REPRODUCES, "the proof still reproduces; the fix did not land", code
    if code == VERIFIER_INVALID:
        return "invalid", "verify_finding.py rejected its input", code
    if code == VERIFIER_NEEDS_HUMAN:
        return UNVERIFIABLE, "verify_finding.py could not settle the proof", code
    return UNVERIFIABLE, f"verify_finding.py exited {code}, which is not in its contract", code


# ------------------------------------------------------- step 2: shell rows


#: The deny checks the tool gate applies to a shell command, in the order
#: ``hooks.on_tool_call`` applies them, as (tier name, attribute of
#: ``kiro_crew.security``). Declared as data, and kept IDENTICAL to ``_TIERS`` in
#: ``scripts/deny_diff.py``: the two gates make the same claim -- "no golden path is
#: refused at the tool gate" -- and it holds only while both measure the same
#: composite. A check the tree does not carry is coverage lost, and the probe
#: reports it as unavailable rather than skipping it.
TIERS: tuple[tuple[str, str], ...] = (
    ("sensitive-path", "is_sensitive_path"),
    ("sensitive-bash", "is_sensitive_bash_command"),
    ("exfil", "audit_bash_exfiltration"),
    ("deny-rules", "is_denied"),
)

#: Reason reported for a tier whose check answers True/False rather than a string.
#: The path fence is the one such check, and a bare ``True`` would otherwise render
#: as an empty refusal.
BOOL_TIER_REASON = "Blocked: access to sensitive path"

#: This script re-entered in probe mode. The probe imports the product's fence, so
#: it is spelled as a re-entry rather than as an inline ``-c`` program: the same
#: import in a less inspectable shape, and its code would live in a string instead
#: of in the file a reviewer is already reading.
CLASSIFY_FLAG = "--classify-stdin"


def classifier_python(worktree: Path) -> str:
    """The interpreter the fence is read with: the worktree's own, when it has one.

    A checkout under review may pin dependencies the running interpreter does not
    have, and the fence has to be imported the way the fixed tree would import it.
    Falls back to this interpreter, which is correct whenever the package imports
    from source alone.
    """
    candidates = (
        worktree / ".venv" / "bin" / "python",
        worktree / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def probe_argv(worktree: Path) -> list[str]:
    """The ONE argv the classifier probe ever has: this file, re-entered.

    Fixed rather than configurable, and that is a security property, not a missing
    feature. A flag that took an argv here would decode caller-supplied text straight
    into ``subprocess.Popen`` -- arbitrary command execution that never passes the
    tool gate the outer invocation did. What varies is only WHICH fence the probe
    imports, and that is decided by the worktree: its ``src`` leads ``PYTHONPATH``,
    so the tree under review supplies ``kiro_crew.security``. A test that needs a
    different fence stages one there.
    """
    return [classifier_python(worktree), os.path.abspath(__file__), CLASSIFY_FLAG]


def parse_probe_output(text: str, commands: list[str]) -> tuple[bool, dict[str, str | None], str]:
    """The probe's stdout as a verdict. ``(available, {cmd: reason}, note)``.

    Pure, so it is testable without a second producer: the only writer of this
    payload is :func:`classify_stdin` in this same file, which always emits a
    well-formed object -- so no shape below is reachable from the shipped probe, and
    the checks exist for the reason a parser validates any input it did not just
    construct in-process. An unreadable answer already has a verdict, and it is 20;
    reading a field off an unchecked payload would instead raise and exit 1, outside
    the contract these exit codes ARE.
    """
    lines = text.strip().splitlines()
    if not lines:
        return False, {}, "the deny classifier probe printed no JSON verdict"
    try:
        parsed = json.loads(lines[-1])
    except ValueError:
        return False, {}, "the deny classifier probe printed no JSON verdict"
    if not isinstance(parsed, dict):
        return False, {}, "the deny classifier probe printed JSON that is not an object"
    if not parsed.get("available"):
        return False, {}, str(parsed.get("error") or "the deny classifier is not importable")
    raw = parsed.get("results")
    if not isinstance(raw, list):
        return False, {}, "the deny classifier probe printed no result list"
    results: dict[str, str | None] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("command"), str):
            return False, {}, "the deny classifier probe printed a malformed result entry"
        reason = item.get("reason")
        if reason is not None and not isinstance(reason, str):
            return False, {}, "the deny classifier probe printed a non-text refusal reason"
        results[item["command"]] = reason
    missing = [command for command in commands if command not in results]
    if missing:
        # A probe that answered about only some commands is not a fence that
        # permitted the rest. Refusing the whole batch keeps the unanswered rows out
        # of the passing set.
        return False, {}, f"the deny classifier probe skipped {len(missing)} command(s)"
    return True, results, ""


def classify_commands(
    commands: list[str], worktree: Path, timeout: int
) -> tuple[bool, dict[str, str | None], str]:
    """Ask the deny fence about each command. ``(available, {cmd: reason}, note)``.

    ``reason`` is ``None`` for a command the fence permits and a refusal string for
    one it denies -- ``is_denied``'s own return shape, carried through rather than
    reduced to a boolean, because the reason is what tells a reviewer WHICH rule ate
    their golden path.

    ``available`` false means the fence could not be read at all, which every
    caller must turn into ``unverifiable``.
    """
    if not commands:
        return True, {}, ""
    fence_root = worktree / "src"
    # The probe is told where the fence MUST have come from, so it can prove the
    # import landed there rather than falling through to an installed package.
    payload = json.dumps({"commands": commands, "fence_root": str(fence_root)})
    outcome, code, text = run_child(
        probe_argv(worktree),
        worktree,
        timeout,
        env_extra={"PYTHONPATH": str(fence_root)},
        stdin_text=payload,
        capture=True,
    )
    if outcome != "ran":
        return False, {}, f"the deny classifier probe did not complete ({outcome})"
    if code != 0:
        return False, {}, f"the deny classifier probe exited {code}"
    return parse_probe_output(text, commands)


def fence_provenance_problem(module_file: str | None, fence_root: str) -> str | None:
    """Why an imported fence is NOT the worktree's, or ``None`` when it is.

    Both sides are compared as real paths, so a worktree whose ``src`` is a symlink
    to the source tree (the way an end-to-end smoke stages one) still matches, and
    a package imported from site-packages never does. An empty ``fence_root`` is
    the parent failing to say where the fence must come from, and a module with no
    ``__file__`` is a namespace package with no source of its own; neither is a
    fence this probe can vouch for.
    """
    if not fence_root:
        return "kiro_crew.security: the probe was not told which worktree owns the fence"
    if not module_file:
        return "kiro_crew.security has no source file; it is not the worktree's fence"
    root = os.path.realpath(fence_root)
    actual = os.path.realpath(module_file)
    if actual == root or actual.startswith(root.rstrip(os.sep) + os.sep):
        return None
    return (
        f"kiro_crew.security was imported from {actual}, outside the worktree's src"
        f" ({root}); the tree under review carries no fence to classify against"
    )


def classify_stdin(stream: Any, out: Any) -> int:
    """The probe, running inside the child: classify stdin's commands, print JSON.

    Imports the product's fence HERE, in a process whose ``PYTHONPATH`` leads the
    worktree, and reports an unavailable import as data rather than as a crash --
    the parent has a verdict for "the fence cannot be read" and none for a
    traceback. The import is then checked for PROVENANCE: ``kiro_crew.security``
    must resolve beneath the ``fence_root`` the parent named, or the composite is
    unavailable. A leading ``PYTHONPATH`` entry only wins when the tree has the
    package; a checkout without it would otherwise borrow the installed fence and
    classify every golden path against code that is not under review.
    """
    try:
        request = json.loads(stream.read() or "{}")
    except ValueError as exc:
        json.dump({"available": False, "error": f"unreadable request: {exc}"}, out)
        out.write("\n")
        return 0
    commands = [str(item) for item in (request.get("commands") or [])]
    fence_root = str(request.get("fence_root") or "")
    try:
        # Loaded HERE, in the child, by name: the whole point of the probe is that the
        # fence is bound in a process whose PYTHONPATH leads the worktree. A module-level
        # import would bind whatever copy the parent interpreter already had -- and fail
        # outright when the skill runs as a bare synced file with no package on the path.
        security = importlib.import_module("kiro_crew.security")
    except Exception as exc:  # noqa: BLE001 - any import failure is the same verdict
        json.dump({"available": False, "error": f"kiro_crew.security: {exc}"}, out)
        out.write("\n")
        return 0
    borrowed = fence_provenance_problem(getattr(security, "__file__", None), fence_root)
    if borrowed is not None:
        json.dump({"available": False, "error": borrowed}, out)
        out.write("\n")
        return 0
    checks: list[tuple[str, Any]] = []
    for name, attribute in TIERS:
        check = getattr(security, attribute, None)
        if check is None:
            # A missing tier is coverage lost, not a tier that permitted. Reporting
            # the composite as unavailable keeps the shell rows out of the passing
            # set instead of certifying them against three checks out of four.
            json.dump({"available": False, "error": f"kiro_crew.security has no {attribute}"}, out)
            out.write("\n")
            return 0
        checks.append((name, check))
    results = []
    for command in commands:
        reason: str | None = None
        for name, check in checks:
            try:
                outcome = check(command)
            except Exception as exc:  # noqa: BLE001 - a raising check is unreadable, not a pass
                json.dump({"available": False, "error": f"{name} raised: {exc}"}, out)
                out.write("\n")
                return 0
            if not outcome:
                continue
            # The path fence answers True/False; the others answer a reason or None.
            # Both mean refused, and the tier is named so the reviewer knows which
            # fix to reach for.
            text = BOOL_TIER_REASON if outcome is True else str(outcome)
            reason = f"[{name}] {text}"
            break
        results.append({"command": command, "reason": reason})
    json.dump({"available": True, "results": results}, out)
    out.write("\n")
    return 0


# -------------------------------------------------------- step 2: the check


def describe_row(row: dict[str, Any], why: str) -> dict[str, Any]:
    """One row as a reviewer needs it: what it is, and what happened to it.

    ``entry`` is the row's position in the corpus file, which is what a reviewer
    opens. ``reason`` -- the human's own note about why the operation matters --
    travels with every entry, because it is what tells a reviewer whether to change
    the fix or retire the row.
    """
    return {
        "entry": int(row["entry"]),
        "kind": str(row["kind"]),
        "surface": str(row["surface"]),
        "platform": str(row["platform"]),
        "command_or_flow": str(row["command_or_flow"]),
        "reason": str(row["reason"]),
        "why": why,
    }


def check_golden_paths(
    rows: list[dict[str, Any]], worktree: Path, timeout: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
    """Classify the shell corpus; hand every other kind to a human, unexecuted.

    Returns ``(broken, unverifiable, needs_human, checked)``. The split is the
    design: the first two are verdicts about checks this script makes, and the third
    is corpus it deliberately makes no check about -- see the module docstring for
    why a corpus row is never run.

    ``checked`` counts the shell rows only, which is the set this script made a
    claim about. Reporting the whole table there would let a corpus of nothing but
    human rows describe itself as fully checked.
    """
    broken: list[dict[str, Any]] = []
    unsettled: list[dict[str, Any]] = []
    needs_human: list[dict[str, Any]] = []

    shell_rows = [row for row in rows if str(row["kind"]) == CHECKED_KIND]
    commands = sorted({str(row["command_or_flow"]) for row in shell_rows})
    available, verdicts, note = classify_commands(commands, worktree, timeout)
    for row in shell_rows:
        if not available:
            unsettled.append(describe_row(row, note))
            continue
        refusal = verdicts.get(str(row["command_or_flow"]))
        if refusal is not None:
            broken.append(describe_row(row, f"the deny fence refuses it: {refusal}"))

    for row in rows:
        kind = str(row["kind"])
        if kind == CHECKED_KIND:
            continue
        needs_human.append(
            describe_row(
                row,
                f"a {kind} golden path is never run from the corpus;"
                " a human has to exercise this operation",
            )
        )

    return broken, unsettled, needs_human, len(shell_rows)


def fold_verdict(*candidates: str) -> str:
    """The strongest verdict present, per :data:`VERDICT_PRECEDENCE`.

    One walk over a declared order rather than a chain of ``if`` statements, so the
    ladder documented in the module docstring is the ladder that runs. The property
    that matters is the last one: ``holds`` is reachable only when no stronger
    verdict is present at all.
    """
    for verdict in VERDICT_PRECEDENCE:
        if verdict in candidates:
            return verdict
    return HOLDS  # pragma: no cover - every caller passes at least one candidate


# ---------------------------------------------------------------------- CLI


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a security fix and its golden paths")
    parser.add_argument("--db", default=None, help="ledger path (default: data home)")
    parser.add_argument("--finding-id", type=int, default=None)
    parser.add_argument("--worktree", default=None)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    # Deliberately NO flag that names the classifier program, the corpus file, or the
    # platform. The probe's argv is this file (:func:`probe_argv`); a flag that let a
    # caller substitute one would execute caller-supplied argv with the operator's
    # access, outside the tool gate. The corpus is the file beside this script and
    # the platform is the host's (:func:`corpus_path`, :func:`host_platform`); a flag
    # for either would let the caller shrink what the gate checks.
    parser.add_argument(CLASSIFY_FLAG, action="store_true", dest="classify_stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.classify_stdin:
        return classify_stdin(sys.stdin, sys.stdout)

    if args.finding_id is None or args.worktree is None:
        print("--finding-id and --worktree are both required", file=sys.stderr)
        return EXIT_INVALID
    if args.timeout <= 0:
        print("--timeout must be a positive number of seconds", file=sys.stderr)
        return EXIT_INVALID
    worktree = Path(args.worktree)
    if not worktree.is_dir():
        print(f"--worktree is not a directory: {worktree}", file=sys.stderr)
        return EXIT_INVALID
    if not is_git_worktree(worktree):
        print(
            f"--worktree is not a git checkout: {worktree};"
            " a disposable checkout is the whole blast-radius bound",
            file=sys.stderr,
        )
        return EXIT_INVALID
    # The sibling ledger ships in this same bundle, so one that is absent or will
    # not import is a BROKEN INSTALLATION -- the same class as an absent verifier or
    # an unreadable corpus, and reported the same way: ``unverifiable``, exit 20,
    # with the payload a caller parses on every other path. Never a traceback and
    # exit 1, which is no verdict in this script's contract at all.
    try:
        ledger = load_ledger()
    except Exception as exc:  # noqa: BLE001 - any failure to load is one verdict
        problem = (
            f"ledger.py could not be loaded from beside this script"
            f" ({exc.__class__.__name__}: {exc}). It ships in the same bundle, so this"
            " is a broken installation; the proof was not re-run, the corpus was not"
            " read, and nothing about this fix is settled"
        )
        return emit(
            {
                "finding_id": args.finding_id,
                "verdict": UNVERIFIABLE,
                "platform": host_platform(),
                "poc": {"verdict": UNVERIFIABLE, "reason": problem, "exit": 0},
                "golden_paths_checked": 0,
                "corpus": str(corpus_path()),
                "corpus_rows": 0,
                "broken": [],
                "unverifiable": [],
                "corpus_problems": [problem],
                "needs_human": [],
            }
        )
    # ONE resolved path, shared by this process and the verifier child. Resolved
    # here rather than defaulted twice, because the child's ``HOME`` is the worktree
    # and the default is ``HOME``-relative.
    db = (Path(args.db) if args.db else ledger.default_db_path()).resolve()

    poc_verdict, poc_reason, poc_exit = run_verifier(
        db=db, finding_id=args.finding_id, worktree=worktree, timeout=args.timeout
    )
    if poc_verdict == "invalid":
        print(poc_reason, file=sys.stderr)
        return EXIT_INVALID

    # The committed file beside this script and this host's rows: see the module
    # docstring for why neither is an argument.
    platform = host_platform()
    corpus = corpus_path()
    corpus_rows, corpus_problem = load_corpus(ledger, corpus)
    rows = rows_for_host(corpus_rows, platform)

    # The golden paths are checked even when the proof still reproduces. The verdict
    # does not change -- ``reproduces`` outranks everything -- but a fix that failed
    # AND broke three legitimate operations is one round of feedback instead of two,
    # and the second round would only be reached after the first was fixed.
    broken, unsettled, needs_human, checked = check_golden_paths(rows, worktree, args.timeout)

    # A corpus that could not be read is not a corpus that passed. Zero rows checked
    # would fold to ``holds`` by construction, which is exactly the vacuous green a
    # broken installation would report on every fix.
    corpus_note = [corpus_problem] if corpus_problem else []
    verdict = fold_verdict(
        poc_verdict,
        *([BROKEN] if broken else []),
        *([UNVERIFIABLE] if unsettled or corpus_note else []),
    )
    payload = {
        "finding_id": args.finding_id,
        "verdict": verdict,
        "platform": platform,
        "poc": {"verdict": poc_verdict, "reason": poc_reason, "exit": poc_exit},
        "golden_paths_checked": checked,
        "corpus": str(corpus),
        "corpus_rows": len(corpus_rows),
        "broken": broken,
        "unverifiable": unsettled,
        "corpus_problems": corpus_note,
        "needs_human": needs_human,
    }
    return emit(payload)


def emit(payload: dict[str, Any]) -> int:
    """Print the one JSON object on stdout, the readable lines on stderr; exit code.

    The ONE printer: every path out of :func:`main` that reached a verdict goes
    through here, so a caller parses one payload shape whether the run finished or
    a broken installation stopped it before the proof was re-run.
    """
    print(json.dumps(payload, sort_keys=True))
    for note in payload["corpus_problems"]:
        print(f"unverifiable: {note}", file=sys.stderr)
    for row in payload["broken"]:
        print(
            f"broken golden path entry {row['entry']} ({row['kind']}): {row['why']}",
            file=sys.stderr,
        )
    for row in payload["unverifiable"]:
        print(
            f"unverifiable golden path entry {row['entry']} ({row['kind']}): {row['why']}",
            file=sys.stderr,
        )
    for row in payload["needs_human"]:
        print(
            f"needs a human, entry {row['entry']} ({row['kind']}): {row['command_or_flow']}",
            file=sys.stderr,
        )
    return EXIT_CODES[payload["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
