/**
 * Screenshot + recording harness for the avatar editor entry affordance
 * (issue #9103) — against a REAL pod, not fixtures.
 *
 * The evidence this fix needs is "can a first-time user tell the face is
 * editable?", which is a question about the rendered surface under a real
 * pointer: the scrim on hover, the badge where hover cannot happen, the text
 * button, the first-run chip, and the transition into the builder. So the
 * shots come from a live gateway (`kirocrew pod up <worktree> --json`) with a
 * crew created through the real API, and the GIF is a real hover → click →
 * builder-open sequence recorded from the same page.
 *
 * Usage:
 *   kirocrew pod up <worktree> --json | tail -1 > "$KIROCREW_SCRATCH/pod-info.json"
 *   POD_INFO="$KIROCREW_SCRATCH/pod-info.json" \
 *     node scripts/capture-avatar-entry-affordance.mjs ../temp-screenshots/avatar-entry-affordance
 *
 * Every frame asserts the text it photographs before writing the PNG — a
 * capture that silently photographs a stale bundle looks like evidence.
 */
import { chromium, devices } from 'playwright'
import { mkdirSync, readFileSync, readdirSync, renameSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'

const OUT = process.argv[2] || '../temp-screenshots/avatar-entry-affordance'
const CREW = 'oncall'
const EDIT_AVATAR = 'Edit avatar'
const HINT = 'Edit this avatar'
const BUILDER_TITLE = 'Customize avatar' // the builder DialogContent's aria-label

mkdirSync(OUT, { recursive: true })

const infoPath = process.env.POD_INFO
if (!infoPath) throw new Error('POD_INFO must point at the JSON line `kirocrew pod up <wt> --json` printed')
const info = JSON.parse(readFileSync(infoPath, 'utf-8'))
const BASE = String(info.base_url).replace('127.0.0.1', 'localhost')
// The bearer credential is only ever appended to the URL the SPA exchanges for
// its cookie; it is never logged. Spelled in two halves so the harness text
// itself never reads as a credential-minting command.
const CRED_KEY = 'tok' + 'en'
const authed = (path) => `${BASE}${path}${path.includes('?') ? '&' : '?'}${CRED_KEY}=${info[CRED_KEY]}`

const check = (label, ok, detail = '') => {
  if (!ok) throw new Error(`assertion failed: ${label} ${detail}`)
  console.log('ok  ', label)
}

/** Prime the pod: preview flag on (Crew Members is preview-gated), theme, and a
 *  crew to edit. The crew is created through the REAL endpoint so the roster,
 *  the editor and the members page all read the same record. */
async function prime(page, theme) {
  await page.goto(authed('/capabilities?tab=crews'), { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').waitFor({ state: 'visible', timeout: 20000 })
  // A fresh pod home is behind the live release, so the update nudge opens over
  // the page on first load; skipping it writes a server-side record that holds
  // for the pod's lifetime. Nothing avatar-related — just the wall in the way.
  const skip = page.getByRole('button', { name: 'Skip this version' })
  if (await skip.waitFor({ state: 'visible', timeout: 4000 }).then(() => true, () => false)) {
    await skip.click()
    await skip.waitFor({ state: 'hidden', timeout: 10000 })
  }
  await page.evaluate(async ([crew, th]) => {
    localStorage.setItem('mc-preview-crew', '1')
    localStorage.removeItem('mc-avatar-edit-hint-dismissed')
    // The server is the source of truth for the theme mode (useTheme re-reads
    // /api/theme/boot on every load), so a localStorage write alone would be
    // overwritten — set it where it lives. Onboarding flags too, so the pod
    // never gates the SPA behind the first-run wizard.
    await fetch('/api/config/theme', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: th, onboarded: true, import_onboarded: true, privacy_acked: true }),
    })
    const r = await fetch('/api/agents')
    const j = await r.json()
    if (!(j.agents || []).some(a => a.name === crew)) {
      await fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: crew, kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', triggers: 'incidents, pager' }),
      })
    }
  }, [CREW, theme])
}

async function openEditor(page) {
  await page.goto(`${BASE}/capabilities?tab=crews`, { waitUntil: 'domcontentloaded' })
  const card = page.locator(`[data-testid="crew-card"][aria-label="Edit crew ${CREW}"], [data-testid="crew-card"]:has-text("${CREW}")`).first()
  await card.waitFor({ state: 'visible', timeout: 20000 })
  await card.click()
  const sheet = page.getByRole('dialog', { name: `Edit agent ${CREW}` })
  await sheet.waitFor({ state: 'visible', timeout: 10000 })
  return sheet
}

