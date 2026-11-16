"use strict";

// The module source and constructor options shared by the window-lifecycle*.test.js
// files. (Not a *.test.js file, so `node --test test/*.test.js` does not run it.)

const fs = require("node:fs");
const path = require("node:path");

const MODULE_PATH = path.join(__dirname, "..", "window-lifecycle.js");
// Normalize to LF regardless of the checkout's line-ending translation: the
// source-scanning regexes anchor on a literal "\n", and a Windows checkout
// with core.autocrlf on disk-translates the file to CRLF, which shifts every
// "}\n" anchor to "}\r\n" and fails the match on a file that is otherwise
// unchanged.
const SOURCE = fs.readFileSync(MODULE_PATH, "utf8").replace(/\r\n/g, "\n");

function validOptions(overrides = {}) {
  return {
    electron: {},
    store: { get: () => null },
    backendUrl: "http://localhost:5476",
    port: 5476,
    mintLocalToken: async () => "",
    fetchRemoteToken: async () => ({ token: "" }),
    requestQuit: () => {},
    connectWindow: async () => {},
    // Keep construction independent of the host running the suite.
    platform: "test",
    ...overrides,
  };
}

module.exports = { MODULE_PATH, SOURCE, validOptions };
