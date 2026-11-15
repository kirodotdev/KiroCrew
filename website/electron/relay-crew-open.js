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
 *
 * When any renewal stops on SIGNED_OUT (the gateway reads the Kiro sign-in on
 * every mint), every renewal stops and every relay Crew window closes. This is
 * noticed at the next renewal, not at the moment of sign-out.
 *
 * An account switch is caught per window: each window keeps the gateway's
 * non-secret sign-in marker from its first grant and sends it on every
 * renewal. The gateway refuses SIGN_IN_CHANGED when the vault now holds
 * another account, and that one window closes at its own next renewal.
 *
 * A window stops waiting for its Crew, and shows a notice in place of the
 * Crew's page, when renewal stops on CREW_OFFLINE or CREW_ACCESS_NOT_GRANTED,
 * or when the window reports the Crew unreachable (its retries ran out). The
 * renewal is stopped either way, so nothing retries behind the notice. Check
 * again is one fresh attempt through `restart`; a refusal puts the notice back.
 * Access refusals are noticed at the next renewal, up to 240 s after the change.
 */

// The relay's endpoint ID grammar has one definition, in the window module.
const { isEndpointId } = require("./relay-crew-window");
const { NOTICE, NOTICE_FOR_CODE, noticeText } = require("./relay-crew-notice");

