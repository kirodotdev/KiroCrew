// resolveSenderIsLocal: the three-gate sender-locality check that decides
// whether a WebContents counts as the local dashboard, its verdict cache, and
// the app-menu channels that exercise it. Covers the remote-origin reject path
// and its throttled logging, verdict expiry and re-probing, navigation
// invalidation, the generation-counter guard on the cache write, and the
// paint-without-waiting behaviour for a lapsed or inconclusive probe.
//
// The shared harness scaffolding lives in ipc-registrar-test-helpers.js.

"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  SOURCE,
  harness,
  lastCall,
} = require("./ipc-registrar-test-helpers");

// Sibling of "shell handlers preserve sender, argument, and return shapes"
// covering the REMOTE-origin path for `app-menu:execute`. A connection
// window pointed at a foreign gateway shares this preload, so the same
// listener sees a `senderFrame.url` that fails gate 1 of
// assertLocalDashboard. The handler must NOT throw at the renderer (menu
// dispatch is fire-and-forget) — it must swallow the gate reject, log it
// through glog, and forward `senderIsLocal=false` so `menu.execute` still
// runs (safe items dispatch, LOCAL_ONLY items become no-ops).
test("app-menu:execute forwards senderIsLocal=false and logs the reject when the sender is remote", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const senderFrame = { url: "http://evil-gateway.example.com/chat" };
  const sender = { id: "remote-sender", mainFrame: senderFrame };
  const event = { sender, senderFrame };

  // Fire-and-forget: the listener must not throw to the renderer even though
  // the gate will reject internally.
  assert.doesNotThrow(() => {
    h.listeners.get("app-menu:execute")(event, "edit-menu", 3);
  });
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(
    lastCall(h.windowCalls, "menu.execute").slice(1),
    [sender, "edit-menu", 3, /*senderIsLocal*/ false],
  );
  assert.ok(
    h.logs.some((line) => line.includes("app-menu:execute rejected for sender origin")),
    "gate reject must be logged through glog so investigators can attribute a silent LOCAL_ONLY refusal",
  );
});

// GPT 5.6 Review F1 on 7234d97b9 — UPHOLD-FENCED, adjudication `flagged=0`
// (no rarity argument available, unlike the earlier probe-DoS findings which
// adjudication flagged as pre-existing-on-base edge cases).
//
// `glog` is an unrotated synchronous `fs.appendFileSync` on the MAIN thread
// (`main.js` `glog`), and the gate writes one line per rejection. What makes
// this finding different from its predecessors: two of the three channels this
// PR routes through the gate are fire-and-forget `ipcRenderer.send` paths —
// `app-menu:execute` and `dev-mode-changed` (`preload.js`) — so a remote page
// loops them at renderer speed with NO backpressure. Every PRE-EXISTING gated
// channel (`crash-reports:get`/`reveal`, `wsl:detect`, `dashboard:open-file`,
// `pane:clear-http-cache`) is an awaited `ipcRenderer.invoke` that
// self-throttles to the IPC round trip, so the "pre-existing on base" rebuttal
// that cleared the probe findings genuinely does not cover these two.
//
// The throttle bounds the WRITE, never the VERDICT. Every call still runs all
// three gates and still rejects, so there is no cached `false` to strand a
// legitimate local user on a permanently-greyed titlebar menu — the failure
// mode the negative-cache removal (Design Review) exists to prevent. This is
// the same shape as the single-flight coalescer: a third option that satisfies
// both lanes where their literal asks conflicted.
test("a flooding remote sender cannot force one gate log write per call", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const senderFrame = { url: "http://evil-gateway.example.com/chat" };
  const sender = { id: "flooding-sender", mainFrame: senderFrame };
  const event = { sender, senderFrame };

  const FLOOD = 200;
  // Each IPC message arrives as its OWN main-thread task, and microtasks drain
  // between tasks — so the single-flight coalescer is EMPTY when the next
  // message lands and cannot collapse a sustained loop. Firing the whole burst
  // synchronously would be collapsed by the coalescer and would test nothing;
  // awaiting a turn between calls is the real attack shape.
  for (let index = 0; index < FLOOD; index += 1) {
    h.listeners.get("app-menu:execute")(event, "edit-menu", index);
    await new Promise((resolve) => setImmediate(resolve));
  }

  const rejectionLines = h.logs.filter((line) => line.includes("app-menu:execute rejected"));

  // The FIRST rejection must survive — a throttle that silences the
  // diagnostic entirely trades one failure for another.
  assert.ok(
    rejectionLines.length >= 1,
    "the first rejection must still be logged or the gate becomes silent",
  );
  // Bounded well below the call count: this is the whole finding.
  assert.ok(
    rejectionLines.length < FLOOD / 10,
    `flood of ${FLOOD} produced ${rejectionLines.length} log writes — `
      + "the rejection log must be throttled per WebContents, not written per call",
  );
  // The verdict path is UNTOUCHED: every call still reached `menu.execute`
  // with `senderIsLocal=false`. Throttling the write must not throttle,
  // cache, or short-circuit the security decision.
  const executeCalls = h.windowCalls.filter((call) => call[0] === "menu.execute");
  assert.equal(
    executeCalls.length,
    FLOOD,
    "every flooded call must still be gated and dispatched — the throttle covers the LOG, not the VERDICT",
  );
  for (const call of executeCalls) {
    assert.equal(call[4], false, "every flooded call must still resolve senderIsLocal=false");
  }
});

