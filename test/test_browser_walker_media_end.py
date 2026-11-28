"""The walker's media endings in a real headless Chromium playing a real file.

YouTube takes the source away when a song ends (``removeAttribute("src")`` then
``load()``): the element then reads paused at 0 with no duration and is never
``ended`` itself, so a reader that only looks at ``ended`` misses the end. The
walker counts the ending as it happens (``mediaEnds`` in ``snapshot_walker.js``) and
reports it as a new ``endId``. Here a real ``<video>`` plays a short advert that
hands over to the song on the same page (no ending), then the song, whose source
the page takes away at its end (one ending). The walker runs in its isolated world,
as the gateway runs it.

The browser is pinned, never searched for: set ``KIROCREW_TEST_CHROMIUM`` to a
Chromium binary to run this test, which skips without it. The same cases run on
every CI host with stubbed elements in
``website/src/test/snapshotWalker.mediaEnd.test.ts``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import tempfile
import wave
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.browser_harness import ops
from kiro_crew.browser_harness.relay import CdpRelay
from kiro_crew.testing.wait import async_wait_until

CHROMIUM_ENV = "KIROCREW_TEST_CHROMIUM"
_LISTENING = re.compile(rb"DevTools listening on (ws://\S+)")


def _wav(seconds: float) -> str:
    """A silent mono WAV of *seconds*, as a data URL."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(1)
        out.setframerate(8000)
        out.writeframes(b"\x80" * int(8000 * seconds))
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()


#: An advert first, then the song on the same page; at the song's end the page
#: takes its source away, as YouTube's player does.
PAGE = f"""<!doctype html><html><body>
<video id="v" src="{_wav(0.6)}" style="width:320px;height:180px"></video>
<script>
const v = document.getElementById('v');
window.stage = 'advert';
v.addEventListener('ended', () => {{
  if (window.stage === 'advert') {{
    window.stage = 'song';
    v.src = '{_wav(1.2)}';
    v.play();
  }} else {{
    window.stage = 'over';
    v.removeAttribute('src');
    v.load();
  }}
}});
</script></body></html>"""


async def _run(binary: str, profile: Path) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        binary,
        "--headless=new",
        "--no-first-run",
        "--disable-gpu",
        "--no-sandbox",
        "--autoplay-policy=no-user-gesture-required",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "about:blank",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        # Per-user browser state outside --user-data-dir stays in the test's own
        # directory; the singleton socket needs a short TMPDIR.
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
        drain = asyncio.ensure_future(proc.stderr.read())
        await relay.start(url)
        target = (await relay.send("Target.createTarget", {"url": "about:blank"}))["targetId"]

        async def ignore(_msg: dict[str, Any]) -> None:
            return None

        sid = await relay.attach(target, ignore)
        tree = await relay.send("Page.getFrameTree", {}, sid)
        frame = tree["frameTree"]["frame"]["id"]
        await relay.send("Page.setDocumentContent", {"frameId": frame, "html": PAGE}, sid)
        world = await relay.send(
            "Page.createIsolatedWorld",
            {"frameId": frame, "worldName": "kirocrew_guard_walker"},
            sid,
        )

        async def media() -> dict[str, Any]:
            out = await relay.send(
                "Runtime.evaluate",
                {
                    "expression": f"{ops.WALKER_SOURCE}.media('state')",
                    "contextId": world["executionContextId"],
                    "returnByValue": True,
                },
                sid,
            )
            return out["result"]["value"]

        async def page(expression: str) -> Any:
            out = await relay.send(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True, "awaitPromise": True},
                sid,
            )
            return out["result"].get("value")

        got: dict[str, Any] = {"before": await media()}
        await page("document.getElementById('v').play().then(() => true)")

        async def over() -> bool:
            return bool(await page("window.stage") == "over")

        await async_wait_until(over, describe=lambda: "the page never reached the song's end")
        got["stage"] = await page("window.stage")
        got["emptied"] = await media()
        # Past the walker's grace with nothing playing, the ending counts.
        last: dict[str, Any] = {}

        async def counted() -> bool:
            last.update(await media())
            return bool(last.get("ended"))

        await async_wait_until(counted, describe=lambda: f"no ending counted: {last}")
        got["counted"] = dict(last)
        got["again"] = await media()
        return got
    finally:
        await relay.close()
        if proc.returncode is None:
            proc.terminate()
        await asyncio.wait_for(proc.wait(), 30)
        if drain is not None:
            await drain


def test_a_song_whose_source_is_taken_away_at_the_end_counts_once_and_its_advert_not(
    tmp_path: Path,
) -> None:
    binary = os.environ.get(CHROMIUM_ENV, "")
    if not binary:
        pytest.skip(f"{CHROMIUM_ENV} is not set")
    got = asyncio.run(_run(binary, tmp_path / "profile"))
    assert got["stage"] == "over", got
    assert got["before"]["endId"] is None
    # The element as YouTube leaves it: never `ended`, no duration, at 0.
    emptied = got["emptied"]
    assert emptied["paused"] is True and emptied["duration"] is None, emptied
    assert emptied["currentTime"] == 0
    counted = got["counted"]
    # One ending: the song's. The advert handed over to the song on the same page.
    assert re.fullmatch(r"[a-z0-9]+:1", counted["endId"] or ""), counted
    assert counted["ended"] is True
    assert got["again"]["endId"] == counted["endId"]
