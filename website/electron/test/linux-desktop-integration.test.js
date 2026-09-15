"use strict";
// Linux AppImage desktop integration: pure helpers and an injected fake fs for
// failure boundaries, plus one isolated real-filesystem metadata smoke test.

const { test } = require("node:test");
const assert = require("node:assert");
const realFs = require("node:fs");
const realOs = require("node:os");
const realPath = require("node:path");
const realChildProcess = require("node:child_process");
const {
  parseDesktopEntry,
  escapeExecArg,
  rewriteExec,
  renderDesktopEntry,
  planIntegration,
  dataHome,
  integrateLinuxDesktop,
} = require("../linux-desktop-integration");

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

test("parseDesktopEntry reads the [Desktop Entry] group only", () => {
  const e = parseDesktopEntry(BUNDLED_DESKTOP + "\n[Desktop Action new]\nIcon=other\n");
  assert.equal(e.Name, "Kiro Crew");
  assert.equal(e.Icon, "kirocrew-desktop");
  assert.equal(e.StartupWMClass, "kirocrew-desktop");
  assert.equal(e.Exec, "AppRun --no-sandbox %U");
  assert.equal(e.Categories, "Development;");
});

test("escapeExecArg applies the two-layer Desktop Entry Exec escaping", () => {
  const q = '"';
  const bs = "\\"; // exactly one backslash
  // no reserved chars: quoted verbatim
  assert.equal(escapeExecArg("/opt/Kiro.AppImage"), '"/opt/Kiro.AppImage"');
  assert.equal(escapeExecArg("/home/a b/Kiro.AppImage"), '"/home/a b/Kiro.AppImage"');
  // literal % -> %% (field-code layer)
  assert.equal(escapeExecArg("/opt/100%/Kiro.AppImage"), '"/opt/100%%/Kiro.AppImage"');
  // literal $ -> \\$  (quoting adds one backslash, value-string layer doubles it)
  assert.equal(escapeExecArg("/o/a$b"), q + "/o/a" + bs + bs + "$b" + q);
  // literal " -> \\"
  assert.equal(escapeExecArg('/o/a"b'), q + "/o/a" + bs + bs + '"' + "b" + q);
  // literal backtick -> \\`
  assert.equal(escapeExecArg("/o/a`b"), q + "/o/a" + bs + bs + "`b" + q);
  // literal backslash -> four backslashes (both layers double it)
  assert.equal(escapeExecArg("/o/a" + bs + "b"), q + "/o/a" + bs + bs + bs + bs + "b" + q);
});

test("rewriteExec swaps AppRun and removes its unconditional sandbox bypass", () => {
  assert.equal(
    rewriteExec("AppRun --no-sandbox %U", "/opt/KiroCrew-x86_64.AppImage"),
    '"/opt/KiroCrew-x86_64.AppImage" %U'
  );
  assert.equal(
    rewriteExec("AppRun --custom-flag --no-sandbox %U", "/opt/KiroCrew-x86_64.AppImage"),
    '"/opt/KiroCrew-x86_64.AppImage" --custom-flag %U'
  );
  assert.equal(rewriteExec("AppRun %U", "/home/a b/Kiro.AppImage"), '"/home/a b/Kiro.AppImage" %U');
  assert.equal(rewriteExec("", "/x/K.AppImage"), '"/x/K.AppImage" %U');
  // a path with a field-code char and a quote still serializes validly (two-layer escape)
  const bs = "\\";
  assert.equal(rewriteExec("AppRun %U", '/opt/50%/a"b.AppImage'),
    '"/opt/50%%/a' + bs + bs + '"b.AppImage" %U');
});

test("renderDesktopEntry emits required keys and omits empties", () => {
  const body = renderDesktopEntry({
    name: "Kiro Crew",
    comment: "",
    execLine: '"/x/K.AppImage" %U',
    iconName: "kirocrew-desktop",
    wmClass: "kirocrew-desktop",
    categories: "Development;",
  });
  assert.match(body, /^\[Desktop Entry\]$/m);
  assert.match(body, /^Exec="\/x\/K\.AppImage" %U$/m);
  assert.match(body, /^Icon=kirocrew-desktop$/m);
  assert.match(body, /^StartupWMClass=kirocrew-desktop$/m);
  assert.doesNotMatch(body, /^Comment=/m); // empty comment omitted
});

test("planIntegration requires Linux, AppImage identity, and both runtime paths", () => {
  const imageEnv = { APPIMAGE: "/a", APPDIR: "/b" };
  assert.equal(planIntegration(imageEnv, "darwin", "appimage").integrate, false);
  assert.equal(planIntegration({ APPDIR: "/b" }, "linux", "appimage").integrate, false);
  assert.equal(planIntegration({ APPIMAGE: "/a" }, "linux", "appimage").integrate, false);
  assert.equal(planIntegration(imageEnv, "linux", "package").integrate, false);
  assert.equal(planIntegration(imageEnv, "linux", "unknown").integrate, false);
  assert.equal(planIntegration({ APPIMAGE: "/a\nb", APPDIR: "/b" }, "linux", "appimage").integrate, false);
  assert.equal(planIntegration({ APPIMAGE: "/a\tb", APPDIR: "/b" }, "linux", "appimage").integrate, false);
  assert.equal(planIntegration({ APPIMAGE: "/a\0b", APPDIR: "/b" }, "linux", "appimage").integrate, false);
  assert.equal(planIntegration({ APPIMAGE: "/a", APPDIR: "/b\x7fc" }, "linux", "appimage").integrate, false);
  const ok = planIntegration(imageEnv, "linux", "appimage");
  assert.deepEqual(ok, { integrate: true, appImage: "/a", appDir: "/b" });
});

test("planIntegration honors the KIROCREW_DISABLE_DESKTOP_INTEGRATION opt-out", () => {
  for (const v of ["1", "true", "YES", " on "]) {
    const r = planIntegration(
      { APPIMAGE: "/a", APPDIR: "/b", KIROCREW_DISABLE_DESKTOP_INTEGRATION: v },
      "linux",
      "appimage"
    );
    assert.equal(r.integrate, false, `opt-out value ${JSON.stringify(v)} should skip`);
    assert.match(r.reason, /opt-out/);
  }
  // a non-truthy value does not opt out
  assert.equal(
    planIntegration(
      { APPIMAGE: "/a", APPDIR: "/b", KIROCREW_DISABLE_DESKTOP_INTEGRATION: "0" },
      "linux",
      "appimage"
    ).integrate,
    true
  );
});

