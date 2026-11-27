"use strict";

/**
 * Another local dashboard window (Connection > New Window) opening a relay
 * Crew: the Crew shows as a tab in the main window, so the open is handed to
 * the main window's dashboard, which opens it as its own click would.
 *
 * Only the endpoint ID crosses. The main dashboard says when it listens
 * (`listen`), and a new document in it (a reload, a crash) or its end stops
 * that, so nothing is handed to a page that can't hear it and nothing is held
 * for later: with no listener the asking window gets a refusal to show.
 *
 * Each `listen` call passes an async gate before it lands, so an unsubscribe
 * and the subscribe right after it can land in either order. The page numbers
 * its calls and only its newest counts.
 *
 * The main window is raised in the same call as the click, through
 * `showMainWindow`, which also cancels a pending hide to the tray.
 */

const OPEN_REQUEST = "relay-crew:open-request";

function createRelayCrewHandoff({ showMainWindow }) {
  let target = null;
  // wc -> the newest call number seen from its current document.
  const seqs = new WeakMap();
  const watched = new WeakSet();
  // One set of listeners per dashboard, however often it resubscribes.
  const watch = (wc) => {
    if (watched.has(wc)) return;
    watched.add(wc);
    const reset = () => {
      seqs.delete(wc);
      if (target === wc) target = null;
    };
    wc.on("did-navigate", reset);
    wc.on("render-process-gone", reset);
    wc.once("destroyed", reset);
  };
  return {
    // `wc` is the main window's dashboard, already checked by the caller.
    listen(wc, on, seq) {
      watch(wc);
      if (Number.isSafeInteger(seq)) {
        if (seqs.has(wc) && seq <= seqs.get(wc)) return;
        seqs.set(wc, seq);
      }
      if (on === true) target = wc;
      else if (target === wc) target = null;
    },
    // `{ ok: true, window: true }` once handed over (the asking window keeps
    // no tab), or a refusal when the main dashboard is not listening.
    handOff(endpointId) {
      if (!target || target.isDestroyed()) return { ok: false, error: "UNAVAILABLE" };
      showMainWindow({ focus: true });
      target.send(OPEN_REQUEST, endpointId);
      return { ok: true, window: true };
    },
  };
}

module.exports = { createRelayCrewHandoff };
