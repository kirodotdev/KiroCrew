/**
 * linux-desktop-integration.js — install this AppImage's icon and launcher
 * into the user's desktop on first run (Linux only).
 *
 * An AppImage is a single self-mounted file: unlike a .deb or the macOS/Windows
 * installers, nothing copies its bundled `.desktop` entry or its hicolor icons
 * into `~/.local/share`, so a freshly-downloaded AppImage shows a generic icon
 * in the launcher and (on Wayland, where the window icon is resolved from the
 * app-id -> desktop entry -> icon theme) for the running window too. External
 * integration daemons (appimaged, AppImageLauncher) exist to close that gap but
 * are not packaged for every distro. This module closes it from inside the app,
 * which is the one path that is always present.
 *
 * Behaviour: silent and idempotent. Launching the app is taken as consent (the
 * same implicit consent every installer relies on); the writes land only under
 * the user's own `~/.local/share`, are keyed to OUR app id so a hand-made
 * launcher of a different name is never touched, and a stamp keyed on
 * (version, AppImage path) means the work runs once per install location rather
 * than every boot, while a moved AppImage re-integrates so its launcher never
 * points at a stale path. Two escape hatches keep it from being imposed: it is
 * skipped entirely when `KIROCREW_DISABLE_DESKTOP_INTEGRATION` is set truthy,
 * and when an external integration daemon (appimaged / AppImageLauncher) has
 * already written an `appimagekit_*.desktop` for this same AppImage, so the two
 * never produce duplicate menu entries. Every launcher we write carries an
 * `X-KiroCrew-Generated` key: a same-name launcher WITHOUT it is a hand-made or
 * third-party file and is backed up rather than overwritten, and only a launcher
 * that carries it is ever removed or replaced.
 *
 * Safety: every destination write refuses a symlinked target or linked ancestor
 * and lands via an O_EXCL temp + atomic rename, so a pre-planted symlink can
 * never redirect a write onto an unrelated file. Cache-refresh binaries are
 * resolved only from fixed system directories, never via `PATH`, so a planted
 * executable earlier in `PATH` is never run.
 *
 * The AppImage type-2 runtime exports two variables to the running process that
 * make this possible without shipping any path assumptions:
 *   - `APPIMAGE` — absolute path to the .AppImage file (the launcher's Exec)
 *   - `APPDIR`   — the mount root, so the bundled `.desktop` and the hicolor
 *                  icon tree are readable at `${APPDIR}/...`
 * Both absent => not launched as an AppImage (a dev run, or another target) and
 * this module no-ops.
 *
 * Pure helpers (parse/rewrite/render/plan/isCurrent) carry the logic and are
 * unit-tested; `integrateLinuxDesktop` is the thin impure orchestrator that
 * takes its fs/os/child_process/platform dependencies injected, matching this
 * package's other modules (see data-home.js, gateway-stop.js).
 */

"use strict";

// Standard freedesktop hicolor sizes the AppImage may ship. We copy whichever
// are actually present; a size the bundle omits is simply skipped.
const ICON_SIZES = ["16x16", "24x24", "32x32", "48x48", "64x64", "128x128", "256x256", "512x512", "1024x1024"];

// Cache-refresh binaries are resolved ONLY from these fixed system directories,
// never from $PATH, so an executable planted earlier in PATH cannot be run.
// `/usr/local/bin` is deliberately EXCLUDED: it is group-/user-writable on many
// single-user Linux desktops, so trusting it would reopen the very planted-
// binary hole this allowlist exists to close (e.g. a planted `kbuildsycoca6`
// resolving on a non-KDE box). Distros ship these refreshers in `/usr/bin`, so
// dropping `/usr/local/bin` costs no real coverage.
const SYSTEM_BIN_DIRS = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"];

// Truthy env values, matching how the rest of the electron shell reads
// KIROCREW_* flags (see disable-gpu.js).
const TRUTHY = new Set(["1", "true", "yes", "on"]);

// Key stamped into every launcher we generate. A later run reads it back to
// tell our own output from a hand-made or third-party file at the same path,
// so we never clobber the latter without a backup (see launcherOwnership).
const OWNERSHIP_KEY = "X-KiroCrew-Generated";

/**
 * Parse the small subset of Desktop Entry keys we care about from a `.desktop`
 * file body. Values are read from the first `[Desktop Entry]` group; keys after
 * an unrelated group header are ignored. Returns a plain object with only the
 * keys that were present.
 */
