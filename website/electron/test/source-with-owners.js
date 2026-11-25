"use strict";

const fs = require("node:fs");
const path = require("node:path");

/** Read source contracts across a facade's call-time dependency-injected owners.
 * Runtime tests still load the real files; source checks see the owning body at
 * its facade declaration, with dependency accesses expressed as facade names.
 */
function readFileSync(filename, encoding) {
  const source = fs.readFileSync(filename, encoding);
  if (typeof source !== "string") return source;
  return source.replace(
    /return require\("(\.\/runtime\/[^"]+)"\)\.(\w+)\(\{[\s\S]*?\}, \.\.\.arguments\);/g,
    (_call, owner, name) => {
      const text = fs.readFileSync(path.resolve(path.dirname(filename), owner + ".js"), "utf8");
      const from = text.indexOf(") {", text.indexOf("function " + name + "("));
      const to = text.lastIndexOf("\n  }");
      if (from < 0 || to <= from) throw new Error("cannot locate owner body: " + owner);
      return "/* owner require(\"" + owner + "\") */" + text.slice(from + 3, to)
        .replace(/\(0, deps\.(\w+)\)/g, "$1")
        .replace(/deps\.dirname/g, "__dirname")
        .replace(/deps\.(\w+)/g, "$1");
    },
  );
}

module.exports = { readFileSync };
