"""Tests for ``atomic_write``'s parent link/junction refusal (issue #4381).

``mkdir(parents=True)``, ``mkstemp(dir=...)`` and ``os.replace`` follow every
path component except the final one, so a symlink pre-planted at a secret's
parent directory redirects the whole write to the link's target while the caller
sees success. The refusal lives in the shared helper because every
``restrict_to_owner=True`` caller has the same exposure, and patching them one at
a time is the whack-a-mole shape the issue rejects.

What must NOT be refused is as load-bearing as what must: a symlinked ``$HOME``
and a data home relocated onto another disk are supported layouts, and a link at
or above the trust anchor is the operator's own choice.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import atomic_write as aw

SECRET = "sk-live-DEADBEEF"

# Windows symlink creation needs a privilege the CI account does not have (a
# junction is the Windows shape, and only for directories); the guard's Windows
# arm rides on platform_compat.is_link_or_junction, which has its own coverage.
requires_symlinks = pytest.mark.skipif(
    not hasattr(os, "symlink") or os.name == "nt",
    reason="planting a directory symlink requires privileges this platform may withhold",
)


@pytest.fixture
def owned_home(tmp_path, monkeypatch):
    """A Kiro Crew data home under *tmp_path*, so the walk has a trust anchor."""
    home = tmp_path / "datahome"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


@requires_symlinks
def test_secret_write_refuses_a_linked_parent(owned_home, tmp_path):
    """The reported shape: the parent directory is a pre-planted link."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, owned_home / "state")

    with pytest.raises(OSError, match="symlink or junction"):
        aw.atomic_write(owned_home / "state" / "token", SECRET, restrict_to_owner=True)

    assert not (elsewhere / "token").exists(), "the secret must not land at the link's target"


@requires_symlinks
def test_secret_write_refuses_a_linked_ancestor(owned_home, tmp_path):
    """A link ABOVE a not-yet-existing parent is the same redirect.

    ``mkdir(parents=True)`` would walk through the link and build the missing
    directories under its target, so the check has to run before the mkdir and
    has to look past the immediate parent.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, owned_home / "state")

    with pytest.raises(OSError, match="symlink or junction"):
        aw.atomic_write(owned_home / "state" / "deep" / "token", SECRET, restrict_to_owner=True)

    assert not (elsewhere / "deep").exists(), "no directory tree may be built under the target"


@requires_symlinks
def test_refusal_names_the_linked_component(owned_home, tmp_path):
    """The message must name the LINK, not just the destination.

    An operator reading the error has to know which directory to replace, and
    the destination path alone does not say which component of the chain is the
    link. The assertion names the component as the subject of the sentence
    because the destination string contains the link's path as a prefix — a
    plain substring check would pass even if the component were dropped.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = owned_home / "vaults"
    os.symlink(elsewhere, link)

    with pytest.raises(OSError) as excinfo:
        aw.atomic_write(link / "sub" / "pat", SECRET, restrict_to_owner=True)

    assert f"{link} is a symlink" in str(excinfo.value)


@requires_symlinks
def test_secret_write_allows_a_symlinked_data_home(tmp_path, monkeypatch):
    """A data home relocated onto another disk IS a link at the anchor."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    linked_home = tmp_path / "datahome"
    os.symlink(real, linked_home)
    monkeypatch.setenv("KIROCREW_HOME", str(linked_home))

    target = linked_home / "token"
    aw.atomic_write(target, SECRET, restrict_to_owner=True)

    assert target.read_text() == SECRET


@requires_symlinks
def test_secret_write_allows_real_subdirs_under_a_symlinked_data_home(tmp_path, monkeypatch):
    """Only the LINK components matter: real directories below one still write."""
    real = tmp_path / "elsewhere"
    (real / "sub").mkdir(parents=True)
    linked_home = tmp_path / "datahome"
    os.symlink(real, linked_home)
    monkeypatch.setenv("KIROCREW_HOME", str(linked_home))

    target = linked_home / "sub" / "token"
    aw.atomic_write(target, SECRET, restrict_to_owner=True)

    assert target.read_text() == SECRET


@requires_symlinks
def test_non_secret_write_is_unaffected(owned_home, tmp_path):
    """The guard is scoped to secret writes.

    Widening it to every ``atomic_write`` would change the behaviour of every
    non-secret persistence site in the repo, which is not what the report is
    about and is not something a bug fix gets to do.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, owned_home / "state")

    aw.atomic_write(owned_home / "state" / "notes.json", "{}")

    assert (elsewhere / "notes.json").read_text() == "{}"


