/** Real-browser RECORDING of the docked-to-expanded transition and back.
 *
 * Drives website/capture/crew-webview-transition.html, which mounts the REAL
 * `CrewWebview` over a gateway that answers the panel read and mints
 * successfully. The clip walks what a still cannot show: the docked summary card,
 * the swap to the `fixed inset-0` full-window view on "Open dashboard", the
 * rendered document, and the swap back on "Collapse the dashboard".
 *
 * Playwright's own context recorder writes the webm, so the file is a real
 * capture of a real browser rather than frames stitched afterwards. It names the
 * file itself, so the run renames the result to the numbered evidence name the
 * other artifacts use.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort    # in another shell (website/)
 *   node scripts/record-crew-webview-transition.mjs http://127.0.0.1:6824 ../temp-screenshots/crew-webview
 */
import { chromium } from 'playwright'
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
} from 'node:fs'
import { spawnSync } from 'node:child_process'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
const OUT = process.argv[3] || '../temp-screenshots/crew-webview'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 1120, height: 720 }
const FINAL = join(OUT, '08-expand-collapse.webm')

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

// The recorder's directory sits UNDER the output directory rather than in the
// system temp dir: on this host temp is a different mount, and a cross-device
// rename fails with EXDEV. Same filesystem keeps the move atomic and cheap.
const videoDir = mkdtempSync(join(OUT, '.clip-'))
const recordingStartedAt = Date.now()
const context = await browser.newContext({
  viewport: VIEWPORT,
  deviceScaleFactor: 1, // the recorder scales to the viewport; 2 buys file size, not clarity
  recordVideo: { dir: videoDir, size: VIEWPORT },
})
const page = await context.newPage()
page.on('pageerror', e => {
  console.error('pageerror:', e.message)
  failures++
})
await page.goto(`${BASE}/capture/crew-webview-transition.html?theme=dark`, {
  waitUntil: 'domcontentloaded',
})

// Docked first, and held long enough to read: a clip that opens mid-transition
// does not show what the transition started from.
const expand = page.locator('[data-testid="crew-webview-expand"]')
await expand.waitFor({ state: 'visible', timeout: 15000 })
// The recorder starts when the CONTEXT is created, which is before the page
// navigates and paints, so the head of every clip is blank. Measured here rather
// than guessed at, and trimmed after the file is finalized.
const blankSecs = (Date.now() - recordingStartedAt) / 1000
check('starts docked', (await page.locator('[data-expanded="true"]').count()) === 0)
await page.waitForTimeout(1200)

await expand.click()
const dialog = page.locator('[data-expanded="true"]')
await dialog.waitFor({ state: 'visible', timeout: 15000 })
const inner = page.frameLocator('[data-expanded="true"] iframe').locator('body')
await inner.waitFor({ state: 'visible', timeout: 15000 })
// Read INSIDE the frame: a visible iframe element proves nothing about whether a
// document painted, and an empty frame would make the clip show a blank panel.
check('expanded and the document painted', ((await inner.textContent()) || '').includes('Sources read'))
await page.waitForTimeout(1600)

const collapse = page.locator('[data-testid="crew-webview-collapse"]')
await collapse.click()
await dialog.waitFor({ state: 'hidden', timeout: 15000 })
check('collapses back to the docked card', await expand.isVisible())
await page.waitForTimeout(1200)

// The video is only finalized on close, and its path is only knowable after.
await page.close()
await context.close()
await browser.close()

const written = readdirSync(videoDir).filter(f => f.endsWith('.webm'))
check('the recorder wrote exactly one clip', written.length === 1)
if (written.length === 1) {
  const raw = join(videoDir, written[0])
  rmSync(FINAL, { force: true })
  // Trim the blank head. This RE-ENCODES rather than stream-copying: a copy can
  // only cut at a keyframe, and this recorder emits one at the start, so a copy
  // keeps every blank frame while reporting success. If ffmpeg is not on the host
  // the untrimmed clip is kept and SAID to be untrimmed, because a missing tool
  // must not silently change what the evidence shows.
  let trimmed = false
  if (blankSecs > 0.2) {
    const r = spawnSync(
      'ffmpeg',
      [
        '-loglevel', 'error',
        '-ss', blankSecs.toFixed(2),
        '-i', raw,
        '-c:v', 'libvpx-vp9',
        '-crf', '34',
        '-b:v', '0',
        '-an',
        FINAL,
      ],
      { stdio: 'inherit' },
    )
    trimmed = r.status === 0 && existsSync(FINAL) && statSync(FINAL).size > 10000
    if (!trimmed) {
      rmSync(FINAL, { force: true })
      console.log(`could not trim (ffmpeg status ${r.status}); keeping the untrimmed clip`)
    }
  }
  if (!trimmed) renameSync(raw, FINAL)
  const bytes = statSync(FINAL).size
  check('the clip is not empty', bytes > 10000)
  console.log(`clip bytes: ${bytes}; blank head measured ${blankSecs.toFixed(2)}s, trimmed: ${trimmed}`)
}
rmSync(videoDir, { recursive: true, force: true })

if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - recording in ${FINAL}`)
