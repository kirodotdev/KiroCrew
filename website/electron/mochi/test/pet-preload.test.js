/**
 * pet-preload — the pet/panel/avatar windows' IPC bridge (`window.mochi`).
 *
 * These pin the named channels that replaced the broken generic
 * `contextMenuAction` relay. Each pet-menu action must map to its OWN channel:
 * a missing one is then a visible `undefined`, not a silent dead menu item.
 * The preload is also the only surface these windows can reach, so the channel
 * list must stay explicitly enumerated — no generic pass-through.
 */

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const Module = require("module");

/** Load the preload with Electron stubbed, capturing the exposed API + IPC. */
function loadPreload() {
  const exposed = {};
  const sent = [];
  const invoked = [];
  const listeners = {};
  const electron = {
    contextBridge: {
      exposeInMainWorld: (key, api) => {
        exposed[key] = { ...(exposed[key] || {}), ...api };
      },
    },
    ipcRenderer: {
      send: (channel, ...args) => sent.push({ channel, args }),
      invoke: (channel, ...args) => { invoked.push({ channel, args }); return Promise.resolve(); },
      on: (channel, fn) => { (listeners[channel] = listeners[channel] || []).push(fn); },
      removeListener: (channel, fn) => {
        listeners[channel] = (listeners[channel] || []).filter((f) => f !== fn);
      },
    },
  };

  const modPath = path.join(__dirname, "..", "pet-preload.js");
  delete require.cache[require.resolve(modPath)];
  const origLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === "electron") return electron;
    return origLoad(request, parent, isMain);
  };
  try {
    require(modPath);
  } finally {
    Module._load = origLoad;
  }
  return { api: exposed.mochi, sent, invoked, listeners };
}

test("exposes window.mochi and window.kirocrew", () => {
  const { api } = loadPreload();
  assert.ok(api, "mochi must be exposed");
});

test("each pet-menu action sends its OWN named channel", () => {
  const { api, sent } = loadPreload();
  const cases = [
    ["openChat", "mochi-pet:open-chat"],
    ["openAvatars", "mochi-avatar:open"],
    ["openMemories", "mochi-pet:open-memories"],
    ["openSettings", "mochi-pet:open-settings"],
    ["openDashboard", "mochi-panel:open-dashboard"],
  ];
  for (const [method, channel] of cases) {
    assert.strictEqual(typeof api[method], "function", `missing method: ${method}`);
    sent.length = 0;
    api[method]();
    assert.deepStrictEqual(
      sent.map((s) => s.channel),
      [channel],
      `${method} must send exactly ${channel}`,
    );
  }
});

test("setPanelWidth sends the width channel WITH the width argument", () => {
  const { api, sent } = loadPreload();
  assert.strictEqual(typeof api.setPanelWidth, "function");
  api.setPanelWidth(480);
  assert.deepStrictEqual(sent, [{ channel: "mochi-panel:set-width", args: [480] }]);
});

test("does NOT expose a generic contextMenuAction relay", () => {
  // The retired relay is the whole bug: a string-keyed renderer→main call with
  // no handler. Its absence is the fix.
  const { api } = loadPreload();
  assert.strictEqual(api.contextMenuAction, undefined);
});

test("onOpenMemories subscribes to the panel view channel and unsubscribes", () => {
  const { api, listeners } = loadPreload();
  let fired = 0;
  const off = api.onOpenMemories(() => { fired += 1; });
  assert.strictEqual((listeners["mochi-panel:show-memories"] || []).length, 1);

  // Main → renderer: invoking the registered handler drives the callback.
  listeners["mochi-panel:show-memories"][0]();
  assert.strictEqual(fired, 1);

  off();
  assert.strictEqual((listeners["mochi-panel:show-memories"] || []).length, 0, "must remove its listener");
});

test("exposes no settings-view subscription (settings is its own window)", () => {
  const { api } = loadPreload();
  // The in-panel settings overlay is gone; the pet opens the settings WINDOW via
  // mochi-pet:open-settings, which pet/settingsWindow.js owns.
  assert.strictEqual(api.onOpenSettings, undefined);
  assert.strictEqual(typeof api.openSettings, "function");
  assert.strictEqual(typeof api.closeSettings, "function");
});

