"use strict";

const path = require("path");

// Give a renderer-triggered download (`<a download>` + a.click() on a blob URL,
// as the Portability "Download Export (.zip)" button does) a real save path.
//
// The dashboard renders in a WebContentsView under a BaseWindow, which Chromium
// cannot parent a native "Save As" dialog to. With no `will-download` listener
// the DownloadItem waits on that dialog, which never appears, so nothing lands
// on disk while the renderer still reports "Download started" (issue #13047).
// Setting the save path explicitly (OS downloads dir + the item's filename,
// de-duplicated like a browser) lets the download complete with no dialog.
//
// Dependency-injected (`app`, `fs`, `log`) so it unit-tests without a live
// Electron session.

/**
 * A filesystem-equivalent key for a path, used so the in-flight reservation set
 * recognises two spellings that name the SAME filesystem entry as one.
 *
 * On a case-INSENSITIVE volume (the default on macOS/APFS and on Windows/NTFS)
 * `Downloads/Report.pdf` and `Downloads/report.pdf` are the same file. The disk
 * check (`fs.existsSync`) already folds case on such a volume, but the
 * reservation `Set` is a plain string compare: two concurrent downloads named
 * `Report.pdf` and `report.pdf` start before either file exists, so each sees
 * the other's reservation as a different string, both select "their" path, and
 * one silently overwrites/corrupts the other. Keying the set by this folded
 * form closes that: the second download sees the first's reservation and picks
 * a suffixed name instead.
 *
 * Case-insensitivity is decided by the host OS (`process.platform`): `darwin`
 * and `win32` are treated as case-insensitive, every other platform (Linux) as
 * case-sensitive. This is the fail-SAFE direction for data: a case-sensitive
 * APFS/NTFS volume treated as insensitive only makes collision avoidance MORE
 * conservative (it may suffix two names that could have coexisted), never less
 * safe — it can never let one download overwrite another. The returned key is
 * used ONLY for reservation lookup/insert/release; the save path written to
 * disk always keeps its original spelling.
 *
 * @param {string} p - an absolute path
 * @param {string} [platform] - override for `process.platform` (testing)
 * @returns {string} the folded reservation key
 */
function fsPathKey(p, platform = process.platform) {
  const s = String(p);
  return platform === "darwin" || platform === "win32" ? s.toLowerCase() : s;
}

/**
 * Resolve a collision-free absolute save path under `dir` for `filename`.
 *
 * Mirrors the browser's own behaviour: "export.zip", then "export (1).zip",
 * "export (2).zip", ... when earlier ones already exist. Bounded so a wedged
 * filesystem (every candidate "exists") cannot spin forever: when the plain
 * name and every suffix 1..maxTries are already taken, it returns `null` so the
 * caller can refuse the download rather than hand back an occupied path that
 * would overwrite (and destroy) an existing file.
 *
 * A candidate is unavailable if it is already on disk OR present in `reserved`
 * -- the set of paths picked for downloads that are still in flight. Checking
 * only the disk is not enough: two concurrent same-named downloads both start
 * before either file exists, so without the reservation set they would select
 * the same path and one would overwrite the other. `reserved` is keyed by
 * {@link fsPathKey}, so on a case-insensitive volume two spellings that name
 * the SAME entry (`Report.pdf` / `report.pdf`) collide as they do on disk.
 *
 * @param {{existsSync: (p: string) => boolean}} fs - node `fs` (injectable)
 * @param {string} dir - target directory (absolute)
 * @param {string} filename - the download's own filename
 * @param {number} [maxTries] - collision-suffix ceiling (default 1000)
 * @param {{has: (p: string) => boolean}} [reserved] - fsPathKey-keyed set of
 *   paths already claimed by in-flight downloads
 * @param {string} [platform] - override for `process.platform` (testing)
 * @returns {string | null} an unused absolute path (original spelling), or null if none is free
 */
function uniqueSavePath(fs, dir, filename, maxTries = 1000, reserved = null, platform = process.platform) {
  const ext = path.extname(filename);
  const stem = path.basename(filename, ext);
  const taken = (p) =>
    fs.existsSync(p) || (reserved != null && reserved.has(fsPathKey(p, platform)));
  let candidate = path.join(dir, filename);
  for (let n = 1; taken(candidate); n += 1) {
    if (n > maxTries) return null;
    candidate = path.join(dir, `${stem} (${n})${ext}`);
  }
  return candidate;
}

