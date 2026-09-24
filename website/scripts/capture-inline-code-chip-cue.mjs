/**
 * Screenshot harness for the inline-code chip cue.
 *
 * Proves, from the live DOM before any pixel is taken:
 *  1. At rest, a click-to-copy chip wears code styling (no accent colour, no
 *     underline on hover, the `copy` cursor) while a backend-confirmed path chip
 *     keeps the actionable look (accent, hover underline, pointer, glyph) — and
 *     each chip's accessible name states ITS click ("Copy …" vs "Open …"). The
 *     colours are asserted as COMPUTED values on both Kiro themes: the theme's
 *     inline-code rule outranks the `text-accent` utility, so only a chip rule
 *     keyed on `data-chip-action` makes the accent actually paint.
 *  2. Hovering the copy chip shows the tooltip that names the action.
 *  3. Clicking it copies, flips the tooltip to "Copied!", and fills the sr-only
 *     status region with the same word; the bubble survives the pointer leaving
 *     and closes by itself when the flash ends.
 *  4. With both clipboard layers stubbed to refuse, the click renders the
 *     "Copy failed" notice in the SAME bubble (an ErrorNotice, announced once),
 *     claims nothing, and moves nothing in the paragraph; it too outlives the
 *     leave and clears itself.
 *  5. A long path chip still breaks across lines (measured: more than one
 *     client rect), and a copy on a long copy chip moves nothing on the line —
 *     the text after it has the same box before and after the confirmation.
 *  6. On the default theme, a session chip paints in the accent like the path
 *     chips (the copy chip beside it is neutral), and its Ctrl+click copy
 *     confirms in its title only once the write lands, without switching.
 *  7. With the clipboard refusing, the title-cued chips — session, path,
 *     broken image — each open the SAME bubble-borne "Copy failed" notice the
 *     copy chip uses, at the pressed chip, one bubble at a time, and nothing
 *     enters the sentence around them.
 *
 * Paths are probed through the same `/api/file-read` HEAD request production
 * issues; a route registered AFTER the harness's catch-all answers it with the
 * `X-Path-Kind` header (Playwright matches newest-first).
 *
 * Usage: node scripts/capture-inline-code-chip-cue.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

const { openTranscriptHarness } = await import('./lib/transcript-harness.mjs')

const OUT = process.argv[2] || '../temp-screenshots/inline-code-chip-cue'
const SLOT = 'chat-chipcue'
// A synthetic project: the value renders into the frame, so it must not be a
// real checkout path.
const PROJECT = '/home/user/project'

mkdirSync(OUT, { recursive: true })

const FILE = '/home/user/project/website/vitest.config.mts'
const DIR = '/home/user/project/website/src/test'
const LONG_FILE = '/home/user/project/website/src/components/feature/subfeature/another/level/AnExtremelyLongComponentFileNameThatKeepsGoingAndGoing.tsx'
const LONG_COPY = 'SOME_VERY_LONG_ENVIRONMENT_VARIABLE_NAME_THAT_WRAPS_THE_LINE=/opt/some/deeply/nested/config/path/for/the/gateway/service/settings.toml'
const OTHER_SLOT = 'chat-42-1758000000'
/** A local image that 404s, so the message renders the broken-image chip. */
const MISSING_IMG = '/home/user/project/website/temp-screenshots/sidebar-before.png'
/** What the stubbed backend confirms. Anything else answers 404. */
const KINDS = { [FILE]: 'file', [DIR]: 'dir', [LONG_FILE]: 'file' }

const now = Date.now() / 1000
const slots = [{
  key: SLOT, title: 'Inline code chips', running: false,
  last_message: 'where the tests live', messages: 4, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
}, {
  // A second OPEN session, so its key in a message becomes a session chip
  // (the reader's own session never does).
  key: OTHER_SLOT, title: 'Sidebar rewrite', running: false,
  last_message: 'rebased onto main', messages: 2, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now) - 60,
  source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 6, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 900, content: 'Where do the frontend tests live, and how do I run them?' },
    {
      role: 'assistant', ts: now - 850, content: [
        `Run \`npm test\` from the website directory. The runner config is \`${FILE}\` and the fixtures sit under \`${DIR}\`.`,
        '',
        'Set `NODE_ENV=production` before `npm run build` to get the shipped bundle.',
      ].join('\n'),
    },
    { role: 'user', ts: now - 500, content: 'And the long ones?' },
    {
      role: 'assistant', ts: now - 30, content: [
        `The deepest one is \`${LONG_FILE}\` — it wraps like any other inline code.`,
        '',
        `The gateway reads \`${LONG_COPY}\` at boot, then falls back to defaults.`,
      ].join('\n'),
    },
    { role: 'user', ts: now - 25, content: 'Where did the sidebar work go, and the screenshot?' },
    {
      role: 'assistant', ts: now - 20, content: [
        `The sidebar rewrite continues in \`${OTHER_SLOT}\`; its runner config is \`${FILE}\` as well.`,
        '',
        `![Sidebar before the rewrite](${MISSING_IMG})`,
      ].join('\n'),
    },
  ],
}

