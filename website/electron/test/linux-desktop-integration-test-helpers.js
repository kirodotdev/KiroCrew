"use strict";
// Shared fixtures for linux-desktop-integration.test.js and its
// linux-desktop-integration.*.test.js parts. (Not a *.test.js file, so
// `node --test test/*.test.js` does not run it.)

const realFs = require("node:fs");
const realOs = require("node:os");
const realPath = require("node:path");
const realChildProcess = require("node:child_process");
const { EventEmitter } = require("node:events");

const LINUX_FILESYSTEM_ONLY = {
  skip: process.platform !== "linux" ? "requires Linux filesystem APIs" : false,
};

const BUNDLED_DESKTOP = [
  "[Desktop Entry]",
  "Name=Kiro Crew",
  "Exec=AppRun --no-sandbox %U",
  "Terminal=false",
  "Type=Application",
  "Icon=kirocrew-desktop",
  "StartupWMClass=kirocrew-desktop",
  "Comment=Kiro Crew — AI agent desktop app",
  "Categories=Development;",
  "",
].join("\n");

// ---- orchestrator against an in-memory fake fs ----

function makeFakeFs(present, opts = {}) {
  const files = new Map(Object.entries(present));
  const dirs = new Set(["/", ...(opts.dirs || [])]);
  const symlinks = new Set(opts.symlinks || []);
  const inodes = new Map();
  const inodeMetadata = new Map();
  const inodeXattrs = new Map();
  const uid = opts.uid === undefined ? 1000 : opts.uid;
  const gid = opts.gid === undefined ? 1000 : opts.gid;
  const umask = opts.umask === undefined ? 0o022 : opts.umask;
  let nextInode = 1;
  let clock = 1000;
  const defaultMode = (p) => dirs.has(p) ? 0o40700 : 0o100644;
  const inodeFor = (p) => {
    if (!inodes.has(p)) {
      const ino = nextInode++;
      inodes.set(p, ino);
      inodeMetadata.set(ino, {
        mode: (opts.modes && opts.modes[p]) || defaultMode(p),
        uid,
        gid,
        ctimeMs: ++clock,
        mtimeNs: BigInt(clock) * 1_000_000n,
      });
    }
    return inodes.get(p);
  };
  const metadataFor = (p) => inodeMetadata.get(inodeFor(p));
  const touchMetadata = (p) => { metadataFor(p).ctimeMs = ++clock; };
  const writeMetadata = (p) => {
    const metadata = metadataFor(p);
    metadata.ctimeMs = ++clock;
    metadata.mtimeNs = BigInt(clock) * 1_000_000n;
  };
  for (const p of [...files.keys(), ...dirs, ...symlinks]) inodeFor(p);
  const enoent = () => Object.assign(new Error("ENOENT"), { code: "ENOENT" });
  return {
    _files: files,
    _dirs: dirs,
    _touchMetadata: touchMetadata,
    _setMtime: (p, mtimeNs) => {
      metadataFor(p).mtimeNs = BigInt(mtimeNs);
      touchMetadata(p);
    },
    _setXattr: (p, name, value) => {
      const ino = inodeFor(p);
      if (!inodeXattrs.has(ino)) inodeXattrs.set(ino, new Map());
      inodeXattrs.get(ino).set(name, value);
      touchMetadata(p);
    },
    _fingerprintFor: (p) => {
      if (!files.has(p) && !dirs.has(p)) return null;
      const ino = inodeFor(p);
      const entries = [...(inodeXattrs.get(ino) || new Map())].sort(([a], [b]) => a < b ? -1 : 1);
      return require("crypto").createHash("sha256")
        .update(JSON.stringify(entries)).digest("hex");
    },
    existsSync: (p) => files.has(p),
    readFileSync: (p) => { if (!files.has(p)) throw enoent(); return files.get(p); },
    readdirSync: (d) =>
      [...files.keys()]
        .filter((p) => p.startsWith(d + "/") && !p.slice(d.length + 1).includes("/"))
        .map((p) => p.slice(d.length + 1)),
    mkdirSync: (d, o) => {
      dirs.add(d);
      const ino = inodeFor(d);
      inodeMetadata.set(ino, {
        mode: 0o40000 | (((o && o.mode) === undefined ? (0o777 & ~umask) : o.mode) & 0o7777),
        uid,
        gid,
        ctimeMs: ++clock,
        mtimeNs: BigInt(clock) * 1_000_000n,
      });
    },
    writeFileSync: (p, data, o) => {
      if (o && o.flag && o.flag.includes("x") && files.has(p)) {
        throw Object.assign(new Error("EEXIST"), { code: "EEXIST" });
      }
      const existed = files.has(p);
      files.set(p, data);
      const ino = inodeFor(p);
      if (!existed) {
        inodeMetadata.set(ino, {
          mode: 0o100000 | ((((o && o.mode) === undefined ? 0o666 : o.mode) & ~umask) & 0o7777),
          uid,
          gid,
          ctimeMs: ++clock,
          mtimeNs: BigInt(clock) * 1_000_000n,
        });
      } else {
        writeMetadata(p);
      }
    },
    chmodSync: (p, mode) => {
      if (!files.has(p) && !dirs.has(p)) throw enoent();
      const metadata = metadataFor(p);
      metadata.mode = (metadata.mode & 0o170000) | (mode & 0o7777);
      touchMetadata(p);
    },
    openSync: (p) => {
      if (!files.has(p) && !dirs.has(p)) throw enoent();
      return p;
    },
    fsyncSync: () => {},
    closeSync: () => {},
    linkSync: (src, dest) => {
      if (!files.has(src)) throw enoent();
      if (files.has(dest) || symlinks.has(dest) || dirs.has(dest)) {
        throw Object.assign(new Error("EEXIST"), { code: "EEXIST" });
      }
      files.set(dest, files.get(src));
      inodes.set(dest, inodeFor(src));
      touchMetadata(src);
    },
    renameSync: (src, dest) => {
      if (!files.has(src)) throw enoent();
      files.set(dest, files.get(src));
      inodes.set(dest, inodeFor(src));
      touchMetadata(dest);
      files.delete(src);
      inodes.delete(src);
    },
    unlinkSync: (p) => {
      const ino = inodes.get(p);
      files.delete(p);
      inodes.delete(p);
      if (ino !== undefined && [...inodes.values()].includes(ino)) {
        inodeMetadata.get(ino).ctimeMs = ++clock;
      }
    },
    lstatSync: (p) => {
      const link = symlinks.has(p);
      if (!link && !files.has(p) && !dirs.has(p)) throw enoent();
      const metadata = metadataFor(p);
      return {
        isSymbolicLink: () => link,
        isFile: () => !link && files.has(p),
        isDirectory: () => !link && dirs.has(p),
        uid: metadata.uid,
        gid: metadata.gid,
        mode: metadata.mode,
        ctimeMs: metadata.ctimeMs,
        mtimeNs: metadata.mtimeNs,
        dev: 1,
        ino: inodeFor(p),
        size: files.has(p) ? Buffer.byteLength(String(files.get(p))) : 0,
      };
    },
    realpathSync: (p) => (opts.realpath && opts.realpath[p]) || p,
  };
}

