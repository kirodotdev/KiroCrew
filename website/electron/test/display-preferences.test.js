const { test } = require("node:test");
const assert = require("node:assert");
const { TIERS, DEFAULT_TIER, resolveTier, getFontSize, setFontSize } = require("../display-preferences");

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

test("TIERS: exposes Chrome's 5 named font-size tiers in ascending px order", () => {
  assert.strictEqual(TIERS.length, 5);
  const pxValues = TIERS.map((tier) => tier.px);
  assert.deepStrictEqual(pxValues, [9, 12, 16, 20, 24]);
  for (const tier of TIERS) {
    assert.ok(typeof tier.name === "string" && tier.name.length > 0, "tier has name");
    assert.ok(typeof tier.label === "string" && tier.label.length > 0, "tier has label");
    assert.ok(typeof tier.px === "number" && Number.isFinite(tier.px), "tier has numeric px");
  }
});

test("DEFAULT_TIER: is Medium (16px) — matches Chromium's own default", () => {
  assert.strictEqual(DEFAULT_TIER.name, "medium");
  assert.strictEqual(DEFAULT_TIER.px, 16);
});

// ── resolveTier ──

test("resolveTier: exact px returns the matching tier", () => {
  assert.strictEqual(resolveTier(9).name, "verySmall");
  assert.strictEqual(resolveTier(12).name, "small");
  assert.strictEqual(resolveTier(16).name, "medium");
  assert.strictEqual(resolveTier(20).name, "large");
  assert.strictEqual(resolveTier(24).name, "veryLarge");
});

test("resolveTier: unknown or non-finite input falls back to DEFAULT_TIER", () => {
  assert.strictEqual(resolveTier(999), DEFAULT_TIER);
  assert.strictEqual(resolveTier(NaN), DEFAULT_TIER);
  assert.strictEqual(resolveTier(undefined), DEFAULT_TIER);
  assert.strictEqual(resolveTier(null), DEFAULT_TIER);
  assert.strictEqual(resolveTier("16"), DEFAULT_TIER); // strings are not px
});

// ── getFontSize / setFontSize (store-injected) ──

test("getFontSize: returns Medium (16) when no value is persisted", () => {
  assert.strictEqual(getFontSize(makeStore()), 16);
});

test("getFontSize: returns the persisted value when it is a valid tier px", () => {
  assert.strictEqual(getFontSize(makeStore({ displayPreferences: { fontSize: 20 } })), 20);
  assert.strictEqual(getFontSize(makeStore({ displayPreferences: { fontSize: 9 } })), 9);
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
  let attached = alreadyAttached;
  const wc = {
    id,
    isDestroyed: () => destroyed,
    debugger: {
      isAttached: () => attached,
      attach: (version) => {
        if (attached) throw new Error("Debugger is already attached to the target");
        attachCalls.push(version);
        attached = true;
      },
      detach: () => { attached = false; },
      sendCommand: async (method, params) => {
        if (sendCommandFails) throw sendCommandFails;
        commands.push([method, params]);
      },
    },
    _commands: commands,
    _attachCalls: attachCalls,
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
