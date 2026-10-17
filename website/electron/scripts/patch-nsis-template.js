"use strict";

// electron-builder's differential-aware NSIS path extracts the complete app
// archive under $PLUGINSDIR and then CopyFiles the whole tree into $INSTDIR.
// Kiro Crew's bundled Python backend is thousands of small files, so that
// second pass dominates Windows install/update time (and makes Defender scan
// the payload twice). electron-builder exposes hooks before and after this
// operation, but none at the publish boundary itself. Keep the dependency pin
// and make the smallest possible, fail-closed template patch: custom installers
// may publish the staged tree efficiently, while every other installer keeps
// upstream's CopyFiles behavior.

const fs = require("node:fs");
const path = require("node:path");

const EXPECTED_APP_BUILDER_VERSION = "26.15.3";
const HOOK_MARKER = "!ifmacrodef customPublishAppPackage";

const ORIGINAL_LINES = [
  "    # Attempt to copy files in atomic way",
  '    CopyFiles /SILENT "$PLUGINSDIR\\7z-out\\*" $OUTDIR',
];

const PATCHED_LINES = [
  "    # Let a product publish large staged directories without copying every file twice.",
  `    ${HOOK_MARKER}`,
  '      !insertmacro customPublishAppPackage "$PLUGINSDIR\\7z-out" "$OUTDIR"',
  "    !else",
  "      # Upstream fallback for products without the custom publish hook.",
  '      CopyFiles /SILENT "$PLUGINSDIR\\7z-out\\*" $OUTDIR',
  "    !endif",
];

function patchTemplateText(source) {
  const eol = source.includes("\r\n") ? "\r\n" : "\n";
  if (source.includes(HOOK_MARKER)) {
    const patched = PATCHED_LINES.join(eol);
    const occurrences = source.split(patched).length - 1;
    if (occurrences !== 1) {
      throw new Error(
        `electron-builder NSIS template drift: publish hook is incomplete or duplicated (${occurrences})`
      );
    }
    return { text: source, changed: false };
  }

  const original = ORIGINAL_LINES.join(eol);
  const occurrences = source.split(original).length - 1;
  if (occurrences !== 1) {
    throw new Error(
      `electron-builder NSIS template drift: expected one publish block, found ${occurrences}`
    );
  }
  return {
    text: source.replace(original, PATCHED_LINES.join(eol)),
    changed: true,
  };
}

function patchInstalledTemplate() {
  const packageJsonPath = require.resolve("app-builder-lib/package.json");
  const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, "utf8"));
  if (packageJson.version !== EXPECTED_APP_BUILDER_VERSION) {
    throw new Error(
      `refusing to patch app-builder-lib ${packageJson.version}; expected ${EXPECTED_APP_BUILDER_VERSION}`
    );
  }

  const templatePath = path.join(
    path.dirname(packageJsonPath),
    "templates",
    "nsis",
    "include",
    "extractAppPackage.nsh"
  );
  const result = patchTemplateText(fs.readFileSync(templatePath, "utf8"));
  if (result.changed) {
    fs.writeFileSync(templatePath, result.text, "utf8");
    process.stdout.write("Patched electron-builder's NSIS app publish hook.\n");
  } else {
    process.stdout.write("electron-builder's NSIS app publish hook is already patched.\n");
  }
}

if (require.main === module) {
  patchInstalledTemplate();
}

module.exports = {
  EXPECTED_APP_BUILDER_VERSION,
  HOOK_MARKER,
  patchTemplateText,
};
