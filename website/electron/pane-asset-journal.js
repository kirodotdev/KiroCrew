"use strict";

/**
 * Journal the module-graph fetches of the remote-crew panes, from the main process.
 *
 * The failure this closes: a pane's document loads (the parent sees
 * `frame navigated status=200` and `iframe-load`), the inline scripts in its
 * index.html run (its service worker registers), and then NOTHING — no console
 * line, no `mc-embedded-boot`, no `mc-embedded-ready`, no API call ever reaches the
 * remote gateway. Observed on four different crews across four days, always the one
 * whose tunnel landed on the first allocated local port. Every renderer-side signal
 * is silent there by construction: the entry bundle is an ES module with ~240
 * statically preloaded chunks, and Chromium evaluates a module graph only once every
 * edge has loaded. One `/assets/*` response that stalls mid-stream over a
 * just-opened SSH tunnel leaves the graph waiting forever — no error event, no
 * timeout, no JavaScript of ours runs, so nothing on the renderer side can report it.
 *
 * The main process CAN see it: `session.webRequest` observes every request the
 * pane's frame issues. This module counts, per pane origin, the hashed-asset
 * requests started / completed / failed, and journals:
 *
 *   `[pane-assets] origin=http://localhost:7778 started=242 done=241 failed=0
 *    inflight=1 STALLED=/assets/App-abc.js after=20000ms`
 *
 * when a request has been in flight past `STALL_MS`, and a one-line summary when a
 * pane's graph settles (`inflight` returns to 0 after having been non-zero). A pane
 * that never settles and shows one STALLED line is this bug; a pane whose graph
 * settled and still never announced readiness is a different one.
 *
 * Scope is deliberately narrow: loopback origins only, `/assets/` paths only (the
 * content-hashed chunks the module graph is made of), and never the dashboard's own
 * origin — its bundle is served by the local gateway and is not the surface that
 * stalls. URLs are journaled without their query string, so a `?token=` can never
 * reach the log through this path (hashed assets carry none, but the strip is
 * unconditional).
 *
 * Bounded: one stall line per request, one settle line per settle, the request
 * table is capped so a pane that issues requests forever cannot grow main-process
 * memory without bound, and the journal as a whole writes at most `LOG_LINES_MAX`
 * lines per attachment (one final `budget-exhausted` line, then silence). The
 * remote pane is untrusted: without the aggregate cap a hostile one could loop
 * single fetches and turn every settle into a log line, growing the unrotated
 * launch log until the disk is full. The bug this exists to diagnose shows in the
 * first handful of lines, so the cap costs nothing legitimate.
 */

/** In-flight time after which a hashed-asset request is journaled as stalled. */
const STALL_MS = 20_000;
/** Ceiling on tracked in-flight requests per origin. Past it, new requests count but are not timed. */
const INFLIGHT_TRACK_MAX = 512;
/** Ceiling on origins tracked; the instances feature caps warm panes far below this. */
const ORIGINS_MAX = 64;
/**
 * Ceiling on journal lines written per attachment (per app launch), across all
 * origins. Generous for the diagnostic (a stuck pane produces a few lines; a
 * healthy one produces one settle line per navigation) and tiny for a disk.
 */
const LOG_LINES_MAX = 2_000;

const PREFIX = "[pane-assets]";

