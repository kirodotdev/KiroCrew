"use strict";

// Owner-only mode for the shell's electron-store files (config.json and Mochi's
// mochi-machine.json). config.json holds the remote hosts and the kirocrew
// binary path the app runs over ssh, so another local account must not be able
// to read or rewrite it.
//
// conf defaults `configFileMode` to 0o666, and atomically chmods every write to
// exactly that mode, past the umask. Each Store therefore passes
// STORE_FILE_MODE, so every save lands owner-only. That covers writes only:
// conf leaves a complete file untouched on open, so a file that is already
// group- or world-accessible stays that way until the next save, and
// tightenStoreFileMode closes it at boot.

const fs = require("fs");
const path = require("path");

const STORE_FILE_MODE = 0o600;

// Any group or other permission bit.
const NON_OWNER_BITS = 0o077;

/**
 * chmod an existing store file to STORE_FILE_MODE when any group or other bit
 * is set. Never throws: it runs at boot, before any window exists.
 *
 * Skipped on Windows, where a POSIX mode does not express who can open the
 * file, and every file reports group and other bits.
 *
 * Follows a symlink, as atomically does when conf writes the file, so the
 * file this tightens is the one conf writes.
 *
 * @param {string} filePath the store's `path`
 * @param {object} [opts]
 * @param {object} [opts.fsImpl] injected for tests
 * @param {string} [opts.platform] injected for tests; defaults to process.platform
 * @param {(msg:string) => void} [opts.log]
 * @returns {boolean} whether the mode was changed
 */
function tightenStoreFileMode(filePath, { fsImpl = fs, platform = process.platform, log = null } = {}) {
  if (platform === "win32") return false;
  try {
    const mode = fsImpl.statSync(filePath).mode & 0o777;
    if ((mode & NON_OWNER_BITS) === 0) return false;
    fsImpl.chmodSync(filePath, STORE_FILE_MODE);
    if (log) log(`${path.basename(filePath)} mode ${mode.toString(8)} tightened to ${STORE_FILE_MODE.toString(8)}`);
    return true;
  } catch (err) {
    if (log) log(`could not tighten ${path.basename(filePath)}: ${(err && err.message) || err}`);
    return false;
  }
}

module.exports = { STORE_FILE_MODE, tightenStoreFileMode };
