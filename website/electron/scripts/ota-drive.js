#!/usr/bin/env node
/**
 * Unattended driver for the end-to-end OTA update test.
 *
 * The update flow is deliberately consent-gated (autoDownload=false), so it
 * cannot be exercised by launching the app and waiting -- something has to
 * press "Download" and "Restart & Update". Rather than add a test-only
 * auto-consent hook to shipped code, this drives the REAL renderer over the
 * Chrome DevTools Protocol and calls the SAME preload-exposed API a click
 * calls (window.updateAPI.download / .install). The code path under test is
 * therefore identical to the one users hit.
 *
 * Requires: the app launched with --remote-debugging-port=<port>, a local feed
 * serving a newer version (scripts/local-feed-server.js), and a `kirocrew`
 * gateway resolvable on PATH (the app only reaches initAutoUpdate after the
 * gateway connects).
 *
 * Usage:
 *   node scripts/ota-drive.js --cdp 9222 --expect 1.0.1 [--timeout 180]
 *
 * Exit 0 only if the updater reached the "downloaded" state and install was
 * dispatched. Verifying the BUNDLE actually swapped is the caller's job (read
 * CFBundleShortVersionString afterwards) -- this script deliberately does not
 * conflate "install dispatched" with "install succeeded".
 */
const http = require("http");

function arg(name, def) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : def;
}
const CDP_PORT = parseInt(arg("cdp", "9222"), 10);
const EXPECT = arg("expect", null);
const TIMEOUT_MS = parseInt(arg("timeout", "180"), 10) * 1000;

const log = (...a) => console.log("[ota-drive]", ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function getJson(path) {
  return new Promise((resolve, reject) => {
    http
      .get({ host: "127.0.0.1", port: CDP_PORT, path }, (res) => {
        let b = "";
        res.on("data", (c) => (b += c));
        res.on("end", () => {
          try { resolve(JSON.parse(b)); } catch (e) { reject(e); }
        });
      })
      .on("error", reject);
  });
}

/** Find the renderer target that actually has the update bridge attached. */
async function findUpdateTarget(deadline) {
  while (Date.now() < deadline) {
    let targets = [];
    try { targets = await getJson("/json/list"); } catch { /* devtools not up yet */ }
    for (const t of targets.filter((x) => x.type === "page" && x.webSocketDebuggerUrl)) {
      const probe = await evaluate(t.webSocketDebuggerUrl, "typeof window.updateAPI").catch(() => null);
      if (probe && probe.value === "object") {
        log(`found renderer with updateAPI: ${t.url.slice(0, 80)}`);
        return t.webSocketDebuggerUrl;
      }
    }
    await sleep(1000);
  }
  throw new Error("no renderer exposing window.updateAPI appeared before the timeout");
}

/** One-shot CDP Runtime.evaluate over a fresh socket (keeps state simple). */
function evaluate(wsUrl, expression) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const timer = setTimeout(() => { try { ws.close(); } catch {} reject(new Error("evaluate timed out")); }, 15000);
    ws.onopen = () => ws.send(JSON.stringify({
      id: 1,
      method: "Runtime.evaluate",
      params: { expression, awaitPromise: true, returnByValue: true },
    }));
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.id !== 1) return;
      clearTimeout(timer);
      try { ws.close(); } catch {}
      if (msg.error) { reject(new Error(JSON.stringify(msg.error))); return; }
      if (msg.result && msg.result.exceptionDetails) {
        reject(new Error(msg.result.exceptionDetails.text || "evaluate threw"));
        return;
      }
      resolve(msg.result && msg.result.result);
    };
    ws.onerror = () => { clearTimeout(timer); reject(new Error("websocket error")); };
  });
}

// Installs a recorder in the page so update states can be polled. Idempotent:
// re-running must not stack listeners or lose already-seen states.
const RECORDER = `
(() => {
  if (!window.__ota) {
    window.__ota = { states: [] };
    window.updateAPI.onState((s) => window.__ota.states.push(s));
  }
  return window.__ota.states.length;
})()`;

