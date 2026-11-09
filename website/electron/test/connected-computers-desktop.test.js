"use strict";

// Issue 29949 (G1): the native menu's share, status and Off for a window on a
// Remote Crew, served by the supervised desktop host. Authored before the
// module; not run locally (host rule), CI runs it.

const test = require("node:test");
const assert = require("node:assert/strict");

const { createDesktopSharing, menuVisibility } = require("../connected-computers-desktop");

function fakeHost({ state = "running", protection = "login Keychain", replies = {} } = {}) {
  const calls = [];
  return {
    calls,
    state: () => state,
    protection: () => protection,
    async request(op, fields = {}, opts = {}) {
      calls.push({ op, fields, opts });
      const reply = replies[op];
      return typeof reply === "function" ? reply(fields) : reply || { ok: false, code: "unexpected" };
    },
  };
}

function fakeDialog(answers) {
  const shown = [];
  const queue = [...answers];
  const next = (kind, opts) => {
    shown.push({ kind, opts });
    const answer = queue.shift();
    if (answer === undefined) throw new Error(`no scripted answer for ${kind}: ${opts.message || opts.title}`);
    return Promise.resolve(answer);
  };
  return {
    shown,
    showMessageBox: (_win, opts) => next("message", opts || _win),
    showOpenDialog: (_win, opts) => next("open", opts || _win),
  };
}

const DESCRIBED = {
  ok: true,
  consent: "tok-1",
  instance: { id: "desktop-7778", name: "Cloud crew", ssh_host: "clouddesk" },
  mcp: [{ name: "notes" }, { name: "secret", refused: "turned off locally" }],
};

function sharing(host, dialog, port = 7778) {
  return createDesktopSharing({ dialog, host, getWindow: () => null, getTargetPort: () => port });
}

test("B02: share asks natively, then sends exactly the picked folder and ticked tools", async () => {
  const host = fakeHost({
    replies: {
      describe: DESCRIBED,
      share: (f) => ({ ok: true, grant: { grant_id: "g1", root: f.root, gateway_label: "cloud" } }),
    },
  });
  const dialog = fakeDialog([
    { response: 0 }, // confirm
    { canceled: false, filePaths: ["/work/proj"] },
    { response: 0 }, // share "notes"
    { response: 0 }, // success notice
  ]);
  const result = await sharing(host, dialog).shareThisComputer();
  assert.equal(result.ok, true);
  assert.deepEqual(host.calls.map((c) => c.op), ["describe", "share"]);
  assert.deepEqual(host.calls[1].fields, {
    instance_id: "desktop-7778",
    consent: "tok-1",
    root: "/work/proj",
    mcp: ["notes"],
  });
  // The crew name shown came from the host (the app's own store), not a page.
  assert.match(dialog.shown[0].opts.detail, /Cloud crew/);
  // A refused tool is never offered.
  assert.equal(dialog.shown.filter((d) => /secret/.test(d.opts.message || "")).length, 0);
});

test("B02: cancelling the confirm or the picker sends no share", async () => {
  for (const answers of [[{ response: 1 }], [{ response: 0 }, { canceled: true, filePaths: [] }]]) {
    const host = fakeHost({ replies: { describe: DESCRIBED } });
    const result = await sharing(host, fakeDialog(answers)).shareThisComputer();
    assert.deepEqual(result, { ok: false, code: "cancelled" });
    assert.deepEqual(host.calls.map((c) => c.op), ["describe"]);
  }
});

test("B13: the target is this window's own connection id, never a page value", async () => {
  const host = fakeHost({ replies: { describe: { ok: false, code: "unknown_instance", error: "x" } } });
  const dialog = fakeDialog([{ response: 0 }]);
  const result = await sharing(host, dialog, 9001).shareThisComputer();
  assert.equal(result.code, "unknown_instance");
  assert.deepEqual(host.calls[0].fields, { instance_id: "desktop-9001" });
  assert.match(dialog.shown[0].opts.message, /not a Remote Crew/);
});

test("R2: no OS keyring means no dialog and no request", async () => {
  const host = fakeHost({ protection: "" });
  const dialog = fakeDialog([{ response: 0 }]);
  const result = await sharing(host, dialog).shareThisComputer();
  assert.equal(result.code, "protection_unavailable");
  assert.deepEqual(host.calls, []);
});

