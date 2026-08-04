const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");

describe("electron-builder files list", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
  const bundledFiles = pkg.build.files;

  it("includes every local require() from main.js", () => {
    const main = fs.readFileSync(path.join(ROOT, "main.js"), "utf8");
    const localRequires = [...main.matchAll(/require\("\.\/([^"]+)"\)/g)].map(m => m[1] + ".js");

    const missing = localRequires.filter(f => !bundledFiles.includes(f));
    assert.deepStrictEqual(missing, [], `Missing from build.files: ${missing.join(", ")}`);
  });

  it("does not reference files that no longer exist", () => {
    const stale = bundledFiles.filter(f => !fs.existsSync(path.join(ROOT, f)));
    assert.deepStrictEqual(stale, [], `Stale entries in build.files: ${stale.join(", ")}`);
  });
});


describe("macOS bundle naming", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
  const extendInfo = pkg.build.mac.extendInfo || {};
  const buildScript = fs.readFileSync(
    path.resolve(ROOT, "..", "..", "packaging", "build-desktop.sh"),
    "utf8"
  );

  it("keeps CFBundleName aligned with productName for Electron helpers", () => {
    assert.equal(pkg.build.productName, "KiroCrew");
    assert.equal(
      Object.hasOwn(extendInfo, "CFBundleName"),
      false,
      "CFBundleName overrides break Electron helper-app discovery"
    );
  });

  it("uses CFBundleDisplayName for spaced stable and nightly names", () => {
    assert.equal(extendInfo.CFBundleDisplayName, "Kiro Crew");
    assert.match(
      buildScript,
      /-c\.mac\.extendInfo\.CFBundleDisplayName=Kiro Crew Nightly/
    );
    assert.doesNotMatch(buildScript, /-c\.mac\.extendInfo\.CFBundleName=/);
  });
});


