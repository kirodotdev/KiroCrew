const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

it("Linux BrowserWindows carry the packaged application icon", () => {
  const main = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  assert.match(main, /if \(IS_WIN \|\| IS_LINUX\) \{[\s\S]*?opts\.icon = path\.join\(__dirname, iconFile\)/);
});

const ROOT = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(ROOT, "..", "..");
const INSTALLER_ASSETS = path.join(REPO_ROOT, "packaging", "installer-assets");
const ASSISTED_INSTALLER_TEMPLATE = path.join(
  ROOT,
  "node_modules",
  "app-builder-lib",
  "templates",
  "nsis",
  "assistedInstaller.nsh"
);

function normalizedNsisBlock(source, pattern) {
  const match = source.match(pattern);
  assert.ok(match, `expected NSIS block matching ${pattern}`);
  return match[0]
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(Boolean)
    .join("\n");
}

function tiffPages(file) {
  const bytes = fs.readFileSync(file);
  const byteOrder = bytes.toString("ascii", 0, 2);
  assert.ok(byteOrder === "II" || byteOrder === "MM");
  const littleEndian = byteOrder === "II";
  const uint16 = offset =>
    littleEndian ? bytes.readUInt16LE(offset) : bytes.readUInt16BE(offset);
  const uint32 = offset =>
    littleEndian ? bytes.readUInt32LE(offset) : bytes.readUInt32BE(offset);
  assert.equal(uint16(2), 42);

  const pages = [];
  const visited = new Set();
  let directoryOffset = uint32(4);
  while (directoryOffset !== 0) {
    assert.ok(!visited.has(directoryOffset), "TIFF directory chain must not loop");
    visited.add(directoryOffset);
    const entryCount = uint16(directoryOffset);
    const tags = new Map();
    for (let index = 0; index < entryCount; index += 1) {
      const entryOffset = directoryOffset + 2 + index * 12;
      const tag = uint16(entryOffset);
      const type = uint16(entryOffset + 2);
      const count = uint32(entryOffset + 4);
      if (count !== 1) continue;
      let value;
      if (type === 3) value = uint16(entryOffset + 8);
      if (type === 4) value = uint32(entryOffset + 8);
      if (type === 5) {
        const rationalOffset = uint32(entryOffset + 8);
        value = uint32(rationalOffset) / uint32(rationalOffset + 4);
      }
      if (value !== undefined) tags.set(tag, value);
    }
    pages.push({
      width: tags.get(256),
      height: tags.get(257),
      xResolution: tags.get(282),
      yResolution: tags.get(283),
      resolutionUnit: tags.get(296),
    });
    directoryOffset = uint32(directoryOffset + 2 + entryCount * 12);
  }
  return pages;
}

function bmpInfo(file) {
  const bytes = fs.readFileSync(file);
  assert.equal(bytes.toString("ascii", 0, 2), "BM");
  return {
    width: bytes.readInt32LE(18),
    height: Math.abs(bytes.readInt32LE(22)),
    bitsPerPixel: bytes.readUInt16LE(28),
  };
}

function bmpDarkPixelCount(file, { left, top, right, bottom }) {
  const bytes = fs.readFileSync(file);
  assert.equal(bytes.toString("ascii", 0, 2), "BM");
  const offset = bytes.readUInt32LE(10);
  const width = bytes.readInt32LE(18);
  const signedHeight = bytes.readInt32LE(22);
  const height = Math.abs(signedHeight);
  assert.equal(bytes.readUInt16LE(28), 24);
  const stride = Math.ceil((width * 3) / 4) * 4;
  let count = 0;
  for (let y = top; y < bottom; y += 1) {
    const row = signedHeight > 0 ? height - 1 - y : y;
    for (let x = left; x < right; x += 1) {
      const pixel = offset + row * stride + x * 3;
      const blue = bytes[pixel];
      const green = bytes[pixel + 1];
      const red = bytes[pixel + 2];
      if (red < 96 && green < 96 && blue < 96) count += 1;
    }
  }
  return count;
}

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


describe("first-download installer design contract", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
  const background = path.join(INSTALLER_ASSETS, "dmg-background.tiff");
  const sidebar = path.join(INSTALLER_ASSETS, "windows-installer-sidebar.bmp");
  const header = path.join(INSTALLER_ASSETS, "windows-installer-header.bmp");
  const installer = fs.readFileSync(path.join(ROOT, "build", "installer.nsh"), "utf8");
  const buildWorkflow = fs.readFileSync(
    path.join(REPO_ROOT, ".github", "workflows", "build.yml"),
    "utf8"
  );
  const runtimeScript = fs.readFileSync(
    path.join(REPO_ROOT, ".github", "scripts", "test-windows-installer.ps1"),
    "utf8"
  );

  it("positions the macOS app and Applications target on the branded background", () => {
    assert.equal(pkg.build.dmg.background, "../../packaging/installer-assets/dmg-background.tiff");
    assert.equal(pkg.build.dmg.title, "${productName}");
    assert.equal(pkg.build.dmg.iconSize, 96);
    assert.equal(pkg.build.dmg.iconTextSize, 13);
    assert.equal(pkg.build.dmg.filesystem, "HFS+");
    assert.deepEqual(pkg.build.dmg.contents, [
      { x: 170, y: 246, type: "file" },
      { x: 490, y: 246, type: "link", path: "/Applications" },
    ]);
    assert.deepEqual(tiffPages(background), [
      { width: 660, height: 420, xResolution: 72, yResolution: 72, resolutionUnit: 2 },
      { width: 1320, height: 840, xResolution: 144, yResolution: 144, resolutionUnit: 2 },
    ]);
  });

  it("uses NSIS-native branded artwork without changing the assisted install flow", () => {
    assert.equal(
      pkg.build.nsis.installerSidebar,
      "../../packaging/installer-assets/windows-installer-sidebar.bmp"
    );
    assert.equal(
      pkg.build.nsis.installerHeader,
      "../../packaging/installer-assets/windows-installer-header.bmp"
    );
    assert.deepEqual(bmpInfo(sidebar), { width: 164, height: 314, bitsPerPixel: 24 });
    assert.deepEqual(bmpInfo(header), { width: 150, height: 57, bitsPerPixel: 24 });
    for (const eyeRegion of [
      { left: 84, top: 15, right: 96, bottom: 29 },
      { left: 106, top: 8, right: 119, bottom: 23 },
      { left: 98, top: 32, right: 114, bottom: 47 },
    ]) {
      assert.ok(bmpDarkPixelCount(header, eyeRegion) >= 6);
    }
    assert.equal(pkg.build.nsis.oneClick, false);
    assert.equal(pkg.build.nsis.perMachine, false);
    assert.equal(pkg.build.nsis.allowToChangeInstallationDirectory, false);
    assert.equal(pkg.build.nsis.runAfterFinish, true);
  });

  it("cross-fades native pages without stalling extraction and enforces an install-time ceiling", () => {
    const assistedInstallerTemplate = fs.readFileSync(ASSISTED_INSTALLER_TEMPLATE, "utf8");
    assert.doesNotMatch(installer, /Page custom/);
    assert.doesNotMatch(installer, /NSD_(?:Create|Kill)Timer|Sleep\s+\d+/);
    assert.match(installer, /KIRO_SPI_GETCLIENTAREAANIMATION/);
    assert.match(installer, /SystemParametersInfoW/);
    assert.match(installer, /AnimateWindow\(p \$HWNDPARENT, i \$\{KIRO_FADE_IN_MS\}, i \$0/);
    assert.match(installer, /AnimateWindow\(p \$HWNDPARENT, i \$\{KIRO_FADE_OUT_MS\}, i \$0/);
    assert.match(installer, /Function \.onGUIEnd[\s\S]*?Call KiroFadeOutPage[\s\S]*?FunctionEnd/);
    assert.match(
      installer,
      /!macro customWelcomePage[\s\S]*?MUI_PAGE_CUSTOMFUNCTION_PRE KiroWelcomePre[\s\S]*?MUI_PAGE_CUSTOMFUNCTION_SHOW KiroFadeInPage[\s\S]*?MUI_PAGE_CUSTOMFUNCTION_LEAVE KiroFadeOutPage[\s\S]*?!insertmacro MUI_PAGE_WELCOME/
    );
    assert.match(
      installer,
      /!macro customInstallMode[\s\S]*?MUI_PAGE_CUSTOMFUNCTION_SHOW KiroFadeInPage[\s\S]*?MUI_PAGE_CUSTOMFUNCTION_LEAVE KiroInstallModeLeave/
    );
    assert.match(
      installer,
      /!macro customPageAfterChangeDir[\s\S]*?MUI_PAGE_CUSTOMFUNCTION_SHOW KiroFadeInPage[\s\S]*?MUI_PAGE_CUSTOMFUNCTION_LEAVE KiroFadeOutPage/
    );
    assert.match(
      installer,
      /!macro customFinishPage[\s\S]*?MUI_PAGE_CUSTOMFUNCTION_SHOW KiroFadeInPage[\s\S]*?MUI_PAGE_CUSTOMFUNCTION_LEAVE KiroFadeOutPage[\s\S]*?!insertmacro MUI_PAGE_FINISH/
    );
    const startAppContract = /Function StartApp[\s\S]*?!define MUI_FINISHPAGE_RUN_FUNCTION "StartApp"/;
    assert.equal(
      normalizedNsisBlock(installer, startAppContract),
      normalizedNsisBlock(assistedInstallerTemplate, startAppContract),
      "customFinishPage must retain electron-builder's locked StartApp contract"
    );
    assert.match(buildWorkflow, /test-windows-installer\.ps1/);
    assert.match(runtimeScript, /^\$MaxInstallSeconds = 120$/m);
    assert.match(runtimeScript, /^\$MaxGatewayReadySeconds = 30$/m);
    assert.match(runtimeScript, /silent-install-seconds=/);
    assert.match(runtimeScript, /gateway-ready-seconds=/);
    assert.match(runtimeScript, /startupPycCount -lt 1000/);
    assert.match(runtimeScript, /\/api\/ready/);
    assert.match(
      runtimeScript,
      /\$env:KIRO_HOME = Join-Path \$gatewayHome "kiro"/
    );
    assert.match(runtimeScript, /WaitForExit\(\$MaxInstallSeconds \* 1000\)/);
    assert.match(runtimeScript, /native-install-mode\.png/);
  });

  it("publishes the staged Windows payload without a second small-file copy pass", () => {
    const patchScript = fs.readFileSync(
      path.join(ROOT, "scripts", "patch-nsis-template.js"),
      "utf8"
    );
    assert.equal(pkg.scripts.postinstall, "node scripts/patch-nsis-template.js");
    assert.match(patchScript, /EXPECTED_APP_BUILDER_VERSION = "26\.15\.3"/);
    assert.match(patchScript, /!ifmacrodef customPublishAppPackage/);
    assert.match(
      installer,
      /!macro customPublishAppPackage SOURCE DESTINATION[\s\S]*?Rename "\$\{SOURCE\}\\resources" "\$\{DESTINATION\}\\resources"[\s\S]*?Rename "\$\{SOURCE\}\\locales" "\$\{DESTINATION\}\\locales"[\s\S]*?CopyFiles \/SILENT "\$\{SOURCE\}\\\*" "\$\{DESTINATION\}"/
    );
  });

  it("ships the Windows startup caches generated after the platform prune", () => {
    const backendResource = pkg.build.extraResources.find(
      resource => resource.from === "backend-dist"
    );
    const buildScript = fs.readFileSync(
      path.join(REPO_ROOT, "packaging", "build-desktop.sh"),
      "utf8"
    );
    assert.deepEqual(backendResource.filter, ["**/*"]);
    assert.match(buildScript, /rm -rf[\s\S]*?include libs tcl/);
    assert.match(buildScript, /llama_cpp_libs\/linux_aarch64/);
    assert.match(buildScript, /DLLs\/_tkinter\.pyd DLLs\/tcl\*\.dll DLLs\/tk\*\.dll/);
    assert.match(
      buildScript,
      /precompile_windows\.py" \\\r?\n\s*--root "\$out" --module kiro_crew\.cli_server/
    );
  });

  it("shows native progress for updates without adding setup decisions", () => {
    assert.deepEqual(
      [...installer.matchAll(/^LangString KiroUpdateProgress (\d+) /gm)].map((match) => Number(match[1])),
      [
        1033, 1031, 1036, 3082, 2052, 1028, 1041, 1042, 1040, 1043, 1030, 1053, 1044,
        1035, 1049, 2070, 1046, 1045, 1058, 1029, 1051, 1038, 1025, 1055, 1054, 1066,
      ],
      "the update progress contract must cover every default electron-builder installer language"
    );
    assert.match(
      installer,
      /LangString KiroUpdateProgress 1033 "This can take several minutes\. \$\{PRODUCT_NAME\} will reopen automatically\."/
    );
    assert.match(
      installer,
      /Function KiroWelcomePre[\s\S]*?\$KiroVisibleUpdate == 1[\s\S]*?Abort[\s\S]*?FunctionEnd/
    );
    assert.match(
      installer,
      /!macro customInstallMode[\s\S]*?\$KiroVisibleUpdate == 1[\s\S]*?StrCpy \$isForceMachineInstall 1[\s\S]*?StrCpy \$isForceCurrentInstall 1/
    );
    assert.match(
      installer,
      /Function KiroFinishPagePre[\s\S]*?\$KiroVisibleUpdate == 1[\s\S]*?\$\{If\} \$\{isForceRun\}[\s\S]*?Call StartApp[\s\S]*?!insertmacro quitSuccess[\s\S]*?FunctionEnd/
    );
    assert.match(
      installer,
      /!macro customInit[\s\S]*?\$\{If\} \$\{isUpdated\}[\s\S]*?\$\{If\} \$\{Silent\}[\s\S]*?SetSilent normal/
    );
    assert.match(
      installer,
      /Function KiroInstFilesShow[\s\S]*?\$KiroVisibleUpdate == 1[\s\S]*?GetDlgItem \$0 \$HWNDPARENT 1038[\s\S]*?SendMessage \$0 \$\{KIRO_WM_SETTEXT\} 0 "STR:\$\(KiroUpdateProgress\)"[\s\S]*?EnableWindow \$0 0[\s\S]*?ShowWindow \$0 \$\{KIRO_SW_HIDE\}[\s\S]*?FunctionEnd/
    );
    assert.match(
      installer,
      /Function KiroAbortPre[\s\S]*?\$KiroVisibleUpdate == 1[\s\S]*?Abort[\s\S]*?FunctionEnd[\s\S]*?!define MUI_PAGE_FUNCTION_ABORTWARNING KiroAbortPre/
    );
    assert.doesNotMatch(installer, /NSD_(?:Create|Kill)Timer|Sleep\s+\d+/);
  });

  it("keeps install-root ownership and legacy startup cleanup without custom pages", () => {
    assert.match(installer, /Function KiroEnsureAppInstallDir[\s\S]*?FunctionEnd/);
    assert.match(installer, /!macro customInit[\s\S]*?Call KiroEnsureAppInstallDir/);
    assert.match(
      installer,
      /!macro customPageAfterChangeDir[\s\S]*?Function KiroValidateInstallDirAfterMode/
    );
    assert.match(
      installer,
      /Function KiroInstallModeLeave[\s\S]*?Call KiroValidateInstallDirAfterMode[\s\S]*?Call KiroFadeOutPage[\s\S]*?FunctionEnd/
    );
    assert.doesNotMatch(installer, /Page custom KiroValidateInstallDirAfterMode/);
    assert.match(
      installer,
      /Function KiroValidateInstallDirAfterMode[\s\S]*?Call KiroEnsureAppInstallDir[\s\S]*?StrCpy \$INSTDIR \$KiroInstallDir[\s\S]*?FunctionEnd/
    );
    const validateInstallDir = normalizedNsisBlock(
      installer,
      /Function KiroValidateInstallDirAfterMode[\s\S]*?FunctionEnd/
    );
    assert.doesNotMatch(validateInstallDir, /Abort/);
    assert.match(installer, /GetFileAttributesW/);
    assert.match(installer, /KiroCheckFreshInstallDir/);
    assert.match(
      installer,
      /\$\{ifNot\} \$\{isUpdated\}[\s\S]*?DeleteRegValue SHELL_CONTEXT "\$\{KIRO_RUN_KEY\}" "\$\{PRODUCT_NAME\}"/
    );
    assert.match(installer, /MessageBox MB_OK\|MB_ICONSTOP "\$\(\^CantWrite\)\$KiroInstallDir"/);
    assert.doesNotMatch(installer, /MessageBox[^\r\n]*\$\(\^DirBrowseText\)/);
    assert.match(runtimeScript, /pre-existing-user-file\.txt/);
    assert.match(runtimeScript, /existing-install-file/);
    assert.match(runtimeScript, /invalidProcess\.ExitCode -ne 2/);
    assert.equal(pkg.devDependencies["electron-builder"], "26.15.3");
  });

  it("reuses the shipped logo and opening-animation ghost artwork", () => {
    const normalize = text => text.replaceAll(",", " ").replace(/\s+/g, " ");
    const loading = normalize(fs.readFileSync(path.join(ROOT, "loading.html"), "utf8"));
    const siteLogo = normalize(
      fs.readFileSync(path.join(REPO_ROOT, "site", "public", "kirocrew-logo.svg"), "utf8")
    );
    const dmgSource = normalize(
      fs.readFileSync(path.join(INSTALLER_ASSETS, "dmg-background.svg"), "utf8")
    );
    const sidebarSource = normalize(
      fs.readFileSync(path.join(INSTALLER_ASSETS, "windows-installer-sidebar.svg"), "utf8")
    );
    const headerSource = normalize(
      fs.readFileSync(path.join(INSTALLER_ASSETS, "windows-installer-header.svg"), "utf8")
    );

    const openingGhost = "M398.554 818.914C316.315 1001.03";
    const logoGhost = "M84.76 266.62c-19.2 42.53";
    const logoEyes = [
      "M140.41 203.27c-7.67 0",
      "M171.94 203.27c-7.67 0",
      "M61.06 92.87c-1.57-4.01",
      "M67.53 109.37c-1.57-4.01",
      "M194.79 60.97c3.96 2.55",
      "M178.51 50.47c3.96 2.55",
    ];
    assert.ok(loading.includes(openingGhost));
    assert.ok(dmgSource.includes(openingGhost));
    assert.ok(sidebarSource.includes(openingGhost));
    assert.ok(siteLogo.includes(logoGhost));
    assert.ok(sidebarSource.includes(logoGhost));
    assert.ok(headerSource.includes(logoGhost));
    for (const eye of logoEyes) {
      assert.ok(siteLogo.includes(eye));
      assert.ok(sidebarSource.includes(eye));
      assert.ok(headerSource.includes(eye));
    }
  });

  it("applies the branded layout again after signing and stapling", () => {
    const workflow = fs.readFileSync(
      path.join(REPO_ROOT, ".github", "workflows", "sign-and-notarize.yml"),
      "utf8"
    );
    const helperPath = path.join(REPO_ROOT, "packaging", "signing", "build-dmg.sh");
    const helper = fs.readFileSync(helperPath, "utf8");

    assert.match(workflow, /bash packaging\/signing\/build-dmg\.sh/);
    assert.match(workflow, /unsigned_dmg_key=pre-signed/);
    assert.match(workflow, /work\/layout-template\.dmg/);
    assert.match(helper, /hdiutil convert/);
    assert.match(helper, /hdiutil resize -size min/);
    assert.match(helper, /template and signed app names differ/);
  });
});


// Shared by the microphone and local-network describes below: both need to read
// the two entitlements plists as parsed key->value maps rather than as text.
// Hoisted to module scope so the two blocks cannot drift into slightly different
// parsers and disagree about what a file actually grants.

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

// There are TWO signing lanes reading TWO different files (electron-builder
// locally, the enterprise signing service for release), so an entitlement
// present in one and absent from the other still ships a broken bundle on that
// lane. Every entitlement assertion below runs against both.
const ENTITLEMENT_LANES = {
  "electron-builder (build/entitlements.mac.plist)": path.join(
    ROOT, "build", "entitlements.mac.plist"
  ),
  "signing service (packaging/signing/Entitlements.entitlements)": path.resolve(
    ROOT, "..", "..", "packaging", "signing", "Entitlements.entitlements"
  ),
};

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

  for (const [lane, file] of Object.entries(ENTITLEMENT_LANES)) {
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

// macOS 15 (Sequoia) gates local-network access behind TCC for EVERY app, not
// just sandboxed ones. An app that declares no local-network intent gets no
// prompt and no row in Privacy & Security -> Local Network, so there is nothing
// for the user to allow and `tccutil reset LocalNetwork <bundle-id>` fails with
// no record to reset. In that state unicast connections from the app's process
// subtree to non-gateway RFC1918 addresses fail INSTANTLY with EHOSTUNREACH,
// which reads as a routing fault rather than a denied permission. That was the
// shipped state: extendInfo declared the microphone and nothing else.
//
// This is the same failure SHAPE as the dead microphone above — capability
// refused, no prompt, no toggle — but NOT the same mechanism, and the difference
// is the whole point of these tests. Local network on a non-sandboxed
// hardened-runtime app is TCC-only: the Info.plist usage string is the entire
// declaration, and there is no entitlement to add. Two neighbouring keys look
// like they would help and must stay OUT:
//
//   * the multicast entitlement is for multicast/broadcast, needs an
//     Apple-granted provisioning profile, and signing with it unprovisioned
//     fails outright;
//   * the App-Sandbox network-client entitlement only means anything under App
//     Sandbox, which this app does not use — shipping it implies a sandbox the
//     bundle is not built for.
//
// So the assertions run in both directions: the usage string must be present,
// and neither cargo-culted entitlement may appear in either signing lane.
describe("macOS local network privacy declaration", () => {
  const MULTICAST = "com.apple.developer.networking.multicast";
  const SANDBOX_NETWORK_CLIENT = "com.apple.security.network.client";

  it("declares local-network intent with real prompt copy", () => {
    // Without this key macOS shows no prompt and creates no TCC record, which
    // is precisely the state where the user cannot grant access even though the
    // System Settings pane exists. As with the mic, assert the VALUE: macOS
    // refuses an empty purpose string, so a present-but-blank key ships the bug.
    const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
    const usage = (pkg.build.mac.extendInfo || {}).NSLocalNetworkUsageDescription;
    assert.equal(
      typeof usage,
      "string",
      "NSLocalNetworkUsageDescription must be declared, or LAN access is denied " +
        "with no prompt and no Privacy & Security row to flip"
    );
    assert.ok(
      usage.trim().length >= 20,
      "must be real prompt copy explaining WHY the local network is used"
    );
    // Electron's own bundled Info.plist carries generic boilerplate of the form
    // "This app needs access to …" for keys nobody wrote copy for. Shipping that
    // as a user-facing security prompt is the failure this guards.
    assert.equal(
      /this app needs access to/i.test(usage),
      false,
      "must not ship Electron's generic boilerplate as the prompt copy"
    );
  });

  for (const [lane, file] of Object.entries(ENTITLEMENT_LANES)) {
    it(`adds no local-network entitlement in the ${lane} lane`, () => {
      // Checked as parsed keys, not substrings: the comments in these files and
      // in this test MENTION both names to explain why they are absent, and a
      // substring test would fail on the very prose documenting the rule.
      const { entitlements } = parseEntitlements(fs.readFileSync(file, "utf8"));
      assert.notEqual(
        entitlements[MULTICAST],
        true,
        `${file} must not request ${MULTICAST} — it needs an Apple-granted ` +
          "provisioning profile and breaks signing when unprovisioned; plain " +
          "unicast LAN access does not need it"
      );
      assert.notEqual(
        entitlements[SANDBOX_NETWORK_CLIENT],
        true,
        `${file} must not request ${SANDBOX_NETWORK_CLIENT} — it is an ` +
          "App-Sandbox key and this bundle is not sandboxed"
      );
    });
  }
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

  it("gives nightly its own Windows appId so a channel update cannot orphan the other's shortcuts", () => {
    // WHY THIS IS A WINDOWS-ONLY IDENTITY, SEPARATE FROM THE SHARED macOS ONE.
    //
    // The shared appId is deliberate on macOS: Squirrel.Mac validates an update
    // against the host app's designated requirement, which pins the bundle id,
    // so splitting it would strand every installed mac app. NSIS has no such
    // constraint -- it keys install identity off nsis.guid and productFilename,
    // both of which nightly already overrides.
    //
    // On Windows the shared id is actively harmful, because ${APP_ID} reaches
    // TWO shell registrations that are global per-id rather than per-install:
    //
    //   WinShell::SetLnkAUMI       stamps the AppUserModelID onto both the
    //                              desktop and Start Menu shortcuts
    //   WinShell::UninstAppUserModelId  removes that registration wholesale
    //
    // The damaging path is a real UNINSTALL, not the update path: because
    // nsis.allowToChangeInstallationDirectory is false, the
    // allowToChangeInstallationDirectory define is never emitted, so
    // setIsTryToKeepShortcuts always yields "true" and an update runs the old
    // uninstaller with --keep-shortcuts, which skips the block below. Uninstall
    // one channel, though, and WinShell::UninstAppUserModelId runs against the
    // id BOTH channels share -- deregistering the AppUserModelID the surviving
    // channel's shortcuts still carry. Its desktop shortcut then resolves to a
    // dead registration and the shell reports that app as relocated or missing
    // even though its .exe is untouched.
    //
    // main.js already splits the RUNTIME id (app.setAppUserModelId picks
    // com.amazon.kiro.crew.nightly for a nightly stamp). Leaving the PACKAGED
    // id shared makes the two disagree: the app claims one identity at runtime
    // while its own shortcuts were stamped with the other.
    const buildScript = fs.readFileSync(
      path.resolve(ROOT, "..", "..", "packaging", "build-desktop.sh"),
      "utf8"
    );
    assert.ok(
      buildScript.includes("-c.win.appId=com.amazon.kiro.crew.nightly"),
      "build-desktop.sh must give the nightly channel its own WINDOWS appId, or " +
        "uninstalling one channel deregisters the other channel's shortcut " +
        "AppUserModelID and Windows reports that app as relocated"
    );
    // It must stay WINDOWS-scoped: appInfo.id prefers the platform-specific
    // value, so a top-level override would change the macOS bundle id too and
    // break Squirrel.Mac's designated-requirement check on every installed mac.
    assert.ok(
      !buildScript.includes("-c.appId=com.amazon.kiro.crew.nightly"),
      "the nightly appId override must be win-scoped; a top-level appId would " +
        "also move the macOS bundle id and strand installed mac updates"
    );
    // The runtime id main.js claims for a nightly build must equal the one the
    // installer stamps, or the shortcuts and the process disagree again.
    assert.ok(
      main.includes("com.amazon.kiro.crew.nightly"),
      "main.js must claim the same nightly AppUserModelID the installer stamps"
    );
    // And the shared production id must remain the mac/appId default.
    assert.equal(electronPkg.build.appId, "com.amazon.kiro.crew");
  });
});
