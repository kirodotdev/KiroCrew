"use strict";

/**
 * The desktop app's record of an update the user armed.
 *
 * Arming is the only thing a click does: the apply runs on the user's own quit,
 * in Electron main, after the gateway has fully stopped. The record is kept in
 * the app's own store, not in the gateway's state, because the gateway is down
 * when the apply runs and because a quit-time apply that was interrupted or
 * failed must still be there at the next app start.
 *
 * The record names the version that was armed. It is cleared by the code that
 * finished the apply (or gave up on it); nothing here decides that.
 */

const ARM_KEY = "updateArm";
const APPLY_ERROR_KEY = "updateApplyError";
const GOVERNED_KEY = "updatePolicyGoverned";

function createUpdateArm({ store, now = () => Date.now() }) {
  const readKey = (key) => {
    try { return store.get(key, null); } catch { return null; }
  };
  // `strict` lets a failed write reach the caller; the rest are best-effort.
  const writeKey = (key, value, { strict = false } = {}) => {
    try {
      if (value === null) {
        if (typeof store.delete === "function") store.delete(key);
        else store.set(key, null);
      } else {
        store.set(key, value);
      }
    } catch (err) {
      if (strict) throw err;
      /* an unwritable store leaves the record as it was */
    }
  };

  /** @returns {{version: string, armedAt: string, attempts: number}|null} */
  function read() {
    const rec = readKey(ARM_KEY);
    if (!rec || typeof rec.version !== "string" || !rec.version) return null;
    return {
      version: rec.version,
      armedAt: typeof rec.armedAt === "string" ? rec.armedAt : "",
      attempts: Number.isInteger(rec.attempts) ? rec.attempts : 0,
    };
  }

  /**
   * Arm `version`. Re-arming the same version keeps its original time and attempts.
   * Throws when the record cannot be written: a click that reports "armed" without
   * a record would leave the user quitting into an update that never applies.
   */
  function arm(version) {
    const existing = read();
    if (existing && existing.version === version) return existing;
    const rec = { version, armedAt: new Date(now()).toISOString(), attempts: 0 };
    writeKey(ARM_KEY, rec, { strict: true });
    return rec;
  }

  /** Count one apply attempt BEFORE it runs, so an interrupted run is on record. */
  function markAttempt() {
    const rec = read();
    if (!rec) return null;
    const next = { ...rec, attempts: rec.attempts + 1 };
    writeKey(ARM_KEY, next);
    return next;
  }

  function clear() {
    writeKey(ARM_KEY, null);
  }

  /** Keep the failure of a start-time apply for the lane to show once. */
  function recordApplyError(version, message) {
    writeKey(APPLY_ERROR_KEY, { version, message: String(message || ""), at: new Date(now()).toISOString() });
  }

  /** Read and forget the recorded start-time apply failure. */
  function takeApplyError() {
    const rec = readKey(APPLY_ERROR_KEY);
    if (!rec || typeof rec.message !== "string") return null;
    writeKey(APPLY_ERROR_KEY, null);
    return { version: typeof rec.version === "string" ? rec.version : "", message: rec.message };
  }

  /**
   * Remember whether a security policy provider governed this install's updates
   * the last time the app knew. The verdict arrives from the dashboard, so a
   * start-time apply, which runs before any window exists, can only read the
   * last one. Written on change only.
   */
  function recordGoverned(governed) {
    const next = governed === true;
    if (readKey(GOVERNED_KEY) === next) return;
    writeKey(GOVERNED_KEY, next);
  }

  /** The last recorded governance verdict; false when none was ever recorded. */
  function wasGoverned() {
    return readKey(GOVERNED_KEY) === true;
  }

  return { read, arm, markAttempt, clear, recordApplyError, takeApplyError, recordGoverned, wasGoverned };
}

module.exports = { createUpdateArm, ARM_KEY, APPLY_ERROR_KEY, GOVERNED_KEY };
