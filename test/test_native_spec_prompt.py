"""Which harnesses load a spec's own ``prompt``, and which keep it across a compaction.

``ACP_BACKENDS_NATIVE_SPEC_PROMPT`` is the ACP runtime's gate on recording the
copy a harness loaded (``native_context_documents``); the prompt builder reads
that recorded copy, and nothing else, before it withholds its
``[AGENT SYSTEM PROMPT]`` copy of a custom persona (kirodotdev/KiroCrew#13305).
A wrong membership drops that persona outright, so the set is pinned: kiro-cli
and KAS, and every other harness, including one Crew does not know, records
nothing and keeps the block.

``native_spec_prompt_across_compaction`` is the one provider fact read after a
compaction: whether the harness's own summarization keeps that prompt. It is a
separate claim, admitted per backend on a live transcript, so only kiro-cli
answers True, and only from the release that transcript was taken on
(``NATIVE_SPEC_PROMPT_ACROSS_COMPACTION_MIN_KIRO_CLI_VERSION``, read off the
handshake's ``agentInfo.version``): a release below the floor, and a handshake
that reported no version, answer False and are sent the block, a duplicate at
worst. KAS answers False until a transcript shows it, and re-sends the
block after a compaction as a harness without the prompt does.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_NATIVE_SPEC_PROMPT,
    ACP_BACKENDS_NATIVE_SPEC_PROMPT_ACROSS_COMPACTION,
    spec_prompt_retention_verified,
)
from kiro_crew.agent_sdk.backends import NATIVE_SPEC_PROMPT_ACROSS_COMPACTION_MIN_KIRO_CLI_VERSION
from kiro_crew.mcp_hot_reload import parse_kiro_cli_version
from kiro_crew.providers.base import LLMProvider

_NATIVE = {ACP_BACKEND_KIRO, ACP_BACKEND_KAS}
FLOOR = "2.28.0"
BELOW_FLOOR = "2.27.1"


def _label(backend: str) -> str:
    return backend or "kiro-cli"


def test_only_kiro_cli_and_kas_deliver_the_spec_prompt():
    assert ACP_BACKENDS_NATIVE_SPEC_PROMPT == frozenset(_NATIVE)
    assert ACP_BACKENDS_NATIVE_SPEC_PROMPT <= ACP_BACKENDS_KNOWN


def test_provider_that_does_not_say_keeps_the_block():
    """The base default is False, so a provider added later keeps the block
    until it opts in."""

    class _Silent(LLMProvider):
        async def start(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

        async def stream(self, message, *, allow_image=True):
            return
            yield

        async def approve_tool(self, request_id, *, always=False) -> bool:
            return True

        async def reject_tool(self, request_id) -> None:
            return None

        def context_usage_pct(self) -> float:
            return 0.0

    assert _Silent().native_spec_prompt_across_compaction is False


def test_only_kiro_cli_is_shown_to_keep_the_spec_prompt_across_compaction():
    """Retention is admitted only on a live transcript (kiro-cli has one, KAS
    none), and only a harness that delivers the prompt can retain it."""
    assert ACP_BACKENDS_NATIVE_SPEC_PROMPT_ACROSS_COMPACTION == frozenset({ACP_BACKEND_KIRO})
    assert ACP_BACKENDS_NATIVE_SPEC_PROMPT_ACROSS_COMPACTION <= ACP_BACKENDS_NATIVE_SPEC_PROMPT
    assert ACP_BACKEND_KAS not in ACP_BACKENDS_NATIVE_SPEC_PROMPT_ACROSS_COMPACTION


def test_retention_floor_is_the_transcripts_release():
    """A floor, not a list: at or above the release the transcript was taken on
    the claim holds; below it, and with no parsable version, it does not."""
    assert NATIVE_SPEC_PROMPT_ACROSS_COMPACTION_MIN_KIRO_CLI_VERSION == (2, 28, 0)
    assert spec_prompt_retention_verified((2, 28, 0)) is True
    assert spec_prompt_retention_verified((2, 28, 1)) is True
    assert spec_prompt_retention_verified((3, 0, 0)) is True
    assert spec_prompt_retention_verified((2, 27, 1)) is False
    assert spec_prompt_retention_verified((2, 27, 99)) is False
    assert spec_prompt_retention_verified(None) is False
    assert spec_prompt_retention_verified(parse_kiro_cli_version("")) is False
    assert spec_prompt_retention_verified(parse_kiro_cli_version("kiro-cli 2.28.0")) is True


@pytest.mark.parametrize("version", [FLOOR, BELOW_FLOOR, ""], ids=["floor", "below", "unknown"])
@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN), ids=_label)
def test_direct_provider_reports_retention_across_compaction(tmp_path, backend, version):
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider(work_dir=tmp_path, acp_backend=backend)
    provider._client._agent_version = version  # what the handshake reported
    assert provider.agent_version == version
    expected = backend == ACP_BACKEND_KIRO and version == FLOOR
    assert provider.native_spec_prompt_across_compaction is expected


@pytest.mark.parametrize("version", [FLOOR, BELOW_FLOOR, ""], ids=["floor", "below", "unknown"])
@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN) + ["byo-harness"], ids=_label)
def test_session_provider_reports_retention_across_compaction(tmp_path, backend, version):
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
    from kiro_crew.acp.session_provider import AcpSessionProvider

    runtime = AcpRuntime(work_dir=tmp_path, acp_backend=backend)
    runtime._agent_version = version  # the handshake is per process; the handle delegates
    handle = AcpSessionHandle("persona", asyncio.Queue(), runtime, watchdog=WatchdogSettings())
    provider = AcpSessionProvider(handle, runtime, owns_runtime=True)
    assert provider.agent_version == version
    expected = backend == ACP_BACKEND_KIRO and version == FLOOR
    assert provider.native_spec_prompt_across_compaction is expected


def test_compatibility_shim_exports_the_retention_set():
    from kiro_crew import acp_backends

    assert "ACP_BACKENDS_NATIVE_SPEC_PROMPT_ACROSS_COMPACTION" in acp_backends.__all__
    assert (
        acp_backends.ACP_BACKENDS_NATIVE_SPEC_PROMPT_ACROSS_COMPACTION
        is ACP_BACKENDS_NATIVE_SPEC_PROMPT_ACROSS_COMPACTION
    )
    assert "spec_prompt_retention_verified" in acp_backends.__all__
    assert acp_backends.spec_prompt_retention_verified is spec_prompt_retention_verified
