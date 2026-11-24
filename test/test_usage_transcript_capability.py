"""Which harnesses Session Activity counts from usage rows, read as a capability.

``dashboard.handlers.usage_store._SHARD_COUNTED_HARNESSES`` is every known backend
whose ``SessionCapabilities.writes_crew_readable_transcript`` is False. The twelve
backends that existed when the store spelled the list out are pinned here by name,
so a changed answer names its harness; a backend added later is free to join.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.handlers import usage_store

#: The ten harnesses the store listed by name.
SHARD_COUNTED = frozenset(
    {
        "antigravity",
        "claude",
        "codex",
        "copilot",
        "deepseek",
        "goose",
        "mimocode",
        "opencode",
        "pi",
        "strands",
    }
)


#: The two harnesses whose transcripts the parse reads.
TRANSCRIPT_COUNTED = frozenset({"", "kas"})


def test_the_ten_are_still_members_and_kiro_and_kas_are_not() -> None:
    """A backend added later may join; none of the twelve may change sides."""
    members = usage_store._SHARD_COUNTED_HARNESSES
    assert SHARD_COUNTED <= members
    assert not members & TRANSCRIPT_COUNTED
    assert members == usage_store._shard_counted_harnesses()


@pytest.mark.parametrize("backend", sorted(SHARD_COUNTED | TRANSCRIPT_COUNTED))
def test_each_backend_keeps_its_answer(backend: str) -> None:
    writes = capabilities_for(backend).writes_crew_readable_transcript
    assert writes is (backend in TRANSCRIPT_COUNTED)


def test_every_known_backend_is_on_the_side_its_record_says() -> None:
    members = usage_store._SHARD_COUNTED_HARNESSES
    for backend in ACP_BACKENDS_KNOWN:
        assert (backend in members) is not capabilities_for(
            backend
        ).writes_crew_readable_transcript, backend


@pytest.mark.parametrize("harness", ["", "kiro", "unknown", "other", "claude_code"])
def test_a_row_naming_no_known_backend_is_not_a_member(harness: str) -> None:
    """A row with no harness, or a harness value that is no backend id, counts as before."""
    assert harness not in usage_store._SHARD_COUNTED_HARNESSES


def test_an_unknown_backend_writes_no_readable_transcript() -> None:
    assert (
        capabilities_for("some-harness-nobody-registered").writes_crew_readable_transcript is False
    )


def test_membership_follows_the_capability(monkeypatch) -> None:
    """Give codex a readable transcript and the store stops counting it from rows."""

    def _codex_reads(backend: str):
        record = capabilities_for(backend)
        if backend == ACP_BACKEND_CODEX:
            return dataclasses.replace(record, writes_crew_readable_transcript=True)
        return record

    before = usage_store._shard_counted_harnesses()
    monkeypatch.setattr(usage_store, "capabilities_for", _codex_reads)
    assert usage_store._shard_counted_harnesses() == before - {ACP_BACKEND_CODEX}
