"""Who is calling a ``kirocrew-dashboard`` tool, and what that caller may see or shape.

The strict checks the folder and tag tools run before they read the session list or write
the tree, the app scope that narrows both (``_caller_app_scope``), the caller's own
sidebar slot, the dispatch-time refusal of a ``channel:`` caller's board write, and
``_is_messaging_caller``, which tells a channel or messaging-thread caller apart.
``SERVER_NAME`` is defined here because those refusals are audited under it.
:mod:`kiro_crew.mcp_dashboard` binds every name defined here, so each one reads, imports
and patches as ``kiro_crew.mcp_dashboard.<name>``.
"""

from __future__ import annotations

from kiro_crew.constants import CHANNEL_SESSION_NAMESPACES
from kiro_crew.mcp_dashboard_references import _get_rows
from kiro_crew.mcp_tools.table import ToolContext
from kiro_crew.sel import sel
from kiro_crew.validation import SLACK_THREAD_TS_RE

SERVER_NAME = "kirocrew-dashboard"


def _refuse_channel_board_write(name: str, caller_key: str) -> str | None:
    """Refuse a board write from a ``channel:`` caller, at dispatch.

    The ``CHANNEL_AGENT_BLOCKED_TOOLS`` name match runs at the permission
    prompt, which an auto-approved call never reaches, so the containment has to
    hold here too (the same split ``chat_session_pin`` uses). A channel agent
    acts on thread text other people wrote, and the board is the person's own
    sidebar layout.
    """
    if not caller_key.startswith("channel:"):
        return None
    try:
        sel().log_tool_invocation(
            session_key=caller_key,
            source="mcp",
            tool_name=name,
            tool_kind=SERVER_NAME,
            outcome="rejected_blocked_tool",
        )
    except Exception:
        # Stdio-silent: stderr would corrupt the JSON-RPC stream. The refusal
        # below holds either way.
        pass
    return (
        f"Error: {name} is not available to channel agents — the board is the "
        "person's own sidebar layout, and a channel agent acts on thread text "
        "other people wrote."
    )


#: Caller-key prefixes whose bearer is DELEGATED work — it runs on behalf of
#: whatever created it, so "matches no chat slot" cannot be read as "has no app
#: to be confined to". A subagent runs under its spawner; a cron can be created
#: by an app (``CronSDK`` tags those jobs), so a cron key can carry an app's
#: reach without carrying its slot.
#:
#: This list is KNOWINGLY INCOMPLETE, and that is a deliberate position rather
#: than an oversight. It enumerates the delegated key forms that exist today; a
#: key form added later will read as unscoped until it is added here. The sound
#: shape is the inverse — grant authority only on POSITIVE confirmation that the
#: caller is the person, and refuse everything unplaceable — but that also takes
#: these tools from callers who legitimately have no slot and no app (Slack
#: threads, channel sessions, and the person's own crons), which is a behaviour
#: change with its own tradeoff. Until that inversion is taken, adding a prefix
#: here is the cheap half and the gap is documented rather than hidden.
_DELEGATED_CALLER_PREFIXES = ("subagent:", "cron:")


def _caller_app_scope(caller_key: str, rows: list[dict]) -> str | None:
    """App owning the calling session, "" when it owns none, ``None`` to refuse.

    Located by finding the CALLER's own row: a slot created by an app carries
    that app in ``app`` (App Kit §5.2), and a session the person started carries
    none — which is what keeps "organize my sessions" working for the user's own
    agent while confining an app's.

    Searched over the unfiltered rows on purpose: the caller's own session may
    itself be incognito, and it is still the caller.

    An unlocatable caller is unscoped only when it is the kind of session that
    never had an app to be confined to — a Slack thread or a channel session has
    no dashboard slot, and refusing those would deny the tools to callers no app
    could have been attached to.

    A **delegated** caller is the exception (see
    ``_DELEGATED_CALLER_PREFIXES``): absence proves nothing about who it runs
    for, so it is refused rather than granted the person's authority.

    A ``dashboard:`` caller is refused for a DIFFERENT reason, which is why it
    is not in that tuple: it is not delegated work, it NAMES a slot. So absence
    is never the "never had a slot to be confined to" case that makes a Slack
    thread unscoped — it means the named slot is not there, which happens when
    the tab was closed while this call was still in flight (the slot is popped
    synchronously, without draining in-flight MCP calls) or when the key is
    simply wrong. An app-owned session going through that race would otherwise
    hand its agent the authority the app itself does not have.
    """
    slot = caller_key.split(":", 1)[-1] if ":" in caller_key else caller_key
    for r in rows:
        if str(r.get("key") or "") == slot:
            return str(r.get("app") or "")
    # A slot bound to a channel or cron session runs its turns under
    # ``linked_session_key`` ("when set, _run_chat uses this as session key" --
    # ``DashboardState``), so the caller presents THAT key and the match above
    # cannot find it. The rows serialize the field, so an app-owned linked slot
    # would otherwise read as unscoped here even though its owner is on record.
    #
    # POSITIVE matches only: a channel- or cron-born row is created with no
    # ``app``, so returning on an ownerless match would answer "" (the person)
    # for a delegated caller that the refusal below would otherwise fail closed
    # on -- weakening this layer instead of extending it.
    for r in rows:
        if str(r.get("linked_session_key") or "") == caller_key:
            linked_app = str(r.get("app") or "")
            if linked_app:
                return linked_app
            break
    if caller_key.startswith(_DELEGATED_CALLER_PREFIXES):
        return None
    if caller_key.startswith("dashboard:"):
        return None
    return ""


