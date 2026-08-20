const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const {
  REQUIRED_STDLIB_PARTS,
  SPAWN_MARKER,
  currentAttemptLog,
  findMissingBundleParts,
  describeIncompleteBundle,
  shouldReclassifyAsInstalling,
} = require("../bundle-integrity");

// resolveStdlibDir / hasBundledInterpreter / isIncompleteBundleCrash are internal
// steps of the exported functions, so they are exercised through them:
//   - layout + interpreter detection -> findMissingBundleParts
//   - crash matching -> shouldReclassifyAsInstalling (failedToStart + a log tail)
const missingFor = (fs, root) => findMissingBundleParts(fs, path, root);
// The matcher reads only the CURRENT launch attempt, so a tail must carry the
// spawn marker; `bundled` is what main.js observed about the binary it chose.
const crashIsInstalling = (childOutput) =>
  shouldReclassifyAsInstalling({
    failedToStart: true,
    failure: { code: 1, bundled: true },
    logTail: `${SPAWN_MARKER}
${childOutput}`,
    bundled: true,
  });

const ROOT = "/mock/backend-dist/kirocrew-backend";
const WIN_PY = path.join(ROOT, "python.exe");

// A fully-extracted package: the dir AND the __init__.py that makes it
// importable. The gate probes the latter, since an extractor creates the
// directory before filling it.
const pkgPaths = (stdlib, rel) => [
  path.join(stdlib, ...rel.split("/")),
  path.join(stdlib, ...rel.split("/"), "__init__.py"),
];
const allPkgPaths = (stdlib, parts = REQUIRED_STDLIB_PARTS) =>
  parts.flatMap((rel) => pkgPaths(stdlib, rel));

// Fake fs over an explicit set of existing paths. Only existsSync + readdirSync
// are used by the module, so the fakes stay this small.
function fakeFs(existing, dirs = {}) {
  const set = new Set(existing);
  return {
    existsSync: (p) => set.has(p),
    readdirSync: (p) => {
      if (!dirs[p]) throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
      return dirs[p];
    },
  };
}

// Fake fs that resolves paths case-INSENSITIVELY, modelling macOS APFS (and
// NTFS). Required to cover the layout probes: a case-sensitive Set cannot
// express "Lib and lib are the same directory", which is exactly the condition
// that made a Lib-first probe resolve a POSIX tree to the wrong directory.
function fakeFsCaseInsensitive(existing, dirs = {}) {
  const lower = new Set(existing.map((p) => p.toLowerCase()));
  const lowerDirs = new Map(Object.entries(dirs).map(([k, v]) => [k.toLowerCase(), v]));
  return {
    existsSync: (p) => lower.has(String(p).toLowerCase()),
    readdirSync: (p) => {
      const hit = lowerDirs.get(String(p).toLowerCase());
      if (!hit) throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
      return hit;
    },
  };
}

/** Every path a COMPLETE Windows-layout bundle would carry. */
function completeWindows(root = ROOT) {
  const lib = path.join(root, "Lib");
  return [path.join(root, "python.exe"), lib, ...allPkgPaths(lib)];
}

// Interpreter detection, observed through the public function: a tree WITH an
// interpreter but no stdlib is judged (reports the stdlib root); a tree without
// one is not this gate's business and must stay silent.
describe("interpreter detection", () => {
  it("judges a Windows tree (python.exe at the root)", () => {
    assert.deepStrictEqual(missingFor(fakeFs([WIN_PY]), ROOT), ["Lib"]);
  });

  it("judges a POSIX tree (bin/python3.12)", () => {
    const bin = path.join(ROOT, "bin");
    const fs = fakeFs([bin], { [bin]: ["python3.12", "kirocrew"] });
    assert.deepStrictEqual(missingFor(fs, ROOT), ["Lib"]);
  });

  it("stays silent on the legacy flat layout (frozen exe, no interpreter tree)", () => {
    const flat = path.join(ROOT, "kirocrew-backend");
    assert.deepStrictEqual(missingFor(fakeFs([flat]), ROOT), []);
  });

  it("stays silent on a bin/ dir holding no python3 binary", () => {
    const bin = path.join(ROOT, "bin");
    const fs = fakeFs([bin], { [bin]: ["kirocrew"] });
    assert.deepStrictEqual(missingFor(fs, ROOT), []);
  });
});