@requires_symlinks
def test_leaf_link_is_replaced_not_followed(owned_home, tmp_path):
    """A link at the LEAF is not a redirect, so it must not be refused.

    ``os.replace`` does not follow the final component: it swaps the link itself
    for the new file, leaving the link's target untouched. Refusing here would
    reject a write that is already safe.
    """
    victim = tmp_path / "elsewhere" / "victim"
    victim.parent.mkdir()
    victim.write_text("original")
    state = owned_home / "state"
    state.mkdir()
    os.symlink(victim, state / "token")

    aw.atomic_write(state / "token", SECRET, restrict_to_owner=True)

    assert victim.read_text() == "original", "the link's target must not be overwritten"
    assert (state / "token").read_text() == SECRET
    assert not (state / "token").is_symlink()


@requires_symlinks
def test_binary_secret_payload_is_guarded_too(owned_home, tmp_path):
    """The guard sits ahead of the text/bytes split, so both payloads get it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, owned_home / "keys")

    with pytest.raises(OSError, match="symlink or junction"):
        aw.atomic_write(owned_home / "keys" / "hmac", b"\x00\x01\x02", restrict_to_owner=True)

    assert not (elsewhere / "hmac").exists()


@requires_symlinks
def test_warn_policy_does_not_downgrade_the_refusal(owned_home, tmp_path):
    """``restrict_on_error="warn"`` is about a failed lockdown, not a redirect.

    Two callers pass it because losing their state file is worse than a file
    another local user can read. Neither accepts writing the secret to a
    directory it did not name, so the refusal must still raise here — and the
    callers that swallow ``OSError`` then skip the write, which is fail-closed.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, owned_home / "state")

    with pytest.raises(OSError, match="symlink or junction"):
        aw.atomic_write(
            owned_home / "state" / "refresh.json",
            "{}",
            restrict_to_owner=True,
            restrict_on_error="warn",
        )

    assert not (elsewhere / "refresh.json").exists()


@requires_symlinks
def test_outside_owned_roots_still_refuses_the_planted_chain(tmp_path, monkeypatch):
    """A destination outside every Kiro Crew root keeps a best-effort check.

    There the walk stops at the first ancestor that already exists, because
    everything below that is a directory the write would create itself. A link
    planted at that boundary is still caught.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "datahome"))
    outside = tmp_path / "outside"
    outside.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, outside / "state")

    with pytest.raises(OSError, match="symlink or junction"):
        aw.atomic_write(outside / "state" / "deep" / "token", SECRET, restrict_to_owner=True)

    assert not (elsewhere / "deep").exists()


@requires_symlinks
def test_secret_write_refuses_a_link_pointing_back_into_the_owned_tree(owned_home):
    """An in-tree alias must be refused, not waved through as "contained".

    A link planted below the anchor whose target is the anchor itself resolves to
    a path trivially inside the owned tree, so a containment-style check passes
    it. The write still lands somewhere the caller never named -- at the anchor
    root, under the leaf's own name, where it can clobber a same-named file.
    """
    os.symlink(owned_home, owned_home / "state")

    with pytest.raises(OSError):
        aw.atomic_write(owned_home / "state" / "token", SECRET, restrict_to_owner=True)

    assert not (owned_home / "token").exists(), "the secret must not land at the anchor root"


@requires_symlinks
def test_secret_write_refuses_an_in_tree_alias_shadowing_a_deeper_link(owned_home, tmp_path):
    """The refusal must not short-circuit the rest of the chain.

    ``inner`` redirects out of the tree and ``alias`` points back at the anchor.
    A guard that stopped walking as soon as a component resolved onto the anchor
    would inspect neither, so both are planted here to pin that every component
    below the anchor is judged.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, owned_home / "inner")
    os.symlink(owned_home, owned_home / "alias")

    with pytest.raises(OSError):
        aw.atomic_write(owned_home / "alias" / "inner" / "token", SECRET, restrict_to_owner=True)

    assert not (elsewhere / "token").exists()


