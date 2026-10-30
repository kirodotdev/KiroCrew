"""Recognise an npm registry refusal and say what it means.

``npm install`` reads its registry from the operator's own npm configuration.
When that registry refuses the request -- an expired login is the usual cause --
npm prints ``code E401`` or ``code E403`` with advice (``npm login``) that is
often wrong for the registry in use, and nothing in it says the failure comes
from the operator's registry setting rather than from Kiro Crew or Playwright.

This module turns that output into a plain message naming the configured
registry. It never changes which registry npm uses: installing from
:data:`PUBLIC_NPM_REGISTRY` instead is a separate, opt-in retry the operator
chooses (see :func:`kiro_crew.browser_cli.install.install`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import urlsplit

#: The registry the opt-in retry installs from.
PUBLIC_NPM_REGISTRY = "https://registry.npmjs.org/"

#: Job error code for a refusal by the operator's configured registry. The
#: dashboard offers the public-registry retry only for this code.
ERROR_REGISTRY_AUTH = "registry_auth"
#: Job error code for a refusal by the public registry during the opt-in retry.
#: No further retry is offered: the public registry is the retry.
ERROR_PUBLIC_REGISTRY_REFUSED = "public_registry_refused"

# npm's own diagnostic line: ``npm error code E401`` on npm 10+, ``npm ERR! code
# E401`` before. Only a whole line in that shape counts, and only the FIRST such
# line: a lifecycle script's output can contain the text ``code E401`` while npm
# itself reports another failure code.
_NPM_CODE_LINE_RE = re.compile(r"^npm (?:error|ERR!) code (\S+)\s*$", re.MULTILINE)
_AUTH_CODES = frozenset({"E401", "E403"})

#: Scope of the package the installer fetches. npm prefers a configured
#: ``@playwright:registry`` over ``registry`` for it.
_PACKAGE_SCOPE = "@playwright"

#: Seconds allowed for ``npm config get registry``. It reads local files only.
_CONFIG_GET_TIMEOUT_S = 30.0

#: Longest registry URL shown; a configured value past this is not a URL we name.
_MAX_REGISTRY_LEN = 512


def auth_failure_code(output: str) -> str | None:
    """``"E401"`` or ``"E403"`` when npm reported a registry refusal, else ``None``."""
    match = _NPM_CODE_LINE_RE.search(output)
    if match is None or match.group(1) not in _AUTH_CODES:
        return None
    return match.group(1)


def display_registry(raw: str) -> str | None:
    """The registry fit to show, as ``scheme://host[:port]/``, or ``None``.

    Only the scheme, host and port are kept. The user name, password, PATH, query
    and fragment are all dropped, because a registry URL can carry a credential
    in any of them (some hosted registries put the token in the path) and the
    message is shown in the dashboard and written to the log. The host is enough
    for the operator to tell which registry refused.
    """
    text = raw.strip()
    if not text or len(text) > _MAX_REGISTRY_LEN or any(c.isspace() for c in text):
        return None
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{parts.scheme}://{netloc}/"


def configured_registry(
    npm: str, run: Callable[[list[str], float], tuple[int, str, str]], *, prefix: str
) -> str | None:
    """The registry npm uses for the CLI package, as :func:`display_registry` shows it.

    A configured ``@playwright:registry`` wins over ``registry`` for this
    package, exactly as npm resolves it, so it is asked first; npm answers
    ``undefined`` when it is unset, which is not a URL and falls through.

    npm is asked in the same configuration context as the install
    (``--global --prefix <prefix>``): a global install does not read a project
    ``.npmrc`` in the working directory, so the lookup must not read it either.

    *run* is the installer's own subprocess runner, so this child gets the same
    environment, process group and shutdown handling as the install itself.
    ``None`` when npm cannot answer.
    """
    for key in (f"{_PACKAGE_SCOPE}:registry", "registry"):
        rc, out, _err = run(
            [npm, "config", "get", key, "--global", "--prefix", prefix], _CONFIG_GET_TIMEOUT_S
        )
        if rc != 0:
            continue
        lines = [line for line in out.splitlines() if line.strip()]
        shown = display_registry(lines[-1]) if lines else None
        if shown is not None:
            return shown
    return None


def auth_failure_message(code: str, registry: str | None, *, public: bool) -> str:
    """Plain English for the log and for clients that do not know the error code.

    The dashboard renders its own translated text from the job's error code and
    parameters; this is what a log reader, an older dashboard and the agent
    hand-off see.
    """
    if public:
        return (
            f"The public npm registry ({PUBLIC_NPM_REGISTRY}) refused the request "
            f"(npm {code}). A proxy or network policy on this machine may block it. "
            "Sign in to your own npm registry again, then install again."
        )
    where = (
        f"your configured npm registry ({registry})" if registry else "your configured npm registry"
    )
    return (
        f"npm could not sign in to {where} (npm {code}). The login there has probably "
        "expired. Sign in to that registry again and install again, or install once "
        f"from the public npm registry ({PUBLIC_NPM_REGISTRY}). Your npm settings stay "
        "as they are: only that one install uses the public registry."
    )
