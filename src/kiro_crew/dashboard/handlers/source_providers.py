"""Pull-request source data and owner-only review-thread mutation.

The browser sends a GitHub pull-request or GitLab merge-request URL. This
module validates the parsed host and path, then delegates authentication to a
validated absolute provider CLI. Credentials stay inside ``gh``/``glab`` and are
never returned to the browser. Credential-backed access is restricted to the
configured dashboard owner. Standalone local dashboards use their signed
bootstrap identity as the implicit owner when no channel owner is configured.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote, urlparse, urlunparse

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.sandbox import resource_limit_preexec, sandboxed_spawn_argv
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

_MAX_URL_LENGTH = 2048
# Hard per-section limits enforced while draining provider stdout. Diff-bearing
# sections get more room than metadata/checks, but no subprocess may retain the
# old payload-sized allowance independently.
_METADATA_OUTPUT_BYTES = 1 * 1024 * 1024
_DISCUSSION_OUTPUT_BYTES = 2 * 1024 * 1024
_DIFF_OUTPUT_BYTES = 4 * 1024 * 1024
_CHECKS_OUTPUT_BYTES = 1 * 1024 * 1024
_MAX_ERROR_BYTES = 64 * 1024
# Bound the normalized aggregate returned to the browser. Reservations below
# conservatively cover raw bytes, decoded JSON, normalized copies, and Python
# object overhead while a complete direct fetch remains alive.
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_SECONDARY_PAGE_SIZE = 100
_COMMAND_TIMEOUT_SECS = 30
_CACHE_TTL_SECS = 30
_CACHE_MAX_ENTRIES = 32
_CACHE_MAX_BYTES = 48 * 1024 * 1024
_PROVIDER_CONCURRENCY = 4
# Bound direct full/check fetches by task count and retained-memory weight.
# Same-URL callers coalesce before admission; detached stale tasks keep their
# reservation until their underlying task actually completes.
_DIRECT_FETCH_PENDING_MAX = 16
_DIRECT_FETCH_MAX_RESERVED_BYTES = 128 * 1024 * 1024
_FULL_FETCH_RESERVATION_BYTES = 64 * 1024 * 1024
_CHECKS_FETCH_RESERVATION_BYTES = 8 * 1024 * 1024
_PROVIDER_EXECUTABLE_OVERRIDES = {
    "gh": "KIROCREW_GH_BIN",
    "glab": "KIROCREW_GLAB_BIN",
}
_PROVIDER_EXECUTABLE_CANDIDATES = {
    executable: tuple(
        f"{directory}/{executable}"
        for directory in (
            "/usr/local/libexec/kirocrew",
            "/usr/libexec/kirocrew",
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/home/linuxbrew/.linuxbrew/bin",
        )
    )
    for executable in ("gh", "glab")
}
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
    "gh": frozenset({"GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN"}),
    "glab": frozenset({"GLAB_CONFIG_DIR", "GITLAB_TOKEN"}),
}
# url -> (stored_at, serialized_size_bytes, normalized_payload)
_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}
_CACHE_LOCK = asyncio.Lock()
_FULL_FETCH_INFLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}
_FULL_FETCH_TASKS: dict[str, set[asyncio.Task[dict[str, Any]]]] = {}
_FULL_FETCH_GENERATIONS: dict[str, int] = {}
_CHECKS_FETCH_INFLIGHT: dict[str, asyncio.Task[list[dict[str, Any]]]] = {}
_DIRECT_FETCH_RESERVATIONS: dict[asyncio.Task[Any], int] = {}
_provider_semaphore = asyncio.Semaphore(_PROVIDER_CONCURRENCY)
_SAFE_ERROR_RE = re.compile(r"\s+")
_PROVIDER_TOOL_NAME = "source_provider_cli"
logger = logging.getLogger(__name__)


class SourceProviderError(RuntimeError):
    """A provider CLI could not return the requested source data."""


def _sel():
    import kiro_crew.dashboard.handlers as _pkg  # circular import: package exports this module

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


def _path_parents(path: Path) -> list[Path]:
    """Return every parent through the filesystem root."""
    parents: list[Path] = []
    current = path.parent
    while True:
        parents.append(current)
        if current.parent == current:
            return parents
        current = current.parent


def _validate_provider_executable(candidate: str) -> str:
    """Return an agent-unwritable canonical executable path or raise.

    Provider children receive provider authentication, so ordinary same-user
    Homebrew/Linuxbrew paths are not a trust boundary. Requiring a canonical,
    root-owned, non-writable hierarchy makes the validated path stable through
    ``execve`` against the non-root agent threat model and closes the prior
    validation-to-execution replacement race.
    """
    if not os.path.isabs(candidate):
        raise ValueError("path must be absolute")
    getuid = getattr(os, "getuid", None)
    geteuid = getattr(os, "geteuid", getuid)
    if getuid is None or geteuid is None:
        raise ValueError("filesystem ownership checks are unavailable")
    if geteuid() == 0:
        raise ValueError("provider execution is disabled for a root gateway")

    original = Path(candidate)
    try:
        resolved = original.resolve(strict=True)
    except OSError as exc:
        raise ValueError("path does not exist") from exc
    if original != resolved:
        raise ValueError("path must be canonical and contain no symlinks")

    checked = [resolved, *_path_parents(resolved)]
    for index, path in enumerate(checked):
        try:
            path_stat = path.stat()
        except OSError as exc:
            raise ValueError("executable hierarchy is not accessible") from exc
        if index == 0:
            if not stat.S_ISREG(path_stat.st_mode):
                raise ValueError("path is not a regular file")
            if not os.access(path, os.X_OK):
                raise ValueError("file is not executable")
        elif not stat.S_ISDIR(path_stat.st_mode):
            raise ValueError("executable parent is not a directory")
        if path_stat.st_uid != 0:
            label = "executable" if index == 0 else "executable parent"
            raise ValueError(f"{label} is not root-owned")
        if path_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or os.access(path, os.W_OK):
            label = "executable" if index == 0 else "executable parent"
            raise ValueError(f"{label} is writable by the gateway user")
    return str(resolved)


def _resolve_provider_executable(executable: str) -> str:
    """Resolve gh/glab without consulting the mutable process PATH."""
    if executable not in _PROVIDER_EXECUTABLE_CANDIDATES:
        raise SourceProviderError("unsupported provider command")
    override_name = _PROVIDER_EXECUTABLE_OVERRIDES[executable]
    override = os.environ.get(override_name)
    if override is not None:
        try:
            return _validate_provider_executable(override)
        except ValueError as exc:
            raise SourceProviderError(
                f"{override_name} is not a trusted executable: {exc}"
            ) from exc

    for candidate in _PROVIDER_EXECUTABLE_CANDIDATES[executable]:
        try:
            return _validate_provider_executable(candidate)
        except ValueError:
            continue
    provider = "GitHub" if executable == "gh" else "GitLab"
    managed_dir = os.path.dirname(_PROVIDER_EXECUTABLE_CANDIDATES[executable][0])
    raise SourceProviderError(
        f"Can't load pull requests: the {provider} CLI ({executable}) isn't "
        f"installed in a location this panel trusts.\n"
        "\n"
        f"To fix this, copy your existing {executable} into a trusted location "
        "(needs sudo), then click Retry:\n"
        "\n"
        f"  sudo mkdir -p {managed_dir}\n"
        f'  sudo cp "$(command -v {executable})" {managed_dir}/{executable}\n'
        f"  sudo chown -R root {managed_dir}\n"
        f"  sudo chmod 755 {managed_dir}/{executable}\n"
        "\n"
        f"You won't have to sign in again -- your existing "
        f"`{executable} auth login` credentials are reused automatically.\n"
        "\n"
        f"Why sudo? This panel runs {executable} unattended with your "
        f"{provider} credentials, so -- unlike chat or your terminal -- it only "
        f"accepts a root-owned {executable} your user cannot write. A Homebrew "
        "or otherwise user-owned copy is intentionally refused here, even "
        "though it works elsewhere.\n"
        "\n"
        f"Alternative: point {override_name} at an already-trusted, absolute "
        f"{executable} path."
    )


@dataclass(frozen=True)
class SourceRef:
    provider: str
    url: str
    host: str
    owner: str
    repo: str
    number: int
    project: str = ""


def parse_source_url(raw_url: str) -> SourceRef:
    """Validate and normalize a supported pull/merge-request URL.

    Only public GitHub and GitLab hosts are accepted in this first version.
    Exact parsed-host checks prevent URLs that merely mention a trusted host in
    their path, query, or userinfo from reaching a provider CLI.
    """
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > _MAX_URL_LENGTH:
        raise ValueError("A pull-request URL is required.")
    parsed = urlparse(raw_url.strip())
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("Only HTTPS pull-request URLs without userinfo are supported.")
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")

    if host in {"github.com", "www.github.com"}:
        match = re.fullmatch(r"/([^/]+)/([^/]+)/pull/(\d+)", path, re.IGNORECASE)
        if not match:
            raise ValueError("Expected a GitHub URL like https://github.com/org/repo/pull/123.")
        owner, repo, number = match.groups()
        if owner in {".", ".."} or repo in {".", ".."}:
            raise ValueError("Invalid GitHub owner/repo path.")
        normalized = urlunparse(("https", "github.com", path, "", "", ""))
        return SourceRef("github", normalized, "github.com", owner, repo, int(number))

    if host in {"gitlab.com", "www.gitlab.com"}:
        # String ops instead of a regex: the previous /(.+)/-/merge_requests/
        # pattern backtracked polynomially on adversarial paths (CodeQL 192).
        marker = "/-/merge_requests/"
        idx = path.lower().rfind(marker)
        project = path[1:idx] if idx > 0 else ""
        number_text = path[idx + len(marker) :] if idx > 0 else ""
        if not project or not number_text.isdigit():
            raise ValueError(
                "Expected a GitLab URL like https://gitlab.com/group/project/-/merge_requests/123."
            )
        number = number_text
        if any(segment in {"", ".", ".."} for segment in project.split("/")):
            raise ValueError("Invalid GitLab project path.")
        normalized = urlunparse(("https", "gitlab.com", path, "", "", ""))
        repo = project.rsplit("/", 1)[-1]
        owner = project.rsplit("/", 1)[0] if "/" in project else ""
        return SourceRef(
            "gitlab", normalized, "gitlab.com", owner, repo, int(number), project=project
        )

    raise ValueError("Only github.com pull requests and gitlab.com merge requests are supported.")


def _safe_error(stderr: bytes) -> str:
    text = stderr.decode("utf-8", errors="replace").strip()
    text = redact_exfiltration_urls(text)[0]
    text = redact_credentials(text)[0]
    text = _SAFE_ERROR_RE.sub(" ", text)
    return text[:600] or "provider command failed"


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
    """Kill and reap a provider process tree after timeout, overflow, or cancellation."""
    if proc.returncode is None:
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
        except (OSError, ValueError):
            # Best-effort PID fallback if group lookup races with launcher exit.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    with contextlib.suppress(ProcessLookupError):
        await proc.wait()


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


async def _run_json(
    *argv: str,
    max_output_bytes: int = _METADATA_OUTPUT_BYTES,
) -> Any:
    """Run an allowlisted provider CLI with isolation, bounds, and SEL audit."""
    executable = argv[0] if argv else ""
    if max_output_bytes <= 0 or max_output_bytes > _DIFF_OUTPUT_BYTES:
        _audit_provider_cli(executable, "denied", "invalid_output_limit")
        raise SourceProviderError("invalid provider output limit")
    if executable not in {"gh", "glab"}:
        _audit_provider_cli(executable, "denied", "unsupported_provider")
        raise SourceProviderError("unsupported provider command")
    if platform_compat.IS_WINDOWS:
        _audit_provider_cli(executable, "denied", "sandbox_unavailable")
        raise SourceProviderError(
            "Pull-request source providers are not supported on Windows because "
            "OS-level provider sandboxing is unavailable."
        )
    try:
        resolved_executable = _resolve_provider_executable(executable)
    except SourceProviderError:
        _audit_provider_cli(executable, "denied", "executable_untrusted")
        raise

    allowed_env_keys = _PROVIDER_BASE_ENV_KEYS | _PROVIDER_AUTH_ENV_KEYS[executable]
    base_env = {key: value for key, value in os.environ.items() if key in allowed_env_keys}
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
        # parse_source_url only accepts gitlab.com URLs; pin the CLI to that
        # host so a self-managed default in glab config can't redirect the
        # API paths to a different instance.
        base_env["GITLAB_HOST"] = "gitlab.com"

    cleanup_path: str | None = None
    invoked = False
    try:
        async with _provider_semaphore:
            try:
                wrapped_argv, env, cleanup_path = sandboxed_spawn_argv(
                    [resolved_executable, *argv[1:]], mode="standard", env=base_env
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
                proc = await asyncio.create_subprocess_exec(
                    *wrapped_argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    preexec_fn=resource_limit_preexec(),
                    start_new_session=platform_compat.IS_POSIX,
                    creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
                )
            except OSError as exc:
                raise SourceProviderError(f"{executable} could not start") from exc
            stdout, stderr = await _collect_process_output(proc, executable, max_output_bytes)
        if proc.returncode != 0:
            message = _safe_error(stderr)
            lowered = message.lower()
            if (
                "unauthenticated" in lowered
                or "not logged in" in lowered
                or "authentication" in lowered
            ):
                message = f"{message} Run `{executable} auth login`, then retry."
            raise SourceProviderError(message)
        try:
            result = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceProviderError(f"{executable} returned invalid JSON") from exc
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


def _or_empty(value: Any) -> Any:
    """Coerce an already-recorded gather failure into an empty section."""
    if isinstance(value, BaseException):
        return []
    return value


def _mark_partial(partial_sections: list[str], section: str) -> None:
    """Add a partial-result section once while preserving display order."""
    if section not in partial_sections:
        partial_sections.append(section)


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _redact_provider_data(value: Any) -> Any:
    """Recursively redact secrets and suspicious URLs in provider-controlled data."""
    if isinstance(value, str):
        value = redact_exfiltration_urls(value)[0]
        return redact_credentials(value)[0]
    if isinstance(value, list):
        return [_redact_provider_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_provider_data(item) for key, item in value.items()}
    return value


def _payload_size_bytes(data: dict[str, Any]) -> int:
    """Return the compact UTF-8 JSON size used for response and cache bounds."""
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _author(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("login") or value.get("username") or value.get("name") or "")
    return str(value or "")


def _github_check(item: dict[str, Any]) -> dict[str, Any]:
    conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
    status = str(item.get("status") or "").upper()
    if status and status != "COMPLETED":
        bucket = "pending"
    elif conclusion in {"SUCCESS", "NEUTRAL"}:
        bucket = "passed"
    elif conclusion in {"SKIPPED", "STALE"}:
        bucket = "skipped"
    elif conclusion in {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR"}:
        bucket = "failed"
    else:
        bucket = "pending"
    return {
        "name": item.get("name") or item.get("context") or "Check",
        "workflow": item.get("workflowName") or "",
        "status": status,
        "conclusion": conclusion,
        "bucket": bucket,
        "url": item.get("detailsUrl") or item.get("targetUrl") or "",
        "startedAt": item.get("startedAt") or "",
        "completedAt": item.get("completedAt") or "",
    }


def _github_comment(item: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or item.get("databaseId") or ""),
        "kind": kind,
        "author": _author(item.get("author") or item.get("user")),
        "body": item.get("body") or "",
        "state": item.get("state") or "",
        "createdAt": item.get("createdAt")
        or item.get("submittedAt")
        or item.get("created_at")
        or "",
        "url": item.get("url") or item.get("html_url") or "",
        "path": item.get("path") or "",
        "line": item.get("line") or item.get("original_line"),
        "threadId": "",
        "resolvable": False,
        "resolved": False,
    }


_GITHUB_REVIEW_THREADS_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!)"
    "{repository(owner:$owner,name:$repo)"
    "{pullRequest(number:$number)"
    "{reviewThreads(first:100){nodes{id isResolved "
    "comments(first:10){nodes{databaseId}}}}}}}"
)


def _github_thread_ids(payload: Any) -> set[str]:
    """Return review-thread IDs scoped to the queried pull request."""
    if not isinstance(payload, dict):
        return set()
    try:
        nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        return set()
    return {str(node["id"]) for node in _as_list(nodes) if node.get("id")}


def _github_thread_map(payload: Any) -> dict[str, dict[str, Any]]:
    """Map an inline comment databaseId to its review thread id and state."""
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, dict):
        return result
    try:
        nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        return result
    for node in _as_list(nodes):
        thread_id = node.get("id")
        if not thread_id:
            continue
        is_resolved = bool(node.get("isResolved"))
        comments = node.get("comments")
        comment_nodes = comments.get("nodes") if isinstance(comments, dict) else []
        for comment in _as_list(comment_nodes):
            database_id = comment.get("databaseId")
            if database_id is None:
                continue
            result[str(database_id)] = {
                "threadId": str(thread_id),
                "resolved": is_resolved,
            }
    return result


async def _fetch_github(ref: SourceRef) -> dict[str, Any]:
    fields = ",".join(
        [
            "additions",
            "author",
            "baseRefName",
            "body",
            "changedFiles",
            "comments",
            "commits",
            "deletions",
            "headRefName",
            "headRefOid",
            "isDraft",
            "mergedAt",
            "number",
            "reviews",
            "state",
            "statusCheckRollup",
            "title",
            "updatedAt",
            "url",
        ]
    )
    repo_api = f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}"
    details = await _run_json("gh", "pr", "view", ref.url, "--json", fields)
    if not isinstance(details, dict):
        raise SourceProviderError("GitHub returned an invalid pull-request payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    files_raw: Any
    review_comments_raw: Any
    review_threads_raw: Any
    files_raw, review_comments_raw, review_threads_raw = await asyncio.gather(
        _run_json(
            "gh",
            "api",
            f"{repo_api}/files?per_page={_SECONDARY_PAGE_SIZE}",
            max_output_bytes=_DIFF_OUTPUT_BYTES,
        ),
        _run_json(
            "gh",
            "api",
            f"{repo_api}/comments?per_page={_SECONDARY_PAGE_SIZE}",
            max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
        ),
        _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_REVIEW_THREADS_QUERY}",
            "-f",
            f"owner={ref.owner}",
            "-f",
            f"repo={ref.repo}",
            "-F",
            f"number={ref.number}",
            max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
        ),
        return_exceptions=True,
    )
    partial_sections: list[str] = []
    if isinstance(files_raw, BaseException):
        _mark_partial(partial_sections, "files")
    if isinstance(review_comments_raw, BaseException) or isinstance(
        review_threads_raw, BaseException
    ):
        _mark_partial(partial_sections, "inline review comments")
    files = _or_empty(files_raw)
    review_comments = _or_empty(review_comments_raw)
    thread_map = _github_thread_map(_or_empty(review_threads_raw))
    file_rows = _as_list(files)
    review_comment_rows = _as_list(review_comments)
    changed_files = details.get("changedFiles")
    if (isinstance(changed_files, int) and changed_files > len(file_rows)) or (
        not isinstance(changed_files, int) and len(file_rows) >= _SECONDARY_PAGE_SIZE
    ):
        _mark_partial(partial_sections, "files")
    # GitHub's review-comment endpoint does not expose its total in the
    # primary payload. A full page means another page may exist.
    if len(review_comment_rows) >= _SECONDARY_PAGE_SIZE:
        _mark_partial(partial_sections, "inline review comments")

    inline_comments = [_github_comment(item, "inline") for item in review_comment_rows]
    for comment in inline_comments:
        info = thread_map.get(comment["id"])
        if info:
            comment["threadId"] = info["threadId"]
            comment["resolved"] = info["resolved"]
            comment["resolvable"] = True

    comments = [
        *(_github_comment(item, "comment") for item in _as_list(details.get("comments"))),
        *(_github_comment(item, "review") for item in _as_list(details.get("reviews"))),
        *inline_comments,
    ]
    commits = []
    for item in _as_list(details.get("commits")):
        authors = _as_list(item.get("authors"))
        commits.append(
            {
                "sha": item.get("oid") or "",
                "title": item.get("messageHeadline") or "",
                "body": item.get("messageBody") or "",
                "author": _author(authors[0]) if authors else "",
                "date": item.get("committedDate") or item.get("authoredDate") or "",
                "url": (
                    f"https://github.com/{ref.owner}/{ref.repo}/commit/{item.get('oid')}"
                    if item.get("oid")
                    else ""
                ),
            }
        )

    normalized_files = []
    for item in _as_list(files):
        normalized_files.append(
            {
                "path": item.get("filename") or "",
                "status": item.get("status") or "modified",
                "additions": item.get("additions") or 0,
                "deletions": item.get("deletions") or 0,
                "patch": item.get("patch") or "",
            }
        )

    return {
        "provider": "github",
        "url": details.get("url") or ref.url,
        "number": details.get("number") or ref.number,
        "title": details.get("title") or "",
        "description": details.get("body") or "",
        "state": details.get("state") or "",
        "draft": bool(details.get("isDraft")),
        "mergedAt": details.get("mergedAt") or "",
        "updatedAt": details.get("updatedAt") or "",
        "headBranch": details.get("headRefName") or "",
        "baseBranch": details.get("baseRefName") or "",
        "headSha": details.get("headRefOid") or "",
        "author": _author(details.get("author")),
        "additions": details.get("additions") or 0,
        "deletions": details.get("deletions") or 0,
        "changedFiles": details.get("changedFiles") or len(normalized_files),
        "commits": commits,
        "checks": [_github_check(item) for item in _as_list(details.get("statusCheckRollup"))],
        "comments": comments,
        "files": normalized_files,
        "partialSections": partial_sections,
    }


def _gitlab_check(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "").lower()
    if status in {"success", "passed"}:
        bucket = "passed"
    elif status in {"skipped", "manual"}:
        bucket = "skipped"
    elif status in {"failed", "canceled", "cancelled"}:
        bucket = "failed"
    else:
        bucket = "pending"
    return {
        "name": item.get("name") or "Job",
        "workflow": item.get("stage") or "",
        "status": status.upper(),
        "conclusion": status.upper(),
        "bucket": bucket,
        "url": item.get("web_url") or "",
        "startedAt": item.get("started_at") or "",
        "completedAt": item.get("finished_at") or "",
    }


async def _fetch_gitlab(ref: SourceRef) -> dict[str, Any]:
    project = quote(ref.project, safe="")
    mr_api = f"projects/{project}/merge_requests/{ref.number}"
    details = await _run_json("glab", "api", mr_api)
    if not isinstance(details, dict):
        raise SourceProviderError("GitLab returned an invalid merge-request payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    commits_raw: Any
    discussions_raw: Any
    changes_raw: Any
    pipelines_raw: Any
    commits_raw, discussions_raw, changes_raw, pipelines_raw = await asyncio.gather(
        _run_json("glab", "api", f"{mr_api}/commits?per_page={_SECONDARY_PAGE_SIZE}"),
        _run_json(
            "glab",
            "api",
            f"{mr_api}/discussions?per_page={_SECONDARY_PAGE_SIZE}",
            max_output_bytes=_DISCUSSION_OUTPUT_BYTES,
        ),
        _run_json("glab", "api", f"{mr_api}/changes", max_output_bytes=_DIFF_OUTPUT_BYTES),
        _run_json("glab", "api", f"{mr_api}/pipelines?per_page=20"),
        return_exceptions=True,
    )
    partial_sections: list[str] = []
    for raw_value, section in (
        (commits_raw, "commits"),
        (discussions_raw, "review discussions"),
        (changes_raw, "files"),
        (pipelines_raw, "checks"),
    ):
        if isinstance(raw_value, BaseException):
            _mark_partial(partial_sections, section)
    commits = _or_empty(commits_raw)
    discussions = _or_empty(discussions_raw)
    changes = _or_empty(changes_raw)
    pipelines = _or_empty(pipelines_raw)
    commit_rows = _as_list(commits)
    discussion_rows = _as_list(discussions)
    if len(commit_rows) >= _SECONDARY_PAGE_SIZE:
        _mark_partial(partial_sections, "commits")
    if len(discussion_rows) >= _SECONDARY_PAGE_SIZE:
        _mark_partial(partial_sections, "review discussions")

    jobs: Any = []
    pipeline_rows = _as_list(pipelines)
    if pipeline_rows and pipeline_rows[0].get("id"):
        try:
            jobs = await _run_json(
                "glab",
                "api",
                f"projects/{project}/pipelines/{pipeline_rows[0]['id']}/jobs?per_page={_SECONDARY_PAGE_SIZE}",
            )
        except SourceProviderError:
            _mark_partial(partial_sections, "checks")
            jobs = []

    raw_changes = changes.get("changes") if isinstance(changes, dict) else []
    change_rows = _as_list(raw_changes)
    reported_change_count = str(details.get("changes_count") or "").rstrip("+")
    if (isinstance(changes, dict) and changes.get("overflow")) or (
        reported_change_count.isdigit() and int(reported_change_count) > len(change_rows)
    ):
        _mark_partial(partial_sections, "files")
    normalized_files = []
    for item in change_rows:
        status = (
            "deleted"
            if item.get("deleted_file")
            else (
                "added"
                if item.get("new_file")
                else "renamed" if item.get("renamed_file") else "modified"
            )
        )
        patch = item.get("diff") or ""
        normalized_files.append(
            {
                "path": item.get("new_path") or item.get("old_path") or "",
                "status": status,
                "additions": sum(
                    1
                    for line in patch.splitlines()
                    if line.startswith("+") and not line.startswith("+++")
                ),
                "deletions": sum(
                    1
                    for line in patch.splitlines()
                    if line.startswith("-") and not line.startswith("---")
                ),
                "patch": patch,
            }
        )

    gitlab_comments = []
    for discussion in _as_list(discussions):
        thread_id = str(discussion.get("id") or "")
        for note in _as_list(discussion.get("notes")):
            if note.get("system"):
                continue
            gitlab_comments.append(
                {
                    "id": str(note.get("id") or ""),
                    "kind": "comment",
                    "author": _author(note.get("author")),
                    "body": note.get("body") or "",
                    "state": "",
                    "createdAt": note.get("created_at") or "",
                    "url": "",
                    "path": "",
                    "line": None,
                    "threadId": thread_id,
                    "resolvable": bool(note.get("resolvable")),
                    "resolved": bool(note.get("resolved")),
                }
            )

    return {
        "provider": "gitlab",
        "url": details.get("web_url") or ref.url,
        "number": details.get("iid") or ref.number,
        "title": details.get("title") or "",
        "description": details.get("description") or "",
        "state": details.get("state") or "",
        "draft": bool(details.get("draft") or details.get("work_in_progress")),
        "mergedAt": details.get("merged_at") or "",
        "updatedAt": details.get("updated_at") or "",
        "headBranch": details.get("source_branch") or "",
        "baseBranch": details.get("target_branch") or "",
        "headSha": details.get("sha") or "",
        "author": _author(details.get("author")),
        "additions": sum(item["additions"] for item in normalized_files),
        "deletions": sum(item["deletions"] for item in normalized_files),
        "changedFiles": len(normalized_files),
        "commits": [
            {
                "sha": item.get("id") or item.get("short_id") or "",
                "title": item.get("title") or "",
                "body": item.get("message") or "",
                "author": item.get("author_name") or "",
                "date": item.get("created_at") or item.get("committed_date") or "",
                "url": item.get("web_url") or "",
            }
            for item in commit_rows
        ],
        "checks": [_gitlab_check(item) for item in _as_list(jobs)],
        "comments": gitlab_comments,
        "files": normalized_files,
        "partialSections": partial_sections,
    }


async def _fetch_github_checks(ref: SourceRef) -> list[dict[str, Any]]:
    data = await _run_json(
        "gh",
        "pr",
        "view",
        ref.url,
        "--json",
        "statusCheckRollup",
        max_output_bytes=_CHECKS_OUTPUT_BYTES,
    )
    if not isinstance(data, dict):
        raise SourceProviderError("GitHub returned an invalid checks payload")
    return [_github_check(item) for item in _as_list(data.get("statusCheckRollup"))]


async def _fetch_gitlab_checks(ref: SourceRef) -> list[dict[str, Any]]:
    project = quote(ref.project, safe="")
    mr_api = f"projects/{project}/merge_requests/{ref.number}"
    pipelines = await _run_json(
        "glab",
        "api",
        f"{mr_api}/pipelines?per_page=1",
        max_output_bytes=_CHECKS_OUTPUT_BYTES,
    )
    pipeline_rows = _as_list(pipelines)
    if not pipeline_rows:
        return []
    pipeline = pipeline_rows[0]
    pipeline_id = pipeline.get("id")
    if not pipeline_id:
        return [_gitlab_check({**pipeline, "name": "Pipeline"})]
    jobs = await _run_json(
        "glab",
        "api",
        f"projects/{project}/pipelines/{pipeline_id}/jobs?per_page={_SECONDARY_PAGE_SIZE}",
        max_output_bytes=_CHECKS_OUTPUT_BYTES,
    )
    job_rows = _as_list(jobs)
    if not job_rows:
        return [_gitlab_check({**pipeline, "name": "Pipeline"})]
    return [_gitlab_check(item) for item in job_rows]


async def _fetch_pull_request_checks_uncached(ref: SourceRef) -> list[dict[str, Any]]:
    fetched = await (
        _fetch_github_checks(ref) if ref.provider == "github" else _fetch_gitlab_checks(ref)
    )
    checks = _redact_provider_data(fetched)
    if not isinstance(checks, list):
        raise SourceProviderError("provider returned an invalid checks payload")
    payload = {"checks": checks}
    if _payload_size_bytes(payload) > _MAX_PAYLOAD_BYTES:
        raise SourceProviderError("provider checks payload was too large")
    return checks


_T = TypeVar("_T")


def _finish_inflight(store: dict[str, asyncio.Task[_T]], url: str, task: asyncio.Task[_T]) -> None:
    """Drop a completed shared fetch and consume orphaned exceptions."""
    if store.get(url) is task:
        store.pop(url, None)
    if not task.cancelled():
        with contextlib.suppress(Exception):
            task.exception()


def _direct_fetch_tasks() -> set[asyncio.Task[Any]]:
    """Snapshot unique direct full/check tasks, including detached stale full work."""
    tasks: set[asyncio.Task[Any]] = set(_CHECKS_FETCH_INFLIGHT.values())
    for full_tasks in _FULL_FETCH_TASKS.values():
        tasks.update(full_tasks)
    return tasks


def _ensure_direct_fetch_capacity(reservation_bytes: int) -> None:
    tasks = _direct_fetch_tasks()
    reserved = sum(
        amount
        for task, amount in _DIRECT_FETCH_RESERVATIONS.items()
        if task in tasks and not task.done()
    )
    if (
        len(tasks) >= _DIRECT_FETCH_PENDING_MAX
        or reservation_bytes > _DIRECT_FETCH_MAX_RESERVED_BYTES - reserved
    ):
        raise SourceProviderError(
            "Too many pull-request source requests are pending; retry shortly."
        )


def _reserve_direct_fetch(task: asyncio.Task[Any], reservation_bytes: int) -> None:
    """Hold a conservative retained-byte lease until the task terminates."""
    _DIRECT_FETCH_RESERVATIONS[task] = reservation_bytes

    def release(done: asyncio.Task[Any]) -> None:
        _DIRECT_FETCH_RESERVATIONS.pop(done, None)

    task.add_done_callback(release)


async def fetch_pull_request_checks(raw_url: str) -> list[dict[str, Any]]:
    """Fetch current CI checks, coalescing concurrent requests for one URL."""
    ref = parse_source_url(raw_url)
    task = _CHECKS_FETCH_INFLIGHT.get(ref.url)
    if task is None:
        _ensure_direct_fetch_capacity(_CHECKS_FETCH_RESERVATION_BYTES)
        task = asyncio.create_task(_fetch_pull_request_checks_uncached(ref))
        _CHECKS_FETCH_INFLIGHT[ref.url] = task
        _reserve_direct_fetch(task, _CHECKS_FETCH_RESERVATION_BYTES)

        def finish_checks(done: asyncio.Task[list[dict[str, Any]]]) -> None:
            _finish_inflight(_CHECKS_FETCH_INFLIGHT, ref.url, done)

        task.add_done_callback(finish_checks)
    return await asyncio.shield(task)


async def _fetch_pull_request_uncached(ref: SourceRef, generation: int) -> dict[str, Any]:
    fetched = await (_fetch_github(ref) if ref.provider == "github" else _fetch_gitlab(ref))
    data = _redact_provider_data(fetched)
    if not isinstance(data, dict):
        raise SourceProviderError("provider returned an invalid pull-request payload")
    payload_size = _payload_size_bytes(data)
    if payload_size > _MAX_PAYLOAD_BYTES:
        raise SourceProviderError("provider pull-request payload was too large")

    async with _CACHE_LOCK:
        if _FULL_FETCH_GENERATIONS.get(ref.url, 0) != generation:
            # A successful mutation invalidated this generation while provider
            # I/O was running. Return its result to existing waiters, but never
            # let pre-mutation data overwrite the post-mutation cache.
            return data
        now = time.monotonic()
        # Sweep expired entries on write, then cap by both recency count and
        # aggregate serialized weight. A PR combines several provider commands,
        # so per-command pipe limits alone do not bound retained cache memory.
        for key in [
            key for key, (stored_at, _, _) in _CACHE.items() if now - stored_at >= _CACHE_TTL_SECS
        ]:
            del _CACHE[key]
        _CACHE[ref.url] = (now, payload_size, data)
        while (
            len(_CACHE) > _CACHE_MAX_ENTRIES
            or sum(entry[1] for entry in _CACHE.values()) > _CACHE_MAX_BYTES
        ):
            del _CACHE[min(_CACHE, key=lambda key: _CACHE[key][0])]
    return data


async def fetch_pull_request(raw_url: str, *, refresh: bool = False) -> dict[str, Any]:
    """Fetch a PR/MR, sharing one provider fanout per normalized URL."""
    ref = parse_source_url(raw_url)
    now = time.monotonic()
    async with _CACHE_LOCK:
        cached = _CACHE.get(ref.url)
        if not refresh and cached and now - cached[0] < _CACHE_TTL_SECS:
            return cached[2]
        task = _FULL_FETCH_INFLIGHT.get(ref.url)
        if task is None:
            _ensure_direct_fetch_capacity(_FULL_FETCH_RESERVATION_BYTES)
            generation = _FULL_FETCH_GENERATIONS.get(ref.url, 0)
            task = asyncio.create_task(_fetch_pull_request_uncached(ref, generation))
            _FULL_FETCH_INFLIGHT[ref.url] = task
            _FULL_FETCH_TASKS.setdefault(ref.url, set()).add(task)
            _reserve_direct_fetch(task, _FULL_FETCH_RESERVATION_BYTES)

            def finish_full_fetch(done: asyncio.Task[dict[str, Any]]) -> None:
                _finish_inflight(_FULL_FETCH_INFLIGHT, ref.url, done)
                active = _FULL_FETCH_TASKS.get(ref.url)
                if active is None:
                    return
                active.discard(done)
                if not active:
                    _FULL_FETCH_TASKS.pop(ref.url, None)
                    _FULL_FETCH_GENERATIONS.pop(ref.url, None)

            task.add_done_callback(finish_full_fetch)
    # Shield the shared fetch so one disconnected browser cannot cancel work
    # still awaited by another request for the same URL.
    return await asyncio.shield(task)


async def api_pull_request_source(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request`` with ``{url, refresh?}``."""
    denied = _authorize_owner_request(
        request, "source.pull_request.read", allow_local_no_owner=True
    )
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.read", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        data = await fetch_pull_request(
            str(body.get("url") or ""), refresh=bool(body.get("refresh"))
        )
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.read", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, "source.pull_request.read", "failed", "invalid_request")
        return web.json_response({"error": str(exc)}, status=400)
    except SourceProviderError as exc:
        _audit_source_api(request, "source.pull_request.read", "failed", "provider_error")
        return web.json_response({"error": str(exc)}, status=503)
    _audit_source_api(request, "source.pull_request.read", "completed")
    return web.json_response(data)


