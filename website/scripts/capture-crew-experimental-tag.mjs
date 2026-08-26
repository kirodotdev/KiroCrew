/**
 * Screenshot harness for "Crew Mode is tagged Experimental at the point of choice".
 *
 * What the shots have to prove, and why a unit test cannot:
 *
 *   1. The tag is legible in the create menu BEFORE the mode is chosen. The
 *      vitest spec asserts the tag is inside the crew menu item and not the
 *      autopilot one; it cannot say whether the row reads as a caution or as
 *      decoration next to a 13px label.
 *   2. It survives a long localised label. The menu settles at its
 *      `max-w-[264px]` bound in every locale, but NOT because of the tag: the
 *      two mode descriptions are wrapping sentences that fill the width on their
 *      own, so this row's real budget is the ~208px of item content box inside
 *      it. The title row is `flex-wrap` so a label that outgrows that budget
 *      drops the tag to a second line instead of overflowing, and wrapping is a
 *      layout outcome jsdom does not compute (no box model, so `flex-wrap` never
 *      wraps there) — it can only be measured in a real engine.
 *
 *      As shipped the wrap is insurance and stays unexercised: the longest of
 *      the twelve labels still fits on one line. What this pass therefore proves
 *      is the absence of clipping, not the presence of a wrap.
 *
 *      The third pass uses `es`, whose "Nuevo chat de Crew Mode" is the longest
 *      of the twelve shipped labels. NOT the `en-XA` pseudolocale, which would
 *      be the obvious choice and is silently useless here: it is `devOnly`, so
 *      `isRestorableLanguage` drops it in a production build and a persisted
 *      `mc-lang=en-XA` resolves back to `en` — the pass then renders English,
 *      reports success, and proves nothing.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free — no
 * kiro-cli, no dashboard token, no provider CLI).
 *
 * The menu trigger is found by PROBING rather than by accessible name: its
 * aria-label is itself translated, so `getByLabelText('More create options')`
 * would pass in English and miss in `en-XA` — the one pass that matters here.
 *
 * Usage: node scripts/capture-crew-experimental-tag.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-experimental-tag'
mkdirSync(OUT, { recursive: true })

/**
 * One ordinary session, not an empty list.
 *
 * This change lives entirely in the create menu, so an empty sidebar would be
 * the honest fixture — but booting the built SPA against `slots: []` throws in
 * the app shell (`Cannot read properties of undefined (reading 'startsWith')`)
 * and renders the error boundary instead of the sidebar, so there is no menu to
 * open. That is a pre-existing zero-session boot bug, unrelated to the tag and
 * out of scope here; every other harness in this folder seeds at least one slot,
 * and this one follows suit rather than capturing around a crash.
 */
const SLOTS = [{
  key: 'chat-a', title: 'Draft the release notes', running: false, messages: 4,
  agent: 'kirocrew', modified: Math.floor(Date.now() / 1000),
  last_ts: '2026-08-19T00:10:00Z', folder_id: '', last_message: 'Grouped the entries by area.',
}]

/** Slot detail: without this the transcript read falls to the generic branch. */
const DETAIL = { running: false, has_more: false, total: 0, queue: [], messages: [] }
const extra = (path, route) => {
  if (path.startsWith('/api/chat/slots/')) return json(route, DETAIL), true
  return false
}

/**
 * Open the create menu without depending on any translated string.
 *
 * Clicks each menu trigger in turn and keeps the one that reveals the crew
 * entry, whose `data-testid` is the only locale-invariant handle in the menu.
 * Returns the menu locator so the caller can shoot it directly.
 */
/**
 * Wait until the menu's entry animation has actually finished.
 *
 * `DropdownMenuContent` enters with `fade-in-0 zoom-in-95 slide-in-from-top-2`,
 * and `waitFor({ state: 'visible' })` resolves on the FIRST frame of that — so a
 * shot taken there catches a 95%-scaled, part-transparent, still-sliding menu.
 * It also corrupts measurement: `getBoundingClientRect()` returns the visually
 * SCALED box, so the same English menu measured 252px on one pass and 257px on
 * another. `zoom-in-95` is the whole discrepancy (252/265 ~ 0.951).
 *
 * Awaiting `getAnimations({ subtree: true })` rather than sleeping a fixed
 * number of ms: tailwindcss-animate uses real CSS keyframe animations, which the
 * Web Animations API reports, so this settles exactly when they end instead of
 * guessing a duration that a future easing change would invalidate. The
 * geometry-stability pass afterwards covers anything the API does not report.
 */
async function settleAnimations(page, menu) {
  await menu.evaluate(el => Promise.all(
    el.getAnimations({ subtree: true }).map(a => a.finished.catch(() => {})),
  ))
  // Two consecutive frames at the same width before believing it.
  let last = -1
  for (let i = 0; i < 30; i++) {
    const w = await menu.evaluate(el => el.getBoundingClientRect().width)
    if (Math.abs(w - last) < 0.01) return
    last = w
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))
  }
  throw new Error(`menu geometry never stabilised (last ${last}px)`)
}

