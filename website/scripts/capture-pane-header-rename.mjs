/**
 * Evidence for #9727 — split-view pane headers carry the same rename (Pen /
 * inline editor) and regenerate (Sparkles) controls as the single-session
 * header. Against a REAL pod, not fixtures.
 *
 *   kirocrew pod up <worktree> --json | tail -1 > "$KIROCREW_SCRATCH/pod-info.json"
 *   # the pod needs dashboard.session_grid = true and >= 4 sessions:
 *   #   kirocrew pod api <wt> PUT /api/dashboard/config --allow-write --data '{"session_grid": true}'
 *   #   kirocrew pod api <wt> GET /api/chat/slots   -> pass four keys as PANE_SLOTS
 *   POD_INFO="$KIROCREW_SCRATCH/pod-info.json" PANE_SLOTS=a,b,c,d \
 *     node scripts/capture-pane-header-rename.mjs ../temp-screenshots/pane-header-rename
 *   # then verify the rename landed: kirocrew pod api <wt> GET /api/chat/slots
 *
 * Frames:
 *   1. pane-header-rest.png      — 2x2 split at rest (controls hidden, titles only)
 *   2. pane-header-hover.png     — pointer on one pane's title bar: Pen + Sparkles revealed
 *   3. pane-header-editing.png   — title clicked: inline editor open in that pane
 *   4. pane-header-renamed.png   — Enter committed; the pane shows the new title
 *   5. pane-header-rename-error.png — the server refused a rename (route forced
 *      to 500): title rolled back, in-pane error notice
 *   6. pane-header-generating.png — Sparkles pressed, response held: per-pane spinner
 *   7. pane-header-rename.webm / .gif — click → inline editor → Enter, recorded
 *      (the persistent title element changing state; ffmpeg from Playwright's cache)
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, renameSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'
import { execFileSync } from 'node:child_process'
import { check, podInfo } from './lib/crew-pod-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/pane-header-rename'
mkdirSync(OUT, { recursive: true })
const { authed } = podInfo(readFileSync)
const NEW_TITLE = 'Renamed from the pane header'

const uid = () => Math.random().toString(36).slice(2)
const leaf = (slot) => ({ type: 'leaf', id: uid(), kind: 'session', slot })
const grid2x2 = (slots) => ({
  type: 'split', id: uid(), dir: 'row', sizes: [0.5, 0.5],
  children: [
    { type: 'split', id: uid(), dir: 'col', sizes: [0.5, 0.5], children: [leaf(slots[0]), leaf(slots[1])] },
    { type: 'split', id: uid(), dir: 'col', sizes: [0.5, 0.5], children: [leaf(slots[2]), leaf(slots[3])] },
  ],
})

async function main() {
  // The pod's slot keys come from the shell (`kirocrew pod api <wt> GET
  // /api/chat/slots`): a bare fetch from here carries no credential.
  const slots = (process.env.PANE_SLOTS || '').split(',').map((k) => k.trim()).filter(Boolean)
  check('PANE_SLOTS names >= 4 sessions for a 2x2 grid', slots.length >= 4, `got ${slots.length}`)
  const four = slots.slice(0, 4)

  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2,
    // One context, one credential exchange: the whole run is recorded and the
    // click → edit → Enter segment is cut out of it afterwards.
    recordVideo: { dir: OUT, size: { width: 1400, height: 900 } },
  })
  const t0 = Date.now()
  // The split layout is per anchor slot in localStorage; seed it BEFORE the SPA
  // boots so one navigation (one credential exchange) is all this run needs.
  await context.addInitScript(([anchor, tree]) => {
    if (!localStorage.getItem('mc-split-layouts')) localStorage.setItem('mc-split-layouts', JSON.stringify({ [anchor]: tree }))
  }, [four[0], grid2x2(four)])
  const page = await context.newPage()
  await page.goto(authed(`/chat/${four[0]}`), { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').waitFor({ state: 'visible', timeout: 20000 })
  const skip = page.getByRole('button', { name: 'Skip this version' })
  if (await skip.waitFor({ state: 'visible', timeout: 4000 }).then(() => true, () => false)) {
    await skip.click()
    await skip.waitFor({ state: 'hidden', timeout: 10000 })
  }

  // A persisted layout for the anchor re-enters the grid on load; otherwise
  // the header offers "Enter split view" (or "Return to split view").
  const panes = page.locator('[data-chat-pane]')
  const inGrid = await panes.first().waitFor({ state: 'visible', timeout: 8000 }).then(() => true, () => false)
  if (!inGrid) {
    const enter = page.getByRole('button', { name: /Return to split view|Enter split view/ }).first()
    await enter.waitFor({ state: 'visible', timeout: 20000 })
    await enter.click()
  }
  await panes.nth(3).waitFor({ state: 'visible', timeout: 20000 })
  check('2x2 grid renders four panes', (await panes.count()) === 4, `count=${await panes.count()}`)

  // Every pane header carries the regenerate button (hidden until hover).
  const regenButtons = page.getByRole('button', { name: 'Regenerate title with LLM' })
  check('every pane header has a Sparkles button', (await regenButtons.count()) === 4, `count=${await regenButtons.count()}`)

  await page.mouse.move(700, 850)
  await page.waitForTimeout(600)
  await page.screenshot({ path: join(OUT, 'pane-header-rest.png') })

  // Hover the top-left pane's header: Pen + Sparkles fade in.
  const targetPane = panes.nth(0)
  const header = targetPane.locator('.group\\/header').first()
  const titleBtn = header.getByRole('button').first()
  const box = await header.boundingBox()
  await page.mouse.move(box.x + 60, box.y + box.height / 2, { steps: 12 })
  await page.waitForTimeout(500)
  const sparkOpacity = await header.getByRole('button', { name: 'Regenerate title with LLM' })
    .evaluate((el) => getComputedStyle(el).opacity)
  check('Sparkles is revealed on header hover', Number(sparkOpacity) > 0, `opacity=${sparkOpacity}`)
  await page.screenshot({ path: join(OUT, 'pane-header-hover.png') })

  // Click the title: inline editor seeded with the current title.
  const before = (await titleBtn.textContent())?.trim()
  const clipStart = (Date.now() - t0) / 1000 - 0.6
  await titleBtn.click()
  const input = targetPane.locator('input[value]').first()
  await input.waitFor({ state: 'visible', timeout: 5000 })
  check('editor is seeded with the pane title', (await input.inputValue()) === before, `${await input.inputValue()} vs ${before}`)
  await page.screenshot({ path: join(OUT, 'pane-header-editing.png') })

  // Enter commits: the pane (and the store) show the new title.
  await page.waitForTimeout(500)
  await input.fill(NEW_TITLE)
  await page.waitForTimeout(400)
  await input.press('Enter')
  await targetPane.getByRole('button', { name: NEW_TITLE }).waitFor({ state: 'visible', timeout: 5000 })
  await page.waitForTimeout(900)
  const clipEnd = (Date.now() - t0) / 1000
  await page.screenshot({ path: join(OUT, 'pane-header-renamed.png') })

  // Failure state: the server refuses the next rename. The title rolls back and
  // the pane reports it inline.
  await page.route('**/api/chat/slots/*/title', (route) => route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'title store unavailable' }) }))
  await targetPane.getByRole('button', { name: NEW_TITLE }).click()
  const input2 = targetPane.locator('input[value]').first()
  await input2.waitFor({ state: 'visible', timeout: 5000 })
  await input2.fill('This rename is refused')
  await input2.press('Enter')
  const notice = targetPane.getByTestId('chat-pane-title-error')
  await notice.waitFor({ state: 'visible', timeout: 5000 })
  await targetPane.getByRole('button', { name: NEW_TITLE }).waitFor({ state: 'visible', timeout: 5000 })
  check('refused rename rolls the title back', (await targetPane.getByRole('button', { name: /This rename is refused/ }).count()) === 0)
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, 'pane-header-rename-error.png') })
  await page.unroute('**/api/chat/slots/*/title')
  await notice.getByRole('button').first().click().catch(() => {})

  // Generating state: hold the regenerate response so the spinner is on screen.
  let releaseGenerate
  await page.route('**/api/chat/slots/*/generate-title', (route) => {
    releaseGenerate = () => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ title: 'Generated by the assistant' }) })
  })
  const hdrBox = await header.boundingBox()
  await page.mouse.move(hdrBox.x + 60, hdrBox.y + hdrBox.height / 2, { steps: 8 })
  await header.getByRole('button', { name: 'Regenerate title with LLM' }).click()
  await header.locator('.animate-spin').waitFor({ state: 'visible', timeout: 5000 })
  check('Sparkles is replaced by the spinner while generating', (await header.getByRole('button', { name: 'Regenerate title with LLM' }).count()) === 0)
  await page.screenshot({ path: join(OUT, 'pane-header-generating.png') })
  releaseGenerate?.()
  await targetPane.getByRole('button', { name: 'Generated by the assistant' }).waitFor({ state: 'visible', timeout: 5000 })
  await page.unroute('**/api/chat/slots/*/generate-title')

  const video = page.video()
  await context.close()
  const webm = join(OUT, 'pane-header-rename.webm')
  renameSync(await video.path(), webm)
  // Cut the click → edit → Enter segment and make a GIF beside it.
  const ffmpeg = process.env.FFMPEG || join(homedir(), '.cache', 'ms-playwright', 'ffmpeg-1011', 'ffmpeg-linux')
  if (existsSync(ffmpeg)) {
    const clip = join(OUT, 'pane-header-rename-clip.webm')
    execFileSync(ffmpeg, ['-y', '-loglevel', 'error', '-ss', String(Math.max(0, clipStart)), '-to', String(clipEnd), '-i', webm, '-c', 'copy', clip])
    renameSync(clip, webm)
    // Playwright's bundled ffmpeg ships without a GIF muxer; a system ffmpeg
    // (FFMPEG=...) adds the GIF beside the clip, otherwise the webm is the evidence.
    try {
      execFileSync(ffmpeg, ['-y', '-loglevel', 'error', '-i', webm, '-vf', 'fps=10,crop=390:44:425:36,scale=780:-1:flags=lanczos', join(OUT, 'pane-header-rename.gif')], { stdio: 'ignore' })
    } catch {
      console.log('GIF skipped — this ffmpeg has no GIF encoder; webm clip kept')
    }
  } else {
    console.log(`no ffmpeg at ${ffmpeg}; kept the full recording only`)
  }

  await browser.close()
  console.log(`wrote 6 frames + recording to ${OUT}`)
}

main().catch((e) => { console.error(e); process.exit(1) })