// A flooding WebContents must not spend ANOTHER window's diagnostic budget:
// the throttle is keyed per WebContents, so a second window's first rejection
// still lands even after the first sender has exhausted its own burst.
test("the gate log throttle is per WebContents, not global", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const floodFrame = { url: "http://evil-gateway.example.com/chat" };
  const floodSender = { id: "flooding-sender", mainFrame: floodFrame };
  for (let index = 0; index < 200; index += 1) {
    h.listeners.get("app-menu:execute")(
      { sender: floodSender, senderFrame: floodFrame },
      "edit-menu",
      index,
    );
    await new Promise((resolve) => setImmediate(resolve));
  }

  const quietFrame = { url: "http://other-gateway.example.com/chat" };
  const quietSender = { id: "quiet-sender", mainFrame: quietFrame };
  h.logs.length = 0;
  h.listeners.get("app-menu:execute")(
    { sender: quietSender, senderFrame: quietFrame },
    "file-menu",
    0,
  );
  await new Promise((resolve) => setImmediate(resolve));

  assert.ok(
    h.logs.some((line) => line.includes("app-menu:execute rejected")),
    "a second WebContents must keep its own diagnostic budget — one flooding "
      + "sender cannot blind investigators to every other window's rejects",
  );
});

// A sustained flood — the actual attack — must stay VISIBLE at full magnitude:
// once the throttle window rolls over, the suppressed count is reported, so an
// investigator reading `gateway-launch.log` sees the scale rather than a
// truncated handful of lines that understate it.
test("a sustained flood reports its suppressed volume when the throttle window rolls", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const senderFrame = { url: "http://evil-gateway.example.com/chat" };
  const sender = { id: "flooding-sender", mainFrame: senderFrame };
  const event = { sender, senderFrame };

  const realDateNow = Date.now;
  const windowStart = realDateNow.call(Date);
  Date.now = () => windowStart;
  try {
    // First window: exhaust the burst and bank suppressed rejections.
    for (let index = 0; index < 40; index += 1) {
      h.listeners.get("app-menu:execute")(event, "edit-menu", index);
      await new Promise((resolve) => setImmediate(resolve));
    }
    const firstWindowWrites = h.logs.filter((line) => line.includes("rejected")).length;
    assert.ok(
      firstWindowWrites > 0 && firstWindowWrites < 40,
      `expected a bounded burst inside one window, got ${firstWindowWrites} of 40`,
    );

    // Roll past the window; the NEXT rejection flushes the suppressed count.
    Date.now = () => windowStart + 60_001;
    h.logs.length = 0;
    h.listeners.get("app-menu:execute")(event, "edit-menu", 40);
    await new Promise((resolve) => setImmediate(resolve));

    const summary = h.logs.find((line) => line.includes("further local-dashboard gate rejections suppressed"));
    assert.ok(summary, "the window rollover must report how many rejections it suppressed");
    assert.match(
      summary,
      /^\d+ further local-dashboard gate rejections suppressed for this sender in the last 60000ms$/,
      `summary must name the suppressed count and the window — got ${JSON.stringify(summary)}`,
    );
  } finally {
    Date.now = realDateNow;
  }
});

