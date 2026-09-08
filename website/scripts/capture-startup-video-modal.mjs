/**
 * Screenshot + recording evidence for the startup feature-intro video modal.
 *
 * Drives the isolated capture entry (website/capture/startup-video-modal.html),
 * which mounts the REAL modal against the REAL placeholder asset.
 *
 * Frames:
 *   01-share-off.png  governance off — the fail-closed default. Asserted to carry
 *                     NO share control in any state, greyed included, because
 *                     "hidden, not disabled" is the claim under review.
 *   02-share-on.png   governance granted — the share entry appears.
 *   03-share-card.png the existing chat share card, reached from that entry,
 *                     carrying the clip's title and its blurb + doc link.
 *   open-and-play.webm the entrance, an attempted play, and the acknowledgement
 *                     closing it — a still cannot show an animated entrance. The
 *                     play only appears when the capture browser has an H.264
 *                     decoder; the bundled Chromium does not (see below).
 *
 * Both share states are ASSERTED, not just photographed: a frame proves what a
 * reviewer can see, and "no element that could open an intent URL" is a claim
 * about the DOM.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6837 --strictPort   # in another shell
 *   node scripts/capture-startup-video-modal.mjs http://127.0.0.1:6837 ../temp-screenshots/startup-feature-videos
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6837'
const OUT = process.argv[3] || '../temp-screenshots/startup-feature-videos'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 1100, height: 720 }
/** The entrance runs 220ms; settle past it so a still is not mid-transition. */
const ENTRANCE_SETTLE_MS = 600

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than the
// system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

function fail(msg) {
  console.error(`FAIL: ${msg}`)
  failures += 1
}

async function scene(name, query) {
  const page = await browser.newPage({ viewport: VIEWPORT })
  await page.goto(`${BASE}/capture/startup-video-modal.html?${query}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[role="dialog"]')
  await page.waitForSelector('[data-testid="startup-video"]')
  await page.waitForTimeout(ENTRANCE_SETTLE_MS)
  return page
}

/* ── 01 governance OFF — the fail-closed default ─────────────────────────── */
{
  const page = await scene('share-off', 'scene=share-off')
  const shareBtn = await page.locator('[data-testid="startup-video-share"]').count()
  if (shareBtn !== 0) fail(`share entry rendered with governance off (${shareBtn} found)`)
  // Hidden, NOT disabled: a greyed control still explains a policy the user
  // cannot act on, so its absence is part of the contract.
  const disabled = await page.locator('button[disabled]').count()
  if (disabled !== 0) fail(`${disabled} disabled button(s) with governance off — expected none`)
  for (const id of ['share-x', 'share-linkedin']) {
    if (await page.locator(`[data-testid="${id}"]`).count() !== 0) fail(`${id} reachable with governance off`)
  }
  await page.screenshot({ path: join(OUT, '01-share-off.png') })
  await page.close()
}

/* ── 02 governance ON — the entry appears ────────────────────────────────── */
{
  const page = await scene('share-on', 'scene=share-on')
  if (await page.locator('[data-testid="startup-video-share"]').count() !== 1) {
    fail('share entry missing with governance on')
  }
  // Still nothing that reaches an intent until the card is opened.
  if (await page.locator('[data-testid="share-x"]').count() !== 0) {
    fail('intent button present before the share card was opened')
  }
  await page.screenshot({ path: join(OUT, '02-share-on.png') })

  /* ── 03 the existing share card, reached from that entry ───────────────── */
  await page.locator('[data-testid="startup-video-share"]').click()
  await page.waitForSelector('[data-testid="share-card"]', { timeout: 15_000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, '03-share-card.png') })
  await page.close()
}

/* ── the recording: entrance, a real play, and the acknowledgement ───────── */
{
  const ctx = await browser.newContext({
    viewport: VIEWPORT,
    recordVideo: { dir: OUT, size: VIEWPORT },
  })
  const page = await ctx.newPage()
  await page.goto(`${BASE}/capture/startup-video-modal.html?scene=share-on`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-testid="startup-video"]')
  await page.waitForTimeout(700)
  // Try to play the clip for real, so the recording can show the poster giving
  // way to frames — `preload="none"` means nothing is fetched until this point.
  //
  // Playwright's bundled Chromium is the OPEN-SOURCE build, which omits the
  // proprietary codecs, so an H.264 source rejects here with NotSupportedError.
  // Chrome, Edge, Safari and the Electron shell all decode it, so the clip is
  // right and the harness is the limited party — the recording still carries the
  // entrance and the acknowledgement, which is what a still cannot show.
  const played = await page.evaluate(async () => {
    const v = document.querySelector('[data-testid="startup-video"]')
    if (!v) return 'no-element'
    try {
      await v.play()
      return 'played'
    } catch (e) {
      return `no-codec: ${e instanceof Error ? e.name : String(e)}`
    }
  })
  console.log(`  playback in the capture browser: ${played}`)
  await page.waitForTimeout(2500)
  await page.locator('button', { hasText: 'Got it' }).first().click()
  await page.waitForTimeout(600)
  await page.close()
  await ctx.close()
  // Playwright names videos by an internal id; give it the reported name.
  const vid = readdirSync(OUT).find(f => f.endsWith('.webm') && f !== 'open-and-play.webm')
  if (vid) renameSync(join(OUT, vid), join(OUT, 'open-and-play.webm'))
}

await browser.close()
if (failures) {
  console.error(`startup-video capture: ${failures} assertion failure(s)`)
  process.exit(1)
}
console.log(`startup-video capture: frames + recording written to ${OUT}, all assertions pass`)
