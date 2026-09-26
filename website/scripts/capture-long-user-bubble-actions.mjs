/**
 * Screenshot harness for the action strip under a pinned long user prompt.
 *
 * A user prompt taller than the viewport hands over to the pinned-prompt card
 * the moment its row top crosses the fold, and its row is hidden for as long as
 * the card stands in for it. The row also carries the message's action strip
 * (copy, copy link, pin, timestamp), so before the fix that strip was never on
 * screen for a tall prompt: its bottom only comes into view once its top is
 * above the fold, i.e. once the whole row is hidden. This scene drives the REAL
 * transcript (built SPA, `/api/**` fixtures) into exactly that state and reads
 * the strip's fate off the live DOM, not the pixels alone:
 *
 *   - `expected=after`  (fixed build): the strip is visible and hit-testable
 *     beneath the card, the card's bottom sits at or above the strip, the pin
 *     control is there (the fixture messages carry `meta.mid`, which is what
 *     makes a message pinnable), the Edit pencil is dropped while the row is the
 *     stand-in and present once it is not, and a click on Copy lands (the button
 *     flips to its copied state).
 *   - `expected=before` (unfixed build): the strip is `visibility: hidden` with
 *     the row — the defect, recorded so the pair is evidence of a change.
 *
 * Usage, from website/ after `npm run build`:
 *   node scripts/capture-long-user-bubble-actions.mjs [outDir] [--dist DIR] [--expected before|after]
 *   node scripts/capture-long-user-bubble-actions.mjs [outDir] --record   # one webm, both scroll directions
 *
 * `--dist` points a run at another build of the same page (the before/after
 * pair is two runs, two dists, one script).
 */
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

const { openTranscriptHarness } = await import('./lib/transcript-harness.mjs')

const argv = process.argv.slice(2)
const flag = (name, fallback) => {
  const i = argv.indexOf(name)
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback
}
const OUT = argv.find((a, i) => !a.startsWith('--') && (i === 0 || !argv[i - 1].startsWith('--')))
  || '../temp-screenshots/long-user-bubble-actions'
const DIST = flag('--dist', undefined)
const EXPECTED = flag('--expected', 'after')
if (!['before', 'after'].includes(EXPECTED)) {
  console.error(`FAIL: --expected must be before|after, got ${JSON.stringify(EXPECTED)}`)
  process.exit(2)
}
const SLOT = 'chat-longbubble'
// Derived from this script's own location (scripts/ -> website/ -> repo root),
// never hardcoded: this path RENDERS into the captured screenshot, so a personal
// absolute path both leaks a home directory and misrepresents any other checkout.
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
// Far taller than any viewport: 70 lines at ~23px each is ~1600px of bubble.
const LONG_PROMPT = [
  'Please review the deployment plan below before I hand it to the team.',
  ...Array.from({ length: 68 }, (_, i) => `Step ${i + 1}: check the ${['cache', 'queue', 'index', 'replica', 'gateway'][i % 5]} rollout order, confirm the health check window, and record who signs off.`),
  'That is the whole plan. Tell me what is missing.',
].join('\n')
const REPLY = [
  'Read the plan end to end. Two gaps: the queue drain in step 12 has no owner, and the replica',
  'cutover in step 40 lands inside the health check window it is supposed to wait for.',
  '',
  'Everything else is ordered correctly. Steps 1-11 can run in parallel; the rest is strictly serial.',
].join('\n')
// A prompt short enough to sit whole on screen unpinned, tall enough to fold.
const MEDIUM_PROMPT = [
  'Three constraints before you touch the plan:',
  'Constraint A: the cache and queue steps must never run in the same window.',
  'Constraint B: every replica cutover needs a named approver on the call.',
  'Constraint C: the gateway steps are frozen until the index rebuild reports green.',
  'Fold those in and send me the revised order.',
].join('\n')
const REPLY_2 = [
  'Folded in. Constraint A splits steps 1-11 into two windows, cache first and queue second.',
  'Constraint B adds an approver line to steps 4, 9, 14 and every later replica step.',
  'Constraint C moves the four gateway steps behind step 43, where the index rebuild reports.',
  '',
  'The revised order is below, with the changed steps marked.',
  // Tall enough that the prompt above it can scroll all the way up to the fold:
  // the transcript's scroll range ends at this reply's bottom.
  ...Array.from({ length: 44 }, (_, i) => `Revised ${i + 1}: unchanged from the original order.`),
].join('\n')
// A steer confirmed into the running turn: badge above an accent bubble, which is
// the row shape whose stand-in height is measured from the row top.
const STEER_PROMPT = [
  'Steering while you work: skip the replica steps entirely for this pass.',
  'The replica team is doing their own cutover tomorrow, so leave 4, 9, 14 and the later ones out.',
  'Everything else stands.',
].join('\n')
const REPLY_3 = [
  'Understood, the replica steps are out of this pass.',
  ...Array.from({ length: 40 }, (_, i) => `Pass ${i + 1}: no replica work, order otherwise unchanged.`),
].join('\n')

