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
  // 1. Windows SOURCE CHECKOUT: a pip/venv install exposes `kirocrew.exe`
  //    under `Scripts\` (not the POSIX `bin/kirocrew` launcher). Probed before
  //    the bundled candidates so a developer running from a checkout gets
  //    their own venv, and as an absolute `.exe` that `spawn()` can launch
  //    without a shell. On POSIX these are skipped entirely so mac/Linux
  //    behavior is unchanged.
  //
  //    Only the checkout venvs are ranked here. The BUNDLE's own
  //    Scripts\kirocrew.exe is ranked further down, below the .cmd shim --
  //    see the note there.
  if (isWindows) {
    candidates.push(
      // Source checkout: repo-root `.venv` — electron/ is <repo>/website/electron,
      // so the venv is two levels up; one level up covers a <repo>/website venv.
      path.resolve(dirname, "..", "..", ".venv", "Scripts", "kirocrew.exe"),
      path.resolve(dirname, "..", ".venv", "Scripts", "kirocrew.exe")
    );
  }
  candidates.push(
    // 2. Windows bundled layout (packaging/build-desktop.sh
    //    build_backend_windows): the PBS interpreter ships python.exe at
    //    the tree root with a bin\kirocrew.cmd launcher shim. Probed on
    //    every platform (costs one ENOENT elsewhere) so this function
    //    stays platform-agnostic and testable; only a Windows bundle
    //    actually contains the .cmd. Keep in sync with
    //    build-desktop.sh's bin/kirocrew.cmd.
    //
    //    This MUST outrank backend-dist/.../Scripts/kirocrew.exe below.
    //    `pip install` also drops a console-script .exe in the bundle's
    //    Scripts\ dir, but distlib embeds the ABSOLUTE interpreter path of
    //    the machine that built it, so inside a shipped bundle that .exe
    //    points at a build-agent path (D:\a\KiroCrew\...) that does not
    //    exist on the user's machine. The .cmd shim resolves the
    //    interpreter via %~dp0 and is the only relocatable launcher of the
    //    two. Ranking them the other way round both broke the build-time
    //    resolver gate and, had the gate not caught it, would have shipped
    //    an app whose backend could never start.
    path.join(resourcesPath || "", "backend-dist", "kirocrew-backend", "bin", "kirocrew.cmd"),
    path.resolve(dirname, "backend-dist", "kirocrew-backend", "bin", "kirocrew.cmd"),
    // 3. Bundled POSIX layout (packaging/build-desktop.sh): a
    //    python-build-standalone interpreter copied into backend-dist with a
    //    `bin/kirocrew` launcher wrapper (exec python3.12 -s -P -m kiro_crew).
    //    This is what a freshly-built .app actually ships. Keep this in sync
    //    with build-desktop.sh's BACKEND_OUT/bin/kirocrew path.
    path.join(resourcesPath || "", "backend-dist", "kirocrew-backend", "bin", "kirocrew"),
    path.resolve(dirname, "backend-dist", "kirocrew-backend", "bin", "kirocrew"),
    path.resolve(dirname, "..", "bin", "kirocrew")
  );
  if (isWindows) {
    // 4. The bundle's pip console-script .exe. Ranked BELOW the .cmd shim
    //    (distlib bakes the building machine's absolute interpreter path into
    //    it, so in a shipped bundle it points at a path that does not exist)
    //    but still ABOVE the user-level install paths below: a bundled app
    //    must prefer its own backend over whatever happens to be installed on
    //    the machine. It is correct for a bundle built where it runs (a local
    //    `make desktop`), which is why it is probed at all.
    candidates.push(
      path.join(resourcesPath || "", "backend-dist", "kirocrew-backend", "Scripts", "kirocrew.exe"),
      path.resolve(dirname, "backend-dist", "kirocrew-backend", "Scripts", "kirocrew.exe")
    );
  }
  // 5. Well-known install paths (toolbox, installer symlink, and venv). Last,
  //    so a packaged app never prefers a stray user-level install over the
  //    backend it shipped with.
  candidates.push(
    path.join(home, ".toolbox", "bin", "kirocrew"),
    path.join(home, ".local", "bin", "kirocrew"),
    path.join(home, ".kirocrew-app", ".venv", "bin", "kirocrew")
  );
  if (isWindows) {
    // Windows equivalents of the user-level paths above (one-liner installer
    // venv, toolbox, and local pip Scripts dirs).
    candidates.push(
      path.join(home, ".kirocrew-app", ".venv", "Scripts", "kirocrew.exe"),
      path.join(home, ".toolbox", "bin", "kirocrew.exe"),
      path.join(home, ".local", "bin", "kirocrew.exe")
    );
  }
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

/**
 * Resolve the local OpenSSH client for an `execFile` call.
 *
 * POSIX returns bare `"ssh"`: `execFile` searches PATH, as the gateway's own
 * ssh calls do (`instances/token_mint.py`), so a Nix or Homebrew ssh is found
 * and a GUI launch with the default launchd PATH still reaches `/usr/bin/ssh`.
 * Windows has no `/usr/bin/ssh`, so it takes the first `ssh.exe` on PATH, then
 * the in-box OpenSSH client, then bare `"ssh.exe"` so a miss surfaces as a
 * spawn ENOENT naming the binary.
 *
 * @param {typeof import("fs")} fs - Node fs module (needs `accessSync`, `constants.F_OK`)
 * @param {typeof import("path")} path - Node path module for the host platform
 * @param {Record<string, string|undefined>} [env] - environment holding PATH and SystemRoot
 * @param {boolean} [isWindows] - whether the host is Windows
 * @returns {string} Absolute `ssh.exe` path when one is found on Windows, else a bare name
 */
function findSshBin(
  fs,
  path,
  env = process.env,
  isWindows = process.platform === "win32"
) {
  if (!isWindows) return "ssh";
  const pathVar = env.PATH || env.Path || "";
  const candidates = pathVar
    .split(path.delimiter)
    .filter(Boolean)
    .map((dir) => path.join(dir, "ssh.exe"));
  candidates.push(path.join(env.SystemRoot || "C:\\Windows", "System32", "OpenSSH", "ssh.exe"));
  for (const bin of candidates) {
    try {
      fs.accessSync(bin, fs.constants.F_OK);
      return bin;
    } catch {
      // not here; try the next candidate
    }
  }
  return "ssh.exe";
}

module.exports = { findKirocrewBin, findSshBin };
