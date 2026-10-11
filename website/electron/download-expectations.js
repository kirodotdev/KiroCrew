"use strict";

// A short-lived, single-use registry of downloads the dashboard has EXPLICITLY
// asked for.
//
// The `will-download` handler (download-handler.js) auto-saves a download to
// Downloads with no dialog. Letting it save every same-origin / dashboard-blob
// download is too broad: anything that renders in the dashboard origin — a
// rendered artifact, a preview, markdown, an embedded resource — could mint a
// same-origin or blob download and have it land silently in Downloads without
// the user ever asking. The only downloads that should auto-save are the ones
// the app's own export path deliberately starts.
//
// So the renderer's `downloadBlob` helper (the export button's path) announces
// each download BEFORE it clicks the link — "expect one download named <name>"
// — over the preload/IPC bridge. That registers an expectation here. The
// handler then auto-saves ONLY a download that matches a pending expectation
// (same origin + same leaf filename) and consumes it; a second download of the
// same name, a download nobody announced, or one that arrives after the window
// has elapsed does NOT match, and falls back to Chromium's default handling
// (the Save dialog / normal behaviour) rather than a silent save.
//
// Properties that make this a real narrowing and not a rubber stamp:
//   - single use: a claim consumes the expectation, so one announcement authors
//     exactly one silent save (a replay/duplicate download is not auto-saved);
//   - time-boxed: an expectation expires after `ttlMs` (default 30s), so a
//     stale announcement cannot authorise a download minted much later;
//   - origin-scoped: an expectation is keyed by the announcing frame's origin
//     and only matches a download whose origin is the same, so one origin
//     cannot pre-authorise another's download.
//
// `now` is injected so the TTL is testable without real timers.

const DEFAULT_TTL_MS = 30_000;
// Bound the registry so a renderer that announces without ever downloading
// cannot grow it without limit; the oldest live expectation is dropped first.
const MAX_PENDING = 64;

function createDownloadExpectations({ now = () => Date.now(), ttlMs = DEFAULT_TTL_MS } = {}) {
  // Each entry: { origin, filename, expiresAt }. A plain array (not a Map keyed
  // by name) because two different origins may legitimately expect the same
  // filename, and each claim must consume exactly one entry.
  let pending = [];

  function prune(t) {
    pending = pending.filter((e) => e.expiresAt > t);
  }

  /**
   * Register one expectation. Returns nothing; a malformed origin/filename is
   * ignored (fails closed — no expectation is created, so nothing auto-saves).
   *
   * @param {object} args
   * @param {string} args.origin - the announcing frame's `.origin`
   * @param {string} args.filename - the leaf filename the download will carry
   */
  function register({ origin, filename } = {}) {
    const t = now();
    prune(t);
    const o = String(origin || "");
    const f = String(filename || "");
    // An opaque / empty origin or an empty name can never be matched safely.
    if (!o || o === "null" || !f) return;
    pending.push({ origin: o, filename: f, expiresAt: t + ttlMs });
    // Keep the newest MAX_PENDING; drop the oldest when over.
    if (pending.length > MAX_PENDING) {
      pending = pending.slice(pending.length - MAX_PENDING);
    }
  }

  /**
   * Claim (consume) a matching live expectation. Returns true and removes
   * exactly one entry when a live expectation matches BOTH the origin and the
   * filename; returns false otherwise (nothing is consumed).
   *
   * @param {object} args
   * @param {string} args.origin - the download's effective origin
   * @param {string} args.filename - the download's leaf filename
   * @returns {boolean}
   */
  function claim({ origin, filename } = {}) {
    const t = now();
    prune(t);
    const o = String(origin || "");
    const f = String(filename || "");
    if (!o || o === "null" || !f) return false;
    const idx = pending.findIndex((e) => e.origin === o && e.filename === f);
    if (idx === -1) return false;
    pending.splice(idx, 1);
    return true;
  }

  /** Test/diagnostic: number of live (unexpired) expectations. */
  function size() {
    prune(now());
    return pending.length;
  }

  return { register, claim, size };
}

module.exports = { createDownloadExpectations, DEFAULT_TTL_MS, MAX_PENDING };
