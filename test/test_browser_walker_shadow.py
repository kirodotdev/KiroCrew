"""The snapshot walker in a real headless Chromium, on a web-component dialog.

Alaska Airlines draws its fare upsell as ``<fs-auro-dialog>``: a 0-px-high host
whose open shadow root holds a fixed dialog with a ``<slot>``, and the dialog's
content (a scroll box) is the host's light-DOM child. Its buttons are
``<auro-button>``s: an inner shadow ``<button>`` around a ``<slot>``, labelled by
the host's text. ``offsetParent`` is null across the shadow boundary and a shadow
button's own ``textContent`` holds no slotted text, so the walker judges
visibility with ``checkVisibility()`` and names through ``assignedNodes()``.

The browser is pinned, never searched for: set ``KIROCREW_TEST_CHROMIUM`` to a
Chromium binary to run this test, which skips without it. The same page shape
runs on every CI host in ``website/src/test/snapshotWalker.shadowDom.test.ts``.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.browser_harness import ops
from kiro_crew.browser_harness.relay import CdpRelay

CHROMIUM_ENV = "KIROCREW_TEST_CHROMIUM"

PAGE = """<!doctype html><html><body style="margin:0">
<script>
class XButton extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: 'open'}).innerHTML =
    '<button style="padding:8px"><span style="overflow:hidden"><slot></slot></span></button>'; }
}
class XDialog extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: 'open'}).innerHTML =
    '<div style="position:fixed;top:40px;left:40px;width:400px;height:200px;background:#fff"><slot></slot></div>'; }
}
class XLabel extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: 'open'}).innerHTML =
    '<button style="padding:8px"><slot name="label"></slot><slot style="display:none"></slot></button>'; }
}
customElements.define('x-button', XButton);
customElements.define('x-dialog', XDialog);
customElements.define('x-label', XLabel);
</script>
<main><p>Fares</p><x-button id="cart">Add to cart</x-button>
<x-button id="pay">Pay<textarea style="display:none">tok-in-markup</textarea><span hidden>note-hidden</span></x-button>
<x-button>Continue <span style="display:contents">to seats</span></x-button>
<x-button>$1<span>.99</span></x-button>
<x-label><span slot="label">Book</span>stray-text</x-label></main>
<x-dialog style="display:block;height:0"><div style="position:relative;overflow:auto;height:180px">
  <h2>Get your money back</h2><x-button>Continue with flights</x-button></div></x-dialog>
<div style="display:none"><button>Hidden away</button></div>
</body></html>"""

_LISTENING = re.compile(rb"DevTools listening on (ws://\S+)")


async def _snapshot(binary: str, profile: Path) -> list[dict[str, Any]]:
    proc = await asyncio.create_subprocess_exec(
        binary,
        "--headless=new",
        "--no-first-run",
        "--disable-gpu",
        "--no-sandbox",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "about:blank",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        # The browser's per-user state outside --user-data-dir (NSS db, caches)
        # goes under HOME and XDG dirs: point them, and the CWD, into the test's
        # own directory so nothing outlives the run. Chromium's singleton socket
        # lives in TMPDIR and aborts on a path past the unix-socket limit, which
        # the runner's per-test temp base can exceed.
        cwd=str(profile.parent),
        env={
            **os.environ,
            "HOME": str(profile.parent / "home"),
            "XDG_CACHE_HOME": str(profile.parent / "cache"),
            "XDG_CONFIG_HOME": str(profile.parent / "config"),
            "TMPDIR": os.environ.get("KIROCREW_TEST_SHORT_TMP_ROOT") or tempfile.gettempdir(),
        },
    )
    relay = CdpRelay()
    drain: asyncio.Task[bytes] | None = None
    try:
        assert proc.stderr is not None
        url = None
        while url is None:
            line = await asyncio.wait_for(proc.stderr.readline(), 30)
            if not line:
                raise RuntimeError("chromium exited before it listened")
            found = _LISTENING.search(line)
            url = found.group(1).decode() if found else None
        # Keep reading so a chatty Chromium never blocks on a full pipe.
        drain = asyncio.ensure_future(proc.stderr.read())
        await relay.start(url)
        target = (await relay.send("Target.createTarget", {"url": "about:blank"}))["targetId"]

        async def ignore(_msg: dict[str, Any]) -> None:
            return None

        sid = await relay.attach(target, ignore)
        tree = await relay.send("Page.getFrameTree", {}, sid)
        frame = tree["frameTree"]["frame"]["id"]
        # Parses and runs the page's scripts before it answers, so the custom
        # elements are defined and upgraded by the time the walker runs.
        await relay.send("Page.setDocumentContent", {"frameId": frame, "html": PAGE}, sid)
        out = await relay.send(
            "Runtime.evaluate",
            {"expression": f"{ops.WALKER_SOURCE}.snapshot()", "returnByValue": True},
            sid,
        )
        return out["result"]["value"]["nodes"]
    finally:
        await relay.close()
        if proc.returncode is None:
            proc.terminate()
        await asyncio.wait_for(proc.wait(), 30)
        if drain is not None:
            await drain


def test_a_slotted_web_component_dialog_and_its_buttons_are_in_the_snapshot(
    tmp_path: Path,
) -> None:
    binary = os.environ.get(CHROMIUM_ENV, "")
    if not binary:
        pytest.skip(f"{CHROMIUM_ENV} is not set")
    nodes = asyncio.run(_snapshot(binary, tmp_path / "profile"))
    buttons = [n["name"] for n in nodes if n["role"] == "button"]
    # A shadow <button> is named by the text slotted into it.
    assert "Add to cart" in buttons
    # Slotted field text and hidden slotted text never join a name.
    assert "Pay" in buttons
    assert not any("tok-in-markup" in b or "note-hidden" in b for b in buttons)
    # A display:contents wrapper's text counts; pieces join as drawn; a hidden
    # slot adds nothing.
    assert "Continue to seats" in buttons
    assert "$1.99" in buttons
    assert "Book" in buttons
    # The dialog slotted into a fixed shadow box is walked, buttons and heading.
    assert "Continue with flights" in buttons
    assert any(n["role"] == "heading" and n["name"] == "Get your money back" for n in nodes)
    # display: none still hides.
    assert "Hidden away" not in buttons
    assert all(n.get("ref") for n in nodes if n["role"] == "button")
