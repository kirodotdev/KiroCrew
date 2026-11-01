"use strict";
// The release channel the desktop update lane must follow, or "" when the
// Settings preference decides.
//
// An enterprise build follows `stable`, the minimal set of the `updates.channel`
// control (RFC crew-enterprise-edition, D3 and appendix row "Update channel"), the
// same channel the backend's update paths follow (kiro_crew/edition_locks.py).
// The Settings preference cannot widen it, so it replaces the preference
// (auto-update.js resolveChannel) and the switcher refuses (update:set-channel).
// An oss build has no required channel: nothing here changes it.
const { EDITION_ENTERPRISE, readBakedEdition } = require("../../edition");

const ENTERPRISE_UPDATE_CHANNEL = "stable";

/**
 * @param {"oss"|"enterprise"} [edition] the baked edition (edition.js)
 * @returns {() => ""|"stable"|"insider"|"nightly"}
 */
function requiredUpdateChannelFor(edition = readBakedEdition()) {
  const required = edition === EDITION_ENTERPRISE ? ENTERPRISE_UPDATE_CHANNEL : "";
  return () => required;
}

module.exports = { ENTERPRISE_UPDATE_CHANNEL, requiredUpdateChannelFor };