// Layout resolution, observed through the public function. A tree whose stdlib
// root is found but empty reports every part; one whose root cannot be found at
// all reports ["Lib"]. That difference is what makes these assertions meaningful.
describe("stdlib layout resolution", () => {
  const ALL = REQUIRED_STDLIB_PARTS.length;

  it("finds the Windows layout (Lib/)", () => {
    const fs = fakeFs([WIN_PY, path.join(ROOT, "Lib")]);
    assert.equal(missingFor(fs, ROOT).length, ALL, "found Lib/, so every part is judged missing");
  });

  it("finds the POSIX layout by scanning for a python3.* dir", () => {
    const lib = path.join(ROOT, "lib");
    const py = path.join(lib, "python3.12");
    const bin = path.join(ROOT, "bin");
    const fs = fakeFs([bin, lib, py], { [lib]: ["python3.12", "pkgconfig"], [bin]: ["python3.12"] });
    assert.equal(missingFor(fs, ROOT).length, ALL);
  });

  it("reports the stdlib root itself when no layout is present", () => {
    assert.deepStrictEqual(missingFor(fakeFs([WIN_PY]), ROOT), ["Lib"]);
  });

  it("reports the stdlib root when lib/ holds no python3.* dir", () => {
    const lib = path.join(ROOT, "lib");
    const fs = fakeFs([WIN_PY, lib], { [lib]: ["pkgconfig"] });
    assert.deepStrictEqual(missingFor(fs, ROOT), ["Lib"]);
  });

  // On a case-insensitive volume (macOS APFS by default) `Lib` resolves to the
  // POSIX tree's real `lib`, whose children are `python3.<minor>/` and
  // `pkgconfig/` -- NOT stdlib packages. Resolving there would report every part
  // missing and refuse a valid bundle, so the POSIX layout must win.
  it("reports a complete POSIX bundle as complete on a case-insensitive volume", () => {
    const lib = path.join(ROOT, "lib");
    const py = path.join(lib, "python3.12");
    const bin = path.join(ROOT, "bin");
    const fs = fakeFsCaseInsensitive(
      [bin, lib, py, ...allPkgPaths(py)],
      { [lib]: ["python3.12", "pkgconfig"], [bin]: ["python3.12", "kirocrew"] }
    );
    assert.deepStrictEqual(missingFor(fs, ROOT), []);
  });

  // The mirror case: on Windows `lib` case-matches the real `Lib`, which holds
  // stdlib packages and no python3.<minor> child, so the POSIX probe must decline
  // and fall through to Lib/.
  it("reports a complete Windows bundle as complete on a case-insensitive volume", () => {
    const lib = path.join(ROOT, "Lib");
    const fs = fakeFsCaseInsensitive(
      [WIN_PY, lib, ...allPkgPaths(lib)],
      { [lib]: REQUIRED_STDLIB_PARTS.slice() }
    );
    assert.deepStrictEqual(missingFor(fs, ROOT), []);
  });
});