def _own_chat_slot(caller_key: str, rows: list[dict]) -> tuple[dict, str | None]:
    """The caller's OWN sidebar slot row, or why it has none to file.

    Resolved from the VERIFIED caller key only, never from an argument: this is
    what lets ``chat_folder_file_self`` be granted to an unattended conductor
    where ``chat_folder_move_session`` is withheld — the one placement it can
    write is its own.

    Only a ``dashboard:<slot>`` key qualifies, and it must name a row that is
    present. That key IS the slot: the slot key never changes for the life of
    the tab, so the row this resolves and the row the PATCH lands on are the
    same object (the endpoint re-checks identity under its lock). A slot bound
    to a channel or a schedule presents its ``linked_session_key`` instead, and
    that binding is REBOUND on live slots with no running gate — so a key that
    matched one slot at this read could name a different conversation by the
    time the write arrives. Filing on that match would file someone else's
    session; those callers are refused rather than raced.

    Two more refusals, both for what the placement would mean. A private
    (incognito / temporary) session is kept out of the sidebar record by the
    person's choice, and a folder placement is durable sidebar metadata. A crew
    member's pinned DM thread (``mode == "member"``) is one thread that spans
    every goal the member ever runs and lives on the Crew page, not in the
    sidebar tree — so a conductor running AS a member does not file itself; its
    workers still go under ``<goal>/<agent>`` via ``session_create``.
    """
    if not caller_key.startswith("dashboard:"):
        return {}, (
            "Error: this session has no sidebar slot to file — only a dashboard "
            "chat session can be placed in a folder."
        )
    slot_key = caller_key.split(":", 1)[1]
    row: dict | None = None
    for r in rows:
        if str(r.get("key") or "") == slot_key:
            row = r
            break
    if row is None:
        return {}, (
            "Error: this session has no sidebar slot to file — only a dashboard "
            "chat session can be placed in a folder."
        )
    if str(row.get("mode") or "") == "member":
        return {}, (
            "Error: a crew member's pinned DM thread lives on the Crew page and is "
            "not filed in a sidebar folder — it spans every goal the member runs. "
            "Skip filing yourself; create your workers with "
            "session_create's `folder` set to `<goal>/<agent>` instead."
        )
    if str(row.get("memory_mode") or "persistent") != "persistent":
        return {}, (
            "Error: a private (incognito or temporary) session is kept out of the "
            "sidebar record and cannot be filed."
        )
    return row, None


def _visible_chat_slots(ctx: ToolContext) -> tuple[list[dict], str | None]:
    """The live sessions these tools may see, private and foreign ones removed.

    Two filters, at the ONE place the list enters this server — both the tree
    render and the session resolver read the result, so neither can expose a
    title or key it should not, and neither can file such a session.

    **Private.** A slot whose ``memory_mode`` is not ``persistent`` — incognito
    or temporary — is a session the user chose to keep out of the record. The
    endpoint returns it like any other. An absent field reads as persistent,
    matching the slot default.

    **Foreign.** The endpoint is not app-scoped, so an app agent holding this
    set would otherwise enumerate every session's title and key across every
    other app and the user's own. Identity is resolved STRICTLY (see
    ``chat_folder_move_session`` for why the lenient walk is unsafe) and the
    list is narrowed to the caller's own app; an unverifiable caller is refused
    rather than handed a list it cannot be scoped against.
    """
    rows, err = _get_rows(ctx.client, "/api/chat/slots")
    if err:
        return [], err
    live = [r for r in rows if str(r.get("memory_mode") or "persistent") == "persistent"]
    caller_key, strict_err = ctx.caller.require_strict_session_key(
        "cannot verify which session is calling, so the session list is "
        "withheld — these tools scope what they show to the caller"
    )
    if not caller_key:
        return [], strict_err
    scope = _caller_app_scope(caller_key, rows)
    if scope is None:
        return [], (
            "cannot establish what this caller is allowed to see, so the session "
            "list is withheld — a subagent or a scheduled job runs on behalf of "
            "whatever created it and cannot be granted more than that"
        )
    if not scope:
        return live, None
    return [r for r in live if str(r.get("app") or "") == scope], None


