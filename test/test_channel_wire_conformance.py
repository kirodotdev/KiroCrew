"""Live vendor conformance -- the only test that can prove a fixture is true.

Fixtures can go stale silently when the vendor changes a field, so this replays the
recorded SHAPE against the real endpoint. Opt-in: it needs real credentials and the
network, so it never runs in the normal suite or on a fork PR:

    KIROCREW_WIRE_PROBE=1 \\
    KIROCREW_WIRE_PROBE_WEIXIN_TOKEN=<bot token> \\
    pytest test/test_channel_wire_conformance.py -v

On drift ``assert_same_shape`` names the field and both types; re-probe and rewrite
the fixture with a fresh ``live_probe`` provenance. Only shapes are compared: tokens,
ids and timestamps are volatile.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from kiro_crew.testing.channel_fixtures import assert_same_shape, load_fixture

# The fixtures root lives in the TEST tree, so the layout coupling lives here
# too -- kiro_crew.testing.channel_fixtures ships in the wheel and deliberately
# has no default root (no test/ tree exists in an installed package).
CHANNEL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "channels"

_PROBE_ENABLED = os.environ.get("KIROCREW_WIRE_PROBE") == "1"

pytestmark = pytest.mark.skipif(
    not _PROBE_ENABLED,
    reason=(
        "live vendor conformance is opt-in: set KIROCREW_WIRE_PROBE=1 plus the "
        "per-channel credential env vars (real network calls)"
    ),
)


def _require(var: str) -> str:
    val = os.environ.get(var, "")
    if not val:
        pytest.skip(f"{var} not set -- cannot probe this channel")
    return val


class TestWeixinConformance:
    def test_get_bot_qrcode_still_matches_the_recorded_shape(self) -> None:
        """Re-verify the QR-code response shape against live iLink.

        If ``qrcode_img_content`` stops being a string URL, or fields change, the
        fixture is stale and the diff says so.
        """
        from kiro_crew.weixin.client import WeixinClient

        token = _require("KIROCREW_WIRE_PROBE_WEIXIN_TOKEN")
        fixture = load_fixture("weixin", "get_bot_qrcode", root=CHANNEL_FIXTURES)

        async def _probe() -> dict:
            client = WeixinClient(token=token)
            await client.connect()
            try:
                return await client.get_bot_qrcode()
            finally:
                await client.close()

        live = asyncio.run(_probe())

        assert_same_shape(fixture.payload, live, context="weixin get_bot_qrcode")
        # The field must be a scannable URL: a non-URL string passes the shape check
        # but breaks the QR renderer.
        assert str(live["qrcode_img_content"]).startswith("http"), (
            "qrcode_img_content is no longer a URL -- the QR render path "
            "(server-side encode to PNG data URI) assumes it is"
        )


# WeCom has NO live probe on purpose: WeCom allows one subscription per bot, so a
# probe would disconnect the running gateway (`no-test-side-effects` forbids that).
# Frame shapes are pinned on a fake socket in `test_wecom_wire.py`, the pong ACK in
# `test_wecom_client_cov80.py::test_pong_ack_clears_the_pending_pong`, and unverified
# vendor semantics (e.g. non-empty `body.chatid` means "group", assumed by
# `wecom/transport.py::receive`) are named in each fixture's `_provenance`.
# A probe belongs only against a throwaway bot with its own credentials.
