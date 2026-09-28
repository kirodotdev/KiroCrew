#!/usr/bin/env python3
"""Report Python dependency vulnerabilities against a recorded baseline.

The repository had no Python vulnerability signal at all: the step that once
provided it ran an unpinned `pip install pip-audit`, so it was neither
reproducible nor able to tell a new advisory from a new tool version, and it was
removed. This restores the signal with the tool pinned in the dev dependency
group, so the audit runs from the same install the rest of CI already performs.

REPORTS, never blocks. The exit status is 0 for any number of advisories; it is
non-zero only when the audit cannot be trusted to have run at all, because a
report that cannot distinguish "clean" from "did not run" is worse than no
report. Findings are compared against `python-audit-baseline.json` and a count
that ROSE above its baseline is called out, which is the same shape the npm
build-chain pass uses. Exceptions are read from the repository's existing
`.vulnerability-exceptions.json`, so a Python advisory is governed by the same
expiring, owned entries as a Node one rather than by a second format.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_FILENAME = "python-audit-baseline.json"
EXCEPTIONS_FILENAME = ".vulnerability-exceptions.json"
#: Audited environment. `pip-audit` reads the installed distributions, which is
#: the set that actually runs, rather than a manifest's declared ranges.
AUDIT_TIMEOUT_SECONDS = 300
#: The key the baseline records counts under. A single environment today, but
#: keyed so a second one (a packaged desktop venv, say) can be added without
#: changing the file's shape.
ENVIRONMENT_KEY = "dev-environment"


class AuditError(RuntimeError):
    """The audit could not produce a trustworthy count."""


@dataclass(frozen=True)
class PythonFinding:
    package: str
    version: str
    advisory: str

    @property
    def identity(self) -> str:
        return f"{self.package} {self.version} {self.advisory}"


def audit_command() -> list[str]:
    """The pinned audit argv.

    Run through `sys.executable -m` rather than a bare `pip-audit`: the pinned
    tool is installed into the interpreter CI just built, and a bare name would
    resolve against PATH, which is how a different version gets audited than the
    one that was pinned.
    """
    return [
        sys.executable,
        "-m",
        "pip_audit",
        "--progress-spinner",
        "off",
        "--format",
        "json",
    ]


def parse_report(output: str) -> list[PythonFinding]:
    """Turn pip-audit's JSON into findings.

    Unparseable output raises rather than counting zero: a tool that changed its
    output shape would otherwise report a clean environment forever.
    """
    try:
        document = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AuditError(f"audit output was not JSON: {exc}") from exc
    dependencies = document.get("dependencies") if isinstance(document, Mapping) else None
    if not isinstance(dependencies, list):
        raise AuditError("audit output carried no 'dependencies' list")

    findings: list[PythonFinding] = []
    for entry in dependencies:
        if not isinstance(entry, Mapping):
            raise AuditError("audit output carried a non-object dependency entry")
        name = entry.get("name")
        version = entry.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise AuditError("audit output carried a dependency without a name and version")
        for vuln in entry.get("vulns") or []:
            if not isinstance(vuln, Mapping):
                raise AuditError("audit output carried a non-object vulnerability entry")
            advisory = vuln.get("id")
            if not isinstance(advisory, str) or not advisory:
                raise AuditError("audit output carried a vulnerability without an id")
            findings.append(PythonFinding(package=name, version=version, advisory=advisory))
    return findings


def run_audit(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = AUDIT_TIMEOUT_SECONDS,
) -> list[PythonFinding]:
    command = audit_command()
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise AuditError(f"audit did not finish within {timeout:g}s") from exc
    except OSError as exc:
        raise AuditError(f"audit could not be started: {exc}") from exc

    # pip-audit exits 1 when it FOUND vulnerabilities, which is a successful run.
    # Anything else is the tool failing rather than reporting.
    if result.returncode not in (0, 1):
        stderr = (result.stderr or "").strip().splitlines()
        tail = stderr[-1] if stderr else "no stderr"
        raise AuditError(f"audit exited {result.returncode}: {tail}")
    return parse_report(result.stdout or "")


def excepted_keys(path: Path, *, today: date | None = None) -> set[tuple[str, str]]:
    """(package, advisory) pairs governed by an exception that is still in force.

    Both halves matter. Matching the advisory id alone would let an entry filed
    for one package suppress the same advisory against a different one, and an
    exception is a statement about a specific dependency. Expiry matters more: the
    whole point of the `expires` field is that an exception stops applying, so an
    entry read without checking it suppresses a finding forever -- the exact
    failure the expiry field exists to prevent.

    An entry missing or misdating either field is DROPPED rather than trusted, so
    a malformed file excepts less, never more.
    """
    horizon = today or datetime.now(timezone.utc).date()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(document, Mapping):
        return set()
    entries = document.get("exceptions")
    if not isinstance(entries, list):
        return set()

    keys: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        package = entry.get("package")
        advisory = entry.get("advisory")
        expires = entry.get("expires")
        if not isinstance(package, str) or not package:
            continue
        if not isinstance(advisory, str) or not advisory:
            continue
        if not isinstance(expires, str):
            continue
        try:
            # An exception is valid THROUGH its expiry date, matching the npm
            # gate's own reading of the same field in the same file.
            if date.fromisoformat(expires) < horizon:
                continue
        except ValueError:
            continue
        keys.add((package, advisory))
    return keys


def unexcepted(
    findings: Sequence[PythonFinding], excepted: set[tuple[str, str]]
) -> list[PythonFinding]:
    return [finding for finding in findings if (finding.package, finding.advisory) not in excepted]


def load_baseline(path: Path) -> int | None:
    """The recorded count, or None when there is no usable one.

    None rather than 0, so a missing or malformed file reads as unmeasured: zero
    would report every advisory that predates the baseline as newly introduced.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, Mapping):
        return None
    environments = document.get("environments")
    if not isinstance(environments, Mapping):
        return None
    recorded = environments.get(ENVIRONMENT_KEY)
    if isinstance(recorded, bool) or not isinstance(recorded, int) or recorded < 0:
        return None
    return recorded


