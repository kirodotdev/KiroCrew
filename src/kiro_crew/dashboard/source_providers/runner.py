"""The one way a provider CLI (``gh``/``glab``) is run on a caller's behalf.

Every read and mutation reaches the provider through :func:`_run_provider`: an
allowlisted executable at a validated absolute path, a strict environment
allowlist with a pinned ``PATH``, the OS sandbox, hard output and time bounds,
bounded concurrency, and a credential-free SEL lifecycle event around each
attempt. Credentials stay inside the CLI and never reach its argv or a log.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew import github_runner, platform_compat
from kiro_crew.dashboard.source_providers import LOGGER_NAME, hosts, sanitize
from kiro_crew.dashboard.source_providers.contract import SourceProviderError

# Validation policy, well-known install dirs, and the strict-mode toggle are
# owned by the shared hardened runner (kiro_crew.github_runner) so every
# gh/glab-spawning surface applies exactly the same trust policy and never
# drifts. Bound under the historical private names because the source-provider
# handler is their long-standing import location (issue_radar's glab resolution
# and the provider tests reach them there).
from kiro_crew.github_runner import GH_ENV_PASSTHROUGH as _GH_ENV_PASSTHROUGH
from kiro_crew.github_runner import (
    PROVIDER_EXECUTABLE_CANDIDATES as _PROVIDER_EXECUTABLE_CANDIDATES,
)
from kiro_crew.github_runner import STRICT_PROVIDER_BIN_ENV as _STRICT_PROVIDER_BIN_ENV
from kiro_crew.github_runner import (
    gitlab_ambient_token_allowed,
    provider_executable_candidates,
)
from kiro_crew.github_runner import strict_provider_bins as _strict_provider_bins
from kiro_crew.github_runner import validate_provider_executable as _validate_provider_executable
from kiro_crew.sandbox import (
    create_subprocess_limited,
    sandboxed_spawn_argv,
    sandboxed_spawn_argv_async,
)

logger = logging.getLogger(LOGGER_NAME)


# Hard per-section limits enforced while draining provider stdout. Diff-bearing
# sections get more room than metadata/checks, but no subprocess may retain the
# old payload-sized allowance independently.
_METADATA_OUTPUT_BYTES = 1 * 1024 * 1024
_DISCUSSION_OUTPUT_BYTES = 2 * 1024 * 1024
_DIFF_OUTPUT_BYTES = 4 * 1024 * 1024
_CHECKS_OUTPUT_BYTES = 1 * 1024 * 1024
_MAX_ERROR_BYTES = 64 * 1024
_COMMAND_TIMEOUT_SECS = 30
_PROVIDER_CONCURRENCY = 4
# Provider commands are absolute. Keep PATH deterministic only for trusted
# system helpers a provider may invoke; never inherit a workspace-controlled
# PATH or search it for gh/glab.
_PROVIDER_SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# Only variables needed to configure the provider CLI, reach its API, and use
# that provider's authentication cross this trust boundary. In particular,
# unrelated gateway/AWS/Slack credentials and arbitrary PATH entries are never
# inherited.
_PROVIDER_BASE_ENV_KEYS = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NO_PROXY",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)
_PROVIDER_AUTH_ENV_KEYS = {
    # The gh set derives from the canonical union owned by the shared runner
    # (every key is gh-scoped auth/network/TLS config). GH_HOST passes through
    # it, but _run_json pins GH_HOST=github.com afterward, so the final env
    # cannot drift to a configured enterprise default — and for the same
    # reason the enterprise tokens are withheld: a github.com-pinned child can
    # never use them, so forwarding them is pure surplus credential surface.
    "gh": frozenset(_GH_ENV_PASSTHROUGH) - {"GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"},
    "glab": frozenset({"GLAB_CONFIG_DIR", "GITLAB_TOKEN"}),
}
_provider_semaphore = asyncio.Semaphore(_PROVIDER_CONCURRENCY)
_PROVIDER_TOOL_NAME = "source_provider_cli"


def _sel():
    import kiro_crew.dashboard.handlers as _pkg  # circular import: the package loads this module

    return _pkg.sel()


def _audit_provider_cli(
    executable: str,
    outcome: str,
    reason: str,
    *,
    critical: bool = False,
) -> None:
    """Emit a credential-free provider lifecycle event."""
    provider = executable if executable in {"gh", "glab"} else "unknown"
    try:
        _sel().log_tool_invocation(
            session_key="dashboard:source-provider",
            source="dashboard",
            tool_name=_PROVIDER_TOOL_NAME,
            tool_kind="provider_cli",
            outcome=outcome,
            downstream_service=provider,
            error=reason,
            metadata={"provider": provider, "reason": reason},
            critical=critical,
        )
    except Exception:
        if critical:
            raise
        logger.debug("SEL provider CLI audit failed", exc_info=True)


def _provider_setup_message(executable: str, override_name: str, last_error: str) -> str:
    """User-facing guidance when no acceptable provider CLI was found."""
    provider = "GitHub" if executable == "gh" else "GitLab"
    detail = f"\nLast check reported: {last_error}.\n" if last_error else ""
    if _strict_provider_bins():
        managed_dir = os.path.dirname(_PROVIDER_EXECUTABLE_CANDIDATES[executable][0])
        return (
            f"Can't load pull requests: {_STRICT_PROVIDER_BIN_ENV} is set, so this "
            f"host only accepts a root-owned {executable}.\n"
            "\n"
            f"  sudo mkdir -p {managed_dir}\n"
            f'  sudo cp "$(command -v {executable})" {managed_dir}/{executable}\n'
            f"  sudo chown -R root {managed_dir}\n"
            f"  sudo chmod 755 {managed_dir}/{executable}\n"
            f"{detail}"
            "\n"
            f"You won't have to sign in again -- your existing "
            f"`{executable} auth login` credentials are reused automatically.\n"
            "\n"
            f"Alternative: point {override_name} at an already-trusted, absolute "
            f"{executable} path, or unset {_STRICT_PROVIDER_BIN_ENV}."
        )
    return (
        f"Can't load pull requests: the {provider} CLI ({executable}) isn't "
        "available to the Kiro Crew gateway.\n"
        "\n"
        "Install it and sign in, then click Retry:\n"
        "\n"
        f"  brew install {executable}      # or your distro's package manager\n"
        f"  {executable} auth login\n"
        f"{detail}"
        "\n"
        f"Already installed? The gateway searches the standard install dirs plus "
        f"its own PATH and accepts your own {executable} -- Homebrew included. It "
        "still refuses one owned by another user, one that is world-writable, and "
        "one inside your project or workspace tree, since the agent can write "
        "there.\n"
        "\n"
        f"Alternative: point {override_name} at an absolute {executable} path."
    )


def _resolve_provider_executable(executable: str) -> str:
    """Resolve gh/glab: explicit override, well-known install dirs, then PATH."""
    if executable not in _PROVIDER_EXECUTABLE_CANDIDATES:
        raise SourceProviderError("unsupported provider command")
    override_name = github_runner.PROVIDER_CLI_OVERRIDE_ENV[executable]
    override = os.environ.get(override_name)
    if override is not None:
        try:
            return _validate_provider_executable(override)
        except ValueError as exc:
            raise SourceProviderError(
                f"{override_name} is not a trusted executable: {exc}",
                reason="executable_untrusted",
            ) from exc

    last_error = ""
    for candidate in provider_executable_candidates(executable):
        try:
            return _validate_provider_executable(candidate)
        except ValueError as exc:
            message = str(exc)
            # "does not exist" is noise on a host that simply lacks that dir;
            # keep the most informative rejection for the setup message.
            if message != "path does not exist":
                last_error = message
            continue
    reason = "executable_untrusted" if last_error else "executable_not_found"
    raise SourceProviderError(
        _provider_setup_message(executable, override_name, last_error),
        reason=reason,
    )


class _ProviderOutputTooLarge(RuntimeError):
    """A provider subprocess exceeded an output stream's byte limit."""


