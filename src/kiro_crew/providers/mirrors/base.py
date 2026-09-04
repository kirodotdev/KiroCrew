"""The agent-config mirror contract: one declared projection per backend.

One agent spec (``~/.kiro/agents/<name>.json``) is the single source of truth for
every backend Kiro Crew drives, and no two backends read it the same way. kiro-cli
is handed ``--agent`` and reads the file itself; KAS takes client agents over the
wire as ``_meta.kiro.customAgents``; claude-agent-acp takes MCP servers as a
``session/new`` parameter and other settings from a file it loads on its own.

Projecting the spec onto those shapes is a thing every backend author has to do,
and until this module nothing said so. The cost was paid twice: KAS shipped with
its ``mcpServers`` block omitted, leaving a session holding
``tools: ["@kirocrew-core", ...]`` with nothing defining ``kirocrew-core`` — refs
naming nothing and every Crew tool silently absent — and claude-agent-acp arrived
later with the identical defect, diagnosed a second time by a second
investigation that did not know the first had happened.

What this module adds is not translation logic (that stays with each backend, in
its own file beside this one) but a **declaration that a reviewer can read and a
test can assert**: for every concern the spec carries, each backend states which
of four things happens to it. ``no-channel`` and ``withheld`` are the two the
previous code could not tell apart, and conflating them is the documented cause
of the ``hooks`` regression — see ``UNSUPPORTED_SPEC_KEYS`` in
:mod:`kiro_crew.acp.kas_agents`, whose comment states the rule this vocabulary
generalises: *no slot on the wire is not no such capability in the backend*.

Design: ``docs/request-for-change/rfc-agent-config-mirror.md``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class Concern(Enum):
    """A thing the agent spec expresses that a backend may or may not receive.

    Closed on purpose. A mirror must rule on every member, so adding one here
    obliges every backend to answer it — which is the mechanism that stops a new
    setting being silently delivered to one backend and dropped by the rest.
    """

    MCP_SERVERS = "mcpServers"
    TOOL_ALLOWLIST = "tools"
    DENIED_TOOLS = "disabledTools"
    AUTO_APPROVE = "autoApprove"
    MODEL = "model"
    MODEL_ALLOWLIST = "availableModels"
    PERMISSION_MODE = "permissions.defaultMode"
    PROMPT = "prompt"
    RESOURCES = "resources"
    HOOKS = "hooks"


class Disposition(Enum):
    """What happens to one concern on one backend."""

    #: Reaches the backend in the spec's own shape.
    DELIVERED = "delivered"
    #: Reaches it under another name or in another vocabulary.
    TRANSLATED = "translated"
    #: The backend HAS this capability; this transport cannot carry it. A backlog
    #: item with a known destination, NOT a decision — ``channel`` names where it
    #: would have to go.
    NO_CHANNEL = "no-channel"
    #: Deliberately not sent. A decision, with its reason.
    WITHHELD = "withheld"


@dataclass(frozen=True)
class Ruling:
    """A backend's answer for one concern.

    ``reason`` is required for every disposition, including ``delivered`` — the
    channel is the interesting part even when nothing is lost, and a mirror whose
    rulings are unexplained is the state this module exists to replace.
    """

    disposition: Disposition
    reason: str
    #: Required for ``NO_CHANNEL``: the delivery path that would carry it. Empty
    #: for every other disposition.
    channel: str = ""

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("a Ruling needs a reason")
        if self.disposition is Disposition.NO_CHANNEL and not self.channel.strip():
            raise ValueError(
                "a no-channel Ruling must name the channel that would carry it — "
                "an unaddressed gap is what this vocabulary exists to prevent"
            )
        if self.disposition is not Disposition.NO_CHANNEL and self.channel:
            raise ValueError("channel is only meaningful for a no-channel Ruling")


class AgentConfigMirror(ABC):
    """Projects the agent spec onto ONE backend's native configuration.

    Two faces, because the channel genuinely differs by transport rather than by
    vendor: params contributed to ``session/new`` / ``session/load``, and files
    the harness loads by itself. A backend may use either, both, or neither —
    claude-agent-acp is the existence proof that both is ordinary.

    Both faces default to a no-op, so a backend that needs only one implements
    only one. What a backend may NOT skip is :meth:`rulings`: the default is
    abstract precisely so a new backend cannot inherit silence.
    """

    #: The ``acp_backend`` id this mirror serves. Must be in ``ACP_BACKENDS_KNOWN``.
    backend: str = ""

    @abstractmethod
    def rulings(self) -> Mapping[Concern, Ruling]:
        """This backend's answer for every :class:`Concern`.

        Abstract, and checked for completeness by the parity test, so an
        unaddressed concern fails a build rather than shipping a session that is
        quietly missing something.
        """

    def session_params(self, agent: str | None, **kwargs: Any) -> dict[str, Any]:
        """Wire face: params to merge into ``session/new`` / ``session/load``.

        **Tools may only be delivered where Crew still governs their use.** A
        backend that gates tool calls natively (kiro-cli) satisfies that by
        construction. One that gates them through a file Crew may or may not own
        does NOT: delivering a tool whose calls cannot reach Crew's gate hands the
        session a capability nothing can withhold. Callers therefore pass
        ``permission_surface_owned``, and a mirror in the second class MUST fail
        closed on it rather than deliver anyway. Only Claude is in that class
        today; every other mirror may ignore the flag.

        Callers also pass ``work_dir``, the session's project checkout. A mirror
        cannot discover it, and a backend whose agent specs can live in the project
        needs it to resolve the same spec set the native harness would — resolving
        a narrower set silently drops whatever restrictions the missed spec carried.

        **The SHARED call site must stay a synchronous in-memory read** — it is
        shared with kiro-cli, and adapter work there would put a new scheduling
        and failure point on every backend's construction path, kiro-cli included
        (harness-parity H13). This method itself is allowed to block: Claude's
        implementation reads the agent spec. The obligation is therefore on the
        WIRING, not on the method — resolve it off the loop on this backend's own
        spawn path, cache the result, and serve the shared call site from that
        cache. ``AcpClient._resolve_session_mcp_servers`` /
        ``_session_mcp_servers`` are that pair.
        """
        return {}

    def write_files(self, agent: str | None, **kwargs: Any) -> None:
        """File face: write the native config files this backend loads itself.

        **Create-or-decline.** Crew creates a file or leaves the path entirely
        alone; it never reads, merges into, rewrites or deletes a file it did not
        author. This is not a style rule — it is what removes the whole class of
        defects that a merge-into-the-user's-file mirror carries (following a
        symlink on a path a checked-out repository controls, deleting a file the
        user created, doing blocking filesystem work on a teardown path). A mirror
        that needs to preserve a user's file wants a Crew-owned directory, not a
        merge.

        Blocking. Callers run it off the event loop.
        """
        return None
