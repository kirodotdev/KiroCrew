/**
 * Drift guards for the macOS menu-bar tray template icon.
 *
 * macOS renders tray icons as template images: a monochrome (black + alpha)
 * glyph the system recolors for light/dark/tinted menu bars. The failure this
 * guards against is SILENT on the Linux CI host and on developer machines
 * without a packaged mac build: the tray simply renders the full-colour icon
 * again, clashing with neighbouring status items. macOS rendering cannot be
 * exercised here, so these tests assert the objective parts instead:
 *
 *   1. the template assets exist, at 18px with an exact @2x retina variant,
 *      and every visible pixel is pure black (a colour pixel would render
 *      wrong on every menu bar, and nothing else would ever catch it);
 *   2. createTray() actually takes the template path on macOS, calls
 *      setTemplateImage(true), and keeps the channel-aware full-colour
 *      selection for the other platforms;
 *   3. electron-builder ships the new assets and still ships the colour ones.
 *
 * The assets are encoded as single-IDAT, filter-0 RGBA PNGs on purpose so this
 * test can decode pixels with just zlib.inflateSync — no image dependency.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");

const ROOT = path.join(__dirname, "..");

/** Decode a filter-0, 8-bit RGBA PNG into { width, height, pixels }. */
function decodeFilter0Png(file) {
  const buf = fs.readFileSync(file);
  assert.equal(buf.readUInt32BE(0), 0x89504e47, `${file}: not a PNG`);
  let off = 8;
  let width = 0;
  let height = 0;
  const idat = [];
  while (off < buf.length) {
    const len = buf.readUInt32BE(off);
    const tag = buf.toString("ascii", off + 4, off + 8);
    const data = buf.subarray(off + 8, off + 8 + len);
    if (tag === "IHDR") {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      assert.equal(data[8], 8, `${file}: bit depth must be 8`);
      assert.equal(data[9], 6, `${file}: color type must be RGBA`);
      assert.equal(data[12], 0, `${file}: must be non-interlaced`);
    } else if (tag === "IDAT") {
      idat.push(data);
    }
    off += 12 + len;
  }
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = 1 + width * 4;
  assert.equal(raw.length, stride * height, `${file}: unexpected scanline data`);
  const pixels = [];
  for (let y = 0; y < height; y++) {
    assert.equal(raw[y * stride], 0, `${file}: row ${y} must use filter 0`);
    for (let x = 0; x < width; x++) {
      const p = y * stride + 1 + x * 4;
      pixels.push([raw[p], raw[p + 1], raw[p + 2], raw[p + 3]]);
    }
  }
  return { width, height, pixels };
}

test("tray template assets are monochrome black-on-transparent at 1x and 2x", () => {
  const base = decodeFilter0Png(path.join(ROOT, "trayTemplate.png"));
  const retina = decodeFilter0Png(path.join(ROOT, "trayTemplate@2x.png"));

  assert.equal(base.width, 18);
  assert.equal(base.height, 18);
  assert.equal(retina.width, base.width * 2, "@2x must be exactly double");
  assert.equal(retina.height, base.height * 2, "@2x must be exactly double");

  for (const { width, pixels } of [base, retina]) {
    pixels.forEach(([r, g, b, a], i) => {
      if (a === 0) return; // fully transparent — colour bytes irrelevant
      const at = `${width}px pixel #${i}`;
      assert.equal(r, 0, `${at}: template pixels must be pure black (r)`);
      assert.equal(g, 0, `${at}: template pixels must be pure black (g)`);
      assert.equal(b, 0, `${at}: template pixels must be pure black (b)`);
    });
    // macOS template rendering reads only the alpha channel, so the glyph
    // contract is alpha coverage: faint scaling residue (alpha 1-8) must not
    // count as a glyph. Require a real footprint of strongly-opaque pixels
    // (>=10% of the canvas; the committed assets carry ~50%).
    const strong = pixels.filter((p) => p[3] > 128).length;
    assert.ok(
      strong >= Math.floor(pixels.length * 0.1),
      `${width}px: only ${strong}/${pixels.length} strongly-opaque pixels — glyph missing or degenerate`
    );
  }
});

test("createTray uses the template image on macOS and colour icons elsewhere", () => {
  const main = fs.readFileSync(path.join(ROOT, "main.js"), "utf8");
  const start = main.indexOf("function createTray()");
  assert.notEqual(start, -1, "createTray not found");
  const body = main.slice(start, main.indexOf("\n}", start));

  // Polarity matters: assert the exact guard shape, then split the two
  // branches and pin each assertion INSIDE its branch. Token-presence
  // greps over the whole body would still pass with the guard inverted
  // or setTemplateImage moved to the colour branch.
  assert.match(
    body,
    /if \(IS_MAC && fs\.existsSync\(templatePath\)\)/,
    "template path must be guarded by IS_MAC && existsSync"
  );
  const elseIdx = body.indexOf("} else {");
  assert.notEqual(elseIdx, -1, "createTray must have a colour-icon fallback branch");
  const macBranch = body.slice(body.indexOf("if (IS_MAC"), elseIdx);
  const elseBranch = body.slice(elseIdx);

  // macOS branch: consumes the template path + explicit template flag.
  // (The "trayTemplate.png" literal lives in the templatePath declaration
  // above the guard; the branch loads it via the variable.)
  assert.match(
    body,
    /const templatePath = path\.join\(__dirname, "trayTemplate\.png"\)/,
    "templatePath must resolve to the template asset"
  );
  assert.match(macBranch, /createFromPath\(templatePath\)/, "mac branch must load the template asset");
  assert.match(macBranch, /setTemplateImage\(true\)/, "template flag must be set in the mac branch");
  assert.doesNotMatch(elseBranch, /setTemplateImage/, "colour branch must not set the template flag");

  // Non-mac branch: channel-aware colour selection untouched (selection
  // happens above the guard; the fallback consumes iconFile).
  assert.match(body, /icon-nightly\.png/, "nightly colour icon selection must remain");
  assert.match(body, /"icon\.png"/, "stable colour icon selection must remain");
  assert.match(elseBranch, /iconFile/, "colour branch must consume the channel-aware selection");
});

test("electron-builder ships the template assets alongside the colour icons", () => {
  const files = require(path.join(ROOT, "package.json")).build.files;
  for (const asset of ["trayTemplate.png", "trayTemplate@2x.png", "icon.png", "icon-nightly.png"]) {
    assert.ok(files.includes(asset), `${asset} missing from build.files`);
  }
});
