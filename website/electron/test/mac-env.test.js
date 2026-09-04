const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  LAUNCHCTL,
  MAX_LAUNCHD_READ_BYTES,
  READ_TIMEOUT_MS,
  readLaunchdPath,
  sanitizePathEntries,
  mergeGatewayPath,
  resolveGatewayPath,
} = require("../mac-env");

// The PATH a GUI-launched .app actually receives on macOS — the whole reason
// this module exists (issue #2367).
const GUI_PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
// A realistic launchd-domain value: the GUI entries plus user-managed dirs.
const LAUNCHD_PATH = `/Users/u/.opencode/bin:/Users/u/.cargo/bin:/opt/homebrew/bin:${GUI_PATH}`;

/** execFileSync stub that records its call and returns `out`. */
const stubExec = (out, calls = []) => {
  const fn = (file, args, opts) => {
    calls.push({ file, args, opts });
    if (out instanceof Error) throw out;
    return out;
  };
  fn.calls = calls;
  return fn;
};

describe("readLaunchdPath", () => {
  it("returns the trimmed launchd value on darwin", () => {
    const exec = stubExec(`${LAUNCHD_PATH}\n`);
    assert.equal(readLaunchdPath({ execFileSync: exec, platform: "darwin" }), LAUNCHD_PATH);
  });

  it("invokes launchctl by absolute path, with a timeout and no inherited stderr", () => {
    const exec = stubExec("/usr/bin\n");
    readLaunchdPath({ execFileSync: exec, platform: "darwin" });
    const [call] = exec.calls;
    // Absolute: this runs precisely because PATH is not yet trustworthy.
    assert.equal(call.file, LAUNCHCTL);
    assert.ok(call.file.startsWith("/"));
    assert.deepEqual(call.args, ["getenv", "PATH"]);
    assert.equal(call.opts.timeout, READ_TIMEOUT_MS);
    // The READ bound is the module's only size limit -- pin it to maxBuffer so
    // it cannot drift back into being a merged-PATH cap (see the large-inherited
    // -PATH test for why that shape was removed).
    assert.equal(call.opts.maxBuffer, MAX_LAUNCHD_READ_BYTES);
    assert.deepEqual(call.opts.stdio, ["ignore", "pipe", "ignore"]);
  });

  it("does not shell out at all off darwin", () => {
    for (const platform of ["win32", "linux"]) {
      const exec = stubExec(LAUNCHD_PATH);
      assert.equal(readLaunchdPath({ execFileSync: exec, platform }), null);
      assert.equal(exec.calls.length, 0, `should not run launchctl on ${platform}`);
    }
  });

  it("returns null for an unset or blank variable", () => {
    // `launchctl getenv` on an unset key prints nothing.
    assert.equal(readLaunchdPath({ execFileSync: stubExec(""), platform: "darwin" }), null);
    assert.equal(readLaunchdPath({ execFileSync: stubExec("\n"), platform: "darwin" }), null);
    assert.equal(readLaunchdPath({ execFileSync: stubExec("   \n"), platform: "darwin" }), null);
  });

  it("degrades to null when launchctl fails, is missing, or times out", () => {
    for (const err of [new Error("ENOENT"), Object.assign(new Error("timed out"), { killed: true })]) {
      assert.equal(readLaunchdPath({ execFileSync: stubExec(err), platform: "darwin" }), null);
    }
  });
});

describe("sanitizePathEntries", () => {
  it("keeps absolute entries in order", () => {
    assert.deepEqual(sanitizePathEntries("/usr/bin:/bin"), ["/usr/bin", "/bin"]);
  });

  it("drops relative entries, which a child re-resolves against its own cwd", () => {
    assert.deepEqual(sanitizePathEntries("/usr/bin:.:bin:../up:~/bin"), ["/usr/bin"]);
  });

  it("drops entries containing a .. segment", () => {
    assert.deepEqual(sanitizePathEntries("/usr/bin:/opt/../etc"), ["/usr/bin"]);
  });

  it("keeps a directory whose NAME merely starts with dots", () => {
    // Only a literal ".." SEGMENT is traversal; "..cache" is a real dir name.
    assert.deepEqual(sanitizePathEntries("/opt/..cache/bin"), ["/opt/..cache/bin"]);
  });

  it("drops entries with NUL or newlines", () => {
    assert.deepEqual(sanitizePathEntries("/usr/bin:/a\0b:/c\nd:/e\rf"), ["/usr/bin"]);
  });

  it("drops empty segments rather than emitting an empty entry", () => {
    // An empty PATH entry means "cwd" to most resolvers — the exact hazard
    // the relative-entry rule exists to prevent.
    assert.deepEqual(sanitizePathEntries("/usr/bin::/bin:"), ["/usr/bin", "/bin"]);
  });

  it("returns [] for non-strings and empty input", () => {
    for (const v of ["", null, undefined, 42, {}]) assert.deepEqual(sanitizePathEntries(v), []);
  });
});

