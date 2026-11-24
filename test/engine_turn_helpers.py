"""A KAS session as the engine sub-agent page tests need it: what it registered, and a turn on it.

The card code decides "this chat's sub-agents run in the engine" from the session's
own record of what it mounted (``AcpSessionHandle.kas_shipped_helper_parent`` beside
``kas_projected_agent``), read off the client the turn publishes on its slot
(``slot._acp_client``). ``registered`` is that record alone; ``session`` is a
``turn_harness`` provider whose live client holds a handle, as the ACP session
provider holds its own.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from turn_harness import ScriptedProvider

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

#: The shipped default agent, the one the engine's sub-agent tool is mounted for.
DEFAULT_AGENT = "kirocrew"


def registered(mounted_for: str = DEFAULT_AGENT, agent: str = DEFAULT_AGENT) -> Any:
    """The two fields of a session handle the card code reads: the agent the session
    runs now, and the agent its registration mounted Kiro Crew's helper for."""
    return SimpleNamespace(kas_projected_agent=agent, kas_shipped_helper_parent=mounted_for)


def client(handle: Any, backend: str = ACP_BACKEND_KAS) -> Any:
    """The live client a turn publishes on its slot, holding *handle*."""
    return SimpleNamespace(backend=backend, _handle=handle)


def session(handle: Any) -> type[ScriptedProvider]:
    """A scripted provider whose client holds *handle*."""

    class _Session(ScriptedProvider):
        @property
        def client(self) -> Any:
            return client(handle)

    return _Session


def live_reading_says_kas(monkeypatch: Any) -> None:
    """Make the preview's live reading see a KAS session, so the config alone decides it.

    The card code does not take that reading: a test sets it to show the cards follow
    the session's registration when the two disagree.
    """
    monkeypatch.setattr("kiro_crew.agent_discovery.served_provider_label", lambda provider: "kas")
