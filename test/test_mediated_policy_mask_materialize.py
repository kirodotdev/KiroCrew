"""``_materialize_secret_request_policy_mask_target`` — the absent-equivalent the
sandbox mask needs.

``secret_request_policy.json`` is masked from the agent, but ``mount(2)`` cannot
mask an absent path, and absent is the default install state. So the launcher
materializes the file's absent-equivalent before installing the mask. These tests
pin that it (a) writes a document ``load_authorization`` reads as "no secret is
authorized", (b) never touches a real owner file, and (c) refuses a pre-planted
symlink at the name rather than sealing its referent while the name stays writable.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox
from kiro_crew.secrets_mediation.policy import POLICY_FILENAME, PolicyError, load_authorization

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX no-follow semantics")


def _run(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[str]:
    monkeypatch.setattr(sandbox, "config_dir", lambda: root)
    return sandbox._materialize_secret_request_policy_mask_target()


def test_materializes_an_absent_equivalent_policy(tmp_path, monkeypatch):
    created = _run(monkeypatch, tmp_path)
    target = tmp_path / POLICY_FILENAME
    assert str(target) in created
    # The stub must read as "no authorization" for any secret — exactly like absent.
    with pytest.raises(PolicyError):
        load_authorization(tmp_path, "ANY_SECRET")
    # And it is valid JSON of the documented shape.
    assert json.loads(target.read_text())["authorizations"] == {}


def test_never_overwrites_a_real_owner_policy(tmp_path, monkeypatch):
    real = tmp_path / POLICY_FILENAME
    real.write_text(
        json.dumps(
            {
                "version": 1,
                "authorizations": {
                    "K": {"origin": "https://api.example.com", "placement": {"type": "bearer"}}
                },
            }
        ),
        encoding="utf-8",
    )
    before = real.read_text()
    created = _run(monkeypatch, tmp_path)
    assert created == []  # untouched
    assert real.read_text() == before


@_POSIX_ONLY
def test_refuses_a_symlink_at_the_policy_name(tmp_path, monkeypatch):
    """A pre-planted symlink must be refused, not materialized-through: sealing
    its referent would leave the replaceable name writable for an attacker
    policy."""
    attacker = tmp_path / "attacker_policy.json"
    attacker.write_text('{"version":1,"authorizations":{}}', encoding="utf-8")
    link = tmp_path / POLICY_FILENAME
    os.symlink(attacker, link)
    with pytest.raises(sandbox.SandboxCeilingUnsealable):
        _run(monkeypatch, tmp_path)
