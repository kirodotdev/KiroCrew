"use strict";

// Display preferences module — currently owns the fontSize slot. Future
// slots (line height, min font size, monospace size) drop in by extending
// schema.js with additional tiers/validators and re-exporting here; the
// IPC namespace, the menu integration and the preload bridge stay stable.
//
// Pure module: no Electron imports, no side effects on load. The main
// process wires Electron surfaces (webContents, ipcMain, store) in via
// function arguments, which is what keeps this unit-testable without
// mounting Electron.
const { TIERS, DEFAULT_TIER, isValidPx } = require("./schema");

// One key under the main electron-store, nested so future slots can join
// without a top-level key each. Matches host-config.js / local-gateway.js
// precedent — one dotted path per feature, no per-feature store file.
const STORE_KEY_FONT_SIZE = "displayPreferences.fontSize";

// Read the persisted fontSize. Three outcomes, all safe:
//   • Missing / null  → DEFAULT_TIER.px (never persisted, fresh install)
//   • Valid tier px   → return as-is
//   • Anything else   → DEFAULT_TIER.px + warning (corrupt / stale schema)
//
// We deliberately DO NOT rewrite the store on corruption. The user's config
// is their document; a silent overwrite would erase the invalid value the
// operator might want to see when debugging. The menu still renders and
// startup still succeeds — that's what matters here.
function getFontSize(store) {
  const persisted = store.get(STORE_KEY_FONT_SIZE);
  if (persisted === undefined || persisted === null) return DEFAULT_TIER.px;
  if (!isValidPx(persisted)) {
    // eslint-disable-next-line no-console -- warning path is intentionally
    // console-based; matches window-state.js / zoom.js style. No logger
    // abstraction exists in this subtree.
    console.warn(
      `[display-preferences] Persisted fontSize ${JSON.stringify(persisted)} is not a valid tier — ` +
      `falling back to ${DEFAULT_TIER.label} (${DEFAULT_TIER.px}px).`,
    );
    return DEFAULT_TIER.px;
  }
  return persisted;
}

// Persist a fontSize. Returns true on write, false on refusal. Refusal is
// silent to the log — the caller (IPC handler or menu click) is what
// notifies the user, if anything: a menu click on a tier can only ever pass
// a valid px, so a false return there means the schema drifted at runtime
// and the log noise would be more misleading than helpful.
function setFontSize(store, px) {
  if (!isValidPx(px)) return false;
  store.set(STORE_KEY_FONT_SIZE, px);
  currentFontSize = px;
  return true;
}

// Module-level cache of the current fontSize. Read at window-creation sites
// in window-lifecycle.js, where the electron-store isn't necessarily in
// scope — the four `webPreferences: { defaultFontSize: … }` blocks would
// otherwise need `store` threaded into every window factory.
//
// The cache is authoritative for the current process; the store is
// authoritative across process launches. Both are kept aligned by
// setFontSize above (successful writes update both; refused writes update
// neither). initCurrentFontSize seeds this from the store at boot.
let currentFontSize = DEFAULT_TIER.px;

// Call ONCE at boot from main.js, BEFORE any BaseWindow is created. Later
// window creations can then call getCurrentFontSize() with zero arguments
// and land on the persisted value. Returning the seeded px so the caller
// can log it or route it as needed.
function initCurrentFontSize(store) {
  currentFontSize = getFontSize(store);
  return currentFontSize;
}

// Read the current fontSize from anywhere in the main process. Pre-init
// returns DEFAULT_TIER.px so a mis-ordered import can't crash startup —
// the worst case is a first paint at Medium that reflows a moment later
// once initCurrentFontSize runs, which is strictly better than throwing.
function getCurrentFontSize() {
  return currentFontSize;
}

// Shell-owned WebContents registry. Tags every webContents belonging to a
// dashboard window that the shell itself created (main window +
// prompt/modal BrowserWindows) so the runtime font-size ripple can filter
// out unrelated WebContents — specifically the embedded browser panel's
// WebContentsView, which loads arbitrary user-browsed sites and MUST NOT
// receive a `Page.setFontSizes` CDP command tuned for shell UI.
//
// Invariant: a webContents is in this set IFF its window was constructed
// via one of the `createShell*` helpers in window-lifecycle.js — those
// helpers are the ONLY places `markShellOwned` is called. A contract
// test in `test/window-lifecycle.test.js` forbids bare `new BrowserWindow`
// / `new WebContentsView` calls in window-lifecycle.js so this invariant
// cannot silently drift.
//
// WeakSet (not Set) so a destroyed window's webContents becomes GC-eligible
// naturally — no manual "unmark on close" plumbing needed.
const SHELL_OWNED_WC = new WeakSet();