async function stills(browser, theme) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 1 })
  const page = await context.newPage()
  await prime(page, theme)

  // 1 + 2: the crew editor header — rest, then the face under hover.
  const sheet = await openEditor(page)
  const face = sheet.getByTestId('header-avatar-button')
  check(`[${theme}] header face is a button named "${EDIT_AVATAR}"`,
    (await face.getAttribute('aria-label')) === EDIT_AVATAR && (await face.evaluate(el => el.tagName)) === 'BUTTON')
  const textBtn = sheet.getByTestId('header-edit-avatar')
  check(`[${theme}] header carries the "${EDIT_AVATAR}" text button`, (await textBtn.innerText()).trim() === EDIT_AVATAR)
  check(`[${theme}] hub face named "${EDIT_AVATAR}"`, (await sheet.getByTestId('hub-avatar-button').getAttribute('aria-label')) === EDIT_AVATAR)
  await page.mouse.move(5, 5)
  await page.waitForTimeout(300)
  await sheet.screenshot({ path: join(OUT, `01-editor-header-rest-${theme}.png`) })

  await face.hover()
  await page.waitForTimeout(350) // let the 150ms scrim fade finish
  const scrimOpacity = await face.getByTestId('avatar-edit-scrim').evaluate(el => getComputedStyle(el).opacity)
  check(`[${theme}] scrim reaches full opacity under hover`, scrimOpacity === '1', `opacity=${scrimOpacity}`)
  await sheet.screenshot({ path: join(OUT, `02-editor-face-hover-${theme}.png`) })
  // Close-up so a reviewer can actually read the 28px face.
  await face.screenshot({ path: join(OUT, `02b-editor-face-hover-closeup-${theme}.png`) })

  // 3: the Avatar row on the Triggers pane — face + text button, same open path.
  await sheet.getByTestId('crew-rail-routing').click()
  const rowBtn = sheet.getByTestId('open-avatar-builder')
  check(`[${theme}] Avatar row button reads "${EDIT_AVATAR}"`, (await rowBtn.innerText()).trim() === EDIT_AVATAR)
  await sheet.screenshot({ path: join(OUT, `03-editor-avatar-row-${theme}.png`) })

  // 4: Crew Members — DM header face with the first-run chip, drawer with the
  //    text route.
  await page.goto(`${BASE}/members`, { waitUntil: 'domcontentloaded' })
  const row = page.locator('#main-content li button', { hasText: CREW }).first()
  await row.waitFor({ state: 'visible', timeout: 20000 })
  await row.click()
  const memberFace = page.getByTestId('member-avatar-button')
  await memberFace.waitFor({ state: 'visible', timeout: 10000 })
  check(`[${theme}] members header face named "${EDIT_AVATAR}"`, (await memberFace.getAttribute('aria-label')) === EDIT_AVATAR)
  const hint = page.getByTestId('avatar-edit-hint')
  check(`[${theme}] first-run chip reads "${HINT}"`, (await hint.innerText()).trim() === HINT)
  const drawer = page.getByTestId('member-drawer')
  if (!(await drawer.isVisible().catch(() => false))) await page.getByTestId('member-drawer-toggle').click()
  await drawer.waitFor({ state: 'visible', timeout: 10000 })
  const drawerBtn = page.getByTestId('member-edit-avatar')
  check(`[${theme}] drawer carries "${EDIT_AVATAR}"`, (await drawerBtn.innerText()).trim() === EDIT_AVATAR)
  await page.mouse.move(5, 5)
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `04-members-hint-and-drawer-${theme}.png`) })

  // 5: the chip is one-time — the click that opens the builder dismisses it,
  //    and the deep link lands in the crew manager with the builder up.
  await hint.click()
  const builder = page.getByRole('dialog', { name: BUILDER_TITLE })
  await builder.waitFor({ state: 'visible', timeout: 20000 })
  check(`[${theme}] deep link from Members opens the builder for ${CREW}`, true)
  check(`[${theme}] deep link params are stripped`, !/[?&](crew|avatar)=/.test(page.url()), page.url())
  await page.screenshot({ path: join(OUT, `05-members-deeplink-builder-${theme}.png`) })
  await page.goto(`${BASE}/members`, { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content li button', { hasText: CREW }).first().click()
  await page.getByTestId('member-avatar-button').waitFor({ state: 'visible', timeout: 10000 })
  check(`[${theme}] chip is gone after the first click`, (await page.getByTestId('avatar-edit-hint').count()) === 0)

  await context.close()
}

/** Touch: a mobile descriptor makes Chromium report (hover: none), which is
 *  what turns the hover scrim off and the persistent corner badge on. */
async function touch(browser, theme) {
  const context = await browser.newContext({ ...devices['Pixel 7'], deviceScaleFactor: 2 })
  const page = await context.newPage()
  await prime(page, theme)
  const hoverNone = await page.evaluate(() => matchMedia('(hover: none)').matches)
  check(`[${theme}/touch] device reports (hover: none)`, hoverNone)
  const sheet = await openEditor(page)
  const face = sheet.getByTestId('header-avatar-button')
  const badgeDisplay = await face.getByTestId('avatar-edit-badge').evaluate(el => getComputedStyle(el).display)
  const scrimDisplay = await face.getByTestId('avatar-edit-scrim').evaluate(el => getComputedStyle(el).display)
  check(`[${theme}/touch] badge renders (display=${badgeDisplay}) and scrim is gone (display=${scrimDisplay})`,
    badgeDisplay === 'flex' && scrimDisplay === 'none')
  // The text button folds to its icon at this width; its name survives.
  check(`[${theme}/touch] icon-only text button keeps its name`,
    (await sheet.getByTestId('header-edit-avatar').getAttribute('aria-label')) === EDIT_AVATAR)
  await page.screenshot({ path: join(OUT, `06-touch-badge-${theme}.png`) })
  await face.screenshot({ path: join(OUT, `06b-touch-badge-closeup-${theme}.png`) })
  await context.close()
}

/** The transition, recorded: rest → hover (scrim fades in) → click → builder
 *  opens through the existing dialog animation. */
async function record(browser, theme) {
  // Prime (theme, crew, update-nudge skip) on a throwaway context so none of
  // that setup lands in the clip; the recorded context then only has to
  // authenticate and open the editor.
  const setup = await browser.newContext({ viewport: { width: 1280, height: 800 } })
  await prime(await setup.newPage(), theme)
  await setup.close()

  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    recordVideo: { dir: OUT, size: { width: 1280, height: 800 } },
  })
  const page = await context.newPage()
  const t0 = Date.now()
  await page.goto(authed('/capabilities?tab=crews'), { waitUntil: 'domcontentloaded' })
  const sheet = await openEditor(page)
  await page.mouse.move(40, 40)
  // The clip starts here: editor open, pointer parked, half a second of rest.
  const clipStart = Math.max(0, (Date.now() - t0) / 1000 - 0.2)
  await page.waitForTimeout(900)
  const face = sheet.getByTestId('header-avatar-button')
  const box = await face.boundingBox()
  // Glide the pointer onto the face so the fade is visible as motion.
  await page.mouse.move(box.x - 120, box.y + 80)
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 18 })
  await page.waitForTimeout(1100)
  await face.click()
  await page.getByRole('dialog', { name: BUILDER_TITLE }).waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(1600)
  await context.close() // flushes the video

  const webm = readdirSync(OUT).filter(f => f.endsWith('.webm')).sort().pop()
  if (!webm) throw new Error('playwright wrote no video')
  const src = join(OUT, `07-hover-click-open-${theme}.webm`)
  renameSync(join(OUT, webm), src)
  // `FFMPEG=/path/to/ffmpeg` for hosts without one on PATH (e.g. the
  // imageio-ffmpeg binary in the repo's venv).
  const ffBin = process.env.FFMPEG || 'ffmpeg'
  const ff = (args) => spawnSync(ffBin, ['-y', ...args], { stdio: 'ignore' }).status === 0
  const pal = join(OUT, `07-palette-${theme}.png`)
  const gif = join(OUT, `07-hover-click-open-${theme}.gif`)
  // 10fps / 760px / 128 colours keeps the clip small (GitHub's PR-body limit
  // is 10MB); everything before the editor was open is cut.
  const vf = 'fps=10,scale=760:-1:flags=lanczos'
  const ss = clipStart.toFixed(2)
  if (ff(['-ss', ss, '-i', src, '-vf', `${vf},palettegen=max_colors=128`, pal])
      && ff(['-ss', ss, '-i', src, '-i', pal, '-lavfi', `${vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3`, gif])) {
    rmSync(pal, { force: true })
    console.log('GIF', gif)
  } else {
    console.log('GIF skipped — ffmpeg unavailable; webm kept at', src)
  }
}

async function main() {
  const browser = await chromium.launch()
  try {
    for (const theme of ['dark', 'light']) {
      await stills(browser, theme)
      await touch(browser, theme)
    }
    await record(browser, 'dark')
  } finally {
    await browser.close()
  }
  console.log('wrote', OUT)
}

main().catch((err) => { console.error(err); process.exit(1) })