const slots = [{
  key: SLOT, title: 'Deployment plan review', running: false,
  last_message: 'The revised order is below.', messages: 6, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
}]
// `meta.mid` is what makes a message pinnable in ChatPage (the pin button is
// gated on it), so every row carries one: the report names the pin icon.
const detail = {
  running: false, has_more: false, total: 6, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 3000, content: 'Morning. I have a long one coming.', meta: { mid: 'm-1' } },
    { role: 'assistant', ts: now - 2950, content: 'Go ahead, paste it in full.', meta: { mid: 'm-2' } },
    { role: 'user', ts: now - 900, content: LONG_PROMPT, meta: { mid: 'm-3' } },
    { role: 'assistant', ts: now - 600, content: REPLY, meta: { mid: 'm-4' } },
    { role: 'user', ts: now - 300, content: MEDIUM_PROMPT, meta: { mid: 'm-5' } },
    { role: 'assistant', ts: now - 200, content: REPLY_2, meta: { mid: 'm-6' } },
    // `steer: true` + `steerState: 'consumed'` is the backend-confirmed injection
    // UserMessage draws with the badge and the accent bubble.
    { role: 'user', ts: now - 120, content: STEER_PROMPT, meta: { mid: 'm-7', steer: true, steerState: 'consumed' } },
    { role: 'assistant', ts: now - 30, content: REPLY_3, meta: { mid: 'm-8' } },
  ],
}

let failures = 0
const assert = (label, ok) => {
  console.log(`${ok ? 'PASS' : 'FAIL'}: ${label}`)
  if (!ok) failures += 1
}

/** Geometry + visibility of one prompt's row, bubble, strip and the card. */
async function inspect(page, marker) {
  return page.evaluate((needle) => {
    const rows = [...document.querySelectorAll('[data-display-index]')]
    const row = rows.find(r => r.textContent.includes(needle))
    if (!row) return { error: `row containing ${JSON.stringify(needle)} not mounted` }
    const bubble = row.querySelector('.message-bubble')
    const copy = row.querySelector('button[title="Copy"]')
    const pin = row.querySelector('button[title="Pin message"], button[title="Unpin message"]')
    const edit = row.querySelector('[data-message-edit]') || row.querySelector('button[title="Edit & resend"]')
    const strip = copy ? copy.parentElement : null
    const card = document.querySelector('[data-testid="pinned-prompt"]')
    const scroller = document.querySelector('.chat-container')
    const r = el => { const b = el.getBoundingClientRect(); return { top: b.top, bottom: b.bottom, left: b.left, right: b.right, height: b.height } }
    const mid = el => { const b = el.getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2] }
    let copyHit = null
    if (copy) {
      const [x, y] = mid(copy)
      const hit = document.elementFromPoint(x, y)
      copyHit = hit ? (hit === copy || copy.contains(hit)) : false
    }
    return {
      rowHidden: row.style.visibility === 'hidden',
      rowStandin: row.hasAttribute('data-pinned-standin'),
      rowStandinValue: row.getAttribute('data-pinned-standin'),
      row: r(row),
      bubble: bubble ? r(bubble) : null,
      strip: strip ? { ...r(strip), visibility: getComputedStyle(strip).visibility, opacity: getComputedStyle(strip).opacity } : null,
      copy: copy ? { ...r(copy), visibility: getComputedStyle(copy).visibility, hit: copyHit, label: copy.getAttribute('aria-label') } : null,
      pin: pin ? { visibility: getComputedStyle(pin).visibility, label: pin.getAttribute('aria-label') } : null,
      edit: edit ? { display: getComputedStyle(edit).display, visibility: getComputedStyle(edit).visibility } : null,
      card: card ? { ...r(card), text: (card.textContent || '').slice(0, 40) } : null,
      viewport: { w: innerWidth, h: innerHeight },
      scrollTop: scroller ? scroller.scrollTop : null,
    }
  }, marker)
}

