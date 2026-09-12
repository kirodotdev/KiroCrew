"use strict";
// Linux first-run desktop integration: pure helpers, plus the orchestrator
// driven against an in-memory fake fs. No real filesystem or process is touched.

const { test } = require("node:test");
const assert = require("node:assert");
const {
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

test("rewriteExec swaps the program token for the AppImage path, keeping args", () => {
  assert.equal(
    rewriteExec("AppRun --no-sandbox %U", "/opt/KiroCrew-x86_64.AppImage"),
    '"/opt/KiroCrew-x86_64.AppImage" --no-sandbox %U'
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
  assert.doesNotMatch(body, /^X-KiroCrew-Generated=/m); // omitted when no generated field
});

test("renderDesktopEntry stamps the ownership marker when generated is set", () => {
  const body = renderDesktopEntry({ name: "K", execLine: '"x" %U', iconName: "k", generated: "0.5.0" });
  assert.match(body, /^X-KiroCrew-Generated=0\.5\.0$/m);
});

test("launcherOwnership tells ours from foreign, symlink, and absent", () => {
  const p = "/a/x.desktop";
  const ours = { lstatSync: () => ({ isSymbolicLink: () => false }), readFileSync: () => "[Desktop Entry]\nExec=/x\nX-KiroCrew-Generated=1.2.3\n" };
  const foreign = { lstatSync: () => ({ isSymbolicLink: () => false }), readFileSync: () => "[Desktop Entry]\nExec=/x\n" };
  const link = { lstatSync: () => ({ isSymbolicLink: () => true }) };
  const absent = { lstatSync: () => { throw Object.assign(new Error("ENOENT"), { code: "ENOENT" }); } };
  assert.equal(launcherOwnership(ours, p), "ours");
  assert.equal(launcherOwnership(foreign, p), "foreign");
  assert.equal(launcherOwnership(link, p), "foreign"); // a symlink we did not create is never ours
  assert.equal(launcherOwnership(absent, p), "absent");
});

test("planIntegration gates on the injected platform and both AppImage vars", () => {
  assert.equal(planIntegration({ APPIMAGE: "/a", APPDIR: "/b" }, "darwin").integrate, false);
  assert.equal(planIntegration({ APPDIR: "/b" }, "linux").integrate, false);
  assert.equal(planIntegration({ APPIMAGE: "/a" }, "linux").integrate, false);
  const ok = planIntegration({ APPIMAGE: "/a", APPDIR: "/b" }, "linux");
  assert.deepEqual(ok, { integrate: true, appImage: "/a", appDir: "/b" });
});

test("planIntegration honors the KIROCREW_DISABLE_DESKTOP_INTEGRATION opt-out", () => {
  for (const v of ["1", "true", "YES", " on "]) {
    const r = planIntegration({ APPIMAGE: "/a", APPDIR: "/b", KIROCREW_DISABLE_DESKTOP_INTEGRATION: v }, "linux");
    assert.equal(r.integrate, false, `opt-out value ${JSON.stringify(v)} should skip`);
    assert.match(r.reason, /opt-out/);
  }
  // a non-truthy value does not opt out
  assert.equal(
    planIntegration({ APPIMAGE: "/a", APPDIR: "/b", KIROCREW_DISABLE_DESKTOP_INTEGRATION: "0" }, "linux").integrate,
    true
  );
});

test("hasExternalIntegration detects an appimagekit_ launcher for THIS AppImage", () => {
  const appsDir = "/home/u/.local/share/applications";
  const mk = (files) => ({
    readdirSync: (d) => (d === appsDir ? Object.keys(files) : (() => { throw new Error("nope"); })()),
    readFileSync: (p) => {
      const name = p.slice(appsDir.length + 1);
      if (!(name in files)) throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
      return files[name];
    },
  });
  const path = fakePath;
  // an appimagekit entry whose Exec points at our AppImage -> present
  const withOurs = mk({
    "appimagekit_abc-Kiro_Crew.desktop": "[Desktop Entry]\nExec=/opt/K.AppImage %U\nIcon=x\n",
    "other.desktop": "[Desktop Entry]\nExec=/usr/bin/thing\n",
  });
  assert.equal(hasExternalIntegration(withOurs, path, appsDir, "/opt/K.AppImage"), true);
  // an appimagekit entry for a DIFFERENT AppImage -> absent
  const withOther = mk({ "appimagekit_zzz-Other.desktop": "[Desktop Entry]\nExec=/opt/Other.AppImage %U\n" });
  assert.equal(hasExternalIntegration(withOther, path, appsDir, "/opt/K.AppImage"), false);
  // no appimagekit entries, or an unreadable apps dir -> absent
  assert.equal(hasExternalIntegration(mk({ "plain.desktop": "[Desktop Entry]\nExec=/opt/K.AppImage\n" }), path, appsDir, "/opt/K.AppImage"), false);
  assert.equal(hasExternalIntegration({ readdirSync: () => { throw new Error("ENOENT"); } }, path, appsDir, "/opt/K.AppImage"), false);
});

test("execRefersToPath compares the whole first token, not a substring", () => {
  assert.equal(execRefersToPath('"/opt/K.AppImage" %U', "/opt/K.AppImage"), true);
  assert.equal(execRefersToPath("/opt/K.AppImage %U", "/opt/K.AppImage"), true);
  assert.equal(execRefersToPath("/opt/K.AppImage", "/opt/K.AppImage"), true); // bare TryExec
  // path-prefix collision must NOT match
  assert.equal(execRefersToPath('"/opt/K.AppImage.bak" %U', "/opt/K.AppImage"), false);
  assert.equal(execRefersToPath("/opt/K.AppImage2 %U", "/opt/K.AppImage"), false);
  assert.equal(execRefersToPath(undefined, "/opt/K.AppImage"), false);
});

test("hasExternalIntegration does not false-positive on a path-prefix collision", () => {
  const appsDir = "/home/u/.local/share/applications";
  const fs = {
    readdirSync: (d) => (d === appsDir ? ["appimagekit_x-Bak.desktop"] : (() => { throw new Error("nope"); })()),
    readFileSync: () => "[Desktop Entry]\nExec=/opt/K.AppImage.bak %U\n",
  };
  assert.equal(hasExternalIntegration(fs, fakePath, appsDir, "/opt/K.AppImage"), false);
});

test("dataHome honors an absolute XDG_DATA_HOME, else ~/.local/share", () => {
  assert.equal(dataHome({ XDG_DATA_HOME: "/custom/data" }, "/home/u"), "/custom/data");
  assert.equal(dataHome({ XDG_DATA_HOME: "relative" }, "/home/u"), "/home/u/.local/share");
  assert.equal(dataHome({}, "/home/u"), "/home/u/.local/share");
});

test("isCurrent is true only when the stamp matches this version AND AppImage path", () => {
  const stamp = JSON.stringify({ version: "0.5.0", appImage: "/opt/K.AppImage" });
  const fs = { readFileSync: (p) => (p === "S" ? stamp : (() => { throw new Error("nope"); })()) };
  assert.equal(isCurrent(fs, "S", "0.5.0", "/opt/K.AppImage"), true);
  assert.equal(isCurrent(fs, "S", "0.6.0", "/opt/K.AppImage"), false);   // version changed
  assert.equal(isCurrent(fs, "S", "0.5.0", "/new/K.AppImage"), false);   // AppImage moved
  assert.equal(isCurrent({ readFileSync: () => { throw new Error("enoent"); } }, "S", "0.5.0", "/opt/K.AppImage"), false);
});

// ---- orchestrator against an in-memory fake fs ----

function makeFakeFs(present, opts = {}) {
  const files = new Map(Object.entries(present));
  const dirs = new Set();
  const symlinks = new Set(opts.symlinks || []);
  const enoent = () => Object.assign(new Error("ENOENT"), { code: "ENOENT" });
  return {
    _files: files,
    _dirs: dirs,
    existsSync: (p) => files.has(p),
    readFileSync: (p) => { if (!files.has(p)) throw enoent(); return files.get(p); },
    readdirSync: (d) =>
      [...files.keys()]
        .filter((p) => p.startsWith(d + "/") && !p.slice(d.length + 1).includes("/"))
        .map((p) => p.slice(d.length + 1)),
    mkdirSync: (d) => { dirs.add(d); },
    writeFileSync: (p, data, o) => {
      if (o && o.flag && o.flag.includes("x") && files.has(p)) {
        throw Object.assign(new Error("EEXIST"), { code: "EEXIST" });
      }
      files.set(p, data);
    },
    renameSync: (a, b) => { if (!files.has(a)) throw enoent(); files.set(b, files.get(a)); files.delete(a); },
    unlinkSync: (p) => { files.delete(p); },
    copyFileSync: (src, dest) => { if (!files.has(src)) throw enoent(); files.set(dest, files.get(src)); },
    lstatSync: (p) => {
      const link = symlinks.has(p);
      if (!link && !files.has(p) && !dirs.has(p)) throw enoent();
      return { isSymbolicLink: () => link };
    },
    realpathSync: (p) => (opts.realpath && opts.realpath[p]) || p, // ancestor symlinks modelled via opts.realpath
  };
}

const fakePath = {
  join: (...parts) => parts.join("/").replace(/\/+/g, "/"),
  dirname: (p) => p.slice(0, p.lastIndexOf("/")) || "/",
};

const APPDIR = "/mnt/appimage";
const HOME = "/home/u";
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
  return {
    fs, path: fakePath, os: { homedir: () => HOME },
    platform: "linux",
    env: { APPIMAGE: "/opt/K.AppImage", APPDIR, HOME },
    version: "0.5.0",
    stampDir: `${HOME}/.config/kirocrew-desktop`,
    run: () => {},
    log: () => {},
    ...overrides,
  };
}

test("integrateLinuxDesktop installs icons + launcher, resolves refreshers, stamps", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16", "128x128", "512x512"]));
  const runs = [];
  const res = integrateLinuxDesktop(baseDeps(fs, { run: (cmd, args) => runs.push([cmd, args]) }));
  assert.deepEqual(res, { installed: true, iconName: "kirocrew-desktop", sizes: 3 });

  // Icons copied under our name, at the shipped sizes only.
  assert.equal(fs._files.get(`${HOME}/.local/share/icons/hicolor/512x512/apps/kirocrew-desktop.png`), "PNG:512x512");
  assert.ok(!fs._files.has(`${HOME}/.local/share/icons/hicolor/256x256/apps/kirocrew-desktop.png`));

  // Launcher written with Exec at the real AppImage and matching WMClass.
  const launcher = fs._files.get(`${HOME}/.local/share/applications/kirocrew-desktop.desktop`);
  assert.match(launcher, /^Exec="\/opt\/K\.AppImage" --no-sandbox %U$/m);
  assert.match(launcher, /^StartupWMClass=kirocrew-desktop$/m);

  // Refreshers resolved to ABSOLUTE system-dir paths, kbuildsycoca5 skipped (absent).
  assert.deepEqual(runs.map((r) => r[0]), [
    "/usr/bin/gtk-update-icon-cache",
    "/usr/bin/update-desktop-database",
    "/usr/bin/kbuildsycoca6",
  ]);

  // Stamp records version AND the AppImage path.
  const stamp = JSON.parse(fs._files.get(`${HOME}/.config/kirocrew-desktop/linux-desktop-integration.json`));
  assert.equal(stamp.version, "0.5.0");
  assert.equal(stamp.appImage, "/opt/K.AppImage");
});

