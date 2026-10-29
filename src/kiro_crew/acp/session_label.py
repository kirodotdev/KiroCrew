"""An opt-in, per-session identity LABEL for an MCP server Kiro Crew does not own.

A server Kiro Crew authored (its managed servers, and the builtin app servers that
run its own code) learns which session launched it from the signed per-session
token on its element, or from ``KIROCREW_SESSION_KEY`` where the process names one
session. Both are withheld from every other server on purpose: the token is a
transport identity PROOF the gateway accepts, and ``KIROCREW_SESSION_KEY`` is a rung
of the strict resolver, so handing either to a command a manifest or a spec chose
would let that command act as the session.

Some servers need much less: the name of the chat they serve, so a call can be tied
to the chat's own turns. They can declare that, per server, with
:data:`SESSION_LABEL_MARKER` set to ``true`` on the ``mcpServers`` entry (an app
manifest's entry reaches the agent spec verbatim, so the app declares it there).
Such a server is handed :data:`SESSION_LABEL_ENV` and nothing else: no port, no
token, no internal secret. The value is the session key, which the dashboard
already shows in its URL, but under a name no Kiro Crew resolver reads, so it
grants nothing a resolver would honour. It is a label, not an attestation.

**Where it travels.** A plain environment value names a session only when the
process it is set on serves one session. On the shared kiro-cli runtime one process
hosts many sessions, and kiro-cli starts a session's MCP servers while serving that
session's ``session/new``, so the label rides the session's own ``mcpServers``
ELEMENT, which re-declares the opted-in server for that session only
(:func:`kiro_session_label_elements`). On KAS the injected agent is built per
session, so it rides the projected entry's ``env``
(``acp.kas_agents._project_mcp_servers``).

**Absent, never stale.** The label is baked when the server starts. A warm-pool
session is created before any session claims it, so it starts with no key; such a
server gets no label rather than a wrong one, and a ``rekey()`` does not add one
(a running process's environment cannot be changed). A pooled broker stub is
shared across sessions and is never labelled either.

**The launch is the declaration's.** The element carries exactly the ``command``,
``args`` and ``env`` kiro-cli would have launched natively, plus the label. A
declaration the element cannot reproduce faithfully stays native and unlabelled:
any restriction :func:`kiro_crew.acp.session_mcp.native_mount_withholding` names, an
``autoApprove`` grant (an element has no slot for it), and an ``env`` value holding
a reference (``${NAME}`` or ``secret://``) whose resolution the element would not
repeat.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from pathlib import Path
from typing import Any

from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

logger = logging.getLogger(__name__)

#: The per-server opt-in, on an ``mcpServers`` entry. In the ``x-`` extension
#: namespace for the reason ``mcp_provenance.MARKER_KEY`` is: kiro-cli's config
#: fields are camelCase and can never contain a hyphen, and it drops unknown keys
#: at deserialization rather than rejecting them. Only the literal ``true`` opts in.
SESSION_LABEL_MARKER = "x-kirocrew-session-label"

#: The one variable an opted-in server receives. Deliberately NOT
#: ``KIROCREW_SESSION_KEY``: that name is a rung of the strict resolver
#: (``mcp_caller.resolve_own_identity``), and a label must not be readable as one.
SESSION_LABEL_ENV = "KIROCREW_SESSION_LABEL"

#: Substrings of an ``env`` value that kiro-cli or the gateway resolve at launch.
#: An element would carry the literal text, so such an entry stays native.
_ENV_REFERENCE_MARKS = ("${", "secret://")


def declares_session_label(entry: Any) -> bool:
    """Whether *entry* opts into the session label. Only a boolean ``true`` does."""
    return isinstance(entry, dict) and entry.get(SESSION_LABEL_MARKER) is True


def label_env_pairs(pairs: Any, session_key: str) -> list[dict[str, str]]:
    """ACP ``env`` *pairs* with the label set last, replacing any same-named pair.

    Set last so a declared value cannot shadow it: the label names the session the
    element was built for, and nothing the declaration says changes that.
    """
    kept = [
        p
        for p in (pairs if isinstance(pairs, list) else [])
        if isinstance(p, dict) and p.get("name") != SESSION_LABEL_ENV
    ]
    return [*kept, {"name": SESSION_LABEL_ENV, "value": session_key}]


def _label_withheld(entry: dict[str, Any]) -> str | None:
    """Why *entry* cannot be re-declared faithfully as an element, or ``None``."""
    if entry.get("autoApprove"):
        return "it carries autoApprove, which a per-session element cannot express"
    env = entry.get("env")
    if isinstance(env, dict) and any(
        isinstance(value, str) and any(mark in value for mark in _ENV_REFERENCE_MARKS)
        for value in env.values()
    ):
        return "its env holds a reference resolved at launch, which an element would not resolve"
    return None


def kiro_session_label_elements(
    agent: str | None,
    *,
    work_dir: str | Path | None,
    session_key: str,
    existing_names: Collection[str] = (),
    spec_override: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Per-session ``mcpServers`` elements carrying the label, for the kiro-cli runtime.

    One element per spec entry that opts in (:func:`declares_session_label`), that the
    spec's ``tools`` grants, that no element in *existing_names* already carries (a
    broker stub or Crew's own identity element), and that is not one of Crew's own
    servers (those carry the token instead). Empty with no *session_key* (a warm-pool
    session: absent, never stale) and in registry mode, where the catalog decides.

    Blocking (reads the spec and, only when a server opts in, the MCP settings);
    callers run it off the loop. Never raises: a label is an addition, and an error
    here must leave the session as it would have been without it.
    """
    try:
        return _kiro_session_label_elements(
            agent,
            work_dir=work_dir,
            session_key=session_key,
            existing_names=existing_names,
            spec_override=spec_override,
        )
    except Exception:  # noqa: BLE001 -- a label must never cost a session start
        logger.warning(
            "session label: could not build label elements for agent %r; its servers "
            "start without one",
            agent,
            exc_info=True,
        )
        return []


