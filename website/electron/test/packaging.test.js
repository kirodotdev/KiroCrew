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

  it("keeps the data home out of the Windows uninstaller's reach", () => {
    // NSIS generates its own uninstaller, so there is no in-app uninstall
    // handler to audit any more -- the guarantee moves entirely into config.
    // deleteAppDataOnUninstall MUST stay false/absent: it would delete the
    // Electron userData dir on uninstall, and the KiroCrew home under
    // ~/.kiro/crew is user data that survives an uninstall by design.
    assert.notEqual(
      electronPkg.build.nsis?.deleteAppDataOnUninstall,
      true,
      "desktop uninstall must not opt into deleting app data"
    );
  });

  it("carries no Squirrel.Windows lifecycle handling", () => {
    // Squirrel spawned the app with --squirrel-install/-updated/-uninstall and
    // gave it ~15s to create/remove shortcuts and exit. NSIS does that itself,
    // so the handler is gone; a re-introduction would be a silent regression
    // back to a target electron-updater cannot drive.
    assert.equal(main.includes("--squirrel-"), false, "no Squirrel lifecycle flags in main.js");
    assert.equal(main.includes("Update.exe"), false, "no Squirrel Update.exe resolution in main.js");
    assert.equal(
      Object.hasOwn(electronPkg.build, "squirrelWindows"),
      false,
      "squirrelWindows config must not come back"
    );
    assert.deepEqual(electronPkg.build.win.target, ["nsis"]);
  });

  it("uses an assisted installer so nightly installs beside stable", () => {
    // getWindowsInstallationDirName(appInfo, !oneClick || isPerMachine) in
    // app-builder-lib only uses productFilename ("KiroCrew" / "KiroCrew
    // Nightly") when that flag is true. Under oneClick+perUser it falls back to
    // appInfo.sanitizedName -- the npm package name -- which would put both
    // channels in ONE directory named after the package rather than the product.
    // build-desktop.sh's -c.nsis.guid override separates the registry half.
    assert.equal(electronPkg.build.nsis.oneClick, false);
    assert.equal(electronPkg.build.nsis.perMachine, false);
  });

  it("gives nightly its own Linux package identity so it installs beside stable", () => {
    const nightlyOverrides = fs.readFileSync(
      path.resolve(ROOT, "..", "..", "packaging", "build-desktop.sh"),
      "utf8"
    );
    // Linux packages key install identity off the PACKAGE NAME, so a shared name
    // makes dpkg/rpm treat a nightly install as an upgrade of stable and remove
    // it -- the same class as the nsis.guid hazard above. The launcher and
    // desktop-entry names are per-install paths and must move with it.
    for (const override of [
      "-c.deb.packageName=kirocrew-nightly",
      "-c.rpm.packageName=kirocrew-nightly",
      "-c.linux.executableName=kirocrew-desktop-nightly",
      "-c.extraMetadata.desktopName=kirocrew-desktop-nightly.desktop",
    ]) {
      assert.ok(
        nightlyOverrides.includes(override),
        `build-desktop.sh must pass ${override} for the nightly channel`
      );
    }
    // And the stable defaults they override must be the ones actually shipped,
    // so a rename on either side fails here instead of silently colliding.
    assert.equal(electronPkg.build.deb.packageName, "kirocrew");
    assert.equal(electronPkg.build.rpm.packageName, "kirocrew");
    assert.equal(electronPkg.build.linux.executableName, "kirocrew-desktop");
    assert.equal(electronPkg.desktopName, "kirocrew-desktop.desktop");
    // syncDesktopName is what ties Electron's app_id and the entry's
    // StartupWMClass to desktopName; without it the nightly override above
    // would move the filename but not the window association.
    assert.equal(electronPkg.build.linux.syncDesktopName, true);
  });

  it("reclaims the updater cache the generated uninstaller cannot reach", () => {
    // The uninstaller template only ever clears $APPDATA (Roaming), and only
    // under deleteAppDataOnUninstall -- which stays false here to protect
    // ~/.kiro/crew. The electron-updater cache lives under $LOCALAPPDATA and so
    // matches no built-in path: without this macro a full installer payload
    // (~200MB) is orphaned on every uninstall.
    const nsh = fs.readFileSync(path.join(ROOT, "build", "installer.nsh"), "utf8");
    assert.match(nsh, /!macro customUnInstall\b/, "the customUnInstall hook must be defined");
    assert.match(
      nsh,
      /RMDir \/r "\$LOCALAPPDATA\\\$\{APP_PACKAGE_NAME\}-updater"/,
      "the cache name must be COMPOSED from ${APP_PACKAGE_NAME}, not hardcoded: " +
        "app-builder-lib derives updaterCacheDirName from the npm package name, so a " +
        "literal copy stops matching after a rename and silently leaks again"
    );
  });

  it("never deletes the update cache on the auto-update path", () => {
    // electron-updater runs this same uninstaller during an UPDATE (NsisUpdater
    // spawns the new installer with --updated, which the generated isUpdated
    // flag test reads). The cache root holds installer.exe, the baseline the
    // NEXT update diffs against, plus an in-flight pending/ download. Deleting
    // it mid-update would discard a live download and force every subsequent
    // update to transfer the whole installer.
    const nsh = fs.readFileSync(path.join(ROOT, "build", "installer.nsh"), "utf8");
    // Strip comments before locating the guard, so prose mentioning isUpdated or
    // RMDir cannot satisfy (or break) a structural assertion about code.
    const code = nsh
      .split("\n")
      .map(l => (l.trim().startsWith(";") ? "" : l))
      .join("\n");
    const body = code.slice(code.indexOf("!macro customUnInstall"));
    assert.match(body, /\$\{ifNot\} \$\{isUpdated\}/, "every removal must sit behind ifNot isUpdated");
    // Structural, not textual: assert no removal escapes the guard, so a later
    // edit that appends one after ${endIf} fails here.
    const guardStart = body.indexOf("${ifNot} ${isUpdated}");
    const guardEnd = body.indexOf("${endIf}");
    assert.ok(guardEnd > guardStart, "the isUpdated guard must be closed");
    for (const m of body.matchAll(/^\s*(?:RMDir|Delete)\b/gm)) {
      assert.ok(
        m.index > guardStart && m.index < guardEnd,
        `removal at offset ${m.index} sits outside the isUpdated guard`
      );
    }
  });

  it("keeps the uninstaller away from the Kiro Crew data home", () => {
    // The one thing this macro must never touch: sessions, memory, the DB and
    // config. It is user data and survives an uninstall by design.
    const nsh = fs.readFileSync(path.join(ROOT, "build", "installer.nsh"), "utf8");
    // Assert over the EXECUTABLE lines only. Matching raw file text would trip
    // on this file's own prose explaining what it deliberately spares -- a test
    // that fails on its own rationale teaches the next author to delete the
    // rationale. NSIS comments start with ';'.
    const removals = nsh
      .split("\n")
      .map(l => l.trim())
      .filter(l => l && !l.startsWith(";"))
      .filter(l => /^(RMDir|Delete)\b/.test(l));
    assert.ok(removals.length > 0, "expected at least one removal statement to audit");
    for (const line of removals) {
      assert.doesNotMatch(line, /\.kiro/, `data home in a removal path: ${line}`);
      assert.doesNotMatch(line, /\$PROFILE|\$USERPROFILE/, `profile-rooted removal: ${line}`);
      // Kiro-Cli is a separate product with its own installer; removing another
      // product's files would be a bug, not thoroughness.
      assert.doesNotMatch(line, /Kiro-Cli/i, `another product's files: ${line}`);
      // A bare $LOCALAPPDATA / $APPDATA with no subdirectory would wipe the
      // user's entire per-user app data.
      assert.doesNotMatch(
        line,
        /"\$(LOCALAPPDATA|APPDATA)\\?"/,
        `removal targets an app-data ROOT: ${line}`
      );
    }
  });

  it("pins the updater cache name the running app actually resolves", () => {
    // updaterCacheDirName is sanitizedName.toLowerCase() + "-updater" over the
    // npm package `name` (app-builder-lib appInfo.ts), and PublishManager copies
    // it into app-update.yml, which is what electron-updater reads at runtime.
    // The NSIS macro composes the same value from ${APP_PACKAGE_NAME} (=
    // appInfo.name), so this asserts the ONE assumption that lets those two
    // agree: the package name is already lowercase, making the toLowerCase()
    // step a no-op. An uppercase name would leave the macro's composed path
    // mismatched against the real cache dir.
    assert.equal(
      electronPkg.name,
      electronPkg.name.toLowerCase(),
      "an uppercase npm name would desync the NSIS-composed cache path from " +
        "updaterCacheDirName's lowercased value"
    );
    // The name is also NOT platform-scoped: it names the updater cache and the
    // Electron userData dir on every OS, so a mac-specific name is misleading
    // on the two platforms whose installers actually consume it.
    assert.doesNotMatch(
      electronPkg.name,
      /-mac$|-win$|-linux$/,
      "the npm name feeds cross-platform paths; it must not claim one platform"
    );
  });

  it("gives nightly its own per-user state so an uninstall cannot cross channels", () => {
    // THE INVARIANT THAT MAKES customUnInstall's RMDir SAFE. The npm `name`
    // determines updaterCacheDirName AND Electron's userData dir. Shared between
    // channels, both installs write one %LOCALAPPDATA%\<name>-updater and one
    // %APPDATA%\<name> -- so uninstalling nightly would delete stable's pending
    // update download, its differential baseline, and its window state (and vice
    // versa). productName and nsis.guid separate the install directory and the
    // registry key; neither touches per-user state.
    const buildScript = fs.readFileSync(
      path.resolve(ROOT, "..", "..", "packaging", "build-desktop.sh"),
      "utf8"
    );
    assert.ok(
      buildScript.includes("-c.extraMetadata.name=kirocrew-desktop-nightly"),
      "build-desktop.sh must give the nightly channel its own npm name, or the " +
        "uninstaller's cache removal reaches into the other channel's install"
    );
    // The stable default it overrides must be the one actually shipped, so a
    // rename on either side fails here instead of silently re-sharing.
    assert.equal(electronPkg.name, "kirocrew-desktop");
  });
});