describe("mergeGatewayPath", () => {
  it("appends launchd-only entries AFTER the inherited ones", () => {
    const { path, added } = mergeGatewayPath({ basePath: GUI_PATH, launchdPath: LAUNCHD_PATH });
    assert.equal(path, `${GUI_PATH}:/Users/u/.opencode/bin:/Users/u/.cargo/bin:/opt/homebrew/bin`);
    assert.deepEqual(added, ["/Users/u/.opencode/bin", "/Users/u/.cargo/bin", "/opt/homebrew/bin"]);
  });

  it("never lets a launchd entry shadow a system binary that already resolves", () => {
    // The security property of appending: /usr/bin stays ahead of a
    // user-writable directory, so `python3` cannot be rebound by it.
    const { path } = mergeGatewayPath({
      basePath: GUI_PATH,
      launchdPath: "/Users/u/evil/bin:/usr/bin",
    });
    assert.ok(path.indexOf("/usr/bin") < path.indexOf("/Users/u/evil/bin"));
  });

  it("de-duplicates first-wins and is idempotent", () => {
    const once = mergeGatewayPath({ basePath: GUI_PATH, launchdPath: LAUNCHD_PATH });
    const twice = mergeGatewayPath({ basePath: once.path, launchdPath: LAUNCHD_PATH });
    assert.equal(twice.path, once.path);
    assert.deepEqual(twice.added, [], "a re-merge contributes nothing new");
  });

  it("reports no additions when launchd matches the inherited PATH", () => {
    const { path, added } = mergeGatewayPath({ basePath: GUI_PATH, launchdPath: GUI_PATH });
    assert.equal(path, GUI_PATH);
    assert.deepEqual(added, []);
  });

  it("keeps the inherited PATH verbatim, including entries it rejects from launchd", () => {
    // Validation applies to what is ADDED, never to what was inherited:
    // rewriting an inherited entry would stop resolving a command that
    // resolves today. Matches env.py::augmented_path, which validates the
    // dirs it contributes and passes base_path through untouched.
    const odd = "/usr/bin:.:/opt/../etc::relative";
    const { path, added } = mergeGatewayPath({ basePath: odd, launchdPath: "/opt/x/bin:./nope" });
    assert.ok(path.startsWith(odd), "inherited value must survive byte-for-byte");
    assert.equal(path, `${odd}:/opt/x/bin`);
    assert.deepEqual(added, ["/opt/x/bin"], "only the valid launchd entry is appended");
  });

  it("appends to a very large inherited PATH instead of suppressing the fix", () => {
    // There is deliberately NO merged-size cap. An earlier revision capped the
    // MERGED value at 8KB, which meant a user whose inherited PATH already
    // exceeded that got every launchd entry dropped -- the fix silently doing
    // nothing for exactly the users with the most crowded PATH. The launchd
    // READ is bounded instead (MAX_LAUNCHD_READ_BYTES via maxBuffer), which is
    // the only bound that is load-bearing: the environment-block limit behind
    // E2BIG is ~1MB and a 32KB read cannot approach it.
    const filler = [];
    let bytes = 0;
    while (bytes < 12 * 1024) {
      const d = `/opt/f${String(filler.length).padStart(4, "0")}/bin`;
      filler.push(d);
      bytes += d.length + 1;
    }
    const basePath = filler.join(":");
    assert.ok(Buffer.byteLength(basePath, "utf8") > 8192, "fixture must exceed the old cap");

    const { path, added } = mergeGatewayPath({ basePath, launchdPath: "/u/x/bin" });
    assert.deepEqual(added, ["/u/x/bin"], "a large inherited PATH must not suppress the append");
    assert.ok(path.startsWith(basePath), "the inherited PATH is still preserved whole");
    assert.equal(path, `${basePath}:/u/x/bin`);
  });

  it("never drops an inherited entry, whatever its size", () => {
    const longDir = `/opt/${"L".repeat(4000)}/bin`;
    const basePath = `/usr/bin:${longDir}`;
    const { path } = mergeGatewayPath({ basePath, launchdPath: "/u/x/bin" });
    assert.ok(path.includes(longDir), "a long inherited entry must survive");
    assert.deepEqual(path.split(":").slice(0, 2), ["/usr/bin", longDir]);
  });

  it("returns the inherited value unchanged when no launchd entry survives validation", () => {
    const { path, added } = mergeGatewayPath({ basePath: "relative", launchdPath: "also/relative" });
    assert.equal(path, "relative", "an invalid inherited value is still passed through");
    assert.deepEqual(added, []);
  });

  it("preserves a zero-length inherited entry, even though that yields '::'", () => {
    // A trailing ":" is a ZERO-LENGTH ENTRY, which POSIX defines as the cwd, so
    // "/usr/bin:" is [/usr/bin, cwd]. Appending must produce
    // [/usr/bin, cwd, /opt/x/bin] -- spelled "/usr/bin::/opt/x/bin". Collapsing
    // it to one ":" would DELETE the cwd entry and stop resolving a command that
    // resolves today. This test exists because "::" reads like a formatting bug
    // and a reviewer already proposed "fixing" it.
    const { path } = mergeGatewayPath({ basePath: "/usr/bin:", launchdPath: "/opt/x/bin" });
    assert.equal(path, "/usr/bin::/opt/x/bin");

    // Stated as the invariant rather than the spelling: every entry the
    // inherited value had is still present, at the same index.
    const before = "/usr/bin:".split(":");
    const after = path.split(":");
    assert.deepEqual(after.slice(0, before.length), before, "inherited entries unmoved");
    assert.deepEqual(after.slice(before.length), ["/opt/x/bin"], "only the new dir is appended");
  });

  it("preserves a leading zero-length entry too", () => {
    const { path } = mergeGatewayPath({ basePath: ":/usr/bin", launchdPath: "/opt/x/bin" });
    assert.equal(path, ":/usr/bin:/opt/x/bin");
    assert.deepEqual(path.split(":").slice(0, 2), ["", "/usr/bin"]);
  });

  it("survives an empty inherited PATH", () => {
    const { path, added } = mergeGatewayPath({ basePath: "", launchdPath: "/opt/x/bin" });
    assert.equal(path, "/opt/x/bin");
    assert.deepEqual(added, ["/opt/x/bin"]);
  });
});

