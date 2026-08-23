"""Persistent Agent Channels — collaborative multi-agent execution.

A channel is a shared communication space where multiple specialized
agents work on assigned roles, post progress, react to @mentions,
and stay alive until dismissed or timed out.

All agent-to-agent communication goes through the channel (no private
messaging).  The human is the final approver for mutating operations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from kiro_crew import name_grant
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.trust_patterns import extract_bash_command

logger = logging.getLogger(__name__)

_MAX_AGENTS = 3
_MAX_CHANNELS = 1
_MAX_MESSAGES = 200
_MAX_A2A_EXCHANGES = 3

# Max time an agent blocks on its inbox before re-checking its stop condition,
# guaranteeing subscribe() can never park indefinitely.
_INBOX_POLL_SECS = 1.0

# Direct-to-user messaging tools a channel agent may never invoke — channel
# agents communicate exclusively through channel posts.  send_notification
# reaches the user like send_message does (notification feed publish, badge,
# and sound), so both sit
# behind the same containment boundary.  All THREE session-control tools sit
# behind it, in both directions: stopping one of the user's dashboard sessions
# reaches the user through that session's transcript, and READING one pulls a
# private dashboard conversation into a channel other humans can see.  The read
# tool mutates nothing, but containment here is about what crosses the boundary,
# not about who writes — so the exfiltration direction is blocked alongside the
# control one.  CREATE is blocked for a different reason than the other two: it
# writes nothing into an existing conversation, but a session it opened would be
# one the channel agent then owns and may act on, which is exactly the
# containment this list exists to hold.  SEND is the sharpest of the four: stop
# only cancels and read only exfiltrates, but send delivers text that the target
# session RUNS as a turn — so external channel content would execute inside a
# private dashboard conversation.
# Matched against the rendered
# permission-request text/title via _blocked_tool_named() (boundary-aware,
# not naive substring — "Editing send_notification.py" must NOT match).
CHANNEL_AGENT_BLOCKED_TOOLS: tuple[str, ...] = (
    "send_message",
    "send_notification",
    "session_stop",
    "session_send",
    "session_read_message",
    "session_create",
    "session_close",
)

# Boundary-aware matcher: the tool name must stand alone in the rendered
# title — not embedded in a filename/path/identifier ("send_notification.py",
# "/tmp/send_message_backup"). MCP separator runs of 2+ underscores are
# normalized to spaces first so BOTH qualified invocation forms match:
# "kirocrew-core___send_message" (kiro-cli) and
# "mcp__kirocrew-core__send_message" (canonical MCP prefix form).
_BLOCKED_TOOL_RE = re.compile(
    r"(?<![\w.\-/])("
    + "|".join(re.escape(t) for t in CHANNEL_AGENT_BLOCKED_TOOLS)
    + r")(?![\w.\-/])"
)
_MCP_SEPARATOR_RE = re.compile(r"_{2,}")


def _shell_base_binary(cmd: str) -> str | None:
    """The single binary a SIMPLE shell command invokes, else None.

    Shell-aware (shlex) tokenization, fail-closed on everything that is not
    one plain invocation: parse failures, empty commands, shell operators or
    substitution anywhere (``|``, ``&``, ``;``, newline, backtick, ``$(``,
    redirects), and env-assignment or quoted/space-bearing first tokens. A
    base-command trust grant keyed on anything richer than one unambiguous
    binary name has already been shown to widen scope (naive first-token
    slicing turned ``"./my tool" --safe`` into a ``"./my`` prefix grant, and
    env prefixes into match-anything grants), so anything ambiguous simply
    has no base.
    """
    if not cmd or _CHANNEL_SHELL_OPERATOR_RE.search(cmd):
        return None
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    if not tokens:
        return None
    base = tokens[0]
    # An env-assignment first token means the binary is the SECOND token —
    # refuse rather than guess (the grant would silently cover any command
    # run under that prefix).
    if not base or "=" in base:
        return None
    # A base containing whitespace can only come from quoting; a quoted
    # executable path is matched by the exact-command tier, not by base.
    if any(c.isspace() for c in base):
        return None
    # POSITIVE shape check: the base must look like a plain binary name or
    # path. Enumerating bad metacharacters one by one (``!``, ``(``, …) is a
    # losing game — the shell always has one more; anything outside this
    # conservative charset is refused instead.
    if not _CHANNEL_BASE_BINARY_RE.fullmatch(base):
        return None
    # Shell reserved words pass the charset but are interpreted by the shell,
    # not executed as a binary — ``time rm ...`` must not yield a ``time``
    # grant that covers ``time <anything>``.
    if base in _CHANNEL_SHELL_RESERVED_WORDS:
        return None
    return base


# Anything that makes a command more than ONE plain invocation. Deliberately
# broader than the enforcement splitter's operator set: over-matching here
# fails toward "no per-command grant available", never toward a wider grant.
# Parentheses and braces cover subshell / brace grouping — "(rm /tmp/a)" must
# not yield "(rm" as a trusted binary.
_CHANNEL_SHELL_OPERATOR_RE = re.compile(r"[|&;`\n\r<>(){}]|\$\(")

# What a trusted base binary is ALLOWED to look like: alphanumerics plus the
# few characters real binary names and paths use. Everything else — negation
# ``!``, test ``[``, arithmetic ``((`` — simply has no derivable base.
_CHANNEL_BASE_BINARY_RE = re.compile(r"[A-Za-z0-9._/+-]+")

# Words the shell interprets instead of executing. A grant keyed on one would
# cover whatever command the shell runs under it.
_CHANNEL_SHELL_RESERVED_WORDS = frozenset(
    "! case coproc do done elif else esac fi for function if in select "
    "then time until while".split()
)


def _match_trusted_channel_command(cmd: str, agent: "ChannelAgent") -> str | None:
    """Match a pending shell command against the agent's trust grants.

    Two literal forms only, both case-sensitive (on POSIX ``./Deploy.sh`` and
    ``./deploy.sh`` are different executables):

    - exact: the whole command text equals a granted command, OR
    - base: the command is a SIMPLE invocation (see ``_shell_base_binary``)
      whose binary equals a granted base.

    Compound commands never match a base grant and only match an exact grant
    granted for that identical compound; there is no pattern language and no
    per-segment matching, so a grant can never cover text the user did not
    read. Returns an audit label or None.
    """
    if cmd in agent._trusted_commands:
        return f"command:{cmd}"
    base = _shell_base_binary(cmd)
    if base is not None and base in agent._trusted_bases:
        return f"base:{base}"
    return None


def _blocked_tool_named(rendered: str) -> bool:
    """True when a blocked messaging tool is named (as a tool) in *rendered*."""
    return bool(_BLOCKED_TOOL_RE.search(_MCP_SEPARATOR_RE.sub(" ", rendered)))


class ListenMode(Enum):
    """How an agent receives channel messages."""

    ALL = "all"  # every message (orchestrator)
    MENTION = "mention"  # only @mention or human broadcast
    SILENT = "silent"  # initial task only, then done


class ApprovalPolicy(Enum):
    """What tool calls require human approval."""

    ALL = "all"  # every tool call
    WRITES = "writes"  # only mutating ops (default)
    TRUSTED = "trusted"  # auto-approve everything


@dataclass
class ChannelMessage:
    """A single message in a channel."""

    id: str
    from_id: str  # "human" or agent id
    from_role: str  # display name
    content: str
    mention: str | list[str] | None = None  # target agent id(s)
    msg_type: str = "progress"  # progress|mention|broadcast|approval|done|system
    timestamp: float = field(default_factory=time.time)
    thread_id: str | None = None  # parent message ID (None = top-level)
    reply_to: str | None = None  # agent ID of parent message sender
    reply_count: int = 0  # thread reply count (top-level only)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from_id": self.from_id,
            "from_role": self.from_role,
            "content": self.content,
            "mention": self.mention,
            "msg_type": self.msg_type,
            "timestamp": self.timestamp,
            "thread_id": self.thread_id,
            "reply_to": self.reply_to,
            "reply_count": self.reply_count,
        }


@dataclass
class ChannelAgent:
    """A persistent agent in a channel."""

    id: str
    role: str
    agent_name: str
    task: str
    session_key: str = ""  # f"channel:{channel_id}:{agent_id}"
    state: str = "pending"  # pending|working|listening|done|failed
    is_orchestrator: bool = False
    approval_policy: ApprovalPolicy = ApprovalPolicy.WRITES
    listen_mode: ListenMode = ListenMode.MENTION
    inbox: asyncio.Queue[ChannelMessage] = field(default_factory=asyncio.Queue)
    _approval_future: asyncio.Future | None = field(default=None, repr=False)
    # Per-command trust grants (trust_command / trust_base tiers) — runtime
    # only, like the chat slot's session-scoped grants: agents are relaunched
    # with fresh sessions on gateway restart, so grants deliberately do not
    # persist. Grants are OPAQUE LITERALS, never patterns: ``_trusted_commands``
    # holds exact full command texts (matched by string equality) and
    # ``_trusted_bases`` holds single binary names (matched by shlex-token
    # equality). No pattern language exists here by design — every derived
    # sub-pattern scheme reviewed on this surface (segment globs, base globs
    # from naive tokenization) leaked scope the user never consented to.
    _trusted_commands: set[str] = field(default_factory=set, repr=False)
    _trusted_bases: set[str] = field(default_factory=set, repr=False)
    # Canonical shell command of the approval currently awaiting a decision,
    # extracted server-side from the provider event's ``tool_input`` when the
    # provider classified the tool as shell ("" otherwise). The approve
    # endpoint binds trust_command / trust_base grants to THIS value — display
    # titles and request-body patterns are LLM-influenced and never scope a
    # grant. Set while ``_approval_future`` is pending, cleared with it.
    _pending_approval_command: str = field(default="", repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "agent_name": self.agent_name,
            "task": self.task,
            "session_key": self.session_key,
            "state": self.state,
            "is_orchestrator": self.is_orchestrator,
            "approval_policy": self.approval_policy.value,
            "listen_mode": self.listen_mode.value,
        }


@dataclass
class Channel:
    """A shared communication space for multiple agents."""

    id: str
    topic: str
    orchestrator_id: str | None = None
    members: dict[str, ChannelAgent] = field(default_factory=dict)
    messages: list[ChannelMessage] = field(default_factory=list)
    _msg_index: dict[str, ChannelMessage] = field(default_factory=dict)
    exchange_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    trusted: bool = False  # channel-level trust — auto-approve all tools
    _broadcast_fn: Any = None  # set by ChannelManager
    _save_fn: Any = None  # set by ChannelManager
    _max_agents: int = _MAX_AGENTS
    max_exchanges: int = _MAX_A2A_EXCHANGES

    def add_agent(
        self,
        role: str,
        agent_name: str = "",
        task: str = "",
        is_orchestrator: bool = False,
        approval_policy: ApprovalPolicy | str = ApprovalPolicy.WRITES,
        listen_mode: ListenMode | str = ListenMode.MENTION,
    ) -> ChannelAgent | None:
        if len(self.members) >= self._max_agents:
            logger.warning("Channel %s at agent capacity (%d)", self.id, self._max_agents)
            return None
        if isinstance(approval_policy, str):
            approval_policy = ApprovalPolicy(approval_policy)
        if isinstance(listen_mode, str):
            try:
                listen_mode = ListenMode(listen_mode)
            except ValueError:
                listen_mode = ListenMode.MENTION
        # First agent is orchestrator by default
        if not self.orchestrator_id and not is_orchestrator and len(self.members) == 0:
            is_orchestrator = True
        if is_orchestrator:
            listen_mode = ListenMode.ALL
        agent_id = uuid.uuid4().hex[:8]
        agent = ChannelAgent(
            id=agent_id,
            role=role,
            agent_name=agent_name,
            task=task,
            session_key=f"channel:{self.id}:{agent_id}",
            is_orchestrator=is_orchestrator,
            approval_policy=approval_policy,
            listen_mode=listen_mode,
        )
        self.members[agent_id] = agent
        if is_orchestrator:
            self.orchestrator_id = agent_id
        self._broadcast(
            "channel_agent_joined",
            {
                "channel_id": self.id,
                "agent": agent.to_dict(),
            },
        )
        self._save()
        return agent

    def remove_agent(self, agent_id: str, reason: str = "dismissed") -> bool:
        agent = self.members.pop(agent_id, None)
        if not agent:
            return False
        agent.state = "done"
        self._broadcast(
            "channel_agent_left",
            {
                "channel_id": self.id,
                "agent_id": agent_id,
                "reason": reason,
            },
        )
        self._save()
        return True

    async def post(
        self,
        from_id: str,
        content: str,
        from_role: str = "",
        mention: str | list[str] | None = None,
        msg_type: str = "progress",
        thread_id: str | None = None,
    ) -> ChannelMessage:
        # Normalize mentions to a set
        mentions: set[str] = set()
        if isinstance(mention, list):
            mentions = set(mention)
        elif mention:
            mentions = {mention}
        mentions.discard(from_id)  # no self-mentions

        # Resolve reply_to from thread parent
        reply_to: str | None = None
        if thread_id:
            parent = self._msg_index.get(thread_id)
            if parent:
                reply_to = parent.from_id
                parent.reply_count += 1

        msg = ChannelMessage(
            id=uuid.uuid4().hex[:8],
            from_id=from_id,
            from_role=from_role or from_id,
            content=content,
            mention=list(mentions) if mentions else None,
            msg_type=msg_type,
            thread_id=thread_id,
            reply_to=reply_to,
        )
        self.messages.append(msg)
        self._msg_index[msg.id] = msg
        if len(self.messages) > _MAX_MESSAGES:
            removed = self.messages.pop(0)
            self._msg_index.pop(removed.id, None)

        # Human message resets A2A exchange budget — agents get fresh rounds
        if from_id == "human":
            self.exchange_counts.clear()

        for agent in self.members.values():
            if agent.id == from_id or agent.state in ("done", "failed"):
                continue
            if agent.listen_mode == ListenMode.SILENT:
                continue

            is_human = from_id == "human"

            # Thread routing: default listener = parent sender
            if thread_id and reply_to == agent.id and not mentions:
                await agent.inbox.put(msg)
                continue

            # Thread fallback: if reply_to doesn't match any agent (e.g. system message),
            # route human thread replies to orchestrator
            if (
                thread_id
                and is_human
                and not mentions
                and agent.is_orchestrator
                and reply_to not in self.members
            ):
                await agent.inbox.put(msg)
                continue

            # Orchestrator gets all top-level human messages (no @mention needed)
            if is_human and not mentions and not thread_id and agent.is_orchestrator:
                await agent.inbox.put(msg)
                continue

            # Everyone else: strict @mention only
            if agent.id not in mentions:
                continue

            # A2A exchange limit
            if not is_human:
                pair = (from_id, agent.id)
                if self.exchange_counts.get(pair, 0) >= self.max_exchanges:
                    logger.info(
                        "A2A limit reached: %s → %s in channel %s",
                        from_id,
                        agent.id,
                        self.id,
                    )
                    continue
                self.exchange_counts[pair] = self.exchange_counts.get(pair, 0) + 1

            await agent.inbox.put(msg)

        # Dead agent bounce
        for mid in mentions:
            target = self.members.get(mid)
            if target and target.state in ("done", "failed"):
                bounce = ChannelMessage(
                    id=uuid.uuid4().hex[:8],
                    from_id="system",
                    from_role="System",
                    mention=None,
                    msg_type="system",
                    content=f"⚠️ @{target.role} is no longer active.",
                )
                self.messages.append(bounce)
                self._msg_index[bounce.id] = bounce
                if len(self.messages) > _MAX_MESSAGES:
                    removed = self.messages.pop(0)
                    self._msg_index.pop(removed.id, None)
                self._broadcast(
                    "channel_message", {"channel_id": self.id, "message": bounce.to_dict()}
                )

        # Always broadcast to frontend
        self._broadcast(
            "channel_message",
            {
                "channel_id": self.id,
                "message": msg.to_dict(),
            },
        )
        self._save()
        return msg

    async def subscribe(self, agent_id: str):
        """Async generator yielding messages for an agent."""
        agent = self.members.get(agent_id)
        if not agent:
            return
        while agent.state not in ("done", "failed"):
            # Bounded get so the agent re-checks its stop condition instead of
            # parking forever on inbox.get() when no message ever arrives
            # (sender died, channel closed, shutdown signalled). Without the
            # timeout nothing would wake the blocked get() and the task/thread
            # would leak, blocking clean shutdown.
            try:
                msg = await asyncio.wait_for(agent.inbox.get(), timeout=_INBOX_POLL_SECS)
            except asyncio.TimeoutError:
                continue
            yield msg

    def _broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        if self._broadcast_fn:
            self._broadcast_fn(event_type, data)

    def _save(self) -> None:
        if self._save_fn:
            self._save_fn(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "orchestrator_id": self.orchestrator_id,
            "members": {k: v.to_dict() for k, v in self.members.items()},
            "message_count": len(self.messages),
            "created_at": self.created_at,
        }

    def serialize(self) -> dict[str, Any]:
        """Full serialization for persistence."""
        return {
            "id": self.id,
            "topic": self.topic,
            "orchestrator_id": self.orchestrator_id,
            "created_at": self.created_at,
            "members": {k: v.to_dict() for k, v in self.members.items()},
            "messages": [m.to_dict() for m in self.messages],
            "exchange_counts": {f"{a}:{b}": c for (a, b), c in self.exchange_counts.items()},
            "trusted": self.trusted,
            "max_exchanges": self.max_exchanges,
        }

    @classmethod
    def deserialize(
        cls, data: dict[str, Any], broadcast_fn: Any = None, save_fn: Any = None
    ) -> "Channel":
        """Restore a channel from serialized data."""
        ch = cls(id=data["id"], topic=data["topic"])
        ch.orchestrator_id = data.get("orchestrator_id")
        ch.created_at = data.get("created_at", time.time())
        ch.trusted = data.get("trusted", False)
        ch.max_exchanges = data.get("max_exchanges", _MAX_A2A_EXCHANGES)
        ch._broadcast_fn = broadcast_fn
        ch._save_fn = save_fn
        for aid, ad in data.get("members", {}).items():
            agent = ChannelAgent(
                id=ad["id"],
                role=ad["role"],
                agent_name=ad.get("agent_name", ""),
                task=ad.get("task", ""),
                session_key=ad.get("session_key", f"channel:{data['id']}:{ad['id']}"),
                state="done",  # always restore as done
                is_orchestrator=ad.get("is_orchestrator", False),
                listen_mode=ListenMode(
                    ad.get("listen_mode", "all" if ad.get("is_orchestrator") else "mention")
                ),
            )
            ch.members[aid] = agent
        for md in data.get("messages", []):
            msg = ChannelMessage(
                id=md["id"],
                from_id=md["from_id"],
                from_role=md["from_role"],
                content=md["content"],
                mention=md.get("mention"),
                msg_type=md.get("msg_type", "progress"),
                timestamp=md.get("timestamp", 0),
                thread_id=md.get("thread_id"),
                reply_to=md.get("reply_to"),
                reply_count=md.get("reply_count", 0),
            )
            ch.messages.append(msg)
            ch._msg_index[msg.id] = msg
        for k, v in data.get("exchange_counts", {}).items():
            parts = k.split(":", 1)
            if len(parts) == 2:
                ch.exchange_counts[(parts[0], parts[1])] = v
        return ch


class ChannelManager:
    """Create and manage persistent agent channels."""

    def __init__(
        self,
        broadcast_fn: Any = None,
        max_channels: int = _MAX_CHANNELS,
        max_agents: int = _MAX_AGENTS,
        channels_dir: str | None = None,
    ):
        self._channels: dict[str, Channel] = {}
        self._broadcast_fn = broadcast_fn
        self._max_channels = max_channels
        self._max_agents = max_agents
        # Resolve the channels dir lazily in __init__ (not as a class attr) so
        # merely importing this module never triggers config_dir() and its
        # one-time data-home migration as an import side effect — that must fire
        # only at the single chosen point (ensure_data_home() in the CLI prologue).
        self._CHANNELS_DIR = channels_dir or str(config_dir() / "channels")
        self._load_all()

    def _save_channel(self, channel: Channel) -> None:
        """Persist channel state to disk.

        Routed through the shared :func:`atomic_write` helper rather than a
        hand-rolled temp-write-and-rename. The hand-rolled form derived its temp
        name from the destination (``<id>.json.tmp``), so two writers persisting
        the same channel raced on one filename: the loser could publish a
        half-written payload, or fail outright when its rename found the temp
        already moved. It also missed the helper's bounded retry for the Windows
        rename window, where a scanner holding the temp file makes a correct
        write lose its payload.

        Durability and permission semantics are deliberately unchanged: no
        ``fsync`` (the pre-existing best-effort contract for channel state) and
        no explicit ``mode``, so the file still lands at the umask default.
        ``json.dumps`` is ASCII-only by default, so the helper's UTF-8 encoding
        puts the same bytes on disk as the previous locale-default text handle.

        ``os.makedirs`` stays OUTSIDE the ``try`` on purpose. The helper creates
        the parent itself, so this call is now belt-and-braces -- but moving
        directory creation inside the ``try`` would newly swallow a "cannot
        create the channels directory" failure that callers see raised today.
        """
        os.makedirs(self._CHANNELS_DIR, exist_ok=True)
        path = os.path.join(self._CHANNELS_DIR, f"{channel.id}.json")
        try:
            atomic_write(path, json.dumps(channel.serialize()))
        except Exception:
            logger.exception("Failed to save channel %s", channel.id)

    def _delete_channel_file(self, channel_id: str) -> None:
        path = os.path.join(self._CHANNELS_DIR, f"{channel_id}.json")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _load_all(self) -> None:
        """Load persisted channels on startup."""
        if not os.path.isdir(self._CHANNELS_DIR):
            return
        for fname in os.listdir(self._CHANNELS_DIR):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self._CHANNELS_DIR, fname)
            try:
                with open(path) as f:
                    data = json.load(f)
                ch = Channel.deserialize(
                    data, broadcast_fn=self._broadcast_fn, save_fn=self._save_channel
                )
                ch._max_agents = self._max_agents
                self._channels[ch.id] = ch
                logger.info("Restored channel %s (%s)", ch.id, ch.topic)
            except Exception:
                logger.exception("Failed to load channel from %s", path)

    def create(self, topic: str) -> Channel | None:
        if len(self._channels) >= self._max_channels:
            logger.warning("Channel capacity reached (%d)", self._max_channels)
            return None
        channel_id = uuid.uuid4().hex[:8]
        channel = Channel(
            id=channel_id,
            topic=topic,
            _broadcast_fn=self._broadcast_fn,
            _save_fn=self._save_channel,
            _max_agents=self._max_agents,
        )
        self._channels[channel_id] = channel
        if self._broadcast_fn:
            self._broadcast_fn("channel_created", channel.to_dict())
        logger.info("Channel %s created: %s", channel_id, topic)
        return channel

    def get(self, channel_id: str) -> Channel | None:
        return self._channels.get(channel_id)

    def close(self, channel_id: str) -> bool:
        channel = self._channels.pop(channel_id, None)
        if not channel:
            return False
        for agent in channel.members.values():
            agent.state = "done"
            if agent._task and not agent._task.done():
                agent._task.cancel()
        if self._broadcast_fn:
            self._broadcast_fn("channel_closed", {"channel_id": channel_id})
        self._delete_channel_file(channel_id)
        logger.info("Channel %s closed", channel_id)
        return True

    def list_channels(self) -> list[dict[str, Any]]:
        return [ch.to_dict() for ch in self._channels.values()]

    @property
    def count(self) -> int:
        return len(self._channels)


# ── Channel Agent Execution Loop ──


async def run_channel_agent(
    agent: ChannelAgent,
    channel: Channel,
    sessions: Any,  # SessionManager
    is_yolo: Any = None,  # callable returning bool
) -> None:
    """Two-phase agent lifecycle: working → listening."""
    agent.state = "pending"
    channel._broadcast(
        "channel_agent_status",
        {"channel_id": channel.id, "agent_id": agent.id, "state": "pending"},
    )

    try:
        client, _is_new, _resumed = await sessions.get_or_create(
            agent.session_key,
            agent=agent.agent_name or None,
            approval_policy=agent.approval_policy.value,
        )

        agent.state = "listening"
        channel._broadcast(
            "channel_agent_status",
            {"channel_id": channel.id, "agent_id": agent.id, "state": "listening"},
        )

        # No initial task — agents wait until @mentioned
        # Notify orchestrator about new agent so it can onboard them
        if not agent.is_orchestrator and channel.orchestrator_id:
            task_desc = f" Task: {agent.task}" if agent.task else ""
            await channel.post(
                "system",
                f"🆕 @{agent.role} joined the channel.{task_desc} "
                f"Please @mention them to assign work or ask them to stand by.",
                from_role="System",
                mention=channel.orchestrator_id,
                msg_type="system",
            )
        elif agent.is_orchestrator:
            await channel.post(
                "system",
                f"✅ @{agent.role} is ready. Send a message to get started.",
                from_role="System",
                msg_type="system",
            )

        async for msg in channel.subscribe(agent.id):
            agent.state = "working"
            channel._broadcast(
                "channel_agent_status",
                {"channel_id": channel.id, "agent_id": agent.id, "state": "working"},
            )

            members = [
                a.role
                for a in channel.members.values()
                if a.id != agent.id and a.state not in ("done", "failed")
            ]
            others = f" Team: {', '.join('@' + m for m in members)}." if members else ""
            prompt = (
                f"[CHANNEL] You are '{agent.role}'.{others} "
                "ONLY write @AgentName when you want to assign them work or ask them a question — "
                "the system routes it to their inbox. To just talk ABOUT an agent, use their name without @. "
                "Do NOT use spawn_run. Do NOT use send_message. Do NOT @mention yourself.\n"
                f"[{msg.from_role}]: {msg.content}"
            )
            # Orchestrator posts top-level when: (a) responding to human, or
            # (b) reporting results back after finishing work with an agent.
            # Everything else (agent coordination) stays in thread.
            is_toplevel_human = msg.from_id == "human" and not msg.thread_id
            is_agent_report_back = (
                agent.is_orchestrator and msg.from_id != "human" and msg.thread_id is not None
            )
            orch_toplevel = agent.is_orchestrator and (is_toplevel_human or is_agent_report_back)
            tid = None if orch_toplevel else (msg.thread_id or msg.id)
            await _stream_task(agent, channel, client, prompt, thread_id=tid, is_yolo=is_yolo)

            agent.state = "listening"
            channel._broadcast(
                "channel_agent_status",
                {
                    "channel_id": channel.id,
                    "agent_id": agent.id,
                    "state": "listening",
                },
            )

    except Exception:
        logger.exception("Channel agent %s (%s) failed", agent.id, agent.role)
        agent.state = "failed"
    finally:
        if agent.state not in ("done", "failed"):
            agent.state = "done"
        channel._broadcast(
            "channel_agent_status",
            {"channel_id": channel.id, "agent_id": agent.id, "state": agent.state},
        )
        sessions.release(agent.session_key)
        logger.info("Channel agent %s (%s) finished: %s", agent.id, agent.role, agent.state)


async def _stream_task(
    agent: ChannelAgent,
    channel: Channel,
    client: Any,  # LLMProvider
    message: str,
    thread_id: str | None = None,
    is_yolo: Any = None,  # callable returning bool
) -> None:
    """Stream an LLM task, posting output as channel messages."""
    from kiro_crew.providers.base import (
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        EVENT_TEXT_CHUNK,
        EVENT_TOOL_CALL,
    )
    from kiro_crew.security import (
        redact_and_truncate,
        redact_credentials,
        redact_exfiltration_urls,
    )
    from kiro_crew.sel import sel

    chunks: list[str] = []

    try:
        async for event in client.stream(message):
            if event.kind == EVENT_TEXT_CHUNK:
                chunks.append(event.text)

            elif event.kind == EVENT_TOOL_CALL:
                # Don't post messages — broadcast status like chat page footer
                tool_name = event.text or ""
                tool_name, _ = redact_exfiltration_urls(tool_name)
                tool_name, _ = redact_credentials(tool_name)
                channel._broadcast(
                    "channel_agent_status",
                    {
                        "channel_id": channel.id,
                        "agent_id": agent.id,
                        "state": "tool_running",
                        "tool": tool_name,
                    },
                )

            elif event.kind == EVENT_PERMISSION_REQUEST:
                # Block direct-to-user messaging tools — channel agents
                # communicate via channel posts only. send_notification is
                # functionally equivalent for reaching the user (feed
                # publish, badge, sound), so it shares the
                # containment boundary.
                if _blocked_tool_named(event.text or event.title or ""):
                    sel().log_tool_invocation(
                        session_key=agent.session_key,
                        agent=agent.agent_name,
                        source="channel",
                        tool_name=event.text,
                        outcome="rejected_blocked_tool",
                    )
                    await client.reject_tool(event.request_id)
                    continue
                # YOLO mode (global) or channel trust — auto-approve
                if (is_yolo and is_yolo()) or channel.trusted:
                    sel().log_tool_invocation(
                        session_key=agent.session_key,
                        agent=agent.agent_name,
                        source="channel",
                        tool_name=event.text,
                        outcome=(
                            "auto_approved_yolo"
                            if (is_yolo and is_yolo())
                            else "auto_approved_channel_trust"
                        ),
                    )
                    await client.approve_tool(event.request_id)
                    continue
                # Per-command trust grants (trust_command / trust_base) — agent-
                # scoped patterns granted via the approve endpoint. Security:
                # grants are SHELL-ONLY and match against the ACTUAL command
                # extracted from tool_input, never the LLM-authored display
                # title; a tool the provider did not classify as shell is never
                # matched even when its arguments carry a nested "command" key
                # (e.g. cron_add), so a shell grant cannot leak onto MCP tools
                # (deny-by-default). Mirrors the dashboard chat slot's
                # trusted-pattern gate, hardened on the is_shell axis.
                _is_shell = bool(getattr(event, "is_shell", False))
                _cmd = (
                    extract_bash_command(event.tool_input) if _is_shell and event.tool_input else ""
                )
                if (agent._trusted_commands or agent._trusted_bases) and _cmd:
                    matched = _match_trusted_channel_command(_cmd, agent)
                    if matched:
                        # The grant names a PROGRAM; the shell resolves that
                        # name again through a PATH that can lead with
                        # agent-writable directories, and the file behind a
                        # trusted `./deploy.sh` can have been replaced since
                        # the human approved it. Same shared check as every
                        # other name-based tier (hook auto-approve, chat
                        # trusted patterns): a refusal does not reject — the
                        # request falls through to the interactive card below,
                        # where the human decides on this specific command.
                        # Check the COMMAND THE GRANT MATCHED (_cmd, from
                        # extract_bash_command) rather than re-deriving it
                        # from the event: event.shell_command returns None
                        # for raw non-JSON tool_input, and a None command
                        # would make the check vouch for nothing while the
                        # tier still auto-approves.
                        _ng_refusal = await name_grant.refusal_for_command_off_loop(_cmd)
                        if _ng_refusal is None:
                            sel().log_tool_invocation(
                                session_key=agent.session_key,
                                agent=agent.agent_name,
                                source="channel",
                                tool_name=event.text or event.title or "",
                                outcome="auto_approved_trusted_pattern",
                                metadata={"pattern": matched},
                            )
                            await client.approve_tool(event.request_id)
                            continue
                        name_grant.log_decline(
                            source="channel",
                            session_key=agent.session_key,
                            agent=agent.agent_name,
                            event=event,
                            refusal=_ng_refusal,
                            tier="channel_trusted_pattern",
                            sel_factory=sel,
                        )
                # Normal mode — interactive approval
                # Redact over the FULL input, then bound: cutting first can
                # split a credential at the boundary into fragments no
                # redaction regex matches, leaking it into the approval prompt.
                # tool_input is model-authored and size-unbounded, so the
                # full-text pass runs off-loop (no-blocking-call-on-event-loop).
                sanitized_input = await asyncio.to_thread(
                    redact_and_truncate, event.tool_input, 500
                )
                # The card's tool name. For a shell tool prefer the CANONICAL
                # command (from ``tool_input``) over the display title: kiro's
                # ``title`` for shell calls can be a model-authored prose
                # description, and the trust tiers derive their consent-proof
                # pattern from this name — a prose name would make the tiers
                # mismatch the real command and fail with
                # ``approval_superseded``. Non-shell tools keep the provider
                # name (``text`` then ``title`` — the same fallback the
                # blocked-tool check above uses; ACP permission events
                # populate only ``title``).
                # Use the same redactors on the command BYTES that would become
                # authority. ``tool_input_redacted`` is transport provenance:
                # re-running the scanners cannot reveal bytes an ACP transport
                # already removed. Either signal makes the command display-only.
                _safe_cmd, _cmd_credential_redacted = redact_credentials(_cmd)
                _safe_cmd, _cmd_url_redacted = redact_exfiltration_urls(_safe_cmd)
                _command_grantable = bool(_cmd) and not (
                    bool(getattr(event, "tool_input_redacted", False))
                    or _cmd_credential_redacted
                    or _cmd_url_redacted
                    or _safe_cmd != _cmd
                    or "[REDACTED" in _cmd
                )
                if _cmd:
                    # ``Running:`` is the channel UI's explicit proof that
                    # command-scoped tiers are available. Keep an ungrantable
                    # redacted command visible, but do not give it that marker
                    # or the card would offer decisions the server must refuse.
                    _card_name = (
                        f"Running: {_safe_cmd}"
                        if _command_grantable
                        else f"Shell command (allow once): {_safe_cmd}"
                    )
                else:
                    _card_name = event.text or event.title or ""
                sanitized_name, _ = redact_credentials(_card_name)
                sanitized_name, _ = redact_exfiltration_urls(sanitized_name)
                loop = asyncio.get_running_loop()
                # Bind-target for a per-command trust decision on THIS
                # approval: the canonical shell command ("" for non-shell
                # tools, which the tiers refuse — fail closed). A command the
                # provider redacted is also refused: two commands differing
                # only in their credentials redact to the SAME text, so a
                # grant scoped to the redacted form would silently cover
                # commands the user never consented to.
                approval_future = loop.create_future()
                agent._pending_approval_command = _cmd if _command_grantable else ""
                agent._approval_future = approval_future
                try:
                    # Posting and waiting are one ownership scope. If the post
                    # itself fails, neither the Future nor its command authority
                    # may leak onto the next approval handled by this agent.
                    await channel.post(
                        agent.id,
                        f"⚠️ Approval needed: **{sanitized_name}**\n```\n{sanitized_input}\n```",
                        from_role=agent.role,
                        msg_type="approval",
                        thread_id=thread_id,
                    )
                    decision = await asyncio.wait_for(approval_future, timeout=3600)
                except asyncio.TimeoutError:
                    decision = "rejected"
                finally:
                    if agent._approval_future is approval_future:
                        agent._approval_future = None
                        agent._pending_approval_command = ""

                if decision not in ("approved", "rejected", "trust"):
                    decision = "rejected"

                sel().log_tool_invocation(
                    session_key=agent.session_key,
                    agent=agent.agent_name,
                    source="channel",
                    tool_name=event.text,
                    outcome=decision,
                )

                if decision in ("approved", "trust") and _is_shell and _cmd:
                    # A human read this exact command and said yes: record the
                    # identity of the file behind each program name (same
                    # witness the chat slot pins), so a later per-command
                    # grant is honoured only while the same file answers to
                    # the name. Pin BEFORE releasing execution: approving
                    # first would let a self-replacing script swap the file
                    # and get the replacement pinned. Runs off-loop (stats +
                    # digests files).
                    await asyncio.to_thread(name_grant.pin_human_approval, _cmd)
                if decision == "trust":
                    channel.trusted = True
                    await client.approve_tool(event.request_id)
                elif decision == "approved":
                    await client.approve_tool(event.request_id)
                else:
                    await client.reject_tool(event.request_id)

            elif event.kind == EVENT_COMPLETE:
                break
    except Exception:
        logger.exception("LLM stream error for agent %s (%s)", agent.id, agent.role)
        await channel.post(
            agent.id,
            "❌ An error occurred while processing. Check logs for details.",
            from_role=agent.role,
            msg_type="system",
            thread_id=thread_id,
        )
        return

    full_text = "".join(chunks).strip()
    if not full_text:
        return
    # Sanitize LLM output before posting
    full_text, _ = redact_exfiltration_urls(full_text)
    full_text, _ = redact_credentials(full_text)
    # Extract @mentions from agent's response
    mention_ids = [
        m.id for m in channel.members.values() if m.id != agent.id and f"@{m.role}" in full_text
    ]
    await channel.post(
        agent.id,
        full_text,
        from_role=agent.role,
        msg_type="progress",
        thread_id=thread_id,
        mention=mention_ids or None,
    )