test("integrateLinuxDesktop is a no-op off Linux and for a non-AppImage launch", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  assert.equal(integrateLinuxDesktop(baseDeps(fs, { platform: "darwin" })).skipped, true);
  assert.equal(integrateLinuxDesktop(baseDeps(fs, { env: { HOME } })).skipped, true);
});

test("integrateLinuxDesktop skips when an appimagekit_ launcher already integrates this AppImage", () => {
  const present = bundleWithIcons(["16x16"]);
  present[`${HOME}/.local/share/applications/appimagekit_abc-Kiro_Crew.desktop`] =
    "[Desktop Entry]\nExec=/opt/K.AppImage %U\nIcon=kirocrew\n";
  const fs = makeFakeFs(present);
  const res = integrateLinuxDesktop(baseDeps(fs));
  assert.deepEqual(res, { skipped: true, reason: "external integration present" });
  // our own launcher was NOT written
  assert.ok(!fs._files.has(`${HOME}/.local/share/applications/kirocrew-desktop.desktop`));
});

test("integrateLinuxDesktop backs up a foreign launcher instead of clobbering it", () => {
  const present = bundleWithIcons(["16x16"]);
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const foreignBody = "[Desktop Entry]\nName=My Custom\nExec=/opt/K.AppImage --my-flag %U\n"; // no ownership key
  present[launcher] = foreignBody;
  const fs = makeFakeFs(present);
  const res = integrateLinuxDesktop(baseDeps(fs));
  assert.equal(res.installed, true);
  // the hand-made launcher was preserved verbatim in a backup
  assert.equal(fs._files.get(`${launcher}.kirocrew-bak`), foreignBody);
  // and ours, carrying the ownership marker, now occupies the launcher path
  assert.match(fs._files.get(launcher), /^X-KiroCrew-Generated=0\.5\.0$/m);
});