/** Origin + path of a request URL, or null when it is not a loopback hashed asset. */
function classifyUrl(url) {
  let parsed;
  try {
    parsed = new URL(String(url));
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  const host = parsed.hostname;
  const loopback = host === "localhost" || host === "127.0.0.1" || host === "[::1]"
    || host.endsWith(".localhost");
  if (!loopback) return null;
  if (!parsed.pathname.startsWith("/assets/")) return null;
  return { origin: parsed.origin, path: parsed.pathname };
}

/**
 * The tracker proper, with its clock and timers injected so the stall detection is
 * testable without waiting 20 seconds. `log` receives complete lines.
 */
function createPaneAssetTracker({
  log,
  now = () => Date.now(),
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (t) => clearTimeout(t),
  stallMs = STALL_MS,
  skipOrigin = "",
  logLinesMax = LOG_LINES_MAX,
} = {}) {
  if (typeof log !== "function") throw new TypeError("log is required");
  /** origin -> { started, done, failed, inflight: Map<requestId, {path, startedAt, timer}> , hadInflight } */
  const origins = new Map();
  // Aggregate line budget. Counters keep counting after it is spent (the snapshot
  // hook still reports them); only the journal goes quiet.
  let linesWritten = 0;
  const emit = (line) => {
    if (linesWritten >= logLinesMax) return;
    linesWritten += 1;
    if (linesWritten === logLinesMax) {
      log(`${PREFIX} budget-exhausted lines=${logLinesMax}; further pane-asset lines suppressed for this session`);
      return;
    }
    log(line);
  };

  const stats = (origin) => {
    let s = origins.get(origin);
    if (!s) {
      if (origins.size >= ORIGINS_MAX) return null;
      s = { started: 0, done: 0, failed: 0, stalled: 0, inflight: new Map(), untracked: 0, hadInflight: false };
      origins.set(origin, s);
    }
    return s;
  };

  const summary = (origin, s) =>
    `origin=${origin} started=${s.started} done=${s.done} failed=${s.failed} inflight=${s.inflight.size + s.untracked}`;

  const onStart = (requestId, url) => {
    const c = classifyUrl(url);
    if (!c || c.origin === skipOrigin) return;
    const s = stats(c.origin);
    if (!s) return;
    s.started += 1;
    s.hadInflight = true;
    if (s.inflight.size >= INFLIGHT_TRACK_MAX) {
      s.untracked += 1;
      return;
    }
    const startedAt = now();
    const timer = setTimer(() => {
      const entry = s.inflight.get(requestId);
      if (!entry) return;
      s.stalled += 1;
      emit(`${PREFIX} ${summary(c.origin, s)} STALLED=${entry.path} after=${now() - entry.startedAt}ms`);
    }, stallMs);
    s.inflight.set(requestId, { path: c.path, startedAt, timer });
  };

  const finish = (requestId, url, outcome) => {
    const c = classifyUrl(url);
    if (!c || c.origin === skipOrigin) return;
    const s = origins.get(c.origin);
    if (!s) return;
    const entry = s.inflight.get(requestId);
    if (entry) {
      clearTimer(entry.timer);
      s.inflight.delete(requestId);
    } else if (s.untracked > 0) {
      s.untracked -= 1;
    } else {
      // A completion for a request we never saw start (attached mid-flight).
      return;
    }
    if (outcome === "done") s.done += 1;
    else s.failed += 1;
    if (s.hadInflight && s.inflight.size === 0 && s.untracked === 0) {
      s.hadInflight = false;
      emit(`${PREFIX} ${summary(c.origin, s)} settled stalled=${s.stalled}`);
    }
  };

  return {
    onStart,
    onCompleted: (requestId, url) => finish(requestId, url, "done"),
    onError: (requestId, url) => finish(requestId, url, "failed"),
    /** Test/inspection hook: a snapshot of one origin's counters. */
    snapshot(origin) {
      const s = origins.get(origin);
      if (!s) return null;
      return { started: s.started, done: s.done, failed: s.failed, stalled: s.stalled, inflight: s.inflight.size + s.untracked };
    },
  };
}

/**
 * Wire the tracker to an Electron `session.webRequest`. Filtered at the source to
 * loopback `/assets/*` URLs so the listeners never see the dashboard's API traffic.
 * Tolerates a missing session so it can never break window creation.
 *
 * Observation only, on the non-blocking events: `onSendHeaders` (the request is
 * on the wire), `onCompleted`, `onErrorOccurred`. None of them takes a callback,
 * so the ~240 chunk fetches of a pane's module graph never wait on a
 * main-process round-trip just to be counted — the path this journal watches is
 * the one the stagger exists to decongest, and a blocking listener would add
 * load to it. The trade: a request that fails BEFORE its headers go out
 * (connection refused) reaches `onErrorOccurred` with no start on record and is
 * ignored, so `failed=` undercounts pre-send failures. The stall this journal
 * exists for is a request that DID go out and never finished; those are all
 * tracked and timed.
 *
 * Listener ownership: Electron keeps ONE listener per `webRequest` event per
 * session, so this module owns `onSendHeaders`, `onCompleted` and
 * `onErrorOccurred` on the pane session it is attached to. A later registration
 * elsewhere on the same session evicts these silently; register through here.
 */
function attachPaneAssetJournal(session, log, dashboardOrigin) {
  const wr = session && session.webRequest;
  if (!wr || typeof wr.onSendHeaders !== "function") return false;
  if (typeof log !== "function") return false;
  let skipOrigin = "";
  try {
    skipOrigin = new URL(String(dashboardOrigin)).origin;
  } catch {
    skipOrigin = "";
  }
  const tracker = createPaneAssetTracker({ log, skipOrigin });
  const filter = {
    urls: [
      "http://localhost:*/assets/*",
      "http://127.0.0.1:*/assets/*",
      "http://*.localhost:*/assets/*",
      "https://localhost:*/assets/*",
      "https://127.0.0.1:*/assets/*",
      "https://*.localhost:*/assets/*",
    ],
  };
  wr.onSendHeaders(filter, (details) => {
    tracker.onStart(details.id, details.url);
  });
  wr.onCompleted(filter, (details) => {
    tracker.onCompleted(details.id, details.url);
  });
  wr.onErrorOccurred(filter, (details) => {
    tracker.onError(details.id, details.url);
  });
  return true;
}

module.exports = {
  INFLIGHT_TRACK_MAX,
  PREFIX,
  attachPaneAssetJournal,
  classifyUrl,
  createPaneAssetTracker,
};
