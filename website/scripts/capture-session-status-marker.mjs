/**
 * Screenshot + measurement harness for the session row's STATUS MARKER position.
 *
 * The defect this proves is a COLLISION, so the evidence has to be two x's read
 * from a real browser: where the row's status glyph starts, and how far in the
 * recency tint's opaque accent stripe reaches.
 *
 * The marker used to live in an absolutely-positioned gutter inside the row's
 * `pl-3.5`, at x 1..13. The recency tint (Display settings → "Highlight recent
 * sessions", `utils/recencyTint.ts`) paints an opaque accent stripe up to 7px wide
 * at that same left edge, and the session-colour bar takes the first 2px. Accent
 * ink on an accent stripe is a 1:1 contrast, so on a recent session — which is
 * exactly the session that is running — the spinner lost its left half and read as
 * clipped and mis-placed. Inline at the head of the secondary line, the marker
 * starts at the content column instead and clears both by construction.
 *
 * Deliberately SHAPE-AGNOSTIC: it finds the marker by its own class
 * (`svg.animate-spin`, the unread dot's `.rounded-full`) rather than by walking a
 * fixed child index, so the SAME script runs against the before and after trees.
 * That is what makes the pair comparable — a before/after captured by two
 * different scripts proves nothing about the change.
 *
 * The tint is OFF by default (`RECENT_TINT_COUNT = 0`), so the fixture turns it on
 * through the same server config the dashboard reads, and every slot carries a
 * settled `last_turn_ts` so the ranking has something to rank.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static server
 * with every /api/** call answered from fixtures (gateway-free).
 *
 * Set MARKER_BASELINE=1 when running against the PRE-FIX tree: the overlap check
 * becomes a report (the baseline fails it, which is the point) and the shots are
 * written as `00-BEFORE-*`.
 *
 * Usage: node scripts/capture-session-status-marker.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { recencyTintMaxPx } from './lib/recency-tint.mjs'
import { MARKER_SELECTORS } from './lib/session-row-marker.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-status-marker'
const ACTIVE = 'chat-run'
/** The fixture that must render the unread dot — seeded into `mc-unread-slots`. */
const UNREAD = 'chat-unread'
const BASELINE = process.env.MARKER_BASELINE === '1'

/** `recencyTint.ts` MAX_W — READ from that source, never copied: a hardcoded width
 *  keeps asserting the old band after the real stripe widens. */
const TINT_MAX_PX = recencyTintMaxPx()
/** Enough ranks that the top rows sit at the cap (7px / 100% accent). */
const TINT_COUNT = 5

mkdirSync(OUT, { recursive: true })
const now = Math.floor(Date.now() / 1000)
const ago = mins => new Date((now - mins * 60) * 1000).toISOString()

// Every slot has a settled `last_turn_ts`: that is the field the tint ranks by
// (NOT `last_ts`, which moves on every streamed tool call).
const slots = [
  {
    key: ACTIVE, title: 'Add UCE Superstar sessions to the roster', running: true, messages: 4,
    agent: 'default', modified: now, last_ts: ago(0), last_turn_ts: ago(0), folder_id: '',
    last_message: 'Reading the row anatomy.',
  },
  {
    key: 'chat-agents', title: 'Validator in orchestrator mode', running: false, messages: 12,
    agent: 'default', modified: now - 4 * 60, last_ts: ago(4), last_turn_ts: ago(4), folder_id: '',
    last_message: 'cd ~/workplace/Redwood && npm test',
  },
  {
    key: 'chat-approve', title: 'Board view default fix', running: true, pending_approval: true,
    messages: 8, agent: 'autofix', modified: now - 11 * 60, last_ts: ago(11), last_turn_ts: ago(11),
    folder_id: '', last_message: 'git push origin fix/board-view',
  },
  {
    key: 'chat-ask', title: 'Scheduler comparison', running: true, needs_input: true, messages: 6,
    agent: 'research', modified: now - 14 * 60, last_ts: ago(14), last_turn_ts: ago(14),
    folder_id: '', last_message: 'Compared three schedulers.',
  },
  {
    // Unread and idle: the one marker whose words (`last_message`) do not name it,
    // so it is the one that keeps an accessible name after the move — and the one
    // whose box only exists as a flex item, which is why it is seeded and ASSERTED
    // below rather than merely present in the fixture list.
    key: UNREAD, title: 'Terminal font picker round 7', running: false, messages: 24,
    agent: 'kirocrew', modified: now - 26 * 60, last_ts: ago(26), last_turn_ts: ago(26),
    folder_id: '', last_message: 'All 59 checks green.',
  },
  {
    // Outside the tinted top-5 — the control row: no stripe, same marker x.
    key: 'chat-idle', title: 'PR #3683 migration trade-offs', running: false, messages: 3,
    agent: 'default', modified: now - 400 * 60, last_ts: ago(400), last_turn_ts: ago(400),
    folder_id: '', last_message: 'Let us go with B tomorrow.',
  },
]

