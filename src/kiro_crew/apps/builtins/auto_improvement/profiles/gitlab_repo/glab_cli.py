"""The one place this app decides what a ``glab`` child process may see.

Every host-side ``glab`` spawn in the app (the draft-MR recipe, the MR watcher's reads)
takes its environment from :func:`glab_env`, so the cross-host token fence is a
property of the spawn, not of each call site. Kept free of heavy imports: the
clone validator (also loaded by the MCP server) reads :func:`glab_prefers_ssh` here.
"""

from __future__ import annotations

import os
import shutil
import subprocess

#: GitLab SaaS. ``www.gitlab.com`` is folded to ``gitlab.com``.
GITLAB_SAAS_HOSTS = frozenset({"gitlab.com", "www.gitlab.com"})

#: Ambient token variables with no host binding. They were issued by gitlab.com (or
#: the operator meant them for it), so they must never reach a self-managed host.
GITLAB_SAAS_TOKEN_ENV = ("GITLAB_TOKEN", "OAUTH_TOKEN", "GITLAB_ACCESS_TOKEN")

#: Variables that choose which host ``glab`` talks to. Dropped from the inherited
#: environment and replaced by one ``GITLAB_HOST`` the caller validated, so an ambient
#: value cannot redirect the call (and the token with it) to another instance.
_HOST_STEERING_ENV = ("GITLAB_HOST", "GL_HOST", "GITLAB_URI", "GITLAB_API_HOST")


def canonical_gitlab_host(host: str) -> str:
    """``host`` lower-cased, trailing dot dropped, ``www.gitlab.com`` folded."""
    value = (host or "").strip().lower().rstrip(".")
    return "gitlab.com" if value in GITLAB_SAAS_HOSTS else value


def glab_env(host: str) -> dict[str, str]:
    """The child environment for a ``glab`` call against ``host``.

    A copy of the current environment, HOST-PINNED: ``GITLAB_HOST`` is set to the
    validated ``host`` and every other host-steering variable is dropped. The gitlab.com
    token variables are kept only when ``host`` is gitlab.com; for any other host,
    including an empty one, they are removed so glab authenticates from its own
    per-host config instead of forwarding a SaaS credential to a host that never issued
    it. An empty ``host`` also leaves ``GITLAB_HOST`` unset (fail closed: no token, no
    chosen host).
    """
    pinned = canonical_gitlab_host(host)
    env = {k: v for k, v in os.environ.items() if k not in _HOST_STEERING_ENV}
    if pinned != "gitlab.com":
        for key in GITLAB_SAAS_TOKEN_ENV:
            env.pop(key, None)
    if pinned:
        env["GITLAB_HOST"] = pinned
    return env


def glab_prefers_ssh(host: str) -> bool:
    """True iff the ``glab`` CLI uses SSH for git operations against ``host``.

    A private clone or push needs whichever transport is actually authenticated, so
    this reads the tool that owns the setting: the host-scoped value first (the
    setting is commonly per-host, and the global default can point at a transport
    that cannot authenticate this host), then the global one. Defaults to https on
    any error; callers build the URL from validated host and path, so the transport
    choice cannot retarget anything.
    """
    if shutil.which("glab") is None:
        return False
    for args in (
        ["glab", "config", "get", "git_protocol", "--host", host],
        ["glab", "config", "get", "git_protocol"],
    ):
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return False
        value = (proc.stdout or "").strip().lower()
        if proc.returncode == 0 and value:
            return value == "ssh"
    return False