async function main() {
  const h = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots, detail,
    // Tall enough for all six messages: a transcript that scrolls pins the
    // user's question over the top of the frame it would otherwise clip.
    viewport: { width: 1180, height: 1500 },
  })
  await h.page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: h.base })

  // Newest route wins: answer the path probe with the confirmed kinds, and 404
  // every image read so the message's screenshot renders as the broken-image chip.
  await h.page.route('**/api/file-read**', route => {
    const url = new URL(route.request().url())
    const kind = KINDS[url.searchParams.get('path') ?? '']
    if (!kind) return route.fulfill({ status: 404, body: '' })
    return route.fulfill({ status: 200, headers: { 'X-Path-Kind': kind }, body: '' })
  })
  await h.page.route('**/api/file-raw**', route => route.fulfill({ status: 404, body: '' }))

  let failures = 0
  const assert = (label, ok) => {
    console.log(`${ok ? 'PASS' : 'FAIL'}: ${label}`)
    if (!ok) failures += 1
  }
  /** Clip the frame to `locator`'s box, grown by `pad` so a bubble above it stays in.
   *  The transcript is taller than the viewport, so bring the target fully into
   *  view first — a box partly above y=0 would clip whatever else sits there. */
  const shotAround = async (locator, name, pad = 48) => {
    await locator.scrollIntoViewIfNeeded()
    await h.page.waitForTimeout(200)
    const box = await locator.boundingBox()
    const clip = {
      x: Math.max(0, box.x - 16), y: Math.max(0, box.y - pad),
      width: Math.min(1180 - Math.max(0, box.x - 16), box.width + 32), height: box.height + pad + 16,
    }
    await h.page.screenshot({ path: `${OUT}/${name}.png`, clip })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  const copyChip = () => h.page.locator('code[aria-label="Copy npm test"]')
  const fileChip = () => h.page.locator(`code[data-path="${FILE}"]`).first()
  const dirChip = () => h.page.locator(`code[data-path="${DIR}"]`)
  const assistantMsg = i => h.page.locator('[data-role="assistant"] .msg-content').nth(i)
  const firstMsg = () => assistantMsg(0)
  const lastMsg = () => assistantMsg(1)
  const thirdMsg = () => assistantMsg(2)

  for (const theme of ['dark', 'light']) {
    await h.load(theme, { selector: 'textarea[data-composer-input]', settle: 900 })
    await fileChip().waitFor({ timeout: 10_000 })
    await dirChip().waitFor({ timeout: 10_000 })

    const rest = await h.page.evaluate(([copySel, fileSel, dirSel]) => {
      const read = sel => {
        const el = document.querySelector(sel)
        const cs = getComputedStyle(el)
        return {
          name: el.getAttribute('aria-label'), cls: el.className, cursor: cs.cursor,
          display: cs.display, color: cs.color, glyph: !!el.querySelector('svg:not([class*="opacity-0"])'),
          title: el.getAttribute('title'), action: el.getAttribute('data-chip-action'),
          decoLine: cs.textDecorationLine, decoStyle: cs.textDecorationStyle,
          // The prose around the chip: what "neutral" means on this theme.
          prose: getComputedStyle(el.closest('p')).color,
        }
      }
      return { copy: read(copySel), file: read(fileSel), dir: read(dirSel) }
    }, ['code[aria-label="Copy npm test"]', `code[data-path="${FILE}"]`, `code[data-path="${DIR}"]`])

    assert(`${theme}: copy chip is named for its click (${rest.copy.name})`, rest.copy.name === 'Copy npm test')
    assert(`${theme}: file chip is named for its click (${rest.file.name})`, rest.file.name === `Open ${FILE}`)
    assert(`${theme}: dir chip is named for its click (${rest.dir.name})`, rest.dir.name === `Browse ${DIR}`)
    assert(`${theme}: copy chip cursor is "copy" (${rest.copy.cursor})`, rest.copy.cursor === 'copy')
    assert(`${theme}: path chips cursor is "pointer" (${rest.file.cursor}, ${rest.dir.cursor})`,
      rest.file.cursor === 'pointer' && rest.dir.cursor === 'pointer')
    assert(`${theme}: copy chip carries no accent colour and no hover-solid underline`,
      !/text-accent|hover:underline/.test(rest.copy.cls))
    assert(`${theme}: path chips carry the actionable classes and a visible glyph`,
      /text-accent/.test(rest.file.cls) && /hover:underline/.test(rest.file.cls) && rest.file.glyph && rest.dir.glyph)
    // The COMPUTED colour, not the class: on the shipped default theme (Kiro)
    // index.css paints every inline code span (`[data-theme="kiro-*"] .msg-content
    // :not(pre)>code{color:…}`, specificity 0,2,2) and the unlayered `text-accent`
    // utility (0,1,0) loses to it, so the class alone painted nothing — every chip
    // wore the code colour, and even with the accent restored the two purples
    // (#c19aff / #b07fff) read as one kind of chip. The rule: a copy chip is
    // NEUTRAL (the prose colour, as on every non-Kiro theme) with a dotted
    // underline; a chip whose click navigates is the accent. Keyed on
    // `data-chip-action`.
    const ACCENT_COLOUR = { dark: 'rgb(176, 127, 255)', light: 'rgb(142, 72, 255)' } // #b07fff (message links) / --accent
    assert(`${theme}: chips declare their action (${rest.copy.action} / ${rest.file.action} / ${rest.dir.action})`,
      rest.copy.action === 'copy' && rest.file.action === 'navigate' && rest.dir.action === 'navigate')
    assert(`${theme}: copy chip paints in the NEUTRAL prose colour (${rest.copy.color} vs prose ${rest.copy.prose})`,
      rest.copy.color === rest.copy.prose)
    assert(`${theme}: copy chip wears a dotted underline at rest (${rest.copy.decoLine} / ${rest.copy.decoStyle})`,
      rest.copy.decoLine === 'underline' && rest.copy.decoStyle === 'dotted')
    assert(`${theme}: path chips paint in the accent (${rest.file.color}, ${rest.dir.color})`,
      rest.file.color === ACCENT_COLOUR[theme] && rest.dir.color === ACCENT_COLOUR[theme])
    assert(`${theme}: path chips carry no underline at rest (${rest.file.decoLine})`, rest.file.decoLine === 'none')
    assert(`${theme}: copy and path chips differ in colour (${rest.copy.color} vs ${rest.file.color})`,
      rest.copy.color !== rest.file.color)
    assert(`${theme}: copy chip has no glyph and no native title`, !rest.copy.glyph && rest.copy.title === null)
    assert(`${theme}: file chip title names the open action`, /Click to open/.test(rest.file.title ?? ''))
    assert(`${theme}: every chip is a plain inline box (${rest.copy.display} / ${rest.file.display})`,
      rest.copy.display === 'inline' && rest.file.display === 'inline' && rest.dir.display === 'inline')

    await shotAround(firstMsg(), `rest-${theme}`, 16)
  }

  // --- Hover: the tooltip names the action; the underline stays the dotted one. ---
  await copyChip().hover()
  await h.page.waitForTimeout(300)
  const hoverTip = await h.page.locator('[role="tooltip"]').textContent().catch(() => null)
  assert(`hover: tooltip reads "Click to copy" (${hoverTip})`, hoverTip === 'Click to copy')
  const hoverDeco = await copyChip().evaluate(el => `${getComputedStyle(el).textDecorationLine}/${getComputedStyle(el).textDecorationStyle}`)
  assert(`hover: copy chip keeps its dotted underline, never a link's solid one (${hoverDeco})`, hoverDeco === 'underline/dotted')
  const describedBy = await copyChip().getAttribute('aria-describedby')
  const tipId = await h.page.locator('[role="tooltip"]').getAttribute('id')
  assert('hover: tooltip is the chip\'s accessible description', !!describedBy && describedBy === tipId)
  await shotAround(firstMsg(), 'hover-tooltip')

  // Contrast: hovering the path chip underlines it SOLID — the link look it keeps.
  await fileChip().hover()
  await h.page.waitForTimeout(150)
  const pathDeco = await fileChip().evaluate(el => `${getComputedStyle(el).textDecorationLine}/${getComputedStyle(el).textDecorationStyle}`)
  assert(`hover: path chip underlines solid (${pathDeco})`, pathDeco === 'underline/solid')
  assert('hover: path chip shows no instant bubble (native title only)', (await h.page.locator('[role="tooltip"]').count()) === 0)

  // --- Click: copies, confirms in the bubble and the status region, clears. ---
  // The status region is a sibling INSIDE the message DOM (out of flow, sr-only),
  // so the message's textContent legitimately gains "Copied!"; what must not
  // change is the chip's own text and the paragraph's box.
  const chipTextBefore = await copyChip().evaluate(el => el.textContent)
  const paraBefore = await firstMsg().evaluate(el => JSON.stringify(el.querySelector('p').getBoundingClientRect()))
  const copyBoxBefore = await copyChip().boundingBox()
  await copyChip().hover()
  await h.page.waitForTimeout(200)
  await copyChip().click()
  await h.page.waitForTimeout(250)
  const clipboard = await h.page.evaluate(() => navigator.clipboard.readText()).catch(() => null)
  assert(`click: clipboard holds the chip text (${JSON.stringify(clipboard)})`, clipboard === 'npm test')
  const copiedTip = await h.page.locator('[role="tooltip"]').textContent().catch(() => null)
  assert(`click: tooltip flips to "Copied!" (${copiedTip})`, copiedTip === 'Copied!')
  const status = await h.page.locator('[role="status"]').filter({ hasText: 'Copied!' }).count()
  assert(`click: one status region announces "Copied!" (${status})`, status === 1)
  const chipTextAfter = await copyChip().evaluate(el => el.textContent)
  const paraAfter = await firstMsg().evaluate(el => JSON.stringify(el.querySelector('p').getBoundingClientRect()))
  const copyBoxAfter = await copyChip().boundingBox()
  assert('click: nothing was appended inside the chip', chipTextAfter === chipTextBefore)
  assert('click: the paragraph box did not change', paraAfter === paraBefore)
  assert('click: the chip box did not change', JSON.stringify(copyBoxBefore) === JSON.stringify(copyBoxAfter))
  await shotAround(firstMsg(), 'copied-confirmation')
  // The pointer moves on right after the click, as a mouse user does: the
  // confirmation must survive the leave and close on its own when the flash ends.
  await h.page.mouse.move(5, 5)
  await h.page.waitForTimeout(100)
  const leftTip = await h.page.locator('[role="tooltip"]').textContent().catch(() => null)
  assert(`click: "Copied!" survives the pointer leaving (${leftTip})`, leftTip === 'Copied!')
  await h.page.waitForTimeout(1600)
  assert('click: the bubble closed by itself once the flash ended', (await h.page.locator('[role="tooltip"]').count()) === 0)
  assert('click: status region is empty again',
    (await h.page.locator('[role="status"]').filter({ hasText: 'Copied!' }).count()) === 0)
  // Hovering again shows the prompt, not a stale outcome.
  await copyChip().hover()
  await h.page.waitForTimeout(300)
  const clearedTip = await h.page.locator('[role="tooltip"]').textContent().catch(() => null)
  assert(`click: tooltip is back to "Click to copy" (${clearedTip})`, clearedTip === 'Click to copy')
  await h.page.mouse.move(5, 5)
  await h.page.waitForTimeout(100)

  // --- Refused write: both clipboard layers say no -> the failure is rendered. ---
  // The async Clipboard API rejects (a refused `clipboard-write` permission) and
  // the execCommand fallback reports false, which is the shape a sandboxed or
  // permission-denied document produces; `copyToClipboard` then resolves false.
  await h.page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: () => Promise.reject(new DOMException('denied', 'NotAllowedError')) },
    })
    document.execCommand = () => false
  })
  const envChip = h.page.locator('code[aria-label="Copy NODE_ENV=production"]')
  const envParaBefore = await envChip.evaluate(el => JSON.stringify(el.closest('p').getBoundingClientRect()))
  await envChip.hover()
  await h.page.waitForTimeout(200)
  await envChip.click()
  await h.page.waitForTimeout(300)
  // Same place as the confirmation: the bubble, through ErrorNotice. Nothing in
  // the text flow, so the paragraph's box is byte-identical.
  const failNotice = h.page.locator('[role="tooltip"] [data-testid="md-chip-copy-error"]')
  assert(`refused: exactly one "Copy failed" notice renders, inside the bubble (${await failNotice.count()})`, (await failNotice.count()) === 1)
  assert('refused: the notice reads "Copy failed"', /Copy failed/.test((await failNotice.textContent()) ?? ''))
  // Announced once, through the notice itself: it is the bubble's accessible
  // `role="alert"` (no aria-hidden ancestor) and the only alert that carries
  // the failure — no sr-only copy of it anywhere.
  const bubbleAlerts = await h.page.locator('[role="tooltip"] [role="alert"]').count()
  const hiddenNotice = await h.page.locator('[aria-hidden="true"] [data-testid="md-chip-copy-error"]').count()
  const alertTexts = await h.page.locator('[role="alert"]').evaluateAll(els => els.map(el => (el.textContent ?? '').trim().slice(0, 60)))
  const failAlerts = alertTexts.filter(t => /Copy failed/.test(t)).length
  assert(`refused: the notice is the accessible alert, and the only one carrying the failure (${await failNotice.getAttribute('role')}, ${bubbleAlerts}, ${hiddenNotice}, ${failAlerts}; alerts: ${JSON.stringify(alertTexts)})`,
    (await failNotice.getAttribute('role')) === 'alert' && bubbleAlerts === 1 && hiddenNotice === 0 && failAlerts === 1)
  assert('refused: nothing entered the paragraph',
    await envChip.evaluate(el => el.closest('p').querySelector('[data-testid="md-chip-copy-error"], [role="alert"]') === null))
  const envParaAfter = await envChip.evaluate(el => JSON.stringify(el.closest('p').getBoundingClientRect()))
  assert('refused: the paragraph box did not change', envParaBefore === envParaAfter)
  const refusedTip = await h.page.locator('[role="tooltip"]').textContent().catch(() => null)
  assert(`refused: tooltip never claims "Copied!" (${refusedTip})`, !!refusedTip && !/Copied!/.test(refusedTip))
  assert('refused: nothing announced as copied',
    (await h.page.locator('[role="status"]').filter({ hasText: 'Copied!' }).count()) === 0)
  await shotAround(firstMsg(), 'copy-failed-notice')
  // The failure holds the bubble through the leave too...
  await h.page.mouse.move(5, 5)
  await h.page.waitForTimeout(100)
  assert('refused: the failure survives the pointer leaving', (await failNotice.count()) === 1)
  // ...and through the pointer reaching the NEXT chip: held bubbles rank above
  // hints, so the neighbour's hint yields (one bubble, still the failure)...
  await h.page.locator('code[aria-label="Copy npm run build"]').hover()
  await h.page.waitForTimeout(300)
  assert('refused: moving onto the next chip does not take the failure', (await failNotice.count()) === 1)
  assert('refused: one bubble while the failure holds', (await h.page.locator('[role="tooltip"]').count()) === 1)
  // ...and gets its turn once the flash ends with the pointer still resting there.
  await h.page.waitForTimeout(2700)
  const afterFlashTip = await h.page.locator('[role="tooltip"]').textContent().catch(() => null)
  assert(`refused: the failure cleared itself and the neighbour's hint took over (${afterFlashTip})`,
    (await failNotice.count()) === 0 && afterFlashTip === 'Click to copy')
  await h.page.mouse.move(5, 5)
  await h.page.waitForTimeout(100)
  assert('refused: the hint closed on leave, so the bubble is gone', (await h.page.locator('[role="tooltip"]').count()) === 0)
  // Restore the real clipboard for the long-chip scene below.
  await h.page.reload({ waitUntil: 'domcontentloaded' })
  await h.page.waitForSelector('textarea[data-composer-input]', { timeout: 20000 })
  await h.page.waitForTimeout(900)

  // --- Long chips: still wrap; a copy moves nothing. ---
  const longFile = h.page.locator(`code[data-path="${LONG_FILE}"]`)
  await longFile.waitFor({ timeout: 10_000 })
  const longCopy = h.page.locator(`code[aria-label="Copy ${LONG_COPY}"]`)
  await lastMsg().scrollIntoViewIfNeeded()
  const rects = await longFile.evaluate(el => el.getClientRects().length)
  assert(`wrap: the long path chip spans ${rects} line boxes (must be > 1)`, rects > 1)
  const copyRects = await longCopy.evaluate(el => el.getClientRects().length)
  assert(`wrap: the long copy chip spans ${copyRects} line boxes (must be > 1)`, copyRects > 1)
  // The prose right after the long copy chip: its box must not move when the
  // confirmation shows — that is what "non-layout" means in pixels.
  const tailBefore = await lastMsg().evaluate(el => {
    const p = el.querySelectorAll('p')[1]
    const r = p.getBoundingClientRect()
    return { h: r.height, bottom: r.bottom }
  })
  await longCopy.hover()
  await h.page.waitForTimeout(200)
  await longCopy.click()
  await h.page.waitForTimeout(250)
  const longTip = await h.page.locator('[role="tooltip"]').textContent().catch(() => null)
  assert(`wrap: long copy chip confirms in the bubble (${longTip})`, longTip === 'Copied!')
  const tailAfter = await lastMsg().evaluate(el => {
    const p = el.querySelectorAll('p')[1]
    const r = p.getBoundingClientRect()
    return { h: r.height, bottom: r.bottom }
  })
  assert(`wrap: paragraph box unchanged by the confirmation (${JSON.stringify(tailBefore)} -> ${JSON.stringify(tailAfter)})`,
    tailBefore.h === tailAfter.h && tailBefore.bottom === tailAfter.bottom)
  const copyRectsAfter = await longCopy.evaluate(el => el.getClientRects().length)
  assert(`wrap: long copy chip still spans ${copyRectsAfter} line boxes while confirmed`, copyRectsAfter === copyRects)
  await shotAround(lastMsg(), 'long-path-wraps')

  // --- Session chip, on the default (Kiro dark) theme: recoloured by the same
  // rule as the path chips, and its Ctrl+click copy gated on the write. ---
  await h.load('dark', { selector: 'textarea[data-composer-input]', settle: 900 })
  await h.page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: h.base })
  const sessionChip = h.page.locator(`code[data-session-key="${OTHER_SLOT}"]`)
  await sessionChip.waitFor({ timeout: 10_000 })
  const brokenChip = h.page.locator('[role="button"]').filter({ hasText: 'Sidebar before the rewrite' })
  await brokenChip.waitFor({ timeout: 10_000 })
  await thirdMsg().scrollIntoViewIfNeeded()
  const sessionRest = await sessionChip.evaluate(el => {
    const cs = getComputedStyle(el)
    return {
      action: el.getAttribute('data-chip-action'), color: cs.color, cursor: cs.cursor,
      title: el.getAttribute('title'), name: el.getAttribute('aria-label'), svgs: el.querySelectorAll('svg').length,
      prose: getComputedStyle(el.closest('p')).color,
    }
  })
  assert(`session: chip declares "navigate" and paints in the accent (${sessionRest.action}, ${sessionRest.color})`,
    sessionRest.action === 'navigate' && sessionRest.color === 'rgb(176, 127, 255)')
  assert(`session: chip is named for its click (${sessionRest.name})`, sessionRest.name === `Switch to session ${OTHER_SLOT}`)
  assert('session: title advertises Ctrl+click to copy', /Ctrl\+click to copy/.test(sessionRest.title ?? ''))
  assert(`session: the accent differs from the prose colour a copy chip wears (${sessionRest.color} vs ${sessionRest.prose})`,
    sessionRest.color !== sessionRest.prose)
  await sessionChip.click({ modifiers: ['Control'] })
  await h.page.waitForTimeout(250)
  const sessionCopied = await sessionChip.evaluate(el => ({ title: el.getAttribute('title'), svgs: el.querySelectorAll('svg').length }))
  const sessionClip = await h.page.evaluate(() => navigator.clipboard.readText()).catch(() => null)
  assert(`session: Ctrl+click copies the key (${JSON.stringify(sessionClip)})`, sessionClip === OTHER_SLOT)
  assert(`session: the title confirms once the write lands (${sessionCopied.title})`, sessionCopied.title === 'Copied!')
  assert(`session: the check icon appears (${sessionRest.svgs} -> ${sessionCopied.svgs})`, sessionCopied.svgs === sessionRest.svgs + 1)
  assert('session: Ctrl+click did not switch sessions',
    (await h.page.locator('textarea[data-composer-input]').count()) === 1 && (await sessionChip.count()) === 1)
  await shotAround(thirdMsg(), 'session-chip-copied', 16)
  await h.page.waitForTimeout(1600)
  assert('session: the confirmation clears after 1.5s', (await sessionChip.getAttribute('title')) !== 'Copied!')

  // --- The ONE failure surface on the title-cued chips: session, path, broken
  // image. Both clipboard layers refuse, as above. Each refusal opens the same
  // bubble-borne "Copy failed" notice the copy chip uses, at the pressed chip,
  // and nothing enters the sentence around it. ---
  await h.page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: () => Promise.reject(new DOMException('denied', 'NotAllowedError')) },
    })
    document.execCommand = () => false
  })
  const thirdFile = thirdMsg().locator(`code[data-path="${FILE}"]`)
  const inFlowNotices = thirdMsg().locator('[data-testid="md-chip-copy-error"], [role="alert"]')
  const bubbleNotice = h.page.locator('[role="tooltip"] [data-testid="md-chip-copy-error"]')
  const thirdParaBefore = await thirdMsg().locator('p').evaluateAll(ps => ps.map(p => p.innerHTML))
  const refusals = [
    { chip: sessionChip, name: 'session', modifiers: ['Control'], frame: 'session-copy-failed' },
    { chip: thirdFile, name: 'path', modifiers: ['Control'], frame: 'path-copy-failed' },
    { chip: brokenChip, name: 'broken image', modifiers: [], frame: 'broken-image-copy-failed' },
  ]
  for (const { chip, name, modifiers, frame } of refusals) {
    await chip.click({ modifiers })
    await h.page.waitForTimeout(300)
    assert(`${name} refused: exactly one "Copy failed" notice, inside a bubble (${await bubbleNotice.count()})`,
      (await bubbleNotice.count()) === 1 && /Copy failed/.test((await bubbleNotice.textContent()) ?? ''))
    assert(`${name} refused: one bubble in the document`, (await h.page.locator('[role="tooltip"]').count()) === 1)
    assert(`${name} refused: the notice is the accessible alert and the only one carrying the failure`,
      (await bubbleNotice.getAttribute('role')) === 'alert'
      && (await h.page.locator('[aria-hidden="true"] [data-testid="md-chip-copy-error"]').count()) === 0
      && (await h.page.locator('[role="alert"]').evaluateAll(els => els.filter(el => /Copy failed/.test(el.textContent ?? '')).length)) === 1)
    assert(`${name} refused: nothing entered the message flow (${await inFlowNotices.count()})`, (await inFlowNotices.count()) === 0)
    assert(`${name} refused: no dismiss control (the flash clears itself)`, (await bubbleNotice.locator('button').count()) === 0)
    assert(`${name} refused: the chip never claims "Copied!" (${await chip.getAttribute('title')})`,
      (await chip.getAttribute('title')) !== 'Copied!')
    // The bubble sits over the pressed chip: its left edge is the chip's first
    // line fragment's, as for the copy chip.
    const chipLeft = await chip.evaluate(el => Math.round((el.getClientRects()[0] ?? el.getBoundingClientRect()).left))
    const tipLeft = await h.page.locator('[role="tooltip"]').evaluate(el => Math.round(parseFloat(el.style.left)))
    assert(`${name} refused: the bubble opens at the pressed chip (${tipLeft} vs ${chipLeft})`, Math.abs(tipLeft - chipLeft) <= 8)
    await shotAround(thirdMsg(), frame)
  }
  const thirdParaAfter = await thirdMsg().locator('p').evaluateAll(ps => ps.map(p => p.innerHTML))
  assert('refused: every paragraph of the message is byte-identical to before the three refusals',
    JSON.stringify(thirdParaAfter) === JSON.stringify(thirdParaBefore))
  await h.page.waitForTimeout(3100)
  assert('refused: the last failure cleared itself and no bubble remains', (await h.page.locator('[role="tooltip"]').count()) === 0)

  await h.close()
  if (failures > 0) {
    console.error(`${failures} assertion(s) failed`)
    process.exit(1)
  }
}

await main()