/** Scroll so the marked prompt's bubble BOTTOM sits at `frac` of the viewport. */
async function placeBubbleBottom(page, marker, frac) {
  await page.evaluate(([needle, f]) => {
    const rows = [...document.querySelectorAll('[data-display-index]')]
    const row = rows.find(r => r.textContent.includes(needle))
    const bubble = row.querySelector('.message-bubble')
    const scroller = document.querySelector('.chat-container')
    const target = scroller.getBoundingClientRect().top + scroller.clientHeight * f
    scroller.scrollTop += bubble.getBoundingClientRect().bottom - target
  }, [marker, frac])
  await page.waitForTimeout(500)
}

/** Scroll so the marked prompt's row TOP sits at `frac` of the viewport. */
async function placeRowTop(page, marker, frac) {
  await page.evaluate(([needle, f]) => {
    const rows = [...document.querySelectorAll('[data-display-index]')]
    const row = rows.find(r => r.textContent.includes(needle))
    const scroller = document.querySelector('.chat-container')
    const target = scroller.getBoundingClientRect().top + scroller.clientHeight * f
    scroller.scrollTop += row.getBoundingClientRect().top - target
  }, [marker, frac])
  await page.waitForTimeout(500)
}

/** Scroll so the marked prompt's row TOP sits `px` below the pinned card's fold
 *  line (negative = above it): the fold is where a prompt hands over, so a small
 *  negative value puts a short prompt into its folding stand-in state with its
 *  bottom, and the strip beneath it, still on screen. */
async function placeRowTopFromFold(page, marker, px) {
  await page.evaluate(([needle, dy]) => {
    const rows = [...document.querySelectorAll('[data-display-index]')]
    const row = rows.find(r => r.textContent.includes(needle))
    const scroller = document.querySelector('.chat-container')
    // The card sits ROW_PAD_Y (4px) under the fold; with no card mounted fall
    // back to the scroller's own top edge, which is where a frameless host folds.
    const card = document.querySelector('[data-testid="pinned-prompt"]')
    const fold = card ? card.getBoundingClientRect().top - 4 : scroller.getBoundingClientRect().top
    scroller.scrollTop += row.getBoundingClientRect().top - (fold + dy)
  }, [marker, px])
  await page.waitForTimeout(500)
}

/** Scroll so the marked prompt's bubble BOTTOM sits `px` below the fold line —
 *  small values leave the card at its resting clamp with this prompt still the
 *  pinned one (the next prompt has not reached the fold yet). */
async function placeBubbleBottomFromFold(page, marker, px) {
  await page.evaluate(([needle, dy]) => {
    const rows = [...document.querySelectorAll('[data-display-index]')]
    const row = rows.find(r => r.textContent.includes(needle))
    const bubble = row.querySelector('.message-bubble')
    const scroller = document.querySelector('.chat-container')
    const card = document.querySelector('[data-testid="pinned-prompt"]')
    const fold = card ? card.getBoundingClientRect().top - 4 : scroller.getBoundingClientRect().top
    scroller.scrollTop += bubble.getBoundingClientRect().bottom - (fold + dy)
  }, [marker, px])
  await page.waitForTimeout(500)
}

const LONG = 'Step 68:'
const MEDIUM = 'Constraint C:'
const STEER = 'skip the replica steps entirely'

/** Scroll the transcript by `total` px in small steps, so a recording shows the
 *  hand-off, the fold and the strip's appearance as continuous motion. */
async function scrollBy(page, total, step = 14, pauseMs = 28) {
  const dir = Math.sign(total)
  let left = Math.abs(total)
  while (left > 0) {
    const d = Math.min(step, left) * dir
    await page.evaluate((dy) => { document.querySelector('.chat-container').scrollTop += dy }, d)
    await page.waitForTimeout(pauseMs)
    left -= Math.abs(d)
  }
}