function markShellOwned(webContents) {
  if (webContents) SHELL_OWNED_WC.add(webContents);
}

function isShellOwned(webContents) {
  return SHELL_OWNED_WC.has(webContents);
}

// Chrome DevTools Protocol version. Same convention as browser-control.js:
// pinned as a constant so the version isn't magic in the call site. 1.3 is
// the stable-protocol version Electron ships with; experimental commands
// like Page.setFontSizes are still callable, they just aren't versioned in
// 1.3's frozen surface.
// Docs: https://chromedevtools.github.io/devtools-protocol/
const CDP_VERSION = "1.3";

// Live-apply the fontSize across every open webContents. Uses the Chrome
// DevTools Protocol via `webContents.debugger.sendCommand("Page.setFontSizes")`,
// which is the ONLY runtime-mutable path Electron exposes for base font
// size — `webContents.setDefaultFontSize` does NOT exist as an API, despite
// `webPreferences.defaultFontSize` being a valid CONSTRUCTION-time option.
//
// The debugger attach is idempotent from our perspective: if the debugger
// is already held (e.g. browser-control.js owns it for the browser panel),
// we skip the attach and go straight to sendCommand — the existing session
// works for our command too. A double-attach throws
// "Debugger is already attached to the target", which we catch and treat
// as a "skip this wc" — surfacing the whole ripple would strand every
// later webContents.
//
// `webContentsModule` is passed in (not imported) so this file stays
// unit-testable without mounting Electron. Any `webContents`-shaped object
// with a getAllWebContents() -> Array<{ isDestroyed(), debugger: { isAttached(),
// attach(v), sendCommand(method, params) } }> works, which is what
// browser-control.test.js and this file's tests both exercise.
//
// Docs: https://www.electronjs.org/docs/api/debugger
// Docs: https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-setFontSizes
// Track webContents whose apply is still pending because the CDP attach or
// Page.setFontSizes call failed (typical cause: DevTools is open on the
// dashboard and holds the single debugger slot, or a foreign CDP owner had
// it during the ripple). Fable UX Review flagged the silent no-op on
// `7e3aa89991` — before this pair the menu radio would check, the store
// would persist, but the visible text stayed at the prior tier until the
// user restarted. Now: on the next `focus` event for that webContents the
// pending tier is re-applied, so closing DevTools and clicking the window
// self-heals the mismatch.
const PENDING_REAPPLY = new WeakMap();
const FOCUS_LISTENER_ATTACHED = new WeakMap();

function schedulePendingReapply(wc, px) {
  PENDING_REAPPLY.set(wc, px);
  if (FOCUS_LISTENER_ATTACHED.get(wc)) return;
  if (typeof wc.on !== "function") return; // no listener surface — nothing to hook
  wc.on("focus", () => {
    const target = PENDING_REAPPLY.get(wc);
    if (target === undefined) return;
    // Fire-and-forget: applyFontSizeToOne handles its own errors and will
    // re-schedule itself if this retry also fails (DevTools still open).
    void applyFontSizeToOne(wc, target);
  });
  FOCUS_LISTENER_ATTACHED.set(wc, true);
}