async function openCreateMenu(page) {
  const triggers = page.locator('button[aria-haspopup="menu"]')
  const n = await triggers.count()
  if (n === 0) throw new Error('no menu triggers found — did the SPA boot?')
  for (let i = 0; i < n; i++) {
    await triggers.nth(i).click()
    const crew = page.locator('[data-testid="new-crew-chat"]')
    if (await crew.count()) {
      await crew.first().waitFor({ state: 'visible', timeout: 5000 })
      return crew.first().locator('xpath=ancestor::*[@role="menu"][1]')
    }
    // Wrong trigger: close whatever it opened before probing the next one.
    await page.keyboard.press('Escape')
    await page.waitForTimeout(120)
  }
  throw new Error(`probed ${n} menu trigger(s); none revealed the crew entry`)
}

async function shoot(page, menu, label) {
  // The tag is small; a full-page shot buries it. Clip to the menu, padded so
  // the border and shadow survive the crop.
  const box = await menu.evaluate(el => {
    const r = el.getBoundingClientRect()
    return { x: r.x, y: r.y, width: r.width, height: r.height }
  })
  await page.screenshot({
    path: `${OUT}/${label}.png`,
    clip: {
      x: Math.max(0, box.x - 10),
      y: Math.max(0, box.y - 10),
      width: box.width + 20,
      height: box.height + 20,
    },
  })
  return box
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const [theme, lang, label] of [
      ['dark', 'en', '01-menu-dark-en'],
      ['light', 'en', '02-menu-light-en'],
      ['dark', 'es', '03-menu-dark-longest-locale'],
    ]) {
      const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
      logPageProblems(page)
      await stubDashboardApi(page, { slots: SLOTS, theme, extra })
      // Written AFTER stubDashboardApi so it is not cleared by that seed's
      // localStorage.clear(); i18n reads `mc-lang` synchronously on first paint.
      await page.addInitScript(l => localStorage.setItem('mc-lang', l), lang)

      await page.goto(base, { waitUntil: 'networkidle' })
      const menu = await openCreateMenu(page)
      await settleAnimations(page, menu)

      // Assert the settled state directly, so a future change that reintroduces
      // mid-flight capture fails here instead of quietly shipping a blurred shot.
      const anim = await menu.evaluate(el => {
        const cs = getComputedStyle(el)
        return { opacity: cs.opacity, transform: cs.transform, running: el.getAnimations({ subtree: true }).length }
      })
      if (anim.opacity !== '1') throw new Error(`${label}: menu opacity ${anim.opacity}, animation unfinished`)
      if (!['none', 'matrix(1, 0, 0, 1, 0, 0)'].includes(anim.transform)) {
        throw new Error(`${label}: menu transform ${anim.transform}, still scaled/sliding`)
      }
      if (anim.running !== 0) throw new Error(`${label}: ${anim.running} animation(s) still attached`)

      const crewItem = page.locator('[data-testid="new-crew-chat"]').first()
      const tag = crewItem.locator('[data-testid="crew-experimental-tag"]')

      if (await tag.count() !== 1) {
        throw new Error(`${label}: expected exactly 1 tag inside the crew item, got ${await tag.count()}`)
      }
      // The bound the wrap exists to respect. Asserted rather than eyeballed so
      // a future label change cannot quietly reintroduce the clipping.
      const menuBox = await menu.evaluate(el => el.getBoundingClientRect().width)
      if (menuBox > 264) throw new Error(`${label}: menu is ${menuBox}px, past its 264px max`)

      // Clipping check: the tag must sit fully inside the menu's content box.
      const overflow = await tag.evaluate((el, menuEl) => {
        const t = el.getBoundingClientRect()
        const m = menuEl.getBoundingClientRect()
        return { right: t.right - m.right, bottom: t.bottom - m.bottom }
      }, await menu.elementHandle())
      if (overflow.right > 0.5 || overflow.bottom > 0.5) {
        throw new Error(`${label}: tag escapes the menu by ${JSON.stringify(overflow)}`)
      }

      // Did the row actually wrap? Reported rather than asserted: whether a
      // given locale needs two lines is a fact about its label, not a
      // regression. What IS asserted is the containment check above — the tag
      // stays inside the menu on either branch.
      const wrapped = await tag.evaluate((el, labelEl) => {
        const t = el.getBoundingClientRect()
        const l = labelEl.getBoundingClientRect()
        return t.top >= l.bottom - 2
      }, await crewItem.locator('span').first().elementHandle())

      const box = await shoot(page, menu, label)
      const text = (await tag.textContent() || '').trim()
      console.log(
        `${label}: menu ${Math.round(box.width)}px · tag ${JSON.stringify(text)}`
        + ` · ${wrapped ? 'wrapped to its own line' : 'same line as the label'} · not clipped`,
      )
      await page.close()
    }
    console.log(`\nOK — screenshots in ${OUT}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