/**
 * Recording: the states a still cannot carry. One long prompt scrolled from
 * unpinned through hand-off and the fold to the card's rest (the strip appears
 * under the folding card, then re-hides at rest), then back up; then the
 * five-line prompt hovered unpinned (pencil present), scrolled into its stand-in
 * state (pencil gone, the rest of the strip in place) and back. Both directions,
 * dark theme, at the viewport size so the seam stays legible.
 */
async function record() {
  const { mkdirSync: mk } = await import('node:fs')
  const videoDir = `${OUT}/video-${process.pid}`
  mk(videoDir, { recursive: true })
  const h = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots, detail,
    viewport: { width: 1280, height: 860 }, deviceScaleFactor: 1,
    recordVideo: { dir: videoDir, size: { width: 1280, height: 860 } },
    dist: DIST,
  })
  await h.load('dark', { selector: 'textarea[data-composer-input]', settle: 1200 })
  // Start with the long prompt's top 260px under the fold: unpinned, its bubble
  // filling the viewport, its bottom (and strip) below the screen.
  await placeRowTopFromFold(h.page, LONG, 260)
  await placeRowTopFromFold(h.page, LONG, 260)
  await h.page.waitForTimeout(1200)
  const g0 = await inspect(h.page, LONG)
  // Down: through the hand-off (row top crosses the fold), the fold (card bottom
  // tracks the bubble bottom, strip visible beneath it), to rest (card at its
  // clamp, strip hidden again). Total travel: row top from fold+260 to the point
  // where the bubble bottom sits 40px under the fold.
  const travel = Math.round(260 + g0.bubble.height + ROW_PAD - 40)
  await scrollBy(h.page, travel)
  await h.page.waitForTimeout(1400)
  const rest = await inspect(h.page, LONG)
  console.log(`record: at rest marker=${JSON.stringify(rest.rowStandinValue)} copy.visibility=${rest.copy?.visibility} card.h=${Math.round(rest.card?.height ?? 0)}`)
  // Back up, the same way.
  await scrollBy(h.page, -travel)
  await h.page.waitForTimeout(1200)
  // The Edit pencil: unpinned and hovered, then pinned, then back.
  await placeRowTopFromFold(h.page, MEDIUM, 200)
  await placeRowTopFromFold(h.page, MEDIUM, 200)
  await h.page.locator('[data-display-index]').filter({ hasText: MEDIUM }).locator('.message-bubble').hover()
  await h.page.waitForTimeout(1400)
  await scrollBy(h.page, 240)
  await h.page.waitForTimeout(1400)
  const pinnedMedium = await inspect(h.page, MEDIUM)
  console.log(`record: medium pinned marker=${JSON.stringify(pinnedMedium.rowStandinValue)} edit.display=${pinnedMedium.edit?.display}`)
  await scrollBy(h.page, -240)
  await h.page.locator('[data-display-index]').filter({ hasText: MEDIUM }).locator('.message-bubble').hover()
  await h.page.waitForTimeout(1400)
  const path = await h.close()
  const { renameSync, rmSync } = await import('node:fs')
  const out = `${OUT}/pin-handoff-both-directions.webm`
  renameSync(path, out)
  rmSync(videoDir, { recursive: true, force: true })
  console.log('wrote', out)
}
const ROW_PAD = 4

