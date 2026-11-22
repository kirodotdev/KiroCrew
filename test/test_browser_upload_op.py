"""The ``browser`` tool's ``upload`` op: one checked local file into an ``<input type=file>``.

Several files: ``test_browser_upload_many``.

The handler (``browser_harness.upload``) runs against the scripted page of
``test_browser_harness_ops``. The helper worker's half
(``helper_worker._local_set_files``) is checked against a scripted CDP session.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from pathlib import Path
from typing import Any

import pytest
from test_browser_harness_ops import FakeBrowser

from conftest import requires_symlinks
from kiro_crew import hooks
from kiro_crew.browser_harness import helper_worker, ops, supervisor, upload


@pytest.fixture
def fake(tmp_path: Path) -> FakeBrowser:
    browser = FakeBrowser(tmp_path)
    browser.page["fileInput"] = {"ok": True}
    browser.helpers["set_files"] = {"ok": True}
    return browser


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any, str]]:
    rows: list[tuple[str, Any, str]] = []
    monkeypatch.setattr(
        upload, "_audit", lambda path, size, outcome: rows.append((path, size, outcome))
    )
    return rows


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _ctx(fake: FakeBrowser) -> ops.OpContext:
    return dataclasses.replace(fake.ctx(), upload_dir=fake.tmp / "uploads")


def _file(tmp_path: Path, name: str = "report.csv", data: bytes = b"a,b\n1,2\n") -> Path:
    path = tmp_path / "work" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _set_files_calls(fake: FakeBrowser) -> list[dict[str, Any]]:
    return [kw for h, kw in fake.calls if h == "set_files"]


def test_upload_is_an_advertised_op_with_its_helper_on_both_allowlists() -> None:
    assert "upload" in ops.OPS
    assert supervisor.HELPERS["set_files"] == ("expression", "files")
    assert helper_worker._ALLOWLIST["set_files"] == (
        "_local_set_files",
        ("expression", "files"),
    )


def test_upload_gives_the_browser_a_staged_copy_with_the_same_name(
    fake: FakeBrowser, audits: list[tuple[str, Any, str]], tmp_path: Path
) -> None:
    source = _file(tmp_path)
    out = run(ops.run_op("upload", {"ref": "e4", "path": str(source)}, _ctx(fake)))
    assert out == {"ok": True, "ref": "e4", "file": "report.csv", "size": 8}
    (call,) = _set_files_calls(fake)
    assert call["expression"] == ops.WALKER_SOURCE + '.fileInput("e4", true)'
    (staged,) = call["files"]
    staged_path = Path(staged)
    assert staged_path != source
    assert staged_path.name == "report.csv"
    assert staged_path.parent.parent.parent == fake.tmp / "uploads"
    assert staged_path.read_bytes() == source.read_bytes()
    assert audits == [(str(source), 8, "ok")]
    assert fake.steps[-1] == "Upload report.csv to e4"


def test_the_target_is_judged_before_any_file_is_read(
    fake: FakeBrowser, audits: list[tuple[str, Any, str]], tmp_path: Path
) -> None:
    source = _file(tmp_path)
    for code in ("not_file_input", "disabled", "not_visible", "ref_stale"):
        fake.page["fileInput"] = {"ok": False, "code": code}
        out = run(ops.run_op("upload", {"ref": "e4", "path": str(source)}, _ctx(fake)))
        assert out["ok"] is False and out["code"] == code
    assert _set_files_calls(fake) == []
    assert not (fake.tmp / "uploads").exists()
    assert audits == []


@pytest.mark.parametrize("ref", [None, "x1", "f1e2"])
def test_a_bad_or_cross_origin_frame_ref_is_refused(fake: FakeBrowser, ref: Any) -> None:
    out = run(ops.run_op("upload", {"ref": ref, "path": "/tmp/x"}, _ctx(fake)))
    assert out["code"] == "bad_ref"
    assert "fileInput" not in fake.page_methods()


def test_upload_without_a_staging_leaf_is_not_available(fake: FakeBrowser, tmp_path: Path) -> None:
    out = run(ops.run_op("upload", {"ref": "e4", "path": str(_file(tmp_path))}, fake.ctx()))
    assert out["code"] == "not_available"


def _refusal_codes(
    fake: FakeBrowser, audits: list[tuple[str, Any, str]], cases: dict[str, str]
) -> None:
    for path, code in cases.items():
        out = run(ops.run_op("upload", {"ref": "e4", "path": path}, _ctx(fake)))
        assert out["ok"] is False and out["code"] == code, path
    assert _set_files_calls(fake) == []
    assert [row[2] for row in audits] == list(cases.values())


def test_path_refusals(
    fake: FakeBrowser, audits: list[tuple[str, Any, str]], tmp_path: Path
) -> None:
    _file(tmp_path)
    _refusal_codes(
        fake,
        audits,
        {
            "": "bad_path",
            "work/report.csv": "bad_path",
            str(tmp_path / "work" / "gone.csv"): "path_missing",
            # A folder goes only into a folder field (``webkitdirectory``).
            str(tmp_path / "work"): "not_folder_input",
        },
    )


@requires_symlinks
def test_a_final_link_is_refused(
    fake: FakeBrowser, audits: list[tuple[str, Any, str]], tmp_path: Path
) -> None:
    link = tmp_path / "work" / "link.csv"
    link.symlink_to(_file(tmp_path))
    _refusal_codes(fake, audits, {str(link): "path_symlink"})


def test_a_file_with_another_hard_link_is_refused(
    fake: FakeBrowser, audits: list[tuple[str, Any, str]], tmp_path: Path
) -> None:
    source = _file(tmp_path)
    try:
        os.link(source, tmp_path / "work" / "alias.csv")
    except OSError:
        pytest.skip("this file system has no hard links")
    _refusal_codes(fake, audits, {str(source): "path_refused"})
    assert not any((fake.tmp / "uploads").rglob("*.csv"))


def test_a_file_over_the_cap_is_refused(
    fake: FakeBrowser,
    audits: list[tuple[str, Any, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upload, "UPLOAD_MAX_BYTES", 4)
    out = run(ops.run_op("upload", {"ref": "e4", "path": str(_file(tmp_path))}, _ctx(fake)))
    assert out["code"] == "file_too_large"
    assert _set_files_calls(fake) == []


def _secret_is_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """``hooks.is_sensitive_path`` is the gate ``hooks.safe_copy_file_nolink`` reads."""
    monkeypatch.setattr(
        hooks,
        "is_sensitive_path",
        lambda p, base_dir=None: f"{os.sep}secret{os.sep}" in p,
    )


@requires_symlinks
def test_a_sensitive_path_is_refused_as_given_and_as_resolved(
    fake: FakeBrowser,
    audits: list[tuple[str, Any, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "key.pem").write_bytes(b"k")
    (tmp_path / "alias").symlink_to(secret_dir, target_is_directory=True)
    _secret_is_sensitive(monkeypatch)
    for path in (secret_dir / "key.pem", tmp_path / "alias" / "key.pem"):
        out = run(ops.run_op("upload", {"ref": "e4", "path": str(path)}, _ctx(fake)))
        assert out["code"] == "path_refused", path
    assert _set_files_calls(fake) == []
    assert [row[2] for row in audits] == ["path_refused", "path_refused"]
    assert not any((fake.tmp / "uploads").rglob("key.pem"))


@requires_symlinks
@pytest.mark.parametrize("flip_back", [False, True])
def test_a_folder_swapped_for_a_link_between_check_and_open_is_refused(
    fake: FakeBrowser,
    audits: list[tuple[str, Any, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flip_back: bool,
) -> None:
    work, secret, parked = tmp_path / "work", tmp_path / "secret", tmp_path / "parked"
    for folder, data in ((work, b"decoy"), (secret, b"SECRET")):
        folder.mkdir()
        (folder / "x.env").write_bytes(data)
    _secret_is_sensitive(monkeypatch)
    from kiro_crew import platform_compat

    # The open the copy judges, on every OS (on Windows it is not ``os.open``).
    real_open = platform_compat.open_file_no_reparse
    opened_name = f"{os.sep}work{os.sep}x.env"
    swapped: list[str] = []

    def racing_open(path: Any, *rest: Any, **kw: Any) -> int:
        if not str(path).endswith(opened_name):
            return real_open(path, *rest, **kw)
        swapped.append(str(path))
        work.rename(parked)
        work.symlink_to(secret, target_is_directory=True)
        try:
            return real_open(path, *rest, **kw)
        finally:
            if flip_back:
                work.unlink()
                parked.rename(work)

    monkeypatch.setattr(platform_compat, "open_file_no_reparse", racing_open)
    out = run(ops.run_op("upload", {"ref": "e4", "path": str(work / "x.env")}, _ctx(fake)))
    assert swapped, "the race never ran"
    assert out["code"] == "path_refused"
    assert _set_files_calls(fake) == []
    assert not any((fake.tmp / "uploads").rglob("*"))
    assert audits == [(str(work / "x.env"), None, "path_refused")]


def test_a_node_gone_at_set_time_is_stale_and_its_copy_removed(
    fake: FakeBrowser, audits: list[tuple[str, Any, str]], tmp_path: Path
) -> None:
    fake.helpers["set_files"] = {"ok": False}
    out = run(ops.run_op("upload", {"ref": "e4", "path": str(_file(tmp_path))}, _ctx(fake)))
    assert out["code"] == "ref_stale"
    assert list((fake.tmp / "uploads").iterdir()) == []
    assert audits[-1][2] == "ref_stale"


def test_staging_keeps_only_recent_copies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upload, "UPLOAD_KEEP", 3)
    upload_dir = tmp_path / "uploads"
    source = _file(tmp_path)
    now = 1_000_000.0
    old = upload_dir / "old"
    old.mkdir(parents=True)
    os.utime(old, (now - upload.UPLOAD_KEEP_S - 1, now - upload.UPLOAD_KEEP_S - 1))
    made = []
    for i in range(4):
        made_one = upload.stage(upload.expand([str(source)])[0], upload_dir, now=now + i)
        ((_source, staged, _size),) = made_one.files
        os.utime(staged.parent.parent, (now + i, now + i))
        made.append(staged.parent.parent)
    left = sorted(p.name for p in upload_dir.iterdir())
    assert "old" not in left
    assert sorted(p.name for p in made[-3:]) == left


def test_outline_marks_a_file_input() -> None:
    line = ops.format_outline(
        [{"depth": 0, "role": "textbox", "name": "CV", "ref": "e2", "state": {"file": True}}]
    )
    assert line == '- textbox "CV" [ref=e2] [file]'


class _Cdp:
    def __init__(self, found: dict[str, Any]) -> None:
        self.found = found
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def cdp(self, method: str, **params: Any) -> Any:
        self.sent.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F1"}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            return {"result": self.found}
        return {}


def test_worker_sets_the_node_the_isolated_world_gave_and_frees_it() -> None:
    cdp = _Cdp({"type": "object", "subtype": "node", "objectId": "obj-1"})
    assert helper_worker._local_set_files(cdp, 'W.fileInput("e4", true)', ["/s/a.txt"]) == {
        "ok": True
    }
    methods = [m for m, _ in cdp.sent]
    assert methods == [
        "Page.getFrameTree",
        "Page.createIsolatedWorld",
        "Runtime.evaluate",
        "DOM.setFileInputFiles",
        "Runtime.releaseObject",
    ]
    evaluate = dict(cdp.sent)["Runtime.evaluate"]
    assert evaluate["contextId"] == 7 and evaluate["returnByValue"] is False
    assert dict(cdp.sent)["DOM.setFileInputFiles"] == {"files": ["/s/a.txt"], "objectId": "obj-1"}


def test_worker_sets_nothing_when_the_walker_gave_no_node() -> None:
    cdp = _Cdp({"type": "object", "subtype": "null"})
    assert helper_worker._local_set_files(cdp, "W", ["/s/a.txt"]) == {"ok": False}
    assert "DOM.setFileInputFiles" not in [m for m, _ in cdp.sent]


@pytest.mark.parametrize("files", [[], ["/a"] * 101, "/a", [1], [""]])
def test_worker_takes_one_to_a_hundred_paths(files: Any) -> None:
    with pytest.raises(ValueError):
        helper_worker._local_set_files(_Cdp({}), "W", files)


def test_a_path_the_shared_screen_refuses_is_never_probed(
    fake: FakeBrowser, audits: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import hooks

    target = os.path.abspath("share.pdf")
    probed: list[str] = []
    real_lstat = os.lstat

    def lstat(p: Any, *a: Any, **k: Any) -> Any:
        if str(p) == target:
            probed.append(str(p))
        return real_lstat(p, *a, **k)

    with monkeypatch.context() as mp:
        mp.setattr(hooks, "validate_file_path", lambda _p: None)
        mp.setattr(os, "lstat", lstat)
        out = run(ops.run_op("upload", {"ref": "e4", "path": target}, _ctx(fake)))
    assert out["code"] == "path_refused" and probed == []
    assert _set_files_calls(fake) == []