// `app-menu:items` never REJECTS — blanking a connection window's menu
// dropdown is bad UX and buys no security. It still delegates to
// windows.menu.items so labels/accelerators reach the renderer. What the
// handler MUST do is thread the sender's classification down so the
// enabled column reflects the per-action gate; LOCAL_ONLY rows get
// `enabled: false` inside `serializeMenuItems`, not a silent no-op at
// execute time. Pin: remote sender → senderIsLocal=false forwarded.
test("app-menu:items forwards senderIsLocal=false so LOCAL_ONLY rows advertise as disabled", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const senderFrame = { url: "http://evil-gateway.example.com/chat" };
  const sender = { id: "remote-sender", mainFrame: senderFrame };
  const event = { sender, senderFrame };

  const result = await h.handlers.get("app-menu:items")(event, "file-menu");
  assert.deepEqual(result, { from: "menu.items" });
  assert.deepEqual(
    lastCall(h.windowCalls, "menu.items").slice(1),
    [sender, "file-menu", /*senderIsLocal*/ false],
  );
  // Gate logged the reject so investigators can attribute a greyed row to
  // a REMOTE sender rather than a native `enabled: false` from the menu.
  assert.ok(
    h.logs.some((line) => line.includes("app-menu:items rejected for sender origin")),
    "gate reject must be logged through glog",
  );
});

// dev-mode-changed flips the visibility of the DevTools menu item. A remote
// sender must not be able to un-hide it: even though clicking DevTools is
// separately blocked by LOCAL_ONLY_ITEM_IDS, leaving visibility un-gated
// hands a remote a UI knob on the local user's menu. Log-and-drop is the
// right shape here — a hostile IPC should not throw into the renderer, and
// the local user's dev-mode state must not change.
test("dev-mode-changed is refused for a remote sender", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const senderFrame = { url: "http://evil-gateway.example.com/chat" };
  const sender = { id: "remote-sender", mainFrame: senderFrame };
  const event = { sender, senderFrame };

  assert.doesNotThrow(() => {
    h.listeners.get("dev-mode-changed")(event, true);
  });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(
    h.windowCalls.some(([name]) => name === "menu.setDevMode"),
    false,
    "a remote sender must not flip devtools visibility",
  );
  assert.ok(
    h.logs.some((line) => line.includes("dev-mode-changed rejected for sender origin")),
    "gate reject must be logged through glog",
  );
});

// A cached POSITIVE verdict asserts three facts, and one of them is mutable
// independently of the sender: gate 3's port ownership. Gates 1 and 2 are
// properties of the PAGE, so a navigation invalidates them and the cache is
// wired to exactly that. Gate 3 is a property of the MACHINE — the primary
// port's LISTEN owner — and nothing about the sender changes when our gateway
// exits and a foreign process or an `ssh -L` tunnel rebinds that port. The
// cache would then keep returning `true`, skipping the one gate that exists to
// catch a tunnel, and keep unlocking the clipboard roles and devtools-toggle
// for the WebContents' remaining lifetime. SPA route changes fire
// `did-navigate-in-page` and are deliberately non-clearing, so there is no
// recovery short of a full top-frame navigation.
//
// The verdict is therefore given a short expiry: the cache still collapses the
// repeat calls one menu interaction produces (the netstat/PowerShell shell-out
// is why it exists at all — see Spock FUNC-01), but a positive older than the
// window falls through to a fresh three-gate probe. This bounds the WRITE of
// staleness, never the verdict itself: a `false` is still never cached, so a
// transient probe hiccup still cannot strand a legitimate local user on a
// permanently-greyed menu. See the F1 finding in GPT 5.6 Review on bd68985db
// (UPHOLD-FENCED, adjudication `flagged=0`).
function localSenderEvent(id = "local-sender") {
  const senderFrame = { url: "http://localhost:5476/chat" };
  const sender = { id, mainFrame: senderFrame, on() {} };
  return { sender, senderFrame };
}

function probeCount(h) {
  return h.gatewayCalls.filter(([name]) => name === "probePrimaryPortOwner").length;
}