test("a refusal from the host is shown with its own words and reported", async () => {
  const host = fakeHost({
    replies: { describe: DESCRIBED, share: { ok: false, code: "refused", error: "your home folder cannot be shared whole" } },
  });
  const dialog = fakeDialog([
    { response: 0 },
    { canceled: false, filePaths: ["/home/me"] },
    { response: 1 },
    { response: 0 },
  ]);
  const result = await sharing(host, dialog).shareThisComputer();
  assert.deepEqual(result, { ok: false, code: "refused" });
  assert.match(dialog.shown[dialog.shown.length - 1].opts.detail, /home folder/);
});

test("a share that times out never claims success", async () => {
  const host = fakeHost({ replies: { describe: { ...DESCRIBED, mcp: [] }, share: { ok: false, code: "timeout" } } });
  const dialog = fakeDialog([{ response: 0 }, { canceled: false, filePaths: ["/w"] }, { response: 0 }]);
  const result = await sharing(host, dialog).shareThisComputer();
  assert.equal(result.ok, false);
  assert.match(dialog.shown[2].opts.detail, /Sharing Status/);
});

test("G2: a host that is not running explains itself and sends nothing", async () => {
  // "disabled" (the preview off here) included: Share never offers to turn it on.
  for (const state of ["disabled", "elsewhere", "failed", "starting", "unavailable"]) {
    const host = fakeHost({ state });
    const dialog = fakeDialog([{ response: 0 }]);
    const result = await sharing(host, dialog).shareThisComputer();
    assert.equal(result.ok, false);
    assert.equal(result.code, state);
    assert.deepEqual(host.calls, []);
  }
});

test("B07: status lists active grants and stops only the one chosen", async () => {
  const host = fakeHost({
    replies: {
      status: {
        ok: true,
        grants: [
          { grant_id: "g1", gateway_label: "cloud", root: "/a", state: "connected" },
          { grant_id: "g0", gateway_label: "old", root: "/z", status: "revoked", state: "revoked" },
          { grant_id: "g2", gateway_label: "lab", root: "/b", state: "connected" },
        ],
      },
      revoke: (f) => ({ ok: true, revoked: f.grant_id }),
    },
  });
  // Choose the second listed grant, confirm, then the result notice.
  const dialog = fakeDialog([{ response: 2 }, { response: 0 }, { response: 0 }]);
  await sharing(host, dialog).showSharingStatus();
  assert.match(dialog.shown[0].opts.detail, /\/a/);
  assert.doesNotMatch(dialog.shown[0].opts.detail, /\/z/);
  assert.deepEqual(host.calls.map((c) => c.op), ["status", "revoke"]);
  assert.deepEqual(host.calls[1].fields, { grant_id: "g2" });
});

test("B01: Off asks once, then turns off every grant", async () => {
  const host = fakeHost({ replies: { off: { ok: true, revoked: ["g1", "g2"] } } });
  const dialog = fakeDialog([{ response: 0 }, { response: 0 }]);
  const result = await sharing(host, dialog).turnOffSharing();
  assert.equal(result.ok, true);
  assert.deepEqual(host.calls.map((c) => c.op), ["off"]);
  assert.match(dialog.shown[1].opts.message, /2/);
});

test("B01: Off cancelled sends nothing", async () => {
  const host = fakeHost({ replies: { off: { ok: true, revoked: [] } } });
  await sharing(host, fakeDialog([{ response: 1 }])).turnOffSharing();
  assert.deepEqual(host.calls, []);
});

test("B01: Off with no host running points at the CLI and claims nothing", async () => {
  const host = fakeHost({ state: "failed" });
  const dialog = fakeDialog([{ response: 0 }]);
  const result = await sharing(host, dialog).turnOffSharing();
  assert.equal(result.ok, false);
  assert.match(dialog.shown[0].opts.detail, /kirocrew connected-computer off/);
});

test("FLAG: the menu shows sharing only while the host can serve it", () => {
  // The preview off on this computer ("disabled") hides every item too.
  for (const state of ["stopped", "unavailable", "disabled"]) {
    assert.deepEqual(menuVisibility(state), { share: false, status: false, off: false }, state);
  }
  for (const state of ["running", "starting", "restarting", "elsewhere", "failed"]) {
    assert.deepEqual(menuVisibility(state), { share: true, status: true, off: true }, state);
  }
});

test("FLAG: the preview off here only explains the switch; nothing is written or sent", async () => {
  const host = fakeHost({ state: "disabled" });
  const dialog = fakeDialog([{ response: 0 }]);
  const result = await sharing(host, dialog).shareThisComputer();
  assert.deepEqual(result, { ok: false, code: "disabled" });
  assert.deepEqual(host.calls, []);
  assert.equal(dialog.shown.length, 1);
  assert.deepEqual(dialog.shown[0].opts.buttons, ["OK"]);
  assert.match(dialog.shown[0].opts.message, /turned off/);
});
