"""The session's own ``$KIROCREW_SCRATCH`` directory as an outbound-image root.

The agent is told to stage screenshots under ``$KIROCREW_SCRATCH``, a directory
the gateway allocates per runtime under the managed scratch root -- never under
the working directory. Inline image extraction must therefore admit THAT one
directory beside the cwd, and nothing wider: not the managed scratch root, not
another runtime's scratch, not a path that only reaches scratch by ``..`` or by a
link.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import agent_scratch
from kiro_crew.messaging.outbound_files import (
    REASON_SENSITIVE,
    REASON_SYMLINK,
    REASON_UNREADABLE,
    approved_roots,
    containing_root,
    extract_local_refs,
    extract_local_refs_off_loop,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

#: A linked ANCESTOR under an admitted root, aimed at a LOCAL directory outside
#: it: Windows rewrites the local linked prefix to its target and POSIX resolves
#: through it, so on both the descriptor-pinned read finds the bytes outside the
#: root and answers ``REASON_UNREADABLE``.
_LINKED_ANCESTOR_REASON = REASON_UNREADABLE


def _png(directory: Path, name: str = "shot.png") -> Path:
    p = directory / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_PNG)
    return p


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """An isolated data home, a project cwd, and two allocated scratch dirs."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.delenv("KIROCREW_SCRATCH_ROOT", raising=False)
    cwd = tmp_path / "project"
    cwd.mkdir()
    mine = agent_scratch.allocate_scratch("runtime")
    other = agent_scratch.allocate_scratch("runtime")
    assert mine != other and mine.parent == other.parent
    return cwd, mine, other


def _refused_as_outside(result: object) -> None:
    assert result.files == []  # type: ignore[attr-defined]
    rejection = result.rejections[0]  # type: ignore[attr-defined]
    assert rejection.reason == REASON_SENSITIVE
    assert "outside the approved workspace" in str(rejection)


class TestOwnScratchIsAdmitted:
    def test_an_image_in_the_sessions_own_scratch_is_uploaded(
        self, layout: tuple[Path, Path, Path]
    ) -> None:
        cwd, mine, _ = layout
        shot = _png(mine, "repro-1.png")
        result = extract_local_refs(
            f"![shot]({shot})", within_root=str(cwd), scratch_root=str(mine)
        )
        assert result.rejections == []
        assert [f.path for f in result.files] == [str(shot)]
        assert result.files[0].data == _PNG

    def test_a_nested_file_in_own_scratch_is_uploaded(
        self, layout: tuple[Path, Path, Path]
    ) -> None:
        cwd, mine, _ = layout
        shot = _png(mine / "shots" / "a", "b.png")
        result = extract_local_refs(f"![x]({shot})", within_root=str(cwd), scratch_root=str(mine))
        assert [f.path for f in result.files] == [str(shot)]

    def test_the_scratch_root_is_pinned_canonically_and_listed_last(
        self, layout: tuple[Path, Path, Path]
    ) -> None:
        cwd, mine, _ = layout
        roots = approved_roots(str(cwd), scratch_root=str(mine))
        assert roots[0].lexical == str(cwd)
        scratch = roots[-1]
        assert scratch.lexical == str(mine)
        assert scratch.read_root == os.path.join(os.path.realpath(mine.parent), mine.name)
        assert scratch.read_root_is_canonical is True

    @pytest.mark.asyncio
    async def test_the_async_form_admits_own_scratch(self, layout: tuple[Path, Path, Path]) -> None:
        cwd, mine, _ = layout
        shot = _png(mine)
        result = await extract_local_refs_off_loop(
            f"![x]({shot})", within_root=str(cwd), scratch_root=str(mine)
        )
        assert [f.path for f in result.files] == [str(shot)]


