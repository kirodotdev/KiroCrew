"""``upload`` with several files: each through C's own path checks, one staging folder.

The handler runs against the scripted page of ``test_browser_harness_ops``.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import pytest
from test_browser_harness_ops import FakeBrowser

from kiro_crew.browser_harness import helper_worker, ops, upload


@pytest.fixture
def fake(tmp_path: Path) -> FakeBrowser:
    browser = FakeBrowser(tmp_path)
    browser.page["fileInput"] = {"ok": True, "multiple": True}
    browser.helpers["set_files"] = {"ok": True}
    return browser


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any, str]]:
    rows: list[tuple[str, Any, str]] = []
    monkeypatch.setattr(
        upload, "_audit", lambda path, size, outcome: rows.append((path, size, outcome))
    )
    return rows


def _ctx(fake: FakeBrowser) -> ops.OpContext:
    return dataclasses.replace(fake.ctx(), upload_dir=fake.tmp / "uploads")


def _files(tmp_path: Path, *names: str) -> list[str]:
    out = []
    for name in names:
        path = tmp_path / "work" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"bytes of {name}".encode())
        out.append(str(path))
    return out


def _calls(fake: FakeBrowser, helper: str) -> list[dict[str, Any]]:
    return [kw for h, kw in fake.calls if h == helper]


def _upload(fake: FakeBrowser, args: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(ops.run_op("upload", {"ref": "e4", **args}, _ctx(fake)))


def test_the_gateway_and_the_worker_cap_the_same_count() -> None:
    assert upload.UPLOAD_MAX_FILES == helper_worker._MAX_FILES


def test_several_files_go_to_a_multiple_input_from_one_staging_folder(
    fake: FakeBrowser, audits: list[tuple[str, Any, str]], tmp_path: Path
) -> None:
    paths = _files(tmp_path, "a.txt", "b.txt")
    out = _upload(fake, {"path": paths})
    assert out == {
        "ok": True,
        "ref": "e4",
        "files": [{"file": "a.txt", "size": 14}, {"file": "b.txt", "size": 14}],
    }
    (call,) = _calls(fake, "set_files")
    staged = [Path(f) for f in call["files"]]
    assert [p.name for p in staged] == ["a.txt", "b.txt"]
    # One upload folder, a sub-folder per file.
    assert staged[0].parent.parent == staged[1].parent.parent
    assert staged[0].parent.parent.parent == fake.tmp / "uploads"
    assert [p.read_bytes() for p in staged] == [b"bytes of a.txt", b"bytes of b.txt"]
    assert audits == [(paths[0], 14, "ok"), (paths[1], 14, "ok")]
    assert fake.steps[-1] == "Upload a.txt, b.txt to e4"


def test_several_files_on_a_single_input_are_refused_before_any_file_is_read(
    fake: FakeBrowser, audits: list[tuple[str, Any, str]], tmp_path: Path
) -> None:
    fake.page["fileInput"] = {"ok": True, "multiple": False}
    out = _upload(fake, {"path": _files(tmp_path, "a.txt", "b.txt")})
    assert out["code"] == "not_multiple"
    assert not (fake.tmp / "uploads").exists() and audits == []


@pytest.mark.parametrize("path", [[], ["/a"] * 11, None])
def test_a_path_list_must_hold_one_to_ten(fake: FakeBrowser, path: Any) -> None:
    assert _upload(fake, {"path": path})["code"] == "bad_path"


@pytest.mark.parametrize("name", ["a.txt", "A.TXT"])
def test_two_files_with_one_name_are_refused_and_nothing_is_staged(
    fake: FakeBrowser, tmp_path: Path, name: str
) -> None:
    # One name in any case: a case-insensitive disk would stage both as one file.
    first = _files(tmp_path, "a.txt")[0]
    other = tmp_path / "elsewhere" / name
    other.parent.mkdir()
    other.write_text("x")
    out = _upload(fake, {"path": [first, str(other)]})
    assert out["code"] == "duplicate_name"
    assert not any((fake.tmp / "uploads").glob("*/*"))
    assert _calls(fake, "set_files") == []


def test_the_files_together_are_capped(
    fake: FakeBrowser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upload, "UPLOAD_MAX_BYTES", 20)
    out = _upload(fake, {"path": _files(tmp_path, "a.txt", "b.txt")})
    assert out["code"] == "file_too_large"
    assert not any((fake.tmp / "uploads").glob("*/*"))
    assert _calls(fake, "set_files") == []


def test_a_refused_file_in_the_list_stages_none_of_them(
    fake: FakeBrowser, audits: list[tuple[str, Any, str]], tmp_path: Path
) -> None:
    good = _files(tmp_path, "a.txt")[0]
    missing = str(tmp_path / "missing.txt")
    out = _upload(fake, {"path": [good, missing]})
    assert out["code"] == "path_missing"
    assert not any((fake.tmp / "uploads").glob("*/*"))
    assert _calls(fake, "set_files") == []
    assert audits == [(good, None, "path_missing"), (missing, None, "path_missing")]


def test_one_path_still_answers_as_before(fake: FakeBrowser, tmp_path: Path) -> None:
    out = _upload(fake, {"path": _files(tmp_path, "a.txt")[0]})
    assert out == {"ok": True, "ref": "e4", "file": "a.txt", "size": 14}


def test_the_path_keystone_sees_every_file_of_a_list() -> None:
    from kiro_crew.platform.tool_paths import target_paths

    found = target_paths({"op": "upload", "args": {"ref": "e4", "path": ["/a/x", "/b/y"]}})
    assert list(found) == ["/a/x", "/b/y"]


def test_each_file_is_staged_in_a_folder_of_its_own(fake: FakeBrowser, tmp_path: Path) -> None:
    # Two names the disk may treat as one (here NFC and NFD) must not replace each other.
    first = tmp_path / "one" / "caf\u00e9.txt"
    second = tmp_path / "two" / "cafe\u0301.txt"
    for path, text in ((first, "first"), (second, "second")):
        path.parent.mkdir()
        path.write_text(text)
    staged = upload.stage_many([str(first), str(second)], fake.tmp / "uploads")
    assert [p.parent for p, _ in staged][0] != [p.parent for p, _ in staged][1]
    assert sorted(p.read_text() for p, _ in staged) == ["first", "second"]
