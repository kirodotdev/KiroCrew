const { test } = require("node:test");
const assert = require("node:assert");
const { TIERS, DEFAULT_TIER, getFontSize, setFontSize } = require("../display-preferences");

// Minimal electron-store stand-in. Real electron-store supports dot notation
// on get/set — walk the path so the tests exercise the same contract the
// module uses in production.
function makeStore(initial = {}) {
  const data = JSON.parse(JSON.stringify(initial));
  return {
    get: (key) => key.split(".").reduce((obj, seg) => (obj == null ? undefined : obj[seg]), data),
    set: (key, value) => {
      const segments = key.split(".");
      let cursor = data;
      for (let i = 0; i < segments.length - 1; i++) {
        const seg = segments[i];
        if (cursor[seg] == null || typeof cursor[seg] !== "object") cursor[seg] = {};
        cursor = cursor[seg];
      }
      cursor[segments[segments.length - 1]] = value;
    },
    _data: data,
  };
}

// ── TIERS shape ──

test("TIERS: exposes 4 named font-size tiers in ascending px order (Chrome's ladder minus the sub-10px rung)", () => {
  // Four tiers, not five: Chrome ships a fifth "Very Small" (9px) at the
  // bottom, but Kiro Crew's chrome is px-pinned via ~4k text-[NNpx]
  // declarations that don't reflow cleanly below 10px baseline (see
  // website/AGENTS.md). The 9px `verySmall` tier was dropped in the Fable
  // First-Principles response — pin the four remaining, and the exact
  // px values so a mistaken migration to a different scheme is caught.
  assert.strictEqual(TIERS.length, 4);
  const pxValues = TIERS.map((tier) => tier.px);
  assert.deepStrictEqual(pxValues, [12, 16, 20, 24]);
  for (const tier of TIERS) {
    assert.ok(typeof tier.name === "string" && tier.name.length > 0, "tier has name");
    assert.ok(typeof tier.label === "string" && tier.label.length > 0, "tier has label");
    assert.ok(typeof tier.px === "number" && Number.isFinite(tier.px), "tier has numeric px");
    assert.ok(tier.px >= 10, `tier ${tier.name}=${tier.px}px must be >= 10 (website/AGENTS.md floor)`);
  }
});

test("DEFAULT_TIER: is Medium (16px) — matches Chromium's own default", () => {
  assert.strictEqual(DEFAULT_TIER.name, "medium");
  assert.strictEqual(DEFAULT_TIER.px, 16);
});

// ── resolveTier removal (exercised via public API) ──

test("changeFontSize + getFontSize: an exact tier px persists and re-reads as the same tier", () => {
  // Before Fable's subtraction, direct resolveTier tests verified the px→tier
  // mapping. resolveTier is now un-exported (0 non-test consumers per Fable)
  // so its behaviour is exercised through changeFontSize's happy path — the
  // ONLY entry point production code uses to reach it.
  const store = makeStore();
  setFontSize(store, 20);
  assert.strictEqual(getFontSize(store), 20);
});

test("changeFontSize: rejects a px that no tier owns, keeping the previous tier's px", () => {
  // Formerly `resolveTier: unknown or non-finite input falls back to
  // DEFAULT_TIER`. Now exercised through setFontSize's validation.
  const store = makeStore();
  setFontSize(store, 16);
  setFontSize(store, 999);
  assert.strictEqual(getFontSize(store), 16, "invalid px is rejected, previous stays");
});

test("changeFontSize: refuses non-numeric input (strings that happen to parse are rejected)", () => {
  // Formerly `resolveTier("16") returns DEFAULT_TIER`. The IPC validator
  // (isValidPx) is the frontline defender.
  const store = makeStore();
  setFontSize(store, 20);
  setFontSize(store, "16");
  assert.strictEqual(getFontSize(store), 20, "string input is rejected, previous stays");
});

// ── getFontSize / setFontSize (store-injected) ──

test("getFontSize: returns Medium (16) when no value is persisted", () => {
  assert.strictEqual(getFontSize(makeStore()), 16);
});

test("getFontSize: returns the persisted value when it is a valid tier px", () => {
  assert.strictEqual(getFontSize(makeStore({ displayPreferences: { fontSize: 20 } })), 20);
  assert.strictEqual(getFontSize(makeStore({ displayPreferences: { fontSize: 12 } })), 12);
});

