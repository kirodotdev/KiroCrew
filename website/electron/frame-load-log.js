"use strict";

/**
 * Frame-load diagnostics for the dashboard's own webContents.
 *
 * The remote-crew panes are cross-origin iframes pointed at SSH-forwarded
 * loopback ports. When one of them never becomes a live document the UI shows
 * "loading pane" forever and NOTHING is recorded anywhere: the main window
 * hooked no load events, the remote gateway keeps no HTTP access log (no
 * aiohttp `access_log` is configured), and a packaged app has no devtools
 * console to open. So the one question that decides the diagnosis — did the
 * frame ever navigate, and with what status — had no answer in any log.
 *
 * This module closes that gap by journaling frame navigations, frame load
 * failures, and renderer console errors into the same gateway-launch.log the
 * rest of the launch path writes to.
 *
 * Kept electron-free so node:test can drive the formatters directly.
 */

/**
 * Identical console errors repeat every paint (a broken pane can emit the same
 * line hundreds of times), and this log is read by tailing it. Cap the repeats
 * so one loud message cannot bury the navigation lines around it.
 */
const CONSOLE_REPEAT_LIMIT = 3;

/**
 * The dashboard's own pane journal (`website/src/lib/paneLog.ts`) emits at INFO
 * level and is allowlisted below regardless of severity: those lines are the
 * renderer half of this diagnosis and are deliberately few.
 *
 * The prefix alone is NOT the authorization — see `isTrustedFrameMessage`. It is a
 * marker the dashboard's own document uses, not a capability, because a crew pane
 * can print the same characters.
 */
const PANE_LOG_PREFIX = "[pane]";

/**
 * A higher cap for allowlisted lines. A pane stuck in a re-mint loop repeats the
 * same journal line, and the REPEAT COUNT is the finding there — capping it at
 * three would hide the loop this instrumentation exists to catch.
 */
const PANE_REPEAT_LIMIT = 20;

/**
 * Longest console text this journals per line.
 *
 * Console text is pane-reachable (see `sanitizeLogText`), and this log is read by
 * tailing it, so one enormous message would scroll every line that matters out of
 * the window even though the repeat cap counted it only once.
 */
const MESSAGE_MAX_LENGTH = 512;

/**
 * How many distinct messages the repeat counter remembers.
 *
 * The counter is keyed by message text, which a compromised pane chooses. Without
 * a bound it can emit endless distinct errors and grow this map for the life of
 * the main process. Dropping the whole map on overflow restarts the counting — a
 * few extra repeats reach the log — rather than leaking.
 */
const REPEAT_KEYS_MAX = 500;

/**
 * Total lines an untrusted frame may contribute for the life of the attachment.
 *
 * This is the ONE hard bound on how much a pane can write, and it has to be
 * aggregate rather than per-path because a pane drives more than one path. The
 * repeat cap is keyed by message TEXT, and text is exactly what a compromised pane
 * controls: varying it defeats a per-message cap, and a pane can just as easily loop
 * its own frame's NAVIGATIONS — each `did-fail-load` / `did-frame-navigate` /
 * `did-start-navigation` is another unconditional line. So every line attributable
 * to an untrusted frame, console or navigation alike, is charged against this single
 * budget (see `record` in `attachFrameLoadLogging`): a limit on one path is a limit
 * a chooser walks around by switching paths.
 *
 * It is per-attachment (one main window), not per-frame, because frame identity is
 * itself pane-influenced once a pane can reload itself into a fresh frame.
 *
 * The trade is explicit: past the budget a genuinely broken pane stops explaining
 * itself. That is acceptable because the first lines are the diagnosis — a framing
 * refusal or a refused fetch repeats, it does not evolve — while an unbounded log is
 * a disk-exhaustion path on the user's machine.
 */
const UNTRUSTED_LOG_BUDGET = 100;