test("dataHome normalizes absolute XDG_DATA_HOME and HOME boundaries", () => {
  assert.equal(dataHome({ XDG_DATA_HOME: "/custom/data" }, "/home/u"), "/custom/data");
  assert.equal(dataHome({ XDG_DATA_HOME: "/custom//data/./" }, "/home/u"), "/custom/data");
  assert.equal(dataHome({ XDG_DATA_HOME: "/custom/base/../data/" }, "/home/u"), "/custom/data");
  assert.equal(dataHome({ XDG_DATA_HOME: "relative" }, "/home/u/"), "/home/u/.local/share");
  assert.equal(dataHome({}, "/home//u/./"), "/home/u/.local/share");
});

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
      });
    }
    return inodes.get(p);
  };
  const metadataFor = (p) => inodeMetadata.get(inodeFor(p));
  const touchMetadata = (p) => { metadataFor(p).ctimeMs = ++clock; };
  for (const p of [...files.keys(), ...dirs, ...symlinks]) inodeFor(p);
  const enoent = () => Object.assign(new Error("ENOENT"), { code: "ENOENT" });
  return {
    _files: files,
    _dirs: dirs,
    _touchMetadata: touchMetadata,
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
        });
      } else {
        touchMetadata(p);
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

function bundleWithIcons(sizes, extra = {}) {
  const present = { [`${APPDIR}/kirocrew-desktop.desktop`]: BUNDLED_DESKTOP, ...SYS_BINS, ...extra };
  for (const s of sizes) present[`${APPDIR}/usr/share/icons/hicolor/${s}/apps/kirocrew-desktop.png`] = `PNG:${s}`;
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
          st.gid === expected.metadata.gid && hash === expected.hash &&
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
          oldMetadata: { mode: old.mode, uid: old.uid, gid: old.gid, ctimeMs: old.ctimeMs },
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
        st.gid === expected.metadata.gid && hash === expected.hash &&
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

test("integrateLinuxDesktop creates icons + launcher and resolves refreshers", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16", "128x128", "512x512"]));
  const runs = [];
  const res = integrateLinuxDesktop(baseDeps(fs, { run: (cmd, args) => runs.push([cmd, args]) }));
  assert.deepEqual(res, { installed: true, iconName: "kirocrew-desktop", sizes: 3 });

  // Icons copied under our name, at the shipped sizes only.
  assert.equal(fs._files.get(`${HOME}/.local/share/icons/hicolor/512x512/apps/kirocrew-desktop.png`), "PNG:512x512");
  assert.ok(!fs._files.has(`${HOME}/.local/share/icons/hicolor/256x256/apps/kirocrew-desktop.png`));

  // Launcher written with Exec at the real AppImage and matching WMClass.
  const launcher = fs._files.get(`${HOME}/.local/share/applications/kirocrew-desktop.desktop`);
  assert.match(launcher, /^Exec="\/opt\/K\.AppImage" %U$/m);
  assert.match(launcher, /^StartupWMClass=kirocrew-desktop$/m);

  // The private manifest records the exact launcher and icon bytes we wrote.
  const state = JSON.parse(fs._files.get(STATE_PATH));
  assert.equal(state.schema, 2);
  const launcherRecord = state.artifacts[`${HOME}/.local/share/applications/kirocrew-desktop.desktop`];
  const iconRecord = state.artifacts[`${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`];
  assert.match(launcherRecord.owned[0], /^[0-9a-f]{64}$/);
  assert.match(iconRecord.owned[0], /^[0-9a-f]{64}$/);
  assert.ok(fs._files.has(launcherRecord.anchorPath));
  assert.equal(
    fs.lstatSync(launcherRecord.anchorPath).ino,
    fs.lstatSync(`${HOME}/.local/share/applications/kirocrew-desktop.desktop`).ino
  );

  // Refreshers resolved to ABSOLUTE system-dir paths, kbuildsycoca5 skipped (absent).
  assert.deepEqual(runs.map((r) => r[0]), [
    "/usr/bin/gtk-update-icon-cache",
    "/usr/bin/update-desktop-database",
    "/usr/bin/kbuildsycoca6",
  ]);
});

test("integrateLinuxDesktop makes provenance durable before public publication", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  const events = [];
  const fsyncSync = fs.fsyncSync;
  const linkSync = fs.linkSync;
  fs.fsyncSync = (fd) => { events.push(`fsync:${fd}`); return fsyncSync(fd); };
  fs.linkSync = (src, dest) => { events.push(`link:${dest}`); return linkSync(src, dest); };

  integrateLinuxDesktop(baseDeps(fs));

  const icon = `${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`;
  const stateDirSync = events.indexOf(`fsync:${HOME}/.config/kirocrew-desktop`);
  const xdgParentSync = events.indexOf(`fsync:${HOME}/.local`);
  const iconDir = `${HOME}/.local/share/icons/hicolor/16x16/apps`;
  const iconDirSync = events.indexOf(`fsync:${iconDir}`);
  const manifestSync = events.indexOf(`fsync:${STATE_PATH}`);
  const iconPublish = events.indexOf(`link:${icon}`);
  const iconSync = events.indexOf(`fsync:${icon}`);
  assert.ok(stateDirSync >= 0 && stateDirSync < manifestSync);
  assert.ok(xdgParentSync >= 0 && xdgParentSync < iconPublish);
  assert.ok(iconDirSync >= 0 && iconDirSync < manifestSync);
  assert.ok(manifestSync >= 0 && manifestSync < iconPublish);
  assert.ok(iconPublish >= 0 && iconPublish < iconSync);
});

test("integrateLinuxDesktop verifies current managed files without refreshing caches", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const runs = [];

  const res = integrateLinuxDesktop(baseDeps(fs, { run: (cmd) => runs.push(cmd) }));

  assert.deepEqual(res, { installed: true, iconName: "kirocrew-desktop", sizes: 1 });
  assert.deepEqual(runs, []);
});

test("integrateLinuxDesktop updates its managed launcher after the AppImage moves", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.equal(res.installed, true);
  assert.match(fs._files.get(launcher), /^Exec="\/new\/K\.AppImage"/m);
  const state = JSON.parse(fs._files.get(STATE_PATH));
  assert.equal(state.appImage, "/new/K.AppImage");
  assert.equal(state.version, "0.6.0");
});