test("getFontSize: falls back to 16 when the persisted value is out of range", () => {
  assert.strictEqual(getFontSize(makeStore({ displayPreferences: { fontSize: 999 } })), 16);
});

test("getFontSize: falls back to 16 when the persisted value is not a number", () => {
  assert.strictEqual(getFontSize(makeStore({ displayPreferences: { fontSize: "20" } })), 16);
  assert.strictEqual(getFontSize(makeStore({ displayPreferences: { fontSize: null } })), 16);
});

test("setFontSize: writes a valid px to the store and returns true", () => {
  const store = makeStore();
  const ok = setFontSize(store, 20);
  assert.strictEqual(ok, true);
  assert.strictEqual(store.get("displayPreferences.fontSize"), 20);
});

test("setFontSize: rejects an invalid px, does not write, returns false", () => {
  const store = makeStore({ displayPreferences: { fontSize: 12 } });
  const ok = setFontSize(store, 999);
  assert.strictEqual(ok, false);
  assert.strictEqual(store.get("displayPreferences.fontSize"), 12);
});

// ── module-level currentFontSize cache ──
//
// The cache lives at module scope, so tests that assert its behaviour must
// either seed it fresh each time or account for what a previous test left
// behind. Rather than clear require.cache (fragile with node:test), each
// test seeds the cache explicitly via initCurrentFontSize(store).

test("initCurrentFontSize: hydrates the cache from the store and returns the seeded px", () => {
  const { initCurrentFontSize, getCurrentFontSize } = require("../display-preferences");
  const seeded = initCurrentFontSize(makeStore({ displayPreferences: { fontSize: 20 } }));
  assert.strictEqual(seeded, 20);
  assert.strictEqual(getCurrentFontSize(), 20);
});

test("getCurrentFontSize: hydrating from an empty store yields DEFAULT_TIER.px", () => {
  // The module-level `let currentFontSize = DEFAULT_TIER.px;` initialiser is
  // the strictly-pre-init fallback; asserting THAT directly requires clearing
  // require.cache, which is fragile with node:test. Instead we exercise the
  // observable equivalent: hydrating from a fresh (empty) store — the same
  // shape a cold main-process boot sees before any user has ever written a
  // preference — must resolve to DEFAULT_TIER.px.
  const { initCurrentFontSize, getCurrentFontSize, DEFAULT_TIER: DEFAULT } = require("../display-preferences");
  initCurrentFontSize(makeStore());
  assert.strictEqual(getCurrentFontSize(), DEFAULT.px);
  assert.strictEqual(getCurrentFontSize(), 16);
});

test("setFontSize: a successful write also updates the cache", () => {
  const { initCurrentFontSize, getCurrentFontSize, setFontSize: set } = require("../display-preferences");
  const store = makeStore();
  initCurrentFontSize(store); // seed to Medium
  set(store, 12);
  assert.strictEqual(getCurrentFontSize(), 12);
});

test("setFontSize: a rejected write does NOT update the cache", () => {
  const { initCurrentFontSize, getCurrentFontSize, setFontSize: set } = require("../display-preferences");
  const store = makeStore({ displayPreferences: { fontSize: 16 } });
  initCurrentFontSize(store);
  set(store, 999); // refused
  assert.strictEqual(getCurrentFontSize(), 16);
});

// ── applyFontSizeToAllWebContents (CDP Page.setFontSizes path) ──
//
// Live font-size apply runs through the Chrome DevTools Protocol via
// `webContents.debugger.sendCommand("Page.setFontSizes", ...)`, NOT through
// a hypothetical `webContents.setDefaultFontSize` — that method does not
// exist on Electron's webContents. Tests here mock the `debugger` sub-
// object the way `browser-control.test.js` does, so the mock shape stays
// faithful to the real Electron API surface.
//
// Docs: https://www.electronjs.org/docs/api/debugger
// Docs: https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-setFontSizes