function parseDesktopEntry(text) {
  const out = {};
  let inEntry = false;
  for (const raw of String(text).split(/\r?\n/)) {
    const line = raw.trim();
    if (line.startsWith("[") && line.endsWith("]")) {
      inEntry = line === "[Desktop Entry]";
      continue;
    }
    if (!inEntry || !line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    if (!(key in out)) out[key] = line.slice(eq + 1).trim();
  }
  return out;
}

/**
 * Escape one argument for a Desktop Entry `Exec` value.
 *
 * The freedesktop spec stacks TWO escaping layers on a quoted `Exec` argument,
 * and a reader undoes them in order (value-string first, then quoting), so a
 * writer must apply them in reverse:
 *   1. Quoting layer: inside the double quotes, precede `"`, `` ` ``, `$` and
 *      `\` with a backslash.
 *   2. Value-string layer: the desktop-entry value type escapes a backslash as
 *      `\\`, applied to the WHOLE value (including the backslashes layer 1 just
 *      added). So a literal `\` ends up as four backslashes and a literal `$`
 *      as `\\$`, exactly as the spec's own examples show.
 * Independently, a literal percent is written `%%` so it is not read as a field
 * code (`%f`, `%U`, ...). We always quote (the value is a filesystem path), so
 * an AppImage path containing `"`, `$`, backtick, `%` or `\` still serializes
 * to a launcher that starts.
 */
function escapeExecArg(arg) {
  const quoted = String(arg).replace(/(["`$\\])/g, "\\$1"); // layer 1: Exec quoting
  const valueEscaped = quoted.replace(/\\/g, "\\\\");       // layer 2: value-string
  const fieldEscaped = valueEscaped.replace(/%/g, "%%");    // field-code literal
  return `"${fieldEscaped}"`;
}

/**
 * Rewrite a bundled `Exec=` line so it launches the AppImage by its real path.
 *
 * The bundled entry runs `AppRun ... %U`, valid only inside the mount. The
 * installed launcher must invoke the AppImage file itself, so the leading
 * program token is replaced with the escaped, quoted AppImage path (see
 * escapeExecArg) and every remaining argument (flags, field codes like %U) is
 * preserved.
 */
function rewriteExec(bundledExec, appImagePath) {
  const quoted = escapeExecArg(appImagePath);
  const tokens = String(bundledExec || "").trim().split(/\s+/).filter(Boolean);
  if (tokens.length <= 1) return `${quoted} %U`;
  return [quoted, ...tokens.slice(1)].join(" ");
}

/**
 * Render the launcher `.desktop` body from resolved fields. Only fields with a
 * value are emitted; Name/Exec/Icon are always present by construction.
 */
function renderDesktopEntry(fields) {
  const lines = ["[Desktop Entry]", "Type=Application"];
  const emit = (k, v) => { if (v) lines.push(`${k}=${v}`); };
  emit("Name", fields.name);
  emit("Comment", fields.comment);
  emit("Exec", fields.execLine);
  emit("Icon", fields.iconName);
  lines.push("Terminal=false");
  emit("StartupWMClass", fields.wmClass);
  emit("Categories", fields.categories);
  emit(OWNERSHIP_KEY, fields.generated);
  return lines.join("\n") + "\n";
}

/**
 * Decide whether to run at all. Integration is Linux-only, requires both
 * AppImage runtime variables, and is skipped when the user opts out via
 * `KIROCREW_DISABLE_DESKTOP_INTEGRATION` (truthy 1/true/yes/on, matching the
 * `KIROCREW_DISABLE_GPU` convention). `platform` is injected (defaulting to
 * `process.platform`) rather than read from an env var, matching this package's
 * injection convention. Returns { integrate, appImage, appDir, reason }.
 */
function planIntegration(env, platform = process.platform) {
  if (platform !== "linux") return { integrate: false, reason: "not linux" };
  const optOut = env && env.KIROCREW_DISABLE_DESKTOP_INTEGRATION;
  if (typeof optOut === "string" && TRUTHY.has(optOut.trim().toLowerCase())) {
    return { integrate: false, reason: "opt-out via KIROCREW_DISABLE_DESKTOP_INTEGRATION" };
  }
  const appImage = env && env.APPIMAGE;
  const appDir = env && env.APPDIR;
  if (!appImage || !appDir) return { integrate: false, reason: "not an AppImage launch" };
  return { integrate: true, appImage, appDir };
}

/**
 * The XDG data home the icons and launcher live under: `$XDG_DATA_HOME` when
 * set (and absolute), else `~/.local/share`.
 */
function dataHome(env, home) {
  const xdg = env && env.XDG_DATA_HOME;
  if (xdg && xdg.startsWith("/")) return xdg;
  return `${home}/.local/share`;
}

/**
 * Report whether the stamp already records THIS (version, AppImage path) pair.
 * A missing or unparseable stamp, a different version, OR a different AppImage
 * path returns false so the (idempotent) install runs again. The AppImage-path
 * check is what re-integrates after the user MOVES the AppImage (a flow the app
 * itself prompts via offerRelocationIfUnupdatable): a version-only stamp would
 * skip as "already current" and leave the launcher's Exec pointing at the old,
 * now-missing path.
 */
function isCurrent(fs, stampPath, version, appImage) {
  try {
    const rec = JSON.parse(fs.readFileSync(stampPath, "utf8"));
    return !!rec && rec.version === version && rec.appImage === appImage;
  } catch {
    return false;
  }
}

/**
 * Find the bundled top-level `.desktop` file inside the AppImage mount and
 * return its parsed entry, or null if none is present/readable.
 */
function readBundledEntry(fs, path, appDir) {
  let names;
  try {
    names = fs.readdirSync(appDir).filter((n) => n.endsWith(".desktop"));
  } catch {
    return null;
  }
  for (const name of names) {
    try {
      return parseDesktopEntry(fs.readFileSync(path.join(appDir, name), "utf8"));
    } catch {
      // Try the next candidate.
    }
  }
  return null;
}

/**
 * Classify an existing launcher at `launcherPath`: `"ours"` when it carries the
 * OWNERSHIP_KEY we stamp into every launcher we write, `"foreign"` when a file
 * (or a symlink we did not create) exists without that key, `"absent"` when
 * nothing is there or it cannot be read. A `"foreign"` file must be backed up
 * rather than clobbered; only an `"ours"` file may be removed or overwritten.
 */
function launcherOwnership(fs, launcherPath) {
  let st;
  try { st = fs.lstatSync(launcherPath); } catch { return "absent"; }
  if (st && st.isSymbolicLink()) return "foreign";
  try {
    const entry = parseDesktopEntry(fs.readFileSync(launcherPath, "utf8"));
    return entry[OWNERSHIP_KEY] ? "ours" : "foreign";
  } catch {
    return "absent";
  }
}

/**
 * Whether a Desktop Entry `Exec`/`TryExec` value refers to `appImagePath` as its
 * program, comparing the FIRST token as a whole path rather than by substring.
 * A substring test would false-positive on a path-prefix collision (an entry for
 * `/opt/K.AppImage.bak` must not match `/opt/K.AppImage`). The first token is a
 * double-quoted path or the text up to the first space; a bare `TryExec` path
 * compares whole.
 */
function execRefersToPath(value, appImagePath) {
  if (typeof value !== "string") return false;
  const v = value.trim();
  let token;
  if (v.startsWith('"')) {
    const end = v.indexOf('"', 1);
    token = end === -1 ? v.slice(1) : v.slice(1, end);
  } else {
    const sp = v.indexOf(" ");
    token = sp === -1 ? v : v.slice(0, sp);
  }
  return token === appImagePath;
}

/**
 * Report whether an external AppImage integration daemon has ALREADY installed
 * a launcher for THIS AppImage. appimaged and AppImageLauncher write their own
 * `appimagekit_*.desktop` files into the applications dir with an `Exec=` (and
 * often `TryExec=`) pointing at the AppImage. When one already references this
 * AppImage path, running our own integration would leave the user with two
 * duplicate menu entries, so the orchestrator skips and leaves the daemon's
 * entry in place. Best-effort: an unreadable candidate is ignored.
 */
function hasExternalIntegration(fs, path, appsDir, appImagePath) {
  let names;
  try {
    names = fs.readdirSync(appsDir).filter((n) => /^appimagekit_.*\.desktop$/.test(n));
  } catch {
    return false;
  }
  for (const name of names) {
    try {
      const entry = parseDesktopEntry(fs.readFileSync(path.join(appsDir, name), "utf8"));
      if ([entry.Exec, entry.TryExec].some((v) => execRefersToPath(v, appImagePath))) {
        return true;
      }
    } catch {
      // Unreadable candidate; ignore and keep scanning.
    }
  }
  return false;
}

/**
 * Resolve a cache-refresh binary to an absolute path from a fixed system-dir
 * allowlist, or null if it is not found in any of them. Never consults `$PATH`.
 */
function resolveSystemBinary(fs, path, name) {
  for (const dir of SYSTEM_BIN_DIRS) {
    const candidate = path.join(dir, name);
    try {
      if (fs.existsSync(candidate)) return candidate;
    } catch {
      // Try the next directory.
    }
  }
  return null;
}

/**
 * Write `data` to `destPath` without following a planted symlink and without a
 * partial-write window:
 *   - refuse a symlinked destination (lstat, no-follow), and
 *   - refuse a linked ancestor: the destination's REAL directory must stay
 *     inside `root`, so a symlinked parent cannot redirect the write, then
 *   - write to a fresh O_EXCL temp in the same directory (a planted temp
 *     symlink is refused with EEXIST, never followed), and
 *   - rename it over the destination — atomic, and REPLACES a symlink
 *     destination rather than writing through it.
 * Throws on any refusal so the caller skips that write.
 */
function safeWrite(fs, path, root, destPath, data) {
  let st = null;
  try { st = fs.lstatSync(destPath); } catch (e) { if (!e || e.code !== "ENOENT") throw e; }
  if (st && st.isSymbolicLink()) throw new Error(`refusing symlinked destination: ${destPath}`);
  const realRoot = fs.realpathSync(root);
  const realDir = fs.realpathSync(path.dirname(destPath));
  if (realDir !== realRoot && !realDir.startsWith(realRoot + "/")) {
    throw new Error(`refusing destination outside ${realRoot}: ${realDir}`);
  }
  const tmp = `${destPath}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(tmp, data, { flag: "wx", mode: 0o644 });
  try {
    fs.renameSync(tmp, destPath);
  } catch (e) {
    try { fs.unlinkSync(tmp); } catch { /* ignore cleanup failure */ }
    throw e;
  }
}

/**
 * Install the AppImage's icon set and a launcher into the user's desktop.
 *
 * Silent, idempotent, best-effort: any single step failing is logged and
 * skipped rather than propagated, because desktop integration must never take
 * the app down at boot. Returns a result object describing what happened
 * ({ skipped, reason } or { installed, iconName, sizes }).
 *
 * Dependencies are injected for testability:
 *   fs, path, os        — node builtins
 *   platform            — process.platform (injected; defaults to it)
 *   env                 — process.env (APPIMAGE/APPDIR/XDG_DATA_HOME/HOME)
 *   version             — app.getVersion(), for the run-once stamp
 *   stampDir            — a writable dir for the stamp (app.getPath('userData'))
 *   run(cmd, args)      — spawn a cache-refresh command (given an ABSOLUTE path)
 *   log(message)        — diagnostic sink
 */
function integrateLinuxDesktop(deps) {
  const { fs, path, os, env, platform, version, stampDir, run, log } = deps;
  const note = (m) => { try { if (log) log(m); } catch { /* ignore */ } };

  const plan = planIntegration(env, platform);
  if (!plan.integrate) return { skipped: true, reason: plan.reason };

  const home = (env && env.HOME) || os.homedir();
  const share = dataHome(env, home);
  const appsDir = path.join(share, "applications");

  const entry = readBundledEntry(fs, path, plan.appDir);
  if (!entry || !entry.Icon) {
    note("no bundled .desktop with an Icon= found; skipping");
    return { skipped: true, reason: "no bundled desktop entry" };
  }
  const iconName = entry.Icon;
  const iconRoot = path.join(share, "icons", "hicolor");
  const launcherPath = path.join(appsDir, `${iconName}.desktop`);

  // Coexistence: if appimaged / AppImageLauncher already integrated THIS
  // AppImage (their appimagekit_*.desktop points at it), skip so the user is
  // never left with two duplicate "Kiro Crew" menu entries. Checked BEFORE the
  // run-once stamp so an external tool that installs AFTER we stamped is still
  // seen; when it is, remove OUR OWN generated launcher (never a foreign one)
  // so our earlier entry does not linger as the duplicate.
  if (hasExternalIntegration(fs, path, appsDir, plan.appImage)) {
    if (launcherOwnership(fs, launcherPath) === "ours") {
      try {
        fs.unlinkSync(launcherPath);
        note("removed our generated launcher; an external daemon now integrates this AppImage");
      } catch (e) {
        note(`could not remove our launcher: ${e && e.message}`);
      }
    }
    return { skipped: true, reason: "external integration present" };
  }

  const stampPath = path.join(stampDir, "linux-desktop-integration.json");
  if (isCurrent(fs, stampPath, version, plan.appImage)) return { skipped: true, reason: "already current" };

  // 1. Copy every shipped hicolor size for our icon name (no-follow atomic).
  const srcRoot = path.join(plan.appDir, "usr", "share", "icons", "hicolor");
  let installed = 0;
  for (const size of ICON_SIZES) {
    const src = path.join(srcRoot, size, "apps", `${iconName}.png`);
    try {
      if (!fs.existsSync(src)) continue;
      const destDir = path.join(iconRoot, size, "apps");
      fs.mkdirSync(destDir, { recursive: true });
      safeWrite(fs, path, share, path.join(destDir, `${iconName}.png`), fs.readFileSync(src));
      installed += 1;
    } catch (e) {
      note(`icon ${size} install failed: ${e && e.message}`);
    }
  }
  if (installed === 0) {
    note("no shipped icons found under the mount; skipping");
    return { skipped: true, reason: "no icons in bundle" };
  }

  // 2. Write our launcher, pointing Exec at the real AppImage path (no-follow).
  //    Prove ownership before overwriting: a same-name launcher WITHOUT our
  //    OWNERSHIP_KEY is a hand-made or third-party file, so back it up once
  //    rather than silently clobbering it.
  try {
    fs.mkdirSync(appsDir, { recursive: true });
    if (launcherOwnership(fs, launcherPath) === "foreign") {
      const bak = `${launcherPath}.kirocrew-bak`;
      try {
        if (!fs.existsSync(bak)) {
          safeWrite(fs, path, share, bak, fs.readFileSync(launcherPath));
          note(`backed up an existing non-generated launcher to ${bak}`);
        }
      } catch (e) {
        note(`backup of existing launcher failed: ${e && e.message}`);
      }
    }
    const body = renderDesktopEntry({
      name: entry.Name || "Kiro Crew",
      comment: entry.Comment,
      execLine: rewriteExec(entry.Exec, plan.appImage),
      iconName,
      wmClass: entry.StartupWMClass || iconName,
      categories: entry.Categories,
      generated: version,
    });
    safeWrite(fs, path, share, launcherPath, body);
  } catch (e) {
    note(`launcher write failed: ${e && e.message}`);
    return { skipped: true, reason: "launcher write failed" };
  }

  // 3. Refresh the desktop caches, best-effort, across the mainstream DEs.
  //    Each binary is resolved to an absolute path from SYSTEM_BIN_DIRS (never
  //    $PATH) and skipped if absent — the files on disk are what matter, and
  //    each DE also picks them up on its own dir-watch or relogin.
  //      - gtk-update-icon-cache: GTK icon-theme cache (GNOME/XFCE/Cinnamon).
  //      - update-desktop-database: freedesktop MIME/app database.
  //      - kbuildsycoca6 / kbuildsycoca5: KDE Plasma 6 / 5 service cache.
  //    Qt/KDE reads the hicolor dirs directly, so no KDE icon-cache tool exists
  //    or is needed; the sycoca rebuild is the launcher-database analog.
  if (run) {
    const refreshers = [
      ["gtk-update-icon-cache", ["-f", "-t", iconRoot]],
      ["update-desktop-database", [appsDir]],
      ["kbuildsycoca6", []],
      ["kbuildsycoca5", []],
    ];
    for (const [name, args] of refreshers) {
      const bin = resolveSystemBinary(fs, path, name);
      if (!bin) { note(`refresher ${name} not found in a system dir; skipping`); continue; }
      try { run(bin, args); } catch { /* best-effort */ }
    }
  }

  // 4. Stamp so this runs once per (version, install path), not every boot.
  try {
    fs.mkdirSync(stampDir, { recursive: true });
    safeWrite(fs, path, stampDir, stampPath,
      JSON.stringify({ version, iconName, sizes: installed, appImage: plan.appImage }) + "\n");
  } catch (e) {
    note(`stamp write failed (integration will re-run next boot): ${e && e.message}`);
  }

  note(`installed ${iconName} at ${installed} size(s)`);
  return { installed: true, iconName, sizes: installed };
}

module.exports = {
  parseDesktopEntry,
  escapeExecArg,
  rewriteExec,
  renderDesktopEntry,
  planIntegration,
  dataHome,
  isCurrent,
  execRefersToPath,
  launcherOwnership,
  hasExternalIntegration,
  integrateLinuxDesktop,
};
