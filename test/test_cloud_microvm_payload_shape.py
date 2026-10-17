"""The launcher and the guest must read the same wire format, or nothing boots.

The run-hook payload is the only channel into a MicroVM at boot. The lane writes
it and the guest reads it, and the two live in different source roots: the lane
is gateway code, the guest ships inside a customer-facing image whose Dockerfile
deliberately copies only the ``container`` package. So the decoder is COPIED into
the guest rather than imported, and this file is what keeps the copy honest.

It also pins the fact that cost three launches: **the platform delivers the
launcher's payload base64-encoded.** Undocumented, and invisible to a local
harness, which hands the guest exactly what the launcher wrote.
"""

from __future__ import annotations

import base64
import json
import pathlib

import pytest

from kiro_crew.cloud.microvm import payload as _payload_module
from kiro_crew.cloud.microvm.payload import (
    RunHookPayload,
    decode_payload,
    payload_encoding,
)

# Rooted at the MODULE rather than at the process working directory, so the
# comparison is of the file this interpreter actually imported.
_LANE = pathlib.Path(_payload_module.__file__)
_GUEST = (
    _LANE.resolve().parents[2]
    / "apps"
    / "builtins"
    / "aws_control"
    / "crew"
    / "runtime"
    / "container"
    / "microvm"
    / "payload_shape.py"
)


def _payload() -> RunHookPayload:
    return RunHookPayload(
        tag="l2crew",
        activation_id="9f806f70-cffe-447e-97d4-e41e7f61b253",
        activation_code="0123456789abcdefghij",
        region="us-east-1",
        control_secret_ref="kirocrew/crew/l2crew/CONTROL_SECRET",
        identity_secret_ref="kirocrew/identity/l2crew",
        generation=1,
    )


def test_the_guest_copy_is_byte_identical_to_the_lane():
    """A drift here is a VM that boots, registers nothing and bills for hours.

    Compared as text from ``decode_payload`` onward, which is the whole of what
    is copied. The guest file's own header explains why it is a copy; everything
    after the first function must match exactly.
    """
    lane = _LANE.read_text()
    guest = _GUEST.read_text()
    start = lane.index("def decode_payload(")
    expected = lane[start:]
    assert expected in guest, (
        "the guest's payload_shape.py has drifted from the lane's payload.py. "
        "Edit the lane's decoder and re-copy it; do not edit the guest copy."
    )


def test_raw_json_is_accepted():
    """What the launcher writes, and what the local harness delivers."""
    raw = _payload().encode()
    assert payload_encoding(raw) == "json"
    assert decode_payload(raw)["tag"] == "l2crew"


def test_base64_is_accepted_because_that_is_what_the_platform_sends():
    """Measured live: a 434-byte payload arrived as a 576-byte body.

    576 is the base64 length of 432 bytes, and the three launches before this
    was understood all failed with the guest reporting only that the body was
    unreadable.
    """
    raw = _payload().encode()
    wire = base64.b64encode(raw.encode()).decode()
    # The relationship the measurement rests on, asserted rather than described.
    assert len(wire) == 4 * ((len(raw.encode()) + 2) // 3)
    assert payload_encoding(wire) == "base64"
    assert decode_payload(wire)["ssm"]["id"] == "9f806f70-cffe-447e-97d4-e41e7f61b253"


@pytest.mark.parametrize("key", ["payload", "runHookPayload"])
@pytest.mark.parametrize("inner_base64", [True, False])
def test_an_envelope_is_accepted_in_either_inner_form(key, inner_base64):
    raw = _payload().encode()
    inner = base64.b64encode(raw.encode()).decode() if inner_base64 else raw
    wire = json.dumps({key: inner})
    assert payload_encoding(wire) == "envelope"
    assert decode_payload(wire)["gen"] == 1


@pytest.mark.parametrize(
    "wire",
    [
        "",
        "{}",
        "not json at all",
        json.dumps({"v": 2, "tag": "x"}),
        # Base64 of a JSON document that is not a version-1 payload: decodes
        # cleanly and must still be refused, or a wrong-version payload would be
        # accepted through the encoded path but not the raw one.
        base64.b64encode(json.dumps({"v": 99}).encode()).decode(),
        # Valid base64 of bytes that are not UTF-8 JSON.
        base64.b64encode(b"\xff\xfe\x00binary").decode(),
    ],
)
def test_a_body_that_is_not_a_version_one_payload_is_refused(wire):
    """Liberal about ENCODING, strict about CONTENT.

    Every accepted form is checked for ``v == 1`` before it is believed, so
    widening the decoder did not widen what counts as a payload.
    """
    with pytest.raises(ValueError):
        decode_payload(wire)


def test_base64_decoding_validates_its_alphabet():
    """Or a failed JSON parse becomes a successful decode of the wrong bytes.

    Without ``validate=True`` base64 silently drops characters outside its
    alphabet, so a JSON document that failed to parse would be "decoded" into
    bytes that are not it. The input here is deliberately a near-miss: it looks
    base64-ish and contains characters that are not.
    """
    with pytest.raises(ValueError):
        decode_payload('{"v": 1, "tag": "unterminated')