async def _read_stream_limited(stream: asyncio.StreamReader, limit: int, label: str) -> bytes:
    """Drain one subprocess pipe while enforcing a hard byte limit."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise _ProviderOutputTooLarge(f"provider {label} was too large")
        chunks.append(chunk)


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Kill and reap a provider process tree after timeout, overflow, or cancellation.

    The reap is bounded and drains the pipes: this path is reached with the
    stdout/stderr readers already cancelled by ``wait_for``, so a killed child
    blocked writing into a full pipe -- or a surviving descendant still holding
    the pipes open -- would make a bare ``await proc.wait()`` hang the calling
    task forever.
    """
    await platform_compat.kill_and_reap(proc)


async def _collect_process_output(
    proc: asyncio.subprocess.Process,
    executable: str,
    max_output_bytes: int,
) -> tuple[bytes, bytes]:
    """Read both pipes concurrently with hard limits and bounded lifetime."""
    if proc.stdout is None or proc.stderr is None:
        await _terminate_process(proc)
        raise SourceProviderError(f"{executable} did not expose provider output")
    tasks = [
        asyncio.create_task(_read_stream_limited(proc.stdout, max_output_bytes, "response")),
        asyncio.create_task(_read_stream_limited(proc.stderr, _MAX_ERROR_BYTES, "error output")),
        asyncio.create_task(proc.wait()),
    ]
    try:
        stdout, stderr, _ = await asyncio.wait_for(
            asyncio.gather(*tasks), timeout=_COMMAND_TIMEOUT_SECS
        )
        if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
            raise SourceProviderError(f"{executable} returned invalid provider output")
        return stdout, stderr
    except asyncio.TimeoutError as exc:
        await _terminate_process(proc)
        raise SourceProviderError(f"{executable} timed out reading the pull request") from exc
    except _ProviderOutputTooLarge as exc:
        await _terminate_process(proc)
        raise SourceProviderError(str(exc)) from exc
    except asyncio.CancelledError:
        await _terminate_process(proc)
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


