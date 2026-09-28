"""Channel registry: descriptors + the boot/shutdown loops.

One registry entry per channel carries the host's per-channel LIFECYCLE seams —
the members tuple, the start call, the shutdown gather. The other per-channel
seams (the ``orch._<channel>_*`` hoist in ``slack/gateway.py``, the ``loader.py``
config dataclass, the ``sandbox.py`` credential denylist, the
``dashboard/state.py`` connected fields, the uncredentialed-skip probe table in
``_start_channel_transports``) are still hand-edited.

This module owns the TYPES and the LOOPS
only — it must not import any channel package (``dispatch.py`` pins the
dependency direction: ``<channel> -> messaging``, never the reverse). The one
place that knows every builtin channel is :mod:`kiro_crew.channels`, which sits
ABOVE both this package and the channel packages.

Two views over the same descriptor list, because Slack is deliberately split:

* :func:`governed_members` — ALL channels, including Slack. Governance
  membership (the ``channels`` scope) covers every transport.
* the boot loop (:func:`start_channels`) — only descriptors with a ``start``
  factory. Slack's is ``None`` because it owns its own socket-client lifecycle
  (a governance deny must DROP that client, not merely skip a start call — see
  ``_connect_slack``), so the host starts it separately, after the others,
  preserving today's boot order.

Descriptor honesty rule: a field exists here ONLY if something consumes it.
Config schemas, credential field names for the sandbox denylist, and capability
attachment are REAL parts of the RFC's descriptor design, but each lands here
only TOGETHER WITH its consumer, never ahead of it as a decorative field nothing
reads.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

#: A channel start factory: takes the gateway orchestrator, returns the live
#: client handle (or ``None`` when the channel is disabled/uncredentialed —
#: every factory is a guarded no-op). The orchestrator is typed as a loose
#: ``Any`` on purpose: the factories predate the registry and read pre-hoisted
#: ``orch._<channel>_*`` attributes, so narrowing this to a
#: ``ChannelBootContext`` has to wait until that back-reference goes — naming a
#: type over the current shape would freeze the wrong contract.
StartFactory = Callable[[Any], Awaitable[Any]]


@dataclass(frozen=True)
class ChannelDescriptor:
    """Everything the host needs to know about one channel, in one place.

    ``channel_type`` is the single identity used EVERYWHERE: the governance
    member id, ``MessagingTransport.channel_type``, the session-key surface
    segment, the config section name, and the dashboard badge prefix. The
    contract tests pin that these never diverge.
    """

    channel_type: str
    """Identity. Must equal the transport class's ``channel_type``."""

    start: Optional[StartFactory] = None
    """Boot factory, or ``None`` for a host-managed lifecycle (Slack)."""

    credentials: tuple[str, ...] = ()
    #: ``credential key -> config attribute`` for the channels that accept the
    #: secret from ``config.json`` as well as the environment. Readiness has to
    #: consult BOTH, or it reports a missing credential for a channel the gateway
    #: starts fine — the env var is the RECOMMENDED home, not the only one.
    #: Absent means env-only by design (Teams' ``app_password`` is deliberately
    #: never read from a file the agent can see).
    credential_fallbacks: tuple[tuple[str, str], ...] = ()
    #: Config attributes that must be non-empty for the channel to START, beyond
    #: its credentials. Deliberately separate from ``credentials``: these are not
    #: secrets and have no credential-store key, so folding them in would make
    #: ``missing_credentials`` name something that is not a credential and send the
    #: operator looking in the wrong place. WeCom/WeChat's ``account_id`` is the
    #: case — its gateway refuses to start without one.
    required_config: tuple[str, ...] = ()
    #: Config keys of this channel's section that are CONNECTION parameters:
    #: the enable flag, the token or app identity the transport authenticates
    #: with, the endpoint it connects to, the store it opens. A live transport
    #: cannot adopt a new one of these, so a config change touching any of them
    #: is applied by restarting the channel in-process
    #: (``GatewayOrchestrator.restart_channel``). Every OTHER key of the section
    #: (allow-lists, thresholds, render toggles) is pushed onto the running
    #: transport or read at point of use, so a change there must NOT restart the
    #: channel -- the restart applier compares the changed paths against this
    #: set and nothing else. Empty for a host-managed lifecycle (Slack).
    #: The other half of "what happens when ``<channel>.*`` changes" is that
    #: channel's ``transport.reconfigure(section)`` (its allow-list and live
    #: fields), registered from its ``transport_dispatch.py`` via
    #: ``live.watch_section``; the two are disjoint by construction and
    #: ``test_channel_boot_keys_contract.py`` pins that.
    boot_keys: frozenset[str] = frozenset()
    """Credential keys this channel needs ALL of before it can connect.

    Data rather than a per-channel branch, so a diagnostic can report every
    channel's readiness from one loop. Empty means the channel needs no
    credential at all — iMessage is the real case: its transport is the
    operator's own Messages.app, so there is nothing to store or rotate, and an
    empty tuple must read as "nothing missing" rather than "not configured".
    """