function makeWc(id, { destroyed = false, alreadyAttached = false, sendCommandFails = null, shellOwned = true } = {}) {
  const commands = [];
  const attachCalls = [];
  const detachCalls = [];
  const focusListeners = [];
  let attached = alreadyAttached;
  let sendCommandFailsLocal = sendCommandFails;
  const wc = {
    id,
    isDestroyed: () => destroyed,
    on: (event, cb) => {
      if (event === "focus") focusListeners.push(cb);
    },
    _fireFocus: () => {
      for (const cb of focusListeners.slice()) cb();
    },
    _setSendCommandFails: (err) => { sendCommandFailsLocal = err; },
    _setAttachSucceeds: () => {
      // Toggle attach to succeed on next call (used by tests that simulate
      // DevTools closing between the initial fail and the focus retry).
      // Nothing state-changing here — see `attach` below for the trigger.
    },
    debugger: {
      isAttached: () => attached,
      attach: (version) => {
        if (attached) throw new Error("Debugger is already attached to the target");
        attachCalls.push(version);
        attached = true;
      },
      detach: () => {
        detachCalls.push(true);
        attached = false;
      },
      sendCommand: async (method, params) => {
        if (sendCommandFailsLocal) throw sendCommandFailsLocal;
        commands.push([method, params]);
      },
    },
    _commands: commands,
    _attachCalls: attachCalls,
    _detachCalls: detachCalls,
    _focusListeners: focusListeners,
  };
  // By default every wc a test creates is shell-owned — the filter added
  // in PR #10247 (Design Review response) scopes the ripple to shell-owned
  // WebContents, and the existing tests were written before that filter
  // existed. Set `shellOwned: false` to exercise the exclusion path.
  if (shellOwned) {
    const { markShellOwned } = require("../display-preferences");
    markShellOwned(wc);
  }
  return wc;
}

test("applyFontSizeToAllWebContents: attaches CDP with protocol 1.3 and sends Page.setFontSizes on every live webContents", async () => {
  const { applyFontSizeToAllWebContents } = require("../display-preferences");
  const wc1 = makeWc(1);
  const wc2 = makeWc(2);
  const wc3 = makeWc(3);
  await applyFontSizeToAllWebContents({ getAllWebContents: () => [wc1, wc2, wc3] }, 20);

  for (const wc of [wc1, wc2, wc3]) {
    assert.deepStrictEqual(wc._attachCalls, ["1.3"], `wc${wc.id} attached with protocol 1.3`);
    assert.deepStrictEqual(
      wc._commands,
      [["Page.setFontSizes", { fontSizes: { standard: 20, fixed: 20 } }]],
      `wc${wc.id} sent Page.setFontSizes`,
    );
  }
});

test("applyFontSizeToAllWebContents: skips destroyed webContents entirely (no attach, no sendCommand)", async () => {
  const { applyFontSizeToAllWebContents } = require("../display-preferences");
  const alive = makeWc(1);
  const dead = makeWc(2, { destroyed: true });
  await applyFontSizeToAllWebContents({ getAllWebContents: () => [alive, dead] }, 12);

  assert.deepStrictEqual(alive._commands, [["Page.setFontSizes", { fontSizes: { standard: 12, fixed: 12 } }]]);
  assert.deepStrictEqual(dead._commands, [], "destroyed wc must not receive commands");
  assert.deepStrictEqual(dead._attachCalls, [], "destroyed wc must not be attached");
});

test("applyFontSizeToAllWebContents: an already-attached webContents is NOT re-attached but still gets the command", async () => {
  // browser-control.js may hold the debugger on the browser panel; a re-attach
  // would throw. We check isAttached() first and just re-use the existing
  // session, so the ripple still delivers.
  const { applyFontSizeToAllWebContents } = require("../display-preferences");
  const wc = makeWc(1, { alreadyAttached: true });
  await applyFontSizeToAllWebContents({ getAllWebContents: () => [wc] }, 24);

  assert.deepStrictEqual(wc._attachCalls, [], "no re-attach when already attached");
  assert.deepStrictEqual(wc._commands, [["Page.setFontSizes", { fontSizes: { standard: 24, fixed: 24 } }]]);
  // Critical: we do NOT detach a session we didn't attach — that session
  // belongs to someone else (browser-control.js, DevTools) and stealing
  // it would break them.
  assert.deepStrictEqual(wc._detachCalls, [], "must not detach a session we didn't attach");
});