/**
 * One log line's worth of text, with the framing characters neutralized.
 *
 * Everything a console message carries is untrusted input: the crew panes are
 * cross-origin iframes OF THIS webContents, so `console-message` also fires for
 * whatever a remote gateway's document prints. `gateway-launch.log` is a
 * line-oriented file read by tailing it, so a raw newline in that text would let a
 * pane forge whole entries — including entries shaped like this module's own
 * navigation lines, which is exactly the evidence the log exists to provide.
 * Escape the line breaks, replace the remaining C0 controls (an ESC sequence can
 * rewrite what a terminal reader sees), and cap the length.
 */
function sanitizeLogText(text, limit = MESSAGE_MAX_LENGTH) {
  const flat = String(text == null ? "" : text)
    .replace(/\r/g, "\\r")
    .replace(/\n/g, "\\n")
    .replace(/[\u0000-\u001f\u007f]/g, "?");
  if (flat.length <= limit) return flat;
  return `${flat.slice(0, limit)}... [${flat.length - limit} more chars truncated]`;
}

/**
 * A URL safe to journal.
 *
 * A pane URL carries the crew's session token in `?token=…`, which must never
 * be written to a log file. The query is dropped, but the line still records
 * THAT a token was present: the failure worth diagnosing is a token the remote
 * rejected, so its presence is signal while its value is only a secret.
 */
function safeUrl(url) {
  const text = String(url || "");
  const cut = text.indexOf("?");
  if (cut < 0) return text;
  const query = text.slice(cut + 1);
  const marker = /(^|&)token=/.test(query) ? "?token=<redacted>" : "?<query>";
  return text.slice(0, cut) + marker;
}

/** Which frame the event is about — the dashboard itself, or a crew pane. */
function frameLabel(isMainFrame) {
  return isMainFrame ? "main" : "subframe";
}

/**
 * A navigation that STARTED. Paired with the committed-navigation line below,
 * this is what separates the two failures that look identical on screen: a start
 * with no commit is a request that went out and never came back (a dead tunnel
 * that still accepts locally), while no start at all means the frame was never
 * pointed anywhere and no amount of remote debugging would have found it.
 *
 * Same-document navigations are skipped — the dashboard is a router-driven SPA
 * and they carry no load information.
 */
function formatFrameStartNavigation({ url, isInPlace, isMainFrame } = {}) {
  if (isInPlace) return "";
  return `frame navigation STARTED (${frameLabel(isMainFrame)}) ${safeUrl(url)}`;
}

/**
 * A committed navigation. `status` is the HTTP code the frame actually got, so
 * a pane the remote answered with 403 (token refused) is distinguishable from
 * one that was never requested at all — the latter logs no line here.
 */
function formatFrameNavigate({ url, httpResponseCode, isMainFrame } = {}) {
  const status = Number.isFinite(httpResponseCode) ? httpResponseCode : "?";
  return `frame navigated (${frameLabel(isMainFrame)}) status=${status} ${safeUrl(url)}`;
}

/**
 * A navigation that started and failed. The net error code is the whole point:
 * ERR_CONNECTION_REFUSED means the forwarded port had no listener, while
 * ERR_BLOCKED_BY_RESPONSE means the response arrived and framing was refused.
 */
function formatFrameFailLoad({ errorCode, errorDescription, url, isMainFrame } = {}) {
  const code = Number.isFinite(errorCode) ? errorCode : "?";
  const desc = String(errorDescription || "").trim() || "unknown";
  return `frame load FAILED (${frameLabel(isMainFrame)}) code=${code} ${desc} ${safeUrl(url)}`;
}

/**
 * Normalize both `console-message` shapes.
 *
 * Electron >= 35 emits a single details object (`level` is a string); the
 * legacy positional form (`event, level:number, message, line, sourceId`) is
 * deprecated but still delivered, and is what the rest of this app reads.
 * Accepting both means this keeps working across the upgrade that drops one.
 *
 * `frame` is the emitting `WebFrameMain` when the runtime supplies one, and `null`
 * otherwise. It is carried out of here rather than resolved here because it is a
 * live main-process handle, not log content: `isTrustedFrameMessage` is what reads it.
 *
 * @returns {{severity: string, message: string, sourceId: string, line: number,
 *   frame: object|null}|null}
 */
