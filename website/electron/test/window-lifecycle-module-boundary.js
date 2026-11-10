"use strict";

function register(deps) {
(0, deps.describe)("window lifecycle module boundary", () => {
  (0, deps.it)("loads in plain Node and never requires Electron at module scope", () => {
    deps.assert.doesNotMatch(
      deps.SOURCE,
      /require\(\s*["']electron["']\s*\)/,
      "Electron must come from the factory argument so node:test can load this module",
    );
    deps.assert.equal(typeof deps.createWindowLifecycle, "function");
  });

  (0, deps.it)("composes its runtime owners, none of which loads Electron or anchors on its own directory", () => {
    // Electron arrives only through the factory argument, and every asset path
    // (preload, icons, tray template) stays anchored on the facade's directory.
    const owners = deps.fs.readdirSync(deps.RUNTIME_DIR).filter((name) => name.endsWith(".js")).sort();
    deps.assert.deepEqual(owners, [
      "browser-panels.js",
      "chrome.js",
      "contents.js",
      "linux-captions.js",
      "prompts.js",
      "remote-host.js",
      "session-security.js",
    ]);
    for (const owner of owners) {
      const source = (0, deps.readSource)(deps.path.join(deps.RUNTIME_DIR, owner), "utf8");
      deps.assert.doesNotMatch(source, /require\(\s*["']electron["']\s*\)/, `${owner} loads Electron`);
      deps.assert.doesNotMatch(source, /__dirname/, `${owner} must not resolve assets from runtime/window`);
      deps.assert.doesNotMatch(source, /require\(\s*"\.\.\/\.\.\/window-lifecycle"\s*\)/, `${owner} requires the facade`);
      const stem = owner.replace(/\.js$/, "");
      deps.assert.match(
        deps.SOURCE,
        new RegExp(`require\\("\\./runtime/window/${stem}"\\)`),
        `the facade composes ${owner} through an explicit, packaged require`,
      );
    }
  });

  (0, deps.it)("fails loudly for every required composition dependency", () => {
    const cases = [
      ["electron", /electron is required/],
      ["store", /store is required/],
      ["backendUrl", /backendUrl is required/],
      ["port", /port is required/],
      ["mintLocalToken", /mintLocalToken is required/],
      ["fetchRemoteToken", /fetchRemoteToken is required/],
      ["requestQuit", /requestQuit is required/],
      ["connectWindow", /connectWindow is required/],
    ];

    for (const [key, expected] of cases) {
      const options = (0, deps.validOptions)();
      delete options[key];
      deps.assert.throws(
        () => (0, deps.createWindowLifecycle)(options),
        expected,
        `${key} must not silently degrade`,
      );
    }
    deps.assert.doesNotThrow(() => (0, deps.createWindowLifecycle)((0, deps.validOptions)()));
  });
});
}

module.exports = { register };
