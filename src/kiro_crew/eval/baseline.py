"""Compare an eval run's per-scenario scores with a saved run, and write a JUnit file.

``kirocrew eval --baseline FILE [--max-drop FRACTION]`` reads an earlier run's JSON
summary (the ``eval_<timestamp>.json`` the command already writes) and names every
scenario whose score fell by more than the allowed amount. A scenario's score is its
share of passed assertions (``assertions`` is ``"passed/total"`` in the summary), or
1 or 0 from ``passed`` when it has no assertions.

Nothing here refuses a run. A baseline that cannot be used, or a bad ``--max-drop``,
is dropped with one warning and the command goes on as it would without them.

The gate only adds to the exit code, never takes from it: every failed scenario
still fails the run, as it does without a baseline, and a score that fell past the
allowance is named a regression and fails the run too. The baseline cannot excuse a
failure, because a score alone cannot tell a known failure from a new one (a turn
that errors records no assertion result, so its scenario can fail at an unchanged
score).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.terminal_safe import safe_terminal_line

# Float noise from a division must not turn an exact allowed drop into a regression.
_EPSILON = 1e-9
_MAX_DIGITS = 9
# The read gate's own wording when the sensitive-path list matched. Every other
# ``Blocked:`` refusal from it (a folder, a pipe, a file swapped after the check)
# is a file that could not be read, not a security refusal.
_SENSITIVE_PATH_REFUSALS = ("Blocked: access to sensitive path", "Blocked: opened sensitive path")
# The write guard's refusals, told apart by the words that follow the quoted name.
_WRITE_REFUSALS = (
    (": the destination", "the sensitive-path check refused it"),
    (" is a symbolic link", "the name is a link"),
    (" has ", "the name is a hard link"),
    (" on the way to it is a link or junction", "a folder on the way to it is a link"),
)


@dataclass(frozen=True)
class BaselineGate:
    """The saved scores to compare with and how far a score may fall."""

    scores: dict[str, float]
    max_drop: float


@dataclass(frozen=True)
class Comparison:
    """What differs between a run and its baseline.

    ``regressions`` holds ``(name, was, now)``; ``reasons`` maps each of them to the
    line its JUnit failure carries.
    """

    regressions: list[tuple[str, float, float]]
    reasons: dict[str, str]
    not_in_baseline: list[str]
    not_in_run: list[str]


def _warn(text: str) -> None:
    print(f"Warning: {text}", file=sys.stderr)


def scenario_score(entry: object) -> float | None:
    """The score of one scenario summary, or None when it has none to read."""
    if not isinstance(entry, dict):
        return None
    text = entry.get("assertions")
    if isinstance(text, str):
        done, sep, total = text.partition("/")
        digits = (done, total)
        if sep and all(d.isascii() and d.isdecimal() and len(d) <= _MAX_DIGITS for d in digits):
            if int(total) > 0:
                return min(int(done) / int(total), 1.0)
    passed = entry.get("passed")
    return (1.0 if passed else 0.0) if isinstance(passed, bool) else None


def scores_by_name(doc: object) -> dict[str, float]:
    """Every scenario's score in a run summary; ``ValueError`` when it holds none."""
    rows = doc.get("scenarios") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise ValueError("no scenario list")
    scores: dict[str, float] = {}
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        score = scenario_score(row)
        if isinstance(name, str) and score is not None:
            scores[name] = score
    if not scores:
        raise ValueError("no scenario scores")
    return scores


def _read_failure_class(exc: Exception) -> str:
    """Why a baseline read failed, as a class; never the exception text (it names targets)."""
    from kiro_crew.security import is_unverifiable_path_refusal

    if isinstance(exc, FileNotFoundError):
        return "the file was not found"
    if isinstance(exc, PermissionError):
        refusal = str(exc)
        if is_unverifiable_path_refusal(refusal):
            return "the sensitive-path check did not finish"
        if refusal.startswith(_SENSITIVE_PATH_REFUSALS):
            return "the sensitive-path check refused it"
    return "the file could not be read"


def load_baseline(path: str) -> tuple[dict[str, float] | None, str]:
    """Read a saved run through the sensitive-path gate: ``(scores, "")`` or ``(None, reason)``."""
    from kiro_crew.hooks import safe_read_file

    try:
        text = safe_read_file(path)
    except (OSError, UnicodeDecodeError) as exc:
        return None, _read_failure_class(exc)
    try:
        return scores_by_name(json.loads(text)), ""
    except (ValueError, RecursionError):
        return None, "it is not an eval result file"