async def api_pull_request_checks(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/checks`` with ``{url}``."""
    denied = _authorize_owner_request(
        request, "source.pull_request.checks", allow_local_no_owner=True
    )
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.checks", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        checks = await fetch_pull_request_checks(str(body.get("url") or ""))
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.checks", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, "source.pull_request.checks", "failed", "invalid_request")
        return web.json_response({"error": str(exc)}, status=400)
    except SourceProviderError as exc:
        _audit_source_api(request, "source.pull_request.checks", "failed", "provider_error")
        return web.json_response({"error": str(exc)}, status=503)
    _audit_source_api(request, "source.pull_request.checks", "completed")
    return web.json_response({"checks": checks})


_GITHUB_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_=+-]{1,128}$")
_GITLAB_THREAD_ID_RE = re.compile(r"^[A-Fa-f0-9]{1,128}$")

_GITHUB_RESOLVE_MUTATION = (
    "mutation($threadId:ID!){resolveReviewThread(input:{threadId:$threadId})"
    "{thread{isResolved}}}"
)


async def _invalidate_pull_request_cache(url: str) -> None:
    """Supersede cached and in-flight data before a provider mutation."""
    async with _CACHE_LOCK:
        _CACHE.pop(url, None)
        if _FULL_FETCH_TASKS.get(url):
            _FULL_FETCH_GENERATIONS[url] = _FULL_FETCH_GENERATIONS.get(url, 0) + 1
        else:
            _FULL_FETCH_GENERATIONS.pop(url, None)
        _FULL_FETCH_INFLIGHT.pop(url, None)


async def resolve_pull_request_thread(raw_url: str, thread_id: str) -> None:
    """Resolve a review thread after conservatively invalidating cached data."""
    ref = parse_source_url(raw_url)
    thread_pattern = _GITHUB_THREAD_ID_RE if ref.provider == "github" else _GITLAB_THREAD_ID_RE
    if not thread_pattern.fullmatch(thread_id or ""):
        raise ValueError("A valid thread id is required.")
    if ref.provider == "github":
        threads = await _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_REVIEW_THREADS_QUERY}",
            "-f",
            f"owner={ref.owner}",
            "-f",
            f"repo={ref.repo}",
            "-F",
            f"number={ref.number}",
        )
        if thread_id not in _github_thread_ids(threads):
            raise ValueError("Review thread does not belong to this pull request.")
        # Invalidate before dispatch. Once the provider call starts its remote
        # result is uncertain under cancellation, so stale generations must
        # already be unable to refill or satisfy a post-mutation refresh.
        await _invalidate_pull_request_cache(ref.url)
        await _run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_RESOLVE_MUTATION}",
            "-f",
            f"threadId={thread_id}",
        )
    else:
        project = quote(ref.project, safe="")
        await _invalidate_pull_request_cache(ref.url)
        await _run_json(
            "glab",
            "api",
            "-X",
            "PUT",
            f"projects/{project}/merge_requests/{ref.number}/discussions/{thread_id}",
            "-f",
            "resolved=true",
        )


_LOCAL_DASHBOARD_OWNER_SUBJECTS = frozenset({"local-app", "local-startup"})


def is_owner_dashboard_request(request: web.Request) -> bool:
    """Return whether request has a configured or implicit local owner identity."""
    state = request.app["state"]
    owner_id = str(getattr(state, "owner_id", "") or "")
    caller = str(request.get("user") or "")
    if "app" not in request or request["app"] != "" or not caller:
        return False
    if owner_id:
        return caller == owner_id
    return caller in _LOCAL_DASHBOARD_OWNER_SUBJECTS


def _audit_source_api(
    request: web.Request,
    operation: str,
    outcome: str,
    error: str = "",
) -> None:
    """Best-effort source API audit without sensitive request or provider data."""
    caller = str(request.get("user") or "anonymous")
    try:
        _sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome=outcome,
            source="dashboard",
            error=error,
        )
    except Exception:
        logger.debug("SEL source API audit failed", exc_info=True)


def _authorize_owner_request(
    request: web.Request, operation: str, *, allow_local_no_owner: bool = False
) -> web.Response | None:
    """Require an explicit dashboard-user claim matching the configured owner.

    When no owner is configured, read-only operations may allow either signed
    standalone-local bootstrap identity. Mutations remain owner-only. Once an
    owner is configured, every operation requires an exact owner match.
    """
    state = request.app["state"]
    owner_id = str(getattr(state, "owner_id", "") or "")
    caller = str(request.get("user") or "")
    if not owner_id:
        if (
            allow_local_no_owner
            and request.get("app") == ""
            and caller in _LOCAL_DASHBOARD_OWNER_SUBJECTS
        ):
            return None
        _audit_source_api(request, operation, "denied", "owner_not_configured")
        return web.json_response({"error": "forbidden"}, status=403)
    if "app" not in request or request["app"] != "":
        _audit_source_api(request, operation, "denied", "app_token_not_allowed")
        return web.json_response({"error": "forbidden"}, status=403)
    if not caller:
        _audit_source_api(request, operation, "denied", "non_owner")
        return web.json_response({"error": "forbidden"}, status=403)
    if caller != owner_id:
        _audit_source_api(request, operation, "denied", "non_owner")
        return web.json_response({"error": "forbidden"}, status=403)
    return None


async def api_pull_request_resolve(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/resolve`` mutation.

    Credential-backed provider access requires an explicit dashboard-user claim.
    Configured installations require an exact owner match. Standalone local
    installations accept only signed local bootstrap subjects. App tokens and
    missing auth claims fail closed.
    """
    denied = _authorize_owner_request(request, "source.pull_request.resolve")
    if denied is not None:
        return denied

    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.resolve", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        await resolve_pull_request_thread(
            str(body.get("url") or ""), str(body.get("threadId") or "")
        )
    except asyncio.CancelledError:
        # The provider may have accepted the mutation before the client
        # disconnected, so record the uncertain outcome and preserve task
        # cancellation for aiohttp's shutdown/disconnect handling.
        _audit_source_api(request, "source.pull_request.resolve", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, "source.pull_request.resolve", "failed", "invalid_request")
        return web.json_response({"error": str(exc)}, status=400)
    except SourceProviderError as exc:
        _audit_source_api(request, "source.pull_request.resolve", "failed", "provider_error")
        return web.json_response({"error": str(exc)}, status=503)
    except Exception:
        _audit_source_api(request, "source.pull_request.resolve", "failed", "internal_error")
        raise
    _audit_source_api(request, "source.pull_request.resolve", "completed")
    return web.json_response({"resolved": True})


