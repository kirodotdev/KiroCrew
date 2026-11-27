"""The second attempt at a private GitHub source, through the host's ``gh`` sign-in.

An owner-designated fetch of ``https://github.com/<owner>/<repo>`` runs in the
``strict`` sandbox, which hides ``~/.config/gh``, so a private repository fails
there even when ``gh`` on this computer can read it. When that first attempt
fails the way a private repository fails (:func:`_private_github_shaped`), the
caller asks :func:`_gh_access` whether ``gh`` is present, signed in and can read
the repository, and on success re-runs the same split git fetch with
:func:`_gh_credential_env` on its network step alone.

Kiro Crew never sees the token: ``gh`` hands it to ``git`` over the credential
helper protocol. Every ``gh`` command here is an argv list with no shell, a
timeout, the trusted binary and minimal environment of
:mod:`kiro_crew.github_runner`, and an audit event before the spawn.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex as _shlex
import subprocess as _subprocess
from urllib.parse import urlsplit as _urlsplit

from kiro_crew import github_runner as _github_runner
from kiro_crew.apps.registry_pipeline import _FACADE
from kiro_crew.apps.registry_pipeline.git_targets import (
    _FETCH_CAUSE_GH_MISSING,
    _FETCH_CAUSE_GH_NO_ACCESS,
    _FETCH_CAUSE_GH_SIGNED_OUT,
    _FETCH_CAUSE_GH_STRICT,
    _FETCH_CAUSE_TIMEOUT,
    _GIT_REPOSITORY_NOT_FOUND_LABEL,
    _git_output_is_auth_shaped,
)

logger = logging.getLogger(_FACADE)

_GH_HOST = "github.com"
_GH_AUDIT_CALLER = "core:app-registry"
_GH_PROBE_TIMEOUT = 20

# GitHub's own name rules: an account is 1-39 alphanumerics or single inner
# hyphens; a repository is 1-100 of letters, digits, ``.``, ``_`` and ``-``.
_GH_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_GH_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}\Z")
_GH_URL_PATH_RE = re.compile(r"/([^/]+)/([^/]+)/?")


def _github_https_repo(git_url: str) -> str:
    """``owner/repo`` when *git_url* is exactly ``https://github.com/<owner>/<repo>``.

    Optionally with a trailing ``.git`` or ``/``. Anything else answers ``""``: user
    info, a port, a query or fragment, another host or scheme, extra path parts, or a
    name outside GitHub's rules. The answer is the only repository text that reaches
    a ``gh`` argv, so this is the validation gate for it.
    """
    if not isinstance(git_url, str) or not git_url.isascii():
        return ""
    # ``urlsplit`` drops tab, CR and LF silently, so they are refused before it runs.
    if any(ch.isspace() or not ch.isprintable() for ch in git_url):
        return ""
    try:
        parts = _urlsplit(git_url)
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme != "https" or parts.netloc.lower() != _GH_HOST or port is not None:
        return ""
    if parts.query or parts.fragment or "?" in git_url or "#" in git_url:
        return ""
    # A URL path, not a filesystem one: exactly ``/<owner>/<repo>``, an optional
    # trailing slash.
    match = _GH_URL_PATH_RE.fullmatch(parts.path)
    if match is None:
        return ""
    owner, repo = match.group(1), match.group(2)
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not _GH_OWNER_RE.match(owner) or not _GH_REPO_RE.match(repo):
        return ""
    if repo in {".", ".."} or "--" in owner:
        return ""
    return f"{owner}/{repo}"


def _private_github_shaped(text: str) -> bool:
    """Whether git output *text* reads like a repository this request could not see.

    A private GitHub repository fetched without a usable credential either asks
    for a user name (auth-shaped) or answers ``Repository not found``. A missing
    branch, a DNS or network failure, or a timeout is none of these and keeps its
    own report, so ``gh`` is never tried for it.
    """
    if not text:
        return False
    if _git_output_is_auth_shaped(text):
        return True
    low = text.lower()
    # The second test is the fixed label a redacted fetch keeps for GitHub's line.
    return (
        "repository not found" in low
        or _GIT_REPOSITORY_NOT_FOUND_LABEL in low
        or ("fatal: repository '" in low and "' not found" in low)
    )


def _run_gh_probe(gh_path: str, args: list[str]) -> _subprocess.CompletedProcess[str]:
    """One audited ``gh`` command, argv only, pinned to github.com."""
    return _github_runner.run_gh(
        [gh_path, *args],
        timeout=_GH_PROBE_TIMEOUT,
        audit_caller=_GH_AUDIT_CALLER,
        pin_host=_GH_HOST,
    )


async def _gh_access(repo: str) -> tuple[str, str]:
    """``(gh_path, "")`` when ``gh`` can read *repo*, else ``("", cause)``.

    *repo* must be the output of :func:`_github_https_repo`; anything else is
    refused here too, before a process starts. Strict provider-binary mode refuses
    the retry with ``gh_strict_mode`` before resolving or probing ``gh``. Otherwise,
    a successful ``gh api`` repository read answers immediately, without checking
    inactive accounts. Only a failed
    read runs ``gh auth status --hostname github.com`` to name the cause:
    ``gh_no_access`` when status succeeds, ``gh_signed_out`` otherwise. No probe
    uses ``--active``, so older GitHub CLI versions work too. Each step fails
    closed and never echoes ``gh``'s output: ``gh_missing`` when no trusted ``gh``
    resolves or it cannot start, ``timeout`` when a step runs past its limit.
    """
    owner, _, name = repo.partition("/")
    if not owner or _github_https_repo(f"https://{_GH_HOST}/{owner}/{name}") != repo:
        return "", _FETCH_CAUSE_GH_NO_ACCESS
    if _github_runner.strict_provider_bins():
        return "", _FETCH_CAUSE_GH_STRICT
    try:
        gh_path = await asyncio.to_thread(_github_runner.resolve_gh)
    except _github_runner.SetupError:
        return "", _FETCH_CAUSE_GH_MISSING
    try:
        proc = await asyncio.to_thread(
            _run_gh_probe, gh_path, ["api", f"repos/{owner}/{name}", "--silent"]
        )
        if proc.returncode == 0:
            return gh_path, ""
        proc = await asyncio.to_thread(
            _run_gh_probe, gh_path, ["auth", "status", "--hostname", _GH_HOST]
        )
    except _subprocess.TimeoutExpired:
        return "", _FETCH_CAUSE_TIMEOUT
    except (_github_runner.SetupError, OSError):
        return "", _FETCH_CAUSE_GH_MISSING
    cause = _FETCH_CAUSE_GH_NO_ACCESS if proc.returncode == 0 else _FETCH_CAUSE_GH_SIGNED_OUT
    logger.info("gh cannot read the registry source (%s)", cause)
    return "", cause


def _resolve_gh_git() -> str:
    """Trusted absolute git for a fetch that can read the host's gh sign-in."""
    try:
        for candidate in _github_runner.provider_executable_candidates("git"):
            try:
                return _github_runner.validate_provider_executable(candidate)
            except ValueError:
                continue
    except Exception:
        return ""
    return ""


