"""Tool-name normalization and the guest tool gate.

Two gates in this package answer "may this session run this tool" by exact name
against a frozenset: the heartbeat gate in ``slack/gateway.py`` and the guest gate
here. They differ in what they are allowed to read.

The heartbeat gate reads the wire TITLE, because a heartbeat turn is the owner's
own unattended session and the title is all its edition-supplied allowlist can
match against. :func:`normalize_tool_title` is that path's one implementation, and
it carries a security property that must not be restated elsewhere -- an
edition-supplied allowlist entry matches only the server-QUALIFIED identity, so a
second MCP server exposing a destructive tool under an allowlisted bare name
cannot be approved by it.

The guest gate reads neither a title nor a normalized form of one. A guest turn is
driven by somebody who is not the owner, so it is authorized on the harness's own
``_meta`` identity alone -- see :func:`is_guest_safe_tool`.

Homed in its own module because ``slack/gateway.py`` imports ``slack/handler.py``,
so the two gates have no shared parent among themselves. This module imports only
``kiro_crew.hooks``, so either can read it.
"""

from __future__ import annotations

from kiro_crew.hooks import HookManager, HooksConfig

#: Status prefixes kiro-cli puts in front of a tool title on the wire.
_STATUS_PREFIXES = ("Running: ",)

#: The tools an allow-listed guest turn may run, matched by exact bare name.
#:
#: Deliberately NOT a subset of ``HEARTBEAT_SAFE_TOOLS``, and the difference is the
#: point. Heartbeat is the OWNER's own unattended session, so reading the owner's
#: files is in scope for it and ``Read`` / ``Grep`` / ``Glob`` are on its list. A
#: guest is a different person: a filesystem read is exactly what the guest posture
#: exists to refuse, and so is every tool reading owner state -- artifacts, memory,
#: crons, the knowledge base, AWS.
#:
#: One entry qualifies. ``web_search`` takes a QUERY: the destination is the search
#: provider whatever the guest writes, so a guest chooses words, not a host.
#:
#: ``web_fetch`` is excluded for the opposite reason, and its exclusion is the test
#: a new entry has to pass. It takes a URL, so a guest chooses the host, and the
#: response comes back into the channel -- a read primitive pointed at whatever the
#: owner's machine can reach, including link-local metadata endpoints and services
#: bound to loopback. Nothing in this package guards that builtin against those
#: destinations. So an entry needs all four: it reads no owner data, writes nothing,
#: runs no command on the host, AND does not let the guest choose what it talks to.
GUEST_SAFE_TOOLS = frozenset(
    {
        "web_search",
    }
)


def normalize_tool_title(event_title: str) -> tuple[str, str]:
    """Return ``(bare_name, qualified_name)`` for a wire tool title.

    ``bare_name`` is what an exact-name allowlist is tested against.
    ``qualified_name`` is the ``@server/Tool`` identity when the title carries a
    resolvable MCP server, normalized from either wire spelling
    (``mcp__server__Tool`` or ``@server/Tool``), and ``""`` when it does not.

    A caller matching an edition-contributed allowlist must use
    ``qualified_name`` and must treat an empty one as no match: a bare-name entry
    would let any server's same-named tool through.

    Steps: strip the status prefix, capture the qualified identity, then strip the
    server prefix to leave the bare name. An empty or whitespace-only title
    normalizes to ``("", "")``, which no allowlist contains.
    """
    if not event_title:
        return "", ""
    name = event_title.strip()
    if not name:
        return "", ""
    for prefix in _STATUS_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    qualified = ""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            qualified = f"@{parts[1]}/{parts[2]}"
            name = parts[2]
    elif name.startswith("@") and "/" in name:
        qualified = name
        name = name.rsplit("/", 1)[-1]
    return name, qualified


def is_guest_safe_tool(tool_name: str, mcp_server_name: str) -> bool:
    """Return True if a guest may run the tool identified by *tool_name*.

    Both arguments come from ``AcpEvent`` fields the HARNESS authors under
    ``_meta``, never from the model: ``tool_name`` is the canonical tool identity
    and ``mcp_server_name`` is set only for an MCP-served call. The event's
    ``title`` is deliberately NOT accepted here. ``title`` is LLM-authored prose
    -- ``select_tool_title`` even prefers a shell call's own ``rawInput``
    description -- so a crafted title is model-controlled text, and a gate keyed
    on it authorizes whatever the model says it is doing.

    Three refusals, all fail-closed:

    * An empty *tool_name* denies. The harness carries a name for every tool,
      built-ins included, so absence means the identity could not be verified,
      and an unverified call is not a safe one. There is no fallback to *title*:
      a fallback is the whole hole, since a frame can omit ``_meta`` and supply
      any title it likes.
    * A non-empty *mcp_server_name* denies. The guest spec mounts no MCP servers
      at all, so an MCP-served call on a guest turn is already outside the
      posture, and a bare-name match would let ANY server's same-named tool
      through -- the risk :func:`normalize_tool_title` documents for the
      qualified-identity path.
    * Anything else is matched by exact string against :data:`GUEST_SAFE_TOOLS`,
      with no normalization, no verb-based fallback and no edition extension
      seam. A guest message is untrusted text from somebody who is not the owner,
      so a read-shaped name a prompt injection invented (``list_env_secrets``,
      ``get_all_credentials``) must not widen the set.
    """
    if mcp_server_name:
        return False
    return bool(tool_name) and tool_name in GUEST_SAFE_TOOLS


def build_guest_hooks(user_hooks: HookManager) -> HookManager:
    """Return a HookManager scoped for a guest turn.

    The owner's ``auto_approve_tools`` (``*``, ``Write*``, …) must never widen what
    a guest runs. ``llm_helpers._resolve_permission`` and the native Slack turn
    loop both consult ``hooks.on_tool_call()`` BEFORE the guest gate, so a
    user-config auto-approve reaching that check would approve a tool the guest
    gate exists to refuse. Dropping the list makes that check unable to grant.

    Kept: the sensitive-path deny (structural, not user config) and the owner's
    ``auto_deny_tools`` plus denied-command state, because a deny can only narrow
    what a guest reaches.

    Dropped: ``auto_approve_tools``, and the chat-only ``auto_replies`` /
    ``transforms`` / ``context_rules`` -- a guest must not trigger the owner's
    canned replies or have its text rewritten by the owner's rules.
    """
    user_cfg = user_hooks._config  # noqa: SLF001 — internal hooks state by design
    scoped = HooksConfig(
        auto_approve_tools=[],
        auto_deny_tools=list(user_cfg.auto_deny_tools),
        denied_commands_disabled_ids=list(user_cfg.denied_commands_disabled_ids),
        denied_commands_disable_all=user_cfg.denied_commands_disable_all,
        denied_commands_user_added=list(user_cfg.denied_commands_user_added),
    )
    return HookManager(scoped)