const POLL = `JSON.stringify(window.__ota ? window.__ota.states : [])`;

async function waitForState(wsUrl, wanted, deadline) {
  let last = "";
  while (Date.now() < deadline) {
    const r = await evaluate(wsUrl, POLL);
    const states = JSON.parse(r.value || "[]");
    const names = states.map((s) => s.state).join(",");
    if (names !== last) { log(`states: ${names || "(none)"}`); last = names; }
    const hit = states.find((s) => s.state === wanted);
    if (hit) return hit;
    const err = states.find((s) => s.state === "error");
    if (err) throw new Error(`updater reported error: ${err.message}`);
    await sleep(2000);
  }
  throw new Error(`state "${wanted}" not reached before the timeout`);
}

(async () => {
  const deadline = Date.now() + TIMEOUT_MS;
  const ws = await findUpdateTarget(deadline);
  await evaluate(ws, RECORDER);

  // Wait for the update IPC to actually ANSWER before driving anything.
  //
  // preload.js exposes window.updateAPI unconditionally, so findUpdateTarget's
  // `typeof window.updateAPI` probe succeeds while main.js may not have called
  // ipcMain.handle("update:*") yet. Driving straight into that produced a bare
  // "No handler registered for 'update:check'" and killed the run before the
  // swap was ever attempted. main.js now registers the handlers before the
  // awaited gateway boot, so this should pass on the first poll -- but keep the
  // gate: if it ever regresses we get a named failure at the right step instead
  // of an unhandled rejection three lines later.
  //
  // NOTE the .then(): the expression is stringified in-page, and
  // JSON.stringify(aPromise) is "{}" -- awaitPromise cannot rescue that, because
  // the value handed back is already a string. This is why every previous run
  // logged "app info: {}" and told us nothing.
  const infoExpr = "window.updateAPI.getInfo().then(i => JSON.stringify(i))";
  let info = null;
  for (let attempt = 1; Date.now() < deadline; attempt++) {
    const r = await evaluate(ws, infoExpr).catch((e) => ({ __err: e.message }));
    if (r && !r.__err && typeof r.value === "string" && r.value !== "{}") { info = r.value; break; }
    const why = r && r.__err ? r.__err : `unusable result ${JSON.stringify(r && r.value)}`;
    if (attempt === 1 || attempt % 5 === 0) log(`update IPC not answering yet (${why})`);
    await sleep(2000);
  }
  if (info === null) {
    throw new Error(
      "update IPC never answered: window.updateAPI exists but no ipcMain handler responded. " +
      "Check that main.js registers ipcMain.handle(\"update:*\") BEFORE `await startGateway()`.",
    );
  }
  log("app info:", info);

  log("triggering a check (the launch-delay check may already have run)");
  await evaluate(ws, "window.updateAPI.check()");
  const found = await waitForState(ws, "found", deadline);
  log(`DISCOVERY ok -> found ${found.version} (no download yet: consent gate held)`);
  if (EXPECT && found.version !== EXPECT) {
    throw new Error(`expected to find ${EXPECT}, feed offered ${found.version}`);
  }

  log("granting consent -> updateAPI.download()");
  await evaluate(ws, "window.updateAPI.download()");
  const downloaded = await waitForState(ws, "downloaded", deadline);
  log(`DOWNLOAD ok -> staged ${downloaded.version}`);

  log("dispatching install -> updateAPI.install() (app will quit + swap)");
  await evaluate(ws, "window.updateAPI.install()").catch((e) => {
    // The app quits mid-call, so a dropped socket here is expected/normal.
    log(`install dispatched (socket closed as the app quit: ${e.message})`);
  });
  log("OK: discovery -> consent -> download -> install dispatched");
  process.exit(0);
})().catch((e) => {
  console.error("[ota-drive] FAIL:", e.message);
  process.exit(1);
});