/**
 * Reduce a download's reported filename to a bare, non-escaping leaf name.
 *
 * `item.getFilename()` is untrusted (it comes from the download's
 * `Content-Disposition`/URL), so it must never be trusted to stay inside the
 * downloads directory. The handler runs inside the user's own OS Electron
 * build, so the name is reduced with BOTH POSIX and Windows separator rules
 * (`path.basename` would leave `..\\..\\x` intact on a POSIX host): any `/` or
 * `\\` directory component is stripped to the last segment. A NUL byte (which
 * can truncate a path at the syscall boundary) and the empty / `.` / `..`
 * markers carry no usable leaf, and a Windows reserved device name (`CON`,
 * `NUL`, `COM1`, ...) must not be used as a filename, so all of these fall back
 * to a fixed `"download"`. Chromium sanitizes the path too, but doing it here
 * does not rely on that and keeps the save path inside Downloads.
 *
 * @param {string} raw - the download's reported filename
 * @returns {string} a safe single-segment filename
 */
function safeFilename(raw) {
  const s = String(raw || "");
  // A NUL can truncate the path at the OS boundary; refuse the whole name.
  if (s.includes("\0")) return "download";
  // Reduce to the last segment under both separator conventions, so a Windows
  // `\\` component is stripped even when this runs on a POSIX host.
  const base = path.win32.basename(path.posix.basename(s));
  if (!base || base === "." || base === "..") return "download";
  // Windows reserved device names (optionally with an extension) cannot be
  // ordinary filenames; reject them regardless of host so a packaged Windows
  // build never tries to write to a device.
  const stem = base.split(".")[0].toUpperCase();
  const RESERVED = /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/;
  if (RESERVED.test(stem)) return "download";
  return base;
}

/**
 * Whether a single download URL is eligible for the no-dialog auto-save.
 *
 * Eligible only when the URL's effective origin equals the dashboard origin:
 *   - a `blob:` URL whose INNER origin is the dashboard (so a blob minted by a
 *     cross-origin frame — `blob:https://other/uuid` — is NOT eligible; and a
 *     `blob:null/...` opaque-origin blob yields origin `"null"`, never a match),
 *   - an `http(s)` URL same-origin with the dashboard.
 * `new URL(u).origin` yields `"null"` for `data:`, `file:` and `filesystem:`
 * URLs, so those never match a real dashboard origin and are rejected here.
 * The comparison is an EXACT `.origin` equality (scheme + host + port), never a
 * prefix/substring test, so `http://host:port.evil.com` or a userinfo trick
 * resolves to its real origin and fails the check.
 *
 * @param {string} one - a single URL to test
 * @param {string} dashOrigin - the dashboard's own `.origin` (already parsed)
 * @returns {boolean}
 */
function urlOriginIsDashboard(one, dashOrigin) {
  try {
    return new URL(String(one)).origin === dashOrigin;
  } catch {
    return false;
  }
}

/**
 * Whether a download should be auto-saved to Downloads with no prompt.
 *
 * Auto-saving every download the dashboard session sees is too broad: a page
 * the user navigated to, or an embedded cross-origin resource, could start a
 * download that then lands silently in Downloads. Limit the no-dialog save to
 * downloads that are genuinely the dashboard's own: EVERY hop of the download's
 * URL chain must have an effective origin equal to the dashboard origin (a
 * dashboard `blob:` or a dashboard same-origin URL). Checking the whole chain —
 * not just the final URL — means a redirect that starts cross-origin and lands
 * on a same-origin URL is still rejected.
 *
 * Fails closed: a missing/unparseable `dashboardUrl`, an empty chain, or ANY
 * non-dashboard hop yields false (do not auto-save) rather than throwing.
 *
 * @param {string[]} chain - the download's full URL chain (`item.getURLChain()`),
 *   or a single-element array with `item.getURL()` when no chain is available
 * @param {string} [dashboardUrl] - the dashboard window's own origin URL
 * @returns {boolean}
 */