test("applyFontSizeToAllWebContents: detaches the debugger after send when WE attached it (Fable Design Review Watch on f2acc8200)", async () => {
  // Fable Design Review Watch on `f2acc8200`: the earlier "attach + send +
  // never detach" shape held the single debugger slot forever after the
  // first tier apply — subsequent View → Toggle Developer Tools couldn't
  // attach. Fix: track a local `weAttached` flag, and if we attached, we
  // detach in the finally block after send completes (regardless of
  // whether sendCommand succeeded or failed).
  const { applyFontSizeToAllWebContents } = require("../display-preferences");
  const wc = makeWc(1);
  await applyFontSizeToAllWebContents({ getAllWebContents: () => [wc] }, 20);

  assert.deepStrictEqual(wc._attachCalls, ["1.3"], "we attached (fresh session)");
  assert.deepStrictEqual(
    wc._detachCalls, [true],
    "we detach AFTER send — the debugger slot is released for View → Toggle Developer Tools",
  );
});

test("applyFontSizeToAllWebContents: still detaches when sendCommand fails (finally block, not try body)", async () => {
  // The detach MUST run even when Page.setFontSizes throws — otherwise a
  // one-time send failure permanently orphans the debugger slot. The fix
  // uses finally { detach } for exactly this reason.
  const { applyFontSizeToAllWebContents } = require("../display-preferences");
  const wc = makeWc(1, { sendCommandFails: new Error("Page navigating") });
  await applyFontSizeToAllWebContents({ getAllWebContents: () => [wc] }, 12);

  assert.deepStrictEqual(wc._attachCalls, ["1.3"]);
  assert.deepStrictEqual(wc._detachCalls, [true],
    "detach fires in finally block even when sendCommand throws");
});

test("applyFontSizeToAllWebContents: empty webContents list is a safe no-op", async () => {
  const { applyFontSizeToAllWebContents } = require("../display-preferences");
  await assert.doesNotReject(() =>
    applyFontSizeToAllWebContents({ getAllWebContents: () => [] }, 16),
  );
});

test("applyFontSizeToAllWebContents: an attach failure on one webContents does NOT strand later ones", async () => {
  // Attach can fail (DevTools open, foreign owner). The remaining live
  // webContents must still receive the update — same TOCTOU discipline as
  // the sendCommand-throw case.
  const { applyFontSizeToAllWebContents, markShellOwned } = require("../display-preferences");
  const wc1 = makeWc(1);
  const wc2 = {
    id: 2,
    isDestroyed: () => false,
    debugger: {
      isAttached: () => false,
      attach: () => { throw new Error("Another debugger is already attached to the target"); },
      sendCommand: async () => {},
    },
  };
  // Inline wc — mark it shell-owned so the filter doesn't short-circuit
  // this test's actual assertion (that an attach failure on the middle wc
  // does not strand the ripple).
  markShellOwned(wc2);
  const wc3 = makeWc(3);

  const originalWarn = console.warn;
  console.warn = () => {};
  try {
    await assert.doesNotReject(() =>
      applyFontSizeToAllWebContents({ getAllWebContents: () => [wc1, wc2, wc3] }, 20),
    );
  } finally {
    console.warn = originalWarn;
  }

  assert.deepStrictEqual(wc1._commands, [["Page.setFontSizes", { fontSizes: { standard: 20, fixed: 20 } }]]);
  assert.deepStrictEqual(wc3._commands, [["Page.setFontSizes", { fontSizes: { standard: 20, fixed: 20 } }]]);
});

test("applyFontSizeToAllWebContents: a sendCommand rejection does NOT strand later webContents", async () => {
  // A page navigating away between attach and sendCommand, or a torn-down
  // frame, will reject Page.setFontSizes. Log once and continue.
  const { applyFontSizeToAllWebContents } = require("../display-preferences");
  const wc1 = makeWc(1);
  const wc2 = makeWc(2, { sendCommandFails: new Error("Frame is not available") });
  const wc3 = makeWc(3);

  const originalWarn = console.warn;
  console.warn = () => {};
  try {
    await assert.doesNotReject(() =>
      applyFontSizeToAllWebContents({ getAllWebContents: () => [wc1, wc2, wc3] }, 20),
    );
  } finally {
    console.warn = originalWarn;
  }

  assert.deepStrictEqual(wc1._commands, [["Page.setFontSizes", { fontSizes: { standard: 20, fixed: 20 } }]]);
  assert.deepStrictEqual(wc3._commands, [["Page.setFontSizes", { fontSizes: { standard: 20, fixed: 20 } }]]);
});

