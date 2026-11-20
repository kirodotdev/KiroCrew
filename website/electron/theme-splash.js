"use strict";

// The active theme pack's boot-splash art (`branding/splash.*`). The dashboard
// reports the image bytes whenever the active theme changes, and the next
// loading.html paint shows them, before any gateway is up to serve the pack.
// Like the theme accent, it is the last value reported; one file under the
// shell's own userData dir, since the shell cannot read a remote gateway's home.
// The pack's `branding.splashFit` rides in the file name (`theme-splash-cover.*`
// fills the window), so a fit is never stored apart from the art it came with.

const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");

// The two types the pack validator accepts for branding/splash, and its cap.
const SPLASH_EXTENSIONS = Object.freeze({ "image/png": "png", "image/svg+xml": "svg" });
const SPLASH_MAX_BYTES = 512 * 1024;
const SPLASH_STEMS = Object.freeze(["theme-splash", "theme-splash-cover"]);

function splashFiles(dir) {
  return SPLASH_STEMS.flatMap((stem) =>
    Object.values(SPLASH_EXTENSIONS).map((ext) => path.join(dir, `${stem}.${ext}`)));
}

/**
 * Keep `image` ({ type, bytes, fit }) as the splash art, replacing any earlier
 * one; `fit: "cover"` marks art that fills the window, anything else keeps the
 * centre box. Anything else (null for a theme without a splash, a wrong type,
 * an empty or oversized body) clears it, so the stock splash returns.
 */
function saveThemeSplash(dir, image) {
  const ext = image && SPLASH_EXTENSIONS[image.type];
  const bytes = image && image.bytes;
  const keep = Boolean(ext) && bytes instanceof Uint8Array
    && bytes.length > 0 && bytes.length <= SPLASH_MAX_BYTES;
  const stem = keep && image.fit === "cover" ? "theme-splash-cover" : "theme-splash";
  try {
    const target = keep ? path.join(dir, `${stem}.${ext}`) : "";
    if (keep) {
      fs.writeFileSync(`${target}.tmp`, bytes);
      fs.renameSync(`${target}.tmp`, target);
    }
    for (const file of splashFiles(dir)) {
      if (file !== target) fs.rmSync(file, { force: true });
    }
  } catch {
    // An unwritable userData dir keeps what was stored before; the theme still applies.
  }
}

/** The stored art as a file: URL for loading.html, or "" when there is none. */
function themeSplashUrl(dir) {
  const file = splashFiles(dir).find((candidate) => fs.existsSync(candidate));
  return file ? pathToFileURL(file).href : "";
}

module.exports = { SPLASH_MAX_BYTES, saveThemeSplash, themeSplashUrl };