async function applyFontSizeToOne(wc, px) {
  if (wc.isDestroyed()) return;
  // Track whether WE attached the debugger — Fable Design Review (f2acc8200)
  // caught a bug where the earlier "attach + send + never detach" shape held
  // the single debugger slot forever after the first tier apply, breaking
  // View → Toggle Developer Tools. Now: if we attach, we own detach; if
  // someone else was already attached, we send-and-leave-alone.
  let weAttached = false;
  if (!wc.debugger.isAttached()) {
    try {
      wc.debugger.attach(CDP_VERSION);
      weAttached = true;
    } catch (err) {
      // Common cause in production: another owner (browser-control.js LIGHT
      // on the browser panel) already holds the debugger. In dev: DevTools
      // is open on this webContents. Either way, we don't own the session,
      // so we can't send our command. Log once and continue with the next
      // webContents — one blocked wc must not strand the ripple.
      // eslint-disable-next-line no-console -- warning path matches the
      // console.warn pattern used elsewhere in this module.
      console.warn(
        `[display-preferences] debugger.attach failed: ${err && err.message ? err.message : err}`,
      );
      schedulePendingReapply(wc, px);
      return;
    }
  }
  try {
    await wc.debugger.sendCommand("Page.setFontSizes", {
      fontSizes: { standard: px, fixed: px },
    });
    // Success — clear any pending re-apply so a later focus event doesn't
    // re-fire a stale target on top of the value we just landed.
    PENDING_REAPPLY.delete(wc);
  } catch (err) {
    // Page navigating mid-command, frame torn down, session lost. Same
    // TOCTOU discipline as the attach case above: log and continue.
    // eslint-disable-next-line no-console -- warning path matches the
    // console.warn pattern used elsewhere in this module.
    console.warn(
      `[display-preferences] Page.setFontSizes failed: ${err && err.message ? err.message : err}`,
    );
    schedulePendingReapply(wc, px);
  } finally {
    // Release the debugger slot we occupied — leaving it attached would
    // starve View → Toggle Developer Tools + any other CDP consumer for
    // the lifetime of this webContents. Only detach what WE attached; a
    // pre-existing session (e.g. browser-control.js) is not ours to close.
    if (weAttached && !wc.isDestroyed()) {
      try {
        if (wc.debugger.isAttached()) {
          wc.debugger.detach();
        }
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn(
          `[display-preferences] debugger.detach failed: ${err && err.message ? err.message : err}`,
        );
      }
    }
  }
}

function applyFontSizeToAllWebContents(webContentsModule, px) {
  // Filter to shell-owned WebContents ONLY. The embedded browser panel's
  // WebContentsView is deliberately excluded — it hosts arbitrary user-
  // browsed sites and must not receive `Page.setFontSizes`, both because
  // that would restyle third-party pages against user intent and because
  // attaching the CDP debugger to it would collide with browser-control.js
  // ownership.
  //
  // The filter matches the construction-time seed exactly: any webContents
  // whose `defaultFontSize` was seeded lives in `SHELL_OWNED_WC`; any that
  // was NOT seeded stays out. Same "seeded ↔ rippled" invariant.
  //
  // Fire each apply and collect the promises so a caller (or a test) can
  // await the full ripple. Each promise handles its own errors, so
  // Promise.all here never rejects — the return value is a completion
  // signal, not a success signal.
  const pending = [];
  for (const wc of webContentsModule.getAllWebContents()) {
    if (!isShellOwned(wc)) continue;
    pending.push(applyFontSizeToOne(wc, px));
  }
  return Promise.all(pending);
}

// Single mutation path for the fontSize slot. Both the IPC set handler
// (dashboard-driven) and the menu callback (main-process-driven) call this,
// so the two entry points can never drift apart — validate → persist →
// live-apply → return the outcome.
//
// Return contract: the fontSize now in effect. On a refused change, this
// is the value the store already holds, which is what the caller renders
// back to the dashboard so its optimistic UI can rehydrate.
//
// If a future slot (line height, min font size) lands, mirror this shape:
// one `change<Slot>` per slot, one caller-agnostic path.
function changeFontSize(store, webContentsModule, px) {
  const ok = setFontSize(store, px);
  if (!ok) return getFontSize(store);
  applyFontSizeToAllWebContents(webContentsModule, px);
  return px;
}

module.exports = {
  TIERS,
  DEFAULT_TIER,
  getFontSize,
  setFontSize,
  initCurrentFontSize,
  getCurrentFontSize,
  applyFontSizeToAllWebContents,
  changeFontSize,
  markShellOwned,
  isShellOwned,
  // resolveTier and STORE_KEY_FONT_SIZE were exported here on 8742b9cc7 for
  // test-only reach-in. Fable First-Principles response caught 0 non-test
  // consumers (`reconcileFontSizeChecks` does its own `tiers.find`); removed.
  // Their corresponding tests were rewritten to exercise the resolution
  // path through changeFontSize instead — see test/display-preferences.test.js.
};
