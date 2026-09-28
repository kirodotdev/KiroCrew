/**
 * Screenshot harness for the terminal Copy soft key (issue #5571, Copy half).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with every /api/** call answered from fixtures via Playwright route
 * interception — no gateway, no PTY. The terminal WebSocket is a tiny scripted
 * PTY that prints a prompt, ECHOES input back, AND reprints a fresh `$ ` prompt
 * after each submitted command, so text written into the terminal renders as an
 * OUTPUT line above a live prompt row (the realistic shell shape) and can be
 * selected with the Select soft key. This is what exercises stage 1's
 * skip-the-prompt behaviour: the tap selects the output line ABOVE the
 * reprinted prompt, not the prompt row the cursor rests on.
 *
 * Touch is emulated the same way the Paste harness does it: `hasTouch` context
 * + CDP `(hover: none)` / `(pointer: coarse)` media — the predicate
 * `useIsTouchDevice` renders the key bar on. The selection is created the
 * production touch way: tapping the new Select soft key, which builds a
 * selection out of the terminal buffer through a STAGED CYCLE (tap 1 = last
 * line, tap 2 = whole buffer, tap 3 = wrap around), announcing each stage — NO
 * `page.mouse` drag, since a mouse gesture is exactly the path that never
 * exists on touch.
 *
 * The clipboard is NOT mocked. The success scenario grants real
 * `clipboard-write`, taps Select to build a selection, taps Copy, verifies the
 * copied text via `navigator.clipboard.readText()`, AND captures the transient
 * "Copied" success state on the key; the no-selection scenario taps Copy with
 * nothing selected and asserts the "Tap Select, then Copy" guidance, proving
 * the key never copies the whole scrollback.
 *
 * Captured:
 *   01-copy-key-touch-dark.png       key bar on a touch viewport with the new
 *                                    Select + Copy keys in the clipboard region
 *   02-select-all-stage-dark.png     after tapping Select TWICE: the stage-2
 *                                    whole-buffer selection, with the "All
 *                                    selected — restart" stage
 *                                    announcement visible on the Select key —
 *                                    demonstrating the discoverable staged cycle
 *   03-copy-success-dark.png         after tapping Copy: the "Copied" success
 *                                    state, with the text read back from the
 *                                    real clipboard
 *   04-copy-no-selection-dark.png    the "Tap Select, then Copy" guidance state
 *                                    (never copies the buffer)
 *   05-copy-key-touch-light.png      light-theme parity of the key bar
 *   06-copy-key-phone-390-dark.png   iPhone-width (390px) portrait: the key
 *                                    caps scroll in their own region and the
 *                                    Copy key stays pinned on-screen
 *   07-copy-no-selection-phone-390.png  the guidance state at 390px: the label
 *                                    truncates inside the pinned region instead
 *                                    of widening past the viewport
 *   08-select-stage-phone-390.png    iPhone-width (390px): a Select stage
 *                                    announcement ("Last line · tap for all")
 *                                    shown FULLY VISIBLE on the key — proving
 *                                    the shortened, front-loaded strings survive
 *                                    truncation at phone width
 *
 * Usage: node scripts/capture-terminal-copy-key.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/terminal-copy-key'
mkdirSync(OUT, { recursive: true })

async function stubContext(context, theme) {
  // The shared stub takes a page in its own harnesses; a BrowserContext exposes
  // the same three methods it uses (route, routeWebSocket, addInitScript) and
  // this harness runs exactly one page per context, so the effect is identical.
  await stubDashboardApi(context, {
    theme,
    // Seed the docked terminal open with one tab, the same persisted shape a
    // reload restores — clicking the nav row in a harness is flaky. Passed here
    // rather than via addInitScript because the stub's own script clears storage.
    localStorageEntries: {
      'mc-bottom-terminal': JSON.stringify({
        open: true, height: 300,
        tabs: [{ id: 'fixture-tab-1' }], activeId: 'fixture-tab-1',
      }),
    },
    extra: async (path, route) => {
      // Create-slot must return a keyed slot object — the shared stub's array
      // answer puts a keyless slot in redux and crashes the shell.
      if (path === '/api/chat/slots' && route.request().method() === 'POST') {
        await json(route, { key: 'fixture-chat', title: 'New Session…', agent: 'kirocrew' })
        return true
      }
      if (path.startsWith('/api/chat/slots/')) {
        await json(route, {})
        return true
      }
      return false
    },
  })

  // AFTER the shared stub, which routes `/api/ws` itself: the terminal socket
  // matches that pattern too, and the LAST matching route registered wins.
  // Scripted PTY: ready + prompt on connect, then echo keystrokes back so the
  // text we later select is really on screen (CR → CRLF, as a shell would).
  await context.routeWebSocket(/\/api\/ws\/terminal\//, ws => {
    ws.send(JSON.stringify({ type: 'ready' }))
    ws.send(Buffer.from('\x1b[1;32m$\x1b[0m '))
    ws.onMessage(m => {
      if (typeof m === 'string') return // resize/control frames
      const data = m.toString('utf8')
      // Echo the keystrokes back (CR → CRLF, as a shell would). On the Enter
      // that submits the line, ALSO reprint a fresh `$ ` prompt on the next row
      // — the realistic live-shell shape: the command's OUTPUT lands on its own
      // line and the cursor then rests on a reprinted prompt below it. This is
      // what stage 1 must skip (select the output above the prompt, not the
      // prompt), so the capture exercises the real code path.
      ws.send(Buffer.from(data.replace(/\r/g, '\r\n')))
      if (data.includes('\r')) {
        ws.send(Buffer.from('\x1b[1;32m$\x1b[0m '))
      }
    })
  })
}

/** New page with touch emulation active (the key bar's render condition). */
async function touchPage(context, base) {
  const page = await context.newPage()
  logPageProblems(page)
  const cdp = await context.newCDPSession(page)
  await cdp.send('Emulation.setEmulatedMedia', {
    features: [
      { name: 'hover', value: 'none' },
      { name: 'any-hover', value: 'none' },
      { name: 'pointer', value: 'coarse' },
      { name: 'any-pointer', value: 'coarse' },
    ],
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.getByTestId('terminal-key-bar').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(1200) // scripted PTY prompt renders
  return page
}

/** Crop: the terminal panel including the key bar (the surface under test). */
async function shotPanel(page, name) {
  const bar = await page.getByTestId('terminal-key-bar').boundingBox()
  if (!bar) throw new Error('key bar not laid out')
  const clip = {
    x: Math.max(0, bar.x - 8),
    y: Math.max(0, bar.y - 240),
    width: Math.min(page.viewportSize().width, bar.width + 16),
    height: bar.height + 240 + 12,
  }
  await page.screenshot({ path: `${OUT}/${name}.png`, clip })
}

/**
 * Type text into the scripted PTY so it echoes onto its own buffer line, then
 * create the selection the PRODUCTION touch way: tap the Select soft key. No
 * `page.mouse` drag — a mouse gesture is the exact path touch devices lack, and
 * relying on one was a reviewer criticism of the earlier harness. Select runs a
 * staged cycle; `taps` says how many times to tap it (1 = last line, 2 = whole
 * buffer). Returns the typed text.
 *
 * The Select button is located by its icon, NOT by accessible name: the name is
 * "Select" only in the idle state and becomes the SHORT stage label ("Line ·
 * tap for all", "All · tap for line") after the first tap, so a name-based
 * locator would miss the button on the second tap.
 */
async function typeThenSelect(page, text, taps = 1) {
  await page.locator('.xterm-helper-textarea').first().click()
  await page.keyboard.type(text)
  await page.keyboard.press('Enter')
  await page.waitForTimeout(400)
  const select = page.getByTestId('terminal-key-bar').locator('button:has(svg.lucide-text-select)')
  for (let i = 0; i < taps; i++) {
    await select.click()
    await page.waitForTimeout(200)
  }
  return text
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  // ---- Grant context: real clipboard-write permission ----
  const grantCtx = await browser.newContext({
    viewport: { width: 820, height: 780 }, deviceScaleFactor: 2,
    hasTouch: true,
    permissions: ['clipboard-read', 'clipboard-write'],
  })
  await stubContext(grantCtx, 'dark')
  const page = await touchPage(grantCtx, base)

  // 1) The bar with the Select + Copy keys present, prompt rendered.
  const copyBtn = page.getByTestId('terminal-key-bar').getByRole('button', { name: 'Copy' })
  const selectBtn = page.getByTestId('terminal-key-bar').getByRole('button', { name: 'Select' })
  if (!(await copyBtn.count())) throw new Error('Copy key not rendered')
  if (!(await selectBtn.count())) throw new Error('Select key not rendered')
  await shotPanel(page, '01-copy-key-touch-dark')

  // 2) Tap Select TWICE (NO mouse drag) to advance the staged cycle to stage 2
  //    — the whole buffer — and capture it WITH its SHORT stage label ("All ·
  //    tap for line") visible on the key: the staged cycle is what makes
  //    multi-line copy discoverable (UX) and what reaches the whole buffer in
  //    ≤2 taps. xterm paints the selection into `.xterm-selection` rect divs
  //    (not the DOM Selection API), so assert those rects exist as proof the
  //    selection is on screen. The label is PERSISTENT (it stays while the
  //    selection is live), so there is no revert race to guard against.
  const marker = 'copy-me-from-the-touch-key'
  await typeThenSelect(page, marker, 2) // 2 taps → stage 2 (whole buffer)
  const selectionRects = await page.locator('.xterm-selection div').count()
  if (selectionRects < 1) {
    throw new Error('Select key did not create a visible xterm selection (no .xterm-selection rects)')
  }
  const allStage = page.getByTestId('terminal-key-bar')
    .getByRole('button', { name: 'All · tap for line' })
  await allStage.waitFor({ state: 'visible', timeout: 5000 })
  await shotPanel(page, '02-select-all-stage-dark')

  // 3) Tap Copy: reads the Select-made selection into the clipboard and shows
  //    the transient "Copied" success state. Read the clipboard back to prove
  //    the getSelection → writeText path end to end, then capture "Copied".
  await copyBtn.click()
  await page.waitForTimeout(300)
  const clip = await page.evaluate(() => navigator.clipboard.readText())
  if (!clip.includes(marker)) {
    throw new Error('copied clipboard did not contain the selection: ' + JSON.stringify(clip).slice(0, 200))
  }
  const copied = page.getByTestId('terminal-key-bar').getByRole('button', { name: 'Copied' })
  await copied.waitFor({ state: 'visible', timeout: 5000 })
  await shotPanel(page, '03-copy-success-dark')
  // The success beat self-reverts after ~2s; a shot past the revert would show
  // an ordinary Copy key — misleading evidence, fail loud.
  if (!(await copied.isVisible())) {
    throw new Error('"Copied" state reverted before the screenshot was taken — rerun')
  }

  // 4) No-selection guidance: tap Copy with nothing selected. The key must
  //    NOT copy the buffer — it shows "Tap Select, then Copy".
  const noselCtx = await browser.newContext({
    viewport: { width: 820, height: 780 }, deviceScaleFactor: 2,
    hasTouch: true,
    permissions: ['clipboard-read', 'clipboard-write'],
  })
  await stubContext(noselCtx, 'dark')
  const noselPage = await touchPage(noselCtx, base)
  await noselPage.getByTestId('terminal-key-bar').getByRole('button', { name: 'Copy' }).click()
  const guidance = noselPage.getByRole('button', { name: 'Tap Select, then Copy' })
  await guidance.waitFor({ state: 'visible', timeout: 5000 })
  await shotPanel(noselPage, '04-copy-no-selection-dark')
  // The guidance self-reverts after ~4s; if the shot slipped past the revert
  // it captured an ordinary Copy key — misleading evidence, fail loud.
  if (!(await guidance.isVisible())) {
    throw new Error('guidance state reverted before the screenshot was taken — rerun')
  }

  // ---- Phone width (390px portrait): Copy must be ON-SCREEN, pinned ----
  const phoneCtx = await browser.newContext({
    viewport: { width: 390, height: 720 }, deviceScaleFactor: 2,
    hasTouch: true, isMobile: true,
  })
  await stubContext(phoneCtx, 'dark')
  const phonePage = await touchPage(phoneCtx, base)
  const pinned = phonePage.getByTestId('terminal-key-bar').getByRole('button', { name: 'Copy' })
  const box = await pinned.boundingBox()
  if (!box || box.x + box.width > 390 + 1 || box.x < 0) {
    throw new Error(`Copy key not fully on-screen at 390px: ${JSON.stringify(box)}`)
  }
  await phonePage.screenshot({ path: `${OUT}/06-copy-key-phone-390-dark.png`, clip: { x: 0, y: Math.max(0, box.y - 60), width: 390, height: Math.min(720, box.y + box.height + 12) - Math.max(0, box.y - 60) } })

  // No-selection guidance at 390px: the label must stay inside the viewport.
  const noselPhoneCtx = await browser.newContext({
    viewport: { width: 390, height: 720 }, deviceScaleFactor: 2,
    hasTouch: true, isMobile: true,
    permissions: ['clipboard-read', 'clipboard-write'],
  })
  await stubContext(noselPhoneCtx, 'dark')
  const noselPhone = await touchPage(noselPhoneCtx, base)
  await noselPhone.getByTestId('terminal-key-bar').getByRole('button', { name: 'Copy' }).click()
  const guidancePinned = noselPhone.getByRole('button', { name: 'Tap Select, then Copy' })
  await guidancePinned.waitFor({ state: 'visible', timeout: 5000 })
  const gbox = await guidancePinned.boundingBox()
  if (!gbox || gbox.x + gbox.width > 390 + 1 || gbox.x < 0) {
    throw new Error(`guidance-state key overflows the 390px viewport: ${JSON.stringify(gbox)}`)
  }
  await noselPhone.screenshot({ path: `${OUT}/07-copy-no-selection-phone-390.png`, clip: { x: 0, y: Math.max(0, gbox.y - 60), width: 390, height: Math.min(720, gbox.y + gbox.height + 12) - Math.max(0, gbox.y - 60) } })

  // ---- Phone width (390px): a Select STAGE LABEL must be FULLY on the key AND
  //      the sibling Copy button's TEXT label must survive (UX flagged that the
  //      long teaching sentence crowded Copy toward its bare-glyph min width at
  //      390px). Type + tap Select once, then assert the SHORT front-loaded
  //      "Line · tap for all" string is the button's accessible name and its
  //      rendered label is not truncated (scrollWidth ≤ clientWidth), and that
  //      Copy still shows its "Copy" text (not clipped to the glyph). The label
  //      is persistent, so there is no revert race. ----
  const stagePhoneCtx = await browser.newContext({
    viewport: { width: 390, height: 720 }, deviceScaleFactor: 2,
    hasTouch: true, isMobile: true,
    permissions: ['clipboard-read', 'clipboard-write'],
  })
  await stubContext(stagePhoneCtx, 'dark')
  const stagePhone = await touchPage(stagePhoneCtx, base)
  await typeThenSelect(stagePhone, 'phone-select', 1) // 1 tap → stage 1 (last line)
  const stageKey = stagePhone.getByTestId('terminal-key-bar')
    .getByRole('button', { name: 'Line · tap for all' })
  await stageKey.waitFor({ state: 'visible', timeout: 5000 })
  // The visible label span must not be clipped: front-loaded + short means the
  // whole string fits inside the truncate box at 390px.
  const labelClipped = await stageKey.evaluate(btn => {
    const span = btn.querySelector('span[aria-hidden="true"]')
    return span ? span.scrollWidth > span.clientWidth + 1 : true
  })
  if (labelClipped) {
    throw new Error('Select stage label is truncated at 390px — the short string still does not fit')
  }
  // Copy's TEXT label must survive alongside the live stage label — the whole
  // point of the short-label fix (GPT/UX). Assert its visible span still reads
  // "Copy" and is not clipped to the bare glyph.
  const copyPhoneKey = stagePhone.getByTestId('terminal-key-bar')
    .getByRole('button', { name: 'Copy' })
  const copyLabelOk = await copyPhoneKey.evaluate(btn => {
    const span = btn.querySelector('span[aria-hidden="true"]')
    return !!span && span.textContent === 'Copy' && span.scrollWidth <= span.clientWidth + 1
  })
  if (!copyLabelOk) {
    throw new Error('Copy text label did not survive beside the Select stage label at 390px')
  }
  const sbox = await stageKey.boundingBox()
  await stagePhone.screenshot({ path: `${OUT}/08-select-stage-phone-390.png`, clip: { x: 0, y: Math.max(0, sbox.y - 60), width: 390, height: Math.min(720, sbox.y + sbox.height + 12) - Math.max(0, sbox.y - 60) } })

  // ---- Light theme parity ----
  const lightCtx = await browser.newContext({
    viewport: { width: 820, height: 780 }, deviceScaleFactor: 2,
    hasTouch: true,
    permissions: ['clipboard-read', 'clipboard-write'],
  })
  await stubContext(lightCtx, 'light')
  const lightPage = await touchPage(lightCtx, base)
  await shotPanel(lightPage, '05-copy-key-touch-light')

  await browser.close()
  srv.close()
  console.log(`wrote 8 screenshots to ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
