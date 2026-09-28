/**
 * Evidence for #9727 (UX round): the pane header's regenerate control is no
 * longer an unlabeled glyph — the hover-revealed state carries the visible
 * "Auto-title" text next to the Sparkles, in every pane of a split.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with all /api/** answered from fixtures (gateway-free) — the pod path
 * in capture-pane-header-rename.mjs needs the user systemd bus, which is not
 * reachable from every agent sandbox. Split view is reached the way a user
 * reaches it: `mc-split-layouts` holds a two-session layout anchored at the
 * left slot, and landing on that anchor auto-enters split; `session_grid` is
 * turned on by the config fixture.
 *
 * Frames:
 *   1. pane-header-autotitle-hover.png — pointer on the left pane's title bar:
 *      Pen + Sparkles + "Auto-title" revealed; the right pane stays at rest.
 *   2. pane-header-autotitle-hover-zh.png — the same in zh-CN, so the label is
 *      shown to come from the catalog, not a hardcoded string.
 *   3. pane-header-hover.png — the rename story's hover frame, on the current
 *      control (label present), replacing the pre-label pod capture.
 *   4. pane-header-editing.png — the editor open on a LONG title: the whole
 *      title is selected on open, so its start is visible (UX round).
 *   5. pane-header-rename-error.png — the rename PATCH answers 500: inline
 *      notice in the pane, title rolled back to the previous name.
 *   5b. pane-header-renamed.png — a rename the server accepted: the pane
 *      shows the new title at rest, controls hidden (replaces the pre-label
 *      pod capture of the same state, which showed an icon-only sparkle the
 *      current control cannot render).
 *   6. pane-header-generating.png — the generate-title POST is held open:
 *      spinner + "Auto-title…" in place of the button in the pane that asked.
 *   6b. pane-header-undo-offer.png — the generated title landed: the previous
 *      name is offered back inline ("Undo"), visible at rest; the harness then
 *      presses it and asserts the previous title is restored and the offer gone.
 *   7. main-header-hover.png / 8. main-header-editing.png — the SAME control
 *      on the single-session header (no split layout seeded): hover reveals
 *      Pen + Sparkles + "Auto-title"; the editor opens with the long title
 *      selected and scrolled to its start. Closes the UX lane's evidence gap
 *      for the non-compact host.
 *
 * Asserts, before shooting, that the regenerate button in the hovered pane is
 * visible and contains the catalog text, and that the pane at rest does not
 * show it.
 *
 * Usage: node scripts/capture-pane-header-autotitle.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, json } from './lib/stub-dashboard-api.mjs'
import { stubSplitPanes } from './lib/split-pane-fixture.mjs'

const OUT = process.argv[2] || '../temp-screenshots/pane-header-rename'
mkdirSync(OUT, { recursive: true })

const LEFT = 'chat-left'
const RIGHT = 'chat-right'

const slots = [
  {
    key: LEFT, title: 'Release checklist review before the Thursday cut, with the migration note and the smoke run still open', messages: 4, running: false,
    agent: 'kirocrew', created: '2026-06-01T09:00:00Z', last_ts: '2026-08-13T10:00:00Z', folder_id: '',
  },
  {
    key: RIGHT, title: 'Pipeline triage', messages: 2, running: false,
    agent: 'oncall', created: '2026-08-12T09:00:00Z', last_ts: '2026-08-13T09:30:00Z', folder_id: '',
  },
]

const LAYOUT = {
  [LEFT]: {
    type: 'split', id: 'sp-1', dir: 'row', sizes: [0.5, 0.5],
    children: [
      { type: 'leaf', id: 'lf-1', kind: 'session', slot: LEFT },
      { type: 'leaf', id: 'lf-2', kind: 'session', slot: RIGHT },
    ],
  },
}

const leftMsgs = [
  { role: 'user', content: 'Which checklist items are still open?', ts: '2026-08-13T09:56:00Z', meta: { mid: 'm-1' } },
  { role: 'assistant', content: 'The changelog entry, the migration note, and the smoke run.', ts: '2026-08-13T09:57:00Z', meta: { mid: 'm-2' } },
]
const rightMsgs = [
  { role: 'user', content: 'Anything paging overnight?', ts: '2026-08-13T09:28:00Z', meta: { mid: 's-1' } },
  { role: 'assistant', content: 'Nothing paged. One warning cleared itself at 03:10.', ts: '2026-08-13T09:29:00Z', meta: { mid: 's-2' } },
]

const TRANSCRIPTS = { [LEFT]: leftMsgs, [RIGHT]: rightMsgs }

const LABEL = { en: 'Auto-title', 'zh-CN': '自动命名' }

async function shoot(browser, base, locale, file) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, locale })
  const page = await context.newPage()
  await stubSplitPanes(page, { slots, transcripts: TRANSCRIPTS, layout: LAYOUT })
  // The locale seed is this harness's own (the zh-CN frame); it runs after the
  // fixture's localStorage.clear() like the layout seed does.
  await page.addInitScript(lang => { localStorage.setItem('mc-lang', lang) }, locale)
  logPageProblems(page)

  await page.goto(`${base}/chat/${LEFT}`, { waitUntil: 'domcontentloaded' })
  await page.getByText('Pipeline triage').first().waitFor({ timeout: 20_000 })
  await page.waitForTimeout(2000)

  // Two pane toolbars, each hosting the shared control.
  const bars = page.locator('.group\\/header')
  const count = await bars.count()
  if (count < 2) throw new Error(`expected 2 pane headers, found ${count}`)
  const leftBar = bars.nth(0)
  const rightBar = bars.nth(1)

  await leftBar.hover()
  await page.waitForTimeout(400)
  const leftLabel = leftBar.getByText(LABEL[locale], { exact: true })
  await leftLabel.waitFor({ state: 'visible', timeout: 5_000 })
  const leftOpacity = await leftLabel.evaluate(el => {
    const btn = el.closest('button')
    return btn ? Number(getComputedStyle(btn).opacity) : -1
  })
  if (!(leftOpacity > 0.2)) throw new Error(`hovered pane's regenerate button is not revealed (opacity=${leftOpacity})`)
  const rightOpacity = await rightBar.getByText(LABEL[locale], { exact: true }).evaluate(el => {
    const btn = el.closest('button')
    return btn ? Number(getComputedStyle(btn).opacity) : -1
  })
  if (rightOpacity !== 0) throw new Error(`pane at rest still shows the regenerate control (opacity=${rightOpacity})`)

  await page.screenshot({ path: `${OUT}/${file}`, fullPage: false })
  console.log(`wrote ${OUT}/${file} (hovered label "${LABEL[locale]}", opacity ${leftOpacity}; rest pane opacity ${rightOpacity})`)
  await context.close()
}

const LONG_TITLE = slots[0].title
const GENERATED_TITLE = 'Thursday cut: changelog, migration note, smoke run'
const REFUSED_DRAFT = 'Thursday cut checklist'
const COMMITTED_DRAFT = 'Thursday cut — go/no-go'

/**
 * The rename story on the current control: hover, editor open with the whole
 * title selected, a refused rename rolled back with the inline notice, and a
 * held regenerate showing the spinner. Same fixture app; the rename PATCH is
 * answered 500 and the generate-title POST is held so the frame is stable.
 */
