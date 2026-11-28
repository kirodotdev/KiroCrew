"use strict";

/**
 * The words a relay Crew tab (or window) shows once it stops waiting for its Crew.
 *
 * Main-process copy, so English only: the dashboard's catalogs and i18n gates
 * cover the dashboard renderer, not Electron main (website/docs/i18n-catalog.md,
 * "Scope: the dashboard renderer").
 *
 * The lost-access notice says only what the refusal says, that access is not
 * granted. It does not say who changed it or why.
 */

const NOTICE = Object.freeze({
  UNAVAILABLE: "unavailable",
  NO_ACCESS: "no_access",
  REMOTE_OFF: "remote_off",
  REFUSED: "refused",
});

/** The renewal stop codes that have a notice of their own. */
const NOTICE_FOR_CODE = Object.freeze({
  CREW_OFFLINE: NOTICE.UNAVAILABLE,
  CREW_ACCESS_NOT_GRANTED: NOTICE.NO_ACCESS,
  ACCESS_DENIED: NOTICE.NO_ACCESS,
  REMOTE_ACCESS_DISABLED: NOTICE.REMOTE_OFF,
  // The relay refusing the page twice in a row, for a reason it does not name.
  SIGN_IN_FAILED: NOTICE.REFUSED,
});

/** `{ title, body, check, close }`; an empty string hides that part. */
function noticeText(kind, name) {
  const crew = typeof name === "string" && name.trim() ? name.trim() : "This Crew";
  if (kind === NOTICE.REMOTE_OFF) {
    return {
      title: `${crew}'s owner turned Remote Control off.`,
      body: "Once they turn it on again, press Check again.",
      check: "Check again",
      close: "Close",
    };
  }
  if (kind === NOTICE.REFUSED) {
    return {
      title: `${crew} refused access to this window.`,
      body: "Press Check again to try once more. If it keeps refusing, ask the owner whether you still have access.",
      check: "Check again",
      close: "Close",
    };
  }
  if (kind === NOTICE.NO_ACCESS) {
    return {
      title: `You no longer have access to ${crew}.`,
      body: "To get access back, ask your admin.",
      check: "",
      close: "Close",
    };
  }
  return {
    title: `${crew} is currently unavailable`,
    body: "The machine running it is offline, or Kiro Crew is not running there. Once it is back, press Check again.",
    check: "Check again",
    close: "Close",
  };
}

module.exports = { NOTICE, NOTICE_FOR_CODE, noticeText };
