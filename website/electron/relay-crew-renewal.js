"use strict";

/**
 * Access token renewal for a relay Crew window.
 *
 * The window (`relay-crew-window.js`) adds the newest token from a TokenHolder
 * to every request for the Crew's origin. This module owns that holder: it
 * mints a token, sets it, renews it before it ends, mints again once on a
 * main-frame 401, and clears it when the window closes or access is refused.
 *
 * `mint` is injected and resolves either a grant
 * `{ accessToken, expiresAt, renewInSeconds, serverDate }` (epoch seconds on
 * the service's clock) or a refusal `{ code, retryAfter }` with the control
 * plane client's typed code. Outcomes and log lines never carry the token.
 *
 * Not built: stopping on the relay's WebSocket close 4410. Main can't see
 * WebSocket close codes and the window has no preload, so a turned-off Crew is
 * noticed at the next mint, which then refuses.
 */

const RENEW_MIN_S = 5;
const RENEW_MAX_S = 240;
/** Lead before the token ends when the answer names no renewInSeconds. */
const RENEW_DEFAULT_LEAD_S = 60;
/** Each renewal comes up to this much early, so windows don't renew in step. */
const RENEW_SPREAD_S = 5;
/** The host lease's backoff steps (relay_client/backoff.py), with full jitter. */
const BACKOFF_STEPS_S = [2, 5, 15, 30, 60];
/** A second main-frame 401 this soon after the first stops the window. */
const UNAUTHORIZED_WINDOW_MS = 30_000;

/**
 * Refusals that stop renewal with no automatic retry: none of them changes
 * until the person acts (the grant, the account, the sign-in, the settings or
 * the Crew coming back online). Only UNREACHABLE, SERVICE_ERROR and
 * BAD_ANSWER back off and retry, and THROTTLED retries once first.
 */
const STOP_REFUSALS = Object.freeze([
  "CREW_ACCESS_NOT_GRANTED",
  "REMOTE_ACCESS_DISABLED",
  "ACCESS_DENIED",
  "CREW_OFFLINE",
  "SIGNED_OUT",
  "SIGN_IN_CHANGED",
  "NOT_SET_UP",
  "INVALID_REQUEST",
]);
const OUTCOME = Object.freeze({
  ...Object.fromEntries(STOP_REFUSALS.map((code) => [code, code])),
  SIGN_IN_FAILED: "SIGN_IN_FAILED",
  CLOSED: "CLOSED",
  STOPPED: "STOPPED",
  RENEW_FAILING: "RENEW_FAILING",
  RENEWED: "RENEWED",
});

/** Milliseconds until the next renewal, clamped to 5 to 240 s. */
function renewDelayMs({ renewInSeconds, lifetimeSeconds }, random = Math.random) {
  const s = renewInSeconds > 0
    ? renewInSeconds - random() * RENEW_SPREAD_S
    : lifetimeSeconds - RENEW_DEFAULT_LEAD_S;
  const clamped = Math.min(RENEW_MAX_S, Math.max(RENEW_MIN_S, Number.isFinite(s) ? s : RENEW_MIN_S));
  return Math.round(clamped * 1000);
}

function asGrant(r) {
  if (!r || typeof r.accessToken !== "string" || !r.accessToken || !Number.isFinite(r.expiresAt)) return null;
  return r;
}

