"use strict";

// The `pageRaised` flag on the fullscreen-changed send tells the renderer that a
// page's video, not the user, made the window fullscreen, so the "Press F11"
// hint stays away. Windows and Linux emit the window's enter-full-screen before
// the page's enter-html-full-screen, so the entry send must wait for the claim.
// These drive a real dashboard window through both orders.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { wireDashboardWindow } = require("./_window-harness");

// The deferred send and html-fullscreen.js's claim both settle on setImmediate;
// one queued after them runs once both have.
const afterImmediates = () => new Promise((resolve) => setImmediate(resolve));

async function fullScreenSends(t, platform) {
  const h = await wireDashboardWindow(t, { platform });
  let fullScreen = false;
  h.win.isFullScreen = () => fullScreen;
  const sends = [];
  h.viewContents.send = (channel, isFullScreen, info) => {
    if (channel === "fullscreen-changed") sends.push({ isFullScreen, pageRaised: info.pageRaised });
  };
  const enter = async ({ byVideo }) => {
    fullScreen = true;
    h.win.emit("enter-full-screen");
    if (byVideo) h.viewContents.emit("enter-html-full-screen");
    await afterImmediates();
  };
  return { sends, enter };
}

describe("fullscreen-changed pageRaised", () => {
  for (const platform of ["win32", "linux"]) {
    it(`${platform}: a video's fullscreen is sent as page-raised, window event first`, async (t) => {
      const { sends, enter } = await fullScreenSends(t, platform);
      await enter({ byVideo: true });
      assert.deepEqual(sends, [{ isFullScreen: true, pageRaised: true }]);
    });

    it(`${platform}: the user's F11 fullscreen is not page-raised`, async (t) => {
      const { sends, enter } = await fullScreenSends(t, platform);
      await enter({ byVideo: false });
      assert.deepEqual(sends, [{ isFullScreen: true, pageRaised: false }]);
    });
  }
});