_AUTH_FAILURE_MARKERS = (
    "unauthenticated",
    "not logged in",
    "authentication",
)
# GitLab's own wording for an expired sign-in. Scoped to glab so `gh` keeps the
# exact markers it had.
_GLAB_AUTH_FAILURE_MARKERS = _AUTH_FAILURE_MARKERS + ("401 unauthorized", "http 401")


def _provider_failure(executable: str, stderr: bytes, host: str = "") -> SourceProviderError:
    """The error for a failed provider call: redacted stderr plus a sign-in hint.

    For a self-managed GitLab the hint names the host: a bare ``glab auth login``
    signs in to gitlab.com by default, so it sends the user to the wrong server.
    ``glab auth login --hostname <host>`` signs in to the right one. That command
    also rides on the error as ``login_command``, built only from the allowlisted
    host, so the dashboard shows it without parsing provider-controlled text.
    """
    message = sanitize._safe_error(stderr)
    lowered = message.lower()
    markers = _GLAB_AUTH_FAILURE_MARKERS if executable == "glab" else _AUTH_FAILURE_MARKERS
    if not any(marker in lowered for marker in markers):
        return SourceProviderError(message)
    if executable == "glab" and host and host.casefold() != "gitlab.com":
        login_command = f"glab auth login --hostname {host}"
        return SourceProviderError(
            f"{message} Run `{login_command}`, then retry.",
            login_command=login_command,
        )
    return SourceProviderError(f"{message} Run `{executable} auth login`, then retry.")


def _provider_failure_message(executable: str, stderr: bytes, host: str = "") -> str:
    """The message of :func:`_provider_failure`, for callers that need only the text."""
    return str(_provider_failure(executable, stderr, host))


def _parse_json_success(executable: str, host: str = "") -> Callable[[int, bytes, bytes], Any]:
    """The ordinary provider contract: exit 0 and a JSON body, anything else fails."""

    def parse(returncode: int, stdout: bytes, stderr: bytes) -> Any:
        if returncode != 0:
            raise _provider_failure(executable, stderr, host)
        try:
            return json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceProviderError(f"{executable} returned invalid JSON") from exc

    return parse


@dataclass(frozen=True)
class _ConditionalRead:
    """One conditional REST GET: ``status`` is 200 or 304, ``etag`` the validator
    the server sent back (``""`` when it sent none). The body is deliberately
    not carried: the probes decide on status and validator alone and never
    compare bodies, so a 200 body is dead weight."""

    status: int
    etag: str


