"""Two coexisting Slack transcripts must not be conflated by a shared legacy alias.

`transcript_stems` returns BOTH the canonical `slack_<ts>` spelling and the legacy
bare `<ts>` one, because `ConversationLog._path` may resolve to either. That holds
only while ONE of them is backed by a transcript file. Once both are, they are two
separate live sessions, and the shared legacy alias is not a name either key
resolves to -- so intersecting the raw alias sets reports one live session as the
other.

`coexisting_transcript_stems` names the aliases a DIFFERENT surviving transcript
backs, and `resolved_transcript_stems` subtracts them before any comparison.
"""

from __future__ import annotations

from kiro_crew.history import same_transcript, transcript_stems


def test_coexisting_slack_transcripts_sharing_a_legacy_stem_are_not_one_transcript(tmp_path):
    """Two live Slack sessions sharing the legacy bare spelling must not compare equal.

    ``transcript_stems`` returns both Slack spellings, and when both are backed each belongs to a
    different live session, so matching on the shared alias routes one session's context to the
    other.
    """
    canonical_key = "slack:1111.0001"
    stems = transcript_stems(canonical_key)
    assert len(stems) == 2, f"the legacy alias is not reachable for this key: {stems}"
    legacy_stem = stems[1]
    for stem in stems:
        (tmp_path / f"{stem}.jsonl").write_text('{"role": "user", "content": "x"}\n')

    assert not same_transcript(canonical_key, legacy_stem, tmp_path), (
        "two coexisting Slack transcripts were reported as one transcript: "
        f"{canonical_key!r} would route its context at {legacy_stem!r}"
    )


def test_a_lone_legacy_slack_transcript_still_matches_its_canonical_key(tmp_path):
    """The alias fallback survives the coexistence check, or a legacy thread loses its context."""
    canonical_key = "slack:1111.0001"
    legacy_stem = transcript_stems(canonical_key)[1]
    (tmp_path / f"{legacy_stem}.jsonl").write_text('{"role": "user", "content": "x"}\n')

    assert same_transcript(
        canonical_key, legacy_stem, tmp_path
    ), f"a lone legacy transcript stopped matching the key that resolves to it: {legacy_stem!r}"
