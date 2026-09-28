"use strict";
// Files that package.json's `files` list names but that a plain checkout does
// not contain: build-desktop.sh stages them on demand (step 3b) and the EXIT
// trap removes them again, so their absence is not staleness. Shared by the
// packaging and shell-contract staleness tests so the exemption has one owner.
// (Not a *.test.js file, so `node --test test/*.test.js` does not run it.)
module.exports = { BUILD_TIME_INPUTS: new Set(["EXTERNALLY-MANAGED"]) };
