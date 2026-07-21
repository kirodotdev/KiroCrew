#!/usr/bin/env node
/**
 * Local static update feed for Stage 3 auto-update testing.
 *
 * Binds 127.0.0.1 ONLY. Mirrors the production static-feed contract
 * (CloudFront serving files CI wrote): the feed is a plain JSON document
 * and the APP does the version compare client-side, engaging Squirrel only
 * when the feed version differs from the running app. This server never
 * returns 204 -- that behavior lives in the app now.
 *
 *   GET /feed/<channel>/latest-mac.json -> 200 {version,url,name,pub_date}
 *   GET /download                       -> streams the update .zip
 *
 * Usage:
 *   node local-feed-server.js --port 8799 --zip /path/KiroCrew.zip --version 0.1.0-nightly.20260722000000
 *   KIROCREW_UPDATE_FEED=http://127.0.0.1:8799/feed <app binary>
 *
 * The app permits plain http for loopback hosts only (fetchFeedHttps), so
 * this harness needs no TLS.
 */
const http = require("http");
const fs = require("fs");

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

const server = http.createServer((req, res) => {
  const u = new URL(req.url, `http://127.0.0.1:${PORT}`);

  if (u.pathname === "/download") {
    const stat = fs.statSync(ZIP);
    res.writeHead(200, { "Content-Type": "application/zip", "Content-Length": stat.size });
    fs.createReadStream(ZIP).pipe(res);
    console.log(`[feed] served update zip (${stat.size} bytes)`);
    return;
  }

  if (u.pathname.endsWith("/latest-mac.json")) {
    const body = JSON.stringify({
      version: LATEST,
      url: `http://127.0.0.1:${PORT}/download`,
      name: LATEST,
      pub_date: new Date().toISOString(),
    });
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(body);
    console.log(`[feed] ${u.pathname} -> 200 latest=${LATEST} (app compares client-side)`);
    return;
  }

  res.writeHead(404);
  res.end();
  console.log(`[feed] ${u.pathname} -> 404 (expected /feed/<channel>/latest-mac.json or /download)`);
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`[feed] listening on http://127.0.0.1:${PORT}`);
  console.log(`[feed] serving latest=${LATEST} from ${ZIP}`);
  console.log(`[feed] point the app at it: KIROCREW_UPDATE_FEED=http://127.0.0.1:${PORT}/feed`);
});