def _refuse_tree_shaping_if_unverifiable(
    ctx: ToolContext, verb: str
) -> tuple[str, str, str | None]:
    """``(verified_caller_key, caller_app, error)`` — how to WRITE, or why not.

    Folders now carry an owner (``chat_folders._folder_owner_app``), so an app
    HAS a folder of its own to write to and the endpoint bounds each write to
    it: an app may create at the top level or inside its own folders, and may
    rename, reparent or delete only what it owns. The person keeps full
    authority over everything, and an app keeps read of the whole tree plus
    filing its OWN sessions into any folder.

    So this layer does not decide the policy — that would be a second copy of a
    rule the endpoint enforces under the store lock, and only the endpoint can
    see the authoritative tree. What stays here is the one question the endpoint
    cannot answer: whether the caller can be placed at all. An unverifiable or
    delegated caller has no scope to bound a write to, and a write to shared
    structure is not the place to assume the caller is the human.

    The verified key is RETURNED rather than left for the write helpers to
    re-derive. A request that names no key carries the frame's attribution key
    (``mcp_core._resolve_session_key``), whose ``/proc`` ancestor walk can
    resolve to a different slot than the strict check above —
    so re-resolving would check one identity and write under another, and for an
    app-owned session the walk landing on an ancestor makes the write arrive at
    the endpoint looking like the unconfined person. Every caller of this gate
    must pass what it returns straight to the write.

    ``caller_app`` (``""`` for the person) comes back for the same reason: it is
    the identity the endpoint will judge each write against, and a caller that
    must issue SEVERAL writes to keep one call atomic can only check them all up
    front by holding it. Re-deriving it would mean a second ``/api/chat/slots``
    fetch, against a roster that may have changed.
    """
    caller_key, strict_err = ctx.caller.require_strict_session_key(
        f"Error: cannot verify which session is calling, so {verb} is "
        "refused — reshaping the shared folder tree requires a caller "
        "identity the gateway can vouch for."
    )
    if not caller_key:
        return "", "", strict_err
    rows, err = _get_rows(ctx.client, "/api/chat/slots")
    if err:
        return "", "", f"Error: {err}"
    scope = _caller_app_scope(caller_key, rows)
    if scope is None:
        return (
            "",
            "",
            (
                f"Error: cannot establish what this caller is allowed to change, so "
                f"{verb} is refused — a subagent or a scheduled job runs on behalf of "
                "whatever created it and cannot be granted more than that."
            ),
        )
    if scope and not caller_key.startswith("dashboard:"):
        # An app-owned LINKED session -- a channel- or cron-bound slot runs its
        # turns under ``linked_session_key`` -- is the one shape whose staleness
        # nothing downstream can notice. The endpoint re-derives the app from
        # this key and refuses a key that NAMES a slot which is gone, but
        # ``caller_names_a_missing_slot`` is ``dashboard:``-only BY DESIGN: for
        # any other shape absence is not evidence of a vanished slot, since a
        # Slack thread or a channel session legitimately never had one, and
        # refusing those would take these tools from callers no app could have
        # been attached to.
        #
        # So for a linked key the endpoint cannot tell "this app's slot just
        # closed" from "no app owns this caller", and would read the second and
        # apply the person's authority. THIS layer can tell, because it resolved
        # the scope positively a moment ago, so the refusal belongs here.
        return (
            "",
            "",
            (
                f"Error: {verb} is refused for a channel- or schedule-bound session "
                "owned by an app — its identity cannot be re-verified at the point of "
                "the write, so the folder rules could not be bounded to it. Run this "
                "from the app's own dashboard session."
            ),
        )
    return caller_key, scope, None


def _is_messaging_caller(caller_key: str) -> bool:
    """True when *caller_key* is a channel agent or a messaging-channel session.

    Reads the namespace roster from ``constants`` rather than
    ``messaging.link``: importing anything from ``messaging`` runs its package
    init, which this stdio server does not need.
    """
    prefixes = ("channel:",) + tuple(
        f"{ns}{sep}" for ns in CHANNEL_SESSION_NAMESPACES for sep in (":", "_")
    )
    return caller_key.startswith(prefixes) or bool(SLACK_THREAD_TS_RE.fullmatch(caller_key))