# ── Lightweight CI check status for sidebar chips ────────────────────────────
# Separate from the full PR cache: chips poll with the slots list, so this
# path must never block a slots response. Reads are served from this cache;
# refreshes are fire-and-forget with inflight dedup and a bounded map.

_CHECK_TTL_SECS = 60
# Public alias for periodic drivers (the owner-WS refresh loop) that pace
# their wakeups to the cache TTL. Sleeping exactly one TTL between rounds
# means each round finds the previous round's entries just expired — one
# provider fetch per URL per TTL, no wasted wakeups.
CHECK_STATUS_TTL_SECS = _CHECK_TTL_SECS
# The dashboard caps live slots at 500. Keeping one tiny status entry per slot
# avoids eviction churn when a large workspace is open.
_CHECK_CACHE_MAX = 512
# Bound both running and semaphore-waiting tasks. Overflow URLs receive a cache
# timestamp with no status, which backs them off for one TTL instead of creating
# a new task on every slots request.
_CHECK_PENDING_MAX = 16
# Public alias for periodic drivers that need to know the per-round admission
# cap so they can rotate which URLs they submit first across rounds (fair
# scheduling when the number of stale chips exceeds the cap).
CHECK_STATUS_PENDING_MAX = _CHECK_PENDING_MAX
_CHECK_UPDATE_DEBOUNCE_SECS = 0.1
# Bound concurrent gh/glab refresh operations so a cold cache across many
# sessions can't spawn a burst of provider subprocesses at once. TTL + inflight
# dedup handle rate and duplication; this caps instantaneous concurrency.
_CHECK_CONCURRENCY = 4
# These globals are loop-affine: the dashboard creates and mutates them only
# from its single asyncio event loop. They are not thread-safe by design.
_check_semaphore = asyncio.Semaphore(_CHECK_CONCURRENCY)
_check_cache: dict[str, tuple[float, dict[str, str] | None]] = {}
_check_inflight: set[str] = set()
_CHECK_TASKS: set[asyncio.Task] = set()  # keep strong refs until done
_CheckUpdateCallback = Callable[[], None]
_check_update_callbacks: set[_CheckUpdateCallback] = set()
_check_update_handle: asyncio.TimerHandle | None = None


