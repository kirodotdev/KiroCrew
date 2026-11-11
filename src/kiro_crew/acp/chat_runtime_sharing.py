"""Which chat sessions may share one agent process, and which one.

Subagents already multiplex sessions onto their parent's process. This module
extends the same demux to top-level dashboard chat slots, which otherwise spawn
one process each. It is switched by the ``chat_runtime_sharing`` feature preview
(``dashboard.feature_previews``), off by default: with it off every start keeps
its own process, exactly as before.

The lease table that holds the runtimes is
:class:`kiro_crew.runtime_ownership.RuntimeOwnership` -- one registry for every
runtime in the gateway, consulted by the kill gate. This module supplies the two
things that registry deliberately does not decide, plus the cap:

``eligible_for_chat_sharing``
    Whether a session may share at all. A cron, hook or task-runner session keeps
    its own process, and so does an incognito or temporary session -- see the
    function's own reasoning. A crew-member session also keeps its own process:
    member memory and workspace are isolated per member, so a member session is
    refused with the ``member_session`` reason rather than sharing one runtime
    across members.

:class:`ChatRuntimeKey`
    WHICH process, as the registry's compatibility key. Two sessions may land on
    one process only when every PROCESS-LEVEL spawn input matches. A per-session
    input (the ACP ``cwd``, the session key, the crew identity) is deliberately
    absent from the key, because ``create_session`` carries it per session. An
    input whose scope is ambiguous is IN the key: a key that is too narrow costs
    memory, while one that is too wide runs a session under another session's
    process configuration.

``chat_runtime_cap``
    How many sessions one process may serve, which is the blast radius of that
    process dying. Forced to 1 whenever sharing is off, so the switch cannot be
    defeated by a stored cap.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKENDS_CHAT_RUNTIME_SHARING
from kiro_crew.messaging.link import telemetry_channel_of
from kiro_crew.runtime_ownership import CHAT_RUNTIME_CAP

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.agent_sdk.tool_search import ToolSearchSettings

logger = logging.getLogger(__name__)

#: The feature preview (``dashboard.feature_previews``) that turns chat runtime
#: sharing on. Off when absent, unreadable or anything but ``True``.
CHAT_RUNTIME_SHARING_PREVIEW: Final[str] = "chat_runtime_sharing"

#: Oldest kiro-cli whose KAS relay may host several chat sessions on one process.
#: kiro-cli 3.1.0 ships ``@kiro/agent`` 0.66.91, the first engine that admits one
#: turn per session itself: a second prompt to a session with a running turn is
#: queued behind it and steered into it (``steering_queued`` then
#: ``steering_injected``) instead of racing it (``promptAdmitted`` ->
#: ``runAdmittedPrompt``'s ``steerBehindOlderVisiblePrompt`` and
#: ``interruptRunningTurn``; absent from 0.66.70 in kiro-cli 3.0.0). Across
#: sessions the engine keeps its own order: a turn of a Crew custom-agent session
#: waits in the engine's one-slot model queue (``AgentController.progressModelQueue``)
#: while another session's turn runs, and every ``session/update`` names its
#: session. That is why a KAS shared process needs no Crew-side chat turn gate
#: (``AcpRuntime.chat_turn_gate``): the engine already gives each frame one owner.
#: Measured: on a raw kiro-cli 3.1.0 relay process, two and four sessions prompted
#: at once each got only their own text, in order, with every update naming its
#: session; through Crew on a pod, three shared chats each got their own 30 lines
#: in order and their turns ran one after another (the gate did not hold them).
KAS_CHAT_SHARING_MIN_KIRO_CLI: Final[tuple[int, int, int]] = (3, 1, 0)


def chat_runtime_sharing_enabled(cfg: Any) -> bool:
    """Whether the ``chat_runtime_sharing`` feature preview is on in *cfg*.

    Read through ``edition_locks.feature_previews`` like every server reader of a
    preview switch: an LTS build reads every preview as off, so a stored ``true``
    carried over from another build never shares a process there.
    """
    from kiro_crew.edition_locks import feature_previews

    previews = feature_previews(getattr(getattr(cfg, "dashboard", None), "feature_previews", None))
    return isinstance(previews, Mapping) and previews.get(CHAT_RUNTIME_SHARING_PREVIEW) is True


def kas_chat_sharing_ready() -> bool:
    """Whether the next KAS spawn runs an engine that may host several chats.

    True only on the kiro-cli relay route at or above
    :data:`KAS_CHAT_SHARING_MIN_KIRO_CLI`. Crew's own engine bundle
    (``agent.kas_bundle``, or the automatic switch when the relay cannot run) is
    pinned to an older engine and keeps one process per chat. An unreadable
    version or route answers False: absence of evidence keeps the session on its
    own process. Blocking (reads config, may run ``kiro-cli --version`` once);
    call it off the event loop.
    """
    try:
        from kiro_crew.acp.kas_route import resolve_kas_route
        from kiro_crew.kiro_cli import installed_kiro_cli_version

        if resolve_kas_route().bundle:
            return False
        version = installed_kiro_cli_version()
    except Exception:  # noqa: BLE001 - a failed probe keeps the unshared path
        logger.debug("chat runtime sharing: KAS engine readiness unreadable", exc_info=True)
        return False
    return version is not None and version >= KAS_CHAT_SHARING_MIN_KIRO_CLI


#: Memory modes that may share a process. A shared process OUTLIVES any single
#: session on it, because the other sessions hold the reference -- so an
#: incognito or temporary session's teardown cannot take the process (and the
#: scratch files it wrote) with it, which is exactly what that mode promises.
#: ``create_session`` also latches ``recording_allowed`` off for the WHOLE
#: runtime when it starts a non-persistent session, so one such session joining
#: would stop recording for every other session on the process.
_SHAREABLE_MEMORY_MODES = frozenset({"persistent"})


def chat_runtime_cap(*, sharing_enabled: bool, configured: int) -> int:
    """How many sessions one chat runtime may serve.

    ``CHAT_RUNTIME_CAP`` -- 1 -- whenever sharing is off, whatever the stored
    value says. The switch is the thing an operator turns, so a cap left behind
    from an earlier experiment must not be able to pool sessions after it is
    turned off, and a cap of 1 makes every acquisition found its own entry: one
    process per session, which is the unshared behaviour.

    A configured value below 1 is also read as 1 rather than refused. The loader
    already clamps it, so this is the second line of the same defence, and the
    recoverable direction for a nonsensical cap is the behaviour that shipped
    before sharing existed.
    """
    if not sharing_enabled:
        return CHAT_RUNTIME_CAP
    return max(CHAT_RUNTIME_CAP, int(configured))


def chat_sharing_ineligible_reason(
    *,
    session_key: str | None,
    memory_mode: str,
    sharing_enabled: bool,
    backend: str,
    member_context: bool,
    remote_workspace: bool = False,
    kas_engine_ready: bool = False,
) -> str:
    """WHY this session may not share a chat runtime, or ``""`` when it may.

    ``remote_workspace`` is True for a start placed in a session environment (a
    remote workspace or a Kiro Cloud sandbox): that placement is attached to the
    PROCESS, so a second session on it would run in the first one's environment.
    ``kas_engine_ready`` is :func:`kas_chat_sharing_ready`, read by the caller off
    the loop; it is asked only for a KAS start.

    The decision itself lives here, and :func:`eligible_for_chat_sharing` is the
    boolean over it, so there is exactly one place the branches are written. The
    reason is returned rather than only logged because a refusal is invisible
    otherwise: an operator who turned sharing on and still sees one process per
    session has no way to tell WHICH condition refused, and the branch name is
    the whole answer.

    Each value names the condition, never a session property worth hiding: a
    branch name carries no key, no path and no environment value.
    """
    if not sharing_enabled:
        return "sharing_disabled"
    if backend not in ACP_BACKENDS_CHAT_RUNTIME_SHARING:
        return "backend_not_chat_shareable"
    if backend == ACP_BACKEND_KAS and not kas_engine_ready:
        return "kas_engine_not_shareable"
    if remote_workspace:
        return "session_environment"
    if memory_mode not in _SHAREABLE_MEMORY_MODES:
        return "memory_mode_not_shareable"
    if telemetry_channel_of(session_key) != "dashboard":
        return "origin_not_dashboard"
    if member_context:
        return "member_session"
    return ""


def eligible_for_chat_sharing(
    *,
    session_key: str | None,
    memory_mode: str,
    sharing_enabled: bool,
    backend: str,
    member_context: bool,
    remote_workspace: bool = False,
    kas_engine_ready: bool = False,
) -> bool:
    """Whether this session may join (or found) a shared chat runtime.

    Scoped to dashboard chat slots. ``telemetry_channel_of`` is the repository's
    single classification of a session key's origin, and it answers
    ``"dashboard"`` only for a chat slot -- a cron, hook, task-runner, subagent
    or channel-bound key classifies as something else, so each keeps its own
    process without this function naming any of them.

    A chat slot is necessary but NOT sufficient. ``backend`` must also be a host
    that may share a runtime under a top-level CHAT slot, asked as membership in
    ``ACP_BACKENDS_CHAT_RUNTIME_SHARING`` -- a set decided SEPARATELY from the
    subagent ``ACP_BACKENDS_SESSION_SHARING`` on purpose (harness-parity H6):
    serving several subagent sessions on one process does not establish that a
    top-level chat session may, because the chat teardown is different and it is
    the chat teardown this membership asserts is safe. A host outside it is served
    by ``AcpRuntime`` yet its ``destroy()`` may not leave THIS session's resume
    record intact: the shared teardown destroys this session's handle to leave a
    process it may not kill, so on such a host every ordinary chat close would
    discard its own resume record. Membership is asked rather than a specific host
    being excluded, so a host added later is out until someone puts it in
    deliberately -- and it names the CHAT set, so a backend that shares subagent
    sessions is not silently granted chat sharing on top.

    This cannot be settled by the compatibility key: two sessions on the SAME
    unsupported backend have equal keys, so they would share with each other,
    which is the broken case rather than a safe one.

    A crew-member session is refused: it keeps its own process. A member session
    carries member-specific memory and workspace isolation, so sharing one
    runtime across member sessions is an isolation boundary this change does not
    cross -- the safe default wins. Member sharing can return in a follow-up once
    a maintainer approves it.
    """
    return not chat_sharing_ineligible_reason(
        session_key=session_key,
        memory_mode=memory_mode,
        member_context=member_context,
        sharing_enabled=sharing_enabled,
        backend=backend,
        remote_workspace=remote_workspace,
        kas_engine_ready=kas_engine_ready,
    )


def _freeze_env(extra_env: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    """A hashable, order-independent form of the child's extra environment."""
    if not extra_env:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in extra_env.items()))


