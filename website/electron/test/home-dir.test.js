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
const MARKER = path.join(CANONICAL, ".data-home-ready");

describe("resolveHome (shared-fixture parity cases)", () => {
  assert.ok(CASES.length >= 7, "fixture must load");
  for (const c of CASES) {
    it(c.name, () => {
      const env = c.env_override ? { KIROCREW_HOME: OVERRIDE } : {};
      const existing = [];
      if (c.legacy) existing.push(LEGACY);
      if (c.canonical) existing.push(CANONICAL);
      // The marker lives inside the canonical home; resolveHome is
      // marker-authoritative, so the fake fs must model it too.
      if (c.marker) existing.push(MARKER);
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

  it("rejects an invalid override (root / system dir) and falls through -- parity with paths.py", () => {
    // Backend _valid_override_home refuses "/" and /usr,/System,/etc; Electron
    // must agree or the two read different config/secret homes (GPT 5.6 MEDIUM).
    const fakeFs = { existsSync: () => false };
    for (const bad of ["/", "/etc", "/usr", "/System"]) {
      assert.equal(
        resolveHome({ env: { KIROCREW_HOME: bad }, os: fakeOs, path, fs: fakeFs }),
        CANONICAL,
        `override ${bad} should be rejected`,
      );
    }
  });

  it("expands a leading '~' in the override to an absolute path -- parity with Python expanduser()", () => {
    // Python _valid_override_home returns Path(override).expanduser().resolve();
    // Electron must NOT read a literal "~/foo" or the two diverge (GPT 5.6 MEDIUM).
    const fakeFs = { existsSync: () => false };
    assert.equal(
      resolveHome({ env: { KIROCREW_HOME: "~/foo" }, os: fakeOs, path, fs: fakeFs }),
      path.join(HOME, "foo"),
    );
    assert.equal(
      resolveHome({ env: { KIROCREW_HOME: "~" }, os: fakeOs, path, fs: fakeFs }),
      HOME,
    );
    // secretCandidates uses the same expanded, absolute override.
    assert.deepEqual(secretCandidates({ env: { KIROCREW_HOME: "~/foo" }, os: fakeOs, path }), [
      path.join(HOME, "foo", ".local_secret"),
    ]);
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

  it("ignores an invalid (root) override and uses canonical+legacy -- parity", () => {
    assert.deepEqual(secretCandidates({ env: { KIROCREW_HOME: "/" }, os: fakeOs, path }), [
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
