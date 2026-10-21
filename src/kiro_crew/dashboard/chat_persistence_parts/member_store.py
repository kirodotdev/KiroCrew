"""The private member-store assignment.

Pinning a restored or prewarmed session to its member's private memory store,
releasing a prewarmed session, the store-ownership check and the member identity
a restore resolves for a slot. Composed onto
:mod:`kiro_crew.dashboard.chat_persistence`; see this package's ``__init__``. New
member-store rules go here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_persistence import (
        _SKIP_MEMBER_RESTORE,
        DashboardState,
        KiroCrewConfig,
        logger,
        named_store_or_empty,
        redact_log_via_context,
    )


def _pin_private_agent_assignment(
    session_key: str,
    agent: str,
    config: KiroCrewConfig,
    *,
    conversation_log=None,
    native_context: bool = False,
    authorized_store: str | None = None,
    memory_mode: str = "persistent",
    validate_only: bool = False,
) -> str:
    """Pin an authorized member selection, never a name recovered from history.

    Callers must authorize the owner's request or its session-control creation
    before using this helper. Restricted sessions cannot create persistent child
    sessions through aggregate controls. Legacy members keep declared V1 memory.

    ``authorized_store`` is passed through to :func:`_member_private_selection`,
    which owns both the store classification and that fence.
    """
    selected, store = _member_private_selection(agent, config, authorized_store=authorized_store)
    if not store:
        return ""
    from kiro_crew.execution_context import (  # noqa: F811
        bind_session_execution,
        read_session_execution,
        resolve_member_execution,
    )
    from kiro_crew.history import ConversationLog
    from kiro_crew.memory_stores import UnknownMemoryStore  # noqa: F811

    execution = resolve_member_execution(
        config, selected, memory_mode=memory_mode, validate_memory_files=False
    )
    log = conversation_log if conversation_log is not None else ConversationLog()
    # ``has_messages``, not ``has_log``: the transcript file already exists once
    # the slot's metadata (title, agent, model) was flushed, and an agent pick
    # on an empty chat must not read as "this chat has V1 history". It fails
    # closed: a transcript that exists but cannot be read is not provably
    # empty, so the member cannot change.
    previous = read_session_execution(session_key)
    if (
        previous is not None
        and previous.member_id is not None
        and previous.member_id != execution.member_id
    ):
        raise UnknownMemoryStore(
            "This conversation belongs to another member. Open a new conversation."
        )
    if previous is None or previous.member_id is None:
        try:
            has_history = native_context or log.has_messages(session_key)
        except OSError as exc:
            raise UnknownMemoryStore(
                "This conversation's transcript is unreadable, so its history cannot be "
                "verified; its member selection was not changed."
            ) from exc
        if has_history:
            raise UnknownMemoryStore(
                "This conversation retains its existing history. Open a new conversation for member memory."
            )
    if previous is not None:
        if previous.member_id == execution.member_id and previous.store == execution.store:
            execution = previous.with_mode(memory_mode)
        else:
            execution = execution.with_mode(previous.memory_mode)
    if validate_only:
        return store
    # Establishing, so it vouches. Every caller of this helper has already
    # authorized the owner's own request (see the docstring), and the store being
    # published is the one CONFIG resolves for the selected member: `execution`
    # comes from `resolve_member_execution`, and `previous` is reused above only
    # when its member AND store equal that config-resolved pair. So the vouched
    # store is never one the session's own record chose. Without the vouch, a
    # dashboard tab the owner bound to a member is published but unvouched, and
    # its own-store `session_create` is refused as `memory_delegation_denied`
    # ("this process holds no vouched identity for the caller") for its whole life.
    bind_session_execution(
        session_key,
        execution,
        replace_existing=previous is not None,
        expected=previous,
        vouch=True,
    )
    return store


def _member_private_selection(
    agent: str, config: KiroCrewConfig, *, authorized_store: str | None = None
) -> tuple[str, str]:
    """Resolve an explicit member choice without opening its learned database.

    The selection and prewarm-release paths share this classification. An
    optional captured store limits the choice to the caller's admitted routing.
    """
    # An empty name is the default TEMPLATE, which owns no member memory; it
    # is never read as the default crewmate alias.
    selected = agent
    if not selected or selected == "default":
        return "", ""
    store = getattr(config.agents.get(selected), "memory_store", "")
    if not isinstance(store, str) or not store:
        return "", ""
    if authorized_store is not None and named_store_or_empty(store) != named_store_or_empty(
        authorized_store
    ):
        return "", ""
    record = config.memory_stores.get(store)
    if record is None or record.memory_version != 2:
        return "", ""
    return selected, store


async def release_prewarmed_session(
    state: DashboardState, session_key: str, agent: str, config: KiroCrewConfig
) -> bool:
    """Drop a speculative pre-warm's resume pointer before member context capture.

    ``session.eager_spawn`` is on by default, so opening a new chat pre-creates
    a session for it while the chat is still on the default agent. That
    allocation publishes the ACP session id into ``SessionMap``, and the agent
    switch's own reset preserves the persistence entry
    (``SessionManager.reset`` clears the SID only when a live session is still
    registered). Read as a live or resumable runtime, that surviving pointer
    stands for V1 context the transcript does not show -- but on a chat the
    user has never sent a message in there is no such context, and the member choice
    the owner asked for is refused for a runtime nobody is using.

    The pointer is discarded rather than ignored, so the invariant the guard
    protects is satisfied in fact: nothing can resume the default agent's
    pre-warmed process into the member context, and the first member
    turn cold-starts under the store it validated. Returns whether a pointer
    was dropped.

    Every condition fails CLOSED, because a wrong "yes" here throws away a
    conversation the user can still resume:

    * a pick that is not a private V2 member keeps its pre-warm — a V1 chat
      has nothing to grant and must not lose its resumable session;
    * a live provider means the caller has not torn its session down, so this
      is not the settled post-reset window this helper is written for;
    * any transcript row, or a transcript that cannot be read at all, means
      the chat is not provably empty. That is the same probe, and the same
      unreadable-is-not-empty rule, that :func:`_pin_private_agent_assignment`
      applies before it grants.

    Ordered cheapest-first, and the no-pointer case returns before the
    transcript read: a chat with nothing to drop must not pay a file read or a
    map write on every private pick.
    """
    if not _member_private_selection(agent, config)[1]:
        return False
    sessions = getattr(state, "sessions", None)
    log = state.conversation_log
    if sessions is None or log is None:
        return False
    if not sessions.resumable_sid(session_key):
        return False
    if sessions.get_provider(session_key) is not None:
        return False
    try:
        if await asyncio.to_thread(log.has_messages, session_key):
            return False
    except OSError:
        return False
    return bool(await asyncio.to_thread(sessions.forget_conversation, session_key))


def member_store_ownership_holds(config: KiroCrewConfig, member: str, entry_store: str) -> bool:
    """Whether *member* still privately owns *entry_store* according to *config*.

    The one spelling of that question, shared by the reopen path in
    ``handlers/members_thread.py`` and the grant below, so the check that returns a
    thread and the check that publishes authority over it cannot drift apart.

    :func:`require_member_memory_store` answers identity -- the member exists,
    its store resolves, nothing else owns that store, and the store's own
    recorded identity matches the member's immutable id -- and comparing its
    answer with *entry_store* is the continuity check: a member repointed while
    the request waited owns something other than what the caller decided about. It is
    deliberately blind to the record's ``owner_member`` NAME, which is what
    :func:`session_control._store_is_member_owned` authorizes admission on, so
    that field is compared here too: a reopen tolerating a stale name would
    serve a thread the admission gate refuses.

    Blocking: the identity check reads the store's database, so call it in a
    thread.
    """
    from kiro_crew.memory_stores import (  # noqa: F811
        UnknownMemoryStore,
        require_member_memory_store,
    )

    try:
        if require_member_memory_store(config, member) != entry_store:
            return False
    except UnknownMemoryStore:
        return False
    record = config.memory_stores.get(entry_store)
    return record is not None and record.owner_member == member


async def pin_private_agent_store(
    state: DashboardState,
    session_key: str,
    agent: str,
    config: KiroCrewConfig,
    *,
    memory_mode: str = "persistent",
    validate_only: bool = False,
) -> str:
    """Pin one dashboard selection from current private ownership off the loop.

    ``native_context`` is whether the session already has a live or resumable
    provider: such a session carries V1 context no transcript row shows yet.
    Callers snapshot the slot before awaiting and re-compare afterwards.

    Every selection is resolved and published from config read under the store
    namespace lock, and that lock is held through immutable binding publication,
    so member deletion, recreation, and store retirement cannot land between
    validation and publication. The request-entry member/store pair is kept only
    to compare against: a selection whose privateness or store changed while the
    request waited is refused rather than published, in either direction. A
    grant downgraded to non-private would write member content to the shared
    store, and one upgraded to private would publish authority the caller never
    asked for. This wrapper deliberately does not take the async config lock:
    callers may already hold a slot lock, while config writers serialize private
    ownership changes through the namespace lock.
    """
    selected = agent
    _entry_selected, entry_store = _member_private_selection(selected, config)
    native_context = state.sessions.get_provider(session_key) is not None or bool(
        state.sessions.resumable_sid(session_key)
    )

    from kiro_crew.memory_stores import (  # noqa: F811
        UnknownMemoryStore,
        memory_store_namespace_lock,
    )

    changed = (
        f"Crew Member {selected!r} changed during private memory assignment; "
        "no private memory was granted"
    )

    @memory_store_namespace_lock()
    def pin_current_assignment() -> str:
        current = KiroCrewConfig.load()
        # The classification the selection path already uses, asked of current
        # config: an empty store means the selection is not private, so one
        # comparison covers both a changed store and a changed privateness.
        _current_selected, current_store = _member_private_selection(selected, current)
        if current_store != entry_store:
            raise UnknownMemoryStore(changed)
        if current_store and not member_store_ownership_holds(current, selected, current_store):
            raise UnknownMemoryStore(changed)
        return _pin_private_agent_assignment(
            session_key,
            selected,
            current,
            conversation_log=state.conversation_log,
            native_context=native_context,
            memory_mode=memory_mode,
            validate_only=validate_only,
        )

    return await asyncio.to_thread(pin_current_assignment)


def _member_restore_identity(slot_name: str) -> tuple[str, str] | None:
    """Resolve a member slot's restore identity from its binding.

    Returns ``None`` for ordinary keys (caller restores normally),
    ``(member, "member")`` when ``dm.json`` names a dispatchable crew, and
    :data:`_SKIP_MEMBER_RESTORE` when the key has no binding or its stored
    member name cannot be dispatched. The BINDING is the authority — transcript
    metadata lives in the same operator-editable JSONL it would otherwise re-pin
    from, so it is never consulted for a member key's agent or mode.
    """
    # Function-local ON PURPOSE: kiro_crew.members imports kiro_crew.artifacts
    # (slugify), and importing that at module scope closes the
    # artifacts -> ... -> webhooks -> validation -> artifacts cycle when this
    # module is imported outside the dashboard handler tree.
    from kiro_crew import members as members_mod

    prefix = members_mod.DM_SLOT_KEY_PREFIX
    if not slot_name.startswith(prefix):
        return None
    binding = members_mod.read_dm_binding_for_slot(slot_name)
    member = (binding or {}).get("member", "")
    if not members_mod.is_dispatchable_member_name(member):
        logger.warning(
            "restore: member slot %r has no dispatchable dm binding; leaving it "
            "unpublished until the Crew Member config or binding is repaired",
            redact_log_via_context(slot_name),
        )
        return _SKIP_MEMBER_RESTORE
    return member, members_mod.DM_SLOT_MODE
