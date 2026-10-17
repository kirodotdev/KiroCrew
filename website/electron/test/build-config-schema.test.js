"use strict";

// Validates package.json's "build" field against electron-builder's OWN schema,
// using electron-builder's OWN validator.
//
// Why this exists: electron-builder validates the config only when it packages,
// so a misplaced key is invisible to `tsc`, vitest and the node:test suite and
// surfaces as three simultaneously red Build Desktop jobs -- macOS and Linux
// included, because validation happens before any platform branch. The failure
// text is also actively misleading: an unknown key under `win` is reported as
// "configuration.win should be one of these: null", which names neither the
// offending key nor the place it belongs.
//
// electron-builder's WindowsConfiguration (and its siblings) are
// `additionalProperties: false`, so this catches the whole class: a key that
// moved between major versions, a typo, a key placed at the wrong nesting level.
// It calls app-builder-lib's `validateSchema` rather than driving ajv directly
// so the check cannot diverge from what the build itself enforces.

const test = require("node:test");
const assert = require("node:assert");

const { validateSchema } = require("app-builder-lib/out/util/config/schemaValidator");
const schema = require("app-builder-lib/scheme.json");
const buildConfig = require("../package.json").build;

test("the electron-builder build config satisfies the installed schema", () => {
  // Throws with electron-builder's own formatted diagnostics when invalid.
  validateSchema(schema, buildConfig, { name: "Configuration" });
});

test("publisherName sits where the signature-verification chain reads it", () => {
  // publisherName belongs to WindowsSigntoolConfiguration, not
  // WindowsConfiguration. Placement is not cosmetic: it is the only path
  // WindowsSignToolManager.computedPublisherName reads, PublishManager copies
  // that into app-update.yml, and app-update.yml is the only thing
  // NsisUpdater.verifySignature consults. An absent value there makes the
  // updater return early and SKIP verification rather than fail, so a
  // misplaced key would quietly cost the fail-closed Authenticode check that
  // the Windows publish lane is built around.
  const win = buildConfig.win;
  assert.ok(win, "the win build config is gone");
  assert.strictEqual(
    win.publisherName,
    undefined,
    "publisherName must live in win.signtoolOptions; at win level the schema " +
      "rejects the whole config and every desktop build fails",
  );
  assert.deepStrictEqual(win.signtoolOptions.publisherName, ["Amazon Web Services, Inc."]);
  // verifyUpdateCodeSignature defaults on (isForceCodeSigningVerification is
  // `!== false`). Setting it false drops publisherName from app-update.yml and
  // disables the check.
  assert.notStrictEqual(win.verifyUpdateCodeSignature, false);
});
