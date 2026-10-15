"""An outgoing ACP frame never carries a lone UTF-16 surrogate.

``json.dumps`` escapes half an emoji as ``\\ud83d`` instead of refusing it, and
kiro-cli refuses the whole line with a ``-32700 Parse error`` that has no ``id``.
That error cannot reach the request it belongs to, so a prompt holding such text
left the turn on "Thinking..." forever. ``encode_frame`` replaces each lone half
with U+FFFD; valid text, including real surrogate pairs, is encoded as before.

``_strict_loads`` stands in for kiro-cli's parser: it decodes the line and then
refuses any string UTF-8 cannot encode, which is exactly a lone surrogate.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_runtime_helpers import _make_runtime

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_framing import encode_frame

HIGH = "\ud83d"
LOW = "\ude00"
EMOJI = "\U0001f600"


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _strict_loads(line: bytes):
    """Parse ``line`` the way a strict JSON parser does, or raise."""
    decoded = json.loads(line.decode("utf-8"))
    for text in _strings(decoded):
        text.encode("utf-8")  # UnicodeEncodeError on a lone surrogate
    return decoded


def _prompt(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "session/prompt",
        "params": {"sessionId": "sA", "prompt": [{"type": "text", "text": text}]},
    }


def _text(frame) -> str:
    return frame["params"]["prompt"][0]["text"]


def test_the_plain_encoding_is_refused_by_a_strict_parser():
    """The premise: what the transports wrote before cannot be parsed."""
    line = (json.dumps(_prompt(f"hi {HIGH}")) + "\n").encode()

    with pytest.raises(UnicodeEncodeError):
        _strict_loads(line)


@pytest.mark.parametrize(
    "text, expected",
    [
        (f"hi {HIGH}", "hi �"),
        (f"{LOW} tail", "� tail"),
        (f"a{HIGH}b{LOW}c", "a�b�c"),
        (f"{LOW}{HIGH}", "��"),
        (f"{EMOJI}{HIGH}", f"{EMOJI}�"),
    ],
    ids=["high", "low", "split-pair", "reversed-pair", "after-valid-emoji"],
)
def test_a_lone_surrogate_becomes_the_replacement_character(text, expected):
    frame = _strict_loads(encode_frame(_prompt(text)))

    assert _text(frame) == expected
    assert frame["params"]["sessionId"] == "sA"


def test_a_lone_surrogate_in_a_key_is_replaced_too():
    message = {"jsonrpc": "2.0", "id": 1, "result": {f"k{HIGH}": [f"v{LOW}"]}}

    frame = _strict_loads(encode_frame(message))

    assert frame["result"] == {"k�": ["v�"]}


@pytest.mark.parametrize(
    "text",
    [
        "plain ascii",
        "café 中文",
        f"smile {EMOJI} done",
        "\\ud83d is six typed characters",
    ],
    ids=["ascii", "bmp", "emoji", "escaped-text"],
)
def test_valid_text_is_encoded_exactly_as_before(text):
    message = _prompt(text)

    line = encode_frame(message)

    assert line == (json.dumps(message) + "\n").encode()
    assert _text(_strict_loads(line)) == text


def test_a_pair_held_as_two_code_points_is_kept_as_the_character():
    paired = _prompt(f"x{HIGH}{LOW}y")

    line = encode_frame(paired)

    assert line == encode_frame(_prompt(f"x{EMOJI}y"))
    assert _text(_strict_loads(line)) == f"x{EMOJI}y"


def _written(stdin: MagicMock) -> bytes:
    return b"".join(call.args[0] for call in stdin.write.call_args_list)


@pytest.mark.asyncio
async def test_runtime_prompt_reaches_the_agent_parseable():
    rt, _, proc = _make_runtime()
    rt._session_queues["sA"] = MagicMock()

    await rt.send_request(
        "session/prompt", {"sessionId": "sA", "prompt": [{"type": "text", "text": f"hi {HIGH}"}]}
    )

    frame = _strict_loads(_written(proc.stdin))
    assert frame["method"] == "session/prompt"
    assert _text(frame) == "hi �"


@pytest.mark.asyncio
async def test_runtime_response_reaches_the_agent_parseable():
    rt, _, proc = _make_runtime()

    await rt.send_response(9, {"content": f"tool output {LOW}"})

    frame = _strict_loads(_written(proc.stdin))
    assert frame == {"jsonrpc": "2.0", "id": 9, "result": {"content": "tool output �"}}


@pytest.mark.asyncio
async def test_client_prompt_reaches_the_agent_parseable(tmp_path):
    client = AcpClient(work_dir=tmp_path)
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc

    await client._send_request(
        "session/prompt", {"sessionId": "sA", "prompt": [{"type": "text", "text": f"{HIGH}!"}]}
    )

    frame = _strict_loads(_written(proc.stdin))
    assert _text(frame) == "�!"
