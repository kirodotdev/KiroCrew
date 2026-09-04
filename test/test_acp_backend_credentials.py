"""An ACP adapter's OAuth token store is on the sensitive-path floor.

Each adapter owns its own sign-in flow and persists its own tokens. Kiro Crew
never reads them — it only ever checks that the file EXISTS so it can name the
right sign-in command — so an agent ``fs_read`` of one buys nothing legitimate
and would let it impersonate the operator against that vendor.

Claude Code is already selectable, so its leaf being off the floor was a live
read, not a hypothetical. Codex's leaf has the same shape.

Two properties are pinned separately because they fail apart:

* the ``$HOME``-rooted default is classified, and the adapter's sibling CONFIG
  files are NOT (routing diagnosis reads them and they hold no credential); and
* the leaf is re-anchored under every home override the adapter honours, since
  an override moves the token out from under a ``$HOME``-rooted entry.

Each test is revert-verified: with the corresponding half of the change removed
they fail.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import security
from kiro_crew.security import _SENSITIVE_HOME_DIRS, is_sensitive_path

#: leaf, the env var that moves its parent, and the basename under that override.
ADAPTER_TOKEN_LEAVES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (".codex/auth.json", ("CODEX_HOME",), "auth.json"),
    (
        ".claude/.credentials.json",
        ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME"),
        ".credentials.json",
    ),
)

#: Sibling files that must STAY readable. Losing these would break routing
#: diagnosis, and unlike the token they carry no credential.
READABLE_SIBLINGS: tuple[str, ...] = (
    ".codex/config.toml",
    ".claude/settings.json",
    ".claude/settings.local.json",
)


def _clear_cache() -> None:
    """Drop the TTL target cache so an env change is observed immediately."""
    security._home_targets_cache.clear()


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Anchor every case on a scratch home with no adapter overrides set.

    The overrides are cleared rather than merely unset-if-absent: a developer
    machine that genuinely exports ``CODEX_HOME`` would otherwise make the
    default-location assertions pass for the wrong reason.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_HOME"):
        monkeypatch.delenv(var, raising=False)
    _clear_cache()
    yield tmp_path
    _clear_cache()


@pytest.mark.parametrize("leaf,_env_vars,_basename", ADAPTER_TOKEN_LEAVES)
def test_token_leaf_is_on_the_floor(leaf, _env_vars, _basename) -> None:
    """The leaf is listed, so the registry and the gate cannot drift apart."""
    assert leaf in _SENSITIVE_HOME_DIRS, (
        f"{leaf} must be in _SENSITIVE_HOME_DIRS; without it an agent fs_read "
        "can lift that adapter's OAuth token"
    )


@pytest.mark.parametrize("leaf,_env_vars,_basename", ADAPTER_TOKEN_LEAVES)
def test_default_location_is_blocked(_isolated_home, leaf, _env_vars, _basename) -> None:
    """The documented ``$HOME``-rooted location is refused."""
    target = os.path.join(str(_isolated_home), *leaf.split("/"))
    assert is_sensitive_path(target) is True


@pytest.mark.parametrize("sibling", READABLE_SIBLINGS)
def test_sibling_config_stays_readable(_isolated_home, sibling) -> None:
    """Only the token leaf is classified, never the whole adapter directory.

    Classifying the directory would be the easy over-broad fix and would break
    routing diagnosis, which reads these files.
    """
    target = os.path.join(str(_isolated_home), *sibling.split("/"))
    assert is_sensitive_path(target) is False, (
        f"{sibling} carries no credential and routing diagnosis reads it; "
        "classify the token leaf, not the directory"
    )


@pytest.mark.parametrize("leaf,env_vars,basename", ADAPTER_TOKEN_LEAVES)
def test_home_override_is_anchored(monkeypatch, tmp_path, leaf, env_vars, basename) -> None:
    """An override moves the token, and the gate follows it.

    One variable at a time: an adapter honouring two roots must cover EACH of
    them, and asserting them together would pass while one was missed.
    """
    for var in env_vars:
        override = tmp_path / f"override-{var.lower()}"
        override.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(override))
        _clear_cache()
        try:
            moved = override / basename
            assert is_sensitive_path(str(moved)) is True, (
                f"{leaf} moved by {var} is no longer gated; a literal "
                "$HOME-rooted entry only covers the default location"
            )
        finally:
            monkeypatch.delenv(var, raising=False)
            _clear_cache()


@pytest.mark.parametrize("leaf,env_vars,basename", ADAPTER_TOKEN_LEAVES)
def test_default_location_survives_an_override(
    monkeypatch, tmp_path, _isolated_home, leaf, env_vars, basename
) -> None:
    """Setting an override ADDS a target; it never drops the default one.

    The override anchoring is additive precisely so a host where the adapter
    still uses its default path keeps its protection.
    """
    override = tmp_path / "elsewhere"
    override.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(env_vars[0], str(override))
    _clear_cache()
    default_target = os.path.join(str(_isolated_home), *leaf.split("/"))
    assert is_sensitive_path(default_target) is True


def test_override_roots_are_part_of_the_cache_key() -> None:
    """A changed override must invalidate the cached target set.

    The TTL cache is keyed on the resolved roots, so a root the BUILDER anchors
    on but the KEY omits would serve targets computed for the previous value —
    the fail-open shape the resolved-home key already exists to prevent. Asserts
    on the key's own fields rather than on cache behaviour, so the reason a
    failure happened is visible.
    """
    fields = set(security._ResolvedRoots._fields)
    for _leaf, root_fields in security._OVERRIDE_ANCHORED_LEAVES:
        for field in root_fields:
            assert field in fields, (
                f"_OVERRIDE_ANCHORED_LEAVES anchors on {field!r}, which is not a "
                "_ResolvedRoots field, so it cannot be part of the cache key"
            )


def test_every_anchored_leaf_is_actually_on_the_floor() -> None:
    """The override table cannot name a leaf the read tier does not classify.

    Guards the opposite drift from the tests above: an entry removed from
    ``_SENSITIVE_HOME_DIRS`` while its override anchor stayed would leave the
    table describing protection that no longer exists.
    """
    for leaf, _root_fields in security._OVERRIDE_ANCHORED_LEAVES:
        assert (
            leaf in _SENSITIVE_HOME_DIRS
        ), f"{leaf} is override-anchored but absent from _SENSITIVE_HOME_DIRS"
