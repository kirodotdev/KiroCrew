#!/usr/bin/env node
/**
 * Local static update feed for end-to-end auto-update testing.
 *
 * Binds 127.0.0.1 ONLY. Mirrors the production static-feed contract exactly
 * (CloudFront serving files CI wrote), which since the electron-updater
 * migration means TWO documents per channel:
 *
 *   GET /feed/<channel>/latest-mac.yml   -> 200 electron-updater metadata (CURRENT)
 *   GET /feed/<channel>/latest-mac.json  -> 200 legacy hand-rolled feed (BRIDGE)
 *   GET /download                        -> streams the update .zip
 *
 * The yml is what the current client reads. The JSON is the transition bridge
 * for installs fielded BEFORE the migration (see the "Write legacy update feed"
 * step in sign-and-notarize.yml) -- serving both here means this harness can
 * exercise an old-client upgrade path as well as the current one.
 *
 * sha512 MUST be base64, not hex: electron-updater compares base64 and treats a
 * hex digest as a checksum mismatch, which surfaces as a confusing download
 * failure rather than a format error.
 *
 * files[].url is emitted ABSOLUTE, matching production, where the metadata is
 * served from the pointer host (updates.crew.kiro.dev) while the bytes live on
 * the byte host (download.crew.kiro.dev). electron-updater resolves file urls
 * with `new URL(fileUrl, base)`, which ignores the base for absolute urls.
 *
 * Usage:
 *   node local-feed-server.js --port 8799 --zip /path/KiroCrew.zip --version 1.0.1
 *   KIROCREW_UPDATE_FEED=http://127.0.0.1:8799/feed <app binary>
 *
 * The client permits plain http for loopback hosts only (buildFeedBase), so
 * this harness needs no TLS.
 */
const http = require("http");
const fs = require("fs");
const crypto = require("crypto");

function arg(name, def) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : def;
}
const PORT = parseInt(arg("port", "8799"), 10);
const ZIP = arg("zip");
const LATEST = arg("version", "1.0.1");

if (!ZIP || !fs.existsSync(ZIP)) {
  console.error("ERROR: --zip <path> is required and must exist");
  process.exit(1);
}

// Computed once at startup: electron-updater verifies this against the bytes it
// downloaded and refuses to install on a mismatch (fail-closed).
const ZIP_BYTES = fs.readFileSync(ZIP);
const ZIP_SIZE = ZIP_BYTES.length;
const ZIP_SHA512 = crypto.createHash("sha512").update(ZIP_BYTES).digest("base64");
const DOWNLOAD_URL = `http://127.0.0.1:${PORT}/download`;

function macYaml() {
  return [
    `version: ${LATEST}`,
    "files:",
    `  - url: ${DOWNLOAD_URL}`,
    `    sha512: '${ZIP_SHA512}'`,
    `    size: ${ZIP_SIZE}`,
    `path: ${DOWNLOAD_URL}`,
    `sha512: '${ZIP_SHA512}'`,
    `releaseDate: '${new Date().toISOString()}'`,
    "",
  ].join("\n");
}

function legacyJson() {
  return JSON.stringify({
    version: LATEST,
    url: DOWNLOAD_URL,
    dmg: DOWNLOAD_URL,
    name: LATEST,
    pub_date: new Date().toISOString(),
  });
}

const server = http.createServer((req, res) => {
  const u = new URL(req.url, `http://127.0.0.1:${PORT}`);

  if (u.pathname === "/download") {
    res.writeHead(200, { "Content-Type": "application/zip", "Content-Length": ZIP_SIZE });
    res.end(ZIP_BYTES);
    console.log(`[feed] served update zip (${ZIP_SIZE} bytes)`);
    return;
  }

  // electron-updater channel file. Linux builds ask for latest-linux.yml; the
  // same metadata shape serves both, so match on the suffix.
  if (u.pathname.endsWith(".yml")) {
    const body = macYaml();
    res.writeHead(200, { "Content-Type": "text/yaml", "Cache-Control": "no-cache" });
    res.end(body);
    console.log(`[feed] ${u.pathname} -> 200 version=${LATEST} (client compares + verifies sha512)`);
    return;
  }

  if (u.pathname.endsWith("/latest-mac.json")) {
    res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-cache" });
    res.end(legacyJson());
    console.log(`[feed] ${u.pathname} -> 200 version=${LATEST} (LEGACY bridge — pre-migration clients)`);
    return;
  }

  res.writeHead(404);
  res.end();
  console.log(`[feed] ${u.pathname} -> 404 (expected /feed/<channel>/latest-mac.yml, .json, or /download)`);
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`[feed] listening on http://127.0.0.1:${PORT}`);
  console.log(`[feed] serving version=${LATEST} from ${ZIP}`);
  console.log(`[feed]   size=${ZIP_SIZE} sha512(base64)=${ZIP_SHA512.slice(0, 24)}…`);
  console.log(`[feed] point the app at it: KIROCREW_UPDATE_FEED=http://127.0.0.1:${PORT}/feed`);
});
