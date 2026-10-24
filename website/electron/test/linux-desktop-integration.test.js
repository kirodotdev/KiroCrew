"use strict";
// Linux AppImage desktop integration: pure helpers and the orchestrator's
// install, update, race, and symlink boundaries against an injected fake fs.
// Ownership/recovery cases live in linux-desktop-integration.ownership.test.js
// and real-filesystem/worker cases in linux-desktop-integration.real-fs.test.js.

const { test } = require("node:test");
const assert = require("node:assert");
const {
  parseDesktopEntry,
  escapeExecArg,
  rewriteExec,
  renderDesktopEntry,
  appImageLauncherName,
  planIntegration,
  dataHome,
  integrateLinuxDesktop,
} = require("../linux-desktop-integration");
const {
  BUNDLED_DESKTOP,
  makeFakeFs,
  APPDIR,
  HOME,
  STATE_ROOT,
  STATE_PATH,
  SYS_BINS,
  bundleWithIcons,
  baseDeps,
} = require("./linux-desktop-integration-test-helpers");

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

test("rewriteExec preserves active extract-and-run mode once before the field code", () => {
  const appImage = '/opt/50%/a"b.AppImage';
  const appDir = "/tmp/appimage_extracted_0123456789abcdef0123456789abcdef";
  const execLine = rewriteExec(
    "AppRun --custom-flag --no-sandbox %U",
    appImage,
    appDir
  );
  assert.equal(
    execLine,
    `${escapeExecArg(appImage)} --custom-flag --appimage-extract-and-run %U`
  );
  assert.doesNotMatch(execLine, /--no-sandbox/);
  for (const arg of process.argv.slice(1)) {
    assert.equal(execLine.includes(arg), false, `copied process argv ${JSON.stringify(arg)}`);
  }

  const duplicated = rewriteExec(
    "AppRun --appimage-extract-and-run --custom-flag --appimage-extract-and-run %U",
    appImage,
    appDir
  );
  assert.equal(duplicated.match(/--appimage-extract-and-run/g).length, 1);
  assert.match(duplicated, /--appimage-extract-and-run %U$/);
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

test("integrateLinuxDesktop creates icons + launcher and resolves refreshers", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16", "128x128", "512x512"]));
  const runs = [];
  const res = integrateLinuxDesktop(baseDeps(fs, { run: (cmd, args) => runs.push([cmd, args]) }));
  assert.deepEqual(res, { installed: true, iconName: "kirocrew-desktop", sizes: 3 });

  // Icons copied under our name, at the shipped sizes only.
  assert.equal(fs._files.get(`${HOME}/.local/share/icons/hicolor/512x512/apps/kirocrew-desktop.png`), "PNG:512x512");
  assert.ok(!fs._files.has(`${HOME}/.local/share/icons/hicolor/256x256/apps/kirocrew-desktop.png`));

  // Launcher written with Exec at the real AppImage and matching WMClass.
  const launcher = fs._files.get(`${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`);
  assert.match(launcher, /^Exec="\/opt\/K\.AppImage" %U$/m);
  assert.match(launcher, /^StartupWMClass=kirocrew-desktop$/m);

  // The private manifest records the exact launcher and icon bytes we wrote.
  const state = JSON.parse(fs._files.get(STATE_PATH));
  assert.equal(state.schema, 3);
  const launcherRecord = state.artifacts[`${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`];
  const iconRecord = state.artifacts[`${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`];
  assert.match(launcherRecord.owned[0], /^[0-9a-f]{64}$/);
  assert.match(iconRecord.owned[0], /^[0-9a-f]{64}$/);
  assert.equal(typeof launcherRecord.metadata.mtimeNs, "string");
  assert.equal(typeof iconRecord.metadata.mtimeNs, "string");
  assert.ok(fs._files.has(launcherRecord.anchorPath));
  assert.equal(
    fs.lstatSync(launcherRecord.anchorPath).ino,
    fs.lstatSync(`${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`).ino
  );

  // Refreshers resolved to ABSOLUTE system-dir paths, kbuildsycoca5 skipped (absent).
  assert.deepEqual(runs.map((r) => r[0]), [
    "/usr/bin/gtk-update-icon-cache",
    "/usr/bin/update-desktop-database",
    "/usr/bin/kbuildsycoca6",
  ]);
});

test("integrateLinuxDesktop preserves the runtime's active extract-and-run mode", () => {
  const appDir = "/tmp/appimage_extracted_0123456789abcdef0123456789abcdef";
  const fs = makeFakeFs(bundleWithIcons(["16x16"], {}, appDir));

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/opt/K.AppImage", APPDIR: appDir, HOME },
  }));

  assert.equal(res.installed, true);
  const launcher = fs._files.get(
    `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`
  );
  assert.match(
    launcher,
    /^Exec="\/opt\/K\.AppImage" --appimage-extract-and-run %U$/m
  );
  assert.equal(launcher.match(/--appimage-extract-and-run/g).length, 1);
  assert.doesNotMatch(launcher, /--no-sandbox/);
});