class TestNothingWiderIsAdmitted:
    def test_without_a_scratch_root_scratch_stays_refused(
        self, layout: tuple[Path, Path, Path]
    ) -> None:
        """The pre-existing behaviour for callers that hand no scratch root."""
        cwd, mine, _ = layout
        shot = _png(mine)
        _refused_as_outside(extract_local_refs(f"![x]({shot})", within_root=str(cwd)))

    def test_no_authorized_cwd_means_no_roots_even_with_scratch(
        self, layout: tuple[Path, Path, Path]
    ) -> None:
        """Scratch never stands in for an upload authorization that was withheld."""
        _, mine, _ = layout
        assert approved_roots("", scratch_root=str(mine)) == ()
        assert containing_root(_png(mine), "", scratch_root=str(mine)) is None

    def test_another_sessions_scratch_is_refused(self, layout: tuple[Path, Path, Path]) -> None:
        cwd, mine, other = layout
        theirs = _png(other, "secret.png")
        assert containing_root(theirs, str(cwd), scratch_root=str(mine)) is None
        _refused_as_outside(
            extract_local_refs(f"![x]({theirs})", within_root=str(cwd), scratch_root=str(mine))
        )

    def test_a_traversal_out_of_own_scratch_is_refused(
        self, layout: tuple[Path, Path, Path]
    ) -> None:
        """``..`` is collapsed before comparison, so the spelling buys nothing."""
        cwd, mine, other = layout
        theirs = _png(other, "secret.png")
        spelled = mine / ".." / other.name / theirs.name
        assert containing_root(spelled, str(cwd), scratch_root=str(mine)) is None
        _refused_as_outside(
            extract_local_refs(f"![x]({spelled})", within_root=str(cwd), scratch_root=str(mine))
        )

    def test_the_managed_scratch_root_itself_is_refused(
        self, layout: tuple[Path, Path, Path]
    ) -> None:
        """A file directly under the managed root is in nobody's own scratch."""
        cwd, mine, _ = layout
        loose = _png(mine.parent, "loose.png")
        _refused_as_outside(
            extract_local_refs(f"![x]({loose})", within_root=str(cwd), scratch_root=str(mine))
        )

    @pytest.mark.parametrize("which", ["managed-root", "data-home", "filesystem-root"])
    def test_a_scratch_root_wider_than_one_allocation_admits_nothing(
        self, layout: tuple[Path, Path, Path], which: str
    ) -> None:
        """Only a direct child of the managed scratch root is ever an approved root."""
        cwd, mine, other = layout
        wide = {
            "managed-root": mine.parent,
            "data-home": mine.parent.parent,
            "filesystem-root": Path(os.path.abspath(os.sep)),
        }[which]
        roots = approved_roots(str(cwd), scratch_root=str(wide))
        assert [r.lexical for r in roots if r.lexical == str(wide)] == []
        theirs = _png(other, "secret.png")
        _refused_as_outside(
            extract_local_refs(f"![x]({theirs})", within_root=str(cwd), scratch_root=str(wide))
        )

    def test_a_directory_outside_the_managed_root_admits_nothing(
        self, layout: tuple[Path, Path, Path], tmp_path: Path
    ) -> None:
        cwd, _, _ = layout
        fake = tmp_path / "not-scratch"
        shot = _png(fake)
        _refused_as_outside(
            extract_local_refs(f"![x]({shot})", within_root=str(cwd), scratch_root=str(fake))
        )

    def test_a_relative_scratch_root_admits_nothing(self, layout: tuple[Path, Path, Path]) -> None:
        cwd, mine, _ = layout
        shot = _png(mine)
        _refused_as_outside(
            extract_local_refs(f"![x]({shot})", within_root=str(cwd), scratch_root=mine.name)
        )

    @requires_symlinks
    def test_a_symlink_leaf_in_own_scratch_is_refused(
        self, layout: tuple[Path, Path, Path], tmp_path: Path
    ) -> None:
        cwd, mine, _ = layout
        real = _png(tmp_path / "elsewhere", "secret.png")
        link = mine / "shot.png"
        link.symlink_to(real)
        result = extract_local_refs(f"![x]({link})", within_root=str(cwd), scratch_root=str(mine))
        assert result.files == [] and result.rejections[0].reason == REASON_SYMLINK

    @requires_symlinks
    def test_a_linked_directory_escaping_own_scratch_is_refused(
        self, layout: tuple[Path, Path, Path], tmp_path: Path
    ) -> None:
        """Lexically inside scratch, but the opened descriptor sits outside it."""
        cwd, mine, _ = layout
        real = _png(tmp_path / "elsewhere", "secret.png")
        (mine / "alias").symlink_to(real.parent, target_is_directory=True)
        through = mine / "alias" / real.name
        admitted = containing_root(through, str(cwd), scratch_root=str(mine))
        assert admitted is not None and admitted.lexical == str(mine)
        result = extract_local_refs(
            f"![x]({through})", within_root=str(cwd), scratch_root=str(mine)
        )
        assert result.files == []
        assert result.rejections[0].reason == _LINKED_ANCESTOR_REASON

    @requires_symlinks
    def test_a_scratch_dir_that_is_itself_a_link_admits_nothing(
        self, layout: tuple[Path, Path, Path], tmp_path: Path
    ) -> None:
        """A link planted at an allocation's name does not move the root to its target."""
        cwd, mine, _ = layout
        elsewhere = tmp_path / "elsewhere"
        shot = _png(elsewhere)
        planted = mine.parent / "runtime-deadbeef"
        planted.symlink_to(elsewhere, target_is_directory=True)
        spelled = planted / shot.name
        assert containing_root(spelled, str(cwd), scratch_root=str(planted)) is None
        _refused_as_outside(
            extract_local_refs(f"![x]({spelled})", within_root=str(cwd), scratch_root=str(planted))
        )