def _freeze_path(value: str | Path | None) -> str:
    """A comparable spelling of a path-shaped spawn input (``""`` when unset).

    Both spellings of one directory have to land on the same key, and a bare
    ``str()`` does not deliver that: on Windows a ``Path`` stringifies with
    backslashes while a caller's plain string keeps the separators it was
    written with, so the same work directory would key two processes and a
    session would spawn instead of sharing. Coercing through ``Path`` first
    gives one spelling per platform, and it also folds a trailing separator,
    which is the same directory by every other measure.

    Deliberately NOT ``resolve()``: this runs on the placement path for every
    session start, and resolving touches the filesystem and can raise. The key
    compares what the caller asked to spawn with, which is what the pre-spawn
    gates judge too.
    """
    if not value:
        return ""
    return str(Path(value))


def _freeze_tool_search(settings: "ToolSearchSettings | None") -> tuple[object, ...]:
    """The Tool Search choice as sent at ``initialize``, or ``()`` when unset.

    Sent once per process in ``clientCapabilities``, so a session cannot carry
    its own -- a session joining a process configured differently would run with
    a setting it did not ask for.
    """
    if settings is None:
        return ()
    return (
        bool(settings.enabled),
        int(settings.min_pct),
        int(settings.min_tokens),
    )


@dataclass(frozen=True)
class ChatRuntimeKey:
    """Every process-level spawn input of one ``AcpRuntime``.

    One line per field on why it cannot vary between two sessions on a process:

    ``work_dir``
        The child's own cwd, and the directory four pre-spawn gates judge:
        derived-spec freshness, fork governance, the sealed-target refusal and
        the voice-workspace assertion. ``kiro-cli`` also resolves ``--agent``
        against ``<work_dir>/.kiro/agents`` before the global directory, so the
        spec a session runs follows the process, not the session.
    ``agent``
        Carried on argv as ``--agent``, and rewritten in place by the native
        skill projection.
    ``model``
        NOT about which model a session runs: a host that pins one at process
        start takes it on argv, but the chat path applies a model per session
        after ``session/new``, so two sessions on one process each run their own.
        It is in the key because the effort overlay's row is NAMED by the model.
        That file is one per work directory and ``kiro-cli`` reads it once at
        startup, so two slots agreeing on the effort LEVEL still disagree on
        effort DELIVERY when their models differ: the process loaded a table
        holding only the founder's row, and the joiner runs at its own model's
        default. Keying effort alone does not close that.
    ``sandbox_mode``
        Chooses the wrap, the credential mask and whether the host's internal
        sandbox is delegated to.
    ``extra_env``
        The child's environment, which one process has exactly one of.
    ``acp_backend``
        Selects the harness, and through it the binary and the handshake.
    ``tool_search``
        Sent once in the ``initialize`` handshake.
    ``memory_mode``
        Latches the runtime's recording permission at ``create_session``.

        The same mirror: ``_SHAREABLE_MEMORY_MODES`` admits one mode, so this
        field also carries a single value on every shared key, and for the same
        reason it stays.
    ``shared_scratch``
        The session tree's work directory, mounted into the process.
    ``mcp_gateway_overlay`` / ``mcp_gateway_socket``
        Held on the runtime and read when composing each session's MCP array.
        Scope is ambiguous, so both are in the key.
    ``reasoning_effort``
        The level the spawn writes into the work directory's ``cli.json``
        overlay, which ``kiro-cli`` reads once at startup. The overlay is one
        file per work directory keyed by model, so two sessions asking for
        different levels would have the last writer decide for the whole
        process and a joining session would silently run at the founder's
        level.
    ``spawn_identity``
        The ACCOUNT era this start read, and the one field that is not a spawn
        parameter at all.

        A process authenticates once, at spawn, and holds that credential for
        its whole life. Every other field describes what a session asks to
        spawn WITH, and those are identical before and after a ``kiro login``
        to a different account -- so without this field two eras of one
        account's inputs compare equal and a session starting after the switch
        joins a process still holding the old credential and runs its turns
        there. Carrying the era makes the two eras two keys, so the join
        cannot happen and the later session founds its own process; the
        sessions already on the old one are left to their own retirement.

        Empty on an entry point that wires no identity reader -- the CLI, the
        tests -- where it is inert rather than unsafe: an empty value is equal
        to an empty value, which is the placement those paths had before this
        field existed, and none of them is the dashboard chat path this
        governs.
    ``spec_generation``
        The GENERATION of the agent spec the spawn hands ``kiro-cli`` on argv.

        That spec is read once, at startup, and it carries the tool surface and
        the auto-approvals the process runs under for its whole life. The
        per-session MCP array is recomposed and freshness-gated for each
        session, so the session-scoped half is covered -- but the spawn-time
        ``--agent`` load is not, and a joining session skips the projection
        rebuild precisely because the founder's is live. So an edit that
        revokes a grant leaves the process running the pre-revocation surface,
        and a session joining afterwards inherits it.

        Keyed rather than re-checked on join, for the same reason the era is:
        the revocation cannot be undone inside a running process, so the only
        answer that holds is not to land there. Two generations are two keys.

    ``forward_ssh_auth_sock``
        Whether this process was spawned with the operator's ``SSH_AUTH_SOCK``
        forwarded into the sandbox -- an authorization granting USE of the
        operator's ssh-agent keys for the process's whole life, decided once at
        spawn from the keystone consent. A joining session inheriting a founder's
        forwarding (or being denied its own) is a credential-scope mismatch the
        process cannot shed, so the frozen decision is keyed like the era: two
        forwarding states are two keys.
    ``launch_agent``
        The agent file the spawn copies its launch spec from (a Bot's own
        definition rather than its template's), read once at startup.
    ``harness_pin``
        The harness a session environment or member pinned, which selects the
        binary and its handshake (``SpawnContext.harness_pin``).
    ``agent_env``
        The operator's ``agent.env`` variables as the start read them
        (``acp.agent_env.agent_env_overlay``). The child takes them at spawn, so
        its shell commands and the MCP servers it starts keep those values for
        its whole life; two values are two keys.
    ``kas_telemetry``
        Whether the kiro-cli telemetry opt-out leaves telemetry on, judged for a
        KAS process (``kas_cli_settings.kiro_cli_telemetry_enabled``). KAS takes the
        opt-out once, in ``initialize``, so a chat whose folder opts out must not
        join a process started with telemetry on. Always True for other backends.
    """

    work_dir: str
    agent: str
    model: str
    sandbox_mode: str
    extra_env: tuple[tuple[str, str], ...]
    acp_backend: str
    tool_search: tuple[object, ...]
    memory_mode: str
    shared_scratch: str
    mcp_gateway_overlay: str
    mcp_gateway_socket: str
    reasoning_effort: str
    spawn_identity: str
    spec_generation: str
    forward_ssh_auth_sock: bool
    launch_agent: str
    harness_pin: str
    agent_env: tuple[tuple[str, str], ...]
    kas_telemetry: bool

    @classmethod
    def build(
        cls,
        *,
        work_dir: str | Path | None,
        agent: str,
        sandbox_mode: str,
        extra_env: dict[str, str] | None,
        acp_backend: str,
        tool_search: "ToolSearchSettings | None",
        memory_mode: str,
        shared_scratch: Path | None,
        mcp_gateway_overlay: str | Path | None,
        mcp_gateway_socket: str | Path | None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        spawn_identity: str | None = None,
        spec_generation: str | None = None,
        forward_ssh_auth_sock: bool = False,
        launch_agent: str | None = None,
        harness_pin: str | None = None,
        agent_env: dict[str, str] | None = None,
        kas_telemetry: bool = True,
    ) -> "ChatRuntimeKey":
        """Build the key from the values a caller is about to spawn with.

        Keyword-only and exhaustive on purpose: a field added to the runtime's
        constructor and forgotten here would widen the key silently, which is
        the failure this shape makes impossible to do by accident.

        Exhaustive is not the same as self-enforcing, so the shape is locked from
        outside as well: a structural test walks the process-level spawn inputs
        AND the workspace ``cli.json`` writers against these fields, and fails on
        any name that is neither keyed nor listed with a reason. Two channels
        because an input can reach the process as a parameter or as a file.
        """
        return cls(
            work_dir=_freeze_path(work_dir),
            agent=agent or "",
            model=model or "",
            sandbox_mode=sandbox_mode or "",
            extra_env=_freeze_env(extra_env),
            acp_backend=acp_backend or "",
            tool_search=_freeze_tool_search(tool_search),
            memory_mode=memory_mode or "",
            shared_scratch=_freeze_path(shared_scratch),
            mcp_gateway_overlay=_freeze_path(mcp_gateway_overlay),
            mcp_gateway_socket=_freeze_path(mcp_gateway_socket),
            reasoning_effort=reasoning_effort or "",
            spawn_identity=spawn_identity or "",
            spec_generation=spec_generation or "",
            forward_ssh_auth_sock=bool(forward_ssh_auth_sock),
            launch_agent=launch_agent or "",
            harness_pin=harness_pin or "",
            agent_env=_freeze_env(agent_env),
            kas_telemetry=bool(kas_telemetry),
        )