// Under the hardened runtime, an Info.plist usage string does NOT grant a
// protected resource — the matching `device.*` entitlement does. With
// audio-input missing, the runtime refused the microphone BEFORE macOS (TCC) was
// consulted, so voice input reported "permission denied" and the user was never
// prompted and had no System Settings toggle to fix it. There are TWO signing
// lanes reading TWO different files (electron-builder locally, the enterprise
// signing service for release), so an entitlement present in one and absent from
// the other still ships a broken bundle on that lane. Pin both.
describe("macOS microphone entitlement (both signing lanes)", () => {
  const MIC = "com.apple.security.device.audio-input";
  const CAMERA = "com.apple.security.device.camera";

  /**
   * Strip XML comments, repeatedly, until the text stops changing.
   *
   * One pass is not enough: removing an outer `<!-- … -->` can splice together
   * text that forms a NEW `<!--`, so a single replace can leave a comment
   * opener behind. Looping to a fixed point (then asserting nothing is left)
   * is what makes "this key is real, not commented-out prose" trustworthy.
   */
  function stripComments(xml) {
    let out = xml;
    for (let i = 0; i < 20; i += 1) {
      const next = out.replace(/<!--[\s\S]*?-->/g, "");
      if (next === out) return next;
      out = next;
    }
    return out;
  }

  /**
   * Parse an entitlements plist into a plain { key: value } map.
   *
   * Deliberately a scanner rather than a built-from-a-string RegExp: composing
   * a pattern out of a key name means hand-rolling escaping, which is easy to
   * get subtly wrong (CodeQL flags exactly that), and a text match cannot tell
   * a genuine <dict> entry from one mentioned in a comment. Walking the tags
   * gives an exact key->value answer with no escaping in the picture at all.
   * Booleans are all these files hold; anything else is reported as its raw tag.
   */
  function parseEntitlements(xml) {
    const body = stripComments(xml);
    assert.equal(body.includes("<!--"), false, "unterminated XML comment");
    const out = {};
    const tag = /<key>([\s\S]*?)<\/key>\s*(<[^>]+>)/g;
    let m;
    while ((m = tag.exec(body)) !== null) {
      const name = m[1].trim();
      const value = m[2].replace(/\s|\//g, "");
      out[name] = value === "<true>" ? true : value === "<false>" ? false : m[2];
    }
    return { entitlements: out, body };
  }

  const LANES = {
    "electron-builder (build/entitlements.mac.plist)": path.join(
      ROOT, "build", "entitlements.mac.plist"
    ),
    "signing service (packaging/signing/Entitlements.entitlements)": path.resolve(
      ROOT, "..", "..", "packaging", "signing", "Entitlements.entitlements"
    ),
  };

  for (const [lane, file] of Object.entries(LANES)) {
    it(`grants the microphone in the ${lane} lane`, () => {
      const { entitlements } = parseEntitlements(fs.readFileSync(file, "utf8"));
      assert.equal(
        entitlements[MIC],
        true,
        `${file} must set ${MIC} to <true/> as a real dict entry, or the ` +
          "hardened runtime refuses the mic and no prompt ever appears"
      );
    });

    it(`does not request the camera in the ${lane} lane`, () => {
      // Least privilege: permission-handler.js denies any explicit video
      // request, so the camera entitlement would widen the TCC surface for a
      // capability the app never uses. Checked as a parsed key rather than a
      // substring, because these files carry comments that MENTION the camera
      // to explain its absence — a substring test would fail on the very prose
      // documenting the rule.
      const { entitlements } = parseEntitlements(fs.readFileSync(file, "utf8"));
      assert.notEqual(
        entitlements[CAMERA],
        true,
        `${file} must not grant ${CAMERA} — permission-handler.js denies video`
      );
    });

    it(`parses as a well-formed plist in the ${lane} lane`, () => {
      // codesign rejects a malformed plist outright, and the key assertions
      // above would still read a value out of a file that cannot be signed.
      const { entitlements, body } = parseEntitlements(fs.readFileSync(file, "utf8"));
      assert.match(body, /<plist[^>]*>\s*<dict>/, "expected a plist wrapping one dict");
      assert.equal(
        (body.match(/<dict>/g) || []).length,
        (body.match(/<\/dict>/g) || []).length,
        "unbalanced <dict> tags"
      );
      // A dangling key (no value after it) breaks signing, and every value in
      // these files is a boolean — so key count must equal parsed-entry count.
      assert.equal(
        (body.match(/<key>/g) || []).length,
        Object.keys(entitlements).length,
        "every entitlement key must be followed by a value"
      );
      for (const [name, value] of Object.entries(entitlements)) {
        assert.equal(typeof value, "boolean", `${name} must be <true/> or <false/>`);
      }
    });
  }

  it("keeps electron-builder pointed at the entitlements file it signs with", () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
    assert.equal(pkg.build.mac.entitlements, "build/entitlements.mac.plist");
    // `entitlements` is the one that matters for the mic: in Chromium the audio
    // capture runs in the BROWSER (main) process — the renderer only requests it
    // over IPC — and TCC attributes access to the responsible main bundle.
    // Verified against shipping apps: Chrome's and Slack's Renderer helpers
    // carry NO audio-input entitlement, yet their microphones work. Inherit is
    // pinned too so helpers keep the JIT/library-validation keys they need
    // (harmless for audio, and it matches what Slack does).
    assert.equal(pkg.build.mac.entitlementsInherit, "build/entitlements.mac.plist");
    // Without hardenedRuntime the resource-access entitlements are moot — this
    // is what makes audio-input load-bearing rather than decorative.
    assert.equal(pkg.build.mac.hardenedRuntime, true);
  });

  it("ships real Info.plist usage-string copy, not just the key", () => {
    // The entitlement grants the capability; this string is what macOS SHOWS.
    // macOS rejects an EMPTY purpose string, so asserting only that the key
    // exists would pass in exactly the state the prompt is refused — assert the
    // value. Declared here rather than inherited from Electron's generic
    // boilerplate ("This app needs access to the microphone"), so a
    // user-visible, load-bearing prompt is not at an upstream default's mercy.
    const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
    const usage = (pkg.build.mac.extendInfo || {}).NSMicrophoneUsageDescription;
    assert.equal(typeof usage, "string", "NSMicrophoneUsageDescription must be declared");
    assert.ok(
      usage.trim().length >= 20,
      "must be real prompt copy explaining WHY the mic is used, not empty/placeholder"
    );
  });
});

describe("uninstall data preservation contract", () => {
  const electronPkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
  const websitePkg = JSON.parse(
    fs.readFileSync(path.resolve(ROOT, "..", "package.json"), "utf8")
  );
  const main = fs.readFileSync(path.join(ROOT, "main.js"), "utf8");

  it("defines no package-manager uninstall hooks", () => {
    for (const [name, scripts] of [
      ["electron", electronPkg.scripts || {}],
      ["website", websitePkg.scripts || {}],
    ]) {
      assert.equal(Object.hasOwn(scripts, "preuninstall"), false, `${name} preuninstall`);
      assert.equal(Object.hasOwn(scripts, "postuninstall"), false, `${name} postuninstall`);
    }
  });

  it("keeps the Squirrel uninstall handler shortcut-only", () => {
    const match = main.match(
      /else if \(cmd === "--squirrel-uninstall"\) \{([\s\S]*?)\n  \} else if/
    );
    assert.ok(match, "expected an explicit Squirrel uninstall branch");
    assert.equal(
      match[1].trim(),
      'run(["--removeShortcut=" + target]);',
      "Squirrel uninstall must remain shortcut-only and never touch the data home"
    );
    assert.notEqual(
      electronPkg.build.nsis?.deleteAppDataOnUninstall,
      true,
      "desktop uninstall must not opt into deleting app data"
    );
  });
});
