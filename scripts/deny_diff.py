#!/usr/bin/env python3
"""deny_diff — does a tightened deny rule now refuse work the product depends on?

Stdlib only, no third-party deps, cross-platform. Run from the repo root::

    python3 scripts/deny_diff.py --base origin/main --head HEAD \\
        --corpus src/kiro_crew/builtin_skills/security-conductor/golden-paths.json

Exit 0 = no regressions, exit 1 = regressions, exit 2 = corpus/ref error.

Why this gate exists
--------------------
A security fix is almost always a deny rule made stricter, and "stricter" has no
upper bound that review can see. The rule's author reads the pattern and the
attack it now catches; nobody reads the set of ordinary commands that pattern
also newly matches. So the failure mode is not a missed vulnerability -- it is a
gate that starts refusing a read-only ``gh`` query, a feature-branch push, a
chat start or an installed cron, and the refusal surfaces days later as an agent
that "just stopped working" with a message that names a pattern rather than a
cause.

A model review cannot answer that question reliably: it would have to simulate a
17,000-line matcher over a corpus of commands nobody wrote down. A before/after
classification can, deterministically, because both sides of the comparison are
the REAL classifier -- the code as it exists at each ref -- not a description of
it.

What counts as "refused"
------------------------
The whole composite the tool gate applies to a shell command, in its order, not
just the rule catalog -- see :data:`_TIERS`. Each of those checks returns a denial
at ``hooks.on_tool_call``, so a gate that measured only the last would go green on
a tightening of the first three and the green badge would then stand as evidence
the question was asked. The reported tier says which one decided, because "the
path fence refused it" and "a catalog rule matched it" need different fixes.
``enabled_ids``/``denied_regexes`` are left at their defaults, which fails closed
to every built-in rule enabled -- the strictest posture an operator can be
running, and the only one that needs no config.

How the differential stays honest
---------------------------------
Four properties matter, and each one is a deliberate cost.

*Both sides are real code.* Each ref is materialized with ``git archive`` into
its own directory and classified by a child process whose ``PYTHONPATH`` points
at that checkout, so the verdict comes from that ref's own matcher. A
re-implementation of the rules in this script would agree with itself forever
and catch nothing.

*Each side proves which tree it loaded.* The child re-reports the file
``kiro_crew.security`` actually resolved to and refuses when it sits outside the
checkout it was pointed at. Without that check an installed copy of the package
shadowing the path could serve BOTH sides from one tree, and the gate would
report zero regressions forever while looking like it ran.

*The parent never imports the product.* Two reasons, one of them empirical: the
classifier is the thing under test, so importing it here would pin the harness
to one side of the comparison; and an inline ``python -c "import kiro_crew..."``
is itself refused by the argv floor this gate exists to keep honest. The child
is this same file re-executed with :data:`_WORKER_FLAG`, which imports the
product only in that mode.

*The child is hermetic.* Every ``KIROCREW_*`` variable is stripped from the
child's environment and ``KIROCREW_HOME`` is repointed at a throwaway
directory, so the verdict depends on the checkout alone and the classifier's
best-effort SEL audit writes land somewhere disposable instead of in the
operator's real log. A differential whose result moved with the caller's
environment would be unfalsifiable.

The corpus
----------
Rows come from the security-conductor's golden-paths seed: operations that are
legitimate BY DECISION, each with the reason it is legitimate. ``kind`` is
``shell``, ``flow`` or ``cron``; only ``shell`` rows are classified here,
because a flow and a cron are named by this gate's siblings and have no single
command line to hand a matcher. They are counted and reported as skipped rather
than dropped, so a corpus that is mostly unclassifiable says so instead of
reporting a confident zero.

The corpus is read from the BASE ref by the workflow, never the head checkout.
The corpus IS the contract a change is judged against, so a head-owned corpus
would let the same PR that tightens a rule delete the row that rule breaks --
removing it from both sides and passing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

#: Re-executes this file as the classification child. A flag rather than a
#: separate script so the harness is one reviewable file, and prefixed with an
#: underscore because it is an implementation detail of this script's own
#: subprocess call, not part of the CLI a caller composes.
_WORKER_FLAG = "--_classify-worker"

#: Row kinds the seed may carry. Only ``shell`` is classifiable -- see the module
#: docstring.
_KINDS = frozenset({"shell", "flow", "cron"})

#: Platform selectors a row may declare.
_PLATFORMS = frozenset({"any", "posix", "windows"})

#: The deny checks this gate measures, in the order ``hooks.on_tool_call`` applies
#: them to a shell command, as (tier name, attribute of ``kiro_crew.security``).
#:
#: Declared as data rather than inline in the worker so ``test/test_deny_diff.py``
#: can compare it against the hooks gate itself. The gate's whole claim is that a
#: green means "no golden path is newly refused AT THE TOOL GATE", and that holds
#: only while these two sets agree -- nothing about adding a fifth check over there
#: would otherwise reach this file, so the differential would keep passing while
#: quietly covering less of the product than it says.
_TIERS: tuple[tuple[str, str], ...] = (
    ("sensitive-path", "is_sensitive_path"),
    ("sensitive-bash", "is_sensitive_bash_command"),
    ("exfil", "audit_bash_exfiltration"),
    ("deny-rules", "is_denied"),
)

#: Reason reported for a tier whose check answers True/False rather than a string.
#: The path fence is the one such check, and a bare ``True`` would otherwise render
#: as an empty refusal in the report.
_BOOL_TIER_REASON = "Blocked: access to sensitive path"

#: Seconds a single classification child may take for the WHOLE corpus. One
#: child classifies every row, so this bounds the gate at two spawns, not two
#: per row.
_WORKER_TIMEOUT = 600

#: How much of a refusal reason is reported per row. The reason names the pattern
#: that matched, never the payload, but it can carry a diagnostic line and an
#: operator note, and a job summary with 40 of those is unreadable.
_REASON_CHARS = 300


class DenyDiffError(Exception):
    """A corpus or ref problem: the differential cannot be computed (exit 2).

    Distinct from "regressions found", which is a RESULT (exit 1). Conflating the
    two is how a gate reports green because it never ran.
    """


@dataclass(frozen=True)
class Row:
    """One golden path: an operation that is legitimate by decision."""

    index: int
    kind: str
    command: str
    platform: str
    reason: str

    def applies_to(self, platform: str) -> bool:
        return self.platform in ("any", platform)


@dataclass(frozen=True)
class Verdict:
    """What one ref's deny composite said about one command.

    ``tier`` names which of :data:`_TIERS` decided, so a reader knows whether to
    look at the path fence or at the rule catalog. Empty when nothing refused.
    """

    denied: bool
    reason: str
    tier: str = ""


@dataclass
class Report:
    """The differential, in the form both the text and JSON renderings read."""

    base: str
    head: str
    base_sha: str
    head_sha: str
    platform: str
    #: Where the rows came from. Named ``source`` and not ``corpus`` because on an
    #: attribute access the preceding identifier and that name join into a dotted
    #: fragment the repo's internal-content scan reads as a hostname. The scan is
    #: right to be blunt about the shape; renaming the field is the cheap fix.
    source: str
    total_rows: int
    skipped_kind: int = 0
    skipped_platform: int = 0
    #: Tiers declared in :data:`_TIERS` that the BASE tree does not carry, i.e. deny
    #: checks this change introduces. Reported because they are the reason a whole
    #: tier's worth of rows can turn up as regressions at once.
    base_absent_tiers: list[str] = field(default_factory=list)
    regressions: list[tuple[Row, Verdict]] = field(default_factory=list)
    loosenings: list[tuple[Row, Verdict]] = field(default_factory=list)
    unchanged_denied: int = 0
    unchanged_allowed: int = 0

    @property
    def classified(self) -> int:
        return (
            len(self.regressions)
            + len(self.loosenings)
            + self.unchanged_denied
            + self.unchanged_allowed
        )

    @property
    def exit_code(self) -> int:
        return 1 if self.regressions else 0


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def load_corpus(path: Path) -> list[Row]:
    """Parse the golden-paths seed at *path* into rows.

    Accepts a bare list or an object wrapping one under ``golden_paths``, which is
    the seed's own shape.

    Every rejection is a :class:`DenyDiffError`. A corpus that silently dropped a
    malformed row would report "no regressions" over a subset nobody chose --
    exactly the false green this gate exists to prevent.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DenyDiffError(f"cannot read corpus {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DenyDiffError(f"corpus {path} is not valid JSON: {exc}") from exc

    if isinstance(payload, dict):
        if "golden_paths" not in payload:
            raise DenyDiffError(f"corpus {path} is an object without a 'golden_paths' key")
        payload = payload["golden_paths"]
    if not isinstance(payload, list):
        raise DenyDiffError(f"corpus {path} must hold a list of rows, got {type(payload).__name__}")
    if not payload:
        raise DenyDiffError(f"corpus {path} holds no rows")

    return [_row(path, position, entry) for position, entry in enumerate(payload)]


def _row(path: Path, position: int, entry: object) -> Row:
    """One validated row, or a :class:`DenyDiffError` naming its position."""
    where = f"corpus {path} row {position}"
    if not isinstance(entry, dict):
        raise DenyDiffError(f"{where} is not an object")

    kind = entry.get("kind")
    if kind not in _KINDS:
        raise DenyDiffError(f"{where} has kind {kind!r}, expected one of {sorted(_KINDS)}")

    command = entry.get("command_or_flow")
    if not isinstance(command, str) or not command.strip():
        raise DenyDiffError(f"{where} has no non-empty 'command_or_flow'")

    platform = entry.get("platform", "any")
    if platform not in _PLATFORMS:
        raise DenyDiffError(
            f"{where} has platform {platform!r}, expected one of {sorted(_PLATFORMS)}"
        )

    reason = entry.get("reason", "")
    if not isinstance(reason, str):
        raise DenyDiffError(f"{where} has a non-string 'reason'")

    return Row(index=position, kind=kind, command=command, platform=platform, reason=reason)


# ---------------------------------------------------------------------------
# Materializing a ref
# ---------------------------------------------------------------------------


def resolve_checkout(repo_root: Path, ref: str, dest: Path) -> Path:
    """Materialize *ref*'s ``src`` tree under *dest*; return *dest*.

    ``git archive`` piped through stdlib :mod:`tarfile` rather than a ``tar``
    subprocess or ``git worktree``: ``tar`` is not a portable dependency on the
    Windows runner, and a worktree mutates the caller's repository -- an
    administrative write this gate has no business making to answer a read-only
    question.

    This function is the seam the tests replace. Pointing it at a hand-built tree
    is what lets the exit-code paths be proven against a few-line fake composite
    instead of against the real one, whose verdicts are the thing under test and
    cannot also be the fixture.
    """
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest.with_suffix(".tar")
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "archive",
            "--format=tar",
            "-o",
            str(archive),
            ref,
            "src",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise DenyDiffError(f"cannot archive ref {ref!r}: {detail}")
    try:
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise DenyDiffError(f"cannot unpack archive of ref {ref!r}: {exc}") from exc
    finally:
        archive.unlink(missing_ok=True)
    if not (dest / "src" / "kiro_crew").is_dir():
        raise DenyDiffError(f"ref {ref!r} has no src/kiro_crew tree")
    return dest


def _rev_parse(repo_root: Path, ref: str) -> str:
    """*ref*'s commit sha, or the ref itself when it does not resolve.

    Reported for provenance only, so an unresolvable ref falls back to the spelling
    the caller gave rather than failing: the archive step above is the one that
    decides whether a ref is usable, and it produces the better message.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--short", ref],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout.strip() if proc.returncode == 0 else ref


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _child_env(checkout: Path, home: Path) -> dict[str, str]:
    """Environment for a classification child: hermetic, and pointed at *checkout*.

    Every ``KIROCREW_*`` variable is dropped. The caller's sandbox and port
    variables leak into any child by default, and a classifier that read them would
    make the verdict a function of who ran the gate. ``KIROCREW_HOME`` is then set
    to *home* so the classifier's best-effort audit writes land in a throwaway
    directory rather than the operator's real security log.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("KIROCREW_")}
    env["KIROCREW_HOME"] = str(home)
    env["PYTHONPATH"] = str(checkout / "src")
    # A .pyc for a tree deleted at the end of the run is pure cost, and writing
    # into the archive dir muddies "the checkout is exactly the ref".
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def classify(
    checkout: Path, commands: list[str], *, home: Path, side: str = "head"
) -> tuple[list[Verdict], list[str]]:
    """Classify *commands* with the deny composite living under *checkout*.

    Returns the verdicts and the names of any declared tiers that tree does not
    carry. *side* decides what a missing tier MEANS, which is the whole reason it is
    a parameter -- see :func:`_worker_main`.

    One child for the whole list: the composite's import cost dwarfs its per-command
    cost, so per-row spawning would make the gate slower than the test suite it
    guards without changing a single verdict.
    """
    if not commands:
        return [], []
    request = {
        "commands": commands,
        "expect_root": str((checkout / "src").resolve()),
        "side": side,
    }
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), _WORKER_FLAG],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_child_env(checkout, home),
        timeout=_WORKER_TIMEOUT,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise DenyDiffError(f"classifier under {checkout} failed: {detail}")
    try:
        payload = json.loads(proc.stdout)
        verdicts = payload["verdicts"]
        absent = [str(name) for name in payload.get("absent_tiers", [])]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DenyDiffError(
            f"classifier under {checkout} produced unreadable output: {exc}"
        ) from exc
    if len(verdicts) != len(commands):
        raise DenyDiffError(
            f"classifier under {checkout} returned {len(verdicts)} verdicts "
            f"for {len(commands)} commands"
        )
    out: list[Verdict] = []
    for entry in verdicts:
        if entry.get("error"):
            raise DenyDiffError(f"classifier under {checkout} errored: {entry['error']}")
        out.append(
            Verdict(
                denied=bool(entry["denied"]),
                reason=str(entry.get("reason") or ""),
                tier=str(entry.get("tier") or ""),
            )
        )
    return out, absent


