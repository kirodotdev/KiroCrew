/**
 * Capture harness — chat-core P5-d: the pinned-prompt banner in ChatPane.
 *
 * Against a REAL pod, not fixtures. Frames:
 *   01  Members DM (frameless ChatPane) scrolled mid-transcript: the banner
 *       pins the current turn's prompt under the host header, its bubble row
 *       hidden behind it.
 *   02  Same, banner expanded (chevron) — full prompt text.
 *   03  After clicking the banner: the transcript glided back to the prompt
 *       and the banner handed over to the PREVIOUS turn's prompt.
 *   04  The image-prompt banner: an inline thumbnail (`pinnedImageUrl`).
 *   05  Split view: the member thread as a titled pane beside another session,
 *       banner under the pane's own title bar.
 *   06  RECORDING (.webm, light): the Members DM scrolled by wheel steps so the
 *       bubble→banner hand-off and the push-out are visible, then the chevron
 *       morph, then the click-to-jump glide. Motion cannot be judged from a
 *       still (website/AGENTS.md lens 13).
 * Each still is shot in light and dark.
 *
 * Prereqs (once per pod; the member thread needs a long transcript):
 *   kirocrew pod up <worktree> --seed rich --json | tail -1 > "$KIROCREW_SCRATCH/pod-info.json"
 *   kirocrew pod api <worktree> POST /api/members/default/thread --allow-write
 *   # write sessions/dashboard_member-default.jsonl with >= 6 user turns, then
 *   kirocrew pod api <worktree> POST /api/restart --allow-write
 * Run:
 *   POD_INFO="$KIROCREW_SCRATCH/pod-info.json" OUT_DIR=<dir> \
 *     node website/scripts/capture-chatpane-pinned-prompt.mjs
 */
import { readFileSync, mkdirSync, renameSync, rmSync, existsSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'
import { spawnSync } from 'node:child_process'
import { chromium } from 'playwright'
import { check, podInfo } from './lib/crew-pod-harness.mjs'

const OUT = process.env.OUT_DIR || 'temp-screenshots/chatpane-pinned-prompt'
mkdirSync(OUT, { recursive: true })
const { authed } = podInfo(readFileSync)
const MEMBER = 'default'
const MEMBER_SLOT = 'member-default'
const OTHER_SLOT = 'coder-demo'
const BANNER = '[data-testid="pinned-prompt"]'

async function prime(page, theme) {
  await page.goto(authed('/members'), { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').waitFor({ state: 'visible', timeout: 20000 })
  const skip = page.getByRole('button', { name: 'Skip this version' })
  if (await skip.waitFor({ state: 'visible', timeout: 4000 }).then(() => true, () => false)) {
    await skip.click()
    await skip.waitFor({ state: 'hidden', timeout: 10000 })
  }
  await page.evaluate(async (th) => {
    localStorage.setItem('mc-preview-crew', '1')
    await fetch('/api/config/theme', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: th, onboarded: true, import_onboarded: true, privacy_acked: true }),
    })
    await fetch('/api/dashboard/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_grid: true }),
    })
  }, theme)
}

/** Scroll the pane's transcript so that the row at `rowIdx` sits just above the
 *  fold — a prompt fully behind the band takes the pin. */
async function scrollRowBehindFold(pane, rowIdx, extra = 0) {
  await pane.evaluate((el, [idx, ex]) => {
    const sc = el.querySelector('.chat-container')
    const row = sc.querySelector(`[data-display-index="${idx}"]`)
    const delta = row.getBoundingClientRect().bottom - sc.getBoundingClientRect().top
    sc.scrollTop += delta + ex
  }, [rowIdx, extra])
}

