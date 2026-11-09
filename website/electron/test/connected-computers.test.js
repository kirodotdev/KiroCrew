"use strict";

// Issue 29949: the desktop shell's native consent for sharing this computer.
// Authored before the module; not run locally (host rule), CI runs it.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const MODULE_PATH = path.join(__dirname, "..", "connected-computers.js");
const PRELOAD_SOURCE = fs.readFileSync(path.join(__dirname, "..", "preload.js"), "utf8");
const REGISTRAR_SOURCE = fs.readFileSync(path.join(__dirname, "..", "ipc-registrar.js"), "utf8");
const {
  DESCRIBE_PATH,
  REDEEM_PATH,
  CANCEL_PATH,
  CONSENT_RE,
  MAX_MCP_PROMPTS,
  askNative,
  confirmConsent,
} = require(MODULE_PATH);

const CONSENT = "a".repeat(32);
const BACKEND = "http://127.0.0.1:5476";

function fakeFetch(routes) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init, body: init && init.body ? JSON.parse(init.body) : null });
    const route = routes[url.slice(BACKEND.length)];
    if (!route) return { status: 404, json: async () => ({}) };
    return { status: route.status ?? 200, json: async () => route.body };
  };
  fn.calls = calls;
  return fn;
}

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
  instance: { id: "inst-1", name: "Work crew" },
  mcp: [{ name: "notes", refused: "" }, { name: "browser", refused: "needs a network" }],
};

function deps(dialog, fetchFn, secret = "s3cret") {
  return { dialog, window: null, backendUrl: BACKEND, getSecret: () => secret, fetchFn };
}

test("B02: confirm + folder redeems exactly that consent, root and ticked MCP", async () => {
  const fetchFn = fakeFetch({
    [DESCRIBE_PATH]: { body: DESCRIBED },
    [REDEEM_PATH]: { body: { grant: { grant_id: "g1" } } },
  });
  const dialog = fakeDialog({ mcpAnswers: [0] });
  const out = await confirmConsent(CONSENT, deps(dialog, fetchFn));
  assert.deepEqual(out, { ok: true, grant: { grant_id: "g1" } });
  const redeem = fetchFn.calls.find((c) => c.url.endsWith(REDEEM_PATH));
  assert.deepEqual(redeem.body, { consent: CONSENT, root: "/home/u/proj", mcp: ["notes"] });
  assert.equal(redeem.init.headers["X-Internal-Secret"], "s3cret");
  // A refused server is never offered as a tick.
  assert.ok(!dialog.shown.some((s) => s.kind === "message" && /browser/.test(s.opts.message || "")));
});

test("B02: the instance name shown comes from the gateway, not the renderer", async () => {
  const fetchFn = fakeFetch({ [DESCRIBE_PATH]: { body: DESCRIBED }, [CANCEL_PATH]: { body: {} } });
  const dialog = fakeDialog({ confirm: 1 });
  await confirmConsent(CONSENT, deps(dialog, fetchFn));
  assert.match(dialog.shown[0].opts.detail, /Work crew/);
});

test("B02: cancel on the confirm box redeems nothing and cancels the consent", async () => {
  const fetchFn = fakeFetch({ [DESCRIBE_PATH]: { body: DESCRIBED }, [CANCEL_PATH]: { body: {} } });
  const out = await confirmConsent(CONSENT, deps(fakeDialog({ confirm: 1 }), fetchFn));
  assert.deepEqual(out, { ok: false, error: "cancelled" });
  assert.ok(!fetchFn.calls.some((c) => c.url.endsWith(REDEEM_PATH)));
  assert.ok(fetchFn.calls.some((c) => c.url.endsWith(CANCEL_PATH) && c.body.consent === CONSENT));
});

test("B02: cancel on the folder picker redeems nothing and cancels the consent", async () => {
  const fetchFn = fakeFetch({ [DESCRIBE_PATH]: { body: DESCRIBED }, [CANCEL_PATH]: { body: {} } });
  const out = await confirmConsent(CONSENT, deps(fakeDialog({ canceled: true }), fetchFn));
  assert.equal(out.ok, false);
  assert.ok(!fetchFn.calls.some((c) => c.url.endsWith(REDEEM_PATH)));
  assert.ok(fetchFn.calls.some((c) => c.url.endsWith(CANCEL_PATH)));
});

test("B13: a malformed consent or missing secret shows no dialog and calls nothing", async () => {
  for (const [consent, secret] of [["../x", "s"], [CONSENT, ""], [42, "s"]]) {
    const fetchFn = fakeFetch({});
    const dialog = fakeDialog();
    const out = await confirmConsent(consent, deps(dialog, fetchFn, secret));
    assert.equal(out.ok, false);
    assert.equal(dialog.shown.length, 0);
    assert.equal(fetchFn.calls.length, 0);
  }
  assert.ok(CONSENT_RE.test(CONSENT));
});

test("B13: an unknown consent (describe 404) shows no dialog", async () => {
  const fetchFn = fakeFetch({ [DESCRIBE_PATH]: { status: 404, body: { code: "no_consent" } } });
  const dialog = fakeDialog();
  const out = await confirmConsent(CONSENT, deps(dialog, fetchFn));
  assert.deepEqual(out, { ok: false, error: "no_consent" });
  assert.equal(dialog.shown.length, 0);
});

test("a gateway refusal on redeem surfaces its code, never success", async () => {
  const fetchFn = fakeFetch({
    [DESCRIBE_PATH]: { body: DESCRIBED },
    [REDEEM_PATH]: { status: 409, body: { code: "turned_off" } },
  });
  const out = await confirmConsent(CONSENT, deps(fakeDialog({ mcpAnswers: [1] }), fetchFn));
  assert.deepEqual(out, { ok: false, error: "turned_off" });
});

test("the bridge passes only the consent id and the main side is local-gated", () => {
  assert.match(PRELOAD_SOURCE, /connectedComputersAPI/);
  assert.match(PRELOAD_SOURCE, /ipcRenderer\.invoke\("connected-computers:confirm", String\(/);
  const at = REGISTRAR_SOURCE.indexOf('ipcMain.handle("connected-computers:confirm"');
  assert.ok(at !== -1, "the confirm handler must be registered");
  assert.match(REGISTRAR_SOURCE.slice(at, at + 600), /assertLocalDashboard\(event, "connected-computers:confirm"\)/);
});

// The native menu's share, status and Off for a window on a Remote Crew are
// connected-computers-desktop.js, tested in connected-computers-desktop.test.js;
// the store-to-connection mapping is instancesFromStore, tested in
// connected-computers-host.test.js.

test("the native offer limit matches the protocol's exposed-server limit", () => {
  const protocol = fs.readFileSync(path.join(__dirname, "..", "..", "..", "src", "kiro_crew",
    "connected_computers", "protocol.py"), "utf8");
  const limit = protocol.match(/^MAX_EXPOSED_MCP: Final\[int\] = (\d+)$/m);
  assert.ok(limit, "the protocol must declare its exposed-server limit");
  assert.equal(MAX_MCP_PROMPTS, Number(limit[1]));
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
