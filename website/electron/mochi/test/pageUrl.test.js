/**
 * pageUrl — the one place that decides whether a Mochi window carries a token.
 *
 * The regression that matters most here is the LOCAL path: `self` must produce
 * exactly the URL the shell used before instance switching existed. If a token
 * ever leaked onto the local load, every local user would take a new session
 * cookie mint on every pet open.
 */
const test = require("node:test");
const assert = require("node:assert");

const { mochiPageUrl } = require("../pageUrl");

test("local gateway: no token, URL unchanged from the pre-switching shape", () => {
  assert.strictEqual(
    mochiPageUrl("http://localhost:5476", "pet.html"),
    "http://localhost:5476/app-windows/mochi/pet.html",
  );
  // An explicitly empty token is the `self` case, not "append nothing weird".
  assert.strictEqual(
    mochiPageUrl("http://localhost:5476", "panel.html", ""),
    "http://localhost:5476/app-windows/mochi/panel.html",
  );
});

test("remote instance: token rides on the first load", () => {
  assert.strictEqual(
    mochiPageUrl("http://localhost:7778", "pet.html", "abc123"),
    "http://localhost:7778/app-windows/mochi/pet.html?token=abc123",
  );
});

test("token is percent-encoded — an HMAC token can contain + / =", () => {
  const url = mochiPageUrl("http://localhost:7778", "settings.html", "a+b/c=d&e");
  assert.strictEqual(url, "http://localhost:7778/app-windows/mochi/settings.html?token=a%2Bb%2Fc%3Dd%26e");
  // The query must not be splittable into extra params by the token's contents.
  assert.strictEqual(new URL(url).searchParams.get("token"), "a+b/c=d&e");
});

test("a trailing slash on the origin does not produce a double slash", () => {
  assert.strictEqual(
    mochiPageUrl("http://localhost:5476/", "avatar.html"),
    "http://localhost:5476/app-windows/mochi/avatar.html",
  );
});

test("every Mochi page goes through the same builder", () => {
  // The app segment is the builder's, not the caller's: callers pass the bare
  // window name and every URL lands under the one app-window namespace. This is
  // what removed the old flat `/mochi-<name>.html`, where `<app>` and `<name>`
  // were joined by a hyphen and could no longer be told apart.
  for (const page of [
    "pet.html",
    "panel.html",
    "settings.html",
    "avatar.html",
  ]) {
    assert.strictEqual(
      mochiPageUrl("http://localhost:1", page),
      `http://localhost:1/app-windows/mochi/${page}`,
    );
    assert.strictEqual(
      mochiPageUrl("http://localhost:1", page, "t"),
      `http://localhost:1/app-windows/mochi/${page}?token=t`,
    );
  }
});
