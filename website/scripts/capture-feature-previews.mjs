/**
 * Regenerate the Feature Previews "See what it looks like" media set —
 * `public/app-assets/feature-previews/` — from a RUNNING pod, so the pictures
 * the dialog shows stay pictures of the real surfaces rather than frozen bytes.
 *
 * Why this exists: `FeaturePreviewIntroDialog.tsx` promises "what you see is
 * what will appear". A preview surface is, by definition, the part of the app
 * that changes fastest, so the captures drift. This script is the honest way to
 * re-shoot them; a hand-made mockup is not (see the component comment).
 *
 * Every capture is REAL: each preview flag is turned on in the pod's
 * localStorage, the theme is set through the pod's own `PUT /api/config/theme`,
 * and the page is driven with Playwright. Stills are PNG; the one interaction
 * (the create menu opening) is recorded with Playwright's `recordVideo` and
 * turned into a palette-quantised GIF with the ffmpeg that `imageio-ffmpeg`
 * ships in the repo's venv (12 fps, 800 px wide, ~6 s loop).
 *
 * Usage (from website/, with the pod up and this branch's dist provisioned):
 *
 *   kirocrew pod up <worktree> --seed rich --json | tail -1 > /tmp/pod.json
 *   POD_INFO=/tmp/pod.json node scripts/capture-feature-previews.mjs [outDir]
 *
 * `outDir` defaults to `public/app-assets/feature-previews`. Budgets the PR
 * agreed to: PNG <= 200 KB, GIF <= 1.5 MB — the script fails loudly past them.
 * Adding a preview: add a `shoot*` step below AND a builder in
 * `pages/settings/FeaturePreviewsSection.tsx`; a preview with no honest capture
 * gets no builder and so no button.
 */
import { chromium } from 'playwright'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { execFileSync } from 'node:child_process'

const info = JSON.parse(fs.readFileSync(process.env.POD_INFO, 'utf8').trim().split('\n').pop())
const base = info.base_url
const token = info.token
const out = path.resolve(process.argv[2] || 'public/app-assets/feature-previews')
fs.mkdirSync(out, { recursive: true })

const PNG_MAX = 200 * 1024
const GIF_MAX = 1.5 * 1024 * 1024
const VIEW = { width: 1200, height: 760 }
const GIF_VIEW = { width: 1000, height: 640 }

/** The venv's imageio-ffmpeg binary: a full build (palettegen/paletteuse),
 *  unlike Playwright's bundled ffmpeg, which only decodes screencasts. */
function ffmpegExe() {
  const py = path.resolve('../.venv/bin/python')
  return execFileSync(py, ['-c', 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'], { encoding: 'utf8' }).trim()
}

async function settle(page) {
  await page.waitForURL(u => !String(u).includes('token='), { timeout: 20_000 }).catch(() => {})
  await page.waitForTimeout(1200)
}

/** Fresh context: sign in with the pod token, set the theme server-side (the
 *  boot fetch overrides localStorage otherwise), turn the given flags on. */
async function session(browser, theme, flags, viewport = VIEW, extra = {}) {
  const ctx = await browser.newContext({ viewport, ...extra })
  const page = await ctx.newPage()
  await page.goto(`${base}/?token=${token}`, { waitUntil: 'load' })
  await settle(page)
  const status = await page.evaluate(async (mode) => {
    const r = await fetch('/api/config/theme', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }),
    })
    return r.status
  }, theme)
  if (status !== 200) throw new Error(`theme PUT ${status}`)
  await page.evaluate(({ theme, flags }) => {
    localStorage.setItem('mc-theme', theme)
    for (const f of flags) localStorage.setItem(f, '1')
  }, { theme, flags })
  return { ctx, page }
}

function check(file, max) {
  const size = fs.statSync(file).size
  if (size > max) throw new Error(`${path.basename(file)} is ${size} bytes, over the ${max} budget`)
  console.log(`${path.basename(file)}  ${size} B`)
}

async function shoot(page, file) {
  await page.screenshot({ path: file })
  check(file, PNG_MAX)
}

const browser = await chromium.launch()

for (const theme of ['light', 'dark']) {
  // Webhooks: the page is the flag's only door.
  {
    const { ctx, page } = await session(browser, theme, ['mc-preview-webhooks'])
    await page.goto(`${base}/webhooks`, { waitUntil: 'load' })
    await settle(page)
    await shoot(page, path.join(out, `webhooks-page-${theme}.png`))
    await ctx.close()
  }
  // Crew, door 1: the Members page.
  {
    const { ctx, page } = await session(browser, theme, ['mc-preview-crew'])
    await page.goto(`${base}/members`, { waitUntil: 'load' })
    await settle(page)
    await shoot(page, path.join(out, `crew-members-${theme}.png`))
    await ctx.close()
  }
  // Crew, door 2: the create menu opening — an interaction, so a GIF.
  {
    const vdir = fs.mkdtempSync(path.join(os.tmpdir(), 'fp-gif-'))
    const seed = await session(browser, theme, ['mc-preview-crew'], GIF_VIEW)
    const state = await seed.ctx.storageState()
    await seed.ctx.close()
    const ctx = await browser.newContext({
      viewport: GIF_VIEW, storageState: state, recordVideo: { dir: vdir, size: GIF_VIEW },
    })
    await ctx.addInitScript((mode) => {
      localStorage.setItem('mc-theme', mode)
      localStorage.setItem('mc-preview-crew', '1')
    }, theme)
    const page = await ctx.newPage()
    await page.goto(`${base}/`, { waitUntil: 'load' })
    await settle(page)
    await page.getByRole('button', { name: 'Older Sessions' }).first().waitFor()
    await page.waitForTimeout(2200)
    const more = page.getByRole('button', { name: 'More create options' })
    await more.hover(); await page.waitForTimeout(500)
    await more.click()
    await page.getByTestId('new-crew-chat').waitFor()
    await page.waitForTimeout(700)
    await page.getByTestId('new-crew-chat').hover()
    await page.waitForTimeout(1800)
    await page.keyboard.press('Escape')
    await page.waitForTimeout(600)
    const video = page.video()
    await ctx.close()
    const webm = await video.path()
    const gif = path.join(out, `crew-menu-${theme}.gif`)
    const ffmpeg = ffmpegExe()
    // Skip the first 3 s (page settling), then two-pass palette GIF — the same
    // recipe as the browser-recording skill: sharp text, bounded size.
    const filters = 'fps=12,scale=min(800\\,iw):-2:flags=lanczos'
    const palette = path.join(vdir, 'palette.png')
    execFileSync(ffmpeg, ['-y', '-loglevel', 'error', '-ss', '3', '-i', webm, '-vf', `${filters},palettegen=max_colors=128`, palette])
    execFileSync(ffmpeg, ['-y', '-loglevel', 'error', '-ss', '3', '-i', webm, '-i', palette, '-lavfi', `${filters} [x]; [x][1:v] paletteuse=dither=none`, gif])
    fs.rmSync(vdir, { recursive: true, force: true })
    check(gif, GIF_MAX)
  }
}

await browser.close()
console.log(`media set written to ${out}`)