test("appImageLauncherName gives stable and nightly ids distinct from the package entry", () => {
  assert.equal(appImageLauncherName("kirocrew-desktop"), "kirocrew-desktop-appimage.desktop");
  assert.equal(
    appImageLauncherName("kirocrew-desktop-nightly"),
    "kirocrew-desktop-nightly-appimage.desktop"
  );
  // Neither id equals the deb/rpm entry electron-builder installs for its
  // channel (desktopName), so a user-level launcher never shadows a package.
  for (const packageId of ["kirocrew-desktop.desktop", "kirocrew-desktop-nightly.desktop"]) {
    assert.notEqual(appImageLauncherName("kirocrew-desktop"), packageId);
    assert.notEqual(appImageLauncherName("kirocrew-desktop-nightly"), packageId);
  }
});

test("the AppImage launcher never shadows a same-id package entry and keeps window association", () => {
  // A user-level copy of the package launcher id is what XDG would prefer over
  // /usr/share/applications; the AppImage must neither create nor touch it.
  const packageLauncher = `${HOME}/.local/share/applications/kirocrew-desktop.desktop`;
  const fs = makeFakeFs(bundleWithIcons(["16x16"], {
    [packageLauncher]: "[Desktop Entry]\nName=KiroCrew\nExec=/usr/bin/kirocrew-desktop %U\n", // brand-ok -- package launcher fixture
  }));

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { installed: true, iconName: "kirocrew-desktop", sizes: 1 });
  const managed = fs._files.get(`${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`);
  assert.ok(managed, "managed launcher written under its own id");
  // StartupWMClass still equals Electron's app_id (desktopName sans suffix), so
  // GNOME/KDE map the running window to this entry through that key, not the id.
  assert.match(managed, /^StartupWMClass=kirocrew-desktop$/m);
  assert.match(managed, /^Icon=kirocrew-desktop$/m);
  assert.equal(
    fs._files.get(packageLauncher),
    "[Desktop Entry]\nName=KiroCrew\nExec=/usr/bin/kirocrew-desktop %U\n" // brand-ok -- package launcher fixture
  );
  const state = JSON.parse(fs._files.get(STATE_PATH));
  assert.equal(state.artifacts[packageLauncher], undefined);
});

test("nightly AppImage keeps its own launcher id and icon name beside stable", () => {
  const nightlyEntry = BUNDLED_DESKTOP
    .replace("Name=Kiro Crew", "Name=Kiro Crew Nightly")
    .replace("Icon=kirocrew-desktop", "Icon=kirocrew-desktop-nightly")
    .replace("StartupWMClass=kirocrew-desktop", "StartupWMClass=kirocrew-desktop-nightly");
  const stableLauncher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
  const fs = makeFakeFs({
    [`${APPDIR}/kirocrew-desktop-nightly.desktop`]: nightlyEntry,
    [`${APPDIR}/usr/share/icons/hicolor/16x16/apps/kirocrew-desktop-nightly.png`]: "PNG:nightly",
    [stableLauncher]: "stable launcher body",
    ...SYS_BINS,
  });

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { installed: true, iconName: "kirocrew-desktop-nightly", sizes: 1 });
  const launcher = fs._files.get(
    `${HOME}/.local/share/applications/kirocrew-desktop-nightly-appimage.desktop`
  );
  assert.match(launcher, /^Name=Kiro Crew Nightly$/m);
  assert.match(launcher, /^Icon=kirocrew-desktop-nightly$/m);
  assert.match(launcher, /^StartupWMClass=kirocrew-desktop-nightly$/m);
  assert.equal(fs._files.get(stableLauncher), "stable launcher body");
  assert.ok(!fs._files.has(`${HOME}/.local/share/applications/kirocrew-desktop-nightly.desktop`));
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;

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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
  const original = fs._files.get(launcher);
  fs._files.set(STATE_PATH, "not json");

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "manifest unavailable" });
  assert.equal(fs._files.get(launcher), original);
});



