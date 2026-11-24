"""Plain words for an MCP server npm could not download because a registry refused it.

An ``npx``-launched server fails before it starts when the package registry in the
user's npm settings refuses npm's sign-in, most often because a registry token has
expired. npm then prints its error code first and, last, advice written for the public
registry (recover a password at npmjs.com, run ``npm login``) and a log path, so the
end of its stderr that a failed probe shows names neither the cause nor the registry.

:func:`npm_sign_in_hint` reads the whole redacted stderr, from its first line, and
says in two short sentences what happened, which registry, and what to do. The
registry is the one npm's stderr names; npm leaves it out of a refusal that carries a
sign-in challenge, so then npm itself is asked which registry its settings use
(``npm config get``), in the sandbox the server ran in. The settings that hold the
registry (``~/.npmrc``) are a credential store this module never opens.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from kiro_crew import platform_compat
from kiro_crew.sandbox import (
    app_backend_secret_mask_targets,
    create_subprocess_limited,
    sandboxed_spawn_argv,
    sandboxed_spawn_argv_async,
)

logger = logging.getLogger("kiro_crew.mcp_discovery")

#: npm's own error-code line for a refused or missing registry sign-in, in its current
#: spelling (``npm error``) and its older one (``npm ERR!``). Anchored to that line so a
#: server's own text that merely mentions a code is not read as npm's verdict.
_SIGN_IN_CODE = re.compile(
    r"^npm (?:error|ERR!) code (E401|E403|ENEEDAUTH|EAUTHUNKNOWN)[ \t\r]*$", re.MULTILINE
)

#: The lines where npm's stderr names the registry it was refused by: the status line
#: of a 401 or 403 answer without a sign-in challenge (``401 Unauthorized - GET <url>``),
#: and a registry that requires a sign-in npm does not have (``... logged in to <url>``).
_REGISTRY_URL = re.compile(
    r"^npm (?:error|ERR!) (?:40[13]\b.*? - [A-Z]+ |need auth\b.*?logged in to )(https?://\S+)",
    re.MULTILINE,
)

#: What a host must look like to be named: a name, an IPv4 or bracketed IPv6 address,
#: and an optional port. Anything else (a redaction marker, stray punctuation) is left
#: out of the sentence.
_HOST = re.compile(r"[A-Za-z0-9.-]+(?::\d+)?|\[[0-9A-Fa-f:.]+\](?::\d+)?")

#: The scope of a scoped package name (``@scope/name``): npm takes such a package from
#: ``@scope:registry`` when the settings name one.
_SCOPE = re.compile(r"@[A-Za-z0-9][\w.~-]*(?=/)")

#: How long ``npm config get`` may take. It reads npm's settings and answers without
#: the network, so this is only a bound on a stuck npm.
_NPM_CONFIG_WAIT_SECS = 5.0

#: How each code reads in the sentence: what the registry did, then what to do.
_SIGN_IN_AGAIN = "Sign in to that registry again or renew its token"
_REFUSALS = {
    "E401": ("did not accept npm's sign-in", _SIGN_IN_AGAIN),
    "EAUTHUNKNOWN": ("did not accept npm's sign-in", _SIGN_IN_AGAIN),
    "ENEEDAUTH": ("needs npm to sign in", _SIGN_IN_AGAIN),
    # A registry answers 403 both to an expired sign-in and to one that may not
    # download this package, so the advice covers both.
    "E403": (
        "refused npm access",
        "Check that your sign-in to that registry is current and may download this package",
    ),
}


def _host(url: str) -> str:
    """The host and port of *url*, without any user part, or ``""`` when it has none."""
    host = urlsplit(url).netloc.rpartition("@")[2]
    return host if _HOST.fullmatch(host) else ""


def _registry_host(stderr: str) -> str:
    """The host and port of the registry npm's stderr says refused it, or ``""``."""
    match = _REGISTRY_URL.search(stderr)
    return _host(match.group(1)) if match else ""


def _package_scope(args: Sequence[str]) -> str:
    """The scope of the package an ``npx`` launch names, or ``""``.

    The package is the ``-p``/``--package`` value, else the first argument that is not
    an option; the server's own arguments after it are never read as the package.
    """
    for i, arg in enumerate(args):
        if arg in ("-p", "--package"):
            arg = args[i + 1] if i + 1 < len(args) else ""
        elif arg.startswith("--package="):
            arg = arg.partition("=")[2]
        elif arg.startswith("-"):
            continue
        match = _SCOPE.match(arg)
        return match.group(0) if match else ""
    return ""


async def _configured_registry(env: dict[str, str], args: Sequence[str]) -> str:
    """The registry URL npm's settings give the launched package, or ``""``.

    Asked of npm itself with the failed server's own environment, so npm reads the
    settings the server's npm read. The scope's own registry, when the settings name
    one, comes before the default. npm runs in the sandbox with the probe's masks,
    since *env*'s ``PATH`` is config text that picks which ``npm`` runs.
    """
    npm = shutil.which("npm", path=env.get("PATH", ""))
    if not npm:
        return ""
    scope = _package_scope(args)
    keys = [f"{scope}:registry", "registry"] if scope else ["registry"]
    argv, child_env, cleanup = await sandboxed_spawn_argv_async(
        [npm, "config", "get", *keys],
        mode="standard",
        env=env,
        strip_python_env=True,
        extra_hidden_dirs=await asyncio.to_thread(app_backend_secret_mask_targets),
        _prepare=sandboxed_spawn_argv,
    )
    try:
        proc = await create_subprocess_limited(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=child_env,
            start_new_session=platform_compat.IS_POSIX,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_NPM_CONFIG_WAIT_SECS)
        except BaseException:
            await platform_compat.kill_and_reap(proc)
            raise
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)
    # One key prints its value alone; several print one ``key=value`` line each.
    lines = out.decode("utf-8", "replace").splitlines()
    values = lines[:1] if len(keys) == 1 else [line.partition("=")[2] for line in lines]
    return next((v.strip() for v in values if v.strip() not in ("", "undefined", "null")), "")


async def npm_sign_in_hint(stderr: str, env: dict[str, str], args: Sequence[str]) -> str:
    """What an npm registry sign-in failure in *stderr* means, or ``""`` when it holds none.

    *stderr* is a failed server's whole stderr, already redacted; *env* and *args* are
    the environment and arguments it was launched with. The text names the registry's
    host, npm's error code, and what to do. It never quotes a token or a path. The host
    is left out when neither npm's stderr nor npm's settings give one.
    """
    code = _SIGN_IN_CODE.search(stderr)
    if not code:
        return ""
    host = _registry_host(stderr)
    if not host:
        try:
            host = _host(await _configured_registry(env, args))
        except Exception:
            logger.debug("npm did not say which registry it uses", exc_info=True)
    refusal, action = _REFUSALS[code.group(1)]
    registry = f"the package registry {host}" if host else "the package registry"
    return (
        f"npm could not download this server: {registry} in your npm settings {refusal} "
        f"(npm error {code.group(1)}). {action}, then test the server again."
    )
