/**
 * Record the Recent-activity day fold on a REAL pod: one day opening into its
 * time strip, then "Show N more days" unfolding and folding back. Writes a webm
 * (Playwright's recorder), then frames + a GIF cropped to the detail panel.
 *
 * Usage:
 *   POD_INFO="$KIROCREW_SCRATCH/pod-info.json" \
 *     node scripts/record-members-activity-days.mjs <outDir> [FFMPEG=/path]
 *
 * The GIF is assembled by Pillow (PIL) because Playwright's bundled ffmpeg has
 * no gif encoder or palette filters; set PYTHON to an interpreter that has it.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { check, podInfo } from './lib/crew-pod-harness.mjs'

const OUT = process.argv[2]
if (!OUT) throw new Error('usage: <outDir>')
mkdirSync(OUT, { recursive: true })
const { BASE, authed } = podInfo(readFileSync)
const MEMBER = process.env.MEMBER || 'radar'
const FF = process.env.FFMPEG || 'ffmpeg'
const PY = process.env.PYTHON || 'python3'
const W = 1280, H = 800

const browser = await chromium.launch()
const tStart = Date.now()
const context = await browser.newContext({
  viewport: { width: W, height: H },
  recordVideo: { dir: OUT, size: { width: W, height: H } },
  timezoneId: 'UTC',
  locale: 'en',
})
const page = await context.newPage()
await page.goto(authed('/members'), { waitUntil: 'domcontentloaded' })
await page.locator('#main-content').waitFor({ state: 'visible', timeout: 20000 })
const skip = page.getByRole('button', { name: /Skip this version|跳过此版本/ })
if (await skip.waitFor({ state: 'visible', timeout: 2500 }).then(() => true, () => false)) {
  await skip.click()
  await skip.waitFor({ state: 'hidden', timeout: 10000 })
}
await page.evaluate(async () => {
  localStorage.setItem('mc-preview-crew', '1')
  localStorage.setItem('mc-lang', 'en')
  localStorage.setItem('mc-theme', 'dark')
  await fetch('/api/config/theme', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: 'dark', language: 'en', onboarded: true, import_onboarded: true, privacy_acked: true }),
  })
})
await page.goto(`${BASE}/members`, { waitUntil: 'domcontentloaded' })
const row = page.locator('#main-content li button', { hasText: MEMBER }).first()
await row.waitFor({ state: 'visible', timeout: 20000 })
await row.click()
await page.getByTestId('member-title-row').waitFor({ state: 'visible', timeout: 10000 })
const drawer = page.getByTestId('member-crew-summary')
if (!(await drawer.isVisible().catch(() => false))) await page.getByTestId('member-panel-toggle').click()
await page.getByTestId('member-activity-days').waitFor({ state: 'visible', timeout: 15000 })
await page.waitForTimeout(1200)
// Where the panel sits, for the crop below.
const panel = page.getByTestId('member-side-panel')
const box = await panel.boundingBox()
check('panel located', !!box)
const t0 = Date.now()
const days = page.getByTestId('member-activity-day')
check('three day rows before the fold', (await days.count()) === 3)
await days.nth(1).hover()
await page.waitForTimeout(500)
await days.nth(1).click()
await page.getByTestId('member-activity-times').waitFor({ state: 'visible' })
check('opened day carries a routed time', (await page.locator('[data-routed]').count()) >= 1)
await page.waitForTimeout(1600)
await days.nth(1).click()
await page.waitForTimeout(700)
const more = page.getByTestId('member-activity-more')
await more.hover()
await page.waitForTimeout(400)
await more.click()
await page.waitForTimeout(400)
check('unfolded to six day rows', (await days.count()) === 6)
await page.waitForTimeout(1500)
await more.click()
await page.waitForTimeout(400)
check('folded back to three', (await days.count()) === 3)
await page.waitForTimeout(700)
const elapsed = (Date.now() - t0) / 1000
// Frames to drop: everything before the interaction began (page load, member
// switch), at the 10 fps the frame extraction below uses.
const dropFrames = Math.max(0, Math.round((t0 - tStart) / 100) - 3)
// THIS run's video, by handle — never "the newest .webm in OUT", which on a
// reused directory can be a previous run's file.
const video = page.video()
if (!video) throw new Error('playwright recorded no video')
await context.close()
await browser.close()
const recorded = await video.path()
const src = join(OUT, 'activity-days-fold.webm')
renameSync(recorded, src)
console.log('WEBM', src, `(${elapsed.toFixed(1)}s of interaction)`)

// Frames: crop to the detail panel (+8px margin), 10 fps, PNG.
// Fresh frames directory: leftover f-NNNN.png from a longer earlier run would
// otherwise be folded into the tail of this GIF.
const framesDir = join(OUT, 'frames')
rmSync(framesDir, { recursive: true, force: true })
mkdirSync(framesDir, { recursive: true })
const cx = Math.max(0, Math.floor(box.x) - 8), cw = Math.min(W - cx, Math.ceil(box.width) + 16)
const ff = spawnSync(FF, ['-y', '-i', src, '-vf', `crop=${cw}:${H}:${cx}:0`, '-r', '10', join(framesDir, 'f-%04d.png')], { stdio: 'ignore' })
if (ff.status !== 0) { console.log('FRAMES skipped — ffmpeg failed; webm kept at', src); process.exit(0) }
const py = `
import glob, sys
from PIL import Image
files = sorted(glob.glob(sys.argv[1] + '/f-*.png'))
frames = [Image.open(f).convert('RGB') for f in files]
# Drop the load/settle frames so the GIF starts on the drawer at rest.
frames = frames[int(sys.argv[3]):] or frames
q = [f.quantize(colors=128, method=Image.Quantize.MEDIANCUT) for f in frames]
q[0].save(sys.argv[2], save_all=True, append_images=q[1:], duration=100, loop=0, optimize=True)
print('GIF', sys.argv[2], len(q), 'frames')
`
writeFileSync(join(OUT, 'mkgif.py'), py)
const gif = join(OUT, 'activity-days-fold.gif')
const r = spawnSync(PY, [join(OUT, 'mkgif.py'), framesDir, gif, String(dropFrames)], { encoding: 'utf-8' })
process.stdout.write(r.stdout || '')
if (r.status !== 0) { console.log('GIF skipped:', r.stderr); process.exit(0) }
