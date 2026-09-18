"""The pod grant-store corridor through the re-anchored os-home mask.

The whole sensitive-dir tier is re-anchored under a pod child's remapped
home. On Linux ``is_kiro_cli`` does not skip Crew's launcher (``delegate_to_kiro``
is darwin/win32 only), so that mask reaches the pod's kiro-cli child -- and it
covered ``.aws``, which is the pod's OWN grant store: the child reads the seeded
SSO token from it and writes its MCP OAuth grants into it. These tests pin the
carve-out and pin that carving it out did not reopen the file-credential leg.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew.sandbox import (
    _POD_OS_HOME_GRANT_STORE_LEAVES,
    _POD_OS_HOME_MASKED_SUBLEAVES,
    _pod_os_home_targets,
)

_TIER = (".aws", ".ssh", ".gnupg", ".kube", ".config/gh")
_OS_HOME = os.path.normpath("/pods/p1/os-home")


def _expected(leaf: str) -> str:
    """The production spelling of a re-anchored target.

    Built through the SAME ``join``+``normpath`` the helper uses: a literal
    ``"/pods/p1/os-home/.ssh"`` only matches on POSIX, and mixing separators is
    what made shard 3 fail on Windows with `'/pods/p1/os-home\\.ssh'`.
    """
    return os.path.normpath(os.path.join(_OS_HOME, *leaf.split("/")))


@pytest.fixture()
def pod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIROCREW_POD", "1")
    monkeypatch.setenv("KIROCREW_OS_HOME", _OS_HOME)


def test_grant_store_is_not_masked_so_the_child_can_read_and_write_it(
    pod_env: None,
) -> None:
    """The corridor: ``<os-home>/.aws`` must not be bind-masked empty.

    Red before the carve-out -- ``.aws`` was re-anchored like every other leaf,
    so the child could not read the seeded token and its grant writes landed in
    the overlay instead of the pod tree.
    """
    targets = _pod_os_home_targets(_TIER)
    assert _expected(".aws") not in targets


def test_file_credential_leg_stays_masked_under_the_pod_home(pod_env: None) -> None:
    """Carving out the store must not re-expose the AWS profile files."""
    targets = _pod_os_home_targets(_TIER)
    for leaf in _POD_OS_HOME_MASKED_SUBLEAVES:
        assert _expected(leaf) in targets


def test_every_other_tier_leaf_is_still_re_anchored(pod_env: None) -> None:
    """A sibling leaf stays empty-masked -- the carve-out is one subtree, not the tier."""
    targets = _pod_os_home_targets(_TIER)
    for leaf in (".ssh", ".gnupg", ".kube", ".config/gh"):
        assert _expected(leaf) in targets
    # Exactly one tier leaf is exempt, and it is the grant store.
    exempt = [leaf for leaf in _TIER if _expected(leaf) not in targets]
    assert exempt == list(_POD_OS_HOME_GRANT_STORE_LEAVES)


def test_non_pod_masks_are_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside a pod the function yields nothing, so no mask changes anywhere."""
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_OS_HOME", _OS_HOME)
    assert _pod_os_home_targets(_TIER) == []
    monkeypatch.setenv("KIROCREW_POD", "0")
    assert _pod_os_home_targets(_TIER) == []
    monkeypatch.setenv("KIROCREW_POD", "1")
    monkeypatch.delenv("KIROCREW_OS_HOME", raising=False)
    assert _pod_os_home_targets(_TIER) == []


def test_a_leaf_that_matches_the_default_spelling_is_not_duplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An os-home equal to the real home yields no re-anchored duplicates."""
    monkeypatch.setenv("KIROCREW_POD", "1")
    monkeypatch.setenv("KIROCREW_OS_HOME", os.path.expanduser("~"))
    assert _pod_os_home_targets(_TIER) == []