#: Largest agent spec this will hash. A spec is a few kilobytes of JSON or
#: markdown; a file past this bound is not one, and hashing an arbitrary number of
#: bytes on the placement path of every eligible start is not a cost this may take.
#: Exceeding it FRAGMENTS rather than hashing a prefix: a prefix hash would answer
#: "same" to a rewrite past the cap, which is the exact evasion the hash exists to
#: catch.
_SPEC_READ_CAP_BYTES = 1 << 20


def _observe_spec_bytes(path: Path) -> str | None:
    """One spec file's content token, or None when it cannot be observed.

    Read through ``pinned_fs.open_fenced_for_read``, the repository's own gate,
    rather than ``Path.read_bytes``. An agents directory is agent-writable, so a
    by-name read is a check-to-open window: the name can be re-pointed at a
    credential file between the resolution and the read. That reader refuses a
    link at the final component, refuses a non-regular or hardlinked file, reads
    the kernel's own path back off the descriptor and asks the sensitive-path
    fence about it. Hashing a spec must not become a way to hash ``~/.aws``.

    One DESCRIPTOR serves the read and the security check: the size check, the
    read and the sensitive-path identity all come from the same open inode, so a
    link cannot be swapped in between opening and reading. Two brackets guard
    against a write landing DURING the observation. The fd is fstat'd before and
    after the read, which catches an IN-PLACE rewrite (its mtime moves on the same
    inode). A by-name re-stat afterwards catches a write delivered by RENAME,
    which the fd bracket cannot: an atomic replace swaps a new inode under the
    name while the still-open fd keeps reading the old one, so only the NAME's
    identity reveals it. A mismatch on either fragments rather than hashing bytes
    the spawn will not load.

    The token carries the byte length beside the digest. The digest alone would
    do; the length makes a log line or a failing assertion readable without
    hashing anything to compare against.
    """
    from kiro_crew.pinned_fs import open_fenced_for_read
    from kiro_crew.security import is_sensitive_canonical_path

    try:
        fd = open_fenced_for_read(path, fence=is_sensitive_canonical_path)
    except (OSError, ValueError):
        return None
    try:
        before = os.fstat(fd)
        if before.st_size > _SPEC_READ_CAP_BYTES:
            return None
        data = os.read(fd, _SPEC_READ_CAP_BYTES)
        after = os.fstat(fd)
        if (before.st_mtime_ns, before.st_size, before.st_ino, before.st_dev) != (
            after.st_mtime_ns,
            after.st_size,
            after.st_ino,
            after.st_dev,
        ):
            return None
        if len(data) != after.st_size:
            # A short read is not the file: hashing what arrived would answer
            # "same" for two files sharing a prefix.
            return None
        # The fd bracket above catches an IN-PLACE rewrite (its mtime moves on the
        # same inode), but it is BLIND to a write delivered by RENAME: a
        # ``write_text`` / atomic replace unlinks the observed inode and swaps a
        # NEW one in under the name, so the still-open fd keeps reading the old,
        # now-orphaned inode -- ``before`` and ``after`` on it stay identical while
        # the name the spawn will actually load has changed underneath. Re-stat by
        # NAME and compare against the inode this observation was pinned to; a
        # rename moves the name's ``(ino, dev)`` (and its mtime), so the mismatch
        # fragments rather than hashing a generation the child will not load.
        try:
            by_name = os.stat(path)
        except OSError:
            return None
        if (by_name.st_ino, by_name.st_dev, by_name.st_mtime_ns, by_name.st_size) != (
            before.st_ino,
            before.st_dev,
            before.st_mtime_ns,
            before.st_size,
        ):
            return None
    except OSError:
        return None
    finally:
        os.close(fd)
    return f"{len(data)}-{hashlib.sha256(data).hexdigest()}"


