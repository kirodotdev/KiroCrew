"""The hard-stop abort address resolves through the started provider's client.

After ``_start_kiro_runtime_impl`` the ``AcpProvider``'s ``_client`` is an
``AcpSessionProvider``. ``LLMProvider.runtime_abort_target`` reads ``_pid`` and
``_mcp_gateway_socket`` off that client, so both must reach the runtime, or a
hard stop skips the abort push and in-flight pooled tool calls keep running.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.mcp_gateway.abort import RuntimeAbortTarget
from kiro_crew.providers.acp import AcpProvider


def _started_provider(runtime: MagicMock) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend="")
    provider._client = AcpSessionProvider(MagicMock(), runtime)  # type: ignore[assignment]
    return provider


def _runtime(pid: int, socket_path: str | None) -> MagicMock:
    runtime = MagicMock(spec=AcpRuntime)
    runtime.pid = pid
    if socket_path is not None:
        runtime._mcp_gateway_socket = socket_path
    return runtime


def test_the_socket_is_read_from_the_runtime() -> None:
    provider = _started_provider(_runtime(4242, "/run/kc/gw.sock"))
    assert provider._client._mcp_gateway_socket == "/run/kc/gw.sock"


def test_a_started_provider_names_its_runtime_for_the_abort() -> None:
    provider = _started_provider(_runtime(4242, "/run/kc/gw.sock"))
    assert provider.runtime_abort_target() == RuntimeAbortTarget.build(4242, "/run/kc/gw.sock")


def test_a_runtime_without_a_gateway_socket_still_yields_no_target() -> None:
    provider = _started_provider(_runtime(4242, None))
    assert provider._client._mcp_gateway_socket is None
    assert provider.runtime_abort_target() is None