def _worker_main() -> int:
    """The child: run the product's deny composite over each command.

    The ONLY place this file touches product code, reached only via
    :data:`_WORKER_FLAG`. It reports a classifier failure as a structured ``error``
    row rather than a traceback, so the parent can say which ref broke instead of
    surfacing a stack from a tree the reader cannot see.
    """
    try:
        payload = json.loads(sys.stdin.read())
        commands = list(payload["commands"])
        expect_root = Path(payload["expect_root"]).resolve()
        side = str(payload.get("side") or "head")
    except (KeyError, TypeError, ValueError) as exc:
        print(f"worker: unreadable request: {exc}", file=sys.stderr)
        return 2
    try:
        import kiro_crew.security as security
    except Exception as exc:
        print(f"worker: cannot import kiro_crew.security: {exc}", file=sys.stderr)
        return 2

    # Which tree actually answered. An installed copy of the package that shadowed
    # the path would serve both refs from one tree and every differential would
    # come back empty -- a false green with no symptom, so it is checked rather
    # than assumed. ``normcase`` first: Windows paths are case-insensitive and
    # accept either separator, so a raw comparison can report "outside" for the
    # very tree the child was pointed at.
    loaded = Path(os.path.normcase(str(Path(security.__file__ or "").resolve())))
    root = Path(os.path.normcase(str(expect_root)))
    if not loaded.is_relative_to(root):
        print(
            f"worker: kiro_crew.security resolved to {loaded}, outside {root}",
            file=sys.stderr,
        )
        return 2

    # :data:`_TIERS` in ITS order, so the first tier to refuse here is the tier that
    # would refuse in production.
    #
    # A tier the tree does not carry means opposite things on the two sides, and the
    # difference is the gate's own primary use case. The worker is always THIS file
    # at head, so it iterates head's tier table against whichever tree it was
    # pointed at; a PR that adds a deny check adds it to both the table and
    # ``security``, and the BASE tree then has no such attribute. Refusing there
    # would exit 2 on exactly the tightening this gate exists to measure. The honest
    # reading is that a check which did not exist at base refused nothing at base,
    # so the tier is skipped and its refusals at head surface as regressions -- which
    # is the answer the reviewer wanted.
    #
    # At HEAD the same absence is coverage silently lost, so it stays an error.
    strict = side != "base"
    checks: list[tuple[str, Callable[[str], object]]] = []
    absent: list[str] = []
    for name, attribute in _TIERS:
        check = getattr(security, attribute, None)
        if check is None:
            if strict:
                print(f"worker: {attribute} is absent from this tree", file=sys.stderr)
                return 2
            absent.append(name)
            continue
        checks.append((name, check))

    verdicts: list[dict[str, object]] = []
    for command in commands:
        row: dict[str, object] = {"denied": False, "reason": "", "tier": ""}
        for name, check in checks:
            try:
                outcome = check(command)
            except Exception as exc:
                row = {"error": f"{type(exc).__name__} in {name} on {command!r}: {exc}"}
                break
            if not outcome:
                continue
            # The path fence answers True/False; the other three answer a reason or
            # None. Both shapes mean "refused", and a bare True must not render as
            # an empty refusal.
            reason = _BOOL_TIER_REASON if outcome is True else str(outcome)
            row = {"denied": True, "reason": reason[:_REASON_CHARS], "tier": name}
            break
        verdicts.append(row)
    json.dump({"verdicts": verdicts, "absent_tiers": absent}, sys.stdout)
    return 0