def agent_spec_generation(work_dir: str | Path | None, agent: str) -> str:
    """A token that CHANGES whenever the spec the spawn will load changes.

    ``kiro-cli`` resolves ``--agent`` against ``<work_dir>/.kiro/agents`` before
    the user-level directory, and reads what it finds once at startup. Both
    scopes are therefore observed, in that order, and both go into the token:
    which one wins is the child's decision, not this function's, and an edit to
    either is a different process configuration.

    The observation is the file's CONTENT, hashed, and not its stat metadata. A
    stat triple of mtime, size and inode is cheaper and is what this repository's
    freshness gates compare, but they compare it to detect a change they will then
    react to, while this one has to survive a change made deliberately to look
    like none: a revocation rewritten to the same byte length with its mtime
    restored keeps every field of that triple, and the key would stay equal while
    the granted surface differed. Hashing is the only answer that cannot be
    dressed up, and a spec is a small file read once per eligible start.

    Bracketed stat-read-stat, the same rule the derived-spec gate uses: the file
    is stat'd, read, and stat'd again, and a mismatch means a write landed inside
    the read -- so the bytes may be half of one spec and half of another. That is
    not an observation, and it fragments rather than being reported.

    Blocking I/O: call it off the event loop.

    An absent spec contributes ``-``, which is a REAL answer rather than a
    failure: both sides agree the file is not there, and the child falls back the
    same way for both. A name that is not a registered agent is NOT such an
    answer -- it is a refusal that resolves to no path in any scope -- so it
    fragments to a unique token instead of folding to ``-``. Every other failure
    -- unstattable, unreadable, unstable across the bracket, or larger than the
    read cap -- contributes a unique token too, so a start that cannot prove the
    generation founds its own process rather than joining on an unproven match.
    Absence of evidence must not admit a join.
    """
    parts: list[str] = []
    candidates: list[Path | None] = []
    try:
        from kiro_crew.agent import (
            agent_spec_path,
            is_registered_agent_name,
            kiro_agents_dir_path,
        )
        from kiro_crew.config.paths import project_agents_dir

        # A name that is not a registered agent -- malformed, path-shaped, or
        # simply unknown -- is a REFUSAL, not an absence: agent_spec_path returns
        # None for it in every scope, which would otherwise fold to the same "-"
        # a genuinely-absent spec contributes, so a start whose agent name cannot
        # be resolved would share a runtime with one whose spec is really gone.
        # Fragment instead, so such a start founds its own process.
        if not is_registered_agent_name(agent):
            return f"unresolved-{uuid.uuid4().hex}"

        # Both resolved through the repository's own helpers rather than spelled
        # out here: the workspace scope is the directory kiro-cli itself searches
        # first (no upward walk), and the user scope honours KIRO_HOME, so a
        # literal would reintroduce the split brain those resolvers exist to close.
        scopes: list[Path] = []
        if work_dir:
            scopes.append(project_agents_dir(work_dir))
        scopes.append(kiro_agents_dir_path())
        for scope in scopes:
            try:
                candidates.append(agent_spec_path(agent, agents_dir=scope))
            except Exception:
                # An ambiguous or malformed resolution is not a generation this
                # function may guess at, so it fragments like an unstattable file.
                return f"unresolved-{uuid.uuid4().hex}"
    except Exception:
        return f"unresolved-{uuid.uuid4().hex}"
    for path in candidates:
        if path is None:
            parts.append("-")
            continue
        observed = _observe_spec_bytes(path)
        if observed is None:
            return f"unobservable-{uuid.uuid4().hex}"
        parts.append(observed)
    return "|".join(parts)


