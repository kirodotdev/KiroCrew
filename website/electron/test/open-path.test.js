"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  LINUX_LAUNCHER_PATH,
  ELECTRON_MAJOR_VERIFIED,
  hardenedLauncherPath,
  openPathHardened,
} = require("../open-path");

// A user PATH with an agent-writable directory sorted AHEAD of the system
// binaries -- the exact shadowing shape this module exists to defeat on Linux.
// If `xdg-open` resolved through this, `/home/u/.local/evil/xdg-open` would win.
const HOSTILE_PATH = "/home/u/.local/evil:/usr/bin:/bin";

describe("hardenedLauncherPath", () => {
  it("is the canonical system bin dirs on Linux", () => {
    assert.equal(hardenedLauncherPath("linux"), "/usr/bin:/bin:/usr/sbin:/sbin");
    assert.equal(hardenedLauncherPath("linux"), LINUX_LAUNCHER_PATH);
  });

  it("contains no user- or agent-writable directory on Linux", () => {
    // Every entry is a root-owned system directory, so a writable entry on the
    // real PATH cannot participate in resolution.
    for (const entry of hardenedLauncherPath("linux").split(":")) {
      assert.match(entry, /^\/(usr\/)?s?bin$/);
    }
  });

  it("is null (no hardening) on macOS and Windows", () => {
    // Those platforms resolve the opener through LaunchServices / the registry,
    // not PATH, so there is nothing to harden -- narrowing would be inert code
    // that reads as protection. null means "leave PATH untouched".
    assert.equal(hardenedLauncherPath("darwin"), null);
    assert.equal(hardenedLauncherPath("win32"), null);
  });
});

describe("openPathHardened (Linux: hardening active)", () => {
  // A fake shell whose openPath records the PATH VISIBLE AT CALL TIME -- this is
  // what the native fork+execvp would resolve `xdg-open` through.
  const recordingShell = (result = "", seen = {}) => ({
    openPath: (p) => {
      seen.pathAtCall = seen.env.PATH;
      seen.arg = p;
      return Promise.resolve(result);
    },
  });

  it("narrows PATH to the Linux launcher path AT THE MOMENT of the call", async () => {
    const env = { PATH: HOSTILE_PATH };
    const seen = { env };
    await openPathHardened(recordingShell("", seen), "/tmp/x.png", {
      platform: "linux",
      env,
    });
    assert.equal(seen.pathAtCall, "/usr/bin:/bin:/usr/sbin:/sbin");
    assert.doesNotMatch(seen.pathAtCall, /evil/);
  });

  it("restores the caller's PATH verbatim after the call", async () => {
    const env = { PATH: HOSTILE_PATH };
    const seen = { env };
    await openPathHardened(recordingShell("", seen), "/tmp/x.png", {
      platform: "linux",
      env,
    });
    // The Gateway spawn and agent tooling read this AFTER the launch -- it must
    // be exactly what it was, hostile entry included (#2367 wants it preserved).
    assert.equal(env.PATH, HOSTILE_PATH);
  });

  it("restores PATH even when openPath throws synchronously", () => {
    const env = { PATH: HOSTILE_PATH };
    const throwingShell = {
      openPath: () => {
        throw new Error("boom");
      },
    };
    assert.throws(
      () => openPathHardened(throwingShell, "/tmp/x.png", { platform: "linux", env }),
      /boom/,
    );
    assert.equal(env.PATH, HOSTILE_PATH);
  });

  it("restores PATH to UNSET when it started unset (never resurrects it as empty)", async () => {
    // An empty PATH string is a cwd entry -- resurrecting the key as "" would be
    // its own shadowing bug, so an absent key must stay absent.
    const env = {};
    const seen = { env };
    await openPathHardened(recordingShell("", seen), "/tmp/x.png", {
      platform: "linux",
      env,
    });
    assert.equal(seen.pathAtCall, "/usr/bin:/bin:/usr/sbin:/sbin");
    assert.equal(Object.prototype.hasOwnProperty.call(env, "PATH"), false);
  });

  it("passes the caller's path through unchanged and returns openPath's promise", async () => {
    const env = { PATH: HOSTILE_PATH };
    const seen = { env };
    const err = await openPathHardened(recordingShell("no app", seen), "/tmp/photo.jpg", {
      platform: "linux",
      env,
    });
    assert.equal(seen.arg, "/tmp/photo.jpg");
    assert.equal(err, "no app");
  });
});

describe("openPathHardened (macOS / Windows: pass-through, no hardening)", () => {
  const recordingShell = (result = "", seen = {}) => ({
    openPath: (p) => {
      seen.pathAtCall = seen.env.PATH;
      seen.arg = p;
      return Promise.resolve(result);
    },
  });

  for (const platform of ["darwin", "win32"]) {
    it(`leaves PATH untouched and still opens the path on ${platform}`, async () => {
      const env = { PATH: HOSTILE_PATH };
      const seen = { env };
      const err = await openPathHardened(recordingShell("", seen), "/tmp/x.txt", {
        platform,
        env,
      });
      // No narrowing: the call sees the real PATH, and it is unchanged after.
      // (These platforms resolve the opener without PATH, so nothing is exposed
      // by leaving it alone -- and the launch runs on a worker anyway, so a
      // narrow/restore around the sync call could not span it even if we tried.)
      assert.equal(seen.pathAtCall, HOSTILE_PATH);
      assert.equal(env.PATH, HOSTILE_PATH);
      assert.equal(seen.arg, "/tmp/x.txt");
      assert.equal(err, "");
    });
  }
});

describe("Electron-version guard for the synchronous-fork assumption", () => {
  // The Linux mitigation is airtight only while Electron's Linux OpenPath forks
  // synchronously inside the shell.openPath() call. That was verified against
  // the pinned major. This test goes RED when package.json's electron pin moves
  // past ELECTRON_MAJOR_VERIFIED, forcing a human to re-verify the Linux launch
  // is still synchronous (platform_util_linux.cc) and bump the constant in the
  // same change -- turning a silent revert into a visible CI failure.
  it("ELECTRON_MAJOR_VERIFIED matches the electron major pinned in package.json", () => {
    const pkg = JSON.parse(
      fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf-8"),
    );
    const spec =
      (pkg.devDependencies && pkg.devDependencies.electron) ||
      (pkg.dependencies && pkg.dependencies.electron) ||
      "";
    const major = Number(spec.replace(/^[^\d]*/, "").split(".")[0]);
    assert.ok(Number.isInteger(major), `could not parse electron major from "${spec}"`);
    assert.equal(
      major,
      ELECTRON_MAJOR_VERIFIED,
      `package.json pins electron ${major} but the synchronous-fork assumption ` +
        `was verified against ${ELECTRON_MAJOR_VERIFIED}. Re-verify Linux OpenPath ` +
        `still forks synchronously in platform_util_linux.cc, then update ` +
        `ELECTRON_MAJOR_VERIFIED in open-path.js.`,
    );
  });
});
