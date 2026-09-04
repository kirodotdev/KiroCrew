"""GitHub-specific transport behind :mod:`github_client`'s patchable facade."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Protocol

from kiro_crew import github_runner

from .errors import (
    ProviderCliError,
    ProviderPermissionError,
    ProviderSetupError,
    sanitize_cli_stderr,
)

GhCliError = ProviderCliError
GhPermissionError = ProviderPermissionError
GhSetupError = ProviderSetupError

GH_OVERRIDE_ENV = "KIROCREW_ISSUE_RADAR_GH"
GH_AUTH_MARKERS = (
    "gh auth login",
    "not logged in",
    "authentication required",
    "requires authentication",
    "bad credentials",
    "http 401",
)


class GhRun(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        timeout: float,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess: ...


def resolve_binary(*, override_env: str = GH_OVERRIDE_ENV) -> str:
    """Resolve the trusted ``gh`` binary for Issue Radar."""
    try:
        return github_runner.resolve_gh(override_env=override_env)
    except github_runner.SetupError as exc:
        raise GhSetupError(str(exc), reason="not_installed") from exc


def stderr_tail(
    proc: subprocess.CompletedProcess, *, sanitize: Callable[[str], str] = sanitize_cli_stderr
) -> str:
    """Return the sanitized final stderr lines suitable for a response body."""
    return sanitize(" ".join((proc.stderr or "").strip().splitlines()[-3:]))


def run(
    argv: list[str],
    *,
    timeout: float,
    input_text: str | None,
    gh_bin: Callable[[], str],
) -> subprocess.CompletedProcess:
    """Run one audited, host-pinned ``gh`` command with client error mapping."""
    executable = gh_bin()
    try:
        return github_runner.run_gh(
            [executable, *argv[1:]],
            timeout=timeout,
            input_text=input_text,
            audit_caller="core:issue-radar",
            pin_host="github.com",
        )
    except FileNotFoundError as exc:  # pragma: no cover - resolve_binary guards first
        raise GhSetupError(
            "the `gh` CLI is not installed on this host", reason="not_installed"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GhCliError(f"`gh` timed out after {timeout}s") from exc
    except github_runner.SetupError as exc:
        # An audit-or-deny refusal is retryable host state, not missing setup.
        raise GhCliError(str(exc)) from exc


def raise_if_auth_failure(stderr: str, *, markers: tuple[str, ...] = GH_AUTH_MARKERS) -> None:
    """Classify a missing GitHub CLI session as a setup error."""
    if any(marker in (stderr or "").lower() for marker in markers):
        raise GhSetupError(
            "the `gh` CLI is not authenticated — run `gh auth login`",
            reason="not_authenticated",
        )


def run_api(
    path: str,
    jq_filter: str,
    *,
    timeout: float,
    paginate: bool,
    gh_run: GhRun,
    auth_classifier: Callable[[str], None],
    sanitize: Callable[[str], str] = sanitize_cli_stderr,
) -> list[dict]:
    """Run a read-only ``gh api`` request and parse its JSONL output."""
    argv = ["gh", "api", path]
    if paginate:
        argv.append("--paginate")
    argv += ["--jq", jq_filter]
    proc = gh_run(argv, timeout=timeout)
    if proc.returncode != 0:
        tail = stderr_tail(proc, sanitize=sanitize)
        auth_classifier(tail)
        raise GhCliError(f"gh api {path} failed (exit {proc.returncode}): {tail}")

    rows: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def run_write(
    method: str,
    path: str,
    payload: dict | None,
    *,
    timeout: float,
    gh_run: GhRun,
    sanitize: Callable[[str], str] = sanitize_cli_stderr,
) -> dict | list | None:
    """Run a REST mutation with payload-on-stdin and write error mapping."""
    argv = ["gh", "api", "--method", method, path]
    input_text = None
    if payload is not None:
        argv += ["--input", "-"]
        input_text = json.dumps(payload)
    proc = gh_run(argv, timeout=timeout, input_text=input_text)

    if proc.returncode != 0:
        stderr = proc.stderr or ""
        tail = sanitize(" ".join(stderr.strip().splitlines()[-3:]))
        if "HTTP 403" in stderr or "HTTP 401" in stderr:
            raise GhPermissionError(
                f"GitHub refused the write ({method} {path}) — your `gh` session "
                f"lacks the required triage/push access: {tail}"
            )
        raise GhCliError(f"gh api {method} {path} failed (exit {proc.returncode}): {tail}")

    output = (proc.stdout or "").strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def run_graphql_mutation(
    query: str,
    variables: dict[str, str],
    *,
    timeout: float,
    gh_run: GhRun,
    sanitize: Callable[[str], str] = sanitize_cli_stderr,
) -> dict:
    """Run one GitHub GraphQL mutation and return its ``data`` object."""
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        argv += ["-F", f"{key}={value}"]
    proc = gh_run(argv, timeout=timeout)
    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if proc.returncode != 0 or '"errors"' in (proc.stdout or ""):
        tail = sanitize(" ".join(combined.strip().splitlines()[-3:]))
        lowered = combined.lower()
        if (
            "HTTP 403" in combined
            or "HTTP 401" in combined
            or "not authorized" in lowered
            or "must have push access" in lowered
            or "resource not accessible" in lowered
        ):
            raise GhPermissionError(
                "GitHub refused the request — your `gh` session lacks the "
                f"required access: {tail}"
            )
        raise GhCliError(f"gh api graphql failed (exit {proc.returncode}): {tail}")
    try:
        parsed = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError as exc:
        raise GhCliError("gh returned unexpected output for a GraphQL mutation") from exc
    data = parsed.get("data") if isinstance(parsed, dict) else None
    return data if isinstance(data, dict) else {}
