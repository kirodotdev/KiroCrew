"use strict";

// The module source and constructor options shared by the window-lifecycle*.test.js
// files. (Not a *.test.js file, so `node --test test/*.test.js` does not run it.)

const path = require("node:path");
const { readFileSync: readSource } = require("./source-with-owners");

const MODULE_PATH = path.join(__dirname, "..", "window-lifecycle.js");
// The facade text with each runtime/ owner body it delegates to read in at the
// call, so a source pin finds the code where the facade declares it.
// Normalize to LF regardless of the checkout's line-ending translation: the
// source-scanning regexes anchor on a literal "\n", and a Windows checkout
// with core.autocrlf on disk-translates the file to CRLF, which shifts every
// "}\n" anchor to "}\r\n" and fails the match on a file that is otherwise
// unchanged.
const SOURCE = readSource(MODULE_PATH, "utf8").replace(/\r\n/g, "\n");

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
