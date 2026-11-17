const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  describeChromiumSandboxOff,
  showChromiumSandboxNotice,
  DISMISSED_KEY,
  OTHER_CAUSES_URL,
} = require("../chromium-sandbox-notice");

const CLI = "/tmp/.mount_abc123/resources/backend-dist/kirocrew-backend/bin/kirocrew";
const APPIMAGE = "/home/u/Applications/kirocrew.AppImage";

// AppRun's launch on stock Ubuntu 24.04: the userns probe failed, so the
// switch is on argv, and the AppArmor restriction is the reason.
const ubuntuAppImage = {
  platform: "linux",
  env: { APPIMAGE },
  argv: ["/tmp/.mount_abc123/kirocrew-desktop", "--no-sandbox"],
  readSysctl: () => "1\n",
  cliBin: CLI,
};

function fakeStore(initial = {}) {
  const data = { ...initial };
  return { data, get: (k) => data[k], set: (k, v) => { data[k] = v; } };
}

function fakeDialog(answer) {
  const calls = [];
  return {
    calls,
    showMessageBox: async (...args) => {
      calls.push(args);
      return answer;
    },
  };
}

describe("describeChromiumSandboxOff", () => {
  it("explains the switch, the fix and the missing CLI in one notice", () => {
    const notice = describeChromiumSandboxOff(ubuntuAppImage);

    assert.ok(notice);
    assert.ok(notice.message.includes("sandbox turned off"));
    assert.ok(notice.detail.includes("FUSE"));
    assert.ok(notice.detail.includes("--no-sandbox"));
    assert.ok(notice.detail.includes("does not install a kirocrew command"));
    assert.equal(notice.command, `'${CLI}' sandbox install-profile --path '${APPIMAGE}'`);
    assert.ok(notice.detail.includes(notice.command));
    // The consequence and the action lead; the FUSE explanation follows.
    assert.ok(notice.detail.startsWith("Agents cannot run"));
    assert.ok(notice.detail.indexOf(notice.command) < notice.detail.indexOf("FUSE"));
  });

  // The .deb and .rpm put only `kirocrew-desktop` on PATH, so naming them as
  // the way to get a `kirocrew` command would send the user to another
  // `command not found`. The one-line install is what provides it.
  it("names the one-line install, not a package, as the way to get kirocrew", () => {
    for (const readSysctl of [() => "1\n", () => "0\n"]) {
      const { detail } = describeChromiumSandboxOff({ ...ubuntuAppImage, readSysctl });
      assert.ok(detail.includes("The one-line install puts kirocrew on your PATH"), detail);
      assert.equal(detail.includes("packages do"), false, detail);
    }
  });

  it("names no command when the restriction is not AppArmor's", () => {
    const notice = describeChromiumSandboxOff({ ...ubuntuAppImage, readSysctl: () => "0\n" });

    assert.ok(notice);
    assert.equal(notice.command, "");
    assert.ok(notice.detail.includes("--no-sandbox"));
    assert.ok(notice.detail.includes("does not install a kirocrew command"));
  });

  it("says nothing when the sandbox is on", () => {
    assert.equal(
      describeChromiumSandboxOff({ ...ubuntuAppImage, argv: ["/tmp/.mount_abc123/kirocrew-desktop"] }),
      null,
    );
  });

  it("says nothing off an AppImage or off Linux", () => {
    assert.equal(describeChromiumSandboxOff({ ...ubuntuAppImage, env: {} }), null);
    assert.equal(describeChromiumSandboxOff({ ...ubuntuAppImage, env: undefined }), null);
    assert.equal(describeChromiumSandboxOff({ ...ubuntuAppImage, platform: "darwin" }), null);
    assert.equal(describeChromiumSandboxOff({ ...ubuntuAppImage, argv: undefined }), null);
  });
});

describe("showChromiumSandboxNotice", () => {
  const notice = describeChromiumSandboxOff(ubuntuAppImage);
  const noCommand = describeChromiumSandboxOff({ ...ubuntuAppImage, readSysctl: () => "0\n" });
  const window = { isDestroyed: () => false };

  it("copies the command when the first button is pressed", async () => {
    const dialog = fakeDialog({ response: 0, checkboxChecked: false });
    const copied = [];
    const store = fakeStore();

    const result = await showChromiumSandboxNotice({
      notice, dialog, store, window,
      clipboard: { writeText: (t) => copied.push(t) },
      openExternal: () => assert.fail("no guide for a host with a command"),
    });

    assert.equal(result, "copied");
    assert.deepEqual(copied, [notice.command]);
    assert.equal(dialog.calls[0][0], window);
    assert.deepEqual(dialog.calls[0][1].buttons, ["Copy command", "Close"]);
    assert.equal(store.data[DISMISSED_KEY], undefined);
  });

  it("opens the guide when there is no command to copy", async () => {
    const opened = [];
    const result = await showChromiumSandboxNotice({
      notice: noCommand,
      dialog: fakeDialog({ response: 0, checkboxChecked: false }),
      store: fakeStore(),
      clipboard: { writeText: () => assert.fail("nothing to copy") },
      openExternal: (url) => { opened.push(url); },
    });

    assert.equal(result, "guide");
    assert.deepEqual(opened, [OTHER_CAUSES_URL]);
  });

  it("remembers a dismissal and stays quiet afterwards", async () => {
    const store = fakeStore();
    const deps = {
      notice, store, window,
      clipboard: { writeText: () => {} },
      openExternal: () => {},
    };

    assert.equal(
      await showChromiumSandboxNotice({ ...deps, dialog: fakeDialog({ response: 1, checkboxChecked: true }) }),
      "closed",
    );
    assert.equal(store.data[DISMISSED_KEY], true);

    const again = fakeDialog({ response: 0, checkboxChecked: false });
    assert.equal(await showChromiumSandboxNotice({ ...deps, dialog: again }), "dismissed");
    assert.equal(again.calls.length, 0);
  });

  it("shows nothing when there is no notice", async () => {
    const dialog = fakeDialog({ response: 0 });
    assert.equal(await showChromiumSandboxNotice({ notice: null, dialog, store: fakeStore() }), "none");
    assert.equal(dialog.calls.length, 0);
  });
});

// The helper is only useful if the shell calls it, and before the gateway boot:
// the reported window never got past the splash.
it("main.js shows the notice before the gateway starts", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  const createWindow = source.indexOf("const mainWindow = windows.createMainWindow();");
  const notice = source.indexOf("showChromiumSandboxNotice({", createWindow);
  const gatewayStart = source.indexOf("await gateway.start();", createWindow);

  assert.ok(createWindow >= 0, "expected the main window creation");
  assert.ok(notice > createWindow, "the notice must be wired after the window exists");
  assert.ok(gatewayStart > notice, "the notice must not wait for the gateway");
});
