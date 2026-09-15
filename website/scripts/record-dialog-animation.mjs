/**
 * Recording harness for the shadcn dialog's enter/exit animation.
 *
 * Two things here are sequences a still frame cannot judge:
 *
 *   1. `DialogContent` is centred with `-translate-x-1/2 -translate-y-1/2`, so a
 *      keyframe that declares a bare `transform: scale(...)` outranks that class
 *      for the animation's whole duration and parks the panel with its top-left
 *      corner at the viewport centre, then snaps it back on the last frame.
 *   2. `ui/select.tsx` carries `animate-in`/`zoom-in-95` in its class list, which
 *      emits no CSS at all unless `tailwindcss-animate` is installed — so the
 *      dropdown either animates or pops, and only motion tells the two apart.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, answering /api/** from the same fixtures capture-crews-list-modal.mjs
 * uses — gateway-free, no kiro-cli, no dashboard auth. Rebuild dist first: the
 * change under test is CSS the build emits, so a stale bundle records the wrong
 * side.
 *
 * Emits webm plus, when ffmpeg is present, mp4 + a real-time GIF + a 4x-slowed
 * GIF. The slow one exists because the defect lasts 200ms — five frames at
 * Playwright's fixed 25fps, which a real-time GIF at 10fps reduces to two.
 *
 * Usage: node scripts/record-dialog-animation.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, readdirSync, existsSync, rmSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/dialog-animation')
const PREFIX = process.argv[3] || 'after'
// PREFIX is concatenated into a directory path that is then removed RECURSIVELY,
// so a prefix carrying a separator or `..` would escape OUT and delete something
// else. Constrain it to a slug rather than trying to sanitise a path.
if (!/^[A-Za-z0-9_-]+$/.test(PREFIX)) {
  throw new Error(
    `refusing prefix ${JSON.stringify(PREFIX)}: letters, digits, underscore and hyphen only`,
  )
}
const NAME = `${PREFIX}-dialog-animation`
mkdirSync(OUT, { recursive: true })

// deviceScaleFactor stays 1: a 2x frame doubles encode cost and every artifact
// below is downscaled anyway.
const SIZE = { width: 1280, height: 800 }

const CREWS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'core-ws', memory_store: 'core-mem' },
  { name: 'oncall', kiro_agent: 'kirocrew', workspace: 'oncall', memory_store: 'oncall-mem' },
  { name: 'research', kiro_agent: 'kirocrew', workspace: 'research', memory_store: 'research-mem' },
]

/** One dialog open, held long enough to read, then dismissed. */
async function openAndClose(page, { withSelect } = {}) {
  await page.getByRole('button', { name: 'Edit crew oncall' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(1300)

  if (withSelect) {
    // The Radix Select inside the dialog: its animation classes are inert
    // without the plugin, so this is where the second half of the change shows.
    await dialog.getByRole('combobox', { name: 'Memory Store' }).click()
    await page.getByRole('option').first().waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(900)
    await page.keyboard.press('Escape')
    await page.waitForTimeout(600)
  }

  await page.keyboard.press('Escape')
  await dialog.waitFor({ state: 'hidden', timeout: 10000 })
  await page.waitForTimeout(900)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  // Record into a per-run subdirectory rather than OUT itself. Playwright names
  // its video randomly, so picking "the webm in OUT" cannot distinguish this
  // run's file from a previous prefix's leftover — and a random name beginning
  // with a digit sorts BEFORE a renamed one, so a naive sort/pop would rename
  // the earlier capture and publish it as this side. One directory per run
  // removes the ambiguity instead of trying to filter around it.
  const raw = join(OUT, `_raw-${PREFIX}`)
  if (existsSync(raw)) rmSync(raw, { recursive: true })
  mkdirSync(raw, { recursive: true })
  const context = await browser.newContext({
    viewport: SIZE,
    deviceScaleFactor: 1,
    recordVideo: { dir: raw, size: SIZE },
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { extra: crewsApi({ crews: CREWS, defaultAgent: 'kirocrew' }) })

  await page.goto(`${base}/capabilities`, { waitUntil: 'domcontentloaded' })
  await page.locator('[data-testid="crew-card"]').first()
    .waitFor({ state: 'visible', timeout: 20000 })
  // Let the roster's own entrance settle so the first frames show a populated
  // page rather than a skeleton — this is evidence, not a loading demo.
  await page.waitForTimeout(1400)

  // A single mid-animation still, taken while the entrance is in flight. The
  // video is the evidence; this is the one frame worth linking on its own.
  await page.getByRole('button', { name: 'Edit crew oncall' }).click()
  await page.waitForTimeout(70)
  await page.screenshot({ path: `${OUT}/${PREFIX}-midflight.png`, animations: 'allow' })
  await page.getByRole('dialog').waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(700)
  await page.keyboard.press('Escape')
  await page.getByRole('dialog').waitFor({ state: 'hidden', timeout: 10000 })
  await page.waitForTimeout(700)

  // Twice, because one 200ms event is easy to miss on a first watch.
  await openAndClose(page)
  await openAndClose(page, { withSelect: true })

  await context.close() // flushes the video file
  await browser.close()
  srv.close()

  const webm = readdirSync(raw).filter(f => f.endsWith('.webm')).sort().pop()
  if (!webm) throw new Error('playwright wrote no video')
  const src = join(OUT, `${NAME}.webm`)
  renameSync(join(raw, webm), src)
  rmSync(raw, { recursive: true })
  console.log('WEBM', src)
  console.log('STILL', `${OUT}/${PREFIX}-midflight.png`)

  const ff = args => spawnSync('ffmpeg', ['-y', ...args], { stdio: 'ignore' }).status === 0
  const mp4 = join(OUT, `${NAME}.mp4`)
  if (ff(['-i', src, '-movflags', 'faststart', '-pix_fmt', 'yuv420p',
          '-vf', 'scale=1280:-2', mp4])) console.log('MP4', mp4)

  // Palette-optimised GIFs: a plain -i → .gif is 4-5x larger at worse quality.
  const gif = (suffix, filters) => {
    const pal = join(OUT, `${NAME}-${suffix}-palette.png`)
    const out = join(OUT, `${NAME}-${suffix}.gif`)
    if (ff(['-i', src, '-vf', `${filters},palettegen`, pal])
        && ff(['-i', src, '-i', pal, '-lavfi', `${filters}[x];[x][1:v]paletteuse`, out])) {
      console.log('GIF', out)
    } else {
      console.log(`GIF ${suffix} skipped — ffmpeg unavailable or failed; webm still written`)
    }
  }
  gif('realtime', 'fps=12,scale=900:-1:flags=lanczos')
  gif('slowmo', 'setpts=4*PTS,fps=16,scale=900:-1:flags=lanczos')
}

main().catch(err => { console.error(err); process.exit(1) })
