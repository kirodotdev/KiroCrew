"""Dashboard shared state — ChatSlot and DashboardState."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import fnmatch
import hashlib
import json
import logging
import math
import os
import re
import shlex
import threading
import time
import traceback
import uuid
from collections.abc import Coroutine, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, NamedTuple, TypeVar

from aiohttp import web

from kiro_crew.acp.types import STOP_REASON_CANCELLED
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import (
    DASHBOARD_PORT,
    _raw_config,
    config_dir,
    resolve_effective_agent,
)
from kiro_crew.constants import (
    OPTIONS_RE_LINE,
    SUBAGENT_BATCH_COMPLETION_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
)
from kiro_crew.dashboard.chat_compaction_notice import deliver_channel_compaction_notice
from kiro_crew.dashboard.dashboard_persistence import DashboardPersistenceCoordinator
from kiro_crew.dashboard.folder_repository import FOLDERS_FILE, FolderRepository
from kiro_crew.dashboard.interaction_coordinator import (
    ApprovalCoordinator,
    QuestionCoordinator,
)
from kiro_crew.dashboard.notification_coordinator import NotificationCoordinator
from kiro_crew.dashboard.remote_mirror import mirror_frame as _mirror_relay_frame
from kiro_crew.dashboard.session_pulse_counter import increment_user_session_count_off_loop
from kiro_crew.dashboard.side_state import SideState
from kiro_crew.dashboard.slot_buffers import SlotBufferCoordinator
from kiro_crew.dashboard.slot_projection import SlotProjection
from kiro_crew.dashboard.slot_queue_repository import SlotQueueRepository
from kiro_crew.dashboard.slot_registry import SlotRegistry
from kiro_crew.dashboard.system_notices import is_system_notice
from kiro_crew.dashboard.websocket_hub import WebSocketHub
from kiro_crew.deny_guidance import remediation_for
from kiro_crew.history import latest_transcript_ts, mint_row_mid, monotonic_transcript_ts
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.messaging.link import (
    SLACK_NAMESPACE,
    UNBIND_REASON_DASHBOARD_UNLINK,
    UNBIND_REASON_ENTRY_DELETED,
    UNBIND_REASON_ORIGIN_REBIND,
    UNBIND_REASON_PRUNED_STALE,
    UNBIND_REASON_SESSION_DESTROYED,
    UNBIND_REASON_UNSPECIFIED,
    UNBIND_REASON_USER_UNLINK,
    ChannelLink,
    channel_namespace_of,
    is_channel_session_key,
)
from kiro_crew.messaging.renderer import display_safe
from kiro_crew.notifications.bus import (
    NotificationBus,
    NotificationValidationError,
    normalize_note,
    payload_from_legacy,
)
from kiro_crew.notifications.rate_limit import AppRateLimiter
from kiro_crew.notifications.resource_pressure import ResourcePressureNotifier
from kiro_crew.notifications.settings import ChannelSettings
from kiro_crew.preview_text import strip_markdown_preview
from kiro_crew.release_channel import channel as _release_channel_of_build
from kiro_crew.safety_override import safety_override
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard._types import (  # noqa: F401
        ContextBuilder,
        ConversationLog,
        CronService,
        HistoryConsolidator,
        LessonStore,
        SessionManager,
        SubagentManager,
        TaskRunner,
    )
    from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog  # noqa: F401
    from kiro_crew.messaging.transport import MessagingTransport  # noqa: F401
    from kiro_crew.power import SleepInhibitor  # noqa: F401
    from kiro_crew.slack.outbound import PostedOptions  # noqa: F401

logger = logging.getLogger(__name__)

#: Cache for :meth:`DashboardState.served_bundle_id` — the served frontend
#: entry point's ``(mtime_ns, size)`` -> short content hash. One slot: there is
#: exactly one served bundle per process, and the snapshot that reads it sits
#: on the hot status path.
_BUNDLE_ID_CACHE: dict[str, tuple[tuple[int, int], str]] = {}
_FOLDER_REPOSITORY = FolderRepository(lambda: logger)


def _new_notification_coordinator() -> NotificationCoordinator:
    """Build a coordinator whose providers resolve facade seams at call time."""
    return NotificationCoordinator(
        logger_provider=lambda: logger,
        payload_from_legacy=lambda *args, **kwargs: payload_from_legacy(*args, **kwargs),
        validation_error=NotificationValidationError,
        redact_value=lambda value: _redact_note_value(value),
        sweep_expired=lambda notes: sweep_expired_notifications(notes),
        persist_one=lambda note: _persist_notification(note),
        rewrite_all=lambda notes: _rewrite_notifications(notes),
        executor_provider=lambda: _notification_io_executor(),
        max_persisted=_MAX_PERSISTED_NOTIFICATIONS,
    )


def _notifications_for(state: Any) -> NotificationCoordinator:
    """Return the per-state coordinator, including for ``__new__`` test states."""
    coordinator = getattr(state, "_notification_coordinator", None)
    if not isinstance(coordinator, NotificationCoordinator):
        coordinator = _new_notification_coordinator()
        state._notification_coordinator = coordinator
    return coordinator


def _approvals_for(state: Any) -> ApprovalCoordinator:
    coordinator = getattr(state, "_approval_coordinator", None)
    if not isinstance(coordinator, ApprovalCoordinator):
        coordinator = ApprovalCoordinator()
        state._approval_coordinator = coordinator
    return coordinator


def _questions_for(state: Any) -> QuestionCoordinator:
    coordinator = getattr(state, "_question_coordinator", None)
    if not isinstance(coordinator, QuestionCoordinator):
        coordinator = QuestionCoordinator()
        state._question_coordinator = coordinator
    return coordinator


def _permission_marker() -> Callable[[list[dict], str, str], bool]:
    """Resolve the module-level marker lazily for monkeypatched tests/callers."""
    return _mark_permission_resolved


def _new_websocket_hub(state: Any) -> WebSocketHub:
    """Build a hub whose providers resolve facade seams at call time."""
    return WebSocketHub(
        state,
        serving_loop_provider=lambda: state.serving_loop,
        running_loop_provider=lambda: state._running_loop(),
        logger_provider=lambda: logger,
        redact_credentials_provider=lambda: redact_credentials,
        redact_exfiltration_urls_provider=lambda: redact_exfiltration_urls,
        scope_state_provider=lambda: state,
    )


def _websocket_for(state: Any) -> WebSocketHub:
    """Return the per-state hub, including for ``__new__`` test states."""
    hub = getattr(state, "_websocket_hub", None)
    if not isinstance(hub, WebSocketHub):
        hub = _new_websocket_hub(state)
        state._websocket_hub = hub
    return hub


def _new_dashboard_persistence() -> DashboardPersistenceCoordinator:
    """Build persistence providers that preserve module monkeypatch seams."""
    return DashboardPersistenceCoordinator(
        config_dir_provider=lambda: config_dir(),
        atomic_write_provider=lambda: atomic_write,
        logger_provider=lambda: logger,
        json_codec_provider=lambda: json,
        wall_time_provider=lambda: time.time(),
    )


def _persistence_for(state: Any) -> DashboardPersistenceCoordinator:
    """Return the per-state coordinator, including for ``__new__`` test states."""
    coordinator = getattr(state, "_persistence_coordinator", None)
    if not isinstance(coordinator, DashboardPersistenceCoordinator):
        coordinator = _new_dashboard_persistence()
        state._persistence_coordinator = coordinator
    return coordinator


def _registry_for(state: Any) -> SlotRegistry:
    """Return the per-state registry boundary for facade-compatible test states."""
    registry = getattr(state, "_slot_registry", None)
    if not isinstance(registry, SlotRegistry):
        registry = SlotRegistry()
        state._slot_registry = registry
    return registry


#: The single ceiling on live slots, owned by the module that owns the slot
#: table (see :meth:`DashboardState.live_slot_count`). Every path that allocates
#: a slot -- session create, chat fork, session import -- tests ``live_slot_count()``
#: against this one number, so raising the ceiling is a single edit and no entry
#: point can silently drift to a different limit.
MAX_LIVE_SLOTS = 500

#: The most live slots ONE creator may hold, as a sub-ceiling under
#: :data:`MAX_LIVE_SLOTS`. The global ceiling alone bounds the total but not the
#: distribution, so a single automated creator working through a nudge loop can
#: reach 500 on its own and every later create -- including the person opening a
#: new chat tab -- gets the 429. That is the availability half of the cap: a
#: bounded resource that one caller may exhaust entirely is not bounded from
#: anybody else's point of view.
#:
#: Deliberately far above real use and far below the global ceiling: a decomposed
#: goal runs on the order of ten concurrent sessions, so 50 never binds honest
#: work, while 450 slots stay reachable by everyone else no matter what one
#: caller does. Enforced in ``session_control.create_session``, which is the only
#: entry point that creates on some OTHER caller's behalf; a person's own new tab
#: and a fork are attributed to nobody and so are bounded by the global ceiling
#: alone.
MAX_SLOTS_PER_CREATOR = 50

# Structured monitor wakeups are automation, not user speech. The controller
# owns the complete envelope; every delivery surface passes it through unchanged.
MONITOR_WAKE_PREFIX = "[Monitor wake]"

#: Return type of a mutate_folders callback.
_T = TypeVar("_T")

_CHANNEL_ID_PREFIX_RE = re.compile(r"^([a-z][a-z0-9_-]*):(.*)$", re.IGNORECASE)
_CHANNEL_LABELS = {
    "slack": "Slack",
    "discord": "Discord DM",
    "telegram": "Telegram",
    "teams": "Microsoft Teams",
    "webex": "Webex",
    "wecom": "WeCom",
    "weixin": "WeChat",
    "imessage": "iMessage",
    "whatsapp": "WhatsApp",
    "feishu": "Feishu",
}


def _safe_folder_tree(folders: object) -> list[dict[str, Any]]:
    """Well-formed folder entries safe to ship on the slots broadcast frame.

    The loader validates disk rows, but lightweight states and test doubles can
    bypass it. Keep the broadcast hot path tolerant of an unset or malformed
    in-memory value.
    """
    if not isinstance(folders, list):
        return []
    return [f for f in folders if isinstance(f, dict) and isinstance(f.get("id"), str)]


def _slots_ws_frame(
    slots: object,
    *,
    yolo: bool,
    channel_trusted: bool,
    gitlab_hosts_gen: object,
    folders: object,
    folders_gen: object,
    governance_gen: object,
) -> str:
    """Serialize the dashboard-user ``slots`` WS frame.

    ONE builder for the two sites that send this frame — the generic fan-out in
    :meth:`DashboardState._broadcast` and the owner frame in
    :meth:`DashboardState._do_slots_broadcast` — because they must carry the SAME
    keys and nothing else enforces that. ``_send_ws_all`` skips owner sockets for
    ``slots``, so the generic frame no longer backstops an owner: a key present in
    one envelope and not the other silently deprives every owner window, with no
    error anywhere. Hand-building both is exactly how they drifted before (the
    owner frame was a strict subset, missing ``folders`` and
    ``gitlabHostsGeneration``), so the remedy is one builder rather than a second
    careful copy — drift becomes impossible by construction instead of merely
    detectable.

    DELIBERATELY NOT used for the app-token frame in
    :meth:`DashboardState._serialize_for_client`. That envelope diverges on
    purpose: it omits ``folders`` (apps do not render the chat folder tree) and
    gates ``yolo`` / ``channelTrusted`` behind the token's declared scope. Routing
    it through here would widen an app's payload, so it is a third shape by
    design, not a duplicate awaiting cleanup.
    """
    return json.dumps(
        {
            "type": "slots",
            "data": slots,
            "yolo": yolo,
            "channelTrusted": channel_trusted,
            "gitlabHostsGeneration": gitlab_hosts_gen,
            "folders": folders,
            "foldersGeneration": folders_gen,
            # Which governance ceiling is installed. A centrally pushed policy
            # (``policy_distribution.apply_ceiling``) swaps the ceiling mid-session
            # and bumps this counter; the client invalidates its cached
            # ``dashboardConfig`` on a change, so a governance-derived field there
            # (``social_share_enabled``) follows the ceiling instead of waiting out
            # its stale window. Process-local, like the two counters above.
            "governanceGeneration": governance_gen,
        }
    )


def _split_namespaced_channel_id(channel_id: str | None) -> tuple[str, str] | None:
    """Return ``(channel_type, target)`` for a ``<type>:<target>`` id."""
    if not channel_id:
        return None
    match = _CHANNEL_ID_PREFIX_RE.match(channel_id)
    if not match:
        return None
    return match.group(1).lower(), match.group(2)


def _is_genuine_slack_link(thread_ts: str | None, channel_id: str | None) -> bool:
    """True only for a complete Slack link, never another channel's legacy id."""
    namespaced = _split_namespaced_channel_id(channel_id)
    return bool(
        thread_ts and channel_id and (namespaced is None or namespaced[0] == SLACK_NAMESPACE)
    )


def _link_label(channel_type: str) -> str:
    """Human label for a known channel; preserve unknown types verbatim."""
    return _CHANNEL_LABELS.get(channel_type, channel_type)


def _redacted_link_target(target: str | None) -> str:
    """Return a non-sensitive tail hint, never a raw conversation id."""
    if not target:
        return "…"
    safe, _ = redact_exfiltration_urls(target)
    safe, _ = redact_credentials(safe)
    if safe != target:
        return "…redacted"
    if len(safe) <= 6:
        return f"…{safe[-2:]}" if len(safe) > 2 else "…"
    return f"…{safe[-6:]}"


# Native kiro-cli subagent reconnect policy. The slot state, writer, and replay
# path all import these bounds so retention cannot drift between modules.
NATIVE_SUBAGENT_OUTPUT_TAIL = 40_000
NATIVE_SUBAGENT_OUTPUT_HARD = 80_000
NATIVE_SUBAGENT_DONE_RESULT_CAP = 8_000
NATIVE_SUBAGENT_DONE_TRUNC_MARKER = "…(earlier output truncated)\n"
NATIVE_SUBAGENT_TERMINAL_KEEP = 50
NATIVE_SUBAGENT_TERMINAL_TTL_SECS = 3600.0

# Cap on a slot's queued-completion delivery ledger (see
# ``_ChatSlot.note_pending_subagent_delivery``). Well above any legitimate
# in-flight set — the slot queue itself is capped at 50 rows — so it only ever
# evicts entries left behind by rows that vanished from the queue without being
# consumed, and eviction merely defers those agents' cleanup to the next start.
_MAX_PENDING_SUBAGENT_DELIVERIES = 128


def _delivery_key(content: str) -> str:
    """Identity of a queued completion for delivery bookkeeping.

    A digest of the announce rather than the text itself: a wave digest runs to
    tens of kilobytes, and the ledger only needs to recognise the same announce
    again after a pre-consumption failure re-queues it verbatim under a new
    queue-entry id. Not a security boundary — nothing is authenticated by it.
    """
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:32]


# Slot-list broadcast coalescing window. The sub-agent slots debouncer in
# slack/gateway.py hardcodes the same value independently; the two are not shared.
_SLOTS_BROADCAST_INTERVAL_S: float = 0.2


def native_subagent_output_tail(chunks: list[str], limit: int = NATIVE_SUBAGENT_OUTPUT_TAIL) -> str:
    """Join only the trailing ``limit`` characters of native-card output."""
    if limit <= 0:
        return ""
    collected: list[str] = []
    total = 0
    for chunk in reversed(chunks):
        collected.append(chunk)
        total += len(chunk)
        if total >= limit:
            break
    collected.reverse()
    return "".join(collected)[-limit:]


# Running build's git (branch, short_commit). Resolved ONCE by the CLI gateway
# entrypoint via set_build_info() — AFTER KIROCREW_PROJECT_DIR is detected and
# BEFORE asyncio.run() starts the loop. Deliberately NOT resolved at import time:
# under systemd the entrypoint imports this module before main() detects the
# project dir, so an import-time git_build_info() would see no project dir and the
# lru_cache would then pin ("", "") forever. DashboardState (built on the loop) and
# status_snapshot() only READ this global — they never call git_build_info() — so
# no subprocess ever runs on the event loop.
_build_info: tuple[str, str] = ("", "")


# Auto-minted dashboard slot keys share the shape "<prefix>-<N>-<ts>" where
# <prefix> is chat (the only auto-mint prefix in this fork), <N> is the
# monotonic _slot_counter, and <ts> is a unix second. Minting and index-parsing
# both go through these helpers so the format lives in exactly one place — a
# future change to the key shape can't silently desync the minter from
# reseed_slot_counter() (which would let the post-restart tab<->session
# collision quietly return).
def _mint_slot_key(prefix: str, counter: int, ts: int) -> str:
    """Build an auto-minted slot key of the canonical ``<prefix>-<N>-<ts>`` shape."""
    return f"{prefix}-{counter}-{ts}"


def _slot_index_from_key(key: str) -> int | None:
    """Return the ``<N>`` index from a ``<prefix>-<N>-<ts>`` slot key, else None.

    Non-auto-minted keys (Slack sessions, ascii-sanitized display names) don't
    match the shape and return ``None``. The ``isascii()`` guard keeps a stray
    unicode-digit char (``str.isdigit()`` is True for e.g. superscripts, but
    ``int()`` would raise) from aborting boot-time reseeding.
    """
    parts = key.rsplit("-", 2)
    if len(parts) == 3 and parts[1].isascii() and parts[1].isdigit():
        return int(parts[1])
    return None


def set_build_info(info: tuple[str, str]) -> None:
    """Record the running build's ``(branch, short_commit)`` for status payloads.

    Called once from the CLI gateway entrypoint (sync, pre-loop, post-detection).
    Defaults to ``("", "")`` for non-git / packaged installs, which the frontend
    renders by omitting the build-info rows.
    """
    global _build_info
    _build_info = info


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """Log unhandled exceptions from fire-and-forget tasks.

    Shared by gateway._deliver_result and chat.py queue-drain paths.
    Short-circuits on cancelled tasks (task.exception() would raise CancelledError).
    Exception message is redacted to avoid leaking credentials/URLs to log sinks.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        try:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            redacted_tb, _ = redact_credentials(tb)
            redacted_tb, _ = redact_exfiltration_urls(redacted_tb)
            logger.error("Background task failed:\n%s", redacted_tb)
        except Exception as redaction_err:
            # Include the redaction failure class so bugs in the redactor are visible,
            # without logging the raw traceback (which defeats the redaction contract).
            logger.error(
                "Background task failed (redaction error %s): %s",
                type(redaction_err).__name__,
                type(exc).__name__,
            )


# ── Read-only bash command classification ──

_READ_ONLY_BASH_PREFIXES: tuple[str, ...] = (
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "egrep",
    "fgrep",
    "wc",
    "which",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "diff",
    "pwd",
    "echo",
    "date",
    "whoami",
    "hostname",
    "uname",
    "readlink",
    "realpath",
    "basename",
    "dirname",
    "git status",
    "git log",
    "git diff",
    "git show",
    "git branch",
    "git tag",
    "git remote",
    "git rev-parse",
    "git describe",
    "git ls-files",
    "git ls-tree",
    "git cat-file",
    "git blame",
    "brazil ws show",
    "brazil ws list",
    "brazil workspace show",
    "brazil workspace list",
    "brazil versionset print",
    "brazil versionset show",
    "brazil-path",
    "python --version",
    "python3 --version",
    "node --version",
    "java -version",
    "javac -version",
)

_READ_ONLY_PIPE_RE = re.compile(
    r"^\s*(grep|egrep|fgrep|head|tail|wc|sort|uniq|cut|less|more|cat)\b"
)

# Reject redirections and command substitutions, conservatively.
#
# `<` is matched only as `<(` here, NOT bare. Bare `<` and word-initial `#` are
# TOKEN ELISION rather than command execution, and they are handled per verb in
# `_side_effect_reason` instead: see `_ELISION_SENSITIVE` for why a global refusal
# was the wrong place for them.
_UNSAFE_SHELL_RE = re.compile(r">|`|\$\(|<\(|(?<!&)&(?!&)")

# Discard-only redirect idioms that are read-only despite containing '>'/'&':
# `2>/dev/null`, `>/dev/null`, `&>/dev/null`, `2>>/dev/null`, and `2>&1`.
# These sink or merge output, never writing a real file, so they must be
# stripped before _UNSAFE_SHELL_RE — otherwise every `find … 2>/dev/null`
# falls through to an interactive prompt. A redirect to any real path
# (e.g. `cmd > out.txt`) still trips _UNSAFE_SHELL_RE and stays unsafe.
# The `(?![\w./-])` guard pins the match to the literal device `/dev/null`:
# without it, `>/dev/nullx` or `>/dev/null/../etc/passwd` would be scrubbed as
# a sink, smuggling a real-file write past the unsafe-shell check.
_DEVNULL_REDIR_RE = re.compile(r"(?:\d*>>?|&>)\s*/dev/null(?![\w./-])|\d*>&\d+")

# ── Side effects reached through an allowlisted read-only verb ──
#
# The allowlist above names a verb and is matched as a prefix, so it vouches
# for every flag, subcommand and operand that verb accepts. Some of those
# write a file, change a ref or launch another program — and none of it goes
# through a shell redirect, so `_UNSAFE_SHELL_RE` never sees it.
#
# The tables are keyed by verb because the same spelling is harmless
# elsewhere: `ls -o` is a long listing format and `grep -o` prints only the
# match, while `sort -o FILE` truncates and writes FILE.

# Flags that make the program write a file named on its own command line.
_WRITE_FLAGS: dict[str, tuple[str, ...]] = {
    # `-R` writes without being handed a filename: tree re-runs itself in every
    # directory it descends into, adding `-o 00Tree.html` each time, so the file
    # is named by tree rather than by the command line. Same outcome as `-o`, one
    # step removed, which is why looking only for a filename-bearing flag missed
    # it.
    "tree": ("-o", "--output", "-R"),
    "uniq": ("-o",),
    "git diff": ("--output",),
    "git show": ("--output",),
    "git log": ("--output",),
    # A pager, and not on the prefix allowlist — but the PIPE-TARGET check runs
    # this table too, and `cat f | less -O FILE` writes FILE from a segment whose
    # leading verb is a read.
    "less": ("-o", "-O", "--log-file", "--LOG-FILE"),
}

# Flags that hand control to a program the repository names, not the caller:
# an external diff driver comes from the repo's config or .gitattributes.
_EXEC_FLAGS: dict[str, tuple[str, ...]] = {
    # `--textconv` is the same hand-off as `--ext-diff` through a different config
    # key, and it was missing here while `git cat-file` below already listed it —
    # the table gap, not the design, is what let `git diff --textconv` through.
    #
    # Scope, stated plainly: this stops the COMMAND LINE from selecting the
    # program. It does not stop a textconv driver the user configured from being
    # applied by default, because that name comes from git config, which is not
    # part of a repository and is not something a checkout can add. Requiring
    # `--no-textconv` would be the only way to cover that, and it would take plain
    # `git diff` off the read-only path — the most common read there is.
    "git diff": ("--ext-diff", "--textconv"),
    "git show": ("--ext-diff", "--textconv"),
    "git log": ("--ext-diff", "--textconv"),
    # `--filters` runs the repository's clean/smudge filter — a command from
    # `.gitattributes`, i.e. chosen by the checkout rather than by the caller.
    # `--textconv` is the same hand-off through a different config key.
    "git cat-file": ("--filters", "--textconv"),
    # A pager that runs a filter over its input, reachable as a pipe target.
    # `-k` is the sharper one and it is INDIRECT: it loads a lesskey file, and a
    # lesskey file can set environment variables — including `LESSOPEN`, which less
    # treats as an input PREPROCESSOR and runs. So a checkout-supplied lesskey is
    # arbitrary command execution, two steps removed from anything on the command
    # line. Verified against `less --help` on less 608: `-k [file]` /
    # `--lesskey-file=[file]`, plus `--lesskey-src` on newer builds.
    #
    # Case is load-bearing here, as it is for `file -C`: lowercase `-k` loads the
    # keyfile, while uppercase `-K` is `--quit-on-intr` and an ordinary read.
    "less": ("--filter", "-k", "--lesskey-file", "--lesskey-src"),
}

# Flags that name a file which in turn NAMES THE PATHS the program opens. This is
# an INDIRECTION, not a write, which is why a write-flag table could not hold it:
# the hook layer applies `is_sensitive_path` to the command text, so it sees the
# list file and nothing else while the program reads every path inside.
#
# Measured on coreutils, with a NUL-separated list containing `/etc/hostname`:
# `wc --files0-from=list0` and `du --files0-from=list0` both read `/etc/hostname`,
# a path that never appears in argv.
#
# Scoped to the heads that HAVE the flag and are not already covered, and
# deliberately spelled in full: `--file` is a prefix of `--files0-from`, so a
# shorter entry would be reached by the abbreviation walk in `_glob_reaches` and
# cost `grep --file=PATTERNS` and `stat --file` for no gain.
#
# `sort` HAS the flag (checked against `--help`) and is deliberately NOT here.
# It is already refused by `_OPTION_ACCEPT_LISTS`, which admits an option only if
# it is listed, and `--files0-from` is not in `_SORT_READONLY_LONG`. Listing it
# twice would change nothing but the reason string, while making a reader think
# this table is what closes it. The glob defence is unaffected: it derives
# `--files0-from` from the entries below, not from the key it appears under.
_INDIRECT_LIST_FLAGS: tuple[str, ...] = ("--files0-from",)

_INDIRECT_LIST_FLAGS_BY_PREFIX: dict[str, tuple[str, ...]] = {
    prefix: _INDIRECT_LIST_FLAGS for prefix in ("wc", "du")
}

# Pagers take a `+` argument that is not an option but a string in the pager's OWN
# command language, and that language contains a shell escape. Measured under a
# real pty: `git log | less '+!touch FILE'` CREATED the file.
#
# Two things about that measurement decide the shape of this rule.
#
# It did NOT fire when stdout was a pipe -- less degrades to `cat` with no tty and
# never runs the startup command. So this is not unconditional code execution; it
# depends on whether whatever executes the command supplies a tty. The classifier
# cannot know that (the gate at `hooks.on_tool_call` hands the string to an agent
# runtime, it does not run it), so it must fail closed on the spelling.
#
# `more` did NOT fire on util-linux, which has no `+command`. It is listed anyway
# because on the BSDs `more` is not a separate program: FreeBSD's
# `usr.bin/less/Makefile` installs it as a link (`LINKS= ${BINDIR}/less
# ${BINDIR}/more`), and Apple's `less/main.c` detects the name at startup:
#
#     if (strcmp(last_component(progname), "more") == 0)
#             less_is_more = 1;
#
# `less_is_more` changes defaults, not the `+` startup-command path, so `more '+!cmd'`
# reaches the same shell escape there. This module ships to macOS, and the name a
# binary is invoked under does not tell the classifier which implementation answers,
# so both names are listed rather than branching on `sys.platform`.
#
# The whole `+` prefix is refused rather than the dangerous letters (`!` shell,
# `|` pipe-to-shell, `v` editor, `s` save-to-file), because enumerating them is the
# denylist this PR exists to argue against: the set is the pager's command
# language, and it grows without asking.
_PAGER_STARTUP_VERBS: frozenset[str] = frozenset(("less", "more"))

# Words bash DELETES before exec, which `shlex.split` keeps. The token list this
# module reads is therefore a strict SUPERSET of the real argv, and a phantom word
# can make a segment look like something it is not. Measured on a scratch repo,
# with an empty file named `--list` present for the redirect form:
#
#     git branch injected # --list     -> CREATED the branch
#     git tag forged # --list          -> CREATED the tag
#     git branch injected < --list     -> CREATED the branch
#     git branch injected <<< --list   -> CREATED the branch
#
# In each case `shlex` supplied a `--list` that put the segment in list mode, so
# the operand walk read `injected` as a pattern instead of a ref to create.
#
# WHY THIS IS PER VERB AND NOT A GLOBAL REFUSAL. A phantom word can only ADD to the
# token list. For a verb decided by its FLAGS, an added word is either inert or gets
# read as a flag it does not have, so the worst case is an extra prompt: safe. Only a
# verb decided by POSITION or MODE can be flipped, because there the count and the
# order of the words carry the decision. Refusing globally cost five ordinary reads
# that no phantom word could have made unsafe (`wc -l < f`, `grep TODO < f`,
# `cat < f`, `wc -l <f`, `head -20 < log.txt`), which is a worse trade than the
# narrower rule.
#
# WHY REFUSE RATHER THAN STRIP THE REDIRECT. Stripping the operator and its target
# looks equivalent and is not: `shlex` has already discarded the quoting, so a token
# beginning with `<` is indistinguishable from a QUOTED argument that begins with
# `<`. Stripping would drop a word bash keeps, and for exactly these verbs that
# opens a hole rather than closing one: `git branch '<new'` reaches `shlex` as
# `[git, branch, <new]`, and dropping `<new` leaves a bare `git branch` that reads
# as a listing while bash creates the ref. Refusing fails closed without
# reimplementing bash's quoting rules, which this module deliberately does not do.
_ELISION_SENSITIVE_RE = re.compile(r"(?:^|\s)#|<")

# `git branch` and `git tag` each carry a read mode and a write mode under one
# subcommand, so the prefix match admits the destructive spellings. Some of
# these also open `$EDITOR`, which runs a program of the environment's
# choosing: `git branch --edit-description` always does, and `git tag <name>`
# does whenever `tag.gpgSign` or `tag.forceSignAnnotated` is set.
_GIT_REF_WRITE_FLAGS: dict[str, tuple[str, ...]] = {
    "branch": (
        "-d",
        "-D",
        "--delete",
        "-m",
        "-M",
        "--move",
        "-c",
        "-C",
        "--copy",
        "-f",
        "--force",
        "-u",
        "--set-upstream",
        "--set-upstream-to",
        "--unset-upstream",
        "--edit-description",
    ),
    "tag": (
        "-d",
        "--delete",
        "-a",
        "--annotate",
        "-s",
        "--sign",
        "-m",
        "--message",
        "-F",
        "--file",
        "-f",
        "--force",
        "--cleanup",
        # `-u <keyid>` is `--local-user`: it makes the tag ANNOTATED and signed,
        # so it creates a ref exactly as `-a` does. `-u` was in the `branch` list
        # (set-upstream) and missing here, and the omission was reachable —
        # `git tag -ulin@kiro.co release` created a signed tag.
        "-u",
        "--local-user",
    ),
}

# Why a bare operand needs TWO tables rather than one.
#
# `git branch <name>` creates a ref, so a bare operand is the signal. Deciding
# when an operand is NOT that name was collapsed into a single "this flag eats
# the next token" set, and that conflated two different things git keeps apart:
#
#   * whether the flag CONSUMES the following word, which git's own option
#     parser decides by whether the argument is required or optional. A required
#     argument is taken from the next word; an OPTIONAL one must be attached with
#     `=`, and a separate word is left as an operand. `--color` is optional, so
#     `git branch --color newbranch` still creates `newbranch` — while the guard
#     read `newbranch` as the colour and passed the segment.
#   * whether the command is in LIST mode, where an operand is a pattern to match
#     rather than a ref to create. `git branch --list newbranch` lists, it does
#     not create, so treating that operand as a creation would be a false denial.
#
# Splitting them keeps both answers right: `--color` is in neither set, and
# `--list` is in the list-mode set only.

# Flags whose argument is REQUIRED, so git takes it from the following word.
_GIT_REF_VALUE_FLAGS: frozenset[str] = frozenset(("--points-at", "--format", "--sort"))


def _consumes_next_word(token: str) -> bool:
    """Whether *token* is a required-argument flag that takes the FOLLOWING word.

    Abbreviations count, because git's parser resolves them: `git branch --form`
    reaches `--format` and eats the next word, so reading `--form` as an ordinary
    option left `-l` in `git branch --form -l newbranch` looking like a list flag
    and licensed the bare operand that created the branch.

    This is the fourth site on this guard to need the abbreviation axis — after
    `_matched_flag`, `_option_accept_list_violation` and `_glob_reaches` — which is why
    named helper rather than a fourth inline prefix test.

    An ATTACHED value (`--format=x`) takes nothing from the next word, so it is
    not one of these. Over-matching an ambiguous abbreviation is safe: git rejects
    it rather than running it.
    """
    if "=" in token or not token.startswith("--") or len(token) <= 2:
        return False
    return any(flag == token or flag.startswith(token) for flag in _GIT_REF_VALUE_FLAGS)


# Flags that put `git branch` / `git tag` in list mode, where a bare operand is a
# pattern. `--points-at` appears in both: it consumes its value AND selects.
_GIT_REF_LIST_FLAGS: frozenset[str] = frozenset(
    (
        "-l",
        "--list",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
    )
)


def _glob_shifts_arguments(token: str) -> bool:
    """Whether a glob in *token* can change how many arguments the program gets.

    A separate question from `_glob_hides_word`, which asks what a pattern can
    expand INTO. This one is about the COUNT, and it cuts both ways:

    * several matches become several WORDS — with `in1` and `in2` present,
      `uniq in*` runs `uniq in1 in2`, whose second operand is an OUTPUT file;
    * no match under `nullglob` makes the word VANISH — `git branch --format
      nomatch* --list newbranch` loses the format's value, so `--format` eats
      `--list` instead and `newbranch` stops being a pattern.

    Neither outcome needs the pattern to resemble anything this module decides on,
    so it is only asked where the argument COUNT or POSITION carries the verdict:
    a `uniq` operand, and a required git option's value. An operand whose meaning
    does not depend on its position — `ls *.py`, `git branch --list 'feat/*'` —
    is not affected, which is what keeps ordinary globbing on the read path.
    """
    return bool(_GLOB_META_RE.search(token) or _EXTGLOB_RE.search(token))


# The SHORT list-mode flags, per subcommand, because they do not agree. `-l` is
# a listing for both, but `git tag -n[<num>]` prints annotation lines — a listing
# form `git branch` has no counterpart for, and reading it as anything else
# denied a common inspection (`git tag -n 'v1.*'`) its auto-approval. Kept as
# LETTERS, not flags, because they arrive bundled: `git tag -n2` and `git tag -ln`
# both select, and only a per-letter test sees that. A letter here must never be
# a write flag for the same subcommand — `-n` is not in `_GIT_REF_WRITE_FLAGS`
# ("tag"), which is what makes reading it as a listing safe.
_GIT_REF_LIST_SHORTS: dict[str, str] = {"branch": "l", "tag": "ln"}

# Flags that CANCEL list mode, per subcommand. git's parse-options auto-generates
# a `--no-<opt>` negation for a boolean, so `--list` has one and it undoes the
# listing — `git branch --list --no-list newbranch` CREATES the branch. Verified
# against git rather than inferred, which matters because the neighbouring
# `--no-` spellings do NOT behave this way:
#
#     git branch --list --no-list nl1                -> branch nl1 CREATED
#     git branch --list --no-lis nl2                 -> branch nl2 CREATED
#     git branch -l --no-list nl3                    -> branch nl3 CREATED
#     git branch --contains HEAD --no-contains nc1   -> no ref (git errors)
#     git branch --merged --no-merged nm1            -> no ref (git errors)
#     git tag -l --no-list t1                        -> no ref (unknown option)
#
# So `--no-contains` and `--no-merged` are real list FILTERS rather than
# negations, and treating them as cancelling would deny two ordinary reads;
# `git tag` has no `--no-list` at all. The table says only what was measured.
_GIT_REF_LIST_CANCEL_FLAGS: dict[str, tuple[str, ...]] = {"branch": ("--no-list",)}


def _cancels_list_mode(token: str, subcommand: str) -> bool:
    """Whether *token* turns list mode off for *subcommand*.

    Abbreviations count, as everywhere else on this guard: `--no-lis` reaches
    `--no-list`. Cancelling can only move a bare operand from "pattern" to
    "creates a ref", i.e. toward the prompt, so over-matching here is safe.
    """
    head = token.split("=", 1)[0]
    if not head.startswith("--") or len(head) <= 2:
        return False
    cancels = _GIT_REF_LIST_CANCEL_FLAGS.get(subcommand, ())
    return any(flag.startswith(head) for flag in cancels)


# `git remote` subcommands that rewrite remote configuration. `set-url` is the
# sharpest: it repoints the remote, so later fetches and pushes go elsewhere.
_GIT_REMOTE_WRITE_SUBCOMMANDS: frozenset[str] = frozenset(
    ("add", "remove", "rm", "rename", "set-url", "set-head", "set-branches", "prune", "update")
)

# Allowlist entries that name a VERSION PROBE, matched as a prefix like every
# other entry — so a trailing operand was vouched for too. That is not academic:
# `javac` does not act on `-version` and exit, it prints the version and then
# compiles whatever else it was handed, so
#
#     javac -version -processorpath evil.jar -processor Evil Payload.java
#
# auto-approved and ran an annotation processor — ordinary compiled Java on a
# path the caller supplies, i.e. arbitrary code execution. Reported by #5038,
# and the highest-severity shape in this family.
#
# All five probes are listed, not only `javac`. Whether an interpreter ignores a
# trailing operand is a property of the installed release rather than of the
# flag, and JDK single-file source mode (`java Foo.java`) already moved that
# answer once. A version probe has no legitimate operand, so requiring the exact
# spelling costs nothing and does not depend on being right about each tool.
_EXACT_ONLY_BASH_PREFIXES: frozenset[str] = frozenset(
    (
        "python --version",
        "python3 --version",
        "node --version",
        "java -version",
        "javac -version",
    )
)

# `sort` is vetted POSITIVELY: every option token must be a recognised read-only
# flag, and anything unrecognised goes to the human prompt.
#
# The inversion is here because the denylist demonstrably did not converge on this
# one tool. Five review rounds produced six distinct spellings of the same escape:
# `-o FILE`, `--output=FILE`, attached `-oFILE`, bundled `-uo FILE`, abbreviated
# `--o FILE`, and finally `--compress-program=PROG` -- which is not a write at all
# but arbitrary CODE EXECUTION. Verified: with the input large enough to spill to
# temporaries, `sort -S 1k --compress-program=./payload big.txt` ran the payload and
# exited 0. Enumerated from this box's own `sort --help`, so it is complete for that
# release rather than for a guess.
#
# Deliberately NOT read-only: `-o/--output` (writes), `-T/--temporary-directory`
# (writes temporaries into a caller-named directory), `--compress-program`
# (executes), and `--random-source` / `--files0-from` (open a caller-named path).
# An unlisted flag costs a prompt, so omission is the safe direction.
# `k`, `t` and `S` are value-taking AND read-only (key, field separator, buffer
# size); `o` and `T` are value-taking and NOT read-only, which is what makes the
# value branch below refuse them while `-k2n` and `-S1k` pass.
_SORT_READONLY_SHORT: frozenset[str] = frozenset("bdfgiMhnRrVcCmsuzktS")
# Short options that consume the rest of the token (or the next one) as a VALUE.
# Needed so `-k2n` and `-S1k` read as flag-plus-value instead of a letter cluster
# where `2` and `1` look like unknown options.
_SORT_VALUE_SHORT: frozenset[str] = frozenset("ktSTo")
_SORT_READONLY_LONG: frozenset[str] = frozenset(
    (
        "--ignore-leading-blanks",
        "--dictionary-order",
        "--ignore-case",
        "--general-numeric-sort",
        "--ignore-nonprinting",
        "--month-sort",
        "--human-numeric-sort",
        "--numeric-sort",
        "--random-sort",
        "--reverse",
        "--sort",
        "--version-sort",
        "--batch-size",
        "--check",
        "--debug",
        "--key",
        "--merge",
        "--stable",
        "--buffer-size",
        "--field-separator",
        "--parallel",
        "--unique",
        "--zero-terminated",
        "--help",
        "--version",
    )
)


# `date`'s read-only surface. Enumerated from GNU coreutils `date --help`, then
# checked for a second axis the other accept-lists did not have to face: whether the
# same LETTER means different things in different `date` implementations.
#
# `-d` IS on the list, and the reason it is here is worth recording because an earlier
# revision of this change left it OFF on the belief that BSD/macOS `date -d` sets the
# kernel's daylight-saving value. That belief came from documentation, not from the
# implementations, and checking the implementations showed it is false on every
# platform that ships today. Read from each project's own `getopt(3)` string:
#
#   GNU coreutils      `-d STRING`  parses and PRINTS  (verified by execution, 8.22)
#   FreeBSD  bin/date  "f:I::jnRr:uv:z:"   no `d` at all -> invalid option
#   Apple    shell_cmds/date  "f:I::jnRr:uv:z:"   no `d` at all -> invalid option
#   OpenBSD  bin/date  "af:jr:uz:"         no `d` at all -> invalid option
#   NetBSD   bin/date  "ad:f:jnRr:Uuz:"    `-d` sets rflag and parsedate()s optarg,
#                                          i.e. the GNU meaning: a reference time to
#                                          PRINT. `setthetime()` is reached only from
#                                          a bare operand, never from `-d`.
#
# So `-d` either reads or errors, never writes. The historical `-d dst` that set the
# kernel daylight-saving flag is gone from every current BSD.
#
# There was also an internal tell that should have caught this without the source
# dive: `--date=` was already on the read-only long list, and `-d` is the same option
# under a shorter spelling on every implementation that has it. Admitting one and
# refusing the other could not both be right.
#
# `-s`/`--set` IS the setter GNU shares, verified accepted and failing only on
# privilege ("date: cannot set date: Operation not permitted"). `-f`, `-r`, `-I` and
# `-d` take values, which is what makes `-Iseconds`, `-r FILE` and `-d yesterday` read
# cleanly while `-s` is refused -- the dilemma that kept `-s` out of the old
# write-flag table.
#
# The other three accept-lists were swept for divergence and need no change: BSD
# `sort`'s writers are `-o`/`-T` and BSD `file`'s is `-C`, all already excluded, and
# BSD `hostname` offers only `-f`/`-s` plus a name OPERAND, which `operands="none"`
# already refuses. BSD `date`'s other setters (`-t` minutes west, `-j`, `-n`, `-v`)
# are likewise absent from this list, so they fail closed already.
_DATE_READONLY_SHORT: frozenset[str] = frozenset("dfIrRu")
_DATE_VALUE_SHORT: frozenset[str] = frozenset("dfIrs")
_DATE_READONLY_LONG: frozenset[str] = frozenset(
    (
        "--date",
        "--file",
        "--iso-8601",
        "--reference",
        "--rfc-2822",
        "--rfc-3339",
        "--universal",
        "--utc",
        "--help",
        "--version",
    )
)
# Long and short forms that consume the NEXT token, so an operand count is not
# fooled by a flag's value. `-I` is absent on purpose: its TIMESPEC is optional and
# must be attached (`-Iseconds`), so `-I` never eats the following word.
_DATE_VALUE_FLAGS: frozenset[str] = frozenset(
    ("-d", "-f", "-r", "-s", "--date", "--file", "--reference", "--set")
)

# `hostname`'s surface, from its own `--help`. Tiny and fully enumerable, which is
# why a positive list is cheap here. `-b/--boot` and `-F/--file` SET the name with
# no operand (both verified: privilege-only failure, with `-F` re-tested against a
# file that EXISTS -- against a missing path it fails at open() and looks read-only).
_HOSTNAME_READONLY_SHORT: frozenset[str] = frozenset("aAdfiIsyVh")
_HOSTNAME_VALUE_SHORT: frozenset[str] = frozenset("F")
_HOSTNAME_READONLY_LONG: frozenset[str] = frozenset(
    (
        "--alias",
        "--all-fqdns",
        "--all-ip-addresses",
        "--domain",
        "--fqdn",
        "--ip-address",
        "--long",
        "--nis",
        "--short",
        "--yp",
        "--help",
        "--version",
    )
)


# `file`'s surface, from its own `--help`. It reached an accept-list rather than a
# `-C` denylist entry because that is the shape this change keeps converging on: an
# unlisted option prompts instead of passing, so a flag missing from the help text
# costs a prompt rather than a write. `git blame --textconv` is the reason that
# distinction is not academic.
#
# `-C/--compile` is the setter: with `-m FILE` it compiles that magic file and writes
# `FILE.mgc` beside it (verified, 464 bytes). `-z/--uncompress` is ALSO excluded, on
# the omission-is-cheap principle rather than a measured escape -- libmagic can shell
# out to an external decompressor for formats it does not handle internally, and the
# flag is rare enough that a prompt costs nothing. Everything else prints.
# `f`/`--files-from` is absent, and for a different reason than `-C`: it does not
# write, it INDIRECTS. `file -f LIST` opens every path named inside LIST, and those
# paths never appear in the command, so the hook layer's path gates
# (`is_sensitive_path` / `is_sensitive_bash_command`, applied to the command text) see
# only LIST and cannot see what is actually read. A guard that inspects argv is blind
# to one more level of indirection, so the option has to go rather than the guard get
# cleverer. `sort --files0-from` was already excluded for the same shape; `hostname -F`
# is already refused as a setter.
#
# Kept: `-m/--magic-file`, `-e/--exclude`, `-F/--separator` all take a value, but the
# value IS the path or string being used, visible in argv, so the guards can act on it.
# There is no indirection to hide behind.
#
# `p`/`--preserve-date` is absent too, and it is the subtlest of the three exclusions.
# It LOOKS read-only because it RESTORES the access time rather than setting a caller
# chosen one -- which is how it was originally, and wrongly, admitted here. Restoring
# still requires a `utimes()` call on the named path, and the `ctime` that call bumps is
# NOT restorable. So the option erases the evidence that a file was read while leaving a
# permanent metadata modification behind: the wrong side of read-only in both directions.
#
# MEASURED, because `noatime` on this box hides the atime effect entirely and made the
# obvious test inconclusive. `ctime` advances on any inode metadata write and is visible
# whatever the mount options are:
#
#   file t.txt            -> ctime unchanged   (control)
#   file -b t.txt         -> ctime unchanged   (control)
#   file -p t.txt         -> ctime ADVANCED
#   file --preserve-date  -> ctime ADVANCED
#
# The same probe was then run over every other accept-list flag that opens a named file
# -- `file -m/-k/-L/-s/-r`, `sort`, `sort -u`, `sort -k1`, `date -r`, `date -f`, plus
# `cat` and `wc -l` as controls -- and all twelve are clean. `-p` is the only one.
#
# `-z`/`--uncompress` is also absent, and it belongs to a class this module already
# names elsewhere rather than to the write-flag class. From `file`'s own
# `src/compress.c`, the decompressor is SPAWNED:
#
#     status = posix_spawnp(&pid, compr[method].argv[0], &fa, NULL, ...)
#
# with `compr[]` holding `"gzip"`, `"bzip2"`, `"lzip"`, `"xz"`, `"lrzip"`, `"zstd"` and
# `method` selected from the examined file's magic bytes. So `-z` runs a program whose
# NAME is chosen by the content being inspected, which is the same hand-off as
# `git diff --ext-diff`. Stated because it is a behaviour change: on the write-flag
# table `file -z` auto-approved, and under this list it prompts.
_FILE_READONLY_SHORT: frozenset[str] = frozenset("vmbceFiklLhnN0rsd")
# `f` stays here so `-f LIST` and `-fLIST` are both recognised as flag-plus-value and
# refused, rather than `LIST` being mistaken for an operand.
_FILE_VALUE_SHORT: frozenset[str] = frozenset("mefF")
_FILE_READONLY_LONG: frozenset[str] = frozenset(
    (
        "--apple",
        "--brief",
        "--checking-printout",
        "--debug",
        "--dereference",
        "--exclude",
        "--keep-going",
        "--list",
        "--magic-file",
        "--mime",
        "--mime-encoding",
        "--mime-type",
        "--no-buffer",
        "--no-dereference",
        "--no-pad",
        "--print0",
        "--raw",
        "--separator",
        "--special-files",
        "--help",
        "--version",
    )
)
# `file` needs no VALUE-FLAG set: `spec.value_flags` is read only by `_operands`,
# which is behind an early return for `operands == "any"`, and `file`'s operands are
# the files it identifies. A set here would look load-bearing and never be read.


class _AcceptSpec(NamedTuple):
    """A tool's read-only surface, stated positively.

    One registry rather than three bespoke checks, because the algorithm turned out
    identical for every tool that needed it. `sort` had this shape first; `date` and
    `hostname` arrived at it for the same reason -- a per-tool DENYLIST had already
    leaked on each of them, and an accept-list is closed by construction instead.
    """

    reason_fmt: str  # carries `{tok}`
    readonly_short: frozenset[str]
    value_short: frozenset[str]
    readonly_long: frozenset[str]
    operands: str  # "any" (they are inputs) | "none" | "plus" (only +FORMAT)
    operand_reason: str
    value_flags: frozenset[str]  # for operand counting


_OPTION_ACCEPT_LISTS: dict[str, _AcceptSpec] = {
    "sort": _AcceptSpec(
        reason_fmt="pipe target 'sort {tok}' is not a recognised read-only option",
        readonly_short=_SORT_READONLY_SHORT,
        value_short=_SORT_VALUE_SHORT,
        readonly_long=_SORT_READONLY_LONG,
        # sort's operands are input FILES, which it reads.
        operands="any",
        operand_reason="",
        value_flags=frozenset(),
    ),
    "date": _AcceptSpec(
        # The reason names the accepted spelling because `date -d` is a form agents
        # emit constantly, and a refusal that only says no turns every one of them
        # into a human prompt instead of a self-serve retry.
        reason_fmt=(
            "'date {tok}' is not a recognised read-only option; "
            "'--date=<expr>' is the read-only spelling"
        ),
        readonly_short=_DATE_READONLY_SHORT,
        value_short=_DATE_VALUE_SHORT,
        readonly_long=_DATE_READONLY_LONG,
        # `date 08221200` sets the clock (verified: privilege-only failure). A `+`
        # operand is the output FORMAT and only prints.
        operands="plus",
        operand_reason=("'date <operand>' sets the system clock unless it is a +FORMAT string"),
        value_flags=_DATE_VALUE_FLAGS,
    ),
    "file": _AcceptSpec(
        reason_fmt="'file {tok}' is not a recognised read-only option",
        readonly_short=_FILE_READONLY_SHORT,
        value_short=_FILE_VALUE_SHORT,
        readonly_long=_FILE_READONLY_LONG,
        # `file`'s operands are the FILES it identifies, which it only reads.
        operands="any",
        operand_reason="",
        value_flags=frozenset(),
    ),
    "hostname": _AcceptSpec(
        reason_fmt="'hostname {tok}' is not a recognised read-only option",
        readonly_short=_HOSTNAME_READONLY_SHORT,
        value_short=_HOSTNAME_VALUE_SHORT,
        readonly_long=_HOSTNAME_READONLY_LONG,
        operands="none",
        operand_reason=("'hostname <operand>' sets the hostname; every read form is flag-only"),
        value_flags=frozenset(("-F", "--file")),
    ),
}

#: Keys whose verdict depends on the COUNT or ORDER of words rather than on which
#: flags are present, so a word bash deletes can flip it. See `_ELISION_SENSITIVE_RE`
#: for the measurements and for why the refusal is scoped here instead of applied to
#: every command.
#:
#: Derived from the tables that carry those decisions, so a tool added to the accept
#: list with an operand rule joins this set without a second edit. `git remote` and
#: `uniq` are named: their decision is positional in the walk itself (first
#: non-option word is the subcommand; second operand is the output file) rather than
#: expressed in a table this can read.
_ELISION_SENSITIVE_KEYS: frozenset[str] = (
    frozenset(f"git {subcommand}" for subcommand in _GIT_REF_WRITE_FLAGS)
    | frozenset(("git remote", "uniq"))
    | frozenset(verb for verb, spec in _OPTION_ACCEPT_LISTS.items() if spec.operands != "any")
)


def _option_accept_list_violation(prefix: str, tokens: list[str]) -> str:
    """Reason *tokens* leave *prefix*'s positively-vetted read-only surface, else "".

    Deny-by-default per tool: an option has to be RECOGNISED as read-only, so an
    unlisted one prompts instead of passing. That is what makes this closed by
    construction where a write-flag denylist was not -- a spelling nobody thought of
    is refused rather than admitted.

    A long flag must match EXACTLY, which disposes of getopt_long abbreviation for
    free: `--out` is an abbreviation of `--output` and simply is not in the read-only
    set. The cost is that an abbreviation of a read-only flag (`--rev` for
    `--reverse`) also prompts.
    """
    spec = _OPTION_ACCEPT_LISTS[prefix]
    # `--` does not stop this loop either. HARDENING rather than a fix here: measured,
    # every value-taking read flag of this box's `sort` REJECTS `--` as its value and
    # aborts (`-k` "invalid number", `-S` "invalid -S argument '--'", `-t`
    # "multi-character tab"), so `sort -k -- -o OUT` writes nothing today. That is
    # sort's argument validation saving us, not this classifier, and it is not a
    # property worth depending on -- the git path above proved the same shape does
    # write when the tool is more permissive. Cost is a prompt on an input FILE named
    # like an option (`sort -- -o`).
    for token in tokens:
        if not token.startswith("-") or token == "-":
            continue  # operand, or `-` for stdin
        if _GLOB_META_RE.search(token):
            # An option-shaped token whose real spelling the shell has not produced
            # yet. No legitimate option contains a glob metacharacter, so this costs
            # nothing, and an operand glob is untouched: it has no leading dash.
            return spec.reason_fmt.format(tok=token)
        if token.startswith("--"):
            if token.partition("=")[0] not in spec.readonly_long:
                return spec.reason_fmt.format(tok=token)
            continue
        for letter in token[1:]:
            if letter in spec.value_short:
                # This option takes a value, so the remainder of the token is that
                # value and carries no further option letters.
                if letter not in spec.readonly_short:
                    return spec.reason_fmt.format(tok=f"-{letter}")
                break
            if letter not in spec.readonly_short:
                return spec.reason_fmt.format(tok=f"-{letter}")
    if spec.operands == "any":
        return ""
    operands = _operands(tokens, spec.value_flags)
    if spec.operands == "none" and operands:
        return spec.operand_reason
    if spec.operands == "plus" and any(not o.startswith("+") for o in operands):
        return spec.operand_reason
    return ""


def _operands(args: list[str], value_flags: frozenset[str] = frozenset()) -> list[str]:
    """Operand tokens in *args*, honouring the `--` terminator.

    Before the terminator a leading-dash word is an option; after it EVERY word
    is an operand however it is spelled. That second half is what
    `uniq -- input -pwned` turned on: counting only the non-dash words saw one
    operand and passed a segment that writes `-pwned`.
    """
    if "--" in args:
        at = args.index("--")
        before, after = args[:at], args[at + 1 :]
    else:
        before, after = args, []
    out: list[str] = []
    previous = ""
    for tok in before:
        if tok.startswith("-"):
            # A short option consumes the NEXT word only when the token is the bare
            # flag; `-Iseconds` carries its own value, so treating it as `-I` plus a
            # separate operand would deny an ordinary read.
            previous = tok if tok in value_flags else ""
            continue
        if previous:
            previous = ""
            continue
        out.append(tok)
    return out + after


#: Shell expansions whose RESULT is the argument, while ``shlex`` hands this
#: module the unexpanded text. Every check here is keyed on the token, so where
#: the two disagree the guard inspects one string and the program receives
#: another:
#:
#:     git diff $'--output=/tmp/pwned'       shlex: `$--output=…`     bash: `--output=…`
#:     git diff $"--output=/tmp/pwned"       shlex: `$--output=…`     bash: `--output=…`
#:     git diff ${HOME:+--output=/tmp/pwned} shlex: literal           bash: `--output=…`
#:     git remote se${x}t-url …              shlex: `se${x}t-url`     bash: `set-url`
#:     git diff --{out,out}put=/tmp/pwned    shlex: `--{out,out}put=` bash: `--output=…`
#:
#: Matched as ONE class rather than one spelling at a time. Closing ``$'`` alone
#: left ``$"`` (locale translation) and ``${…}`` (parameter expansion) open on the
#: identical path, and the remaining forms are bounded only by bash's grammar.
#: Un-expanding them here would mean reimplementing that grammar, so a segment
#: carrying one is refused instead: a read-only command has no need of any of
#: them, and a rejected segment falls through to the human approval prompt.
#:
#: Brace expansion belongs to the same class even though it carries no ``$``: it
#: is performed FIRST, before any other expansion, and it can assemble a flag out
#: of fragments that match nothing here. Only the forms bash actually expands are
#: matched — a comma list or a ``..`` range — so a lone ``{`` (a JSON argument, a
#: Go template) is left alone.
#:
#: ``$(…)`` and backticks are already refused upstream by ``_UNSAFE_SHELL_RE``;
#: this covers what that pattern does not reach.
#:
#: Positional and special parameters (``$1``, ``$@``, ``$*``, ``$?``, ``$$``,
#: ``$!``, ``$#``, ``$-``) belong to the same class and are matched by their own
#: alternative. Their NAME is not an identifier, so the ``$[A-Za-z_]`` branch
#: above never saw them, and in a `bash -c` string with no positional arguments
#: ``$@`` and ``$*`` expand to NOTHING — which is what makes them the sharpest
#: spelling here rather than a curiosity:
#:
#:     git remote $@set-url origin …   shlex: `$@set-url`      bash: `set-url`
#:     git diff $1--output=/tmp/pwned  shlex: `$1--output=…`   bash: `--output=…`
#:
#: Matched on the raw segment, so a QUOTED occurrence is refused too even though
#: bash would not expand it (``grep '*.{js,ts}' f``). That is the same trade the
#: ``$`` forms already make, and it errs toward the prompt.
#:
#: Applied only to a GUARDED verb (see ``_side_effect_reason``). A verb this
#: module has no table for cannot have a decision subverted by a hidden word,
#: so ``cat $HOME/.bashrc`` and ``head -20 $LOG`` — the ordinary reads — stay on
#: the auto-approve path.
_SHELL_EXPANSION_RE = re.compile(
    r"\$['\"{]"
    r"|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|\$[0-9@*#?$!\-]"
    r"|\{[^{}\s]*,[^{}\s]*\}"
    r"|\{[^{}\s]*\.\.[^{}\s]*\}"
)
#: Pathname-expansion metacharacters. NOT part of the class above, because a glob
#: is usually the argument itself in a read-only command (`ls *.py`) — it is
#: refused only in the positions where the spelling is what gets classified. See
#: the note in `_side_effect_reason`.
#:
#: A leading `~` is deliberately NOT here: tilde expansion yields a path starting
#: with `/`, so it cannot synthesize a flag or a subcommand.
_GLOB_META_RE = re.compile(r"[*?\[]")

#: Bash EXTGLOB operators, which synthesize a token the same way an ordinary glob
#: does — `git diff @(--output=pwned)` matches a file of that name and git writes
#: it. Reported by #5038, which measured it.
#:
#: These get their own regex and their own verdict because `fnmatch` — the test
#: that makes the plain-glob case precise — does not implement extglob: it reads
#: `@(` as two literal characters, so `fnmatch("--output", "@(--output")` is False
#: and the pattern that reaches the flag looks inert. Nothing can be proven about
#: an extglob token here, so a guarded verb refuses it outright. That is the same
#: trade the `$`-led forms make, and it costs nothing: unlike a plain glob, an
#: extglob has no ordinary use in a read-only command.
#:
#: Extglob is off by default in a non-interactive `bash -c`, so reaching this needs
#: `shopt -s extglob` (or a `BASHOPTS` carrying it) AND a matching file — narrower
#: than the plain-glob case, closed here because it is the same cause.
_EXTGLOB_RE = re.compile(r"[?*+@!]\(")

#: Every word this module decides on: the flags of all four tables, the
#: ``git remote`` write subcommands, and the option terminator. A glob is
#: dangerous exactly when the filesystem can hand the program one of THESE in
#: place of the pattern, so the test is ``fnmatch`` against this set rather than
#: "the token contains a metacharacter" — which would have taken `ls *.py` and
#: `git diff *.py` off the read-only path for no gain.
_GLOB_SENSITIVE_WORDS: frozenset[str] = (
    frozenset(
        flag
        for table in (
            _WRITE_FLAGS,
            _EXEC_FLAGS,
            _GIT_REF_WRITE_FLAGS,
            # Derived, not restated: this is the whole reason `wc --file*` is
            # refused. A checkout containing a file named `--files0-from=payload`
            # turns that pattern into the flag, and measured, `wc --file*` then read
            # a path that appears nowhere in the command. Listing the flag in one
            # table and having the glob defence read that table is what keeps the
            # two from drifting -- the same coupling that broke when `sort` moved
            # off the denylist.
            _INDIRECT_LIST_FLAGS_BY_PREFIX,
        )
        for flags in table.values()
        for flag in flags
    )
    | _GIT_REMOTE_WRITE_SUBCOMMANDS
    # A glob that expands to `--` shifts every following word into operand
    # position, which is how the terminator changes what the walk below decides.
    | frozenset(("--",))
    # The accept-listed tools have no denylist to derive from, but they do not need
    # one: a letter that TAKES A VALUE and is not READ-ONLY is refused by the
    # registry by construction, so it is precisely a word a glob must not reach.
    # For `sort` that yields `-o` and `-T`. Without this, moving a tool to a positive
    # list dropped it out of this set -- measured, `cat f | sort ?uo victim` was
    # auto-approved because `-o` had stopped being a sensitive word.
    | frozenset(
        f"-{letter}"
        for spec in _OPTION_ACCEPT_LISTS.values()
        for letter in spec.value_short - spec.readonly_short
    )
)

#: Verbs whose OWN tables carry a short flag, so a glob can expand into a bundled
#: cluster for them (``?uo`` -> ``-uo``, which supplies ``-o``). A cluster is not
#: a word in the set above, so it takes the extra test in `_glob_hides_word` —
#: and only here, which is what keeps `git diff *.py` (long flags only) passing.
#: Derived from the tables so the two cannot drift apart.
_SHORT_FLAG_VERBS: frozenset[str] = frozenset(
    key
    for table in (_WRITE_FLAGS, _EXEC_FLAGS)
    for key, flags in table.items()
    if any(len(flag) == 2 and flag[0] == "-" for flag in flags)
) | frozenset(
    verb for verb, spec in _OPTION_ACCEPT_LISTS.items() if spec.value_short - spec.readonly_short
)


def _glob_hides_word(token: str, has_short_flags: bool) -> bool:
    """Whether *token*'s glob can expand into a word this module decides on.

    Two shapes, because a pattern reaches a flag two different ways:

    * it matches a decided word outright — ``s?t-url`` matches ``set-url``,
      ``--outp?t`` matches ``--output``, ``?o`` matches ``-o``, and a bare ``*``
      matches every one of them. ``fnmatchcase`` answers this exactly, so a
      pattern that CANNOT reach one (``*.py``) is left alone;
    * its metacharacter is the FIRST character, so the filesystem chooses the
      leading character too and the expansion can be a short-option CLUSTER
      (``?uo`` -> ``-uo``, which :func:`_matched_flag` reads as supplying ``-o``).
      A cluster is not a word in the set above, so it needs its own test — but
      only where the verb HAS a short flag to be bundled into, which keeps
      ``git diff *.py`` (long flags only) passing.

    An EXTGLOB token short-circuits to True: ``fnmatch`` cannot model extglob, so
    neither shape below can rule on one. See `_EXTGLOB_RE`.
    """
    if _EXTGLOB_RE.search(token):
        return True
    if not _GLOB_META_RE.search(token):
        return False
    head = token.split("=", 1)[0]
    # A token that already LOOKS like an option is refused on the metacharacter
    # alone, without asking what it can match. `fnmatch` answers "can this reach a
    # decided word", and a short-option CLUSTER is not one of those words, so
    # `sort -u? victim` slipped: no candidate is three characters long, the
    # metacharacter is not first so the cluster test below does not fire, and bash
    # resolves `-u?` against a file named `-uo` — which `_matched_flag` would have
    # rejected had it ever seen it. Nothing legitimate is lost, because the head is
    # the flag NAME: a glob in a flag's VALUE is split off above, which is what
    # keeps `git log --grep=[abc]` a read.
    if token.startswith("-") and _GLOB_META_RE.search(head):
        return True
    # Every word this module decides on, PLUS every abbreviation of a long one,
    # because `_matched_flag` resolves an abbreviation and so does the parser it
    # guards. Testing only the full spellings left `git diff ??out=victim`
    # auto-approved: `fnmatch("--output", "??out")` is False on the length alone,
    # `git diff`'s table is long-only so the cluster arm below does not fire, and
    # bash resolves `??out` against a file named `--out` that git then reads as
    # `--output`. The full spelling `??output` was already refused, which is what
    # made the gap look closed.
    if any(_glob_reaches(head, word) for word in _GLOB_SENSITIVE_WORDS):
        return True
    return has_short_flags and _GLOB_META_RE.match(token) is not None


def _glob_reaches(head: str, word: str) -> bool:
    """Whether glob *head* can expand to *word* or to an abbreviation of it.

    A long option is abbreviable to any unambiguous prefix, and an ambiguous one
    is rejected by the tool rather than run — so every prefix of `--` plus one
    character is tested, and over-matching can only add a prompt.

    Compared CASE-INSENSITIVELY, because `nocaseglob` decouples the pattern's case
    from the filename's: with it set, `git diff ??OUT=victim` expands to
    `--out=victim` and git writes the file, while a case-sensitive test saw a
    pattern matching nothing. Measured — `bash -O nocaseglob -c 'echo git diff
    ??OUT=victim'` prints `git diff --out=victim`, and plain `bash -c` does not.

    The case sensitivity this module DOES rely on is elsewhere and unaffected:
    `_matched_flag` still distinguishes `file -C` (compiles a magic file) from
    `file -c` (prints one), because that reads a literal token rather than asking
    what a pattern could become.
    """
    folded = head.lower()
    lowered = word.lower()
    if fnmatch.fnmatchcase(lowered, folded):
        return True
    if not word.startswith("--") or len(word) <= 3:
        return False
    return any(fnmatch.fnmatchcase(lowered[:cut], folded) for cut in range(3, len(word)))


def _matched_flag(tokens: list[str], flags: tuple[str, ...]) -> str:
    """Return the first token in *tokens* that supplies one of *flags*.

    Matches the flag on its own (``-o``), joined to its value (``--output=x``)
    and bundled into a short-option cluster (``-uo`` supplies ``-o``), so the
    check cannot be stepped around by respelling the same flag.
    """
    shorts = {f[1] for f in flags if len(f) == 2 and f[0] == "-"}
    longs = [f for f in flags if f.startswith("--") and len(f) > 2]
    for tok in tokens:
        if tok in flags:
            return tok
        for flag in flags:
            if tok.startswith(flag + "="):
                return flag
        # A GNU long option may be ABBREVIATED to any unambiguous prefix, so
        # `--out=FILE` and `--outp=FILE` reach the same `--output` that exact
        # matching missed. Accept any prefix of a known flag that is at least
        # `--` plus one character: the parser this guards resolves it, so the
        # guard has to as well. Over-matching here can only add a prompt.
        head = tok.split("=", 1)[0]
        if head.startswith("--") and len(head) > 2:
            for flag in longs:
                if flag.startswith(head):
                    return flag
        if shorts and len(tok) > 1 and tok[0] == "-" and tok[1] != "-":
            for ch in tok[1:]:
                if ch in shorts:
                    return "-" + ch
    return ""


def _side_effect_reason(segment: str) -> str:
    """Reason *segment* has a side effect, despite naming a read-only verb.

    Returns "" when the segment is genuinely read-only. Called after the verb
    has cleared the allowlist, because the allowlist only decides *which
    program* runs — not what the rest of the command line asks it to do.
    """
    try:
        # Discard-only redirects are scrubbed first, mirroring the unsafe-shell
        # check upstream, because the exact-match rule below would otherwise read
        # one as a trailing operand. `java -version 2>&1` is the canonical probe —
        # java prints its version to stderr — so counting `2>&1` as an operand
        # would deny the single most common read on this path.
        tokens = shlex.split(_DEVNULL_REDIR_RE.sub(" ", segment))
    except ValueError:
        # Cannot recover argv, so cannot vouch for the operands.
        return "quoting cannot be resolved"
    if not tokens:
        return ""
    # A version probe acts on an operand, so its entry matches EXACTLY: the
    # allowlist named `javac -version`, the prefix match vouched for everything
    # after it, and javac compiled it. See `_EXACT_ONLY_BASH_PREFIXES`.
    spelled = " ".join(tokens).lower()
    for probe in _EXACT_ONLY_BASH_PREFIXES:
        if spelled.startswith(probe + " "):
            return f"'{probe}' takes no operand, and acts on one when given it"
    # The verb is matched case-insensitively, like the allowlist does, so an
    # unusual spelling cannot step past the table. Flags keep their case,
    # because for these programs the case carries the meaning.
    verb = tokens[0].rsplit("/", 1)[-1].lower()
    args = tokens[1:]

    # Checked on the RAW segment, before any table lookup, because the thing being
    # guarded against is a word that reached `shlex` but will not reach the program.
    elision_key = f"git {args[0].lower()}" if verb == "git" and args else verb
    if elision_key in _ELISION_SENSITIVE_KEYS and _ELISION_SENSITIVE_RE.search(segment):
        return f"a word bash removes could change what '{elision_key}' does"

    # Whether an unexpanded word can subvert THIS segment's classification.
    #
    # Every check below is keyed on a table, so a verb with no table has no
    # decision to subvert: whatever `cat $HOME/.bashrc` expands to, this
    # function was always going to return "". Refusing an expansion there buys
    # nothing and costs the most ordinary read on the auto-approve path, so the
    # refusal is scoped to the verbs whose arguments this module reads.
    #
    # `git` is guarded whatever the subcommand, because the subcommand itself is
    # a decided word: `git $x` reaches bash as `git branch -D release`.
    #
    # `hostname` and `date` are guarded through `_OPTION_ACCEPT_LISTS` rather than a
    # write-flag table, because their rule is an OPERAND rule: an unexpanded word IS
    # the decision there, so `hostname $EVIL` renames the host under a spelling this
    # module read as harmless.
    guarded = (
        verb in ("git", "uniq")
        or verb in _WRITE_FLAGS
        or verb in _EXEC_FLAGS
        or verb in _OPTION_ACCEPT_LISTS
        # A verb whose arguments this module reads for an INDIRECTION or a pager
        # startup command has a decision to subvert just as much as one with a
        # write-flag table, so it belongs in the same guard.
        or verb in _INDIRECT_LIST_FLAGS_BY_PREFIX
        or verb in _PAGER_STARTUP_VERBS
    )

    # ANSI-C quoting is stripped by `shlex` but honoured by bash, so the token
    # this check inspects is not the token the shell runs: `git diff $'-o'` reaches
    # `shlex` as `$-o` — matching no flag — while bash passes `-o`. The same trick
    # hides a subcommand (`git remote $'set-url'`), and a positional or special
    # parameter does it with no quoting at all — `git remote $@set-url …`, where
    # `$@` expands to nothing in a `bash -c` string. It is a spelling with no
    # legitimate use in a read-only command, so the segment is refused outright
    # rather than un-quoted here, which would mean reimplementing bash's rules.
    if guarded and _SHELL_EXPANSION_RE.search(segment):
        return "a shell expansion hides the real argument"

    # Pathname expansion cannot be refused wholesale: a glob usually IS the
    # argument — `ls *.py`, `grep -rn TODO src/*` — so the question is whether
    # THIS pattern can reach a word this module decides on. `_glob_hides_word`
    # answers it with `fnmatch`, which is what keeps the ordinary forms passing:
    #
    #     git remote s?t-url origin https://evil   (a file named `set-url` nearby)
    #     git diff --outp?t=/tmp/pwned
    #     cat f | sort ?o victim                   (a file named `-o` nearby)
    #
    # The last one is why a leading-dash test is not enough. `?o` does not start
    # with `-`, so a test keyed on the spelling skipped it, bash resolved it to
    # `-o`, and `sort` truncated `victim` under an auto-approval.
    if guarded:
        has_short_flags = verb in _SHORT_FLAG_VERBS or (
            verb == "git" and bool(args) and args[0].lower() in _GIT_REF_WRITE_FLAGS
        )
        for token in args:
            if _glob_hides_word(token, has_short_flags):
                return "a glob could expand into a flag or subcommand"

    # The allowlist names `git <subcommand>`, so that is the unit to key on.
    key = verb
    if verb == "git" and args:
        subcommand = args[0].lower()
        key = f"git {subcommand}"
        args = args[1:]

        if subcommand in _GIT_REF_WRITE_FLAGS:
            hit = _matched_flag(args, _GIT_REF_WRITE_FLAGS[subcommand])
            if hit:
                return f"'git {subcommand} {hit}' changes a ref"
            # A bare operand names a ref to create, unless the command is in list
            # mode (where it is a pattern) or the operand is a required flag value.
            #
            # List mode is decided over the whole argument list, because the
            # selecting flag can come after the operand it makes into a pattern:
            # `git branch newbranch --list` is still a list. It must stop at `--`,
            # though: after the terminator a word spelled like a flag is an
            # operand, so `git tag -- --list` CREATES the ref `--list` while
            # reading that `--list` as list mode passed it off as a read.
            options = args[: args.index("--")] if "--" in args else args
            list_shorts = _GIT_REF_LIST_SHORTS.get(subcommand, "")
            # A required flag's VALUE is not an option, however it is spelled. git
            # takes it from the following word, so `git branch --format -l newbranch`
            # hands `-l` to `--format` and never sees a list flag — while scanning
            # every token read that `-l` as one, and the bare operand it then
            # licensed created the branch. The walk below already tracks this for
            # operands; list mode has to track it too, over the same tokens.
            selectors: list[str] = []
            consumed = ""
            for tok in options:
                if _consumes_next_word(consumed):
                    # A glob HERE decides by COUNT, not by what it becomes: under
                    # `nullglob` an unmatched pattern vanishes, so the flag eats the
                    # NEXT word instead and every later position shifts by one.
                    # `git branch --format nomatch* --list newbranch` loses the
                    # format's value, `--format` takes `--list`, and `newbranch`
                    # stops being a pattern. See `_glob_shifts_arguments`.
                    if _glob_shifts_arguments(tok):
                        return "a glob in a required option's value shifts the arguments"
                    consumed = ""
                    continue
                if tok.startswith("-"):
                    # An ATTACHED value takes nothing from the next word.
                    consumed = "" if "=" in tok else tok
                    selectors.append(tok)
                    continue
                consumed = ""
            list_mode = any(
                tok.split("=", 1)[0] in _GIT_REF_LIST_FLAGS
                or (
                    len(tok) > 1
                    and tok[0] == "-"
                    and tok[1] != "-"
                    # EVERY character of the cluster must be a list letter or a
                    # digit, not merely one of them. `any` read an attached VALUE
                    # as part of the cluster, which is the same trap the note on
                    # the accept-list registry records for `date -Iseconds`: the `l` in
                    # `git tag -ulin@kiro.co` selected list mode and the bare
                    # operand it then licensed created a signed tag. A digit is
                    # allowed because `-n` carries an optional count (`-n5`).
                    #
                    # A MIXED cluster (`-lv`) is no longer a listing here and falls
                    # through to the prompt. That is the intended trade: the letter
                    # this cannot distinguish from a value is exactly the letter a
                    # write flag arrives on, and the ordinary spellings — a separate
                    # `-l`, `--list`, or `-n5` — are unaffected.
                    and all(ch in list_shorts or ch.isdigit() for ch in tok[1:])
                )
                for tok in selectors
            )
            # A `--no-list` anywhere in the span undoes it, and the operand it was
            # protecting becomes a ref to create. Applied AFTER the scan and
            # unconditionally, rather than as git's last-wins: cancelling can only
            # move an operand toward the prompt, so being coarse here is the safe
            # direction. See `_GIT_REF_LIST_CANCEL_FLAGS` for what was measured.
            if list_mode and any(_cancels_list_mode(tok, subcommand) for tok in selectors):
                list_mode = False
            previous = ""
            operand_only = False
            for tok in args:
                # `--` ends the options. Everything after it is an operand, however
                # it is spelled: `git tag -- -z` creates the tag `-z`, while a
                # leading-dash test read it as one more option and passed. A SECOND
                # `--` is itself an operand, so the terminator is consumed once.
                if tok == "--" and not operand_only:
                    operand_only = True
                    previous = ""
                    continue
                if not operand_only and tok.startswith("-"):
                    # An ATTACHED value (`--sort=x`) takes nothing from the next
                    # word, so it must not mark the following operand as consumed.
                    previous = "" if "=" in tok else tok
                    continue
                if _consumes_next_word(previous):
                    previous = ""
                    continue
                if list_mode:
                    continue
                return f"'git {subcommand} {tok}' creates a ref"

        if subcommand == "remote":
            # `git remote -v set-url …` puts an option BEFORE the subcommand, and
            # git accepts it there. Keying on `args[0]` therefore saw `-v` and let
            # the mutation through, so the leading options are skipped and the
            # first non-option word is the subcommand — the same token git uses.
            for tok in args:
                if tok.startswith("-"):
                    continue
                if tok in _GIT_REMOTE_WRITE_SUBCOMMANDS:
                    return f"'git remote {tok}' rewrites remote configuration"
                # A glob HERE, even one that reaches no decided word, because this
                # loop stops at the first non-option token and `nullglob` can make
                # a token VANISH: with it exported, `git remote nomatch* set-url
                # origin …` loses `nomatch*` entirely and git receives `set-url` —
                # while this loop broke on the pattern and never looked further.
                #
                # `_glob_hides_word` above cannot cover it: that test asks whether
                # the pattern can EXPAND INTO a decided word, and `nomatch*` cannot
                # — the mutation comes from the token disappearing, not from what it
                # becomes. Removing this check on the grounds that the general test
                # subsumed it is what opened the hole.
                if _GLOB_META_RE.search(tok) or _EXTGLOB_RE.search(tok):
                    return "a glob in the subcommand hides the real argument"
                # Likewise an expansion: `guarded` refuses those for `git` before
                # this point, so reaching here with one is impossible — but the
                # subcommand position is load-bearing enough to state rather than
                # infer.
                if _SHELL_EXPANSION_RE.search(tok):
                    return "a shell expansion hides the real argument"
                break

    hit = _matched_flag(args, _WRITE_FLAGS.get(key, ()))
    if hit:
        return f"'{key} {hit}' writes a file"
    hit = _matched_flag(args, _EXEC_FLAGS.get(key, ()))
    if hit:
        return f"'{key} {hit}' runs a program named by the repository"

    hit = _matched_flag(args, _INDIRECT_LIST_FLAGS_BY_PREFIX.get(key, ()))
    if hit:
        return f"'{key} {hit}' reads paths named inside a file, which this check cannot see"

    # A `+` argument to a pager is a string in the pager's own command language,
    # not an option, and that language reaches a shell. A glob is refused here too:
    # the shell has not produced the real spelling yet, so `less +*` could resolve
    # against a file named `+!cmd` and nothing downstream would see the `+`.
    if verb in _PAGER_STARTUP_VERBS:
        for token in args:
            if token.startswith("+"):
                return f"'{verb} {token}' runs a pager startup command, which reaches a shell"
            if _GLOB_META_RE.search(token) or _EXTGLOB_RE.search(token):
                return f"a glob in a '{verb}' argument could expand into a startup command"

    # `uniq INPUT OUTPUT` writes its second operand. `--` ends the options here
    # too, so a word after it is an operand however it is spelled:
    # `uniq -- input -pwned` writes `-pwned`, while a leading-dash test counted
    # one operand and passed the segment as a read.
    if verb == "uniq":
        operands = _operands(args)
        # Counting the tokens is only sound if each one stays ONE word. A glob
        # here decides by count: with `in1` and `in2` present, `uniq in*` runs
        # `uniq in1 in2`, and the second operand is the OUTPUT file — so a single
        # pattern passed a segment that truncates a file. `uniq`'s operands are
        # positional, which is what makes this different from `ls *.py`.
        if any(_glob_shifts_arguments(tok) for tok in operands):
            return "a glob in a 'uniq' operand can expand into a second operand, which it writes"
        if len(operands) > 1:
            return "'uniq INPUT OUTPUT' writes its second operand"

    # Tools whose read-only option surface is enumerated POSITIVELY. Deny-by-default:
    # an option has to be recognised as a read before it passes, so a spelling nobody
    # thought of prompts instead of being admitted. This is what a per-tool write-flag
    # denylist could not give us on these four -- see the note above the registry for
    # the six `sort` spellings that arrived one review round at a time.
    if verb in _OPTION_ACCEPT_LISTS:
        violation = _option_accept_list_violation(verb, args)
        if violation:
            return violation

    return ""


def _classify_bash(cmd: str) -> str:
    """Single source of truth for read-only bash classification.

    Returns "" when the command is read-only, otherwise a human-readable
    reason it was rejected. :func:`is_read_only_bash` and
    :func:`unsafe_bash_reason` both delegate here so the two can never
    diverge — the invariant "reason is non-empty iff not read-only" holds
    by construction rather than by parallel maintenance. Deny-by-default.
    """
    if not cmd.strip():
        return "empty command"
    # Strip discard-only redirects (output sinks / stderr-merge) before the
    # unsafe-shell check; they are read-only but contain '>' / '&'.
    scrubbed = _DEVNULL_REDIR_RE.sub(" ", cmd)
    if _UNSAFE_SHELL_RE.search(scrubbed):
        return "unsafe shell pattern (redirect, command/process substitution, or backgrounding)"
    parts = re.split(r"\s*(?:&&|\|\||;|\n)\s*", cmd.strip())
    for part in parts:
        if not part.strip():
            continue
        pipe_parts = [p.strip() for p in part.split("|") if p.strip()]
        if not pipe_parts:
            return "unsafe shell pattern"
        # The verb is compared case-insensitively, but the side-effect check
        # below needs the original spelling: flags are case-sensitive, and the
        # two cases can mean opposite things (`file -C` compiles a magic file,
        # `file -c` only prints one).
        head = pipe_parts[0].strip()
        first = head.lower()
        if not any(first == p or first.startswith(p + " ") for p in _READ_ONLY_BASH_PREFIXES):
            base = first.split()[0] if first.split() else first
            return f"command '{base}' is not on the read-only allowlist"
        # Clearing the allowlist only settles which program runs. The rest of
        # the command line can still write a file, change a ref or start
        # another program.
        side_effect = _side_effect_reason(head)
        if side_effect:
            return f"not read-only: {side_effect}"
        for target in pipe_parts[1:]:
            matched = _READ_ONLY_PIPE_RE.match(target)
            if not matched:
                tgt = target.split()[0] if target.split() else target
                return f"pipe target '{tgt}' is not a read-only filter"
            # The name the allowlist matched must be the program bash actually
            # runs. `_READ_ONLY_PIPE_RE` ends its filter name at a `\b`, and `$`
            # satisfies that, so `sort$IFS-o victim` matched the entry `sort` while
            # bash split `$IFS` into whitespace and ran `sort -o victim`. Nothing
            # downstream recovered: `_side_effect_reason` reads the verb as
            # `sort$ifs-o`, finds no table for it, and returns "".
            #
            # The leading segment of a pipeline was never exposed to this, because
            # its allowlist test pins the boundary to a literal space
            # (`first == p or first.startswith(p + " ")`). This makes the pipe
            # allowlist say the same thing: the first argv word, exactly.
            try:
                target_tokens = shlex.split(target)
            except ValueError:
                return "pipe target quoting cannot be resolved"
            if not target_tokens or target_tokens[0] != matched.group(1):
                tgt = target_tokens[0] if target_tokens else target
                return (
                    f"pipe target '{tgt}' is not the read-only filter "
                    f"'{matched.group(1)}' it matched"
                )
            # The pipe allowlist matches only the leading verb, so a filter's
            # own output flag (`sort -o FILE`) needs the same check.
            side_effect = _side_effect_reason(target)
            if side_effect:
                return f"pipe target is not read-only: {side_effect}"
    return ""


def is_read_only_bash(cmd: str) -> bool:
    """Check if a bash command is read-only. Deny-by-default."""
    return _classify_bash(cmd) == ""


def unsafe_bash_reason(cmd: str) -> str:
    """Human-readable reason a bash command failed read-only classification.

    Used to make rejection messages specific ("unsafe shell pattern …")
    instead of the generic adapter default ("User refused permission to run
    tool"). Returns "" when the command IS read-only (no reason to reject on
    safety grounds).
    """
    return _classify_bash(cmd)


# ── Shared helpers ──


def parse_cls_meta(cls_val: str) -> dict | None:
    """Parse a JSON-encoded ``cls`` string into a meta dict.

    Returns the parsed dict (with ``tool_input`` sanitized) or ``None``
    if ``cls_val`` is not valid JSON or not a dict.  Used by both
    ``_prepare_messages`` (HTTP history) and ``_broadcast_chat_message``
    (live WS push) so the frontend sees an identical ``meta`` structure.
    """
    if not cls_val:
        return None
    try:
        meta = json.loads(cls_val)
        if not isinstance(meta, dict):
            return None
    except (json.JSONDecodeError, TypeError):
        return None

    # Defence-in-depth: sanitize LLM-controlled content at every read boundary
    if isinstance(meta.get("tool_input"), str):
        sanitized, _ = redact_exfiltration_urls(meta["tool_input"])
        sanitized, _ = redact_credentials(sanitized)
        meta["tool_input"] = sanitized

    # Normalize: backend stores as request_id, frontend expects approval_id
    if "request_id" in meta and "approval_id" not in meta:
        meta["approval_id"] = meta.pop("request_id")

    return meta


def chat_message_frame(note: dict, *, include_metadata: bool) -> dict[str, Any]:
    """Serialise a broadcast note into the wire ``chat_message`` frame.

    ONE serialiser for both delivery doors — the WebSocket arm in
    ``_broadcast_note`` and the SSE arm in ``handlers/updates.py:api_stream``.
    They are fed the same note by ``_broadcast()``, so a field added here
    reaches both; building the frame twice is how the SSE door kept dropping
    ``meta`` after #7981 fixed the WS one (#8045).

    ``include_metadata`` is a REQUIRED keyword and names a property of the
    TRANSPORT, not a preference: whether that door has per-client authorization
    downstream of this call.

    * The WS door does (``_send_ws_all`` -> ``_ws_client_allowed``, a
      deny-by-default event-scope gate, then ``_serialize_for_client``), so it
      passes ``True`` and lets the gate decide per socket.
    * The SSE queue does NOT. ``_broadcast()`` fans the raw note out to every
      registered queue with no per-app filtering, so ``api_stream`` must make
      the decision itself and passes ``include_metadata`` only for a
      dashboard-user token.

    That asymmetry is load-bearing. ``meta`` carries tool/LLM content
    (``tool_input``, a live ``oauth_url``, ``approval_id``), so putting it on an
    unfiltered queue exposes it to any app token granted that route regardless
    of its ``slots:*`` scope — the same class as GPT #6789, which leaked
    public-repo status onto ``/api/stream`` by enriching a payload that feeds
    both doors. Keep enrichment on the door that filters.

    ``cls``/``meta`` are conditional in BOTH directions when included: carried
    when the note has them (``meta.mid`` is the per-row delivery identity a
    client dedups on, so a frame without it cannot be recognised as a
    redelivery), and omitted entirely when it does not — an absent value must
    not arrive as a ``null`` or ``{}`` key a consumer has to special-case.
    """
    frame: dict[str, Any] = {
        "slot": note["slot"],
        "role": note["role"],
        "content": note["content"],
        "ts": note.get("ts", ""),
    }
    if not include_metadata:
        return frame
    if note.get("cls"):
        frame["cls"] = note["cls"]
    if note.get("meta"):
        frame["meta"] = note["meta"]
    return frame


def is_stop_event_row(m: dict) -> bool:
    """True when *m* is the card recorded because the user pressed Stop.

    Three carriers, and the in-memory one is the easy miss: the stop is appended
    as ``slot.append("system", stop_msg, stop_msg)`` with **no** ``meta=`` kwarg,
    so ``_ChatSlot.append`` never creates a ``meta`` key and the discriminator
    exists ONLY inside the JSON-encoded ``cls``/``content``. ``parse_cls_meta()``
    is what unpacks it, and it runs on the way OUT to a client
    (``_prepare_messages`` / ``_broadcast_chat_message``) — which is why the
    frontend sees ``meta.kind`` while the live window does not. Checking only
    ``kind``/``meta.kind`` here therefore matched a restored row but never a
    freshly-stopped one, silently diverging from the frontend mirror in exactly
    the case the two must agree on.

    Mirrors ``isStopEvent`` in ``website/src/store/chatSlice.ts``.
    """
    if m.get("kind") == "stop_event":
        return True
    meta = m.get("meta") or {}
    if meta.get("kind") == "stop_event":
        return True
    # Live window: the discriminator is still JSON inside `cls`. Prefilter on
    # the literal before parsing — this runs from `to_dict()` on the
    # push_slots_update path for every walked tail row, and `parse_cls_meta`
    # costs a json.loads plus credential/URL redaction when the row carries a
    # string tool_input (permission cards). `"stop_event"` is the literal
    # discriminator, so a cls without the substring can never parse to a match.
    # Non-string `cls` (an object-valued row from a foreign writer or a
    # corrupted transcript) is refused up front: the membership test would
    # raise on it, and `parse_cls_meta` would only swallow it into None anyway.
    cls_val = m.get("cls") or ""
    if not isinstance(cls_val, str) or "stop_event" not in cls_val:
        return False
    parsed = parse_cls_meta(cls_val)
    return bool(parsed and parsed.get("kind") == "stop_event")


def is_turn_interrupted(messages: list[dict]) -> bool:
    """True when the transcript shows a turn that ended without a reply.

    Two shapes qualify: the last conversational row is the USER's (nothing came
    back at all — a gateway restart mid-turn leaves exactly this), or it is the
    ASSISTANT's but an error row follows it (the turn streamed partway then died,
    which is otherwise shape-identical to a clean completion).

    One shape is explicitly excluded: a trailing ``stop_event``. The user pressing
    Stop is a deliberate ending, not an interruption, and stopping before the
    reply emitted any text produces the same ``[user, ...]`` tail as a crash.

    Selects the wording injected for the model (``_MANUAL_RESUME_MSG`` vs
    ``_MANUAL_CONTINUE_MSG``), gates whether the composer offers the Resume
    control (the ``continuable && interrupted`` composition in
    ``website/src/pages/ChatPage.tsx``), and feeds the ``interrupted`` field of
    the slot summary so the sidebar can stop rendering a goal loop as actively
    working while its session sits behind a Resume button. A False result means
    "as far as the transcript shows, the last turn finished or was ended on
    purpose", NOT "there is nothing to do": a force-quit runs no ``finally``, so
    the error row that would have proved an interruption was never written.

    Mirrors ``selectTurnInterrupted`` in ``website/src/store/chatSlice.ts`` —
    the two must agree, or the composer promises one thing and the agent is
    told another.

    Deliberately does not distinguish "produced some output" from "produced
    none": ``_MANUAL_RESUME_MSG`` is worded to hold in both cases, so the
    distinction would buy a branch and nothing else.
    """
    saw_trailing_error = False
    for m in reversed(messages):
        role = m.get("role")
        meta = m.get("meta") or {}
        # A deliberate Stop ENDS the turn; it does not interrupt it. Tested
        # before the user/assistant branch because stopping before the reply
        # emitted any text leaves ``[user, stop_event]`` -- shape-identical to
        # "the gateway died before anything came back". See ``is_stop_event_row``
        # for why the discriminator has to be resolved from three carriers.
        # Only the NEWEST turn's terminator reaches here -- an older stop card
        # is never scanned, because a later user/assistant row returns first.
        if is_stop_event_row(m):
            return False
        if is_system_notice(role, meta):
            continue
        if role in ("user", "assistant") and m.get("content"):
            return True if role == "user" else saw_trailing_error
        if role == "error":
            saw_trailing_error = True
    return False


def _mark_permission_resolved(
    messages: list[dict],
    request_id: str,
    decision: str,
    *,
    only_if_pending: bool = False,
) -> bool:
    """Persist a resolved decision into a permission message's cls JSON.

    Returns True when a permission message was written. Callers holding the
    owning slot MUST set ``slot._dirty = True`` on a True return — the periodic
    flush skips non-dirty slots, so an unflagged in-place mutation can be lost
    on restart and the card comes back as an unanswerable orphan.

    ``only_if_pending`` leaves an already-resolved message untouched (and
    returns False). Use it for backstop callers that must not clobber a richer
    decision already recorded by the primary resolver — e.g. "trust"/"yolo",
    which the UI renders as "Trusted — auto-approving future calls" and would
    otherwise be flattened to a bare "approved".
    """
    for msg in reversed(messages):
        if msg.get("role") == "permission":
            try:
                cls = json.loads(msg.get("cls", "{}"))
                if not isinstance(cls, dict):
                    # Valid JSON but not an object — cannot carry "resolved".
                    # Mirrors parse_cls_meta() / _sweep_stale_permissions().
                    continue
                if cls.get("request_id") == request_id:
                    if only_if_pending and "resolved" in cls:
                        return False
                    cls["resolved"] = decision
                    msg["cls"] = json.dumps(cls)
                    return True
            except (json.JSONDecodeError, TypeError):
                pass
    return False


# ── Constants ──


_DEFAULT_PORT = DASHBOARD_PORT
_SSE_INTERVAL_SECS = 5
_NOTIFICATIONS_FILE = "notifications.jsonl"
_MAX_PERSISTED_NOTIFICATIONS = 200
_AUTO_COMPACT_NOTICE = "🔄 Auto-compacted at {pct:.0f}%."
_AUTO_COMPACT_FAILED_NOTICE = (
    "⚠ Auto-compact failed at {pct:.0f}% — will retry after cooldown. "
    "You can run `/compact` manually."
)
_SESSION_RECYCLED_NOTICE = (
    "♻️ This session was recycled by the watchdog ({reason}). "
    "Conversation history is preserved — your next message starts a fresh process."
)
#: Sent to a conversation that just lost its inbound resume binding, so the next
#: message landing in a brand-new session is explained rather than mysterious.
#: ``!sessions`` is Discord's command and Discord is the only transport that binds
#: inbound, so the instruction is reachable wherever this notice can arrive.
_INBOUND_UNBIND_NOTICE = (
    '🔗 This conversation was detached from session "{title}" — {why}. '
    "Run `!sessions` to reattach."
)

#: Human phrasing per audited reason, so the notice never shows an audit token.
#: The two vocabularies stay separate on purpose: a reason can be renamed or split
#: without rewriting user copy, and this copy can be reworded without touching the
#: trail. An unmapped reason falls back to the generic phrase rather than leaking
#: through as a raw token.
_INBOUND_UNBIND_WHY: dict[str, str] = {
    UNBIND_REASON_DASHBOARD_UNLINK: "someone unlinked it from the dashboard",
    UNBIND_REASON_ORIGIN_REBIND: "this conversation was relinked to a new session",
    UNBIND_REASON_SESSION_DESTROYED: "that session was deleted",
    UNBIND_REASON_ENTRY_DELETED: "that session's record was removed",
    UNBIND_REASON_PRUNED_STALE: "that session's record was no longer on disk",
    UNBIND_REASON_UNSPECIFIED: "the link was cleared",
}
_INBOUND_UNBIND_WHY_DEFAULT = "the link was cleared"


#: Shown when the out-of-band watchdog finds a turn whose consumer stopped
#: pulling events. Deliberately describes the observation rather than promising a
#: remedy: nothing is cancelled or retried, because what the turn is blocked on
#: is not knowable from the loop that noticed. Stating a duration is the point —
#: it is what distinguishes this from a turn that is merely slow.
_STUCK_TURN_NOTICE = (
    "⏳ This turn has produced nothing for {minutes} min and is not waiting on an "
    "approval — it may be stuck. Nothing has been cancelled. Press Stop to end it "
    "and try again."
)


def stuck_turn_notice(parked_secs: float) -> str:
    """Render the stuck-turn notice for a park of ``parked_secs``.

    Module-level and pure so the rounding is testable without standing up a whole
    ``DashboardState``. Floors at 1 minute: the hook's threshold is minutes-scale,
    so "0 min" would only ever read as a bug to the person seeing it.
    """
    return _STUCK_TURN_NOTICE.format(minutes=max(1, int(parked_secs // 60)))


_MAX_SLOT_MESSAGES = 10000  # Keep all messages — virtual scrolling handles performance

#: Transient/streaming roles that are never persisted by the save path
#: (``chat_persistence._build_message_entry`` returns ``None`` for them) and are
#: skipped by durable readers. Defined here — beside the trim path that must
#: count the durable rows it folds into the frozen prefix — and re-exported by
#: ``chat_persistence`` as ``_TRANSIENT_ROLES`` for its readers, so there is one
#: definition rather than two that can drift.
_TRANSIENT_ROLES = frozenset({"chunk", "done", "streaming", "queued", "permission"})


def durable_row_count(rows: Iterable[dict]) -> int:
    """How many of *rows* a durable read returns: everything non-transient.

    The one shared counting rule for the durable-only frozen-prefix counter
    (``_disk_older_durable_count``): every site that sets or advances it counts
    with this, so the restore paths, the channel restore and the trim path can
    never disagree about which rows are durable.
    """
    return sum(1 for m in rows if m.get("role") not in _TRANSIENT_ROLES)


#: Roles that exist only on the wire: appended so a reader/flush can see them,
#: never broadcast as a `chat_message` and never persisted (the mirror of
#: ``_TRANSIENT_ROLES`` above minus the rows that ARE broadcast).
#: They get no ``meta.mid`` — see ``_ChatSlot.append``.
_WIRE_ONLY_ROLES = frozenset({"chunk", "done", "streaming"})


def row_mid(row: Any) -> str | None:
    """The delivery identity stamped on an appended window row, or ``None``.

    The ONE extraction of ``meta.mid`` shared by every dual-writer that reads
    the id off a ``_ChatSlot.append`` return to stamp its durable transcript
    copy. Mirrors the read side (``_append_unflushed_tail`` matches only a
    non-empty ``str``): any other shape reads as "no identity" here rather than
    being persisted as an id the reader is structurally unable to match.
    Tolerates a non-dict *row* so a caller handed a test double degrades to
    ``None`` (an id-less durable copy) instead of raising.
    """
    if not isinstance(row, dict):
        return None
    meta = row.get("meta")
    mid = meta.get("mid") if isinstance(meta, dict) else None
    return mid if isinstance(mid, str) and mid else None


def append_and_surface(
    state: "DashboardState",
    slot: "_ChatSlot",
    role: str,
    content: str,
    cls: str = "",
    *,
    meta: dict | None = None,
    broadcast_user: bool = False,
    extra: dict | None = None,
) -> dict[str, Any]:
    """Append a row and surface it live -- through exactly one identity-carrying door.

    The ONE way to append a window row that must also render in the open chat
    immediately. ``_ChatSlot.append`` already delivers a live ``chat_message``
    (via ``_on_message`` -> ``_broadcast_chat_message``, carrying the minted
    ``meta.mid``) whenever ``not slot._has_reader`` -- so an unconditional
    manual ``broadcast_ws("chat_message", ...)`` after an append ships the SAME
    row a second time. Worse, the hand-built frames carried no ``meta.mid``,
    and the frontend's redelivery guard declines mid-less frames rather than
    guessing (``isRedeliveredMessage``), so each extra copy rendered as a new
    bubble (#5981).

    The manual frame is emitted only in the one case append's own callback is
    suppressed (``slot._has_reader``: an HTTP stream reader is draining
    ``_pending`` for the actively streaming client, and other windows still
    need the row) -- mirroring the pattern at ``handlers/files.py`` -- and it
    carries the appended row's ``ts`` + ``meta`` (mid included), so a client
    that receives the row through two doors can now recognise "this row again"
    instead of rendering a duplicate.

    ``user`` rows: ``append`` skips broadcasting them by default because the
    composer that submitted them already rendered them optimistically -- true
    only of a message typed in THIS dashboard. Callers surfacing a user row
    that originated elsewhere (a channel mirror, a Go-button label) pass
    ``broadcast_user=True`` and get the same single identity-carrying delivery.

    Redaction is the caller's job (unchanged from the sites this replaces):
    content passed here must already be display-safe. The append path re-redacts
    non-user content in ``_broadcast_chat_message``; the reader-suppressed frame
    below does not, matching the manual frames it replaces.

    Returns the appended row (so callers can read ``row_mid`` off it).
    """
    if broadcast_user:
        msg = slot.append(role, content, cls, broadcast_user=True, meta=meta)
    else:
        msg = slot.append(role, content, cls, meta=meta)
    if getattr(slot, "_has_reader", False):
        frame: dict[str, Any] = {
            "slot": slot.key,
            "role": role,
            "content": content,
            "ts": msg.get("ts", ""),
        }
        if cls:
            frame["cls"] = cls
        row_meta = msg.get("meta")
        if isinstance(row_meta, dict) and row_meta:
            frame["meta"] = row_meta
        if extra:
            frame.update(extra)
        state.broadcast_ws("chat_message", frame)
    return msg


#: Roles whose LIVE append starts the slot's next turn, and so consumes the answer
#: channel an unanswered stateless question card was waiting on. Mirrors the
#: frontend's ``QUESTION_RETIRING_ROLES``: the two must agree, or a session reports
#: needs_input with no card on screen (client retired, server did not) or renders a
#: card whose answer channel is already gone (server retired, client did not).
#: Widening coverage is a data edit here.
_QUESTION_RETIRING_ROLES = frozenset({"user", "nudge"})
#: Roles that carry an inbound PROMPT -- the rows that ask this session to do
#: something, as opposed to the rows produced while it works. ``user`` is a human
#: send from any surface; ``inject`` is automation delivering a cron notification
#: or a subagent completion event. Used to rank a session by when its work was
#: requested while the answer is still streaming (``to_dict``'s ``last_turn_ts``).
_PROMPT_ROLES = frozenset({"user", "inject"})
_MAX_SOURCE_LINKS_PER_SLOT = 64
# How many source links each slot payload actually serializes (the sidebar
# renders at most this many chips). Shared with the periodic check-status
# refresh so the driver and the serializer cannot drift.
_SERIALIZED_SOURCE_LINKS_PER_SLOT = 3


def _budgeted_source_links(links: list[dict]) -> list[dict]:
    """Apply the sidebar chip budget PER KIND, changes first.

    Pull requests and issues each get their own
    ``_SERIALIZED_SOURCE_LINKS_PER_SLOT`` allowance. A single shared budget
    sliced before the kind filter would let three mentioned issues crowd every
    PR chip out of the sidebar -- and, because the check-status refresh reads
    the same slice, would also stop scheduling that PR's CI status updates.
    Budgeting per kind keeps pre-existing pull-request behaviour unchanged and
    makes issues purely additive.
    """
    changes, issues = _source_links_by_kind(links)
    return changes[:_SERIALIZED_SOURCE_LINKS_PER_SLOT] + issues[:_SERIALIZED_SOURCE_LINKS_PER_SLOT]


def _source_links_by_kind(links: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split links into (changes, issues), preserving discovery order in each.

    ``kind`` is absent on older payloads and means ``"change"`` there, so the
    default keeps a pre-``kind`` link rendering as the pull request it always
    was.
    """
    changes = [link for link in links if link.get("kind", "change") == "change"]
    issues = [link for link in links if link.get("kind", "change") == "issue"]
    return changes, issues


# Deduplicated SEL audit of the non-owner public-repo status grant. This runs on
# the per-link serialization path (every push broadcast), so an un-deduplicated
# write would be unbounded — collapse to one event per URL per window, mirroring
# ws_event_scope._audit_decision. AUTOSDE (backend-security-controls) requires a
# SEL event for every permission decision, grants included.
_PUBLIC_STATUS_GRANT_AUDIT: dict[str, float] = {}
_PUBLIC_STATUS_DENY_AUDIT: dict[str, float] = {}
_PUBLIC_STATUS_GRANT_WINDOW_SECS = 300.0


def _audit_public_status_grant(url: str) -> None:
    now = time.monotonic()
    last = _PUBLIC_STATUS_GRANT_AUDIT.get(url)
    if last is not None and (now - last) < _PUBLIC_STATUS_GRANT_WINDOW_SECS:
        return
    _PUBLIC_STATUS_GRANT_AUDIT[url] = now
    try:
        sel().log_api_access(
            caller="dashboard-user",
            operation="source_link_public_status",
            outcome="allowed",
            source="source_links",
            resources=url,
        )
    except Exception:
        logger.debug("SEL audit for public-status grant failed", exc_info=True)


def _audit_public_status_denied(url: str) -> None:
    # Symmetric to the grant audit: AUTOSDE backend-security-controls requires a
    # SEL event for every permission DECISION, denials included. A non-owner
    # dashboard user requested status on a repo that is NOT confirmed public
    # (private / unknown / stale) and was denied — record it, deduplicated per
    # URL per window (same hot per-link broadcast path as the grant).
    now = time.monotonic()
    last = _PUBLIC_STATUS_DENY_AUDIT.get(url)
    if last is not None and (now - last) < _PUBLIC_STATUS_GRANT_WINDOW_SECS:
        return
    _PUBLIC_STATUS_DENY_AUDIT[url] = now
    try:
        sel().log_api_access(
            caller="dashboard-user",
            operation="source_link_public_status",
            outcome="denied",
            source="source_links",
            resources=url,
        )
    except Exception:
        logger.debug("SEL audit for public-status denial failed", exc_info=True)


def _project_source_links(
    links: list[dict],
    include_check_status: bool,
    *,
    dashboard_user: bool = False,
) -> list[dict]:
    """Attach cached chip status to each link, gated on kind and on the caller.

    The chip-status cache is pull-request-only: it holds a {ci, state}
    projection of a PR/MR lifecycle. Consulting it for an issue would key on a
    URL it never stores -- and if a PR and an issue ever normalized to the same
    key, the issue chip would inherit the PR's CI glyph. Gate on kind.

    ``include_check_status`` is the OWNER gate: the owner sees status for every
    link, public or private. ``dashboard_user`` is the weaker, PUBLIC-ONLY gate:
    a non-owner but authenticated dashboard user sees status only for a link
    whose repository is KNOWN public, because that lifecycle state is already
    world-visible on the provider's website. Private and not-yet-known repos
    fall through to owner-only (fail closed). App tokens pass neither flag and
    keep bare chips.

    Shared by the budgeted slots payload and the unbudgeted overflow-expand
    read so the two cannot decorate the same link differently.
    """

    def _attach(link: dict) -> bool:
        if link.get("kind", "change") != "change":
            return False
        if include_check_status:
            return True
        granted = dashboard_user and _repo_is_public(link["url"]) is True
        if granted:
            # SEL-audit the AUTHORIZATION DECISION (AUTOSDE
            # backend-security-controls: every permission decision, grants
            # included). This grants a non-owner status on a repo whose public
            # visibility we positively confirmed — a real access-control
            # decision, so it must leave an allow event. Deduplicated per URL per
            # window because this runs per-link on every push broadcast; an
            # un-deduplicated write here would be unbounded on the hot path.
            _audit_public_status_grant(link["url"])
        elif dashboard_user:
            # DENY decision: a non-owner dashboard user requested status on a
            # change link but the repo is not confirmed public (private /
            # unknown / stale), so status is withheld. AUTOSDE requires the
            # denial to be audited too — deduplicated the same way (GPT #6789
            # round-15). include_check_status (owner) paths never reach here, and
            # app tokens set neither flag so they are not "denied dashboard-user"
            # decisions.
            _audit_public_status_denied(link["url"])
        return granted

    def _status(url: str) -> dict:
        # Project ONLY the known chip-status keys, never the raw cache dict.
        # Splatting ``**cache`` was fail-open: the cache payload already outgrew
        # its docstring once (mergeStateStatus), so the next field would ride
        # silently into every frame — including, via the general broadcast, an
        # app-token frame (ws_event_scope strips the same names on the far end,
        # but the two lists must not be the sole guarantee). Enumerating here
        # fails closed at the source: an unlisted field is simply not emitted.
        cached = _cached_check_status(url) or {}
        return {k: cached[k] for k in _CHIP_STATUS_KEYS if k in cached}

    return [{**link, **(_status(link["url"]) if _attach(link) else {})} for link in links]


# The only fields the chip-status cache is allowed to project onto a source
# link. Kept in step with ws_event_scope._SOURCE_LINK_STATUS_KEYS (the app-token
# strip) and the frontend SidebarSourceLink type; a field absent here is never
# emitted, so widening the cache cannot leak a new field into any frame.
_CHIP_STATUS_KEYS = ("ci", "state", "mergeable", "mergeStateStatus")


_NON_DURABLE_SOURCE_LINK_ROLES = frozenset({"chunk", "done", "streaming", "queued", "permission"})
# FIFO ceiling on a slot's pending-context queue (app-kit context inject +
# Slack thread backfill). Shared so the two eviction sites cannot drift.
_MAX_PENDING_CONTEXT = 50


def context_entry_expired(entry: dict, now: float) -> bool:
    """True if a pending-context entry's TTL has elapsed.

    Shared by the drain, the per-source cap count, and the deferred-note
    promotion so they cannot disagree about which entries are still live. It
    lives here rather than in chat_runner because ``_ChatSlot`` itself needs it.
    """
    max_age = entry.get("maxAge")
    if max_age is None:
        return False
    return entry.get("injectedAt", 0) + max_age < now


def _note_authorized_elsewhere(stamped: object, live_session: str) -> bool:
    """True when note content records a session other than *live_session*.

    Reads the same ``session`` stamp off a pending-context entry or off a
    transcript row's ``meta``. Absent stamp means not note content that carries
    an authorizing session, so it is never dropped.
    """
    if not isinstance(stamped, dict):
        return False
    authorized = stamped.get("noteSession")
    return authorized is not None and authorized != live_session


# Bare chat-N label matcher used by DashboardState.resolve_slot() for prefix fallback.
# Gates the prefix lookup to prevent broad matches (e.g. bare "chat" binding to any slot).
_CHAT_N_RE = re.compile(r"chat-\d+")

# Display label for a chat slot that has no real title yet — shown in the UI
# instead of the internal ``chat-N-<ts>`` key (which is an identifier, not a
# name). Applied at the serialization boundary (``_ChatSlot.display_title``),
# so a brand-new empty session, the pre-send window, and the pre-LLM window all
# read the same. The LLM auto-title / fallback replace it with a real title.
NEW_SESSION_TITLE = "New Session…"

# Matches a slot-key *identifier* used as a title (both the stripped
# ``chat-N-<ts>`` and the resumed ``dashboard_chat-N-<ts>`` forms). An untitled
# slot whose title is still such an identifier should display as
# NEW_SESSION_TITLE, not the raw key. Real titles never match this.
_SLOT_KEY_TITLE_RE = re.compile(r"(?:dashboard_)?chat-\d+-\d+$")

# Cron notification wrapper format — used by handlers.py (create), chat.py (detect), ChatPage.tsx (render)
CRON_NOTIFY_PREFIX = "[Cron notification from "
CRON_NOTIFY_END = "[End of cron notification]"
CRON_NOTIFY_RE = re.compile(rf'^{re.escape(CRON_NOTIFY_PREFIX)}"(.*)"\]')
# Both sub-agent markers, for the checks that must treat either shape as a system
# injection. Pass this straight to ``str.startswith`` (it accepts a tuple) instead
# of listing the prefixes per call site: the batch marker is a SIBLING of the
# per-agent one rather than an extension of it, so a per-prefix check written
# against one silently misses the other, and a third shape would miss both.
SUBAGENT_COMPLETION_PREFIXES = (
    SUBAGENT_COMPLETION_PREFIX,
    SUBAGENT_BATCH_COMPLETION_PREFIX,
)
# One-shot synthesis turn fired after ALL sub-agents in a fan-out complete and
# each result has been processed in its own turn (see gateway._subagent_done arm
# + chat_runner drain/idle branch). Its visible reply is the consolidated,
# user-facing summary. Rendered as an "inject" message (not a user bubble); the
# prefix marks it as a synthetic continuation so it is NOT mirrored to linked
# surfaces (Slack/Telegram) as though the user typed it.
SUBAGENT_SYNTHESIS_PREFIX = "[SYSTEM] Sub-agent synthesis:"
SUBAGENT_SYNTHESIS_PROMPT = (
    f"{SUBAGENT_SYNTHESIS_PREFIX} all sub-agents you spawned have completed and each result was "
    "processed above. Produce a single consolidated synthesis as your reply for the user: "
    "(1) restate the original goal you spawned the sub-agents for, (2) synthesize the combined "
    "findings across all of them (do not just repeat each result in turn), and (3) give concrete "
    "recommended next actions or decisions. This is the user-facing deliverable — keep it clear "
    "and actionable."
)
# Synthetic continuation injected after a recoverable tool refusal (host-gate
# policy deny or the read-only bash gate) ended a turn early. Carries the
# refusal reason back to the model so it can adapt instead of stalling for the
# user. Rendered as an "inject" message (not a user bubble) and never mirrored
# to a linked Slack thread as user input.
REFUSAL_RECOVERY_PREFIX = "[Tool refusal — automatic recovery]"
# Synthetic continuation injected after a genuinely-wedged (stale) turn was
# detected + reset. Tells the model its previous turn was interrupted by a
# system stall — NOT the user — and to resume from its last committed step
# rather than restart. Rendered as an "inject" message (not a user bubble) and
# never mirrored to a linked Slack thread as user input.
STALE_RECOVERY_PREFIX = "[Stalled turn — automatic recovery]"
# Synthetic continuation injected after the per-session watchdog judged an
# in-flight tool dead/stuck and cancelled the session. Unlike the legacy path
# (which re-queued the ORIGINAL user message verbatim — restarting the whole
# task from scratch), this hands the model the stall context so it can check
# partial results and continue. Rendered as an "inject" message (not a user
# bubble) and never mirrored to a linked Slack thread as user input.
TOOL_STALL_RECOVERY_PREFIX = "[Tool stall — automatic recovery]"
# Prefix on the continuation injected after a reset recovers an interrupted
# connection. The body lives in chat_utils so queue provenance and turn routing
# share one canonical instruction.
CONN_RECOVERY_PREFIX = "[Connection lost — automatic recovery]"
# Prefix on the continuation injected when a reset recovers a turn the backend
# refused because the session was still busy. Separate from
# CONN_RECOVERY_PREFIX even though both requeue the same continuation shape:
# nothing was disconnected, and the marker is what the transcript renders, so
# sharing the connection marker would report a dropped connection to a user
# whose status card reads "Session busy". Body: _BUSY_RECOVER_MSG in chat_utils.
BUSY_RECOVERY_PREFIX = "[Session busy — automatic recovery]"
# Prefix on the runner-injected CONTINUE that resumes a turn cut short by a
# transient backend 5xx after tokens/tools had already streamed. The body lives
# in chat_utils as _POSTTOKEN_RECOVER_MSG; the prefix is here so all eight
# recovery markers share one home and the frontend has one list to mirror.
POSTTOKEN_RECOVERY_PREFIX = "[Interrupted turn — automatic recovery]"
# Prefix on the runner-injected nudge that breaks a repeated empty-generation
# pattern (the model returned no output twice). Body: _EMPTY_AUTO_CONTINUE_MSG.
EMPTY_RESPONSE_RECOVERY_PREFIX = "[Empty response — automatic recovery]"
# Prefix on the runner-injected continuation sent when a turn ended on a
# PROMISE-ONLY final message: the model announced an immediate action ("I'll do
# that now") and then yielded without making the tool call, so the work never
# happened and the turn still billed. Body: _PROMISE_ONLY_CONTINUE_MSG in
# chat_utils. One bounded attempt (slot._promise_only_retries), never a loop.
PROMISE_ONLY_RECOVERY_PREFIX = "[Unfinished action — automatic recovery]"
# Prefix on the runner-injected continuation sent when the BACKEND compacted the
# conversation in the middle of a turn and then ended the turn without finishing
# the work. The compaction itself succeeded — nothing failed — but the request
# that was in flight when the context filled was abandoned, and the turn lands
# looking clean (a settled footer with elapsed time), so without this the chat
# just stops. Body: _COMPACTION_CONTINUE_MSG in chat_utils. One bounded attempt
# (slot._compaction_continue_retries), never a loop.
COMPACTION_RECOVERY_PREFIX = "[Context compacted — automatic recovery]"
# Prefix on the continuation injected when the USER pressed Continue on an
# interrupted turn. Body: _MANUAL_RESUME_MSG in chat_utils. Named into the
# *_RECOVERY_PREFIX family because test_recovery_card_prefixes.py keys the
# cross-language drift guard on that suffix — a marker outside the family is
# invisible to it, and the card would silently render machine prose as a bubble.
# The VALUE is what carries the user-facing meaning, and it deliberately does NOT
# say "automatic recovery" like the five above: a person pressed the button, and
# the card must not claim the system recovered by itself.
MANUAL_RESUME_RECOVERY_PREFIX = "[Continue — requested by the user]"
# Prefix on the continuation injected when a Stop hook returns a block decision
# (`{"decision": "block", "reason": ...}` on exit-0 stdout). The reason IS the
# instruction, handed back as the next turn so a hook can steer the session
# without a round-trip to the user. Named into the *_RECOVERY_PREFIX family so
# test_recovery_card_prefixes.py's drift guard sees it — a marker outside the
# family renders as a full-width bubble instead of a card. The VALUE deliberately
# does not say "recovery": the turn completed and a hook asked for another, so
# nothing failed and nothing was recovered.
HOOK_CONTINUATION_RECOVERY_PREFIX = "[Hook continuation — automatic]"
# Prefix on the informational row surfaced when a Stop-hook continuation run hits
# the `agent.max_stop_hook_nudges` cap: the next block decision is refused, no
# turn is dispatched, and this row is appended instead so the transcript shows
# the loop was force-stopped (with the reached depth as "#N"). Named into the
# *_RECOVERY_PREFIX family so test_recovery_card_prefixes.py's drift guard sees
# it — a marker outside the family renders as a full-width bubble, not a card.
# The VALUE does not say "recovery": nothing failed or recovered, a safety cap
# fired.
HOOK_HALTED_RECOVERY_PREFIX = "[Stop-hook nudge cap reached]"
# Prefix on the DISPLAY-ONLY row appended when a tool deny's reason was steered
# into the running turn (see chat_runner._steer_policy_notice). Nothing is
# queued and no turn is dispatched — the agent already has the reason — so this
# row exists purely so the person sees the same blocked-tool card they used to
# get from the recovery continuation, instead of only a generic "Steered" chip
# that reads as though they had steered the turn themselves.
#
# Named into the *_RECOVERY_PREFIX family because test_recovery_card_prefixes.py
# keys its cross-language drift guard on that suffix — a marker outside the
# family is invisible to it and the row would render as a full-width bubble of
# machine prose. The VALUE deliberately does not say "recovery": nothing was
# recovered and no continuation was sent, which is the whole point. Same
# reasoning as HOOK_HALTED_RECOVERY_PREFIX, whose row is also display-only.
REFUSAL_INBAND_RECOVERY_PREFIX = "[Tool blocked — reason sent to the agent]"


def should_queue_refusal_recovery(
    refusal_reasons: list,
    stopping: bool,
    needs_reset: bool,
    stop_reason: str,
    *,
    notices_sent: int = 0,
    notices_pending: int = 0,
) -> bool:
    """Decide whether to auto-queue a refusal-recovery prompt after a turn.

    Returns False (skip recovery) when:
    - No refusals occurred
    - A stop is still in progress
    - A session reset is already re-queuing
    - The turn was cancelled by the user (not a policy block)
    - Every refusal was already explained IN-BAND and the backend confirmed it

    ``notices_sent`` is how many :func:`build_refusal_steer_notice` bodies were
    steered into the turn, and ``notices_pending`` how many of those the
    ``steering_consumed`` echo did NOT account for. The extra turn is skipped only
    when every refusal got a notice AND none is still pending — an unconfirmed
    steer is treated as undelivered, so the fallback continuation still runs. The
    check is deliberately coarse (counts, not a per-refusal pairing): its two
    failure directions are not symmetric. Skipping wrongly leaves the model with
    kiro-cli's "User denied tool execution" and no correction, while queueing
    wrongly costs one turn the model would otherwise have been told twice — which
    is exactly what this path already cost before in-band delivery existed.

    Both are keyword-only with defaults so a caller on a harness without mid-turn
    steer keeps the original three-condition behaviour unchanged.
    """
    if refusal_reasons and notices_sent >= len(refusal_reasons) and notices_pending == 0:
        return False
    return bool(
        refusal_reasons
        and not stopping
        and not needs_reset
        and stop_reason != STOP_REASON_CANCELLED
    )


def should_queue_hook_continuation(stopping: bool, needs_reset: bool, stop_reason: str) -> bool:
    """Decide whether a Stop hook's block decision may inject a continuation.

    Mirrors :func:`should_queue_refusal_recovery`'s suppression set so a hook can
    never override the Stop button: a stop in progress, a pending session reset,
    or a user-cancelled turn all win over the hook.
    """
    return bool(not stopping and not needs_reset and stop_reason != STOP_REASON_CANCELLED)


def parse_hook_continuations(stdouts: list[str]) -> list[str]:
    """Extract continuation instructions from Stop-hook exit-0 stdout texts.

    ``stdouts`` is what ``_fire`` returns for the Stop event: one entry per exit-0
    hook, plus ``BLOCKED:`` markers for exit-2 denials. Only a well-formed block
    decision carrying a non-blank ``reason`` contributes, because ``reason`` is
    the message that gets injected — a block without one has nothing to say, so
    the turn stops normally. Every other string is ignored, which is what keeps an
    ordinary Stop hook that merely logs from continuing the session.
    """
    reasons: list[str] = []
    for stdout in stdouts:
        try:
            decision = json.loads(stdout)
        except (ValueError, TypeError, RecursionError):
            # RecursionError is a RuntimeError, not a ValueError: json.loads
            # raises it on deeply-nested input, and a pathological hook must not
            # error an otherwise-successful turn.
            continue
        if not isinstance(decision, dict) or decision.get("decision") != "block":
            continue
        reason = decision.get("reason")
        if isinstance(reason, str) and reason.strip():
            reasons.append(reason)
    return reasons


def build_refusal_recovery_prompt(
    refusals: list[tuple[str, str]], *, credential_tool_hint: str = "", answered: bool = False
) -> str:
    """Build the body of an automatic continuation after a recoverable tool refusal.

    When a tool call is refused for a recoverable, system-side reason — a
    host-gate policy deny, the read-only bash safety gate, or a PreToolUse policy
    hook block — the reason reaches the dashboard pill and the SEL audit log but
    never the model: kiro-cli's own tool result for a rejected permission is the
    fixed string "User denied tool execution", which is indistinguishable from a
    human having clicked No. So the agent apologises for a cancellation that
    never happened and yields.

    This continuation is the FALLBACK path. The primary path is
    :func:`build_refusal_steer_notice`, which delivers the same reason in-band on
    a harness that supports mid-turn steer, costing no extra turn. This one runs
    when that was impossible (harness without steer) or when the steer was never
    folded in (no ``steering_consumed`` echo covered it).

    ``refusals`` is a list of ``(tool_title, reason)`` tuples recorded during the
    turn (already redacted by the caller). The returned text hands those reasons
    back to the model and frames the block as a system policy decision — NOT a
    user cancellation — so the agent can adapt (an allowed alternative, a
    different tool) or stop on its own with a reason. The caller prepends
    :data:`REFUSAL_RECOVERY_PREFIX`. Returns "" if there is nothing to recover.

    ``answered`` says the turn ALREADY streamed text the user has read, despite
    the block. The premise of the default wording — "the turn ended early, pick up
    where you left off" — is then false, and acting on it makes the model re-answer
    a question the user has already read, once per blocked call and at full turn
    cost. So the body flips to awareness-only: same block reasons, same
    remediation, but an explicit instruction not to restate what was sent.

    That flag deliberately does NOT claim the turn *finished* — no caller can tell
    a delivered answer from a one-line preamble ("Let me check the logs.") before
    the blocked call, because the two are indistinguishable prose flushed at the
    same point in the stream. So this branch conditions its instruction on whether
    the task is done rather than asserting it: continue-from-there is the default
    and stopping is the narrow case. Asserting a finished answer here would tell a
    turn that had only narrated its intent to stop with the work undone.
    The reason still has to be delivered rather than dropped, because on a backend
    without mid-turn steer this turn is the ONLY channel for it — without it the
    model's last word on the subject is kiro-cli's "User denied tool execution",
    and it will keep attributing the block to the user in later turns.

    Lives here (a leaf module that owns the prefix) rather than in context.py so
    chat_runner can import it at module top without a circular import. There is
    deliberately no retry cap: the model decides when to stop, and the user's
    Stop button remains the hard breaker.
    """
    if not refusals:
        return ""
    lines = [
        (
            "One or more tool calls in your previous turn were blocked by a Kiro "
            "Crew safety policy. This was NOT a user action — do not treat it as a "
            "cancellation or interruption by the user. That turn already put text "
            "on screen for the user, so this note is for awareness: carry on from "
            "there rather than starting over."
            if answered
            else "One or more tool calls in your previous turn were blocked by a "
            "Kiro Crew safety policy, which ended the turn early. This was NOT a "
            "user action — do not treat it as a cancellation or interruption by "
            "the user."
        ),
        "",
        "Blocked:",
    ]
    for title, reason in refusals:
        lines.append(f"  - {title}: {reason}" if reason else f"  - {title}")
    lines += [
        "",
        (
            "Do NOT repeat, restate or re-derive what you already sent — the user "
            "has read it. If the task is NOT finished, continue from there: use an "
            "allowed alternative (for a shell command, a read-only variant) or a "
            "different tool, and say what it changed. Only if the task IS finished "
            "and the block left nothing missing, reply with one short line noting "
            "the block and stop."
            if answered
            else "Decide how to proceed: use an allowed alternative (for a shell "
            "command, a read-only variant), a different tool, or — if the block is "
            "correct and you genuinely cannot proceed — say so and stop. Otherwise "
            "continue the task where you left off."
        ),
    ]
    # Per-class remediation, de-duplicated across the turn's refusals: several
    # blocked calls in one turn are usually the same wall hit from different
    # angles, and repeating identical prose per bullet buries the one instruction
    # that differs. Ordered by first appearance so the earliest refusal's
    # guidance leads.
    guidance: list[str] = []
    for title, reason in refusals:
        text = remediation_for(reason, title, credential_tool_hint=credential_tool_hint)
        if text and text not in guidance:
            guidance.append(text)
    if guidance:
        # NOT rendered as `  - ` bullets: RecoveryCard counts every bullet-shaped
        # line in this body as one blocked tool call (`BULLET_RE`), so guidance in
        # that shape would inflate the card's "N blocked" count with prose. The
        # bullet list above is the wire form of the blocked-item count; this
        # section is prose about it, and the two must stay distinguishable.
        lines += ["", "How to do this properly:"]
        for text in guidance:
            lines += [f"    {text}"]
    return "\n".join(lines)


#: Why a tool call was denied, for the in-band notice's cause-specific wording.
#: The notice's INVARIANT half — that this was not a user action, the generic
#: string it is correcting, and the instruction to decide inside this turn — is
#: identical for every cause; only the clause naming the cause and the guidance
#: about what to do next differ. Kept as data rather than three near-copies of
#: the notice so the invariant half cannot drift between them, which is the half
#: doing the actual work of overwriting the model's wrong conclusion.
DENY_CAUSE_POLICY = "policy"
DENY_CAUSE_INVALID_NAME = "invalid_name"
DENY_CAUSE_HOOK_ERROR = "hook_error"

#: cause → (clause completing "The tool call you just made …", what to do next).
_DENY_CAUSE_TEXT: dict[str, tuple[str, str]] = {
    DENY_CAUSE_POLICY: (
        "was blocked by a Kiro Crew safety policy",
        "use an allowed alternative (for a shell command, a read-only variant), use "
        "a different tool, or — if the block is correct and you genuinely cannot "
        "proceed — say so and stop with the reason.",
    ),
    DENY_CAUSE_INVALID_NAME: (
        "was refused because its tool name failed validation",
        "reissue the call with a name that passes validation. The action itself was "
        "never judged, so do not abandon it or look for a different approach on this "
        "evidence — and do not repeat the same malformed name.",
    ),
    DENY_CAUSE_HOOK_ERROR: (
        "could not be authorized because a PreToolUse hook raised while deciding it",
        "treat this as a host fault, not a verdict on the action: nothing judged the "
        "call itself. Retrying the identical call is reasonable once; if it faults "
        "again, say what happened rather than working around it silently.",
    ),
}


def build_refusal_steer_notice(
    title: str,
    reason: str,
    *,
    cause: str = DENY_CAUSE_POLICY,
    credential_tool_hint: str = "",
) -> str:
    """Body of the in-band deny notice steered into the RUNNING turn.

    Sent BEFORE the permission rejection goes back on the wire, which is what
    makes it race-free: while the ``session/request_permission`` is still
    unanswered the turn is provably in flight, so the steer is queued rather than
    dropped, and kiro-cli folds it in at the next model-inference boundary — the
    one immediately after the rejected tool resolves. The model therefore learns
    why inside the SAME turn and no recovery continuation is needed.

    The notice must correct an attribution the model has already been handed:
    a rejected permission is reported to the model as a generic tool failure with
    no channel for the host to say more (ACP's permission response carries only
    ``outcome``/``optionId``). Naming kiro-cli's exact wording — measured against
    kiro-cli 2.19.1 — is what lets the model overwrite the wrong conclusion rather
    than hold both, and attributing the quote to that backend keeps the sentence
    true on another steer-capable harness whose wording has not been measured.
    ``title``/``reason`` must already be redacted by the caller.

    *cause* selects the wording. The distinction is not cosmetic: a policy block
    is a verdict the model must route around, an invalid tool name is the model's
    own malformed output and is the one case it can simply fix, and a hook fault
    judged nothing at all. Telling the model "safety policy" for the latter two
    would send it looking for an allowed alternative to an action nobody refused.
    An unknown cause degrades to the policy wording rather than raising: a wrong
    noun is recoverable, and losing the notice would hand the model back
    kiro-cli's "user denied" with nothing to correct it.

    Returns "" when there is nothing to say, so a caller can treat the empty
    string as "no notice was sent" and fall back to the recovery continuation.
    """
    if not (title or "").strip() and not (reason or "").strip():
        return ""
    clause, guidance = _DENY_CAUSE_TEXT.get(cause, _DENY_CAUSE_TEXT[DENY_CAUSE_POLICY])
    what = f"{title}: {reason}" if reason else title
    # Class-specific remediation, for the policy cause only. The other two causes
    # judged nothing about the action — an invalid tool name is the model's own
    # malformed output and a hook fault is a host fault — so naming a sanctioned
    # alternative there would answer a question nobody asked and imply the action
    # itself had been refused.
    remediation = (
        remediation_for(reason, title, credential_tool_hint=credential_tool_hint)
        if cause == DENY_CAUSE_POLICY
        else ""
    )
    tail = f"\n\nHow to do this properly: {remediation}" if remediation else ""
    # "host notice", not "policy notice": the tag has to be true for all three
    # causes, and only one of them IS a policy. Naming the ACTOR is also what the
    # notice exists to do — the model has just been told the user denied this, and
    # every sentence after this one is spent correcting that.
    return (
        f"[Kiro Crew host notice] The tool call you just made {clause}. "
        "This was NOT a user action — the user did not "
        "cancel, reject, or interrupt anything. The tool result you were handed for "
        "it is generic and wrong about who denied it — on kiro-cli it reads "
        '"User denied tool execution".\n\n'
        f"Blocked: {what}\n\n"
        "Do not apologise for a cancellation and do not ask the user whether to "
        f"retry. Decide and continue in this same turn: {guidance}{tail}"
    )


def build_stale_recovery_prompt() -> str:
    """Body of the continuation injected after an auto-recovered stalled turn.

    A previous turn wedged: the ACP layer detected a genuinely stale turn (total
    stdout+stderr silence past the timeout), probed it via ``session/cancel``, got
    no ack, and the dashboard reset the session. The prior work already committed
    to the conversation is restored by ``session/load`` resume; this nudge tells
    the model to CONTINUE from that last committed step rather than restart the
    task from scratch. The caller prepends :data:`STALE_RECOVERY_PREFIX`. Framed
    as a system stall — NOT a user cancellation — so the agent doesn't stop.
    """
    return (
        "Your previous turn was interrupted by a system stall and has been "
        "automatically recovered. This was NOT a user action — do not treat it "
        "as a cancellation or interruption by the user. The work you already "
        "completed is preserved in the conversation above. Continue from where "
        "you left off and finish the task; do not restart it or repeat steps "
        "that already succeeded."
    )


# Shell output-redirection target, e.g. `> build.log` / `>> build.log`. The
# character class excludes `&` so fd-dup forms (`2>&1`, `>&2`) self-exclude.
_REDIRECT_TARGET_RE = re.compile(r">>?\s*([^\s;|&]+)")


def extract_log_redirect_target(command: str) -> str:
    """The first real file a shell command redirects output into, or "".

    Used by the tool-stall recovery nudge: when a long command redirected its
    output (long commands typically redirect, e.g. ``> build.log 2>&1``), the model
    should inspect that file's tail instead of blindly re-running the command.
    ``/dev/null`` and fd-dups (``2>&1``) are ignored.
    """
    for m in _REDIRECT_TARGET_RE.finditer(command or ""):
        target = m.group(1).strip("\"'")
        if not target or target == "/dev/null":
            continue
        return target
    return ""


def build_tool_stall_recovery_prompt(
    tool_title: str,
    idle_secs: int,
    command: str = "",
    stuck_input: bool = False,
) -> str:
    """Body of the continuation injected after a watchdog tool-stall cancel.

    The per-session watchdog judged an in-flight tool dead (its process exited
    without a result frame), stuck on interactive input, or opaque past the
    UNKNOWN budget, and cancelled the session's turn. This nudge is a SYSTEM
    action — NOT a user cancellation — and replaces the legacy behavior of
    re-queuing the original user message verbatim (which restarted the entire
    task and re-ran the very command that stalled). The caller prepends
    :data:`TOOL_STALL_RECOVERY_PREFIX`.
    """
    idle_mins = max(1, round(idle_secs / 60))
    tool_label = tool_title or "a tool call"
    lines = [
        f"Your previous turn stalled: {tool_label} produced no response for "
        f"~{idle_mins} minute(s) and the turn was ended by a Kiro Crew watchdog. "
        "This was NOT a user action — do not treat it as a cancellation or "
        "interruption by the user.",
        "",
        "Before doing anything else, check whether the tool actually completed "
        "or left partial results — do NOT blindly re-run the whole task or "
        "repeat steps that already succeeded.",
    ]
    log_target = extract_log_redirect_target(command)
    if log_target:
        lines += [
            "",
            f"The command's output was redirected to `{log_target}` — inspect it "
            "with tail (last ~50 lines); do NOT cat the whole file.",
        ]
    if stuck_input:
        lines += [
            "",
            "The command appeared to be waiting for interactive input it will "
            "never receive. Re-run it non-interactively (e.g. with -y, "
            "--no-input, or </dev/null) instead of repeating it as-is.",
        ]
    lines += [
        "",
        "Then continue the task from where you left off.",
    ]
    return "\n".join(lines)


# [OPTIONS: a | b | c] — the marker ends a LINE here, so use the MULTILINE/
# single-line canonical parser. Defined once in constants.py (shared with
# slack/format.py and the renderer surfaces) so the ReDoS-hardened grammar can
# never drift between copies; see OPTIONS_RE_LINE for the full rationale
# (tempered body, ``\n`` exclusion under MULTILINE). Per-choice whitespace is
# stripped by the caller; dashboard pills and Slack buttons parse OPTIONS
# identically because they share this exact object.
_OPTIONS_RE = OPTIONS_RE_LINE


def _redact(text: str) -> str:
    """Sanitise LLM output before surfacing to dashboard."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _parse_options(text: str) -> list[str]:
    """Extract pipe-separated choices from the LAST [OPTIONS: A | B | C] in text."""
    matches = _OPTIONS_RE.findall(text)
    if not matches:
        return []
    parts = [p.strip() for p in matches[-1].split("|")]
    return [p for p in parts if p]


VALID_MEMORY_MODES = ("persistent", "incognito", "temporary")


def _ascii_slot_key(name: str) -> str:
    """Return *name* with any character outside printable ASCII replaced by ``-``.

     A slot key becomes the session key (``dashboard:{slot.key}``) that
     kirocrew-core sends as the ``X-Session-Key`` HTTP header on every gateway
     call. Header values are latin-1 per RFC 7230, so a non-latin-1 char (e.g.
     an em-dash from a title-derived slot name) would abort every tool call
    . ASCII control characters (notably CR/LF) are excluded too, so
     a name can never inject into or split the header. Idempotent;
     printable-ASCII names — including the auto-generated ``chat-N-<ts>`` keys —
     are returned unchanged. (Path-separator/traversal containment for keys later
     used as filesystem paths is enforced separately at the persistence layer.)
    """
    return re.sub(r"[^\x20-\x7e]", "-", name)


# Characters that survive the history layer's ``_safe_key()`` filename fold
# (``re.sub(r"[^\w\-.]", "_", key)``). ``re.ASCII`` pins ``\w`` to
# ``[a-zA-Z0-9_]`` — the input is already ASCII-folded, so this matches what
# ``_safe_key`` produces byte-for-byte.
_SLOT_KEY_FILENAME_UNSAFE_RE = re.compile(r"[^\w\-.]", flags=re.ASCII)


def _normalize_slot_key(name: str) -> str:
    """Return *name* folded to the exact charset of a persisted session filename.

    Guarantees the invariant a restart depends on: for any input,
    ``_safe_key(_history_key_for(key))`` == ``f"dashboard_{key}"`` — i.e. the
    slot key equals its JSONL filename stem minus the ``dashboard_`` prefix.

    Three steps compose: strip a ``dashboard:``/``dashboard_`` transport
    prefix (a full session key or filename stem sometimes reaches slot-name
    positions; ``_history_key_for`` strips the same prefixes when building the
    history key, so such names already share one transcript with their bare
    form and must share one slot), then :func:`_ascii_slot_key` (header
    safety), then a filename fold using the same character class as
    ``history._safe_key``.

    Without the filename fold, a display-style slot name (e.g.
    ``Artifact: My Doc`` from the artifact iterate flow) diverges from its
    sanitized filename stem. After a gateway restart, ``restore_open_slots``
    rehydrates the raw key from ``open_slots.json`` while
    ``restore_recent_sessions`` derives a second slot from the filename stem —
    the dedup guards compare mismatched strings, so the user sees two
    identical sidebar sessions backed by one transcript, and the next
    ``_persist_open_slots`` flush cements both keys. Idempotent;
    auto-generated ``chat-N-<ts>`` keys are returned unchanged.
    """
    if name.startswith("dashboard:"):
        name = name[len("dashboard:") :]
    while name.startswith("dashboard_"):
        name = name[len("dashboard_") :]
    return _SLOT_KEY_FILENAME_UNSAFE_RE.sub("_", _ascii_slot_key(name))


class SlotOrigin:
    """Slot creation origin — who initiated the slot.

    Used by the WS event scope gate to decide which events an app token may
    receive (e.g. ``slots:user`` grants visibility into ``USER``-origin slots
    regardless of their ``_app`` owner).
    """

    USER = "user"  # initiated from the dashboard UI (no app token)
    APP = "app"  # initiated by an app SDK call (carries owner _app)
    CRON = "cron"  # initiated by a cron job
    SYSTEM = "system"  # gateway-internal (startup, migration, etc.)


def request_slot_origin(app: str) -> str:
    """Origin for a slot created while serving an HTTP request.

    The request layer is the only place that knows whether an app token was
    presented, which is what separates APP from USER. Call it with the
    request's app name (``request.get("app", "")``) — empty means the caller
    authenticated as the dashboard user, so the slot genuinely is USER.

    Background callers (cron, workflow, Slack, rehydrate) must NOT use this:
    they have no request and would mislabel their slot as a person's, which is
    exactly what `slots:user` grants an app access to. They declare their own
    origin, or leave it untagged.
    """
    return SlotOrigin.APP if app else SlotOrigin.USER


class _ChatSlot:
    """Independent chat session that runs server-side."""

    __slots__ = (
        "_buffers",
        "_projection",
        "_queue_repository",
        "_source_links_cache",
        "_source_links_revision",
        "key",
        "title",
        "agent",
        "model",
        "_model_withheld",
        "_model_withheld_for",
        "reasoning_effort",
        "autocompact_pct",
        "mode",
        "workspace",
        "project",
        "created_at",
        "messages",
        "total_messages",
        "_task",
        "_turn_generation",
        "event",
        "_pending",
        "_pending_consumers",
        "_pending_release_deferred",
        "_queue",
        "_last_enqueue_ts",
        "_approval_futures",
        "_trust",
        "_trust_scope",
        "_trust_reads",
        "_trusted_patterns",
        "_titled",
        "_title_origin",
        "_title_epoch",
        "_title_refresh_mark",
        "_auto_tagged",
        "_title_in_flight",
        "_title_retry_pending",
        "_summary_in_flight",
        "_summary_turn_mark",
        "_detail_render_lock",
        "_last_stop_reason",
        "_created_by",
        "_artifact",
        "_channel_folder_filed",
        "_resumed_count",
        "_hook_continuation_depth",
        "_todo",
        "_mcp_report",
        "_mcp_report_session_id",
        "_on_message",
        "_on_question_retired",
        "_has_reader_flag",
        "_stop_state_raw",
        "_stop_generation",
        "_stop_event_id",
        "_stop_escalated_card_id",
        "_pending_reset_history_key",
        "_pending_discard_conversation_key",
        "_eager_spawn_task",
        "_prefetch_ttl_task",
        "_dirty_flag",
        "_dirty_gen",
        "_metadata_persist_inflight",
        "_orch_tracker",
        "_plan_cancelled",
        "_auto_run",
        "_in_stage_execution",
        "_queue_held",
        "_recovery_chat_triggered",
        "_stage_titles",
        "_stage_descriptions",
        "_plan_goal",
        "_slack_linked",
        "_slack_channel",
        "_slack_thread_ts",
        "channel_origin",
        "folder_id",
        "_folder_changed",
        "_folder_suggested",
        "pinned",
        "tags",
        "_pending_subagent_failures",
        "_pending_synthesis",
        "_synthesis_inflight",
        "_subagent_deliveries_inflight",
        "_subagents_inline_collected",
        "_subagent_delivery_pending",
        "_recovery_retrigger_count",
        "_prompt_busy_retries",
        "_acp_pipe_death_retries",
        "_stale_recovery_retries",
        "_stale_recovery_exhausted_emitted",
        "_tool_stall_retries",
        "_tool_stall_exhausted_emitted",
        "_transient_5xx_retries",
        "_fallback_candidate_idx",
        "_fallback_walked",
        "_active_fallback_model",
        "_fallback_primary_model",
        "_fallback_slot_model",
        "_model_pick_gen",
        "_fallback_pick_gen",
        "_posttoken_retry_used",
        "_prestream_exhausted_cycles",
        "_poisoned_reset_used",
        "_empty_response_retries",
        "_promise_only_retries",
        "_promise_only_stop_gen",
        "_compaction_continue_retries",
        "_batch_rejected",
        "_compaction_failed_retries",
        "_compaction_fail_streak",
        "_compaction_fail_cooldown_until",
        "color_index",
        "color_hex",
        "color_theme",
        "theme_consent",
        "theme_consent_sha",
        "memory_mode",
        "_ephemeral",
        "_pending_context",
        "_deferred_notes",
        "_app",
        "_human_seen",
        "_origin",
        "_pending_variants",
        "_lock",
        "forked_from",
        "_fork_lock",
        "_model_pick_lock",
        "_remote_pick_lock",
        "_tab_id",
        "_channel_window_mtime",
        "_disk_older_count",
        "_disk_older_durable_count",
        "_disk_window_len",
        "_disk_meta_created_at",
        "_disk_meta_observed",
        "_disk_tail_ts",
        "_frozen_prefix_cache",
        "_pending_rewrite",
        "_file_changes",
        "linked_session_key",
        # Remote-execution binding: this slot lives in the LOCAL list and local
        # history, but its turns run on a connected peer crew. See
        # ``dashboard/remote_relay.py``.
        "executor",
        "instance_id",
        "remote_slot",
        "_relay_in_flight",
        "_active_turn_session_key",
        "_side",
        "_acp_client",
        "_last_turn_awaiting_permission",
        "_last_turn_children_announced",
        "_steer_segment_cut",
        "_native_subagent_tracker",
        "_native_subagent_output",
        "_steer_confirmed",
        "_pending_steers",
        "_steer_delivery_ids",
        "_steer_send_ids",
        "_wait_state",
        "_end_wait_request",
        "_wait_last_ping",
        "_wait_steer_baseline",
        "_wait_contested",
        "_question_pending",
    )

    def __init__(
        self,
        key: str,
        title: str = "",
        agent: str = "",
        workspace: str = "default",
        model: str = "",
        mode: str = "",
        memory_mode: str = "persistent",
        ephemeral: bool = False,
    ) -> None:
        self.key = key
        self.title = title or key
        self.agent = agent
        self.model = model
        # Spawn-time withhold verdict for `model`, and the model id it was
        # computed for. Read through the `model_withheld` property, never these
        # two directly: the pairing is what makes the verdict self-invalidating
        # when any of slot.model's writers re-pins the slot.
        self._model_withheld: bool = False
        self._model_withheld_for: str = ""
        # Reasoning effort: "" = provider default, else one of low/medium/high/max.
        # Currently consumed by an alternate ACP backend (--effort flag); ACP wired later.
        self.reasoning_effort: str = ""
        # Per-session auto-compact threshold override (percent). None = follow
        # the global session.autocompact_pct. Persisted with the slot and
        # re-seeded into the SessionManager after restore.
        self.autocompact_pct: float | None = None
        # "" = default chat, "orchestrator" = orchestrated chat
        self.mode = mode
        self.workspace = workspace
        self.project: str = ""
        # Remote-execution binding. ``executor`` is "local" for every ordinary
        # slot; "remote" means the turn is dispatched over an instance tunnel to
        # ``instance_id`` and run by the peer's slot ``remote_slot``. The local
        # side still owns the transcript, the sidebar row and history — only
        # execution moves. Fail-closed: a slot whose executor says "remote" but
        # whose instance_id or remote_slot is empty refuses to dispatch rather
        # than silently falling back to running the turn on this machine, which
        # would put the peer's work on the wrong host.
        self.executor: str = "local"
        self.instance_id: str = ""
        self.remote_slot: str = ""
        # True only while a remote turn is executing on the peer. Persisted (with
        # the binding) so a gateway crash mid-turn is detectable on reload: a slot
        # that comes back still carrying it lost its relay reader to the restart,
        # and rehydration appends an "interrupted" row rather than leaving the
        # transcript silently stopped. Set/cleared in ``remote_relay.relay_remote_turn``.
        self._relay_in_flight: bool = False
        self.created_at: str = datetime.now(timezone.utc).isoformat()
        self.messages: list[dict[str, Any]] = []
        self._buffers = SlotBufferCoordinator()
        self._projection = SlotProjection()
        self._queue_repository = SlotQueueRepository(
            id_provider=lambda: uuid.uuid4().hex[:12],
            timestamp_provider=lambda: datetime.now(timezone.utc).isoformat(),
            delivery_key=lambda content: _delivery_key(content),
            max_pending_deliveries=lambda: _MAX_PENDING_SUBAGENT_DELIVERIES,
        )
        # (content revision, links) cache for the sidebar PR chips scan.
        self._source_links_revision = 0
        self._source_links_cache: tuple[tuple[int, int], list[dict]] | None = None
        self.total_messages: int = 0  # lifetime count (survives trimming)
        self._task: asyncio.Task[Any] | None = None
        # Monotonic publication history for turn ownership. ``task`` returns to
        # None after teardown, so consumers that span awaits cannot distinguish
        # "stayed idle" from "ran and finished" by comparing task references.
        self._turn_generation: int = 0
        self.event = asyncio.Event()
        self._pending: list[dict[str, str]] = []
        # Number of readers currently treating ``_pending`` as their delivery
        # queue -- see ``pending_consumer``. Zero means a row left in the queue
        # can never reach a client, which is what makes releasing it safe.
        self._pending_consumers: int = 0
        # Set when a release was ASKED FOR and refused because a consumer held
        # the queue. Without it the refusal is silent and final: the turn-end
        # purge never runs again for that slot, so the rows it declined to drop
        # outlive every consumer and the leak survives its own fix.
        self._pending_release_deferred: bool = False
        self._queue: list[dict[str, Any]] = []  # [{"id": uuid, "content": str}, ...]
        # Newest enqueue instant, read only while ``_queue`` is non-empty — see
        # ``_note_enqueue``.
        self._last_enqueue_ts: str = ""
        self._approval_futures: dict[str, asyncio.Future[str]] = {}  # type: ignore[type-arg]
        self._trust: bool = False  # auto-approve tools for this slot
        # SafetyOverride scope key holding an EXPIRING, SEL-audited auto-approve
        # grant, for an unattended app worker with no human present to click
        # "trust this session". Empty on an ordinary session, and empty is what
        # makes the approval path ignore it entirely. Never a substitute for
        # ``_trust``: this names where the live decision is held, it is not itself
        # the decision — ``safety_override().is_scope_active()`` is.
        self._trust_scope: str = ""
        self._trust_reads: bool = False  # auto-approve read-only bash commands
        self._trusted_patterns: set[str] = set()  # session-scoped fnmatch globs
        self._titled: bool = False  # True once a title has been assigned
        # Provenance of the current title: "auto" (LLM auto-titler or its
        # fallback) or "user" (manual rename). Governs the background title
        # REFRESH: only "auto" titles are ever refreshed, so a manual rename is
        # final. Persisted as ``title_origin`` and rehydrated in
        # chat_persistence; a legacy title with no stored origin rehydrates as
        # "user" so a possibly-manual name is never rewritten. "" = untitled.
        self._title_origin: str = ""
        # Monotonic counter bumped on every EXPLICIT title assignment (manual
        # rename or the manual generate-title endpoint). Background title tasks
        # snapshot it before generating and re-check it after each await, so an
        # explicit title landing mid-generation is never overwritten (see
        # chat_title._maybe_auto_title / maybe_refresh_title).
        self._title_epoch: int = 0
        # User-message count at the last background title refresh ATTEMPT (0 =
        # never refreshed). Each milestone in chat_title._TITLE_REFRESH_MILESTONES
        # fires at most once, attempt-counted (a KEEP/SKIP/error consumes it), so
        # the refresh token budget is hard-bounded. Persisted so a gateway
        # restart cannot re-spend consumed milestones.
        self._title_refresh_mark: int = 0
        self._auto_tagged: bool = False  # True once auto-tag has been attempted
        # Guards against concurrent LLM auto-title attempts (on-send trigger vs
        # the end-of-turn chat_done trigger racing on the same slot).
        self._title_in_flight: bool = False
        # Records a chat_done retry that arrived during the on-send attempt.
        self._title_retry_pending: bool = False
        # Excludes concurrent session-summary generations for this slot. A
        # summary pass outlives the turn that triggered it, so a fast follow-up
        # turn would otherwise start a second pass over the same transcript.
        self._summary_in_flight: bool = False
        # Serializes the slot-detail render offload (see api_chat_slot_detail):
        # rendering redacts the ENTIRE history with a regex battery, so on a
        # multi-MB session two concurrent refetches (WS reconnect + switchSlot)
        # would burn that CPU twice in parallel worker threads for the same
        # payload. The lock queues them instead; each holder re-renders from
        # fresh state, so a queued waiter never serves a stale response.
        self._detail_render_lock = asyncio.Lock()
        # User-turn count at the last successful summary, so the configured
        # regeneration cadence can be honored without re-reading the sidecar.
        self._summary_turn_mark: int = 0
        # Stop reason of the most recently completed turn. Recorded because a
        # turn that ended on a timeout, cancel or tool stall did not really
        # finish, and deriving anything from it would describe work that was
        # interrupted mid-flight as if it had concluded.
        self._last_stop_reason: str = ""
        #: The resolved slot key of the caller that asked for this session via the
        #: session-control create verb, or "" for a slot nobody asked for -- a
        #: person's own tab, a fork, a restore. Read by
        #: ``DashboardState.creator_slot_count`` for ``MAX_SLOTS_PER_CREATOR``.
        self._created_by: str = ""
        # Artifact companion binding: set when this slot is a
        # companion chat session for an artifact (slug). At most one
        # non-archived slot per slug by convention — the frontend flow
        # maintains the invariant (archive-then-create); the backend accepts
        # any valid slug and does not enforce uniqueness. This IS serialized
        # (to_dict) and persisted (history meta) — the dashboard resolves the
        # active binding from the slots snapshot, and the binding must survive
        # gateway restarts.
        self._artifact: str = ""
        # True once per-channel default filing has been APPLIED to this
        # conversation (see kiro_crew.dashboard.channel_folders). Persisted,
        # because it is the only durable record that the automatic placement
        # already happened: `folder_id` is omitted from the metadata line when
        # empty, so a conversation the user drags out to the top level is
        # otherwise indistinguishable from one that was never filed, and the
        # next reconcile pass after a restart would file it right back in.
        # Default filing is a first-surface action, not a recurring one.
        self._channel_folder_filed: bool = False
        self._resumed_count: int = 0  # messages loaded from history on resume
        # Depth of the current unbroken Stop-hook continuation run: 0 on a normal
        # turn, incremented on each consecutive hook-continuation turn, reset by
        # any turn that is not a hook continuation. Surfaced to Stop hooks as
        # `hook_continuation_count` for diagnostics or stricter hook-owned limits.
        self._hook_continuation_depth: int = 0
        # Agent-authored TODO list, replaced wholesale from each todo_list tool
        # result (every command echoes the full list, so there is nothing to
        # merge). Shape: {description: str, tasks: [{id, text, completed}]}.
        # None = the agent has never used its todo tool in this slot, which the
        # UI renders as "no pill" rather than "an empty list".
        self._todo: dict[str, Any] | None = None
        # What THIS slot's agent session reported about its MCP servers, as
        # published by the ACP layer at session init and updated by later
        # registration frames. None = this slot has no live session that
        # reported, which the UI must render as absence of knowledge — NOT as
        # "no servers". Cleared on session reset: the report is evidence, and a
        # report describing a torn-down session is worse than none.
        self._mcp_report: dict[str, Any] | None = None
        # The session the cached report describes — see set_mcp_report.
        self._mcp_report_session_id: str = ""
        # Callback for broadcasting messages via global SSE
        self._on_message: object | None = None  # Callable[[str, dict], None] | None
        # Announce stateless question cards this slot retires, so every client
        # drops them: Callable[[str, list[str]], None] | None, wired by
        # DashboardState like _on_message. A retirement that only mutates state
        # is invisible to a second window, and to a /pending response already in
        # flight — either would re-render a card whose answer has been sent.
        self._on_question_retired: object | None = None
        self._has_reader_flag: bool = False  # True when HTTP SSE stream is draining
        self._stop_state_raw: str = "idle"  # 'idle' | 'soft_pending' | 'killing'
        # Monotonic count of stop INITIATIONS (idle → active edges of
        # _stop_state). Teardown resets _stop_state back to "idle" but never
        # touches this, so long-running decision points (the poisoned-
        # conversation canary probe) can capture it before a wait and detect
        # a Stop that fired AND resolved during the wait — re-reading
        # _stop_state alone would miss it (the exact race documented in
        # chat_handlers._make_stop_resolver).
        self._stop_generation: int = 0
        self._stop_event_id: str | None = None  # transcript message id for in-flight stop
        # Id of the stop card the user escalated to a hard kill, or None. Kept
        # separate from `_stop_state` because turn teardown resets that back to
        # "idle" (see the `_stopping` setter below), which would erase the
        # escalation and let a late cooperative ack relabel the card as a clean
        # stop. Holds an id rather than a bool so the marker cannot leak onto a
        # later card: a boolean left set would make the NEXT card's cooperative
        # ack defer to a hard callback that never fires, stranding it at
        # "stopping". Every card has a fresh uuid, so a stale id simply stops
        # matching and no card-open path has to remember to clear it.
        self._stop_escalated_card_id: str | None = None
        # Set by api_chat_slot_project; consumed in _run_chat instead of
        # inline because the endpoint can be reached from inside the kiro-cli
        # process group via the set_project MCP tool.
        self._pending_reset_history_key: str | None = None
        # Set by the reset_conversation directive; consumed alongside the
        # project-change reset above. Deferred for the same reason: the tool is
        # called from inside the turn it wants to end, and the immediate route
        # refuses a busy slot rather than tearing down a turn mid-write.
        self._pending_discard_conversation_key: str | None = None
        # Debounced speculative session-creation task (session.eager_spawn).
        # At most one per slot: scheduling a new one cancels the previous, so
        # rapid signals (create + project set) collapse into a single spawn.
        self._eager_spawn_task: asyncio.Task[None] | None = None
        # Unclaimed-prefetch teardown timer (resume prefetch). At most one per
        # slot: a newer resumed prefetch cancels the previous timer.
        self._prefetch_ttl_task: asyncio.Task[None] | None = None
        self._dirty_flag: bool = False  # True when messages changed since last flush
        # Bumped by the _dirty setter on every True. Lets the periodic flush tell
        # "the True I started this save under" from "a NEW True set during it".
        self._dirty_gen: int = 0
        # A guarded metadata write has changed this live slot but has not yet
        # committed.  The periodic writer must not serialize that provisional
        # state to an unpinned transcript while the guarded write waits.
        self._metadata_persist_inflight: int = 0
        self._orch_tracker: Any = None  # OrchestrationTracker, set by gateway
        # Plan-cancel latch closing the cancel/Go race (#6046): the Cancel
        # handler can only stop a tracker that exists, but _stage_loop creates
        # the tracker lazily, so a cancel processed between a Go POST being
        # accepted and its _stage_loop coroutine starting would no-op on the
        # tracker and the plan would advance anyway. The handler sets this flag
        # unconditionally; _stage_loop checks it before creating a tracker and
        # exits without advancing. Cleared ONLY when a new plan is armed
        # (_reset_auto_run_for_new_plan) — never on Go, so a Go on a cancelled
        # plan cannot resurrect it (that would just invert the race).
        self._plan_cancelled: bool = False
        self._auto_run: bool = False  # "Go All" — skip stage gates
        # True only while _stage_loop is driving a stage-execution turn. Gates
        # the end-of-turn plan detector so a stage turn whose output happens to
        # contain plan-like text cannot re-arm / re-count the plan (which
        # corrupted the stage total and produced "Stage N of M" over-runs).
        # It ALSO gates mid-plan message handling: while set, api_chat queues a
        # user message (chip card) even when slot.task is momentarily idle between
        # stages, and _start_next_queued_turn HOLDS user messages (recovery/system
        # still drain) until the plan ends — so autopilot reuses the normal-chat
        # queue/chip path without changing slot.task / slot.running semantics.
        self._in_stage_execution: bool = False
        # Whether the queue is HELD rather than drained; False drains. Set by
        # _run_chat's teardown to that turn's outcome, and read by every drain gate:
        # _run_chat's own tail, the synthesis dispatch in _finish_queue_cycle, and
        # the orchestrator's two handoffs (_exit_cancelled_plan, _stage_loop's
        # finally). When a turn has proved every QUEUED prompt would fail
        # identically, those prompts must not be popped into repeat failures — they
        # stay queued (visible, individually cancellable) and resume on the user's
        # next send. The claim is about the QUEUE, not about this turn having run
        # nothing: two causes are discovered before a turn runs, but a deferred
        # project reset is discovered at the END of a turn that completed normally.
        #
        # ONE predicate rather than one flag per cause, deliberately. Each cause as
        # its own boolean is hand-wired into all four gates, so adding one means
        # editing four conditions and MISSING one silently dequeues and burns
        # prompts — which is exactly how the synthesis and stage-handoff gaps got
        # shipped. A new cause now composes by setting this flag, and a new drain
        # site composes by asking this one question.
        #
        # A BOOLEAN and not a reason string: the causes today are a signed-out CLI,
        # a queued project change refused before the turn, and one left deferred
        # after it — and no gate, log line or test ever asked which of them it was.
        self._queue_held: bool = False
        self._recovery_chat_triggered: bool = False  # guard against concurrent failure recovery
        self._stage_titles: list[str] = []  # stage titles extracted from plan
        self._stage_descriptions: list[list[str]] = []  # bullet points per stage
        self._plan_goal: str = ""  # goal from 📋 Plan for: header
        self._slack_linked: bool = False  # True when linked to a Slack thread
        self._slack_channel: str = ""
        self._slack_thread_ts: str = ""
        self.folder_id: str = ""  # project folder assignment
        self._folder_changed: bool = False  # re-inject [FOLDER] breadcrumb next turn after move
        # One-shot claim for the post-titling folder suggestion (see
        # chat_folder_suggest.maybe_suggest_folder). In-memory only: a restored
        # slot is already titled, so the suggestion hook never re-fires for it
        # and a reset flag cannot produce a second card.
        self._folder_suggested: bool = False
        self.pinned: bool = False  # pinned to top of sidebar
        self.tags: list[str] = []  # assigned tag ids (see DashboardState._tags)
        self._pending_subagent_failures: list[str] = []
        # Fix 2 (B1): armed by gateway when the LAST sub-agent of a fan-out
        # completes; consumed once by chat_runner's drain/idle branch to fire a
        # single post-fan-out synthesis turn. Cleared if a user message drains
        # first (user takes over).
        self._pending_synthesis: bool = False
        # True while chat_runner owns the one readiness-wait/synthesis task.
        # Kept separate from _pending_synthesis so readiness loss does not
        # consume the one-shot request or permit duplicate waiters.
        self._synthesis_inflight: bool = False
        # Fix 2 (B1) race guard: number of sub-agent completion deliveries
        # currently in flight for this slot (incremented in gateway._subagent_done
        # from entry until the completion is queued/launched). The synthesis
        # fire-gate requires this to be 0 so a concurrently-finishing sibling
        # can't let an earlier turn fire synthesis before its result lands.
        self._subagent_deliveries_inflight: int = 0
        # IDs of sub-agents whose results were already delivered inline via the
        # blocking spawn_sub_agents MCP tool.  _subagent_done skips injection
        # for these to prevent a duplicate turn that clobbers [OPTIONS:] buttons.
        self._subagents_inline_collected: set[str] = set()
        # Queued sub-agent completions whose delivery tombstone is still owed:
        # queue-id -> the agent ids whose ``result.txt`` that row promises. A
        # completion routed into a BUSY slot is queued, so the parent's context
        # does not contain it until the row drains; the run loop therefore skips
        # its own ``mark_delivered`` and the drain settles these instead, so the
        # retention TTL is measured from consumption rather than from run
        # completion (issue #4839). See ``take_pending_subagent_deliveries``.
        self._subagent_delivery_pending: dict[str, list[str]] = {}
        self._recovery_retrigger_count: int = 0
        self._prompt_busy_retries: int = 0
        self._acp_pipe_death_retries: int = 0
        # Auto-recovery of a genuinely-wedged (stale) turn: bumped when the ACP
        # layer signals STOP_REASON_STALE_RECOVER; bounded (3) so a permanently
        # broken session surfaces "start a new chat" instead of looping. Reset on
        # a completed turn (alongside the other retry budgets).
        self._stale_recovery_retries: int = 0
        # Tool-stall recovery: bumped when the ACP layer ends a turn with
        # STOP_REASON_TOOL_STALL. A SEPARATE budget from pipe-death (the legacy
        # path charged stalls against _acp_pipe_death_retries and re-queued the
        # original message verbatim — one false positive burned the whole
        # session budget). Bounded (3); reset on a completed turn.
        self._tool_stall_retries: int = 0
        # Telemetry dedup for the exhausted outcome: set when outcome=exhausted
        # is emitted for the corresponding budget, cleared when the budget
        # resets on a completed turn. Keeps a repeatedly-stalling wedged slot
        # from re-emitting "exhausted" every stall, and keeps a later ok turn
        # from mis-emitting "recovered" for an already-exhausted cycle —
        # WITHOUT mutating the budget itself (a wedged slot stays terminal
        # until a turn actually completes; it never re-enters a fresh
        # recovery cycle just because the metric fired).
        self._stale_recovery_exhausted_emitted: bool = False
        self._tool_stall_exhausted_emitted: bool = False
        # Transient backend 5xx (InternalServerError / DispatchFailure /
        # ConnectionReset) retries on the interactive stream path. Distinct
        # budget from prompt-busy / pipe-death; reset on a completed turn.
        self._transient_5xx_retries: int = 0
        # Throttle-exhaustion model-fallback walk state (agent.fallback_model).
        # _fallback_candidate_idx / _fallback_walked are PER-CYCLE (next chain
        # position to try + candidates already tried this logical turn, for the
        # chain-exhausted error story); both reset with the other retry budgets
        # on a landed turn. _active_fallback_model / _fallback_primary_model are
        # STICKY session state: set when a fallback swap lands, kept across
        # turns until the start-of-turn restore probe moves the session back to
        # the primary (deliberately NOT reset on turn completion).
        self._fallback_candidate_idx: int = 0
        self._fallback_walked: list[str] = []
        self._active_fallback_model: str = ""
        self._fallback_primary_model: str = ""
        # Snapshot of slot.model taken when the fallback activated, used to heal
        # slot.model if the automatic provider backfill wrote the fallback id
        # into an empty slot while the fallback was active (slot.model is
        # re-sent as a set_model override on resume, so leaving the fallback
        # there would re-pin it after the primary recovered).
        self._fallback_slot_model: str = ""
        # Explicit model-pick generation. Bumped ONLY by the explicit set-model
        # surfaces (single-slot pick, bulk switch, provider-switch clear) —
        # never by the automatic provider backfill — so the fallback restore
        # probe can tell a genuine user pick (drop sticky state, never
        # override) from the backfill writing the served fallback into an
        # unpinned slot (heal and restore). _fallback_pick_gen is the value
        # snapshotted when the fallback activated.
        self._model_pick_gen: int = 0
        self._fallback_pick_gen: int = 0
        # One-shot guard for the post-token (text-only) transient retry: a turn
        # that has already streamed answer tokens may be re-prompted at most
        # ONCE on a transient 5xx (and only when no tool call fired). Reset on a
        # completed turn alongside _transient_5xx_retries.
        self._posttoken_retry_used: bool = False
        # Poisoned-conversation escalation (cross-cycle). A cycle that EXHAUSTS
        # the transient-5xx ladder with ZERO output counts one pre-stream
        # exhaustion; consecutive exhausted cycles indicate the backend is
        # deterministically rejecting this session's persisted conversation
        # (not a momentary blip — a fresh session on the same gateway works).
        # At the threshold, chat_runner discards the native conversation
        # (clearing the poisoned resume sid, keeping the session-map entry)
        # and re-queues once. Streak broken only by a LANDED turn or a
        # non-matching terminal error.
        self._prestream_exhausted_cycles: int = 0
        # One-shot guard for that discard+retry: consumed when a discard is
        # enqueued, re-armed only by a LANDED turn — so a genuine prolonged
        # outage gets at most one fresh-conversation attempt, never a
        # discard loop.
        self._poisoned_reset_used: bool = False
        self._empty_response_retries: int = 0
        # One bounded synthetic continuation when a turn ended on a promise-only
        # final message (announced an immediate action, then yielded with no tool
        # call). Reset like the other per-turn retry budgets on a landed turn.
        self._promise_only_retries: int = 0
        # Monotonic _stop_generation snapshot taken when a promise-only continuation
        # is enqueued; the dispatch-point purge compares against it to catch a Stop
        # that pressed AND resolved to idle while the continuation waited (#2696).
        self._promise_only_stop_gen: int = 0
        # One bounded synthetic continuation when the BACKEND compacted the
        # conversation mid-turn and then ended the turn without finishing the
        # work (see COMPACTION_RECOVERY_PREFIX). Bounded separately from the
        # promise-only budget: the two failure modes are independent, and a turn
        # that hits one must not be denied recovery from the other. Reset like
        # the other per-turn retry budgets on a landed turn.
        self._compaction_continue_retries: int = 0
        self._batch_rejected: bool = False
        # Per-turn compaction-status failure tracking (Mesh compaction-spam
        # fix). Distinct from SessionManager._compact_cooldown_until, which
        # only gates the *proactive* session-level auto-compact trigger —
        # this gates the per-turn EVENT_COMPACTION_STATUS notice path in
        # chat_runner, which previously had no backoff at all and could
        # append one near-identical "Compaction failed: unknown error"
        # message per turn indefinitely.
        # Retry budget for a turn the backend abandoned after a TRANSIENT
        # compaction failure. Distinct from _compaction_fail_streak above,
        # which only paces the NOTICE: this one bounds how many times the
        # abandoned message is re-queued. Reset on a landed turn alongside the
        # other recovery budgets.
        self._compaction_failed_retries: int = 0
        self._compaction_fail_streak: int = 0
        self._compaction_fail_cooldown_until: float = 0.0
        self.color_index: int | None = None
        # Custom per-session color (#rrggbb, lowercase). Mutually exclusive
        # with color_index: the PATCH handler clears one when the other is
        # set, and the frontend renders color_hex with priority. Unlike
        # color_index (resolved against the viewer's generated palette, so it
        # follows theme/palette switches), a custom hex is deliberately
        # frozen.
        self.color_hex: str | None = None
        self.color_theme: str = ""
        # Explicit user consent for the active INSTALLED theme's experience
        # layer (persona injection is gated on this; fail-closed default).
        self.theme_consent: bool = False
        # Content-bound persona consent: sha256 hex of the installed pack's
        # persona text the user granted in the consent modal. Persona injection
        # requires this to match the persona read from disk (fail-closed None).
        self.theme_consent_sha: str | None = None
        if memory_mode not in VALID_MEMORY_MODES:
            raise ValueError(
                f"invalid memory_mode {memory_mode!r}, must be one of {VALID_MEMORY_MODES}"
            )
        self.memory_mode: str = memory_mode
        self._ephemeral: bool = ephemeral  # Incognito mode: no memory writes
        self._pending_context: list[dict[str, Any]] = []
        self._deferred_notes: list[dict[str, Any]] = []
        self._app: str = ""  # App identity tag (App Kit §5.2)
        # FIX 1 (unattended approval park). Evidence that a HUMAN has driven
        # this slot through a dashboard-user route (typed a message, answered an
        # approval). Only ever set by a caller with an empty ``request_app``, so
        # an app cannot forge it. It is the escape hatch on ``unattended``: an
        # app-owned tab a person is actually working in gets the full 2h
        # approval window back from their first interaction onward.
        #
        # PERSISTED (``human_seen`` in the session metadata, restored by both
        # slot-restore paths) and monotonic — it only ever goes False → True, so
        # it needs no clearing rule and the ``auto_tagged`` once-flag beside it
        # is the shape to copy. Persistence is load-bearing rather than tidy: a
        # gateway restart is not evidence that the person left. It happens on
        # every upgrade and every crash, the browser tab reconnects to the same
        # slot, and without the flag on disk that tab's approval window silently
        # collapses from 2h to the 180s deny-fast — a behaviour change for EVERY
        # app-owned session, not just for worker fleets. The fast deny still
        # covers every app-owned slot no human has ever touched, which is what
        # a crew, a cron worker and an app-spawned session all are.
        self._human_seen: bool = False
        # Deliberately "" (not USER): a slot built outside get_or_create_slot
        # matches NO slots:* scope, so it stays invisible to app tokens rather
        # than being silently classified as user-initiated. Deny-by-default.
        self._origin: str = ""
        # Regenerate feature: variants pending attachment to next finalized assistant message
        self._pending_variants: list[dict] = []
        self._lock = asyncio.Lock()
        self.forked_from: str | None = None  # parent slot key if this is a fork
        self._fork_lock: asyncio.Lock = asyncio.Lock()  # serialises concurrent forks on this slot
        # Serialises explicit model-pick transactions (check → mutate → live
        # switch → rollback) on this slot: picks interleaving at the set_model
        # await could otherwise roll back each other's state. Deliberately NOT
        # slot._lock, which guards message-window edits and must not be held
        # across a multi-second network await.
        self._model_pick_lock: asyncio.Lock = asyncio.Lock()
        # Serialises one remote header pick's whole transaction (forward to the
        # peer → mirror locally → persist) on this slot. Concurrent picks each
        # suspend at the tunnel await, so without this their peer writes and
        # their metadata writes can complete in opposite orders and a restart
        # restores a value the crew does not hold.
        #
        # Deliberately NOT ``slot._lock``, for the reason its sibling above
        # gives: that lock guards message-window edits and must not be held
        # across a multi-second network await. A remote pick is exactly such an
        # await, so it gets its own lock rather than blocking every window edit
        # on the tunnel's round-trip.
        self._remote_pick_lock: asyncio.Lock = asyncio.Lock()
        self._tab_id: str = ""  # permanent tab identity for cross-restart session chaining
        # Transcript mtime the in-memory window was last brought up to date
        # against. Only meaningful for a slot bound to a channel session, whose
        # transcript the channel also writes to (see channel_slots).
        self._channel_window_mtime: float = 0.0
        self._disk_older_count: int = (
            0  # count of disk messages OLDER than in-memory window (stable, set at restore/resume)
        )
        # Durable-only frozen-prefix counter: how many durable rows (role not
        # in ``_TRANSIENT_ROLES``) have LEFT the in-memory window off the front.
        # Absolute message positions (``session_control.read_messages``) are
        # built over durable rows, so they base on THIS counter;
        # ``_disk_older_count`` keeps its save-model contract (the frozen
        # prefix saves must not rewrite) untouched. The two also draw the
        # unpersisted-overflow line differently — see the trim path below.
        # Maintained at every site that sets or advances ``_disk_older_count``,
        # always via :func:`durable_row_count`. Never persisted — every restore
        # path recomputes it from the messages on disk.
        self._disk_older_durable_count: int = 0
        # Count of in-memory window messages the LAST save persisted to disk
        # (the on-disk window region). Trimming may only fold a leading window
        # message into the frozen prefix once it is known to be on disk; this
        # watermark is what makes the #8 trim credit safe. It is NOT a fragile
        # "what to append" counter — saves always re-serialize the WHOLE window.
        self._disk_window_len: int = 0
        # The ``created_at`` of the on-disk metadata line this slot LAST
        # OBSERVED (at restore or at its own save). This is the session file's
        # identity: ``delete_session`` removes the file and a later writer
        # (e.g. a channel/cron ``append_off_loop``) creates a FRESH one with a
        # new ``created_at``, so a pending save comparing the current on-disk
        # value against this field can tell "the same file I knew" from "a
        # different file born after my session was permanently deleted" — the
        # delete-won guard in ``_save_slot_to_history`` refuses to merge the
        # deleted window into the latter. Empty means "never observed a disk
        # identity" (fresh slot), which the guard treats as no evidence.
        self._disk_meta_created_at: str = ""
        # Whether this slot has OBSERVED its transcript's metadata on disk at
        # all — set wherever ``_disk_meta_created_at`` is recorded (hydrate
        # sites and committed saves). Legacy metadata carries no
        # ``created_at``, which leaves the identity string above EMPTY even
        # though the file was genuinely observed; without this bit the
        # delete-won guard would read that empty identity as "fresh slot, no
        # evidence" and let a save racing a permanent delete recreate the
        # deleted transcript. The bit supplies the missing-file witness for
        # legacy sessions; the identity COMPARISON still requires a non-empty
        # ``created_at`` on both sides.
        self._disk_meta_observed: bool = False
        # The newest ``ts`` seen on disk at the last save, INCLUDING rows this
        # slot never observed. A subagent, cron, or CLI appending to a session a
        # live tab also has open writes rows that ``_save_slot_to_history``
        # preserves as "foreign" without ever folding them into ``messages`` --
        # so the window is not a superset of the file, and flooring the next
        # append on the window tail alone can tie a foreign row's timestamp.
        # Cached rather than read per append: consulting the file here would put
        # a stat plus a bounded read on the event loop, which AUTOSDE's
        # no-blocking-call-on-event-loop rule forbids. Refreshed at the save
        # boundary, where the lock is already held and the foreign lines are
        # already parsed.
        self._disk_tail_ts: str | None = (
            None  # Cached frozen-prefix bytes for the append-safe save model.
        )
        # The session file is FROZEN-PREFIX (the first _disk_older_count on-disk
        # message lines, OLDER than the in-memory window) + a fresh re-serialize
        # of the whole window. The prefix is never rewritten, so a restart that
        # loaded only a recent window can no longer destroy older history. This
        # caches the prefix bytes keyed by (path-mtime, path-size,
        # _disk_older_count) so a 5s flush is O(window), not O(file). The
        # (mtime, size) pair also doubles as the "did another process write this
        # file since we last saved?" signal that gates the cross-process
        # foreign-append merge. See chat_persistence._save_*.
        self._frozen_prefix_cache: tuple[float, int, int, str, list[str]] | None = None
        # Set by rewind/regenerate after they TRUNCATE the window. While set,
        # _save_slot_to_history takes the archive-safe rewrite path so the
        # dropped tail is archived — even if the inline rewrite save failed:
        # the next 5s flush then retries the rewrite instead of silently
        # overwriting (the default save skips archiving). Cleared on a
        # successful rewrite save.
        self._pending_rewrite: bool = False
        self._file_changes: list[dict[str, str]] = (
            []
        )  # [{path, content}] before-snapshots accumulated per turn for file-chip diffs
        self.linked_session_key: str = ""  # when set, _run_chat uses this as session key
        # Where the turn CURRENTLY in flight actually started, as opposed to
        # where the slot would route a new one. The two diverge whenever the
        # routing above is reassigned on a live slot — a cron injection binds an
        # existing slot to ``cron:<id>`` with no ``running`` gate — and a cancel
        # must address the turn, not the routing. Runtime-only: never persisted,
        # never serialized, empty after a restart, and ``_run_chat`` is its sole
        # lifecycle owner (installed once the turn is committed, cleared after
        # its session is released).
        self._active_turn_session_key: str = ""
        # True only when this slot was created to DISPLAY a conversation that
        # already lives in a channel transcript (the reconciler surfacing a
        # thread, a restore, a History resume). It is what separates such a tab
        # from a dashboard slot that merely happens to be NAMED like one --
        # a filename-shaped name is not provenance, and inferring it from the
        # name would let `POST /api/chat/slots` with a colliding `slack_<ts>`
        # name write a fresh conversation into an existing thread's transcript.
        self.channel_origin: bool = False
        self._side: SideState | None = None
        # Live inner AcpClient for the in-flight turn, published by _run_chat at
        # turn start and cleared in its finally. Lets a concurrent request (the
        # dashboard steer handler) reach the running session's client to inject
        # a mid-turn steer. None when idle.
        self._acp_client = None
        # Hang-attribution snapshot stashed by _run_chat's finally just before
        # _acp_client is dropped; read by finish_turn_task when the dashboard
        # ceiling cut the turn (kirocrew.turn.timeout.cause).
        self._last_turn_awaiting_permission = False
        self._last_turn_children_announced = False
        # Sync callable published by _run_chat alongside _acp_client (cleared in
        # the same finally): flushes the turn's accumulated text as a finalized
        # assistant segment NOW. The steer handler calls it right BEFORE
        # persisting the steer user message, so the transcript order is
        # [assistant(pre-steer), user(steer), assistant(post-steer)] — matching
        # what the client rendered live — instead of the whole segment landing
        # BELOW the steer bubble at end-of-turn (and stranding the pre-steer
        # chunk entries above it, which _flush_segment's trailing-run walk could
        # then never reclaim). None when idle.
        self._steer_segment_cut: Callable[[], None] | None = None
        # Native kiro-cli subagents run inside the parent ACP turn. Keep their
        # live and terminal state on the slot so reconnects can hydrate cards.
        self._native_subagent_tracker: dict[str, dict[str, Any]] = {}
        self._native_subagent_output: dict[str, list[str]] = {}
        # Delivery ids whose steer a NON-EMPTY echo actually accounted for. The
        # settle path can remove an entry from `_pending_steers` for two reasons
        # that look identical afterwards: a matched echo (evidence), or the
        # `settle_all_on_empty` sweep on an EMPTY echo (no evidence at all).
        # `chat_delivery` infers `consumed` from the entry being gone, so without
        # this it reads an empty frame as a confirmed injection and persists a
        # success badge nothing proved. Keyed on the delivery id, not the text, so
        # a later identical steer cannot inherit an earlier one's evidence.
        self._steer_confirmed: set[str] = set()
        # Mid-turn steers handed to the backend but not yet confirmed consumed
        # (no steering_consumed / EVENT_STEER_CONSUMED echo yet). Appended by
        # the dashboard steer handler BEFORE the steer RPC's await (so a turn
        # dying mid-write still sees it), settled by _run_chat when the
        # consumed echo arrives (matched against the echo's snapshot text),
        # and — the point of the mechanism — REQUEUED as ordinary queue cards
        # by _run_chat's finally when the turn dies first (stall-cancel, user
        # STOP, error). Without this, a steer swallowed by a dying turn
        # vanished with no trace (see the requeue site).
        self._pending_steers: list[str] = []
        # Opaque id per in-flight steer, keyed by its text (the one-per-text
        # rule in chat_delivery makes that key unique). The requeue moves the id
        # onto the queue entry and the drain unions entry meta onto the row it
        # writes, which is how a caller can tell a delivery the drain already
        # persisted from one the running turn consumed — a distinction the bare
        # text cannot make.
        self._steer_delivery_ids: dict[str, str] = {}
        # The client's `sendId` for an in-flight steer that supplied one, keyed by
        # the same message text as `_steer_delivery_ids`. Kept in LOCKSTEP with
        # that map -- every site that removes a delivery id removes this too -- so
        # "present here" always implies "present there" and no reader has to ask
        # which of the two a half-finished path left behind. Only the requeue reads
        # it: it moves the id onto the queue entry's meta so the drained row
        # carries `meta.sendId` like an accepted steer's row does (#6751). A steer
        # that persists its own row stamps the id directly and drops this entry.
        self._steer_send_ids: dict[str, str] = {}
        # In-flight `wait` tool sleep, as reported by the tool's own keepalive
        # ping: {"wait_id": str, "seconds": int, "deadline_ts": float}. The
        # deadline is on the dashboard's clock (see api_session_keepalive) so
        # the browser can count down against it directly. None whenever no wait
        # is sleeping.
        self._wait_state: dict | None = None
        # wait_id the user asked to end early, parked here until the sleeping
        # tool collects it on its next poll. Consumed exactly once.
        self._end_wait_request: str | None = None
        # Wall clock of the tracked wait's last keepalive ping. Server-side only
        # (deliberately NOT in to_dict): it is the heartbeat that distinguishes a
        # sleep that ended from one that is still running, which is how
        # _service_wait_ping tells a legitimate hand-over from two concurrent
        # waits colliding on one session key.
        self._wait_last_ping: float = 0.0
        # The session's steer stamp as it read when the tracked wait was minted.
        # Server-side only (deliberately NOT in to_dict). A steer newer than
        # this baseline is what ends the sleep early; re-reading it per sleep is
        # what stops ONE unconsumed steer from ending every subsequent sleep in
        # the turn and handing the model a `wait` that returns instantly.
        self._wait_steer_baseline: float = 0.0
        # True once two sleeps have been seen sharing this slot: neither may be
        # tracked or ended, because there is no way to know which one the user is
        # looking at. Latched for the rest of the turn and cleared by the same
        # turn-end block that clears the two fields above -- an earlier revision
        # expired it on a timer, which let whichever sleep pinged first after
        # expiry re-publish its deadline onto the other's pill.
        self._wait_contested: bool = False
        # Agent questions this slot has not answered yet, keyed by the ask's
        # identity: ``{card_id: {"ts": float, "blocking": bool}}``, empty when the
        # agent is not waiting on anything.
        #
        # A question card is a websocket broadcast with no transcript row, so
        # without this record the only surface that knows the agent is waiting is
        # the browser tab that happened to receive the card — a reload, a second
        # window, or the sessions board sees a quiet, finished-looking session.
        #
        # A MAP rather than one slot-wide record because asks overlap: a single
        # field let a second ask overwrite the first, and then whichever resolved
        # first cleared the only record while the other was still parked. Each ask
        # owns its own entry and retires exactly that one. ``blocking``
        # distinguishes an ask_question HTTP round-trip (the turn is parked on the
        # answer) from a stateless card (the turn has ended and the answer arrives
        # as the next message) — the difference between "the agent is stuck" and
        # "the agent is done and asked you something", and which entries a user
        # message may retire.
        self._question_pending: dict[str, dict] = {}

    @property
    def _dirty(self) -> bool:
        """True while this slot holds state not yet confirmed on disk.

        Deliberately a property so that ``_dirty_gen`` is bumped centrally by the
        ~20 existing ``slot._dirty = True`` sites without editing any of them.

        Two independent readers depend on this staying True for the WHOLE
        duration of a save, not just until the save starts:

        * ``chat_fork`` treats it as "unpersisted in-memory state exists". A False
          read makes it skip both the in-memory tail append and the durable
          pre-fork save, so it forks from stale disk and the new session silently
          omits the newest messages.
        * ``_save_slot_to_history``'s resumed-slot no-op guard skips when
          ``_resumed_count > 0 and len(window) <= _resumed_count and not _dirty``;
          its comment states the assumption directly — "a dirty slot whose length
          merely equals the resumed count still falls through ... otherwise an
          in-place edit after resume would never reach disk."

        So the periodic flush must NOT clear this early to protect itself against
        clobbering a concurrent mark. It compares ``_dirty_gen`` instead.
        """
        return self._dirty_flag

    @_dirty.setter
    def _dirty(self, value: bool) -> None:
        self._dirty_flag = value
        if value:
            # Monotonic: only ever advances, so a wrapped-around compare is
            # impossible and a missed bump can only cause an extra (harmless)
            # flush, never a skipped one.
            self._dirty_gen += 1

    @property
    def _plan_stage_count(self) -> int:
        return len(self._stage_titles)

    @property
    def _stop_state(self) -> str:
        return self._stop_state_raw

    @_stop_state.setter
    def _stop_state(self, value: str) -> None:
        # Count stop INITIATIONS (idle → active edge) in a monotonic
        # generation that teardown never rewinds — see __init__ comment.
        # Escalations (soft_pending → killing) and resets (→ idle) are not
        # new initiations and do not bump it.
        if value != "idle" and self._stop_state_raw == "idle":
            self._stop_generation += 1
        self._stop_state_raw = value

    @property
    def _stopping(self) -> bool:
        return self._stop_state != "idle"

    @_stopping.setter
    def _stopping(self, value: bool) -> None:
        self._stop_state = "soft_pending" if value else "idle"

    def set_todo(self, todo: dict[str, Any] | None) -> bool:
        """Replace the slot's TODO snapshot. Returns True when it changed.

        The return value gates the live websocket push so an unchanged list —
        common, because a single turn can echo the same snapshot on several
        tool results — does not fan a redundant broadcast out to every socket.
        """
        normalised: dict[str, Any] | None = None
        if isinstance(todo, dict):
            tasks = todo.get("tasks")
            normalised = {
                "description": str(todo.get("description") or ""),
                "tasks": list(tasks) if isinstance(tasks, list) else [],
            }
        if normalised == self._todo:
            return False
        self._todo = normalised
        return True

    def todo_payload(self) -> dict[str, Any] | None:
        """The serialized TODO snapshot with server-derived progress counts.

        ``completed``/``total`` are computed here rather than in the browser so
        the pill's "N of M" cannot drift from the list it labels. ``current`` is
        the first not-completed task's text — kiro-cli's todo model is a plain
        ``completed`` boolean with NO in-progress state, so "current task" is
        this derivation, not something the agent reports.
        """
        if self._todo is None:
            return None
        tasks = [t for t in self._todo.get("tasks", []) if isinstance(t, dict)]
        completed = sum(1 for t in tasks if t.get("completed"))
        current = next((str(t.get("text") or "") for t in tasks if not t.get("completed")), "")
        return {
            "description": self._todo.get("description", ""),
            "tasks": tasks,
            "completed": completed,
            "total": len(tasks),
            "current": current,
        }

    def set_mcp_report(self, report: dict[str, Any] | None, session_id: str = "") -> bool:
        """Replace this slot's MCP session report. True when it changed.

        The payload is built and sanitized by
        :class:`kiro_crew.acp.mcp_session_report.McpSessionReport`, which owns
        redaction and the per-bucket caps; this only stores what it produced, so
        a caller cannot widen those bounds by writing here.

        ``session_id`` is the session the report DESCRIBES. It is stored beside
        the payload because this copy outlives its owner: the report itself lives
        on the transport and is inherently the session's, but a cached
        projection is only as valid as the identity it was taken under.
        """
        normalised = report if isinstance(report, dict) else None
        if normalised == self._mcp_report and session_id == self._mcp_report_session_id:
            return False
        self._mcp_report = normalised
        self._mcp_report_session_id = session_id if normalised is not None else ""
        return True

    def clear_mcp_report(self) -> bool:
        """Drop the report because this slot's session is gone. True if it had one.

        Distinct from ``set_mcp_report(None)`` only in intent: it is called from
        the session-reset funnel so a report can never outlive the session it
        describes and be read as the next one's.
        """
        return self.set_mcp_report(None)

    def mcp_report_payload(self) -> dict[str, Any] | None:
        """The stored MCP session report, or None when this slot has none."""
        return self._mcp_report

    def note_disk_tail(self, *candidates: str | None) -> None:
        """Record the newest ``ts`` known to be ON DISK for this session.

        The save boundary is the only place a slot can learn about a row it never
        observed (see ``_disk_tail_ts``), so it calls this with whatever it just
        wrote -- foreign rows included. Keeping the update here rather than
        assigning the attribute from the persistence module means the **monotone**
        rule lives with the field it guards: the floor may only ever move FORWARD.
        A save that moved it backwards would re-open the same-``ts`` tie the floor
        exists to prevent, and unparseable candidates are skipped rather than
        ranked (``latest_transcript_ts``), so one corrupt row cannot capture it.
        """
        self._disk_tail_ts = latest_transcript_ts(self._disk_tail_ts, *candidates)

    def append(
        self,
        role: str,
        content: str,
        cls: str = "",
        ts: str = "",
        *,
        broadcast: bool = True,
        broadcast_user: bool = False,
        meta: dict | None = None,
        mint_mid: bool = True,
    ) -> dict[str, Any]:
        # A LIVE turn-consuming row retires every unanswered STATELESS question:
        # the card's own submit path sends one, and anything else that starts the
        # slot's next turn consumes the answer channel the card was waiting on.
        # Retiring here rather than at the composer covers every entrance —
        # queued dispatch, an auto-nudge cycle, a channel row relayed from Slack —
        # instead of the one send site that happens to be in front of the user.
        #
        # The role set mirrors the frontend's `QUESTION_RETIRING_ROLES`, which
        # drops the card on the same frames. They must agree: a role the client
        # retires but the server keeps leaves a session reporting needs_input with
        # no card on screen, and a later rehydration re-renders a card whose
        # answer channel is gone.
        #
        # Gated on *broadcast*, which is what separates a live append from a
        # REPLAY: `channel_slots._rebuild_window` (transcript rotation recovery),
        # `chat_fork` and `session_transfer` all re-append historical rows with
        # `broadcast=False`. Replaying an old row says nothing about the question
        # asked a moment ago, and clearing on it would retire a live card's status
        # — and broadcast the retirement — for a message sent hours earlier.
        #
        # BLOCKING records are left alone either way: nothing a turn-consuming row
        # can do resolves a parked wait, so clearing one would report the agent as
        # working while its tool call is still stuck on the answer. Those are
        # owned by the round-trip in request_question, which retires its own entry
        # on every exit.
        if role in _QUESTION_RETIRING_ROLES and broadcast and self._question_pending:
            retired = [
                cid for cid, rec in self._question_pending.items() if not rec.get("blocking")
            ]
            self._question_pending = {
                cid: rec for cid, rec in self._question_pending.items() if rec.get("blocking")
            }
            # Announce it: a retirement that only mutates state is invisible to a
            # second window and to a /pending response already in flight, either
            # of which would then re-render a card whose answer was just sent —
            # and submitting that card appends a duplicate turn.
            if retired and self._on_question_retired:
                try:
                    self._on_question_retired(self.key, retired)  # type: ignore[operator]
                except Exception:
                    pass
        msg: dict[str, Any] = {
            "role": role,
            "content": content,
            "cls": cls,
            # This window is re-serialized into the SAME transcript file that
            # ConversationLog.append writes, so it owes the reader the same
            # ordering guarantee: strictly after the row before it, even when
            # the clock does not tick between two appends. An explicit *ts*
            # (a row replayed from a channel transcript) is preserved verbatim
            # -- rewriting it would reorder the replay it came from.
            #
            # The floor is the later of the window tail and the last on-disk tail
            # this slot was told about, because the window is NOT a superset of
            # the file: a row written by another process is preserved as a
            # foreign line without entering ``messages``, so flooring on the
            # window alone leaves it un-ordered-against. Both candidates are
            # in-process reads -- no file I/O on the event loop.
            "ts": ts
            or monotonic_transcript_ts(
                latest_transcript_ts(
                    self.messages[-1].get("ts") if self.messages else None,
                    self._disk_tail_ts,
                ),
                datetime.now(timezone.utc),
            ),
        }
        if meta:
            msg["meta"] = meta
        # Stamp a per-row delivery identity. A client sees the SAME row through
        # two doors — the slot-detail HTTP rebuild and the live `chat_message`
        # broadcast — and must be able to tell "this row again" from "another row
        # that happens to look identical". `ts` cannot answer that: a coarse OS
        # clock stamps two rows appended in the same tick identically (the same
        # collision mergePreservedClientTs already guards), and content cannot
        # either, since two identical messages are legitimate. So identity is an
        # explicit id, minted once here, carried on the message dict, and thus
        # present on every path that ships it: persisted by _build_message_entry,
        # restored with the rest of `meta`, broadcast as `payload["meta"]`, and
        # returned by _prepare_messages.
        #
        # Random rather than a per-slot counter deliberately: a counter rebased
        # after a restore could reissue an id a restored row already holds, and a
        # colliding id makes a client DROP a real message. There is no such
        # failure mode for a random id.
        #
        # A caller-supplied `mid` (a row replayed from disk) is preserved — the
        # id must survive the round trip or a post-restart redelivery of that row
        # would not be recognisable.
        #
        # A restore caller passes ``mint_mid=False`` for a durable row whose disk
        # representation has no id. Minting one only in the in-memory window would
        # advertise an identity the full-history readers cannot resolve; features
        # such as response-level Fork would then bypass their legacy pagination
        # guard and fail against the still-id-less transcript. A supplied disk id
        # remains in ``meta`` regardless of this flag.
        #
        # Skipped for the wire-only roles: `chunk` is appended once per streamed
        # token and `done`/`streaming` are internal markers. None of them is ever
        # broadcast as a `chat_message` (the broadcast below excludes them) or
        # persisted (`_TRANSIENT_ROLES`), so an id would buy nothing and cost a
        # uuid4 plus a dict on the hottest path in the runner.
        if (
            mint_mid
            and role not in _WIRE_ONLY_ROLES
            and not (isinstance(msg.get("meta"), dict) and msg["meta"].get("mid"))
        ):
            existing = msg.get("meta")
            msg["meta"] = {
                **(existing if isinstance(existing, dict) else {}),
                "mid": mint_row_mid(),
            }
        self.messages.append(msg)
        self.invalidate_source_links()
        self.total_messages += 1
        self._dirty = True
        self._pending.append(msg)
        self.event.set()
        # Broadcast via global SSE when no HTTP stream reader is active
        # Skip: chunk (too noisy), done (internal). A "user" row is skipped by
        # DEFAULT because the composer that submitted it already rendered it
        # optimistically -- but that is only true of a message typed in this
        # dashboard. A row replayed from a CHANNEL transcript was typed in
        # Slack, so nothing rendered it here; those callers pass
        # ``broadcast_user=True`` or the message stays invisible until a full
        # transcript reload, arriving AFTER the reply it came before.
        if (
            broadcast
            and self._on_message
            and role not in ("chunk", "done")
            and (role != "user" or broadcast_user)
            and not self._has_reader
        ):
            self._on_message(self.key, msg)  # type: ignore[operator]
        # Trim old messages to bound memory usage
        if len(self.messages) > _MAX_SLOT_MESSAGES:
            excess = len(self.messages) - _MAX_SLOT_MESSAGES
            # A trimmed leading window message may only join the frozen prefix
            # once it is actually on disk. Credit _disk_older_count only
            # for the persisted portion; the unpersisted overflow (should not
            # happen between 5s flushes) is logged rather than silently counted
            # as on-disk, which would have stranded those turns.
            persisted_trim = min(excess, self._disk_window_len)
            # The durable counter counts the WHOLE evicted slice, including the
            # unpersisted overflow the disk counter excludes. The two draw
            # different lines because they answer different questions:
            # ``_disk_older_count`` claims on-disk lines (its save contract), so
            # counting a row that never reached disk would corrupt the frozen
            # prefix. ``_disk_older_durable_count`` is a POSITION base with no
            # disk contract — if a durable row leaves the window uncounted,
            # every later absolute position shifts down and a poller's cursor
            # silently skips rows. Counting the lost rows instead makes a cursor
            # that pointed at them refuse loudly (``since < base``), which is
            # the recoverable outcome. Counted BEFORE the ``del`` below —
            # afterwards the slice is gone.
            durable_trim = durable_row_count(self.messages[:excess])
            del self.messages[:excess]
            self._resumed_count = max(0, self._resumed_count - excess)
            self._disk_older_count += persisted_trim
            self._disk_older_durable_count += durable_trim
            self._disk_window_len = max(0, self._disk_window_len - excess)
            if persisted_trim < excess:
                logger.warning(
                    "Slot %s trimmed %d messages not yet flushed to disk; "
                    "they will not be recoverable from history",
                    self.key,
                    excess - persisted_trim,
                )
            # The frozen prefix grew → its cached bytes are stale.
            self._frozen_prefix_cache = None
        # Hand back the row as appended (id included): a dual-writer that also
        # persists this message through ``ConversationLog.append`` needs the
        # ``meta.mid`` minted above so BOTH copies carry the same identity —
        # re-minting at the durable copy would give the reconciliation walk two
        # ids for one logical message. Read the id off the return with
        # :func:`row_mid`, never an inline ``meta`` poke.
        return msg

    def push_wire_frame(self, cls: str, content: str) -> None:
        """Queue an ephemeral frame for live SSE readers only."""
        self._buffers.push_wire_frame(self, cls, content)

    @property
    def is_remote(self) -> bool:
        """True when this slot's turns must be dispatched to a peer crew.

        Requires the whole binding, not just the ``executor`` marker: a slot
        carrying ``executor == "remote"`` with no instance or no peer slot is
        broken, and treating it as local would run the turn on this machine —
        the one outcome the binding exists to prevent. Callers therefore get
        False here and a refusal at the dispatch site, not a silent local run.
        """
        return bool(self.executor == "remote" and self.instance_id and self.remote_slot)

    def drain(self) -> list[dict[str, str]]:
        """Return and clear pending messages."""
        return self._buffers.drain(self)

    @property
    def pending_has_consumer(self) -> bool:
        """True while something can still deliver rows out of the pending queue."""
        return self._buffers.pending_has_consumer(self)

    @property
    def _has_reader(self) -> bool:
        """True while an HTTP SSE stream is draining this slot."""
        return self._has_reader_flag

    @_has_reader.setter
    def _has_reader(self, value: bool) -> None:
        was = self._has_reader_flag
        self._has_reader_flag = bool(value)
        if was and not self._has_reader_flag:
            self._retry_deferred_release()

    def _retry_deferred_release(self) -> int:
        """Retry a release refused while a delivery consumer was attached."""
        return self._buffers.retry_deferred_release(self)

    @contextlib.contextmanager
    def pending_consumer(self) -> Iterator[None]:
        """Hold the pending list as an attached delivery queue for the block."""
        with self._buffers.pending_consumer(self):
            yield

    def release_pending_chunks(self) -> int:
        """Release chunk rows unless a live consumer still owns the queue."""
        return self._buffers.release_pending_chunks(self)

    def purge_chunks(self) -> int:
        """Drop finalized stream chunks from the transcript and live queue."""
        return self._buffers.purge_chunks(self)

    def append_pending_context(self, entry: dict[str, Any]) -> None:
        """Append one live context entry after expiry pruning and FIFO eviction."""
        self._buffers.append_pending_context(
            self,
            entry,
            max_pending_context=_MAX_PENDING_CONTEXT,
            entry_expired=context_entry_expired,
        )

    def drop_foreign_authorized_notes(self) -> int:
        """Drop note content whose authorization belongs to another session."""
        return self._buffers.drop_foreign_authorized_notes(
            self,
            authorized_elsewhere=_note_authorized_elsewhere,
            logger=logger,
        )

    def deferred_context_count(self) -> int:
        """Held notes whose context half has not reached the queue yet."""
        return self._buffers.deferred_context_count(self)

    def flush_deferred_notes(self) -> int:
        """Flush held notes in order, restoring the unwritten suffix on failure."""
        return self._buffers.flush_deferred_notes(self, logger=logger)

    def mark_permission_resolved(self, approval_id: str, decision: str = "approved") -> None:
        """Update the matching stored permission row without marking it dirty."""
        self._buffers.mark_permission_resolved(self, approval_id, decision)

    def update_message(
        self,
        ts: str,
        *,
        content: str | None = None,
        meta: dict | None = None,
        mid: str | None = None,
    ) -> dict | None:
        """Replace fields on a previously appended message.

        Identified by ``mid`` (this row's server-minted identity) when one is
        given, falling back to ``ts``. Prefer ``mid``: two rows can carry the same
        ``ts``, so a ts lookup resolves the first match and can patch the wrong
        row.
        """
        return self._buffers.update_message(
            self,
            ts,
            content=content,
            meta=meta,
            mid=mid,
        )

    # ── Queue helpers (dict-based queue items) ──

    def queue_append(
        self,
        content: str,
        kind: str = "",
        meta: dict | None = None,
        *,
        directive_user_origin: bool = False,
    ) -> str:
        return self._queue_repository.queue_append(
            self,
            content,
            kind,
            meta,
            directive_user_origin=directive_user_origin,
        )

    def _note_enqueue(self) -> None:
        self._queue_repository.note_enqueue(self)

    def queue_insert(
        self,
        index: int,
        content: str,
        kind: str = "",
        payload: str = "",
        meta: dict | None = None,
        on_consumed: Callable[[bool], None] | None = None,
        on_irreversibly_consumed: Callable[[], Awaitable[None] | None] | None = None,
        directive_user_origin: bool = False,
    ) -> str:
        return self._queue_repository.queue_insert(
            self,
            index,
            content,
            kind,
            payload,
            meta,
            on_consumed,
            on_irreversibly_consumed,
            directive_user_origin,
        )

    def queue_pop(self, index: int = 0) -> dict[str, Any]:
        return self._queue_repository.queue_pop(self, index)

    def note_pending_subagent_delivery(self, content: str, agent_ids: list[str]) -> None:
        self._queue_repository.note_pending_subagent_delivery(self, content, agent_ids)

    def owes_subagent_delivery(self, contents: list[str]) -> bool:
        return self._queue_repository.owes_subagent_delivery(self, contents)

    def take_pending_subagent_deliveries(self, contents: list[str]) -> list[str]:
        return self._queue_repository.take_pending_subagent_deliveries(self, contents)

    def queue_remove_by_id(self, queue_id: str) -> str | None:
        return self._queue_repository.queue_remove_by_id(self, queue_id)

    def queue_edit_by_id(
        self,
        queue_id: str,
        content: str,
        *,
        directive_user_origin: bool = False,
    ) -> bool:
        return self._queue_repository.queue_edit_by_id(
            self,
            queue_id,
            content,
            directive_user_origin=directive_user_origin,
        )

    def queue_promote_by_id(self, queue_id: str) -> bool:
        return self._queue_repository.queue_promote_by_id(self, queue_id)

    @property
    def task(self) -> asyncio.Task[Any] | None:
        return self._task

    @task.setter
    def task(self, value: asyncio.Task[Any] | None) -> None:
        if value is not None and value is not self._task:
            self._turn_generation += 1
        self._task = value

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def queue_depth(self) -> int:
        """Number of prompts currently queued behind the active turn."""
        return len(self._queue)

    @property
    def model_withheld(self) -> bool | None:
        """Whether the live session withholds this slot's pinned model.

        Tri-state, and the third state carries as much information as the other
        two: ``True`` the account cannot run the pin (the spawn withheld it and
        the session is on the backend default), ``False`` it can, ``None``
        **unknown** -- no session has advertised a comparable list for this pin
        yet. The frontend must fail open on ``None`` (see ``displayModel``);
        unknown is not denied.

        Recorded per model id, not per slot: the verdict answers a question
        about ONE pin, so it is reported only while the slot still carries the
        model it was computed for. That makes every writer of ``slot.model``
        (the picker, the bulk pick, the rollback, the restore paths, the
        canonical backfill) invalidate it for free -- a stale verdict outliving
        a re-pin would be a display that names the wrong model, and there are
        too many writers to keep an explicit reset at each one.

        DISPLAY only. This is never a write source: the pin is deliberately
        KEPT when withheld (it is inert and self-heals on re-upgrade), and the
        pin-to-agent row writes ``slot.model`` itself.
        """
        if not self.model or self._model_withheld_for != self.model:
            return None
        return self._model_withheld

    def record_model_withheld(self, withheld: bool | None) -> None:
        """Pin a spawn-time withhold verdict to the model it was computed for.

        ``None`` forgets the verdict (back to unknown) -- what a session
        teardown does, since the verdict describes the session that advertised
        the list, not the slot.
        """
        if withheld is None:
            self._model_withheld = False
            self._model_withheld_for = ""
            return
        self._model_withheld = withheld
        self._model_withheld_for = self.model or ""

    @property
    def is_restricted(self) -> bool:
        """True when memory writes (consolidation, lessons) are blocked."""
        return self.memory_mode != "persistent"

    @property
    def blocks_reads(self) -> bool:
        """True when memory-context injection into this session is blocked."""
        return self.memory_mode == "temporary"

    @property
    def unattended(self) -> bool:
        """True when no human is driving this session's turns.

        FIX 1 + FIX 2 share this predicate: it decides which slots get the
        deny-fast approval window (:meth:`DashboardState.approval_timeout_for`)
        and which turns are charged against the background concurrency cap
        (:meth:`DashboardState.run_background_turn`).

        ``_app`` is the whole test, plus the ``_human_seen`` escape hatch. Why
        app-ownership and not ``_trust``:

        * ``_trust`` is False *by construction* wherever this predicate is
          consulted. The runner auto-approves and ``continue``s while trust
          holds, so a tool only reaches the interactive wait once trust is
          absent — and trust is in-memory, so a gateway restart clears it on
          every app worker. A ``_trust``-based detector reads False in exactly
          the situation it exists to detect.
        * ``_app`` is set only by an app creating the slot (App Kit §5.2), is
          persisted in the session metadata, and is already the ownership axis
          every other isolation decision in these files keys on. A session a
          person created has ``_app == ""`` and is therefore never affected —
          which is what keeps interactive behaviour byte-identical.

        Both halves are persisted, and they have to be: ``_app`` surviving a
        restart while ``_human_seen`` did not is what made an attended app tab
        silently revert to the deny-fast window after every upgrade.
        """
        return bool(self._app) and not self._human_seen

    def enqueue_or_run_prompt(
        self,
        prompt: str,
        run_chat_coro: Callable[[DashboardState, _ChatSlot, str], Coroutine[Any, Any, None]],
        state: DashboardState,
    ) -> bool:
        """Queue *prompt* if busy, otherwise start an agent turn.

        Encapsulates the queue-vs-run decision so callers don't need to
        touch ``_queue``, ``task``, or ``_background_tasks`` directly.
        Always registers :func:`_log_task_exception` to prevent silent failures.

        Returns ``True`` if the prompt started an agent turn, ``False`` if
        it was queued. Lets callers gate UI-visible side-effects (notifications,
        SSE pushes) on whether the prompt actually ran.

        Concurrency: the check (``self.running``) and mutation (``self.task = ...``)
        run synchronously on the asyncio event loop with no ``await`` between them,
        so two concurrent callers targeting the same slot cannot both observe
        ``running == False`` within a single loop iteration.
        """
        if self.running:
            # circular import: session_control imports this module at module level.
            from kiro_crew.dashboard.session_control import containment_meta

            # Stamp the containment constraints holding at ADMISSION, so the
            # queue drain can re-assert them at delivery (issue #5911): a target
            # that gains a channel/mirror link while this prompt waits must not
            # execute it under the weaker constraints that admitted it.
            self.queue_append(prompt, meta=containment_meta(state, self))
            return False
        self.append("user", prompt, "msg msg-u")
        task = asyncio.create_task(run_chat_coro(state, self, prompt))
        self.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
        task.add_done_callback(_log_task_exception)
        return True

    @property
    def display_title(self) -> str:
        """Title for UI display. Shows ``NEW_SESSION_TITLE`` while the slot is
        still on its untouched default key (untitled) — covering brand-new
        empty sessions and the window before the LLM title lands — otherwise
        the real title. Slots with a meaningful non-key title (plan, cron,
        fork, slack) are unaffected since their title != key.
        """
        if not self._titled and (
            not self.title or self.title == self.key or _SLOT_KEY_TITLE_RE.match(self.title)
        ):
            return NEW_SESSION_TITLE
        return self.title

    def invalidate_source_links(self) -> None:
        """Mark cached sidebar PR/MR/issue links stale after message-content mutation."""
        self._source_links_revision += 1

    def _pr_source_links(self) -> list[dict]:
        """Return cached source links ordered by their most recent mention."""
        return self._projection.source_links(
            self,
            max_links=_MAX_SOURCE_LINKS_PER_SLOT,
            non_durable_roles=_NON_DURABLE_SOURCE_LINK_ROLES,
        )

    def source_links_payload(
        self, *, include_check_status: bool = False, dashboard_user: bool = False
    ) -> dict:
        """Every source link this slot carries — the unbudgeted read.

        ``to_dict`` serializes at most ``_SERIALIZED_SOURCE_LINKS_PER_SLOT`` per
        kind, so the sidebar's "+N" overflow chip has nothing on the client to
        expand into. This is what that expand fetches.

        Ordering repeats the budgeted slice's grouping (changes, then issues) so
        the chips already on screen keep their positions and the revealed ones
        append inside their own group instead of shuffling the row.
        """
        links = self._pr_source_links()
        changes, issues = _source_links_by_kind(links)
        return {
            "links": _project_source_links(
                changes + issues, include_check_status, dashboard_user=dashboard_user
            ),
            "total": len(links),
        }

    def to_dict(self, *, include_check_status: bool = False, dashboard_user: bool = False) -> dict:
        # Skip extraction itself when the chips are off, not just the two fields
        # the projection derives from it: `_pr_source_links()` scans the transcript
        # under a per-call parse budget, and paying for a payload nothing renders
        # is the cost this switch exists to remove. The getter is a cache-only
        # snapshot lookup -- it never touches config, which is what makes it safe
        # to call from this synchronous per-slot path on the event loop.
        from kiro_crew.dashboard.handlers.source_providers import (
            session_card_source_links_enabled,
        )

        source_links = self._pr_source_links() if session_card_source_links_enabled() else []
        return self._projection.to_dict(
            self,
            include_check_status=include_check_status,
            source_links=source_links,
            prompt_roles=_PROMPT_ROLES,
            redact=_redact,
            parse_options=_parse_options,
            strip_options=lambda text: _OPTIONS_RE.sub("", text).strip(),
            parse_cls_meta=parse_cls_meta,
            is_turn_interrupted=is_turn_interrupted,
            is_system_notice=is_system_notice,
            latest_transcript_ts=latest_transcript_ts,
            strip_markdown_preview=strip_markdown_preview,
            resolve_effective_agent=resolve_effective_agent,
            budget_source_links=_budgeted_source_links,
            # Bind the PUBLIC-only gate through the projection's positional
            # (links, include_check_status) callable so slot_projection.py needs
            # no change: the owner gate rides include_check_status as before, and
            # dashboard_user (a non-owner authenticated dashboard user) unlocks
            # status ONLY for links whose repo is known public.
            project_source_links=(
                lambda links, incl: _project_source_links(
                    links, incl, dashboard_user=dashboard_user
                )
            ),
        )


class DashboardState:
    """Shared state injected into all handlers via ``app["state"]``."""

    # Class-level defaults, NOT just __init__ assignments. push_slots_update and
    # _persist_open_slots read these on every call, and a partially-constructed
    # state built with DashboardState.__new__(DashboardState) — the pattern used
    # by several endpoint test suites, which set only the attributes the handler
    # under test touches — never runs __init__. Without a class default those
    # reads raise AttributeError. __init__ still assigns per-instance values
    # below; these only supply the "nothing suspended, not restoring" baseline.
    _slots_push_suspend: int = 0
    _slots_push_pending: bool = False
    restoring_open_slots: bool = False
    # push_slots_update() coalescing state, on that same read path. The lock
    # defaults to None rather than to a shared Lock(): a None lock means "no
    # coalescing", so a __new__-built state broadcasts straight through instead
    # of every instance in the process contending on one class-level mutex.
    # __init__ installs the real per-instance lock.
    _slots_broadcast_lock: "threading.Lock | None" = None
    _slots_broadcast_timer: "asyncio.TimerHandle | None" = None
    _slots_broadcast_last: float = 0.0
    # The one loop this dashboard is served on. Every surface that hands work in
    # from a foreign thread -- the coalesced slots broadcast, an off-loop
    # websocket send, the log handler's fan-out -- resolves it through
    # :attr:`serving_loop` rather than keeping a copy of its own: two copies are
    # two answers to one question and can disagree, and a caller that finds its
    # own copy unset drops the work silently. Bound at app startup; the property
    # latches lazily so a ``__new__``-built state still resolves one.
    _serving_loop: "asyncio.AbstractEventLoop | None" = None
    # Keys the last open-tab restore could not read (not keys it proved absent).
    # _persist_open_slots folds these back into the snapshot so a transient read
    # failure cannot erase the reopen seed. The class-level baseline is an
    # IMMUTABLE frozenset on purpose: a bare set() here would be one object
    # shared by every __new__-built instance. __init__ and the restore each
    # assign a fresh set(), so mutation only ever touches an instance attribute.
    unrestored_slot_keys: "frozenset[str] | set[str]" = frozenset()
    crew: Any = None  # Crew Mode control plane (set by gateway; None = unavailable)

    def __init__(
        self,
        sessions: SessionManager,
        crons: CronService,
        lessons: LessonStore,
        start_time: float,
        subagents: SubagentManager | None = None,
        context_builder: ContextBuilder | None = None,
        conversation_log: ConversationLog | None = None,
        consolidator: HistoryConsolidator | None = None,
        task_runner: TaskRunner | None = None,
        slack_client: Any = None,
        owner_id: str = "",
    ):
        self.sessions = sessions
        self.crons = crons
        self.lessons = lessons
        self.start_time = start_time
        # Published only at the final boot-to-ready boundary in server.py.
        # The socket binds earlier, so /api/ready can truthfully return 503
        # while session restoration, channel relaunch, and tunnel setup finish.
        self.ready: bool = False
        # Wired by server.py after the gateway-owned prerequisite service is
        # constructed. The central chat runner reads this latch so every turn
        # entry path is protected, including task/workflow continuations.
        self.kiro_prerequisite_service: Any = None
        self.subagents = subagents
        # Crew Mode control plane; attached by the gateway after
        # SubagentManager construction (None = crew mode unavailable).
        self.crew: Any = None
        self.channel_manager: Any = None  # lazy-init in server.py
        self.tunnel_manager: Any = None  # lazy-init in server.py (TunnelManager)
        self.instances_manager: Any = None  # lazy-init in server.py (SshTunnelManager)
        self.instances_registry: Any = None  # lazy-init in server.py (InstancesRegistry)
        # Cloud provisioning launch jobs (lazy-init in handlers_cloud).
        self.cloud_launch_store: Any = None  # LaunchJobStore
        self.cloud_launch_cancels: Any = None  # dict[str, threading.Event]
        self.cloud_launch_engine: Any = (
            None  # test-injected LaunchEngine (None -> RealLaunchEngine)
        )
        self.cloud_launch_sync: bool = False  # tests set True to run launches inline
        self.cloud_launch_reaped: bool = False  # orphan reap is once per process
        self.cloud_launch_lock: Any = None  # asyncio.Lock serializing launch creation
        # MCP gateway control plane — wired by GatewayOrchestrator AFTER
        # dashboard init (the broker starts before dashboard_state exists).
        # Read by the /api/mcp-gateway/* handlers off request.app['state'].
        self._mcp_gateway_manager: Any = None  # GatewayManager | None
        self._mcp_gateway_apply: Any = None  # async (enabled: bool) -> dict
        self._mcp_gateway_apply_stub: Any = None  # async () -> dict
        self._mcp_resolve_refresh: Any = None  # async () -> dict
        # Secretary subsystem removed; kept as permanent None for apps/routes.py
        # builtin-service restart lookup (getattr-based, no-op when None).
        self._secretary_restart: Any = None  # restart callback (always None — service removed)
        self.workflow_service: Any = None  # lazy-init in server.py (WorkflowService, M6)
        self.context_builder = context_builder
        self.conversation_log = conversation_log
        self.consolidator = consolidator
        self.task_runner = task_runner
        self.slack_client = slack_client
        # True only when the Slack socket-mode connect actually succeeded this
        # session. slack_client being set proves tokens existed at boot, not
        # that they are valid — the gateway records the real outcome after
        # _connect_slack(). Read by the Slack settings status badge.
        self.slack_socket_connected: bool = False
        # Short reason from the failed connect attempt (e.g. "invalid_auth"),
        # empty when connected or never attempted. Read by the settings badge.
        self.slack_connect_error: str = ""
        # True once the Discord channel's Gateway WebSocket transport started
        # this session (set by maybe_start_discord). Read by the Discord
        # settings status badge.
        self.discord_connected: bool = False
        # Short reason when the Discord channel failed to start, empty when
        # running or never attempted. Read by the settings badge.
        self.discord_connect_error: str = ""
        # True once the Telegram channel's long-polling transport started this
        # session (set by maybe_start_telegram). Read by the Telegram settings
        # status badge.
        self.telegram_connected: bool = False
        # Short reason when the Telegram channel failed to start, empty when
        # running or never attempted. Read by the settings badge.
        self.telegram_connect_error: str = ""
        # True only while the Webex device WebSocket is connected + authorized
        # this session (kept truthful by WebexClient.on_state_change). Read by
        # the Webex settings status badge.
        self.webex_connected: bool = False
        # Short reason from the most recent Webex connection failure, empty
        # when connected or never attempted. Read by the settings badge.
        self.webex_connect_error: str = ""
        # True only while the iMessage watch is live on the local bridge (kept
        # truthful by IMessageClient.on_state_change). Read by the iMessage
        # settings status badge.
        self.imessage_connected: bool = False
        # Short reason the iMessage channel is not running — a missing imsg
        # binary, a Messages database the process cannot read (Full Disk
        # Access), or a non-macOS host. Empty when connected or never attempted.
        self.imessage_connect_error: str = ""
        # True only while the WeCom (企业微信) channel's WebSocket is connected
        # + subscribed (kept live by WeComClient.on_status, wired in
        # maybe_start_wecom). Read by the WeCom settings status badge.
        self.wecom_connected: bool = False
        # Short reason from the most recent WeCom connection failure (connect
        # error, immediate close on bad credentials, or server kick), empty
        # when connected or never attempted. Read by the settings badge.
        self.wecom_connect_error: str = ""
        # True only while the Feishu (飞书/Lark) channel's WebSocket receiver
        # thread is alive (kept truthful by LarkClient.on_state_change, wired in
        # maybe_start_feishu). Read by the Feishu settings status badge.
        #
        # Receiver-liveness, NOT a credential probe: lark-oapi owns the socket
        # and exposes no connect/subscribe transition to hook, and a REST
        # tenant-token probe would have to pick a domain (open.feishu.cn vs
        # open.larksuite.com), reporting a false failure for whichever tenant it
        # guessed wrong. Liveness still catches rejected credentials, because
        # lark's ws.start() RETURNS within seconds when the app is refused —
        # which flips this to False with the reason attached.
        self.feishu_connected: bool = False
        # Short reason the Feishu channel is not running — a missing lark-oapi
        # extra, rejected app credentials, or a receiver thread that died. Empty
        # when connected or never attempted. Read by the settings badge.
        self.feishu_connect_error: str = ""
        # True only while the Teams channel's credentials validated this
        # session (kept truthful by TeamsClient.on_state_change). Read by the
        # Teams settings status badge.
        self.teams_connected: bool = False
        # Short reason from the most recent Teams credential/connection failure,
        # empty when connected or never attempted. Read by the settings badge.
        self.teams_connect_error: str = ""
        # Late-bound inbound webhook handler for the Teams channel. The route
        # POST /api/messaging/teams is registered at app-build time (aiohttp
        # freezes routes at startup); maybe_start_teams sets this to the built
        # client's on_activity once credentials are present. None => 503.
        self.teams_on_activity: Any = None
        # True only while the Weixin (personal WeChat over iLink) channel's
        # long-poll loop is running (set in maybe_start_weixin). Read by the
        # WeChat settings status badge — a credential present at boot is NOT
        # enough to report "connected".
        self.weixin_connected: bool = False
        # Short reason from the most recent Weixin start failure, empty when
        # connected or never attempted. Read by the settings badge.
        self.weixin_connect_error: str = ""
        # True only while the WhatsApp (QR-linked personal account) client's
        # event loop is running (set in maybe_start_whatsapp). Read by the
        # WhatsApp settings status badge — a paired session DB on disk is NOT
        # enough to report "connected".
        self.whatsapp_connected: bool = False
        # Short reason from the most recent WhatsApp start failure, empty when
        # connected or never attempted. Read by the settings badge.
        self.whatsapp_connect_error: str = ""
        # Live channel transports (Telegram/WeCom/...) for channel-neutral
        # cross-surface mirror delivery — registered at boot by each channel's
        # gateway via ``register_channel_transport``. Slack keeps its dedicated
        # ``slack_client`` above (rich streaming mirror), so it is not stored here.
        self.channel_transports: dict[str, "MessagingTransport"] = {}
        self.owner_id = owner_id
        self._owner_hash: str | None = None
        # Branch+commit are resolved once by the CLI entrypoint (set_build_info,
        # pre-loop, post-detection); status_snapshot() reads this attribute so
        # subprocess never runs on the event loop. See module-level _build_info.
        self._build_info: tuple[str, str] = _build_info
        self.messages_received = 0
        # Broadcast: each SSE client gets its own queue; _notify_event wakes all
        self._sse_queues: list[asyncio.Queue[dict[str, Any]]] = []
        self._notify_event = asyncio.Event()
        # Depth + pending flag for suspend_slots_push(); see that method.
        self._slots_push_suspend = 0
        self._slots_push_pending = False
        # Time-based coalescing state for push_slots_update(). Guarded by a
        # threading.Lock because callers are not all on the event loop.
        self._slots_broadcast_lock = threading.Lock()
        # True while the startup open-tab restore is in flight. Suppresses the
        # open_slots.json snapshot so a periodic flush cannot overwrite the file
        # being restored from with a half-populated slot set — see
        # _persist_open_slots.
        self.restoring_open_slots = False
        # Per-instance (see the class-level frozenset baseline for why).
        self.unrestored_slot_keys: set[str] = set()
        self._notification_log: list[dict[str, Any]] = _load_notifications()
        self._unread_count: int = 0
        self._notification_coordinator = _new_notification_coordinator()
        # Notification bus (schema v2) — notify() adapts legacy calls onto it;
        # _deliver_note is the delivery sink (log, count, broadcast, persist).
        self.notification_bus = NotificationBus(sink=self._deliver_note)
        # Future of the most recent delivery-sink persist job (None when the
        # last persist ran inline). The app push handler awaits it to give a
        # durability guarantee; legacy producers ignore it (best-effort).
        self.last_notification_persist: asyncio.Future[bool] | None = None
        # Per-app push rate limiter (RFC Phase 2). State-owned (not a module
        # global) so its lifecycle matches the gateway instance and tests get
        # isolation for free.
        self.notification_rate_limiter = AppRateLimiter()
        # Per-channel user settings (RFC Phase 3): mute + priority override,
        # applied at the delivery sink so the bus stays pure.
        self.notification_channel_settings = ChannelSettings()
        # Resource-pressure producer: samples host posture (driven from the
        # event-loop heartbeat) and pushes episode-deduped notes to
        # system.resources. State-owned like the bus/limiter/settings so its
        # lifecycle matches the gateway instance.
        self.resource_pressure_notifier = ResourcePressureNotifier(self.notification_bus)
        self._slots: dict[str, _ChatSlot] = {}
        self._slot_registry = SlotRegistry()
        # Process-local Spec Builder outbox claims, keyed by directory + delivery.
        # Directory scope matters because aliases use different slots for the same
        # files; durable status remains owned by the app's decision ledger.
        self._spec_decision_deliveries_inflight: set[tuple[str, str]] = set()
        # Consumed claims whose durable finalization failed remain blocked from
        # redispatch while a later Spec Builder detail poll retries the ledger write.
        self._spec_decision_deliveries_consumed: set[tuple[str, str]] = set()
        # Slot keys that EXIST but are deliberately absent from ``_slots`` while
        # they are being built (see ``session_transfer``'s import path, which
        # retracts a slot so it is unreachable until its transcript and context
        # are in place). They still consume memory, so every cap must count them:
        # ``len(_slots)`` alone undercounts by however many imports are in flight,
        # and each concurrent import would then be waved past a full-slot cap.
        self._slots_under_construction: set[str] = set()
        self._slack_to_slot: dict[str, str] = {}  # Slack session_key → slot name
        # Live OPTIONS controls, keyed by the SESSION KEY that owns them.
        #
        # Deliberately here and not on ``_ChatSlot``: a plain Slack thread often
        # has no dashboard slot at all, and a slot-held record was simply dropped
        # for those sessions — so the whole expiry lifecycle never engaged and the
        # stale click it exists to prevent stayed possible (#1694). Keying by
        # session key makes the slotless case ordinary rather than special.
        #
        # One store, not a slot field plus a fallback: a slot can come into
        # existence at any moment (the channel surface reconciler creates one), so
        # a fallback map would go invisible the instant one appeared. It also
        # removes the two-store divergence that let a record be filed under one
        # index and cleared under another.
        self._slack_options_by_key: dict[str, tuple[PostedOptions, ...]] = {}
        self._slot_counter = 0
        # slot key → last context-meter reading, for seeding the bar when a
        # session is reopened after its ACP session is gone. Readings this
        # process took live here immediately; `_loaded` tracks whether the
        # file written by an earlier process has been merged in yet, and
        # `_dirty` whether the off-loop flush still owes a write. The map is
        # touched from the event loop (broadcast/read), the flush executor,
        # and the shutdown thread, so EVERY access — including the flags —
        # holds `_context_snapshots_lock`. File IO happens outside the lock:
        # the flush serializes under it, writes without it.
        self._context_snapshots: dict[str, dict] = {}
        self._context_snapshots_loaded = False
        self._context_snapshots_dirty = False
        self._context_snapshots_lock = threading.Lock()
        # Serializes whole flushes (dirty-check through file write). Two flush
        # paths exist — the periodic executor pass and the shutdown save — and
        # the data lock above deliberately excludes the file write, so without
        # this an overlapping pair can land writes out of order: the slower
        # flush writes an OLDER serialization last, rolling the file back, and
        # the already-cleared dirty flag means nothing corrects it until a new
        # reading arrives. Only flush threads contend here; the event loop
        # never acquires it.
        self._context_snapshots_flush_lock = threading.Lock()
        self._folders: list[dict[str, Any]] = []  # project folder definitions
        self._cron_folders: list[dict[str, Any]] = []  # cron job folder groupings
        # Malformed cron_folders.json entries dropped at load time, kept verbatim
        # so save_cron_folders round-trips them back instead of erasing bytes it
        # could not parse (mirrors the hooks store's unparsed-entry preservation).
        self._unparsed_cron_folder_entries: list[Any] = []
        self._chat_pins: list[dict[str, Any]] = []  # pinned chat messages
        # Malformed chat_pins.json entries dropped at load time, kept verbatim
        # so save_chat_pins round-trips them back instead of erasing bytes a
        # hand-edit left in a shape the loader could not validate (mirrors the
        # cron-folder unparsed-entry preservation, #5768/#5792).
        self._unparsed_chat_pin_entries: list[Any] = []
        # Serializes pin mutation + persistence so concurrent requests cannot
        # interleave snapshots and replace chat_pins.json out of order.
        # LoopBoundLock, not asyncio.Lock (#4800): DashboardState outlives any
        # single event loop (in-process gateway restart, test loops).
        self._chat_pins_lock = LoopBoundLock()
        # Serializes read-modify-write of the folder store; see
        # mutate_folders(). Constructed here rather than lazily so two
        # concurrent first-callers cannot each make their own lock and
        # serialize against nothing. LoopBoundLock binds no loop at
        # construction, so building it off-loop is safe — and it stays valid
        # across the loop changes this long-lived state survives (#4800).
        self._folders_lock = LoopBoundLock()
        # Identifies the current folder-TREE snapshot; bumped by mutate_folders
        # (the store's only writer) on each confirmed write. See
        # folders_generation() for what the client does with it.
        self._folders_generation = 0
        # Tag vocabulary: list of {id, name, color, order}. User-managed.
        self._tags: list[dict[str, Any]] = []
        # Malformed tags.json entries dropped at load time, kept verbatim so a
        # save round-trips them back instead of erasing bytes a hand-edit left
        # in a shape the loader could not validate. This store's save path runs
        # DURING load (seed/back-fill), so without preservation a single
        # hand-edited-but-malformed row is wiped at boot with no user action
        # (#5792, the worst of the sibling cases).
        self._unparsed_tag_entries: list[Any] = []
        # True once load_tags() parsed tags.json successfully (or seeded a
        # fresh install). False means the vocabulary state is UNKNOWN (parse
        # or I/O failure) — restore-time pruning must fail open then, because
        # a legitimately-empty vocabulary (user deleted every tag) must still
        # prune dangling ids while an unreadable one must not wipe anything.
        self._tags_authoritative: bool = False
        # Sidebar columns — flat list of {id, name, tag_ids, mode, order, include_untagged}
        self._tag_boards: list[dict[str, Any]] = []
        # Malformed tag-board (sidebar column) entries dropped at load time,
        # kept verbatim so a save round-trips them back rather than erasing a
        # hand-edited-but-typo'd column (#5792).
        self._unparsed_tag_board_entries: list[Any] = []
        self._background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        # Gateway replacement is process-wide, not an ordinary repeatable
        # background mutation.  The task latch coalesces duplicate /api/restart
        # clicks during the response-drain window; the in-progress latch also
        # serializes restart requests arriving through update and other server
        # paths.  Both are cleared when a mocked/failed exec returns, while a
        # successful exec replaces this state with the successor process.
        self._gateway_restart_task: asyncio.Task[None] | None = None
        self._gateway_restart_in_progress: bool = False
        # FIX 2: unattended-turn concurrency cap. Semaphore is created lazily
        # (see _background_turn_sema) because this object outlives / predates
        # the event loop in some hosts. The counters exist so a queued fleet is
        # observable — see background_turn_stats().
        self._bg_turn_sema: asyncio.Semaphore | None = None
        self._bg_turn_cap: int = 0
        self._bg_turns_running: int = 0
        self._bg_turns_waiting: int = 0
        self.no_crons: bool = False  # --no-crons flag: cron execution disabled
        self._hook_store: Any = None  # Lazy-init ScriptHookStore
        # Task refine state (background LLM spec generation)
        self._refine_status: str = "idle"  # idle, running, done, error, cancelled
        self._refine_text: str = ""
        self._refine_error: str = ""
        self._terminal_sessions: dict[str, Any] = {}  # PTY sessions for CLI panel
        self._terminal_reaper: asyncio.Task | None = None  # type: ignore[type-arg]
        self._browser_snapshot_pruner: asyncio.Task | None = None  # type: ignore[type-arg]
        self._browser_install_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._browser_install_error: str | None = None
        self._terminal_title_poller: asyncio.Task | None = None  # type: ignore[type-arg]
        # Background reconciler that surfaces channel-originated sessions
        # (slack:<ts>, discord:…) as chat slots. Held to prevent GC.
        self._channel_slot_reconciler: asyncio.Task | None = None  # type: ignore[type-arg]
        self._loop_heartbeat: asyncio.Task | None = None  # type: ignore[type-arg]
        # Off-loop event-loop stall watchdog; armed under the real gateway
        # entrypoint (faulthandler enabled) and stopped on shutdown. Annotated
        # here so the assignment in start_dashboard type-checks under mypy strict.
        self._loop_watchdog: "LoopStallWatchdog | None" = None
        # Prevent-sleep inhibitor + its poll task. Held to prevent GC and
        # released/cancelled on shutdown; annotated here so the assignments in
        # start_dashboard type-check under mypy.
        self._sleep_inhibitor: "SleepInhibitor | None" = None
        self._prevent_sleep_task: asyncio.Task | None = None  # type: ignore[type-arg]

        # Knowledge Library
        self._knowledge_store: "KnowledgeStore | None" = None  # Lazy-initialized on first access
        self._knowledge_watcher: asyncio.Task | None = None  # type: ignore[type-arg]
        # Slack channel name resolver (lazy-initialized on first /api/slack/channels hit)
        self._channel_resolver: Any = None
        self._refine_input: str = ""
        self._refine_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._refine_session_key: str = ""
        # slack_client is set via constructor param above; gateway may override later
        self._refine_answer_future: asyncio.Future | None = None  # type: ignore[type-arg]
        # WebSocket clients (multiplexed real-time connection)
        self._ws_clients: list[web.WebSocketResponse] = []
        self._owner_ws_clients: set[web.WebSocketResponse] = set()
        self._ws_log_subscribers: set[web.WebSocketResponse] = set()
        self._ws_subagent_subscribers: set[web.WebSocketResponse] = set()
        self._websocket_hub = _new_websocket_hub(self)
        self._approval_coordinator = ApprovalCoordinator()
        self._question_coordinator = QuestionCoordinator()
        # Pending tool approvals: id → asyncio.Future[bool]
        self._pending_approvals: dict[str, dict] = {}
        self._approval_futures: dict[str, asyncio.Future] = {}  # type: ignore[type-arg]
        # Pending agent questions (ask_question MCP tool): ask_id → payload /
        # Future[dict]. Distinct from _approval_futures because the resolution
        # value is the user's answer map, not an allow/deny boolean, and the
        # question card is addressed to one slot rather than the whole gateway.
        self._pending_questions: dict[str, dict] = {}
        self._question_futures: dict[str, asyncio.Future] = {}  # type: ignore[type-arg]
        self._flush_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._persistence_coordinator = _new_dashboard_persistence()
        # Update progress tracking (shared across all connected clients)
        self._update_progress: dict[str, str] | None = None  # {step, detail}
        # Restricted (incognito/temporary): session keys with memory writes disabled
        self._restricted_keys: set[str] = set()
        # Ephemeral: session keys with no memory writes at all
        self._ephemeral_keys: set[str] = set()
        # Per-project file index registry (shared across slots)
        from kiro_crew.dashboard.file_index import FileIndexRegistry

        self.file_indexes = FileIndexRegistry()

    def register_channel_transport(self, transport: "MessagingTransport") -> None:
        """Register a live channel transport for cross-surface mirror delivery.

        Called by each channel's gateway at boot, keyed by ``channel_type`` so
        the dashboard turn path can resolve the transport for a session's
        outbound mirror link and deliver a reply via ``send_message``.
        """
        ct = getattr(transport, "channel_type", "")
        if transport is not None and ct:
            self.channel_transports[ct] = transport
            dispatcher = getattr(transport, "dispatcher", None)
            if dispatcher is not None:
                dispatcher.dashboard_state = self

    def get_channel_transport(self, channel_type: str) -> "MessagingTransport | None":
        """Return the registered transport for *channel_type*, or None."""
        return self.channel_transports.get(channel_type)

    def channel_status(self) -> dict[str, dict[str, Any]]:
        """Per-channel ``{connected, error}``, keyed by ``channel_type``.

        Read off the same ``<channel>_connected`` / ``<channel>_connect_error``
        attributes each channel's own settings endpoint reports, so one page cannot
        disagree with another about whether a channel came up. A channel with no
        attributes yet reads as not connected with no reason, which is the honest
        answer for one that never started.

        The error string is bounded here as well as at each settings endpoint: this
        payload is polled, and a channel that reconnects in a loop would otherwise
        publish an unbounded reason on every tick.
        """
        # Imported here rather than at module scope: `channels` imports every
        # channel package, and those import this module through the gateway.
        try:
            from kiro_crew.channels import builtin_channel_descriptors

            names = [d.channel_type for d in builtin_channel_descriptors()]
        except Exception:
            logger.debug("channel status: roster unavailable", exc_info=True)
            return {}
        out: dict[str, dict[str, Any]] = {}
        for name in names:
            if name == "slack":
                connected = self.slack_client is not None and self.slack_socket_connected
            else:
                connected = bool(getattr(self, f"{name}_connected", False))
            out[name] = {
                "connected": connected,
                "error": str(getattr(self, f"{name}_connect_error", ""))[:120],
            }
        return out

    def wire_session_compact_callback(self) -> None:
        """Register the dashboard's compaction callback on the session manager."""

        async def _on_compacted(key: str, pct: float, *, success: bool) -> None:
            from kiro_crew.dashboard.chat_utils import dashboard_slot_key

            slot_key = dashboard_slot_key(key)
            if slot_key:
                # A channel-born session with an open tab is readable on BOTH
                # surfaces, and the user may be looking at either one, so both
                # get the notice: silently summarized history is the confusing
                # outcome this notice exists to prevent.
                if is_channel_session_key(key):
                    await self._notify_channel_compaction(key, pct, success=success)
            else:
                # No tab to append to, so the notice would be dropped and the
                # user would see summarized history with no explanation. Route
                # it to its own conversation instead.
                await self._notify_channel_compaction(key, pct, success=success)
                return
            slot = self.get_slot(slot_key)
            if slot is None:
                return
            template = _AUTO_COMPACT_NOTICE if success else _AUTO_COMPACT_FAILED_NOTICE
            message = template.format(pct=pct)
            try:
                # Tag kind="compaction" so this proactive auto-compact notice
                # (fired at session.autocompact_pct) is skipped by the dashboard's
                # follow-up [OPTIONS:] backward scan — same invariant as
                # chat_utils._append_compaction_notice. meta.kind covers history
                # reload; slot.append carries the meta on the live broadcast too.
                # (Routing through the chat_utils chokepoint would create a
                # state<->chat_utils import cycle; the notice is a hardcoded
                # template with no LLM content, so its redaction pass is moot.)
                slot.append("assistant", message, "msg msg-a", meta={"kind": "compaction"})
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to append compact notice to slot %s", slot_key
                )
            if success:
                # Reset the context bar — successful compact dropped usage.
                # reset lets the frontend drop its stored token counts too
                # (the "X / Y tokens" tooltip), which no longer describe the
                # compacted session.
                try:
                    self.broadcast_context_usage(
                        slot_key, {"slot": slot_key, "pct": 0.0, "reset": True}
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Failed to broadcast context_usage for slot %s", slot_key
                    )

        self.sessions.set_compact_callback(_on_compacted)

    async def _notify_channel_compaction(self, key: str, pct: float, *, success: bool) -> None:
        """Deliver the auto-compact notice to a channel-originated session.

        Isolated from the dashboard leg: a channel that is unreachable, ungoverned
        or unregistered must not turn a successful compaction into an exception on
        the session manager's background task.
        """
        try:
            await deliver_channel_compaction_notice(self, key, pct, success=success)
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to deliver channel compact notice for %s", key
            )

    def wire_session_unbind_listener(self) -> None:
        """Register the channel notice for a removed inbound resume binding.

        The session map audits every removal itself; what it cannot do is reach
        the conversation, because that means resolving a transport. This is where
        those halves meet. Called from async gateway startup, which is what makes
        the loop capture below correct: the listener itself runs on whatever thread
        performed the clear, so the loop has to be bound here.
        """
        loop = asyncio.get_event_loop()

        def _on_unbind(key: str, link: ChannelLink, reason: str) -> None:
            if reason == UNBIND_REASON_USER_UNLINK:
                # The in-channel unlink command has already replied in this very
                # conversation, so a notice here would be an echo of it.
                return
            if loop.is_closed():
                # The gateway is shutting down; there is nothing left to deliver
                # on. The SEL event already recorded the removal.
                logger.debug("Gateway loop closed; dropping inbound-unbind notice for %s", key)
                return
            try:
                # ``call_soon_threadsafe`` rather than a call-time
                # ``get_running_loop``: SessionMap is synchronous and a clear can
                # arrive on a worker thread, where there is no running loop and the
                # notice would be dropped. The loop captured at wire time is the
                # gateway's own. Stays SYNC and returns at once — the map holds its
                # lock across this call.
                loop.call_soon_threadsafe(self._spawn_unbind_notice, key, link, reason)
            except RuntimeError:
                # Raced a shutdown between the is_closed check and the call.
                logger.debug("Gateway loop gone; dropping inbound-unbind notice for %s", key)

        self.sessions.set_unbind_listener(_on_unbind)

    def _spawn_unbind_notice(self, key: str, link: ChannelLink, reason: str) -> None:
        """Start the notice task on the gateway loop, retaining a strong reference.

        Runs ON the loop (``call_soon_threadsafe`` target), so creating the task is
        safe here. Tracked in ``_background_tasks`` for the same reason
        :meth:`_spawn_ws_send` does it: the loop holds only a weak reference, so an
        untracked task can be collected mid-send and the notice silently vanishes.
        """
        task = asyncio.ensure_future(self._notify_inbound_unbind(key, link, reason))
        self._background_tasks.add(task)
        task.add_done_callback(self._on_unbind_notice_done)

    def _on_unbind_notice_done(self, task: "asyncio.Task") -> None:  # type: ignore[type-arg]
        """Release the finished notice task and consume any exception it stored.

        ``_notify_inbound_unbind`` swallows its own delivery failures, so an
        exception here is unexpected; reading it keeps asyncio from logging a bare
        "exception was never retrieved" at GC time.
        """
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug("inbound-unbind notice task failed: %s", exc)

    async def _notify_inbound_unbind(self, key: str, link: ChannelLink, reason: str) -> None:
        """Tell the conversation behind *link* that it is no longer attached.

        Rides the governed cross-surface ladder rather than the transport directly,
        so the send is capability-checked and governance-vetted like every other
        outbound notice. Best-effort: the binding is already gone and audited, so an
        unreachable, ungoverned or unregistered channel is logged and dropped
        rather than raised on a background task.
        """
        # Lazy: chat_runner imports this module at scope, so a top-level import
        # here would close the cycle.
        from kiro_crew.dashboard.chat_runner import _resolve_channel_target

        try:
            # Off-loop: the ladder's governance gate walks the profile directory,
            # which is unbounded on slow storage.
            target = await asyncio.to_thread(_resolve_channel_target, self, key, link)
            if target is None:
                # The docstring's "logged and dropped" promise, kept: an
                # unreachable, ungoverned or unregistered channel means the user
                # was NOT told their conversation lost its way back, and a silent
                # return here leaves that undeliverable notice invisible to the
                # operator too (the SEL event records the removal, not the
                # delivery failure).
                logging.getLogger(__name__).warning(
                    "inbound-unbind notice for %s undeliverable: no governed %s transport",
                    key,
                    link.channel_type,
                )
                return
            resolved, transport = target
            notice = _INBOUND_UNBIND_NOTICE.format(
                title=self._unbind_notice_title(key),
                why=_INBOUND_UNBIND_WHY.get(reason, _INBOUND_UNBIND_WHY_DEFAULT),
            )
            # The title is user-controlled (a rename, or an LLM-authored one), so
            # the rendered notice goes through the SHARED outbound display sink —
            # display canonicalization, exfiltration URLs, credentials, then
            # mention defang — rather than a second copy of that order here.
            await transport.send_message(
                resolved.channel_id,
                display_safe(notice),
                thread_id=resolved.thread_id,
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to deliver inbound-unbind notice for %s", key, exc_info=True
            )

    def _unbind_notice_title(self, key: str) -> str:
        """Name the detached session the way the user saw it, falling back to *key*.

        A title only exists while a slot is displaying the session; the raw key
        still identifies it, so nothing beyond the in-memory slot is worth a lookup.
        """
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key

        slot_key = dashboard_slot_key(key)
        slot = self.get_slot(slot_key) if slot_key else None
        if slot is None:
            return key
        return slot.display_title or key

    def wire_session_recycle_callback(self) -> None:
        """Register the dashboard's recycle-notification callback.

        Fired when the watchdog recycles a session (e.g. RSS threshold). Posts a
        notice into the slot so the user understands why their session reset.
        """

        async def _on_recycled(key: str, *, reason: str) -> None:
            from kiro_crew.dashboard.chat_utils import (
                _broadcast_expired_oauth_banners,
                dashboard_slot_key,
            )

            # A channel-born session's key is the channel's own even while its
            # tab is open, so ask which tab displays it rather than reading the
            # key's prefix — otherwise that tab resets with no explanation.
            slot_key = dashboard_slot_key(key)
            if not slot_key:
                return
            slot = self.get_slot(slot_key)
            if slot is None:
                return
            message = _SESSION_RECYCLED_NOTICE.format(reason=reason)
            try:
                # Tag kind="compaction" so the dashboard's follow-up [OPTIONS:]
                # backward scan skips this proactive system notice, matching the
                # auto-compact notice invariant.
                slot.append("assistant", message, "msg msg-a", meta={"kind": "compaction"})
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to append recycle notice to slot %s", slot_key
                )
            # The recycle ended the child that owned any open MCP OAuth
            # banner's loopback listener; push the read gate's verdict so an
            # open tab withdraws the dead Authorize link without waiting for
            # its next refetch (issue #8149's RSS-recycle path).
            try:
                _broadcast_expired_oauth_banners(self, slot)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to broadcast OAuth banner expiry for slot %s", slot_key
                )

        self.sessions.set_recycle_callback(_on_recycled)

        def _on_stuck_turn(key: str, parked_secs: float) -> None:
            """Surface a stuck turn in the chat where it is happening.

            Same delivery choice as the recycle notice above, for the same
            reason: the person who needs to know is whoever is watching that
            session, so the notice goes to that transcript rather than to a DM or
            a global feed. A WARNING in the journal is not reaching a user.

            Sync, unlike ``_on_recycled``: the hook that fires this is not
            awaiting anything, and appending to a slot needs no I/O.
            """
            from kiro_crew.dashboard.chat_utils import dashboard_slot_key

            # A channel-born session's key is the channel's own even while its tab
            # is open, so ask which tab displays it (see _on_recycled).
            slot_key = dashboard_slot_key(key)
            if not slot_key:
                return
            slot = self.get_slot(slot_key)
            if slot is None:
                return
            message = stuck_turn_notice(parked_secs)
            try:
                # kind="compaction" for the same reason as the recycle notice: it
                # keeps the dashboard's follow-up [OPTIONS:] backward scan from
                # treating a proactive system notice as the turn's own output.
                slot.append("assistant", message, "msg msg-a", meta={"kind": "compaction"})
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to append stuck-turn notice to slot %s", slot_key
                )

        self.sessions.on_stuck_turn = _on_stuck_turn

    @staticmethod
    def served_bundle_id(index: Path | None = None) -> str:
        """A short content hash of the SERVED frontend bundle's entry point.

        The WS status frame already lets the SPA reload itself across a gateway
        UPGRADE (it compares ``version`` between pushes), but a rebuild of the
        same version — the normal state of a git-checkout dev install, whose
        in-app update pulls, rebuilds, and restarts without the version moving —
        changes neither ``version`` nor ``commit``-visible fields in a way every
        open tab observes. This id is the field that does move: it hashes the
        entry ``index.html`` under ``static/dist`` (whose hashed asset names
        change on every rebuild), so any tab holding a stale bundle can detect
        the swap on its next status frame and reload.

        Cached by ``(mtime_ns, size)``: the snapshot sits on the hot status
        path (every ``/api/status`` poll and WS push), so the file is re-read
        only when a rebuild actually replaced it. Empty string when there is no
        served bundle (source tree without a built frontend, unit tests) — the
        SPA treats empty as UNKNOWN and never reloads over it.

        ``index`` is injectable for tests; the default is the entry point the
        gateway actually serves (``server.py``'s ``_DIST_DIR``).
        """
        if index is None:
            index = Path(__file__).resolve().parent.parent / "static" / "dist" / "index.html"
        try:
            st = index.stat()
        except OSError:
            return ""
        key = (st.st_mtime_ns, st.st_size)
        cached = _BUNDLE_ID_CACHE.get("v")
        if cached and cached[0] == key:
            return cached[1]
        try:
            digest = hashlib.sha256(index.read_bytes()).hexdigest()[:16]
        except OSError:
            return ""
        _BUNDLE_ID_CACHE["v"] = (key, digest)
        return digest

    def _count_lessons(self) -> int:
        """Count lessons from JSONL store + vector store (if enabled)."""
        count = len(self.lessons.load_all())
        if self.context_builder:
            vs = self.context_builder.memory.vector_store
            if vs:
                # COUNT(*) — not get_lessons() — so the status paths that poll
                # this per client do not materialize the whole lesson corpus
                # just to len() it.
                count += vs.count_lessons()
        return count

    def status_snapshot(
        self,
        *,
        cron_jobs: int | None = None,
        lessons: int | None = None,
        update_available: bool | None = None,
        update_can_apply: bool = False,
        update_check_status: str = "unchecked",
        update_command: str = "",
        update_latest_version: str = "",
        update_latest_version_display: str = "",
        update_channel: str = "",
        update_channel_move_pending: bool = False,
        update_managed_by: str = "",
        update_commits_ahead: int = 0,
        update_commits_behind: int = 0,
        update_last_checked_at: float | None = None,
        update_check_interval_secs: int = 43200,
        update_required: bool = False,
        update_min_version: str = "",
        update_can_arm: bool = False,
        version_display: str = "",
    ) -> dict[str, Any]:
        """Core status fields shared by /api/status, SSE, and WebSocket pushes."""
        uptime = int(time.time() - self.start_time)
        branch, commit = self._build_info
        return {
            "uptime": _fmt_duration(uptime),
            "start_time": self.start_time,
            "sessions": self.sessions.count,
            "messages": self.messages_received,
            "cron_jobs": cron_jobs if cron_jobs is not None else len(self.crons.list_jobs()),
            "lessons": lessons if lessons is not None else self._count_lessons(),
            "subagents": self.subagents.count if self.subagents else 0,
            "update_available": update_available,
            # Can THIS install replace its own code without the user leaving the
            # app? Only a git checkout can (``POST /api/update`` is git fetch +
            # reset). Shipped alongside the availability flag so the dashboard can
            # offer an Update button that will actually work, instead of one that
            # 409s on a wheel install — it must not have to run a fresh check just
            # to learn the layout.
            "update_can_apply": update_can_apply,
            # Where the check itself got to: "unchecked", "checking", "succeeded",
            # "failed" or "deferred". ``update_available`` is only authoritative on
            # "succeeded", and is null otherwise — without this pair the UI cannot
            # tell "checked and current" from "never checked", and painting a green
            # "Up to date" pill next to a red "couldn't check" line is the exact
            # half-truth the update contract exists to prevent.
            "update_check_status": update_check_status,
            # The upgrade command for an install that cannot replace itself, so the
            # 12-hourly BACKGROUND check can light the nav badge and still land the
            # user on something actionable. Deriving it only from a manual check
            # left the badge pointing at an Update button that 409s.
            "update_command": update_command,
            # The candidate release's version string ("" until a check finds a
            # newer build). The proactive update popup keys its per-version
            # snooze/skip on this, so it rides the hot-path subset; the
            # changelog text deliberately does not.
            "update_latest_version": update_latest_version,
            # DISPLAY-ONLY fold of the candidate above (clean base on the
            # stable channel); the popup's snooze/skip records key on the raw
            # value. Empty string on emitters that don't pass it.
            "update_latest_version_display": update_latest_version_display,
            # The release channel this INSTALL follows (the ``channel`` file
            # cli.sh wrote), empty when the layout has no channel at all (a git
            # checkout tracks a remote; a desktop bundle or container is updated
            # by something else). Distinct from ``release_channel`` below, which
            # is derived from the running version string and answers "which lane
            # were these BYTES built on". The two diverge for the whole window
            # between switching channels and the new lane's build landing, so the
            # switcher must key on this one or it would snap back on every poll.
            "update_channel": update_channel,
            # Is the running build ahead of everything ``update_channel``
            # publishes? True means that lane never shipped these bytes, so the
            # install is not on it yet and only re-running the installer moves
            # it. Deliberately NOT derived by comparing ``update_channel``
            # against ``release_channel`` below: promotion re-points the soaked
            # candidate's bytes without re-stamping them, so a promoted stable
            # build reports ``release_channel: insider`` while being a stable
            # install with nothing pending. False on every layout with no feed
            # answer.
            "update_channel_move_pending": update_channel_move_pending,
            # Who manages updates on this host: "" (self-managed), or the
            # mechanism that owns them (e.g. "command" for a policy-pinned
            # provider). The panel keys its update copy on this — a
            # command-managed host must not render self-managed installer
            # instructions its policy exists to bypass.
            "update_managed_by": update_managed_by,
            # Commit distance from a git checkout's upstream, both directions.
            # DIVERGED (both > 0) reports ``update_available: False`` exactly
            # like a current checkout — the destructive apply paths must never
            # be offered local commits — so without the counts the badge cannot
            # tell the two apart. 0/0 on non-git layouts and before any check.
            "update_commits_ahead": update_commits_ahead,
            "update_commits_behind": update_commits_behind,
            "update_can_arm": update_can_arm,
            "update_last_checked_at": update_last_checked_at,
            "update_check_interval_secs": update_check_interval_secs,
            # Mandatory-update verdict (enterprise governance pin OR the release
            # feed's breaking-change floor) plus the floor that triggered it.
            # The proactive update modal reads these off the status frame and
            # drops its snooze/skip affordances while required — it must not
            # depend on the user opening Settings to learn the update is
            # mandatory.
            "update_required": update_required,
            "update_min_version": update_min_version,
            # The RUNNING build's version folded for display (clean base on
            # the stable channel; see `_display_version` in
            # handlers/updates.py). DISPLAY-ONLY sibling of the raw `version`
            # the WS frame appends after this snapshot — that one is what the
            # SPA compares across pushes to force a reload over a gateway
            # upgrade, so it must never be folded. Empty string on emitters
            # that don't pass it (they render the raw version, as before).
            "version_display": version_display,
            "no_crons": self.no_crons,
            "branch": branch,
            "commit": commit,
            # Content hash of the SERVED frontend bundle (see
            # ``served_bundle_id``). The SPA compares it across status pushes
            # and reloads when it moves — the signal ``version`` cannot give
            # for a same-version rebuild (a git checkout's in-app update), and
            # one that reaches every open tab, not just the one that clicked
            # Update. Empty when no built bundle is served (dev source tree),
            # which the SPA treats as unknown, never as a change.
            "bundle_id": self.served_bundle_id(),
            # Which release lane these bytes came from: "nightly", "insider" or
            # "stable". Shipped as a RESOLVED ANSWER rather than leaving the
            # dashboard to parse `version` itself, because the rule is not
            # obvious (the same release is stamped as SemVer for desktop and
            # PEP 440 for wheels, and neither PEP 440 prerelease spelling
            # contains a `-`) and a frontend mirror of it would drift silently.
            # The dashboard uses this to give prerelease users an obvious way to
            # report a bug; see release_channel.py for the full rule.
            "release_channel": _release_channel_of_build(),
            # True only when Socket Mode actually connected this session, not
            # merely that tokens were present at boot. slack_client is set
            # whenever tokens existed, even if connect() then failed
            # (invalid_auth, a network error), so keying the status badge on it
            # alone painted a green "Connected" over a Slack that never came up.
            # Require BOTH a wired client and the real connect outcome the
            # gateway records after _connect_slack. This is the same field
            # /api/slack/config already reports to the settings badge.
            "slack_connected": (self.slack_client is not None and self.slack_socket_connected),
            # Every OTHER channel's live state, from the same flags each channel's
            # settings badge reads. Only `slack_connected` reached this payload
            # before, so System > Services was silent about a Telegram or Discord
            # channel that failed to start — the operator saw a healthy page and a
            # bot that never answered. Derived by roster loop, so the next channel
            # is covered without touching this dict.
            "channels": self.channel_status(),
            # Governance enforcement health: "active" (enforcing),
            # "disabled" (permissive default / not restricting), "degraded" (a
            # fail-closed trip, integrity mismatch, or unverified policy this
            # session), or "unknown" (policy not yet loaded).  Pure in-memory read.
            "governance": _governance_status(),
            # FIX 2: cap / in-flight / queued counts for unattended app-owned
            # turns. Published so a fleet parked behind the cap is visibly
            # throttled rather than looking like a set of hung workers.
            "background_turns": self.background_turn_stats(),
        }

    _APPROVAL_TIMEOUT = 7200  # 2 hours — triggers pause (not skip/fail) via deny path
    # Background sources (cron, heartbeat, taskrunner) have no human responder, so
    # waiting the full human window would burn 2h on every unattended approval. They
    # wait only this short window and then deny-fast, letting the turn proceed/fail
    # rather than hang.
    _BACKGROUND_APPROVAL_TIMEOUT_SECS = 180  # 3 minutes — deny-fast for unattended runs
    # Agent questions block a live MCP tool call, so the ceiling is bounded by
    # how long the agent transport will hold that call open — far shorter than
    # the 2h approval window. Callers pick a value inside these bounds.
    _QUESTION_TIMEOUT_DEFAULT = 300  # 5 minutes
    # Hard ceiling set by the ACP tool-stall watchdog, NOT by the `wait` tool.
    # `acp/client.py::_TOOL_STALL_TIMEOUT` is 600s and is armed once a tool call
    # is dispatched; a blocked ask_question emits no progress frames, so a window
    # at or beyond 600s lets the watchdog declare the turn dead and kill it —
    # after which an answer has no turn left to return to. 540s keeps a 60s
    # margin below the watchdog. `wait` can afford 1800s because it is a
    # different mechanism; copying that number here was the bug.
    _QUESTION_TIMEOUT_MAX = 540  # 9 minutes — 60s under the 600s tool-stall watchdog
    _FLUSH_INTERVAL = 5  # seconds between dirty-slot flushes

    # ── FIX 2: bounded concurrency for unattended, app-owned turns ──────────
    # Nothing capped chat slots or concurrent turns. The nearest analogue caps
    # at 12 (dashboard/handlers/terminal.py::_MAX_SESSIONS, 429 on excess) and
    # the only real ceiling was asyncio.Semaphore(4) on agent cold starts plus
    # host memory — so an app that arms N worker slots could put N turns on the
    # runtime at once and exhaust it. Shape copied from
    # apps/builtins/code_review_sage/sage_lib/review_pool.py (default +
    # ``MAX_CONCURRENT_CEIL`` clamp): configurable, but never unbounded.
    MAX_BACKGROUND_TURNS = 4  # default in-flight unattended turns
    MAX_BACKGROUND_TURNS_CEIL = 16  # hard ceiling — config can raise up to here
    # Longest a queued turn may sit waiting for a permit. Needed because the
    # queue wait happens INSIDE the coroutine ``spawn_guarded_turn`` already
    # bounds at ``CHAT_TURN_TIMEOUT`` (7200s), so an unbounded wait would let a
    # fully-saturated cap consume a turn's whole ceiling and then kill it with
    # "turn exceeded the 7200s ceiling" — a true statement that names the wrong
    # cause. 1800s never trips under ordinary throttling and leaves 90 minutes
    # of the ceiling for the turn itself; on expiry the turn fails with a
    # message that says what actually happened.
    _BACKGROUND_QUEUE_WAIT_SECS = 1800

    _log = logging.getLogger(__name__)

    def approval_timeout_for(self, slot: "_ChatSlot") -> float:
        """Approval window for an interactive tool prompt raised inside *slot*.

        FIX 1. The dashboard runner waits on its OWN per-slot future rather than
        going through :meth:`request_approval`, so it never reached the
        deny-fast background branch: every unattended app worker that tripped
        one untrusted tool held its slot for the full
        ``_APPROVAL_TIMEOUT`` (2h) and then denied anyway — two hours of a
        worker's life spent parked, with nothing on screen to explain it.

        Returning the SAME two constants ``request_approval`` uses is the point:
        the previous bug was a hardcoded ``7200.0`` at the call site, which
        could not track either constant. See :attr:`_ChatSlot.unattended` for
        why app-ownership is the detector.
        """
        if slot.unattended:
            return float(self._BACKGROUND_APPROVAL_TIMEOUT_SECS)
        return float(self._APPROVAL_TIMEOUT)

    def effective_max_background_turns(self) -> int:
        """Configured cap on concurrent unattended turns.

        Reads ``config.json → dashboard.max_background_turns`` (same
        ``_raw_config`` route ``sandbox.py`` and ``mcp_gateway/pool.py`` use for
        their tunables) and clamps to ``[1, MAX_BACKGROUND_TURNS_CEIL]`` so an
        operator can widen the fleet without editing code but can never remove
        the bound. Unreadable/garbage config falls back to the default rather
        than failing a turn.
        """
        try:
            raw = (_raw_config().get("dashboard") or {}).get(
                "max_background_turns", self.MAX_BACKGROUND_TURNS
            )
            val = int(raw)
        except Exception:
            self._log.debug("background-turn cap config unavailable; using default", exc_info=True)
            val = self.MAX_BACKGROUND_TURNS
        return max(1, min(val, self.MAX_BACKGROUND_TURNS_CEIL))

    def _background_turn_sema(self) -> asyncio.Semaphore:
        """The cap's semaphore, created on first use and resized when idle.

        Lazy because ``DashboardState`` is constructed before the event loop in
        some hosts (tests, CLI) and ``asyncio.Semaphore`` binds to the running
        loop. Resized only while nothing is in flight: in-flight holders own
        permits on the object they entered, so swapping under them would let the
        cap be exceeded by the difference.
        """
        eff = self.effective_max_background_turns()
        if self._bg_turn_sema is None:
            self._bg_turn_sema = asyncio.Semaphore(eff)
            self._bg_turn_cap = eff
        elif eff != self._bg_turn_cap and not (self._bg_turns_running or self._bg_turns_waiting):
            self._bg_turn_sema = asyncio.Semaphore(eff)
            self._bg_turn_cap = eff
        return self._bg_turn_sema

    def background_turn_stats(self) -> dict[str, int]:
        """Cap / in-flight / queued counts — the cap's observability surface.

        Surfaced in the status payload and asserted by tests, so "the fleet is
        queued behind the cap" is a readable state rather than an invisible
        stall that looks like a hung worker.
        """
        return {
            "cap": self._bg_turn_cap or self.effective_max_background_turns(),
            "running": self._bg_turns_running,
            "waiting": self._bg_turns_waiting,
        }

    async def run_background_turn(self, slot: "_ChatSlot", coro: Any) -> Any:
        """Await *coro* under the unattended-turn cap.

        QUEUES rather than rejects at the cap: a rejected crew turn loses the
        issue it was mid-way through, while a queued one only starts late. An
        attended slot is passed straight through, so this wrapper is inert for
        every human session and adds no semaphore traffic to the interactive
        path.
        """
        if not slot.unattended:
            return await coro
        sema = self._background_turn_sema()
        queued = sema.locked()
        if queued:
            self._bg_turns_waiting += 1
            # info, not debug: this is the difference between "the fleet is
            # throttled" and "a worker is hung", and it is the only signal a
            # queued turn emits before it starts.
            self._log.info(
                "background turn queued behind the cap: slot=%s cap=%d running=%d waiting=%d",
                slot.key,
                self._bg_turn_cap,
                self._bg_turns_running,
                self._bg_turns_waiting,
            )
        try:
            await asyncio.wait_for(sema.acquire(), timeout=self._BACKGROUND_QUEUE_WAIT_SECS)
        except asyncio.TimeoutError:
            coro.close()
            self._log.warning(
                "background turn abandoned after waiting %ds for a permit: slot=%s cap=%d",
                self._BACKGROUND_QUEUE_WAIT_SECS,
                slot.key,
                self._bg_turn_cap,
            )
            raise TimeoutError(
                f"queued {self._BACKGROUND_QUEUE_WAIT_SECS}s behind the background-turn "
                f"cap ({self._bg_turn_cap} concurrent) without a free slot"
            ) from None
        except BaseException:
            # Cancelled while queued: the turn never ran, so close its coroutine
            # rather than leaving an un-awaited coroutine warning behind.
            coro.close()
            raise
        finally:
            if queued:
                self._bg_turns_waiting -= 1
        self._bg_turns_running += 1
        try:
            return await coro
        finally:
            self._bg_turns_running -= 1
            sema.release()

    @property
    def knowledge_store(self):  # type: ignore[override]
        """Lazy-init KnowledgeStore on first access."""
        if self._knowledge_store is None:
            db_dir = os.path.join(str(config_dir()), "workspace", "knowledge")
            os.makedirs(db_dir, exist_ok=True)
            self._knowledge_store = KnowledgeStore(os.path.join(db_dir, "knowledge.db"))
        return self._knowledge_store

    def enable_yolo(self, *, from_config: bool = False) -> None:
        """Activate safety override (delegates to safety_override module)."""
        source = "config" if from_config else "dashboard"
        safety_override().activate(source)

    def disable_yolo(self) -> None:
        """Deactivate safety override (delegates to safety_override module)."""
        safety_override().deactivate("dashboard")

    def is_yolo_active(self) -> bool:
        """Return whether safety override is active (delegates to safety_override module)."""
        return safety_override().is_active()

    @property
    def _yolo(self) -> bool:
        """Backward-compat property for code reading _yolo directly."""
        return safety_override().is_active()

    @_yolo.setter
    def _yolo(self, value: bool) -> None:
        """Backward-compat setter for tests that assign state._yolo = True/False."""
        if value:
            safety_override().activate("dashboard")
        else:
            safety_override().deactivate("dashboard")

    async def request_approval(
        self,
        approval_id: str,
        source: str,
        tool: str,
        *,
        tool_input: str = "",
        tool_purpose: str = "",
        slot: str = "",
        is_background: bool = False,
    ) -> bool:
        """Request interactive approval and deny on timeout or cancellation."""
        return await _approvals_for(self).request(
            self,
            approval_id,
            source,
            tool,
            tool_input=tool_input,
            tool_purpose=tool_purpose,
            slot=slot,
            is_background=is_background,
            redact_url=redact_exfiltration_urls,
            redact_secret=redact_credentials,
        )

    def _audit_and_broadcast_approval(
        self,
        session_key: str,
        approval_id: str,
        approved: bool,
        decision: str = "",
    ) -> None:
        """Audit and broadcast one approval decision."""
        _approvals_for(self).audit_and_broadcast(
            self,
            session_key,
            approval_id,
            approved,
            decision,
            audit_provider=sel,
        )

    def resolve_state_approval(self, approval_id: str, approved: bool) -> bool:
        """Resolve only a state-level background approval."""
        return _approvals_for(self).resolve_state(self, approval_id, approved)

    def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        *,
        rejected_once: bool = False,
    ) -> bool:
        """Resolve one state- or slot-level approval without widening authority."""
        return _approvals_for(self).resolve(
            self,
            approval_id,
            approved,
            rejected_once=rejected_once,
            permission_marker=_permission_marker(),
        )

    def _redact_questions(self, questions: list[dict]) -> list[dict]:
        """Redact questions and reject text collisions introduced by redaction."""
        return _questions_for(self).redact_questions(
            questions,
            redact_url=redact_exfiltration_urls,
            redact_secret=redact_credentials,
        )

    async def post_question_card(self, slot_key: str, questions: list[dict]) -> int:
        """Post a non-blocking owner-only question card."""
        return await _questions_for(self).post_card(self, slot_key, questions)

    def mark_question_pending(
        self,
        slot_key: str,
        *,
        blocking: bool,
        card_id: str,
        questions: list[dict] | None = None,
    ) -> None:
        """Record one unanswered question and push the slot status."""
        _questions_for(self).mark_pending(
            self,
            slot_key,
            blocking=blocking,
            card_id=card_id,
            questions=questions,
        )

    def clear_question_pending(
        self,
        slot_key: str,
        *,
        blocking: bool | None = None,
        card_id: str | None = None,
    ) -> bool:
        """Retire only question records matching both supplied filters."""
        return _questions_for(self).clear_pending(
            self,
            slot_key,
            blocking=blocking,
            card_id=card_id,
        )

    def _broadcast_question_retired(self, slot_key: str, card_ids: list[str]) -> None:
        """Tell owner clients that question cards are no longer actionable."""
        _questions_for(self).broadcast_retired(self, slot_key, card_ids)

    def _push_slots(self) -> None:
        """Push question status without failing the question lifecycle."""
        _questions_for(self).push_slots(self)

    async def request_question(
        self,
        ask_id: str,
        slot_key: str,
        questions: list[dict],
        timeout: int | None = None,
    ) -> dict[str, str] | None:
        """Run the legacy blocking owner question round-trip."""
        return await _questions_for(self).request(
            self,
            ask_id,
            slot_key,
            questions,
            timeout,
        )

    def resolve_question(self, ask_id: str, answers: dict[str, str] | None) -> bool:
        """Resolve one blocking question future if it is still pending."""
        return _questions_for(self).resolve(self, ask_id, answers)

    def cancel_questions_for_slot(self, slot_key: str) -> int:
        """Unblock every blocking question owned by one slot."""
        return _questions_for(self).cancel_for_slot(self, slot_key)

    def start_flush_loop(self) -> None:
        _persistence_for(self).start_flush_loop(self)

    async def _flush_loop(self) -> None:
        await _persistence_for(self)._flush_loop(self)

    def flush_slot_now(self, slot: _ChatSlot) -> None:
        _persistence_for(self).flush_slot_now(self, slot)

    def _flush_dirty_slots(self) -> None:
        _persistence_for(self)._flush_dirty_slots(self)

    def _persist_open_slots(self) -> None:
        _persistence_for(self)._persist_open_slots(self)

    def notify(
        self,
        kind: str,
        title: str,
        body: str,
        *,
        meta: dict | None = None,
        url: str | None = None,
        actions: list[dict[str, Any]] | None = None,
    ) -> None:
        """Validate and deliver a legacy notification without raising."""
        _notifications_for(self).notify(
            self,
            kind,
            title,
            body,
            meta=meta,
            url=url,
            actions=actions,
        )

    def _deliver_note(self, note: dict[str, Any]) -> None:
        """Deliver one bus-validated note to memory, clients, and disk."""
        _notifications_for(self).deliver(self, note)

    def register_sse(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new SSE client and return its dedicated queue."""
        return _notifications_for(self).register_sse(self)

    def unregister_sse(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove an SSE client queue on disconnect."""
        _notifications_for(self).unregister_sse(self, q)

    async def _rewrite_notifications_async(self) -> None:
        """Rewrite notification storage after earlier FIFO append jobs."""
        await _notifications_for(self).rewrite(self)

    async def delete_notification(self, ts: str) -> bool:
        """Remove a single notification by timestamp and persist to disk."""
        return await _notifications_for(self).delete(self, ts)

    async def ack_notification(self, ts: str) -> bool:
        """Mark a notification as acknowledged and persist it."""
        return await _notifications_for(self).set_acknowledged(self, ts, acknowledged=True)

    async def unack_notification(self, ts: str) -> bool:
        """Mark a notification as unread and persist it."""
        return await _notifications_for(self).set_acknowledged(self, ts, acknowledged=False)

    async def resolve_skill_review_notifications(self, slug: str, consumed_at: str) -> int:
        """Acknowledge the consumed generation of one skill review."""
        return await _notifications_for(self).resolve_skill_reviews(self, slug, consumed_at)

    async def clear_notifications(self) -> None:
        """Clear memory and clients before awaiting the durable rewrite."""
        await _notifications_for(self).clear(self)

    def get_slot(self, name: str) -> _ChatSlot | None:
        """Look up a slot by name without creating it. Returns None if absent."""
        return _registry_for(self).get_slot(self, name)

    def running_session_keys(self) -> frozenset[str]:
        """Return effective session keys whose current slots are running."""
        from kiro_crew.dashboard.chat_utils import effective_session_key

        return _registry_for(self).running_session_keys(self, effective_session_key)

    def spend_slot_by_session(self) -> dict[str, str]:
        """Map live effective session identities to their spend-owning slot keys."""
        from kiro_crew.dashboard.chat_utils import effective_session_key

        return _registry_for(self).spend_slot_by_session(self, effective_session_key)

    def native_subagent_snapshots(
        self,
        terminal_limit: int = NATIVE_SUBAGENT_TERMINAL_KEEP,
        ttl_secs: float = NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    ) -> list[dict[str, object]]:
        """Return bounded native running and terminal cards for WS replay.

        DashboardState owns the slot record shape. The WebSocket layer consumes
        these transport-ready snapshots without reaching into private slot data.
        """
        now = time.time()
        running: list[dict[str, object]] = []
        done: list[dict[str, object]] = []
        for slot in list(self._slots.values()):
            output = slot._native_subagent_output
            for info in list(slot._native_subagent_tracker.values()):
                card_id = str(info.get("id") or "")
                if not card_id:
                    continue
                base: dict[str, object] = {
                    "id": card_id,
                    "slot": slot.key,
                    "task": str(info.get("task") or ""),
                    "agent": str(info.get("agent") or ""),
                }
                if info.get("done"):
                    done_at = float(info.get("done_at") or 0.0)
                    if done_at and (now - done_at) > ttl_secs:
                        continue
                    if info.get("stopped"):
                        outcome = "stopped"
                    elif info.get("error"):
                        outcome = "failed"
                    else:
                        outcome = "completed"
                    done.append(
                        {
                            **base,
                            "done": True,
                            "elapsed": float(info.get("elapsed") or 0.0),
                            "error": info.get("error"),
                            "stopped": bool(info.get("stopped")),
                            "outcome": outcome,
                            "result": str(info.get("result") or ""),
                            "done_at": done_at,
                        }
                    )
                else:
                    running.append(
                        {
                            **base,
                            "done": False,
                            "streaming": native_subagent_output_tail(output.get(card_id, [])),
                            "last_tool": str(info.get("last_tool") or ""),
                            "started": float(info.get("started") or now),
                        }
                    )
        if terminal_limit >= 0 and len(done) > terminal_limit:

            def snapshot_done_at(snapshot: dict[str, object]) -> float:
                value = snapshot.get("done_at")
                return float(value) if isinstance(value, (int, float)) else 0.0

            done.sort(key=snapshot_done_at, reverse=True)
            done = done[:terminal_limit]
        return running + done

    def has_slot(self, name: str) -> bool:
        """Check if a slot exists by name."""
        return _registry_for(self).has_slot(self, name)

    def get_linked_slot(self, session_key: str) -> "_ChatSlot | None":
        """Resolve a Slack link and clean up a stale reverse-index entry."""
        return _registry_for(self).get_linked_slot(self, session_key)

    def resolve_slot(self, name: str) -> _ChatSlot | None:
        """Resolve an exact key or the newest timestamped bare ``chat-N`` key."""
        return _registry_for(self).resolve_slot(self, name, _CHAT_N_RE.fullmatch)

    def link_slack(self, slot_name: str, thread_ts: str, channel_id: str) -> None:
        """Update a slot's Slack link state and persist to SessionStore."""
        slot = self._slots.get(slot_name)
        if not slot:
            return
        # A thread handoff is ONE action with TWO persisted writes: the previous
        # owner's link is cleared and this slot's is claimed. Each write rewrites
        # the whole session map, so as two separate writes they are separately
        # interruptible — a failure or a concurrent writer in between leaves the
        # thread with no owner (the clear landed, the claim did not) or with two
        # (the reverse). Batching makes the pair one critical section and one
        # write, matching the same guarantee ``SessionMap.set_slack_link``
        # already gives its own eviction-and-claim.
        with self.sessions.batched_save() if self.sessions else contextlib.nullcontext():
            self._link_slack_persisted(slot, slot_name, thread_ts, channel_id)
        self.push_slots_update()

    def _link_slack_persisted(
        self, slot: Any, slot_name: str, thread_ts: str, channel_id: str
    ) -> None:
        """The link handoff itself: in-memory indexes plus both persisted writes.

        Split out only so :meth:`link_slack` can wrap the whole sequence in one
        ``batched_save``; the dashboard push stays OUTSIDE that block because it
        is not a map mutation.
        """
        # Remove stale mapping if slot was previously linked to a different thread
        old_ts = slot._slack_thread_ts
        if old_ts and old_ts != thread_ts:
            self._slack_to_slot.pop(old_ts, None)
        # Clear persisted link of old slot if this thread was previously owned by another slot
        old_owner = self._slack_to_slot.get(thread_ts)
        if old_owner and old_owner != slot_name:
            old_slot = self._slots.get(old_owner)
            if old_slot:
                old_slot._slack_linked = False
                old_slot._slack_thread_ts = ""
                old_slot._slack_channel = ""
            if self.sessions:
                from kiro_crew.dashboard.chat_utils import (
                    _history_key_for,
                    effective_session_key,
                )

                # The previous owner's slot may already be gone; fall back to
                # deriving its key from the name in that case.
                old_key = (
                    effective_session_key(old_slot) if old_slot else _history_key_for(old_owner)
                )
                self.sessions.set_slack_link(old_key, "", "")
        slot._slack_linked = True
        slot._slack_channel = channel_id
        slot._slack_thread_ts = thread_ts
        self._slack_to_slot[thread_ts] = slot_name
        # Persist so link survives gateway restarts
        if self.sessions:
            from kiro_crew.dashboard.chat_utils import effective_session_key

            self.sessions.set_slack_link(effective_session_key(slot), thread_ts, channel_id)

    def get_or_create_slot(
        self,
        name: str | None = None,
        agent: str = "",
        workspace: str = "default",
        model: str = "",
        mode: str = "",
        memory_mode: str | None = None,
        ephemeral: bool | None = None,
        app: str = "",
        linked_session_key: str = "",
        channel_origin: bool = False,
        origin: str | None = None,
        *,
        # Opt-in for the survey's "new user" session counter, DISTINCT from the
        # origin tag: ``SlotOrigin.USER`` carries the ``slots:user`` privacy
        # semantics and is deliberately set by non-human paths too (the
        # session-control create verb mints USER slots so agent-created
        # sessions stay app-invisible), so origin alone cannot mean "a person
        # started this chat". Only the human request-layer paths (chat-send
        # auto-create, new-chat tab, fork) pass True. Default False is the
        # chosen fail direction, matching
        # the counter's own philosophy below: a future missed opt-in only
        # delays the survey (lost signal); defaulting to count would let
        # unattended agent activity satisfy the gate (corrupted signal).
        count_user_session: bool = False,
    ) -> _ChatSlot:
        """Return existing slot or create a new one.

        *linked_session_key* binds a new slot to the session its conversation
        actually runs on (a channel thread, a cron job). It must be supplied
        here rather than assigned afterwards: the Slack-link hydration below
        reads the persisted link off the slot's effective session key, so a
        binding applied later would hydrate against the wrong key and leave a
        channel-born tab looking unlinked.
        """
        existing, creation = _registry_for(self).prepare_creation(
            self,
            name,
            mode=mode,
            memory_mode=memory_mode,
            normalize_key=lambda value: _normalize_slot_key(value),
            mint_key=lambda prefix, index, timestamp: _mint_slot_key(prefix, index, timestamp),
            timestamp_provider=lambda: time.time(),
        )
        if existing is not None:
            return existing
        assert creation is not None
        name = creation.key
        requested_name = creation.requested_name
        minted_new = creation.minted_new
        slot = _ChatSlot(
            name,
            agent=agent,
            workspace=workspace,
            model=model,
            mode=mode,
            memory_mode=memory_mode or "persistent",
        )
        if requested_name and requested_name != name:
            # The caller asked for a human-readable name (e.g. "Artifact: My
            # Doc"); the key had to be folded, but the pretty form makes a
            # better initial title than the "New session" placeholder. Titles
            # are dashboard-surfaced, so apply the same redaction as explicit
            # title pinning in api_chat_slot_create. ``_titled`` stays False —
            # auto-title and explicit pinning can still override.
            pretty_title, _ = redact_exfiltration_urls(requested_name)
            pretty_title, _ = redact_credentials(pretty_title)
            slot.title = pretty_title
        slot._tab_id = uuid.uuid4().hex[:12]
        slot._on_message = self._broadcast_chat_message
        slot._on_question_retired = self._broadcast_question_retired
        slot._app = app
        # ``origin`` must be declared by the layer that actually knows it, and
        # an undeclared non-app slot stays UNTAGGED ("") rather than being
        # called USER.
        #
        # Deriving USER here would be fail-OPEN: this function cannot tell a
        # person typing in the dashboard from a background injection, so every
        # untagged caller — cron result injection, workflow inject, Slack, the
        # OpenAI-compatible endpoint — would read as USER and hand that private
        # content to an app holding `slots:user`. Only the request layer
        # knows whether an app token was presented, so USER/APP is decided
        # there (see chat_handlers) and background callers declare CRON/SYSTEM.
        #
        # "" is invisible to every cross-slot scope (the gate compares against
        # SlotOrigin.USER), so a caller that forgets to declare loses
        # visibility instead of leaking — the direction this has to fail in.
        slot._origin = origin or (SlotOrigin.APP if app else "")
        if minted_new and count_user_session and slot._origin == SlotOrigin.USER:
            # Count only genuine, newly-minted user chats toward the survey's
            # "new user" window (session_pulse_counter). `minted_new` excludes
            # restore/rehydrate (which passes the persisted key as name) and
            # get-existing, so a restart never re-counts already-seen sessions.
            # `count_user_session` carries the human-started signal: only the
            # request-layer paths a person actually drives (chat-send
            # auto-create, new-chat tab, fork) opt in, so agent-minted USER
            # slots (the session-control create verb) do not satisfy the
            # survey gate on their own (#6139). The
            # origin conjunct stays as the invariant floor: a caller can never
            # count a non-USER slot, flag or not. Best-effort:
            # the helper swallows its own I/O errors and never raises into
            # slot creation.
            #
            # Off the loop, because this method is synchronous and every
            # request-layer birth runs it on the gateway loop -- the counter's
            # read + mkdir + tempfile write + replace would stall it on slow
            # storage. The offload is the counter's, not this allocation's: this
            # block must not become a suspension point, or callers could observe
            # a half-configured slot.
            increment_user_session_count_off_loop()
        if memory_mode and memory_mode != "persistent":
            self._restricted_keys.add(f"dashboard:{name}")
        if ephemeral:
            self._ephemeral_keys.add(f"dashboard:{name}")
        # Hydrate only a complete, genuine Slack link. Other transports still
        # write their namespaced origin id through the legacy channel field;
        # those are projected separately via ``links`` and must never make the
        # destructive Slack actions appear.
        if channel_origin:
            # Additive: never cleared, because get_or_create_slot also returns
            # EXISTING slots and a later plain call must not downgrade a tab
            # that a channel path already claimed.
            slot.channel_origin = True
        if linked_session_key:
            slot.linked_session_key = linked_session_key
        elif self.sessions:
            # No caller-supplied binding, but a channel-stem name means this slot
            # displays a conversation that runs on the channel's own session.
            # Resolving it HERE rather than in each caller is what makes the
            # binding correct by construction: the History resume path builds the
            # slot without one, and an unbound channel tab silently answers from a
            # dashboard-only session whose replies never reach the thread.
            #
            # Only ever adopts a key the session map actually holds, so a slot
            # whose name merely looks channel-shaped stays unbound. Validated the
            # same way ``surface_channel_session`` validates its own argument:
            # only a real channel key may become a binding, so a malformed map
            # answer leaves the slot unbound (a supported state) rather than
            # routing the user's replies to a session no channel reads.
            if is_channel_session_key(name):
                resolved = self.sessions.channel_key_for_stem(name)
                if isinstance(resolved, str) and is_channel_session_key(resolved):
                    slot.linked_session_key = resolved
        try:
            if self.sessions:
                from kiro_crew.dashboard.chat_utils import effective_session_key

                _ts, _ch = self.sessions.get_slack_link(effective_session_key(slot))
                slot._slack_linked = _is_genuine_slack_link(_ts, _ch)
                if slot._slack_linked:
                    namespaced = _split_namespaced_channel_id(_ch)
                    slot._slack_channel = namespaced[1] if namespaced else (_ch or "")
                    slot._slack_thread_ts = _ts or ""
                    # Rebuild the thread -> slot index too, not just the fields:
                    # inbound replies resolve through the index, so restoring
                    # the fields alone leaves a mirrored session delivering to
                    # Slack but not back to its tab after a restart.
                    #
                    # Index ONLY a genuine mirror-OUT. A channel-born session's
                    # ``slack_thread_ts`` is a SELF-reference -- the thread the
                    # session lives IN, not one it mirrors TO -- and indexing
                    # that would make every inbound Slack message resolve to a
                    # "linked" slot and run through the dashboard chat runner
                    # instead of the Slack transport, silently changing the
                    # execution engine and approval semantics of all Slack
                    # traffic.
                    #
                    # Both tests are load-bearing and neither is a name
                    # heuristic. A channel slot whose stem RESOLVED is caught by
                    # ``linked_session_key``; one whose stem did NOT resolve
                    # (leaving that field empty) is caught by comparing the link
                    # against the slot's own filename stem, because a channel
                    # slot is named for the very thread it lives in. A dashboard
                    # slot that merely happens to be named ``slack_...`` matches
                    # neither test and is still indexed.
                    from kiro_crew.history import _safe_key
                    from kiro_crew.messaging.link import canonical_key

                    _self_ref = False
                    if _ts:
                        _self_ref = _safe_key(canonical_key(_ts)) == name
                    if _ts and not slot.linked_session_key and not _self_ref:
                        self._slack_to_slot[_ts] = name
        except Exception:
            pass
        _registry_for(self).put_slot(self, name, slot)
        # Publish the updated key set to SessionManager and the surface
        # registry NOW, not just on the HTTP slot endpoints. Slots born here
        # programmatically (auto-research campaign workers, cron/workflow
        # inject, task runner, spec builder) never pass through those
        # endpoints, so ``_active_dashboard_slots`` stayed stale and the idle
        # sweep's orphan branch reaped their live sessions as "slot gone" —
        # killing the companion subagent runtime (and any subagent mid-prompt
        # on it) along the way. Guarded: tests build this state without a
        # SessionManager, and a sync failure must never break slot creation.
        if self.sessions:
            try:
                from kiro_crew.dashboard.chat_utils import _sync_dashboard_slots

                _sync_dashboard_slots(self)
            except Exception:
                logger.warning(
                    "get_or_create_slot: active-slot sync failed for %s", name, exc_info=True
                )
        self.push_slots_update()
        return slot

    def live_slot_count(self) -> int:
        """Count published and allocated-but-unpublished slots."""
        return _registry_for(self).live_slot_count(self)

    def creator_slot_count(self, creator_key: str) -> int:
        """Count live slots attributed to one non-empty creator key."""
        return _registry_for(self).creator_slot_count(self, creator_key)

    def begin_slot_construction(self, key: str) -> None:
        """Mark a slot key as allocated but not yet published."""
        _registry_for(self).begin_slot_construction(self, key)

    def end_slot_construction(self, key: str) -> None:
        """Release an allocated-but-unpublished slot marker."""
        _registry_for(self).end_slot_construction(self, key)

    def reseed_slot_counter(self) -> None:
        """Advance the mint counter past every parseable current slot key."""
        _registry_for(self).reseed_slot_counter(
            self,
            slot_index_from_key=lambda name: _slot_index_from_key(name),
            logger_provider=lambda: logger,
        )

    def _broadcast_chat_message(self, slot_key: str, msg: dict) -> None:
        """Push a chat message to all SSE clients via the global stream."""
        role = msg.get("role", "")
        content = msg.get("content", "")
        # Mirror the display-time redaction gate _prepare_messages applies on
        # the HTTP history path, so a row's *content* leaves the backend in one
        # byte form regardless of which consumer receives it. Scope: content
        # only — `cls` / `meta` and the live `chat_chunk` stream are
        # deliberately not covered (see the direct_meta comment below). Gate is
        # `!= "user"` for the same reason as there: every non-user role can
        # carry model/tool output, and user-authored content stays raw (the
        # user typed it and is the only one who sees it back).
        if role != "user" and isinstance(content, str) and content:
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        payload: dict[str, Any] = {
            "_type": "chat_message",
            "slot": slot_key,
            "role": role,
            "content": content,
            "ts": msg.get("ts", ""),
        }
        # Include cls for backward compatibility
        cls_val = msg.get("cls", "")
        if cls_val:
            payload["cls"] = cls_val
            # Parse cls as JSON to send structured meta field for new frontend
            meta = parse_cls_meta(cls_val)
            if meta is not None:
                payload["meta"] = meta
        # Also include direct meta (e.g. tool_call_id on tool messages).
        #
        # Deliberately NOT redacted here, unlike the `cls` branch above (which is
        # sanitised by parse_cls_meta). Two reasons, both load-bearing:
        #
        # 1. This is the LIVE oauth banner's egress path. _emit_mcp_oauth_request
        #    appends the banner with a real `oauth_url`, already gated by
        #    security.oauth_url_contains_credential — the shared security gate, which
        #    exempts standard high-entropy OAuth values only at exact code-owned
        #    authorization endpoints while scanning everything else fail-closed.
        #    Running _redact_meta_for_role here would blank a genuine
        #    Google/GitHub consent URL and break the user's ability to authorize
        #    an MCP server.
        # 2. chat_utils imports from this module, so importing the redactors the
        #    other way would be a cycle.
        #
        # What makes that safe: live tool meta is redacted at source (_tool_meta),
        # and a DISK-LOADED message reaches this path only when the caller opts
        # in per-role. Both restore loops pass broadcast=False, and the ONE
        # exception is refresh_channel_window, which replays a channel
        # transcript's tail and passes broadcast_user=True so a message typed in
        # Slack renders at all (nothing rendered it optimistically here). That
        # exception cannot carry unredacted meta: ConversationLog.append writes
        # only role/content/ts/source_thread/source_user for such a row -- no
        # meta dict -- so the arm below never fires for it, and the row's
        # content is human-typed, which is deliberately raw at every other
        # boundary too. The invariant is pinned by
        # test_rehydrate_does_not_broadcast_replayed_messages and
        # test_restore_recent_sessions_does_not_broadcast_either. Do not relax
        # it further without re-checking that meta is still absent.
        direct_meta = msg.get("meta")
        if direct_meta and isinstance(direct_meta, dict):
            payload["meta"] = {**(payload.get("meta") or {}), **direct_meta}
        self._broadcast(payload)

    # ── Folder persistence ──

    _FOLDERS_FILE = FOLDERS_FILE
    _TAGS_FILE = "tags.json"
    _TAG_BOARDS_FILE = "tag_boards.json"

    # Seed vocabulary created on first run when tags.json is missing or empty.
    # status=True tags are mutually-exclusive workflow states. Drag-between-columns
    # strips all status tags from a card and applies the destination column's
    # status tag. Non-status tags survive the drag.
    _DEFAULT_TAGS: list[dict[str, Any]] = [
        {"id": "planned", "name": "Planned", "color": "#6b7280", "order": 0, "status": True},
        {"id": "todo", "name": "ToDo", "color": "#3b82f6", "order": 1, "status": True},
        {
            "id": "implementation",
            "name": "Implementation",
            "color": "#8b5cf6",
            "order": 2,
            "status": True,
        },
        {"id": "review", "name": "Review", "color": "#f59e0b", "order": 3, "status": True},
        {"id": "done", "name": "Done", "color": "#10b981", "order": 4, "status": True},
    ]

    def load_folders(self) -> None:
        """Load usable folder definitions without replacing good state on failure."""
        path = config_dir() / self._FOLDERS_FILE
        self._folders = _FOLDER_REPOSITORY.load(path, self._folders)

    def save_folders(self) -> None:
        """Persist folder definitions for synchronous boot-time callers."""
        path = config_dir() / self._FOLDERS_FILE
        _FOLDER_REPOSITORY.save(path, self._folders, self._atomic_write_json)

    _CRON_FOLDERS_FILE = "cron_folders.json"

    def load_cron_folders(self) -> None:
        """Load cron folder definitions from disk.

        Validates the loaded shape: the file must contain a JSON array of
        folder objects. A non-list root (a hand-edited ``{}``, a string) is
        ignored wholesale — it would crash frontend grouping
        (``folders.map is not a function``). Individual malformed entries are
        dropped from the active list but kept verbatim in
        ``_unparsed_cron_folder_entries`` so the next ``save_cron_folders``
        round-trips them back to disk rather than silently erasing a user's
        hand-edited-but-typo'd folder (mirrors the hooks store's contract).
        """
        path = config_dir() / self._CRON_FOLDERS_FILE
        try:
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, list):
                    logger.warning(
                        "Ignoring %s: expected a JSON array, got %s",
                        self._CRON_FOLDERS_FILE,
                        type(loaded).__name__,
                    )
                    return

                def _is_valid(f: Any) -> bool:
                    return (
                        isinstance(f, dict)
                        and isinstance(f.get("id"), str)
                        and bool(f.get("id"))
                        and isinstance(f.get("name"), str)
                        and bool(f.get("name"))
                        and isinstance(f.get("order"), (int, float))
                        and not isinstance(f.get("order"), bool)
                    )

                valid, unparsed = self._partition_preserving(
                    loaded, _is_valid, "entr(ies)", self._CRON_FOLDERS_FILE
                )
                self._cron_folders = valid
                self._unparsed_cron_folder_entries = unparsed
        except Exception:
            logger.warning("Failed to load cron folders", exc_info=True)

    def _persist_cron_folders(self, folders: list[dict[str, Any]]) -> None:
        """Atomically write ``folders`` to the cron-folders file.

        Takes the list to persist explicitly so a caller can save a candidate
        list before committing it to ``_cron_folders`` (see ``create_cron_folder``),
        keeping in-memory state and disk from diverging mid-operation. This is the
        single persist chokepoint for all folder mutations: ``create_cron_folder``
        passes a candidate list here before committing it, while ``save_cron_folders``
        (used by ``rename_cron_folder`` and ``delete_cron_folder``) passes the
        already-mutated ``_cron_folders`` to commit an in-place edit. Any
        malformed entries preserved at load time (``_unparsed_cron_folder_entries``)
        are appended, so a save cannot erase bytes a hand-edit left in a shape
        the loader could not validate.
        """
        path = config_dir() / self._CRON_FOLDERS_FILE
        unparsed = getattr(self, "_unparsed_cron_folder_entries", [])
        self._atomic_write_json_strict(path, [*folders, *unparsed])

    def save_cron_folders(self) -> None:
        """Persist cron folder definitions to disk (atomic write).

        Writes the active folders plus any malformed entries preserved at load
        time (``_unparsed_cron_folder_entries``), so a save triggered by an
        unrelated folder operation cannot erase bytes a hand-edit left in a
        shape this loader could not validate. Raises on I/O failure so callers
        can surface a 500 to the client rather than silently losing the write.
        """
        self._persist_cron_folders(self._cron_folders)

    def create_cron_folder(self, name: str, folder_id: str) -> dict:
        """Create a new cron folder and persist.

        Returns the created folder dict. Raises ValueError if ``folder_id``
        collides with an existing folder (``rename``/``delete`` act on the
        first id match, so a duplicate would strand the shadowed folder as
        un-renameable/un-deletable). Raises on persistence failure (callers
        should surface a 500).

        The folder is persisted BEFORE it is exposed in ``_cron_folders``: a
        concurrent ``GET /api/cron-folders`` reads the live list, so appending
        first and saving second would let a reader observe (and the frontend
        render) a folder that a failed save then removes — a transient "ghost"
        folder inconsistent with disk. Building the candidate list, persisting
        it, and only then committing the reference means a reader sees either
        the pre-create list or the durably-saved one, never an intermediate.
        """
        if any(f["id"] == folder_id for f in self._cron_folders):
            raise ValueError("cron folder id collision")
        order = max((f["order"] for f in self._cron_folders), default=-1) + 1
        folder = {"id": folder_id, "name": name, "order": order}
        candidate = [*self._cron_folders, folder]
        self._persist_cron_folders(candidate)
        self._cron_folders = candidate
        return folder

    def rename_cron_folder(self, folder_id: str, name: str) -> dict | None:
        """Rename a cron folder and persist.

        Returns the updated folder dict, or None if folder_id not found.
        Raises on persistence failure (callers should surface a 500);
        original name is restored on failure.
        """
        for folder in self._cron_folders:
            if folder["id"] == folder_id:
                old_name = folder["name"]
                folder["name"] = name
                try:
                    self.save_cron_folders()
                except Exception:
                    folder["name"] = old_name
                    raise
                return folder
        return None

    def delete_cron_folder(self, folder_id: str) -> bool:
        """Remove a cron folder and clear its assignment on all jobs.

        Returns True if the folder existed, False otherwise.
        Raises on persistence failure (callers should surface a 500).

        Ordering: the folder removal is the single authoritative write —
        it is removed from memory and persisted FIRST (rolled back in
        memory if the save fails, keeping memory consistent with disk).
        Job ``folder_id`` clears happen afterwards as best-effort cleanup:
        a dangling ``folder_id`` is benign (grouping renders unknown ids
        in the Ungrouped bucket, and a job's next folder move overwrites
        it), so a crash or per-job failure between writes can never strand
        jobs in a half-deleted state — the folder is either fully present
        or fully gone.
        """
        if not any(f["id"] == folder_id for f in self._cron_folders):
            return False
        # Remove the folder definition and persist — the one write that
        # decides whether the delete happened.
        snapshot = list(self._cron_folders)
        self._cron_folders = [f for f in self._cron_folders if f["id"] != folder_id]
        try:
            self.save_cron_folders()
        except Exception:
            self._cron_folders = snapshot
            raise
        # Best-effort: clear the now-dangling folder_id on affected jobs.
        # Failures are logged and tolerated — consumers treat an unknown
        # folder_id as ungrouped, so a leftover id has no user-visible
        # effect and self-heals on the job's next folder assignment.
        for job in self.crons.list_jobs(include_disabled=True):
            if job.folder_id == folder_id:
                try:
                    self.crons.update_job(job.id, folder_id="")
                except Exception:
                    logger.warning(
                        "Failed to clear folder_id on job %s after folder delete "
                        "(benign: unknown ids render as ungrouped)",
                        job.id,
                        exc_info=True,
                    )
        return True

    # ── Chat message pin persistence ──

    _CHAT_PINS_FILE = "chat_pins.json"

    def load_chat_pins(self) -> None:
        """Load pinned chat messages from disk, dropping malformed records.

        Legacy pins (pre-mid era) may lack the ``mid`` field — they are preserved
        for backward compatibility; new pins always carry ``mid``.

        Individual malformed entries are dropped from the active list but kept
        verbatim in ``_unparsed_chat_pin_entries`` so the next ``save_chat_pins``
        round-trips them back to disk rather than silently erasing a user's
        hand-edited-but-typo'd pin (mirrors the cron-folder contract, #5792).

        Error classification:
        - Missing file: normal (first run) → empty list.
        - Malformed JSON / invalid shape: tolerated for compatibility → empty list.
        - Transient I/O errors (PermissionError, OSError): MUST NOT replace
          valid in-memory state — re-raise so callers know load failed.
        """
        path = config_dir() / self._CHAT_PINS_FILE
        try:
            if not path.exists():
                self._chat_pins = []
                self._unparsed_chat_pin_entries = []
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            # Malformed content — treat as empty (data corruption).
            logger.warning("chat_pins.json has malformed content: %s", exc)
            self._chat_pins = []
            # Whole-file parse failure: nothing was parsed, so there are no
            # per-entry bytes to preserve. Do NOT clear a prior load's
            # preserved entries against an unreadable read.
            return
        except OSError:
            # Transient I/O error — do NOT clobber valid in-memory state.
            logger.warning("Transient I/O error reading chat_pins.json", exc_info=True)
            raise

        if not isinstance(raw, list):
            logger.warning("chat_pins.json is not a list (%s); ignoring", type(raw).__name__)
            self._chat_pins = []
            return

        # Reuse the create-time ingress caps (source of truth: chat_pins.py) so a
        # hand-edited chat_pins.json cannot smuggle an over-long mid/message_ts/
        # preview past a loader that only checked type + non-emptiness. Imported
        # here (not at module top) because chat_pins.py imports this module.
        from kiro_crew.dashboard.chat_pins import (
            _MAX_MESSAGE_TS_CHARS,
            _MAX_MID_CHARS,
            _MAX_PREVIEW_INPUT_CHARS,
        )

        def _is_valid(pin: Any) -> bool:
            return (
                isinstance(pin, dict)
                and all(
                    isinstance(pin.get(key), str) and pin.get(key) for key in ("id", "slot_key")
                )
                # Require at least one identity field: mid or message_ts
                and bool(
                    (isinstance(pin.get("mid"), str) and pin.get("mid"))
                    or (isinstance(pin.get("message_ts"), str) and pin.get("message_ts"))
                )
                and isinstance(pin.get("preview"), str)
                and isinstance(pin.get("pinned_at"), str)
                and bool(pin.get("pinned_at"))
                # Enforce the create-time length caps at load, so an over-long
                # record is partitioned into _unparsed rather than served. Guard
                # each optional identity with isinstance before len(): a
                # hand-edited record can carry a non-string mid/message_ts (e.g.
                # a JSON number) while still being valid via the other identity,
                # and len() on a non-string would crash the whole loader. preview
                # is already isinstance-checked as a str above.
                and (not isinstance(pin.get("mid"), str) or len(pin["mid"]) <= _MAX_MID_CHARS)
                and (
                    not isinstance(pin.get("message_ts"), str)
                    or len(pin["message_ts"]) <= _MAX_MESSAGE_TS_CHARS
                )
                and len(pin.get("preview") or "") <= _MAX_PREVIEW_INPUT_CHARS
            )

        valid, unparsed = self._partition_preserving(
            raw, _is_valid, "chat pin record(s)", self._CHAT_PINS_FILE
        )
        self._chat_pins = valid
        self._unparsed_chat_pin_entries = unparsed

    def save_chat_pins(self) -> None:
        """Persist pinned chat messages with an atomic, owner-only file replacement.

        Writes the active pins plus any malformed entries preserved at load
        time (``_unparsed_chat_pin_entries``), so a save triggered by an
        unrelated pin operation cannot erase bytes a hand-edit left in a shape
        this loader could not validate.
        """
        path = config_dir() / self._CHAT_PINS_FILE
        unparsed = getattr(self, "_unparsed_chat_pin_entries", [])
        atomic_write(
            path,
            json.dumps([*self._chat_pins, *unparsed]),
            fsync=True,
            mode=0o600,
        )

    async def remove_chat_pins_for_slots(self, slot_keys: set[str]) -> int:
        """Remove pins when their persisted history sessions are permanently deleted."""
        keys = {key for key in slot_keys if key}
        if not keys:
            return 0
        async with self._chat_pins_lock:
            previous = self._chat_pins
            remaining = [pin for pin in previous if pin.get("slot_key") not in keys]
            removed = len(previous) - len(remaining)
            if not removed:
                return 0
            self._chat_pins = remaining
            try:
                await asyncio.to_thread(self.save_chat_pins)
            except Exception:
                self._chat_pins = previous
                raise
            return removed

    def folders_generation(self) -> int:
        """Monotonic counter identifying the current folder-tree snapshot.

        Rides the ``slots`` WS frame so an already-open tab can tell that the
        folder store actually CHANGED and invalidate its cached
        ``['chat-folders']`` query. Without it a folder created by anything other
        than this tab's own mutation — an agent, a second tab, another device —
        stayed invisible until a reload: the query carries the app-wide
        ``staleTime: Infinity``, so nothing expires it, and the tree the frame
        already carries cannot be written into the cache directly (that would
        clobber an in-flight optimistic edit and, lacking ``history_count``, mark
        the query fresh so the real GET never runs). A generation is the small
        signal that lets the client re-GET instead of guess.

        Read through ``getattr`` because this is reachable from the slots-push
        hot path, which also runs on a ``__new__``-built state that never ran
        ``__init__`` (several endpoint suites build their fixture that way).
        """
        return int(getattr(self, "_folders_generation", 0) or 0)

    async def mutate_folders(self, mutate: Callable[[list[dict[str, Any]]], tuple[bool, _T]]) -> _T:
        """Serialize a folder mutation and confirm its off-loop persistence."""

        def _mark_committed() -> None:
            # This runs under the repository lock and only after the write is
            # confirmed.  A failed/no-op transaction must not make clients
            # re-fetch a tree that never changed, and concurrent commits must
            # not collapse two monotonic generation bumps into one.
            self._folders_generation = self.folders_generation() + 1

        return await _FOLDER_REPOSITORY.mutate(
            lambda: self._folders,
            self._folders_lock,
            mutate,
            lambda: config_dir() / self._FOLDERS_FILE,
            self._write_folders_confirmed,
            _mark_committed,
        )

    async def read_folders(self, read: Callable[[list[dict[str, Any]]], _T]) -> _T:
        """Run a synchronous reader against committed folder state."""
        return await _FOLDER_REPOSITORY.read(lambda: self._folders, self._folders_lock, read)

    def _write_folders_confirmed(self, path: Path, snapshot: list[dict[str, Any]]) -> None:
        """Persist the complete folder value and prove it landed."""
        _FOLDER_REPOSITORY.write_confirmed(path, snapshot, self._atomic_write_json)

    def folder_breadcrumb(self, folder_id: str, sep: str = " › ") -> str:
        """Render a cycle-safe root-to-leaf folder breadcrumb."""
        return _FOLDER_REPOSITORY.breadcrumb(self._folders, folder_id, sep)

    def load_tags(self) -> None:
        """Load tag vocabulary and sidebar columns from disk; seed defaults if missing.

        Only seed when ``tags.json`` does not exist. An explicitly-empty file
        is left as-is (so a user who deletes every tag stays at zero tags
        across restarts), and a parse failure is left untouched (so a
        transient I/O error never silently overwrites saved data).
        """
        tags_path = config_dir() / self._TAGS_FILE
        file_existed = tags_path.exists()
        try:
            vocab_ok = True
            if file_existed:
                raw = json.loads(tags_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    # Keep rows the active list dropped verbatim so the seed/
                    # back-fill save below (and every later save) round-trips
                    # them back instead of erasing a hand-edited-but-typo'd row
                    # at boot with no user action (#5792).
                    self._tags, unparsed = self._partition_preserving(
                        raw,
                        lambda t: isinstance(t, dict) and bool(t.get("id")),
                        "tag entr(ies)",
                        self._TAGS_FILE,
                    )
                    self._unparsed_tag_entries = unparsed
                else:
                    # Valid JSON but not a list (e.g. {}): the vocabulary
                    # state is UNKNOWN, same as a parse failure — do not let
                    # restore-time pruning wipe assignments against it.
                    vocab_ok = False
                    logger.warning("tags.json is not a list; treating vocabulary as unknown")
            # Authoritative only when the file is missing (fresh install,
            # seeded below) or parsed as a list — INCLUDING a legitimately-
            # empty [] — so restore-time pruning of dangling ids is safe.
            self._tags_authoritative = vocab_ok
        except Exception:
            logger.warning("Failed to load tags", exc_info=True)
            # Treat a parse error like a present file: do not re-seed.
            file_existed = True
            # Vocabulary state unknown — restore-time pruning must fail open.
            self._tags_authoritative = False
        # Back-fill the status flag for legacy tags saved before the field existed.
        # The 5 seed ids are canonical status tags; everything else defaults to False.
        seed_ids = {t["id"] for t in self._DEFAULT_TAGS}
        mutated = False
        for t in self._tags:
            if "status" not in t:
                t["status"] = t.get("id") in seed_ids
                mutated = True
        if not file_existed and not self._tags:
            # Fresh install (no tags.json on disk) — seed the default vocabulary.
            self._tags = [dict(t) for t in self._DEFAULT_TAGS]
            mutated = True
        if mutated:
            self.save_tags()

        # Column layout: flat list of {id, name, tag_ids, mode, order}.
        # Empty list = single implicit "all sessions" column (legacy UX).
        columns_path = config_dir() / self._TAG_BOARDS_FILE
        try:
            if columns_path.exists():
                raw = json.loads(columns_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    # Preserve dropped columns verbatim so a later save
                    # round-trips them rather than erasing a hand-edited-but-
                    # typo'd column (#5792).
                    self._tag_boards, unparsed_cols = self._partition_preserving(
                        raw,
                        lambda c: isinstance(c, dict) and bool(c.get("id")),
                        "sidebar column(s)",
                        self._TAG_BOARDS_FILE,
                    )
                    self._unparsed_tag_board_entries = unparsed_cols
                    # Prune column tag_ids missing from the vocabulary: tag
                    # deletion commits the vocab write first (crash-atomic),
                    # so a crash mid-delete can leave dangling ids here. The
                    # column API rejects unknown ids, so a dangling id left
                    # in place would make that column's filter permanently
                    # un-editable (the popover echoes the full list back).
                    # Same fail-open rule as the slot-restore prune: only
                    # prune when the vocabulary is authoritative.
                    if self._tags_authoritative:
                        known = {t.get("id") for t in self._tags}
                        for col in self._tag_boards:
                            tag_ids = col.get("tag_ids")
                            if isinstance(tag_ids, list):
                                col["tag_ids"] = [t for t in tag_ids if t in known]
        except Exception:
            logger.warning("Failed to load sidebar columns", exc_info=True)

    def save_tags(self) -> None:
        """Persist tag vocabulary to disk (atomic write).

        Appends any malformed entries preserved at load time
        (``_unparsed_tag_entries``) so a save — including the seed/back-fill
        save that runs during ``load_tags`` itself — cannot erase bytes a
        hand-edit left in a shape the loader could not validate (#5792).
        """
        unparsed = getattr(self, "_unparsed_tag_entries", [])
        self._atomic_write_json(config_dir() / self._TAGS_FILE, [*self._tags, *unparsed])

    def save_tags_snapshot(self, snapshot: list[dict]) -> None:
        """Persist a pre-captured tag snapshot to disk (strict -- raises on failure).

        Used by the serialized tag-write path in chat_tags.py: the snapshot is
        captured on the event loop under the tags write lock, then this write
        runs in a worker thread. Lives here (not in chat_tags.py) so the file
        location resolves through this module's ``config_dir`` exactly like
        ``save_tags`` -- keeping tests that patch it working unchanged.

        Malformed entries preserved at load time (``_unparsed_tag_entries``)
        are appended so this write path preserves them too (#5792).

        Raises on I/O failure so callers can roll back in-memory state and
        surface HTTP 5xx rather than silently losing data.
        """
        unparsed = getattr(self, "_unparsed_tag_entries", [])
        self._atomic_write_json_strict(config_dir() / self._TAGS_FILE, [*snapshot, *unparsed])

    def save_tag_boards(self) -> None:
        """Persist sidebar column layout to disk (atomic write).

        Appends any malformed columns preserved at load time
        (``_unparsed_tag_board_entries``) so a save cannot erase bytes a
        hand-edit left in a shape the loader could not validate (#5792).
        """
        unparsed = getattr(self, "_unparsed_tag_board_entries", [])
        self._atomic_write_json(
            config_dir() / self._TAG_BOARDS_FILE,
            [*self._tag_boards, *unparsed],
        )

    def save_tag_boards_snapshot(self, snapshot: list[dict]) -> None:
        """Persist a pre-captured boards snapshot to disk (strict -- raises on failure).

        Used by the tag-delete path in chat_tags.py: the snapshot is captured
        on the event loop under the tags write lock, then this write runs in a
        worker thread. Lives here (not in chat_tags.py) so the file location
        resolves through this module's ``config_dir`` exactly like
        ``save_tag_boards`` -- keeping tests that patch it working unchanged.

        Malformed columns preserved at load time
        (``_unparsed_tag_board_entries``) are appended so this write path
        preserves them too (#5792).

        Raises on I/O failure so callers can roll back in-memory state and
        surface HTTP 5xx rather than silently losing data.
        """
        unparsed = getattr(self, "_unparsed_tag_board_entries", [])
        self._atomic_write_json_strict(config_dir() / self._TAG_BOARDS_FILE, [*snapshot, *unparsed])

    @staticmethod
    def _partition_preserving(
        raw: list[Any],
        predicate: Callable[[Any], bool],
        noun: str,
        source_file: str,
    ) -> tuple[list[Any], list[Any]]:
        """Split ``raw`` into ``(active, unparsed)`` by ``predicate``.

        The shared "partition malformed rows at load, keep them verbatim so a
        later save round-trips them back" mechanic behind ``load_cron_folders``,
        ``load_chat_pins``, ``load_tags`` and the tag-board load (all #5792).
        A row is active when ``predicate`` returns truthy; every other row is
        collected into ``unparsed`` so the caller's save path can re-append it
        (``[*active, *unparsed]``) at write time rather than silently erasing
        bytes a hand-edit left in a shape the loader could not validate.

        When any row is preserved, logs the shared preserving-N warning at
        WARNING level. ``noun`` supplies the per-store wording (``"entr(ies)"``,
        ``"chat pin record(s)"``, ``"tag entr(ies)"``, ``"sidebar column(s)"``)
        and ``source_file`` names the file, so the emitted messages stay
        identical to the hand-rolled copies this replaced.
        """
        active: list[Any] = []
        unparsed: list[Any] = []
        for row in raw:
            (active if predicate(row) else unparsed).append(row)
        if unparsed:
            logger.warning(
                "Preserving %d malformed %s while loading %s " "(kept verbatim, not active)",
                len(unparsed),
                noun,
                source_file,
            )
        return active, unparsed

    @staticmethod
    def _atomic_write_json_strict(path: Path, data: Any) -> None:
        """Atomic JSON write that RAISES on failure (no swallowing).

        Used by persistence helpers where the caller needs to know about
        write failures (e.g. to return HTTP 500).

        Delegates to :func:`atomic_write`, which re-raises after cleaning up
        its temp file, so the no-swallowing contract above is unchanged. It
        also carries the Windows ``os.replace`` sharing-violation retry that
        this hand-rolled copy lacked.

        Content stays ``bytes`` rather than ``str`` on purpose: text mode
        applies universal-newline translation, which would rewrite any ``\n``
        inside the JSON on Windows.

        ``mode=0o600`` is explicit because the hand-rolled version created its
        temp with ``tempfile.mkstemp`` and never widened it, so folders.json,
        tags.json, tag_boards.json and cron_folders.json all publish at 0o600
        today. The helper otherwise falls back to the umask default, normally
        0o644, which would widen all four.
        """
        atomic_write(path, json.dumps(data).encode(), fsync=True, mode=0o600)

    @staticmethod
    def _atomic_write_json(path: Path, data: Any) -> None:
        """Atomic JSON write used by folder/tag persistence helpers.

        Delegates to _atomic_write_json_strict but swallows errors (logs a
        warning instead of raising).
        """
        try:
            DashboardState._atomic_write_json_strict(path, data)
        except Exception:
            logger.warning("Failed to write %s", path.name, exc_info=True)

    def source_link_urls(self) -> list[str]:
        """URLs of the sidebar-visible PR/MR chips across all slots.

        Only the links each slot actually serializes (the first
        ``_SERIALIZED_SOURCE_LINKS_PER_SLOT``) are returned — these are the
        chips whose check status the periodic owner-WS refresh keeps fresh.
        Reads the per-slot revision cache, so this is cheap to call on a timer.

        Issue links are excluded: the check-status path reaches ``gh pr view``
        and has no meaning for an issue.

        Returns nothing while the chips are switched off. This is the point of
        gating here rather than only in the payload: the periodic refresh this
        feeds spawns a credentialed provider subprocess per round, and a user who
        turned the chips off should stop paying for status nobody renders.
        """
        from kiro_crew.dashboard.handlers.source_providers import (
            session_card_source_links_enabled,
        )

        if not session_card_source_links_enabled():
            return []
        urls: list[str] = []
        for s in self._slots.values():
            urls.extend(
                link["url"]
                for link in _budgeted_source_links(s._pr_source_links())
                if link.get("kind", "change") == "change"
            )
        return urls

    def source_link_urls_for_slot(self, key: str) -> list[str]:
        """Sidebar-visible PR/MR chip URLs for one slot (same cap and kind filter).

        Empty while the chips are switched off, for the same reason
        :meth:`source_link_urls` is: this feeds the turn-boundary status refresh.
        """
        from kiro_crew.dashboard.handlers.source_providers import (
            session_card_source_links_enabled,
        )

        if not session_card_source_links_enabled():
            return []
        slot = self._slots.get(key)
        if slot is None:
            return []
        return [
            link["url"]
            for link in _budgeted_source_links(slot._pr_source_links())
            if link.get("kind", "change") == "change"
        ]

    def push_source_status(self, delta: dict) -> None:
        """Push a single PR/MR status delta to owner websockets only.

        Chip status is credential-backed provider data, so this never reaches
        non-owner or app-token clients. Fire-and-forget: the panel's own poll
        remains the safety net if a client misses the event.
        """
        if not self._owner_ws_clients:
            return
        self._send_ws_owners(json.dumps({"type": "source_status", "data": delta}))

    def refresh_slot_source_status(self, key: str) -> None:
        """Re-read this slot's PR/MR status now — called at agent turn boundaries.

        A turn that just ran ``gh pr create``, pushed a revision, or drove a
        review round is exactly when a PR's lifecycle moved, and nothing else in
        the system invalidates the status caches on that event: the chips would
        wait out the periodic rotation and the detail panel would not refetch at
        all. Fires ONLY when an OWNER window is open: the read runs the
        operator's credentials, so a non-owner window must not drive it (a
        non-owner renders the owner-populated caches read-only). With no owner
        window open there is nobody entitled to a credentialed read, so no
        provider subprocess is spawned. Rate-floored inside
        ``request_check_refresh_now``.
        """
        # Both the status read and the visibility revalidation below run the
        # operator's `gh`/`glab` credentials, so this turn-boundary refresh runs
        # ONLY when an OWNER window is open. A non-owner dashboard window never
        # drives it (GPT #6789): the owner's own connection keeps the caches warm
        # and non-owner windows render the result read-only via the fail-closed
        # is_repo_public gate. With no owner window open there is nobody entitled
        # to drive a credentialed read, so this is a no-op.
        if not self._owner_ws_clients:
            return
        try:
            urls = self.source_link_urls_for_slot(key)
            if not urls:
                return
            from kiro_crew.dashboard.handlers.source_providers import (
                request_check_refresh_now,
                schedule_visibility_refresh,
            )

            request_check_refresh_now(urls, self.push_slots_update)
            # force=True: the status read above bypasses the TTL, so visibility
            # MUST be revalidated in lockstep — otherwise fresh (now-private)
            # status could be projected against a still-cached-public visibility
            # entry, leaking private status to a non-owner (GPT #6789). Runs
            # AFTER the status schedule: both are fire-and-forget schedulers, and
            # ordering the status call first preserves the turn-boundary contract
            # while the downstream render gate (`_project_source_links` ->
            # is_repo_public) is what actually withholds status until visibility
            # reconfirms.
            schedule_visibility_refresh(urls, self.push_slots_update, force=True)
        except Exception:
            logger.debug("turn-boundary source status refresh failed", exc_info=True)

    def _channel_link_is_live(self, link: ChannelLink) -> bool:
        """Is a proactive-capable transport registered for this channel?

        Deliberately an IN-MEMORY check only. This runs per linked slot inside
        ``serialize_slots``, which sits on the ``push_slots_update`` websocket
        broadcast path, so it must not touch the filesystem: the full governed
        ladder (``chat_runner._resolve_channel_target``) calls
        ``governance_permits``, which walks the profile directory (``iterdir`` +
        ``stat``, with a possible reload) — a slow filesystem there would block
        the event loop on every push and can drive watchdog restarts.

        Governance stays enforced at the async SEND boundary (
        ``_resolve_mirror_target`` in the turn path and in the mirror-link
        reminder handler). A link may therefore read ``live: true`` here and
        still be refused at send time; that asymmetry is deliberate and safe —
        the menu affordance is optimistic, the side effect is gated.
        """
        if link.channel_type == SLACK_NAMESPACE or not link.channel_id:
            return False
        transport = self.get_channel_transport(link.channel_type)
        if transport is None:
            return False
        return bool(
            getattr(
                getattr(transport, "capabilities", None),
                "supports_proactive_send",
                False,
            )
        )

    def _slot_links(self, slot: _ChatSlot) -> tuple[list[dict[str, Any]], bool, str, str]:
        """Build the redacted channel-neutral link projection for one slot."""
        # circular import: chat imports state at module scope.
        from kiro_crew.dashboard.chat_utils import (
            effective_session_key,
            mirror_is_paused,
            slack_mirror_is_paused,
        )

        session_key = effective_session_key(slot)
        # Resolved once per slot rather than per row: all three are storage reads,
        # and a session holds at most one Slack thread, one born-in conversation
        # and one mirror binding.
        slack_paused = slack_mirror_is_paused(self, session_key)
        mirror_paused = mirror_is_paused(self, session_key)
        origin_paused = mirror_is_paused(self, session_key, origin=True)
        mirror: ChannelLink | None = None
        persisted_ts: str | None = None
        persisted_channel: str | None = None
        try:
            candidate = self.sessions.get_mirror_link(session_key)
            if isinstance(candidate, ChannelLink):
                mirror = candidate
        except Exception:
            pass
        try:
            raw_ts, raw_channel = self.sessions.get_slack_link(session_key)
            persisted_ts = raw_ts if isinstance(raw_ts, str) else None
            persisted_channel = raw_channel if isinstance(raw_channel, str) else None
        except Exception:
            pass

        # Prefer persisted values, but retain explicit in-memory Slack links in
        # tests and during the short interval before persistence is observable.
        slack_ts = persisted_ts or slot._slack_thread_ts
        slack_channel = persisted_channel or slot._slack_channel
        namespaced_origin = _split_namespaced_channel_id(persisted_channel)
        genuine_slack = _is_genuine_slack_link(slack_ts, slack_channel)
        # A Slack-BORN session's ``slack_thread_ts`` names the thread it LIVES
        # in, not a mirror target somewhere else: the Slack inbound handler
        # writes it every turn as the thread registry that routes replies back.
        # That makes it a self-reference, and the sidebar already draws an origin
        # glyph from the slot key -- so surfacing it as an outbound mirror badges
        # one conversation twice and offers a session its own origin thread as a
        # releasable mirror. A Slack-born session that genuinely mirrors to a
        # DIFFERENT thread still carries a different ts, so it is unaffected.
        slack_origin_self_link = (
            channel_namespace_of(session_key) == SLACK_NAMESPACE
            and bool(slack_ts)
            and session_key.endswith(slack_ts)
        )
        links: list[dict[str, Any]] = []

        def append_link(link: ChannelLink, direction: str) -> None:
            channel_type = (link.channel_type or "").lower()
            if not channel_type:
                return
            channel_id = link.channel_id or ""
            nested = _split_namespaced_channel_id(channel_id)
            if nested and nested[0] == channel_type:
                channel_id = nested[1]
            normalized = ChannelLink(channel_type, channel_id, link.thread_id)
            # Real on EVERY row, origin included: the conversation a session was
            # born in can be disconnected too, so it stops syndicating there and
            # the session carries on in the dashboard. `direction` still records
            # the provenance the sidebar mark needs; it no longer decides whether
            # the row has a control.
            #
            # Keyed to the row's SOURCE, not just its channel: a session born in
            # Discord that also mirrors to Telegram draws two non-Slack rows, and
            # while both read one value, muting either silently muted the other.
            if channel_type == SLACK_NAMESPACE:
                paused = slack_paused
            elif direction == "origin":
                paused = origin_paused
            else:
                paused = mirror_paused
            links.append(
                {
                    "channel": channel_type,
                    "label": _link_label(channel_type),
                    "target": _redacted_link_target(channel_id),
                    "direction": direction,
                    "live": self._channel_link_is_live(normalized),
                    "paused": paused,
                }
            )

        # Non-Slack transports currently leak their home conversation through
        # slack_channel_id. Surface that as a read-only origin, never a Slack
        # mirror. This prefix sniff is intentionally defensive for unknown
        # future channel types too.
        if namespaced_origin and namespaced_origin[0] != SLACK_NAMESPACE:
            append_link(
                ChannelLink(namespaced_origin[0], namespaced_origin[1]),
                "origin",
            )

        if mirror is not None:
            if mirror.channel_type == SLACK_NAMESPACE:
                # get_mirror_link synthesizes Slack for the legacy fields. If
                # those fields actually hold a namespaced non-Slack origin, the
                # origin above is the only truthful representation.
                if not namespaced_origin and genuine_slack and not slack_origin_self_link:
                    append_link(
                        ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts),
                        "out",
                    )
            else:
                # A resume binding (set by an in-channel `!sessions` pick) routes
                # BOTH ways: this session's replies go to that channel AND
                # messages from it are delivered back here. That is a materially
                # different thing for the user to see and release than an
                # outbound-only `!link` mirror, so it gets its own direction
                # rather than being flattened into "out". Slack is excluded by
                # the branch above — it carries inbound on its own thread index
                # and never sets the marker.
                inbound = False
                try:
                    inbound = bool(self.sessions.mirror_accepts_inbound(session_key))
                except Exception:
                    # Older/stubbed SessionManagers may not expose the accessor;
                    # degrade to the outbound reading rather than dropping the link.
                    inbound = False
                append_link(mirror, "both" if inbound else "out")
        elif genuine_slack and not slack_origin_self_link:
            # Defensive fallback for SessionManager test doubles or older
            # implementations that expose get_slack_link but not get_mirror_link.
            append_link(
                ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts),
                "out",
            )

        if genuine_slack and slack_origin_self_link:
            # The conversation a session was BORN in gets a row too. Suppressing
            # it was the last place a channel appeared with no control at all:
            # you can stop a Slack-born session syndicating to its thread and
            # carry on in the dashboard, and a human reply in that thread brings
            # it back. It stays `origin` so the sidebar keeps showing where the
            # conversation came from — provenance is history and survives a
            # disconnect; only the delivery indicator reflects the mute.
            append_link(ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts), "origin")

        if genuine_slack and not slack_origin_self_link:
            slack_namespace = _split_namespaced_channel_id(slack_channel)
            visible_slack_channel = slack_namespace[1] if slack_namespace else (slack_channel or "")
            # A Slack ROW accompanies `slack_linked=True` unconditionally. The
            # dashboard's channel control is built from `links` alone — it no
            # longer synthesizes a Slack row from this boolean, because a
            # synthesized row cannot know `paused` and so rendered a muted thread
            # as connected. That makes a True here with no row worse than a
            # cosmetic gap: the session IS linked and the menu would offer to
            # connect it. Guaranteed here rather than left to hold incidentally
            # across the branches above.
            if not any(
                row["channel"] == SLACK_NAMESPACE and row["direction"] != "origin" for row in links
            ):
                append_link(ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts), "out")
            return links, True, visible_slack_channel, slack_ts or ""
        return links, False, "", ""

    def serialize_slot(
        self,
        slot: _ChatSlot,
        *,
        include_check_status: bool = False,
        dashboard_user: bool = False,
    ) -> dict[str, Any]:
        """Serialize one slot with state-backed channel-link metadata."""
        payload = slot.to_dict(
            include_check_status=include_check_status, dashboard_user=dashboard_user
        )
        links, slack_linked, slack_channel, slack_thread_ts = self._slot_links(slot)
        payload.update(
            {
                "links": links,
                "slack_linked": slack_linked,
                "slack_channel": slack_channel,
                "slack_thread_ts": slack_thread_ts,
            }
        )
        return payload

    def serialize_slots(
        self, *, include_check_status: bool = False, dashboard_user: bool = False
    ) -> list:
        """Serialize slots, optionally including owner-only provider status.

        ``subagents_running`` remains available to every authenticated caller.
        Credential-backed ``ci`` and ``state`` fields are omitted unless an
        authenticated owner boundary explicitly opts in — EXCEPT a link whose
        repository is known public, which any authenticated dashboard user
        (``dashboard_user=True``) may see because that lifecycle is already
        world-visible. Private/unknown repos and app tokens stay owner-only.
        """
        out = []
        subs = getattr(self, "subagents", None)
        for s in self._slots.values():
            self._drop_orphaned_mcp_report(s)
            d = self.serialize_slot(
                s,
                include_check_status=include_check_status,
                dashboard_user=dashboard_user,
            )
            d["subagents_running"] = bool(subs and subs.running_agents_for(f"dashboard:{s.key}"))
            out.append(d)
        return out

    def _drop_orphaned_mcp_report(self, slot: "_ChatSlot") -> None:
        """Drop a slot's MCP report unless it describes the slot's CURRENT session.

        A report describes exactly ONE session. Clearing it at each teardown was
        the wrong shape — review found path after path that skipped it (the reset
        funnel, the reload and reset-conversation routes, the queued discard, a
        channel handler, the cron reaper, the task runner, a project change) — and
        a LIVENESS check ("does a session exist?") still missed the last of them,
        because a reset RECREATES a session under the same key: the slot looked
        alive while the report described the session that had gone.

        So the question asked here is identity, not liveness — is the live session
        the one this payload was taken under? A mismatch is dropped by
        construction, which closes every one of those paths at once, including any
        future one, and demotes the remaining ``clear_mcp_report()`` calls to a
        courtesy that pushes the delta early rather than the thing correctness
        rests on.

        Validating HERE cannot be bypassed: this is the single projector both
        ``/api/chat/slots`` and the WebSocket snapshot go through.

        The key derivation mirrors ``chat_utils.effective_session_key`` — a
        channel-born slot's turns run on the channel's own session — and is
        inlined because ``chat_utils`` imports this module. One refinement over
        that mirror: an in-flight turn's OWN key wins over the slot's routing.
        The two diverge when the routing is reassigned on a live slot — a cron
        injection binds ``linked_session_key`` to ``cron:<id>`` with no
        ``running`` gate — and the report describes the session the turn is
        actually running on, not the one a FUTURE turn would route to.
        Resolving the routing there reads a different (or absent) provider,
        mismatches, and clears a live session's report mid-turn. Same rule as
        turn cancellation: address the turn, not the routing.
        """
        if slot.mcp_report_payload() is None:
            return
        session_key = (
            getattr(slot, "_active_turn_session_key", "")
            or getattr(slot, "linked_session_key", "")
            or f"dashboard:{slot.key}"
        )
        provider = self.sessions.get_provider(session_key)
        live_id = getattr(provider, "session_id", "") if provider is not None else ""
        if live_id != slot._mcp_report_session_id:
            slot.clear_mcp_report()

    @contextlib.contextmanager
    def suspend_slots_push(self) -> "Iterator[None]":
        """Coalesce every ``push_slots_update()`` inside the block into one at exit.

        ``get_or_create_slot`` broadcasts the FULL slot list on each call, so a bulk
        restore of N tabs serializes 1+2+…+N slots — O(N²) ``to_dict``/redaction
        work for intermediate states no client will ever render (measured ~1.3s at
        N=77, and it grows quadratically). Wrap the restore, emit one broadcast.

        Depth-counted so nested use is safe (an inner block must not flush early),
        and ``@contextmanager``'s try/finally unwinds the depth even if the body
        raises. Only flushes if something actually asked to push.
        """
        self._slots_push_suspend += 1
        try:
            yield
        finally:
            self._slots_push_suspend -= 1
            if self._slots_push_suspend == 0 and self._slots_push_pending:
                self._slots_push_pending = False
                self.push_slots_update()

    def push_slots_update(self) -> None:
        """Push slots, keeping provider status confined to owner websockets.

        Coalesces on a leading plus trailing edge: the first call after an idle
        period broadcasts immediately, further calls inside the window are
        absorbed, and one trailing broadcast carries the final state. A single
        chat turn fires several of these and each one re-serializes every slot,
        so an uncoalesced burst redraws the whole sidebar once per mutation for
        what the user sees as one change. The trailing flush re-serializes at
        delivery time, so a coalesced frame is never a stale frame.
        """
        if self._slots_push_suspend:
            # Inside suspend_slots_push(); remember that a push is owed and let the
            # outermost block emit a single coalesced broadcast on exit.
            self._slots_push_pending = True
            return

        now = time.monotonic()
        broadcast_now = False
        lock = self._slots_broadcast_lock
        if lock is None:
            # Partially-constructed state (built via __new__): no coalescing.
            self._do_slots_broadcast()
            return

        with lock:
            # Resolved once here, at the top of the lock, so the timer branch
            # below and any later cross-thread caller agree on one loop.
            serving = self.serving_loop

            elapsed = now - self._slots_broadcast_last
            if elapsed >= _SLOTS_BROADCAST_INTERVAL_S:
                self._slots_broadcast_last = now
                if self._slots_broadcast_timer is not None:
                    self._slots_broadcast_timer.cancel()
                    self._slots_broadcast_timer = None
                broadcast_now = True
            elif self._slots_broadcast_timer is None:
                # Scheduling onto the serving loop is preferred over broadcasting
                # from a foreign thread; a closed loop falls back to an immediate send.
                loop = serving
                remaining = _SLOTS_BROADCAST_INTERVAL_S - elapsed
                try:
                    if loop is None:
                        self._slots_broadcast_last = now
                        broadcast_now = True
                    elif loop is self._running_loop():
                        self._slots_broadcast_timer = loop.call_later(
                            remaining, self._trailing_slots_flush
                        )
                    else:
                        loop.call_soon_threadsafe(self._schedule_trailing_flush, remaining)
                except RuntimeError:
                    self._slots_broadcast_last = now
                    broadcast_now = True

        # serialize_slots() and _broadcast() are the expensive half; running them
        # outside the lock stops a cross-thread caller from blocking behind them.
        if broadcast_now:
            self._do_slots_broadcast()

    @staticmethod
    def _running_loop() -> asyncio.AbstractEventLoop | None:
        """Return the running loop, or None when called off the event loop."""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def bind_serving_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the loop this dashboard is served on, before any request runs.

        Called from an app startup hook: that is the earliest point the loop
        exists, so every later reader finds it already bound instead of racing to
        latch a copy from whichever thread happens to arrive first.
        """
        self._serving_loop = loop

    @property
    def serving_loop(self) -> "asyncio.AbstractEventLoop | None":
        """The loop to hand cross-thread work to, or None when it is unknowable.

        Prefers the loop bound at startup. When nothing bound one -- a
        ``__new__``-built state, a unit test, a process whose startup hook has not
        run -- it latches the running loop the first time it is read FROM that
        loop, so an off-loop caller arriving later still has a target. ``None``
        means this state has never seen a loop, and the caller owns the decision
        about what to do with the work rather than being handed a guess.
        """
        loop = self._serving_loop
        if loop is None:
            loop = self._running_loop()
            if loop is not None:
                self._serving_loop = loop
        return loop

    def _schedule_trailing_flush(self, delay: float) -> None:
        """Arm the trailing flush. Must run ON the event loop."""
        lock = self._slots_broadcast_lock
        if lock is None:
            return
        with lock:
            if self._slots_broadcast_timer is not None:
                return
            self._slots_broadcast_timer = asyncio.get_running_loop().call_later(
                delay, self._trailing_slots_flush
            )

    def _trailing_slots_flush(self) -> None:
        """Trailing-edge callback: broadcast whatever the state is now."""
        lock = self._slots_broadcast_lock
        if lock is not None:
            with lock:
                self._slots_broadcast_timer = None
                self._slots_broadcast_last = time.monotonic()
        self._do_slots_broadcast()

    def _do_slots_broadcast(self) -> None:
        """Serialize and broadcast the slot list. Bypasses coalescing."""
        from kiro_crew.dashboard.handlers.source_providers import (
            gitlab_hosts_generation,
        )
        from kiro_crew.platform.context import governance_generation

        yolo_active = self.is_yolo_active()  # expire first if needed
        # PUBLIC-repo chip status rides the general frame so any authenticated
        # dashboard user (SSE and WS both run on dashboard-user tokens) sees the
        # merged/closed/CI glyph for a repo whose lifecycle is already
        # world-visible. Private/unknown repos fall through to owner-only inside
        # serialize_slots, and app tokens are stripped of this status in
        # ``_serialize_for_client`` -> ``filter_slots_for_app`` before delivery,
        # so widening the general list here never leaks status to an app scope.
        # SSE carries the BARE list (see below); the WS dashboard-user frame
        # carries the enriched one. GPT #6789: the general ``_slots_list`` feeds
        # BOTH the SSE queue (which has NO per-app filtering) and the WS frame,
        # so enriching it here leaked public-repo status onto ``/api/stream`` for
        # any app token allowed that route. Keep the broadcast list bare and put
        # the public-repo enrichment only on the WS path, where
        # ``_serialize_for_client`` re-filters app tokens.
        slots_data = self.serialize_slots()
        slots_data_ws = self.serialize_slots(dashboard_user=True)
        mgr = getattr(self, "channel_manager", None)
        ch_trusted = bool(mgr and any(ch.trusted for ch in mgr._channels.values()))
        # Piggyback the allowlist generation so clients invalidate the cached
        # ['dashboardConfig'] query only when the GitLab-hosts allowlist actually
        # changed -- an event-driven refresh that replaces a constant 30s poll
        # (which multiplied audit-log writes across every same-key observer).
        #
        # Piggyback the folder tree (the in-memory ``_folders`` list, WITHOUT the
        # per-folder ``history_count`` that ``GET /api/chat/folders`` computes via
        # a synchronous session scan) so the sidebar can group sessions correctly
        # on the FIRST paint. Sessions arrive on this WS frame the instant the
        # socket connects; folders otherwise arrive only via a separate HTTP GET,
        # so the sidebar would render every session ungrouped (Unfiled bucket)
        # until that GET resolved, then visibly re-shuffle them into folders. The
        # HTTP query still runs to backfill ``history_count``; grouping no longer
        # waits on it. Slicing to the fields the client's grouping needs keeps
        # this hot-path frame small and never touches the filesystem.
        self._broadcast(
            {
                "_type": "slots",
                # BARE list for SSE and the generic-envelope fields below.
                "_slots_list": slots_data,
                # Enriched (public-repo status) list for the WS dashboard-user
                # frame ONLY. ``_broadcast`` builds the WS frame from this when
                # present; SSE never reads it, so status cannot reach the
                # unfiltered stream.
                "_slots_list_ws": slots_data_ws,
                "_yolo": yolo_active,
                "slots": json.dumps(slots_data),
                "channelTrusted": ch_trusted,
                "gitlabHostsGeneration": gitlab_hosts_generation(),
                # getattr, not self._folders: this read path runs on EVERY slots
                # push, including on a __new__-built DashboardState that seeded only
                # the push essentials and never ran __init__ (several endpoint
                # suites build their fixture that way). _folders is an __init__-only
                # assignment, so a bare attribute access would AttributeError there
                # — the exact break test_push_slots_update_survives_a_partially_
                # constructed_state pins against. An absent/None folder store is an
                # empty tree.
                #
                # Lightweight states can bypass load_folders(), so keep this hot
                # broadcast path tolerant of malformed in-memory values.
                "folders": _safe_folder_tree(getattr(self, "_folders", None)),
                # Lets the client distinguish "the tree changed" from "a session
                # blinked": this frame fires on routine slot activity, so the
                # tree alone is not a change signal.
                "foldersGeneration": self.folders_generation(),
                "governanceGeneration": governance_generation(),
            }
        )
        # The owner frame is the owner's ONLY slots frame — `_send_ws_all` skips
        # owner sockets for `slots` (see there for why), so this envelope has to
        # carry every key the generic one does. It previously carried a subset,
        # which is why the owner had to receive both: `folders` seeds the
        # first-paint folder tree and `gitlabHostsGeneration` drives the
        # dashboard-config invalidation, and neither was here. Both frames are now
        # built by `_slots_ws_frame`, so a key can no longer reach one and not the
        # other.
        owner_ws_clients = getattr(self, "_owner_ws_clients", None)
        if owner_ws_clients:
            owner_slots = self.serialize_slots(include_check_status=True)
            self._send_ws_owners(
                _slots_ws_frame(
                    owner_slots,
                    yolo=yolo_active,
                    channel_trusted=ch_trusted,
                    gitlab_hosts_gen=gitlab_hosts_generation(),
                    folders=_safe_folder_tree(getattr(self, "_folders", None)),
                    folders_gen=self.folders_generation(),
                    governance_gen=governance_generation(),
                )
            )

    def push_slot_title(self, key: str, title: str, *, full: bool = True) -> None:
        """Push a targeted title update for a single slot.

        By default also pushes a full slots update so the sidebar reflects the
        new title without callers needing to do both. Pass ``full=False`` for
        high-frequency streaming partials (word-by-word title reveal) to send
        only the lightweight ``slot_title`` event; finalize with a ``full=True``
        call once.
        """
        self._broadcast({"_type": "slot_title", "key": key, "title": title})
        if full:
            self.push_slots_update()

    def push_session_summary(self, key: str) -> None:
        """Broadcast that a session's intent summary was regenerated.

        Lets the summary panel invalidate immediately instead of polling, which
        matters because the summary is deliberately a pull-friendly artifact: a
        panel that polled would reintroduce the checking loop the feature exists
        to remove. Fire-and-forget — the client's own staleness window remains
        the safety net if the event is missed.
        """
        self._broadcast({"_type": "session_summary", "key": key})

    def push_artifact_update(self, slug: str, version: int, *, deleted: bool = False) -> None:
        """Broadcast an artifact content change to all connected clients.

        Emitted from the artifact mutation funnel (create / content update /
        revert / pull-latest / relocate / delete) so every open dashboard
        window — main, popouts, companion chat panels — can invalidate its
        artifact queries immediately instead of waiting for the 30s react-query
        staleness window. Fire-and-forget, best-effort: the
        staleness window remains the safety net if a client misses the event.
        """
        self._broadcast(
            {
                "_type": "artifact_update",
                "slug": slug,
                "version": version,
                "deleted": deleted,
            }
        )

    def push_refresh(self, *kinds: str) -> None:
        """Push a lightweight refresh hint for specific data types.

        The frontend receives ``event: refresh`` with ``data: kind1,kind2``
        and fetches fresh data only for those types.  This replaces blind
        polling — the server tells the client *when* to refresh, not the
        client guessing on a timer.

        Supported kinds: ``crons``, ``lessons``, ``agents``, ``history``,
        ``taskrunner``.
        """
        self._broadcast({"_type": "refresh", "kinds": ",".join(kinds)})

    def push_update_progress(self, step: str, detail: str = "") -> None:
        """Broadcast an update progress event to all connected clients.

        ``step`` is a short machine-readable phase name (e.g. ``pulling``,
        ``syncing``, ``building``, ``installing``, ``restarting``, ``failed``).
        ``detail`` is an optional human-readable message.
        """
        self._update_progress = {"step": step, "detail": detail}
        self._broadcast(
            {
                "_type": "update_progress",
                "step": step,
                "detail": detail,
            }
        )

    def clear_update_progress(self) -> None:
        """Reset update progress (e.g. after cancel or completion)."""
        self._update_progress = None

    def _broadcast(self, note: dict[str, Any]) -> None:
        """Send a message to all connected SSE and WS clients."""
        for q in self._sse_queues:
            try:
                q.put_nowait(note)
            except asyncio.QueueFull:
                pass
        self._notify_event.set()
        # WS broadcast — translate internal _type to WS message format
        if self._ws_clients:
            msg_type = note.get("_type", "notification")
            # Payload the scope gate inspects (slot / source keys), tracked per
            # branch so the chokepoint can filter correctly.
            ws_data: object
            if msg_type == "slots":
                # SSE (above) consumed the BARE ``_slots_list``. The WS frame is
                # built from the ENRICHED ``_slots_list_ws`` (public-repo status
                # for dashboard users) when present — app tokens are re-filtered
                # in ``_serialize_for_client`` so they never receive it, and SSE
                # never reads this key. Falls back to the bare list for callers
                # (targeted title pushes, tests) that send only ``_slots_list``.
                slots_list = (
                    note.get("_slots_list_ws")
                    or note.get("_slots_list")
                    or json.loads(note["slots"])
                )
                # ``data`` for slots carries the whole envelope so the
                # chokepoint can per-app filter and re-serialize it.
                ws_data = {
                    "slots": slots_list,
                    "yolo": note.get("_yolo", False),
                    "channelTrusted": note.get("channelTrusted", False),
                }
                # Built by `_slots_ws_frame`, NOT inline: the owner frame in
                # `_do_slots_broadcast` has to carry an identical key set, and it
                # cannot if each site names its own keys. See that function.
                ws_msg = _slots_ws_frame(
                    slots_list,
                    yolo=bool(ws_data["yolo"]),
                    channel_trusted=bool(ws_data["channelTrusted"]),
                    gitlab_hosts_gen=note.get("gitlabHostsGeneration"),
                    folders=note.get("folders"),
                    folders_gen=note.get("foldersGeneration"),
                    governance_gen=note.get("governanceGeneration"),
                )
            elif msg_type == "slot_title":
                ws_data = {"key": note["key"], "title": note["title"]}
                ws_msg = json.dumps({"type": "slot_title", "data": ws_data})
            elif msg_type == "refresh":
                ws_data = {"kinds": note["kinds"].split(",")}
                ws_msg = json.dumps({"type": "refresh", "data": ws_data})
            elif msg_type == "update_progress":
                ws_data = {"step": note["step"], "detail": note.get("detail", "")}
                ws_msg = json.dumps({"type": "update_progress", "data": ws_data})
            elif msg_type == "artifact_update":
                # Typed envelope (not the generic `notification` fallback) so
                # useWebSocket and future consumers get a self-documenting
                # event: {slug, version, deleted}.
                ws_data = {
                    "slug": note["slug"],
                    "version": note.get("version", 0),
                    "deleted": note.get("deleted", False),
                }
                ws_msg = json.dumps({"type": "artifact_update", "data": ws_data})
            elif msg_type == "session_summary":
                # Typed envelope, for the same reason as artifact_update above.
                # Without it this event falls into the generic `notification`
                # fallback, where two things go wrong: the client's
                # `case 'session_summary'` never matches (so the panel is never
                # invalidated and only a reload shows a new summary — defeating
                # the push-on-change design that lets the panel skip polling),
                # and the payload is dispatched as a Notification, putting one
                # entry with no `ts` in the bell feed.
                ws_data = {"key": note["key"]}
                ws_msg = json.dumps({"type": "session_summary", "data": ws_data})
            elif msg_type == "chat_message":
                # One serialiser, both doors — see chat_message_frame().
                # include_metadata=True because THIS door filters downstream:
                # _send_ws_all -> _ws_client_allowed (deny-by-default event
                # scope) decides per socket whether an app token may see this
                # slot at all. The SSE door has no such gate and decides for
                # itself; do not copy this True over there.
                chat_data = chat_message_frame(note, include_metadata=True)
                ws_data = chat_data
                ws_msg = json.dumps({"type": "chat_message", "data": chat_data})
            else:
                ws_data = note
                ws_msg = json.dumps({"type": "notification", "data": note})
            self._send_ws_all(msg_type, ws_data, ws_msg)

    def _spawn_ws_send(self, ws: web.WebSocketResponse, msg: str) -> None:
        _websocket_for(self)._spawn_ws_send(ws, msg)

    def _on_ws_send_done(self, task: asyncio.Task) -> None:
        _websocket_for(self)._on_ws_send_done(task)

    def _ws_client_allowed(self, ws: web.WebSocketResponse, msg_type: str, data: object) -> bool:
        return _websocket_for(self)._ws_client_allowed(ws, msg_type, data)

    def _serialize_for_client(
        self, ws: web.WebSocketResponse, msg_type: str, data: object, default_msg: str
    ) -> str:
        return _websocket_for(self)._serialize_for_client(ws, msg_type, data, default_msg)

    def _serialize_subagent_batch(
        self,
        ws: web.WebSocketResponse,
        msg_type: str,
        data: object,
        default_msg: str,
    ) -> str:
        return _websocket_for(self)._serialize_subagent_batch(ws, msg_type, data, default_msg)

    def _send_ws_all(self, msg_type: str, data: object, msg: str) -> None:
        _websocket_for(self)._send_ws_all(msg_type, data, msg)

    def _send_ws_owners(self, msg: str) -> None:
        _websocket_for(self)._send_ws_owners(msg)

    def broadcast_ws(self, msg_type: str, data: object) -> None:
        # Mirror first, broadcast second. A relay reader consumes the SSE stream,
        # so the mirrored copy must be queued before the frame fans out to local
        # WebSocket clients — otherwise a turn that ends inside the broadcast
        # (chat_done tearing the slot down) could publish to local clients a
        # frame the relay never receives.
        _mirror_relay_frame(self, msg_type, data)
        _websocket_for(self).broadcast_ws(msg_type, data)

    def broadcast_context_usage(self, slot_key: str, payload: dict) -> None:
        _persistence_for(self).broadcast_context_usage(self, slot_key, payload)

    def ensure_context_snapshots_loaded(self) -> None:
        _persistence_for(self).ensure_context_snapshots_loaded(self)

    def context_snapshot_for(self, slot_key: str) -> dict | None:
        return _persistence_for(self).context_snapshot_for(self, slot_key)

    def _persist_context_snapshots(self) -> None:
        _persistence_for(self)._persist_context_snapshots(self)

    async def deliver_ws_owners(self, msg_type: str, data: object) -> int:
        return await _websocket_for(self).deliver_ws_owners(msg_type, data)

    def broadcast_ws_owners(self, msg_type: str, data: object) -> None:
        _websocket_for(self).broadcast_ws_owners(msg_type, data)

    def ws_client_count(self) -> int:
        return _websocket_for(self).ws_client_count()

    def broadcast_browser_event(self, event_type: str, data: dict) -> None:
        _websocket_for(self).broadcast_browser_event(event_type, data)

    def register_ws(self, ws: web.WebSocketResponse, *, owner: bool = False) -> None:
        _websocket_for(self).register_ws(ws, owner=owner)

    def unregister_ws(self, ws: web.WebSocketResponse) -> None:
        _websocket_for(self).unregister_ws(ws)

    def _remove_ws(self, ws: web.WebSocketResponse) -> None:
        _websocket_for(self)._remove_ws(ws)

    def subscribe_logs(self, ws: web.WebSocketResponse) -> None:
        _websocket_for(self).subscribe_logs(ws)

    def unsubscribe_logs(self, ws: web.WebSocketResponse) -> None:
        _websocket_for(self).unsubscribe_logs(ws)

    def subscribe_subagents(self, ws: web.WebSocketResponse) -> None:
        _websocket_for(self).subscribe_subagents(ws)

    def unsubscribe_subagents(self, ws: web.WebSocketResponse) -> None:
        _websocket_for(self).unsubscribe_subagents(ws)

    def broadcast_ws_subagent_subscribers(self, msg_type: str, data: object) -> None:
        _websocket_for(self).broadcast_ws_subagent_subscribers(msg_type, data)

    async def close_all_ws(self) -> None:
        await _websocket_for(self).close_all_ws()


# ── Notification persistence ──


def _redact_note_value(value: Any) -> Any:
    """Recursively redact every string inside a notification note value.

    Notes carry LLM-derived content in nested structures too (e.g. the
    ``actions`` field is a list of dicts whose ``label`` values may be
    model output), so redaction must descend into lists and dicts rather
    than only scanning top-level strings.
    """
    if isinstance(value, str):
        if not value:
            return value
        value, _ = redact_exfiltration_urls(value)
        value, _ = redact_credentials(value)
        return value
    if isinstance(value, list):
        return [_redact_note_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_note_value(item) for key, item in value.items()}
    return value


def _notifications_path() -> Path:
    """Path to the notifications JSONL file."""
    return config_dir() / _NOTIFICATIONS_FILE


def _note_ts_epoch(note: dict[str, Any]) -> float | None:
    """Best-effort epoch seconds for a note's ``ts`` (ISO string or epoch str)."""
    ts = note.get("ts")
    if ts is None:
        return None
    try:
        parsed = float(ts)
        # float() of a numeric STRING beyond float range (e.g. "-1e999")
        # returns inf/-inf without raising — a -inf epoch would make every
        # TTL comparison read "expired" and the sweep would destroy the row,
        # violating the never-destroy-on-ambiguity rule.
        # NaN likewise carries no ordering meaning. Treat both as
        # unparseable (note kept).
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError, OverflowError):
        # OverflowError: float() of a JSON integer beyond float range (e.g.
        # 10**400) raises rather than returning inf — one poison row must
        # not abort the whole sweep.
        pass
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except (ValueError, OverflowError, OSError):
        # .timestamp() raises OverflowError/OSError (not just ValueError) for
        # platform-unrepresentable datetimes -- pre-epoch or far-future ISO
        # strings, most acutely on Windows. Treat them as unparseable (note
        # kept) rather than letting the error escape the sweep: at load time
        # that escape would hit _load_notifications' blanket handler, empty
        # the history, and the next mutation would persist the loss.
        return None


def sweep_expired_notifications(log: list[dict[str, Any]], *, now: float | None = None) -> int:
    """Remove expired PASSIVE notes in place (RFC Phase 5 TTL sweeper).

    A note expires when it is passive, carries a positive integer ``ttl``
    (seconds), and ``ts + ttl`` is in the past. Only passive notes sweep —
    critical/default history has recall value and stays until the user acts.
    Notes with unparseable timestamps are kept (never destroy on ambiguity).
    Returns the number of rows removed.
    """
    now = time.time() if now is None else now
    kept: list[dict[str, Any]] = []
    removed = 0
    try:
        for note in log:
            ttl = note.get("ttl")
            epoch = _note_ts_epoch(note)
            if (
                note.get("priority") == "passive"
                and isinstance(ttl, int)
                and not isinstance(ttl, bool)  # bool is an int subclass
                and ttl > 0
                and epoch is not None
                # ttl < now - epoch (not epoch + ttl < now): adding an
                # arbitrarily large int TTL to a float epoch raises
                # OverflowError, and the sweep-wide guard would abort the
                # whole sweep. int-vs-float comparison never overflows.
                and ttl < now - epoch
            ):
                removed += 1
                continue
            kept.append(note)
    except Exception:
        # The sweep is an optimization -- it must NEVER cost data. A poison
        # row escaping here at load time would hit _load_notifications'
        # blanket handler and empty the entire history (persisted on the
        # next mutation); in _deliver_note it would break every delivery.
        logger.warning("Notification TTL sweep aborted", exc_info=True)
        return 0
    if removed:
        log[:] = kept
    return removed


def _load_notifications() -> list[dict[str, Any]]:
    """Load persisted notifications from disk (newest last)."""
    path = _notifications_path()
    if not path.exists():
        return []
    try:
        entries: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = normalize_note(json.loads(line))
                # Redact at load: rows written before delivery-time redaction
                # existed may carry unredacted LLM-derived content; they are
                # served to SSE clients straight from this list.
                for key, value in parsed.items():
                    if key != "ts":
                        parsed[key] = _redact_note_value(value)
                entries.append(parsed)
            except Exception:  # noqa: BLE001 — skip the bad row, not the whole file
                # normalize_note/_redact_note_value can raise on valid-JSON
                # rows with unexpected shapes (e.g. a top-level array); keep
                # the per-line skip semantics instead of losing all history
                # to the outer except.
                logger.debug("Skipping malformed notification row", exc_info=True)
                continue
        # RFC Phase 5: drop expired passive rows BEFORE the recency cap.
        # Sweeping after truncation loses data: with more than N rows on
        # disk, newer expired-passive rows would displace older LIVE rows
        # during truncation, and the next full rewrite would delete those
        # live rows permanently. Disk rewrites lazily on
        # the next mutation; the in-memory view is authoritative for serving.
        sweep_expired_notifications(entries)
        # Keep only the most recent N live rows
        entries = entries[-_MAX_PERSISTED_NOTIFICATIONS:]
        return entries
    except Exception:
        logger.debug("Failed to load notifications", exc_info=True)
        return []


# Notification file I/O runs exclusively on this single-worker executor when
# an event loop is running: appends (from the delivery sink) and rewrites
# (from delete/ack/clear) execute strictly in submission order, so no lock is
# needed and the loop never blocks on file I/O.
_notification_io_pool: concurrent.futures.ThreadPoolExecutor | None = None


def _notification_io_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Lazily create the single-worker executor for notification persistence."""
    global _notification_io_pool
    if _notification_io_pool is None:
        _notification_io_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="notif-io"
        )
    return _notification_io_pool


def _persist_notification(note: dict[str, str]) -> bool:
    """Append a single notification to the JSONL file on disk.

    Returns True on success. Failures are swallowed (legacy system producers
    are explicitly best-effort — history is a cache, delivery is the
    broadcast) but reported via the return value so callers that need
    durability (the app push endpoint) can surface them.
    """
    path = _notifications_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(note) + "\n")
        # Trim if file grows too large (keep last N lines)
        _maybe_trim_notifications(path)
        return True
    except Exception:
        logger.debug("Failed to persist notification", exc_info=True)
        return False


def _rewrite_notifications(notifications: list[dict[str, str]]) -> None:
    """Rewrite the entire notifications file from the in-memory list."""
    path = _notifications_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(n) + "\n" for n in notifications[-_MAX_PERSISTED_NOTIFICATIONS:]]
        path.write_text("".join(lines), encoding="utf-8")
    except Exception:
        logger.debug("Failed to rewrite notifications file", exc_info=True)


def _maybe_trim_notifications(path: Path) -> None:
    """Trim the notifications file if it exceeds 2x the max.

    Expired passive rows are discarded BEFORE the recency cap — the same
    displacement hazard as the load path: trimming the
    raw tail first would retain newer expired-passive rows while deleting
    older LIVE rows, permanently losing history after the next load-time
    sweep. Unparseable lines are kept (never destroy on ambiguity).
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) <= _MAX_PERSISTED_NOTIFICATIONS * 2:
            return
        keep: list[str] = []
        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                keep.append(line)
                continue
            if isinstance(row, dict) and sweep_expired_notifications([row]) == 1:
                continue  # expired passive row -- drop before the cap
            keep.append(line)
        kept = keep[-_MAX_PERSISTED_NOTIFICATIONS:]
        path.write_text("".join(kept), encoding="utf-8")
    except Exception:
        pass


def _fmt_duration(secs: int) -> str:
    """Format seconds as human-readable duration."""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m {s}s"


def _governance_status() -> str:
    """Governance health for the status snapshot (never raises)."""
    try:
        from kiro_crew.platform.governance_health import governance_status

        return governance_status()
    except Exception:
        return "unknown"


def _cached_check_status(url: str) -> dict | None:
    """Lazy wrapper so state.py has no import-time dep on the handler module."""
    from kiro_crew.dashboard.handlers.source_providers import get_cached_check_status

    return get_cached_check_status(url)


def _repo_is_public(url: str) -> bool | None:
    """Lazy wrapper for the repo-visibility reader (public/private/unknown).

    Function-local import (top-level-imports exception): ``source_providers``
    imports chat-state helpers from this module, so a module-scope import here
    would create a bootstrap import cycle. Same rationale as
    ``_cached_check_status`` directly above.
    """
    from kiro_crew.dashboard.handlers.source_providers import is_repo_public

    return is_repo_public(url)