describe("resolveGatewayPath", () => {
  it("returns the merged PATH when the launchd domain adds directories", () => {
    const got = resolveGatewayPath({
      execFileSync: stubExec(LAUNCHD_PATH),
      platform: "darwin",
      basePath: GUI_PATH,
    });
    assert.ok(got, "expected a merge result");
    assert.ok(got.path.includes("/Users/u/.opencode/bin"));
    assert.equal(got.added.length, 3);
  });

  it("returns null — leave the environment untouched — when nothing is added", () => {
    // Same value in the domain as inherited: rewriting PATH to an equal string
    // would be a pointless environment mutation.
    assert.equal(
      resolveGatewayPath({
        execFileSync: stubExec(GUI_PATH),
        platform: "darwin",
        basePath: GUI_PATH,
      }),
      null
    );
  });

  it("returns null off darwin and when the domain is unreadable", () => {
    assert.equal(
      resolveGatewayPath({ execFileSync: stubExec(LAUNCHD_PATH), platform: "linux", basePath: GUI_PATH }),
      null
    );
    assert.equal(
      resolveGatewayPath({ execFileSync: stubExec(new Error("boom")), platform: "darwin", basePath: GUI_PATH }),
      null
    );
  });

  it("does not drop the inherited PATH when the domain is only partially valid", () => {
    // A mangled domain must never cost the app the PATH it already had.
    const got = resolveGatewayPath({
      execFileSync: stubExec("relative:/opt/good/bin:../bad"),
      platform: "darwin",
      basePath: GUI_PATH,
    });
    assert.ok(got);
    assert.ok(got.path.startsWith(GUI_PATH));
    assert.deepEqual(got.added, ["/opt/good/bin"]);
  });
});