def _parse_conditional_get(executable: str) -> Callable[[int, bytes, bytes], _ConditionalRead]:
    """Parse ``gh api -i`` output for a conditional GET.

    ``-i`` puts the status line and response headers on stdout ahead of the
    body, which is the only way to read the refreshed ``ETag``. The exit code
    is NOT a usable signal here: ``gh`` treats every non-2xx status as a failure,
    so a ``304 Not Modified`` -- the whole point of the request -- exits 1 with
    ``gh: HTTP 304`` on stderr and an empty body. The status line decides
    instead, and only a status other than 200/304 is a provider error.
    """

    def parse(returncode: int, stdout: bytes, stderr: bytes) -> _ConditionalRead:
        text = stdout.decode("utf-8", errors="replace")
        head, sep, _body = text.partition("\r\n\r\n")
        if not sep:
            head, sep, _body = text.partition("\n\n")
        lines = head.splitlines()
        status_parts = lines[0].split() if lines else []
        try:
            status = int(status_parts[1]) if status_parts[0].upper().startswith("HTTP/") else 0
        except (IndexError, ValueError):
            status = 0
        if status not in (200, 304):
            raise _provider_failure(executable, stderr)
        etag = ""
        for line in lines[1:]:
            name, colon, value = line.partition(":")
            if colon and name.strip().lower() == "etag":
                etag = value.strip()
                break
        return _ConditionalRead(status, etag)

    return parse


async def _run_json(
    *argv: str,
    max_output_bytes: int = _METADATA_OUTPUT_BYTES,
    host: str = "",
) -> Any:
    """Run an allowlisted provider CLI expecting exit 0 and a JSON body.

    Thin wrapper over :func:`_run_provider`; every read and mutation that wants a
    plain JSON answer goes through here.
    """
    return await _run_provider(
        *argv,
        max_output_bytes=max_output_bytes,
        host=host,
        parse=_parse_json_success(argv[0] if argv else "", host),
    )