// Client codes that renewal must read differently from the open call: the
// feature turned off stops renewal, and a gateway that can't be reached (or
// refused this shell's secret, which rotates per gateway start) backs off.
const RENEWAL_CODE = Object.freeze({ RELAY_VIEWER_OFF: "NOT_SET_UP", UNAVAILABLE: "UNREACHABLE" });

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
      // This window's own sign-in marker, from its first grant. The gateway
      // refuses SIGN_IN_CHANGED when the vault now holds another account. No
      // marker is shared between windows.
      const answer = await requestToken(endpointId, crew.signInMarker || null);
      if (answer && answer.ok) {
        crew.crewUrl = answer.crewUrl;
        crew.name = answer.name;
        if (!crew.signInMarker && answer.signInMarker) crew.signInMarker = answer.signInMarker;
        return answer.grant;
      }
      const code = (answer && answer.code) || "UNAVAILABLE";
      if (firstCode === null) firstCode = code;
      return { code: RENEWAL_CODE[code] || code, retryAfter: answer ? answer.retryAfter : null };
    };
    const onOutcome = (o) => {
      if (!o || o.stopped !== true) return;
      if (o.code === "SIGNED_OUT") closeAll("SIGNED_OUT");
      // An account switch closes only the window this renewal drives.
      if (o.code === "SIGN_IN_CHANGED") closeOne(endpointId, renewal, "SIGN_IN_CHANGED");
      const kind = NOTICE_FOR_CODE[o.code];
      if (kind) {
        const entry = windows.get(endpointId);
        if (entry && entry.renewal === renewal) showNotice(endpointId, entry, kind);
      }
    };
    const renewal = createRenewal({ endpointId, mint, holder, onOutcome, log });
    const first = await renewal.start();
    if (first && first.ok) return { renewal };
    // A refused first mint emits no outcome, so a sign-out it finds closes here.
    if (firstCode === "SIGNED_OUT") closeAll("SIGNED_OUT");
    return { error: firstCode || (first && first.code) || "UNAVAILABLE" };
  }

  // Stop every renewal (which clears its token holder) and close every window.
  // A mint still in flight when this runs finds `epoch` (a new open) or its
  // entry (a restart) changed, and stops its own renewal.
  let epoch = 0;
  function closeAll(reason) {
    epoch += 1;
    const entries = [...windows];
    windows.clear();
    for (const [endpointId, entry] of entries) {
      try { entry.renewal.stop(); } catch { /* still close the window */ }
      try { entry.win.close(); } catch { /* already gone */ }
      say(endpointId, `closed (${reason})`);
    }
  }

  // Close one window, only while `renewal` still drives it. Its close
  // listener removes the entry.
  function closeOne(endpointId, renewal, reason) {
    const entry = windows.get(endpointId);
    if (!entry || entry.renewal !== renewal) return;
    try { entry.win.close(); } catch { /* already gone */ }
    say(endpointId, `closed (${reason})`);
  }

  // Replace the Crew's page with a notice. The name is the Crew's own, shown
  // only in the window and never logged.
  function showNotice(endpointId, entry, kind) {
    entry.notice = kind;
    try { entry.win.showNotice(noticeText(kind, entry.crew && entry.crew.name)); } catch { /* window gone */ }
    say(endpointId, `notice (${kind})`);
  }

  function wire(entry) {
    entry.renewal.attach(entry.win);
    entry.win.onVisibilityChange((hidden) => entry.renewal.setHidden(hidden));
  }

  async function openNew(endpointId) {
    const holder = createTokenHolder();
    const crew = { crewUrl: "", name: "", signInMarker: null };
    const at = epoch;
    const { renewal, error } = await startRenewal(endpointId, holder, crew);
    if (!renewal) {
      say(endpointId, error);
      return { ok: false, error };
    }
    // Everything closed while the mint was in flight: open nothing.
    if (epoch !== at) {
      renewal.stop();
      say(endpointId, "UNAVAILABLE");
      return { ok: false, error: "UNAVAILABLE" };
    }
    let win;
    try {
      win = openWindow({ endpointId, crewUrl: crew.crewUrl, title: crew.name, getToken: holder.current });
      wire({ win, renewal });
      win.onClosed(() => { if (windows.get(endpointId)?.win === win) windows.delete(endpointId); });
      // Registered once per window, not in wire(): a restart re-wires the same window.
      win.onUnreachable(() => {
        const entry = windows.get(endpointId);
        if (!entry || entry.win !== win) return;
        try { entry.renewal.stop(); } catch { /* still show the notice */ }
        showNotice(endpointId, entry, NOTICE.UNAVAILABLE);
      });
      win.onNoticeAction((action) => {
        const entry = windows.get(endpointId);
        if (!entry || entry.win !== win) return;
        if (action === "close") { try { win.close(); } catch { /* already gone */ } return; }
        if (action === "check" && entry.renewal.isStopped()) once(endpointId, () => restart(endpointId, entry));
      });
    } catch {
      renewal.stop();
      if (win) win.close();
      say(endpointId, "UNAVAILABLE");
      return { ok: false, error: "UNAVAILABLE" };
    }
    windows.set(endpointId, { win, holder, renewal, crew });
    say(endpointId, "opened");
    return { ok: true };
  }

  // An open window whose renewal stopped (the Crew went offline, the sign-in
  // ended): mint again on the same holder, which the window already reads,
  // and reload. A refusal answers its code and leaves the window as it is,
  // except that a refusal with a notice of its own shows it, and a notice
  // already up is shown again so Check again visibly tried.
  async function restart(endpointId, entry) {
    // The same window, so the same sign-in: its first mint sends its marker.
    const crew = { signInMarker: entry.crew ? entry.crew.signInMarker : null };
    const { renewal, error } = await startRenewal(endpointId, entry.holder, crew);
    if (!renewal) {
      // The account changed since the window opened: close it, and the next
      // click opens as the new account.
      if (error === "SIGN_IN_CHANGED" && windows.get(endpointId) === entry) {
        try { entry.win.close(); } catch { /* already gone */ }
        say(endpointId, "closed (SIGN_IN_CHANGED)");
        return { ok: false, error };
      }
      say(endpointId, error);
      const kind = NOTICE_FOR_CODE[error] || entry.notice;
      if (kind && windows.get(endpointId) === entry && !entry.win.isClosed()) {
        // The name from the window's first grant: this refused mint brought none.
        showNotice(endpointId, entry, kind);
      }
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
    entry.crew = crew;
    entry.notice = null;
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

module.exports = { createRelayCrewOpener, isEndpointId };