test("integrateLinuxDesktop preserves a user-modified managed launcher", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const icon = `${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`;
  const customized = "[Desktop Entry]\nType=Application\nName=My Kiro\nExec=/custom/wrapper %U\nIcon=kirocrew-desktop\n";
  fs._files.set(launcher, customized);
  fs._files.set(`${APPDIR}/usr/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`, "NEW ICON");

  const res = integrateLinuxDesktop(baseDeps(fs, { version: "0.6.0" }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), customized);
  assert.equal(fs._files.get(icon), "PNG:16x16"); // icon reconciliation also stops
});

test("integrateLinuxDesktop preserves byte-identical replacement of a managed launcher", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const identical = fs._files.get(launcher);
  fs.unlinkSync(launcher);
  fs._files.set(launcher, identical); // editor-style atomic replacement: same bytes, new inode

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), identical);
  assert.doesNotMatch(identical, /\/new\/K\.AppImage/);
});

test("integrateLinuxDesktop updates managed icons and preserves customized sizes", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16", "128x128"]));
  integrateLinuxDesktop(baseDeps(fs));
  const icon16 = `${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`;
  const icon128 = `${HOME}/.local/share/icons/hicolor/128x128/apps/kirocrew-desktop.png`;
  fs._files.set(icon16, "CUSTOM ICON");
  fs._files.set(`${APPDIR}/usr/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`, "NEW:16");
  fs._files.set(`${APPDIR}/usr/share/icons/hicolor/128x128/apps/kirocrew-desktop.png`, "NEW:128");

  const res = integrateLinuxDesktop(baseDeps(fs, { version: "0.6.0" }));

  assert.deepEqual(res, { installed: true, iconName: "kirocrew-desktop", sizes: 2 });
  assert.equal(fs._files.get(icon16), "CUSTOM ICON");
  assert.equal(fs._files.get(icon128), "NEW:128");
});

test("customized icon sizes remain available while a moved launcher self-heals", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16", "128x128"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const icon16 = `${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`;
  const icon128 = `${HOME}/.local/share/icons/hicolor/128x128/apps/kirocrew-desktop.png`;
  fs._files.set(icon16, "CUSTOM:16");
  fs._files.set(icon128, "CUSTOM:128");

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.deepEqual(res, { installed: true, iconName: "kirocrew-desktop", sizes: 2 });
  assert.equal(fs._files.get(icon16), "CUSTOM:16");
  assert.equal(fs._files.get(icon128), "CUSTOM:128");
  assert.match(fs._files.get(launcher), /\/new\/K\.AppImage/);
});

test("integrateLinuxDesktop completes a manifest-recorded partial installation", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const linkSync = fs.linkSync;
  let failLauncher = true;
  fs.linkSync = (src, dest) => {
    if (dest === launcher && failLauncher) {
      failLauncher = false;
      throw Object.assign(new Error("ENOSPC"), { code: "ENOSPC" });
    }
    return linkSync(src, dest);
  };

  assert.deepEqual(
    integrateLinuxDesktop(baseDeps(fs)),
    { skipped: true, reason: "launcher customized" }
  );
  assert.ok(!fs._files.has(launcher));
  assert.ok(fs._files.has(STATE_PATH));

  const retry = integrateLinuxDesktop(baseDeps(fs));
  assert.equal(retry.installed, true);
  assert.match(fs._files.get(launcher), /^Exec="\/opt\/K\.AppImage"/m);
});

test("integrateLinuxDesktop preserves managed files when the manifest becomes invalid", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const original = fs._files.get(launcher);
  fs._files.set(STATE_PATH, "not json");

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "manifest unavailable" });
  assert.equal(fs._files.get(launcher), original);
});

test("integrateLinuxDesktop restores content changed at the displacement boundary", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const customized = "[Desktop Entry]\nType=Application\nName=Concurrent Edit\nExec=/custom/wrapper %U\n";
  const renameSync = fs.renameSync;
  fs.renameSync = (src, dest) => {
    if (src === launcher && dest.includes(".kirocrew-displaced-")) {
      fs._files.set(src, customized);
    }
    return renameSync(src, dest);
  };

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), customized);
});

test("integrateLinuxDesktop preserves a concurrent writer that wins publication", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const winner = "[Desktop Entry]\nType=Application\nName=Concurrent Winner\nExec=/winner %U\n";
  const linkSync = fs.linkSync;
  fs.linkSync = (src, dest) => {
    if (dest === launcher && src.includes(".tmp-")) fs._files.set(dest, winner);
    return linkSync(src, dest);
  };

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), winner);
});

test("integrateLinuxDesktop restores the old launcher when replacement linking fails", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const original = fs._files.get(launcher);
  const linkSync = fs.linkSync;
  let failReplacement = true;
  fs.linkSync = (src, dest) => {
    if (failReplacement && dest === launcher && src.includes(".tmp-")) {
      failReplacement = false;
      throw Object.assign(new Error("EIO"), { code: "EIO" });
    }
    return linkSync(src, dest);
  };

  const first = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  assert.deepEqual(first, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);

  const retry = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  assert.equal(retry.installed, true);
  assert.match(fs._files.get(launcher), /\/new\/K\.AppImage/);
});

test("integrateLinuxDesktop restores a managed launcher after displacement fsync fails", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const appsDir = `${HOME}/.local/share/applications`;
  const original = fs._files.get(launcher);
  const fsyncSync = fs.fsyncSync;
  let failed = false;
  fs.fsyncSync = (fd) => {
    const displacedExists = [...fs._files.keys()].some((p) => p.startsWith(`${launcher}.kirocrew-displaced-`));
    if (!failed && fd === appsDir && displacedExists) {
      failed = true;
      throw Object.assign(new Error("EIO"), { code: "EIO" });
    }
    return fsyncSync(fd);
  };

  const first = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.deepEqual(first, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);

  const retry = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  assert.equal(retry.installed, true);
  assert.match(fs._files.get(launcher), /^Exec="\/new\/K\.AppImage"/m);
});

test("byte-identical external publication never gains ownership from pending hash alone", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const linkSync = fs.linkSync;
  fs.linkSync = (src, dest) => {
    if (dest === launcher) fs._files.set(dest, fs._files.get(src)); // different inode, same bytes
    return linkSync(src, dest);
  };

  const first = integrateLinuxDesktop(baseDeps(fs));
  assert.deepEqual(first, { skipped: true, reason: "launcher customized" });
  const original = fs._files.get(launcher);

  const second = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  assert.deepEqual(second, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);
  assert.doesNotMatch(original, /\/new\/K\.AppImage/);
});

