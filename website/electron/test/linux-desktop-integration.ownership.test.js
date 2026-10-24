"use strict";
// Linux AppImage desktop integration: metadata ownership, manifest trust, and
// interrupted-update recovery boundaries against an injected fake fs.

const { test } = require("node:test");
const assert = require("node:assert");
const { integrateLinuxDesktop } = require("../linux-desktop-integration");
const {
  makeFakeFs,
  APPDIR,
  HOME,
  STATE_ROOT,
  STATE_PATH,
  bundleWithIcons,
  baseDeps,
} = require("./linux-desktop-integration-test-helpers");

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
  assert.ok(!fs._files.has(`${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`));
});

test("metadata-only chmod makes a managed launcher user-owned", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
  const original = fs._files.get(launcher);
  fs._touchMetadata(launcher); // models an ACL/xattr update with unchanged bytes and mode

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);
});


test("metadata mtime drift makes a managed launcher user-owned", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
  const original = fs._files.get(launcher);
  const customizedMtime = fs.lstatSync(launcher).mtimeNs + 10_000_000_000n;
  fs._setMtime(launcher, customizedMtime);

  const res = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);
  assert.equal(fs.lstatSync(launcher).mtimeNs, customizedMtime);
});


test("mtime edit after busy moved-AppImage repair revokes restored ownership", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
  const original = fs._files.get(launcher);
  fs._beforeLeasedPublish = (target) => target === launcher ? "busy" : null;

  const interrupted = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  assert.deepEqual(interrupted, { skipped: true, reason: "launcher customized" });
  assert.ok(JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher].pending);

  fs._beforeLeasedPublish = null; // the external descriptor is now closed
  const customizedMtime = fs.lstatSync(launcher).mtimeNs + 10_000_000_000n;
  fs._setMtime(launcher, customizedMtime);

  const retried = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));

  assert.deepEqual(retried, { skipped: true, reason: "launcher customized" });
  assert.equal(fs._files.get(launcher), original);
  assert.doesNotMatch(fs._files.get(launcher), /\/new\/K\.AppImage/);
  assert.equal(fs.lstatSync(launcher).mtimeNs, customizedMtime);
});

test("verified obsolete anchor and recovery names are removed on the next launch", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;

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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;

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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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

test("deleted interrupted-update provenance permits fresh ownership and later moves", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
  const linkSync = fs.linkSync;
  let blockLauncherLinks = true;
  fs.linkSync = (src, dest) => {
    if (blockLauncherLinks && dest === launcher) {
      throw Object.assign(new Error("simulated power loss boundary"), { code: "EIO" });
    }
    return linkSync(src, dest);
  };

  integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  assert.ok(!fs._files.has(launcher));
  const stale = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.ok(stale.pending.previous);
  fs.unlinkSync(launcher);
  fs.unlinkSync(stale.pending.tempPath);
  for (const candidate of stale.pending.previous.paths) fs.unlinkSync(candidate);

  blockLauncherLinks = false;
  const recovered = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/new/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  assert.equal(recovered.installed, true);
  assert.match(fs._files.get(launcher), /^Exec="\/new\/K\.AppImage"/m);
  const fresh = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.equal(fresh.pending, undefined);
  assert.equal(fs.lstatSync(launcher).ino, fs.lstatSync(fresh.anchorPath).ino);

  const moved = integrateLinuxDesktop(baseDeps(fs, {
    env: { APPIMAGE: "/moved/K.AppImage", APPDIR, HOME },
    version: "0.6.0",
  }));
  assert.equal(moved.installed, true);
  assert.match(fs._files.get(launcher), /^Exec="\/moved\/K\.AppImage"/m);
});

test("recreating a deleted managed launcher retires its old anchor", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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


test("recreates a managed launcher after both public file and anchor are deleted", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
  const oldAnchor = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher].anchorPath;
  fs.unlinkSync(launcher);
  fs.unlinkSync(oldAnchor);

  const recreated = integrateLinuxDesktop(baseDeps(fs));
  assert.equal(recreated.installed, true);
  assert.ok(fs._files.has(launcher));
  const stable = JSON.parse(fs._files.get(STATE_PATH)).artifacts[launcher];
  assert.ok(fs._files.has(stable.anchorPath));
  assert.notEqual(stable.anchorPath, oldAnchor);
});

test("restored replacement failure leaves only the current anchor after cleanup", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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

test("deleted canonical does not adopt an xattr-edited anchor", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
  assert.ok(fs._files.has(`${dataRoot}/applications/kirocrew-desktop-appimage.desktop`));
  assert.ok(fs._files.has(`${dataRoot}/icons/hicolor/16x16/apps/kirocrew-desktop.png`));
});


test("interrupted direct cleanup retains recovery provenance without orphans", () => {
  const fs = makeFakeFs(bundleWithIcons(["16x16"]));
  integrateLinuxDesktop(baseDeps(fs));
  const launcher = `${HOME}/.local/share/applications/kirocrew-desktop-appimage.desktop`;
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
