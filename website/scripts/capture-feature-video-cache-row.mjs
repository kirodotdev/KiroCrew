/**
 * Screenshot evidence for the feature-video cache row in Settings ▸ Chat.
 *
 * Drives the isolated capture entry (website/capture/feature-video-cache-row.html),
 * which mounts the REAL `ChatPanel` with the status route answered per scene.
 *
 * Frames:
 *   01-cached.png       part of the release on disk, downloads permitted — counts,
 *                       release, and the manual control.
 *   02-downloading.png  a clip in flight: the line names it, the control is
 *                       unavailable so the same pass cannot be queued twice.
 *   03-policy-off.png   downloads forbidden: the line says so and there is NO
 *                       control, greyed included.
 *
 * Each frame is ASSERTED, not merely photographed: "hidden, not disabled" and
 * "unavailable while a fetch runs" are claims about the DOM, which a picture
 * cannot settle on its own.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6837 --strictPort   # in another shell
 *   node scripts/capture-feature-video-cache-row.mjs http://127.0.0.1:6837 ../temp-screenshots/feature-videos-remote
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6837'
const OUT = process.argv[3] || '../temp-screenshots/feature-videos-remote'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 900, height: 700 }
/** The settings cards stagger in; settle past it so a still is not mid-entrance. */
const STAGGER_SETTLE_MS = 900

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than the
// system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

function fail(msg) {
  console.error(`FAIL: ${msg}`)
  failures += 1
}

async function scene(query) {
  const page = await browser.newPage({ viewport: VIEWPORT })
  await page.goto(`${BASE}/capture/feature-video-cache-row.html?${query}`, { waitUntil: 'load' })
  await page.waitForSelector('[data-testid="feature-video-status"]')
  await page.waitForTimeout(STAGGER_SETTLE_MS)
  // The row sits inside the Messages card, below the fold at this viewport.
  await page.locator('[data-testid="feature-video-status"]').scrollIntoViewIfNeeded()
  await page.waitForTimeout(200)
  return page
}

const DOWNLOAD_BTN = 'button:has-text("Download all now")'

/* ── 01 part of the release cached, downloads permitted ──────────────────── */
{
  const page = await scene('scene=cached')
  const line = await page.locator('[data-testid="feature-video-status"]').innerText()
  for (const fragment of ['2026.09.1', '2', '3']) {
    if (!line.includes(fragment)) fail(`cached line "${line}" omits ${fragment}`)
  }
  if (await page.locator(DOWNLOAD_BTN).count() !== 1) fail('manual control missing when downloads are permitted')
  if (await page.locator(DOWNLOAD_BTN).isDisabled()) fail('manual control unavailable with nothing in flight')
  await page.screenshot({ path: join(OUT, '01-cached.png') })
  await page.close()
}

/* ── 02 a clip being fetched right now ──────────────────────────────────── */
{
  const page = await scene('scene=downloading')
  const line = await page.locator('[data-testid="feature-video-status"]').innerText()
  if (!line.includes('monitor-loops')) fail(`in-flight line "${line}" does not name the clip`)
  if (!await page.locator(DOWNLOAD_BTN).isDisabled()) {
    fail('manual control still available while a fetch is in flight')
  }
  await page.screenshot({ path: join(OUT, '02-downloading.png') })
  await page.close()
}

/* ── 03 downloads forbidden by policy ───────────────────────────────────── */
{
  const page = await scene('scene=policy-off')
  const line = await page.locator('[data-testid="feature-video-status"]').innerText()
  if (!/turned off/i.test(line)) fail(`policy line "${line}" does not say what is off`)
  // Hidden, NOT greyed: a control whose only outcome is a refusal explains a
  // policy the user cannot act on.
  if (await page.locator(DOWNLOAD_BTN).count() !== 0) fail('manual control rendered with downloads forbidden')
  await page.screenshot({ path: join(OUT, '03-policy-off.png') })
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`feature-video cache row capture: ${failures} assertion failure(s)`)
  process.exit(1)
}
console.log(`feature-video cache row capture: frames written to ${OUT}, all assertions pass`)