def get_cached_check_status(url: str) -> dict[str, str] | None:
    """Cached status for a PR url: {"ci": running|passed|failed, "state": ...}.

    ``ci`` and ``state`` are each present only when known. Returns None until
    the first background refresh completes.
    """
    entry = _check_cache.get(url)
    return entry[1] if entry else None


def _trim_check_cache() -> None:
    while len(_check_cache) > _CHECK_CACHE_MAX:
        del _check_cache[min(_check_cache, key=lambda key: _check_cache[key][0])]


def _flush_check_updates() -> None:
    """Coalesce completed refreshes into one slots broadcast per event-loop tick."""
    global _check_update_handle
    callbacks = tuple(_check_update_callbacks)
    _check_update_callbacks.clear()
    _check_update_handle = None
    for callback in callbacks:
        with contextlib.suppress(Exception):
            callback()


def _queue_check_update(callback: _CheckUpdateCallback) -> None:
    global _check_update_handle
    _check_update_callbacks.add(callback)
    if _check_update_handle is None:
        _check_update_handle = asyncio.get_running_loop().call_later(
            _CHECK_UPDATE_DEBOUNCE_SECS, _flush_check_updates
        )


def schedule_check_refresh(urls: list[str], on_update: _CheckUpdateCallback | None = None) -> None:
    """Kick bounded background refreshes for stale URLs without blocking."""
    now = time.monotonic()
    for url in dict.fromkeys(urls):
        entry = _check_cache.get(url)
        if entry and now - entry[0] < _CHECK_TTL_SECS:
            continue
        if url in _check_inflight:
            continue
        if len(_check_inflight) >= _CHECK_PENDING_MAX:
            _check_cache[url] = (now, entry[1] if entry else None)
            _trim_check_cache()
            continue
        _check_inflight.add(url)
        task = asyncio.get_running_loop().create_task(_refresh_check_status(url, on_update))
        _CHECK_TASKS.add(task)
        task.add_done_callback(_CHECK_TASKS.discard)


