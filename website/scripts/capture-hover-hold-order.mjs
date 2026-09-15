/**
 * Screenshot harness + behavior check for the HOVER HOLD on session row order.
 *
 * Under the default date-desc sort a background session going active re-sorts
 * the sidebar at any moment, so a row can move out from under the cursor between
 * the user reading it and pressing — and the row's hover bar carries Close, so
 * the mis-click closes a session. The hovered row now keeps the index it had
 * when the pointer arrived and takes its true index on release.
 *
 * This ASSERTS as well as photographs. jsdom mocks framer-motion entirely, so
 * the unit tests can only pin the rendered ORDER; what they cannot show is the
 * thing a reviewer wants to see, which is the row visibly staying put while its
 * neighbours move. Hence a real browser, a real pointer hover, and a per-scene
 * order assertion that exits non-zero — a blank or wrong frame must fail here
 * rather than ship as evidence.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6180 --strictPort   # in another shell
 *   node scripts/capture-hover-hold-order.mjs http://127.0.0.1:6180 [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6180'
const OUT = process.argv[3] || '../temp-screenshots/hover-hold-order'
const HELD = 'chat-migration'

mkdirSync(OUT, { recursive: true })

let failed = false
/** Settled data-theme per scene; all frames must agree or the set is mismatched. */
const sceneThemes = []
const check = (label, ok, detail) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failed = true
}

const browser = await chromium.launch({ executablePath: chromiumExecutable() })

/** Row keys in rendered document order — the same signal the unit tests assert. */
const order = page => page.$$eval('[data-session-row]', els =>
  els.map(el => el.getAttribute('data-session-row')))

/**
 * The colour theme is applied ASYNCHRONOUSLY after mount, so a frame taken too
 * early carries a different theme than its siblings: one pass read kiro-dark
 * before the hover and monokai-dark after it, in the SAME page. Seeding
 * localStorage does not win that race. So wait for `data-theme` to stop moving,
 * and assert the frames agree with each other rather than with a hardcoded name.
 */
async function settleTheme(page) {
  let prev = null
  for (let i = 0; i < 20; i++) {
    const now = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    if (now && now === prev) return now
    prev = now
    await page.waitForTimeout(250)
  }
  return prev
}

async function openSidebar() {
  const page = await browser.newPage({ viewport: { width: 460, height: 620 } })
  page.on('pageerror', e => { console.log(`FAIL pageerror — ${e.message}`); failed = true })
  await page.goto(`${BASE}/capture/hover-hold-order.html?theme=dark`)
  await page.waitForSelector('[data-capture-ready]')
  await page.waitForSelector(`[data-session-row="${HELD}"]`)
  const theme = await settleTheme(page)
  return { page, theme }
}

const shot = (page, name) =>
  page.locator('.sidebar-inner').screenshot({ path: `${OUT}/${name}.png` })

/**
 * Is the row VISIBLY hovered? Read as computed style, because the first pass
 * asserted "a control is present in the row" — which every row satisfies with or
 * without hover, so it could not fail, and it passed over three frames that
 * showed no hover affordance at all.
 *
 * The property read is the row's BACKGROUND, not a revealed action bar: in this
 * lane the row's `⋮` is always visible and probing the row's subtree finds no
 * opacity-gated child, so `hover:bg-bg-hover` is the affordance a user actually
 * sees here. It flips two-way, so the check can fail in both directions — it is
 * asserted unhovered BEFORE the hover and hovered after.
 */
const hoverState = (page, key) => page.evaluate(k => {
  const row = document.querySelector(`[data-session-row="${k}"]`)
  return {
    matchesHover: !!row?.matches(':hover'),
    rowBg: row ? getComputedStyle(row).backgroundColor : 'no-row',
    theme: document.documentElement.getAttribute('data-theme'),
  }
}, key)

const opaqueBg = bg => bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent'

// ── Scene 1+2: one session, because the hold only exists if the pointer arrived
// BEFORE the re-sort. Splitting these into two page loads would photograph two
// unrelated states and prove nothing about the ordering between them.
{
  const { page, theme: themeA } = await openSidebar()
  sceneThemes.push(themeA)
  const rest = await order(page)
  check('scene 1: at rest, newest first', rest.join() === 'chat-rebase,chat-migration,chat-flaky,chat-onboarding', rest.join(' '))

  // Negative half of the control: with no pointer on it, the row must be plain.
  const before = await hoverState(page, HELD)
  check('control: row is NOT hovered before hovering',
    !before.matchesHover && !opaqueBg(before.rowBg), JSON.stringify(before))
  check('control: theme settled before capturing', !!themeA && /-(dark|light)$/.test(themeA), String(themeA))

  // A real pointer hover: this is what fires the delegated onPointerOver.
  await page.hover(`[data-session-row="${HELD}"]`)
  await page.waitForTimeout(400)
  const after = await hoverState(page, HELD)
  check('scene 1: hovered row is VISIBLY hovered (row tinted)',
    after.matchesHover && opaqueBg(after.rowBg), JSON.stringify(after))
  await shot(page, '01-hovered-at-rest')

  // Two OLDER sessions go active. date-desc wants both above the held row.
  await page.evaluate(() => window.__hoverHoldBump())
  await page.waitForTimeout(500)
  const held = await order(page)
  check('scene 2: hovered row keeps index 1 while newer rows sort around it',
    held[1] === HELD, held.join(' '))
  check('scene 2: the two bumped rows really did move up', held[0] === 'chat-flaky', held.join(' '))
  // The hold is only legible if the row is STILL visibly the hovered one after
  // the re-sort — otherwise frame 2 is just a differently ordered list.
  const stillHovered = await hoverState(page, HELD)
  check('scene 2: held row is still visibly hovered after the re-sort',
    stillHovered.matchesHover && opaqueBg(stillHovered.rowBg), JSON.stringify(stillHovered))
  await shot(page, '02-held-while-list-resorts')
}

// ── Scene 3: the contrast case. Same bump, no pointer on the list, so the sort
// lands in full — which is what makes the hold visibly CONDITIONAL rather than a
// blanket freeze.
{
  const { page, theme: themeC } = await openSidebar()
  sceneThemes.push(themeC)
  await page.mouse.move(5, 600) // away from every row
  await page.evaluate(() => window.__hoverHoldBump())
  await page.waitForTimeout(500)
  const free = await order(page)
  check('scene 3: unhovered list re-sorts in full (held row falls to last)',
    free.join() === 'chat-flaky,chat-onboarding,chat-rebase,chat-migration', free.join(' '))
  const none = await hoverState(page, HELD)
  check('scene 3: no row is hovered', !none.matchesHover && !opaqueBg(none.rowBg), JSON.stringify(none))
  check('scene 3: every frame shares one settled theme',
    new Set(sceneThemes).size === 1, sceneThemes.join(' vs '))
  await shot(page, '03-unhovered-resorts-in-full')
}

await browser.close()
process.exit(failed ? 1 : 0)