def _kiro_session_label_elements(
    agent: str | None,
    *,
    work_dir: str | Path | None,
    session_key: str,
    existing_names: Collection[str],
    spec_override: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    # circular import: session_mcp imports agent, which reaches the acp package.
    from kiro_crew.acp import session_mcp

    if not agent or not session_key or session_mcp._registry_mode():
        return []
    spec = (
        spec_override if spec_override is not None else session_mcp._agent_spec_for(agent, work_dir)
    )
    servers = spec.get("mcpServers") if isinstance(spec, dict) else None
    if not isinstance(servers, dict):
        return []
    allow = session_mcp._tools_allowlist(spec)
    owned = {*KIROCREW_BIN_MCP_SERVERS, *session_mcp.IDENTITY_BOUND_SERVERS}
    existing = {str(name) for name in existing_names}
    candidates = [
        (str(name), entry)
        for name, entry in sorted(servers.items(), key=lambda item: str(item[0]))
        if declares_session_label(entry)
        and allow.grants(str(name))
        and str(name) not in existing
        and str(name) not in owned
    ]
    if not candidates:
        return []
    # Read after the candidates are known, so a spec that opts nothing in pays no
    # settings read.
    builtin_launches = session_mcp.builtin_app_identity_launches()
    try:
        settings = session_mcp.native_settings_sources(work_dir)
    except session_mcp.NativeSettingsUnreadable as exc:
        logger.warning(
            "session label: MCP settings unreadable (%s); %d opted-in server(s) of agent "
            "%r start without a session label",
            exc,
            len(candidates),
            agent,
        )
        return []
    out: list[dict[str, Any]] = []
    for name, entry in candidates:
        if name in builtin_launches:
            # Crew's own code: its identity element carries the token, and a second
            # element under the same name would shadow it with the spec's launch.
            continue
        declared = {k: v for k, v in entry.items() if k != SESSION_LABEL_MARKER}
        withheld = session_mcp.native_mount_withholding(name, declared, settings)
        reason = withheld.explain("its session label") if withheld is not None else None
        if reason is None:
            why = _label_withheld(declared)
            reason = f"{name} is not labelled: {why}" if why else None
        if reason is not None:
            logger.warning("session label: %s", reason)
            continue
        element = session_mcp.acp_server_element(
            name, {k: declared[k] for k in ("command", "args", "env", "type") if k in declared}
        )
        if element is None or element.get("type") != "stdio":
            continue
        element["env"] = label_env_pairs(element.get("env"), session_key)
        out.append(element)
    if out:
        logger.info(
            "session label: labelled %d MCP server(s) for agent %r: %s",
            len(out),
            agent,
            ", ".join(e["name"] for e in out),
        )
    return out