test("a positive verdict older than its window re-runs all three gates", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const event = localSenderEvent();

  const realDateNow = Date.now;
  const t0 = realDateNow.call(Date);
  Date.now = () => t0;
  try {
    await h.handlers.get("app-menu:items")(event, "file-menu");
    assert.equal(probeCount(h), 1, "the first call must probe");

    // Past the window: the verdict is no longer good enough to answer with.
    Date.now = () => t0 + 5_001;
    await h.handlers.get("app-menu:items")(event, "file-menu");
    assert.equal(
      probeCount(h),
      2,
      "a lapsed positive must fall through to a fresh port-ownership probe — "
        + "otherwise the verdict outlives the fact gate 3 checks",
    );
  } finally {
    Date.now = realDateNow;
  }
});

test("a lapsed positive whose port owner has changed resolves to not-local", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const event = localSenderEvent();

  const realDateNow = Date.now;
  const t0 = realDateNow.call(Date);
  Date.now = () => t0;
  try {
    await h.handlers.get("app-menu:items")(event, "file-menu");
    assert.deepEqual(
      lastCall(h.windowCalls, "menu.items").slice(1),
      [event.sender, "file-menu", /*senderIsLocal*/ true],
      "a sender passing all three gates must resolve local",
    );

    // Our gateway released the port and something else took it. Nothing about
    // the sender changed, so no navigation fires and nothing else can notice.
    h.gateway.probePrimaryPortOwner = (...args) => {
      h.gatewayCalls.push(["probePrimaryPortOwner", ...args]);
      return Promise.resolve("foreign");
    };
    Date.now = () => t0 + 5_001;

    // The click is the real gate: a lapsed positive must not answer it.
    h.listeners.get("app-menu:execute")(event, "edit-menu", 2);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(
      lastCall(h.windowCalls, "menu.execute").slice(1),
      [event.sender, "edit-menu", 2, /*senderIsLocal*/ false],
      "once a foreign listener owns the primary port the verdict MUST flip — a "
        + "stale `true` here keeps unlocking clipboard read and DevTools against "
        + "a port this shell no longer owns",
    );

    // The foreign finding revoked the last positive, so the next paint greys.
    await h.handlers.get("app-menu:items")(event, "file-menu");
    assert.deepEqual(
      lastCall(h.windowCalls, "menu.items").slice(1),
      [event.sender, "file-menu", /*senderIsLocal*/ false],
      "a revoked positive must not keep painting LOCAL_ONLY rows enabled",
    );
    assert.ok(
      h.logs.some((line) => line.includes("held by foreign")),
      "the re-probe's rejection must be logged so the flip is attributable",
    );
  } finally {
    Date.now = realDateNow;
  }
});

// After an idle gap the menu must paint without
// waiting on the netstat/PowerShell probe. The probe below never settles, so a
// paint that awaited it would hang this test.
test("a lapsed positive paints the menu without waiting on the re-probe", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const event = localSenderEvent();

  const realDateNow = Date.now;
  const t0 = realDateNow.call(Date);
  Date.now = () => t0;
  try {
    await h.handlers.get("app-menu:items")(event, "file-menu");
    h.gateway.probePrimaryPortOwner = (...args) => {
      h.gatewayCalls.push(["probePrimaryPortOwner", ...args]);
      return new Promise(() => {});
    };
    Date.now = () => t0 + 10_000;
    await h.handlers.get("app-menu:items")(event, "file-menu");
    assert.deepEqual(
      lastCall(h.windowCalls, "menu.items").slice(1),
      [event.sender, "file-menu", /*senderIsLocal*/ true],
    );
    assert.equal(probeCount(h), 2, "the paint must still start a background re-probe");
  } finally {
    Date.now = realDateNow;
  }
});

