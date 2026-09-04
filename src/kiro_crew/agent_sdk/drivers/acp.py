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

Imports that pull runtime machinery (``kiro_crew.acp`` and
``kiro_crew.sandbox``) are FUNCTION-LOCAL throughout. The ACP package pulls in
the client and runtime, while the sandbox package pulls in platform composition;
this module is reached from a dashboard handler on the boot path, so importing
either at module scope would charge gateway startup for a subsystem used only by
an explicit action. Call-time lookup also lets tests patch the spawn resolver and
sandbox posture at their defining modules.
"""

from __future__ import annotations

__all__ = [
    "claude_adapter_cached_negative",
    "claude_adapter_install_command",
    "claude_components_resolve",
    "derived_agent_permissions",
    "kiro_cli_resolves",
    "run_kiro_native_commands",
]


def derived_agent_permissions(allowed_tools: object, agent_filename: str) -> dict:
    """The KAS policy a generated agent spec should carry, from its grant list.

    Deriving from the FILTERED ``allowedTools`` instead of restating a literal
    means the rules come out byte-identical, a later edit to the grant list
    carries through, and a ceiling that strips a grant strips its KAS rule with
    it. ``{"rules": []}`` when nothing qualifies -- the key's mere PRESENCE is
    what makes KAS load the spec at all, so the empty policy is still a policy.

    Plain data in, plain data out: a list of grant refs and a spec filename
    (the KAS ``agent_id`` is its stem), a JSON-ready dict back -- no ACP type
    crosses the boundary. ``allowed_tools`` is typed ``object`` because the
    wrapped derive owns the validation and fails closed on any non-list
    (including the absent-key ``None`` a caller reads off a config dict) --
    narrowing it here would just force casts at call sites for a check the
    derive already makes. Function-local import for the same boot-path reason
    as every other function here, though ``kas_permissions`` itself is a leaf
    that depends on nothing else in the package.
    """
    from pathlib import Path

    from kiro_crew.acp.kas_permissions import allowed_tools_to_permissions

    derived = allowed_tools_to_permissions(allowed_tools, agent_id=Path(agent_filename).stem)
    return derived if derived is not None else {"rules": []}


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


def codex_adapter_resolves() -> bool:
    """Whether the codex-acp adapter resolves to a runnable argv.

    ONE component, unlike claude's two: codex-acp ships a compatible Codex binary
    as an npm dependency and reads ``CODEX_PATH`` itself only to run a DIFFERENT
    one, so there is no second executable Crew hands it and no half-install to
    distinguish.
    """
    from kiro_crew.acp.client import _resolve_codex_acp_bin

    adapter_argv, _searched_path = _resolve_codex_acp_bin()
    return bool(adapter_argv)


def codex_adapter_cached_negative() -> bool:
    """Has the RUNNING gateway already resolved the codex adapter as absent?

    Same hazard and same resolution as :func:`claude_adapter_cached_negative`: the
    argv is resolved once per process behind an ``_UNRESOLVED`` sentinel and never
    invalidated, so a fresh probe reporting "installed" after an install would
    disagree with every spawn until a restart. Consulted, never invalidated -- a
    dashboard GET must not mutate a global on the spawn path.
    """
    from kiro_crew.acp import client as _client

    cached = getattr(_client, "_codex_acp_argv_cache", None)
    if cached is None or cached is getattr(_client, "_UNRESOLVED", object()):
        return False
    try:
        argv, _searched = cached  # type: ignore[misc]
    except Exception:
        return False
    return not argv


def codex_adapter_install_command() -> str:
    """``npm i -g <adapter package>``, with the package name read from the repo.

    A global install of the SCOPED package puts the UNSCOPED ``codex-acp`` binary
    on PATH, which is what the resolution ladder looks for -- so this command and
    that ladder agree by construction rather than by coincidence.
    """
    from kiro_crew.acp.client import CODEX_ACP_NPM_PKG

    return f"npm i -g {CODEX_ACP_NPM_PKG}"


def claude_adapter_install_command() -> str:
    """``npm i -g <adapter package>`` -- the adapter's remedy, from the repo.

    The package name is imported rather than restated so it cannot drift from
    the constant the resolver's own docstring points at.
    """
    from kiro_crew.acp.client import CLAUDE_ACP_NPM_PKG

    return f"npm i -g {CLAUDE_ACP_NPM_PKG}"


def _native_command_client_factory():
    """Resolve the direct ACP client at call time so gateway boot stays lazy."""
    from kiro_crew.acp.client import AcpClient

    return AcpClient


async def run_kiro_native_commands(
    commands: tuple[str, ...],
    *,
    work_dir: object,
    agent: str,
    session_key: str,
    timeout_seconds: float,
) -> tuple[str, list[dict]]:
    """Run a structured native-command batch and return only plain data.

    One timeout covers readiness and every command. No prompt is sent. ACP
    exceptions are translated here so no backend type crosses the SDK boundary.
    """
    import asyncio
    import contextlib

    from kiro_crew.acp.client import AcpAuthRequired, AcpError, AcpTimeoutError
    from kiro_crew.sandbox import configured_sandbox_mode

    sandbox_mode = await asyncio.to_thread(configured_sandbox_mode)
    client = _native_command_client_factory()(
        work_dir=work_dir,
        agent=agent,
        sandbox_mode=sandbox_mode,
        session_key=session_key,
    )

    async def _run() -> list[dict]:
        await client.ensure_ready()
        results: list[dict] = []
        for command in commands:
            results.append(await client.command_result(command))
        return results

    try:
        return "ok", await asyncio.wait_for(_run(), timeout=timeout_seconds)
    except AcpAuthRequired:
        return "kiro_auth_required", []
    except (asyncio.TimeoutError, AcpTimeoutError):
        return "connection_test_timeout", []
    except AcpError:
        return "agent_unreachable", []
    except Exception:
        return "connection_test_failed", []
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.shutdown(), timeout=10.0)
