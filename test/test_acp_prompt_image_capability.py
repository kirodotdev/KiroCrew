"""An image block reaches a harness only when its handshake advertised one.

``AcpClient`` built its prompt with every readable picture inlined whatever the
``initialize`` answer said, so deepseek -- whose live handshake advertises
``promptCapabilities.image: false`` -- was sent image blocks. These tests drive
the real handshake on the committed capture, then the real prompt builder, and
check what goes on the wire.
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.prompt_blocks import IMAGE_UNSUPPORTED_NOTE, build_prompt_blocks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_DEEPSEEK

_FRAMES = pathlib.Path(__file__).parent / "fixtures" / "acp_frames"


def _png_bytes() -> bytes:
    pil = pytest.importorskip("PIL.Image")
    import io

    buf = io.BytesIO()
    pil.new("RGB", (2, 2), (127, 127, 127)).save(buf, format="PNG")
    return buf.getvalue()


def _png(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "shot.png"
    p.write_bytes(_png_bytes())
    return p


def _captured_initialize(backend_dir: str) -> dict:
    """The ``initialize`` result off a committed live capture."""
    path = _FRAMES / backend_dir / "handshake-live.jsonl"
    for line in path.read_text().splitlines()[1:]:
        result = json.loads(line).get("result")
        if isinstance(result, dict) and "agentCapabilities" in result:
            return result
    raise AssertionError(f"no initialize result in {path}")


def _client(tmp_path: pathlib.Path) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_DEEPSEEK)
    proc = MagicMock()
    proc.returncode = None
    client._process = proc
    return client


async def _handshake_then_prompt(client: AcpClient, init_result: dict, message: str) -> list:
    """Run ``_initialize_session`` on *init_result*, then ``_send_prompt``.

    Returns the ``prompt`` array of the ``session/prompt`` request sent.
    """
    sent: list[tuple[str, dict]] = []

    async def fake_send(method: str, params: dict) -> int:
        sent.append((method, params))
        return len(sent)

    async def fake_wait(req_id: int, timeout: float = 50.0, **_kw) -> dict:
        return init_result if sent[req_id - 1][0] == "initialize" else {"sessionId": "s"}

    client._send_request = AsyncMock(side_effect=fake_send)
    client._wait_for_response = AsyncMock(side_effect=fake_wait)
    client._drain_notifications = AsyncMock()
    client._settle_orphan_steers = AsyncMock()

    await client._initialize_session()
    await client._send_prompt(message)
    prompts = [params for method, params in sent if method == "session/prompt"]
    assert len(prompts) == 1
    return prompts[0]["prompt"]


def test_the_deepseek_capture_advertises_no_image() -> None:
    """The premise, so the next test cannot pass for the wrong reason."""
    caps = _captured_initialize("deepseek")["agentCapabilities"]["promptCapabilities"]
    assert caps["image"] is False


@pytest.mark.asyncio
async def test_deepseek_is_sent_no_image_block_and_a_visible_note(tmp_path) -> None:
    p = _png(tmp_path)
    prompt = await _handshake_then_prompt(
        _client(tmp_path), _captured_initialize("deepseek"), f"look at {p}"
    )

    assert [b["type"] for b in prompt] == ["text"]
    assert prompt[0]["text"] == f"look at {p} {IMAGE_UNSUPPORTED_NOTE}"


@pytest.mark.asyncio
async def test_a_harness_that_advertises_image_still_gets_the_block(tmp_path) -> None:
    p = _png(tmp_path)
    init = json.loads(json.dumps(_captured_initialize("deepseek")))
    init["agentCapabilities"]["promptCapabilities"]["image"] = True

    prompt = await _handshake_then_prompt(_client(tmp_path), init, f"look at {p}")

    assert [b["type"] for b in prompt] == ["text", "image"]
    assert prompt[0]["text"] == "look at [image: shot.png]"
    assert base64.b64decode(prompt[1]["data"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt_caps",
    [None, {}, {"image": "true"}, {"image": 1}, "image"],
    ids=["absent", "empty", "string", "int", "not-a-dict"],
)
async def test_only_a_literal_true_counts(tmp_path, prompt_caps) -> None:
    p = _png(tmp_path)
    caps: dict = {}
    if prompt_caps is not None:
        caps["promptCapabilities"] = prompt_caps
    prompt = await _handshake_then_prompt(
        _client(tmp_path), {"protocolVersion": 1, "agentCapabilities": caps}, f"see {p}"
    )
    assert [b["type"] for b in prompt] == ["text"]


def test_a_client_that_never_handshook_sends_no_image(tmp_path) -> None:
    """Fails closed: no handshake read means no yes."""
    p = _png(tmp_path)
    client = _client(tmp_path)
    sent: list[dict] = []

    async def fake_send(method: str, params: dict) -> int:
        sent.append(params)
        return 1

    client._send_request = AsyncMock(side_effect=fake_send)
    client._settle_orphan_steers = AsyncMock()
    asyncio.run(client._send_prompt(f"see {p}"))
    assert [b["type"] for b in sent[0]["prompt"]] == ["text"]


class TestTheBuilder:
    def test_unsupported_note_follows_a_readable_picture(self, tmp_path) -> None:
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"see {p} now", agent_accepts_image=False)
        assert blocks == [{"type": "text", "text": f"see {p} {IMAGE_UNSUPPORTED_NOTE} now"}]

    def test_no_note_for_a_file_that_is_not_a_picture(self, tmp_path) -> None:
        p = tmp_path / "fake.png"
        p.write_bytes(b"not an image at all, just text bytes padding" * 4)
        blocks = build_prompt_blocks(f"see {p}", agent_accepts_image=False)
        assert blocks == [{"type": "text", "text": f"see {p}"}]

    def test_caller_text_only_still_wins_with_no_note(self, tmp_path) -> None:
        """``allow_image=False`` is text ABOUT a session: path untouched, no note."""
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"see {p}", allow_image=False, agent_accepts_image=False)
        assert blocks == [{"type": "text", "text": f"see {p}"}]
