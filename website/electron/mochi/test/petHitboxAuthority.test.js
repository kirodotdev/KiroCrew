/**
 * One pet, N overlays: only the ACTIVE overlay may describe the pet.
 *
 * There is one `petHitbox` slot and one overlay window per display, all running
 * the same renderer. When a second overlay reports its own position the two
 * writers overwrite each other, the hit poll alternates between two boxes, and
 * click-through flips on every tick — the pet stops accepting clicks, and on the
 * interactive half of the flip a full-screen transparent window swallows input
 * meant for other applications. Nothing is logged, because from the poll's point
 * of view every frame is a legitimate state change.
 *
 * The same shape of bug applied to the display cache: `displays-info` is
 * broadcast to every overlay, each used to POST its OWN display as the active
 * one, so the pet believed whichever window's request landed last and reported
 * the wrong monitor.
 *
 * These are SOURCE guards, not behaviour tests: `petOverlays.js` needs a live
 * Electron `screen`/`ipcMain` to import, so the handlers cannot be invoked here.
 * They prove the authority check is still in place and still on the right side of
 * the branch; they cannot prove the runtime outcome.
 */

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const OVERLAYS = fs.readFileSync(
  path.join(__dirname, "..", "petOverlays.js"),
  "utf8",
);
const PRELOAD = fs.readFileSync(
  path.join(__dirname, "..", "pet-preload.js"),
  "utf8",
);

test("hitbox frames from a non-active overlay are dropped", () => {
  const handler = OVERLAYS.match(
    /ipcMain\.on\("mochi-pet:update-hitbox",[\s\S]*?\n {2}\}\);/,
  );
  assert.ok(handler, "update-hitbox handler not found — did it move?");
  assert.match(
    handler[0],
    /if \(!isActiveSender\(e\)\) return;/,
    "an inactive overlay must not be able to overwrite the pet hitbox",
  );
});

test("menu hitbox frames are gated the same way", () => {
  const handler = OVERLAYS.match(
    /ipcMain\.on\("mochi-pet:menu-hitbox",[\s\S]*?\n {2}\}\);/,
  );
  assert.ok(handler, "menu-hitbox handler not found — did it move?");
  assert.match(handler[0], /if \(!isActiveSender\(e\)\) return;/);
});

test("authority is decided by webContents identity, not by the payload", () => {
  const fn = OVERLAYS.match(/function isActiveSender\(event\) \{[\s\S]*?\n\}/);
  assert.ok(fn, "isActiveSender not found");
  assert.match(
    fn[0],
    /event\.sender\.id === win\.webContents\.id/,
    "a renderer cannot be trusted to know whether it won the race",
  );
  assert.match(fn[0], /activeDisplayId === null.*return false/s, "no active display => no authority");
});

test("a display handoff clears the outgoing owner's hitbox", () => {
  const fn = OVERLAYS.match(
    /function transferPetToDisplay\([\s\S]*?\n {2}const newWin = overlays\.get\(targetDisplayId\);/,
  );
  assert.ok(fn, "transferPetToDisplay body not found");
  assert.match(
    fn[0],
    /petHitbox = null;/,
    "the old box describes another monitor; the poll must fall back to click-through",
  );
});

test("displays-info carries the real active display, not just the receiver's own", () => {
  // Every send site must pass activeDisplayId, or the receiving overlay has no
  // way to distinguish "my display" from "the pet's display".
  const sends = OVERLAYS.match(/mochi-pet:displays-info[\s\S]{0,160}?\);/g) || [];
  assert.ok(sends.length >= 3, `expected >=3 displays-info sends, found ${sends.length}`);
  for (const send of sends) {
    assert.match(send, /activeDisplayId/, `a displays-info send omits activeDisplayId: ${send}`);
  }
  assert.match(
    PRELOAD,
    /\(_e, displays, myDisplayId, activeDisplayId\) =>\s*\n?\s*cb\(displays, myDisplayId, activeDisplayId\)/,
    "the preload must forward the active id through to the renderer",
  );
});