async def _run_provider(
    *argv: str,
    max_output_bytes: int = _METADATA_OUTPUT_BYTES,
    host: str = "",
    parse: Callable[[int, bytes, bytes], Any],
) -> Any:
    """Run an allowlisted provider CLI with isolation, bounds, and SEL audit.

    ``parse`` turns ``(returncode, stdout, stderr)`` into the result and raises
    :class:`SourceProviderError` for a failed run; it runs inside the audited
    section, so a parse failure is recorded as ``failed/provider_error`` and only
    a parsed result reaches ``completed/success``.

    ``host`` is REQUIRED for ``glab`` and must already have passed
    :func:`parse_source_url`; it is re-checked here so a caller cannot reach an
    unauthorized instance even if a future code path forgets to validate, and an
    omitted host is refused rather than silently resolved to gitlab.com.
    """
    executable = argv[0] if argv else ""
    if max_output_bytes <= 0 or max_output_bytes > _DIFF_OUTPUT_BYTES:
        _audit_provider_cli(executable, "denied", "invalid_output_limit")
        raise SourceProviderError("invalid provider output limit")
    if executable not in {"gh", "glab"}:
        _audit_provider_cli(executable, "denied", "unsupported_provider")
        raise SourceProviderError("unsupported provider command")
    gitlab_host = "gitlab.com"
    if executable == "glab":
        # Required, not defaulted: a call site that forgets `host` would
        # otherwise silently target gitlab.com, so an allowlisted self-managed MR
        # could be read -- or mutated -- on the PUBLIC instance at the same
        # project/IID. Failing loudly makes that class of bug impossible to
        # introduce, including from future mutation endpoints.
        if not host:
            _audit_provider_cli(executable, "denied", "host_not_specified")
            raise SourceProviderError("a GitLab host is required for glab calls")
        if host not in {"gitlab.com", "www.gitlab.com"}:
            if host not in hosts._allowed_gitlab_hosts():
                _audit_provider_cli(executable, "denied", "host_not_allowlisted")
                raise SourceProviderError("GitLab host is not allowlisted")
            gitlab_host = host
    # Windows is not refused here: it has no OS sandbox backend, so it reaches
    # the same no-backend policy a backend-less Linux host does, and
    # ``sandboxed_spawn_argv`` below owns that policy (fail closed unless the
    # operator set ``agent.sandbox_allow_unsandboxed_exec``). Every other bound
    # is platform-independent and still applies: the allowlisted executable, the
    # validated resolved path, the strict env allowlist with a pinned PATH, the
    # output cap, the timeout and the SEL audit.
    try:
        # Off the loop: resolution walks every candidate dir and stats the whole
        # parent chain of each hit (github_runner.validate_provider_executable),
        # and a miss re-walks all of PATH. The sidebar chip refresh reaches this
        # on a timer with no user present, so on the loop thread one slow
        # filesystem freezes every task -- including the liveness heartbeat --
        # until the loop watchdog kills the gateway and the supervisor respawns
        # into the same condition.
        resolved_executable = await asyncio.to_thread(_resolve_provider_executable, executable)
    except SourceProviderError as exc:
        _audit_provider_cli(
            executable,
            "denied",
            exc.reason or "executable_untrusted",
        )
        raise

    allowed_env_keys = _PROVIDER_BASE_ENV_KEYS | _PROVIDER_AUTH_ENV_KEYS[executable]
    if executable == "glab" and not gitlab_ambient_token_allowed(gitlab_host):
        # GITLAB_TOKEN is a single ambient credential with no host binding, so
        # forwarding it while GITLAB_HOST points at a self-managed instance would
        # send a gitlab.com PAT (and every permission it carries) to that server.
        # Self-managed hosts must therefore authenticate from the per-host entry
        # in glab's own config (reachable via GLAB_CONFIG_DIR), which is scoped to
        # the host it was created for.
        allowed_env_keys = allowed_env_keys - {"GITLAB_TOKEN"}
    # Matching follows the shared convention (exact on POSIX, case-folded on
    # Windows — see platform_compat.env_key_allowed) so the filter never
    # depends on the allowlist's casing agreeing with what os.environ yields.
    base_env = {
        key: value
        for key, value in os.environ.items()
        if platform_compat.env_key_allowed(key, allowed_env_keys)
    }
    base_env.update(
        {
            "GH_PAGER": "cat",
            "GLAB_PAGER": "cat",
            "NO_COLOR": "1",
            "PATH": _PROVIDER_SYSTEM_PATH,
        }
    )
    if executable == "gh":
        # All accepted GitHub URLs normalize to github.com. Pin bare API paths
        # to the same host instead of honoring a configured enterprise default.
        base_env["GH_HOST"] = "github.com"
    else:
        # Pin the CLI to the host parse_source_url authorized for this URL, so a
        # self-managed default in glab config can't redirect the bare API paths
        # to a different instance.
        base_env["GITLAB_HOST"] = gitlab_host

    cleanup_path: str | None = None
    invoked = False
    try:
        async with _provider_semaphore:
            try:
                wrapped_argv, env, cleanup_path = await sandboxed_spawn_argv_async(
                    [resolved_executable, *argv[1:]],
                    mode="standard",
                    env=base_env,
                    _prepare=sandboxed_spawn_argv,
                )
            except RuntimeError as exc:
                _audit_provider_cli(executable, "denied", "sandbox_rejected")
                raise SourceProviderError(f"{executable} could not start securely: {exc}") from exc
            audit_task = asyncio.create_task(
                asyncio.to_thread(
                    _audit_provider_cli,
                    executable,
                    "invoked",
                    "dispatch",
                    critical=True,
                )
            )
            try:
                await asyncio.shield(audit_task)
            except asyncio.CancelledError:
                # The worker thread cannot be cancelled once running. Wait for
                # it to settle so an on-disk invoked event is paired with the
                # outer request_cancelled terminal event before we re-raise.
                while not audit_task.done():
                    try:
                        await asyncio.shield(audit_task)
                    except asyncio.CancelledError:
                        continue
                if audit_task.exception() is None:
                    invoked = True
                raise
            except Exception as exc:
                raise SourceProviderError("provider audit unavailable") from exc
            invoked = True
            try:
                proc = await create_subprocess_limited(
                    *wrapped_argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    start_new_session=platform_compat.IS_POSIX,
                    creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
                )
            except OSError as exc:
                raise SourceProviderError(f"{executable} could not start") from exc
            stdout, stderr = await _collect_process_output(proc, executable, max_output_bytes)
        result = parse(proc.returncode if proc.returncode is not None else -1, stdout, stderr)
    except asyncio.CancelledError:
        if invoked:
            _audit_provider_cli(executable, "failed", "request_cancelled")
        raise
    except SourceProviderError:
        if invoked:
            _audit_provider_cli(executable, "failed", "provider_error")
        raise
    except Exception:
        if invoked:
            _audit_provider_cli(executable, "failed", "internal_error")
        raise
    finally:
        if cleanup_path:
            with contextlib.suppress(OSError):
                os.unlink(cleanup_path)
    _audit_provider_cli(executable, "completed", "success")
    return result