async function shootFlow(browser, base) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, locale: 'en' })
  const page = await context.newPage()
  let releaseGenerate = null
  await stubSplitPanes(page, {
    slots, transcripts: TRANSCRIPTS, layout: LAYOUT,
    // Tried first: this harness's own intercepts for the refused rename and
    // the held regenerate; everything else is the shared split-pane wiring.
    extra: async (path, route) => {
      if (path === `/api/chat/slots/${LEFT}/title`) {
        // Only the deliberately refused draft fails; the Undo's rename succeeds.
        const body = route.request().postDataJSON?.() ?? {}
        if (body.title === REFUSED_DRAFT) { await json(route, { error: 'title store unavailable' }, 500); return true }
        await json(route, { ok: true })
        return true
      }
      if (path === `/api/chat/slots/${LEFT}/generate-title`) {
        // Held until the frame is taken; resolved on teardown so the harness exits cleanly.
        await new Promise(resolve => { releaseGenerate = resolve })
        await json(route, { ok: true, title: GENERATED_TITLE })
        return true
      }
      return false
    },
  })
  await page.addInitScript(lang => { localStorage.setItem('mc-lang', lang) }, 'en')
  logPageProblems(page)

  await page.goto(`${base}/chat/${LEFT}`, { waitUntil: 'domcontentloaded' })
  await page.getByText('Pipeline triage').first().waitFor({ timeout: 20_000 })
  await page.waitForTimeout(2000)
  const bars = page.locator('.group\\/header')
  if ((await bars.count()) < 2) throw new Error(`expected 2 pane headers, found ${await bars.count()}`)
  const leftBar = bars.nth(0)

  // 3. hover
  await leftBar.hover()
  await page.waitForTimeout(400)
  await leftBar.getByText(LABEL.en, { exact: true }).waitFor({ state: 'visible', timeout: 5_000 })
  await page.screenshot({ path: `${OUT}/pane-header-hover.png`, fullPage: false })
  console.log(`wrote ${OUT}/pane-header-hover.png`)

  // 4. editing: click the title -> editor seeded with the long title, all selected
  await leftBar.getByText(LONG_TITLE, { exact: true }).click()
  const editor = leftBar.locator('input')
  await editor.waitFor({ state: 'visible', timeout: 5_000 })
  await page.waitForTimeout(300)
  const sel = await editor.evaluate(el => ({ start: el.selectionStart, end: el.selectionEnd, len: el.value.length, scroll: el.scrollLeft }))
  if (sel.start !== 0 || sel.end !== sel.len) throw new Error(`editor did not select the whole title on open: ${JSON.stringify(sel)}`)
  if (sel.scroll !== 0) throw new Error(`editor is scrolled away from the start of the title (scrollLeft=${sel.scroll}); the selection must be anchored backward`)
  await page.screenshot({ path: `${OUT}/pane-header-editing.png`, fullPage: false })
  console.log(`wrote ${OUT}/pane-header-editing.png (selection ${sel.start}..${sel.end} of ${sel.len}, scrollLeft ${sel.scroll})`)

  // 5. refused rename: type a new name, Enter -> PATCH 500 -> notice + rollback
  await editor.fill(REFUSED_DRAFT)
  await editor.press('Enter')
  const notice = page.getByTestId('chat-pane-title-error').first()
  await notice.waitFor({ state: 'visible', timeout: 10_000 })
  await leftBar.getByText(LONG_TITLE, { exact: true }).waitFor({ state: 'visible', timeout: 10_000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/pane-header-rename-error.png`, fullPage: false })
  console.log(`wrote ${OUT}/pane-header-rename-error.png (title rolled back to the previous name)`)

  // 5b. accepted rename: the pane shows the new title at rest; then renamed
  //     back so the generate / Undo frames below keep the long title as the
  //     name Undo offers.
  const renameTo = async (draft) => {
    await leftBar.hover()
    await leftBar.locator('.cursor-text .truncate').first().click()
    const ed = leftBar.locator('input')
    await ed.waitFor({ state: 'visible', timeout: 5_000 })
    await ed.fill(draft)
    await ed.press('Enter')
    await leftBar.getByText(draft, { exact: true }).waitFor({ state: 'visible', timeout: 10_000 })
  }
  await renameTo(COMMITTED_DRAFT)
  await page.mouse.move(700, 850)
  await page.waitForTimeout(400)
  if (await notice.count() !== 0) throw new Error('the failure notice survived the attempt that superseded it')
  await page.screenshot({ path: `${OUT}/pane-header-renamed.png`, fullPage: false })
  console.log(`wrote ${OUT}/pane-header-renamed.png (accepted rename, pane at rest)`)
  await renameTo(LONG_TITLE)

  // 6. generating: Sparkles -> held POST -> spinner
  await leftBar.hover()
  await page.waitForTimeout(300)
  await leftBar.getByText(LABEL.en, { exact: true }).click()
  await leftBar.locator('.animate-spin').first().waitFor({ state: 'visible', timeout: 5_000 })
  await leftBar.getByRole('status').getByText(`${LABEL.en}…`, { exact: true }).waitFor({ state: 'visible', timeout: 5_000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/pane-header-generating.png`, fullPage: false })
  console.log(`wrote ${OUT}/pane-header-generating.png (spinner while generate-title is held)`)

  // 7. generated title landed: the previous name is offered back inline
  //    (visible without hovering), and pressing it restores that name.
  if (releaseGenerate) releaseGenerate()
  await leftBar.getByText(GENERATED_TITLE, { exact: true }).waitFor({ state: 'visible', timeout: 5_000 })
  await page.mouse.move(700, 850)
  await page.waitForTimeout(400)
  const undoBtn = leftBar.getByRole('button', { name: `Undo: ${LONG_TITLE}` })
  await undoBtn.waitFor({ state: 'visible', timeout: 5_000 })
  const undoOpacity = await undoBtn.evaluate(el => Number(getComputedStyle(el).opacity))
  if (undoOpacity !== 1) throw new Error(`undo affordance is not fully visible at rest (opacity=${undoOpacity})`)
  await page.screenshot({ path: `${OUT}/pane-header-undo-offer.png`, fullPage: false })
  console.log(`wrote ${OUT}/pane-header-undo-offer.png (generated title on screen, Undo offered at rest)`)
  await undoBtn.click()
  await leftBar.getByText(LONG_TITLE, { exact: true }).waitFor({ state: 'visible', timeout: 5_000 })
  if (await leftBar.getByRole('button', { name: /^Undo/ }).count() !== 0) throw new Error('undo affordance survived being used')
  await context.close()
}