describe("findMissingBundleParts", () => {
  it("reports nothing for a fully extracted Windows bundle", () => {
    const fs = fakeFs(completeWindows());
    assert.deepStrictEqual(findMissingBundleParts(fs, path, ROOT), []);
  });

  it("reports nothing for a fully extracted POSIX bundle", () => {
    const lib = path.join(ROOT, "lib");
    const py = path.join(lib, "python3.12");
    const bin = path.join(ROOT, "bin");
    const existing = [bin, lib, py, ...allPkgPaths(py)];
    const fs = fakeFs(existing, { [lib]: ["python3.12"], [bin]: ["python3.12", "kirocrew"] });
    assert.deepStrictEqual(findMissingBundleParts(fs, path, ROOT), []);
  });

  // The gate must stay silent on trees it does not model, or it would refuse a
  // launch that would actually have worked.
  it("reports nothing for the legacy flat layout (no interpreter tree to check)", () => {
    const flat = path.join(ROOT, "kirocrew-backend");
    assert.deepStrictEqual(findMissingBundleParts(fakeFs([flat]), path, ROOT), []);
  });

  // The regression this module exists for: an install/update interrupted
  // mid-extraction leaves the stdlib populated only up to some alphabetical
  // point. `urllib` sorts late, so it is typically absent while `http`/`json`
  // are already present -- and `pathlib.py` (a top-level FILE, extracted with
  // the early batch) imports fine and then dies on `from urllib.parse import`.
  it("reports the late-alphabet packages a half-extracted bundle is missing", () => {
    const lib = path.join(ROOT, "Lib");
    const present = REQUIRED_STDLIB_PARTS.filter((rel) => rel < "q");
    const fs = fakeFs([WIN_PY, lib, ...allPkgPaths(lib, present)]);
    const missing = findMissingBundleParts(fs, path, ROOT);
    assert.ok(missing.includes("urllib"), `expected urllib among ${missing.join(", ")}`);
    assert.ok(!missing.includes("json"), "json was extracted and must not be reported");
  });

  // Extraction proceeds roughly in directory order, so the tail is where a
  // racing launch most often lands. A bundle that got as far as Lib/xml is still
  // missing zipfile/zoneinfo, both module-scope imports on the gateway's chain --
  // passing it would let the spawn die on the same raw ModuleNotFoundError.
  it("catches a bundle whose extraction stopped just past xml", () => {
    const lib = path.join(ROOT, "Lib");
    const present = REQUIRED_STDLIB_PARTS.filter((rel) => rel <= "xml");
    const fs = fakeFs([WIN_PY, lib, ...allPkgPaths(lib, present)]);
    const missing = findMissingBundleParts(fs, path, ROOT);
    assert.ok(missing.includes("zoneinfo"), `expected zoneinfo among ${missing.join(", ")}`);
    assert.ok(missing.includes("zipfile"), `expected zipfile among ${missing.join(", ")}`);
    assert.ok(!missing.includes("urllib"), "urllib was extracted and must not be reported");
  });

  // An extractor creates a directory before writing its contents, so the final
  // package is routinely present-but-empty mid-extraction. A directory-existence
  // check would pass that bundle and let the spawn die on the very import the
  // gate exists to prevent, so the probe must look for the __init__.py.
  it("catches a package whose directory exists but is still empty", () => {
    const lib = path.join(ROOT, "Lib");
    const others = REQUIRED_STDLIB_PARTS.filter((rel) => rel !== "zoneinfo");
    const fs = fakeFs([
      WIN_PY,
      lib,
      ...allPkgPaths(lib, others),
      path.join(lib, "zoneinfo"), // dir created, __init__.py not yet written
    ]);
    assert.deepStrictEqual(findMissingBundleParts(fs, path, ROOT), ["zoneinfo"]);
  });

  it("reports the stdlib root itself when extraction has not reached it", () => {
    assert.deepStrictEqual(findMissingBundleParts(fakeFs([WIN_PY]), path, ROOT), ["Lib"]);
  });

  it("reports a single missing package by name", () => {
    const lib = path.join(ROOT, "Lib");
    const urllibPaths = new Set(pkgPaths(lib, "urllib"));
    const existing = completeWindows().filter((p) => !urllibPaths.has(p));
    assert.deepStrictEqual(findMissingBundleParts(fakeFs(existing), path, ROOT), ["urllib"]);
  });

  it("stays silent on an empty/missing backend root rather than throwing", () => {
    assert.deepStrictEqual(findMissingBundleParts(fakeFs([]), path, ""), []);
  });
});

