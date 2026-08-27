/**
 * Screenshot harness for the affordances a bounded initial fetch changes.
 *
 * Bounding the slot-switch fetch leaves `slotHasMore` true where it was always
 * false before, and two controls read it. This scene proves, from the live DOM
 * rather than the pixels alone, that neither degrades silently any more:
 *
 *  1. In-chat search states its scope. It scans the loaded `messages` array, so
 *     on a bounded transcript a bare "No results" would be a false negative for
 *     text that exists earlier in the history.
 *  2. Fork and Plan stay in place and explain themselves. The fork index is an
 *     index into FULL history, so a tail-only window would cut at the wrong
 *     message. Each action keeps its single element and changes STATE, so the
 *     row holds its shape and the reason is readable where the click would be.
 *
 * The fixture serves a first page with `has_more: true`, which is exactly what
 * the bounded fetch produces — the state is driven through the real network
 * shape, not hand-set in the store.
 *
 * Usage: node scripts/capture-bounded-fetch-affordances.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

const { openTranscriptHarness } = await import('./lib/transcript-harness.mjs')

const OUT = process.argv[2] || '../temp-screenshots/bounded-fetch-affordances'
const SLOT = 'chat-boundedfetch'
// Derived from this script's own location (scripts/ -> website/ -> repo root),
// never hardcoded: this path RENDERS into the captured screenshot, so a personal
// absolute path both leaks a home directory and misrepresents any other checkout.
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
const slots = [{
  key: SLOT, title: 'Bounded fetch', running: false,
  last_message: 'rollout complete', messages: 243, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
}]
// A bounded FIRST PAGE: more history exists above it, so `slotHasMore` is true.
const detail = {
  running: false, has_more: true, next_before: 240, total: 243, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 900, content: 'Did the rollout finish?' },
    { role: 'assistant', ts: now - 850, content: 'Yes — the rollout completed and health checks are green.' },
    { role: 'user', ts: now - 500, content: 'Any retries along the way?' },
    { role: 'assistant', ts: now - 30, content: 'Two retries on the first batch, both transient.' },
  ],
}

async function main() {
  const h = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots, detail,
    viewport: { width: 1400, height: 950 },
  })

  let failures = 0
  const assert = (label, ok) => {
    console.log(`${ok ? 'PASS' : 'FAIL'}: ${label}`)
    if (!ok) failures += 1
  }
  const shot = async name => {
    await h.page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }
  const bodyText = () => h.page.evaluate(() => document.body.innerText)

  await h.load('dark', { selector: 'textarea[data-composer-input]', settle: 900 })

  // 1. Search states its scope when nothing in the loaded window matches.
  await h.page.keyboard.press('Control+f')
  await h.page.waitForTimeout(250)
  await h.page.keyboard.type('a phrase from much older history')
  await h.page.waitForTimeout(500)
  await shot('01-search-scope-no-results-dark')
  assert('no-match line names the loaded-window scope', /in loaded history/i.test(await bodyText()))

  // 2. Same clause alongside a real count, so a hit does not imply completeness.
  await h.page.keyboard.down('Control'); await h.page.keyboard.press('a'); await h.page.keyboard.up('Control')
  await h.page.keyboard.type('rollout')
  await h.page.waitForTimeout(500)
  await shot('02-search-scope-with-matches-dark')
  assert('match count also names the scope', /\d+\/\d+ results in loaded history/.test(await bodyText()))
  await h.page.keyboard.press('Escape')
  await h.page.waitForTimeout(250)

  // 3. The actions hold their place, disabled, each carrying the reason.
  const row = h.page.locator('.group').filter({ hasText: 'health checks are green' }).first()
  await row.hover()
  await h.page.waitForTimeout(400)
  const bar = h.page.locator('[data-testid="load-earlier-messages"]').first()
  assert('earlier-messages control is present', await bar.count() > 0)
  // Open the overflow: the items live in a portal, so a closed menu renders none of
  // them and any count taken here would silently measure something else.
  await row.locator('[data-testid="assistant-more-actions"]').first().click()
  await h.page.waitForTimeout(300)
  const blocked = h.page.getByRole('menuitem', { name: /fork conversation from here|plan from here/i })
  const n = await blocked.count()
  // Fork + plan, both kept in place and disabled with the reason as visible text.
  assert(`fork and plan both stay rendered (found ${n})`, n === 2)
  for (let i = 0; i < n; i++) {
    const b = blocked.nth(i)
    assert(`action ${i} is aria-disabled`, await b.getAttribute('aria-disabled') === 'true')
    // aria-disabled, never `disabled`: a disabled button drops focus to <body>.
    assert(`action ${i} stays focusable`, await b.getAttribute('disabled') === null)
    // The reason names the ACTION selecting the item performs, and it is VISIBLE text
    // referenced by aria-describedby, so a keyboard user reads it too.
    const describedBy = await b.getAttribute('aria-describedby')
    assert(`action ${i} references a reason`, !!describedBy)
    // useId wraps ids in colons, which are legal in HTML and for aria-describedby
    // (an IDREF, resolved like getElementById) but not in a bare CSS selector.
    const reasonText = await h.page.locator(`[id="${describedBy}"]`).innerText()
    assert(`action ${i} states the remedy as an action`, /load earlier history/i.test(reasonText))
    assert(`action ${i} does not hide it in a tooltip`, await b.getAttribute('title') === null)
  }
  // Speak and raw-view are ROW buttons, not menu entries: neither depends on the window.
  assert('raw-view toggle is a row button', await row.getByRole('button', { name: /switch to raw markdown view/i }).count() > 0)
  assert('menu holds fork/plan only', await h.page.getByRole('menuitem').count() === 2)
  await shot('03-fork-disabled-in-place-dark')

  // 4. Light theme, so the treatment is checked in both palettes.
  await h.load('light', { selector: 'textarea[data-composer-input]', settle: 900 })
  const lightRow = h.page.locator('.group').filter({ hasText: 'health checks are green' }).first()
  await lightRow.hover()
  await h.page.waitForTimeout(400)
  await lightRow.locator('[data-testid="assistant-more-actions"]').first().click()
  await h.page.waitForTimeout(300)
  await shot('04-fork-disabled-in-place-light')

  await h.close()
  console.log(failures === 0 ? 'ALL ASSERTIONS PASSED' : `${failures} ASSERTION(S) FAILED`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch(err => { console.error(err); process.exit(1) })