test("pending public inode is promoted after post-link durability failure", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const fsyncSync = fs.fsyncSync;
  let failPublicSync = true;
  fs.fsyncSync = (fd) => {
    if (failPublicSync && fd === launcher && /\/new\/K\.AppImage/.test(String(fs._files.get(launcher)))) {
      failPublicSync = false;
      throw Object.assign(new Error("EIO"), { code: "EIO" });
    }
    return fsyncSync(fd);
  };

  const first = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  assert.deepEqual(first, { skipped: true, reason: "launcher customized" });
  assert.match(fs._files.get(launcher), /\/new\/K\.AppImage/);

  const retry = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  assert.equal(retry.installed, true);
  assert.match(fs._files.get(launcher), /\/new\/K\.AppImage/);
  assert.ok([...fs._files.keys()].some((p) => p.startsWith(`${launcher}.kirocrew-displaced-`)));
});

test("integrateLinuxDesktop is a no-op off Linux, outside AppImage, and for package installs", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  assert.equal(integrateLinuxDesktop(baseDeps(fs, { platform: "darwin" })).skipped, true);
  assert.equal(integrateLinuxDesktop(baseDeps(fs, { env: { HOME } })).skipped, true);

  const packageFs = makeFakeFs(bundleWithIcons(["16x16"]));
  const packageResult = integrateLinuxDesktop(baseDeps(packageFs, { installKind: "package" }));
  assert.deepEqual(packageResult, { skipped: true, reason: "not an AppImage install" });
  assert.ok(!packageFs._files.has(`${HOME}/.local/share/applications/kirocrew-desktop.desktop`));
});

test("integrateLinuxDesktop leaves an existing desktop integration untouched", () => {
  const present = bundleWithIcons(["16x16"]);
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const icon = `${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`;
  const existingBody = "[Desktop Entry]\nName=My Custom\nExec=/opt/K.AppImage --my-flag %U\nIcon=kirocrew-desktop\n";
  present[launcher] = existingBody;
  present[icon] = "CUSTOM ICON";
  const fs = makeFakeFs(present);

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), existingBody);
  assert.equal(fs._files.get(icon), "CUSTOM ICON");
});

test("integrateLinuxDesktop leaves an uninspectable launcher path untouched", () => {
  const present = bundleWithIcons(["16x16"]);
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const icon = `${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`;
  const existingBody = "[Desktop Entry]\nName=Uninspectable Launcher\nExec=/opt/K.AppImage %U\n";
  present[launcher] = existingBody;
  present[icon] = "CUSTOM ICON";
  const fs = makeFakeFs(present);
  const lstatSync = fs.lstatSync;
  fs.lstatSync = (p) => {
    if (p === launcher) throw Object.assign(new Error("EACCES"), { code: "EACCES" });
    return lstatSync(p);
  };

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), existingBody);
  assert.equal(fs._files.get(icon), "CUSTOM ICON");
});

test("integrateLinuxDesktop uses but does not replace an unowned existing icon", () => {
  const icon = `${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`;
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const present = bundleWithIcons(["16x16"], { [icon]: "CUSTOM ICON" });
  const fs = makeFakeFs(present);

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { installed: true, iconName: "kirocrew-desktop", sizes: 1 });
  assert.equal(fs._files.get(icon), "CUSTOM ICON");
  assert.ok(fs._files.has(launcher));
});

test("integrateLinuxDesktop loses an icon-creation race without overwriting the winner", () => {
  const icon = `${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`;
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  const linkSync = fs.linkSync;
  fs.linkSync = (src, dest) => {
    if (dest === icon) fs._files.set(dest, "RACE WINNER");
    return linkSync(src, dest);
  };

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { installed: true, iconName: "kirocrew-desktop", sizes: 1 });
  assert.equal(fs._files.get(icon), "RACE WINNER");
  assert.ok(fs._files.has(`${HOME}/.local/share/applications/kirocrew-desktop.desktop`));
});

test("integrateLinuxDesktop loses a launcher-creation race without overwriting the winner", () => {
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const winner = "[Desktop Entry]\nName=Race Winner\nExec=/other/app %U\n";
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  const linkSync = fs.linkSync;
  fs.linkSync = (src, dest) => {
    if (dest === launcher) fs._files.set(dest, winner);
    return linkSync(src, dest);
  };

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), winner);
});

test("a symlinked launcher is treated as existing and leaves integration untouched", () => {
  const launcherPath = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const iconPath = `${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`;
  const present = bundleWithIcons(["16x16"], { [iconPath]: "CUSTOM ICON" });
  const fs = makeFakeFs(present, { symlinks: [launcherPath] });

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.ok(!fs._files.has(launcherPath));
  assert.equal(fs._files.get(iconPath), "CUSTOM ICON");
});

test("desktop integration refuses a symlinked icon destination (no write-through)", () => {
  const iconDest = `${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`;
  const fs = makeFakeFs(bundleWithIcons(["16x16", "128x128"]), { symlinks: [iconDest] });
  const res = integrateLinuxDesktop(baseDeps(fs));
  assert.equal(res.installed, true);
  assert.ok(!fs._files.has(iconDest)); // symlinked icon dest refused, not written through
  assert.equal(fs._files.get(`${HOME}/.local/share/icons/hicolor/128x128/apps/kirocrew-desktop.png`), "PNG:128x128");
});

test("desktop integration refuses a symlinked icon ancestor before creating children", () => {
  const iconRoot = `${HOME}/.local/share/icons`;
  const child = `${iconRoot}/hicolor/16x16/apps`;
  const fs = makeFakeFs(bundleWithIcons(["16x16"]), { symlinks: [iconRoot] });

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { skipped: true, reason: "no managed icons" });
  assert.ok(!fs._dirs.has(child));
  assert.ok(!fs._files.has(`${child}/kirocrew-desktop.png`));
});

test("desktop integration checks symlink ancestors above a pre-existing child", () => {
  const iconRoot = `${HOME}/.local/share/icons`;
  const child = `${iconRoot}/hicolor/16x16/apps`;
  const fs = makeFakeFs(bundleWithIcons(["16x16"]), {
    symlinks: [iconRoot],
    dirs: [child],
  });

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { skipped: true, reason: "no managed icons" });
  assert.ok(!fs._files.has(`${child}/kirocrew-desktop.png`));
});