// ── Focus re-apply on CDP failure (Fable UX Review, `7e3aa89991`) ──
//
// When DevTools is open on the dashboard, the debugger slot is held and
// our `attach` throws. Before this fix the menu radio checked + store
// persisted, but the visible text stayed at the prior tier until restart —
// the menu asserted a state the screen contradicted. Now: the wc's
// pending target is stored on failure and re-applied on the next `focus`
// event, so closing DevTools and clicking the window self-heals.

test("applyFontSizeToOne: on attach failure, re-applies on next focus event once attach unblocks", async () => {
  const { applyFontSizeToAllWebContents } = require("../display-preferences");
  // Custom wc where attach fails until we flip a switch — models DevTools
  // being open, then closed. Focus event fires the pending retry.
  const commands = [];
  const focusListeners = [];
  let attachBlocked = true;
  let attached = false;
  const wc = {
    id: 42,
    isDestroyed: () => false,
    on: (event, cb) => { if (event === "focus") focusListeners.push(cb); },
    debugger: {
      isAttached: () => attached,
      attach: () => {
        if (attachBlocked) throw new Error("Another debugger is already attached to the target");
        attached = true;
      },
      detach: () => { attached = false; },
      sendCommand: async (method, params) => { commands.push([method, params]); },
    },
  };
  const { markShellOwned } = require("../display-preferences");
  markShellOwned(wc);

  const originalWarn = console.warn;
  console.warn = () => {};
  try {
    await applyFontSizeToAllWebContents({ getAllWebContents: () => [wc] }, 20);
    // Initial apply failed silently — nothing landed yet.
    assert.deepStrictEqual(commands, [], "no command lands while attach is blocked");
    assert.strictEqual(focusListeners.length, 1, "focus retry listener attached exactly once");

    // Unblock attach (models DevTools closing) and fire the focus event.
    attachBlocked = false;
    focusListeners[0]();
    await new Promise((resolve) => setImmediate(resolve));

    assert.deepStrictEqual(
      commands,
      [["Page.setFontSizes", { fontSizes: { standard: 20, fixed: 20 } }]],
      "focus retry lands the pending target now that attach succeeds",
    );
  } finally {
    console.warn = originalWarn;
  }
});

test("applyFontSizeToOne: on sendCommand failure, re-applies on next focus event once send unblocks", async () => {
  const { applyFontSizeToAllWebContents, markShellOwned } = require("../display-preferences");
  const commands = [];
  const focusListeners = [];
  let sendFailErr = new Error("Frame is not available");
  let attached = false;
  const wc = {
    id: 43,
    isDestroyed: () => false,
    on: (event, cb) => { if (event === "focus") focusListeners.push(cb); },
    debugger: {
      isAttached: () => attached,
      attach: () => { attached = true; },
      detach: () => { attached = false; },
      sendCommand: async (method, params) => {
        if (sendFailErr) throw sendFailErr;
        commands.push([method, params]);
      },
    },
  };
  markShellOwned(wc);

  const originalWarn = console.warn;
  console.warn = () => {};
  try {
    await applyFontSizeToAllWebContents({ getAllWebContents: () => [wc] }, 24);
    assert.deepStrictEqual(commands, [], "initial send threw, nothing recorded");
    assert.strictEqual(focusListeners.length, 1, "focus retry listener attached exactly once");

    sendFailErr = null;
    focusListeners[0]();
    await new Promise((resolve) => setImmediate(resolve));

    assert.deepStrictEqual(
      commands,
      [["Page.setFontSizes", { fontSizes: { standard: 24, fixed: 24 } }]],
      "focus retry re-sends the pending target after send unblocks",
    );
  } finally {
    console.warn = originalWarn;
  }
});

test("applyFontSizeToOne: a successful apply clears the pending re-apply so later focus events are no-ops", async () => {
  const { applyFontSizeToAllWebContents } = require("../display-preferences");
  const wc = makeWc(44);

  await applyFontSizeToAllWebContents({ getAllWebContents: () => [wc] }, 16);
  assert.deepStrictEqual(
    wc._commands,
    [["Page.setFontSizes", { fontSizes: { standard: 16, fixed: 16 } }]],
    "initial apply succeeded",
  );
  // Successful apply must NOT have attached a focus-retry listener — there
  // is nothing pending to retry, and attaching one anyway would leak a
  // listener per wc per tier-change.
  assert.strictEqual(wc._focusListeners.length, 0, "no focus listener attached on the success path");

  // Confirm: if a focus event WERE fired (via some other path), the wc must
  // not receive a second Page.setFontSizes — nothing pending means nothing
  // re-applied. Simulate by directly invoking any listener (none exists):
  for (const cb of wc._focusListeners) cb();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepStrictEqual(
    wc._commands,
    [["Page.setFontSizes", { fontSizes: { standard: 16, fixed: 16 } }]],
    "no re-fire on focus when nothing is pending",
  );
});