def governed_members(descriptors: tuple[ChannelDescriptor, ...]) -> tuple[str, ...]:
    """Every channel's governance member id — INCLUDING host-managed ones."""
    return tuple(d.channel_type for d in descriptors)


def bootable(descriptors: tuple[ChannelDescriptor, ...]) -> tuple[ChannelDescriptor, ...]:
    """The descriptors the registry boot loop starts (``start`` is not None)."""
    return tuple(d for d in descriptors if d.start is not None)


def changed_boot_keys(
    desc: ChannelDescriptor, changed_paths: "frozenset[str] | set[str]"
) -> frozenset[str]:
    """The ``boot_keys`` of *desc* that *changed_paths* touches.

    *changed_paths* are dotted leaf paths from a config diff
    (``telegram.bot_token``, ``telegram.accounts.main.token``); a key is
    matched on the FIRST segment under the section, so a nested boot key
    (``accounts``) counts however deep the leaf sits. Paths outside the
    section are ignored, so a caller may hand over the whole change set.
    Empty means "nothing that needs a restart changed" -- the answer the
    live-field appliers rely on so their fields never bounce a connection.
    """
    prefix = desc.channel_type + "."
    keys = {p[len(prefix) :].split(".", 1)[0] for p in changed_paths if p.startswith(prefix)}
    return frozenset(keys & desc.boot_keys)


async def start_channels(
    orch: Any,
    descriptors: tuple[ChannelDescriptor, ...],
    permitted: Mapping[str, bool],
    *,
    before_start: Callable[[ChannelDescriptor], None] | None = None,
) -> dict[str, Any]:
    """Start every bootable, governance-permitted channel; return live handles.

    ``permitted`` is computed by the CALLER (off the event loop — the
    governance check does blocking profile-file I/O) and a member absent from
    it counts as not-permitted, preserving the enabled-only-eval semantics:
    a disabled channel is never evaluated, never starts, and never emits a
    spurious deny audit.

    ``before_start`` runs synchronously immediately ahead of each channel's
    factory -- the gateway re-hoists that channel from the config watcher's
    current snapshot there. Channels start one after another and a connect can
    take seconds, so a hook that ran once for all of them would leave the later
    channels building from a document the earlier ones' connects had let go
    stale. It is synchronous so nothing can be dispatched between the read and
    the factory's own subscription.

    Failure isolation is per-channel BY the factories themselves (each
    ``maybe_start_*`` catches, badges the error, and returns ``None``), so a
    raise escaping one factory here is unexpected; it is logged and the
    remaining channels still start rather than aborting the block. The hook
    sits inside the same isolation.
    """
    handles: dict[str, Any] = {}
    for desc in bootable(descriptors):
        if not permitted.get(desc.channel_type, False):
            continue
        assert desc.start is not None  # bootable() guarantees this
        try:
            if before_start is not None:
                before_start(desc)
            client = await desc.start(orch)
        except Exception:
            logger.exception("channel registry: %s failed to start", desc.channel_type)
            client = None
        # Legacy attribute kept in sync for existing readers/tests; the returned
        # handles dict is the authority.
        setattr(orch, f"_{desc.channel_type}_client", client)
        if client is not None:
            handles[desc.channel_type] = client
    return handles


def shutdown_tasks(handles: Mapping[str, Any], *, timeout: float = 2.0) -> list[Awaitable[Any]]:
    """Bounded ``close()`` awaitables for every live handle, in one place.

    Mirrors the shape the gateway's cleanup gather expects. Handles without a
    ``close`` attribute are skipped with a warning rather than raising — a
    shutdown path must never be the thing that fails shutdown.
    """
    tasks: list[Awaitable[Any]] = []
    for channel_type, client in handles.items():
        close = getattr(client, "close", None)
        if close is None:
            logger.warning("channel registry: %s client has no close()", channel_type)
            continue
        tasks.append(asyncio.wait_for(close(), timeout=timeout))
    return tasks
