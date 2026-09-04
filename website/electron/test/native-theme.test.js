"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { resolveThemeSource } = require("../native-theme");

test("Auto maps to 'system' regardless of what it resolved to", () => {
  // The regression this guards: returning the RESOLVED mode here pins
  // prefers-color-scheme and freezes Auto at its first-load value.
  assert.equal(resolveThemeSource("system", "dark"), "system");
  assert.equal(resolveThemeSource("system", "light"), "system");
});

test("explicit preferences are forwarded verbatim", () => {
  assert.equal(resolveThemeSource("dark", "dark"), "dark");
  assert.equal(resolveThemeSource("light", "light"), "light");
});

test("preference wins over a disagreeing resolved mode", () => {
  assert.equal(resolveThemeSource("light", "dark"), "light");
  assert.equal(resolveThemeSource("dark", "light"), "dark");
});

test("missing preference falls back to the resolved mode", () => {
  // Older dashboard bundle with no data-mode-pref: keep the pre-fix behaviour
  // rather than silently forcing 'system'.
  assert.equal(resolveThemeSource("", "dark"), "dark");
  assert.equal(resolveThemeSource(undefined, "light"), "light");
});

test("unusable input defaults to 'system'", () => {
  assert.equal(resolveThemeSource("", ""), "system");
  assert.equal(resolveThemeSource(null, null), "system");
  assert.equal(resolveThemeSource("Dark", "Light"), "system");
  assert.equal(resolveThemeSource({}, []), "system");
});