test("integrateLinuxDesktop removes our own launcher when an external daemon now integrates", () => {
  const present = bundleWithIcons(["16x16"]);
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  present[launcher] = "[Desktop Entry]\nExec=/opt/K.AppImage %U\nX-KiroCrew-Generated=0.4.0\n"; // ours
  present[`${HOME}/.local/share/applications/appimagekit_abc-Kiro.desktop`] = "[Desktop Entry]\nExec=/opt/K.AppImage %U\n";
  const fs = makeFakeFs(present);
  const res = integrateLinuxDesktop(baseDeps(fs));
  assert.deepEqual(res, { skipped: true, reason: "external integration present" });
  assert.ok(!fs._files.has(launcher)); // our duplicate removed
});

test("integrateLinuxDesktop leaves a FOREIGN launcher intact when an external daemon integrates", () => {
  const present = bundleWithIcons(["16x16"]);
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  present[launcher] = "[Desktop Entry]\nName=Hand Made\nExec=/opt/K.AppImage %U\n"; // foreign, no marker
  present[`${HOME}/.local/share/applications/appimagekit_abc-Kiro.desktop`] = "[Desktop Entry]\nExec=/opt/K.AppImage %U\n";
  const fs = makeFakeFs(present);
  const res = integrateLinuxDesktop(baseDeps(fs));
  assert.equal(res.reason, "external integration present");
  assert.ok(fs._files.has(launcher)); // never remove a launcher we did not author
});