#: Tool entries that put kiro-cli's native subagent tool on an agent's surface.
_NATIVE_SUBAGENT_TOOLS = frozenset(
    {"*", "@builtin", "subagent", "use_subagent", "invoke_sub_agent"}
)


def spec_grants_native_subagents(spec: object) -> bool:
    """Whether an agent spec can start the harness's own (native) subagents.

    Such a subagent's frames name its own session, which the runtime can route
    to the chat only while exactly ONE session is registered
    (``runtime_frame_accounting``); beside a co-tenant its approval requests are
    auto-rejected. So a chat whose agent can start them keeps its own process.

    Fails closed: anything that is not a readable spec with a tools list answers
    True, because the default agent kiro-cli falls back to grants every built-in.
    """
    if not isinstance(spec, dict):
        return True
    excluded_raw = spec.get("excludedTools")
    excluded = (
        {t for t in excluded_raw if isinstance(t, str)} if isinstance(excluded_raw, list) else set()
    )
    if "*" in excluded or excluded & {"subagent", "use_subagent", "invoke_sub_agent"}:
        return False
    raw = spec.get("tools")
    if raw == "*":
        return True
    if not isinstance(raw, list):
        return True
    entries = {t for t in raw if isinstance(t, str)}
    if entries & _NATIVE_SUBAGENT_TOOLS:
        return True
    return any(t.startswith(("subagent/", "@builtin/subagent")) for t in entries)


