/**
 * Recording harness for Issue Radar → Crews: navigating the roster.
 *
 * A still frame cannot prove a sequence, and two things on this surface are
 * sequences rather than states: selecting a crew in column 2 repaints column 3
 * with that crew's page, and the create control raises a dialog over it. So this
 * records both, end to end.
 *
 * Same fixtures and same stub as capture-crews.mjs (see
 * lib/issue-radar-crews-fixtures.mjs); only the driving differs.
 *
 * Produces webm, and mp4 + GIF when ffmpeg is present (it is, in the Playwright
 * image). Usage: node scripts/record-crews.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { makeExtra, seedState } from './lib/issue-radar-crews-fixtures.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/crews')
const NAME = 'crews-roster-flow'
mkdirSync(OUT, { recursive: true })

const SIZE = { width: 1440, height: 900 }

/** Every locator below is a testid. Copy-keyed waits have broken this harness
 *  twice — once on a rename, once when the view went away. */
const CREATE = '[data-testid="crew-create"]'
const ROW = (id) => `[data-testid="crew-row-${id}"]`

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: SIZE,
    // deviceScaleFactor stays 1 for video: a 2x frame doubles the encode cost
    // and the GIF is downscaled for the PR anyway.
    recordVideo: { dir: OUT, size: SIZE },
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { theme: 'dark', extra: makeExtra(json) })
  await page.addInitScript((entries) => {
    for (const [k, v] of Object.entries(entries)) localStorage.setItem(k, v)
  }, seedState({ crewView: { kind: 'crew', id: 'c_7f3a01' }, crewFilter: 'all' }))

  await page.goto(`${base}/issue-radar`, { waitUntil: 'domcontentloaded' })
  await page.locator(CREATE).first().waitFor({ state: 'visible', timeout: 20000 })
  // Let the board settle so the first frames show a populated roster rather than
  // a skeleton — the recording is evidence, not a loading demo.
  await page.locator('[data-testid="crew-page"]').first().waitFor({ state: 'visible' })
  await page.waitForTimeout(1400)

  // 1. Select another crew, and wait on ITS row being current rather than on a
  //    timeout, so a slow repaint cannot make this pass on the previous page.
  const other = page.locator(ROW('c_7f3a06')).first()
  if (await other.count()) {
    await other.click()
    await page.locator(`${ROW('c_7f3a06')}[aria-current="page"]`).waitFor({ state: 'visible' })
    await page.waitForTimeout(1200)
  }

  // 2. Back to the crew with a populated work log, which is the page worth showing.
  const first = page.locator(ROW('c_7f3a01')).first()
  await first.click()
  await page.locator(`${ROW('c_7f3a01')}[aria-current="page"]`).waitFor({ state: 'visible' })
  await page.waitForTimeout(1400)

  // 3. Raise the create dialog and hold on it.
  await page.locator(CREATE).first().click()
  await page.getByRole('dialog').waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(1600)

  await context.close() // flushes the video file
  await browser.close()
  srv.close()

  const webm = readdirSync(OUT).filter((f) => f.endsWith('.webm')).sort().pop()
  if (!webm) throw new Error('playwright wrote no video')
  const src = join(OUT, `${NAME}.webm`)
  renameSync(join(OUT, webm), src)
  console.log('WEBM', src)

  const ff = (args) => spawnSync('ffmpeg', ['-y', ...args], { stdio: 'ignore' }).status === 0
  const mp4 = join(OUT, `${NAME}.mp4`)
  if (ff(['-i', src, '-movflags', 'faststart', '-pix_fmt', 'yuv420p', '-vf',
          'scale=1280:-2', mp4])) {
    console.log('MP4', mp4)
  }
  // Palette-optimised GIF: a plain -i → .gif is 4-5x larger at worse quality.
  const pal = join(OUT, `${NAME}-palette.png`)
  const gif = join(OUT, `${NAME}.gif`)
  if (ff(['-i', src, '-vf', 'fps=10,scale=1000:-1:flags=lanczos,palettegen', pal])
      && ff(['-i', src, '-i', pal, '-lavfi',
             'fps=10,scale=1000:-1:flags=lanczos[x];[x][1:v]paletteuse', gif])) {
    console.log('GIF', gif)
  } else {
    console.log('GIF skipped — ffmpeg unavailable or failed; webm still written')
  }
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
