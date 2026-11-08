#!/usr/bin/env python3
"""net_strict_changed.py -- run the test files THIS change touches with the network floor strict.

## The rule this enforces

Testing-conventions D10: a test reaches no host but loopback. The rootdir network
floor (``test/conftest_floor_determinism.py``) already sees an off-loopback
connect, send or name lookup, but in CI it only REPORTS it, in a warnings summary
at the end of a 20-40 minute shard that nobody reads. ``KIROCREW_NET_STRICT=1``
turns the report into a refusal: the call raises ``OffLoopbackNetworkRefused`` at
the call site, and the test fails at teardown with ``this test reached past
loopback``. A test marked ``real_network`` is exempt, and a UDP route probe
(a datagram ``connect``, which sends nothing) is let through.

Strict across the whole suite is not possible yet: some test files already look
up a non-local name. This script therefore turns it on only for the test files the
change adds or changes, so there is no baseline to clean and a file is held to the
rule from the day someone touches it.

## Scope

The files come from ``scripts/ratchet_scope.py`` (``change_merge_base`` and
``change_files``), the same base and the same changed-file set the flake-pattern
ratchet reads. Only the file's NEW path counts: a deleted file has nothing to run,
a rename is its destination. A file name is untrusted (it comes from the pull
request), so it is checked against ``is_runnable_name`` and handed to pytest as an
argument-list entry after ``--``; no shell ever sees it.

## Output and exit code

Prints the report and, when ``$NET_STRICT_REPORT`` names a file, writes the same
text there. This script never touches ``$GITHUB_STEP_SUMMARY``: on CodeBuild the step
runs as an unprivileged user that cannot write it, so the workflow publishes the
report file from a later step under the runner identity.
Exit 0: nothing to run, no base to compare with, or no finding. Exit 1: at least one
finding, or pytest did not finish or left no report. The workflow step carries
``continue-on-error`` for the warn-first release, so exit 1 is visible without
failing the job. Failures that are not a network refusal are counted, not judged:
another gate owns them.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The environment switch the rootdir conftest reads (``_NET_STRICT_ENV`` there).
STRICT_ENV = "KIROCREW_NET_STRICT"

#: The file the rendered report is written to, when set.
REPORT_ENV = "NET_STRICT_REPORT"

_HEADER = "### Network reach in the test files this change touches (warn only)"

#: Seconds pytest may run before the script gives up and reports it.
DEFAULT_TIMEOUT = 240

#: The two spellings strict mode fails a test with. The first is the teardown
#: verdict, the second is the exception raised at the call (a test that swallows
#: ``OSError`` hides it, but the teardown verdict still fires).
_TEARDOWN_MARK = f"reached past loopback under {STRICT_ENV}=1: "
_CALL_MARK = "off-loopback network refused in a test: "
_TEARDOWN_END = ". Stub the network"

#: Roots pytest collects from (``testpaths`` in setup.cfg).
_TEST_ROOTS = ("test/", "src/kiro_crew/apps/builtins/")


def _load_scope():
    path = ROOT / "scripts" / "ratchet_scope.py"
    spec = importlib.util.spec_from_file_location("ratchet_scope", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_runnable_name(rel: str) -> bool:
    """True for a repo-relative path pytest collects as a test module and is safe to pass.

    A name carrying ``::`` would be read as a node id, a control character could
    forge a log line, and a name that does not start under a test root could start
    with ``-``; none of those is a test file, so none is run.
    """
    if not rel.endswith(".py") or not rel.startswith(_TEST_ROOTS):
        return False
    name = rel.rsplit("/", 1)[-1]
    if not (name.startswith("test_") or name.endswith("_test.py")):
        return False
    return "::" not in rel and all(ord(ch) >= 0x20 and ch != "\x7f" for ch in rel)


def select_test_files(pairs: Sequence[tuple[str | None, str | None]], root: Path) -> list[str]:
    """The sorted, de-duplicated test files that exist now among ``(old, new)`` path pairs.

    ``new`` is None for a deletion, which is skipped. A file that is not on disk
    (a path git lists but the checkout lacks) is skipped too: pytest would report
    "file not found" and that is not a network finding.
    """
    chosen = {
        new
        for _old, new in pairs
        if new is not None and is_runnable_name(new) and (root / new).is_file()
    }
    return sorted(chosen)


def changed_test_files(root: Path = ROOT) -> tuple[list[str] | None, str]:
    """``(files, how the base was found)``; files is None when no base is readable."""
    scope = _load_scope()
    base, how = scope.change_merge_base(cwd=root)
    if base is None:
        return None, how
    return select_test_files(scope.change_files(base, cwd=root), root), how


def _clean(text: str) -> str:
    """One printable line: no newline, no backtick, no leading workflow-command marker."""
    flat = " ".join(text.split()).replace("`", "'")
    return flat.replace("::", ": :") if flat.startswith("::") else flat


def _detail(message: str) -> str | None:
    for mark in (_TEARDOWN_MARK, _CALL_MARK):
        if mark in message:
            tail = (message.split(mark, 1)[1].splitlines() or [""])[0]
            return tail.split(_TEARDOWN_END, 1)[0].strip()
    return None


def parse_report(junit_path: Path) -> tuple[list[tuple[str, str]], int] | None:
    """``(network findings, other failures)`` from a pytest junit file, or None.

    A finding is ``(test id, what it reached)``; a test that fails both at the call
    and at teardown is one finding per distinct detail. None means there is no
    readable report (pytest was killed, crashed, or the file is not valid XML), which
    is NOT the same as "no finding": the caller must not call that run clean.

    Parsed with defusedxml, never the stdlib parser: the changed test files run in
    the child, so the child could write any bytes to this file.
    """
    try:
        from defusedxml import DefusedXmlException
        from defusedxml.ElementTree import ParseError, parse
    except ImportError:
        return None  # never fall back to the stdlib parser

    try:
        tree = parse(str(junit_path))
    except (OSError, ParseError, DefusedXmlException):
        return None
    found: set[tuple[str, str]] = set()
    others = 0
    for case in tree.iter("testcase"):
        outcomes = [child for child in case if child.tag in ("failure", "error")]
        if not outcomes:
            continue
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        network = False
        for outcome in outcomes:
            detail = _detail(outcome.get("message") or "") or _detail(outcome.text or "")
            if detail is not None:
                network = True
                found.add((test_id, detail))
        if not network:
            others += 1
    return sorted(found), others


def run_strict(
    files: Sequence[str],
    *,
    root: Path,
    junit: Path,
    extra_args: Sequence[str] = (),
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool, int]:
    """Run pytest on ``files`` with the floor strict. ``(finished, exit code)``.

    The command is an argument list with the files after ``--``: nothing is
    interpolated into a shell. The child inherits this process's environment minus
    the xdist worker markers an outer pytest would leave behind.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST")}
    env.pop("PYTEST_CURRENT_TEST", None)
    env[STRICT_ENV] = "1"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-n0",
        "--no-cov",
        "-q",
        "--tb=no",
        f"--junitxml={junit}",
        *extra_args,
        "--",
        *files,
    ]
    try:
        proc = subprocess.run(command, cwd=root, env=env, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, -1
    return True, proc.returncode


def render(
    files: Sequence[str],
    report: tuple[Sequence[tuple[str, str]], int] | None,
    *,
    finished: bool,
    code: int,
    how: str,
) -> list[str]:
    """The report lines, shared by the log and the job summary."""
    lines = [
        _HEADER,
        "",
        f"Ran {len(files)} changed test file(s) with `{STRICT_ENV}=1`; base: {_clean(how)}.",
    ]
    if report is None:
        reason = (
            f"pytest did not finish within {DEFAULT_TIMEOUT} s"
            if not finished
            else f"pytest exited with code {code}"
        )
        return [*lines, "", f"{reason} and left no readable report. Nothing was measured."]
    findings, others = report
    if findings:
        lines += [
            "",
            "These tests reach a host other than loopback. Stub the network at the seam the",
            "product calls, or mark the test `@pytest.mark.real_network` (testing-conventions D10).",
            "",
            "```",
            *(f"{_clean(test_id)}: {_clean(detail)}" for test_id, detail in findings),
            "```",
        ]
    else:
        lines += ["", "No test reached past loopback."]
    if others:
        lines.append(f"{others} test(s) failed for another reason; this step does not judge them.")
    return lines


def _emit(lines: Sequence[str]) -> None:
    """Print the report, and write it to ``$NET_STRICT_REPORT`` when that names a file."""
    text = "\n".join(lines) + "\n"
    print(text, end="")
    path = os.environ.get(REPORT_ENV)
    if not path:
        return
    try:
        Path(path).write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"net-strict: could not write the report file: {exc}")


def main() -> int:
    files, how = changed_test_files(ROOT)
    if files is None:
        _emit([_HEADER, "", f"No base to compare with ({_clean(how)}); nothing checked."])
        return 0
    if not files:
        _emit([_HEADER, "", f"This change adds or changes no test file ({_clean(how)}); skipped."])
        return 0
    with tempfile.TemporaryDirectory() as scratch:
        junit = Path(scratch) / "junit.xml"
        finished, code = run_strict(files, root=ROOT, junit=junit)
        report = parse_report(junit)
    _emit(render(files, report, finished=finished, code=code, how=how))
    return 0 if finished and report is not None and not report[0] else 1


if __name__ == "__main__":
    sys.exit(main())
