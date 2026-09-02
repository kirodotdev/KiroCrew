"use strict";

/**
 * Keep the boot splash (and the token prompt) out of the window's reachable
 * navigation history — fix for #5538.
 *
 * `showLoadingThenConnect` paints `loading.html` into the main window's own
 * webContents and then replaces it with the dashboard on that SAME
 * webContents, which makes the splash an ordinary previous entry in Chromium's
 * navigation history. Chromium binds mouse button 4 (and two-finger swipe on
 * macOS) to history-back by default, so the gesture pops the dashboard and
 * lands on a page whose only purpose was to be replaced — with no forward
 * affordance, quitting is the only way out. The gateway reconnect/recovery
 * paths re-paint `loading.html` into the live window and `token-prompt.html`
 * is loaded the same way, so every one of those loads mints another dead-end
 * history entry, not just the boot one.
 *
 * The narrow fix is to remove the unreachable entries, NOT to intercept the
 * back gesture (`app-command` / `will-navigate` / swallowing button 4): the
 * dashboard is a single-page app whose own in-app history the user may
 * legitimately navigate, and blocking back globally is a behavior change
 * nobody asked for. So: once a NON-transient page (the dashboard) has actually
 * committed, surgically remove every transient shell entry still in the
 * history via `navigationHistory.removeEntryAtIndex()` (Electron ≥34; the app
 * pins Electron ^43 — the pre-36 `clearHistory()` spelling is gone). Removal
 * is per-entry rather than `navigationHistory.clear()` on purpose: after a
 * gateway reconnect the history legitimately holds the user's own prior
 * dashboard routes behind the re-painted splash, and a wholesale clear would
 * erase those too, breaking in-app Back — the splash entries are the bug, the
 * dashboard entries are the user's.
 *
 * Listening on `did-finish-load` (persistent, armed once per window) rather
 * than hooking each `loadURL` call site has two properties the call-site
 * approach lacks:
 *   - `loadURL`'s promise resolves before the navigation commits, so a
 *     call-site prune can run against the OLD history state; the event fires
 *     only after the document actually loaded.
 *   - the token-prompt → dashboard handoff happens in the RENDERER
 *     (`window.location.href = …` in token-prompt.html), so there is no
 *     main-process `loadURL` to hook for it — but `did-finish-load` still
 *     fires in the main process.
 * In-app SPA route changes (pushState) do not emit `did-finish-load`, so a
 * pure-dashboard history is never touched (see transientEntryIndexes).
 */

// Main-process-loaded shell pages whose only purpose is to be replaced by the
// dashboard. Anything loaded via `wc.loadFile(...)` arrives as a file:// URL.
const TRANSIENT_SHELL_PAGES = new Set(["loading.html", "token-prompt.html"]);

/**
 * True when `url` is one of the transient shell pages this process loads from
 * disk. Only file: URLs qualify — the dashboard is always http(s), so a
 * dashboard route that merely CONTAINS "loading.html" in a path or query can
 * never match.
 */
function isTransientShellPage(url) {
  if (typeof url !== "string" || !url.startsWith("file:")) return false;
  let pathname;
  try {
    pathname = new URL(url).pathname;
  } catch {
    return false;
  }
  const base = pathname.slice(pathname.lastIndexOf("/") + 1);
  return TRANSIENT_SHELL_PAGES.has(base);
}

/**
 * Compute which history entries to remove, given a snapshot of the navigation
 * state. Pure — the I/O lives in armSplashHistoryClear.
 *
 * Returns the indexes of transient shell entries, in DESCENDING order —
 * removal shifts every subsequent index down, so pruning must run
 * highest-index-first to keep the remaining indexes valid. Empty when:
 *   - the page the user is looking at is itself a transient shell page
 *     (the splash must keep working as the current page during boot/recovery;
 *     the prune runs on the success transition, not on splash load), or
 *   - no transient entry exists — e.g. the dashboard's own SPA history, which
 *     is the user's and is deliberately never touched (removing anything else,
 *     let alone clearing, would break in-app Back after a gateway reconnect).
 * The active entry is never listed (Chromium refuses to remove it anyway).
 */
function transientEntryIndexes({ currentUrl, entries, activeIndex }) {
  if (isTransientShellPage(currentUrl)) return [];
  if (!Array.isArray(entries)) return [];
  const indexes = [];
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    if (i !== activeIndex && entry && isTransientShellPage(entry.url)) {
      indexes.push(i);
    }
  }
  return indexes;
}

/**
 * Arm the prune-on-handoff listener on a window's webContents. Call ONCE per
 * window (from setupWindowContents) — the listener is persistent and covers
 * every splash/prompt → dashboard handoff for the window's lifetime: boot,
 * the gateway reconnect/recovery re-paints, and the renderer-driven
 * token-prompt submit.
 *
 * `isAlive` mirrors the surrounding main.js convention: every touch of a
 * webContents is bracketed by destroyed-checks/try-catch because the window
 * can be torn down mid-flight, and `did-finish-load` can race that teardown.
 *
 * Returns the handler (for tests).
 */
function armSplashHistoryClear(wc, { isAlive = () => true, log = () => {} } = {}) {
  const onDidFinishLoad = () => {
    try {
      if (!isAlive()) return;
      const nav = wc.navigationHistory;
      if (!nav) return; // ancient/foreign webContents — nothing to guard
      const indexes = transientEntryIndexes({
        currentUrl: wc.getURL(),
        entries: nav.getAllEntries(),
        activeIndex: nav.getActiveIndex(),
      });
      if (indexes.length === 0) return;
      // Descending order — see transientEntryIndexes.
      for (const i of indexes) nav.removeEntryAtIndex(i);
      log(
        `nav-history: removed ${indexes.length} transient shell entr` +
          `${indexes.length === 1 ? "y" : "ies"} (loading/token-prompt) ` +
          "after dashboard handoff (#5538)"
      );
    } catch {
      /* window may be mid-teardown — same tolerance as the loadFile call sites */
    }
  };
  wc.on("did-finish-load", onDidFinishLoad);
  return onDidFinishLoad;
}

module.exports = { armSplashHistoryClear, transientEntryIndexes, isTransientShellPage };