/**
 * The single-session header (ChatPage, non-compact host): same shared control,
 * no split layout seeded. Two frames: hover (label revealed) and the editor
 * open on the long title (selected, scrolled to the start).
 */
async function shootMainHeader(browser, base) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, locale: 'en' })
  const page = await context.newPage()
  // No layout seed: the anchor slot opens as a plain single chat.
  await stubSplitPanes(page, { slots, transcripts: TRANSCRIPTS, layout: {} })
  await page.addInitScript(lang => { localStorage.setItem('mc-lang', lang) }, 'en')
  logPageProblems(page)

  await page.goto(`${base}/chat/${LEFT}`, { waitUntil: 'domcontentloaded' })
  await page.getByText('The changelog entry').first().waitFor({ timeout: 20_000 })
  await page.waitForTimeout(2000)
  const bars = page.locator('.group\\/header')
  if ((await bars.count()) !== 1) throw new Error(`expected the single-session header only, found ${await bars.count()} hover groups`)
  const header = bars.first()

  await header.hover()
  await page.waitForTimeout(400)
  const label = header.getByText(LABEL.en, { exact: true })
  await label.waitFor({ state: 'visible', timeout: 5_000 })
  await page.screenshot({ path: `${OUT}/main-header-hover.png`, fullPage: false })
  console.log(`wrote ${OUT}/main-header-hover.png (single-session header, label revealed)`)

  await header.getByText(LONG_TITLE, { exact: true }).click()
  const editor = header.locator('input')
  await editor.waitFor({ state: 'visible', timeout: 5_000 })
  await page.waitForTimeout(300)
  const sel = await editor.evaluate(el => ({ start: el.selectionStart, end: el.selectionEnd, len: el.value.length, scroll: el.scrollLeft }))
  if (sel.start !== 0 || sel.end !== sel.len || sel.scroll !== 0) throw new Error(`main-header editor did not open selected-at-start: ${JSON.stringify(sel)}`)
  await page.screenshot({ path: `${OUT}/main-header-editing.png`, fullPage: false })
  console.log(`wrote ${OUT}/main-header-editing.png (selection ${sel.start}..${sel.end} of ${sel.len}, scrollLeft ${sel.scroll})`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    await shoot(browser, base, 'en', 'pane-header-autotitle-hover.png')
    await shoot(browser, base, 'zh-CN', 'pane-header-autotitle-hover-zh.png')
    await shootFlow(browser, base)
    await shootMainHeader(browser, base)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
