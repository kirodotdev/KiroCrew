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


def test_materializes_the_absent_authorize_lock(tmp_path, monkeypatch):
    """The `.lock` leaf is masked from the sandbox, but mount(2) cannot mask an
    absent path — the default state. Without materializing an empty lockfile the
    mask is vacuous and an in-sandbox process could pre-seat the mutex inode."""
    created = _run(monkeypatch, tmp_path)
    lock = tmp_path / ".secret_request_policy.json.lock"
    assert str(lock) in created
    assert lock.exists() and lock.read_bytes() == b""


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
    # The real owner policy is never touched (unchanged, and not in `created`);
    # only the absent lock leaf is materialized so its own mask is non-vacuous.
    assert str(real) not in created
    assert real.read_text() == before
    lock = tmp_path / ".secret_request_policy.json.lock"
    assert str(lock) in created
    assert lock.read_bytes() == b""


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


@_POSIX_ONLY
def test_refuses_a_hardlinked_policy_file(tmp_path, monkeypatch):
    """A pre-existing regular file with >1 hard link means the same inode is
    reachable at another (agent-writable) path, so masking this name would leave
    the alias writable. It must fail closed."""
    other = tmp_path / "agent_writable_alias.json"
    other.write_text('{"version":1,"authorizations":{}}', encoding="utf-8")
    link = tmp_path / POLICY_FILENAME
    os.link(other, link)  # hard link: same inode, st_nlink == 2
    with pytest.raises(sandbox.SandboxCeilingUnsealable):
        _run(monkeypatch, tmp_path)


@_POSIX_ONLY
def test_create_race_revalidates_and_fails_closed_on_a_non_regular_winner(tmp_path, monkeypatch):
    """If a concurrent creator wins the O_EXCL race, the winner is re-validated
    with no-follow lstat; a non-regular winner (e.g. an agent-planted symlink)
    must fail closed rather than be trusted."""
    import errno

    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)

    real_open = os.open

    def racing_open(path, flags, *a, **k):
        # Simulate a concurrent creator winning between lstat and our O_EXCL open,
        # then leaving a SYMLINK at the name (what a hostile racer would plant).
        if str(path) == str(tmp_path / POLICY_FILENAME) and (flags & os.O_EXCL):
            os.symlink(tmp_path / "elsewhere", tmp_path / POLICY_FILENAME)
            raise FileExistsError(errno.EEXIST, "exists")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(sandbox.SandboxCeilingUnsealable):
        sandbox._materialize_secret_request_policy_mask_target()