async def _refresh_check_status(url: str, on_update: _CheckUpdateCallback | None = None) -> None:
    previous = _check_cache.get(url)
    try:
        async with _check_semaphore:
            status = await _fetch_check_status(url)
    except Exception:
        status = None
    finally:
        _check_inflight.discard(url)
    # A transient provider failure must not erase a known status. It still
    # refreshes the timestamp so repeated slots requests respect the TTL.
    if status is None and previous:
        status = previous[1]
    _check_cache[url] = (time.monotonic(), status)
    _trim_check_cache()
    if on_update and status is not None and (previous is None or previous[1] != status):
        _queue_check_update(on_update)


async def _fetch_check_status(url: str) -> dict[str, str] | None:
    ref = parse_source_url(url)
    result: dict[str, str] = {}
    if ref.provider == "github":
        data = await _run_json(
            "gh", "pr", "view", ref.url, "--json", "statusCheckRollup,state,isDraft"
        )
        if not isinstance(data, dict):
            return None
        buckets = [
            _github_check(item)["bucket"] for item in _as_list(data.get("statusCheckRollup"))
        ]
        if buckets:
            result["ci"] = (
                "failed" if "failed" in buckets else "running" if "pending" in buckets else "passed"
            )
        raw_state = str(data.get("state") or "").upper()
        if data.get("isDraft") and raw_state == "OPEN":
            result["state"] = "draft"
        elif raw_state == "MERGED":
            result["state"] = "merged"
        elif raw_state == "CLOSED":
            result["state"] = "closed"
        elif raw_state == "OPEN":
            result["state"] = "open"
        return result or None
    project = quote(ref.project, safe="")
    details = await _run_json("glab", "api", f"projects/{project}/merge_requests/{ref.number}")
    if isinstance(details, dict):
        raw_state = str(details.get("state") or "").lower()
        if details.get("draft") or details.get("work_in_progress"):
            result["state"] = "draft"
        elif raw_state == "merged":
            result["state"] = "merged"
        elif raw_state == "closed":
            result["state"] = "closed"
        elif raw_state in {"opened", "open"}:
            result["state"] = "open"
    pipelines = await _run_json(
        "glab", "api", f"projects/{project}/merge_requests/{ref.number}/pipelines?per_page=1"
    )
    rows = _as_list(pipelines)
    status = str(rows[0].get("status") or "").lower() if rows else ""
    if status == "success":
        result["ci"] = "passed"
    elif status in {"failed", "canceled", "cancelled"}:
        result["ci"] = "failed"
    elif status:
        result["ci"] = "running"
    return result or None
