"""Inline Computer Use screenshots: the image travels beside the text, only when allowed.

A ``computer_get_state`` screenshot reaches the model as an MCP image block when all of
these hold, and as today's path-only text otherwise:

* the dispatcher kept the screenshot (no secure field, the walk not truncated);
* the shim asked for images (``accept_images``);
* the keystone ``computer_use.json`` sets ``inline_screenshots: true``;
* the configured harness is in ``ACP_BACKENDS_TOOL_RESULT_IMAGES``.

Each link is pinned on its own: the structured dispatch result, the gateway's
decision and budget, the shim's framing, and the MCP frame builder.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from computer_use_api_helpers import profiles_dir_fixture  # noqa: F401
from computer_use_api_helpers import _client
from mcp_computer_helpers import fake_backend_fixture  # noqa: F401
from mcp_computer_helpers import home_fixture  # noqa: F401
from mcp_computer_helpers import keystone_fixture  # noqa: F401
from mcp_computer_helpers import _SESSION, _enable

from kiro_crew import mcp_computer
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.computer_use import enable_state
from kiro_crew.computer_use import tools as cu_tools
from kiro_crew.computer_use.types import (
    IMAGE_MIME_JPEG,
    INLINE_IMAGE_NOTE,
    TOOL_GET_STATE,
    TOOL_LIST_APPS,
    DispatchResult,
)
from kiro_crew.dashboard.handlers import computer_use as cu_api
from kiro_crew.mcp_shared import ToolReply, call_tool_with_logging
from kiro_crew.testing.fake_computer_use import (
    FAKE_FILES_APP,
    FAKE_JPEG_BYTES,
    FAKE_LOGIN_APP,
)
from kiro_crew.validation import build_tool_response

# ── the structured dispatch result ──


def test_get_state_carries_the_screenshot_bytes_beside_the_unchanged_text(
    keystone: Path, fake_backend
):
    _enable(keystone)
    args = {"app": FAKE_FILES_APP.name, "screenshot": True}
    result = cu_tools.dispatch_tool_result(TOOL_GET_STATE, args, session_key=_SESSION)
    assert result.image == FAKE_JPEG_BYTES
    assert result.image_mime == IMAGE_MIME_JPEG
    # The text half is exactly what a text-only caller gets.
    text = cu_tools.dispatch_tool(TOOL_GET_STATE, args, session_key=_SESSION)
    assert result.text.split("Screenshot:")[0] == text.split("Screenshot:")[0]


def test_a_window_with_a_secure_field_carries_no_image(keystone: Path, fake_backend):
    _enable(keystone)
    result = cu_tools.dispatch_tool_result(
        TOOL_GET_STATE, {"app": FAKE_LOGIN_APP.name, "screenshot": True}, session_key=_SESSION
    )
    assert result.image == b""


def test_a_snapshot_without_a_screenshot_carries_no_image(keystone: Path, fake_backend):
    _enable(keystone)
    result = cu_tools.dispatch_tool_result(
        TOOL_GET_STATE, {"app": FAKE_FILES_APP.name, "screenshot": False}, session_key=_SESSION
    )
    assert result.image == b""


def test_other_tools_and_refusals_carry_no_image(keystone: Path, fake_backend):
    _enable(keystone)
    assert cu_tools.dispatch_tool_result(TOOL_LIST_APPS, {}, session_key=_SESSION).image == b""
    refused = cu_tools.dispatch_tool_result("computer_teleport", {}, session_key=_SESSION)
    assert refused.text.startswith("Error:") and refused.image == b""


# ── the gateway's decision ──


def _png(width: int, height: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    """Set the configured harness the gateway reads."""

    def _set(backend: str) -> None:
        cfg = SimpleNamespace(agent=SimpleNamespace(acp_backend=backend))
        monkeypatch.setattr(cu_api.KiroCrewConfig, "load", staticmethod(lambda: cfg))

    _set(ACP_BACKEND_KAS)
    return _set


def _result(image: bytes = FAKE_JPEG_BYTES, mime: str = IMAGE_MIME_JPEG) -> DispatchResult:
    return DispatchResult(text="tree", image=image, image_mime=mime)


def test_the_image_is_off_until_the_keystone_turns_it_on(keystone: Path, harness):
    _enable(keystone)
    assert cu_api._inline_image(_result()) is None
    _enable(keystone, inline_screenshots=True)
    block = cu_api._inline_image(_result())
    assert block == {
        "mimeType": IMAGE_MIME_JPEG,
        "data": base64.b64encode(FAKE_JPEG_BYTES).decode("ascii"),
    }


@pytest.mark.parametrize("value", ["true", 1, "yes", None])
def test_only_a_real_json_true_turns_it_on(keystone: Path, harness, value):
    _enable(keystone, inline_screenshots=value)
    assert enable_state.inline_screenshots_enabled() is False
    assert cu_api._inline_image(_result()) is None


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE])
def test_a_harness_not_measured_to_pass_images_gets_none(keystone: Path, harness, backend):
    """Kiro (kiro-cli's v2 engine) is out on purpose: on a real screenshot its model
    guessed the content in every run, so it keeps the path-only reply."""
    _enable(keystone, inline_screenshots=True)
    harness(backend)
    assert cu_api._inline_image(_result()) is None


def test_an_oversized_screenshot_is_fitted_to_the_inline_budget(
    keystone: Path, harness, monkeypatch: pytest.MonkeyPatch
):
    from kiro_crew import imaging

    _enable(keystone, inline_screenshots=True)
    block = cu_api._inline_image(_result(_png(imaging.MAX_IMAGE_EDGE_PX + 600, 300), "image/png"))
    assert block is not None
    width, height = imaging.image_dimensions(base64.b64decode(block["data"]))
    assert max(width, height) <= imaging.MAX_IMAGE_EDGE_PX


def test_bytes_that_cannot_be_verified_are_dropped(keystone: Path, harness):
    _enable(keystone, inline_screenshots=True)
    assert cu_api._inline_image(_result(b"not an image", IMAGE_MIME_JPEG)) is None


# ── the route ──


def _invoke_with_fake_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.computer_use.tools.dispatch_tool_result", lambda *a, **k: _result()
    )


async def _post(body: dict) -> dict:
    async with _client() as client:
        resp = await client.post("/api/computer-use/invoke", json=body)
        assert resp.status == 200
        return await resp.json()


@pytest.mark.asyncio
async def test_the_route_attaches_the_image_when_every_condition_holds(
    keystone: Path, harness, monkeypatch: pytest.MonkeyPatch
):
    _enable(keystone, inline_screenshots=True)
    _invoke_with_fake_image(monkeypatch)
    body = await _post(
        {
            "tool": TOOL_GET_STATE,
            "args": {"app": "Finder"},
            "session_key": _SESSION,
            "accept_images": True,
        }
    )
    assert body["image"]["mimeType"] == IMAGE_MIME_JPEG
    assert body["text"].endswith(INLINE_IMAGE_NOTE)


@pytest.mark.asyncio
@pytest.mark.parametrize("accept", [None, False, "true"])
async def test_the_route_sends_text_only_unless_the_shim_asks(
    keystone: Path, harness, monkeypatch: pytest.MonkeyPatch, accept
):
    _enable(keystone, inline_screenshots=True)
    _invoke_with_fake_image(monkeypatch)
    request = {"tool": TOOL_GET_STATE, "args": {"app": "Finder"}, "session_key": _SESSION}
    if accept is not None:
        request["accept_images"] = accept
    assert await _post(request) == {"text": "tree"}


# ── the shim ──


def test_the_shim_frames_the_gateways_image_as_a_tool_reply(monkeypatch: pytest.MonkeyPatch):
    data = base64.b64encode(FAKE_JPEG_BYTES).decode("ascii")
    monkeypatch.setattr(mcp_computer.enable_state, "is_enabled", lambda *a, **k: True)
    monkeypatch.setattr(
        mcp_computer,
        "_invoke",
        lambda *a, **k: {"text": "tree", "image": {"mimeType": IMAGE_MIME_JPEG, "data": data}},
    )
    reply = mcp_computer._call_tool_inner(TOOL_GET_STATE, {"app": "Finder"})
    assert reply == ToolReply(text="tree", images=((data, IMAGE_MIME_JPEG),))


@pytest.mark.parametrize(
    "image",
    [None, "x", {"mimeType": "image/svg+xml", "data": "AAAA"}, {"mimeType": "image/jpeg"}],
)
def test_the_shim_drops_a_missing_or_malformed_image(monkeypatch: pytest.MonkeyPatch, image):
    monkeypatch.setattr(mcp_computer.enable_state, "is_enabled", lambda *a, **k: True)
    monkeypatch.setattr(
        mcp_computer,
        "_invoke",
        lambda *a, **k: {"text": "tree", **({"image": image} if image else {})},
    )
    assert mcp_computer._call_tool_inner(TOOL_GET_STATE, {"app": "Finder"}) == "tree"


def test_the_shim_asks_the_gateway_for_images(monkeypatch: pytest.MonkeyPatch):
    sent: list[dict] = []

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return b'{"text": "ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, *a, **k):
        sent.append(json.loads(req.data))
        return _Resp()

    monkeypatch.setattr(mcp_computer, "loopback_urlopen", _fake_urlopen)
    mcp_computer._invoke(_SESSION, TOOL_GET_STATE, {"app": "Finder"})
    assert sent and sent[0]["accept_images"] is True


# ── the MCP frame ──


def test_the_frame_appends_image_blocks_after_the_text():
    frame = build_tool_response("tree", images=(("AAAA", IMAGE_MIME_JPEG),))
    assert frame["content"] == [
        {"type": "text", "text": "tree"},
        {"type": "image", "data": "AAAA", "mimeType": IMAGE_MIME_JPEG},
    ]


def test_a_frame_without_images_is_unchanged():
    assert build_tool_response("tree") == {"content": [{"type": "text", "text": "tree"}]}


def test_a_tool_reply_is_audited_by_its_text(monkeypatch: pytest.MonkeyPatch):
    rows: list[dict] = []
    monkeypatch.setattr(
        "kiro_crew.mcp_shared_calls.sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: rows.append(kw)),
    )
    reply = ToolReply(text="Error: refused", images=(("AAAA", IMAGE_MIME_JPEG),))
    out = call_tool_with_logging(
        TOOL_GET_STATE, {}, lambda n, a: a, lambda n, a: reply, "s", "kirocrew-computer"
    )
    assert out is reply
    assert rows[-1]["outcome"] == "failed"


def test_the_inline_note_names_the_attached_image():
    assert "image" in INLINE_IMAGE_NOTE
