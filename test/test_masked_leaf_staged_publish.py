"""A masked FILE leaf's publish temp must never sit at a name no mask covers.

``sandbox._CREW_HIDDEN_LEAVES`` masks a leaf FILE by its exact name, and a mask covers a
PATH rather than an inode. ``atomic_write`` stages its temp with
``mkstemp(dir=path.parent)``, so a plain ``atomic_write`` onto one of those leaves puts the
full payload in the data-home root -- sandbox-visible and same-uid writable -- for the
length of the write, and a crash there leaves the same bytes behind at an unmasked name.

The gateway's own auth stores already publish through the masked ``auth-store-staging``
directory. These tests pin the same for every other hidden file leaf that the gateway
writes with ``atomic_write``: the browser extension token and the Ops Mission Control
secret store and policy ceiling. Each writer's temp, and any orphan a failed publish could
leave, must live inside the masked staging directory and nowhere else.
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path

import pytest

from kiro_crew import atomic_write as aw
from kiro_crew import hidden_leaf_staging
from kiro_crew.sandbox_leaves import _AUTH_STORE_STAGING_LEAF, _CREW_HIDDEN_LEAVES


@pytest.fixture
def crew_home(tmp_path, monkeypatch):
    home = tmp_path / "crew-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


@pytest.fixture
def temp_dirs(monkeypatch):
    """Record the directory of every temp ``atomic_write`` stages."""
    seen: list[Path] = []
    real = tempfile.mkstemp

    def _recording(*args, **kwargs):
        fd, name = real(*args, **kwargs)
        seen.append(Path(name).parent)
        return fd, name

    monkeypatch.setattr(aw.tempfile, "mkstemp", _recording)
    return seen


def _browser_token_write():
    from kiro_crew.browser_cli import token

    token.set_token("tok-" + "a" * 32)
    return token.token_path(), None


def _ops_secret_write():
    from kiro_crew.apps.builtins.ops_mission_control.backend import secrets

    secrets.KeystoneFileBackend().put("pagerduty", "token", "pd-secret-value")
    return secrets.secrets_path(), "pd-secret-value"


def _ops_policy_write():
    from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

    policy_store.put(policy_store.PRIMARY_KEY, "host-a")
    return policy_store.policy_path(), None


WRITERS = {
    "browser-extension-token": _browser_token_write,
    "ops-mission-control-secrets": _ops_secret_write,
    "ops-mission-control-policy": _ops_policy_write,
}


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_hidden_leaf_temp_is_staged_only_under_the_masked_directory(writer, crew_home, temp_dirs):
    target, secret = WRITERS[writer]()

    assert target.parent == crew_home, "fixture drift: the leaf is not in the data-home root"
    assert (
        target.name in _CREW_HIDDEN_LEAVES
    ), f"{target.name} is no longer a hidden leaf; this test no longer guards anything"
    assert target.is_file(), "the write did not publish"
    if secret is not None:
        assert secret in target.read_text(encoding="utf-8")

    staging = crew_home / _AUTH_STORE_STAGING_LEAF
    assert temp_dirs, "atomic_write staged nothing; the recorder is not wired to the writer"
    for d in temp_dirs:
        assert d == staging, (
            f"{writer} staged a temp in {d}, not in the masked {staging}: a same-uid sandboxed "
            "agent can read or link(2) that temp for the length of the write"
        )
    assert _AUTH_STORE_STAGING_LEAF in _CREW_HIDDEN_LEAVES

    # Nothing of the write is left in the data-home root besides the leaf itself (and the
    # ops store's empty lock sidecar, which carries no payload).
    stray = [
        p.name
        for p in crew_home.iterdir()
        if p.is_file() and p != target and not p.name.endswith(".lock")
    ]
    assert stray == [], f"{writer} left unmasked files in the data-home root: {stray}"
    assert (
        list(staging.iterdir()) == []
    ), f"{writer} left a staged temp behind: {list(staging.iterdir())}"


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_failed_publish_leaves_no_orphan_and_keeps_the_old_leaf(writer, crew_home, monkeypatch):
    target, _ = WRITERS[writer]()
    before = target.read_bytes()

    def _refuse(_src, _dst):
        raise OSError(errno.EIO, "simulated publish failure")

    monkeypatch.setattr(hidden_leaf_staging, "replace_with_retry", _refuse)
    with pytest.raises(OSError):
        WRITERS[writer]()

    assert target.read_bytes() == before, "a failed publish changed the published leaf"
    staging = crew_home / _AUTH_STORE_STAGING_LEAF
    assert list(staging.iterdir()) == [], (
        f"{writer} left its staged copy behind after a failed publish: "
        f"{list(staging.iterdir())}"
    )
    stray = [
        p.name
        for p in crew_home.iterdir()
        if p.is_file() and p != target and not p.name.endswith(".lock")
    ]
    assert stray == []


def test_unusable_staging_refuses_rather_than_writing_beside_the_leaf(crew_home, temp_dirs):
    """A planted non-directory at the staging name must NOT fall back to a sibling temp."""
    (crew_home / _AUTH_STORE_STAGING_LEAF).write_text("planted", encoding="utf-8")

    from kiro_crew.browser_cli import token

    with pytest.raises(OSError):
        token.set_token("tok-" + "b" * 32)
    assert not token.token_path().exists()
    assert crew_home not in temp_dirs


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privilege on Windows")
def test_symlinked_staging_is_refused(crew_home, tmp_path, temp_dirs):
    outside = tmp_path / "outside"
    outside.mkdir()
    (crew_home / _AUTH_STORE_STAGING_LEAF).symlink_to(outside, target_is_directory=True)

    target = crew_home / "ops_mission_control_policy.json"
    with pytest.raises(OSError):
        hidden_leaf_staging.publish_through_auth_store_staging(target, "{}")
    assert list(outside.iterdir()) == []
    assert not target.exists()
