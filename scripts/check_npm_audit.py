#!/usr/bin/env python3
"""Fail closed on high or critical production npm vulnerabilities."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

NPM_VERSION = "10.8.2"
AUDIT_TIMEOUT_SECONDS = 120
MAX_EXCEPTION_DAYS = 30
# Lead time on the expiry warning. An exception stays valid through its expiry
# date and the gate fails closed the day after, so this is the window in which
# the owner can still renew or remove it before a build breaks.
EXPIRY_WARNING_DAYS = 7
EXCEPTIONS_FILENAME = ".vulnerability-exceptions.json"
AUDITED_LOCKFILES = (
    "website/package-lock.json",
    "website/electron/package-lock.json",
    "site/package-lock.json",
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SEVERITIES = ("info", "low", "moderate", "high", "critical")
_ALLOWED_SEVERITIES = set(_SEVERITIES)
_BLOCKED_SEVERITIES = {"high", "critical"}
_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(_SEVERITIES)}
_PACKAGE_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
_ADVISORY_RE = re.compile(
    r"^(?:GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-"
    r"[23456789cfghjmpqrvwx]{4}|npm:[1-9][0-9]*)$"
)
_OWNER_RE = re.compile(
    r"^@[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})" r"(?:/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))?$"
)
_EXCEPTION_KEYS = {"package", "advisory", "paths", "reason", "owner", "expires"}


class GateError(RuntimeError):
    """The audit could not produce a trustworthy allow/block decision."""


@dataclass(frozen=True, order=True)
class Finding:
    lockfile: str
    package: str
    advisory: str
    severity: str
    title: str


@dataclass(frozen=True)
class ExceptionRule:
    package: str
    advisory: str
    paths: tuple[str, ...]
    reason: str
    owner: str
    expires: date

    def matches(self, finding: Finding) -> bool:
        return (
            self.package == finding.package
            and self.advisory == finding.advisory
            and finding.lockfile in self.paths
        )


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _require_string(value: Any, field: str, *, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise GateError(f"exception {field!r} must be a trimmed string")
    if not minimum <= len(value) <= maximum:
        raise GateError(
            f"exception {field!r} must contain between {minimum} and {maximum} characters"
        )
    return value


def validate_exception_document(document: Any, *, today: date | None = None) -> list[ExceptionRule]:
    """Validate the complete exception contract without third-party packages."""
    if not isinstance(document, dict) or set(document) != {"version", "exceptions"}:
        raise GateError("exception file must contain exactly 'version' and 'exceptions'")
    if type(document["version"]) is not int or document["version"] != 1:
        raise GateError("exception file version must be the integer 1")

    entries = document["exceptions"]
    if not isinstance(entries, list):
        raise GateError("exception file 'exceptions' must be an array")

    current_date = today or _utc_today()
    latest_expiry = current_date + timedelta(days=MAX_EXCEPTION_DAYS)
    seen_scopes: set[tuple[str, str, str]] = set()
    rules: list[ExceptionRule] = []

    for index, entry in enumerate(entries):
        prefix = f"exception #{index + 1}"
        if not isinstance(entry, dict) or set(entry) != _EXCEPTION_KEYS:
            raise GateError(f"{prefix} must contain exactly {sorted(_EXCEPTION_KEYS)}")

        package = _require_string(entry["package"], "package", minimum=1, maximum=214)
        if not _PACKAGE_RE.fullmatch(package):
            raise GateError(f"{prefix} has an invalid exact npm package name")

        advisory = _require_string(entry["advisory"], "advisory", minimum=1, maximum=64)
        if not _ADVISORY_RE.fullmatch(advisory):
            raise GateError(f"{prefix} advisory must be an exact GHSA id or npm:<source> id")

        paths = entry["paths"]
        if not isinstance(paths, list) or not paths:
            raise GateError(f"{prefix} paths must be a non-empty array")
        if any(not isinstance(path, str) or path not in AUDITED_LOCKFILES for path in paths):
            raise GateError(f"{prefix} contains a path that is not audited")
        if len(paths) != len(set(paths)):
            raise GateError(f"{prefix} paths must not contain duplicates")

        reason = _require_string(entry["reason"], "reason", minimum=20, maximum=500)
        owner = _require_string(entry["owner"], "owner", minimum=2, maximum=100)
        if not _OWNER_RE.fullmatch(owner):
            raise GateError(f"{prefix} owner must be an @github-user or @org/team")

        expires_text = _require_string(entry["expires"], "expires", minimum=10, maximum=10)
        try:
            expires = date.fromisoformat(expires_text)
        except ValueError as exc:
            raise GateError(f"{prefix} expires must be a real YYYY-MM-DD date") from exc
        if expires.isoformat() != expires_text:
            raise GateError(f"{prefix} expires must use exact YYYY-MM-DD format")
        if expires < current_date:
            raise GateError(f"{prefix} expired on {expires_text}")
        if expires > latest_expiry:
            raise GateError(f"{prefix} expires more than {MAX_EXCEPTION_DAYS} days in the future")

        for path in paths:
            scope = (package, advisory, path)
            if scope in seen_scopes:
                raise GateError(f"{prefix} duplicates exception scope {scope!r}")
            seen_scopes.add(scope)

        rules.append(
            ExceptionRule(
                package=package,
                advisory=advisory,
                paths=tuple(paths),
                reason=reason,
                owner=owner,
                expires=expires,
            )
        )

    return rules


def load_exception_rules(path: Path, *, today: date | None = None) -> list[ExceptionRule]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateError(f"cannot read exception file {path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateError(f"exception file {path} is not valid JSON: {exc}") from exc
    return validate_exception_document(document, today=today)


def expiring_exception_rules(
    rules: Sequence[ExceptionRule],
    *,
    today: date,
    within_days: int = EXPIRY_WARNING_DAYS,
) -> list[tuple[ExceptionRule, int]]:
    """Return (rule, days remaining) for exceptions inside the warning window.

    An already-expired rule is never returned: ``validate_exception_document``
    rejects it outright, so by the time a rule reaches here it is still valid and
    the count is zero or positive. Zero means the expiry date itself, which is
    the last day the rule holds.
    """
    upcoming: list[tuple[ExceptionRule, int]] = []
    for rule in rules:
        remaining = (rule.expires - today).days
        if 0 <= remaining <= within_days:
            upcoming.append((rule, remaining))
    # Soonest first, then by scope, so repeated runs emit an identical ordering.
    upcoming.sort(key=lambda item: (item[0].expires, item[0].package, item[0].advisory))
    return upcoming


def expiry_warning_lines(
    rules: Sequence[ExceptionRule],
    *,
    today: date,
    within_days: int = EXPIRY_WARNING_DAYS,
) -> list[str]:
    """Render one actionable GitHub warning annotation per expiring exception."""
    lines: list[str] = []
    for rule, remaining in expiring_exception_rules(rules, today=today, within_days=within_days):
        when = "today" if remaining == 0 else f"in {remaining} day(s)"
        fails_on = rule.expires + timedelta(days=1)
        lines.append(
            f"::warning::vulnerability exception for {rule.package} {rule.advisory} "
            f"(owner {rule.owner}) expires {when} on {rule.expires.isoformat()}; "
            f"this audit fails closed from {fails_on.isoformat()} until the exception is "
            f"renewed or removed in {EXCEPTIONS_FILENAME}"
        )
    return lines


def locate_npx(which: Callable[[str], str | None] = shutil.which) -> str:
    npx = which("npx")
    if not npx:
        raise GateError("npx is unavailable; refusing to skip the production dependency audit")
    return npx


def _advisory_id(advisory: Mapping[str, Any]) -> str:
    url = advisory.get("url")
    if not isinstance(url, str):
        raise GateError("npm audit advisory is missing a string URL")
    match = re.search(r"/advisories/(GHSA-[23456789cfghjmpqrvwx-]+)(?:$|[/?#])", url)
    if match and _ADVISORY_RE.fullmatch(match.group(1)):
        return match.group(1)

    source = advisory.get("source")
    if type(source) is int and source > 0:
        return f"npm:{source}"
    if isinstance(source, str) and source.isdigit() and int(source) > 0:
        return f"npm:{source}"
    raise GateError("npm audit advisory has neither a GHSA URL nor a numeric source id")


def _validate_vulnerability_records(vulnerabilities: Mapping[str, Any]) -> None:
    for package, record in vulnerabilities.items():
        if not isinstance(package, str) or not package:
            raise GateError("npm audit vulnerability map contains an invalid package key")
        if not isinstance(record, dict):
            raise GateError(f"npm audit vulnerability for {package!r} is not an object")
        if record.get("name") != package:
            raise GateError(f"npm audit vulnerability for {package!r} has a mismatched name")
        severity = record.get("severity")
        if severity not in _ALLOWED_SEVERITIES:
            raise GateError(f"npm audit vulnerability for {package!r} has invalid severity")
        via = record.get("via")
        if not isinstance(via, list):
            raise GateError(f"npm audit vulnerability for {package!r} has invalid 'via'")
        if not isinstance(record.get("nodes"), list) or not all(
            isinstance(node, str) for node in record["nodes"]
        ):
            raise GateError(f"npm audit vulnerability for {package!r} has invalid 'nodes'")
        for item in via:
            if isinstance(item, str):
                if item not in vulnerabilities:
                    raise GateError(
                        f"npm audit vulnerability for {package!r} references missing {item!r}"
                    )
            elif isinstance(item, dict):
                advisory_severity = item.get("severity")
                if advisory_severity not in _ALLOWED_SEVERITIES:
                    raise GateError(f"npm audit advisory for {package!r} has invalid severity")
                if not isinstance(item.get("title"), str) or not item["title"]:
                    raise GateError(f"npm audit advisory for {package!r} has invalid title")
                _advisory_id(item)
            else:
                raise GateError(f"npm audit vulnerability for {package!r} has invalid 'via' item")


def _resolve_advisories(
    package: str,
    vulnerabilities: Mapping[str, Any],
    trail: tuple[str, ...] = (),
) -> list[Mapping[str, Any]]:
    if package in trail:
        raise GateError(f"npm audit vulnerability references contain a cycle at {package!r}")

    resolved: list[Mapping[str, Any]] = []
    for item in vulnerabilities[package]["via"]:
        if isinstance(item, str):
            resolved.extend(_resolve_advisories(item, vulnerabilities, trail + (package,)))
        else:
            resolved.append(item)
    return resolved


def _metadata_blocked_count(report: Mapping[str, Any]) -> int:
    metadata = report.get("metadata")
    if not isinstance(metadata, dict):
        raise GateError("npm audit output is missing metadata")
    counts = metadata.get("vulnerabilities")
    if not isinstance(counts, dict):
        raise GateError("npm audit output is missing vulnerability counts")

    required = ("info", "low", "moderate", "high", "critical", "total")
    for severity in required:
        value = counts.get(severity)
        if type(value) is not int or value < 0:
            raise GateError(f"npm audit vulnerability count {severity!r} is invalid")
    if counts["total"] != sum(counts[severity] for severity in required[:-1]):
        raise GateError("npm audit vulnerability counts are internally inconsistent")
    return counts["high"] + counts["critical"]


def parse_audit_report(output: str, *, returncode: int, lockfile: str) -> list[Finding]:
    """Parse npm audit v2 output, accepting exit 1 only as a finding result."""
    if returncode not in {0, 1}:
        raise GateError(f"npm audit exited operationally with status {returncode}")
    if not output.strip():
        raise GateError("npm audit returned empty output")
    try:
        report = json.loads(output)
    except json.JSONDecodeError as exc:
        raise GateError(f"npm audit returned malformed JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise GateError("npm audit JSON root is not an object")
    if "error" in report:
        raise GateError(f"npm audit returned an error object: {report['error']!r}")
    if report.get("auditReportVersion") != 2:
        raise GateError("npm audit output is not an audit report v2 document")

    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        raise GateError("npm audit output is missing the vulnerability map")
    _validate_vulnerability_records(vulnerabilities)
    blocked_count = _metadata_blocked_count(report)

    if (blocked_count == 0 and returncode != 0) or (blocked_count > 0 and returncode != 1):
        raise GateError("npm audit exit status contradicts its high/critical vulnerability counts")

    findings_by_key: dict[tuple[str, str, str], Finding] = {}
    for package, record in vulnerabilities.items():
        advisories = _resolve_advisories(package, vulnerabilities)
        record_severity = record["severity"]
        if record_severity in _BLOCKED_SEVERITIES and not advisories:
            raise GateError(f"high/critical npm vulnerability {package!r} has no advisory details")
        for advisory in advisories:
            severity = advisory["severity"]
            if severity not in _BLOCKED_SEVERITIES:
                continue
            advisory_id = _advisory_id(advisory)
            finding = Finding(
                lockfile=lockfile,
                package=package,
                advisory=advisory_id,
                severity=severity,
                title=advisory["title"],
            )
            key = (lockfile, package, advisory_id)
            previous = findings_by_key.get(key)
            if previous is None or _SEVERITY_RANK[severity] > _SEVERITY_RANK[previous.severity]:
                findings_by_key[key] = finding

    findings = sorted(findings_by_key.values())
    if blocked_count > 0 and not findings:
        raise GateError("npm audit reported high/critical counts without resolvable advisories")
    if blocked_count == 0 and findings:
        raise GateError("npm audit advisories contradict its high/critical vulnerability counts")
    return findings


def audit_command(npx: str) -> list[str]:
    return [
        npx,
        "--yes",
        f"npm@{NPM_VERSION}",
        "audit",
        "--omit=dev",
        "--package-lock-only",
        "--ignore-scripts",
        "--audit-level=high",
        "--json",
    ]


def _shell_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _audit_failure_details(project_dir: Path, command: Sequence[str], stderr: str) -> str:
    tail = stderr[-2000:]
    sanitized_tail = "".join(char if char.isprintable() else " " for char in tail)
    sanitized_tail = re.sub(r"\s+", " ", sanitized_tail).strip()
    command_text = " ".join(_shell_quote(part) for part in command)

    details: list[str] = []
    if sanitized_tail:
        details.append(f"npm audit stderr (last 2000 characters): {sanitized_tail}")
    details.append(f"Rerun locally: cd {_shell_quote(str(project_dir))} && {command_text}")
    details.append(
        "Guidance: fix the audit/tool failure or vulnerable dependency; only use a narrowly "
        f"scoped, expiring {EXCEPTIONS_FILENAME} entry when immediate remediation is not possible."
    )
    return "\n".join(details)


def run_audit(
    lockfile: str,
    *,
    npx: str,
    repo_root: Path = _REPO_ROOT,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[Finding]:
    lock_path = repo_root / lockfile
    project_dir = lock_path.parent
    if not lock_path.is_file():
        raise GateError(f"audited lockfile is missing: {lockfile}")
    if not (project_dir / "package.json").is_file():
        raise GateError(f"audited package manifest is missing beside {lockfile}")

    command = audit_command(npx)
    try:
        result = runner(
            command,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=AUDIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        details = _audit_failure_details(project_dir, command, stderr)
        raise GateError(
            f"npm audit timed out after {AUDIT_TIMEOUT_SECONDS}s for {lockfile}\n{details}"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        stderr_value = getattr(exc, "stderr", "")
        stderr = stderr_value if isinstance(stderr_value, str) else ""
        details = _audit_failure_details(project_dir, command, stderr)
        raise GateError(f"npm audit could not run for {lockfile}: {exc}\n{details}") from exc

    try:
        return parse_audit_report(result.stdout, returncode=result.returncode, lockfile=lockfile)
    except GateError as exc:
        stderr = result.stderr if isinstance(result.stderr, str) else ""
        details = _audit_failure_details(project_dir, command, stderr)
        raise GateError(f"{exc}\n{details}") from exc


def unexcepted_findings(
    findings: Sequence[Finding], rules: Sequence[ExceptionRule]
) -> list[Finding]:
    return [finding for finding in findings if not any(rule.matches(finding) for rule in rules)]


def main() -> int:
    try:
        # One `today` for both the validation and the warning, so a run spanning
        # UTC midnight cannot judge the same rule against two different dates.
        today = _utc_today()
        rules = load_exception_rules(_REPO_ROOT / EXCEPTIONS_FILENAME, today=today)
        # Emitted BEFORE the audit: the audit reaches the network and can fail
        # for reasons of its own, and the owner still needs the expiry notice
        # when it does. This runs on every nightly, which is what makes the
        # warning time-based rather than dependent on someone opening a PR.
        for line in expiry_warning_lines(rules, today=today):
            print(line)
        npx = locate_npx()
        findings: list[Finding] = []
        for lockfile in AUDITED_LOCKFILES:
            print(f"Auditing production dependencies in {lockfile} with npm@{NPM_VERSION} ...")
            findings.extend(run_audit(lockfile, npx=npx))
        blocked = unexcepted_findings(findings, rules)
    except GateError as exc:
        print(f"ERROR: production dependency audit failed closed: {exc}", file=sys.stderr)
        return 1

    excepted_count = len(findings) - len(blocked)
    if blocked:
        print("ERROR: unexcepted high/critical production vulnerabilities:", file=sys.stderr)
        for finding in blocked:
            print(
                f"  {finding.lockfile}: {finding.package} {finding.advisory} "
                f"({finding.severity}) - {finding.title}",
                file=sys.stderr,
            )
        return 1

    print(
        f"Production dependency audit passed: {len(AUDITED_LOCKFILES)} lockfiles, "
        f"{excepted_count} governed exception(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
