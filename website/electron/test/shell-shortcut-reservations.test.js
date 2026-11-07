/**
 * shell-shortcut-reservations: one physical chord per spelling, per platform,
 * and a change notice the window host acts on synchronously.
 */

const test = require("node:test");
const assert = require("node:assert");

const { createShortcutReservations, physicalChord } = require("../shell-shortcut-reservations");

test("two spellings of one chord are one chord, per platform", () => {
  assert.strictEqual(
    physicalChord("CommandOrControl+Shift+M", "darwin"),
    physicalChord("shift+cmd+m", "darwin"),
  );
  assert.strictEqual(
    physicalChord("CommandOrControl+Shift+M", "linux"),
    physicalChord("Ctrl+Shift+m", "linux"),
  );
  assert.notStrictEqual(
    physicalChord("CommandOrControl+Shift+M", "linux"),
    physicalChord("Super+Shift+M", "linux"),
  );
  assert.strictEqual(physicalChord("", "darwin"), "");
  assert.strictEqual(physicalChord("Hyper+M", "darwin"), "");
});

test("a reservation is replaced per owner and notifies only on a change", () => {
  const r = createShortcutReservations({ platform: "linux" });
  let notices = 0;
  r.onChange(() => { notices += 1; });
  r.reserve("a", ["Alt+Shift+M", ""]);
  assert.strictEqual(r.isReserved("Shift+Alt+m"), true);
  r.reserve("a", ["Shift+Alt+M"]);
  assert.strictEqual(notices, 1);
  r.reserve("b", ["Alt+Shift+K"]);
  r.reserve("a", ["Alt+Shift+H"]);
  assert.strictEqual(r.isReserved("Alt+Shift+M"), false);
  assert.strictEqual(r.isReserved("Alt+Shift+K"), true);
  assert.strictEqual(notices, 3);
  r.reserve("a", []);
  r.reserve("a", []);
  assert.strictEqual(r.isReserved("Alt+Shift+H"), false);
  assert.strictEqual(notices, 4);
});

test("a listener that throws does not stop the reservation", () => {
  const r = createShortcutReservations({ platform: "darwin" });
  r.onChange(() => { throw new Error("boom"); });
  r.reserve("a", ["Cmd+Alt+P"]);
  assert.strictEqual(r.isReserved("CommandOrControl+Alt+P"), true);
});
