// The inert dependency object `electron/app-menu.js` `buildMenuTemplate`
// destructures, shared by the Electron-shell screenshot harness and its test
// (#11737).
//
// One copy, on purpose. Two copies existed first, and only the test's copy runs
// in CI: a dependency added to the template would have been caught there while
// the harness's copy went on building a menu with an undefined click handler,
// discovered by whoever next ran the harness by hand. A single module means a
// missing key is one failure in one place.
//
// CommonJS because the harness main process is CommonJS (Electron requires it
// that way) and ESM can import CommonJS, so this is the only module format both
// sides can reach.
//
// Every callback is a no-op. A still picture never activates an item, and the
// harness must not be able to reach into the product's behaviour.

/** Does nothing, deliberately: nothing in a screenshot is ever clicked. */
const noop = () => {};

/**
 * Every dependency the menu template destructures, spelled out.
 *
 * Named explicitly rather than built from a key list, so a dependency added to
 * the template shows up here as an obviously missing line rather than as an
 * `undefined` that only fails when something calls it.
 *
 * @param {boolean} isMac build the macOS arm of the template
 */
function menuDeps(isMac) {
  return {
    isMac,
    appName: "Kiro Crew",
    openSettings: noop,
    openAbout: noop,
    reload: noop,
    forceReload: noop,
    toggleDevTools: noop,
    zoomActualSize: noop,
    zoomIn: noop,
    zoomOut: noop,
    alwaysOnTop: false,
    toggleAlwaysOnTop: noop,
    openNewSessionWindow: noop,
    openNewConnectionWindow: noop,
    renameCurrentWindow: noop,
    promptRemoteHost: noop,
    refreshToken: noop,
    openConfigFile: noop,
  };
}

module.exports = { menuDeps };
