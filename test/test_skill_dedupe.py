"""Phase-0 tests: metadata-only skill dedupe (no embeddings)."""

from __future__ import annotations

from kiro_crew.skills_dedupe import (
    VERDICT_DUP,
    VERDICT_NEW,
    VERDICT_UPDATE,
    build_dedupe_prompt,
    metadata_dedupe_verdict,
    parse_dedupe_verdict,
)

_EXISTING = [
    {"key": "auto/deploy-timeout", "description": "debug apollo deploy timeouts", "triggers": "deploy, timeout"},
    {"key": "auto/rotate-logs", "description": "rotate and prune service logs", "triggers": "logs"},
]


def test_prompt_lists_candidate_and_all_existing():
    cand = {"slug": "fix-deploy", "description": "resolve deployment timeouts", "triggers": "deploy"}
    prompt = build_dedupe_prompt(cand, _EXISTING)
    assert "auto/deploy-timeout" in prompt
    assert "auto/rotate-logs" in prompt
    assert "resolve deployment timeouts" in prompt
    assert "NONE" in prompt


# --- Tri-state verdict --------------------------------------------------------


def test_prompt_lists_three_reply_forms():
    cand = {"slug": "fix-deploy", "description": "resolve deployment timeouts", "triggers": "deploy"}
    prompt = build_dedupe_prompt(cand, _EXISTING)
    assert "NONE" in prompt
    assert "DUP <key>" in prompt
    assert "UPDATE <key>" in prompt


def test_verdict_none_is_new():
    assert parse_dedupe_verdict("NONE", ["auto/deploy-timeout"]) == (VERDICT_NEW, None)
    assert parse_dedupe_verdict(
        "None of these apply.", ["auto/deploy-timeout"]
    ) == (VERDICT_NEW, None)


def test_verdict_dup_with_key():
    assert parse_dedupe_verdict(
        "DUP auto/deploy-timeout", ["auto/deploy-timeout", "auto/rotate-logs"]
    ) == (VERDICT_DUP, "auto/deploy-timeout")


def test_verdict_update_with_key():
    assert parse_dedupe_verdict(
        "UPDATE auto/rotate-logs", ["auto/deploy-timeout", "auto/rotate-logs"]
    ) == (VERDICT_UPDATE, "auto/rotate-logs")


def test_verdict_bare_key_is_dup_backward_compat():
    assert parse_dedupe_verdict(
        "auto/deploy-timeout", ["auto/deploy-timeout"]
    ) == (VERDICT_DUP, "auto/deploy-timeout")


def test_verdict_update_with_invalid_key_is_new():
    # An UPDATE whose named key is not a real existing skill fails open to new.
    assert parse_dedupe_verdict(
        "UPDATE auto/does-not-exist", ["auto/deploy-timeout"]
    ) == (VERDICT_NEW, None)


def test_verdict_robust_to_fences_and_prose():
    reply = "```\nUPDATE auto/rotate-logs\n```"
    assert parse_dedupe_verdict(
        reply, ["auto/deploy-timeout", "auto/rotate-logs"]
    ) == (VERDICT_UPDATE, "auto/rotate-logs")


def test_verdict_longer_key_not_substring_matched():
    assert parse_dedupe_verdict(
        "UPDATE auto/deploy-helper-v2", ["auto/deploy-helper"]
    ) == (VERDICT_NEW, None)


def test_metadata_dedupe_verdict_new_on_no_existing_or_no_judge():
    cand = {"slug": "x", "description": "y", "triggers": "z"}
    assert metadata_dedupe_verdict(
        cand, [], judge_fn=lambda _p: "DUP auto/deploy-timeout"
    ) == (VERDICT_NEW, None)
    assert metadata_dedupe_verdict(cand, _EXISTING, judge_fn=None) == (VERDICT_NEW, None)


def test_metadata_dedupe_verdict_fails_open_on_judge_error():
    def boom(_p):
        raise RuntimeError("model down")

    cand = {"slug": "x", "description": "y", "triggers": "z"}
    assert metadata_dedupe_verdict(cand, _EXISTING, judge_fn=boom) == (VERDICT_NEW, None)


def test_metadata_dedupe_verdict_dup_and_update():
    cand = {"slug": "fix-deploy", "description": "resolve deployment timeouts", "triggers": "deploy"}
    assert metadata_dedupe_verdict(
        cand, _EXISTING, judge_fn=lambda _p: "DUP auto/deploy-timeout"
    ) == (VERDICT_DUP, "auto/deploy-timeout")
    assert metadata_dedupe_verdict(
        cand, _EXISTING, judge_fn=lambda _p: "UPDATE auto/deploy-timeout"
    ) == (VERDICT_UPDATE, "auto/deploy-timeout")


# --- End punctuation and quoting around the key ------------------------------


def test_verdict_dup_key_with_trailing_period():
    assert parse_dedupe_verdict(
        "DUP auto/deploy-timeout.", ["auto/deploy-timeout"]
    ) == (VERDICT_DUP, "auto/deploy-timeout")


def test_verdict_update_key_with_trailing_periods():
    assert parse_dedupe_verdict(
        "UPDATE auto/rotate-logs...", ["auto/deploy-timeout", "auto/rotate-logs"]
    ) == (VERDICT_UPDATE, "auto/rotate-logs")


def test_verdict_dup_key_with_trailing_bang():
    assert parse_dedupe_verdict(
        "DUP auto/deploy-timeout!", ["auto/deploy-timeout"]
    ) == (VERDICT_DUP, "auto/deploy-timeout")


def test_verdict_dup_key_in_backticks():
    assert parse_dedupe_verdict(
        "DUP `auto/deploy-timeout`", ["auto/deploy-timeout"]
    ) == (VERDICT_DUP, "auto/deploy-timeout")


def test_verdict_dup_plain_key():
    assert parse_dedupe_verdict(
        "DUP auto/deploy-timeout", ["auto/deploy-timeout"]
    ) == (VERDICT_DUP, "auto/deploy-timeout")


def test_verdict_bare_none_token():
    assert parse_dedupe_verdict("NONE", ["auto/deploy-timeout"]) == (VERDICT_NEW, None)
    assert parse_dedupe_verdict("NONE.", ["auto/deploy-timeout"]) == (VERDICT_NEW, None)
    # A judge that answers with the verdict's name instead of the NONE token.
    assert parse_dedupe_verdict("NEW", ["auto/deploy-timeout"]) == (VERDICT_NEW, None)


def test_verdict_trailing_period_does_not_invent_a_key():
    # Stripping the period must still only match a real existing key.
    assert parse_dedupe_verdict(
        "DUP auto/does-not-exist.", ["auto/deploy-timeout"]
    ) == (VERDICT_NEW, None)
    assert parse_dedupe_verdict(
        "DUP auto/deploy-helper-v2.", ["auto/deploy-helper"]
    ) == (VERDICT_NEW, None)
    # A period inside the key is part of the key, not end punctuation.
    assert parse_dedupe_verdict(
        "DUP auto/deploy.timeout", ["auto/deploy"]
    ) == (VERDICT_NEW, None)