def _gh_credential_env(env: dict[str, str], gh_path: str) -> dict[str, str]:
    """A copy of *env* for the ONE network git step that signs in through ``gh``.

    Adds the ``gh`` variables :func:`kiro_crew.github_runner.gh_env` forwards (config dir,
    proxy and CA settings, the session bus for the keyring) so the helper finds its
    sign-in, then two command-scope git config entries: an empty
    ``credential.helper``, which clears every helper configured before it, and
    ``credential.helper=!<gh> auth git-credential``. git runs a ``!`` helper
    through ``sh -c``; the string is fixed text and *gh_path*, a validated absolute
    path quoted with :func:`shlex.quote`, so no repository or user value is in it.
    Raises ``ValueError`` for a path that is not absolute.
    """
    if not gh_path or not os.path.isabs(gh_path):
        raise ValueError("the gh credential helper needs a validated absolute gh path")
    out = dict(env)
    for key, value in _github_runner.gh_env(pin_host=_GH_HOST).items():
        if key in _github_runner.GH_ENV_PASSTHROUGH or key == "DBUS_SESSION_BUS_ADDRESS":
            out[key] = value
    try:
        count = int(out.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    count = max(count, 0)
    pairs = (
        ("credential.helper", ""),
        ("credential.helper", f"!{_shlex.quote(gh_path)} auth git-credential"),
    )
    for key, value in pairs:
        out[f"GIT_CONFIG_KEY_{count}"] = key
        out[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    out["GIT_CONFIG_COUNT"] = str(count)
    out["GIT_TERMINAL_PROMPT"] = "0"
    # This process can read the gh sign-in, so it reads no system or global git
    # config: a `url.<x>.insteadOf` there could route the fetch to a remote helper
    # an agent planted. Only git's built-in https transport may run.
    # shortcut: a proxy or CA set only in git config files does not apply here;
    # the environment's proxy and CA variables still do.
    out["GIT_CONFIG_NOSYSTEM"] = "1"
    out["GIT_CONFIG_GLOBAL"] = os.devnull
    out["GIT_ALLOW_PROTOCOL"] = "https"
    return out


def _gh_refusal_sentence(cause: str, repo: str) -> str:
    """What an install banner says when ``gh`` could not read *repo*, by *cause*.

    It names the computer whose sign-in is used, because on a shared or remote crew
    the reader is not at that computer. Never a request to paste a token.
    """
    if cause == _FETCH_CAUSE_GH_STRICT:
        return (
            f"{repo} looks private. Signing in through the GitHub CLI is turned off while "
            "strict provider-binary mode is on for Kiro Crew. Make the repository "
            "readable without a sign-in, or install the app from a folder on the "
            "computer that runs Kiro Crew."
        )
    if cause == _FETCH_CAUSE_GH_MISSING:
        return (
            f"{repo} looks private. Install the GitHub CLI (gh) on the computer that runs "
            f"Kiro Crew and sign in with an account that can read {repo}."
        )
    if cause == _FETCH_CAUSE_GH_SIGNED_OUT:
        return (
            f"{repo} looks private. Sign in to the GitHub CLI (gh) on the computer that "
            f"runs Kiro Crew, with an account that can read {repo}. Kiro Crew uses that "
            "computer's GitHub sign-in."
        )
    if cause == _FETCH_CAUSE_TIMEOUT:
        return f"Checking the GitHub CLI sign-in for {repo} timed out. Try again."
    return (
        f"The GitHub CLI (gh) sign-in on the computer that runs Kiro Crew cannot read "
        f"{repo}. Sign in there with an account that has access to it."
    )
