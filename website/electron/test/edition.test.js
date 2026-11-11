"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  BAKED_EDITION_NAME,
  readBakedEdition,
} = require("../edition");
const {
  DEFAULT_GATEWAY_WAIT_MS,
  LOCAL_GATEWAY_WAIT_MS,
  LTS_PRE_BIND_CHECKS_MS,
  LTS_PRE_BIND_HEADROOM_MS,
  gatewayWaitTimeoutMs,
} = require("../gateway-wait");

function bakedPath(contents) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-edition-"));
  const p = path.join(dir, BAKED_EDITION_NAME);
  if (contents !== undefined) fs.writeFileSync(p, contents);
  return p;
}

test("no baked file reads as oss", () => {
  assert.equal(readBakedEdition({ bakedPath: bakedPath() }), "oss");
});

test("the file build-desktop.sh writes reads as lts", () => {
  assert.equal(readBakedEdition({ bakedPath: bakedPath("lts\n") }), "lts");
});

test("anything else reads as oss", () => {
  for (const text of ["", "oss\n", "Enterprise", " enterprise", "enterprise\n\n", "x".repeat(100)]) {
    assert.equal(readBakedEdition({ bakedPath: bakedPath(text) }), "oss", JSON.stringify(text));
  }
  const dir = bakedPath();
  fs.mkdirSync(dir);
  assert.equal(readBakedEdition({ bakedPath: dir }), "oss");
});

test("the source tree carries no baked edition", () => {
  assert.equal(fs.existsSync(path.join(__dirname, "..", BAKED_EDITION_NAME)), false);
  assert.equal(readBakedEdition(), "oss");
});

test("an oss local gateway gets the cold-start budget, a connection tab the ordinary one", () => {
  for (const watchSpawn of [false, true]) {
    const expected = watchSpawn ? LOCAL_GATEWAY_WAIT_MS : DEFAULT_GATEWAY_WAIT_MS;
    assert.equal(gatewayWaitTimeoutMs({ watchSpawn }), expected);
    assert.equal(gatewayWaitTimeoutMs({ watchSpawn, edition: "oss" }), expected);
  }
  assert.equal(DEFAULT_GATEWAY_WAIT_MS, 30_000);
  assert.equal(LOCAL_GATEWAY_WAIT_MS, 120_000);
});

test("an LTS local gateway gets the pre-bind checks plus headroom", () => {
  const extra = LTS_PRE_BIND_CHECKS_MS + LTS_PRE_BIND_HEADROOM_MS;
  // kiro-cli --version 10s + whoami 30s + policy fetch 10s.
  assert.equal(LTS_PRE_BIND_CHECKS_MS, 50_000);
  assert.ok(LTS_PRE_BIND_HEADROOM_MS > 0);
  assert.equal(
    gatewayWaitTimeoutMs({ watchSpawn: true, edition: "lts" }),
    LOCAL_GATEWAY_WAIT_MS + extra,
  );
});

test("an LTS connection tab keeps the ordinary deadline", () => {
  assert.equal(
    gatewayWaitTimeoutMs({ watchSpawn: false, edition: "lts" }),
    DEFAULT_GATEWAY_WAIT_MS,
  );
});

test("both wait callers pass the baked edition", () => {
  for (const rel of ["gateway-supervisor.js", path.join("runtime", "gateway", "successor-handoff.js")]) {
    const src = fs.readFileSync(path.join(__dirname, "..", rel), "utf8");
    assert.match(src, /require\("(\.\/|\.\.\/\.\.\/)edition"\)/, rel);
    assert.match(src, /edition: (BAKED_EDITION|readBakedEdition\(\)),/, rel);
  }
});

test("the baked edition is packed into app.asar and staged by build-desktop.sh", () => {
  const pkg = require("../package.json");
  assert.ok(pkg.build.files.includes("edition.js"));
  assert.ok(pkg.build.files.includes(BAKED_EDITION_NAME));
  const { BUILD_TIME_INPUTS } = require("./build-time-inputs");
  assert.ok(BUILD_TIME_INPUTS.has(BAKED_EDITION_NAME));
  const script = fs.readFileSync(
    path.join(__dirname, "..", "..", "..", "packaging", "build-desktop.sh"),
    "utf8",
  );
  // Staged only for LTS, from the same build stamp the backend uses.
  assert.match(script, /case "\$\{KIROCREW_BUILD_EDITION:-oss\}" in\n {2}oss\) ;;\n {2}lts\)/);
  assert.match(script, /printf 'lts\\n' > "\$ELECTRON_DIR\/EDITION"/);
  assert.match(script, /^rm -f .*"\$ELECTRON_DIR\/EDITION"$/m);
});
