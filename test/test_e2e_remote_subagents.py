"""Remote subagents end to end, headless: a real gateway in ``--test-mode`` on the
packaged fake ACP backend (with its delegation bridge on), a fake A2A remote
agent on loopback, and the two smoke-shaped stories driven over HTTP exactly as
the GUI user-test's pixel tester would drive them through the chat box.

This is the headless twin of ``test/gui_user/scenarios/subagents-remote-spawn.yaml``
and ``subagents-remote-continue.yaml``: same messages, same assertions, no
browser and no model. It also pins the wiring the GUI boot relies on -- the
``a2a_agents`` config entry, ``KIROCREW_FAKE_ACP_SPAWN_BRIDGE``, the bridge's
loopback call, ``A2AProvider`` against the fake -- so a nightly GUI failure can
be told apart from a plumbing regression.

Gated behind ``KIROCREW_E2E=1`` like ``test_e2e_smoke.py``; needs only loopback.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import json
import os
import threading
import time
import urllib.request

import pytest

from kiro_crew.testing import fake_a2a_server

pytestmark = pytest.mark.skipif(
    not os.environ.get("KIROCREW_E2E"),
    reason="E2E remote-subagent tests. Set KIROCREW_E2E=1 to run.",
)

_REMOTE = "remote-demo"


def _opener(handle):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"http://localhost:{handle.port}/api/status?token={handle.token}", timeout=10):
        pass
    return opener


def _post(opener, port: int, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"http://localhost:{port}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with opener.open(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", "replace")
    # /api/chat streams SSE; the last JSON object we can parse is fine for our
    # purposes (we assert on the subagent runs, not on the chat transcript).
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw}


def _get(opener, port: int, path: str) -> dict:
    with opener.open(f"http://localhost:{port}{path}", timeout=10) as resp:
        return json.loads(resp.read())


def _wait_runs(opener, port: int, *, want: int, timeout: float = 60.0) -> list[dict]:
    """Poll /api/spawn until ``want`` remote runs exist and are all done."""
    deadline = time.monotonic() + timeout
    last: list[dict] = []
    while time.monotonic() < deadline:
        listing = _get(opener, port, "/api/spawn")
        runs = listing.get("agents") or listing.get("subagents") or listing.get("runs") or []
        if isinstance(listing, list):
            runs = listing
        remote = [r for r in runs if r.get("agent") == _REMOTE]
        if len(remote) >= want and all(r.get("done") for r in remote):
            return remote
        last = remote
        time.sleep(0.5)
    raise AssertionError(f"wanted {want} finished {_REMOTE} runs, have {last}")


def _result_text(opener, port: int, run_id: str) -> str:
    d = _get(opener, port, f"/api/spawn/{run_id}")
    return str(d.get("result") or "")


class _Sidecar:
    """The fake A2A server on its own event loop thread for the gateway subprocess to reach."""

    def __init__(self) -> None:
        self.card_url = ""
        self.server: fake_a2a_server.FakeA2AServer | None = None
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        runner, self.card_url, self.server = self._loop.run_until_complete(fake_a2a_server.serve(0))
        self._runner = runner
        self._ready.set()
        self._loop.run_forever()
        self._loop.run_until_complete(runner.cleanup())

    def start(self) -> "_Sidecar":
        self._thread.start()
        assert self._ready.wait(10), "fake A2A server did not start"
        return self

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)


@pytest.fixture(scope="module")
def stack():
    """Sidecar + gateway. The gateway env carries the bridge flag so every fake ACP
    child (the primary session's backend) inherits it."""
    from kiro_crew.testing import fake_acp_backend
    from kiro_crew.testing.harness import spawn_feature_gateway

    sidecar = _Sidecar().start()
    previous_bin = os.environ.get("KIROCREW_KIRO_BIN")
    previous_bridge = os.environ.get("KIROCREW_FAKE_ACP_SPAWN_BRIDGE")
    # The primary agent MUST be the packaged fake (never a real kiro-cli on the
    # host): the directives are the fake's, and a real model would just obey the
    # message literally. Same guard test_e2e_smoke.py applies.
    os.environ["KIROCREW_KIRO_BIN"] = str(fake_acp_backend.__file__)
    os.environ["KIROCREW_FAKE_ACP_SPAWN_BRIDGE"] = "1"
    try:
        with spawn_feature_gateway(fixture="rich", approval="yolo") as handle:
            # Register the remote agent the way the GUI boot's seed_home.py does,
            # after boot: the loader re-reads config.json on every load().
            cfg_path = handle.home / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["a2a_agents"] = [
                {"name": _REMOTE, "agent_card_url": sidecar.card_url, "auth": {"scheme": "none"}}
            ]
            # Hosts without a user-namespace sandbox (some corporate dev boxes)
            # cannot isolate the fake ACP subprocess; the gateway then refuses to
            # spawn it at all. Allow it for this throwaway home -- the process is
            # the packaged fake, not an agent. CI runners have userns and ignore it.
            cfg.setdefault("agent", {})["sandbox_allow_unsandboxed_exec"] = True
            cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            yield handle, sidecar
    finally:
        if previous_bridge is None:
            os.environ.pop("KIROCREW_FAKE_ACP_SPAWN_BRIDGE", None)
        else:
            os.environ["KIROCREW_FAKE_ACP_SPAWN_BRIDGE"] = previous_bridge
        if previous_bin is None:
            os.environ.pop("KIROCREW_KIRO_BIN", None)
        else:
            os.environ["KIROCREW_KIRO_BIN"] = previous_bin
        sidecar.stop()


def test_spawn_story_remote_run_completes_with_codeword(stack):
    handle, sidecar = stack
    opener = _opener(handle)
    slot = _post(opener, handle.port, "/api/chat/slots", {})["key"]

    reply = _post(
        opener,
        handle.port,
        "/api/chat",
        {"slot": slot, "message": f"[[SPAWN:{_REMOTE}]] Reply with only the word OSPREY-3"},
    )
    try:
        runs = _wait_runs(opener, handle.port, want=1)
    except AssertionError as exc:
        # The fake's reply text is the bridge's own diagnosis (port, credential,
        # API refusal); surface it rather than a bare "no runs".
        raise AssertionError(f"{exc}\n--- chat reply ---\n{str(reply)[:2000]}") from None
    run = runs[0]
    assert not run.get("error"), run
    assert "OSPREY-3" in _result_text(opener, handle.port, run["id"])
    # The remote actually received it: one conversation, one task, no contextId on the wire.
    assert sidecar.server is not None and len(sidecar.server.conversations) >= 1
    first = sidecar.server.requests[0]["params"]["message"]
    assert "contextId" not in first


def test_continue_story_recalls_the_codeword_in_a_new_task(stack):
    handle, sidecar = stack
    opener = _opener(handle)
    slot = _post(opener, handle.port, "/api/chat/slots", {})["key"]
    before = len(sidecar.server.conversations) if sidecar.server else 0

    _post(
        opener,
        handle.port,
        "/api/chat",
        {
            "slot": slot,
            "message": f"[[SPAWN:{_REMOTE}]] Remember the codeword HERON-9. Reply only with OK.",
        },
    )
    runs = _wait_runs(opener, handle.port, want=before + 1 if before else 1)
    first_run = [r for r in runs if "HERON-9" in str(r.get("task", ""))][-1]
    assert "OK" in _result_text(opener, handle.port, first_run["id"])

    _post(
        opener,
        handle.port,
        "/api/chat",
        {
            "slot": slot,
            "message": f"[[CONTINUE:{_REMOTE}]] What was the codeword? Reply with just the codeword.",
        },
    )
    runs = _wait_runs(opener, handle.port, want=len(runs) + 1)
    second_run = [r for r in runs if "codeword?" in str(r.get("task", ""))][-1]
    assert "HERON-9" in _result_text(opener, handle.port, second_run["id"])
    # Continuity on the wire: the follow-up carried the retained contextId, so
    # the remote saw ONE conversation with two turns, not two conversations.
    assert sidecar.server is not None
    last = sidecar.server.requests[-1]["params"]["message"]
    assert last.get("contextId") in sidecar.server.conversations
    assert len(sidecar.server.conversations[last["contextId"]].turns) == 2
