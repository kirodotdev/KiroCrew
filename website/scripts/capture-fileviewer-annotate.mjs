/**
 * Screenshot harness for the file viewer's type-first annotator, shot against
 * a REAL isolated pod (`kirocrew pod up <worktree> --json`) rather than a
 * stubbed dist: the pod serves the built SPA and answers `/api/**` itself, so
 * every frame shows the shipped selection → comment flow end to end.
 *
 * MODE=before probes the old two-stage flow (Comment button, then a popover;
 * run against a pod whose dist was built from the base commit). The default
 * MODE=after probes the new flow (selecting text opens the focused input with
 * the icon actions beside it). A frame is written only once its probes
 * demonstrably rendered — a blank or wrong surface throws instead.
 *
 * Frames (prefix from MODE):
 *   <p>-select-<theme>     text selected in the side-panel file viewer
 *   <p>-typing-dark        (after) a comment typed into the focused input
 *   <p>-popover-dark       (before) the popover the Comment button used to open
 *   <p>-keyboard-unfocused-dark (after) Shift+Arrow selection: box open, unfocused
 *   <p>-copy-failed-dark   (after) a refused clipboard write reported inline
 *   <p>-narrow-dark        (after) 390px viewport: full-width input, icons below
 *
 * Usage: node scripts/capture-fileviewer-annotate.mjs <base_url> <token> [outDir]
 *        MODE=before node scripts/capture-fileviewer-annotate.mjs <base_url> <token> [outDir]
 *
 * The token is the one `pod up --json` printed; it is used ONLY to establish
 * the pod's own auth cookie for the browser context.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, writeFileSync, renameSync, rmSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const [BASE, TOKEN, OUT_ARG] = process.argv.slice(2)
if (!BASE || !TOKEN) {
  console.error('usage: node scripts/capture-fileviewer-annotate.mjs <base_url> <token> [outDir]')
  process.exit(2)
}
const OUT = OUT_ARG || '../temp-screenshots/fileviewer-annotate'
const BEFORE = process.env.MODE === 'before'
const PREFIX = BEFORE ? '10-before' : '20-after'
const MAX_EDGE = 2000
const MIN_MBPP = 15

mkdirSync(OUT, { recursive: true })

// ── Fixture file (outside the repo: a real file the pod's /api/file-read serves) ──
const FIXTURE_DIR = process.env.FIXTURE_DIR || join(tmpdir(), 'fileviewer-annotate-fixture')
mkdirSync(FIXTURE_DIR, { recursive: true })
const MD_PATH = join(FIXTURE_DIR, 'proposal.md')
const SELECT_SENTENCE = 'Distribution goes through the existing CDN.'
const SECOND_SENTENCE = 'Every claim below was validated end to end.'
writeFileSync(MD_PATH, `# Ship KAS inside the product

Decision proposal, draft v1. Every claim below was validated end to end.

## Executive summary

The bundle adds one platform per release and keeps the auth interface
unchanged. ${SELECT_SENTENCE}

## What needs alignment

1. Who owns the multi-platform bundle build
2. Version pinning and the upgrade policy
3. How the rollout is staged across regions
`)

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

async function assertRendered(name, probes) {
  const found = []
  for (const { selector, locator, attr } of probes) {
    const count = await locator.count()
    if (count < 1) throw new Error(`frame ${name}: probe \`${selector}\` matched nothing — surface did not render; do not save the frame`)
    const v = attr ? await locator.first().getAttribute(attr) : await locator.first().innerText().catch(() => '')
    found.push({ selector, count, text: (v || '').trim().replace(/\s+/g, ' ').slice(0, 70) })
  }
  return found
}

function record(file, evidence) {
  const { w, h } = pngSize(file)
  const bytes = readFileSync(file).length
  const mbpp = Math.round((bytes * 1000) / (w * h))
  console.log(`wrote ${file}  ${w}x${h}  ${bytes}B  ${mbpp} milli-bytes/px`)
  for (const e of evidence) console.log(`      asserted ${e.selector}  ×${e.count}  →  ${e.text}`)
  if (w > MAX_EDGE || h > MAX_EDGE) throw new Error(`frame ${file}: ${w}x${h} exceeds the ${MAX_EDGE}px edge budget`)
  if (mbpp < MIN_MBPP) throw new Error(`frame ${file}: below the blank-frame floor — re-shoot, do not ship`)
}

const probe = (selector, locator, attr) => ({ selector, locator, attr })

/** Client rects of `needle` inside `root`, scrolling it into view first. */
function measureSentence(root, needle) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let node
  while ((node = walker.nextNode())) {
    const idx = node.data.indexOf(needle)
    if (idx < 0) continue
    node.parentElement.scrollIntoView({ block: 'center' })
    const r = document.createRange()
    r.setStart(node, idx); r.setEnd(node, idx + needle.length)
    return Array.from(r.getClientRects()).map(c => ({ x: c.left, y: c.top, w: c.width, h: c.height }))
  }
  return null
}

