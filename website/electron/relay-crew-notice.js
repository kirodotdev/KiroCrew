"use strict";

/**
 * The words a relay Crew window shows once it stops waiting for its Crew.
 *
 * Main-process copy, so English only: the dashboard's catalogs and i18n gates
 * cover the dashboard renderer, not Electron main (website/docs/i18n-catalog.md,
 * "Scope: the dashboard renderer").
 *
 * The lost-access notice says only what the refusal says, that access is not
 * granted. It does not say who changed it or why. The access-off notice says
 * only that the owner turned access off, which is what REMOTE_ACCESS_DISABLED
 * means.
 */

const NOTICE = Object.freeze({
  UNAVAILABLE: "unavailable",
  NO_ACCESS: "no_access",
  ACCESS_OFF: "access_off",
});

/** The renewal stop codes that have a notice of their own. */
const NOTICE_FOR_CODE = Object.freeze({
  CREW_OFFLINE: NOTICE.UNAVAILABLE,
  CREW_ACCESS_NOT_GRANTED: NOTICE.NO_ACCESS,
  REMOTE_ACCESS_DISABLED: NOTICE.ACCESS_OFF,
});

/** `{ title, body, check, close }`; an empty string hides that part. */
function noticeText(kind, name) {
  const crew = typeof name === "string" && name.trim() ? name.trim() : "This Crew";
  if (kind === NOTICE.NO_ACCESS) {
    return {
      title: `You no longer have access to ${crew}.`,
      body: "To get access back, ask your admin.",
      check: "",
      close: "Close window",
    };
  }
  if (kind === NOTICE.ACCESS_OFF) {
    return {
      title: `${crew}'s owner turned off access`,
      body: "You can open it again when they turn it back on.",
      check: "Check again",
      close: "Close window",
    };
  }
  return { title: `${crew} is currently unavailable`, body: "", check: "Check again", close: "Close window" };
}

module.exports = { NOTICE, NOTICE_FOR_CODE, noticeText };
