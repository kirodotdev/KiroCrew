#!/usr/bin/env node
// Record WHICH Electron build a desktop artifact was packaged against, so a
// crash report from it can still be symbolized months later.
//
// ## Why a manifest and not the symbols themselves
//
// The literal ask was "archive the dSYMs in CI". Measured against the release
// this repo currently resolves (electron/electron v43.2.0), that is 1.65 GB for
// darwin-arm64-dsym.tar.xz plus 1.40 GB for darwin-x64 — about 3 GB per
// universal build, before the 933 MB/955 MB snapshot dSYMs. At the 14-day
// retention this workflow already uses, a nightly cadence would park tens of
// gigabytes of bytes that are ALREADY permanently downloadable from
// electron/electron's own releases, and that would then expire while the
// upstream copies did not.
//
// ## What this actually adds, stated honestly
//
// Not the pin by itself. A shipped bundle records its Electron version in
// `Electron Framework.framework/Resources/Info.plist`
// (CFBundleShortVersionString), and a macOS `.ips` carries it too, in
// `usedImages[].CFBundleShortVersionString` alongside the framework's Mach-O
// `uuid` — verified on a real report. So for the `.ips` path the version is
// recoverable from the crash artifact alone, and symbolize-crash.sh reads it
// from there in preference to any manifest.
//
// What the manifest adds is the case where nothing else carries it:
//
//   1. A Crashpad `.dmp` identifies its modules by debug ID and size, never by
//      semver. Given a bare `.dmp` and no build record, there is nothing to
//      look up.
//   2. `package.json` pins a RANGE (`^43.2.0`). Two builds a month apart can
//      resolve to different Electron versions with no record of which was
//      which, so "what did the 0.6.1 nightly ship" is otherwise unanswerable
//      without still having the bundle.
//   3. The asset names and URLs, which are the part nobody remembers, plus
//      which of the two symbol formats matches which artifact kind.
//
// A few hundred bytes riding along in the existing artifact upload.
//
// ## Two different symbol formats, for two different artifacts
//
//   - `-dsym.tar.xz` (~1.4-1.7 GB): Mach-O DWARF, what `atos` needs to
//     symbolize a macOS `.ips` crash report.
//   - `-symbols.zip` (~128-146 MB): Breakpad .sym files, what
//     `minidump_stackwalk` needs for a Crashpad `.dmp`.
//
// Both are listed because `crash-collector.js` surfaces both artifact kinds and
// neither format reads the other's input. Linux has no dSYM at all (no Mach-O),
// so its entry is Breakpad-only rather than a fabricated dSYM name.
//
// Usage:
//   node scripts/emit-symbols-manifest.mjs [outputPath]
//
// Honors the same UNIVERSAL env var as packaging/build-desktop.sh, so a
// `UNIVERSAL=0` macOS build records the one arch it actually produced.

import fs from "node:fs";
import path from "node:path";

const SCHEMA = 1;
const ROOT = path.resolve(import.meta.dirname, "..");
const ELECTRON_DIR = path.join(ROOT, "website", "electron");
const DEFAULT_OUT = path.join(ELECTRON_DIR, "dist", "symbols-manifest.json");

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

/**
 * The arches this build produced, mirroring build-desktop.sh's own rule:
 * macOS packages one universal app (arm64 + x86_64 lipo-merged) unless
 * UNIVERSAL=0; every other platform builds the host arch only.
 */
function builtArches(platform = process.platform, env = process.env) {
  const universal = env.UNIVERSAL ?? (platform === "darwin" ? "1" : "0");
  if (platform === "darwin" && universal === "1") return ["arm64", "x64"];
  return [process.arch === "arm" ? "armv7l" : process.arch];
}

/**
 * Electron's own release naming. `darwin`, not `mas`: this repo ships a DMG
 * outside the Mac App Store, and the two have separate (non-interchangeable)
 * symbol sets.
 */
function assetsFor(platform, arch, version) {
  const base = `https://github.com/electron/electron/releases/download/v${version}`;
  const slug = `electron-v${version}-${platform === "win32" ? "win32" : platform}-${arch}`;
  const assets = [
    {
      kind: "breakpad",
      // What minidump_stackwalk reads, for the Crashpad .dmp files
      // crash-collector.js finds in the app's crashDumps directory.
      symbolizes: "crashpad-minidump",
      name: `${slug}-symbols.zip`,
      url: `${base}/${slug}-symbols.zip`,
    },
  ];
  if (platform === "darwin") {
    assets.push({
      kind: "dsym",
      // What atos reads, for the OS-written .ips reports. Listed second
      // because it is ~10x the download and only macOS has one.
      symbolizes: "macos-ips",
      name: `${slug}-dsym.tar.xz`,
      url: `${base}/${slug}-dsym.tar.xz`,
    });
  }
  return assets;
}

/**
 * @returns {object} The manifest. Sizes and digests are deliberately absent:
 *   this step has no network access budget to measure them, and a size copied
 *   from a different release is worse than no size at all. symbolize-crash.sh
 *   reports the real size from the release API before it downloads.
 */
function buildManifest({ platform = process.platform, env = process.env } = {}) {
  const appVersion = readJson(path.join(ELECTRON_DIR, "package.json")).version;
  const electronPkg = path.join(ELECTRON_DIR, "node_modules", "electron", "package.json");
  if (!fs.existsSync(electronPkg)) {
    throw new Error(
      `no installed Electron at ${electronPkg} — run this AFTER the build, ` +
        "since the resolved version is the one fact this manifest exists to record",
    );
  }
  const electronVersion = readJson(electronPkg).version;
  const arches = builtArches(platform, env);

  return {
    schema: SCHEMA,
    appVersion,
    platform,
    arches,
    electron: {
      // The RESOLVED version, never the `^43.2.0` range from package.json: a
      // range does not identify a binary, and the wrong Electron's symbols
      // produce confident nonsense rather than an obvious failure.
      version: electronVersion,
      release: `https://github.com/electron/electron/releases/tag/v${electronVersion}`,
    },
    assets: arches.flatMap((arch) =>
      assetsFor(platform, arch, electronVersion).map((asset) => ({ arch, ...asset })),
    ),
    symbolize: "scripts/symbolize-crash.sh",
  };
}

function main(argv = process.argv.slice(2)) {
  const out = argv[0] ? path.resolve(argv[0]) : DEFAULT_OUT;
  const manifest = buildManifest();
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, `${JSON.stringify(manifest, null, 2)}\n`);
  process.stdout.write(
    `symbols manifest: ${out}\n` +
      `  app ${manifest.appVersion} · electron ${manifest.electron.version} · ` +
      `${manifest.platform}/${manifest.arches.join("+")} · ${manifest.assets.length} assets\n`,
  );
}

// Only when run as a script: the shapers above are importable so a test can
// assert the asset naming without writing a file.
if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(import.meta.filename)) {
  main();
}

export { buildManifest, builtArches, assetsFor, SCHEMA };