test("exposes the shared menu's panel-local senders and subscriptions", () => {
  const { api, listeners } = loadPreload();
  // The pet renders the same menu as the panel; these two rows are implemented
  // by the panel, so the pet forwards them and the panel listens.
  assert.strictEqual(typeof api.clearScreenInPanel, "function");
  assert.strictEqual(typeof api.deleteHistoryInPanel, "function");

  let cleared = 0;
  const off = api.onClearScreen(() => { cleared += 1; });
  assert.strictEqual((listeners["mochi-panel:clear-screen"] || []).length, 1);
  listeners["mochi-panel:clear-screen"][0]();
  assert.strictEqual(cleared, 1);
  off();
  assert.strictEqual((listeners["mochi-panel:clear-screen"] || []).length, 0);

  let deleted = 0;
  const offDel = api.onDeleteHistory(() => { deleted += 1; });
  listeners["mochi-panel:delete-history"][0]();
  assert.strictEqual(deleted, 1);
  offDel();
  assert.strictEqual((listeners["mochi-panel:delete-history"] || []).length, 0);
});

test("menu hitbox + open/close reach their own channels", () => {
  const { api, sent } = loadPreload();
  assert.strictEqual(typeof api.setMenuHitbox, "function");
  api.setMenuHitbox({ x: 1, y: 2, w: 3, h: 4 });
  assert.deepStrictEqual(sent.at(-1), {
    channel: "mochi-pet:menu-hitbox",
    args: [{ x: 1, y: 2, w: 3, h: 4 }],
  });
  api.setMenuHitbox(null);
  assert.deepStrictEqual(sent.at(-1), { channel: "mochi-pet:menu-hitbox", args: [null] });
  api.menuOpened();
  assert.strictEqual(sent.at(-1).channel, "mochi-pet:menu-open");
  api.menuClosed();
  assert.strictEqual(sent.at(-1).channel, "mochi-pet:menu-close");
});

test("closeChat hides the panel; file/link actions send their argument", () => {
  const { api, sent } = loadPreload();
  api.closeChat();
  assert.strictEqual(sent.at(-1).channel, "mochi-panel:close");
  api.revealFile("/tmp/x.txt");
  assert.deepStrictEqual(sent.at(-1), {
    channel: "mochi-panel:reveal-file",
    args: ["/tmp/x.txt"],
  });
  api.openExternal("https://example.com");
  assert.deepStrictEqual(sent.at(-1), {
    channel: "mochi-panel:open-external",
    args: ["https://example.com"],
  });
});

// The permanent exclusion list, enforced. Re-adding any of these would either
// hand page content a credential or restore a generic channel relay; see the
// NEVER_EXPOSE block in pet-preload.js for why each one is on the list.
test("never exposes credential getters, tunnel controls, or a generic relay", () => {
  const { api } = loadPreload();
  for (const name of [
    "getGatewaySecret",
    "getGatewayAuth",
    "startGateway",
    "tunnelConnect",
    "tunnelDisconnect",
    "onBackendResolved",
    "onBackendSwitching",
    "onBackendSwitchError",
    "send",
    "invoke",
  ]) {
    assert.strictEqual(api[name], undefined, `pet-preload must not expose ${name}`);
  }
});

// ── Settings native-close guard (preload side) ──────────────────────────────
//
// The shell intercepts the Settings BrowserWindow `close` and asks the
// renderer; this seam is the renderer's half. The acknowledgement contract is
// exact: sent AFTER the subscriber returns, never when it throws — the shell's
// bounded fallback force-closes a renderer that fails to ack, which is the
// only thing standing between a wedged renderer and an unclosable window.

test("onSettingsCloseRequested subscribes, acks after the callback, unsubscribes", () => {
  const { api, sent, listeners } = loadPreload();
  assert.strictEqual(typeof api.onSettingsCloseRequested, "function");

  let fired = 0;
  const off = api.onSettingsCloseRequested(() => { fired += 1; });
  assert.strictEqual((listeners["mochi-settings:close-request"] || []).length, 1);

  sent.length = 0;
  listeners["mochi-settings:close-request"][0]();
  assert.strictEqual(fired, 1);
  assert.deepStrictEqual(
    sent.map((s) => s.channel),
    ["mochi-settings:close-request-ack"],
    "the ack must go back after the subscriber runs",
  );

  off();
  assert.strictEqual(
    (listeners["mochi-settings:close-request"] || []).length,
    0,
    "must remove its listener",
  );
});

test("a throwing close subscriber never acks, so the shell fallback can fire", () => {
  const { api, sent, listeners } = loadPreload();
  api.onSettingsCloseRequested(() => {
    throw new Error("renderer wedged");
  });

  sent.length = 0;
  assert.throws(() => listeners["mochi-settings:close-request"][0]());
  assert.deepStrictEqual(sent, [], "no ack may be sent when the subscriber throws");
});