function createRelayCrewRenewal({ endpointId, mint, holder, onOutcome, log, random = Math.random }) {
  let stopped = false;
  let started = false;
  let win = null;
  let timer = null;
  let inFlight = null;
  let reloadPending = false;
  let firstUnauthorizedAt = null;
  let hidden = false;
  let hiddenRenewals = 0;
  let parked = false;
  let backoffAttempt = 0;
  let throttleRetried = false;
  let failing = false;

  const say = (msg) => { try { if (log) log(`relay crew ${endpointId}: ${msg}`); } catch { /* logging never breaks renewal */ } };
  const emit = (code, isStopped, refusal) => {
    const outcome = refusal ? { code, refusal, stopped: isStopped } : { code, stopped: isStopped };
    try { if (onOutcome) onOutcome(outcome); } catch { /* a bad callback never breaks renewal */ }
  };
  const clearTimer = () => { if (timer !== null) clearTimeout(timer); timer = null; };
  const schedule = (ms) => { clearTimer(); timer = setTimeout(onTimer, ms); };

  function halt() {
    stopped = true;
    clearTimer();
    reloadPending = false;
    holder.clear();
  }
  function finish(code) {
    if (stopped) return;
    halt();
    say(`stopped (${code})`);
    emit(code, true);
  }
  function nextBackoffMs() {
    const step = BACKOFF_STEPS_S[Math.min(backoffAttempt, BACKOFF_STEPS_S.length - 1)];
    backoffAttempt += 1;
    return Math.round(random() * step * 1000);
  }

  function handle(r) {
    const grant = asGrant(r);
    if (grant) {
      started = true;
      holder.set(grant.accessToken);
      backoffAttempt = 0;
      throttleRetried = false;
      if (failing) { failing = false; emit(OUTCOME.RENEWED, false); }
      if (reloadPending && win) { reloadPending = false; win.reload(); }
      // shortcut: with no Date header the lifetime falls back to the device clock.
      const base = Number.isFinite(grant.serverDate) ? grant.serverDate : Date.now() / 1000;
      const ms = renewDelayMs({ renewInSeconds: grant.renewInSeconds, lifetimeSeconds: grant.expiresAt - base }, random);
      schedule(ms);
      say(`token renewed, next in ${Math.round(ms / 1000)}s`);
      return { ok: true };
    }
    const code = r && typeof r.code === "string" && r.code ? r.code : "BAD_ANSWER";
    if (!started) { halt(); say(`first mint refused (${code})`); return { ok: false, code }; }
    if (STOP_REFUSALS.includes(code)) { finish(code); return { ok: false, code }; }
    let ms;
    if (code === "THROTTLED" && !throttleRetried) {
      throttleRetried = true;
      ms = r.retryAfter > 0 ? Math.round(r.retryAfter * 1000) : nextBackoffMs();
    } else {
      ms = nextBackoffMs();
    }
    if (!failing) { failing = true; emit(OUTCOME.RENEW_FAILING, false, code); }
    say(`renewal refused (${code}), retry in ${Math.round(ms / 1000)}s`);
    schedule(ms);
    return { ok: false, code };
  }

  function runMint() {
    if (inFlight) return inFlight;
    clearTimer();
    inFlight = (async () => {
      let r;
      try { r = await mint(); } catch { r = { code: "UNREACHABLE" }; }
      inFlight = null;
      if (stopped) return { ok: false, code: OUTCOME.STOPPED };
      return handle(r);
    })();
    return inFlight;
  }

  function onTimer() {
    timer = null;
    if (stopped) return;
    if (hidden) {
      if (hiddenRenewals >= 1) { parked = true; say("hidden, renewal parked"); return; }
      hiddenRenewals += 1;
    }
    runMint();
  }

  function onUnauthorized() {
    if (stopped) return;
    const t = Date.now();
    if (firstUnauthorizedAt !== null && t - firstUnauthorizedAt <= UNAUTHORIZED_WINDOW_MS) {
      finish(OUTCOME.SIGN_IN_FAILED);
      return;
    }
    firstUnauthorizedAt = t;
    say("main frame 401, minting again");
    reloadPending = true;
    parked = false;
    runMint();
  }

  return {
    start: () => (stopped ? Promise.resolve({ ok: false, code: OUTCOME.STOPPED }) : runMint()),
    attach(w) {
      win = w;
      w.onClosed(() => finish(OUTCOME.CLOSED));
      w.onMainFrameStatus((status) => { if (status === 401) onUnauthorized(); });
    },
    setHidden(h) {
      if (h && !hidden) hiddenRenewals = 0;
      hidden = !!h;
      if (!hidden && parked && !stopped) { parked = false; runMint(); }
    },
    stop: () => finish(OUTCOME.STOPPED),
    isStopped: () => stopped,
  };
}

module.exports = { createRelayCrewRenewal, renewDelayMs, OUTCOME, STOP_REFUSALS };
