"use strict";

// Registered via `node --require` in package.json's `test` script, so this
// runs before ANY test file (or the source it requires) can `require("electron")`.
//
// On a plain `node --test` run there is no real Electron process, so
// `node_modules/electron/index.js` only ever runs its OWN getElectronPath()
// logic to resolve the path to the Electron *binary* — it is never the
// Electron runtime's own module, so every named export (app, BrowserWindow,
// ipcMain, ...) source files destructure from `require("electron")` is
// `undefined` here regardless of what we do. Tests already work around that by
// injecting their own fakes for what they need; what THIS file protects is the
// bare `require("electron")` call not being allowed to reach the network.
//
// getElectronPath() downloads the real binary (via install.js, over the
// network, writing into node_modules/electron/dist) whenever it needs to
// resolve a dist path and the target file is missing — which is exactly what
// happens on a fresh `npm ci`: the `electron` package ships no postinstall any
// more, so `dist/` does not exist until something first resolves the module.
// With multiple test files requiring "electron" concurrently under `node
// --test`'s parallel workers, several of them raced that download and the
// underlying extraction stepped on itself ("File exists (os error 17)").
//
// Setting ELECTRON_OVERRIDE_DIST_PATH short-circuits getElectronPath() before
// it ever looks at dist/ or path.txt, so it returns a path string without
// touching the network — see node_modules/electron/index.js. The directory
// need not exist or contain a real binary: nothing under test actually spawns
// this path, only requires the module for its exports.
if (!process.env.ELECTRON_OVERRIDE_DIST_PATH) {
  const os = require("os");
  const path = require("path");
  process.env.ELECTRON_OVERRIDE_DIST_PATH = path.join(
    os.tmpdir(),
    "kirocrew-electron-test-stub"
  );
}
