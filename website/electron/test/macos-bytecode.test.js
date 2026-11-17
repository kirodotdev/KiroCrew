const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");

it("ships macOS caches for the closure only and bars every other bytecode write", () => {
  // codesign seals every file under a .app's Contents/, so bytecode written
  // there after signing makes Gatekeeper refuse the app as "damaged". The
  // signing service also reads the whole tree on every request, so a cache
  // for every module costs signing time on every release. The macOS backend
  // therefore ships checked-hash caches for the standard library, kiro_crew
  // and the traced closure of its dependencies, and the bundled interpreter
  // refuses to write any other: a module outside that set compiles in memory.
  const buildScript = fs.readFileSync(
    path.join(REPO_ROOT, "packaging", "build-desktop.sh"),
    "utf8"
  );
  const bytecodeScript = fs.readFileSync(
    path.join(REPO_ROOT, "packaging", "desktop-bytecode.sh"),
    "utf8"
  );
  const macStart = buildScript.indexOf("build_backend() {");
  const macEnd = buildScript.indexOf("build_backend_windows() {", macStart);
  assert.ok(macStart >= 0 && macEnd > macStart, "could not locate build_backend()");
  const posixBuilder = buildScript.slice(macStart, macEnd);
  const fnStart = bytecodeScript.indexOf("precompile_macos_backend() {");
  assert.ok(fnStart >= 0, "could not locate precompile_macos_backend()");
  const macBytecode = bytecodeScript.slice(fnStart);

  assert.match(buildScript, /^\. "\$ROOT\/packaging\/desktop-bytecode\.sh"$/m);
  assert.match(
    posixBuilder,
    /if \[ "\$OS" = "darwin" \]; then\n\s*precompile_macos_backend "\$out"\n\s*else/,
    "the macOS backend no longer takes the closure path"
  );
  // Linux keeps the whole tree: nothing there seals the tree or bars writes.
  assert.match(
    posixBuilder,
    /-m compileall -q -f \\\r?\n\s*--invalidation-mode checked-hash "\$out\/lib"/
  );
  // Both run AFTER the prune that deletes pip's timestamp pycs, or their
  // output is deleted.
  const prune = posixBuilder.indexOf("-name __pycache__ -prune");
  assert.ok(prune >= 0 && prune < posixBuilder.indexOf('precompile_macos_backend "$out"'));
  assert.ok(prune < posixBuilder.indexOf("-m compileall"));

  // The closure: CHECKED-hash, the gateway import among the traced entries,
  // and lark_oapi deliberately left out (it is more than half of the caches).
  assert.match(
    macBytecode,
    /precompile_windows\.py" --root "\$out" \\\n\s*--invalidation-mode checked-hash "\$\{trace\[@\]\}" --ban-bytecode-writes "\$sp"/
  );
  assert.match(macBytecode, /for module in kiro_crew\.cli_server /);
  assert.match(macBytecode, /from kiro_crew\.mcp_entry import SLIM_SERVERS/);
  assert.doesNotMatch(macBytecode, /--module lark_oapi|lark_oapi; do/);
  // The standard library and kiro_crew stay whole; the rest of the tree does not.
  assert.match(macBytecode, /whole\+=\( "\$sp\/kiro_crew" \)/);
  assert.match(macBytecode, /\[ "\$entry" = "\$sp" \] \|\| whole\+=\( "\$entry" \)/);
  assert.match(
    macBytecode,
    /-m compileall -q -f --invalidation-mode checked-hash "\$\{whole\[@\]\}"/
  );
  assert.doesNotMatch(macBytecode, /compileall[^\n]*"\$out\/lib"/);
  assert.doesNotMatch(
    macBytecode,
    /--invalidation-mode (timestamp|unchecked-hash)/,
    "a timestamp pyc is rewritten on first use, and the macOS lane validates sources"
  );

  // The coverage gate, all three parts: the closure re-traced in a fresh
  // process, the whole-compiled trees, and the write ban itself.
  assert.match(macBytecode, /--invalidation-mode checked-hash "\$\{trace\[@\]\}" --verify/);
  assert.match(macBytecode, /bytecode_coverage_gate "\$out" "\$out\/lib" "\$sp"/);
  assert.match(macBytecode, /bytecode_coverage_gate "\$out" "\$sp\/kiro_crew" ""/);
  assert.match(
    macBytecode,
    /env -u PYTHONDONTWRITEBYTECODE [^\n]*"\$py" -s -c \\\n\s*'import sys; raise SystemExit\(0 if sys\.dont_write_bytecode else 1\)'/
  );
  assert.match(bytecodeScript, /shipped without a bytecode cache/);
});