# ---------------------------------------------------------------------------
# The differential
# ---------------------------------------------------------------------------


def resolve_platform(selector: str) -> str:
    """The platform rows are filtered against. ``auto`` reads the running host."""
    if selector != "auto":
        return selector
    return "windows" if os.name == "nt" else "posix"


def differential(
    repo_root: Path,
    base: str,
    head: str,
    corpus_path: Path,
    *,
    platform: str,
    workdir: Path,
    resolver: Callable[[Path, str, Path], Path] | None = None,
) -> Report:
    """Classify the corpus at *base* and at *head* and diff the two verdict sets.

    *resolver* defaults to the module-level :func:`resolve_checkout` read at CALL
    time, so a test may either pass a substitute here or monkeypatch the module
    attribute; both reach the same seam.
    """
    resolve = resolver or resolve_checkout
    rows = load_corpus(corpus_path)
    report = Report(
        base=base,
        head=head,
        base_sha=_rev_parse(repo_root, base),
        head_sha=_rev_parse(repo_root, head),
        platform=platform,
        source=str(corpus_path),
        total_rows=len(rows),
    )

    selected: list[Row] = []
    for row in rows:
        if row.kind != "shell":
            report.skipped_kind += 1
        elif not row.applies_to(platform):
            report.skipped_platform += 1
        else:
            selected.append(row)

    if not selected:
        return report

    commands = [row.command for row in selected]
    base_home = workdir / "home-base"
    head_home = workdir / "home-head"
    base_home.mkdir(parents=True, exist_ok=True)
    head_home.mkdir(parents=True, exist_ok=True)
    base_tree = resolve(repo_root, base, workdir / "base")
    head_tree = resolve(repo_root, head, workdir / "head")
    base_verdicts, report.base_absent_tiers = classify(
        base_tree, commands, home=base_home, side="base"
    )
    head_verdicts, _ = classify(head_tree, commands, home=head_home, side="head")

    for row, before, after in zip(selected, base_verdicts, head_verdicts):
        if after.denied and not before.denied:
            report.regressions.append((row, after))
        elif before.denied and not after.denied:
            report.loosenings.append((row, before))
        elif after.denied:
            report.unchanged_denied += 1
        else:
            report.unchanged_allowed += 1
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_text(report: Report) -> str:
    """The human/job-summary rendering. Markdown, because a job summary renders it."""
    lines = [
        "### Denial differential",
        "",
        f"- base `{report.base}` (`{report.base_sha}`) -> "
        f"head `{report.head}` (`{report.head_sha}`)",
        f"- platform `{report.platform}`, corpus `{report.source}`",
        f"- {report.total_rows} row(s): {report.classified} classified, "
        f"{report.skipped_kind} non-shell, {report.skipped_platform} other-platform",
        "",
    ]
    if report.base_absent_tiers:
        lines.append(
            "- this change ADDS the "
            + ", ".join(f"`{name}`" for name in report.base_absent_tiers)
            + " tier(s); the base ref has no such check, so it refused nothing there"
        )
        lines.append("")

    if report.regressions:
        lines.append(f"**Regressions -- newly refused at head ({len(report.regressions)})**")
        lines.append("")
        for row, verdict in report.regressions:
            lines.append(f"- `{row.command}` _[{row.platform}]_")
            if row.reason:
                lines.append(f"    - legitimate because: {row.reason}")
            lines.append(f"    - head refuses at the `{verdict.tier}` tier: {verdict.reason}")
        lines.append("")
    else:
        lines.append("**No regressions** -- every classified golden path still runs at head.")
        lines.append("")

    if report.loosenings:
        lines.append(
            f"Loosenings -- newly allowed at head ({len(report.loosenings)}), informational:"
        )
        lines.append("")
        for row, verdict in report.loosenings:
            lines.append(f"- `{row.command}` _[{row.platform}]_")
            lines.append(f"    - base refused at the `{verdict.tier}` tier: {verdict.reason}")
        lines.append("")

    lines.append(
        f"Unchanged: {report.unchanged_allowed} allowed at both refs, "
        f"{report.unchanged_denied} refused at both."
    )
    return "\n".join(lines)


