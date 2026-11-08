"use strict";
// Which edition this desktop build is: "oss" or "enterprise".
//
// Fixed when the app is BUILT, never at run time, from the same stamp the
// backend's kiro_crew/edition.py gets: packaging/build-desktop.sh reads
// KIROCREW_BUILD_EDITION (set by `make desktop EDITION=enterprise`) and, for an
// enterprise build only, stages the file EDITION next to this module. package.json
// `files` packs it into app.asar, so it is trusted as code is trusted. An oss
// build stages nothing and packs nothing. No environment variable or config
// file is read here.
//
// Anything but a regular file whose text is exactly "enterprise" (trailing
// newline allowed) reads as "oss". An enterprise build changes two things here:
// a longer gateway wait, and the update channel lock
// (runtime/update/policy-channel.js). A damaged file therefore also reopens the
// Settings channel switcher. The file is packed into the signed, write-protected
// app.asar (RFC crew-enterprise-edition, Security considerations), so damaging
// it means modifying the signed app itself.
const fs = require("fs");
const path = require("path");

const BAKED_EDITION_NAME = "EDITION";
const EDITION_ENTERPRISE = "enterprise";
const EDITION_OSS = "oss";
const EDITION_MAX_BYTES = 64;

/**
 * @param {object} [o]
 * @param {string} [o.bakedPath]
 * @returns {"oss"|"enterprise"}
 */
function readBakedEdition({ bakedPath = path.join(__dirname, BAKED_EDITION_NAME) } = {}) {
  try {
    const st = fs.lstatSync(bakedPath);
    if (!st.isFile() || st.size > EDITION_MAX_BYTES) return EDITION_OSS;
    const text = fs.readFileSync(bakedPath, "utf8").replace(/\n$/, "");
    return text === EDITION_ENTERPRISE ? EDITION_ENTERPRISE : EDITION_OSS;
  } catch {
    return EDITION_OSS;
  }
}

module.exports = {
  BAKED_EDITION_NAME,
  EDITION_ENTERPRISE,
  EDITION_OSS,
  readBakedEdition,
};