def agent_spawns_native_subagents(work_dir: str | Path | None, agent: str) -> bool:
    """:func:`spec_grants_native_subagents` for the spec a spawn of *agent* loads.

    Resolved in kiro-cli's order (the work directory's agents, then the user's),
    with the projection's own reader. Blocking: call it off the event loop. Any
    failure to find or read the spec answers True (fail closed).

    shortcut: read once before placement, not re-confirmed after acquisition
    like the keyed inputs; a spec edited to grant subagents in that window can
    still join once. Upgrade by keying the answer if it proves to matter.
    """
    try:
        from kiro_crew.agent import is_registered_agent_name, kiro_agents_dir_path
        from kiro_crew.agent_sdk.agent_specs import agent_spec_absent, load_agent_spec
        from kiro_crew.config.paths import project_agents_dir

        if not is_registered_agent_name(agent):
            return True
        scopes: list[Path] = []
        if work_dir:
            scopes.append(project_agents_dir(work_dir))
        scopes.append(kiro_agents_dir_path())
        for scope in scopes:
            if scope.is_dir() and not agent_spec_absent(scope, agent):
                return spec_grants_native_subagents(load_agent_spec(scope, agent))
    except Exception:
        return True
    return True


__all__ = [
    "CHAT_RUNTIME_SHARING_PREVIEW",
    "KAS_CHAT_SHARING_MIN_KIRO_CLI",
    "ChatRuntimeKey",
    "agent_spawns_native_subagents",
    "agent_spec_generation",
    "chat_runtime_cap",
    "chat_runtime_sharing_enabled",
    "chat_sharing_ineligible_reason",
    "eligible_for_chat_sharing",
    "kas_chat_sharing_ready",
    "spec_grants_native_subagents",
]
