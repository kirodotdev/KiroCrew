const { readFileSync: readSource } = require("./source-with-owners");
"use strict";

// Issue 29949: the desktop shell's native consent for sharing this computer
// from the local dashboard. The share runs on a consent-only desktop host this
// process spawns; no HTTP route grants.
// Authored before the module; not run locally (host rule), CI runs it.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const MODULE_PATH = path.join(__dirname, "..", "connected-computers.js");
const MODULE_SOURCE = readSource(MODULE_PATH, "utf8");
const PRELOAD_SOURCE = readSource(path.join(__dirname, "..", "preload.js"), "utf8");
const REGISTRAR_SOURCE = readSource(path.join(__dirname, "..", "ipc-registrar.js"), "utf8");
const {
  MAX_INSTANCE_ID,
  MAX_MCP_PROMPTS,
  askNative,
  shareFromDashboard,
  validInstanceId,
} = require(MODULE_PATH);

function fakeDialog({ confirm = 0, folder = ["/home/u/proj"], canceled = false, mcpAnswers = [] } = {}) {
  const shown = [];
  const answers = [confirm, ...mcpAnswers];
  return {
    shown,
    // Electron's own signature: an optional parent window, then the options.
    showMessageBox: async (win, opts) => {
      shown.push({ kind: "message", opts: opts || win });
      return { response: answers.shift() ?? 1 };
    },
    showOpenDialog: async (win, opts) => {
      shown.push({ kind: "open", opts: opts || win });
      return { canceled, filePaths: canceled ? [] : folder };
    },
  };
}

const DESCRIBED = {
  ok: true,
  consent: "t".repeat(32),
  instance: { id: "work-crew", name: "Work crew", ssh_host: "workbox" },
  mcp: [{ name: "notes", refused: "" }, { name: "browser", refused: "needs a network" }],
};

function fakeHost({ state = "consent", protection = "Secret Service", replies = {} } = {}) {
  const calls = [];
  const host = {
    calls,
    stopped: 0,
    startAndWait: async () => state,
    protection: () => protection,
    request: async (op, fields) => {
      calls.push({ op, fields });
      return replies[op] ?? { ok: false, code: "unexpected" };
    },
    stop: () => { host.stopped += 1; },
  };
  return host;
}

function deps(dialog, host) {
  const opened = [];
  return {
    opened,
    dialog,
    window: null,
    openHost: () => {
      opened.push(host);
      return host;
    },
  };
}

test("B02: confirm + folder shares exactly that crew, consent, root and ticked tool", async () => {
  const host = fakeHost({ replies: { describe: DESCRIBED, share: { ok: true, grant: { grant_id: "g1" } } } });
  const dialog = fakeDialog({ mcpAnswers: [0] });
  const out = await shareFromDashboard("work-crew", deps(dialog, host));
  assert.deepEqual(out, { ok: true, grant: { grant_id: "g1" } });
  assert.deepEqual(host.calls.map((c) => c.op), ["describe", "share"]);
  assert.deepEqual(host.calls[0].fields, { instance_id: "work-crew" });
  assert.deepEqual(host.calls[1].fields, {
    instance_id: "work-crew",
    consent: DESCRIBED.consent,
    root: "/home/u/proj",
    mcp: ["notes"],
  });
  // A refused server is never offered as a tick.
  assert.ok(!dialog.shown.some((s) => s.kind === "message" && /browser/.test(s.opts.message || "")));
  assert.equal(host.stopped, 1, "the consent-only host ends with its share");
});

test("B02: the crew name shown comes from the host, not the renderer", async () => {
  const host = fakeHost({ replies: { describe: DESCRIBED } });
  const dialog = fakeDialog({ confirm: 1 });
  await shareFromDashboard("work-crew", deps(dialog, host));
  assert.match(dialog.shown[0].opts.detail, /Work crew/);
});

