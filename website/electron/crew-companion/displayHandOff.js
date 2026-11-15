/**
 * displayHandOff.js: hand the built-in avatar's display to the desktop window host.
 *
 * The built-in remembers which display its avatar lives on in its own userData
 * file (`crew-companion-pet-position.json`, written by petOverlay.js). Once the
 * app's own repository copy is installed, the avatar is that copy's spanning
 * overlay, which the window host opens on the display it remembers in
 * `app-window-displays.json` under `<app>/<window id>`. With nothing there the host
 * elects the display under the cursor, so a multi-display user would find the
 * avatar on another screen the first time the copy runs.
 *
 * So at every start, before the host reads its file (it reads it once, on first
 * use), the built-in's display is copied into the host's under the window id the
 * app's repository declares for its avatar (`pet`), and the value copied is
 * recorded in `crew-companion-display-handoff.json`. The copy is redone while the
 * host's entry still holds the value last copied: before adoption the built-in
 * avatar is the one the user moves, so a later move reaches the host on the next
 * start. Once the host's entry differs from that record, the host has written a
 * display of its own (the adopted avatar was moved) and it is never overwritten.
 * After adoption the built-in no longer writes its file, so the copy can only
 * repeat the value the host already has. The host reads the key only for an
 * installed app of this name declaring a spanning window of that id, so writing
 * it ahead of time changes nothing for anyone else.
 */

const fs = require("fs");
const path = require("path");

const BUILTIN_POSITION_FILE = "crew-companion-pet-position.json";
const HAND_OFF_RECORD = "crew-companion-display-handoff.json";
const HOST_KEY = "crew-companion/pet";

function readJsonObject(file) {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf-8"));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function finite(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function writeOwnerOnly(file, value) {
  fs.writeFileSync(file, JSON.stringify(value), { mode: 0o600 });
}

/**
 * Seed the host's remembered display for the avatar from the built-in's own.
 *
 * @param {string} userDataDir the shell's userData directory, where the files live
 * @param {string} hostMemoryFile the host's `app-window-displays.json`
 * @returns {boolean} true when a display was written to the host's file
 */
function seedHostDisplayMemory(userDataDir, hostMemoryFile) {
  if (!userDataDir || !hostMemoryFile) return false;
  const builtin = readJsonObject(path.join(userDataDir, BUILTIN_POSITION_FILE));
  const displayId = builtin ? builtin.displayId : undefined;
  if (!finite(displayId)) return false;
  let host = {};
  if (fs.existsSync(hostMemoryFile)) {
    host = readJsonObject(hostMemoryFile);
    // An unreadable host file is the host's to recover; never overwrite it.
    if (!host) return false;
  }
  const recordFile = path.join(userDataDir, HAND_OFF_RECORD);
  const record = readJsonObject(recordFile);
  const seeded = record && finite(record.seeded) ? record.seeded : undefined;
  if (Object.prototype.hasOwnProperty.call(host, HOST_KEY)) {
    // The host wrote a display of its own: the adopted avatar was moved.
    if (host[HOST_KEY] !== seeded) return false;
    if (host[HOST_KEY] === displayId) return false;
  }
  try {
    writeOwnerOnly(hostMemoryFile, { ...host, [HOST_KEY]: displayId });
    writeOwnerOnly(recordFile, { seeded: displayId });
    return true;
  } catch {
    // The avatar then opens on the display under the cursor; nothing else changes.
    return false;
  }
}

module.exports = { seedHostDisplayMemory, BUILTIN_POSITION_FILE, HAND_OFF_RECORD, HOST_KEY };
