const { test } = require("node:test");
const assert = require("node:assert");
const {
  decideLinuxFrame,
  normalizeFramelessOverride,
  prefersClientSideDecorations,
  isWaylandSession,
  desktopTokens,
  applyWindowControl,
} = require("../linux-frame");

// A GNOME Wayland session -- the canonical frameless target.
const GNOME_WAYLAND = { XDG_CURRENT_DESKTOP: "ubuntu:GNOME", XDG_SESSION_TYPE: "wayland" };

// ── desktopTokens ──

test("splits XDG_CURRENT_DESKTOP on colons and lowercases", () => {
  assert.deepStrictEqual(
    desktopTokens({ XDG_CURRENT_DESKTOP: "ubuntu:GNOME" }),
    ["ubuntu", "gnome"],
  );
});

test("folds all three XDG variables", () => {
  assert.deepStrictEqual(
    desktopTokens({
      XDG_CURRENT_DESKTOP: "KDE",
      XDG_SESSION_DESKTOP: "plasma",
      DESKTOP_SESSION: "plasmax11",
    }),
    ["kde", "plasma", "plasmax11"],
  );
});

test("empty env yields no tokens", () => {
  assert.deepStrictEqual(desktopTokens({}), []);
});

// ── prefersClientSideDecorations ──

test("GNOME-family desktops prefer CSD", () => {
  for (const desk of ["GNOME", "ubuntu:GNOME", "Unity", "Pantheon", "Budgie:GNOME"]) {
    assert.strictEqual(
      prefersClientSideDecorations({ XDG_CURRENT_DESKTOP: desk }),
      true,
      desk,
    );
  }
});

test("SSD desktops and tiling WMs do not prefer CSD", () => {
  for (const desk of ["KDE", "XFCE", "LXQt", "i3", "sway", "Hyprland", "MATE", "X-Cinnamon"]) {
    assert.strictEqual(
      prefersClientSideDecorations({ XDG_CURRENT_DESKTOP: desk }),
      false,
      desk,
    );
  }
});

test("hybrid token sets: an SSD/tiling token beats a GNOME token", () => {
  // Regolith is i3 running on a GNOME session; the tiling WM owns decorations.
  for (const desk of ["Regolith:GNOME", "GNOME-Flashback:GNOME:i3"]) {
    assert.strictEqual(
      prefersClientSideDecorations({ XDG_CURRENT_DESKTOP: desk }),
      false,
      desk,
    );
  }
});

test("headless (no desktop vars) does not prefer CSD", () => {
  assert.strictEqual(prefersClientSideDecorations({}), false);
});

// ── isWaylandSession ──

test("wayland session type is detected case-insensitively", () => {
  assert.strictEqual(isWaylandSession({ XDG_SESSION_TYPE: "wayland" }), true);
  assert.strictEqual(isWaylandSession({ XDG_SESSION_TYPE: "Wayland" }), true);
});

test("x11, tty, and absent session types are not wayland", () => {
  for (const t of ["x11", "tty", "", undefined]) {
    assert.strictEqual(isWaylandSession({ XDG_SESSION_TYPE: t }), false, String(t));
  }
});

// ── normalizeFramelessOverride: operator-editable JSON is untrusted ──

test("only literal booleans pass through; everything else is auto", () => {
  assert.strictEqual(normalizeFramelessOverride(true), true);
  assert.strictEqual(normalizeFramelessOverride(false), false);
  for (const junk of [null, undefined, "true", "false", 1, 0, "", {}, []]) {
    assert.strictEqual(normalizeFramelessOverride(junk), null, JSON.stringify(junk));
  }
});

// ── decideLinuxFrame ──

test("GNOME on Wayland goes frameless (kills the doubled title bar)", () => {
  const d = decideLinuxFrame({ env: GNOME_WAYLAND });
  assert.strictEqual(d.frameless, true);
  assert.strictEqual(d.reason, "csd-desktop");
});

test("GNOME on X11 keeps the native frame (frameless X11 loses edge-resize)", () => {
  const d = decideLinuxFrame({ env: { XDG_CURRENT_DESKTOP: "ubuntu:GNOME", XDG_SESSION_TYPE: "x11" } });
  assert.strictEqual(d.frameless, false);
  assert.strictEqual(d.reason, "not-wayland");
});

test("tiling WM keeps the native frame even on Wayland (fail-safe)", () => {
  const d = decideLinuxFrame({ env: { XDG_CURRENT_DESKTOP: "sway", XDG_SESSION_TYPE: "wayland" } });
  assert.strictEqual(d.frameless, false);
  assert.strictEqual(d.reason, "ssd-or-unknown-desktop");
});

test("unknown/headless environment keeps the native frame (fail-safe)", () => {
  const d = decideLinuxFrame({ env: {} });
  assert.strictEqual(d.frameless, false);
});

test("override=true forces frameless even on KDE/X11", () => {
  const d = decideLinuxFrame({ env: { XDG_CURRENT_DESKTOP: "KDE", XDG_SESSION_TYPE: "x11" }, override: true });
  assert.strictEqual(d.frameless, true);
  assert.strictEqual(d.reason, "override-frameless");
});

test("override=false forces native frame even on GNOME Wayland", () => {
  const d = decideLinuxFrame({ env: GNOME_WAYLAND, override: false });
  assert.strictEqual(d.frameless, false);
  assert.strictEqual(d.reason, "override-native-frame");
});

test("string override is ignored (auto)", () => {
  const d = decideLinuxFrame({ env: GNOME_WAYLAND, override: "false" });
  assert.strictEqual(d.frameless, true, "malformed override must not disable the heuristic");
});

test("no arguments at all keeps the native frame", () => {
  assert.strictEqual(decideLinuxFrame().frameless, false);
});

// ── applyWindowControl: the caption-control IPC dispatch ──

function fakeWin({ maximized = false, destroyed = false } = {}) {
  const calls = [];
  return {
    calls,
    isDestroyed: () => destroyed,
    isMaximized: () => maximized,
    minimize: () => calls.push("minimize"),
    maximize: () => calls.push("maximize"),
    unmaximize: () => calls.push("unmaximize"),
    close: () => calls.push("close"),
  };
}

test("minimize / close dispatch to the window", () => {
  const w = fakeWin();
  assert.strictEqual(applyWindowControl(w, "minimize"), true);
  assert.strictEqual(applyWindowControl(w, "close"), true);
  assert.deepStrictEqual(w.calls, ["minimize", "close"]);
});

test("maximize-toggle maximizes an unmaximized window and restores a maximized one", () => {
  const w1 = fakeWin({ maximized: false });
  assert.strictEqual(applyWindowControl(w1, "maximize-toggle"), true);
  assert.deepStrictEqual(w1.calls, ["maximize"]);

  const w2 = fakeWin({ maximized: true });
  assert.strictEqual(applyWindowControl(w2, "maximize-toggle"), true);
  assert.deepStrictEqual(w2.calls, ["unmaximize"]);
});

test("unknown or forged actions are no-ops that report false", () => {
  const w = fakeWin();
  for (const bad of ["destroy", "devtools", "", null, undefined, 42, {}]) {
    assert.strictEqual(applyWindowControl(w, bad), false, JSON.stringify(bad));
  }
  assert.deepStrictEqual(w.calls, []);
});

test("a destroyed or absent window is a no-op", () => {
  assert.strictEqual(applyWindowControl(fakeWin({ destroyed: true }), "close"), false);
  assert.strictEqual(applyWindowControl(null, "close"), false);
  assert.strictEqual(applyWindowControl({}, "close"), false);
});
