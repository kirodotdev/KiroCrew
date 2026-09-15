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
 * Behaviour: silent and self-healing. Launching the app is consent. A private
 * manifest records the hashes, inode anchors, and metadata of every launcher
 * and icon Kiro Crew writes. A later release or moved AppImage updates a public
 * file only while that provenance still matches; any unknown, unreadable,
 * special, content-edited, or metadata-edited file is preserved. Pre-existing
 * manual launchers have no manifest provenance and are never adopted.
 * `KIROCREW_DISABLE_DESKTOP_INTEGRATION` disables all integration work.
 *
 * Safety: publication uses same-directory temporary files, durable manifest
 * intent, and atomic no-replace hard links. Managed replacement runs inside
 * the packaged helper while it holds kernel write leases on both the old and
 * generated inodes from verification through displacement, publication, and
 * interrupted-publication recovery. An existing descriptor therefore defers
 * generated publication instead of losing a concurrent edit. One live hard-link
 * anchor proves current ownership. Extended-metadata fingerprints are captured
 * before publication and retained as provenance. Cleanup prevalidates each
 * superseded inode, then the packaged helper opens its unpredictable recovery
 * path, acquires a kernel write lease, re-verifies bytes, inode, POSIX metadata,
 * xattrs, and ACLs through that descriptor, and unlinks the same path. Changed
 * or uninspectable data is never deleted, and incomplete cleanup keeps its
 * manifest record for a later run. The preservation contract covers edits to
 * public managed paths and writes through descriptors opened before cleanup. It
 * does not treat a same-UID process that discovers an unpredictable internal
 * recovery name and races the final unlink as customization; such a process can
 * already unlink the user's files directly. This boundary keeps ordinary
 * cleanup bounded while failing closed for normal concurrent filesystem
 * activity.
 * Missing parent directories are created one component at a time without
 * following existing symlinks. The private manifest root is created as 0700
 * regardless of umask. Cache-refresh binaries are resolved only from fixed
 * system directories, never via `PATH`.
 *
 * The AppImage type-2 runtime exports two variables to the running process that
 * make this possible without shipping any path assumptions:
 *   - `APPIMAGE` — absolute path to the .AppImage file (the launcher's Exec)
 *   - `APPDIR`   — the mount root, so the bundled `.desktop` and the hicolor
 *                  icon tree are readable at `${APPDIR}/...`
 * The Electron shell first resolves the install kind from package-type,
 * resourcesPath, and APPIMAGE, with package identity taking precedence. This
 * module runs only when that result is `appimage` and both runtime paths are
 * present, so an identified deb/rpm process that inherits these variables still
 * no-ops.
 *
 * Pure helpers (parse/rewrite/render/plan) carry the logic and are unit-tested;
 * `integrateLinuxDesktop` is the thin impure orchestrator that takes its fs,
 * path, os, env, platform, install-kind, run, and log dependencies injected,
 * matching this package's other modules (see data-home.js, gateway-stop.js).
 */

"use strict";

const crypto = require("crypto");
const posixPath = require("path").posix;

const MANIFEST_SCHEMA = 2;
const MANIFEST_FILE = "state.json";
const MANIFEST_DIR = "linux-desktop-integration";
const MAX_MANIFEST_BYTES = 64 * 1024;
const MAX_ARTIFACT_BYTES = 16 * 1024 * 1024;
let tempNonce = 0;

function uniqueSidePath(base, kind) {
  tempNonce += 1;
  const random = crypto.randomBytes(8).toString("hex");
  return `${base}.${kind}-${process.pid}-${Date.now()}-${tempNonce}-${random}`;
}

function isGeneratedSidePath(artifactPath, candidate, kinds) {
  if (typeof candidate !== "string") return false;
  for (const kind of kinds) {
    const prefix = `${artifactPath}.${kind}-`;
    if (candidate.startsWith(prefix) &&
        /^\d+-\d+-\d+-[0-9a-f]{16}$/.test(candidate.slice(prefix.length))) {
      return true;
    }
  }
  return false;
}

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
 * program token is replaced with the escaped, quoted AppImage path. The
 * builder's unconditional `--no-sandbox` token is removed: AppRun performs its
 * own user-namespace probe and adds that fallback only on hosts that need it.
 * Every other argument and field code is preserved.
 */
function rewriteExec(bundledExec, appImagePath) {
  const quoted = escapeExecArg(appImagePath);
  const tokens = String(bundledExec || "").trim().split(/\s+/).filter(Boolean);
  const args = tokens.slice(1).filter((token) => token !== "--no-sandbox");
  if (args.length === 0) return `${quoted} %U`;
  return [quoted, ...args].join(" ");
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
  return lines.join("\n") + "\n";
}

/**
 * Decide whether to run at all. Integration is Linux-only, requires an install
 * kind already resolved as `appimage` plus both AppImage runtime variables, and
 * is skipped when the user opts out via
 * `KIROCREW_DISABLE_DESKTOP_INTEGRATION` (truthy 1/true/yes/on, matching the
 * `KIROCREW_DISABLE_GPU` convention). `platform` and `installKind` are injected
 * rather than inferred from environment presence. Returns
 * { integrate, appImage, appDir, reason }.
 */
function planIntegration(env, platform = process.platform, installKind = "unknown") {
  if (platform !== "linux") return { integrate: false, reason: "not linux" };
  const optOut = env && env.KIROCREW_DISABLE_DESKTOP_INTEGRATION;
  if (typeof optOut === "string" && TRUTHY.has(optOut.trim().toLowerCase())) {
    return { integrate: false, reason: "opt-out via KIROCREW_DISABLE_DESKTOP_INTEGRATION" };
  }
  if (installKind !== "appimage") return { integrate: false, reason: "not an AppImage install" };
  const appImage = env && env.APPIMAGE;
  const appDir = env && env.APPDIR;
  if (typeof appImage !== "string" || typeof appDir !== "string" || !appImage || !appDir) {
    return { integrate: false, reason: "missing AppImage runtime paths" };
  }
  if (/[\x00-\x1f\x7f]/.test(appImage) || /[\x00-\x1f\x7f]/.test(appDir)) {
    return { integrate: false, reason: "invalid AppImage runtime paths" };
  }
  return { integrate: true, appImage, appDir };
}

/**
 * The XDG data home the icons and launcher live under: `$XDG_DATA_HOME` when
 * set (and absolute), else `~/.local/share`.
 */
function dataHome(env, home) {
  const canonical = (value) => {
    const normalized = posixPath.normalize(value);
    return normalized === "/" ? normalized : normalized.replace(/\/+$/, "");
  };
  const xdg = env && env.XDG_DATA_HOME;
  if (xdg && xdg.startsWith("/")) return canonical(xdg);
  return posixPath.join(canonical(home), ".local", "share");
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

const METADATA_HELPER_SCRIPT = [
  "import fcntl, hashlib, json, os, signal, stat, sys",
  "signal.signal(signal.SIGIO, lambda *_: None)",
  "mode, target = sys.argv[1], sys.argv[2]",
  "selected = lambda n: n.startswith('user.') or n in ('system.posix_acl_access', 'system.posix_acl_default')",
  "def extended(source):",
  "    names = sorted(n for n in os.listxattr(source) if selected(n))",
  "    digest = hashlib.sha256()",
  "    for name in names:",
  "        key = name.encode('utf-8', 'surrogateescape')",
  "        value = os.getxattr(source, name)",
  "        digest.update(len(key).to_bytes(8, 'big'))",
  "        digest.update(key)",
  "        digest.update(len(value).to_bytes(8, 'big'))",
  "        digest.update(value)",
  "    return digest.hexdigest()",
  "def content_hash(source):",
  "    os.lseek(source, 0, os.SEEK_SET)",
  "    digest = hashlib.sha256()",
  "    while True:",
  "        block = os.read(source, 1024 * 1024)",
  "        if not block: break",
  "        digest.update(block)",
  "    return digest.hexdigest()",
  "def sync_parent(path):",
  "    parent = os.path.dirname(path) or '.'",
  "    dirfd = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))",
  "    try: os.fsync(dirfd)",
  "    finally: os.close(dirfd)",
  "if mode == 'fingerprint':",
  "    print(extended(target))",
  "    raise SystemExit(0)",
  "expected = json.loads(sys.argv[3])",
  "side_path = sys.argv[4] if len(sys.argv) > 4 else None",
  "lease_target = side_path if mode == 'recover' else target",
  "fd = None",
  "leased = False",
  "new_fd = None",
  "new_leased = False",
  "moved = False",
  "def outcome(value):",
  "    print(value)",
  "    raise SystemExit(0)",
  "try:",
  "    flags = os.O_RDWR | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)",
  "    try:",
  "        fd = os.open(lease_target, flags)",
  "    except FileNotFoundError:",
  "        outcome('absent')",
  "    try:",
  "        fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_WRLCK)",
  "        leased = True",
  "    except OSError:",
  "        outcome('busy')",
  "    st = os.fstat(fd)",
  "    path_st = os.lstat(lease_target)",
  "    matches = (stat.S_ISREG(st.st_mode) and st.st_dev == expected['dev'] and",
  "               st.st_ino == expected['ino'] and content_hash(fd) == expected['hash'] and",
  "               st.st_mode == expected['metadata']['mode'] and st.st_uid == expected['metadata']['uid'] and",
  "               st.st_gid == expected['metadata']['gid'] and extended(fd) == expected['extended'] and",
  "               path_st.st_dev == st.st_dev and path_st.st_ino == st.st_ino)",
  "    if not matches:",
  "        outcome('changed')",
  "    if mode in ('publish', 'recover'):",
  "        displaced, temp = sys.argv[4], sys.argv[5]",
  "        desired = json.loads(sys.argv[6])",
  "        new_fd = os.open(temp, flags)",
  "        try:",
  "            fcntl.fcntl(new_fd, fcntl.F_SETLEASE, fcntl.F_WRLCK)",
  "            new_leased = True",
  "        except OSError:",
  "            outcome('busy')",
  "        new_st = os.fstat(new_fd)",
  "        new_path_st = os.lstat(temp)",
  "        new_matches = (stat.S_ISREG(new_st.st_mode) and",
  "                       new_st.st_dev == desired['dev'] and new_st.st_ino == desired['ino'] and",
  "                       content_hash(new_fd) == desired['hash'] and",
  "                       new_st.st_mode == desired['metadata']['mode'] and",
  "                       new_st.st_uid == desired['metadata']['uid'] and",
  "                       new_st.st_gid == desired['metadata']['gid'] and",
  "                       extended(new_fd) == desired['extended'] and",
  "                       new_path_st.st_dev == new_st.st_dev and new_path_st.st_ino == new_st.st_ino)",
  "        if not new_matches:",
  "            outcome('changed')",
  "        if mode == 'publish':",
  "            os.rename(target, displaced)",
  "            moved = True",
  "            sync_parent(target)",
  "            old_path_st = os.lstat(displaced)",
  "        else:",
  "            try:",
  "                os.lstat(target)",
  "                outcome('changed')",
  "            except FileNotFoundError:",
  "                pass",
  "            old_path_st = os.lstat(lease_target)",
  "        old_final = os.fstat(fd)",
  "        old_matches = (old_path_st.st_dev == old_final.st_dev and old_path_st.st_ino == old_final.st_ino and",
  "                       old_final.st_dev == expected['dev'] and old_final.st_ino == expected['ino'] and",
  "                       content_hash(fd) == expected['hash'] and",
  "                       old_final.st_mode == expected['metadata']['mode'] and",
  "                       old_final.st_uid == expected['metadata']['uid'] and",
  "                       old_final.st_gid == expected['metadata']['gid'] and",
  "                       extended(fd) == expected['extended'])",
  "        if not old_matches:",
  "            if mode == 'publish':",
  "                try: os.link(displaced, target, follow_symlinks=False)",
  "                except OSError: pass",
  "            outcome('changed')",
  "        os.link(temp, target, follow_symlinks=False)",
  "        os.fsync(new_fd)",
  "        public_st = os.fstat(new_fd)",
  "        public_path_st = os.lstat(target)",
  "        temp_path_st = os.lstat(temp)",
  "        public_matches = (public_st.st_dev == desired['dev'] and public_st.st_ino == desired['ino'] and",
  "                          public_path_st.st_dev == public_st.st_dev and public_path_st.st_ino == public_st.st_ino and",
  "                          temp_path_st.st_dev == public_st.st_dev and temp_path_st.st_ino == public_st.st_ino and",
  "                          content_hash(new_fd) == desired['hash'] and",
  "                          public_st.st_mode == desired['metadata']['mode'] and",
  "                          public_st.st_uid == desired['metadata']['uid'] and",
  "                          public_st.st_gid == desired['metadata']['gid'] and",
  "                          extended(new_fd) == desired['extended'])",
  "        sync_parent(target)",
  "        if not public_matches:",
  "            outcome('changed')",
  "        moved = False",
  "        result = {'status': 'published',",
  "                  'oldMetadata': {'mode': old_final.st_mode, 'uid': old_final.st_uid,",
  "                                  'gid': old_final.st_gid, 'ctimeMs': old_final.st_ctime_ns / 1000000.0}}",
  "        outcome(json.dumps(result, separators=(',', ':')))",
  "    final_path = os.lstat(target)",
  "    final = os.fstat(fd)",
  "    if (final_path.st_dev != final.st_dev or final_path.st_ino != final.st_ino or",
  "        final.st_dev != expected['dev'] or final.st_ino != expected['ino'] or",
  "        final.st_mode != expected['metadata']['mode'] or",
  "        final.st_uid != expected['metadata']['uid'] or",
  "        final.st_gid != expected['metadata']['gid'] or",
  "        content_hash(fd) != expected['hash'] or",
  "        extended(fd) != expected['extended']):",
  "        outcome('changed')",
  "    # Recovery names already have unpredictable 128-bit suffixes. The",
  "    # approved same-UID boundary therefore permits direct leased unlink.",
  "    os.unlink(target)",
  "    sync_parent(target)",
  "    outcome('deleted')",
  "except SystemExit:",
  "    raise",
  "except Exception:",
  "    if moved:",
  "        try: os.link(side_path, target, follow_symlinks=False)",
  "        except OSError: pass",
  "    outcome('error')",
  "finally:",
  "    if new_leased and new_fd is not None:",
  "        try: fcntl.fcntl(new_fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)",
  "        except OSError: pass",
  "    if new_fd is not None:",
  "        try: os.close(new_fd)",
  "        except OSError: pass",
  "    if leased and fd is not None:",
  "        try: fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)",
  "        except OSError: pass",
  "    if fd is not None:",
  "        try: os.close(fd)",
  "        except OSError: pass",
].join("\n");

function runMetadataHelper(fs, path, spawnSync, resourcesPath, args) {
  if (typeof spawnSync !== "function" || typeof resourcesPath !== "string") return null;
  const binDir = path.join(resourcesPath, "backend-dist", "kirocrew-backend", "bin");
  let python;
  try {
    const name = fs.readdirSync(binDir).find((entry) => /^python3\.\d+$/.test(entry));
    if (!name) return null;
    python = path.join(binDir, name);
  } catch {
    return null;
  }

  try {
    const result = spawnSync(
      python, ["-I", "-c", METADATA_HELPER_SCRIPT, ...args],
      { encoding: "utf8", timeout: 5000, maxBuffer: 1024 * 1024, windowsHide: true }
    );
    return result && result.status === 0 ? String(result.stdout || "").trim() : null;
  } catch {
    return null;
  }
}

/** Replace a managed path while holding leases on both old and new inodes. */
function publishManagedArtifact(
  fs, path, spawnSync, resourcesPath, target, displaced, temp, previous, pending
) {
  const value = runMetadataHelper(fs, path, spawnSync, resourcesPath, [
    "publish", target, JSON.stringify(previous), displaced, temp,
    JSON.stringify({
      hash: pending.hash,
      dev: pending.dev,
      ino: pending.ino,
      metadata: pending.metadata,
      extended: pending.extended,
    }),
  ]);
  if (!value) return { status: "error" };
  if (["busy", "changed", "error"].includes(value)) return { status: value };
  try {
    const parsed = JSON.parse(value);
    return parsed && parsed.status === "published" && validMetadata(parsed.oldMetadata)
      ? parsed
      : { status: "error" };
  } catch {
    return { status: "error" };
  }
}

/** Fingerprint user xattrs and POSIX ACLs through the packaged interpreter. */
function fingerprintExtendedMetadata(fs, path, spawnSync, resourcesPath, target) {
  const value = runMetadataHelper(
    fs, path, spawnSync, resourcesPath, ["fingerprint", target]
  );
  return value && /^[0-9a-f]{64}$/.test(value) ? value : null;
}

/** Delete an unchanged random recovery path while its inode is write-leased. */
function deleteRecoveryPath(
  fs, path, spawnSync, resourcesPath, target, recovery
) {
  const value = runMetadataHelper(fs, path, spawnSync, resourcesPath, [
    "delete", target, JSON.stringify(recovery),
  ]);
  return ["absent", "busy", "changed", "deleted", "error"].includes(value) ? value : null;
}

/** Republish a generated inode only while the retained prior inode is leased. */
function recoverManagedArtifact(
  fs, path, spawnSync, resourcesPath, target, priorPath, temp, previous, pending
) {
  const value = runMetadataHelper(fs, path, spawnSync, resourcesPath, [
    "recover", target, JSON.stringify(previous), priorPath, temp,
    JSON.stringify({
      hash: pending.hash,
      dev: pending.dev,
      ino: pending.ino,
      metadata: pending.metadata,
      extended: pending.extended,
    }),
  ]);
  if (!value) return { status: "error" };
  if (["busy", "changed", "error"].includes(value)) return { status: value };
  try {
    const parsed = JSON.parse(value);
    return parsed && parsed.status === "published" && validMetadata(parsed.oldMetadata)
      ? parsed
      : { status: "error" };
  } catch {
    return { status: "error" };
  }
}

/** Create a possibly-missing root and fsync each new parent entry. */
function ensureDurableRoot(fs, path, root) {
  const pending = [];
  let cursor = root;
  while (true) {
    try {
      fs.lstatSync(cursor);
      break;
    } catch (e) {
      if (!e || e.code !== "ENOENT") throw e;
      const parent = path.dirname(cursor);
      if (parent === cursor) throw new Error(`could not find existing ancestor for ${root}`);
      pending.push(cursor);
      cursor = parent;
    }
  }
  for (const target of pending.reverse()) {
    fs.mkdirSync(target);
    syncDirectory(fs, path.dirname(target));
  }
}

/**
 * Create `dir` below `root` one component at a time. Existing symlinks and
 * components whose real path leaves the root are rejected before a child is
 * created through them. The root itself is the configured XDG trust boundary
 * and may resolve through a user-selected symlink.
 */
function ensureSafeDirectory(fs, path, root, dir, createMode) {
  ensureDurableRoot(fs, path, root);
  const realRoot = fs.realpathSync(root);
  const insideRoot = (candidate) =>
    candidate === realRoot || candidate.startsWith(realRoot + "/");
  const pending = [];
  let cursor = dir;

  while (cursor !== root) {
    if (!cursor.startsWith(root + "/")) {
      throw new Error(`refusing directory outside ${root}: ${cursor}`);
    }
    const parent = path.dirname(cursor);
    if (parent === cursor) throw new Error(`could not reach directory root ${root}`);
    try {
      const st = fs.lstatSync(cursor);
      if (st && st.isSymbolicLink()) {
        throw new Error(`refusing symlinked directory: ${cursor}`);
      }
      const realCursor = fs.realpathSync(cursor);
      if (!insideRoot(realCursor)) {
        throw new Error(`refusing directory outside ${realRoot}: ${realCursor}`);
      }
      cursor = parent;
      continue;
    } catch (e) {
      if (!e || e.code !== "ENOENT") throw e;
      pending.push(cursor);
      cursor = parent;
    }
  }

  for (const target of pending.reverse()) {
    const realParent = fs.realpathSync(path.dirname(target));
    if (!insideRoot(realParent)) {
      throw new Error(`refusing directory outside ${realRoot}: ${realParent}`);
    }
    if (createMode === undefined) fs.mkdirSync(target);
    else fs.mkdirSync(target, { mode: createMode });
    syncDirectory(fs, path.dirname(target));
    const realTarget = fs.realpathSync(target);
    if (!insideRoot(realTarget)) {
      throw new Error(`refusing directory outside ${realRoot}: ${realTarget}`);
    }
  }
  return realRoot;
}

function syncPath(fs, target) {
  const fd = fs.openSync(target, "r");
  try {
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}

function syncDirectory(fs, dir) {
  syncPath(fs, dir);
}

function hashData(data) {
  const bytes = Buffer.isBuffer(data) ? data : Buffer.from(String(data));
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

/** Read a regular file without following a leaf symlink. */
function inspectArtifact(fs, artifactPath) {
  let before;
  try {
    before = fs.lstatSync(artifactPath);
  } catch (e) {
    return e && e.code === "ENOENT"
      ? { status: "absent" }
      : { status: "unknown", error: e };
  }
  if (!before || before.isSymbolicLink() ||
      (typeof before.isFile === "function" && !before.isFile()) ||
      (typeof before.size === "number" && before.size > MAX_ARTIFACT_BYTES)) {
    return { status: "unknown" };
  }
  try {
    const data = fs.readFileSync(artifactPath);
    const after = fs.lstatSync(artifactPath);
    if (!after || after.isSymbolicLink() ||
        (typeof after.isFile === "function" && !after.isFile())) {
      return { status: "unknown" };
    }
    for (const key of ["dev", "ino", "size", "mtimeMs"]) {
      if (before[key] !== undefined && after[key] !== undefined && before[key] !== after[key]) {
        return { status: "unknown" };
      }
    }
    return {
      status: "file",
      data,
      hash: hashData(data),
      dev: after.dev,
      ino: after.ino,
      mode: after.mode,
      uid: after.uid,
      gid: after.gid,
      ctimeMs: after.ctimeMs,
    };
  } catch (e) {
    return { status: "unknown", error: e };
  }
}

function emptyManifest() {
  return { schema: MANIFEST_SCHEMA, artifacts: {} };
}

/** Load only the bounded manifest shape this module writes. */
function loadManifest(fs, manifestPath) {
  const snapshot = inspectArtifact(fs, manifestPath);
  if (snapshot.status === "absent") return emptyManifest();
  if (snapshot.status !== "file") return null;
  const bytes = Buffer.isBuffer(snapshot.data)
    ? snapshot.data.length
    : Buffer.byteLength(String(snapshot.data));
  if (bytes > MAX_MANIFEST_BYTES) return null;
  try {
    const parsed = JSON.parse(String(snapshot.data));
    if (!parsed || parsed.schema !== MANIFEST_SCHEMA ||
        !parsed.artifacts || typeof parsed.artifacts !== "object" ||
        Array.isArray(parsed.artifacts)) return null;
    for (const [artifactPath, record] of Object.entries(parsed.artifacts)) {
      if (!record || !Array.isArray(record.owned) ||
          record.owned.some((h) => !/^[0-9a-f]{64}$/.test(h))) return null;
      if (record.owned.length > 0 &&
          (!isGeneratedSidePath(artifactPath, record.anchorPath, ["tmp"]) ||
           !validMetadata(record.metadata) ||
           !/^[0-9a-f]{64}$/.test(record.extended))) return null;
      if (record.recovery !== undefined &&
          (!validRecovery(record.recovery) ||
           record.recovery.paths.some((p) =>
             !isGeneratedSidePath(artifactPath, p, ["tmp", "kirocrew-displaced"])))) return null;
      if (record.pending !== undefined && record.pending !== null) {
        const p = record.pending;
        if (!p || !/^[0-9a-f]{64}$/.test(p.hash) ||
            !/^[0-9a-f]{64}$/.test(p.extended) ||
            !Number.isSafeInteger(p.dev) || !Number.isSafeInteger(p.ino) ||
            !validMetadata(p.metadata) ||
            !isGeneratedSidePath(artifactPath, p.tempPath, ["tmp"])) return null;
        if (p.displacedPath !== undefined &&
            !isGeneratedSidePath(artifactPath, p.displacedPath, ["kirocrew-displaced"])) return null;
        if (p.previous !== undefined &&
            (!validRecovery(p.previous) ||
             p.previous.paths.some((candidate) =>
               !isGeneratedSidePath(artifactPath, candidate, ["tmp", "kirocrew-displaced"])))) return null;
      }
    }
    return parsed;
  } catch {
    return null;
  }
}

/** Establish that manifest provenance lives in an app-owned private directory. */
function prepareStateRoot(fs, path, stateDir, stateRoot, uid) {
  ensureSafeDirectory(fs, path, stateDir, stateRoot, 0o700);
  const st = fs.lstatSync(stateRoot);
  if (!st || st.isSymbolicLink() ||
      (typeof st.isDirectory === "function" && !st.isDirectory())) {
    throw new Error(`refusing unsafe manifest directory: ${stateRoot}`);
  }
  if (uid !== null && uid !== undefined && st.uid !== undefined && st.uid !== uid) {
    throw new Error(`refusing manifest directory owned by another user: ${stateRoot}`);
  }
  if (typeof st.mode === "number" && (st.mode & 0o022) !== 0) {
    throw new Error(`refusing writable manifest directory: ${stateRoot}`);
  }
}

/** Atomically replace the private manifest; report ambiguous post-rename failures. */
function writeManifest(fs, path, stateRoot, manifestPath, state) {
  ensureSafeDirectory(fs, path, path.dirname(stateRoot), stateRoot);
  const tmp = uniqueSidePath(manifestPath, "tmp");
  fs.writeFileSync(tmp, JSON.stringify(state) + "\n", { flag: "wx", mode: 0o600 });
  let committed = false;
  try {
    syncPath(fs, tmp);
    fs.renameSync(tmp, manifestPath);
    committed = true;
    syncPath(fs, manifestPath);
    syncDirectory(fs, stateRoot);
  } catch (e) {
    try { fs.unlinkSync(tmp); } catch { /* renamed temp or cleanup failure */ }
    if (e && typeof e === "object") e.manifestMayReferenceState = committed;
    throw e;
  }
}

function validMetadata(metadata) {
  return !!metadata && ["mode", "uid", "gid", "ctimeMs"].every(
    (key) => typeof metadata[key] === "number" && Number.isFinite(metadata[key])
  );
}

function artifactMetadata(snapshot) {
  return {
    mode: snapshot.mode,
    uid: snapshot.uid,
    gid: snapshot.gid,
    ctimeMs: snapshot.ctimeMs,
  };
}

function metadataMatches(expected, snapshot) {
  return validMetadata(expected) && validMetadata(snapshot) &&
    ["mode", "uid", "gid", "ctimeMs"].every((key) => expected[key] === snapshot[key]);
}

function stableMetadataMatches(expected, snapshot) {
  return validMetadata(expected) && validMetadata(snapshot) &&
    ["mode", "uid", "gid"].every((key) => expected[key] === snapshot[key]);
}

function validRecovery(recovery) {
  return !!recovery && /^[0-9a-f]{64}$/.test(recovery.hash) &&
    /^[0-9a-f]{64}$/.test(recovery.extended) &&
    Number.isSafeInteger(recovery.dev) && Number.isSafeInteger(recovery.ino) &&
    validMetadata(recovery.metadata) &&
    Array.isArray(recovery.paths) && recovery.paths.length > 0 &&
    recovery.paths.length <= 2 && recovery.paths.every((p) => typeof p === "string");
}

function recordOwns(fs, record, snapshot, artifactPath) {
  if (!record || !snapshot || snapshot.status !== "file" ||
      !Array.isArray(record.owned) || !record.owned.includes(snapshot.hash) ||
      !metadataMatches(record.metadata, snapshot) ||
      !isGeneratedSidePath(artifactPath, record.anchorPath, ["tmp"])) return false;
  const anchor = inspectArtifact(fs, record.anchorPath);
  return anchor.status === "file" && anchor.hash === snapshot.hash &&
    sameIdentity(anchor, snapshot) && metadataMatches(record.metadata, anchor);
}

function pendingMatches(fs, record, snapshot, artifactPath, fingerprintMetadata) {
  const pending = record && record.pending;
  if (!pending || !snapshot || snapshot.status !== "file" ||
      typeof fingerprintMetadata !== "function" ||
      !isGeneratedSidePath(artifactPath, pending.tempPath, ["tmp"]) ||
      pending.hash !== snapshot.hash || pending.dev !== snapshot.dev ||
      pending.ino !== snapshot.ino) return false;
  const retained = inspectArtifact(fs, pending.tempPath);
  const extended = fingerprintMetadata(artifactPath);
  return retained.status === "file" && retained.hash === pending.hash &&
    sameIdentity(retained, snapshot) &&
    stableMetadataMatches(pending.metadata, retained) &&
    stableMetadataMatches(pending.metadata, snapshot) &&
    extended !== null && extended === pending.extended;
}

function pendingRecord(
  record, desiredHash, temp, tempPath, tempExtended, displacedPath, current, previousExtended
) {
  const pending = {
    hash: desiredHash,
    dev: temp.dev,
    ino: temp.ino,
    metadata: artifactMetadata(temp),
    extended: tempExtended,
    tempPath,
  };
  if (displacedPath) {
    pending.displacedPath = displacedPath;
    pending.previous = {
      hash: current.hash,
      dev: current.dev,
      ino: current.ino,
      metadata: artifactMetadata(current),
      extended: previousExtended,
      paths: [record.anchorPath, displacedPath],
    };
  }
  const next = { owned: [...new Set((record && record.owned) || [])], pending };
  if (record && typeof record.anchorPath === "string") next.anchorPath = record.anchorPath;
  if (record && validMetadata(record.metadata)) next.metadata = record.metadata;
  if (record && /^[0-9a-f]{64}$/.test(record.extended)) next.extended = record.extended;
  if (record && validRecovery(record.recovery)) next.recovery = record.recovery;
  return next;
}

function restoredPendingMatches(fs, record, snapshot, artifactPath, fingerprintMetadata) {
  const previous = record && record.pending && record.pending.previous;
  if (!validRecovery(previous) || !snapshot || snapshot.status !== "file" ||
      typeof fingerprintMetadata !== "function" ||
      previous.hash !== snapshot.hash || previous.dev !== snapshot.dev ||
      previous.ino !== snapshot.ino ||
      !isGeneratedSidePath(artifactPath, record.anchorPath, ["tmp"]) ||
      !previous.paths.includes(record.anchorPath)) return false;
  const anchor = inspectArtifact(fs, record.anchorPath);
  const extended = fingerprintMetadata(artifactPath);
  return anchor.status === "file" && anchor.hash === snapshot.hash &&
    sameIdentity(anchor, snapshot) &&
    stableMetadataMatches(previous.metadata, snapshot) &&
    stableMetadataMatches(previous.metadata, anchor) &&
    extended !== null && extended === previous.extended;
}

function unlinkRecoveryGroup(
  fs, artifactPath, recovery, fingerprintMetadata, deleteMetadata
) {
  if (!validRecovery(recovery) || typeof fingerprintMetadata !== "function" ||
      typeof deleteMetadata !== "function") return false;

  // Prevalidate the whole hard-link group before removing any name. Unlinking
  // one name changes the shared inode ctime, so sibling checks after mutation
  // intentionally use only stable metadata plus the persisted xattr/ACL hash.
  const candidates = [];
  for (const recoveryPath of recovery.paths) {
    if (!isGeneratedSidePath(
      artifactPath, recoveryPath, ["tmp", "kirocrew-displaced"]
    )) return false;
    const before = inspectArtifact(fs, recoveryPath);
    if (before.status === "absent") continue;
    if (before.status === "unknown") return false;
    const beforeExtended = fingerprintMetadata(recoveryPath);
    if (beforeExtended === null) return false;
    if (before.status !== "file" || before.hash !== recovery.hash ||
        before.dev !== recovery.dev || before.ino !== recovery.ino ||
        !stableMetadataMatches(recovery.metadata, before) ||
        beforeExtended !== recovery.extended) {
      continue; // positively changed recovery data is now user-owned
    }
    candidates.push(recoveryPath);
  }

  let complete = true;
  for (const recoveryPath of candidates) {
    const before = inspectArtifact(fs, recoveryPath);
    if (before.status === "absent") continue;
    if (before.status === "unknown") {
      complete = false;
      continue;
    }
    const beforeExtended = fingerprintMetadata(recoveryPath);
    if (beforeExtended === null) {
      complete = false;
      continue;
    }
    if (before.status !== "file" || before.hash !== recovery.hash ||
        before.dev !== recovery.dev || before.ino !== recovery.ino ||
        !stableMetadataMatches(recovery.metadata, before) ||
        beforeExtended !== recovery.extended) {
      continue; // a change after prevalidation revokes cleanup authority
    }

    const outcome = deleteMetadata(recoveryPath, recovery);
    if (!["absent", "changed", "deleted"].includes(outcome)) complete = false;
  }
  return complete;
}

function restorePendingOwnership(
  fs, path, stateRoot, manifestPath, state, artifactPath, record, current,
  fingerprintMetadata, deleteMetadata
) {
  const pending = record.pending;
  const pendingCleaned = unlinkRecoveryGroup(fs, artifactPath, {
    hash: pending.hash,
    dev: pending.dev,
    ino: pending.ino,
    metadata: pending.metadata,
    extended: pending.extended,
    paths: [pending.tempPath],
  }, fingerprintMetadata, deleteMetadata);
  if (!pendingCleaned) throw new Error(`pending cleanup deferred: ${artifactPath}`);
  let fresh = inspectArtifact(fs, artifactPath);
  let anchor = inspectArtifact(fs, record.anchorPath);
  let freshExtended = fingerprintMetadata(artifactPath);
  let anchorExtended = fingerprintMetadata(record.anchorPath);
  if (fresh.status !== "file" || fresh.hash !== current.hash ||
      !sameIdentity(fresh, current) || !sameIdentity(fresh, anchor) ||
      anchor.hash !== fresh.hash ||
      !stableMetadataMatches(pending.previous.metadata, fresh) ||
      !stableMetadataMatches(pending.previous.metadata, anchor) ||
      freshExtended === null || anchorExtended === null ||
      freshExtended !== pending.previous.extended ||
      anchorExtended !== pending.previous.extended) {
    throw new Error(`restored artifact changed during recovery: ${artifactPath}`);
  }
  const retiredPaths = pending.previous.paths.filter((p) => p !== record.anchorPath);
  if (retiredPaths.length > 0) {
    const retiredCleaned = unlinkRecoveryGroup(fs, artifactPath, {
      ...pending.previous,
      metadata: artifactMetadata(fresh),
      paths: retiredPaths,
    }, fingerprintMetadata, deleteMetadata);
    if (!retiredCleaned) throw new Error(`retired cleanup deferred: ${artifactPath}`);
  }
  fresh = inspectArtifact(fs, artifactPath);
  anchor = inspectArtifact(fs, record.anchorPath);
  freshExtended = fingerprintMetadata(artifactPath);
  anchorExtended = fingerprintMetadata(record.anchorPath);
  if (fresh.status !== "file" || fresh.hash !== current.hash ||
      !sameIdentity(fresh, current) || !sameIdentity(fresh, anchor) ||
      anchor.hash !== fresh.hash ||
      !stableMetadataMatches(pending.previous.metadata, fresh) ||
      !stableMetadataMatches(pending.previous.metadata, anchor) ||
      freshExtended === null || anchorExtended === null ||
      freshExtended !== pending.previous.extended ||
      anchorExtended !== pending.previous.extended) {
    throw new Error(`restored artifact changed during cleanup: ${artifactPath}`);
  }
  const restored = {
    owned: [fresh.hash],
    anchorPath: record.anchorPath,
    metadata: artifactMetadata(fresh),
    extended: pending.previous.extended,
  };
  state.artifacts[artifactPath] = restored;
  try {
    writeManifest(fs, path, stateRoot, manifestPath, state);
  } catch (e) {
    if (!(e && e.manifestMayReferenceState)) state.artifacts[artifactPath] = record;
    throw e;
  }
  return restored;
}

function sameIdentity(a, b) {
  return !!a && !!b && a.status === "file" && b.status === "file" &&
    a.dev !== undefined && a.ino !== undefined && a.dev === b.dev && a.ino === b.ino;
}

function promotePending(
  fs, path, stateRoot, manifestPath, state, artifactPath, record, current,
  fingerprintMetadata
) {
  const pending = record.pending;
  const anchorBefore = inspectArtifact(fs, pending.tempPath);
  const publicExtended = fingerprintMetadata(artifactPath);
  const anchorExtended = fingerprintMetadata(pending.tempPath);
  if (!sameIdentity(current, anchorBefore) || current.hash !== pending.hash ||
      anchorBefore.hash !== pending.hash ||
      !stableMetadataMatches(pending.metadata, current) ||
      !stableMetadataMatches(pending.metadata, anchorBefore) ||
      publicExtended === null || anchorExtended === null ||
      publicExtended !== pending.extended || anchorExtended !== pending.extended) {
    throw new Error(`pending artifact changed before ownership promotion: ${artifactPath}`);
  }
  let recovery;
  if (pending.previous) {
    let recoverySnapshot = null;
    for (const recoveryPath of pending.previous.paths) {
      const snapshot = inspectArtifact(fs, recoveryPath);
      if (snapshot.status === "absent") continue;
      const extended = snapshot.status === "file"
        ? fingerprintMetadata(recoveryPath)
        : null;
      if (snapshot.status !== "file" || snapshot.hash !== pending.previous.hash ||
          snapshot.dev !== pending.previous.dev || snapshot.ino !== pending.previous.ino ||
          !stableMetadataMatches(pending.previous.metadata, snapshot) ||
          extended === null || extended !== pending.previous.extended) {
        throw new Error(`recovery artifact changed before promotion: ${artifactPath}`);
      }
      recoverySnapshot = snapshot;
    }
    if (!recoverySnapshot) {
      throw new Error(`recovery artifact disappeared before promotion: ${artifactPath}`);
    }
    recovery = {
      ...pending.previous,
      metadata: artifactMetadata(recoverySnapshot),
    };
  }
  const promoted = {
    owned: [current.hash],
    anchorPath: pending.tempPath,
    metadata: artifactMetadata(current),
    extended: pending.extended,
  };
  if (recovery) promoted.recovery = recovery;
  state.artifacts[artifactPath] = promoted;
  writeManifest(fs, path, stateRoot, manifestPath, state);

  // Re-check bytes, inode, POSIX metadata, and extended metadata after the
  // promoted manifest is durable. If either hard-link name changed, put
  // pending state back before any future run can treat the inode as owned.
  const fresh = inspectArtifact(fs, artifactPath);
  const freshAnchor = inspectArtifact(fs, pending.tempPath);
  const freshExtended = fingerprintMetadata(artifactPath);
  const freshAnchorExtended = fingerprintMetadata(pending.tempPath);
  if (!recordOwns(fs, promoted, fresh, artifactPath) ||
      !sameIdentity(fresh, freshAnchor) ||
      !stableMetadataMatches(pending.metadata, fresh) ||
      !stableMetadataMatches(pending.metadata, freshAnchor) ||
      freshExtended === null || freshAnchorExtended === null ||
      freshExtended !== pending.extended || freshAnchorExtended !== pending.extended) {
    state.artifacts[artifactPath] = record;
    writeManifest(fs, path, stateRoot, manifestPath, state);
    throw new Error(`public artifact changed during ownership promotion: ${artifactPath}`);
  }

  // The new temp hard link is the active ownership anchor. Any superseded
  // anchor and displaced path stay recorded until a later launch proves their
  // inode and hash are unchanged, which keeps crash recovery without growing
  // one permanent generation per update.
  return promoted;
}

function recoveryForAbsent(fs, record, artifactPath, fingerprintMetadata) {
  if (!record) return null;
  if (record.pending && validRecovery(record.pending.previous)) {
    return record.pending.previous;
  }
  if (!isGeneratedSidePath(artifactPath, record.anchorPath, ["tmp"]) ||
      !Array.isArray(record.owned)) return null;
  const anchor = inspectArtifact(fs, record.anchorPath);
  if (anchor.status !== "file" || !record.owned.includes(anchor.hash) ||
      anchor.dev === undefined || anchor.ino === undefined ||
      !stableMetadataMatches(record.metadata, anchor)) return null;
  const extended = fingerprintMetadata(record.anchorPath);
  if (extended === null || extended !== record.extended) return null;
  return {
    hash: anchor.hash,
    dev: anchor.dev,
    ino: anchor.ino,
    metadata: artifactMetadata(anchor),
    extended,
    paths: [record.anchorPath],
  };
}

function cleanupRecovery(
  fs, path, stateRoot, manifestPath, state, artifactPath, record,
  fingerprintMetadata, deleteMetadata
) {
  const recovery = record && record.recovery;
  if (!recovery) return true;
  try {
    if (!unlinkRecoveryGroup(
      fs, artifactPath, recovery, fingerprintMetadata, deleteMetadata
    )) return false;
    const cleaned = { ...record };
    delete cleaned.recovery;
    state.artifacts[artifactPath] = cleaned;
    try {
      writeManifest(fs, path, stateRoot, manifestPath, state);
    } catch (e) {
      if (!(e && e.manifestMayReferenceState)) state.artifacts[artifactPath] = record;
      throw e;
    }
    return true;
  } catch {
    return false;
  }
}

function abandonPending(
  fs, path, stateRoot, manifestPath, state, artifactPath, record,
  fingerprintMetadata, deleteMetadata
) {
  const pending = record && record.pending;
  if (!pending) return;
  try {
    let complete = true;
    if (validRecovery(pending.previous)) {
      complete = unlinkRecoveryGroup(
        fs, artifactPath, pending.previous, fingerprintMetadata, deleteMetadata
      ) && complete;
    }
    complete = unlinkRecoveryGroup(fs, artifactPath, {
      hash: pending.hash,
      dev: pending.dev,
      ino: pending.ino,
      metadata: pending.metadata,
      extended: pending.extended,
      paths: [pending.tempPath],
    }, fingerprintMetadata, deleteMetadata) && complete;
    if (!complete) return;
    delete state.artifacts[artifactPath];
    try {
      writeManifest(fs, path, stateRoot, manifestPath, state);
    } catch (e) {
      if (!(e && e.manifestMayReferenceState)) state.artifacts[artifactPath] = record;
      throw e;
    }
  } catch {
    // Keep pending provenance for a later cleanup attempt. The external file at
    // the canonical path remains untouched either way.
  }
}

/**
 * Reconcile one public artifact. Unknown content is preserved. `owned` hashes
 * authorize updates; a `pending` hash never does. Pending publication is
 * promoted only when the public path has the exact temporary inode recorded
 * before publication. Managed updates move the old inode aside, verify it, and
 * hard-link the desired inode into the vacant path so a concurrent writer wins.
 */
function reconcileArtifact(
  fs, path, root, stateRoot, manifestPath, state, artifactPath, desired,
  fingerprintMetadata, deleteMetadata, publishMetadata, recoverMetadata
) {
  const desiredHash = hashData(desired);
  let current = inspectArtifact(fs, artifactPath);
  let record = state.artifacts[artifactPath];
  let promotedNow = false;

  if (current.status === "unknown") return { status: "preserved" };
  if (current.status === "absent" && record && record.recovery) {
    if (!cleanupRecovery(
      fs, path, stateRoot, manifestPath, state, artifactPath, record,
      fingerprintMetadata, deleteMetadata
    )) {
      return { status: "deferred" };
    }
    record = state.artifacts[artifactPath];
  }
  if (current.status === "absent" && record && record.pending) {
    const pending = record.pending;
    const retainedPathSafe = isGeneratedSidePath(artifactPath, pending.tempPath, ["tmp"]);
    const retained = retainedPathSafe
      ? inspectArtifact(fs, pending.tempPath)
      : { status: "unknown" };
    const retainedExtended = retained.status === "file"
      ? fingerprintMetadata(pending.tempPath)
      : null;
    const retainedMatches = retainedPathSafe &&
      retained.status === "file" && retained.hash === pending.hash &&
      retained.dev === pending.dev && retained.ino === pending.ino &&
      metadataMatches(pending.metadata, retained) &&
      retainedExtended !== null && retainedExtended === pending.extended;
    if (retainedMatches) {
      if (validRecovery(pending.previous)) {
        const priorPath = pending.previous.paths.find((candidate) => {
          const snapshot = inspectArtifact(fs, candidate);
          return snapshot.status === "file" && snapshot.hash === pending.previous.hash &&
            snapshot.dev === pending.previous.dev && snapshot.ino === pending.previous.ino;
        });
        if (!priorPath) return { status: "preserved" };
        const recovery = recoverMetadata(
          artifactPath, priorPath, pending.tempPath, pending.previous, pending
        );
        if (!recovery || recovery.status !== "published") {
          if (inspectArtifact(fs, artifactPath).status === "absent") {
            try {
              fs.linkSync(priorPath, artifactPath);
              syncPath(fs, artifactPath);
              syncDirectory(fs, path.dirname(artifactPath));
            } catch { /* a concurrent canonical writer wins */ }
          }
          return { status: "preserved" };
        }
        pending.previous.metadata = recovery.oldMetadata;
      } else {
        try {
          fs.linkSync(pending.tempPath, artifactPath);
          syncPath(fs, artifactPath);
          syncDirectory(fs, path.dirname(artifactPath));
        } catch {
          return { status: "preserved" };
        }
      }
      try {
        const published = inspectArtifact(fs, artifactPath);
        if (!sameIdentity(retained, published) || published.hash !== pending.hash) {
          return { status: "preserved" };
        }
        promotePending(
          fs, path, stateRoot, manifestPath, state, artifactPath, record, published,
          fingerprintMetadata
        );
        syncDirectory(fs, path.dirname(artifactPath));
        return { status: "created", hash: published.hash };
      } catch {
        return { status: "preserved" };
      }
    }
  }
  if (current.status === "file" && pendingMatches(fs, record, current, artifactPath, fingerprintMetadata)) {
    try {
      record = promotePending(
        fs, path, stateRoot, manifestPath, state, artifactPath, record, current,
        fingerprintMetadata
      );
      promotedNow = true;
    } catch {
      return { status: "preserved" };
    }
  } else if (current.status === "file" &&
      restoredPendingMatches(fs, record, current, artifactPath, fingerprintMetadata)) {
    try {
      record = restorePendingOwnership(
        fs, path, stateRoot, manifestPath, state, artifactPath, record, current,
        fingerprintMetadata, deleteMetadata
      );
      current = inspectArtifact(fs, artifactPath);
    } catch {
      return { status: "preserved" };
    }
  }
  if (current.status === "file" && !recordOwns(fs, record, current, artifactPath)) {
    if (record && record.pending) {
      abandonPending(
        fs, path, stateRoot, manifestPath, state, artifactPath, record,
        fingerprintMetadata, deleteMetadata
      );
    }
    return { status: "preserved" };
  }
  if (current.status === "file" && !promotedNow && record && record.recovery) {
    const cleaned = cleanupRecovery(
      fs, path, stateRoot, manifestPath, state, artifactPath, record,
      fingerprintMetadata, deleteMetadata
    );
    record = state.artifacts[artifactPath];
    if (!cleaned && current.hash !== desiredHash) {
      return { status: "deferred", hash: current.hash };
    }
  }
  if (current.status === "file" && current.hash === desiredHash) {
    return { status: "current", hash: desiredHash };
  }

  const destDir = path.dirname(artifactPath);
  const tmp = uniqueSidePath(artifactPath, "tmp");
  try {
    ensureSafeDirectory(fs, path, root, destDir);
    fs.writeFileSync(tmp, desired, { flag: "wx", mode: 0o644 });
    syncPath(fs, tmp);
  } catch {
    try { fs.unlinkSync(tmp); } catch { /* ignore cleanup failure */ }
    return { status: "preserved" };
  }
  const temp = inspectArtifact(fs, tmp);
  const tempExtended = temp.status === "file" ? fingerprintMetadata(tmp) : null;
  if (temp.status !== "file" || temp.hash !== desiredHash ||
      temp.dev === undefined || temp.ino === undefined || tempExtended === null) {
    try { fs.unlinkSync(tmp); } catch { /* ignore cleanup failure */ }
    return { status: "preserved" };
  }
  try {
    syncDirectory(fs, destDir);
  } catch {
    try { fs.unlinkSync(tmp); } catch { /* ignore cleanup failure */ }
    return { status: "preserved" };
  }

  if (current.status === "absent") {
    const priorRecord = record;
    const next = pendingRecord(null, desiredHash, temp, tmp, tempExtended);
    const previous = recoveryForAbsent(fs, record, artifactPath, fingerprintMetadata);
    if (record && !previous) {
      try { fs.unlinkSync(tmp); } catch { /* unreferenced random temp */ }
      return { status: "deferred" };
    }
    if (previous) next.pending.previous = previous;
    state.artifacts[artifactPath] = next;
    let pendingDurable = false;
    try {
      writeManifest(fs, path, stateRoot, manifestPath, state);
      pendingDurable = true;
      fs.linkSync(tmp, artifactPath);
      const published = inspectArtifact(fs, artifactPath);
      if (!sameIdentity(temp, published) || published.hash !== desiredHash) {
        return { status: "preserved" };
      }
      syncPath(fs, artifactPath);
      syncDirectory(fs, destDir);
      promotePending(
        fs, path, stateRoot, manifestPath, state, artifactPath,
        state.artifacts[artifactPath], published, fingerprintMetadata
      );
      syncDirectory(fs, destDir);
      return { status: "created", hash: desiredHash };
    } catch (e) {
      if (!pendingDurable && !(e && e.manifestMayReferenceState)) {
        if (priorRecord === undefined) delete state.artifacts[artifactPath];
        else state.artifacts[artifactPath] = priorRecord;
        try {
          fs.unlinkSync(tmp);
          syncDirectory(fs, destDir);
        } catch { /* a partial manifest may still make the temp recoverable */ }
      }
      return { status: "preserved" };
    }
  }

  const displaced = uniqueSidePath(artifactPath, "kirocrew-displaced");
  const beforePending = inspectArtifact(fs, artifactPath);
  const previousExtended = fingerprintMetadata(artifactPath);
  if (!recordOwns(fs, record, beforePending, artifactPath) ||
      !sameIdentity(current, beforePending) || previousExtended === null ||
      previousExtended !== record.extended) {
    try { fs.unlinkSync(tmp); } catch { /* unreferenced random temp */ }
    return { status: "preserved" };
  }
  current = beforePending;
  state.artifacts[artifactPath] = pendingRecord(
    record, desiredHash, temp, tmp, tempExtended, displaced, current, record.extended
  );
  const pendingState = state.artifacts[artifactPath];
  try {
    writeManifest(fs, path, stateRoot, manifestPath, state);
  } catch (e) {
    if (!(e && e.manifestMayReferenceState)) {
      state.artifacts[artifactPath] = record;
      try {
        fs.unlinkSync(tmp);
        syncDirectory(fs, destDir);
      } catch { /* the pending manifest may still make the temp recoverable */ }
    }
    return { status: "preserved" };
  }

  const publication = publishMetadata(
    artifactPath, displaced, tmp, pendingState.pending.previous, pendingState.pending
  );
  if (!publication || publication.status !== "published") {
    return { status: "preserved", recoveryPath: displaced };
  }
  pendingState.pending.previous.metadata = publication.oldMetadata;

  try {
    const published = inspectArtifact(fs, artifactPath);
    if (!sameIdentity(temp, published) || published.hash !== desiredHash) {
      return { status: "preserved", recoveryPath: displaced };
    }
    promotePending(
      fs, path, stateRoot, manifestPath, state, artifactPath,
      pendingState, published, fingerprintMetadata
    );
  } catch {
    // Pending state plus retained temp/displaced inodes makes this recoverable.
    return { status: "preserved", recoveryPath: displaced };
  }

  // The current temp hard link remains the live ownership anchor. Superseded
  // names stay manifest-recorded for crash recovery and are removed on a later
  // launch only if their inode and hash remain exactly as Kiro Crew left them.
  // Their suffixes are not desktop or icon extensions, so indexers ignore them
  // during that bounded retention window.
  try { syncDirectory(fs, destDir); } catch { /* public state is already durable */ }
  return { status: "updated", hash: desiredHash };
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
 *   installKind         — resolveLinuxInstall(...).kind from the Electron shell
 *   env                 — process.env (APPIMAGE/APPDIR/XDG_DATA_HOME/HOME)
 *   version             — app.getVersion(), recorded for diagnostics
 *   stateDir            — app.getPath('userData'), parent of private manifest
 *   uid                 — process.geteuid(), for manifest directory ownership
 *   run(cmd, args)      — spawn a cache-refresh command (given an ABSOLUTE path)
 *   log(message)        — diagnostic sink
 *   spawnSync           — child_process.spawnSync, for the packaged xattr/ACL
 *                          fingerprint, lease-held publication, and cleanup
 *                          helper; work is retained when it is absent, busy,
 *                          or fails
 *   resourcesPath       — process.resourcesPath, locates the packaged
 *                          interpreter under backend-dist/kirocrew-backend/bin
 */
function integrateLinuxDesktop(deps) {
  const {
    fs, path, os, env, platform, installKind, version, stateDir, uid, run, log,
    spawnSync, resourcesPath,
  } = deps;
  const note = (m) => { try { if (log) log(m); } catch { /* ignore */ } };
  const fingerprintMetadata = (target) =>
    fingerprintExtendedMetadata(fs, path, spawnSync, resourcesPath, target);
  const deleteMetadata = (target, recovery) =>
    deleteRecoveryPath(
      fs, path, spawnSync, resourcesPath, target, recovery
    );
  const publishMetadata = (target, displaced, temp, previous, pending) =>
    publishManagedArtifact(
      fs, path, spawnSync, resourcesPath,
      target, displaced, temp, previous, pending
    );
  const recoverMetadata = (target, priorPath, temp, previous, pending) =>
    recoverManagedArtifact(
      fs, path, spawnSync, resourcesPath,
      target, priorPath, temp, previous, pending
    );

  const plan = planIntegration(env, platform, installKind);
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
  const launcherBody = renderDesktopEntry({
    name: entry.Name || "Kiro Crew",
    comment: entry.Comment,
    execLine: rewriteExec(entry.Exec, plan.appImage),
    iconName,
    wmClass: entry.StartupWMClass || iconName,
    categories: entry.Categories,
  });

  const stateRoot = path.join(stateDir, MANIFEST_DIR);
  const manifestPath = path.join(stateRoot, MANIFEST_FILE);
  try {
    prepareStateRoot(fs, path, stateDir, stateRoot, uid);
  } catch (e) {
    note(`desktop integration manifest directory is unsafe: ${e && e.message}`);
    return { skipped: true, reason: "manifest unavailable" };
  }
  const state = loadManifest(fs, manifestPath);
  if (!state) {
    note("desktop integration manifest is unreadable or untrusted; preserving desktop files");
    return { skipped: true, reason: "manifest unavailable" };
  }
  state.version = version;
  state.appImage = plan.appImage;
  state.iconName = iconName;

  // A pre-existing launcher with no recorded hash is user-owned. Inspect it
  // before icons so a customized launcher also protects matching icon paths.
  const launcherBefore = inspectArtifact(fs, launcherPath);
  const launcherRecord = state.artifacts[launcherPath];
  if (launcherBefore.status === "unknown" ||
      (launcherBefore.status === "file" &&
       !recordOwns(fs, launcherRecord, launcherBefore, launcherPath) &&
       !pendingMatches(
         fs, launcherRecord, launcherBefore, launcherPath, fingerprintMetadata
       ) &&
       !restoredPendingMatches(
         fs, launcherRecord, launcherBefore, launcherPath, fingerprintMetadata
       ))) {
    if (launcherBefore.status === "file" && launcherRecord && launcherRecord.pending) {
      abandonPending(
        fs, path, stateRoot, manifestPath, state, launcherPath, launcherRecord,
        fingerprintMetadata, deleteMetadata
      );
    }
    note(`launcher at ${launcherPath} was not written by Kiro Crew; preserving it`);
    return { skipped: true, reason: "launcher customized" };
  }

  // Reconcile every shipped icon. A hash recorded before an earlier write lets
  // a later launch finish an interrupted installation without adopting unknown
  // files. User-modified icon bytes are preserved size by size.
  const srcRoot = path.join(plan.appDir, "usr", "share", "icons", "hicolor");
  let available = 0;
  let changed = false;
  for (const size of ICON_SIZES) {
    const src = path.join(srcRoot, size, "apps", `${iconName}.png`);
    try {
      if (!fs.existsSync(src)) continue;
      const dest = path.join(iconRoot, size, "apps", `${iconName}.png`);
      const result = reconcileArtifact(
        fs, path, share, stateRoot, manifestPath, state, dest, fs.readFileSync(src),
        fingerprintMetadata, deleteMetadata, publishMetadata, recoverMetadata
      );
      if (result.status !== "preserved" || inspectArtifact(fs, dest).status === "file") {
        available += 1;
      }
      if (result.status === "created" || result.status === "updated") changed = true;
    } catch (e) {
      note(`icon ${size} reconciliation failed: ${e && e.message}`);
    }
  }
  if (available === 0) {
    note("no managed icon is available; skipping launcher");
    return { skipped: true, reason: "no managed icons" };
  }

  let launcherResult;
  try {
    launcherResult = reconcileArtifact(
      fs, path, share, stateRoot, manifestPath, state, launcherPath, launcherBody,
      fingerprintMetadata, deleteMetadata, publishMetadata, recoverMetadata
    );
  } catch (e) {
    note(`launcher reconciliation failed: ${e && e.message}`);
    return { skipped: true, reason: "launcher reconciliation failed" };
  }
  if (launcherResult.status === "preserved") {
    note(`launcher at ${launcherPath} changed during reconciliation; preserving it`);
    return { skipped: true, reason: "launcher customized" };
  }
  if (launcherResult.status === "created" || launcherResult.status === "updated") changed = true;

  // Refresh desktop caches only when public files changed. Each binary is
  // resolved to an absolute fixed-system path and run without a shell.
  if (changed && run) {
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

  note(`${changed ? "reconciled" : "verified"} ${iconName} at ${available} size(s)`);
  return { installed: true, iconName, sizes: available };
}

module.exports = {
  parseDesktopEntry,
  escapeExecArg,
  rewriteExec,
  renderDesktopEntry,
  planIntegration,
  dataHome,
  integrateLinuxDesktop,
};
