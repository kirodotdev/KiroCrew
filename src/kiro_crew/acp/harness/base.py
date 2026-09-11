"""The backend-neutral harness contract ``AcpRuntime`` talks to.

A :class:`HarnessAdapter` is the answer to "what does this host do differently".
Each implementation answers every question in one file, so onboarding a host is
writing that file. The runtime reads answers rather than testing identities.

That matters because the questions are unrelated to each other and scattered
across the runtime's 3700 lines. An answer per host per question, all in one
place, is what makes the set enumerable: a host is complete when its file has no
abstract member left, and the base class supplies no defaults precisely so that
"complete" and "correct on every seam" are the same condition.

Three rules keep the layer honest.

**It re-declares no membership.** Every capability question resolves through the
``ACP_BACKENDS_*`` frozensets in :mod:`kiro_crew.agent_sdk.backends`, which stay
the single owner of who-can-do-what. A harness that hardcoded its own answer
would be a second copy to drift.

**It holds no runtime.** Everything a harness reads arrives as an argument --
:class:`SpawnContext` for the spawn, explicit keywords for the per-session
projection. Nothing here can reach back into the runtime's state, so a harness
cannot grow a hidden coupling that the next backend then has to reproduce.

**It decides, it does not act.** The runtime keeps ownership of the socket, the
process and the session table; the harness only says what to put in them.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "HarnessAdapter",
    "NotificationAliases",
    "ReclaimPolicy",
    "SessionExtras",
    "SpawnContext",
    "SpawnPlan",
    "TeardownPolicy",
]


# ── Seam 1: spawn ──


@dataclass(frozen=True)
class SpawnContext:
    """Everything a harness may read while building its spawn argv.

    Passed in rather than reached for, so the argv a harness produces is a
    function of its arguments and can be asserted without a runtime.
    """

    agent: str
    """The Kiro Crew agent this process runs. Empty when the host has no notion."""

    work_dir: str | Path | None
    """The cwd the spawn resolves relative paths against.

    Carried as the caller holds it. The pre-spawn gates accept either shape,
    and coercing here would silently change what they are handed."""

    model: str | None
    """A model to pin at process start, when the host accepts one on the CLI."""

    environ: Mapping[str, str]
    """One snapshot of the environment. Shared by the binary search and the
    "not found (searched ...)" message, so the message can never name a
    directory the search did not walk."""

    home: Path
    """One snapshot of the home directory, for the same reason."""

    sandbox_mode: str = "auto"
    """The sandbox tier this spawn will use, as configured.

    Load-bearing for a host whose privileged tools this core ENFORCES, and inert
    for every other. Two tiers hand back an UNWRAPPED child and drop
    :attr:`SpawnPlan.extra_hidden_dirs` on the floor -- the ``off`` tier, and a
    host with no sandbox backend where unsandboxed exec is opted in. An enforced
    host that spawned there would run a third-party binary with the operator's
    credential homes readable and nothing compensating for it, so it must REFUSE
    instead, and it cannot tell without knowing the tier.

    Hand it to ``acp_tool_gate.enforce_sandbox_floor(backend, mode)`` rather than
    reasoning about tiers here. That call IS the refusal: it returns for a harness
    this core does not enforce, it asks whether the mask will be APPLIED instead of
    which tier was named -- the distinction a no-backend host with the opt-in set
    gets wrong -- and it raises with an operator-facing remedy otherwise. It is
    also what ``AcpClient``'s own spawn preflight calls, so the two drivers refuse
    on the same grounds instead of each carrying a copy of them.
    """


@dataclass(frozen=True)
class SpawnPlan:
    """The argv to spawn, plus what the spawn decided about itself."""

    argv: list[str]

    host_auth: bool = False
    """Crew answers this process's credential callbacks.

    Decided per spawn (a dashboard sign-in or sign-out takes effect on the next
    process) and carried here rather than written onto the runtime, so the
    reader loop answers a callback only on a process started expecting it to.
    """

    extra_hidden_dirs: tuple[str, ...] = ()
    """Credential homes the sandbox must deny this process.

    Empty for a host whose privileged tools already ask by construction. For a
    host this core's tool gate ENFORCES, this mask IS the compensating control:
    ACP cannot make such a host ask about a passive read, so the only thing
    standing between a third-party binary and the operator's credentials is the
    OS boundary. A harness that resolves an empty mask for an enforced host has
    not simplified anything -- it has removed the control.

    Carried on the plan rather than resolved by the runtime because resolving it
    is the same blocking filesystem work as the argv search, and one thread hop
    should pay for both.
    """

    extra_expose_files: tuple[str, ...] = ()
    """Individual files handed back read-only from inside a hidden directory.

    The other half of the mask: a host may legitimately need one file under a
    denied home. Every entry must sit inside a directory :attr:`extra_hidden_dirs`
    hides, or it re-exposes something nothing denied.
    """


# ── Seam 3: session/new and session/load extras ──


@dataclass(frozen=True)
class SessionExtras:
    """Per-session payload a host needs beyond ``cwd`` and ``mcpServers``.

    ``custom_agents`` is the wire-registered agent definition list for a host
    with no ``--agent`` spawn flag; ``None`` means the host took its agent at
    spawn time and must not be handed one again.
    """

    custom_agents: list[dict[str, Any]] | None = None


# ── Seam 5: notification aliases ──


@dataclass(frozen=True)
class NotificationAliases:
    """Which inbound method names a host uses for the same three events.

    Hosts that fork the ACP vocabulary send their own spelling alongside (or
    instead of) the standard one. The runtime's demux accepts the union, so an
    alias a harness forgets is a frame silently counted as ``other`` and dropped.
    """

    session_update: tuple[str, ...] = ()
    """Every method name that carries a session update."""

    subagent_list_update: str = ""
    """The method announcing the child-session roster. Empty when the host has
    no subagents to announce."""

    mcp_init: tuple[str, ...] = ()
    """Methods that arrive while a session is initializing and must be staged
    until the session exists, rather than dropped as ownerless."""


# ── Seam 6: teardown ──


@dataclass(frozen=True)
class TeardownPolicy:
    """How a session ends on this host.

    A dataclass rather than a bare string because ending a session is not the same
    act on every host and the difference is not expressible as a verb. kiro-cli
    EVICTS: the record stays on disk and a later resume finds it. KAS DELETES: the
    record is gone, so every local transcript-retention choice is a no-op there and
    a resume degrades to "conversation gone". A caller that offers to keep a
    transcript is offering something one of those hosts cannot honour, and this is
    the type that gives that fact somewhere to live once a caller needs to ask.
    """

    method: str
    """The JSON-RPC method that ends one session."""


# ── Seam 9: reclaim ──


@dataclass(frozen=True)
class ReclaimPolicy:
    """When a warm process is recycled.

    Both numbers are already per-instance on the runtime, so a host with a
    different memory profile needs no branch -- only different values.
    """

    max_age_secs: float
    max_rss_mb: float


# ── The contract ──


class HarnessAdapter(abc.ABC):
    """One host's answers to the nine questions ``AcpRuntime`` has to ask.

    Implement every member. A default that "usually works" is how a new backend
    silently inherits kiro-cli's behaviour on the one seam it actually differs
    on, which is the failure this layer exists to prevent -- so the base class
    supplies no defaults for the per-host facts.
    """

    #: The ``ACP_BACKEND_*`` id this harness serves. Every membership question
    #: below resolves through it, so it is the only identity a harness carries.
    backend: str

    # ── Seam 1: spawn ──

    @abc.abstractmethod
    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Build the pre-sandbox argv, running any pre-spawn side effects.

        Side effects belong here rather than in the runtime because they are the
        part that differs: kiro-cli needs its agent file on disk before a
        ``--agent`` spawn can see the mode, and a host with no agent flag needs
        nothing. Raise ``AcpRuntimeError`` to abort the spawn.
        """

    @abc.abstractmethod
    def apply_spawn_env(self, env: dict[str, str]) -> None:
        """Mutate the child's environment in place for this host.

        Called after the generic environment is assembled and before it is
        scrubbed, so a host can both add its own variables and remove one the
        generic path would otherwise pass through.
        """

    @property
    @abc.abstractmethod
    def internal_sandbox(self) -> bool:
        """The host runs its own sandbox, so Crew delegates isolation to it."""

    @property
    @abc.abstractmethod
    def pod_home_remap(self) -> bool:
        """The child's ``HOME`` is remapped when spawned inside a pod bundle."""

    @property
    @abc.abstractmethod
    def verifies_agent_activation(self) -> bool:
        """After session start, confirm the requested agent is the active mode.

        Only meaningful on a host that selects the agent at spawn: elsewhere the
        activation is an explicit ``set_mode`` whose response already answers it.
        """

    # ── Seam 2: initialize ──

    @property
    @abc.abstractmethod
    def protocol_version(self) -> Any:
        """The ``protocolVersion`` this host accepts.

        Deliberately untyped: hosts disagree on the TYPE, not just the value --
        one takes a date string, another an integer, and sending the wrong shape
        is rejected outright rather than negotiated.
        """

    @property
    @abc.abstractmethod
    def client_capabilities(self) -> dict[str, Any]:
        """The ``clientCapabilities`` object sent at handshake."""

    # ── Seam 3: session/new and session/load extras ──

    @abc.abstractmethod
    async def session_extras(
        self,
        agent: str,
        *,
        work_dir: str | Path | None,
        mcp_gateway_overlay: Any = None,
        member_dispatch: bool = False,
    ) -> SessionExtras:
        """Per-session payload for this host, for both session start paths.

        ``mcp_gateway_overlay`` is handed in because only the caller holds it and
        the projection has to subtract the servers that will ALSO arrive as
        session-level entries -- declaring one twice lets the weaker declaration
        shadow the keyed one. Empty extras is the normal answer for a host that
        took its agent at spawn time.
        """

    @abc.abstractmethod
    def session_mcp_servers(
        self,
        requested: list[dict[str, Any]],
        *,
        agent_capabilities: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """The ``mcpServers`` array to send, given what the caller asked for.

        A TRANSFORM, not an addition, which is the whole reason this is a method:
        a host that reads no agent spec has nothing else describing its tool
        surface, and it may refuse the entire ``session/new`` over a single
        element whose transport it never advertised. So the harness has to be able
        to narrow the caller's list against ``agent_capabilities`` -- the object
        ``initialize`` returned -- and returning a subset is a correct answer.

        ``agent_capabilities`` is empty before the handshake and on a host that
        advertises none. Treat empty as "nothing is known", never as "nothing is
        supported": narrowing to nothing on an unknown host would silently strip
        every tool from every session.

        A host that takes its tools from an agent spec returns ``requested``
        unchanged, which is what keeps its wire byte-identical.
        """

    # ── Seam 4: inbound requests the host answers ──

    @property
    @abc.abstractmethod
    def host_answered_methods(self) -> tuple[str, ...]:
        """Methods this process may send that Crew answers itself.

        Anything not listed here is answered ``-32601``, so a method missing
        from this tuple is a callback the host waits on forever.
        """

    @abc.abstractmethod
    async def answer_request(self, method: str) -> dict[str, Any]:
        """Build the result for one of :attr:`host_answered_methods`.

        Raise ``HostAuthCallbackError`` (or any exception the runtime maps to a
        JSON-RPC error) rather than returning a partial result: a host left
        hanging on a callback is worse than one told its credential expired.
        """

    # ── Seam 5: notification aliases ──

    @property
    @abc.abstractmethod
    def notification_aliases(self) -> NotificationAliases:
        """This host's spellings for the three aliasable inbound events."""

    # ── Seam 6: teardown ──

    @property
    @abc.abstractmethod
    def teardown(self) -> TeardownPolicy:
        """How one session ends, and whether ending it destroys the record."""

    # How a live session's model changes, and how privileged tools are made to
    # ask, are deliberately NOT asked here. Both answers already live in a
    # membership table -- ``ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION`` and
    # ``ACP_BACKEND_ROUTING`` -- and both drivers read those tables directly. A
    # harness member mirroring them would be a second declaration free to
    # disagree with the one that decides, which is the drift this layer exists to
    # remove rather than to add. A host that needs to be ASKED rather than looked
    # up is what would justify a seam here.

    # ── Seam 9: reclaim ──

    @abc.abstractmethod
    def reclaim_policy(self, *, max_age_secs: float, max_rss_mb: float) -> ReclaimPolicy:
        """This host's recycle thresholds, given the runtime's configured ones.

        The runtime's values are passed in so an operator's configuration still
        wins; a harness narrows them for a host that is known to leak faster,
        and otherwise passes them straight through.
        """