function normalizeConsoleMessage(args) {
  const list = Array.isArray(args) ? args : [];
  const first = list[0];
  // New shape: the sole argument carries the message itself.
  if (first && typeof first === "object" && typeof first.message === "string") {
    return {
      severity: severityFromLevel(first.level),
      message: first.message,
      sourceId: String(first.sourceId || ""),
      line: Number(first.lineNumber) || 0,
      frame: first.frame || null,
    };
  }
  // Legacy shape: args[0] is the event, and the payload follows it.
  if (typeof list[2] === "string") {
    return {
      severity: severityFromLevel(list[1]),
      message: list[2],
      sourceId: String(list[4] || ""),
      line: Number(list[3]) || 0,
      frame: (first && typeof first === "object" && first.frame) || null,
    };
  }
  return null;
}

/**
 * The dashboard's own origin, from whatever the caller loaded the window with.
 *
 * Accepts a full URL (`http://localhost:5476/?token=…`) or a bare origin and
 * reduces both to the RFC 6454 serialization `WebFrameMain#origin` reports, so the
 * comparison in `isTrustedFrameMessage` is string equality on canonical values
 * rather than a prefix or substring test. An unparseable value yields `""`, which
 * disables the `[pane]` allowlist entirely — see the fail-closed note below.
 */
function normalizeTrustedOrigin(value) {
  const text = String(value || "");
  if (!text) return "";
  try {
    return new URL(text).origin;
  } catch {
    return "";
  }
}

/**
 * Did this console message come from the dashboard's OWN document?
 *
 * This is the authorization for the `[pane]` allowlist, and it has to be an
 * identity check rather than a text check. The crew panes are cross-origin iframes
 * of the same webContents, so their console output arrives on the very same event:
 * without this gate any pane could print `[pane] …` and have it copied verbatim
 * into `gateway-launch.log`, forging the one record that decides whether a pane was
 * ever requested — while also buying the higher repeat cap and an exemption from
 * the error-severity filter.
 *
 * BOTH halves are required, and neither alone is sufficient:
 *
 * 1. `frame.parent === null` — the top frame of the frame tree. Chromium, not the
 *    page, owns that relationship, so a nested pane cannot claim it.
 * 2. `frame.origin === <the origin the window was loaded with>`. Position alone is
 *    NOT identity: a cross-origin pane that gets a user click on a `target="_top"`
 *    link can navigate the TOP-LEVEL window to a remote document, and that document
 *    then has `parent === null` too. `WebFrameMain#origin` is Chromium's serialized
 *    security origin, so it says where the text really came from.
 *
 * The other candidate signals can be forged from inside a pane and are deliberately
 * NOT used: `sourceId` is the script URL as reported to devtools, which any script
 * can rewrite with a `//# sourceURL=` comment, and the message text is entirely the
 * page's to choose.
 *
 * Fails CLOSED on every unverifiable input — no configured origin, no `frame`
 * (Electron < 35, where the first argument is a bare event), or a frame whose
 * properties throw because it was destroyed. The cost of failing closed is the
 * INFO-level pane journal; the cost of failing open is a forgeable diagnosis.
 */
function isTrustedFrameMessage(frame, trustedOrigin) {
  if (!trustedOrigin) return false;
  if (!frame || typeof frame !== "object") return false;
  try {
    if (frame.parent !== null) return false;
  } catch {
    return false;
  }
  return frameOriginOf(frame) === trustedOrigin;
}

/**
 * The frame's origin, for attributing a line this module does NOT trust.
 *
 * `WebFrameMain#origin` is Chromium's serialized security origin, so unlike
 * `sourceId` it says where the text really came from. Best-effort: a destroyed
 * frame throws, and an absent origin just means the line goes unattributed.
 */