// A probe that cannot name the holder (`unknown`) or finds the port briefly
// unbound while our gateway restarts (`none`) is no evidence of a foreign one,
// so it must not grey the local window's own menu. It is no proof of OUR
// ownership either, so it must not authorize a click: a probe failure can
// coincide with a foreign listener on the port.
test("an inconclusive re-probe refuses the action but keeps the rows painted", async () => {
  for (const owner of ["unknown", "none"]) {
    const h = harness({ storeValues: { runLocalGateway: true } });
    h.registrar.registerShell();
    const event = localSenderEvent(`inconclusive-${owner}`);

    const realDateNow = Date.now;
    const t0 = realDateNow.call(Date);
    Date.now = () => t0;
    try {
      await h.handlers.get("app-menu:items")(event, "file-menu");
      h.gateway.probePrimaryPortOwner = (...args) => {
        h.gatewayCalls.push(["probePrimaryPortOwner", ...args]);
        return Promise.resolve(owner);
      };
      Date.now = () => t0 + 5_001;
      h.listeners.get("app-menu:execute")(event, "edit-menu", 2);
      await new Promise((resolve) => setImmediate(resolve));
      assert.deepEqual(
        lastCall(h.windowCalls, "menu.execute").slice(1),
        [event.sender, "edit-menu", 2, /*senderIsLocal*/ false],
        `${owner}: only a fresh local owner may authorize a LOCAL_ONLY action`,
      );

      await h.handlers.get("app-menu:items")(event, "file-menu");
      assert.deepEqual(
        lastCall(h.windowCalls, "menu.items").slice(1),
        [event.sender, "file-menu", /*senderIsLocal*/ true],
        `${owner}: an inconclusive probe must not grey the local user's rows`,
      );

      // Kept, not re-minted: the next action probes again instead of trusting
      // the inconclusive gap for a fresh window. Let the paint's background
      // re-probe settle first, or the action joins it instead of probing.
      await new Promise((resolve) => setImmediate(resolve));
      const before = probeCount(h);
      h.listeners.get("app-menu:execute")(event, "edit-menu", 2);
      await new Promise((resolve) => setImmediate(resolve));
      assert.equal(probeCount(h), before + 1, `${owner}: a kept positive must not be re-minted`);
    } finally {
      Date.now = realDateNow;
    }
  }
});

// A kept positive paints rows the click path refuses, so it may only outlast a
// short restart-sized gap. A probe that keeps failing must grey the rows (with
// the footer explaining them) rather than leave a menu that silently does nothing.
test("an inconclusive probe stops painting rows enabled after the grace period", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const event = localSenderEvent("inconclusive-past-grace");

  const realDateNow = Date.now;
  const t0 = realDateNow.call(Date);
  Date.now = () => t0;
  try {
    await h.handlers.get("app-menu:items")(event, "file-menu");
    h.gateway.probePrimaryPortOwner = (...args) => {
      h.gatewayCalls.push(["probePrimaryPortOwner", ...args]);
      return Promise.resolve("unknown");
    };
    Date.now = () => t0 + 34_999;
    await h.handlers.get("app-menu:items")(event, "file-menu");
    assert.deepEqual(
      lastCall(h.windowCalls, "menu.items").slice(1),
      [event.sender, "file-menu", /*senderIsLocal*/ true],
      "inside the grace a restart-sized probe gap must not grey the rows",
    );
    await new Promise((resolve) => setImmediate(resolve));

    Date.now = () => t0 + 35_001;
    await h.handlers.get("app-menu:items")(event, "file-menu");
    assert.deepEqual(
      lastCall(h.windowCalls, "menu.items").slice(1),
      [event.sender, "file-menu", /*senderIsLocal*/ false],
      "past the grace an inconclusive probe must grey the rows, never paint a dead menu",
    );
  } finally {
    Date.now = realDateNow;
  }
});

test("an inconclusive probe with no earned positive still fails closed", async () => {
  for (const owner of ["unknown", "none"]) {
    const h = harness({ storeValues: { runLocalGateway: true }, primaryPortOwner: owner });
    h.registrar.registerShell();
    const event = localSenderEvent(`first-${owner}`);
    await h.handlers.get("app-menu:items")(event, "file-menu");
    assert.deepEqual(
      lastCall(h.windowCalls, "menu.items").slice(1),
      [event.sender, "file-menu", /*senderIsLocal*/ false],
      `${owner}: a first call has no positive to keep and must refuse`,
    );
  }
});

test("a foreign finding is not undone by a later inconclusive probe", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const event = localSenderEvent();
  const owners = ["foreign", "unknown"];
  const realDateNow = Date.now;
  const t0 = realDateNow.call(Date);
  Date.now = () => t0;
  try {
    await h.handlers.get("app-menu:items")(event, "file-menu");
    h.gateway.probePrimaryPortOwner = (...args) => {
      h.gatewayCalls.push(["probePrimaryPortOwner", ...args]);
      return Promise.resolve(owners.shift());
    };
    Date.now = () => t0 + 5_001;
    h.listeners.get("app-menu:execute")(event, "edit-menu", 2);
    await new Promise((resolve) => setImmediate(resolve));
    h.listeners.get("app-menu:execute")(event, "edit-menu", 2);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(
      lastCall(h.windowCalls, "menu.execute").slice(1),
      [event.sender, "edit-menu", 2, /*senderIsLocal*/ false],
      "a positive revoked by a foreign holder must stay revoked through an unknown probe",
    );
    await h.handlers.get("app-menu:items")(event, "file-menu");
    assert.deepEqual(
      lastCall(h.windowCalls, "menu.items").slice(1),
      [event.sender, "file-menu", /*senderIsLocal*/ false],
      "an unknown probe must not bring back enabled rows a foreign holder revoked",
    );
  } finally {
    Date.now = realDateNow;
  }
});