@requires_symlinks
def test_redirect_is_refused_even_when_link_detection_misses_it(owned_home, tmp_path, monkeypatch):
    """The resolved-path check must stand on its own.

    ``is_link_or_junction`` is the only thing that sees a Windows junction, but a
    reparse point a platform's ``realpath`` follows while ``islink`` reports
    False would slip past it. Detection is stubbed out here to leave exactly that
    gap: the write must still be refused because the parent resolves somewhere
    other than the path it was named.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, owned_home / "state")
    monkeypatch.setattr(aw.platform_compat, "is_link_or_junction", lambda _path: False)

    with pytest.raises(OSError, match="rather than"):
        aw.atomic_write(owned_home / "state" / "token", SECRET, restrict_to_owner=True)

    assert not (elsewhere / "token").exists()


def test_unresolvable_parent_chain_is_refused(owned_home, monkeypatch):
    """Fail closed: a chain that cannot be resolved is not a chain we trust.

    Resolution can fail (a symlink loop, a component vanishing mid-walk). The
    guard cannot show the write lands where it was named, and a secret write is
    the wrong place to assume the best.
    """
    monkeypatch.setattr(aw, "_resolved_or_none", lambda _path: None)

    with pytest.raises(OSError, match="cannot be resolved"):
        aw.atomic_write(owned_home / "sub" / "token", SECRET, restrict_to_owner=True)


@requires_symlinks
def test_anchor_split_names_the_shallowest_owned_root(owned_home):
    """The split must not let a component nominate itself as the anchor.

    ``alias`` resolves onto the data home, so it maps to an owned root at depth
    zero while the real root sits above it in the same chain. If the split
    returned that component as the anchor, the names below it would be empty and
    the walk would inspect nothing -- so the shallowest depth has to win, and
    the anchor has to come back in the caller's own lexical namespace.
    """
    os.symlink(owned_home, owned_home / "alias")

    anchor, names = aw._link_trust_anchor(owned_home / "alias")

    assert names == ("alias",)
    assert anchor.joinpath(*names) == owned_home / "alias"


@requires_symlinks
def test_in_tree_redirect_is_refused_not_accepted_as_contained(owned_home, monkeypatch):
    """Containment is not the test -- being where you were named is.

    ``state`` points at a sibling directory INSIDE the owned tree, so the write
    still lands under the data home and any containment check passes it. The
    secret is nonetheless in a directory the caller never named, where it can
    clobber a same-named file. Detection is stubbed out so the resolved-path
    comparison is the only thing left to answer.
    """
    (owned_home / "other").mkdir()
    os.symlink(owned_home / "other", owned_home / "state")
    monkeypatch.setattr(aw.platform_compat, "is_link_or_junction", lambda _path: False)

    with pytest.raises(OSError, match="rather than"):
        aw.atomic_write(owned_home / "state" / "token", SECRET, restrict_to_owner=True)

    assert not (owned_home / "other" / "token").exists()


@requires_symlinks
def test_relocated_data_home_under_the_kiro_home_still_writes(tmp_path, monkeypatch):
    """The most specific owned root has to win, or relocation breaks.

    The default layout nests two owned roots: the data home ``~/.kiro/crew``
    sits inside the kiro home ``~/.kiro``. Relocating the data home onto another
    disk makes ``crew`` itself a link. Anchoring on the OUTER root would put that
    link below the anchor and refuse a supported layout, so the anchor must be
    the innermost root containing the destination.

    ``config.paths`` memoises the resolved default home per process, so the
    caches are cleared alongside ``$HOME`` -- otherwise this test would read
    whichever home the process resolved first.
    """
    from kiro_crew.config import paths as config_paths

    relocated = tmp_path / "another-disk"
    relocated.mkdir()
    home = tmp_path / "home"
    (home / ".kiro").mkdir(parents=True)
    os.symlink(relocated, home / ".kiro" / "crew")
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(config_paths, "_resolved_home", None)
    monkeypatch.setattr(config_paths, "_config_dir_memo", None)

    target = home / ".kiro" / "crew" / "sub" / "token"
    aw.atomic_write(target, SECRET, restrict_to_owner=True)

    assert target.read_text() == SECRET
    assert (relocated / "sub" / "token").read_text() == SECRET


@requires_symlinks
def test_symlink_loop_is_refused_not_raised_through(owned_home, monkeypatch):
    """A looped parent must produce the refusal, never the resolver's own error.

    ``Path.resolve()`` reports a symlink loop differently by version: an
    ``OSError`` on the versions that delegate to ``os.path.realpath``, and a
    ``RuntimeError`` on Python 3.10, which this repo still supports. Either one
    escaping would crash the caller instead of refusing the write, and callers
    that swallow ``OSError`` around their secret write (the refresh-token store,
    the Discord resume store) would turn a crash into a lost interaction.
    Detection is stubbed out so the loop reaches the resolver at all.
    """
    (owned_home / "a").symlink_to(owned_home / "b")
    (owned_home / "b").symlink_to(owned_home / "a")
    monkeypatch.setattr(aw.platform_compat, "is_link_or_junction", lambda _path: False)

    with pytest.raises(OSError):
        aw.atomic_write(owned_home / "a" / "token", SECRET, restrict_to_owner=True)


def test_resolver_turns_a_runtime_error_into_cannot_prove(owned_home, monkeypatch):
    """Pin the 3.10 loop error explicitly, on every version.

    The test above exercises whichever error the host's ``pathlib`` raises, so on
    a 3.11+ host it never reaches the ``RuntimeError`` arm. Asserting on the
    wrapper keeps that arm covered everywhere: it has to report "cannot prove"
    (``None``), which the guard turns into a refusal, rather than letting the
    error escape into the caller's secret write.
    """

    def _boom(self, *args, **kwargs):
        raise RuntimeError(f"Symlink loop from {self!r}")

    monkeypatch.setattr(aw.Path, "resolve", _boom)

    assert aw._resolved_or_none(owned_home / "sub") is None


def test_a_resolver_that_raises_contributes_no_anchor(owned_home, monkeypatch):
    """A broken root resolver must cost an anchor, never the write.

    ``kiro_home()`` resolves its override, so a looped or hostile ``KIRO_HOME``
    can raise from inside root collection -- on 3.10 as ``RuntimeError``. The
    roots are best-effort by design: a resolver that cannot answer drops out and
    the remaining ones still anchor the walk.
    """
    from kiro_crew.config import paths as config_paths

    def _boom():
        raise RuntimeError("Symlink loop from KIRO_HOME")

    monkeypatch.setattr(config_paths, "kiro_home", _boom)
    target = owned_home / "sub" / "token"

    aw.atomic_write(target, SECRET, restrict_to_owner=True)

    assert target.read_text() == SECRET


def test_plain_secret_write_under_the_data_home_still_works(owned_home):
    """The guard must not cost the ordinary case anything."""
    target = owned_home / "sub" / "token"

    aw.atomic_write(target, SECRET, restrict_to_owner=True)

    assert target.read_text() == SECRET
