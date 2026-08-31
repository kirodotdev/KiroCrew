"""The ACP driver's half of the machine-local install question.

The layer above (:mod:`kiro_crew.agent_sdk.backend_install`) owns the *contract*
-- three states, which component a remedy names, how long a verdict is reused.
This module owns the one thing that contract cannot express without reaching
into the harness: whether a spawn of that harness would actually **resolve**
right now.

**It asks through the spawn's own resolvers, never a reimplementation.** A probe
that hand-rolled a PATH search would agree with the spawn only by coincidence:
the resolvers here consult an env override, a project-local ``node_modules``,
mise, and an augmented PATH that includes shims a bare ``shutil.which`` cannot
see. A second search would tell the operator they are ready and then fail the
session, which is a worse outcome than saying nothing.

Every function returns plain data -- a bool, a string, a tuple of bools -- so no
ACP type crosses the boundary. Two consequences are deliberate rather than
incidental:

* **A resolver that raises is left to raise.** The failed-CHECK verdict belongs
  to the caller's three-state contract, and swallowing the exception here would
  hand it a ``False`` indistinguishable from an honest "absent" -- which is
  exactly the collapse of ``unknown`` into ``missing`` that contract forbids.
* **The paths themselves are dropped.** Nothing above needs the resolved
  location, and a returned path is a filesystem detail the SDK would then be
  tempted to interpret.

Imports of ``kiro_crew.acp`` are FUNCTION-LOCAL throughout.
``kiro_crew/acp/__init__.py`` pulls in the ACP client and the runtime, and this
module is reached from a dashboard handler on the boot path, so a module-scope
import would drag both into gateway start. It also keeps the lookup at CALL
time, in the resolvers' own defining module -- which is what lets a test patch
the spawn's resolver and have the probe actually see it.
"""

from __future__ import annotations

__all__ = [
    "claude_adapter_cached_negative",
    "claude_adapter_install_command",
    "claude_components_resolve",
    "kiro_cli_resolves",
]


def kiro_cli_resolves() -> bool:
    """Does kiro-cli resolve, through the resolver ``_resolve_spawn_argv`` calls?

    ``_resolve_kiro_bin`` also enforces the executable-trust snapshot, so a
    binary that is present but fails that check raises rather than answering a
    path. That is a failed CHECK, not an absent install, so the exception is
    propagated for the caller to classify.
    """
    from kiro_crew.acp.client import _resolve_kiro_bin

    return bool(_resolve_kiro_bin())


def claude_components_resolve() -> tuple[bool, bool]:
    """``(adapter, claude_cli)`` -- the Claude backend's two halves, separately.

    ``_resolve_claude_acp_bin`` finds the ACP adapter Crew spawns;
    ``_resolve_claude_code_executable`` finds the Claude CLI handed to it as
    ``CLAUDE_CODE_EXECUTABLE``. The adapter's own SDK does not search PATH for
    that second binary, so having one without the other is a real, distinguishable
    half-install with a different remedy -- which is why this returns two answers
    rather than one conjunction.
    """
    from kiro_crew.acp.client import (
        _resolve_claude_acp_bin,
        _resolve_claude_code_executable,
    )

    adapter_argv, _searched_path = _resolve_claude_acp_bin()
    return bool(adapter_argv), bool(_resolve_claude_code_executable())


def claude_adapter_cached_negative() -> bool:
    """Has the RUNNING gateway already resolved the adapter as absent?

    ``AcpClient`` resolves the adapter once per process and keeps the answer for
    the process's whole life (``_claude_acp_argv_cache``, set behind an
    ``_UNRESOLVED`` sentinel and never invalidated). A fresh resolve can therefore
    disagree with what a spawn will actually do, and the dangerous direction is
    exactly the one an operator walks into: a failed Claude session caches
    ``None``, they install the adapter the panel told them to install, and a fresh
    probe would report ``installed`` while every subsequent spawn still reuses the
    cached ``None`` and dies with ``AcpError``.

    So the cache is consulted, not bypassed. Reading it rather than INVALIDATING
    it is deliberate: invalidation would make a dashboard GET mutate a global on
    the spawn path, and the honest disclosure ("installed, restart to use it")
    costs the operator one restart while never promising something that then
    fails.

    Unresolved (no session has needed the adapter yet) is not a negative -- the
    next spawn will resolve fresh, so the fresh answer is the true one.
    """
    from kiro_crew.acp import client as _client

    cached = getattr(_client, "_claude_acp_argv_cache", None)
    if cached is None or cached is getattr(_client, "_UNRESOLVED", object()):
        return False
    try:
        argv, _searched = cached  # type: ignore[misc]
    except Exception:
        return False
    return not argv


def claude_adapter_install_command() -> str:
    """``npm i -g <adapter package>`` -- the adapter's remedy, from the repo.

    The package name is imported rather than restated so it cannot drift from
    the constant the resolver's own docstring points at.
    """
    from kiro_crew.acp.client import CLAUDE_ACP_NPM_PKG

    return f"npm i -g {CLAUDE_ACP_NPM_PKG}"
