"""The session-start refusal for a host that cannot run Kiro Crew's own KAS engine."""

from __future__ import annotations

from kiro_crew.acp.session_handle import AcpRuntimeError


class AcpKasHostUnsupported(AcpRuntimeError):
    """The bundle route was taken on a host the engine cannot run on.

    Raised by ``KasHarness.resolve_spawn`` before any process exists when the
    install refuses with ``KasHostUnsupported`` (no build for the OS or
    architecture, or glibc below the floor). ``transient = False`` is read
    structurally by ``llm_helpers.acp_error_is_transient``: the host is the same
    on the next attempt, so a retry never succeeds, and the verdict does not
    depend on the message's wording.
    """

    transient = False