async function membersFrames(browser, theme) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: theme })
  const page = await ctx.newPage()
  await prime(page, theme)
  await page.goto(authed(`/members?member=${MEMBER}`), { waitUntil: 'domcontentloaded' })
  const pane = page.locator('[data-chat-pane]').first()
  await pane.waitFor({ state: 'visible', timeout: 20000 }).catch(async (e) => {
    await page.screenshot({ path: join(OUT, `debug-members-${theme}.png`) })
    console.log('debug url', page.url(), 'text', (await page.locator('#main-content').textContent().catch(() => '')).slice(0, 300))
    throw e
  })
  await pane.locator('[data-display-index]').nth(6).waitFor({ timeout: 20000 })
  await page.waitForTimeout(800)

  // Parked at the bottom, the last turn's reply is taller than the viewport, so
  // its prompt is already behind the fold: the banner pins it, as on the main chat.
  const atBottom = await page.locator(BANNER).textContent()
  check(`[${theme}] at the bottom the banner pins the last prompt`, /Last one/.test(atBottom || ''), atBottom)
  check(`[${theme}] frameless pane has no title bar of its own`, (await pane.locator('.border-b.bg-card').count()) === 0)

  // Scroll to the top, then bring turn 3's prompt (display row 6) fully behind
  // the fold with its reply still on screen.
  await pane.evaluate(el => { el.querySelector('.chat-container').scrollTop = 0 })
  await page.waitForTimeout(200)
  await scrollRowBehindFold(pane, 6, 60)
  await page.waitForTimeout(500)
  const banner = page.locator(BANNER)
  await banner.waitFor({ state: 'visible', timeout: 5000 })
  const text = await banner.textContent()
  check(`[${theme}] banner carries the prompt that scrolled behind the fold`, /frameless case/.test(text), text)
  const hidden = await pane.locator('[data-display-index="6"]').evaluate(el => getComputedStyle(el).visibility)
  check(`[${theme}] the pinned prompt's own row is hidden (visibility)`, hidden === 'hidden', hidden)
  // The band anchors inside the pane root: its card's top is at or below the pane's top.
  const geom = await page.evaluate(() => {
    const pane = document.querySelector('[data-chat-pane]').getBoundingClientRect()
    const card = document.querySelector('[data-testid="pinned-prompt"]').getBoundingClientRect()
    return { paneTop: pane.top, cardTop: card.top }
  })
  check(`[${theme}] banner sits inside the pane, under the host header`, geom.cardTop >= geom.paneTop - 1, JSON.stringify(geom))
  await page.screenshot({ path: join(OUT, `01-members-dm-pinned-${theme}.png`) })

  // 04 — image prompt: bring row 4 (the roster screenshot prompt) behind the fold.
  await pane.evaluate(el => { el.querySelector('.chat-container').scrollTop = 0 })
  await page.waitForTimeout(200)
  await scrollRowBehindFold(pane, 4, 60)
  await page.waitForTimeout(600)
  const imgCount = await banner.locator('img').count()
  check(`[${theme}] image prompt pins with its thumbnail`, imgCount >= 1, `imgs=${imgCount}`)
  await page.screenshot({ path: join(OUT, `04-members-dm-image-prompt-${theme}.png`) })

  // 02 — expand. A three-line prompt never clamps (no chevron, as on the main
  // chat); the image prompt always earns one, so the expanded frame is shot here.
  const chevron = banner.getByRole('button', { name: /expand pinned turn/i })
  await chevron.click()
  await page.waitForTimeout(400)
  check(`[${theme}] banner expanded`, (await banner.getByRole('button', { name: /collapse pinned turn/i }).count()) === 1)
  await page.screenshot({ path: join(OUT, `02-members-dm-expanded-${theme}.png`) })
  await banner.getByRole('button', { name: /collapse pinned turn/i }).click()
  await page.waitForTimeout(400)

  // 03 — jump back: click the banner body; the glide lands the prompt under the
  // band and the previous turn's prompt takes the pin.
  const before = await pane.evaluate(el => el.querySelector('.chat-container').scrollTop)
  await banner.getByTitle('Jump to this turn').click()
  await page.waitForTimeout(900)
  const after = await pane.evaluate(el => el.querySelector('.chat-container').scrollTop)
  check(`[${theme}] jump glided the transcript up`, after < before, `${before} -> ${after}`)
  const row4Visible = await pane.locator('[data-display-index="4"]').evaluate(el => {
    const r = el.getBoundingClientRect()
    const sc = document.querySelector('[data-chat-pane] .chat-container').getBoundingClientRect()
    return r.top >= sc.top && getComputedStyle(el).visibility !== 'hidden'
  })
  check(`[${theme}] the target prompt is back on screen and un-hidden`, row4Visible)
  await page.screenshot({ path: join(OUT, `03-members-dm-after-jump-${theme}.png`) })
  await ctx.close()
}