describe("crash matching (via shouldReclassifyAsInstalling)", () => {
  // The exact traceback this whole change exists to explain.
  const REAL_LOG = [
    "Traceback (most recent call last):",
    '  File "...\\Lib\\site-packages\\kiro_crew\\platform_compat.py", line 29, in <module>',
    "    from pathlib import Path",
    '  File "...\\Lib\\pathlib.py", line 20, in <module>',
    "    from urllib.parse import quote_from_bytes as urlquote_from_bytes",
    "ModuleNotFoundError: No module named 'urllib'",
  ].join("\n");

  it("recognizes the reported urllib crash", () => {
    assert.equal(crashIsInstalling(REAL_LOG), true);
  });

  // The gap GPT identified: within a package, __init__.py is written before its
  // siblings, so the pre-spawn probe can pass while `import zoneinfo` still
  // fails. No filesystem probe closes that; the traceback does.
  it("recognizes a crash on a package whose __init__.py had already landed", () => {
    assert.equal(
      crashIsInstalling("ModuleNotFoundError: No module named 'zoneinfo'"),
      true
    );
  });

  it("accepts double-quoted module names", () => {
    assert.equal(crashIsInstalling('ModuleNotFoundError: No module named "encodings"'), true);
  });

  // Must NOT excuse a genuine packaging defect as "still installing", or a real
  // missing dependency ships looking like a transient condition.
  it("does not excuse a missing third-party dependency", () => {
    assert.equal(crashIsInstalling("ModuleNotFoundError: No module named 'aiohttp'"), false);
  });

  it("does not excuse a missing first-party module", () => {
    assert.equal(crashIsInstalling("ModuleNotFoundError: No module named 'kiro_crew'"), false);
  });

  // A dotted stdlib name means the package landed but a submodule had not —
  // still the extraction race, so it is judged by its top-level package.
  it("recognizes a missing stdlib SUBMODULE", () => {
    assert.equal(
      crashIsInstalling("ModuleNotFoundError: No module named 'urllib.parse'"),
      true
    );
  });

  it("still refuses a dotted NON-stdlib name", () => {
    assert.equal(
      crashIsInstalling("ModuleNotFoundError: No module named 'kiro_crew.acp'"),
      false
    );
  });

  // The form a half-written package actually raises. Verified against the shipped
  // interpreter: copying only zoneinfo/__init__.py and importing ZoneInfo gives
  // this, NOT ModuleNotFoundError — so matching only the latter would leave the
  // one case the pre-spawn probe cannot see uncovered.
  it("recognizes a partially initialized stdlib package", () => {
    assert.equal(
      crashIsInstalling(
        "ImportError: cannot import name '_tzpath' from partially initialized module "
        + "'zoneinfo' (most likely due to a circular import) (C:\\...\\zoneinfo\\__init__.py)"
      ),
      true
    );
  });

  it("does not excuse a partially initialized NON-stdlib package", () => {
    assert.equal(
      crashIsInstalling(
        "ImportError: cannot import name 'x' from partially initialized module 'kiro_crew'"
      ),
      false
    );
  });

  it("ignores unrelated failures and empty logs", () => {
    assert.equal(crashIsInstalling("OSError: [Errno 98] address already in use"), false);
    assert.equal(crashIsInstalling(""), false);
    assert.equal(crashIsInstalling(undefined), false);
  });
});

describe("shouldReclassifyAsInstalling", () => {
  const STDLIB_CRASH = `${SPAWN_MARKER}
ModuleNotFoundError: No module named 'urllib'`;
  const base = {
    failedToStart: true,
    failure: { code: 1, bundled: true },
    logTail: STDLIB_CRASH,
    bundled: true,
  };

  it("relabels a missing-stdlib crash", () => {
    assert.equal(shouldReclassifyAsInstalling(base), true);
  });

  // The launch log is append-only across launches, so a stdlib traceback from an
  // EARLIER run must not relabel today's bound-port exit: the holder is still
  // there, so Retry alone loops and the force-stop path would be hidden.
  it("defers to a bound port, which needs force-stop rather than a bare retry", () => {
    assert.equal(
      shouldReclassifyAsInstalling({
        ...base,
        logTail: `${STDLIB_CRASH}\nOSError: [Errno 98] address already in use`,
        portInUseInLog: true,
      }),
      false
    );
  });

  // GPT advisory: isPortInUse scanned the WHOLE tail, so a bound-port line left by
  // an earlier launch suppressed a genuine current stdlib crash. Both signals must
  // come from the same attempt slice.
  it("is not suppressed by a bound-port line from a PREVIOUS attempt", () => {
    const raw = [
      SPAWN_MARKER,
      "OSError: [Errno 98] address already in use",
      "gateway child exited code=1 signal=null",
      SPAWN_MARKER,
      "ModuleNotFoundError: No module named 'urllib'",
    ].join("\n");
    // Scoped the way main.js does it.
    const scoped = currentAttemptLog(raw);
    assert.ok(!/address already in use/.test(scoped), "stale port line must be out of scope");
    assert.equal(
      shouldReclassifyAsInstalling({
        ...base,
        logTail: raw,
        portInUseInLog: /address already in use/.test(scoped),
      }),
      true
    );
  });

  it("does not re-derive a label the pre-spawn refusal already set", () => {
    assert.equal(
      shouldReclassifyAsInstalling({ ...base, failure: { incompleteBundle: true } }),
      false
    );
  });

  it("stays out of the way of a timeout or a non-failure", () => {
    assert.equal(shouldReclassifyAsInstalling({ ...base, failedToStart: false }), false);
    assert.equal(shouldReclassifyAsInstalling({ ...base, failure: null }), false);
    assert.equal(shouldReclassifyAsInstalling(), false);
  });

  it("leaves an unrelated crash alone", () => {
    assert.equal(
      shouldReclassifyAsInstalling({
        ...base,
        logTail: `${SPAWN_MARKER}
ModuleNotFoundError: No module named 'aiohttp'`,
      }),
      false
    );
  });

  // Only OUR bundled interpreter can be half-extracted. A user's own install
  // failing this way is a broken environment, and "wait for the installer to
  // finish" would be actively misleading advice.
  it("refuses to excuse a NON-bundled backend", () => {
    assert.equal(shouldReclassifyAsInstalling({ ...base, bundled: false }), false);
  });

  // The log is append-only across launches. A traceback from an EARLIER attempt
  // must not relabel this attempt's unrelated failure (a SIGKILL, say) as an
  // unfinished install -- that would show a reassuring dialog over a real fault.
  it("ignores a stdlib traceback from a previous launch attempt", () => {
    const staleThenNew = [
      SPAWN_MARKER,
      "ModuleNotFoundError: No module named 'urllib'",
      "gateway child exited code=1 signal=null",
      SPAWN_MARKER,
      "Fatal Python error: Segmentation fault",
    ].join("\n");
    assert.equal(shouldReclassifyAsInstalling({ ...base, logTail: staleThenNew }), false);
  });

  it("still relabels when the CURRENT attempt is the one that hit a stdlib crash", () => {
    const staleThenStdlib = [
      SPAWN_MARKER,
      "Fatal Python error: Segmentation fault",
      SPAWN_MARKER,
      "ModuleNotFoundError: No module named 'zoneinfo'",
    ].join("\n");
    assert.equal(shouldReclassifyAsInstalling({ ...base, logTail: staleThenStdlib }), true);
  });

  // Without the boundary the tail may span two attempts, so attribution is
  // unknowable -- decline rather than guess.
  it("declines when the attempt boundary has scrolled out of the tail", () => {
    assert.equal(
      shouldReclassifyAsInstalling({
        ...base,
        logTail: "ModuleNotFoundError: No module named 'urllib'",
      }),
      false
    );
  });
});

