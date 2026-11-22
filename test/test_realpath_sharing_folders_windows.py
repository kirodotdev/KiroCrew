"""A batch on a backslash platform is resolved to ``os.path.realpath``'s answers, with fewer calls.

``_child_realpath.realpaths_sharing_folders`` reads ``/`` paths by walking their prefixes.
On Windows ``realpath`` also returns the on-disk letter case and expands 8.3 short names,
so that walk would drift from it. The Windows branch keeps ``realpath``'s own answers per
folder instead: a path whose leaf is missing is its parent's answer joined with the leaf.

These tests run everywhere. ``_child_realpath.os`` is replaced by a small model of a drive
that resolves the way ``ntpath``'s non-strict ``realpath`` does (the part that exists is
resolved, the rest is joined as spelled). A maintainer must still check the real thing on
Windows: the model is this file's reading of the standard library, not a Windows run.
"""

from __future__ import annotations

import ntpath
import os
from types import SimpleNamespace

import pytest

from kiro_crew.security import _child_realpath
from kiro_crew.security._child_realpath import realpath_many, realpaths_sharing_folders

_ROOT = "C:\\"
_DIR = "dir"
_FILE = "file"


class _Drive:
    """A drive of folders, files and links with ``ntpath``-style non-strict resolution."""

    def __init__(self) -> None:
        self.entries: dict[str, tuple[str, str]] = {}
        self.aliases: dict[str, str] = {}
        self.realpath_calls: list[str] = []

    def add(self, path: str, kind: str = _DIR, target: str = "") -> None:
        self.entries[path.casefold()] = (path, kind + ("\0" + target if target else ""))

    def _find(self, path: str) -> tuple[str, str, str] | None:
        key = self.aliases.get(path.casefold(), path.casefold())
        hit = self.entries.get(key)
        if hit is None:
            return None
        kind, _, target = hit[1].partition("\0")
        return hit[0], kind, target

    def _resolve(self, path: str) -> str:
        path = ntpath.normpath(path)
        if not path.casefold().startswith(_ROOT.casefold()):
            return path  # a drive this model does not hold
        names = [name for name in path[len(_ROOT) :].split("\\") if name]
        current = _ROOT
        for index, name in enumerate(names):
            hit = self._find(ntpath.join(current, name))
            if hit is None:
                return ntpath.join(current, *names[index:])
            current = hit[0] if not hit[2] else self._resolve(hit[2])
        return current

    def realpath(self, path: str) -> str:
        self.realpath_calls.append(path)
        return self._resolve(path)

    def lstat(self, path: str) -> object:
        parent, leaf = ntpath.split(path)
        if leaf:
            resolved = self._resolve(parent)
            if self._find(ntpath.join(resolved, leaf)) is None:
                raise FileNotFoundError(path)
        return object()


def _fake_os(drive: _Drive) -> SimpleNamespace:
    return SimpleNamespace(
        sep="\\",
        lstat=drive.lstat,
        fsencode=os.fsencode,
        fsdecode=os.fsdecode,
        path=SimpleNamespace(
            realpath=drive.realpath,
            split=ntpath.split,
            join=ntpath.join,
            normpath=ntpath.normpath,
            splitdrive=ntpath.splitdrive,
        ),
    )


def _drive_with_home() -> _Drive:
    drive = _Drive()
    for folder in (
        "C:\\Users",
        "C:\\Users\\Me",
        "C:\\Users\\Me\\.config",
        "C:\\Users\\Me\\real",
        "C:\\Users\\Me\\real\\aws",
        "C:\\Program Files",
        "C:\\elsewhere",
        "C:\\elsewhere\\ssh",
    ):
        drive.add(folder)
    drive.add("C:\\Users\\Me\\file", _FILE)
    drive.add("C:\\Users\\Me\\.aws", "link", "C:\\Users\\Me\\real\\aws")  # a junction
    drive.add("C:\\Users\\Me\\cfg", "link", "C:\\Users\\Me\\.config")  # a linked folder
    drive.add("C:\\Users\\Me\\.gnupg", "link", "C:\\nowhere")  # a dangling link at a leaf
    drive.aliases["c:\\progra~1"] = "c:\\program files"  # an 8.3 short name
    drive.aliases["c:\\users\\me\\progra~1"] = "c:\\users\\me\\real"
    return drive


