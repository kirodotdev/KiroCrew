"""Per-host strategy objects for the shared-process ACP runtime.

``AcpRuntime`` hosts many sessions in one child process. What that child IS --
kiro-cli, the KAS relay, or a backend added later -- is answered here, in one
file per host, rather than by a backend test at each point of difference. There
are twelve such points, and a host that answers eleven of them is a host that
starts and then behaves like a different one.

Start at :mod:`kiro_crew.acp.harness.base`: it names every seam and says what each
one is for. :func:`harness_for` is how the runtime gets the right harness, and it
REFUSES a backend with no harness rather than serving it as kiro-cli.
"""

from __future__ import annotations

from kiro_crew.acp.harness.base import (
    HarnessAdapter,
    NotificationAliases,
    ReclaimPolicy,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.harness.kiro import KiroHarness
from kiro_crew.acp.types import ACP_BACKEND_CODEX, ACP_BACKEND_KAS, ACP_BACKEND_KIRO

__all__ = [
    "CodexHarness",
    "HarnessAdapter",
    "KasHarness",
    "KiroHarness",
    "NotificationAliases",
    "ReclaimPolicy",
    "SessionExtras",
    "SpawnContext",
    "SpawnPlan",
    "TeardownPolicy",
    "harness_for",
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