async function splitFrame(browser, theme) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: theme })
  const page = await ctx.newPage()
  await prime(page, theme)
  const layout = {
    [OTHER_SLOT]: {
      type: 'split', id: 'sp-pp', dir: 'col', sizes: [0.4, 0.6],
      children: [
        { type: 'leaf', id: 'lf-a', kind: 'session', slot: OTHER_SLOT },
        { type: 'leaf', id: 'lf-b', kind: 'session', slot: MEMBER_SLOT },
      ],
    },
  }
  await page.evaluate(l => { localStorage.setItem('mc-split-layouts', JSON.stringify(l)) }, layout)
  await page.goto(authed(`/chat/${OTHER_SLOT}`), { waitUntil: 'domcontentloaded' })
  const panes = page.locator('[data-chat-pane]')
  await panes.nth(1).waitFor({ state: 'visible', timeout: 20000 }).catch(async (e) => {
    await page.screenshot({ path: join(OUT, `debug-split-${theme}.png`) })
    throw e
  })
  check(`[${theme}] two split panes mounted`, (await panes.count()) === 2)
  // Find the pane hosting the member thread (its rows carry the DM transcript).
  const memberPane = panes.filter({ hasText: 'frameless case on the Members page' }).first()
  await memberPane.locator('[data-display-index="6"]').waitFor({ timeout: 20000 })
  // Let hydration and the follow controller's bottom re-pin settle before
  // scrolling away, or the re-pin lands after our scroll and the frame shows
  // the bottom of the thread instead of the middle.
  await page.waitForTimeout(2000)
  check(`[${theme}] split pane keeps its own title bar`, (await memberPane.locator('.border-b.bg-card').count()) === 1)
  await memberPane.evaluate(el => {
    const sc = el.querySelector('.chat-container')
    sc.scrollTop = 0
    sc.dispatchEvent(new Event('wheel'))
  })
  await page.waitForTimeout(300)
  await memberPane.evaluate(el => {
    const sc = el.querySelector('.chat-container')
    const row = sc.querySelector('[data-display-index="6"]')
    sc.scrollTop += row.getBoundingClientRect().bottom - sc.getBoundingClientRect().top + 60
  })
  await page.waitForTimeout(700)
  const banner = memberPane.locator(BANNER)
  await banner.waitFor({ state: 'visible', timeout: 5000 })
  const splitText = await banner.textContent()
  check(`[${theme}] split pane banner pins the prompt scrolled behind its fold`, /frameless case/.test(splitText || ''), splitText)
  const geom = await memberPane.evaluate(el => {
    const bar = el.querySelector('.border-b.bg-card').getBoundingClientRect()
    // The band (the card's absolute wrapper), not the card: the card itself
    // rides a translateY push while the next prompt approaches the fold.
    const band = el.querySelector('[data-testid="pinned-prompt"]').closest('.absolute').getBoundingClientRect()
    return { barBottom: bar.bottom, bandTop: band.top }
  })
  check(`[${theme}] banner band hangs off the pane's title bar`, Math.abs(geom.bandTop - geom.barBottom) <= 1, JSON.stringify(geom))
  await page.screenshot({ path: join(OUT, `05-split-pane-pinned-${theme}.png`) })
  await ctx.close()
}

