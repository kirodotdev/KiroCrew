"""A subagent result path is emitted in the home spelling its reader can use.

Regression guard for the case where the data home sits under a SYMLINKED home
directory. ``/home/<user> -> /local/home/<user>`` is the ordinary layout on an
Amazon cloud desktop, and there a path built through ``Path.resolve()`` comes
back with a ``/local/home/...`` prefix that does not match the ``$HOME`` a
reader's path allowlist was keyed on. The file is perfectly readable; only the
spelling is unrecognized, so the read is refused rather than failing, and the
refusal surfaces as an approval prompt that times out instead of an error
anybody can act on.

The invariant these tests pin: every subagent path handed to a reader as TEXT
carries the declared home spelling, while the paths used to actually open files
stay symlink-resolved so the traversal check remains sound.

Each test asserts an observable emission, not an internal call, so reverting
``agent_dir_for_display`` back to ``_agent_dir`` at any of the emission sites
fails it.
"""

import os
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew import subagent_persistence
from kiro_crew.subagent_persistence import (
    _agent_dir,
    agent_dir_for_display,
    create_agent_folder,
    write_result_chunk,
)

_AGENT_ID = "a1b2c3d4"


@pytest.fixture
def symlinked_home(tmp_path, monkeypatch):
    """A data home reached through a symlink, mirroring /home -> /local/home.

    Yields ``(declared, real)`` data-home paths. ``declared`` is what a caller
    is configured with and what its allowlist knows; ``real`` is what
    ``Path.resolve()`` returns. On a platform without usable symlinks the test
    is skipped rather than silently asserting nothing.
    """
    real = tmp_path / "local" / "home" / "u" / ".kiro" / "crew"
    real.mkdir(parents=True)
    link_root = tmp_path / "home"
    link_root.mkdir()
    try:
        (link_root / "u").symlink_to(tmp_path / "local" / "home" / "u")
    except (OSError, NotImplementedError):  # pragma: no cover - platform guard
        pytest.skip("symlinks unavailable on this platform")

    declared = link_root / "u" / ".kiro" / "crew"
    if declared.resolve() == declared:  # pragma: no cover - platform guard
        pytest.skip("symlink not resolved distinctly on this platform")

    monkeypatch.setattr(
        subagent_persistence, "_SUBAGENTS_DIR", declared / "subagents", raising=False
    )
    (declared / "subagents").mkdir(parents=True, exist_ok=True)
    yield declared, real


class TestTheTwoSpellingsAreActuallyDifferent:
    """Guards the fixture itself: without a real divergence nothing is proven."""

    def test_the_fixture_produces_a_distinct_resolved_prefix(self, symlinked_home):
        declared, real = symlinked_home
        assert str(declared) != str(real)
        assert declared.resolve() == real.resolve()


class TestDisplayPathsUseTheDeclaredHome:
    def test_display_helper_returns_the_declared_spelling(self, symlinked_home):
        declared, real = symlinked_home
        shown = agent_dir_for_display(_AGENT_ID)
        assert str(shown).startswith(str(declared))
        assert str(real) not in str(shown)

    def test_io_helper_still_returns_the_resolved_spelling(self, symlinked_home):
        _declared, real = symlinked_home
        opened = _agent_dir(_AGENT_ID)
        assert str(opened).startswith(str(real))

    def test_both_spellings_name_the_same_file(self, symlinked_home):
        create_agent_folder(_AGENT_ID, task="t", agent="")
        write_result_chunk(_AGENT_ID, "payload")
        shown = agent_dir_for_display(_AGENT_ID) / "result.txt"
        opened = _agent_dir(_AGENT_ID) / "result.txt"
        assert shown.read_text(encoding="utf-8") == "payload"
        assert os.path.samefile(shown, opened)

    def test_display_helper_delegates_traversal_validation(self, symlinked_home):
        for bad in ("", ".", "..", "a/b", "a\\b", "a\0b"):
            with pytest.raises(ValueError):
                agent_dir_for_display(bad)