test("recreates a first-install artifact after public and pending temp are deleted", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
  const linkSync = fs.linkSync;
  let failLauncher = true;
  fs.linkSync = (src, dest) => {
    if (dest === launcher && failLauncher) {
      failLauncher = false;
      throw Object.assign(new Error("ENOSPC"), { code: "ENOSPC" });
    }
    return linkSync(src, dest);
  };

  integrateLinuxDesktop(baseDeps(fs));
  const stale = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.equal(stale.anchorPath, undefined);
  assert.ok(fs._files.has(stale.pending.tempPath));
  fs.unlinkSync(stale.pending.tempPath);

  const recreated = integrateLinuxDesktop(baseDeps(fs));
  assert.equal(recreated.installed, true);
  assert.ok(fs._files.has(launcher));
  const stable = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.ok(fs._files.has(stable.anchorPath));
  assert.notEqual(stable.anchorPath, stale.pending.tempPath);
  assert.equal(fs.lstatSync(launcher).ino, fs.lstatSync(stable.anchorPath).ino);
});
test("integrateLinuxDesktop restores content changed at the displacement boundary", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  assert.ok(!packageFs._files.has(`${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`));
});

test("integrateLinuxDesktop leaves an existing desktop integration untouched", () => {
  const present = bundleWithIcons(["16x16"]);
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  assert.ok(fs._files.has(`${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`));
});

test("integrateLinuxDesktop loses a launcher-creation race without overwriting the winner", () => {
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcherPath = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  assert.ok(!fs._files.has(`${appsDir}/kirocrew-desktop-appimage.desktop`));
  assert.ok(fs._files.has(`${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`));
});

test("integrateLinuxDesktop refuses an untrusted manifest directory", () => {
  for (const [label, fs] of [
    ["symlink", makeFakeFs(bundleWithIcons(["16x16"]), { symlinks: [STATE_ROOT] })],
    ["other owner", makeFakeFs(bundleWithIcons(["16x16"]), { dirs: [STATE_ROOT], uid: 2000 })],
  ]) {
    const res = integrateLinuxDesktop(baseDeps(fs));
    assert.deepEqual(res, { skipped: true, reason: "manifest unavailable" }, label);
    assert.ok(!fs._files.has(`${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`), label);
  }
});

test("integrateLinuxDesktop skips cleanly when the bundle ships no icons", () => {
  const fs = makeFakeFs({ [`${APPDIR}/kirocrew-desktop.desktop`]: BUNDLED_DESKTOP, ...SYS_BINS });
  const res = integrateLinuxDesktop(baseDeps(fs));
  assert.equal(res.skipped, true);
  assert.equal(res.reason, "no managed icons");
});

test("absent deferred icons do not authorize launcher restoration", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  assert.equal(integrateLinuxDesktop(baseDeps(fs)).installed, true);
  const icon = `${HOME}/.local/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`;
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
  const source = `${APPDIR}/usr/share/icons/hicolor/16x16/apps/kirocrew-desktop.png`;
  fs._files.set(source, "UPDATED ICON");
  assert.equal(integrateLinuxDesktop(baseDeps(fs)).installed, true);
  const recovery = JSON.parse(fs._files.get(STATE_PATH)).artifacts[icon].recovery;
  assert.ok(recovery);
  fs.unlinkSync(icon);
  fs.unlinkSync(launcher);
  fs._beforeLeasedDelete = (target) => recovery.paths.includes(target) ? "busy" : null;

  const res = integrateLinuxDesktop(baseDeps(fs));

  assert.deepEqual(res, { skipped: true, reason: "no managed icons" });
  assert.ok(!fs._files.has(icon));
  assert.ok(!fs._files.has(launcher));
});

test("a missing launcher with deferred recovery cleanup reports a deferred outcome", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;

  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  const recovery = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher].recovery;
  assert.ok(recovery);

  fs.unlinkSync(launcher);
  fs._beforeLeasedDelete = (target) => (recovery.paths.includes(target) ? "busy" : null);

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher deferred" });
  assert.ok(!fs._files.has(launcher));
});