def render_json(report: Report) -> str:
    """The machine rendering. The workflow reads this to extract the exact rows."""
    return json.dumps(
        {
            "base": report.base,
            "base_sha": report.base_sha,
            "head": report.head,
            "head_sha": report.head_sha,
            "platform": report.platform,
            "corpus": report.source,
            "counts": {
                "total_rows": report.total_rows,
                "classified": report.classified,
                "skipped_kind": report.skipped_kind,
                "skipped_platform": report.skipped_platform,
                "base_absent_tiers": report.base_absent_tiers,
                "regressions": len(report.regressions),
                "loosenings": len(report.loosenings),
                "unchanged_allowed": report.unchanged_allowed,
                "unchanged_denied": report.unchanged_denied,
            },
            "regressions": [
                {
                    "command": row.command,
                    "platform": row.platform,
                    "kind": row.kind,
                    "why_legitimate": row.reason,
                    "head_tier": verdict.tier,
                    "head_refusal": verdict.reason,
                }
                for row, verdict in report.regressions
            ],
            "loosenings": [
                {
                    "command": row.command,
                    "platform": row.platform,
                    "kind": row.kind,
                    "why_legitimate": row.reason,
                    "base_tier": verdict.tier,
                    "base_refusal": verdict.reason,
                }
                for row, verdict in report.loosenings
            ],
            "exit_code": report.exit_code,
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """The repo root, from this file's location (``scripts/<me>``)."""
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == _WORKER_FLAG:
        return _worker_main()

    parser = argparse.ArgumentParser(
        prog="deny_diff",
        description="Report golden paths a deny-rule change newly refuses.",
    )
    parser.add_argument("--base", required=True, help="ref to classify as 'before'")
    parser.add_argument("--head", required=True, help="ref to classify as 'after'")
    parser.add_argument("--corpus", required=True, help="path to the golden-paths JSON")
    parser.add_argument(
        "--platform",
        default="auto",
        choices=sorted(_PLATFORMS | {"auto"}),
        help="which rows to classify; 'auto' reads the running host",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parsed = parser.parse_args(args)

    repo_root = _repo_root()
    try:
        with tempfile.TemporaryDirectory(prefix="deny-diff-") as tmp:
            report = differential(
                repo_root,
                parsed.base,
                parsed.head,
                Path(parsed.corpus),
                platform=resolve_platform(parsed.platform),
                workdir=Path(tmp),
            )
    except DenyDiffError as exc:
        print(f"deny_diff: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as exc:
        print(f"deny_diff: classifier timed out after {exc.timeout}s", file=sys.stderr)
        return 2

    print(render_json(report) if parsed.json else render_text(report))
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
