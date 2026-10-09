"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");

const {
  BAKED_ALLOWLIST_NAME,
  ALLOWLIST_MAX_BYTES,
  parseAuthServerAllowlist,
  readBakedAuthServerAllowlist,
  applyAuthServerAllowlist,
} = require("../auth-allowlist");

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "kc-auth-allowlist-"));
}

test("parse accepts suffix and exact host patterns and normalizes separators", () => {
  assert.equal(
    parseAuthServerAllowlist(" *.example.com, *.example.dev ,intranet.example.org\n"),
    "*.example.com,*.example.dev,intranet.example.org",
  );
});

test("parse refuses empty, wildcard-only, and malformed entries", () => {
  for (const bad of ["", "   ", "*", "*.", "*.example.com,", "a b.com", "exa mple.com", "*.ex*ample.com", "http://x.com", "x.com;y.com", "-x.com", ".x.com"]) {
    assert.equal(parseAuthServerAllowlist(bad), null, `accepted ${JSON.stringify(bad)}`);
  }
});

test("parse refuses a non-string", () => {
  assert.equal(parseAuthServerAllowlist(undefined), null);
  assert.equal(parseAuthServerAllowlist(42), null);
});

test("read returns null when no allowlist is baked", () => {
  const dir = tmpDir();
  assert.equal(readBakedAuthServerAllowlist({ bakedPath: path.join(dir, BAKED_ALLOWLIST_NAME) }), null);
});

test("read returns the parsed baked allowlist", () => {
  const dir = tmpDir();
  const p = path.join(dir, BAKED_ALLOWLIST_NAME);
  fs.writeFileSync(p, "*.example.com,*.example.dev\n");
  assert.equal(readBakedAuthServerAllowlist({ bakedPath: p }), "*.example.com,*.example.dev");
});

test("read ignores an over-cap, malformed, directory, or symlinked baked file", () => {
  const logs = [];
  const log = (m) => logs.push(m);

  const big = tmpDir();
  fs.writeFileSync(path.join(big, BAKED_ALLOWLIST_NAME), "a".repeat(ALLOWLIST_MAX_BYTES + 1));
  assert.equal(readBakedAuthServerAllowlist({ bakedPath: path.join(big, BAKED_ALLOWLIST_NAME), log }), null);

  const bad = tmpDir();
  fs.writeFileSync(path.join(bad, BAKED_ALLOWLIST_NAME), "*");
  assert.equal(readBakedAuthServerAllowlist({ bakedPath: path.join(bad, BAKED_ALLOWLIST_NAME), log }), null);

  const dirCase = tmpDir();
  fs.mkdirSync(path.join(dirCase, BAKED_ALLOWLIST_NAME));
  assert.equal(readBakedAuthServerAllowlist({ bakedPath: path.join(dirCase, BAKED_ALLOWLIST_NAME), log }), null);

  if (process.platform !== "win32") {
    const sym = tmpDir();
    const target = path.join(sym, "target");
    fs.writeFileSync(target, "*.example.com");
    fs.symlinkSync(target, path.join(sym, BAKED_ALLOWLIST_NAME));
    assert.equal(readBakedAuthServerAllowlist({ bakedPath: path.join(sym, BAKED_ALLOWLIST_NAME), log }), null);
  }
  assert.ok(logs.length >= 3, `expected a log line per ignored file, got ${logs.length}`);
});

test("apply appends Electron's integrated-auth switch with the baked allowlist", () => {
  const dir = tmpDir();
  const p = path.join(dir, BAKED_ALLOWLIST_NAME);
  fs.writeFileSync(p, "*.example.com");
  const calls = [];
  const value = applyAuthServerAllowlist({ appendSwitch: (n, v) => calls.push([n, v]), bakedPath: p });
  assert.equal(value, "*.example.com");
  assert.deepEqual(calls, [["auth-server-whitelist", "*.example.com"]]); // wokeignore:rule=whitelist
});

test("apply appends nothing when no allowlist is baked", () => {
  const calls = [];
  const value = applyAuthServerAllowlist({
    appendSwitch: (n, v) => calls.push([n, v]),
    bakedPath: path.join(tmpDir(), BAKED_ALLOWLIST_NAME),
  });
  assert.equal(value, null);
  assert.deepEqual(calls, []);
});

test("apply never enables credential delegation", () => {
  const dir = tmpDir();
  const p = path.join(dir, BAKED_ALLOWLIST_NAME);
  fs.writeFileSync(p, "*.example.com");
  const names = [];
  applyAuthServerAllowlist({ appendSwitch: (n) => names.push(n), bakedPath: p });
  assert.ok(!names.some((n) => /delegate/.test(n)));
});

test("main.js applies the baked allowlist before app ready", () => {
  const src = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  const applyAt = src.indexOf("applyAuthServerAllowlist({");
  assert.ok(applyAt > 0, "main.js never calls applyAuthServerAllowlist");
  const readyAt = src.search(/^app\.whenReady\(\)/m);
  assert.ok(readyAt > 0 && applyAt < readyAt, "applyAuthServerAllowlist must run before app.whenReady()");
});

test("the baked allowlist is packed into app.asar and staged by build-desktop.sh", () => {
  const pkg = require("../package.json");
  assert.ok(pkg.build.files.includes("auth-allowlist.js"));
  assert.ok(pkg.build.files.includes(BAKED_ALLOWLIST_NAME));
  const { BUILD_TIME_INPUTS } = require("./build-time-inputs");
  assert.ok(BUILD_TIME_INPUTS.has(BAKED_ALLOWLIST_NAME));
  const script = fs.readFileSync(path.join(__dirname, "..", "..", "..", "packaging", "build-desktop.sh"), "utf8");
  assert.match(script, /KIROCREW_AUTH_SERVER_ALLOWLIST/);
  assert.match(script, /\$ELECTRON_DIR\/AUTH-SERVER-ALLOWLIST/);
});