test("B02: cancel on the confirm box or the folder picker shares nothing", async () => {
  for (const dialog of [fakeDialog({ confirm: 1 }), fakeDialog({ canceled: true })]) {
    const host = fakeHost({ replies: { describe: DESCRIBED } });
    const out = await shareFromDashboard("work-crew", deps(dialog, host));
    assert.deepEqual(out, { ok: false, error: "cancelled" });
    assert.deepEqual(host.calls.map((c) => c.op), ["describe"]);
    assert.equal(host.stopped, 1);
  }
});

test("B13: a malformed crew id starts no host and shows no dialog", async () => {
  for (const id of ["", 42, null, "x".repeat(MAX_INSTANCE_ID + 1), "bad\nid", "del\u007f"]) {
    const host = fakeHost();
    const dialog = fakeDialog();
    const d = deps(dialog, host);
    const out = await shareFromDashboard(id, d);
    assert.deepEqual(out, { ok: false, error: "unknown_instance" });
    assert.equal(d.opened.length, 0);
    assert.equal(dialog.shown.length, 0);
  }
  assert.ok(validInstanceId("work-crew"));
});

test("B13: an unknown crew (describe refused) shows no dialog", async () => {
  const host = fakeHost({ replies: { describe: { ok: false, code: "unknown_instance" } } });
  const dialog = fakeDialog();
  const out = await shareFromDashboard("work-crew", deps(dialog, host));
  assert.deepEqual(out, { ok: false, error: "unknown_instance" });
  assert.equal(dialog.shown.length, 0);
  assert.equal(host.stopped, 1);
});

test("a host that is not ready, or with the switch off, shows no dialog", async () => {
  for (const [state, error] of [
    ["disabled", "connected_computers_disabled"],
    ["failed", "no_desktop_host"],
    ["timeout", "no_desktop_host"],
    ["unavailable", "no_desktop_host"],
  ]) {
    const host = fakeHost({ state });
    const dialog = fakeDialog();
    const out = await shareFromDashboard("work-crew", deps(dialog, host));
    assert.deepEqual(out, { ok: false, error }, state);
    assert.equal(dialog.shown.length, 0);
    assert.equal(host.calls.length, 0);
    assert.equal(host.stopped, 1);
  }
});

test("R2: no OS protection shows no dialog", async () => {
  const host = fakeHost({ protection: "" });
  const dialog = fakeDialog();
  const out = await shareFromDashboard("work-crew", deps(dialog, host));
  assert.deepEqual(out, { ok: false, error: "protection_unavailable" });
  assert.equal(dialog.shown.length, 0);
});

test("no host available: nothing is shown", async () => {
  const dialog = fakeDialog();
  const out = await shareFromDashboard("work-crew", { dialog, window: null, openHost: () => null });
  assert.deepEqual(out, { ok: false, error: "no_desktop_host" });
  assert.equal(dialog.shown.length, 0);
});

test("a host refusal on share surfaces its code, never success", async () => {
  const host = fakeHost({ replies: { describe: DESCRIBED, share: { ok: false, code: "turned_off" } } });
  const out = await shareFromDashboard("work-crew", deps(fakeDialog({ mcpAnswers: [1] }), host));
  assert.deepEqual(out, { ok: false, error: "turned_off" });
  assert.equal(host.stopped, 1);
});

test("one share at a time: a second request while a dialog is open is refused", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const host = fakeHost({ replies: { describe: DESCRIBED } });
  const dialog = fakeDialog({ confirm: 1 });
  const slow = { ...dialog, showMessageBox: async (...args) => { await gate; return dialog.showMessageBox(...args); } };
  const first = shareFromDashboard("work-crew", deps(slow, host));
  await new Promise((resolve) => setImmediate(resolve));
  const second = await shareFromDashboard("work-crew", deps(fakeDialog(), fakeHost()));
  assert.deepEqual(second, { ok: false, error: "busy" });
  release();
  assert.deepEqual(await first, { ok: false, error: "cancelled" });
});