const fakePath = {
  join: (...parts) => parts.join("/").replace(/\/+/g, "/"),
  dirname: (p) => p.slice(0, p.lastIndexOf("/")) || "/",
};

const APPDIR = "/mnt/appimage";
const HOME = "/home/u";
const STATE_ROOT = `${HOME}/.config/kirocrew-desktop/linux-desktop-integration`;
const STATE_PATH = `${STATE_ROOT}/state.json`;
// System binaries the refresher resolution should find (kbuildsycoca5 absent on purpose).
const SYS_BINS = {
  "/usr/bin/gtk-update-icon-cache": "bin",
  "/usr/bin/update-desktop-database": "bin",
  "/usr/bin/kbuildsycoca6": "bin",
};

function bundleWithIcons(sizes, extra = {}, appDir = APPDIR) {
  const present = {
    [`${appDir}/kirocrew-desktop.desktop`]: BUNDLED_DESKTOP,
    ...SYS_BINS,
    ...extra,
  };
  for (const s of sizes) {
    const icon = `${appDir}/usr/share/icons/hicolor/${s}/apps/kirocrew-desktop.png`;
    present[icon] = `PNG:${s}`;
  }
  return present;
}

function baseDeps(fs, overrides = {}) {
  const resourcesPath = "/opt/KiroCrew/resources";
  const binDir = `${resourcesPath}/backend-dist/kirocrew-backend/bin`;
  if (typeof fs._dirs !== "undefined" && !fs._dirs.has(binDir)) {
    fs.mkdirSync(resourcesPath);
    fs.mkdirSync(`${resourcesPath}/backend-dist`);
    fs.mkdirSync(`${resourcesPath}/backend-dist/kirocrew-backend`);
    fs.mkdirSync(binDir);
    fs.writeFileSync(`${binDir}/python3.12`, "python-stub");
  }
  const spawnSync = (python, args) => {
    const mode = args[3];
    const target = args[4];
    if (mode === "fingerprint") {
      const fingerprint = fs._fingerprintFor ? fs._fingerprintFor(target) : null;
      return fingerprint === null
        ? { status: 1, stdout: "" }
        : { status: 0, stdout: `${fingerprint}\n` };
    }
    if (mode === "publish" || mode === "recover") {
      const previous = JSON.parse(args[5]);
      const displaced = args[6];
      const temp = args[7];
      const pending = JSON.parse(args[8]);
      const priorPath = mode === "recover" ? displaced : target;
      const snapshotMatches = (candidate, expected) => {
        if (!fs._files.has(candidate)) return false;
        const st = fs.lstatSync(candidate);
        const hash = require("crypto").createHash("sha256")
          .update(fs.readFileSync(candidate)).digest("hex");
        return st.dev === expected.dev && st.ino === expected.ino &&
          st.mode === expected.metadata.mode && st.uid === expected.metadata.uid &&
          st.gid === expected.metadata.gid && String(st.mtimeNs) === expected.metadata.mtimeNs &&
          hash === expected.hash &&
          fs._fingerprintFor(candidate) === expected.extended;
      };
      if (!snapshotMatches(priorPath, previous) || !snapshotMatches(temp, pending)) {
        return { status: 0, stdout: "changed\n" };
      }
      if (typeof fs._beforeLeasedPublish === "function") {
        const outcome = fs._beforeLeasedPublish(target, displaced, temp, previous, pending);
        if (outcome) return { status: 0, stdout: `${outcome}\n` };
      }
      if (mode === "publish") fs.renameSync(target, displaced);
      else if (fs._files.has(target)) return { status: 0, stdout: "changed\n" };
      try {
        if (mode === "publish") fs.fsyncSync(fakePath.dirname(target));
        if (!snapshotMatches(mode === "publish" ? displaced : priorPath, previous)) {
          if (mode === "publish") {
            try { fs.linkSync(displaced, target); } catch { /* concurrent winner */ }
          }
          return { status: 0, stdout: "changed\n" };
        }
        fs.linkSync(temp, target);
        fs.fsyncSync(target);
        fs.fsyncSync(fakePath.dirname(target));
        if (!snapshotMatches(target, pending)) {
          return { status: 0, stdout: "changed\n" };
        }
        const old = fs.lstatSync(mode === "publish" ? displaced : priorPath);
        return { status: 0, stdout: JSON.stringify({
          status: "published",
          oldMetadata: {
            mode: old.mode, uid: old.uid, gid: old.gid,
            ctimeMs: old.ctimeMs, mtimeNs: String(old.mtimeNs),
          },
        }) + "\n" };
      } catch {
        if (mode === "publish") {
          try { fs.linkSync(displaced, target); } catch { /* concurrent winner */ }
        }
        return { status: 0, stdout: "error\n" };
      }
    }
    if (mode !== "delete") return { status: 1, stdout: "" };
    if (!fs._files.has(target)) return { status: 0, stdout: "absent\n" };
    const expected = JSON.parse(args[5]);
    const snapshotMatches = () => {
      if (!fs._files.has(target)) return false;
      const st = fs.lstatSync(target);
      const hash = require("crypto").createHash("sha256")
        .update(fs.readFileSync(target)).digest("hex");
      return st.dev === expected.dev && st.ino === expected.ino &&
        st.mode === expected.metadata.mode && st.uid === expected.metadata.uid &&
        st.gid === expected.metadata.gid && String(st.mtimeNs) === expected.metadata.mtimeNs &&
        hash === expected.hash &&
        fs._fingerprintFor(target) === expected.extended;
    };
    if (!snapshotMatches()) return { status: 0, stdout: "changed\n" };
    if (typeof fs._beforeLeasedDelete === "function") {
      const outcome = fs._beforeLeasedDelete(target, expected);
      if (outcome) return { status: 0, stdout: `${outcome}\n` };
    }
    if (!snapshotMatches()) return { status: 0, stdout: "changed\n" };
    fs.unlinkSync(target);
    return { status: 0, stdout: "deleted\n" };
  };
  return {
    fs, path: fakePath, os: { homedir: () => HOME },
    platform: "linux",
    installKind: "appimage",
    env: { APPIMAGE: "/opt/K.AppImage", APPDIR, HOME },
    version: "0.5.0",
    stateDir: `${HOME}/.config/kirocrew-desktop`,
    uid: 1000,
    run: () => {},
    log: () => {},
    spawnSync,
    resourcesPath,
    ...overrides,
  };
}