def write_baseline(path: Path, count: int) -> None:
    """Record *count*, preserving the file's own explanatory comment."""
    comment = ""
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    if isinstance(existing, Mapping):
        comment = str(existing.get("_comment", ""))
    document: dict[str, Any] = {}
    if comment:
        document["_comment"] = comment
    document["version"] = 1
    document["environments"] = {ENVIRONMENT_KEY: count}
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    try:
        findings = run_audit()
    except AuditError as exc:
        # The ONE non-zero exit: the report is absent, not empty.
        print(f"ERROR: Python dependency audit could not run: {exc}", file=sys.stderr)
        return 1

    excepted = excepted_keys(_REPO_ROOT / EXCEPTIONS_FILENAME)
    outstanding = unexcepted(findings, excepted)
    baseline = load_baseline(_REPO_ROOT / BASELINE_FILENAME)
    recorded_text = "no baseline" if baseline is None else str(baseline)
    print(
        f"Python advisories: {len(outstanding)} unexcepted "
        f"({len(findings) - len(outstanding)} governed exception(s), "
        f"baseline {recorded_text})"
    )
    for finding in sorted(outstanding, key=lambda item: item.identity):
        print(f"  {finding.identity}")

    if baseline is not None and len(outstanding) > baseline:
        print(
            "NOTE: the Python advisory count ROSE above its baseline. This gate "
            "reports only; the count is the worklist, and lowering it is what "
            f"clears the note ({len(outstanding)} found, baseline {baseline})."
        )
    return 0


def update_baseline() -> int:
    try:
        findings = run_audit()
    except AuditError as exc:
        print(f"ERROR: cannot regenerate the Python baseline: {exc}", file=sys.stderr)
        return 1
    excepted = excepted_keys(_REPO_ROOT / EXCEPTIONS_FILENAME)
    count = len(unexcepted(findings, excepted))
    path = _REPO_ROOT / BASELINE_FILENAME
    write_baseline(path, count)
    print(f"Recorded Python advisory baseline of {count} in {path.name}.")
    return 0


if __name__ == "__main__":
    if "--update-baseline" in sys.argv[1:]:
        raise SystemExit(update_baseline())
    raise SystemExit(main())