def parse_max_drop(text: str) -> float | None:
    """The allowed drop as a fraction from 0 to 1, or None when *text* is not one."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if 0.0 <= value <= 1.0 else None


def gate_from_args(args: Any) -> BaselineGate | None:
    """The gate ``--baseline`` and ``--max-drop`` ask for, or None (after one warning if unusable)."""
    path = getattr(args, "baseline", None)
    raw = getattr(args, "max_drop", None)
    if not path:
        if raw is not None:
            _warn("--max-drop ignored: it needs --baseline.")
        return None
    name = safe_terminal_line(Path(path).name or "baseline")
    drop = 0.0 if raw is None else parse_max_drop(raw)
    if drop is None:
        _warn(f"--baseline {name} ignored: --max-drop must be a number from 0 to 1.")
        return None
    scores, why = load_baseline(path)
    if scores is None:
        _warn(f"--baseline {name} ignored: {why}.")
        return None
    return BaselineGate(scores, drop)


def compare(gate: BaselineGate, scenarios: list[dict[str, Any]]) -> Comparison:
    """Set the run's scenario summaries against the gate's saved scores."""
    now = {s["name"]: sc for s in scenarios if (sc := scenario_score(s)) is not None}
    regressions = [
        (name, gate.scores[name], score)
        for name, score in now.items()
        if name in gate.scores and gate.scores[name] - score > gate.max_drop + _EPSILON
    ]
    failures = {n: f"score fell from {was:.2f} to {cur:.2f}" for n, was, cur in regressions}
    return Comparison(
        regressions=regressions,
        reasons=failures,
        not_in_baseline=[n for n in now if n not in gate.scores],
        not_in_run=[n for n in gate.scores if n not in now],
    )


def print_comparison(result: Comparison, max_drop: float) -> None:
    """One line per regression, then the scenarios only one of the two runs has."""
    for name, was, now in result.regressions:
        print(
            safe_terminal_line(
                f"Regression: {name}: {was:.2f} -> {now:.2f} (allowed drop {max_drop:.2f})"
            )
        )
    for name in result.not_in_baseline:
        print(safe_terminal_line(f"Not compared: {name} (not in the baseline)"))
    for name in result.not_in_run:
        print(safe_terminal_line(f"Not compared: {name} (not in this run)"))


def _xml_char(c: str) -> bool:
    """Whether *c* may appear in an XML 1.0 document (the spec's ``Char`` production)."""
    o = ord(c)
    return c in "\t\n\r" or 0x20 <= o <= 0xD7FF or 0xE000 <= o <= 0xFFFD or 0x10000 <= o <= 0x10FFFF


def _xml(text: object) -> str:
    """*text* made safe for XML 1.0 text or an attribute: bad characters out, markup escaped."""
    kept = "".join(c for c in str(text) if _xml_char(c))
    for raw, escaped in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;")):
        kept = kept.replace(raw, escaped)
    return kept.replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;")


def junit_xml(scenarios: list[dict[str, Any]], reasons: dict[str, str] | None) -> str:
    """A JUnit report: one test case per scenario, a failure for each failed one.

    *reasons* gives the failure line for a scenario the baseline names a regression;
    any other failed scenario's line is its assertion count.
    """
    cases: list[str] = []
    failed = 0
    for s in scenarios:
        name = str(s.get("name", ""))
        why = (reasons or {}).get(name, "")
        body = ""
        if why or not s.get("passed"):
            failed += 1
            line = why or f"assertions {s.get('assertions')}"
            body = f'<failure message="{_xml(line)}"/>'
        cases.append(
            f'<testcase classname="kirocrew.eval" name="{_xml(name)}" '
            f'time="{float(s.get("elapsed_secs") or 0):.2f}">{body}</testcase>'
        )
    head = f'<testsuite name="kirocrew-eval" tests="{len(cases)}" failures="{failed}">'
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + head + "".join(cases) + "</testsuite>\n"


def _after_quoted_name(text: str) -> str:
    """*text* from just past its first quoted name, so a name made of guard words says nothing."""
    start = next((i for i, c in enumerate(text) if c in "'\""), -1)
    if start < 0:
        return ""
    i = start + 1
    while i < len(text) and text[i] != text[start]:
        i += 2 if text[i] == "\\" else 1
    return text[i + 1 :]


def _write_refusal_class(refusal: str) -> str:
    """Which write-guard refusal *refusal* is, as a class; never its text (it names targets).

    The guard raises one exception type for every case, so the class is read from
    the words after the name the sentence quotes. A refusal this does not know reads
    as the guard's, never as a sensitive-path match.
    """
    rest = _after_quoted_name(refusal)
    for lead, why in _WRITE_REFUSALS:
        if rest.startswith(lead):
            return why
    return "the write guard refused it"


def _is_other_kind_of_file(target: Path) -> bool:
    """True when *target* names a pipe, socket, device or folder; a link is the write guard's."""
    try:
        mode = os.lstat(target).st_mode
    except OSError:
        return False
    return not (stat.S_ISREG(mode) or stat.S_ISLNK(mode))


def write_junit(path: str, scenarios: list[dict[str, Any]], reasons: dict[str, str] | None) -> None:
    """Write the JUnit report at *path* through the eval write guard; on failure warn and go on.

    ``write_text_atomic_nofollow`` refuses a protected destination or a link at the
    name and replaces the file by rename, so an agent-run ``--junit`` cannot write
    over a credential store or the governance trust root, nor leave a half file.
    """
    from kiro_crew.eval.bench.errors import BenchRefusal
    from kiro_crew.eval.bench.safepath import guard_write_path, write_text_atomic_nofollow

    name = safe_terminal_line(Path(path).name or "junit")
    try:
        target = Path(path).expanduser()
        guard_write_path(target.parent, what="JUnit report")
        if _is_other_kind_of_file(target):
            _warn(f"--junit {name} was not written: the name is not a regular file.")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic_nofollow(target, junit_xml(scenarios, reasons), what="JUnit report")
    except BenchRefusal as refusal:
        _warn(f"--junit {name} was not written: {_write_refusal_class(str(refusal))}.")
    except (OSError, RuntimeError):
        # RuntimeError: ``~user`` names no home directory this host can find.
        _warn(f"--junit {name} was not written: the file could not be written.")
