"""Boot-time registration of monitored kinds a downstream edition contributes.

The built-in kinds are fixed in :mod:`kiro_crew.monitoring.registry`. An edition
that knows about a review system the public core does not (an internal code
review host, say) registers its kind here, once, at platform boot, and every
boundary that gates a kind reads it from then on: the ``monitor_watch`` schema
and validator, target inference and normalization, and the controller's provider
table. The pattern is the source-provider seam's
(``dashboard/source_providers/plugins.register_source_provider``).

A contributed kind is **pull-request shaped**. Its provider returns the same
:class:`~kiro_crew.monitoring.pull_request.PullRequestProbeResult` the built-in
review providers do, so the decision, fingerprint, condition and wake paths need
nothing new. That is also why its objectives are limited to
:data:`~kiro_crew.monitoring.registry.PLUGIN_KIND_OBJECTIVES`.

Three things a registration deliberately does NOT grant:

* **No owner credentials from a channel.** A channel-created monitor on a
  contributed kind probes without the owner's credentials, the fail-closed
  posture every non-GitHub/GitLab built-in has. A dashboard-created monitor gets
  them through the same recorded authorization the built-ins use.
* **No pacing from the core.** The controller's cadence floor still applies, but
  a provider that fronts a backend which cannot take that rate must pace itself
  inside :meth:`probe`; the seam has no way to know a backend's limits.
* **No replacement.** A built-in name or a second registration of the same name
  raises, loudly, rather than shadowing a provider somebody chose.
* **No shadow path.** The persistence-only probe path calls a provider without
  the owner-credential gate, so a contributed kind may not declare it.

A factory that raises when the controller is built fails only its own kind: it
is logged, and that kind's monitors stop as blocked on their first tick. A
target parser that raises anything other than ``ValueError`` is read as "not
this kind's URL".

Where to call it: from the edition's platform-context builder (the
``kirocrew.plugins`` entry point ``boot_platform`` runs), and unconditionally.
That builder runs in every process that boots the platform, which matters
because the processes do not share the table: ``monitor_watch`` is validated in
the MCP server process, the controller probes in the gateway, and a kind
registered in only one of them is accepted by one and refused by the other. It
must also run before the gateway constructs its
:class:`~kiro_crew.monitoring.controller.MonitorController`, which reads the
table once, when it is built.

Two consequences of a kind being contributed rather than built in:

* **A boot without the registration stops that kind's monitors.** A persisted
  monitor whose kind is not registered on the next boot takes the controller's
  ``provider_unsupported`` path, which stops it as blocked, exactly as an
  unknown kind does today. Registering conditionally (behind a setting, say)
  therefore retires every monitor of the kind the first boot the condition is
  false; gate the provider's behaviour instead of the registration.
* **The dashboard does not recognise it.** The dashboard's kind vocabulary is
  the checked-in ``website/src/monitoring/contract.json`` snapshot of the
  built-ins, so a contributed-kind monitor is shown as blocked even while it
  probes, and its save and restart controls are disabled. Stop works.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from kiro_crew.monitoring.models import MonitorProbeProvider
from kiro_crew.monitoring.registry import MonitorKind, _add_plugin_kind

_KIND_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


@dataclass(frozen=True)
class MonitorKindPlugin:
    """One contributed kind: its registry entry, its provider and its target parser."""

    #: The registry entry. Its ``name`` is the ``kind`` a caller passes.
    kind: MonitorKind
    #: Builds the provider. A factory rather than an instance so a gateway that
    #: never builds a controller never constructs a client.
    provider_factory: Callable[[], MonitorProbeProvider]
    #: Return the canonical target URL for *raw*, or raise ``ValueError``. Must be
    #: strict: inference tries every registered parser, so one that accepts a URL
    #: it does not own claims another provider's subject. The core refuses a
    #: returned target that is not ``https`` with a host, and applies no host
    #: backstop: pinning the host, and refusing ports, queries and fragments,
    #: is the parser's job, since the provider probes whatever it returns.
    parse_target: Callable[[str], str]


_PLUGINS: dict[str, MonitorKindPlugin] = {}


def register_monitor_kind(plugin: MonitorKindPlugin) -> None:
    """Register a contributed monitored kind. Call once, at platform boot.

    Raises ``ValueError`` for a malformed name, a built-in or duplicate name,
    an objective a contributed kind cannot support, or a missing callable.
    """
    if not isinstance(plugin, MonitorKindPlugin):
        raise ValueError("register_monitor_kind takes a MonitorKindPlugin")
    name = plugin.kind.name
    if not isinstance(name, str) or _KIND_NAME_RE.fullmatch(name) is None:
        raise ValueError(f"monitor kind name {name!r} must be lowercase snake_case")
    if not callable(plugin.provider_factory) or not callable(plugin.parse_target):
        raise ValueError(f"monitor kind {name!r} needs a provider factory and a target parser")
    if name in _PLUGINS:
        raise ValueError(f"monitor kind {name!r} is already registered")
    _add_plugin_kind(plugin.kind)
    _PLUGINS[name] = plugin


def registered_monitor_kind(name: object) -> MonitorKindPlugin | None:
    """The plugin registered under *name*, or None."""
    return _PLUGINS.get(name) if isinstance(name, str) else None


def registered_monitor_kinds() -> tuple[MonitorKindPlugin, ...]:
    """Every contributed kind, in registration order."""
    return tuple(_PLUGINS.values())
