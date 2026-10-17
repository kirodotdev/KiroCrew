"""``remove_retired_conductor_skill`` on a platform without descriptor-relative opens.

Where ``pinned_fs.supports_pinned_walk()`` is false (Windows), the cleanup keeps the
no-link final-name check, the bounded read and the identity checks around the open and
the unlink, by name. A generated revision is removed with its empty directory; an edited
copy, an oversized file and a directory that holds anything else are kept.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kiro_crew.skills as sk
from kiro_crew import pinned_fs

_FIXTURES = Path(__file__).parent / "fixtures" / "retired_conductor_skill"


@pytest.fixture()
def by_name_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(sk, "skills_dir", lambda: root)
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    return root


def _plant(root: Path, data: bytes) -> Path:
    skill = root / "conductor" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_bytes(data)
    return skill


@pytest.mark.parametrize("fixture", ["select-crew-v2.md", "select-crew-v1.md"])
def test_a_generated_revision_is_removed_with_its_empty_directory(
    by_name_root: Path, fixture: str
) -> None:
    skill = _plant(by_name_root, (_FIXTURES / fixture).read_bytes())

    assert sk.remove_retired_conductor_skill() is True
    assert not skill.exists()
    assert not skill.parent.exists()


def test_a_directory_holding_anything_else_survives_the_removal(by_name_root: Path) -> None:
    skill = _plant(by_name_root, (_FIXTURES / "select-crew-v2.md").read_bytes())
    (skill.parent / "notes.md").write_text("mine\n", encoding="utf-8")

    assert sk.remove_retired_conductor_skill() is True
    assert not skill.exists()
    assert (skill.parent / "notes.md").read_text(encoding="utf-8") == "mine\n"


def test_an_edited_copy_is_kept(by_name_root: Path) -> None:
    data = (_FIXTURES / "select-crew-v2.md").read_bytes() + b"\nmy own step\n"
    skill = _plant(by_name_root, data)

    assert sk.remove_retired_conductor_skill() is False
    assert skill.read_bytes() == data


def test_an_oversized_file_is_kept_unread(by_name_root: Path) -> None:
    data = b"x" * (sk._RETIRED_CONDUCTOR_SKILL_MAX_BYTES + 1)
    skill = _plant(by_name_root, data)

    assert sk.remove_retired_conductor_skill() is False
    assert skill.read_bytes() == data


def test_a_missing_skill_is_a_quiet_false(by_name_root: Path) -> None:
    (by_name_root / "conductor").mkdir()

    assert sk.remove_retired_conductor_skill() is False
    assert (by_name_root / "conductor").is_dir()
