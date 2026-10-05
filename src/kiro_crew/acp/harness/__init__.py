"""Per-host strategy objects for both ACP drivers.

``AcpRuntime`` hosts many sessions in one child process. What that child IS --
kiro-cli, the KAS relay, or a backend added later -- is answered here, in one
file per host, rather than by a backend test at each point of difference. There
are twelve such points, and a host that answers eleven of them is a host that
starts and then behaves like a different one.

``AcpClient`` starts one process per session, and the hosts it launches that way
(claude-agent-acp, OpenCode, goose, pi, the DeepSeek Harness) each answer the spawn
half of that contract in their own file too -- binary, argv, labels, masks, gates
and the child's environment -- so adding or fixing one is one adapter file.

Start at :mod:`kiro_crew.acp.harness.base`: it names every seam and says what each
one is for. :func:`harness_for` is how the runtime gets the right harness, and it
REFUSES a backend with no harness rather than serving it as kiro-cli.
:func:`process_adapter_for` is the client's lookup for its per-process hosts.
"""

from __future__ import annotations

from kiro_crew.acp.harness.base import (
    HarnessAdapter,
    LaunchAdapter,
    NotificationAliases,
    ProcessAdapter,
    ProcessSession,
    ReclaimPolicy,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.harness.claude import ClaudeLaunch
from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.acp.harness.deepseek import DeepseekLaunch
from kiro_crew.acp.harness.goose import GooseLaunch
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.harness.kiro import KiroHarness
from kiro_crew.acp.harness.opencode import OpencodeLaunch
from kiro_crew.acp.harness.pi import PiLaunch
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)

__all__ = [
    "ClaudeLaunch",
    "CodexHarness",
    "DeepseekLaunch",
    "GooseLaunch",
    "HarnessAdapter",
    "KasHarness",
    "KiroHarness",
    "LaunchAdapter",
    "NotificationAliases",
    "OpencodeLaunch",
    "PiLaunch",
    "ProcessAdapter",
    "ProcessSession",
    "ReclaimPolicy",
    "SessionExtras",
    "SpawnContext",
    "SpawnPlan",
    "TeardownPolicy",
    "harness_for",
    "process_adapter_for",
]

_HARNESSES: dict[str, type[HarnessAdapter]] = {
    ACP_BACKEND_KIRO: KiroHarness,
    ACP_BACKEND_KAS: KasHarness,
    # This table answers "can the shared-process runtime drive this host?", and
    # ``ACP_BACKENDS_ACP_RUNTIME`` answers "does a session take that path?". They
    # agree for every member here, and they are still separate questions: a harness
    # is written and tested before it is routed, so the table has to be reachable
    # while the set does not yet name it. Gating registration on the set would make
    # a harness unreachable to its own tests, and would leave the runtime resolving
    # one that exists on disk but not in the table.
    ACP_BACKEND_CODEX: CodexHarness,
}


def harness_for(backend: str) -> HarnessAdapter:
    """The harness for ``backend``.

    Raises ``ValueError`` for a backend with no harness. Failing here is the
    point: a backend the shared-process runtime has no harness for would
    otherwise silently inherit kiro-cli's spawn argv, protocol version and
    teardown verb, and the first sign of it would be a session that starts and
    then behaves wrongly.
    """
    try:
        return _HARNESSES[backend]()
    except KeyError:
        raise ValueError(
            f"no ACP harness for backend {backend!r}; "
            f"the shared-process runtime serves {sorted(_HARNESSES)}"
        ) from None


#: The hosts ``AcpClient`` launches one process per session for, each by its own
#: adapter. kiro-cli is not here: the client spawns it through its own explicit arm,
#: which keeps the Kiro construction path the client always had (harness-parity H13).
_PROCESS_ADAPTERS: dict[str, type[ProcessAdapter]] = {
    ACP_BACKEND_CLAUDE: ClaudeLaunch,
    ACP_BACKEND_OPENCODE: OpencodeLaunch,
    ACP_BACKEND_GOOSE: GooseLaunch,
    ACP_BACKEND_PI: PiLaunch,
    ACP_BACKEND_DEEPSEEK: DeepseekLaunch,
}


def process_adapter_for(backend: str) -> ProcessAdapter | None:
    """A fresh per-process adapter for *backend*, or ``None`` for the client's Kiro arm.

    Fresh per call because an adapter carries one launch's facts from its plan to
    the child's environment. ``None`` for every id ``AcpClient`` spawns through its
    own kiro-cli arm -- kiro-cli itself, and the runtime-served hosts when a client
    is asked to launch one -- exactly the set that reached that arm before.
    """
    adapter = _PROCESS_ADAPTERS.get(backend)
    return adapter() if adapter is not None else None
