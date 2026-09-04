/**
 * Screenshot + recording harness for the Crew Members "Edit member" entry
 * (issue #9425) — the #9116 follow-up that replaces the chat surface's
 * oversized "Edit avatar" control with a small pencil RIGHT OF THE NAME in the
 * DM-header title row, revealed on hover, opening the member's whole editor.
 * Against a REAL pod, not fixtures.
 *
 * What the evidence has to show, per frame:
 *   - at rest the title row is face + name — the pencil is invisible, there is
 *     no text button and no chip, and the face is NOT an edit button;
 *   - hovering the title row fades the pencil in; it is named "Edit member";
 *   - the drawer has one text route ("Edit in crew manager"), no "Edit avatar";
 *   - a click lands in the crew manager on THIS member's full editor (name,
 *     template, model, workspace, triggers, avatar), NOT in the avatar builder;
 *   - under (hover: none) the pencil stays visible at low contrast;
 *   - there is NO rule between the header and the transcript — they share one
 *     background and meet on spacing alone, as ChatPage's session header does.
 *
 * Usage:
 *   kirocrew pod up <worktree> --json | tail -1 > "$KIROCREW_SCRATCH/pod-info.json"
 *   POD_INFO="$KIROCREW_SCRATCH/pod-info.json" \
 *     node scripts/capture-members-hover-edit-member.mjs ../temp-screenshots/members-dm-hover-edit-member
 *
 * Every frame asserts the text it photographs before writing the PNG — a
 * capture that silently photographs a stale bundle looks like evidence.
 */