class TestEmittedResultPointersAreReadable:
    """Drives the real emission, not a copy of it.

    ``_notify_orphan`` builds the completion notice a parent agent is told to
    go read. With no parent session there is no injection surface, so it takes
    the undelivered branch and hands the message straight back without touching
    ``self`` -- which is what makes it callable here against a bare stub.
    """

    def _notice(self, agent_id: str) -> str:
        import asyncio

        from kiro_crew.subagent import SubagentManager

        stub = object.__new__(SubagentManager)  # never touched on this branch
        msg = asyncio.run(
            SubagentManager._notify_orphan(
                stub,
                agent_id,
                {"task": "summarize the diff", "parent_session": ""},
                recovery="undeliverable",
                has_result=True,
            )
        )
        assert msg is not None, "the undelivered branch must hand the message back"
        return msg

    def test_orphan_notice_names_a_path_the_reader_can_use(self, symlinked_home):
        declared, real = symlinked_home
        msg = self._notice(_AGENT_ID)
        assert str(declared) in msg, f"declared home missing from notice: {msg}"
        assert str(real) not in msg, (
            "the notice hands out the symlink-resolved spelling, which the "
            f"reader's allowlist does not recognize: {msg}"
        )

    def test_the_named_path_is_actually_openable(self, symlinked_home):
        create_agent_folder(_AGENT_ID, task="t", agent="")
        write_result_chunk(_AGENT_ID, "payload")
        msg = self._notice(_AGENT_ID)
        # Pull the backticked path back out of the notice and open it, so the
        # test proves the emitted text is usable rather than merely well-spelled.
        quoted = [p for p in msg.split("`") if p.endswith("result.txt")]
        assert quoted, f"no result path found in notice: {msg}"
        assert Path(quoted[0]).read_text(encoding="utf-8") == "payload"


class TestNoEmissionSiteStillUsesTheResolvedHelper:
    """A grep-level guard so a NEW emission site cannot quietly regress this.

    Reads the source rather than exercising every call path: the point is that
    ``_agent_dir`` may only reach file operations, and a future edit that
    stringifies it into a message would otherwise pass unnoticed.
    """

    _SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

    def test_agent_dir_is_never_stringified_into_text(self):
        offenders = []
        for rel in ("subagent.py", "mcp_tools/spawn.py"):
            # Explicit utf-8: these sources carry non-ASCII in comments, and a
            # Windows runner's default cp1252 cannot decode them.
            source = (self._SRC / rel).read_text(encoding="utf-8")
            for lineno, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or "_agent_dir(" not in stripped:
                    continue
                if "agent_dir_for_display(" in stripped:
                    continue
                # str(...) or an f-string interpolation means the path is
                # becoming text somebody will read, not a file being opened.
                if "str(_agent_dir(" in stripped or "{_agent_dir(" in stripped:
                    offenders.append(f"{rel}:{lineno}: {stripped}")
        assert not offenders, (
            "these sites stringify the symlink-resolved path into text; use "
            "agent_dir_for_display() so the reader's allowlist recognizes it:\n"
            + "\n".join(offenders)
        )


class TestUnsymlinkedHomeIsUnchanged:
    """On an ordinary home the two helpers agree, so nothing else shifts."""

    def test_helpers_agree_when_home_is_not_a_symlink(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subagent_persistence, "_SUBAGENTS_DIR", tmp_path / "subagents", raising=False
        )
        (tmp_path / "subagents").mkdir()
        assert agent_dir_for_display(_AGENT_ID).resolve() == _agent_dir(_AGENT_ID)

    def test_display_helper_does_not_touch_the_filesystem(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subagent_persistence, "_SUBAGENTS_DIR", tmp_path / "subagents", raising=False
        )
        (tmp_path / "subagents").mkdir()
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("must not create")):
            agent_dir_for_display(_AGENT_ID)
