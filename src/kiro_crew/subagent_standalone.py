"""Sub-agent readers a process may call before it has loaded ``kiro_crew.subagent``.

A stdio tool server answers ``tools/list`` before it loads the code that runs its tools,
and two of the spawn tools' descriptions carry a live value: the sub-agent ceiling and the
agents the calling parent may spawn. The two owners that compute them
(``subagent_parts.host_memory`` and ``subagent_parts.spawn_allowlists``) import for real
every name they read, so they work before the facade is loaded.

Once ``kiro_crew.subagent`` is loaded, ``_facade_compose.compose`` has replaced each owner
function with the copy that runs on the facade's globals. A module that bound an owner
function before that load would keep the replaced object, and a family that must hold one
object per name (``kiro_crew.mcp_tools.spawn``) would then hold two. So each function here
calls the owner's binding as it is at the time of the call: one object serves both sides of
the load, and a patch of ``kiro_crew.subagent.<name>`` reaches it as soon as the facade
exists. This module and the facade are the only importers of those two owners.

The spawn and host-headroom tools (``resource_status``, ``spawn_*``) also run in the tool
server, which never runs a sub-agent itself, so they read the host-memory, start-price,
usage-line and refusal-code names through the readers below too, not through the facade
(the whole manager: about 30 MB of a tool server's memory). Those readers call
``kiro_crew.subagent``'s binding once it is loaded, as the callers' call-time
``from kiro_crew.subagent import`` did, so a test that patches the facade still reaches
them; until then, the owner's. ``start_pricing`` and ``run_output`` are imported on first
use, so the listing path does not load them; only the functions read here run on their own
globals (``STANDALONE_READS``). The refusal codes are plain strings ``agent_policy`` defines;
the spawn tools bind them once, when their handlers load, as they did from the facade.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from types import ModuleType
from typing import TYPE_CHECKING, Any

from kiro_crew.subagent_parts import host_memory, spawn_allowlists

#: Each owner the readers below call before the facade is loaded, and the functions of it
#: that then run on the owner's own globals (``test_subagent_standalone`` checks they bind
#: every global they read).
STANDALONE_READS = {
    "start_pricing": (
        "_dedicated_start_price_gb",
        "_shared_start_price_gb",
        "_spawn_memory_floor_and_cost",
    ),
    "run_output": ("format_subagent_usage", "_finite_nonnegative_number"),
}

_FACADE = "kiro_crew.subagent"
_UNSET = object()

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig


def resolve_max_subagents(cfg: KiroCrewConfig) -> int:
    """The sub-agent count ceiling for *cfg* (``subagent.resolve_max_subagents``)."""
    return host_memory.resolve_max_subagents(cfg)


def visible_agent_names(names: Iterable[str], **options: Any) -> tuple[list[str], int]:
    """A roster made safe to render and bounded (``subagent.visible_agent_names``)."""
    return spawn_allowlists.visible_agent_names(names, **options)


def parent_spawn_allowlists(
    parent_template: str, *args: Any, **options: Any
) -> tuple[tuple[str, ...], ...] | None:
    """Every ``availableAgents`` list a parent's specs declare
    (``subagent.parent_spawn_allowlists``)."""
    return spawn_allowlists.parent_spawn_allowlists(parent_template, *args, **options)


def agent_matches_allowlist(agent: str, allowlist: Iterable[str], **options: Any) -> bool:
    """Whether *agent* satisfies one allowlist (``subagent.agent_matches_allowlist``)."""
    return spawn_allowlists.agent_matches_allowlist(agent, allowlist, **options)


def _owner(short: str) -> ModuleType:
    """The ``subagent_parts`` owner *short*, imported on first use."""
    if short == "start_pricing":
        from kiro_crew.subagent_parts import start_pricing

        return start_pricing
    from kiro_crew.subagent_parts import run_output

    return run_output


def _facade_first(owner: ModuleType, name: str) -> Any:
    """``kiro_crew.subagent``'s binding of *name* once it is loaded, else *owner*'s."""
    facade = sys.modules.get(_FACADE)
    value = _UNSET if facade is None else getattr(facade, name, _UNSET)
    return getattr(owner, name) if value is _UNSET else value


def agent_refusal_codes() -> tuple[str, str, str]:
    """The spawn refusal wire codes: internal, not available, not found
    (``subagent.AGENT_INTERNAL_CODE``, ``AGENT_NOT_AVAILABLE_CODE``, ``AGENT_NOT_FOUND_CODE``)."""
    from kiro_crew.subagent_parts import agent_policy

    return (
        agent_policy.AGENT_INTERNAL_CODE,
        agent_policy.AGENT_NOT_AVAILABLE_CODE,
        agent_policy.AGENT_NOT_FOUND_CODE,
    )


def memory_cause_cgroup_usage_unreadable() -> str:
    """The cause a host reading records for an unreadable cgroup usage file
    (``subagent.MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE``)."""
    return _facade_first(host_memory, "MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE")


def remote_cost_bucket_suffix() -> str:
    """The suffix on a remote run's cost bucket (``subagent.REMOTE_COST_BUCKET_SUFFIX``)."""
    return _facade_first(_owner("start_pricing"), "REMOTE_COST_BUCKET_SUFFIX")


def _available_memory_gb(**options: Any) -> float:
    """Available host memory in GB (``subagent._available_memory_gb``)."""
    return _facade_first(host_memory, "_available_memory_gb")(**options)


def out_of_view_memory_pressure_file() -> Any:
    """The out-of-view memory cgroup file, or None (``subagent.out_of_view_memory_pressure_file``)."""
    return _facade_first(host_memory, "out_of_view_memory_pressure_file")()


def pop_memory_check_cause() -> str:
    """The cause the last host reading on this thread recorded (``subagent.pop_memory_check_cause``)."""
    return _facade_first(host_memory, "pop_memory_check_cause")()


def check_memory_available(**options: Any) -> tuple[bool, float]:
    """The spawn gate's host reading (``subagent.check_memory_available``)."""
    return _facade_first(host_memory, "check_memory_available")(**options)


def _agents_slice_available_gb() -> float:
    """The agents slice's memory headroom in GB (``subagent._agents_slice_available_gb``)."""
    return _facade_first(host_memory, "_agents_slice_available_gb")()


def _container_cgroup_available_gb(**options: Any) -> float:
    """The container cgroup's memory headroom in GB (``subagent._container_cgroup_available_gb``)."""
    return _facade_first(host_memory, "_container_cgroup_available_gb")(**options)


def _dedicated_start_price_gb(*args: Any, **options: Any) -> float:
    """What a dedicated start is reserved at (``subagent._dedicated_start_price_gb``)."""
    return _facade_first(_owner("start_pricing"), "_dedicated_start_price_gb")(*args, **options)


def _shared_start_price_gb(dedicated_gb: float) -> float:
    """What a shared start is reserved at (``subagent._shared_start_price_gb``)."""
    return _facade_first(_owner("start_pricing"), "_shared_start_price_gb")(dedicated_gb)


def _spawn_memory_floor_and_cost(*args: Any, **options: Any) -> tuple[float, float]:
    """The spawn floor and per-start cost (``subagent._spawn_memory_floor_and_cost``)."""
    return _facade_first(_owner("start_pricing"), "_spawn_memory_floor_and_cost")(*args, **options)


def format_subagent_usage(credits: object, elapsed: object) -> str:
    """A run's usage line (``subagent.format_subagent_usage``)."""
    return _facade_first(_owner("run_output"), "format_subagent_usage")(credits, elapsed)
