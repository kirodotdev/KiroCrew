"use strict";

/**
 * The start-time "is a gateway still up" question, answered without trusting an
 * identity judgement that can say "no" for the wrong reason.
 *
 * isCurrentBundleGateway() is false when the listener is another bundle's, but
 * also when it cannot tell at all (Windows, an unpackaged run, an lsof or ps
 * that failed, more than one listener). Only the first of those means idle, so a
 * "no" is followed by a plain port probe: anything listening, or a probe that
 * cannot run, counts as a gateway that may be running, and the apply waits.
 *
 * A free port does not mean idle either: a gateway that is still booting has not
 * bound its listener yet, and one that is draining has already closed it, and
 * both still hold gateway.lock. The socket-independent lock probe has the last
 * word: only a lock reported free ("released") lets the apply run. A held lock,
 * a probe that cannot answer, or a missing probe all keep the arm.
 *
 * @param {object} deps
 * @param {() => Promise<boolean>} deps.isCurrentBundleGateway
 * @param {() => Promise<"bound"|"free"|"unknown">} deps.probePortBinding
 * @param {() => Promise<{verdict: string}>} deps.probeLockHolder
 * @param {string} deps.stopCommand shown when the apply is deferred
 * @returns {() => Promise<{running: boolean, stopCommand: string}>}
 */
function createStartupGatewayProbe({ isCurrentBundleGateway, probePortBinding, probeLockHolder = null, stopCommand }) {
  return async function probeGateway() {
    if (await isCurrentBundleGateway()) return { running: true, stopCommand };
    let binding = "unknown";
    try { binding = await probePortBinding(); } catch { binding = "unknown"; }
    if (binding !== "free") return { running: true, stopCommand };
    let verdict = "unknown";
    try { verdict = probeLockHolder ? (await probeLockHolder()).verdict : "unknown"; } catch { verdict = "unknown"; }
    return { running: verdict !== "released", stopCommand };
  };
}

module.exports = { createStartupGatewayProbe };