test("B13: no HTTP path and no internal secret are used for consent", () => {
  assert.doesNotMatch(MODULE_SOURCE, /X-Internal-Secret|fetch\(|\/api\/connected-computers/);
});

test("the bridge passes only the crew id and the main side is local-gated", () => {
  assert.match(PRELOAD_SOURCE, /connectedComputersAPI/);
  assert.match(PRELOAD_SOURCE, /ipcRenderer\.invoke\("connected-computers:share", String\(/);
  assert.doesNotMatch(PRELOAD_SOURCE, /connected-computers:confirm/);
  const at = REGISTRAR_SOURCE.indexOf('ipcMain.handle("connected-computers:share"');
  assert.ok(at !== -1, "the share handler must be registered");
  assert.match(REGISTRAR_SOURCE.slice(at, at + 600), /assertLocalDashboard\(event, "connected-computers:share"\)/);
});

// The native menu's share, status and Off for a window on a Remote Crew are
// connected-computers-desktop.js, tested in connected-computers-desktop.test.js;
// the store-to-connection mapping is instancesFromStore, tested in
// connected-computers-host.test.js.

test("the native offer limit matches the protocol's exposed-server limit", () => {
  const protocol = readSource(path.join(__dirname, "..", "..", "..", "src", "kiro_crew",
    "connected_computers", "protocol.py"), "utf8");
  const limit = protocol.match(/^MAX_EXPOSED_MCP: Final\[int\] = (\d+)$/m);
  assert.ok(limit, "the protocol must declare its exposed-server limit");
  assert.equal(MAX_MCP_PROMPTS, Number(limit[1]));
});

test("the crew id limit matches the desktop host's", () => {
  const hostSource = readSource(path.join(__dirname, "..", "..", "..", "src", "kiro_crew",
    "connected_computers", "desktop_host.py"), "utf8");
  const limit = hostSource.match(/^MAX_INSTANCE_ID: Final\[int\] = (\d+)$/m);
  assert.ok(limit, "the desktop host must declare its instance id limit");
  assert.equal(MAX_INSTANCE_ID, Number(limit[1]));
});

test("the native dialog can approve the full limit and offers no excess server", async () => {
  const servers = Array.from({ length: MAX_MCP_PROMPTS + 1 }, (_, i) => ({
    name: `server-${i}`, refused: "",
  }));
  const dialog = fakeDialog({ mcpAnswers: Array(MAX_MCP_PROMPTS + 1).fill(0) });
  const result = await askNative("Work crew", servers, { dialog, window: null });
  assert.deepEqual(result.mcp, servers.slice(0, MAX_MCP_PROMPTS).map((s) => s.name));
  assert.equal(dialog.shown.filter((s) => s.kind === "message").length, MAX_MCP_PROMPTS + 1);
});

test("Windows: an unfenced describe warns that commands reach files outside the folder", async () => {
  const { UNFENCED_DETAIL } = require(MODULE_PATH);
  const host = fakeHost({
    replies: { describe: { ...DESCRIBED, confinement: { plan: "windows-none", fenced: false } } },
  });
  const dialog = fakeDialog({ confirm: 1 });
  const out = await shareFromDashboard("work-crew", deps(dialog, host));
  assert.deepEqual(out, { ok: false, error: "cancelled" });
  // Cancelled at the warning: nothing was shared.
  assert.deepEqual(host.calls.map((c) => c.op), ["describe"]);
  const first = dialog.shown[0].opts;
  assert.equal(first.type, "warning");
  // Cancel is the default answer when nothing keeps commands in the folder.
  assert.equal(first.defaultId, 1);
  assert.match(first.detail, /Work crew/);
  assert.ok(first.detail.includes(UNFENCED_DETAIL));
  assert.match(first.detail, /any file your account can/);
});

test("a fenced or older describe keeps the folder-only wording", async () => {
  const { FENCED_DETAIL } = require(MODULE_PATH);
  for (const confinement of [{ plan: "macos-seatbelt", fenced: true }, undefined]) {
    const dialog = fakeDialog({ confirm: 1 });
    await askNative("Work crew", [], { dialog, window: null }, confinement);
    const first = dialog.shown[0].opts;
    assert.equal(first.type, "question");
    assert.equal(first.defaultId, 0);
    assert.ok(first.detail.includes(FENCED_DETAIL));
  }
});