// The expiry must not become a per-call re-probe: gate 3 shells out to
// netstat/PowerShell, and re-running it on every menu click is the latency
// regression the cache was introduced to fix (Spock FUNC-01). Inside the
// window a repeat call must still be answered from the cache.
test("a positive verdict inside its window is answered without re-probing", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const event = localSenderEvent();

  const realDateNow = Date.now;
  const t0 = realDateNow.call(Date);
  Date.now = () => t0;
  try {
    await h.handlers.get("app-menu:items")(event, "file-menu");
    Date.now = () => t0 + 4_999;
    await h.handlers.get("app-menu:items")(event, "edit-menu");
    await h.handlers.get("app-menu:items")(event, "view-menu");
    assert.equal(
      probeCount(h),
      1,
      "repeat calls inside the window must reuse the verdict — a probe per click "
        + "reintroduces the netstat/PowerShell latency the cache exists to avoid",
    );
  } finally {
    Date.now = realDateNow;
  }
});

// The expiry is an ADDITIONAL bound, not a replacement for the navigation
// invalidation. A cross-origin navigation must still clear immediately rather
// than leaving a valid-by-age positive answering for the new document.
test("a navigation still invalidates a positive verdict inside its window", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const navListeners = [];
  const senderFrame = { url: "http://localhost:5476/chat" };
  const sender = {
    id: "navigating-sender",
    mainFrame: senderFrame,
    on(eventName, handler) { navListeners.push([eventName, handler]); },
  };
  const event = { sender, senderFrame };

  const realDateNow = Date.now;
  const t0 = realDateNow.call(Date);
  Date.now = () => t0;
  try {
    await h.handlers.get("app-menu:items")(event, "file-menu");
    assert.equal(probeCount(h), 1);

    // Still well inside the window, so age alone would answer `true`.
    const start = navListeners.find(([name]) => name === "did-start-navigation");
    assert.ok(start, "did-start-navigation must be wired");
    start[1]({}, "https://remote.example/", /*isInPlace*/ false, /*isMainFrame*/ true);

    Date.now = () => t0 + 1_000;
    await h.handlers.get("app-menu:items")(event, "file-menu");
    assert.equal(
      probeCount(h),
      2,
      "navigation must clear the verdict regardless of its age — the expiry is a "
        + "backstop for the mutable port fact, not a substitute for origin checks",
    );
  } finally {
    Date.now = realDateNow;
  }
});

