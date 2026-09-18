"""The prompt path's chained-redirect and post-open checks.

Y1 the UNC check read only the FIRST hop, so link -> link -> share was open: the first
   ``readlink`` returns a local path, the test says no, and ``resolve()`` then follows the rest
   of the chain to the share. One hop is not a fence when hops compose.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from .test_producer import load_build

_AS_NT = ('    elif os.name == "nt":', "    elif True:")


# ---------------------------------------------------------------------------
# Y1
# ---------------------------------------------------------------------------
def test_a_chained_redirect_to_a_share_is_refused(tmp_path: pathlib.Path) -> None:
    """Two local hops and then a share. The first hop alone looks harmless."""
    mod = load_build(mutate=_AS_NT)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    third = agents_dir / "third.md"
    third.symlink_to("//attacker-host/share/persona.md")
    second = agents_dir / "second.md"
    second.symlink_to(third)
    first = agents_dir / "persona.md"
    first.symlink_to(second)

    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path(f"file://{first}", agents_dir)
    assert "network share" in str(caught.value)
    assert "attacker-host" in str(caught.value)


def test_a_single_hop_to_a_share_is_still_refused(tmp_path: pathlib.Path) -> None:
    """The case that already worked must keep working while the chain case is added."""
    mod = load_build(mutate=_AS_NT)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    link = agents_dir / "persona.md"
    link.symlink_to("//attacker-host/share/persona.md")

    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path(f"file://{link}", agents_dir)
    assert "network share" in str(caught.value)


def test_a_chain_of_local_links_is_not_refused(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: following the chain must not become refusing every chain.

    A persona reached through a couple of local links is the supported case the earlier
    over-broad version of this fence destroyed, so it is asserted here rather than assumed.
    """
    mod = load_build(mutate=_AS_NT)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    real = tmp_path / "shared" / "persona.md"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"a shared persona\n")
    mid = agents_dir / "mid.md"
    mid.symlink_to(real)
    first = agents_dir / "persona.md"
    first.symlink_to(mid)

    assert mod._resolve_prompt_path(f"file://{first}", agents_dir) == first


def test_a_redirect_cycle_is_refused_rather_than_followed(tmp_path: pathlib.Path) -> None:
    """A cycle has to terminate somewhere that is not an infinite loop."""
    mod = load_build(mutate=_AS_NT)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    a = agents_dir / "a.md"
    b = agents_dir / "b.md"
    a.symlink_to(b)
    b.symlink_to(a)

    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path(f"file://{a}", agents_dir)
    assert "chain of more than" in str(caught.value)


def test_the_hop_bound_is_a_bound_and_not_a_ban(tmp_path: pathlib.Path) -> None:
    """A chain inside the bound resolves; one past it is refused. Both, so the number means
    something rather than being a synonym for "refuse"."""
    mod = load_build(mutate=_AS_NT)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    real = tmp_path / "persona.md"
    real.write_bytes(b"ok\n")

    inside = real
    for i in range(mod._MAX_REDIRECT_HOPS - 2):
        nxt = agents_dir / f"hop{i}.md"
        nxt.symlink_to(inside)
        inside = nxt
    assert mod._resolve_prompt_path(f"file://{inside}", agents_dir) == inside


@pytest.mark.skipif(os.name != "posix", reason="builds a link chain and a mid-read failure")
def test_a_hop_reached_through_a_redirecting_ancestor_is_refused_before_it_is_statted(
    tmp_path: pathlib.Path,
) -> None:
    """A hop out of a link's contents is reached through ancestors no walk has judged.

    ``lstat`` answers about the entry it is given and says nothing about the components on
    the way to it, so statting such a hop crosses its ancestors. On Windows an ancestor that
    is a reparse point naming a share makes that stat the outbound SMB probe this walk exists
    to prevent, reached by a path the walk never saw.
    """
    mod = load_build(mutate=_AS_NT)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    # An ancestor directory that redirects to a share, with the leaf beneath it entirely
    # ordinary: the leaf's own name and type reveal nothing.
    shared = tmp_path / "via"
    shared.symlink_to("//attacker-host/share", target_is_directory=True)
    first = agents_dir / "persona.md"
    first.symlink_to(shared / "sub" / "persona.md")

    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path(f"file://{first}", agents_dir)
    assert "network share" in str(caught.value), str(caught.value)


def test_the_shape_screen_runs_before_anything_touches_the_filesystem(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A guard that must touch its subject cannot be the outermost one.

    On Windows the touch IS the probe: ``lstat`` on a path whose anchor is a share reaches
    that host, so a walk that starts by statting would perform the exchange it is looking
    for. The string test reads characters and reaches nothing, so it runs in front.

    Observed by call ORDER rather than by re-deriving the rule: every filesystem question the
    walk can ask is recorded, and a share-shaped hop must produce none of them.
    """
    mod = load_build(mutate=_AS_NT)
    asked: list[str] = []
    real = mod._is_redirecting_entry

    def _record(p):
        asked.append(str(p))
        return real(p)

    monkeypatch.setattr(mod, "_is_redirecting_entry", _record)
    with pytest.raises(mod.ExportRefused) as caught:
        mod._refuse_share_reached_through_ancestors(pathlib.Path("//attacker-host/share/x.md"))
    assert "network share" in str(caught.value), str(caught.value)
    assert asked == [], f"the filesystem was asked about a share-shaped path: {asked}"
