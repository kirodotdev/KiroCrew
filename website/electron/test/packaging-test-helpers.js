// Shared by packaging.test.js and its packaging.*.test.js parts. (Not a
// *.test.js file, so `node --test test/*.test.js` does not run it.)
const assert = require("node:assert/strict");

function normalizedNsisBlock(source, pattern) {
  const match = source.match(pattern);
  assert.ok(match, `expected NSIS block matching ${pattern}`);
  return match[0]
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(Boolean)
    .join("\n");
}

module.exports = { normalizedNsisBlock };