class TestSlackWiring:
    """The Slack renderer hands its dispatcher-authorized scratch to extraction."""

    @staticmethod
    def _renderer() -> object:
        from unittest.mock import MagicMock

        from kiro_crew.slack.renderer import SlackRenderer

        return SlackRenderer(MagicMock(), "C1", "t1", reactions_enabled=False, show_thinking=False)

    @pytest.mark.asyncio
    async def test_an_image_in_own_scratch_is_extracted_for_upload(
        self, layout: tuple[Path, Path, Path]
    ) -> None:
        cwd, mine, _ = layout
        shot = _png(mine, "repro.png")
        renderer = self._renderer()
        renderer.authorize_upload_root(str(cwd), scratch_root=str(mine))  # type: ignore[attr-defined]
        body, files, notes = await renderer._extract_uploads(  # type: ignore[attr-defined]
            f"see:\n\n![x]({shot})\n"
        )
        assert [f.path for f in files] == [str(shot)]
        assert notes == "" and str(shot) not in body

    @pytest.mark.asyncio
    async def test_another_sessions_scratch_is_still_refused_visibly(
        self, layout: tuple[Path, Path, Path]
    ) -> None:
        cwd, mine, other = layout
        theirs = _png(other, "secret.png")
        renderer = self._renderer()
        renderer.authorize_upload_root(str(cwd), scratch_root=str(mine))  # type: ignore[attr-defined]
        _, files, notes = await renderer._extract_uploads(f"![x]({theirs})")  # type: ignore[attr-defined]
        assert files == [] and "outside the approved workspace" in notes

    def test_scratch_is_dropped_when_the_cwd_is_not_authorized(
        self, layout: tuple[Path, Path, Path]
    ) -> None:
        _, mine, _ = layout
        renderer = self._renderer()
        renderer.authorize_upload_root("relative/cwd", scratch_root=str(mine))  # type: ignore[attr-defined]
        assert renderer._upload_root == "" and renderer._scratch_root == ""  # type: ignore[attr-defined]

    def test_the_dispatcher_reads_the_runtimes_scratch_dir(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from kiro_crew.slack.transport_dispatch import _own_scratch_dir

        assert _own_scratch_dir(SimpleNamespace(work_scratch_dir=tmp_path)) == str(tmp_path)
        assert _own_scratch_dir(SimpleNamespace(work_scratch_dir=None)) == ""
        assert _own_scratch_dir(SimpleNamespace()) == ""
        assert _own_scratch_dir(SimpleNamespace(work_scratch_dir=object())) == ""