test("integrateLinuxDesktop skips when the stamp matches this version and path", () => {
  const present = bundleWithIcons(["16x16"]);
  present[`${HOME}/.config/kirocrew-desktop/linux-desktop-integration.json`] =
    JSON.stringify({ version: "0.5.0", appImage: "/opt/K.AppImage" });
  const fs = makeFakeFs(present);
  const res = integrateLinuxDesktop(baseDeps(fs));
  assert.deepEqual(res, { skipped: true, reason: "already current" });
});

test("integrateLinuxDesktop re-integrates after the AppImage is MOVED", () => {
  // Stamp says the previous install was at the old path; the app now runs from a new path.
  const present = bundleWithIcons(["16x16"]);
  present[`${HOME}/.config/kirocrew-desktop/linux-desktop-integration.json`] =
    JSON.stringify({ version: "0.5.0", appImage: "/old/K.AppImage" });
  const fs = makeFakeFs(present);
  const res = integrateLinuxDesktop(baseDeps(fs, { env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME } }));
  assert.equal(res.installed, true);
  // Launcher now points at the NEW path, and the stamp is updated.
  assert.match(fs._files.get(`${HOME}/.local/share/applications/kirocrew-desktop.desktop`), /^Exec="\/new\/K\.AppImage"/m);
  assert.equal(JSON.parse(fs._files.get(`${HOME}/.config/kirocrew-desktop/linux-desktop-integration.json`)).appImage, "/new/K.AppImage");
});

test("safeWrite refuses a symlinked destination (no write-through)", () => {
  const launcherPath = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const fs = makeFakeFs(bundleWithIcons(["16x16"]), { symlinks: [launcherPath] });
  const res = integrateLinuxDesktop(baseDeps(fs));
  assert.equal(res.skipped, true);
  assert.equal(res.reason, "launcher write failed");
  // The launcher was NOT written through the symlink.
  assert.ok(!fs._files.has(launcherPath));
});

test("safeWrite refuses a destination whose real directory escapes the data root", () => {
  // The applications dir's realpath resolves OUTSIDE the data root (a symlinked
  // ancestor), which safeWrite must refuse even though the leaf is no symlink.
  const fs = makeFakeFs(bundleWithIcons(["16x16"]), {
    realpath: { [`${HOME}/.local/share/applications`]: "/evil/applications" },
  });
  const res = integrateLinuxDesktop(baseDeps(fs));
  assert.equal(res.skipped, true);
  assert.equal(res.reason, "launcher write failed");
  assert.ok(!fs._files.has(`${HOME}/.local/share/applications/kirocrew-desktop.desktop`));
  // the icons still installed (their real dir stays inside the root)
  assert.ok(fs._files.has(`${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`));
});

test("integrateLinuxDesktop skips cleanly when the bundle ships no icons", () => {
  const fs = makeFakeFs({ [`${APPDIR}/kirocrew-desktop.desktop`]: BUNDLED_DESKTOP, ...SYS_BINS });
  const res = integrateLinuxDesktop(baseDeps(fs));
  assert.equal(res.skipped, true);
  assert.equal(res.reason, "no icons in bundle");
});
