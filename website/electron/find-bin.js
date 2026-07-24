// Universal .app bundles (packaging/build-desktop.sh UNIVERSAL=1) ship one
// complete backend tree per CPU architecture under backend-dist/. Maps a Node
// `process.arch` value to the directory suffix; arches without an entry
// (e.g. "ia32") simply skip the arch-suffixed candidates.
const ARCH_DIR_SUFFIX = { arm64: "arm64", x64: "x64" };

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
 * @param {string} [arch] - CPU arch selecting the backend tree in universal
 *   bundles (defaults to `process.arch`)
 * @param {boolean} [isWindows] - whether the host is Windows (defaults to
 *   `process.platform === "win32"`). On Windows the backend ships as a real
 *   `kirocrew.exe` console script under `Scripts\` (venv) — Node's `spawn()`
 *   does no PATHEXT resolution for a bare name, so an absolute `.exe` path is
 *   required.
 * @returns {string} Absolute path to the binary, or `"kirocrew"` /
 *   `"kirocrew.exe"` (Windows) as a PATH fallback
 */
function findKirocrewBin(
  fs,
  os,
  path,
  resourcesPath,
  dirname,
  arch = process.arch,
  isWindows = process.platform === "win32"
) {
  const home = os.homedir();
  const candidates = [];
  // 0. Universal-bundle layout: arch-suffixed backend trees, selected by the
  //    running shell's arch. Ranked above the unsuffixed layout so a universal
  //    bundle never falls back to a wrong-arch tree; plain per-arch bundles
  //    don't ship these dirs so the probes miss (ENOENT) and fall through.
  const suffix = ARCH_DIR_SUFFIX[arch];
  if (suffix) {
    const archBackend = `kirocrew-backend-${suffix}`;
    candidates.push(
      path.join(resourcesPath || "", "backend-dist", archBackend, "bin", "kirocrew"),
      path.resolve(dirname, "backend-dist", archBackend, "bin", "kirocrew")
    );
  }
  // 0b. Windows layout: a pip/venv install exposes `kirocrew.exe` under
  //     `Scripts\` (not the POSIX `bin/kirocrew` launcher). Probed before the
  //     POSIX candidates so a native Windows source install resolves to an
  //     absolute `.exe` that `spawn()` can launch without a shell. On POSIX
  //     these are skipped entirely so mac/Linux behavior is unchanged.
  if (isWindows) {
    candidates.push(
      // Bundled Windows backend (python-build-standalone → Scripts\kirocrew.exe)
      path.join(resourcesPath || "", "backend-dist", "kirocrew-backend", "Scripts", "kirocrew.exe"),
      path.resolve(dirname, "backend-dist", "kirocrew-backend", "Scripts", "kirocrew.exe"),
      // Source checkout: repo-root `.venv` — electron/ is <repo>/website/electron,
      // so the venv is two levels up; one level up covers a <repo>/website venv.
      path.resolve(dirname, "..", "..", ".venv", "Scripts", "kirocrew.exe"),
      path.resolve(dirname, "..", ".venv", "Scripts", "kirocrew.exe"),
      // One-liner installer venv, and toolbox / local pip Scripts dirs.
      path.join(home, ".kirocrew-app", ".venv", "Scripts", "kirocrew.exe"),
      path.join(home, ".toolbox", "bin", "kirocrew.exe"),
      path.join(home, ".local", "bin", "kirocrew.exe")
    );
  }
  candidates.push(
    // 0b. Windows bundled layout (packaging/build-desktop.sh
    //     build_backend_windows): the PBS interpreter ships python.exe at
    //     the tree root with a bin\kirocrew.cmd launcher shim. Probed on
    //     every platform (costs one ENOENT elsewhere) so this function
    //     stays platform-agnostic and testable; only a Windows bundle
    //     actually contains the .cmd. Keep in sync with
    //     build-desktop.sh's bin/kirocrew.cmd.
    path.join(resourcesPath || "", "backend-dist", "kirocrew-backend", "bin", "kirocrew.cmd"),
    path.resolve(dirname, "backend-dist", "kirocrew-backend", "bin", "kirocrew.cmd"),
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
    path.join(home, ".kirocrew-app", ".venv", "bin", "kirocrew")
  );
  for (const bin of candidates) {
    try {
      fs.accessSync(bin, fs.constants.X_OK);
      return bin;
    } catch (e) {
      if (e.code !== "ENOENT") console.warn(`kirocrew candidate ${bin}: ${e.code}`);
    }
  }
  return isWindows ? "kirocrew.exe" : "kirocrew"; // fall back to PATH
}

module.exports = { findKirocrewBin };