/** Drag-select `sentence` inside `scope` with the real mouse, so the toolbar
 *  opens through the same mouseup path a user drives. */
async function dragSelect(page, scope, sentence) {
  // Measure twice: the first pass scrolls the paragraph into view, which moves it.
  await scope.evaluate(measureSentence, sentence)
  await page.waitForTimeout(150)
  const rects = await scope.evaluate(measureSentence, sentence)
  if (!rects || rects.length === 0) throw new Error(`sentence not found in the rendered preview: ${sentence}`)
  const first = rects[0], last = rects[rects.length - 1]
  await page.mouse.move(first.x + 1, first.y + first.h / 2)
  await page.mouse.down()
  await page.mouse.move(last.x + last.w - 1, last.y + last.h / 2, { steps: 12 })
  await page.mouse.up()
}

async function main() {
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  console.log('mode:', BEFORE ? 'before (Comment button → popover)' : 'after (type-first composer)')
  const browser = await chromium.launch({ executablePath })

  // One authenticated context: the first navigation with ?token= sets the
  // pod's auth cookie; everything after is same-origin and cookie-backed.
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  page.on('pageerror', e => console.log('PAGE ERROR', e.message))
  await page.goto(`${BASE}/?token=${encodeURIComponent(TOKEN)}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  // Create the session and its project directory through the pod's own API
  // (same-origin fetch, so the CSRF Origin check and the cookie both hold; the
  // X-Session-Key header is what the SPA's own client sends on every write).
  const slot = await page.evaluate(async (fixtureDir) => {
    const headers = { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' }
    const created = await fetch('/api/chat/slots', {
      method: 'POST', headers,
      body: JSON.stringify({ name: 'Proposal review' }),
    })
    if (!created.ok) throw new Error(`POST /api/chat/slots → ${created.status} ${await created.text()}`)
    const body = await created.json()
    const key = body.slot ?? body.key ?? body.name
    const proj = await fetch(`/api/chat/slots/${encodeURIComponent(key)}/project`, {
      method: 'POST', headers,
      body: JSON.stringify({ project: fixtureDir }),
    })
    if (!proj.ok) throw new Error(`POST project → ${proj.status} ${await proj.text()}`)
    return key
  }, FIXTURE_DIR)
  console.log('slot:', slot)


  async function openViewer(theme, width = 1440, { keepDraft = false } = {}) {
    await page.setViewportSize({ width, height: 900 })
    await page.evaluate(([slot, theme, keepDraft]) => {
      localStorage.setItem('mc-theme', theme)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot-chat', slot)
      localStorage.setItem('mc-activity-open:' + slot, 'true')
      localStorage.setItem('mc-side-panel-width', '760')
      localStorage.setItem('kirocrew:comment-hint-dismissed', '1')
      // Pending comments persist per file; each frame starts from none, because
      // clicking an already-commented range jumps to its row instead of selecting.
      localStorage.removeItem('mc-comment-drafts')
      // Likewise the persisted in-progress draft, except where a scene sets it up.
      if (!keepDraft) sessionStorage.clear()
    }, [slot, theme, keepDraft])
    await page.goto(`${BASE}/?sid=${encodeURIComponent(slot)}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    // First-run modals would sit over the viewer.
    await page.keyboard.press('Escape').catch(() => {})
    // Open the file the way a user does: the side panel's Files tree.
    const panel = page.locator('div:has(> .side-panel-strip)').last()
    await panel.waitFor({ timeout: 30000 })
    const filesTab = panel.getByRole('tab', { name: 'Files' })
    if (await filesTab.count()) await filesTab.first().click()
    // The @pierre/trees row carries the file name as its accessible name (the
    // visible text is split for middle-truncation), so locate it by role.
    const entry = page.getByRole('treeitem', { name: 'proposal.md', exact: true }).first()
    try {
      await entry.waitFor({ timeout: 15000 })
    } catch (err) {
      const dbg = join(OUT, `debug-tree-${theme}-${width}.png`)
      await page.screenshot({ path: dbg })
      console.error('file tree did not list proposal.md; debug frame at', dbg, await panel.innerText().catch(() => ''))
      throw err
    }
    await entry.click()
    const heading = page.getByRole('heading', { name: 'Executive summary' })
    try {
      await heading.waitFor({ timeout: 30000 })
    } catch (err) {
      const dbg = join(OUT, `debug-${theme}-${width}.png`)
      await page.screenshot({ path: dbg, fullPage: false })
      const state = await page.evaluate(() => ({
        url: location.href,
        strips: document.querySelectorAll('.side-panel-strip').length,
        tabs: localStorage.getItem('mc-panel-tabs:' + localStorage.getItem('mc-active-slot-chat')),
        text: document.body.innerText.slice(0, 600),
      }))
      console.error('viewer did not render; debug frame at', dbg, JSON.stringify(state, null, 1))
      throw err
    }
    await page.waitForTimeout(600)
    return heading
  }

  /** The element to frame: the side panel, or the whole page when it fills the viewport. */
  const frameTarget = async () => {
    const panel = page.locator('div:has(> .side-panel-strip)').last()
    return (await panel.count()) ? panel : page.locator('body')
  }

  async function shot(name, probes, target) {
    const evidence = await assertRendered(name, probes)
    const file = `${OUT}/${name}.png`
    await (target ?? await frameTarget()).screenshot({ path: file })
    record(file, evidence)
  }

  // The proposal's rendered body — not whichever .msg-content happens to be first
  // once an artifact tab is also open.
  const preview = () => page.locator('.msg-content').filter({ hasText: 'Executive summary' }).first()
  const composerInput = () => page.getByLabel('Comment on the selected text')
  const copyBtn = () => page.getByRole('button', { name: 'Copy', exact: true })
  const submitBtn = () => page.getByRole('button', { name: 'Add comment', exact: true })

  for (const theme of ['dark', 'light']) {
    await openViewer(theme)
    await dragSelect(page, preview(), SELECT_SENTENCE)
    await page.waitForTimeout(700)
    if (BEFORE) {
      await shot(`${PREFIX}-select-${theme}`, [
        probe('old toolbar: "Comment" button', page.getByRole('button', { name: 'Comment', exact: true })),
        probe('old toolbar: "Copy" button', copyBtn()),
      ])
      if (theme === 'dark') {
        await page.getByRole('button', { name: 'Comment', exact: true }).click()
        await page.waitForTimeout(600)
        await shot(`${PREFIX}-popover-${theme}`, [
          probe('old popover input', page.getByPlaceholder('Write a comment…'), 'placeholder'),
          probe('old popover heading "Add comment"', page.getByText('Add comment', { exact: true })),
        ])
      }
    } else {
      await composerInput().waitFor({ timeout: 5000 })
      const focused = await composerInput().evaluate(el => document.activeElement === el)
      if (!focused) throw new Error('composer input is not focused after selecting text')
      await shot(`${PREFIX}-select-${theme}`, [
        probe('composer input (focused)', composerInput(), 'placeholder'),
        probe('icon action: Copy', copyBtn(), 'aria-label'),
        probe('submit: Add comment', submitBtn(), 'aria-label'),
        probe('layout=row', page.locator('[data-testid="selection-composer"][data-layout="row"]'), 'data-layout'),
      ])
      if (theme === 'dark') {
        await page.keyboard.type('Name the CDN and who owns the cache policy.')
        await page.waitForTimeout(300)
        await shot(`${PREFIX}-typing-${theme}`, [
          probe('typed comment in the input', composerInput().and(page.locator('textarea:not(:placeholder-shown)')), 'aria-label'),
          probe('submit enabled', submitBtn().and(page.locator('button:not([disabled])')), 'aria-label'),
        ])
        // Enter submits: the comment lands in the pending list at the panel's foot.
        await page.keyboard.press('Enter')
        await page.getByText('1 comment pending').waitFor({ timeout: 5000 })
        await page.waitForTimeout(400)
        await shot(`${PREFIX}-submitted-${theme}`, [
          probe('pending list header', page.getByText('1 comment pending')),
          probe('the recorded comment', page.getByText('Name the CDN and who owns the cache policy.')),
          probe('Submit All', page.getByRole('button', { name: /submit all/i })),
        ])
        // A view switch over a typed draft asks first, in the draft's own words.
        // A DIFFERENT sentence: clicking an already-commented range jumps to its
        // pending row (and clears the selection) by design.
        await dragSelect(page, preview(), SECOND_SENTENCE)
        await page.waitForTimeout(700)
        await composerInput().waitFor({ timeout: 5000 })
        await page.keyboard.type('Half a thought')
        // The header mode toggle (aria-pressed), not the pending row's pencil.
        await page.getByRole('button', { name: 'Edit', exact: true }).and(page.locator('[aria-pressed]')).click()
        const dialog = page.getByRole('dialog')
        await dialog.getByText('Discard your unsaved comment?').waitFor({ timeout: 5000 })
        await page.waitForTimeout(300)
        // The dialog centres on the VIEWPORT, so frame it directly rather than
        // through the panel clip.
        await shot(`${PREFIX}-discard-confirm-${theme}`, [
          probe('discard prompt names the comment', dialog.getByText('Discard your unsaved comment?')),
          probe('Discard comment button', dialog.getByRole('button', { name: 'Discard comment' })),
        ], dialog)
        await dialog.getByRole('button', { name: 'Cancel' }).click()
      }
    }
  }

  if (!BEFORE) {
    // Keyboard selection: the box opens UNFOCUSED (the selection is still being
    // extended), with the placeholder naming the way in.
    await openViewer('dark')
    await page.evaluate(([root, needle]) => {
      const walker = document.createTreeWalker(document.querySelector(root), NodeFilter.SHOW_TEXT)
      let node
      while ((node = walker.nextNode())) {
        const idx = node.data.indexOf(needle)
        if (idx < 0) continue
        const r = document.createRange()
        r.setStart(node, idx); r.setEnd(node, idx + needle.length - 4)
        const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r)
        return
      }
    }, ['.msg-content', SELECT_SENTENCE])
    // Extend by keyboard — the toolbar opens on the Shift+Arrow keyup.
    await page.keyboard.press('Shift+ArrowRight')
    await page.waitForTimeout(700)
    await composerInput().waitFor({ timeout: 5000 })
    const kbFocused = await composerInput().evaluate(el => document.activeElement === el)
    if (kbFocused) throw new Error('keyboard-opened composer must NOT steal focus')
    await shot(`${PREFIX}-keyboard-unfocused-dark`, [
      probe('unfocused composer (Enter placeholder)', page.getByPlaceholder('Press Enter or click to comment…'), 'placeholder'),
      probe('layout=row', page.locator('[data-testid="selection-composer"][data-layout="row"]'), 'data-layout'),
    ])

    // Refused clipboard: the box reports it inline instead of a false checkmark.
    await page.addInitScript(() => {
      Object.defineProperty(navigator, 'clipboard', { value: { writeText: () => Promise.reject(new Error('denied')) }, configurable: true })
      document.execCommand = () => false
    })
    await openViewer('dark')
    await dragSelect(page, preview(), SELECT_SENTENCE)
    await page.waitForTimeout(700)
    await composerInput().waitFor({ timeout: 5000 })
    await page.keyboard.press('Control+c')
    await page.getByText('Copy failed').waitFor({ timeout: 5000 })
    await shot(`${PREFIX}-copy-failed-dark`, [
      probe('inline copy-failed notice', page.getByText('Copy failed')),
      probe('composer input', composerInput(), 'placeholder'),
    ])

    // Narrow viewport: the panel takes the screen, the input goes full width
    // and the icon row drops beneath it.
    await openViewer('dark', 390)
    await dragSelect(page, preview(), SELECT_SENTENCE)
    await page.waitForTimeout(700)
    await composerInput().waitFor({ timeout: 5000 })
    await shot(`${PREFIX}-narrow-dark`, [
      probe('layout=stack', page.locator('[data-testid="selection-composer"][data-layout="stack"]'), 'data-layout'),
      probe('composer input', composerInput(), 'placeholder'),
      probe('icon action: Copy', copyBtn(), 'aria-label'),
    ])
    // The stacked box must sit inside the viewport.
    const box = await page.locator('[data-testid="selection-composer"]').boundingBox()
    if (!box || box.x < 0 || box.x + box.width > 390) throw new Error(`stacked composer overflows the 390px viewport: ${JSON.stringify(box)}`)

    // ── Plain action row (artifact panel host): a refused copy is reported there too ──
    // The artifact panel keeps the two-button [Comment | Copy] row (no composer);
    // its Copy action now returns the clipboard result, so the row shows the same
    // notice. Seed an artifact through the pod's API and open it from the
    // Artifacts tab. (The clipboard stub from the copy-failed frame is still
    // installed on this page.)
    await page.setViewportSize({ width: 1440, height: 900 })
    const artifactName = 'rollout-notes.md'
    await page.evaluate(async ([name, slot]) => {
      const r = await fetch('/api/artifacts', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-Key': `dashboard:${slot}` },
        body: JSON.stringify({ name, kind: 'markdown', origin_session_key: slot, content: '# Rollout notes\n\nStage the rollout by region and hold the second wave until error budgets recover.\n' }),
      })
      if (!r.ok) throw new Error(`POST /api/artifacts → ${r.status} ${await r.text()}`)
    }, [artifactName, slot])
    await openViewer('dark')
    const panelEl = page.locator('div:has(> .side-panel-strip)').last()
    const artifactsTab = panelEl.getByRole('tab', { name: 'Artifacts' })
    if (!(await artifactsTab.count())) throw new Error('Artifacts tab not found in the side-panel strip')
    await artifactsTab.first().click()
    const row = page.getByText(artifactName, { exact: true }).first()
    await row.waitFor({ timeout: 15000 })
    await row.click()
    const artifactHeading = page.getByRole('heading', { name: 'Rollout notes' })
    await artifactHeading.waitFor({ timeout: 15000 })
    await page.waitForTimeout(600)
    const artifactBody = artifactHeading.locator('xpath=..')
    await dragSelect(page, artifactBody, 'hold the second wave until error budgets recover.')
    await page.waitForTimeout(700)
    const plainCopy = page.getByRole('button', { name: 'Copy', exact: true })
    await plainCopy.waitFor({ timeout: 5000 })
    await plainCopy.click()
    await page.getByText('Copy failed').waitFor({ timeout: 5000 })
    await shot(`${PREFIX}-plain-row-copy-failed-dark`, [
      probe('plain row still has Comment', page.getByRole('button', { name: 'Comment', exact: true })),
      probe('inline copy-failed notice on the plain row', page.getByText('Copy failed')),
      probe('no composer on this host', page.locator('body:not(:has([data-testid="selection-composer"]))'), 'class'),
    ])
    // ── Draft restored after a teardown ──────────────────────────────────────
    // Type, tear the page down (a reload stands in for a chat-slot switch: both
    // unmount the toolbar with no chance to ask), reopen the file and select the
    // SAME passage: the draft is back. A different passage would not get it.
    await openViewer('dark')
    await dragSelect(page, preview(), SECOND_SENTENCE)
    await page.waitForTimeout(700)
    await composerInput().waitFor({ timeout: 5000 })
    await page.keyboard.type('Typed before the switch')
    await page.waitForTimeout(300)
    await openViewer('dark', 1440, { keepDraft: true }) // full navigation: the toolbar is torn down unguarded
    await dragSelect(page, preview(), SECOND_SENTENCE)
    await page.waitForTimeout(700)
    await composerInput().waitFor({ timeout: 5000 })
    const restored = await composerInput().inputValue()
    if (restored !== 'Typed before the switch') throw new Error(`draft not restored over its passage: ${JSON.stringify(restored)}`)
    await shot(`${PREFIX}-draft-restored-dark`, [
      probe('restored draft in the input', composerInput().and(page.locator('textarea:not(:placeholder-shown)')), 'aria-label'),
      probe('submit enabled', submitBtn().and(page.locator('button:not([disabled])')), 'aria-label'),
    ])
    await page.getByRole('button', { name: 'Close', exact: true }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Discard comment' }).click()

    // ── Touch: the box opens unfocused and names a tap, not a key ─────────────
    const tctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true })
    const tpage = await tctx.newPage()
    await tpage.goto(`${BASE}/?token=${encodeURIComponent(TOKEN)}`, { waitUntil: 'domcontentloaded' })
    await tpage.waitForTimeout(2000)
    const tauthed = await tpage.evaluate(async () => (await fetch('/api/chat/slots')).ok)
    if (!tauthed) throw new Error('touch context not authenticated')
    await tpage.evaluate(([slot]) => {
      localStorage.setItem('mc-theme', 'dark'); localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot-chat', slot); localStorage.setItem('mc-activity-open:' + slot, 'true')
      localStorage.setItem('kirocrew:comment-hint-dismissed', '1'); localStorage.removeItem('mc-comment-drafts')
      sessionStorage.clear()
    }, [slot])
    await tpage.goto(`${BASE}/?sid=${encodeURIComponent(slot)}`, { waitUntil: 'domcontentloaded' })
    await tpage.waitForTimeout(2500)
    await tpage.keyboard.press('Escape').catch(() => {})
    const tpanel = tpage.locator('div:has(> .side-panel-strip)').last()
    await tpanel.waitFor({ timeout: 30000 })
    const tfiles = tpanel.getByRole('tab', { name: 'Files' })
    if (await tfiles.count()) await tfiles.first().click()
    const tentry = tpage.getByRole('treeitem', { name: 'proposal.md', exact: true }).first()
    await tentry.waitFor({ timeout: 15000 })
    await tentry.click()
    await tpage.getByRole('heading', { name: 'Executive summary' }).waitFor({ timeout: 30000 })
    await tpage.waitForTimeout(600)
    // Touch selections arrive through `selectionchange` (no mouseup), so make
    // the selection the way a long-press does: set the range, then let the
    // debounce fire.
    const tpreview = tpage.locator('.msg-content').filter({ hasText: 'Executive summary' }).first()
    await tpreview.evaluate(measureSentence, SELECT_SENTENCE)
    const tselected = await tpreview.evaluate((root, needle) => {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
      let node
      while ((node = walker.nextNode())) {
        const idx = node.data.indexOf(needle)
        if (idx < 0) continue
        const r = document.createRange(); r.setStart(node, idx); r.setEnd(node, idx + needle.length)
        const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r)
        return sel.toString()
      }
      return ''
    }, SELECT_SENTENCE)
    if (!tselected) throw new Error('touch scene: sentence not found in the proposal preview')
    await tpage.waitForTimeout(1200)
    const tinput = tpage.getByLabel('Comment on the selected text')
    try {
      await tinput.waitFor({ timeout: 5000 })
    } catch (err) {
      const dbg = join(OUT, 'debug-touch.png')
      await tpage.screenshot({ path: dbg })
      console.error('touch composer did not open; debug frame at', dbg, JSON.stringify(await tpage.evaluate(() => ({
        sel: window.getSelection()?.toString(), coarse: matchMedia('(pointer: coarse)').matches, hover: matchMedia('(hover: none)').matches,
        composer: !!document.querySelector('[data-testid="selection-composer"]'), heading: !!document.querySelector('.msg-content'),
      }))))
      throw err
    }
    if (await tinput.evaluate(el => document.activeElement === el)) throw new Error('touch-opened composer must NOT steal focus')
    const tfile = `${OUT}/${PREFIX}-touch-unfocused-dark.png`
    const tprobes = await assertRendered('touch', [
      probe('touch placeholder', tpage.getByPlaceholder('Tap to write a comment…'), 'placeholder'),
      probe('layout=stack', tpage.locator('[data-testid="selection-composer"][data-layout="stack"]'), 'data-layout'),
    ])
    await tpage.screenshot({ path: tfile })
    record(tfile, tprobes)
    await tctx.close()
  }

  // ── Recording: the box follows its selection when the file scrolls ────────
  // A still cannot show relocation; a short .webm can. Its own context so the
  // recording holds only this scene. Skipped (with a note) if the pod refuses a
  // second exchange of the same link token.
  if (!BEFORE && process.env.NO_VIDEO !== '1') {
    const vctx = await browser.newContext({ viewport: { width: 900, height: 600 }, recordVideo: { dir: OUT, size: { width: 900, height: 600 } } })
    const vpage = await vctx.newPage()
    let recorded = false
    try {
      await vpage.goto(`${BASE}/?token=${encodeURIComponent(TOKEN)}`, { waitUntil: 'domcontentloaded' })
      await vpage.waitForTimeout(2000)
      const authed = await vpage.evaluate(async () => (await fetch('/api/chat/slots')).ok)
      if (!authed) throw new Error('second context not authenticated (link token already exchanged)')
      await vpage.evaluate(([slot]) => {
        localStorage.setItem('mc-theme', 'dark'); localStorage.setItem('mc-onboarded', '1')
        localStorage.setItem('mc-active-slot-chat', slot); localStorage.setItem('mc-activity-open:' + slot, 'true')
        localStorage.setItem('mc-side-panel-width', '620'); localStorage.setItem('kirocrew:comment-hint-dismissed', '1')
        localStorage.removeItem('mc-comment-drafts')
      }, [slot])
      await vpage.goto(`${BASE}/?sid=${encodeURIComponent(slot)}`, { waitUntil: 'domcontentloaded' })
      await vpage.waitForTimeout(2500)
      await vpage.keyboard.press('Escape').catch(() => {})
      const vpanel = vpage.locator('div:has(> .side-panel-strip)').last()
      await vpanel.waitFor({ timeout: 30000 })
      const vfiles = vpanel.getByRole('tab', { name: 'Files' })
      if (await vfiles.count()) await vfiles.first().click()
      const ventry = vpage.getByRole('treeitem', { name: 'proposal.md', exact: true }).first()
      await ventry.waitFor({ timeout: 15000 })
      await ventry.click()
      await vpage.getByRole('heading', { name: 'Executive summary' }).waitFor({ timeout: 30000 })
      await vpage.waitForTimeout(600)
      await dragSelect(vpage, vpage.locator('.msg-content').first(), SELECT_SENTENCE)
      await vpage.waitForTimeout(700)
      await vpage.getByLabel('Comment on the selected text').waitFor({ timeout: 5000 })
      await vpage.keyboard.type('Follows the text while I scroll.')
      await vpage.waitForTimeout(600)
      // Scroll the panel's own scroll box in steps; the box re-anchors each time.
      for (const dy of [60, 60, 60, -60, -60, -60]) {
        await vpage.mouse.move(700, 400)
        await vpage.mouse.wheel(0, dy)
        await vpage.waitForTimeout(450)
      }
      await vpage.waitForTimeout(800)
      recorded = true
    } catch (err) {
      console.warn('scroll recording skipped:', err.message)
    }
    const video = vpage.video()
    await vctx.close()
    if (video) {
      const path = await video.path()
      const dest = `${OUT}/${PREFIX}-scroll-follows-dark.webm`
      if (recorded) { renameSync(path, dest); console.log('wrote', dest, statSync(dest).size + 'B') }
      else { rmSync(path, { force: true }) }
    }
  }

  await browser.close()
  console.log(`done — frames in ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
