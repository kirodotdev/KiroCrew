#!/usr/bin/env node

// Regenerate the checked-in installer rasters from the editable SVG sources.
// macOS's sips is used only by designers; release and Windows builds consume
// the committed PNG/BMP files and do not execute this script.

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const here = dirname(fileURLToPath(import.meta.url));
const scratch = mkdtempSync(join(tmpdir(), "kirocrew-installer-assets-"));

function run(binary, args, label) {
  const result = spawnSync(
    binary,
    args,
    { encoding: "utf8" }
  );
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || `${label} failed`);
  }
}

function render(source, format, destination) {
  run(
    "/usr/bin/sips",
    ["-s", "format", format, join(here, source), "--out", destination],
    `sips render for ${source}`
  );
}

function renderSvgAtScale(source, scale, destination) {
  const sourcePath = join(here, source);
  const scaledPath = join(scratch, `${source.replace(".svg", "")}@${scale}x.svg`);
  const scaled = readFileSync(sourcePath, "utf8")
    .replace('width="660"', `width="${660 * scale}"`)
    .replace('height="420"', `height="${420 * scale}"`);
  writeFileSync(scaledPath, scaled);
  run(
    "/usr/bin/sips",
    ["-s", "format", "png", scaledPath, "--out", destination],
    `sips ${scale}x render for ${source}`
  );
}

function bmp32To24(source, destination) {
  const input = readFileSync(source);
  if (input.toString("ascii", 0, 2) !== "BM") throw new Error(`${source} is not a BMP`);

  const sourceOffset = input.readUInt32LE(10);
  const width = input.readInt32LE(18);
  const signedHeight = input.readInt32LE(22);
  const height = Math.abs(signedHeight);
  const bitsPerPixel = input.readUInt16LE(28);
  if (width <= 0 || height <= 0 || bitsPerPixel !== 32) {
    throw new Error(`${source} must be a non-empty 32-bit BMP from sips`);
  }

  const sourceStride = width * 4;
  const destinationStride = Math.ceil((width * 3) / 4) * 4;
  const pixelBytes = destinationStride * height;
  const output = Buffer.alloc(54 + pixelBytes);
  output.write("BM", 0, 2, "ascii");
  output.writeUInt32LE(output.length, 2);
  output.writeUInt32LE(54, 10);
  output.writeUInt32LE(40, 14);
  output.writeInt32LE(width, 18);
  output.writeInt32LE(height, 22);
  output.writeUInt16LE(1, 26);
  output.writeUInt16LE(24, 28);
  output.writeUInt32LE(pixelBytes, 34);
  output.writeInt32LE(2835, 38);
  output.writeInt32LE(2835, 42);

  for (let outputY = 0; outputY < height; outputY += 1) {
    const sourceY = signedHeight < 0 ? height - 1 - outputY : outputY;
    const sourceRow = sourceOffset + sourceY * sourceStride;
    const destinationRow = 54 + outputY * destinationStride;
    for (let x = 0; x < width; x += 1) {
      output[destinationRow + x * 3] = input[sourceRow + x * 4];
      output[destinationRow + x * 3 + 1] = input[sourceRow + x * 4 + 1];
      output[destinationRow + x * 3 + 2] = input[sourceRow + x * 4 + 2];
    }
  }
  writeFileSync(destination, output);
}

try {
  const dmg1x = join(scratch, "dmg-background.png");
  const dmg2x = join(scratch, "dmg-background@2x.png");
  renderSvgAtScale("dmg-background.svg", 1, dmg1x);
  renderSvgAtScale("dmg-background.svg", 2, dmg2x);
  run(
    "/usr/bin/tiffutil",
    ["-cathidpicheck", dmg1x, dmg2x, "-out", join(here, "dmg-background.tiff")],
    "Retina TIFF assembly"
  );
  for (const name of ["windows-installer-sidebar", "windows-installer-header"]) {
    const intermediate = join(scratch, `${name}.bmp`);
    render(`${name}.svg`, "bmp", intermediate);
    bmp32To24(intermediate, join(here, `${name}.bmp`));
  }
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
