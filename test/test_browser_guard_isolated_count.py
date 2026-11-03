"""The screenshot guards count secret fields in an isolated world, and fail closed.

A page's own script can replace ``document.querySelectorAll`` in its world, so a
count taken there can be forced to zero. The guards (``screenshot`` and the
decision hooks' capture check) take the count through the ``js_isolated`` helper
instead, and refuse when that count cannot be taken.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew.browser_harness import decide, dom_census, helper_worker, ops


class _Frames:
    """A frame inspector (a live stream holds the tab) answering *count* for children."""

    def __init__(self, count: int) -> None:
        self.n = count

    async def count(self, _method: str) -> int:
        return self.n


def _ctx(
    tmp_path: Path,
    *,
    main: int | None,
    isolated: Any,
    unreadable: Any = 0,
    frames: Any = None,
) -> tuple[ops.OpContext, list[str]]:
    """A page whose main-world walker answers *main* to the guard counts, whose
    isolated world answers *isolated* to them (an int, ``None``, or an exception to
    raise) and *unreadable* to ``unreadableFrames``; *frames* is the inspector."""
    calls: list[str] = []

    async def helper(name: str, **kw: Any) -> Any:
        if name == "closed_secret_count":
            return 0
        if name == "frame_owner_count":
            return 0
        calls.append(name)
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "js":
            expr = kw["expression"]
            if ".askedValues(" in expr or ".passwordBoxes(" in expr:
                return main
            return None
        if name == "js_isolated":
            assert kw["expression"].startswith(ops.WALKER_SOURCE)
            calls.append(
                "unreadableFrames" if ".unreadableFrames(" in kw["expression"] else "count"
            )
            if ".framesSeen(" in kw["expression"] or ".objectsSeen(" in kw["expression"]:
                return 0
            answer = unreadable if ".unreadableFrames(" in kw["expression"] else isolated
            if isinstance(answer, BaseException):
                raise answer
            return answer
        if name == "screenshot":
            Path(kw["path"]).write_bytes(b"png")
        return None

    ctx = ops.OpContext(helper=helper, screenshot_dir=tmp_path, frames=lambda: frames)
    return ctx, calls


@pytest.mark.asyncio
async def test_a_page_that_forges_its_own_count_to_zero_still_gets_no_screenshot(
    tmp_path: Path,
) -> None:
    # The page-world walker is fooled (0); the isolated world still sees the field.
    ctx, calls = _ctx(tmp_path, main=0, isolated=1)
    out = await ops.run_op("screenshot", {}, ctx)
    assert out.get("code") == "asked_value_on_page"
    assert "screenshot" not in calls


@pytest.mark.asyncio
async def test_a_clean_page_still_gets_its_screenshot(tmp_path: Path) -> None:
    ctx, calls = _ctx(tmp_path, main=0, isolated=0)
    out = await ops.run_op("screenshot", {}, ctx)
    assert out.get("ok") is True, out
    assert "js_isolated" in calls and "screenshot" in calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "isolated", [None, "0", True, -1, RuntimeError("js_isolated: no isolated world")]
)
async def test_a_count_that_cannot_be_taken_refuses_the_screenshot(
    tmp_path: Path, isolated: Any
) -> None:
    ctx, calls = _ctx(tmp_path, main=0, isolated=isolated)
    out = await ops.run_op("screenshot", {}, ctx)
    assert out.get("code") == "secret_check_failed", out
    assert "screenshot" not in calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("isolated", "allowed"),
    [(0, True), (1, False), (None, False), (RuntimeError("gone"), False)],
)
async def test_the_decision_capture_check_reads_the_isolated_count(
    tmp_path: Path, isolated: Any, allowed: bool
) -> None:
    ctx, _calls = _ctx(tmp_path, main=0, isolated=isolated)
    assert await decide._may_capture(ctx) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unreadable", "code"),
    [
        (1, "unreadable_frame_on_page"),
        (3, "unreadable_frame_on_page"),
        (None, "secret_check_failed"),
        ("0", "secret_check_failed"),
        (RuntimeError("gone"), "secret_check_failed"),
    ],
)
async def test_an_unreadable_frame_with_no_inspector_refuses_the_screenshot(
    tmp_path: Path, unreadable: Any, code: str
) -> None:
    # No live stream holds the tab, so nothing can count inside a cross-origin
    # frame: an OTP there would reach the image. The top document reads 0.
    ctx, calls = _ctx(tmp_path, main=0, isolated=0, unreadable=unreadable, frames=None)
    out = await ops.run_op("screenshot", {}, ctx)
    assert out.get("code") == code, out
    assert "screenshot" not in calls


@pytest.mark.asyncio
async def test_a_page_with_only_readable_frames_and_no_inspector_still_gets_its_screenshot(
    tmp_path: Path,
) -> None:
    ctx, calls = _ctx(tmp_path, main=0, isolated=0, unreadable=0, frames=None)
    out = await ops.run_op("screenshot", {}, ctx)
    assert out.get("ok") is True, out
    assert "unreadableFrames" in calls


@pytest.mark.asyncio
@pytest.mark.parametrize("children", [0, 1])
async def test_the_live_view_does_not_lift_the_frame_rule(tmp_path: Path, children: int) -> None:
    # The live stream's frame inspector does not prove it checked every frame yet,
    # so an unreadable frame refuses with the live view open too.
    ctx, calls = _ctx(tmp_path, main=0, isolated=0, unreadable=1, frames=_Frames(children))
    out = await ops.run_op("screenshot", {}, ctx)
    assert out.get("code") == "unreadable_frame_on_page", out
    assert "screenshot" not in calls


@pytest.mark.asyncio
@pytest.mark.parametrize(("children", "ok"), [(0, True), (1, False)])
async def test_with_the_live_view_and_no_unreadable_frame_the_children_still_count(
    tmp_path: Path, children: int, ok: bool
) -> None:
    ctx, _calls = _ctx(tmp_path, main=0, isolated=0, unreadable=0, frames=_Frames(children))
    out = await ops.run_op("screenshot", {}, ctx)
    assert (out.get("ok") is True) is ok, out


@pytest.mark.asyncio
@pytest.mark.parametrize(("unreadable", "allowed"), [(0, True), (1, False), (None, False)])
async def test_the_decision_capture_check_refuses_an_unreadable_frame_with_no_inspector(
    tmp_path: Path, unreadable: Any, allowed: bool
) -> None:
    ctx, _calls = _ctx(tmp_path, main=0, isolated=0, unreadable=unreadable, frames=None)
    assert await decide._may_capture(ctx) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after", "code"),
    [
        ("frame", "unreadable_frame_on_page"),
        ("asked", "asked_value_on_page"),
        (None, "secret_check_failed"),
    ],
)
async def test_a_frame_that_appears_during_the_capture_discards_the_image(
    tmp_path: Path, after: Any, code: str
) -> None:
    # Clean before the shot; a cross-origin sign-in frame (or a filled field) shows
    # up while it is taken. The image is deleted and the agent gets no path.
    state = {"shot": False}
    written: list[Path] = []

    async def helper(name: str, **kw: Any) -> Any:
        if name == "closed_secret_count":
            return 0
        if name == "frame_owner_count":
            return 0
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "js_isolated":
            unread = ".unreadableFrames(" in kw["expression"]
            if not state["shot"]:
                return 0
            if after is None:
                return None
            if ".framesSeen(" in kw["expression"]:
                return 0
            if after == "frame":
                return 1 if unread else 0
            return 0 if unread else 1
        if name == "screenshot":
            p = Path(kw["path"])
            p.write_bytes(b"png")
            written.append(p)
            state["shot"] = True
        return None

    ctx = ops.OpContext(helper=helper, screenshot_dir=tmp_path, frames=lambda: None)
    out = await ops.run_op("screenshot", {}, ctx)
    assert out.get("code") == code, out
    assert "path" not in out
    assert written and not written[0].exists()


@pytest.mark.asyncio
async def test_a_page_that_stays_clean_keeps_its_image(tmp_path: Path) -> None:
    ctx, calls = _ctx(tmp_path, main=0, isolated=0, unreadable=0, frames=None)
    out = await ops.run_op("screenshot", {}, ctx)
    assert out.get("ok") is True and Path(out["path"]).exists()
    # Counted before and after the capture (by the frame check and the two guard
    # counts: quick-ask values and filled secret fields).
    assert calls.count("unreadableFrames") == 6


def test_the_walker_counts_unreadable_frames() -> None:
    # The page-side half: a frame whose document this world cannot read is counted.
    assert "unreadableFrames: function" in ops.WALKER_SOURCE
    assert "framesSeen: function" in ops.WALKER_SOURCE
    body = ops.WALKER_SOURCE.split("function scanFrames()", 1)[1].split("var api = {", 1)[0]
    # Every unreadable frame counts, hidden or not: visibility is no proof the
    # page will not show it for the moment the image is taken.
    assert "(!inner || !isFrame(el))" in body and "draws(" not in body and "d >= 8" in body


@pytest.mark.asyncio
async def test_the_image_is_staged_privately_and_moved_out_only_when_it_passes(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "hidden"
    shots = tmp_path / "shots"
    written: list[Path] = []

    async def helper(name: str, **kw: Any) -> Any:
        if name == "closed_secret_count":
            return 0
        if name == "frame_owner_count":
            return 0
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "js_isolated":
            return 0
        if name == "screenshot":
            p = Path(kw["path"])
            p.write_bytes(b"png")
            written.append(p)
        return None

    ctx = ops.OpContext(helper=helper, screenshot_dir=shots, staging_dir=staging)
    out = await ops.run_op("screenshot", {}, ctx)
    assert out.get("ok") is True, out
    assert written[0].parent == staging
    assert Path(out["path"]).parent == shots and Path(out["path"]).exists()
    assert not written[0].exists()


@pytest.mark.asyncio
async def test_a_rejected_image_never_reaches_the_agent_directory(tmp_path: Path) -> None:
    staging = tmp_path / "hidden"
    shots = tmp_path / "shots"
    state = {"shot": False}

    async def helper(name: str, **kw: Any) -> Any:
        if name == "closed_secret_count":
            return 0
        if name == "frame_owner_count":
            return 0
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "js_isolated":
            return 1 if state["shot"] and ".unreadableFrames(" in kw["expression"] else 0
        if name == "screenshot":
            Path(kw["path"]).write_bytes(b"png")
            state["shot"] = True
        return None

    ctx = ops.OpContext(helper=helper, screenshot_dir=shots, staging_dir=staging)
    out = await ops.run_op("screenshot", {}, ctx)
    assert out.get("code") == "unreadable_frame_on_page", out
    assert not list(shots.glob("shot-*.png")) and not list(staging.glob("*.png"))


@pytest.mark.asyncio
async def test_an_image_that_cannot_be_deleted_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"shot": False}

    async def helper(name: str, **kw: Any) -> Any:
        if name == "closed_secret_count":
            return 0
        if name == "frame_owner_count":
            return 0
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "js_isolated":
            return 1 if state["shot"] else 0
        if name == "screenshot":
            Path(kw["path"]).write_bytes(b"png")
            state["shot"] = True
        return None

    def _stuck(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "unlink", _stuck)
    ctx = ops.OpContext(helper=helper, screenshot_dir=tmp_path, frames=lambda: None)
    out = await ops.run_op("screenshot", {}, ctx)
    assert "path" not in out
    assert "could not be deleted" in out["error"]


@pytest.mark.asyncio
async def test_a_page_that_navigates_to_a_refused_url_during_the_capture_is_discarded(
    tmp_path: Path,
) -> None:
    state = {"shot": False}

    async def helper(name: str, **kw: Any) -> Any:
        if name == "closed_secret_count":
            return 0
        if name == "frame_owner_count":
            return 0
        if name == "current_tab":
            return {"url": "https://blocked.example/" if state["shot"] else "https://example.com/"}
        if name == "js_isolated":
            return 0
        if name == "screenshot":
            Path(kw["path"]).write_bytes(b"png")
            state["shot"] = True
        return None

    ctx = ops.OpContext(
        helper=helper,
        screenshot_dir=tmp_path,
        url_allowed=lambda url: "blocked" not in url,
    )
    out = await ops.run_op("screenshot", {}, ctx)
    assert out.get("ok") is not True and "path" not in out, out
    assert not list(tmp_path.glob("shot-*.png"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tree", "seen", "ok"), [(1, 0, False), (2, 1, False), (1, 1, True), (1, 2, False)]
)
async def test_a_frame_the_walk_cannot_reach_refuses_the_screenshot(
    tmp_path: Path, tree: int, seen: int, ok: bool
) -> None:
    # A frame in a closed shadow root is in the browser's frame tree but out of the
    # page walk; with no live stream nothing checks it, so the screenshot refuses.
    async def helper(name: str, **kw: Any) -> Any:
        if name == "closed_secret_count":
            return 0
        if name == "frame_owner_count":
            return tree
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "js_isolated":
            return seen if ".framesSeen(" in kw["expression"][-20:] else 0
        if name == "screenshot":
            Path(kw["path"]).write_bytes(b"png")
        return None

    ctx = ops.OpContext(helper=helper, screenshot_dir=tmp_path, frames=lambda: None)
    out = await ops.run_op("screenshot", {}, ctx)
    if ok:
        assert out.get("ok") is True, out
    elif seen > tree:
        # The page changed between the reads: neither count can be trusted.
        assert out.get("code") == "secret_check_failed", out
    else:
        assert out.get("code") == "unreadable_frame_on_page", out
    assert await decide._may_capture(ctx) is ok


@pytest.mark.asyncio
@pytest.mark.parametrize("tree", [None, "1", True, RuntimeError("gone")])
async def test_a_frame_tree_that_cannot_be_read_refuses(tmp_path: Path, tree: Any) -> None:
    async def helper(name: str, **kw: Any) -> Any:
        if name == "closed_secret_count":
            return 0
        if name == "frame_owner_count":
            if isinstance(tree, BaseException):
                raise tree
            return tree
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "js_isolated":
            return 0
        return None

    ctx = ops.OpContext(helper=helper, screenshot_dir=tmp_path, frames=lambda: None)
    out = await ops.run_op("screenshot", {}, ctx)
    assert out.get("code") == "secret_check_failed", out


def test_the_worker_counts_frame_owners_through_closed_roots_and_inline_documents() -> None:
    iframe = {"nodeName": "IFRAME", "contentDocument": {"nodeName": "#document"}}
    doc = {
        "nodeName": "#document",
        "children": [
            {
                "nodeName": "HTML",
                "children": [
                    # A closed shadow root, which page script cannot see into.
                    {
                        "nodeName": "DIV",
                        "shadowRoots": [{"nodeName": "#document-fragment", "children": [iframe]}],
                    },
                    {"nodeName": "OBJECT"},
                    {"nodeName": "EMBED"},
                    {
                        "nodeName": "FRAME",
                        "contentDocument": {
                            "nodeName": "#document",
                            "children": [{"nodeName": "IFRAME"}],
                        },
                    },
                ],
            }
        ],
    }
    fake = _Cdp({"DOM.getDocument": {"root": doc}})
    assert helper_worker._local_frame_owner_count(fake) == 5
    assert dict(fake.sent)["DOM.getDocument"] == {"depth": -1, "pierce": True}
    # The DOM domain is left off afterwards, so the session gets no mutation events.
    assert fake.sent[-1][0] == "DOM.disable"


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"root": None},
        {"root": {"children": []}},
        {"root": {"nodeName": "#document", "children": "x"}},
        {"root": {"nodeName": "#document", "children": [{}]}},
        {"root": {"nodeName": "#document", "shadowRoots": False}},
    ],
)
def test_the_worker_refuses_a_malformed_document(reply: dict[str, Any]) -> None:
    with pytest.raises(RuntimeError, match="frame_owner_count"):
        helper_worker._local_frame_owner_count(_Cdp({"DOM.getDocument": reply}))


def test_a_readable_object_document_still_counts_as_unchecked() -> None:
    # The field counts do not walk into an object's document, so a readable HTML
    # object is unchecked: the walker counts it as unreadable.
    body = ops.WALKER_SOURCE.split("function scanFrames()", 1)[1].split("var api = {", 1)[0]
    assert "(!inner || !isFrame(el))" in body


def _closed_ctx(tmp_path: Path, closed: Any) -> tuple[ops.OpContext, list[str]]:
    """A clean page whose browser-side census reports *closed* secret fields in
    closed shadow roots (an int, ``None`` or an exception to raise)."""
    calls: list[str] = []

    async def helper(name: str, **kw: Any) -> Any:
        calls.append(name)
        if name == "closed_secret_count":
            if isinstance(closed, BaseException):
                raise closed
            return closed
        if name == "frame_owner_count":
            return 0
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "js_isolated":
            return 0
        if name == "screenshot":
            Path(kw["path"]).write_bytes(b"png")
        return None

    return ops.OpContext(helper=helper, screenshot_dir=tmp_path, frames=lambda: None), calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("closed", "code"),
    [
        (1, "secret_field_on_page"),
        (None, "secret_check_failed"),
        ("0", "secret_check_failed"),
        (RuntimeError("gone"), "secret_check_failed"),
        (0, None),
    ],
)
async def test_a_secret_field_in_a_closed_shadow_root_refuses_the_screenshot(
    tmp_path: Path, closed: Any, code: Any
) -> None:
    # No page-side walk can enter a closed root: a takeover OTP there would be in
    # the image. The browser's own DOM census sees it, and the screenshot refuses.
    ctx, calls = _closed_ctx(tmp_path, closed)
    out = await ops.run_op("screenshot", {}, ctx)
    if code is None:
        assert out.get("ok") is True, out
    else:
        assert out.get("code") == code, out
        assert "screenshot" not in calls
    assert await decide._may_capture(ctx) is (code is None)


def test_the_census_counts_only_secret_fields_inside_closed_roots() -> None:
    from kiro_crew.browser_harness import dom_census

    def field(**attrs: str) -> dict[str, Any]:
        flat: list[str] = []
        for k, v in attrs.items():
            flat += [k.replace("_", "-"), v]
        return {"nodeName": "INPUT", "attributes": flat}

    doc = {
        "nodeName": "#document",
        "children": [
            field(name="otp"),  # open document: the page walk sees it
            {
                "nodeName": "DIV",
                "shadowRoots": [
                    {
                        "nodeName": "#document-fragment",
                        "shadowRootType": "closed",
                        "children": [
                            field(name="otp"),
                            field(autocomplete="one-time-code"),
                            field(type="password"),
                            field(data_kc_asked=""),
                            field(name="email"),
                            {"nodeName": "TEXTAREA", "attributes": ["id", "api-token"]},
                        ],
                    }
                ],
            },
            {
                "nodeName": "DIV",
                "shadowRoots": [
                    {
                        "nodeName": "#document-fragment",
                        "shadowRootType": "open",
                        "children": [field(type="password")],
                    }
                ],
            },
        ],
    }
    assert dom_census.closed_secret_count(doc) == 5
    fake = _Cdp({"DOM.getDocument": {"root": doc}})
    assert helper_worker._local_closed_secret_count(fake) == 5
    assert fake.sent[-1][0] == "DOM.disable"
    # The worker's copy of the rule stays the walker's and the gateway's.
    assert helper_worker._SECRET_NAME_RE.pattern == dom_census.SECRET_NAME_RE.pattern
    assert helper_worker._SECRET_AUTOCOMPLETE == dom_census.SECRET_AUTOCOMPLETE
    walker_re = ops.WALKER_SOURCE.split("var SECRET_NAME_RE = /", 1)[1].split("/i;", 1)[0]
    assert walker_re == dom_census.SECRET_NAME_RE.pattern
    walker_auto = ops.WALKER_SOURCE.split("var SECRET_AUTOCOMPLETE = [", 1)[1].split("];", 1)[0]
    assert tuple(w.strip().strip('"') for w in walker_auto.split(",")) == (
        dom_census.SECRET_AUTOCOMPLETE
    )


def test_an_out_of_process_frame_in_a_closed_root_counts_as_an_unchecked_secret() -> None:
    """A cross-site frame's document is not handed back inline, so nothing looked
    inside it. In a closed root it is one unknown secret, never zero."""

    def shadow(kind: str, *kids: dict[str, Any]) -> dict[str, Any]:
        return {
            "nodeName": "DIV",
            "shadowRoots": [
                {
                    "nodeName": "#document-fragment",
                    "shadowRootType": kind,
                    "children": list(kids),
                }
            ],
        }

    out_of_process = {"nodeName": "IFRAME", "attributes": ["src", "https://other.example/"]}
    inline = {
        "nodeName": "IFRAME",
        "contentDocument": {"nodeName": "#document", "children": [{"nodeName": "P"}]},
    }
    clean = {"nodeName": "#document", "children": [shadow("closed", inline)]}
    assert dom_census.closed_secret_count(clean) == 0
    for doc, expected in (
        ({"nodeName": "#document", "children": [shadow("closed", out_of_process)]}, 1),
        ({"nodeName": "#document", "children": [shadow("open", out_of_process)]}, 0),
        ({"nodeName": "#document", "children": [out_of_process]}, 0),
    ):
        assert dom_census.closed_secret_count(doc) == expected
        fake = _Cdp({"DOM.getDocument": {"root": doc}})
        assert helper_worker._local_closed_secret_count(fake) == expected


@pytest.mark.parametrize(
    ("attrs", "secret"),
    [
        (["name", "otpCode"], True),
        (["id", "cardCvv"], True),
        (["name", "MFAToken"], True),
        (["name", "apiKey"], True),
        (["name", "APIKey"], True),
        (["name", "code", "autocomplete", "One-Time-Code"], True),
        (["name", "code", "autocomplete", "one-time-code"], True),
        (["name", "x", "autocomplete", "cc-csc"], True),
        (["name", "topCount"], False),
        (["name", "tokenizer"], False),
        (["name", "email"], False),
    ],
)
def test_the_census_names_camel_case_and_autocomplete_secrets(
    attrs: list[str], secret: bool
) -> None:
    node = {"nodeName": "INPUT", "attributes": attrs}
    assert dom_census.is_secret_field(node) is secret
    assert helper_worker._secret_attrs(node) is secret
    for text in ("otpCode", "OTPCode", "cardCvv", "plain", "a1B"):
        assert helper_worker._split_camel(text) == dom_census.split_camel(text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "mods", "refused"),
    [
        ({"focused": True, "secret": True}, ["ctrl"], True),
        ({"focused": True, "secret": True}, ["meta"], True),
        ({"focused": True, "secret": True}, ["shift"], True),
        ({"focused": True, "secret": False}, ["ctrl"], False),
    ],
)
async def test_select_and_copy_chords_are_refused_on_a_filled_secret_field(
    tmp_path: Path, active: dict[str, Any], mods: list[str], refused: bool
) -> None:
    # After a takeover the person's OTP sits in a plain `name="otp"` field; Ctrl+A
    # then Ctrl+C would carry it into a field the agent can read.
    sent: list[str] = []

    async def helper(name: str, **kw: Any) -> Any:
        sent.append(name)
        if name == "closed_secret_count":
            return 0
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "js" and ".activeField(" in kw["expression"]:
            return {"password": False, "asked": False, "tag": "input", **active}
        return None

    out = await ops.run_op(
        "press_key", {"key": "a", "modifiers": mods}, ops.OpContext(helper=helper)
    )
    assert (out.get("code") == "secret_field") is refused, out
    assert ("press_key" in sent) is (not refused)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("closed", "focused", "refused"),
    [(1, True, True), (None, True, True), (0, True, False), (1, False, True), (None, False, True)],
)
async def test_copy_chords_are_refused_while_a_closed_root_holds_a_secret_field(
    tmp_path: Path, closed: Any, focused: bool, refused: bool
) -> None:
    # Focus inside a closed shadow root reads as its host (not a secret field), so
    # the census of closed roots decides: no copy chord while one holds a secret.
    sent: list[str] = []

    async def helper(name: str, **kw: Any) -> Any:
        sent.append(name)
        if name == "closed_secret_count":
            return closed
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "js" and ".activeField(" in kw["expression"]:
            return {
                "focused": focused,  # a closed root on the body reads as nothing focused
                "password": False,
                "asked": False,
                "secret": False,
                "tag": "div",
            }
        return None

    out = await ops.run_op(
        "press_key", {"key": "c", "modifiers": ["ctrl"]}, ops.OpContext(helper=helper)
    )
    assert (out.get("code") == "secret_field") is refused, out
    assert ("press_key" in sent) is (not refused)


def test_the_replay_mask_names_secret_fields_by_the_walkers_rule() -> None:
    from kiro_crew.browser_harness import dom_census, screencast

    mask_re = screencast._MASK_JS.split("const NAMED=/", 1)[1].split("/i;", 1)[0]
    assert mask_re == dom_census.SECRET_NAME_RE.pattern
    mask_auto = screencast._MASK_JS.split("const AUTO=[", 1)[1].split("];", 1)[0]
    assert tuple(w.strip("'") for w in mask_auto.split(",")) == dom_census.SECRET_AUTOCOMPLETE
    assert "input,textarea" in screencast._MASK_JS


class _Cdp:
    """A fake Harness ``helpers`` module: records every CDP call, answers from a table."""

    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self.answers = answers
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def cdp(self, method: str, **params: Any) -> dict[str, Any]:
        self.sent.append((method, params))
        return self.answers.get(method, {})


def test_the_worker_evaluates_in_an_isolated_world_of_the_top_frame() -> None:
    fake = _Cdp(
        {
            "Page.getFrameTree": {"frameTree": {"frame": {"id": "TOP"}}},
            "Page.createIsolatedWorld": {"executionContextId": 7},
            "Runtime.evaluate": {"result": {"value": 3}},
        }
    )
    assert helper_worker._local_js_isolated(fake, "1 + 2") == 3
    world = dict(fake.sent)["Page.createIsolatedWorld"]
    assert world["frameId"] == "TOP" and world["grantUniveralAccess"] is False
    evaluate = dict(fake.sent)["Runtime.evaluate"]
    assert evaluate == {"expression": "1 + 2", "contextId": 7, "returnByValue": True}


@pytest.mark.parametrize(
    "answers",
    [
        {"Page.getFrameTree": {}},
        {
            "Page.getFrameTree": {"frameTree": {"frame": {"id": "TOP"}}},
            "Page.createIsolatedWorld": {},
        },
        {
            "Page.getFrameTree": {"frameTree": {"frame": {"id": "TOP"}}},
            "Page.createIsolatedWorld": {"executionContextId": 7},
            "Runtime.evaluate": {"exceptionDetails": {"text": "boom"}},
        },
    ],
)
def test_the_worker_raises_rather_than_answering_without_an_isolated_value(
    answers: dict[str, dict[str, Any]],
) -> None:
    fake = _Cdp(answers)
    with pytest.raises(RuntimeError, match="js_isolated"):
        helper_worker._local_js_isolated(fake, "1")
    # It never falls back to the page's own world (an evaluate without a context).
    assert all(
        "contextId" in params for method, params in fake.sent if method == "Runtime.evaluate"
    )


def test_the_worker_reports_an_isolated_failure_as_an_error_reply() -> None:
    reply = helper_worker._handle(
        _Cdp({"Page.getFrameTree": {}}),
        {"id": 1, "helper": "js_isolated", "kwargs": {"expression": "1"}},
    )
    assert reply["ok"] is False and "js_isolated" in reply["error"]


def _secret_ctx(tmp_path: Path, before: Any, after: Any = None) -> tuple[ops.OpContext, list[str]]:
    """A clean page whose isolated walker answers *before* to ``secretValues`` until
    the image is taken and *after* (default: the same) once it is."""
    calls: list[str] = []
    state = {"shot": False}

    async def helper(name: str, **kw: Any) -> Any:
        calls.append(name)
        if name in ("closed_secret_count", "frame_owner_count"):
            return 0
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "js_isolated":
            if ".secretValues(" in kw["expression"]:
                answer = after if state["shot"] and after is not None else before
                if isinstance(answer, BaseException):
                    raise answer
                return answer
            return 0
        if name == "screenshot":
            Path(kw["path"]).write_bytes(b"png")
            state["shot"] = True
        return None

    return ops.OpContext(helper=helper, screenshot_dir=tmp_path, frames=lambda: None), calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("secret", "code"),
    [
        (1, "secret_value_on_page"),
        (None, "secret_check_failed"),
        (True, "secret_check_failed"),
        (RuntimeError("gone"), "secret_check_failed"),
        (0, None),
    ],
)
async def test_a_secret_field_filled_during_takeover_refuses_the_screenshot(
    tmp_path: Path, secret: Any, code: Any
) -> None:
    # A person types a code into <input type=text name=otp> during a takeover and
    # hands back: no quick-ask marker, no closed root, but its digits are in the
    # pixels. The walker's secret rule counts it, and the screenshot refuses.
    ctx, calls = _secret_ctx(tmp_path, secret)
    out = await ops.run_op("screenshot", {}, ctx)
    if code is None:
        assert out.get("ok") is True, out
    else:
        assert out.get("code") == code, out
        assert "screenshot" not in calls
        assert not list(tmp_path.glob("shot-*.png"))


@pytest.mark.asyncio
async def test_a_secret_field_filled_during_the_capture_discards_the_image(
    tmp_path: Path,
) -> None:
    ctx, calls = _secret_ctx(tmp_path, 0, after=1)
    out = await ops.run_op("screenshot", {}, ctx)
    assert out.get("code") == "secret_value_on_page", out
    assert "screenshot" in calls
    assert not list(tmp_path.glob("shot-*.png"))


@pytest.mark.asyncio
async def test_the_decision_capture_never_writes_to_the_agent_directory(
    tmp_path: Path,
) -> None:
    # The decision frame is judged again after it is taken. Until then it must sit
    # in the hidden staging leaf, not where an agent-spawned process can read it.
    staging = tmp_path / "hidden"
    shots = tmp_path / "shots"
    written: list[Path] = []
    seen_in_shots: list[list[Path]] = []

    async def helper(name: str, **kw: Any) -> Any:
        if name in ("closed_secret_count", "frame_owner_count"):
            return 0
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "js_isolated":
            return 0
        if name == "screenshot":
            p = Path(kw["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 8)
            written.append(p)
            seen_in_shots.append(list(shots.glob("*")) if shots.exists() else [])
        return None

    ctx = ops.OpContext(
        helper=helper, screenshot_dir=shots, staging_dir=staging, frames=lambda: None
    )
    data = await decide._capture(ctx)
    assert data is not None and data.startswith(b"\x89PNG")
    assert written and written[0].parent == staging
    assert seen_in_shots == [[]]
    assert not written[0].exists()
    assert not (shots.exists() and list(shots.glob("*")))