import { chromium, devices } from 'playwright'
import { mkdirSync, readFileSync, readdirSync, renameSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { check, podInfo, primeCrewPod } from './lib/crew-pod-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/members-dm-hover-edit-member'
const CREW = 'oncall'
const EDIT_MEMBER = 'Edit member'
const EDIT_AVATAR = 'Edit avatar'
const EDIT_IN_MANAGER = 'Edit in crew manager'
const BUILDER_TITLE = 'Customize avatar' // the builder DialogContent's aria-label
const EDITOR_TITLE = `Edit agent ${CREW}` // the crew editor DialogContent's aria-label

mkdirSync(OUT, { recursive: true })

const { BASE, authed } = podInfo(readFileSync)

const prime = (page, theme) => primeCrewPod(page, authed, CREW, theme)

/** Land on the Crew Members page with the crew's DM thread open. */
async function openMember(page) {
  await page.goto(`${BASE}/members`, { waitUntil: 'domcontentloaded' })
  const row = page.locator('#main-content li button', { hasText: CREW }).first()
  await row.waitFor({ state: 'visible', timeout: 20000 })
  await row.click()
  const titleRow = page.getByTestId('member-title-row')
  await titleRow.waitFor({ state: 'visible', timeout: 10000 })
  return { titleRow, pencil: page.getByTestId('member-edit-name-button') }
}

/** The DM header row (face + name + pencil + drawer toggle) as a tight crop,
 *  so a reviewer sees the affordance at 1:1 instead of hunting a 13px glyph
 *  in a 1400px page. */
async function shootHeader(page, path) {
  await page.getByTestId('member-thread-header').screenshot({ path })
}

/** The seam: the header plus the first stretch of transcript under it, so the
 *  presence or absence of a rule between the two is what the frame shows. */
async function shootSeam(page, path) {
  const box = await page.getByTestId('member-thread-header').boundingBox()
  await page.screenshot({ path, clip: { x: box.x, y: box.y, width: box.width, height: box.height + 56 } })
}

async function stills(browser, theme) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await prime(page, theme)

  // 1: the DM header at rest — face + name, pencil invisible, nothing else
  //    that says "edit". The face is a plain face, not a button.
  const { titleRow, pencil } = await openMember(page)
  check(`[${theme}] pencil is a button named "${EDIT_MEMBER}"`,
    (await pencil.getAttribute('aria-label')) === EDIT_MEMBER && (await pencil.evaluate(el => el.tagName)) === 'BUTTON')
  check(`[${theme}] pencil sits inside the title row, after the name`,
    await titleRow.evaluate((row, id) => {
      const btn = row.querySelector(`[data-testid="${id}"]`)
      return !!btn && row.textContent.includes('oncall') && !!(btn.compareDocumentPosition(row.firstElementChild) & Node.DOCUMENT_POSITION_PRECEDING)
    }, 'member-edit-name-button'))
  check(`[${theme}] the face is not an edit button (no scrim, no badge)`,
    (await page.getByTestId('member-avatar-button').count()) === 0
      && (await page.getByTestId('avatar-edit-scrim').count()) === 0
      && (await page.getByTestId('avatar-edit-badge').count()) === 0)
  check(`[${theme}] no first-run chip on the chat surface`, (await page.getByTestId('avatar-edit-hint').count()) === 0)
  check(`[${theme}] no "${EDIT_AVATAR}" text button on the chat surface`,
    (await page.getByTestId('member-edit-avatar').count()) === 0
      && (await page.getByRole('button', { name: EDIT_AVATAR, exact: true }).count()) === 0)
  await page.mouse.move(5, 5)
  await page.waitForTimeout(300)
  const restOpacity = await pencil.evaluate(el => getComputedStyle(el).opacity)
  check(`[${theme}] pencil invisible at rest`, restOpacity === '0', `opacity=${restOpacity}`)
  const headerEl = page.getByTestId('member-thread-header')
  const rule = await headerEl.evaluate(el => getComputedStyle(el).borderBottomWidth)
  check(`[${theme}] no rule under the header (border-bottom-width=${rule})`, rule === '0px')
  await page.screenshot({ path: join(OUT, `01-members-dm-rest-${theme}.png`) })
  await shootHeader(page, join(OUT, `01b-header-row-rest-${theme}.png`))
  await shootSeam(page, join(OUT, `01c-header-seam-after-${theme}.png`))

  // 2: hover the NAME (not the pencil) — the whole title row is the hover
  //    target, so the pencil fades in before the pointer reaches it.
  await titleRow.locator('div').first().hover()
  await page.waitForTimeout(350) // let the 150ms fade finish
  const hoverOpacity = await pencil.evaluate(el => getComputedStyle(el).opacity)
  check(`[${theme}] pencil reaches full opacity when the title row is hovered`, hoverOpacity === '1', `opacity=${hoverOpacity}`)
  await page.screenshot({ path: join(OUT, `02-members-dm-hover-${theme}.png`) })
  await shootHeader(page, join(OUT, `02b-header-row-hover-${theme}.png`))
  // Hovering the drawer toggle to the right must NOT reveal it.
  await page.getByTestId('member-drawer-toggle').hover()
  await page.waitForTimeout(350)
  const toggleHoverOpacity = await pencil.evaluate(el => getComputedStyle(el).opacity)
  check(`[${theme}] hovering the drawer toggle leaves the pencil hidden`, toggleHoverOpacity === '0', `opacity=${toggleHoverOpacity}`)

  // 3: the drawer — plain face in its header, one text route, no "Edit avatar".
  const drawer = page.getByTestId('member-drawer')
  if (!(await drawer.isVisible().catch(() => false))) await page.getByTestId('member-drawer-toggle').click()
  await drawer.waitFor({ state: 'visible', timeout: 10000 })
  const manager = page.getByTestId('member-edit-in-manager')
  check(`[${theme}] drawer carries "${EDIT_IN_MANAGER}" only`,
    (await manager.innerText()).trim() === EDIT_IN_MANAGER && (await page.getByTestId('member-edit-avatar').count()) === 0)
  await page.mouse.move(5, 5)
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `03-members-drawer-${theme}.png`) })

  // 4: click the pencil — the crew manager opens THIS member's full editor;
  //    the avatar builder is NOT open on top of it.
  await titleRow.locator('div').first().hover()
  await page.waitForTimeout(250)
  await pencil.click()
  const editor = page.getByRole('dialog', { name: EDITOR_TITLE })
  await editor.waitFor({ state: 'visible', timeout: 20000 })
  check(`[${theme}] click opens the crew editor for ${CREW}`, true)
  check(`[${theme}] the avatar builder is NOT open`, (await page.getByRole('dialog', { name: BUILDER_TITLE }).count()) === 0)
  check(`[${theme}] deep link params are stripped`, !/[?&](crew|avatar)=/.test(page.url()), page.url())
  // The editor still owns the builder entry: header face + "Edit avatar" text button.
  check(`[${theme}] editor header keeps its "${EDIT_AVATAR}" entry`,
    (await editor.getByTestId('header-edit-avatar').getAttribute('aria-label')) === EDIT_AVATAR)
  await page.mouse.move(5, 5)
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, `04-members-click-editor-${theme}.png`) })

  await context.close()
}

/** Touch: a mobile descriptor makes Chromium report (hover: none), where a
 *  hover-revealed pencil could never be reached — so it stays on at low
 *  contrast instead. */
