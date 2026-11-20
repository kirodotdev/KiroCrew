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
 *
 * Block (differential) downloads: pass --blockmap <new.blockmap> and, for the
 * installed version, --old-version <v> --old-blockmap <old.blockmap>. The zip is
 * then served at /desktop/<version>/KiroCrew.zip, the production key shape, so
 * the client derives the old blockmap's URL by swapping the version string the
 * way it does against the CDN. The zip answers single and multi-range requests
 * (206, multipart/byteranges), and GET /stats reports how many zip bytes went out.
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
const BLOCKMAP = arg("blockmap");
const OLD_VERSION = arg("old-version");
const OLD_BLOCKMAP = arg("old-blockmap");

if (!ZIP || !fs.existsSync(ZIP)) {
  console.error("ERROR: --zip <path> is required and must exist");
  process.exit(1);
}

// Computed once at startup: electron-updater verifies this against the bytes it
// downloaded and refuses to install on a mismatch (fail-closed).
//
// The ZIP is hashed and served as a stream, never held in memory: the OTA lane
// serves an update padded past 1 GiB, and fs.readFileSync refuses any file over
// 2 GiB outright.
const ZIP_SIZE = fs.statSync(ZIP).size;
let ZIP_SHA512 = "";
const ZIP_PATH = BLOCKMAP ? `/desktop/${LATEST}/KiroCrew.zip` : "/download";
const DOWNLOAD_URL = `http://127.0.0.1:${PORT}${ZIP_PATH}`;
// The blockmaps by path, served only when given; anything else is a 404, which
// the client answers with a full download.
const BLOCKMAPS = new Map();
if (BLOCKMAP) BLOCKMAPS.set(`${ZIP_PATH}.blockmap`, BLOCKMAP);
if (OLD_VERSION && OLD_BLOCKMAP) BLOCKMAPS.set(`/desktop/${OLD_VERSION}/KiroCrew.zip.blockmap`, OLD_BLOCKMAP);
let zipBytesServed = 0;

/** `bytes=a-b, c-d` -> [[a, b], ...] (inclusive), or null when it is not one. */
function parseRanges(header) {
  const m = /^bytes=(.+)$/.exec(header || "");
  if (!m) return null;
  const ranges = [];
  for (const part of m[1].split(",")) {
    const r = /^\s*(\d+)-(\d+)\s*$/.exec(part);
    if (!r) return null;
    const start = Number(r[1]);
    const end = Math.min(Number(r[2]), ZIP_SIZE - 1);
    if (start > end) return null;
    ranges.push([start, end]);
  }
  return ranges.length ? ranges : null;
}

/** Stream [start, end] of the zip into res without ending it. */
function pipeRange(res, start, end) {
  return new Promise((resolve, reject) => {
    const body = fs.createReadStream(ZIP, { start, end });
    body.on("data", (chunk) => { zipBytesServed += chunk.length; });
    body.on("error", reject);
    body.on("end", resolve);
    body.pipe(res, { end: false });
  });
}

async function serveRanges(res, ranges) {
  if (ranges.length === 1) {
    const [start, end] = ranges[0];
    res.writeHead(206, {
      "Content-Type": "application/zip",
      "Content-Range": `bytes ${start}-${end}/${ZIP_SIZE}`,
      "Content-Length": end - start + 1,
      "Accept-Ranges": "bytes",
    });
    await pipeRange(res, start, end);
    res.end();
    return;
  }
  const boundary = crypto.randomBytes(12).toString("hex");
  res.writeHead(206, {
    "Content-Type": `multipart/byteranges; boundary=${boundary}`,
    "Accept-Ranges": "bytes",
  });
  for (let i = 0; i < ranges.length; i++) {
    const [start, end] = ranges[i];
    res.write(
      `${i ? "\r\n" : ""}--${boundary}\r\nContent-Type: application/zip\r\n` +
        `Content-Range: bytes ${start}-${end}/${ZIP_SIZE}\r\n\r\n`,
    );
    await pipeRange(res, start, end);
  }
  res.end(`\r\n--${boundary}--\r\n`);
}

function hashZip() {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash("sha512");
    fs.createReadStream(ZIP)
      .on("data", (chunk) => hash.update(chunk))
      .on("error", reject)
      .on("end", () => resolve(hash.digest("base64")));
  });
}

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

  if (u.pathname === "/stats") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ zip_size: ZIP_SIZE, zip_bytes_served: zipBytesServed }));
    return;
  }

  if (BLOCKMAPS.has(u.pathname)) {
    const file = BLOCKMAPS.get(u.pathname);
    res.writeHead(200, { "Content-Type": "application/octet-stream", "Content-Length": fs.statSync(file).size });
    fs.createReadStream(file).pipe(res);
    console.log(`[feed] ${u.pathname} -> 200 blockmap`);
    return;
  }

  if (u.pathname === ZIP_PATH && req.headers.range) {
    const ranges = parseRanges(req.headers.range);
    if (!ranges) {
      res.writeHead(416, { "Content-Range": `bytes */${ZIP_SIZE}` });
      res.end();
      return;
    }
    serveRanges(res, ranges).then(
      () => console.log(`[feed] served ${ranges.length} range(s); zip bytes so far ${zipBytesServed}`),
      (err) => { console.error(`[feed] range read failed: ${err.message}`); res.destroy(err); },
    );
    return;
  }

  if (u.pathname === ZIP_PATH) {
    res.writeHead(200, { "Content-Type": "application/zip", "Content-Length": ZIP_SIZE, "Accept-Ranges": "bytes" });
    const body = fs.createReadStream(ZIP);
    body.on("data", (chunk) => { zipBytesServed += chunk.length; });
    body.on("error", (err) => {
      console.error(`[feed] update zip read failed: ${err.message}`);
      res.destroy(err);
    });
    res.on("finish", () => console.log(`[feed] served update zip (${ZIP_SIZE} bytes)`));
    body.pipe(res);
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

// Listen only once the digest is known, so no client can read a yml with an
// empty sha512.
hashZip().then(
  (digest) => {
    ZIP_SHA512 = digest;
    server.listen(PORT, "127.0.0.1", () => {
      console.log(`[feed] listening on http://127.0.0.1:${PORT}`);
      console.log(`[feed] serving version=${LATEST} from ${ZIP}`);
      console.log(`[feed]   size=${ZIP_SIZE} sha512(base64)=${ZIP_SHA512.slice(0, 24)}…`);
      console.log(`[feed] point the app at it: KIROCREW_UPDATE_FEED=http://127.0.0.1:${PORT}/feed`);
    });
  },
  (err) => {
    console.error(`ERROR: could not hash ${ZIP}: ${err.message}`);
    process.exit(1);
  },
);
