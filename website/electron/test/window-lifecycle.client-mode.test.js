"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { dashboardWindowHarness } = require("./window-lifecycle-test-helpers");

// Issue #14815. With the local gateway off and a Remote Crew configured for the
// launch port, the dashboard is the REMOTE gateway's, reached through
// `ssh -L 5476:localhost:5476`. Its URL is loopback, so every "is this
// localhost?" test says yes -- but the gateway is on another machine, and the
// store's crew record for that port is the only thing that says so (see
// `isGatewayLocalForWindow`). Two local-only integrations read the URL instead
// of the record and ran against the remote:
//
//   * the agent command channel drained `/api/browser/command-drain` for every
//     declared chat slot -- a loopback-only, internal-secret endpoint the remote
//     answers 403 forever -- and put THIS machine's `X-Internal-Secret` on the
//     wire to do it;
//   * the preload exposed every local-only bridge (crash reports, WSL, open
//     file), so the remote's SPA invoked channels the registrar then refused.

/** Let the channel loop run past its pending awaits (microtasks + one macrotask). */
async function settleLoop(rounds = 3) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

const CREW_ON_LAUNCH_PORT = { "5476": { host: "crew.example.test" } };

describe("client mode: a window whose port names a configured crew", () => {
  it("never drains the agent command channel, so no request carries the local secret", async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    // The remote's actual answer to a foreign internal secret: 403, which the
    // loop backs off from and retries for the life of the window.
    const fetchMock = t.mock.method(globalThis, "fetch", async () => ({
      ok: false, status: 403, json: async () => null,
    }));
    t.mock.method(console, "warn", () => {});
    const harness = dashboardWindowHarness({
      platform: "darwin",
      remoteHosts: CREW_ON_LAUNCH_PORT,
      readInternalSecret: () => "this-machines-internal-secret",
    });
    harness.lifecycle.setupWindowContents(harness.win, "http://localhost:5476");
    assert.equal(
      harness.lifecycle.security.isGatewayLocalForWindow(harness.win),
      false,
      "precondition: the store marks this loopback window REMOTE",
    );

    // A declared chat slot is what gives the channel something to drain for.
    assert.deepEqual(harness.lifecycle.browser.trackSession(harness.viewContents, "s1", true), { ok: true });
    await settleLoop();
    // And a tick of idle time changes nothing: the loop keeps idling.
    t.mock.timers.tick(5_000);
    await settleLoop();

    assert.deepEqual(
      fetchMock.mock.calls.map((call) => call.arguments[0]),
      [],
      "a window reaching a configured crew must make NO command-channel request",
    );
    assert.equal(harness.win._mcAgentChannel.isRunning(), true, "the loop idles; it is not torn down");
    await harness.win._mcAgentChannel.stop();
  });

  it("an unconfigured loopback window still drains for its declared slots (positive control)", async (t) => {
    // The same wiring with NO crew record is this machine's own gateway, and the
    // drain must keep running there or the native browser panel goes deaf. This
    // is also the residual the store cannot see: a hand-rolled `ssh -L` with no
    // crew record is indistinguishable from a local gateway here and still
    // drains (and 403s) -- gate 3 of the IPC registrar is the only probe that
    // positively identifies the listener, and the channel does not run it.
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const fetchMock = t.mock.method(globalThis, "fetch", async () => ({
      ok: false, status: 403, json: async () => null,
    }));
    const warned = [];
    t.mock.method(console, "warn", (line) => { warned.push(String(line)); });
    const harness = dashboardWindowHarness({
      platform: "darwin",
      readInternalSecret: () => "this-machines-internal-secret",
    });
    harness.lifecycle.setupWindowContents(harness.win, "http://localhost:5476");
    assert.equal(harness.lifecycle.security.isGatewayLocalForWindow(harness.win), true);

    harness.lifecycle.browser.trackSession(harness.viewContents, "s1", true);
    await settleLoop();

    // Two requests: the idle branch's host-presence heartbeat (no slots, zero
    // wait) that ran before the slot was declared, then the drain for it. The
    // 403 backoff then parks the loop on a mocked timer.
    const calls = fetchMock.mock.calls.map((call) => call.arguments);
    assert.equal(calls.length, 2, "heartbeat, then exactly one drain before the 403 backoff parks the loop");
    for (const [url, init] of calls) {
      assert.equal(url, "http://localhost:5476/api/browser/command-drain");
      assert.equal(init.headers["X-Internal-Secret"], "this-machines-internal-secret");
    }
    assert.deepEqual(JSON.parse(calls[0][1].body), { session_keys: [], wait_ms: 0 }, "heartbeat");
    assert.deepEqual(JSON.parse(calls[1][1].body).session_keys, ["s1"], "drain for the declared slot");
    // The refusal is logged with its phase, not as `[object Object]` (the
    // rendering in the issue's own log excerpt).
    const drainWarning = warned.find((line) => line.includes("drain: HTTP 403"));
    assert.ok(drainWarning, "the 403 must be reported");
    assert.match(drainWarning, /\[browser-agent-channel\] \{"phase":"drain-status","status":403\}: drain: HTTP 403/);
    assert.ok(!warned.some((line) => line.includes("[object Object]")), warned.join("\n"));
    await harness.win._mcAgentChannel.stop();
  });

  it("creates the dashboard view with --kc-remote-gateway, and a local window without it", async (t) => {
    // The preload reads this argument to withhold the local-only bridges, the
    // same way `--kc-linux-frameless` carries a launch-time decision to it.
    t.mock.timers.enable({ apis: ["setTimeout"] });
    t.mock.method(globalThis, "fetch", async () => ({ ok: true, status: 204, json: async () => null }));
    const viewArgs = async (options) => {
      const harness = dashboardWindowHarness(options);
      harness.lifecycle.setupWindowContents(harness.win, "http://localhost:5476");
      await harness.win._mcAgentChannel.stop();
      return harness.log[0];
    };

    assert.equal(
      await viewArgs({ platform: "darwin", remoteHosts: CREW_ON_LAUNCH_PORT }),
      'new WebContentsView:["--kc-remote-gateway"]',
    );
    assert.equal(await viewArgs({ platform: "darwin" }), "new WebContentsView:[]");

    // A crew on ANOTHER port is not this window's crew.
    assert.equal(
      await viewArgs({ platform: "darwin", remoteHosts: { "6124": { host: "other.example.test" } } }),
      "new WebContentsView:[]",
    );

    // Both launch-time arguments ride together on a frameless Linux window.
    assert.equal(
      await viewArgs({ platform: "linux", frameless: true, remoteHosts: CREW_ON_LAUNCH_PORT }),
      'new WebContentsView:["--kc-linux-frameless","--kc-remote-gateway"]',
    );
  });

  it("the argument is a creation-time snapshot: a crew configured afterwards does not change it", async (t) => {
    // Pinned residual, not an aspiration. The argument list of a WebContents is
    // fixed when it is created, so a window opened on a port with no crew record
    // that is later pointed at a crew in place (a connection window whose crew is
    // first saved from its token prompt, 'Set Remote Host…' on the tab menu, the
    // launch-failure dialog's Add Remote Crew) keeps exposing the local-only
    // bridges until it is recreated. For that window the renderer still asks,
    // and the registrar's three-gate `assertLocalDashboard` -- which reads the
    // store at call time -- still refuses, which is exactly the pre-#14815
    // behaviour. The main-process gates are the authority; the argument only
    // stops a window the shell OPENED against a crew from asking at all. The
    // agent channel, by contrast, re-reads the predicate on every iteration
    // (see the drain test above), so it needs no recreation to go idle.
    const remoteHosts = {};
    const harness = dashboardWindowHarness({ platform: "darwin", remoteHosts });
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const fetchMock = t.mock.method(globalThis, "fetch", async () => ({ ok: true, status: 204, json: async () => null }));
    harness.lifecycle.setupWindowContents(harness.win, "http://localhost:5476");
    assert.equal(harness.log[0], "new WebContentsView:[]");
    await settleLoop();
    // Local at creation: the idle branch sent its one host-presence heartbeat.
    assert.equal(fetchMock.mock.calls.length, 1);
    assert.deepEqual(JSON.parse(fetchMock.mock.calls[0].arguments[1].body).session_keys, []);

    remoteHosts["5476"] = { host: "crew.example.test" };
    assert.equal(
      harness.lifecycle.security.isGatewayLocalForWindow(harness.win),
      false,
      "the live predicate flips with the store...",
    );
    assert.equal(harness.log[0], "new WebContentsView:[]", "...the created view's arguments cannot");

    // ...but the agent channel can: a slot declared now is not drained, and the
    // heartbeat stops, because both re-read the predicate each iteration.
    harness.lifecycle.browser.trackSession(harness.viewContents, "s1", true);
    await settleLoop();
    t.mock.timers.tick(5_000);
    await settleLoop();
    assert.equal(fetchMock.mock.calls.length, 1, "no drain and no further heartbeat after the store marks the window remote");
    await harness.win._mcAgentChannel.stop();
  });
});
