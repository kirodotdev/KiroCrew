"""Live vendor conformance -- the only test that can prove a fixture is true.

Everything else in the channel suite runs against fixtures. Fixtures encode a
claim about the vendor's API, and a claim can go stale silently: the vendor adds
a field, retypes one, or changes a content type, and our whole green suite keeps
agreeing with itself. This module closes that loop by replaying the recorded
SHAPE against the real endpoint.

It is opt-in and skipped by default -- it needs real credentials and makes real
network calls, so it must never run in the normal suite or on a fork PR:

    KIROCREW_WIRE_PROBE=1 \\
    KIROCREW_WIRE_PROBE_WEIXIN_TOKEN=<bot token> \\
    pytest test/test_channel_wire_conformance.py -v

On drift the failure names the exact field and both types (see
``assert_same_shape``), which is the actionable form: re-probe, rewrite the
fixture with a fresh ``live_probe`` provenance, and the whole stack re-verifies
against the new shape.

Only shapes are compared, never values -- tokens, ids and timestamps are
volatile by design and pinning them would make this lane flaky rather than
informative.
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
        """Re-verify the #711 contract against live iLink.

        This is the test that would have caught the original bug before it
        shipped: if ``qrcode_img_content`` ever stops being a string URL, or the
        response gains/loses fields, the fixture is stale and the diff says so.
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
        # Restate the load-bearing semantic, not just the type: the field is a
        # scannable URL. A string that stopped being a URL would pass a pure
        # shape check but break the QR renderer exactly as #711 did.
        assert str(live["qrcode_img_content"]).startswith("http"), (
            "qrcode_img_content is no longer a URL -- the QR render path "
            "(server-side encode to PNG data URI) assumes it is"
        )