test("desktop integration refuses a symlinked applications directory", () => {
  const appsDir = `${HOME}/.local/share/applications`;
  const fs = makeFakeFs(bundleWithIcons(["16x16"]), { symlinks: [appsDir] });

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.equal(res.skipped, true);
  assert.equal(res.reason, "launcher customized");
  assert.ok(!fs._files.has(`${appsDir}/kirocrew-desktop.desktop`));
  assert.ok(fs._files.has(`${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`));
});

test("integrateLinuxDesktop refuses an untrusted manifest directory", () => {
  for (const [label, fs] of [
    ["symlink", makeFakeFs(bundleWithIcons(["16x16"]), { symlinks: [STATE_ROOT] })],
    ["other owner", makeFakeFs(bundleWithIcons(["16x16"]), { dirs: [STATE_ROOT], uid: 2000 })],
  ]) {
    const res = integrateLinuxDesktop(baseDeps(fs));
    assert.deepEqual(res, { skipped: true, reason: "manifest unavailable" }, label);
    assert.ok(!fs._files.has(`${HOME}/.local/share/applications/kirocrew-desktop.desktop`), label);
  }
});

test("integrateLinuxDesktop skips cleanly when the bundle ships no icons", () => {
  const fs = makeFakeFs({ [`${APPDIR}/kirocrew-desktop.desktop`]: BUNDLED_DESKTOP, ...SYS_BINS });
  const res = integrateLinuxDesktop(baseDeps(fs));
  assert.equal(res.skipped, true);
  assert.equal(res.reason, "no managed icons");
});


test("private manifest root is 0700 under a 002 umask", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]), { umask: 0o002 });

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.equal(res.installed, true);
  assert.equal(fs.lstatSync(STATE_ROOT).mode & 0o777, 0o700);
  assert.equal(fs.lstatSync(`${HOME}/.local/share`).mode & 0o777, 0o775);
});

test("pre-existing group-writable manifest root still fails closed", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]), {
    dirs: [STATE_ROOT],
    modes: { [STATE_ROOT]: 0o40775 },
  });

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { skipped: true, reason: "manifest unavailable" });
  assert.ok(!fs._files.has(`${HOME}/.local/share/applications/kirocrew-desktop.desktop`));
});

test("metadata-only chmod makes a managed launcher user-owned", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const original = fs._files.get(launcher);
  fs.chmodSync(launcher, 0o600);

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);
  assert.equal(fs.lstatSync(launcher).mode & 0o777, 0o600);
});

test("metadata ctime drift makes a managed launcher user-owned", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const original = fs._files.get(launcher);
  fs._touchMetadata(launcher); // models an ACL/xattr update with unchanged bytes and mode

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);
});

test("verified obsolete anchor and recovery names are removed on the next launch", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;

  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  const afterUpdate = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.equal(afterUpdate.recovery.paths.length, 2);
  for (const p of afterUpdate.recovery.paths) assert.ok(fs._files.has(p));

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.equal(res.installed, true);
  const stable = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.equal(stable.recovery, undefined);
  assert.ok(fs._files.has(stable.anchorPath));
  for (const p of afterUpdate.recovery.paths) assert.ok(!fs._files.has(p));
});

test("changed recovery names are preserved and cleanup authority is revoked", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;

  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  const afterUpdate = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  for (const p of afterUpdate.recovery.paths) fs._files.set(p, "USER RECOVERY DATA");

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.equal(res.installed, true);
  const stable = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.equal(stable.recovery, undefined);
  for (const p of afterUpdate.recovery.paths) {
    assert.equal(fs._files.get(p), "USER RECOVERY DATA");
  }
});


test("metadata-only chmod makes a managed icon user-owned", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const icon = `${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`;
  fs.chmodSync(icon, 0o600);
  fs._files.set(`${APPDIR}/usr/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`, "NEW ICON");

  const res = integrateLinuxDesktop(baseDeps(fs, { version: "0.6.0" }));

  assert.equal(res.installed, true);
  assert.equal(fs._files.get(icon), "PNG:16x16");
  assert.equal(fs.lstatSync(icon).mode & 0o777, 0o600);
});


test("absent canonical path recovers the exact pending inode after an interrupted update", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const linkSync = fs.linkSync;
  let blockLauncherLinks = true;
  fs.linkSync = (src, dest) => {
    if (blockLauncherLinks && dest === launcher) {
      throw Object.assign(new Error("simulated power loss boundary"), { code: "EIO" });
    }
    return linkSync(src, dest);
  };

  const interrupted = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  assert.deepEqual(interrupted, { skipped: true, reason: "launcher customized" });
  assert.ok(!fs._files.has(launcher));

  blockLauncherLinks = false;
  const recovered = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  assert.equal(recovered.installed, true);
  assert.match(fs._files.get(launcher), /\/new\/K\.AppImage/);

  const afterRecovery = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.ok(afterRecovery.recovery);
  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  const stable = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.equal(stable.recovery, undefined);
  for (const p of afterRecovery.recovery.paths) assert.ok(!fs._files.has(p));
});

test("recreating a deleted managed launcher retires its old anchor", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const oldAnchor = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher].anchorPath;
  fs.unlinkSync(launcher);

  const recreated = integrateLinuxDesktop(baseDeps(fs));
  assert.equal(recreated.installed, true);
  const pendingCleanup = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.deepEqual(pendingCleanup.recovery.paths, [oldAnchor]);

  integrateLinuxDesktop(baseDeps(fs));
  const stable = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.equal(stable.recovery, undefined);
  assert.ok(!fs._files.has(oldAnchor));
  assert.ok(fs._files.has(stable.anchorPath));
});