function shouldAutoSave(chain, dashboardUrl) {
  if (!dashboardUrl) return false;
  let dashOrigin;
  try {
    dashOrigin = new URL(String(dashboardUrl)).origin;
  } catch {
    return false;
  }
  // An opaque dashboard origin ("null") must never become a wildcard that
  // matches every data:/file:/blob:null download.
  if (!dashOrigin || dashOrigin === "null") return false;
  const hops = Array.isArray(chain) ? chain.filter((h) => h != null && h !== "") : [];
  if (hops.length === 0) return false;
  return hops.every((hop) => urlOriginIsDashboard(hop, dashOrigin));
}

/**
 * Build the `will-download` listener for the dashboard session.
 *
 * The returned function has the Electron `(event, item, webContents)` shape and
 * is attached with `session.on("will-download", handler)`. Attach it ONCE per
 * session -- `session.on` adds a listener each call and never removes it, so a
 * per-window attach would stack N handlers for N windows. The caller guards
 * this (see window-lifecycle.js).
 *
 * @param {object} deps
 * @param {{getPath: (name: string) => string}} deps.app - Electron `app`
 * @param {{existsSync: (p: string) => boolean}} [deps.fs] - node `fs` (injectable)
 * @param {(...args: unknown[]) => void} [deps.log] - logger (injectable)
 * @param {string} [deps.dashboardUrl] - the dashboard window's own origin URL,
 *   used to decide which downloads are same-origin and so eligible for the
 *   no-dialog auto-save. Omitted/empty means NO download auto-saves. Used as a
 *   fallback when `resolveDashboardUrl` is absent or returns nothing.
 * @param {(webContents: unknown) => (string | undefined)} [deps.resolveDashboardUrl]
 *   - resolve the dashboard origin URL for the download's OWN `webContents`
 *   (the third argument `will-download` passes). The handler is attached ONCE
 *   per session, but every window shares that session, so a single captured
 *   `dashboardUrl` would only match the first window's origin — a later window
 *   on a different gateway would fail `shouldAutoSave`/`claim` and its export
 *   would never land (issue #13047 stays open there). Resolving per-download
 *   from the triggering `webContents` makes auto-save work for every window.
 *   Falls back to `dashboardUrl` when this is absent or returns a falsy value.
 * @param {{claim: (args: {origin: string, filename: string}) => boolean}} [deps.pending]
 *   - the shared download-expectations store. A download auto-saves ONLY when
 *   its (origin, filename) matches a live single-use expectation the renderer
 *   announced via the preload bridge; everything else falls back to Chromium's
 *   default handling. Omitted means NO download auto-saves (fail closed).
 * @param {string} [deps.platform] - override for `process.platform`, used to
 *   decide filesystem case-sensitivity for the in-flight reservation keys
 *   (testing). Defaults to the host platform.
 * @returns {(event: unknown, item: object) => void}
 */
