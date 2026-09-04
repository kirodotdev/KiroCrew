"""The prompt fingerprint: the only thing that can prove WHOSE prompt answered.

Every other gate in this system can pass while the wrong crew answers. The image
digest proves the right artifact is deployed. The container's checks prove the right
bundle is installed. The crew address proves the request named this crew. None of
them proves the answer came from the packaged prompt, and that is precisely where the
first live deployment failed: a stock agent answered "reply with the single word: ok"
indistinguishably from a tuned crew, because that question has the same answer either
way.

These tests hold the fingerprint to the property that makes it evidence: it cannot be
produced by an agent that did not receive this bundle's prompt.

The import below is relative on purpose. ``from packaging.build import ...`` needs
this suite's parent on ``sys.path``, and ``packaging`` there shadows the PyPA
distribution of the same name for every other test in the same pytest worker. The
relative form asks for the module by its real package path, so the collision the
sibling ``test_producer.py`` docstring describes cannot happen in-process at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..build import (
    _inject_fingerprint_challenge,
    bundle_digest,
    fingerprint_challenge,
    prompt_fingerprint,
)


def test_the_fingerprint_differs_for_every_revision() -> None:
    """A value shared across revisions could not tell one bundle from another."""
    a = prompt_fingerprint("acme", "sha256:" + "a" * 64)
    b = prompt_fingerprint("acme", "sha256:" + "b" * 64)
    assert a != b


def test_the_fingerprint_differs_between_crews_on_identical_content() -> None:
    """Two crews packaged from the same content must not share a fingerprint.

    Otherwise a gate for crew A would pass against a deployment serving crew B.
    """
    same = "sha256:" + "c" * 64
    assert prompt_fingerprint("acme", same) != prompt_fingerprint("globex", same)


def test_the_crew_name_and_digest_cannot_be_swapped_into_each_other() -> None:
    """Concatenating the two inputs without a separator would let ("ab","c") and
    ("a","bc") collide, so a rename could silently reuse another crew's value."""
    assert prompt_fingerprint("ab", "c") != prompt_fingerprint("a", "bc")


def test_the_fingerprint_is_long_enough_not_to_be_guessed() -> None:
    fp = prompt_fingerprint("acme", "sha256:" + "d" * 64)
    assert len(fp) == 24 and all(c in "0123456789abcdef" for c in fp)


def test_the_challenge_names_the_crew() -> None:
    """A generic token could fire on a normal conversation in any deployment."""
    assert fingerprint_challenge("acme-support") == "SMC-VERIFY-acme-support"
    assert fingerprint_challenge("globex") != fingerprint_challenge("acme-support")


# --- injection ---------------------------------------------------------------


def _spec(tmp: Path, prompt: str) -> Path:
    p = tmp / "agent.json"
    p.write_text(json.dumps({"name": "acme", "prompt": prompt}), encoding="utf-8")
    return p


def test_injection_keeps_the_curated_persona_intact(tmp_path: Path) -> None:
    persona = "You are Acme's support crew. Never discuss competitors."
    p = _spec(tmp_path, persona)
    _inject_fingerprint_challenge(p, "acme", "deadbeefdeadbeefdeadbeef")
    prompt = json.loads(p.read_text())["prompt"]
    assert persona in prompt


def test_the_challenge_comes_before_the_persona(tmp_path: Path) -> None:
    """Buried under a long persona, the instruction is likelier to be ignored."""
    p = _spec(tmp_path, "x" * 4000)
    _inject_fingerprint_challenge(p, "acme", "deadbeefdeadbeefdeadbeef")
    prompt = json.loads(p.read_text())["prompt"]
    assert prompt.index("SMC-FINGERPRINT") < prompt.index("x" * 100)


def test_the_injected_block_carries_the_challenge_and_the_value(tmp_path: Path) -> None:
    p = _spec(tmp_path, "persona")
    _inject_fingerprint_challenge(p, "acme", "deadbeefdeadbeefdeadbeef")
    prompt = json.loads(p.read_text())["prompt"]
    assert "SMC-VERIFY-acme" in prompt
    assert "SMC-FINGERPRINT deadbeefdeadbeefdeadbeef" in prompt


def test_the_prompt_does_not_instruct_the_agent_to_conceal_itself(tmp_path: Path) -> None:
    """A prompt that tells an agent to hide part of itself from the person talking to
    it is worse than the leak it prevents, and there is nothing here worth hiding."""
    p = _spec(tmp_path, "persona")
    _inject_fingerprint_challenge(p, "acme", "deadbeefdeadbeefdeadbeef")
    prompt = json.loads(p.read_text())["prompt"].lower()
    for banned in ("do not mention", "never reveal", "keep this secret", "do not disclose"):
        assert banned not in prompt


def test_an_empty_persona_still_yields_a_usable_prompt(tmp_path: Path) -> None:
    p = tmp_path / "agent.json"
    p.write_text(json.dumps({"name": "acme"}), encoding="utf-8")
    _inject_fingerprint_challenge(p, "acme", "deadbeefdeadbeefdeadbeef")
    assert "SMC-FINGERPRINT" in json.loads(p.read_text())["prompt"]


# --- the circularity the derivation exists to avoid --------------------------


def test_injection_changes_the_content_digest(tmp_path: Path) -> None:
    """Which is why the fingerprint is derived BEFORE injection.

    Deriving it from the finished bundle would require the value to be part of the
    content it was computed from. This test is the reason the build computes the
    digest twice.
    """
    (tmp_path / "skills").mkdir()
    (tmp_path / "mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
    p = _spec(tmp_path, "persona")

    before = bundle_digest(tmp_path)
    _inject_fingerprint_challenge(p, "acme", prompt_fingerprint("acme", before))
    after = bundle_digest(tmp_path)

    assert before != after
