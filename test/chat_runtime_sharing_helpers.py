"""Placement fakes shared by ``test_chat_runtime_sharing.py`` and its ``_cleanup`` sibling."""

from __future__ import annotations

from kiro_crew.acp_backends import ACP_BACKEND_KIRO


class _PlacementClient:
    backend = ACP_BACKEND_KIRO


class _PlacementProvider:
    """The ``AcpProvider`` fields ``ChatSharePlacement`` reads."""

    def __init__(self, era: str = "acct-A", reader=None, backend: str = ACP_BACKEND_KIRO) -> None:
        self._client = _PlacementClient()
        self._client.backend = backend
        self.memory_mode = "persistent"
        self.member_context = False
        self._shared_scratch = None
        self._harness_pin = ""
        self.pre_spawn_identity = era
        self.spawn_identity_reader = reader

    def _tool_search_settings(self):
        return None

    def _resolve_effort(self):
        return None

    def _owning_session_key(self) -> str:
        return "dashboard:chat-1-1790000000"


def make_placement(era: str = "acct-A", provider=None, **overrides):
    from kiro_crew.providers.chat_share import ChatSharePlacement

    args = dict(
        work_dir="/home/u/.kirocrew/workspace",
        agent="kirocrew",
        sandbox_mode="auto",
        extra_env={},
        mcp_gateway_overlay=None,
        mcp_gateway_socket=None,
        configured_model="",
        launch_agent="",
        session_key="dashboard:chat-1-1790000000",
        cap=10,
        spec_generation="gen-1",
        sandbox_tier="auto",
        forward_ssh=False,
    )
    args.update(overrides)
    return ChatSharePlacement(provider or _PlacementProvider(era=era), **args)


def confirming(*answers):
    """A ``_read_confirmation`` stand-in answering *answers* in turn, then the last forever."""
    pending = list(answers)

    async def read():
        return pending.pop(0) if len(pending) > 1 else pending[0]

    return read