test("real filesystem keeps managed metadata stable and detects chmod", (t) => {
  const root = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), "kirocrew-ldi-"));
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
  realFs.symlinkSync("/usr/bin/python3", realPath.join(binDir, "python3.12"));
  realFs.writeFileSync(realPath.join(appDir, "kirocrew-desktop.desktop"), BUNDLED_DESKTOP);
  realFs.writeFileSync(
    realPath.join(appDir, "usr", "share", "icons", "hicolor", "16x16", "apps", "kirocrew-desktop.png"),
    "REAL PNG"
  );
  const deps = {
    fs: realFs,
    path: realPath,
    os: realOs,
    platform: "linux",
    installKind: "appimage",
    env: { APPIMAGE: realPath.join(root, "KiroCrew.AppImage"), APPDIR: appDir, HOME: home,
      XDG_DATA_HOME: dataDir },
    version: "0.5.0",
    stateDir,
    uid: typeof process.geteuid === "function" ? process.geteuid() : undefined,
    run: () => {},
    log: () => {},
    spawnSync: realChildProcess.spawnSync,
    resourcesPath,
  };

  assert.equal(integrateLinuxDesktop(deps).installed, true);
  assert.equal(integrateLinuxDesktop(deps).installed, true);
  const launcher = realPath.join(dataDir, "applications", "kirocrew-desktop.desktop");
  const statePath = realPath.join(stateDir, "linux-desktop-integration", "state.json");
  const record = JSON.parse(realFs.readFileSync(statePath, "utf8")).artifacts[launcher];
  assert.equal(realFs.statSync(launcher).ino, realFs.statSync(record.anchorPath).ino);

  realFs.chmodSync(launcher, 0o600);
  const moved = integrateLinuxDesktop({
    ...deps,
    env: { ...deps.env, APPIMAGE: realPath.join(root, "Moved.AppImage") },
    version: "0.6.0",
  });
  assert.deepEqual(moved, { skipped: true, reason: "launcher customized" });
  assert.doesNotMatch(realFs.readFileSync(launcher, "utf8"), /Moved\.AppImage/);
});

test("real filesystem detects a concurrent user xattr edit via the packaged helper", (t) => {
  const root = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), "kirocrew-ldi-xattr-"));
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
  realFs.symlinkSync("/usr/bin/python3", realPath.join(binDir, "python3.12"));
  realFs.writeFileSync(realPath.join(appDir, "kirocrew-desktop.desktop"), BUNDLED_DESKTOP);
  realFs.writeFileSync(
    realPath.join(appDir, "usr", "share", "icons", "hicolor", "16x16", "apps", "kirocrew-desktop.png"),
    "REAL PNG"
  );
  const deps = {
    fs: realFs,
    path: realPath,
    os: realOs,
    platform: "linux",
    installKind: "appimage",
    env: { APPIMAGE: realPath.join(root, "KiroCrew.AppImage"), APPDIR: appDir, HOME: home,
      XDG_DATA_HOME: dataDir },
    version: "0.5.0",
    stateDir,
    uid: typeof process.geteuid === "function" ? process.geteuid() : undefined,
    run: () => {},
    log: () => {},
    spawnSync: realChildProcess.spawnSync,
    resourcesPath,
  };

  assert.equal(integrateLinuxDesktop(deps).installed, true);
  const launcher = realPath.join(dataDir, "applications", "kirocrew-desktop.desktop");
  const originalBody = realFs.readFileSync(launcher, "utf8");
  let xattrSupported;
  {
    const py = realPath.join(binDir, "python3.12");
    const result = realChildProcess.spawnSync(py, [
      "-c", "import os,sys; os.setxattr(sys.argv[1], 'user.kirocrew_test', b'marker')", launcher,
    ]);
    xattrSupported = result.status === 0;
  }
  t.skip = !xattrSupported;
  if (!xattrSupported) return;

  const res = integrateLinuxDesktop({
    ...deps,
    env: { ...deps.env, APPIMAGE: realPath.join(root, "Moved.AppImage") },
    version: "0.6.0",
  });

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(realFs.readFileSync(launcher, "utf8"), originalBody);
});


test("restored replacement failure leaves only the current anchor after cleanup", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const linkSync = fs.linkSync;
  let fail = true;
  fs.linkSync = (src, dest) => {
    if (fail && dest === launcher && src.includes(".tmp-")) {
      fail = false;
      throw Object.assign(new Error("EIO"), { code: "EIO" });
    }
    return linkSync(src, dest);
  };

  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));
  assert.equal(integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  })).installed, true);
  assert.equal(integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  })).installed, true);

  const record = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  const side = [...fs._files.keys()].filter((p) => p.startsWith(`${launcher}.`));
  assert.deepEqual(side, [record.anchorPath]);
});

test("a concurrent canonical winner revokes pending state and exact side files", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const winner = "[Desktop Entry]\nType=Application\nName=Winner\nExec=/winner %U\n";
  const linkSync = fs.linkSync;
  fs.linkSync = (src, dest) => {
    if (dest === launcher && src.includes(".tmp-")) fs._files.set(dest, winner);
    return linkSync(src, dest);
  };

  assert.deepEqual(integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  })), { skipped: true, reason: "launcher customized" });
  assert.deepEqual(integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  })), { skipped: true, reason: "launcher customized" });

  const state = JSON.parse(fs._files.get(STATE_PATH));
  assert.equal(state.artifacts[launcher], undefined);
  assert.equal(fs._files.get(launcher), winner);
  assert.deepEqual(
    [...fs._files.keys()].filter((p) => p.startsWith(`${launcher}.`)),
    []
  );
});


test("manifest recovery paths cannot escape through a generated-name prefix", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const target = `${HOME}/Documents/report.md`;
  fs._files.set(target, "KEEP ME");
  const state = JSON.parse(fs._files.get(STATE_PATH));
  const targetStat = fs.lstatSync(target);
  state.artifacts[launcher].recovery = {
    hash: state.artifacts[launcher].owned[0],
    dev: targetStat.dev,
    ino: targetStat.ino,
    metadata: state.artifacts[launcher].metadata,
    paths: [`${launcher}.tmp-1-2-3/../../../../Documents/report.md`],
  };
  fs._files.set(STATE_PATH, JSON.stringify(state));

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { skipped: true, reason: "manifest unavailable" });
  assert.equal(fs._files.get(target), "KEEP ME");
});


test("failed manifest publication removes unreferenced public temps", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  const renameSync = fs.renameSync;
  fs.renameSync = (src, dest) => {
    if (dest === STATE_PATH) throw Object.assign(new Error("EROFS"), { code: "EROFS" });
    return renameSync(src, dest);
  };

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { skipped: true, reason: "no managed icons" });
  const publicTemps = [...fs._files.keys()].filter(
    (p) => p.startsWith(`${HOME}/.local/share/`) && p.includes(".tmp-")
  );
  assert.deepEqual(publicTemps, []);
});

test("metadata change after pending durability revokes update ownership", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const original = fs._files.get(launcher);
  const renameSync = fs.renameSync;
  let changed = false;
  fs.renameSync = (src, dest) => {
    const result = renameSync(src, dest);
    if (!changed && dest === STATE_PATH && /"pending"/.test(String(fs._files.get(dest))) &&
        ![...fs._files.keys()].some((p) => p.startsWith(`${launcher}.kirocrew-displaced-`))) {
      changed = true;
      fs.chmodSync(launcher, 0o600);
    }
    return result;
  };

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);
  assert.equal(fs.lstatSync(launcher).mode & 0o777, 0o600);
});

