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
 *     "Couldn’t copy" notice — naming the recovery: select the text — in the SAME
 *     bubble (an ErrorNotice, announced once), claims nothing, and moves nothing
 *     in the paragraph; it too outlives the leave and clears itself.
 *  5. A long path chip still breaks across lines (measured: more than one
 *     client rect), and a copy on a long copy chip moves nothing on the line —
 *     the text after it has the same box before and after the confirmation.
 *     Its bubble opens BELOW the chip (it is past its message's first line), so
 *     the words leading up to the chip stay readable; a first-line chip's opens
 *     above, off the message. Every bubble stays inside the viewport.
 *  6. On the default theme, a session chip paints in the accent like the path
 *     chips (the copy chip beside it is neutral), and its Ctrl+click copy
 *     confirms in its title only once the write lands, without switching.
 *  7. With the clipboard refusing, the title-cued chips — session, path,
 *     broken image — each open the SAME bubble-borne failure notice the copy
 *     chip uses, at the pressed chip, one bubble at a time, and nothing
 *     enters the sentence around them. Each names the object that failed to
 *     copy ("Couldn’t copy the full session ID for <label>" / "the path" / "the image path"),
 *     and the two dual-action chips (session, path) name the gesture that
 *     asked for it — "(Ctrl+click)" — so the notice never teaches that a plain
 *     click copies; what they copy is not always the text shown, so the copy
 *     chip's "select the text" recovery is not offered.
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
        '',
        // Two more paragraphs: the NODE_ENV chip above sits with lines of its
        // own message under it (its bubble opens below, over those lines), and
        // `Ctrl+C` ends the message (its bubble opens above — below it would
        // sit on the timestamp and action row).
        'Then `npm run preview` serves that bundle locally on the port Vite prints, and the browser picks up the same fixtures the tests use, so what you see is exactly what the build produced.',
        '',
        'Stop it with `Ctrl+C` when you are done.',
      ].join('\n'),
    },
    { role: 'user', ts: now - 500, content: 'And the long ones?' },
    {
      role: 'assistant', ts: now - 30, content: [
        `The deepest one is \`${LONG_FILE}\` — it wraps like any other inline code.`,
        '',
        `The gateway reads \`${LONG_COPY}\` at boot, then falls back to defaults.`,
        '',
        'Override it with `KIROCREW_SETTINGS` when testing; the override wins over the file for that boot only, and the startup log names which one was read.',
        '',
        'Unset it afterwards with `unset KIROCREW_SETTINGS`.',
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
  /** Clip the frame to `locator`'s box, grown by `pad` above and `padBelow`
   *  under it so a bubble on either side stays in. The transcript is taller
   *  than the viewport, so bring the target fully into view first — a box
   *  partly above y=0 would clip whatever else sits there. */
  const shotAround = async (locator, name, pad = 48, padBelow = 56) => {
    await locator.scrollIntoViewIfNeeded()
    await h.page.waitForTimeout(200)
    const box = await locator.boundingBox()
    const clip = {
      x: Math.max(0, box.x - 16), y: Math.max(0, box.y - pad),
      width: Math.min(1180 - Math.max(0, box.x - 16), box.width + 32), height: box.height + pad + padBelow,
    }
    await h.page.screenshot({ path: `${OUT}/${name}.png`, clip })
    console.log('wrote', `${OUT}/${name}.png`)
  }
  /** The open bubble's side and geometry, next to the anchor's own rects. */
  const bubbleGeometry = async chip => {
    const tip = await h.page.locator('[role="tooltip"]').evaluate(el => {
      const r = el.getBoundingClientRect()
      return { placement: el.getAttribute('data-placement'), top: Math.round(parseFloat(el.style.top)), left: Math.round(parseFloat(el.style.left)), right: Math.round(r.right), bottom: Math.round(r.bottom), y: Math.round(r.top) }
    })
    const anchor = await chip.evaluate(el => {
      const rects = [...el.getClientRects()]
      const first = rects[0] ?? el.getBoundingClientRect()
      const box = el.getBoundingClientRect()
      return { firstLeft: Math.round(first.left), firstTop: Math.round(first.top), boxLeft: Math.round(box.left), boxBottom: Math.round(box.bottom), lines: rects.length }
    })
    return { tip, anchor }
  }
  /** The box of everything a message renders UNDER its content — the turn stats
   *  and the timestamp/action row — as one union rect, or null when nothing is
   *  there. What a bubble below a last-line chip used to cover. */
  const footerBox = async msgIndex => h.page.locator('[data-role="assistant"]').nth(msgIndex).evaluate(root => {
    const content = root.querySelector('.msg-content')
    let box = null
    for (let el = content?.nextElementSibling; el; el = el.nextElementSibling) {
      const r = el.getBoundingClientRect()
      if (r.width === 0 || r.height === 0) continue
      box = box ? { top: Math.min(box.top, r.top), bottom: Math.max(box.bottom, r.bottom), left: Math.min(box.left, r.left), right: Math.max(box.right, r.right) } : { top: r.top, bottom: r.bottom, left: r.left, right: r.right }
    }
    return box && { top: Math.round(box.top), bottom: Math.round(box.bottom), left: Math.round(box.left), right: Math.round(box.right) }
  })
  const overlaps = (tip, box) => !!box && tip.y < box.bottom && tip.bottom > box.top && tip.left < box.right && tip.right > box.left

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
  // The npm test chip sits on its message's FIRST line: above is off the
  // message, so the bubble opens there (the flow rule's only "above").
  const hoverGeo = await bubbleGeometry(copyChip())
  assert(`hover: a first-line chip opens its bubble above, at its first fragment (${JSON.stringify(hoverGeo.tip)} vs ${JSON.stringify(hoverGeo.anchor)})`,
    hoverGeo.tip.placement === 'above' && hoverGeo.tip.top === hoverGeo.anchor.firstTop - 8 && Math.abs(hoverGeo.tip.left - hoverGeo.anchor.firstLeft) <= 8)
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
  assert(`refused: exactly one "Couldn’t copy" notice renders, inside the bubble (${await failNotice.count()})`, (await failNotice.count()) === 1)
  const failText = (await failNotice.textContent()) ?? ''
  assert(`refused: the notice names the recovery, verbatim (${JSON.stringify(failText.trim())})`,
    failText.includes('Couldn’t copy — select the text to copy it manually'))
  // The notice is wider than the hint it replaced: the bubble must have been
  // re-measured against the viewport edge for it, so it ends on-screen.
  const failGeo = await bubbleGeometry(envChip)
  const viewportWidth = await h.page.evaluate(() => window.innerWidth)
  assert(`refused: the wider notice's bubble stays inside the viewport (right ${failGeo.tip.right} <= ${viewportWidth - 8}, left ${failGeo.tip.left} >= 8)`,
    failGeo.tip.right <= viewportWidth - 8 && failGeo.tip.left >= 8)
  // The chip sits between its message's first and last lines: the bubble opens
  // below it, over the message's own next line — never on the timestamp and
  // action row under the message.
  const envFooter = await footerBox(0)
  assert(`refused: the bubble opens below a between-line chip and stays off the message's footer row (${JSON.stringify(failGeo.tip)} vs footer ${JSON.stringify(envFooter)})`,
    failGeo.tip.placement === 'below' && !!envFooter && !overlaps(failGeo.tip, envFooter))
  // Announced once, through the notice itself: it is the bubble's accessible
  // `role="alert"` (no aria-hidden ancestor) and the only alert that carries
  // the failure — no sr-only copy of it anywhere.
  const bubbleAlerts = await h.page.locator('[role="tooltip"] [role="alert"]').count()
  const hiddenNotice = await h.page.locator('[aria-hidden="true"] [data-testid="md-chip-copy-error"]').count()
  const alertTexts = await h.page.locator('[role="alert"]').evaluateAll(els => els.map(el => (el.textContent ?? '').trim().slice(0, 60)))
  const failAlerts = alertTexts.filter(t => /Couldn’t copy/.test(t)).length
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
  // The wrapped chip is past its message's first line, so the bubble opens
  // BELOW it — under the box's bottom-left, where the chip ends — instead of
  // above its first fragment, where it covered the sentence before it.
  const wrapGeo = await bubbleGeometry(longCopy)
  assert(`wrap: a lower chip opens its bubble below its last line (${JSON.stringify(wrapGeo.tip)} vs ${JSON.stringify(wrapGeo.anchor)})`,
    wrapGeo.tip.placement === 'below' && wrapGeo.tip.top === wrapGeo.anchor.boxBottom + 8 && wrapGeo.tip.left === wrapGeo.anchor.boxLeft && wrapGeo.tip.y >= wrapGeo.anchor.boxBottom)
  // What the UX read flagged: the bubble used to hide the words before the
  // chip. Those words are the paragraph's opening text; its line must end above
  // where the bubble now starts.
  const openingBottom = await longCopy.evaluate(el => {
    const p = el.closest('p')
    const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT)
    const first = walker.nextNode()
    const range = document.createRange()
    range.selectNodeContents(first)
    return Math.round(range.getClientRects()[0].bottom)
  })
  assert(`wrap: the sentence's opening stays uncovered (line bottom ${openingBottom} <= bubble top ${wrapGeo.tip.y})`, openingBottom <= wrapGeo.tip.y)
  const viewportHeight = await h.page.evaluate(() => window.innerHeight)
  assert(`wrap: the bubble below fits inside the viewport (bottom ${wrapGeo.tip.bottom} <= ${viewportHeight - 8})`, wrapGeo.tip.bottom <= viewportHeight - 8)
  await shotAround(lastMsg(), 'long-path-wraps')

  // --- The message's LAST line: below it the bubble would leave the message
  // and sit on the timestamp and action row, which a reader took for
  // unreachable; the bubble opens above instead, over a line already read. ---
  await h.page.mouse.move(5, 5)
  await h.page.waitForTimeout(1600)
  const lastLineChip = h.page.locator('code[aria-label="Copy unset KIROCREW_SETTINGS"]')
  await lastLineChip.hover()
  await h.page.waitForTimeout(200)
  const lastGeo = await bubbleGeometry(lastLineChip)
  const lastFooter = await footerBox(1)
  assert(`last line: the chip opens its bubble ABOVE, at its first fragment (${JSON.stringify(lastGeo.tip)} vs ${JSON.stringify(lastGeo.anchor)})`,
    lastGeo.tip.placement === 'above' && lastGeo.tip.top === lastGeo.anchor.firstTop - 8 && lastGeo.tip.bottom <= lastGeo.anchor.firstTop)
  assert(`last line: the bubble stays off the message's footer row (${JSON.stringify(lastGeo.tip)} vs footer ${JSON.stringify(lastFooter)})`,
    !!lastFooter && !overlaps(lastGeo.tip, lastFooter))
  await shotAround(lastMsg(), 'last-line-chip-above')
  await h.page.mouse.move(5, 5)
  await h.page.waitForTimeout(300)

  // --- The pane's last visible line: a between-line chip whose bubble would
  // run off the bottom of the viewport takes its `flip` and opens above. The
  // composer is hidden for this scene so the transcript reaches the pane's
  // bottom edge (a maximised transcript pane does the same) and the viewport
  // is shortened to end just under the chip. ---
  // The composer block is the composer's nearest ancestor that does not hold
  // the transcript; hiding it lets the transcript pane reach the viewport's
  // bottom edge (the shape of a maximised transcript pane).
  const setComposerHidden = hidden => h.page.evaluate(hide => {
    const composer = document.querySelector('textarea[data-composer-input]')
    const transcript = document.querySelector('[data-role="assistant"]')
    let node = composer
    while (node && node.parentElement && !node.parentElement.contains(transcript)) node = node.parentElement
    if (node) node.style.display = hide ? 'none' : ''
  }, hidden)
  await setComposerHidden(true)
  const envBox = await envChip.evaluate(el => { const r = el.getBoundingClientRect(); return { top: Math.round(r.top), bottom: Math.round(r.bottom) } })
  const shortHeight = envBox.bottom + 24
  await h.page.setViewportSize({ width: 1180, height: shortHeight })
  await h.page.waitForTimeout(400)
  // The transcript pins to its newest message on a resize; bring the chip back
  // to the pane's bottom edge, where a bubble below it would not fit.
  await envChip.evaluate(el => el.scrollIntoView({ block: 'end' }))
  await h.page.waitForTimeout(400)
  const paneEdge = await envChip.evaluate(el => ({ chipTop: Math.round(el.getBoundingClientRect().top), chipBottom: Math.round(el.getBoundingClientRect().bottom), innerHeight: window.innerHeight }))
  assert(`pane bottom: the chip sits at the pane's last visible line (chip bottom ${paneEdge.chipBottom} within 48px of ${paneEdge.innerHeight})`,
    paneEdge.innerHeight - paneEdge.chipBottom <= 48 && paneEdge.chipBottom <= paneEdge.innerHeight)
  await envChip.hover()
  await h.page.waitForTimeout(200)
  const flipGeo = await bubbleGeometry(envChip)
  assert(`pane bottom: below does not fit, so the between-line chip's bubble flips ABOVE its first fragment (${JSON.stringify(flipGeo.tip)} vs ${JSON.stringify(flipGeo.anchor)})`,
    flipGeo.tip.placement === 'above' && flipGeo.tip.top === flipGeo.anchor.firstTop - 8 && flipGeo.tip.bottom <= flipGeo.anchor.firstTop && flipGeo.tip.bottom <= paneEdge.innerHeight - 8)
  {
    const clipTop = Math.max(0, paneEdge.chipTop - 64)
    await h.page.screenshot({ path: `${OUT}/flip-at-pane-bottom.png`, clip: { x: 0, y: clipTop, width: 1180, height: shortHeight - clipTop } })
    console.log('wrote', `${OUT}/flip-at-pane-bottom.png`)
  }
  await h.page.mouse.move(5, 5)
  await h.page.waitForTimeout(300)
  await h.page.setViewportSize({ width: 1180, height: 1500 })
  await setComposerHidden(false)
  await h.page.evaluate(() => window.scrollTo(0, 0))
  await h.page.waitForTimeout(300)

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
  // bubble-borne failure notice the copy chip uses, at the pressed chip,
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
    { chip: sessionChip, name: 'session', modifiers: ['Control'], frame: 'session-copy-failed', notice: `Couldn’t copy the full session ID for ${OTHER_SLOT} (Ctrl+click)`, side: 'above' },
    { chip: thirdFile, name: 'path', modifiers: ['Control'], frame: 'path-copy-failed', notice: `Couldn’t copy the path ${FILE.split('/').pop()} (Ctrl+click)`, side: 'above' },
    // The broken-image chip is the message's LAST line: below it the bubble
    // would sit on the timestamp and action row, so it opens above.
    { chip: brokenChip, name: 'broken image', modifiers: [], frame: 'broken-image-copy-failed', notice: 'Couldn’t copy the image path', side: 'above' },
  ]
  for (const { chip, name, modifiers, frame, notice, side } of refusals) {
    await chip.click({ modifiers })
    await h.page.waitForTimeout(300)
    // Each names what failed to copy, in the reader's terms: these chips copy
    // something their label need not show (the session's normalised key, the
    // broken image's path), so "select the text" — the copy chip's recovery —
    // would point at the wrong thing and is not offered here; the path chip
    // names the file's tail, so a bubble the clamp has pulled left still says
    // which chip it answers.
    const noticeText = ((await bubbleNotice.textContent()) ?? '').trim()
    assert(`${name} refused: exactly one notice naming what failed to copy, inside a bubble (${await bubbleNotice.count()}: "${noticeText}")`,
      (await bubbleNotice.count()) === 1 && noticeText === notice)
    assert(`${name} refused: one bubble in the document`, (await h.page.locator('[role="tooltip"]').count()) === 1)
    assert(`${name} refused: the notice is the accessible alert and the only one carrying the failure`,
      (await bubbleNotice.getAttribute('role')) === 'alert'
      && (await h.page.locator('[aria-hidden="true"] [data-testid="md-chip-copy-error"]').count()) === 0
      && (await h.page.locator('[role="alert"]').evaluateAll((els, text) => els.filter(el => (el.textContent ?? '').includes(text)).length, notice)) === 1)
    assert(`${name} refused: nothing entered the message flow (${await inFlowNotices.count()})`, (await inFlowNotices.count()) === 0)
    assert(`${name} refused: no dismiss control (the flash clears itself)`, (await bubbleNotice.locator('button').count()) === 0)
    assert(`${name} refused: the chip never claims "Copied!" (${await chip.getAttribute('title')})`,
      (await chip.getAttribute('title')) !== 'Copied!')
    // The bubble sits at the pressed chip, on the side the flow rule picks:
    // above its first line fragment when the chip is on the message's first or
    // last line, else below its box (bottom-left, where the chip ends). A chip
    // near the right edge has its bubble pulled left by the viewport clamp
    // instead, ending 8px inside the edge — that is the clamp doing its job —
    // and the bubble still overlaps the chip it answers.
    const geo = await bubbleGeometry(chip)
    assert(`${name} refused: the flow rule opens the bubble ${side} (${geo.tip.placement})`, geo.tip.placement === side)
    const wantLeft = geo.tip.placement === 'below' ? geo.anchor.boxLeft : geo.anchor.firstLeft
    const wantTop = geo.tip.placement === 'below' ? geo.anchor.boxBottom + 8 : geo.anchor.firstTop - 8
    const clamped = geo.tip.left < wantLeft && geo.tip.right >= viewportWidth - 9
    const atChip = geo.tip.top === wantTop && (Math.abs(geo.tip.left - wantLeft) <= 8 || clamped)
    assert(`${name} refused: the bubble opens at the pressed chip, ${geo.tip.placement} it${clamped ? ', clamped to the viewport edge' : ''} (${JSON.stringify(geo.tip)} vs ${JSON.stringify(geo.anchor)})`, atChip)
    assert(`${name} refused: the bubble stays inside the viewport (right ${geo.tip.right} <= ${viewportWidth - 8})`, geo.tip.right <= viewportWidth - 8 && geo.tip.left >= 8)
    if (clamped) {
      assert(`${name} refused: the clamped bubble still overlaps its chip horizontally (bubble ${geo.tip.left}..${geo.tip.right} vs chip from ${geo.anchor.firstLeft})`, geo.tip.right > geo.anchor.firstLeft)
    }
    const footer = await footerBox(2)
    assert(`${name} refused: the bubble stays off the message's footer row (${JSON.stringify(geo.tip)} vs footer ${JSON.stringify(footer)})`, !!footer && !overlaps(geo.tip, footer))
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