function createWillDownloadHandler({ app, fs = require("fs"), log = () => {}, dashboardUrl, resolveDashboardUrl, pending, platform = process.platform }) {
  // Paths claimed by downloads that are still in flight on this session. The
  // on-disk check alone cannot see a sibling download that has selected a path
  // but not yet written it, so without this two concurrent same-named downloads
  // would pick the same path and one would overwrite the other. One set per
  // handler (one handler per session), released on the item's "done" event.
  const reserved = new Set();

  return function onWillDownload(_event, item, webContents) {
    // Resolve the dashboard origin for THIS download's own window. The handler
    // is attached once per session, but every window shares that session, so a
    // single captured `dashboardUrl` only matches the first window's origin: a
    // later window on a different gateway would fail the checks below and its
    // export would never land (issue #13047 stays open there). Deriving the URL
    // from the triggering `webContents` fixes auto-save for every window; the
    // captured `dashboardUrl` remains the fallback (and keeps the unit tests
    // that inject a fixed origin working).
    let windowUrl;
    if (typeof resolveDashboardUrl === "function") {
      try {
        windowUrl = resolveDashboardUrl(webContents);
      } catch {
        windowUrl = undefined;
      }
    }
    if (!windowUrl) windowUrl = dashboardUrl;

    // Only auto-save downloads the dashboard itself produced: a dashboard
    // `blob:` or a dashboard same-origin URL, across EVERY hop of the redirect
    // chain. For anything else (a page the user navigated to, an embedded
    // cross-origin resource, a cross-origin redirect) do nothing and let
    // Chromium handle the download its own way, rather than silently dropping
    // an arbitrary file into Downloads.
    const chain = typeof item.getURLChain === "function" && item.getURLChain()
      ? item.getURLChain()
      : (typeof item.getURL === "function" ? [item.getURL()] : []);
    if (!shouldAutoSave(chain, windowUrl)) {
      const shown = (chain && chain[chain.length - 1]) || "no url";
      log(`will-download: not auto-saving non-dashboard download (${shown})`);
      return;
    }

    // Same origin is necessary but NOT sufficient: anything that renders in the
    // dashboard origin (a rendered artifact, a preview, markdown) could mint a
    // same-origin / blob download. Auto-save ONLY a download the app explicitly
    // announced via the preload bridge — a live, single-use expectation keyed by
    // (origin, filename). No matching expectation -> leave it to Chromium's
    // default handling (the Save dialog), never a silent save.
    let dashOrigin;
    try {
      dashOrigin = new URL(String(windowUrl)).origin;
    } catch {
      return;
    }
    const filename = safeFilename(item.getFilename());
    if (!pending || typeof pending.claim !== "function"
        || !pending.claim({ origin: dashOrigin, filename })) {
      log(`will-download: no pending expectation for "${filename}"; using default handling`);
      return;
    }

    let savePath;
    try {
      const downloads = app.getPath("downloads");
      savePath = uniqueSavePath(fs, downloads, filename, 1000, reserved, platform);
      if (!savePath) {
        // Every collision-free name is taken (plain name + suffixes 1..maxTries
        // all exist, on disk or reserved by an in-flight download). Refuse
        // rather than overwrite an existing file: cancel the download and log
        // it. The renderer already reported "Download started", so this is a
        // logged no-op, never silent data loss.
        log(`will-download: no free filename for "${filename}" in ${downloads}; cancelling`);
        if (typeof item.cancel === "function") item.cancel();
        return;
      }
      // Claim the path so a concurrent same-named download selects a different
      // one, and set it so Chromium writes the file instead of waiting for a
      // dialog it cannot parent in this window shape. The reservation is keyed
      // by fsPathKey so a case-variant spelling on a case-insensitive volume
      // (Report.pdf vs report.pdf) collides here as it would on disk.
      reserved.add(fsPathKey(savePath, platform));
      item.setSavePath(savePath);
    } catch (err) {
      // If we cannot even compute a path, let Electron fall back to its own
      // default rather than throwing out of the event handler.
      log("will-download: could not set save path:", err && err.message ? err.message : err);
      return;
    }

    item.once("done", (_doneEvent, state) => {
      // Release the reservation whatever the outcome: a completed download now
      // occupies the path on disk, and a cancelled/interrupted one frees it.
      // Release by the SAME fsPathKey the reservation was inserted under.
      reserved.delete(fsPathKey(savePath, platform));
      if (state !== "completed") {
        // "cancelled" | "interrupted" | anything else: the file did not land.
        log(`will-download: download did not complete (state=${state}) for ${savePath}`);
      }
    });
  };
}

// Sessions already wired with a will-download handler. `session.on` appends a
// listener on every call and never removes it, so attaching per-window would
// stack N handlers (and N `item.once("done")` callbacks) for N windows that
// share the default session, and leak them when a window closes. A WeakSet keys
// off the session object itself, so a session is wired at most once and the
// entry disappears with the session.
const wiredSessions = new WeakSet();

/**
 * Attach the will-download handler to `session` exactly once, no matter how many
 * windows share it. Safe to call from every window's setup path.
 *
 * @param {object} session - the Electron session (view.webContents.session)
 * @param {object} deps - forwarded to {@link createWillDownloadHandler} (app, log, fs)
 * @returns {boolean} true if it attached now, false if this session was already wired
 */
function wireWillDownloadOnce(session, deps) {
  if (!session || wiredSessions.has(session)) return false;
  wiredSessions.add(session);
  session.on("will-download", createWillDownloadHandler(deps));
  return true;
}

module.exports = {
  fsPathKey,
  uniqueSavePath,
  safeFilename,
  urlOriginIsDashboard,
  shouldAutoSave,
  createWillDownloadHandler,
  wireWillDownloadOnce,
};
