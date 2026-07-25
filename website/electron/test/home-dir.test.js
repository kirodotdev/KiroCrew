const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { resolveHome, secretCandidates, canonicalHome, legacyHome } = require("../home-dir");

const HOME = "/mock/home";
const fakeOs = { homedir: () => HOME };
const CANONICAL = path.join(HOME, ".kiro", "crew");
const LEGACY = path.join(HOME, ".kirocrew");
const OVERRIDE = "/custom/home";

// The shared cross-language contract: the same cases drive
// test/test_home_resolution_parity.py, which runs the REAL backend resolver
// (config/paths.py) and asserts post-migration content equals what
// resolveHome() reads pre-spawn. Edit semantics there, and this suite fails
// until home-dir.js follows -- and vice versa.
const FIXTURE = path.join(__dirname, "..", "..", "..", "test", "fixtures", "home-resolution-cases.json");
const CASES = JSON.parse(fs.readFileSync(FIXTURE, "utf8")).cases;

const EXPECTED_PATHS = { override: OVERRIDE, legacy: LEGACY, canonical: CANONICAL };

describe("resolveHome (shared-fixture parity cases)", () => {
  assert.ok(CASES.length >= 7, "fixture must load");
  for (const c of CASES) {
    it(c.name, () => {
      const env = c.env_override ? { KIROCREW_HOME: OVERRIDE } : {};
      const existing = [];
      if (c.legacy) existing.push(LEGACY);
      if (c.canonical) existing.push(CANONICAL);
      const fakeFs = { existsSync: (p) => existing.includes(p) };
      assert.equal(
        resolveHome({ env, os: fakeOs, path, fs: fakeFs }),
        EXPECTED_PATHS[c.expected_read_home],
      );
    });
  }

  it("treats existsSync errors as absent (resolves canonical)", () => {
    const fakeFs = { existsSync: () => { throw new Error("EACCES"); } };
    assert.equal(resolveHome({ env: {}, os: fakeOs, path, fs: fakeFs }), CANONICAL);
  });
});

describe("secretCandidates (post-spawn, call-time resolution)", () => {
  it("env override is authoritative and sole", () => {
    const env = { KIROCREW_HOME: OVERRIDE };
    assert.deepEqual(secretCandidates({ env, os: fakeOs, path }), [
      path.join(OVERRIDE, ".local_secret"),
    ]);
  });

  it("orders canonical before legacy -- migration has run by fetch time", () => {
    // Deliberately the REVERSE of resolveHome's both-exist answer: pre-spawn
    // the legacy config content wins (it is about to be force-copied over
    // canonical), but post-spawn the migrated secret lives in canonical;
    // legacy remains only as the backend's migration-failure pin.
    assert.deepEqual(secretCandidates({ env: {}, os: fakeOs, path }), [
      path.join(CANONICAL, ".local_secret"),
      path.join(LEGACY, ".local_secret"),
    ]);
  });
});

describe("path shape helpers", () => {
  it("canonical nests under ~/.kiro, legacy is the retired top-level dir", () => {
    assert.equal(canonicalHome(fakeOs, path), CANONICAL);
    assert.equal(legacyHome(fakeOs, path), LEGACY);
  });
});