async function main() {
  const h = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots, detail,
    viewport: { width: 1280, height: 860 },
    dist: DIST,
  })
  const shot = async (name, clip) => {
    const path = `${OUT}/${name}.png`
    await h.page.screenshot({ path, ...(clip ? { clip } : {}) })
    console.log('wrote', path)
  }
  const rowLocator = marker => h.page.locator('[data-display-index]').filter({ hasText: marker })

  for (const theme of ['dark', 'light']) {
    await h.load(theme, { selector: 'textarea[data-composer-input]', settle: 1200 })
    // The transcript boots at its bottom. Bring the long prompt's bottom to
    // mid-viewport: its top is then far above the fold, so the row is the pinned
    // stand-in and its strip sits under the folding card.
    await placeBubbleBottom(h.page, LONG, 0.5)
    await placeBubbleBottom(h.page, LONG, 0.5) // second pass: the fold re-measured heights after the first
    const g = await inspect(h.page, LONG)
    if (g.error) { assert(g.error, false); break }
    console.log(`${theme}: ${JSON.stringify(g)}`)
    assert(`${theme}: long prompt row is the pinned stand-in (hidden by visibility)`, g.rowHidden)
    assert(`${theme}: pinned card is mounted`, !!g.card)
    assert(`${theme}: bubble bottom is on screen (${Math.round(g.bubble?.bottom ?? -1)}px of ${g.viewport.h})`,
      !!g.bubble && g.bubble.bottom > 0 && g.bubble.bottom < g.viewport.h)
    assert(`${theme}: the action strip renders inside the row, with a pin control`, !!g.strip && !!g.copy && !!g.pin)
    if (EXPECTED === 'after') {
      assert(`${theme}: row carries data-pinned-standin="folding" while the card folds (value ${JSON.stringify(g.rowStandinValue)})`, g.rowStandinValue === 'folding')
      assert(`${theme}: strip is visible (computed ${g.strip?.visibility}, opacity ${g.strip?.opacity})`,
        g.strip?.visibility === 'visible' && g.strip?.opacity === '1')
      assert(`${theme}: pin control is visible (computed ${g.pin?.visibility})`, g.pin?.visibility === 'visible')
      assert(`${theme}: Edit is dropped while standing in (computed display ${g.edit?.display})`, g.edit?.display === 'none')
      assert(`${theme}: card bottom (${Math.round(g.card?.bottom ?? 0)}) does not cover the strip top (${Math.round(g.strip?.top ?? 0)})`,
        !!g.card && !!g.strip && g.card.bottom <= g.strip.top + 0.5)
      assert(`${theme}: Copy is the element under its own centre (hit-testable)`, g.copy?.hit === true)
    } else {
      assert(`${theme}: [defect] strip is hidden with the row (computed ${g.strip?.visibility})`, g.strip?.visibility === 'hidden')
      assert(`${theme}: [defect] Copy is not hit-testable`, g.copy?.hit === false)
    }
    await shot(`${EXPECTED}-01-pinned-long-prompt-${theme}`)
    // Zoom on the seam: the card's bottom edge and the strip beneath it.
    const seamTop = Math.max(0, Math.round((g.bubble?.bottom ?? 300) - 220))
    await shot(`${EXPECTED}-02-strip-under-card-${theme}`, { x: 0, y: seamTop, width: g.viewport.w, height: 320 })
    if (EXPECTED === 'after' && theme === 'dark') {
      // The click must land on the row's button, not on the card overlay. Either
      // outcome label proves the click reached it; the clipboard grant is what
      // lets the successful one show.
      await h.page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: h.base })
      await rowLocator(LONG).locator('button[title="Copy"]').click({ timeout: 5000 })
      await h.page.waitForTimeout(250)
      const after = await inspect(h.page, LONG)
      assert(`dark: Copy click landed (label now "${after.copy?.label}")`, /copied|copy failed/i.test(after.copy?.label || ''))
      await shot(`${EXPECTED}-03-copy-clicked-${theme}`, { x: 0, y: seamTop, width: g.viewport.w, height: 320 })
      // Card at its clamp, strip still uncovered: scroll on until the long prompt's
      // bubble bottom sits 40px under the fold. The card has reached its 48px rest,
      // but the 28px strip (4px under the bubble) still pokes out below the card's
      // bottom, so it must stay shown — hiding it here is the abrupt vanish and
      // blank band the occlusion rule exists to remove.
      await placeBubbleBottomFromFold(h.page, LONG, 40)
      await placeBubbleBottomFromFold(h.page, LONG, 40)
      const clamp = await inspect(h.page, LONG)
      console.log(`dark at clamp, strip uncovered: ${JSON.stringify({ rowHidden: clamp.rowHidden, marker: clamp.rowStandinValue, copy: clamp.copy, card: clamp.card })}`)
      assert(`dark: card is at its resting clamp (${Math.round(clamp.card?.height ?? 0)}px)`, !!clamp.card && clamp.card.height < 80)
      assert(`dark: strip still uncovered keeps the marker (value ${JSON.stringify(clamp.rowStandinValue)})`, clamp.rowStandinValue === 'folding')
      assert(`dark: strip still uncovered stays visible (computed ${clamp.copy?.visibility})`, clamp.copy?.visibility === 'visible')
      assert(`dark: strip bottom (${Math.round(clamp.copy?.bottom ?? 0)}) is below the card bottom (${Math.round(clamp.card?.bottom ?? 0)})`, (clamp.copy?.bottom ?? 0) > (clamp.card?.bottom ?? 0))
      await shot(`${EXPECTED}-08-card-at-clamp-strip-uncovered-${theme}`, { x: 0, y: Math.max(0, Math.round((clamp.card?.top ?? 60) - 30)), width: clamp.viewport.w, height: 200 })
      // Fully at rest: the strip's bottom has passed the card's resting bottom.
      // A visible strip there is one nobody can see but Tab still stops on, so it
      // must go back to hidden.
      await placeBubbleBottomFromFold(h.page, LONG, 10)
      await placeBubbleBottomFromFold(h.page, LONG, 10)
      const rest = await inspect(h.page, LONG)
      console.log(`dark at rest: ${JSON.stringify({ rowHidden: rest.rowHidden, marker: rest.rowStandinValue, copy: rest.copy, card: rest.card })}`)
      assert('dark: long prompt row is still the stand-in at rest', rest.rowHidden === true)
      assert(`dark: marker is bare at rest (value ${JSON.stringify(rest.rowStandinValue)})`, rest.rowStandinValue === '')
      assert(`dark: strip is hidden again at rest (computed ${rest.copy?.visibility})`, rest.copy?.visibility === 'hidden')
      assert(`dark: card is at its resting clamp (${Math.round(rest.card?.height ?? 0)}px)`, !!rest.card && rest.card.height < 80)
    }

    if (EXPECTED === 'after' && theme === 'dark') {
      // The Edit pencil pair. Unpinned first: the five-line prompt whole on
      // screen, hovered so its hover-revealed strip shows — pencil included.
      await placeRowTop(h.page, MEDIUM, 0.3)
      await rowLocator(MEDIUM).locator('.message-bubble').hover()
      await h.page.waitForTimeout(700)
      const un = await inspect(h.page, MEDIUM)
      console.log(`dark unpinned medium: ${JSON.stringify({ rowHidden: un.rowHidden, edit: un.edit, pin: un.pin, row: un.row })}`)
      assert('dark: unpinned prompt row is not hidden', un.rowHidden === false && un.rowStandin === false)
      assert(`dark: unpinned strip shows Edit (computed display ${un.edit?.display})`, !!un.edit && un.edit.display !== 'none')
      await shot(`${EXPECTED}-04-unpinned-strip-with-edit-${theme}`, { x: 0, y: Math.max(0, Math.round(un.row.top - 40)), width: un.viewport.w, height: Math.min(un.viewport.h, Math.round(un.row.height + 80)) })
      // Then pinned: its top 40px past the fold, its bottom still on screen.
      await placeRowTopFromFold(h.page, MEDIUM, -40)
      await placeRowTopFromFold(h.page, MEDIUM, -40)
      const pinned = await inspect(h.page, MEDIUM)
      console.log(`dark pinned medium: ${JSON.stringify({ rowHidden: pinned.rowHidden, edit: pinned.edit, pin: pinned.pin, card: pinned.card, strip: pinned.strip })}`)
      assert('dark: medium prompt row is the pinned stand-in', pinned.rowHidden && pinned.rowStandin)
      assert(`dark: pinned strip drops Edit (computed display ${pinned.edit?.display})`, pinned.edit?.display === 'none')
      assert(`dark: pinned strip keeps Copy visible (computed ${pinned.copy?.visibility})`, pinned.copy?.visibility === 'visible')
      const top = Math.max(0, Math.round((pinned.card?.top ?? 60) - 30))
      await shot(`${EXPECTED}-05-pinned-strip-without-edit-${theme}`, { x: 0, y: top, width: pinned.viewport.w, height: Math.min(pinned.viewport.h - top, Math.round((pinned.strip?.bottom ?? 300) - top + 60)) })

      // A pinned STEER at hand-off: the accent bubble starts under its badge, so
      // the stand-in height is measured from the row top and the card's bottom
      // must land on the bubble's bottom, with the strip right beneath.
      await placeRowTopFromFold(h.page, STEER, -2)
      await placeRowTopFromFold(h.page, STEER, -2)
      const steer = await inspect(h.page, STEER)
      console.log(`dark pinned steer: ${JSON.stringify({ rowHidden: steer.rowHidden, marker: steer.rowStandinValue, bubble: steer.bubble, card: steer.card, strip: steer.strip })}`)
      assert('dark: steer row is the pinned stand-in', steer.rowHidden && steer.rowStandinValue === 'folding')
      assert(`dark: steer card bottom (${Math.round(steer.card?.bottom ?? 0)}) meets the bubble bottom (${Math.round(steer.bubble?.bottom ?? 0)})`,
        !!steer.card && !!steer.bubble && Math.abs(steer.card.bottom - steer.bubble.bottom) <= 1)
      assert(`dark: steer strip visible under the card (computed ${steer.strip?.visibility})`, steer.strip?.visibility === 'visible')
      const steerTop = Math.max(0, Math.round((steer.card?.top ?? 60) - 30))
      await shot(`${EXPECTED}-06-pinned-steer-handoff-${theme}`, { x: 0, y: steerTop, width: steer.viewport.w, height: Math.min(steer.viewport.h - steerTop, Math.round((steer.strip?.bottom ?? 300) - steerTop + 60)) })

      // An editing row never pins: open Edit on the five-line prompt while it is
      // unpinned, then scroll its top past the fold — no card, editor still shown.
      await placeRowTopFromFold(h.page, MEDIUM, 200)
      await rowLocator(MEDIUM).locator('.message-bubble').hover()
      await h.page.waitForTimeout(400)
      await rowLocator(MEDIUM).locator('[data-message-edit]').click({ timeout: 5000 })
      await h.page.waitForTimeout(400)
      await placeRowTopFromFold(h.page, MEDIUM, -60)
      await placeRowTopFromFold(h.page, MEDIUM, -60)
      const editing = await h.page.evaluate((needle) => {
        const rows = [...document.querySelectorAll('[data-display-index]')]
        const row = rows.find(r => r.textContent.includes(needle))
        const ta = row?.querySelector('textarea')
        const send = row ? [...row.querySelectorAll('button')].find(b => /send/i.test(b.textContent || '')) : null
        const card = document.querySelector('[data-testid="pinned-prompt"]')
        const rb = row?.getBoundingClientRect()
        return {
          found: !!row, editing: !!row?.querySelector('[data-message-editing]'),
          rowHidden: row?.style.visibility === 'hidden', marker: row?.getAttribute('data-pinned-standin'),
          rowTop: rb?.top, rowBottom: rb?.bottom,
          textarea: ta ? getComputedStyle(ta).visibility : null,
          send: send ? getComputedStyle(send).visibility : null,
          card: card ? (card.textContent || '').slice(0, 30) : null,
          viewportH: innerHeight,
        }
      }, MEDIUM)
      console.log(`dark editing row past the fold: ${JSON.stringify(editing)}`)
      assert('dark: the prompt is in edit mode', editing.found && editing.editing)
      assert(`dark: editing row top (${Math.round(editing.rowTop ?? 0)}) is above the fold`, (editing.rowTop ?? 999) < 90)
      assert('dark: editing row is not hidden and carries no stand-in marker', editing.rowHidden === false && editing.marker == null)
      assert(`dark: no pinned card stands in for it (card: ${JSON.stringify(editing.card)})`, editing.card == null)
      assert(`dark: editor textarea and Send are visible (${editing.textarea}, ${editing.send})`, editing.textarea === 'visible' && editing.send === 'visible')
      await shot(`${EXPECTED}-07-editing-row-past-fold-unpinned-${theme}`, { x: 0, y: 0, width: 1280, height: Math.min(editing.viewportH, Math.round((editing.rowBottom ?? 400) + 40)) })
      // Leave edit mode so the light pass starts clean.
      await h.page.keyboard.press('Escape')
    }
  }

  await h.close()
  console.log(failures === 0 ? 'ALL ASSERTIONS PASSED' : `${failures} ASSERTION(S) FAILED`)
  process.exit(failures === 0 ? 0 : 1)
}

const RECORD = argv.includes('--record')
;(RECORD ? record() : main()).catch(err => { console.error(err); process.exit(1) })