async function touch(browser, theme) {
  const context = await browser.newContext({ ...devices['Pixel 7'], deviceScaleFactor: 2 })
  const page = await context.newPage()
  await prime(page, theme)
  const hoverNone = await page.evaluate(() => matchMedia('(hover: none)').matches)
  check(`[${theme}/touch] device reports (hover: none)`, hoverNone)
  const { pencil } = await openMember(page)
  const opacity = Number(await pencil.evaluate(el => getComputedStyle(el).opacity))
  check(`[${theme}/touch] pencil persistent at low contrast (opacity=${opacity})`, opacity > 0.3 && opacity < 1)
  check(`[${theme}/touch] pencil named "${EDIT_MEMBER}"`, (await pencil.getAttribute('aria-label')) === EDIT_MEMBER)
  check(`[${theme}/touch] no avatar badge on the face`, (await page.getByTestId('avatar-edit-badge').count()) === 0)
  await page.screenshot({ path: join(OUT, `05-touch-pencil-${theme}.png`) })
  await shootHeader(page, join(OUT, `05b-touch-header-row-${theme}.png`))
  await context.close()
}

/** The transition, recorded: rest → pointer glides onto the name (pencil fades
 *  in) → click → the crew editor opens through the existing dialog animation. */
async function record(browser, theme) {
  // Prime (theme, crew, update-nudge skip) on a throwaway context so none of
  // that setup lands in the clip.
  const setup = await browser.newContext({ viewport: { width: 1280, height: 800 } })
  await prime(await setup.newPage(), theme)
  await setup.close()

  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    recordVideo: { dir: OUT, size: { width: 1280, height: 800 } },
  })
  const page = await context.newPage()
  const t0 = Date.now()
  await page.goto(authed('/members'), { waitUntil: 'domcontentloaded' })
  const row = page.locator('#main-content li button', { hasText: CREW }).first()
  await row.waitFor({ state: 'visible', timeout: 20000 })
  await row.click()
  const titleRow = page.getByTestId('member-title-row')
  await titleRow.waitFor({ state: 'visible', timeout: 10000 })
  const pencil = page.getByTestId('member-edit-name-button')
  await page.mouse.move(40, 40)
  // The clip starts here: thread open, pointer parked, a moment of rest.
  const clipStart = Math.max(0, (Date.now() - t0) / 1000 - 0.2)
  await page.waitForTimeout(900)
  const name = await titleRow.locator('div').first().boundingBox()
  // Glide the pointer onto the NAME so the pencil's fade is visible as motion
  // before the pointer ever reaches it.
  await page.mouse.move(name.x - 40, name.y + 120)
  await page.mouse.move(name.x + Math.min(40, name.width / 2), name.y + name.height / 2, { steps: 18 })
  await page.waitForTimeout(1100)
  const pb = await pencil.boundingBox()
  await page.mouse.move(pb.x + pb.width / 2, pb.y + pb.height / 2, { steps: 8 })
  await page.waitForTimeout(400)
  await pencil.click()
  await page.getByRole('dialog', { name: EDITOR_TITLE }).waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(1600)
  await context.close() // flushes the video

  const webm = readdirSync(OUT).filter(f => f.endsWith('.webm')).sort().pop()
  if (!webm) throw new Error('playwright wrote no video')
  const src = join(OUT, `06-hover-click-open-editor-${theme}.webm`)
  renameSync(join(OUT, webm), src)
  // `FFMPEG=/path/to/ffmpeg` for hosts without one on PATH.
  const ffBin = process.env.FFMPEG || 'ffmpeg'
  const ff = (args) => spawnSync(ffBin, ['-y', ...args], { stdio: 'ignore' }).status === 0
  const pal = join(OUT, `06-palette-${theme}.png`)
  const gif = join(OUT, `06-hover-click-open-editor-${theme}.gif`)
  // 10fps / 760px / 128 colours keeps the clip small (GitHub's PR-body limit
  // is 10MB); everything before the thread was open is cut.
  const vf = 'fps=10,scale=760:-1:flags=lanczos'
  const ss = clipStart.toFixed(2)
  if (ff(['-ss', ss, '-i', src, '-vf', `${vf},palettegen=max_colors=128`, pal])
      && ff(['-ss', ss, '-i', src, '-i', pal, '-lavfi', `${vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3`, gif])) {
    rmSync(pal, { force: true })
    rmSync(src, { force: true })
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