_SPELLINGS = (
    "C:\\Users\\Me",
    "C:\\Users\\Me\\.config",  # a plain folder
    "C:\\Users\\Me\\.config\\gcloud",  # an absent leaf
    "C:\\Users\\Me\\.docker\\config.json",  # an absent leaf under an absent folder
    "C:\\Users\\Me\\.docker\\a\\b\\c",
    "C:\\Users\\Me\\.gnupg",  # a dangling link at the leaf
    "C:\\Users\\Me\\.gnupg\\x",
    "C:\\Users\\Me\\.aws",  # a junction
    "C:\\Users\\Me\\.aws\\credentials",
    "C:\\Users\\Me\\cfg\\gcloud",  # under a linked folder
    "C:\\Users\\Me\\file\\x",  # under a file
    "C:\\PROGRA~1\\App",  # an 8.3 short-name spelling
    "C:\\Users\\Me\\PROGRA~1\\aws\\x",
    "c:\\users\\ME\\.CONFIG\\gcloud",  # a different-case spelling
    "C:\\USERS\\me\\.Aws\\credentials",
    "C:\\",
    "C:\\Users\\Me\\..\\Me\\.ssh",  # not plain: ``realpath`` normalizes it
    "C:/Users/Me/.ssh",
    "C:\\Users\\Me\\.ssh\\",
    "\\\\host\\share\\x",
    "relative\\path",
    "",
)


@pytest.mark.parametrize("reverse", [False, True])
def test_every_answer_is_realpaths_answer(monkeypatch, reverse):
    drive = _drive_with_home()
    monkeypatch.setattr(_child_realpath, "os", _fake_os(drive))
    paths = list(_SPELLINGS[::-1] if reverse else _SPELLINGS)
    expected = [drive._resolve(path) for path in paths]
    assert realpaths_sharing_folders(paths) == expected
    assert realpath_many([path.encode() for path in paths]) == [
        answer.encode() for answer in expected
    ]


def test_a_batch_with_mostly_absent_leaves_makes_fewer_calls_than_paths(monkeypatch):
    drive = _drive_with_home()
    monkeypatch.setattr(_child_realpath, "os", _fake_os(drive))
    paths = [f"C:\\Users\\Me\\.config\\leaf-{i}" for i in range(30)]
    paths += [f"C:\\Users\\Me\\.absent\\leaf-{i}" for i in range(30)]
    expected = [drive._resolve(path) for path in paths]
    assert realpaths_sharing_folders(paths) == expected
    assert 0 < len(drive.realpath_calls) < len(paths)
    assert len(drive.realpath_calls) == len(set(drive.realpath_calls))


def test_a_leaf_that_exists_goes_to_realpath_whole(monkeypatch):
    drive = _drive_with_home()
    monkeypatch.setattr(_child_realpath, "os", _fake_os(drive))
    leaf = "C:\\Users\\Me\\.gnupg"
    assert realpaths_sharing_folders([leaf]) == ["C:\\nowhere"]
    assert drive.realpath_calls == [leaf]


def test_nothing_is_kept_from_one_batch_to_the_next(monkeypatch):
    drive = _drive_with_home()
    monkeypatch.setattr(_child_realpath, "os", _fake_os(drive))
    leaf = "C:\\Users\\Me\\.aws\\credentials"
    assert realpaths_sharing_folders([leaf]) == ["C:\\Users\\Me\\real\\aws\\credentials"]
    drive.add("C:\\Users\\Me\\.aws", "link", "C:\\elsewhere\\ssh")
    assert realpaths_sharing_folders([leaf]) == ["C:\\elsewhere\\ssh\\credentials"]


def test_a_path_that_cannot_be_read_answers_none_and_the_rest_still_answer(monkeypatch):
    drive = _drive_with_home()
    fake = _fake_os(drive)
    real_realpath = fake.path.realpath

    def refusing(path):  # type: ignore[no-untyped-def]
        if path == "C:\\Users\\Me\\.config":
            raise OSError("unreadable")
        return real_realpath(path)

    fake.path.realpath = refusing
    monkeypatch.setattr(_child_realpath, "os", fake)
    paths = ["C:\\Users", "C:\\Users\\Me\\.config", "C:\\Users\\Me\\real"]
    assert realpaths_sharing_folders(paths) == ["C:\\Users", None, "C:\\Users\\Me\\real"]
