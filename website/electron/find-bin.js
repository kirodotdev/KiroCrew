/**
 * Locate the kirocrew backend binary by checking well-known paths in order.
 *
 * Returns the first executable candidate, or bare `"kirocrew"` as a PATH
 * fallback. Dependencies are injected so the function is pure and testable
 * without mocking globals.
 *
 * @param {typeof import("fs")} fs - Node fs module (needs `accessSync`, `constants.X_OK`)
 * @param {typeof import("os")} os - Node os module (needs `homedir()`)
 * @param {typeof import("path")} path - Node path module
 * @param {string|undefined} resourcesPath - `process.resourcesPath` (Electron only)
 * @param {string} dirname - `__dirname` of the calling module
 * @returns {string} Absolute path to the binary, or `"kirocrew"`
 */
function findKirocrewBin(fs, os, path, resourcesPath, dirname) {
  const home = os.homedir();
  const candidates = [
    // 1. Legacy PyInstaller layout: a flat frozen executable at the root of the
    //    bundle. The current builder (packaging/build-desktop.sh) no longer
    //    emits this; kept first only for backward-compat with older bundles.
    path.join(resourcesPath || "", "backend-dist", "kirocrew-backend", "kirocrew-backend"),
    path.resolve(dirname, "backend-dist", "kirocrew-backend", "kirocrew-backend"),
    // 1b. CURRENT bundled layout (packaging/build-desktop.sh): a
    //     python-build-standalone interpreter copied into backend-dist with a
    //     `bin/kirocrew` launcher wrapper (exec python3.12 -s -m kiro_crew).
    //     This is what a freshly-built .app actually ships. Keep this in sync
    //     with build-desktop.sh's BACKEND_OUT/bin/kirocrew path.
    path.join(resourcesPath || "", "backend-dist", "kirocrew-backend", "bin", "kirocrew"),
    path.resolve(dirname, "backend-dist", "kirocrew-backend", "bin", "kirocrew"),
    path.resolve(dirname, "..", "bin", "kirocrew"),
    // 2. Well-known install paths (toolbox, installer symlink, and venv)
    path.join(home, ".toolbox", "bin", "kirocrew"),
    path.join(home, ".local", "bin", "kirocrew"),
    path.join(home, ".kirocrew-app", ".venv", "bin", "kirocrew"),
  ];
  for (const bin of candidates) {
    try {
      fs.accessSync(bin, fs.constants.X_OK);
      return bin;
    } catch (e) {
      if (e.code !== "ENOENT") console.warn(`kirocrew candidate ${bin}: ${e.code}`);
    }
  }
  return "kirocrew"; // fall back to PATH
}

module.exports = { findKirocrewBin };
