"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  EXPECTED_APP_BUILDER_VERSION,
  HOOK_MARKER,
  patchTemplateText,
} = require("../scripts/patch-nsis-template");

const upstreamBlock = [
  "!macro extractUsing7za FILE",
  "    # Attempt to copy files in atomic way",
  '    CopyFiles /SILENT "$PLUGINSDIR\\7z-out\\*" $OUTDIR',
  "    IfErrors 0 DoneExtract7za",
  "!macroend",
].join("\n");

test("the NSIS template patch inserts one guarded publish hook", () => {
  const result = patchTemplateText(upstreamBlock);

  assert.equal(result.changed, true);
  assert.equal(result.text.match(new RegExp(HOOK_MARKER, "g")).length, 1);
  assert.match(
    result.text,
    /!insertmacro customPublishAppPackage "\$PLUGINSDIR\\7z-out" "\$OUTDIR"/
  );
  assert.match(result.text, /!else[\s\S]*?CopyFiles \/SILENT/);
  assert.match(result.text, /!endif[\s\S]*?IfErrors 0 DoneExtract7za/);
});

test("the NSIS template patch is idempotent", () => {
  const once = patchTemplateText(upstreamBlock);
  const twice = patchTemplateText(once.text);

  assert.equal(twice.changed, false);
  assert.equal(twice.text, once.text);
});

test("the NSIS template patch fails closed when upstream drifts", () => {
  assert.throws(
    () => patchTemplateText("!macro extractUsing7za FILE\n!macroend\n"),
    /template drift/
  );
  assert.throws(
    () => patchTemplateText(`before\n${HOOK_MARKER}\nafter\n`),
    /incomplete or duplicated/
  );
  assert.equal(EXPECTED_APP_BUILDER_VERSION, "26.15.3");
});

test("directory publication preserves per-machine ACL inheritance", () => {
  const installer = fs.readFileSync(
    path.join(__dirname, "..", "build", "installer.nsh"),
    "utf8"
  );
  const publishMacro = installer.match(
    /!macro customPublishAppPackage[\s\S]*?!macroend/
  )?.[0];

  assert.ok(publishMacro);
  const guardStart = publishMacro.indexOf('${If} $installMode != "all"');
  const resourcesRename = publishMacro.indexOf(
    'Rename "${SOURCE}\\resources"'
  );
  const guardEnd = publishMacro.lastIndexOf("${EndIf}");
  const fallbackCopy = publishMacro.indexOf("CopyFiles /SILENT");
  for (const index of [guardStart, resourcesRename, guardEnd, fallbackCopy]) {
    assert.notEqual(index, -1);
  }
  assert.ok(guardStart < resourcesRename);
  assert.ok(resourcesRename < guardEnd);
  assert.ok(guardEnd < fallbackCopy);
});