describe("describeIncompleteBundle", () => {
  it("frames the state as an unfinished install and names retry as the fix", () => {
    const msg = describeIncompleteBundle(["urllib", "zoneinfo"]);
    assert.match(msg, /still being installed/i);
    assert.match(msg, /retry/i);
  });

  // Stdlib package names are internals the reader cannot act on, and they are
  // already in the launch log; the dialog carries the count instead.
  it("reports a count rather than leaking stdlib package names", () => {
    const msg = describeIncompleteBundle(["urllib", "zoneinfo"]);
    assert.match(msg, /2 components/);
    assert.ok(!/urllib|zoneinfo/.test(msg), `message leaked package names: ${msg}`);
  });

  // Extraction can run for minutes, so an unqualified "if this persists" is
  // satisfied by two Retry clicks twenty seconds apart -- which would steer the
  // user into the reinstall this message exists to prevent.
  it("anchors the reinstall advice to a duration rather than bare persistence", () => {
    const msg = describeIncompleteBundle(["urllib"]);
    assert.match(msg, /take a few minutes/i);
    assert.match(msg, /after five minutes/i);
    // "persists" with no time qualifier is what made the advice premature.
    assert.ok(!/persists after restarting/i.test(msg), `unanchored advice: ${msg}`);
  });

  it("uses singular wording for exactly one missing component", () => {
    assert.match(describeIncompleteBundle(["zoneinfo"]), /1 component is/);
  });

  it("stays short even when the whole stdlib is missing", () => {
    const msg = describeIncompleteBundle(REQUIRED_STDLIB_PARTS.concat(["Lib"]));
    assert.ok(msg.length < 400, `message too long (${msg.length} chars)`);
  });

  // Used verbatim for the missing-interpreter case, where the caller has no part
  // list to hand over: the copy must still read as an unfinished install rather
  // than naming a count it does not know.
  it("reads correctly with no parts, for the missing-interpreter case", () => {
    const msg = describeIncompleteBundle([]);
    assert.match(msg, /still being installed/i);
    assert.match(msg, /Some components are/);
    assert.ok(!/\b0 components?\b/.test(msg), `message claimed a zero count: ${msg}`);
  });
});