function frameOriginOf(frame) {
  if (!frame || typeof frame !== "object") return "";
  try {
    return String(frame.origin || "");
  } catch {
    return "";
  }
}

/** Map either level encoding onto one vocabulary. */
function severityFromLevel(level) {
  if (typeof level === "string") {
    const name = level.toLowerCase();
    if (name === "error" || name === "warning" || name === "info" || name === "debug") return name;
    return "info";
  }
  if (level >= 3) return "error";
  if (level === 2) return "warning";
  if (level === 1) return "info";
  return "debug";
}

/**
 * `suppressedNext` marks the line as the last of its kind, so a reader who sees
 * three identical entries knows the silence after them is the cap, not recovery.
 *
 * `untrustedOrigin`, when set, names the pane origin the text came from. A console
 * ERROR from a pane is still worth journaling — a framing refusal or a failed fetch
 * inside the pane is exactly the diagnosis — but the reader has to be able to tell
 * that text apart from the dashboard's own, so it is attributed rather than
 * silently blended in. Every field is passed through `sanitizeLogText`, because all
 * three of message, sourceId, and origin are chosen by the emitting document.
 *
 * The untrusted-frame budget is applied by `record`, not here: the budget bounds
 * every emission path, so its notice belongs to the writer they all share rather
 * than to this one formatter.
 */
function formatConsoleMessage(
  { severity, message, sourceId, line },
  { suppressedNext = false, untrustedOrigin = "" } = {},
) {
  const from = untrustedOrigin ? ` (untrusted frame ${sanitizeLogText(safeUrl(untrustedOrigin), 120)})` : "";
  const where = sourceId ? ` (${sanitizeLogText(safeUrl(sourceId), 200)}:${line})` : "";
  const repeatTail = suppressedNext ? " [further repeats suppressed]" : "";
  return `renderer console [${severity}]${from}: ${sanitizeLogText(message)}${where}${repeatTail}`;
}

/**
 * The notice appended to the last untrusted line the attachment will ever write.
 *
 * Every emission path funnels through `record`, so the silence past the budget could
 * follow a console line or a navigation line — this reads on either, and turns "the
 * pane went quiet" (recovery) into "the budget is spent" (the truth) for whoever is
 * tailing the log.
 */
function budgetReachedNotice() {
  return ` [untrusted-frame log budget of ${UNTRUSTED_LOG_BUDGET} reached; further pane lines dropped]`;
}

/**
 * Attach the diagnostics to `contents`, writing through `log`.
 *
 * Console output is filtered to ERRORS plus the dashboard's own `[pane]` journal.
 * Warnings on this surface are dominated by per-paint noise the renderer emits by
 * the hundred (`content-visibility`, `ResizeObserver loop completed`), which
 * would push the load events that matter out of any readable tail.
 *
 * `trustedOrigin` is the URL (or origin) this webContents was loaded with — the ONE
 * document whose `[pane]` lines are the dashboard's own. Omit it and the journal is
 * disabled rather than granted to whoever happens to be the top frame.
 *
 * Tolerates a missing or partial webContents so a caller never has to guard:
 * this is diagnostics and must not be able to break window creation.
 */