test("metadata change during displacement restores and preserves the old inode", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const original = fs._files.get(launcher);
  const renameSync = fs.renameSync;
  fs.renameSync = (src, dest) => {
    const result = renameSync(src, dest);
    if (src === launcher && dest.includes(".kirocrew-displaced-")) fs.chmodSync(dest, 0o600);
    return result;
  };

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);
  assert.equal(fs.lstatSync(launcher).mode & 0o777, 0o600);
});

test("xattr change after lease-held displacement cancels publication", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const original = fs._files.get(launcher);
  const renameSync = fs.renameSync;
  let changed = false;
  fs.renameSync = (src, dest) => {
    const result = renameSync(src, dest);
    if (!changed && src === launcher && dest.includes(".kirocrew-displaced-")) {
      changed = true;
      fs._setXattr(dest, "user.after_displacement", "changed");
    }
    return result;
  };

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);
});


test("xattr edit immediately before displacement is not masked by rename ctime", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const original = fs._files.get(launcher);
  const originalFingerprint = fs._fingerprintFor(launcher);
  const renameSync = fs.renameSync;
  let changed = false;
  fs.renameSync = (src, dest) => {
    if (!changed && src === launcher && dest.includes(".kirocrew-displaced-")) {
      changed = true;
      fs._setXattr(src, "user.test", "changed");
    }
    return renameSync(src, dest); // rename changes ctime again and masks the stat delta
  };

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);
  assert.notEqual(fs._fingerprintFor(launcher), originalFingerprint);
});

test("replacement between recovery inspection and leased unlink survives", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));
  const recovery = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher].recovery;
  const replacedPath = recovery.paths[0];
  let replaced = false;
  fs._beforeLeasedDelete = (target) => {
    if (!replaced && target === replacedPath) {
      replaced = true;
      fs.unlinkSync(target);
      fs.writeFileSync(target, "EXTERNAL REPLACEMENT", { flag: "wx", mode: 0o600 });
    }
    return null;
  };

  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));

  const stable = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.equal(stable.recovery, undefined);
  assert.equal(fs._files.get(replacedPath), "EXTERNAL REPLACEMENT");
  assert.ok(![...fs._files.keys()].some((p) => p.includes(".cleanup-")));
});

test("unavailable metadata helper retains recovery provenance", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));
  const before = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher].recovery;
  assert.match(before.extended, /^[0-9a-f]{64}$/);

  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
    spawnSync: () => ({ status: 1, stdout: "" }),
  }));

  const retained = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher].recovery;
  assert.deepEqual(retained, before);
  for (const p of before.paths) assert.ok(fs._files.has(p));
});

test("real publication and cleanup defer while their inode has an open descriptor", (t) => {
  const root = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), "kirocrew-ldi-lease-"));
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
  realFs.symlinkSync("/usr/bin/python3", realPath.join(binDir, "python3.12"));
  realFs.writeFileSync(realPath.join(appDir, "kirocrew-desktop.desktop"), BUNDLED_DESKTOP);
  realFs.writeFileSync(
    realPath.join(appDir, "usr", "share", "icons", "hicolor", "16x16", "apps", "kirocrew-desktop.png"),
    "REAL PNG"
  );
  const deps = {
    fs: realFs, path: realPath, os: realOs, platform: "linux", installKind: "appimage",
    env: { APPIMAGE: realPath.join(root, "KiroCrew.AppImage"), APPDIR: appDir, HOME: home,
      XDG_DATA_HOME: dataDir },
    version: "0.5.0", stateDir,
    uid: typeof process.geteuid === "function" ? process.geteuid() : undefined,
    run: () => {}, log: () => {}, spawnSync: realChildProcess.spawnSync, resourcesPath,
  };
  assert.equal(integrateLinuxDesktop(deps).installed, true);
  const launcher = realPath.join(dataDir, "applications", "kirocrew-desktop.desktop");
  const originalBody = realFs.readFileSync(launcher, "utf8");
  const currentFd = realFs.openSync(launcher, "r+");
  try {
    const deferred = integrateLinuxDesktop({
      ...deps,
      env: { ...deps.env, APPIMAGE: realPath.join(root, "Moved.AppImage") },
      version: "0.6.0",
    });
    assert.deepEqual(deferred, { skipped: true, reason: "launcher customized" });
    assert.equal(realFs.readFileSync(launcher, "utf8"), originalBody);
  } finally {
    realFs.closeSync(currentFd);
  }
  assert.equal(integrateLinuxDesktop({
    ...deps,
    env: { ...deps.env, APPIMAGE: realPath.join(root, "Moved.AppImage") },
    version: "0.6.0",
  }).installed, true);
  assert.match(realFs.readFileSync(launcher, "utf8"), /Moved\.AppImage/);
  const statePath = realPath.join(stateDir, "linux-desktop-integration", "state.json");
  const recovery = JSON.parse(realFs.readFileSync(statePath, "utf8")).artifacts[launcher].recovery;
  const fd = realFs.openSync(recovery.paths[0], "r+");
  try {
    integrateLinuxDesktop({
      ...deps,
      env: { ...deps.env, APPIMAGE: realPath.join(root, "Moved.AppImage") },
      version: "0.6.0",
    });
    const retained = JSON.parse(realFs.readFileSync(statePath, "utf8")).artifacts[launcher].recovery;
    assert.deepEqual(retained, recovery);
    for (const p of recovery.paths) assert.ok(realFs.existsSync(p));
  } finally {
    realFs.closeSync(fd);
  }

  integrateLinuxDesktop({
    ...deps,
    env: { ...deps.env, APPIMAGE: realPath.join(root, "Moved.AppImage") },
    version: "0.6.0",
  });
  const cleaned = JSON.parse(realFs.readFileSync(statePath, "utf8")).artifacts[launcher];
  assert.equal(cleaned.recovery, undefined);
  for (const p of recovery.paths) assert.ok(!realFs.existsSync(p));
});


test("deleted canonical does not adopt an xattr-edited anchor", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const record = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  fs.unlinkSync(launcher);
  fs._setXattr(record.anchorPath, "user.after_delete", "changed");

  integrateLinuxDesktop(baseDeps(fs));

  assert.ok(!fs._files.has(launcher));
  assert.ok(fs._files.has(record.anchorPath));
  const retained = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.equal(retained.anchorPath, record.anchorPath);
  assert.equal(retained.extended, record.extended);
});


