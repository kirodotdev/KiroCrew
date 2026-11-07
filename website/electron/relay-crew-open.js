"use strict";

/**
 * Open a remote Crew reached through the Kiro relay, from the dashboard's
 * `relayCrewAPI.open(endpointId)`.
 *
 * Wires three parts together: the gateway token client (relay-crew-client.js),
 * the Crew window (relay-crew-window.js) and token renewal
 * (relay-crew-renewal.js). The first token is minted before any window exists,
 * so a refused Crew opens nothing. One window per Crew: opening an open Crew
 * focuses it, and a double click mints once.
 *
 * Answers `{ ok: true }` or `{ ok: false, error: <code> }`, never a token, a
 * Crew URL or an expiry. Log lines name the endpoint and the outcome only.
 */

// The relay's endpoint ID grammar: one lower-case DNS label.
const ENDPOINT_ID_RE = /^[a-z0-9]{20,63}$/;

// Client codes that renewal must read differently from the open call: the
// feature turned off stops renewal, and a gateway that can't be reached (or
// refused this shell's secret, which rotates per gateway start) backs off.
const RENEWAL_CODE = Object.freeze({ RELAY_VIEWER_OFF: "NOT_SET_UP", UNAVAILABLE: "UNREACHABLE" });

function isEndpointId(value) {
  return typeof value === "string" && ENDPOINT_ID_RE.test(value);
}

function createRelayCrewOpener({ requestToken, openWindow, createRenewal, createTokenHolder, log }) {
  // endpointId -> { win, holder, renewal } for each open window.
  const windows = new Map();
  const opening = new Map();
  const say = (endpointId, outcome) => {
    try { if (log) log(`relay crew ${endpointId}: open -> ${outcome}`); } catch { /* never load-bearing */ }
  };

  // A renewal on `holder` and its first mint. Answers the renewal, or the code
  // the dashboard sees for a refused first mint.
  async function startRenewal(endpointId, holder, crew) {
    let firstCode = null;
    const mint = async () => {
      const answer = await requestToken(endpointId);
      if (answer && answer.ok) {
        crew.crewUrl = answer.crewUrl;
        crew.name = answer.name;
        return answer.grant;
      }
      const code = (answer && answer.code) || "UNAVAILABLE";
      if (firstCode === null) firstCode = code;
      return { code: RENEWAL_CODE[code] || code, retryAfter: answer ? answer.retryAfter : null };
    };
    const renewal = createRenewal({ endpointId, mint, holder, log });
    const first = await renewal.start();
    if (first && first.ok) return { renewal };
    return { error: firstCode || (first && first.code) || "UNAVAILABLE" };
  }

  function wire(entry) {
    entry.renewal.attach(entry.win);
    entry.win.onVisibilityChange((hidden) => entry.renewal.setHidden(hidden));
  }

  async function openNew(endpointId) {
    const holder = createTokenHolder();
    const crew = { crewUrl: "", name: "" };
    const { renewal, error } = await startRenewal(endpointId, holder, crew);
    if (!renewal) {
      say(endpointId, error);
      return { ok: false, error };
    }
    let win;
    try {
      win = openWindow({ endpointId, crewUrl: crew.crewUrl, title: crew.name, getToken: holder.current });
      wire({ win, renewal });
      win.onClosed(() => { if (windows.get(endpointId)?.win === win) windows.delete(endpointId); });
    } catch {
      renewal.stop();
      if (win) win.close();
      say(endpointId, "UNAVAILABLE");
      return { ok: false, error: "UNAVAILABLE" };
    }
    windows.set(endpointId, { win, holder, renewal });
    say(endpointId, "opened");
    return { ok: true };
  }

  // An open window whose renewal stopped (the Crew went offline, the sign-in
  // ended): mint again on the same holder, which the window already reads,
  // and reload. A refusal leaves the window as it is and answers its code.
  async function restart(endpointId, entry) {
    const { renewal, error } = await startRenewal(endpointId, entry.holder, {});
    if (!renewal) {
      say(endpointId, error);
      return { ok: false, error };
    }
    // The window closed while the mint was in flight: its close listeners have
    // already fired, so this renewal would never stop.
    if (entry.win.isClosed() || windows.get(endpointId) !== entry) {
      renewal.stop();
      say(endpointId, "UNAVAILABLE");
      return { ok: false, error: "UNAVAILABLE" };
    }
    entry.renewal = renewal;
    wire(entry);
    entry.win.reload();
    entry.win.focus();
    say(endpointId, "renewed");
    return { ok: true };
  }

  function once(endpointId, run) {
    if (opening.has(endpointId)) return opening.get(endpointId);
    const pending = run()
      .catch(() => ({ ok: false, error: "UNAVAILABLE" }))
      .finally(() => opening.delete(endpointId));
    opening.set(endpointId, pending);
    return pending;
  }

  return {
    async open(endpointId) {
      if (!isEndpointId(endpointId)) return { ok: false, error: "INVALID_ENDPOINT" };
      const existing = windows.get(endpointId);
      if (existing && !existing.win.isClosed()) {
        if (existing.renewal.isStopped()) return once(endpointId, () => restart(endpointId, existing));
        existing.win.focus();
        say(endpointId, "focused");
        return { ok: true };
      }
      return once(endpointId, () => openNew(endpointId));
    },
  };
}

module.exports = { createRelayCrewOpener, isEndpointId, ENDPOINT_ID_RE };