function attachFrameLoadLogging(contents, log, trustedOrigin) {
  if (!contents || typeof contents.on !== "function") return false;
  if (typeof log !== "function") return false;

  const dashboardOrigin = normalizeTrustedOrigin(trustedOrigin);
  // Split by trust so pane-chosen text cannot evict the dashboard's own counters
  // (the shared map's overflow reset would otherwise restart trusted counting too).
  const trustedRepeats = new Map();
  const untrustedRepeats = new Map();
  let untrustedLogged = 0;

  // The ONE writer every handler funnels through. Making it the single path to
  // `log` is what lets the untrusted-frame budget be an invariant rather than a
  // check each handler has to remember: a pane drives both console output and its
  // own frame's navigations, so a bound applied on only one path is one the pane
  // walks around by switching to the other. Trusted lines (the dashboard's own top
  // frame) are never budgeted; everything attributable to a pane is charged against
  // one aggregate ceiling, and the last line it admits carries the notice so the
  // silence afterward reads as the cap, not as the pane recovering.
  const record = (line, trusted) => {
    if (!line) return;
    if (trusted) {
      log(line);
      return;
    }
    if (untrustedLogged >= UNTRUSTED_LOG_BUDGET) return;
    untrustedLogged += 1;
    log(untrustedLogged === UNTRUSTED_LOG_BUDGET ? `${line}${budgetReachedNotice()}` : line);
  };

  // A navigation event carries `isMainFrame`, and for `did-frame-navigate` /
  // `did-fail-load` that is the ONLY trust signal the runtime hands us — they pass
  // frameProcessId/frameRoutingId, not a `WebFrameMain` or an origin. A subframe
  // navigation is the pane's, and the pane is the surface that can loop; a top-frame
  // navigation is the dashboard's own and happens a handful of times over the app's
  // life. Treating a top-frame navigation as trusted cannot forge a pane record the
  // way a console line could — nothing here is allowlisted by it — so unlike the
  // `[pane]` gate, position is a sufficient signal for the budget.
  contents.on("did-start-navigation", (_event, url, isInPlace, isMainFrame) => {
    record(formatFrameStartNavigation({ url, isInPlace, isMainFrame }), isMainFrame === true);
  });

  contents.on("did-frame-navigate", (_event, url, httpResponseCode, _statusText, isMainFrame) => {
    record(formatFrameNavigate({ url, httpResponseCode, isMainFrame }), isMainFrame === true);
  });

  contents.on("did-fail-load", (_event, errorCode, errorDescription, url, isMainFrame) => {
    record(formatFrameFailLoad({ errorCode, errorDescription, url, isMainFrame }), isMainFrame === true);
  });

  contents.on("console-message", (...args) => {
    const entry = normalizeConsoleMessage(args);
    if (!entry) return;
    // The `[pane]` allowlist is the dashboard document's, not any frame's: a crew
    // pane can print the same prefix, so the prefix only counts once the frame
    // identity checks out (top frame AND the dashboard's origin — see
    // `isTrustedFrameMessage`).
    const trusted = isTrustedFrameMessage(entry.frame, dashboardOrigin);
    const allowlisted = trusted && entry.message.startsWith(PANE_LOG_PREFIX);
    if (!allowlisted && entry.severity !== "error") return;
    const limit = allowlisted ? PANE_REPEAT_LIMIT : CONSOLE_REPEAT_LIMIT;
    // Keyed by emitter-chosen text, so the key set needs a ceiling of its own.
    const repeats = trusted ? trustedRepeats : untrustedRepeats;
    if (repeats.size >= REPEAT_KEYS_MAX && !repeats.has(entry.message)) repeats.clear();
    const count = (repeats.get(entry.message) || 0) + 1;
    repeats.set(entry.message, count);
    if (count > limit) return;
    // Repeats are dropped BEFORE `record`, so a suppressed repeat never consumes
    // budget — the budget counts lines actually written, on any path.
    record(
      formatConsoleMessage(entry, {
        suppressedNext: count === limit,
        untrustedOrigin: trusted ? "" : frameOriginOf(entry.frame),
      }),
      trusted,
    );
  });

  return true;
}

module.exports = {
  CONSOLE_REPEAT_LIMIT,
  MESSAGE_MAX_LENGTH,
  PANE_LOG_PREFIX,
  PANE_REPEAT_LIMIT,
  REPEAT_KEYS_MAX,
  UNTRUSTED_LOG_BUDGET,
  attachFrameLoadLogging,
  formatConsoleMessage,
  formatFrameFailLoad,
  formatFrameNavigate,
  formatFrameStartNavigation,
  isTrustedFrameMessage,
  normalizeConsoleMessage,
  normalizeTrustedOrigin,
  safeUrl,
  sanitizeLogText,
};