test("xattr edit during publication is never promoted as owned", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const linkSync = fs.linkSync;
  let changed = false;
  fs.linkSync = (src, dest) => {
    const result = linkSync(src, dest);
    if (!changed && dest === launcher && src.includes(".tmp-")) {
      changed = true;
      fs._setXattr(dest, "user.during_publish", "changed");
    }
    return result;
  };

  const first = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));
  const customized = fs._files.get(launcher);
  const second = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/newer/K.AppImage", APPDIR, HOME }, version: "0.7.0",
  }));

  assert.deepEqual(first, { skipped: true, reason: "launcher customized" });
  assert.deepEqual(second, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), customized);
  assert.doesNotMatch(customized, /\/newer\/K\.AppImage/);
});

test("chmod after initial leased verification preserves the recovery inode", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));
  const recovery = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher].recovery;
  let changed = false;
  fs._beforeLeasedDelete = (target) => {
    if (!changed && recovery.paths.includes(target)) {
      changed = true;
      fs.chmodSync(target, 0o600);
    }
    return null;
  };

  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));

  const preserved = recovery.paths.filter((p) => fs._files.has(p));
  assert.equal(preserved.length, 2);
  for (const p of preserved) assert.equal(fs.lstatSync(p).mode & 0o777, 0o600);
});


test("deleted canonical does not adopt a chmod-customized anchor", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const record = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  fs.unlinkSync(launcher);
  fs.chmodSync(record.anchorPath, 0o600);

  integrateLinuxDesktop(baseDeps(fs));

  assert.ok(!fs._files.has(launcher));
  assert.equal(fs.lstatSync(record.anchorPath).mode & 0o777, 0o600);
  const retained = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.equal(retained.anchorPath, record.anchorPath);
  assert.equal(retained.metadata.mode, record.metadata.mode);
});

test("xattr edit during restored cleanup is never re-adopted", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const linkSync = fs.linkSync;
  let failPublication = true;
  fs.linkSync = (src, dest) => {
    if (failPublication && dest === launcher && src.includes(".tmp-")) {
      failPublication = false;
      throw Object.assign(new Error("EIO"), { code: "EIO" });
    }
    return linkSync(src, dest);
  };
  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));
  let edited = false;
  fs._beforeLeasedDelete = (target) => {
    if (!edited && target.includes(".kirocrew-displaced-")) {
      edited = true;
      fs._setXattr(target, "user.during_restore", "changed");
    }
    return null;
  };

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  const retained = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.ok(retained.pending);
  assert.notEqual(fs._fingerprintFor(launcher), retained.pending.previous.extended);
});


test("interrupted recovery restores an xattr-customized prior inode", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const original = fs._files.get(launcher);
  const linkSync = fs.linkSync;
  let blockLauncherLinks = true;
  fs.linkSync = (src, dest) => {
    if (blockLauncherLinks && dest === launcher) {
      throw Object.assign(new Error("simulated interruption"), { code: "EIO" });
    }
    return linkSync(src, dest);
  };

  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));
  assert.ok(!fs._files.has(launcher));
  const pending = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher].pending;
  const priorPath = pending.previous.paths.find((p) => fs._files.has(p));
  fs._setXattr(priorPath, "user.during_interruption", "changed");
  blockLauncherLinks = false;

  const recovered = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));

  assert.deepEqual(recovered, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);
  assert.notEqual(fs._fingerprintFor(launcher), pending.previous.extended);
  assert.doesNotMatch(fs._files.get(launcher), /\/new\/K\.AppImage/);
});


test("pre-lease xattr edit cannot become replacement provenance", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const original = fs._files.get(launcher);
  const fingerprintFor = fs._fingerprintFor;
  let changed = false;
  fs._fingerprintFor = (target) => {
    if (!changed && target === launcher) {
      changed = true;
      fs._setXattr(target, "user.before_lease", "changed");
    }
    return fingerprintFor(target);
  };

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);
  assert.doesNotMatch(original, /\/new\/K\.AppImage/);
});

test("ambiguous first-install manifest commit retains recoverable temp", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const fsyncSync = fs.fsyncSync;
  let failed = false;
  fs.fsyncSync = (fd) => {
    const body = String(fs._files.get(STATE_PATH) || "");
    if (!failed && fd === STATE_PATH && body.includes(launcher) && body.includes('"pending"')) {
      failed = true;
      throw Object.assign(new Error("ambiguous manifest fsync"), { code: "EIO" });
    }
    return fsyncSync(fd);
  };

  const first = integrateLinuxDesktop(baseDeps(fs));
  assert.deepEqual(first, { skipped: true, reason: "launcher customized" });
  assert.ok(!fs._files.has(launcher));
  const pending = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher].pending;
  assert.ok(fs._files.has(pending.tempPath));

  const recovered = integrateLinuxDesktop(baseDeps(fs));
  assert.equal(recovered.installed, true);
  assert.ok(fs._files.has(launcher));
  const stable = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.equal(stable.pending, undefined);
  assert.equal(fs.lstatSync(launcher).ino, fs.lstatSync(stable.anchorPath).ino);
});


test("trailing-slash XDG_DATA_HOME installs through the canonical boundary", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  const dataRoot = `${HOME}/data`;

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/opt/K.AppImage", APPDIR, HOME, XDG_DATA_HOME: `${dataRoot}/` },
  }));

  assert.equal(res.installed, true);
  assert.ok(fs._files.has(`${dataRoot}/applications/kirocrew-desktop.desktop`));
  assert.ok(fs._files.has(`${dataRoot}/icons/hicolor/16x16/apps/kirocrew-desktop.png`));
});


test("interrupted direct cleanup retains recovery provenance without orphans", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));
  const recovery = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher].recovery;
  fs._beforeLeasedDelete = () => { throw new Error("simulated helper termination"); };

  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));

  const retained = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher].recovery;
  assert.deepEqual(retained, recovery);
  for (const p of recovery.paths) assert.ok(fs._files.has(p));
  assert.ok(![...fs._files.keys()].some((p) => p.includes(".cleanup-")));

  delete fs._beforeLeasedDelete;
  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME }, version: "0.6.0",
  }));
  const stable = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.equal(stable.recovery, undefined);
  for (const p of recovery.paths) assert.ok(!fs._files.has(p));
});
