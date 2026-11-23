// The shared rule for when a cached gateway credential is re-resolved before
// the gateway's 300 s link window (LINK_WINDOW_SECS) would refuse it.
const test = require("node:test");
const assert = require("node:assert/strict");

const { LINK_TOKEN_REFRESH_MS, linkTokenIsStale } = require("../link-token-age");

const LINK = { value: "tok", viaCookie: false };

test("the refresh age stays below the gateway's 300 s link window", () => {
  assert.ok(LINK_TOKEN_REFRESH_MS < 300_000);
  assert.ok(LINK_TOKEN_REFRESH_MS >= 60_000, "not so short that it mints per poll");
});

test("a link token is fresh inside the refresh age and stale from it", () => {
  assert.equal(linkTokenIsStale(LINK, 1_000, 1_000), false);
  assert.equal(linkTokenIsStale(LINK, 1_000, 1_000 + LINK_TOKEN_REFRESH_MS - 1), false);
  assert.equal(linkTokenIsStale(LINK, 1_000, 1_000 + LINK_TOKEN_REFRESH_MS), true);
});

test("a wall clock that moved backwards makes a link token stale", () => {
  assert.equal(linkTokenIsStale(LINK, 1_000_000, 999_999), true);
});

test("a borrowed cookie and an empty credential are never aged out", () => {
  assert.equal(linkTokenIsStale({ value: "s", viaCookie: true }, 0, 86_400_000), false);
  assert.equal(linkTokenIsStale({ value: "" }, 0, 86_400_000), false);
  assert.equal(linkTokenIsStale(null, 0, 86_400_000), false);
});