// resolveSenderIsLocal caches a per-WebContents verdict so the netstat /
// PowerShell port-owner probe inside assertLocalDashboard only fires once per
// sender lifetime. The ORDERING inside this function is load-bearing and
// silently regresses if reshuffled — `assertLocalDashboard` is asynchronous,
// so a navigation that lands DURING the probe would fire `did-navigate` with
// no listener attached (if the listener wiring came after the probe), and the
// stale pre-navigation verdict would then be written to the cache. This pin
// asserts the three points that must all be true for the fix to survive
// refactors:
//
//   1. `did-navigate` is wired BEFORE the `await assertLocalDashboard(...)`
//      call — the listener registration appears earlier in the function body
//      than the awaited probe.
//   2. A per-WC generation counter is captured before the probe and consulted
//      after — the write to `senderIsLocalCache` is gated by
//      `senderIsLocalGeneration.get(wc) === genAtStart`.
//   3. The `did-navigate` listener increments the counter as well as clearing
//      the cache, so a probe that overlapped a navigation lands the guard.
test("resolveSenderIsLocal wires did-navigate BEFORE probing and guards the cache write with a generation counter", () => {
  const match = SOURCE.match(
    /async function resolveSenderIsLocal\(event, channel\) \{([\s\S]*?)\n {4}\}/,
  );
  assert.ok(match, "resolveSenderIsLocal must exist as an async function");
  const body = match[1];

  // Anchor at the invalidation-function definition so this pin stays stable
  // regardless of how many `wc.on(...)` listeners are wired (currently two:
  // `did-start-navigation` and `did-navigate`).
  const invalidateDefIndex = body.indexOf("const invalidate = ()");
  const wireIndex = body.indexOf('wc.on("did-navigate"');
  const startWireIndex = body.indexOf('wc.on("did-start-navigation"');
  const probeIndex = body.indexOf("await assertLocalDashboard");
  const genAtStartIndex = body.indexOf("genAtStart");
  const cacheWriteIndex = body.indexOf("senderIsLocalCache.set(wc,");
  assert.ok(invalidateDefIndex !== -1, "shared `invalidate` function missing");
  assert.ok(wireIndex !== -1, "did-navigate wiring missing");
  assert.ok(startWireIndex !== -1, "did-start-navigation wiring missing");
  assert.ok(probeIndex !== -1, "assertLocalDashboard probe missing");
  assert.ok(genAtStartIndex !== -1, "genAtStart capture missing");
  assert.ok(cacheWriteIndex !== -1, "cache write missing");

  assert.ok(
    wireIndex < probeIndex && startWireIndex < probeIndex,
    "BOTH did-start-navigation AND did-navigate must be wired BEFORE the probe "
      + "(order regressed — a navigation during the probe would fire the "
      + "invalidation with no listener attached, and the stale pre-navigation "
      + "verdict would inherit the cache)",
  );

  // The cache write must sit inside a generation guard AND gate on `verdict === true`.
  // Two conditions, ANDed: only-true caching (see the header comment about SPA
  // route changes NOT firing `did-navigate` — a cached `false` would strand the
  // local user's menu greyed until app restart) + generation-counter equality.
  // The current source expresses both via the `applyGenerationGuard` helper
  // (which folds `verdict === true` and the generation equality into a single
  // boolean); the alternate form is an inline `verdict === true && …` guard.
  const guardPattern = /(?:if\s*\(\s*(?:guarded|applyGenerationGuard\([\s\S]{0,80}\))|verdict === true[\s\S]{0,80}(?:generationUnchanged|senderIsLocalGeneration\.get\(wc\)[\s\S]{0,40}=== genAtStart)\)?)[\s\S]{0,120}senderIsLocalCache\.set/;
  assert.match(
    body,
    guardPattern,
    "cache write must (1) require verdict === true — SPA route changes fire "
      + "did-navigate-in-page not did-navigate, so a cached false would persist "
      + "for the WebContents lifetime — AND (2) sit inside a generation-counter "
      + "equality check — otherwise a probe that overlaps a navigation writes stale state",
  );

  // The RETURN path must ALSO gate on the generation counter, not only the
  // cache write. If the probe finishes with `verdict = true` AFTER a cross-
  // origin navigation, returning `true` unlocks LOCAL_ONLY leaves for the
  // now-remote document even though the cache write was correctly skipped.
  // Match a return that reads either the folded `guarded` boolean or an
  // inline `verdict === true && …` expression.
  const returnPattern = /return\s+(?:guarded|verdict === true\s*&&\s*(?:generationUnchanged|\(?senderIsLocalGeneration\.get\(wc\)[\s\S]{0,40}=== genAtStart\)?))\s*[;)]/;
  assert.match(
    body,
    returnPattern,
    "return must ALSO gate on generation-counter equality (not just the cache "
      + "write) — otherwise a probe that finished as `true` after a cross-origin "
      + "navigation dispatches LOCAL_ONLY leaves (paste, devtools-toggle) against "
      + "the wrong origin even though its cache write was correctly skipped",
  );

  // Joining callers on the single-flight coalescer MUST apply the same
  // generation guard the creator does. Without it, a navigation completing
  // between the probe START (creator's genAtStart) and a joiner receiving
  // the resolved verdict hands the joiner a raw `true` and unlocks
  // LOCAL_ONLY leaves for the now-remote document. Two shape assertions:
  //
  //   1. The in-flight entry carries genAtStart alongside the promise, not
  //      the raw promise alone — otherwise there is nothing for the joiner
  //      to compare against.
  //   2. The joiner path applies `applyGenerationGuard(...)` (or an inline
  //      equivalent) rather than returning the raw promise value.
  assert.match(
    body,
    /senderIsLocalInFlight\.set\(wc,\s*\{[\s\S]{0,80}promise[\s\S]{0,40}genAtStart/,
    "in-flight entry must carry both `promise` and `genAtStart` — a bare "
      + "promise leaves joiners with no generation to compare against and they "
      + "return the raw verdict, unlocking LOCAL_ONLY leaves for a now-remote origin",
  );
  const joinerBlock = body.match(/if\s*\(\s*inFlight\s*\)\s*\{([\s\S]*?)\n\s{6}\}/);
  assert.ok(joinerBlock, "coalesced-joiner block `if (inFlight) { … }` must exist");
  assert.match(
    joinerBlock[1],
    /applyGenerationGuard\([\s\S]*?inFlight\.genAtStart\s*\)|verdict === true\s*&&\s*[\s\S]{0,80}=== inFlight\.genAtStart/,
    "joiner MUST apply the generation guard against inFlight.genAtStart — "
      + "otherwise `return inFlight.promise` hands out the raw probe verdict "
      + "and a joiner arriving after a cross-origin navigation dispatches "
      + "LOCAL_ONLY leaves against the wrong origin",
  );

  // The single-flight coalescer must be in place: concurrent callers on the
  // SAME WebContents must not each launch their own netstat/PowerShell probe.
  // Three anchor points must all be present, otherwise a manual-SSH-tunnel
  // remote page can loop `app-menu:*` and spawn one OS probe per call:
  //
  //   1. An in-flight-per-WC map (WeakMap keyed on wc).
  //   2. A short-circuit BEFORE the probe: `if (inFlight) return inFlight;`
  //      so concurrent callers await the same promise.
  //   3. A `finally` (or equivalent) that DELETES the in-flight entry when
  //      the probe resolves — leaving it in place would freeze subsequent
  //      probes on a stale resolved promise.
  assert.match(
    body,
    /senderIsLocalInFlight\.get\(wc\)/,
    "single-flight coalescer must be consulted BEFORE spawning a fresh probe",
  );
  assert.match(
    body,
    /senderIsLocalInFlight\.set\(wc,/,
    "single-flight coalescer must publish the in-flight promise",
  );
  assert.match(
    body,
    /finally[\s\S]{0,80}senderIsLocalInFlight\.delete\(wc\)/,
    "single-flight coalescer must delete the in-flight entry in a `finally` — "
      + "otherwise a stale resolved promise persists and freezes subsequent probes",
  );

  // The invalidation function must bump the counter, not just delete the
  // cache entry — otherwise the guard above never fires.
  const invalidateBody = body.slice(invalidateDefIndex);
  assert.match(
    invalidateBody,
    /senderIsLocalGeneration\.set\([\s\S]*?\+ 1[\s\S]*?\);/,
    "invalidation function must increment senderIsLocalGeneration — both "
      + "listeners share it, and dropping the counter bump breaks every "
      + "generation-equality guard downstream",
  );

  // A `did-navigate`-only listener leaves the cache holding a stale `true`
  // between the moment a top-frame cross-origin navigation is requested and
  // the moment Chromium commits it. Assert a matching `did-start-navigation`
  // listener exists and gates on `isMainFrame` + `!isInPlace` — a sub-frame
  // navigation doesn't move gate 1's origin, and an `isInPlace` pushState
  // doesn't change the frame's origin either. Missing the start-side
  // invalidation is the class of the GPT 5.6 Review F1 finding on 06b1d9c4a.
  assert.match(
    body,
    /wc\.on\("did-start-navigation"/,
    "did-start-navigation listener must be wired — otherwise the cache holds "
      + "a stale `true` between navigation request and commit, and a preload "
      + "IPC in that window could unlock LOCAL_ONLY leaves against the new origin",
  );
  assert.match(
    body,
    /did-start-navigation[\s\S]{0,200}isMainFrame[\s\S]{0,60}isInPlace|did-start-navigation[\s\S]{0,200}isInPlace[\s\S]{0,60}isMainFrame/,
    "did-start-navigation handler must gate on isMainFrame AND !isInPlace — a "
      + "sub-frame navigation doesn't change gate 1's top-frame origin, and an "
      + "in-place pushState doesn't move the origin either",
  );
});
