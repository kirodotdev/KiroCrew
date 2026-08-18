const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { resolveHome, secretCandidates, canonicalHome, legacyHome } = require("../home-dir");

// Absolute paths are built with path.resolve, not written as POSIX literals:
// resolveHome() normalizes every override through path.resolve(), so on Windows
// a literal "/custom/home" comes back as "C:\custom\home" and a hardcoded
// expectation compares a normalized path against an unnormalized one. Deriving
// both sides the same way keeps this suite about the RESOLUTION RULES, which are
// platform-independent, rather than about path syntax, which is not.
const HOME = path.resolve(path.sep, "mock", "home");
const fakeOs = { homedir: () => HOME };
const CANONICAL = path.join(HOME, ".kiro", "crew");
const LEGACY = path.join(HOME, ".kirocrew");
const OVERRIDE = path.resolve(path.sep, "custom", "home");

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

  it("rejects a filesystem root override and falls through -- parity with paths.py", () => {
    // Backend _valid_override_home refuses a root via `p == p.parent`; Electron
    // must agree or the two read different config/secret homes. The root is
    // spelled per-platform ("/" vs "C:\") because that is what the rule is
    // about -- a path whose parent is itself -- and path.parse().root is the
    // only portable way to name the root of the volume this test runs on.
    const fakeFs = { existsSync: () => false };
    const root = path.parse(path.resolve(path.sep)).root;
    assert.equal(
      resolveHome({ env: { KIROCREW_HOME: root }, os: fakeOs, path, fs: fakeFs }),
      CANONICAL,
      `override ${root} should be rejected`,
    );
  });

  // The POSIX system-directory guard. Scoped to POSIX deliberately rather than
  // made cross-platform: the backend's list (_UNSAFE_HOME_PREFIXES) is literally
  // /usr, /System, /etc, and on Windows those are ordinary relative-looking
  // names that path.resolve() rewrites onto the current drive -- so asserting
  // them here would test Windows path syntax, not the shared rule. Windows'
  // equivalent protection is the root check above.
  it("rejects POSIX system-dir overrides and falls through -- parity with paths.py", { skip: process.platform === "win32" ? "POSIX-only rule (backend guards /usr, /System, /etc)" : false }, () => {
    const fakeFs = { existsSync: () => false };
    for (const bad of ["/etc", "/usr", "/System"]) {
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