const problems = []
/** Report in baseline mode, fail in normal mode. */
const check = msg => { if (BASELINE) problems.push(msg); else throw new Error(msg) }

/** Fixtures that own a status marker, so a missing one is a failure, not a skip:
 *  the running spinner, the approval shield, the question mark, and the unread dot. */
const MARKER_ROWS = new Set([ACTIVE, 'chat-approve', 'chat-ask', UNREAD])

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  // deviceScaleFactor 2 for legibility at a 10px glyph, but every shot is CLIPPED
  // to the sidebar so the 2x render stays inside the per-edge ceiling.
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })

  let page = null
  async function load(theme) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots,
      // The flat lane only activates when at least one folder exists (otherwise the
      // toggle is hidden, so a persisted flat preference can never strand anyone).
      folders: [{ id: 'f1', name: 'bug', collapsed: false, order: 0 }],
      theme,
      // The tint count is SERVER config (`dashboard.recent_tint_count`), read
      // through the shared kirocrewConfig query — so it has to be answered here
      // rather than seeded into localStorage. `json()` resolves to undefined, so
      // the handled-flag has to be returned explicitly: returning its promise
      // makes the stub fall through and fulfil the same route twice.
      extra: async (path, route) => {
        if (path === '/api/config/kirocrew') {
          await json(route, { dashboard: { recent_tint_count: TINT_COUNT } })
          return true
        }
        return false
      },
    })
    await page.addInitScript(([slot, unread]) => {
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-privacy-notice-v1', '1')
      localStorage.setItem('mc-sidebar-pinned', 'true')
      // Flat view (FLAT_VIEW_LS_KEY): the date-segmented lane the report came from.
      localStorage.setItem('mc-sidebar-flat-view', '1')
      // Unread is CLIENT state, not payload: `dashboardSlice` hydrates `unreadSlots`
      // from this key. Without it the unread fixture rendered as an ordinary read row
      // and its dot never appeared in any frame — so the ONE marker the component
      // comment flags as fragile (a `w-2 h-2` span only gets its box as a flex item;
      // as an inline child both dimensions are dropped and it vanishes) had zero
      // real-browser evidence, and jsdom cannot supply it — it asserts classes, not
      // rendered boxes.
      localStorage.setItem('mc-unread-slots', JSON.stringify(unread))
    }, [ACTIVE, [UNREAD]])
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  for (const theme of ['light', 'dark']) {
    await load(theme)
    await page.locator('.session-row').first().waitFor({ state: 'visible', timeout: 15000 })

    const rows = await page.locator('.session-row').evaluateAll((els, [tintMax, marks]) => els.map(el => {
      const box = el.getBoundingClientRect()
      // Shape-agnostic (see scripts/lib/session-row-marker.mjs): whichever element
      // carries one of the marker glyphs, at whatever depth, so this same script
      // measures the pre-fix gutter DOM and the current inline DOM.
      const marker = el.querySelector(marks.join(', '))
      const styles = getComputedStyle(el)
      const round = v => Math.round(v * 100) / 100
      return {
        key: el.getAttribute('data-session-row'),
        title: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 28),
        // Does this row actually carry a tint stripe? Read from the row's own
        // computed style rather than assumed from the fixture ranking.
        tinted: styles.boxShadow.includes('inset') || styles.backgroundImage !== 'none',
        markerTag: marker ? marker.tagName.toLowerCase() + '.' + (marker.getAttribute('class') || '').split(/\s+/).slice(0, 2).join('.') : null,
        markerLeft: marker ? round(marker.getBoundingClientRect().left - box.left) : null,
        markerWidth: marker ? round(marker.getBoundingClientRect().width) : null,
        // How much of the marker sits inside the opaque stripe. > 0 is the defect.
        insideTint: marker ? round(Math.max(0, tintMax - (marker.getBoundingClientRect().left - box.left))) : null,
        height: round(box.height),
      }
    }), [TINT_MAX_PX, MARKER_SELECTORS])

    console.log(`\n${theme}  tinted  marker            x     w   in-tint  row`)
    for (const r of rows) {
      console.log(`  ${r.tinted ? ' yes  ' : '  no  '}${String(r.markerTag ?? '—').padEnd(18)}`
        + `${String(r.markerLeft ?? '—').padStart(5)}${String(r.markerWidth ?? '—').padStart(4)}`
        + `${String(r.insideTint ?? '—').padStart(9)}  ${r.title}`)
    }

    // Every fixture that OWNS a marker must actually render one. A row whose marker
    // is missing used to be skipped by the overlap check below (`markerLeft === null
    // → continue`), which is exactly how the unread dot went unmeasured AND
    // unphotographed: the fixture existed, the seed did not, and a silent skip
    // reported the run as clean. `w-2 h-2` on an inline child drops both dimensions,
    // so a vanished dot is a real failure mode, not a hypothetical.
    const mustHaveMarker = rows.filter(r => MARKER_ROWS.has(r.key))
    if (mustHaveMarker.length !== MARKER_ROWS.size) {
      check(`expected ${MARKER_ROWS.size} marker-bearing rows in the fixture, rendered ${mustHaveMarker.length}`)
    }
    for (const r of mustHaveMarker) {
      if (r.markerLeft === null) {
        check(`"${r.key}" rendered NO status marker — nothing to measure and nothing in the frame`)
      } else if (!(r.markerWidth > 0)) {
        check(`"${r.key}" rendered a ${r.markerWidth}px-wide marker — it has no box, so it is invisible`)
      }
    }

    for (const r of rows) {
      if (r.markerLeft === null) continue
      if (r.insideTint > 0) {
        check(`${r.markerTag} starts ${r.markerLeft}px in — ${r.insideTint}px of it inside the `
          + `${TINT_MAX_PX}px recency-tint stripe, on "${r.title}"`)
      }
    }
    const worst = Math.max(0, ...rows.map(r => r.insideTint ?? 0))
    console.log(`  ${rows.length} rows  ${rows.filter(r => r.tinted).length} tinted  `
      + `worst overlap ${worst}px of the ${TINT_MAX_PX}px stripe`)

    const side = await page.locator('.session-row').first()
      .evaluate(el => {
        const list = el.closest('[class*="overflow-y-auto"]') || el.parentElement
        const r = list.getBoundingClientRect()
        return { x: r.x, y: r.y, width: r.width }
      })
    await page.screenshot({
      path: `${OUT}/${BASELINE ? '00-BEFORE' : '01'}-session-rows-${theme}.png`,
      clip: { x: Math.max(0, side.x), y: Math.max(0, side.y), width: Math.min(side.width, 340), height: 560 },
    })
    // A tight crop on the running row: at 2x this is where the collision is
    // actually legible, and it is the frame the report was made from.
    const runRow = await page.locator(`[data-session-row="${ACTIVE}"]`).boundingBox()
    if (runRow) {
      await page.screenshot({
        path: `${OUT}/${BASELINE ? '00-BEFORE' : '01'}-running-row-${theme}.png`,
        clip: { x: runRow.x, y: runRow.y - 4, width: Math.min(runRow.width, 300), height: runRow.height + 8 },
      })
    }
  }

  if (BASELINE) {
    console.log(`\nbaseline: ${problems.length} overlap problem(s) — this is the state the change fixes`)
    for (const m of [...new Set(problems)]) console.log(`  - ${m}`)
  }
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
