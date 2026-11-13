"use strict";

/**
 * app-local-files.js: the two local-file requests a page may make of the shell.
 *
 * A page in a shell window shows paths it did not choose (agent-authored markdown,
 * a pinned file list), so a path from it is untrusted. These are the only two
 * things it can ask for, and each re-checks the path here in the main process:
 *
 *  - reveal: show an existing regular file in the system file manager. Nothing is
 *    opened or run.
 *  - open image: hand an existing regular IMAGE file to the system viewer. The path
 *    is resolved first (so a `.png` link to a key file is judged by what it points
 *    at), the resolved name must carry an image extension, and the open goes
 *    through `openPathHardened`, so no launcher is taken from a writable PATH.
 *
 * Neither reads a file's bytes into a page: that stays on the gateway's own
 * `/api/file-raw`, which keeps its sensitive-path gate.
 *
 * Shared by every window that offers these calls (the desktop window host for an
 * app holding the `localFiles` capability, and the shell's own first-party
 * windows), so there is one copy of the rules. Electron and the file system are
 * injected, so `test/app-local-files.test.js` pins every rule without Electron.
 */

const IMAGE_EXTS = Object.freeze([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"]);
const IMAGE_EXT_SET = new Set(IMAGE_EXTS);

/**
 * Reveal *filePath* in the system file manager when it names an existing regular file.
 *
 * @param {{fs: {statSync: Function}, shell: {showItemInFolder: Function}}} deps
 * @param {unknown} filePath
 * @returns {boolean} whether the shell was asked to reveal it
 */
function revealLocalFile(deps, filePath) {
  if (typeof filePath !== "string" || filePath === "") return false;
  try {
    if (!deps.fs.statSync(filePath).isFile()) return false;
  } catch {
    return false;
  }
  deps.shell.showItemInFolder(filePath);
  return true;
}

/**
 * Open *filePath* in the system viewer when it resolves to an existing regular image.
 *
 * @param {{
 *   fs: {realpathSync: Function, statSync: Function},
 *   path: {extname: Function},
 *   shell: object,
 *   openPathHardened: (shell: object, p: string) => Promise<string>,
 *   onRefused?: (err: unknown) => void,
 * }} deps
 * @param {unknown} filePath
 * @returns {Promise<boolean>} whether the system viewer accepted it
 */
async function openLocalImage(deps, filePath) {
  if (typeof filePath !== "string" || filePath === "") return false;
  try {
    // Resolved BEFORE the extension test: a `.png` link must be judged by its target.
    const real = deps.fs.realpathSync(filePath);
    if (!IMAGE_EXT_SET.has(deps.path.extname(real).toLowerCase())) return false;
    if (!deps.fs.statSync(real).isFile()) return false;
    // A non-empty answer means the OS refused to open it.
    const err = await deps.openPathHardened(deps.shell, real);
    return err === "";
  } catch (err) {
    if (typeof deps.onRefused === "function") deps.onRefused(err);
    return false;
  }
}

module.exports = { IMAGE_EXTS, openLocalImage, revealLocalFile };
