"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { clampBadgeCount, BADGE_MAX } = require("../badge");

test("positive integers pass through", () => {
  assert.equal(clampBadgeCount(1), 1);
  assert.equal(clampBadgeCount(42), 42);
});

test("zero and negatives clear the badge", () => {
  assert.equal(clampBadgeCount(0), 0);
  assert.equal(clampBadgeCount(-5), 0);
});

test("non-numeric input clears the badge", () => {
  assert.equal(clampBadgeCount(undefined), 0);
  assert.equal(clampBadgeCount(null), 0);
  assert.equal(clampBadgeCount("abc"), 0);
  assert.equal(clampBadgeCount(NaN), 0);
  assert.equal(clampBadgeCount(Infinity), 0);
  assert.equal(clampBadgeCount({}), 0);
});

test("numeric strings are accepted", () => {
  assert.equal(clampBadgeCount("3"), 3);
});

test("fractions floor and huge values cap", () => {
  assert.equal(clampBadgeCount(2.9), 2);
  assert.equal(clampBadgeCount(10 ** 9), BADGE_MAX);
});
