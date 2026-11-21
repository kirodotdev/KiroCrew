"""``upload`` folders, globs, ``via: chooser`` and ``via: drop``.

The handler runs against the scripted page of ``test_browser_harness_ops``; the
helper halves run against a recorded ``cdp``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from pathlib import Path
from typing import Any

import pytest
from test_browser_harness_ops import FakeBrowser

from kiro_crew.browser_harness import helper_worker, ops, upload


@pytest.fixture
def fake(tmp_path: Path) -> FakeBrowser:
    browser = FakeBrowser(tmp_path)
    browser.page["fileInput"] = {"ok": True, "multiple": True, "directory": False}
    browser.page["resolve"] = {"ok": True, "x": 40.0, "y": 60.0, "w": 10.0, "h": 10.0}
    browser.helpers["set_files"] = {"ok": True}
    browser.helpers["chooser_files"] = {"ok": True, "mode": "selectMultiple"}
    browser.helpers["drop_files"] = {"ok": True}
    return browser


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any, str]]:
    rows: list[tuple[str, Any, str]] = []
    monkeypatch.setattr(
        upload, "_audit", lambda path, size, outcome: rows.append((path, size, outcome))
    )
    return rows


def _upload(fake: FakeBrowser, args: dict[str, Any]) -> dict[str, Any]:
    ctx = dataclasses.replace(fake.ctx(), upload_dir=fake.tmp / "uploads")
    return asyncio.run(ops.run_op("upload", {"ref": "e4", **args}, ctx))


def _tree(tmp_path: Path) -> Path:
    top = tmp_path / "work" / "photos"
    (top / "sub").mkdir(parents=True)
    (top / "a.png").write_bytes(b"aaa")
    (top / "sub" / "b.png").write_bytes(b"bbbb")
    (top / "notes.txt").write_bytes(b"n")
    return top


def _calls(fake: FakeBrowser, helper: str) -> list[dict[str, Any]]:
    return [kw for h, kw in fake.calls if h == helper]


# --- folders ------------------------------------------------------------------


def test_a_folder_into_a_folder_field_is_refused_not_reported_sent(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    # Neither a set nor a drop fills a webkitdirectory field on every build, and both
    # answered ok while the field stayed empty: refused, nothing staged or sent.
    fake.page["fileInput"] = {"ok": True, "multiple": False, "directory": True}
    out = _upload(fake, {"path": str(_tree(tmp_path))})
    assert out["code"] == "folder_field_unsupported"
    assert "via: drop" in out["error"] and "<folder>/*" in out["error"]
    assert _calls(fake, "set_files") == [] and _calls(fake, "drop_files") == []
    assert not (fake.tmp / "uploads").exists() or not any((fake.tmp / "uploads").rglob("*.png"))
    assert {row[2] for row in audits} == {"folder_field_unsupported"}
    # Its files, by a glob, still go to a field that takes several.
    fake.page["fileInput"] = {"ok": True, "multiple": True, "directory": True}
    assert _upload(fake, {"path": str(tmp_path / "work" / "photos" / "*")})["ok"] is True


def test_a_folder_into_a_plain_file_field_is_refused(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    out = _upload(fake, {"path": str(_tree(tmp_path))})
    assert out["code"] == "not_folder_input" and _calls(fake, "set_files") == []


def test_a_folder_drops_on_a_drop_zone(fake: FakeBrowser, audits: list, tmp_path: Path) -> None:
    out = _upload(fake, {"path": str(_tree(tmp_path)), "via": "drop"})
    assert out["ok"] is True
    (call,) = _calls(fake, "drop_files")
    assert call["x"] == 40.0 and call["y"] == 60.0
    assert [Path(f).name for f in call["files"]] == ["photos"]


def test_links_in_a_folder_are_skipped_never_followed(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    top = _tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"s")
    from kiro_crew.testing.links import make_dir_link

    make_dir_link(top / "linkdir", outside)
    skipped = 1
    try:
        (top / "link.txt").symlink_to(outside / "secret.txt")
        skipped = 2
    except OSError:
        pass  # a file link needs a privilege an unelevated Windows shell lacks
    out = _upload(fake, {"path": str(top), "via": "drop"})
    assert out["ok"] is True and out["skipped"] == skipped
    staged = Path(_calls(fake, "drop_files")[0]["files"][0])
    assert not list(staged.rglob("secret.txt")) and not (staged / "linkdir").exists()


def test_a_protected_file_inside_a_folder_refuses_the_whole_call(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import hooks

    top = _tree(tmp_path)
    protected = str(top / "sub" / "b.png")
    real = hooks.validate_file_path
    monkeypatch.setattr(
        hooks, "validate_file_path", lambda p, *a, **k: None if p == protected else real(p, *a, **k)
    )
    out = _upload(fake, {"path": str(top), "via": "drop"})
    assert out["code"] == "path_refused" and protected in out["error"]
    assert _calls(fake, "drop_files") == []
    assert not any((fake.tmp / "uploads").rglob("*.png"))


def test_a_folder_over_the_file_cap_is_refused(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upload, "UPLOAD_MAX_TREE_FILES", 2)
    out = _upload(fake, {"path": str(_tree(tmp_path)), "via": "drop"})
    assert out["code"] == "too_many_files" and _calls(fake, "drop_files") == []


def test_a_folder_over_the_byte_cap_is_refused_and_nothing_stays_staged(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upload, "UPLOAD_MAX_BYTES", 5)
    out = _upload(fake, {"path": str(_tree(tmp_path)), "via": "drop"})
    assert out["code"] == "file_too_large"
    assert not any(p.is_file() for p in (fake.tmp / "uploads").rglob("*"))


def test_a_folder_walk_is_bounded(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upload, "UPLOAD_MAX_WALK", 3)
    top = _tree(tmp_path)
    for i in range(5):
        (top / f"d{i}").mkdir()
    out = _upload(fake, {"path": str(top), "via": "drop"})
    assert out["code"] == "too_many_files"


# --- globs ---------------------------------------------------------------------


def test_a_glob_sends_its_matching_files(fake: FakeBrowser, audits: list, tmp_path: Path) -> None:
    top = _tree(tmp_path)
    out = _upload(fake, {"path": str(top / "*.png")})
    assert out == {"ok": True, "ref": "e4", "file": "a.png", "size": 3}
    out = _upload(fake, {"path": str(top / "*")})
    # The folder ``sub`` is a match, not a file: skipped.
    assert [f["file"] for f in out["files"]] == ["a.png", "notes.txt"] and out["skipped"] == 1


def test_a_glob_under_home_is_expanded(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "work"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "work"))
    out = _upload(fake, {"path": "~/photos/*.png"})
    assert out["ok"] is True and out["file"] == "a.png"


def test_a_glob_matching_nothing_or_too_much_is_refused(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    top = _tree(tmp_path)
    assert _upload(fake, {"path": str(top / "*.gif")})["code"] == "no_match"
    assert _upload(fake, {"path": str(top / "**" / "*.png")})["code"] == "bad_path"
    monkeypatch.setattr(upload, "UPLOAD_MAX_TREE_FILES", 1)
    assert _upload(fake, {"path": str(top / "*")})["code"] == "too_many_files"
    assert _calls(fake, "set_files") == []


def test_a_protected_glob_match_refuses_the_whole_call(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import hooks

    top = _tree(tmp_path)
    protected = str(top / "notes.txt")
    real = hooks.validate_file_path
    monkeypatch.setattr(
        hooks, "validate_file_path", lambda p, *a, **k: None if p == protected else real(p, *a, **k)
    )
    copied: list[str] = []
    monkeypatch.setattr(hooks, "safe_copy_file_nolink", lambda src, *a, **k: copied.append(src))
    assert _upload(fake, {"path": str(top / "*")})["code"] == "path_refused"
    # Refused before any file is copied, not part way through.
    assert copied == [] and _calls(fake, "set_files") == []


def test_a_glob_on_a_single_file_field_is_refused_when_it_finds_several(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    fake.page["fileInput"] = {"ok": True, "multiple": False, "directory": False}
    out = _upload(fake, {"path": str(_tree(tmp_path) / "*")})
    assert out["code"] == "not_multiple" and _calls(fake, "set_files") == []


# --- via: chooser / drop -------------------------------------------------------


def test_via_chooser_clicks_the_ref_and_answers_the_picker(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    top = _tree(tmp_path)
    out = _upload(fake, {"path": str(top / "a.png"), "via": "chooser"})
    assert out["ok"] is True
    (call,) = _calls(fake, "chooser_files")
    assert (call["x"], call["y"]) == (40.0, 60.0) and "folder" not in call
    assert Path(call["files"][0]).read_bytes() == b"aaa"


@pytest.mark.parametrize(
    "code", ["no_chooser", "not_multiple", "not_folder_input", "frame_refused"]
)
def test_a_picker_that_cannot_take_the_files_is_reported(
    fake: FakeBrowser, audits: list, tmp_path: Path, code: str
) -> None:
    fake.helpers["chooser_files"] = {"ok": False, "code": code}
    out = _upload(fake, {"path": str(_tree(tmp_path) / "a.png"), "via": "chooser"})
    assert out["code"] == code
    assert not any(p.is_file() for p in (fake.tmp / "uploads").rglob("*"))
    assert [row[2] for row in audits] == [code]


def test_a_bad_via_is_refused(fake: FakeBrowser, audits: list, tmp_path: Path) -> None:
    assert _upload(fake, {"path": str(_tree(tmp_path) / "a.png"), "via": "x"})["code"] == "bad_via"


def test_a_non_input_ref_names_the_other_ways(fake: FakeBrowser, audits: list) -> None:
    fake.page["fileInput"] = {"ok": False, "code": "not_file_input"}
    out = _upload(fake, {"path": "/x/a.png"})
    assert out["code"] == "not_file_input" and "via: chooser" in out["error"]


# --- the helper halves ---------------------------------------------------------


class _Cdp:
    def __init__(self, events: list[list[dict[str, Any]]], attrs: list[str] | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.clicked: list[tuple[float, float]] = []
        self.events = events
        self.attrs = attrs or []

    tree: dict[str, Any] = {
        "frame": {"id": "TOP", "securityOrigin": "http://a.test"},
        "childFrames": [
            {"frame": {"id": "SAME", "securityOrigin": "http://a.test"}},
            {"frame": {"id": "OTHER", "securityOrigin": "http://b.test"}},
        ],
    }
    hit = "DIV"
    hit_frame = "TOP"
    hits: list[str] = []
    landed = 2
    drop_frame = "TOP"

    def cdp(self, method: str, **params: Any) -> Any:
        self.sent.append((method, params))
        if method == "DOM.describeNode":
            return {"node": {"attributes": self.attrs, "nodeName": self.hit}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": self.landed}}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "OBJ"}}
        if method == "Runtime.evaluate":
            return {"result": {"subtype": "node", "objectId": "OBJ"}}
        if method == "DOM.getNodeForLocation" and self.hits:
            return {"backendNodeId": 5, "frameId": self.hits.pop(0)}
        if method == "DOM.getNodeForLocation":
            # An overlay with ``pointer-events: none`` over a child frame: only a hit
            # test that skips it, as the drop does, lands in the frame.
            frame = self.hit_frame if params.get("ignorePointerEventsNone") else self.drop_frame
            return {"backendNodeId": 5, "frameId": frame}
        if method == "Page.getFrameTree":
            return {"frameTree": self.tree}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 3}
        return {}

    def drain_events(self) -> list[dict[str, Any]]:
        return self.events.pop(0) if self.events else []

    def click_at_xy(self, x: float, y: float) -> None:
        self.clicked.append((x, y))


def _opened(
    mode: str = "selectMultiple", node: int = 7, frame: str = "TOP"
) -> list[dict[str, Any]]:
    params = {"mode": mode, "backendNodeId": node, "frameId": frame}
    return [{"method": "Page.fileChooserOpened", "params": params}]


def test_the_chooser_helper_intercepts_only_for_its_own_call() -> None:
    cdp = _Cdp([[], _opened()])
    out = helper_worker._local_chooser_files(cdp, 1, 2, ["/s/a", "/s/b"])
    assert out == {"ok": True, "mode": "selectMultiple"} and cdp.clicked == [(1, 2)]
    methods = [m for m, _ in cdp.sent]
    assert methods[0] == "Page.setInterceptFileChooserDialog"
    assert cdp.sent[0][1] == {"enabled": True}
    assert ("DOM.setFileInputFiles", {"files": ["/s/a", "/s/b"], "backendNodeId": 7}) in cdp.sent
    assert cdp.sent[-1] == ("Page.setInterceptFileChooserDialog", {"enabled": False})


def test_the_chooser_helper_sets_nothing_it_cannot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper_worker, "_CHOOSER_WAIT_S", 0.0)
    cdp = _Cdp([])
    assert helper_worker._local_chooser_files(cdp, 1, 2, ["/s/a"])["code"] == "no_chooser"
    cdp = _Cdp([[], _opened("selectSingle")])
    assert helper_worker._local_chooser_files(cdp, 1, 2, ["/s/a", "/s/b"])["code"] == "not_multiple"
    assert "DOM.setFileInputFiles" not in [m for m, _ in cdp.sent]
    assert cdp.sent[-1] == ("Page.setInterceptFileChooserDialog", {"enabled": False})


def test_the_chooser_helper_answers_only_its_own_page_s_frames() -> None:
    for frame, ok in (("TOP", True), ("SAME", True), ("OTHER", False), ("ELSEWHERE", False)):
        cdp = _Cdp([[], _opened(frame=frame)])
        out = helper_worker._local_chooser_files(cdp, 1, 2, ["/s/a"])
        assert out["ok"] is ok, frame
        assert ("DOM.setFileInputFiles" in [m for m, _ in cdp.sent]) is ok
        if not ok:
            assert out["code"] == "frame_refused"


def test_the_drop_helper_drops_files_without_pressing() -> None:
    cdp = _Cdp([])
    assert helper_worker._local_drop_files(cdp, 3, 4, ["/s/a"]) == {"ok": True}
    drags = [p for m, p in cdp.sent if m == "Input.dispatchDragEvent"]
    assert [p["type"] for p in drags] == ["dragEnter", "dragOver", "drop"]
    assert drags[-1]["data"]["files"] == ["/s/a"] and drags[-1]["x"] == 3
    assert "Input.dispatchMouseEvent" not in [m for m, _ in cdp.sent]


@pytest.mark.parametrize(
    ("tag", "frame"),
    [("IFRAME", "TOP"), ("FRAME", "TOP"), ("OBJECT", "TOP"), ("EMBED", "TOP"), ("DIV", "SAME")],
)
def test_the_drop_helper_never_drops_into_a_frame(tag: str, frame: str) -> None:
    # The browser's own hit test: a frame element, or a node inside a child frame
    # (what a closed shadow root wrapping an iframe hit-tests to), is refused.
    cdp = _Cdp([])
    cdp.hit, cdp.hit_frame, cdp.drop_frame = tag, frame, frame
    assert helper_worker._local_drop_files(cdp, 3, 4, ["/s/a"]) == {
        "ok": False,
        "code": "frame_refused",
    }
    assert "Input.dispatchDragEvent" not in [m for m, _ in cdp.sent]


# --- one expander, dot entries, links, names, the no-card tier ------------------


def test_dot_entries_are_skipped_by_folders_and_globs_alike(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    top = _tree(tmp_path)
    (top / ".env").write_bytes(b"SECRET=1")
    (top / ".git").mkdir()
    (top / ".git" / "config").write_bytes(b"[core]")
    out = _upload(fake, {"path": str(top), "via": "drop"})
    staged = Path(_calls(fake, "drop_files")[0]["files"][0])
    assert not (staged / ".env").exists() and not (staged / ".git").exists()
    assert out["skipped"] == 2
    out = _upload(fake, {"path": str(top / "*")})
    assert ".env" not in [f["file"] for f in out["files"]] and out["skipped"] == 3


def test_a_glob_never_follows_a_link_to_a_folder(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    from kiro_crew.testing.links import make_dir_link

    top = _tree(tmp_path)
    make_dir_link(tmp_path / "work" / "linkdir", top)
    out = _upload(fake, {"path": str(tmp_path / "work" / "*" / "*.png")})
    # Only photos/a.png: the link to the same folder is skipped, not walked.
    assert out["file"] == "a.png" and out["skipped"] == 1
    assert _upload(fake, {"path": str(tmp_path / "work" / "linkdir" / "*.png")})["code"] == (
        "path_symlink"
    )


def test_a_refused_folder_is_named_and_its_children_are_not(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import hooks

    top = _tree(tmp_path)
    hidden = str(top / "sub")
    real = hooks.validate_file_path
    monkeypatch.setattr(
        hooks,
        "validate_file_path",
        lambda p, *a, **k: None if p.startswith(hidden) else real(p, *a, **k),
    )
    for path in (str(top), str(top / "sub" / "*")):
        out = _upload(fake, {"path": path, "via": "drop"})
        assert out["code"] == "path_refused" and hidden in out["error"]
        assert "b.png" not in out["error"]


def test_with_no_card_the_strict_hidden_folders_are_refused(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    gh = home / ".config" / "gh"
    gh.mkdir(parents=True)
    (gh / "hosts.yml").write_bytes(b"oauth_token: x")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    from kiro_crew import hooks

    copied: list[str] = []
    real_copy = hooks.safe_copy_file_nolink
    monkeypatch.setattr(
        hooks,
        "safe_copy_file_nolink",
        lambda src, *a, **k: copied.append(src) or real_copy(src, *a, **k),
    )
    ctx = dataclasses.replace(
        fake.ctx(), upload_dir=fake.tmp / "uploads", upload_grant_host="example.com"
    )
    for path in (str(gh / "hosts.yml"), str(gh), str(gh / "*.yml")):
        out = asyncio.run(ops.run_op("upload", {"ref": "e4", "path": path, "via": "drop"}, ctx))
        # Named before any copy, by the folder rule, not by the copy's own fence: the
        # op route turns this answer into the person's card.
        assert out["code"] == upload.NEEDS_CARD and "sandbox hides" in out["error"], path
    assert _calls(fake, "drop_files") == [] and copied == []
    # On the user's card the same file goes as before.
    assert _upload(fake, {"path": str(gh / "hosts.yml"), "via": "drop"})["ok"] is True


def test_the_byte_cap_is_checked_before_any_copy(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import hooks

    copied: list[str] = []
    monkeypatch.setattr(hooks, "safe_copy_file_nolink", lambda src, *a, **k: copied.append(src))
    monkeypatch.setattr(upload, "UPLOAD_MAX_BYTES", 5)
    out = _upload(fake, {"path": str(_tree(tmp_path)), "via": "drop"})
    assert out["code"] == "file_too_large" and copied == []


def test_an_existing_path_with_brackets_is_that_file_not_a_pattern(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "photo [1].png").write_bytes(b"bracketed")
    (work / "photo 1.png").write_bytes(b"sibling")
    out = _upload(fake, {"path": str(work / "photo [1].png")})
    assert out["ok"] is True and out["file"] == "photo [1].png"
    (call,) = _calls(fake, "set_files")
    assert Path(call["files"][0]).read_bytes() == b"bracketed"


def test_the_walk_cap_stops_a_huge_folder_while_it_is_read(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    top = _tree(tmp_path)
    read: list[int] = []
    real = os.scandir

    class _Huge:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def __iter__(self) -> Any:
            for i in range(50):
                read.append(i)
                yield type("E", (), {"name": f"f{i}"})()

    monkeypatch.setattr(upload, "UPLOAD_MAX_WALK", 5)
    # Only around the call: the temp folder's own cleanup lists folders too.
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", lambda p: _Huge() if str(p) == str(top) else real(p))
        out = _upload(fake, {"path": str(top), "via": "drop"})
    assert out["code"] == "too_many_files" and len(read) == 6


def test_the_strict_check_ignores_case() -> None:
    home = os.path.expanduser("~")
    assert upload._strict_hidden(os.path.join(home, ".config", "GH", "hosts.yml"))
    assert upload._strict_hidden(os.path.join(home, ".Kube", "config"))
    assert not upload._strict_hidden(os.path.join(home, "Pictures", "a.png"))


def test_a_parent_swapped_for_a_link_after_the_walk_is_refused_at_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.testing.links import make_dir_link

    home = tmp_path / "home"
    gh = home / ".config" / "gh"
    gh.mkdir(parents=True)
    (gh / "hosts.yml").write_bytes(b"oauth_token: x")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    work = tmp_path / "work" / "x"
    work.mkdir(parents=True)
    (work / "hosts.yml").write_bytes(b"plain")
    groups, _skipped = upload.expand([str(work / "hosts.yml")], strict=True)
    # The agent swaps the checked folder for a link to the hidden one.
    work.rename(tmp_path / "work" / "gone")
    make_dir_link(work, gh)
    with pytest.raises(upload._PathRefused) as refused:
        upload.stage(groups, tmp_path / "uploads", strict=True)
    assert refused.value.code == upload.NEEDS_CARD
    # On the person's card the same copy goes, as before.
    made = upload.stage(groups, tmp_path / "uploads2")
    assert made.files[0][1].read_bytes() == b"oauth_token: x"


def test_a_home_that_is_a_link_is_covered_both_ways(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.testing.links import make_dir_link

    real_home = tmp_path / "local" / "u"
    (real_home / ".config" / "gh").mkdir(parents=True)
    (real_home / ".config" / "gh" / "hosts.yml").write_bytes(b"t")
    linked = tmp_path / "home-u"
    make_dir_link(linked, real_home)
    monkeypatch.setenv("HOME", str(linked))
    monkeypatch.setenv("USERPROFILE", str(linked))
    for spelled in (linked, real_home):
        path = str(spelled / ".config" / "gh" / "hosts.yml")
        assert upload._strict_hidden(path), path
        with pytest.raises(upload._PathRefused) as refused:
            upload.expand([path], strict=True)
        assert refused.value.code == upload.NEEDS_CARD


def test_two_folder_members_differing_only_by_case_are_refused(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    top = tmp_path / "work" / "mix"
    top.mkdir(parents=True)
    (top / "A.txt").write_bytes(b"one")
    try:
        (top / "a.txt").write_bytes(b"two")
    except OSError:
        pytest.skip("this disk folds case")
    if len(list(top.iterdir())) < 2:
        pytest.skip("this disk folds case")
    out = _upload(fake, {"path": str(top), "via": "drop"})
    assert out["code"] == "duplicate_name" and _calls(fake, "drop_files") == []


def test_a_glob_keeps_its_root_anchor() -> None:
    root = os.path.abspath(os.sep)
    assert upload._glob_parts(os.path.join(root, "*.png")) == (root, ["*.png"])
    assert upload._glob_parts(os.path.join(root, "a", "b", "*.png")) == (
        os.path.join(root, "a", "b"),
        ["*.png"],
    )


def test_a_folder_entry_that_is_a_link_is_refused(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    from kiro_crew.testing.links import make_dir_link

    top = _tree(tmp_path)
    make_dir_link(tmp_path / "work" / "alias", top)
    out = _upload(fake, {"path": str(tmp_path / "work" / "alias"), "via": "drop"})
    assert out["code"] == "path_symlink" and _calls(fake, "drop_files") == []


def test_a_failed_delivery_cleans_up_off_the_event_loop(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    seen: list[bool] = []
    real = upload._drop_staged

    def _record(folder: Path) -> None:
        seen.append(threading.current_thread() is threading.main_thread())
        real(folder)

    monkeypatch.setattr(upload, "_drop_staged", _record)
    fake.helpers["drop_files"] = {"ok": False, "code": "frame_refused"}
    out = _upload(fake, {"path": str(_tree(tmp_path)), "via": "drop"})
    assert out["code"] == "frame_refused" and seen == [False]


def test_the_drop_check_and_the_drop_use_one_point() -> None:
    cdp = _Cdp([])
    helper_worker._local_drop_files(cdp, 3.6, 4.4, ["/s/a"])
    hit = next(p for m, p in cdp.sent if m == "DOM.getNodeForLocation")
    drops = [p for m, p in cdp.sent if m == "Input.dispatchDragEvent"]
    assert {(hit["x"], hit["y"])} == {(p["x"], p["y"]) for p in drops}


def test_the_drop_check_hit_tests_as_the_drop_does() -> None:
    cdp = _Cdp([])
    cdp.drop_frame = "SAME"  # the drop itself would land in the child frame
    assert helper_worker._local_drop_files(cdp, 3, 4, ["/s/a"])["code"] == "frame_refused"
    assert "Input.dispatchDragEvent" not in [m for m, _ in cdp.sent]


def test_with_no_card_the_tab_is_read_again_right_before_the_files_go(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A home of its own, so the test folder is outside every strict-hidden folder.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    fake.helpers["current_tab"] = {"url": "https://elsewhere.test/"}
    ctx = dataclasses.replace(
        fake.ctx(), upload_dir=fake.tmp / "uploads", upload_grant_host="example.com"
    )
    args = {"ref": "e4", "path": str(_tree(tmp_path) / "a.png"), "via": "drop"}
    out = asyncio.run(ops.run_op("upload", args, ctx))
    assert out["code"] == "trust_scope_changed" and _calls(fake, "drop_files") == []
    assert not any(p.is_file() for p in (fake.tmp / "uploads").rglob("*"))


def test_a_drop_zone_with_no_ref_takes_the_point(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    # A plain div with drop listeners gets no ref in the snapshot.
    out = _upload(
        fake, {"ref": None, "x": 40, "y": 60, "via": "drop", "path": str(_tree(tmp_path) / "a.png")}
    )
    assert out["ok"] is True and out["at"] == [40.0, 60.0] and "ref" not in out
    (call,) = _calls(fake, "drop_files")
    assert (call["x"], call["y"]) == (40.0, 60.0)
    assert fake.page_methods().count("resolve") == 0


def test_an_input_still_needs_a_ref_and_a_point_must_be_given(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    path = str(_tree(tmp_path) / "a.png")
    assert _upload(fake, {"ref": None, "x": 1, "y": 2, "path": path})["code"] == "bad_ref"
    assert _upload(fake, {"ref": None, "via": "drop", "path": path})["code"] == "bad_target"


def test_a_pattern_that_climbs_is_refused_not_crashed(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    top = _tree(tmp_path)
    for pattern in (
        str(top / "*" / ".."),
        str(tmp_path / "work" / "ph*" / ".." / "photos"),
        # Folds to ``photos/*.png``, which matches: still not the pattern shown.
        str(top / "s*" / ".." / "*.png"),
    ):
        out = _upload(fake, {"path": pattern, "via": "drop"})
        assert out["code"] == "bad_path", pattern
    assert _calls(fake, "drop_files") == []


@pytest.mark.skipif(not os.path.exists("/proc/self/environ"), reason="no /proc here")
def test_with_no_card_the_kernel_views_go_to_the_card(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ctx = dataclasses.replace(
        fake.ctx(), upload_dir=fake.tmp / "uploads", upload_grant_host="example.com"
    )
    for path in ("/proc/self/environ", "/proc/[0-9]*/environ"):
        args = {"ref": "e4", "path": path, "via": "drop"}
        out = asyncio.run(ops.run_op("upload", args, ctx))
        assert out["code"] in (upload.NEEDS_CARD, "path_refused"), path
    assert _calls(fake, "drop_files") == []
    assert upload._strict_hidden("/proc/self/environ")


def test_a_folder_deeper_than_the_stack_is_refused_not_crashed(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    import sys

    top = tmp_path / "work" / "deep"
    top.mkdir(parents=True)
    leaf = top
    # Deeper than Python's own stack would allow for a recursive walk (made one level
    # at a time: ``mkdir(parents=True)`` itself recurses per level).
    try:
        for _ in range(sys.getrecursionlimit() + 50):
            leaf = leaf / "d"
            leaf.mkdir()
    except OSError:
        pytest.skip("this file system caps the path length")
    (leaf / "x.txt").write_bytes(b"x")
    out = _upload(fake, {"path": str(top), "via": "drop"})
    # Refused by name, not a crash, and nothing staged.
    assert out["code"] == "too_deep" and _calls(fake, "drop_files") == []
    assert not any(p.is_file() for p in (fake.tmp / "uploads").rglob("*"))


def test_a_folder_at_the_depth_cap_still_goes(
    fake: FakeBrowser, audits: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upload, "UPLOAD_MAX_DEPTH", 3)
    leaf = tmp_path / "work" / "top" / "a" / "b"
    leaf.mkdir(parents=True)
    (leaf / "x.txt").write_bytes(b"x")
    assert _upload(fake, {"path": str(tmp_path / "work" / "top"), "via": "drop"})["ok"] is True
    (leaf / "c").mkdir()
    (leaf / "c" / "y.txt").write_bytes(b"y")
    out = _upload(fake, {"path": str(tmp_path / "work" / "top"), "via": "drop"})
    assert out["code"] == "too_deep"


def test_the_drop_point_is_checked_again_before_each_drag_event() -> None:
    # The page's dragenter handler puts a frame under the point.
    cdp = _Cdp([])
    cdp.hits = ["TOP", "SAME"]
    out = helper_worker._local_drop_files(cdp, 3, 4, ["/s/a"])
    assert out == {"ok": False, "code": "frame_refused"}
    kinds = [p["type"] for m, p in cdp.sent if m == "Input.dispatchDragEvent"]
    assert kinds == ["dragEnter", "dragCancel"]


def test_files_a_folder_holds_are_asked_of_governance(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    top = _tree(tmp_path)
    denied = str(top / "sub")
    asked: list[str] = []

    def _read_allowed(path: str) -> bool:
        asked.append(path)
        return not path.startswith(denied)

    ctx = dataclasses.replace(
        fake.ctx(), upload_dir=fake.tmp / "uploads", read_allowed=_read_allowed
    )
    out = asyncio.run(ops.run_op("upload", {"ref": "e4", "path": str(top), "via": "drop"}, ctx))
    assert out["code"] == "path_refused" and "b.png" not in out["error"]
    assert str(top / "sub" / "b.png") in asked and _calls(fake, "drop_files") == []


def test_a_folder_through_a_chooser_is_refused_before_anything_is_copied(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    out = _upload(fake, {"path": str(_tree(tmp_path)), "via": "chooser"})
    assert out["code"] == "not_folder_input" and "via: drop" in out["error"]
    assert _calls(fake, "chooser_files") == []
    assert not (fake.tmp / "uploads").exists() or not any((fake.tmp / "uploads").rglob("*.png"))


def test_two_folders_differing_only_by_case_are_refused(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    top = tmp_path / "work" / "mix2"
    (top / "A").mkdir(parents=True)
    try:
        (top / "a").mkdir()
    except FileExistsError:
        pytest.skip("this disk folds case")
    (top / "A" / "first.txt").write_bytes(b"1")
    (top / "a" / "second.txt").write_bytes(b"2")
    out = _upload(fake, {"path": str(top), "via": "drop"})
    assert out["code"] == "duplicate_name" and _calls(fake, "drop_files") == []


def test_a_literal_climb_before_a_bracket_name_is_that_file(
    fake: FakeBrowser, audits: list, tmp_path: Path
) -> None:
    work = tmp_path / "work" / "photos"
    (work / "exports").mkdir(parents=True)
    (work / "photo [1].png").write_bytes(b"one")
    out = _upload(fake, {"path": str(work / "exports" / ".." / "photo [1].png")})
    assert out["ok"] is True and out["file"] == "photo [1].png"