// ── Shell-owned filter (PR #10247 Design Review response) ──
// applyFontSizeToAllWebContents scopes the ripple to WebContents tagged
// via markShellOwned. The embedded browser panel's WebContentsView must
// stay out of this set so `Page.setFontSizes` never lands on
// user-browsed sites.

test("markShellOwned + isShellOwned: adds and reports a wc as shell-owned", () => {
  const { markShellOwned, isShellOwned } = require("../display-preferences");
  const wc = { id: "test-shell-1" };
  assert.strictEqual(isShellOwned(wc), false, "wc is not owned before marking");
  markShellOwned(wc);
  assert.strictEqual(isShellOwned(wc), true, "wc is owned after marking");
});

test("isShellOwned: returns false for unmarked wcs (the browser panel excluded contract)", () => {
  const { isShellOwned } = require("../display-preferences");
  const unmarked = { id: "browser-panel-fake" };
  assert.strictEqual(isShellOwned(unmarked), false);
});

test("applyFontSizeToAllWebContents: skips non-shell-owned WebContents entirely (no attach, no sendCommand)", async () => {
  const { applyFontSizeToAllWebContents } = require("../display-preferences");
  const shellWc = makeWc("shell-A"); // shell-owned by default
  const browserPanelWc = makeWc("browser-panel-B", { shellOwned: false });

  await applyFontSizeToAllWebContents(
    { getAllWebContents: () => [shellWc, browserPanelWc] },
    20,
  );

  // Shell-owned wc got the ripple.
  assert.deepStrictEqual(shellWc._commands, [["Page.setFontSizes", { fontSizes: { standard: 20, fixed: 20 } }]]);
  assert.deepStrictEqual(shellWc._attachCalls, ["1.3"]);

  // Browser panel wc — no attach, no command. The filter short-circuits
  // BEFORE the CDP path runs, matching the "browser panel deliberately
  // excluded from font-size ripple" contract in the PR description.
  assert.deepStrictEqual(browserPanelWc._commands, [], "browser panel wc must not receive Page.setFontSizes");
  assert.deepStrictEqual(browserPanelWc._attachCalls, [], "browser panel wc must not have the debugger attached by this module");
});

test("applyFontSizeToAllWebContents: a mixed list only ripples shell-owned wcs, preserving order among them", async () => {
  const { applyFontSizeToAllWebContents } = require("../display-preferences");
  const shellA = makeWc("shell-A");
  const externalB = makeWc("external-B", { shellOwned: false });
  const shellC = makeWc("shell-C");
  const externalD = makeWc("external-D", { shellOwned: false });
  const shellE = makeWc("shell-E");

  await applyFontSizeToAllWebContents(
    { getAllWebContents: () => [shellA, externalB, shellC, externalD, shellE] },
    24,
  );

  for (const wc of [shellA, shellC, shellE]) {
    assert.deepStrictEqual(wc._commands, [["Page.setFontSizes", { fontSizes: { standard: 24, fixed: 24 } }]], `shell-owned wc ${wc.id} received the ripple`);
  }
  for (const wc of [externalB, externalD]) {
    assert.deepStrictEqual(wc._commands, [], `non-shell-owned wc ${wc.id} was skipped`);
    assert.deepStrictEqual(wc._attachCalls, [], `non-shell-owned wc ${wc.id} was not attached`);
  }
});

test("applyFontSizeToAllWebContents: an all-external wc list is a safe no-op", async () => {
  const { applyFontSizeToAllWebContents } = require("../display-preferences");
  const externals = [
    makeWc("ext-1", { shellOwned: false }),
    makeWc("ext-2", { shellOwned: false }),
    makeWc("ext-3", { shellOwned: false }),
  ];
  await assert.doesNotReject(() =>
    applyFontSizeToAllWebContents({ getAllWebContents: () => externals }, 16),
  );
  for (const wc of externals) {
    assert.deepStrictEqual(wc._commands, []);
    assert.deepStrictEqual(wc._attachCalls, []);
  }
});