// Real-filesystem fixture whose packaged "python3.12" is a wrapper around the
// system interpreter that makes every xattr syscall fail with one errno before
// running the real helper script. This drives the actual helper path on a
// filesystem class the test host cannot provide (no extended attributes), and
// records each helper invocation so its structured outcome can be asserted.
function xattrFailingFixture(t, errnoName) {
  const root = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), `kirocrew-ldi-${errnoName}-`));
  t.after(() => realFs.rmSync(root, { recursive: true, force: true }));
  const home = realPath.join(root, "home");
  const appDir = realPath.join(root, "appdir");
  const dataDir = realPath.join(root, "data");
  const stateDir = realPath.join(root, "config", "kirocrew-desktop");
  const resourcesPath = realPath.join(root, "resources");
  const binDir = realPath.join(resourcesPath, "backend-dist", "kirocrew-backend", "bin");
  realFs.mkdirSync(realPath.join(appDir, "usr", "share", "icons", "hicolor", "16x16", "apps"), {
    recursive: true,
  });
  realFs.mkdirSync(home, { recursive: true });
  realFs.mkdirSync(binDir, { recursive: true });
  realFs.writeFileSync(realPath.join(binDir, "python3.12"), [
    "#!/usr/bin/python3",
    "import errno, os, sys",
    `code = errno.${errnoName}`,
    "def failing(*args, **kwargs):",
    "    raise OSError(code, os.strerror(code))",
    "os.listxattr = failing",
    "os.getxattr = failing",
    "# argv: wrapper, -I, -c, SCRIPT, mode, target, ... -> hand the helper",
    "# the argv shape `python -I -c SCRIPT` would have produced.",
    "script = sys.argv[3]",
    "sys.argv = ['-c'] + sys.argv[4:]",
    "exec(compile(script, '<helper>', 'exec'))",
    "",
  ].join("\n"), { mode: 0o755 });
  realFs.writeFileSync(realPath.join(appDir, "kirocrew-desktop.desktop"), BUNDLED_DESKTOP);
  realFs.writeFileSync(
    realPath.join(appDir, "usr", "share", "icons", "hicolor", "16x16", "apps", "kirocrew-desktop.png"),
    "REAL PNG"
  );
  const helperRuns = [];
  const spawnSync = (python, args, options) => {
    const result = realChildProcess.spawnSync(python, args, options);
    helperRuns.push({ mode: args[3], status: result.status,
      stdout: String(result.stdout || "").trim(), stderr: String(result.stderr || "") });
    return result;
  };
  const deps = {
    fs: realFs, path: realPath, os: realOs, platform: "linux", installKind: "appimage",
    env: { APPIMAGE: realPath.join(root, "KiroCrew.AppImage"), APPDIR: appDir, HOME: home,
      XDG_DATA_HOME: dataDir },
    version: "0.5.0", stateDir,
    uid: typeof process.geteuid === "function" ? process.geteuid() : undefined,
    run: () => {}, log: () => {}, spawnSync, resourcesPath,
  };
  return {
    root, deps, helperRuns,
    launcher: realPath.join(dataDir, "applications", "kirocrew-desktop-appimage.desktop"),
    statePath: realPath.join(stateDir, "linux-desktop-integration", "state.json"),
  };
}

const EMPTY_XATTR_DIGEST = require("crypto").createHash("sha256").digest("hex");

// ---- worker-thread orchestration (startLinuxDesktopIntegration) ----

function fakeWorkerFactory(script) {
  const calls = [];
  const createWorker = (file, options) => {
    const worker = new EventEmitter();
    calls.push({ file, options, worker });
    if (script) setImmediate(() => script(worker, options));
    return worker;
  };
  return { calls, createWorker };
}

const WORKER_DEPS = {
  env: { APPIMAGE: "/opt/K.AppImage", APPDIR, HOME },
  platform: "linux",
  installKind: "appimage",
  version: "0.5.0",
  stateDir: `${HOME}/.config/kirocrew-desktop`,
  uid: 1000,
  resourcesPath: "/opt/KiroCrew/resources",
};

module.exports = {
  LINUX_FILESYSTEM_ONLY,
  BUNDLED_DESKTOP,
  makeFakeFs,
  fakePath,
  APPDIR,
  HOME,
  STATE_ROOT,
  STATE_PATH,
  SYS_BINS,
  bundleWithIcons,
  baseDeps,
  xattrFailingFixture,
  EMPTY_XATTR_DIGEST,
  fakeWorkerFactory,
  WORKER_DEPS,
};
