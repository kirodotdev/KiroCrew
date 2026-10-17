/**
 * Screenshots of the selection toolbar appearing over REAL multi-click
 * selections (#7847), via the capture/selection-toolbar-multiclick harness.
 *
 * Both gestures are genuine Chromium input (`clickCount`), so the browser makes
 * its own selection — including the boundary normalization on the container's
 * last block that used to dismiss the toolbar. Verified against the pre-fix
 * component: the triple-click shot times out there (Chromium ends a last-line
 * paragraph selection at the start of the NEXT block, outside the container,
 * and the toolbar never appeared), so this script is a live regression proof,
 * not just a portrait.
 *
 * Usage: node scripts/capture-selection-toolbar-multiclick.mjs <viteBase> <outDir>
 */
import { chromium } from 'playwright'

// Some dev hosts run a version-manager-built node that injects its own lib dir
// (with an older libstdc++) into LD_LIBRARY_PATH of the node process itself;
// Chromium inherits it and its system Mesa/LLVM then fail to load. The browser
// wants the system loader path, so drop the injection before launching.
delete process.env.LD_LIBRARY_PATH

const base = process.argv[2] || 'http://127.0.0.1:5199'
const outDir = process.argv[3] || '../temp-screenshots/selection-toolbar-multiclick'

const b = await chromium.launch()
const p = await (await b.newContext({ viewport: { width: 900, height: 500 }, deviceScaleFactor: 2 })).newPage()
const last = '[data-testid="last-line"]'

async function shoot(name, clickCount, position) {
  await p.goto(`${base}/capture/selection-toolbar-multiclick.html?theme=dark`, { waitUntil: 'networkidle' })
  await p.locator(last).waitFor({ state: 'visible', timeout: 15_000 })
  await p.click(last, { clickCount, position })
  // Fails loudly against the pre-fix component instead of shooting an empty frame.
  await p.getByRole('button', { name: 'Copy' }).waitFor({ state: 'visible', timeout: 5_000 })
  const out = `${outDir}/${name}.png`
  await p.screenshot({ path: out })
  console.log(`captured ${out}`)
}

// The reported regression gesture: triple-click the LAST line — its paragraph
// selection is normalized past the container and used to dismiss the toolbar.
await shoot('triple-click-last-line', 3, { x: 120, y: 10 })
// The report's headline symptom: a double-click word selection on the last line.
await shoot('double-click-last-line', 2, { x: 120, y: 10 })

await b.close()