async function recording(browser, theme) {
  const videoDir = join(OUT, '.video')
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    colorScheme: theme,
    recordVideo: { dir: videoDir, size: { width: 1440, height: 900 } },
  })
  const page = await ctx.newPage()
  const t0 = Date.now()
  await prime(page, theme)
  // Two loads: the first lands the theme the prime wrote (useTheme reads it at
  // boot), the second is the one on tape — the recording is trimmed to start
  // there, so the priming navigation is not in the clip.
  await page.goto(authed(`/members?member=${MEMBER}`), { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1000)
  const tStart = Date.now()
  await page.goto(authed(`/members?member=${MEMBER}`), { waitUntil: 'domcontentloaded' })
  const pane = page.locator('[data-chat-pane]').first()
  await pane.locator('[data-display-index]').nth(6).waitFor({ timeout: 20000 })
  await page.waitForTimeout(1500)
  // Start just above turn 3's prompt (row 6) so the wheel steps below carry it
  // through the hand-off line and on out; a real reader scrolls, not teleports.
  // Retried until it sticks: hydration lands the pane at the bottom and the
  // follow controller re-pins there until a user gesture releases it.
  const sc = pane.locator('.chat-container')
  for (let attempt = 0; attempt < 5; attempt++) {
    const placed = await sc.evaluate(el => {
      el.dispatchEvent(new Event('wheel'))
      const row = el.querySelector('[data-display-index="6"]')
      const target = el.scrollTop + row.getBoundingClientRect().top - el.getBoundingClientRect().top - 160
      el.scrollTop = target
      return Math.abs(el.scrollTop - target) < 2
    })
    await page.waitForTimeout(500)
    const held = await sc.evaluate(el => {
      const row = el.querySelector('[data-display-index="6"]')
      return Math.abs(row.getBoundingClientRect().top - el.getBoundingClientRect().top - 160) < 4
    })
    if (placed && held) break
  }
  const box = await sc.boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  // Wheel steps of 40px: the prompt slides up, hands over to the banner once
  // fully behind the band, then its reply scrolls under it.
  for (let i = 0; i < 16; i++) {
    await page.mouse.wheel(0, 40)
    await page.waitForTimeout(80)
  }
  await page.waitForTimeout(700)
  const banner = page.locator(BANNER)
  await banner.waitFor({ state: 'visible', timeout: 5000 })
  // A few more steps so the banner is read against the scrolling reply.
  for (let i = 0; i < 8; i++) {
    await page.mouse.wheel(0, 40)
    await page.waitForTimeout(80)
  }
  await page.waitForTimeout(700)
  // Morph: the image prompt is not on this stretch, so expand only if the
  // current banner earned a chevron; a three-line prompt has none (as on main).
  const chev = banner.getByRole('button', { name: /expand pinned turn/i })
  if (await chev.count()) {
    await chev.click(); await page.waitForTimeout(900)
    await banner.getByRole('button', { name: /collapse pinned turn/i }).click(); await page.waitForTimeout(700)
  }
  // Click-to-jump glide.
  await banner.getByTitle('Jump to this turn').click()
  await page.waitForTimeout(1600)
  const video = page.video()
  await ctx.close()
  const src = await video.path()
  const dst = join(OUT, `06-members-dm-pin-and-jump-${theme}.webm`)
  const ff = ffmpegBinary()
  const cut = ((tStart - t0) / 1000).toFixed(2)
  const trimmed = ff && spawnSync(ff, ['-v', 'error', '-y', '-ss', cut, '-i', src, '-c', 'copy', dst], { stdio: 'inherit' }).status === 0
  if (!trimmed) renameSync(src, dst)
  rmSync(videoDir, { recursive: true, force: true })
  console.log(trimmed ? `recorded ${dst} (trimmed ${cut}s of priming)` : `recorded ${dst} (untrimmed: no ffmpeg)`)
}

/** Playwright's own ffmpeg (what it records with), else one on PATH. */
function ffmpegBinary() {
  const cache = join(homedir(), '.cache', 'ms-playwright')
  if (existsSync(cache)) {
    for (const d of readdirSync(cache).filter(n => n.startsWith('ffmpeg')).sort().reverse()) {
      for (const bin of ['ffmpeg-linux', 'ffmpeg-mac', 'ffmpeg-win64.exe']) {
        const p = join(cache, d, bin)
        if (existsSync(p)) return p
      }
    }
  }
  return spawnSync('ffmpeg', ['-version']).status === 0 ? 'ffmpeg' : null
}

const browser = await chromium.launch()
try {
  for (const theme of ['light', 'dark']) {
    await membersFrames(browser, theme)
    await splitFrame(browser, theme)
  }
  await recording(browser, 'light')
  console.log('wrote', OUT)
} finally {
  await browser.close()
}
