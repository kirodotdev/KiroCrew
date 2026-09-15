/**
 * Screenshot harness for the SESSION-ROW EFFECTIVE-AGENT MARKER.
 *
 * Two frames against the REAL built SPA (website/dist), gateway-free:
 *
 *  1. effective-agent-before.png — today's sidebar. Two sessions are bound to
 *     app agents that nothing dispatches (`mochi`, `research-bot`), so the
 *     default agent takes their turns — and the rows say nothing about it. The
 *     stored binding is deliberately left verbatim, so every row here reads as
 *     if the agent it names is the one running.
 *
 *  2. effective-agent-after.png — the same six sessions with `effective_agent`
 *     on the wire. Only the two diverged rows gain "· answered by kirocrew"; the
 *     four honored rows are byte-identical to frame 1, which is the property that
 *     matters most. A marker that also appeared on a healthy row would be worse
 *     than no marker at all.
 *
 *  3. effective-agent-narrow.png — the SAME diverged rows on a minimum-width
 *     sidebar, with a long effective-agent name. The trailing timestamp group is
 *     `ml-auto … shrink-0`, so a marker that refused to shrink would push it off
 *     the row; the frame is captured only after asserting the timestamp is still
 *     inside its row's box, which is the geometry a class-name assertion cannot
 *     reach in jsdom.
 *
 * Every frame ASSERTS as well as photographs: the run exits non-zero if a marker
 * appears on an honored row, is missing from a diverged one, if the "before"
 * frame renders any marker at all, or if the marker clips the trailing meta.
 *
 * Each scenario also emits a 2x sidebar crop, because the meta line is 10px by
 * design and a 1x full-viewport frame renders it as an unreadable smudge.
 *
 * Usage: node scripts/capture-effective-agent-marker.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/effective-agent-marker'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const iso = new Date(now * 1000).toISOString()

const mkSlot = (key, title, agent, extra = {}) => ({
  key, title, agent, running: false, messages: 6, last_message: '',
  memory_mode: 'persistent', project: '', folder_id: '', modified: now,
  last_ts: iso, last_turn_ts: iso, tags: [], source_links: [], source_links_total: 0,
  ...extra,
})

// Two diverged, four honored. The honored set deliberately spans all three
// "nothing to report" spellings the backend can send — "", and the field absent
// altogether (a row from persisted state, or a gateway predating the field) —
// because each is a separate chance to render a false marker.
const DIVERGED = { 'chat-mochi': 'kirocrew', 'chat-research': 'kirocrew' }

const baseSlots = [
  mkSlot('chat-mochi', 'Pet chat — feeding schedule', 'mochi'),
  mkSlot('chat-review', 'Review PR 6104', 'kirocrew'),
  mkSlot('chat-research', 'Compare vector stores', 'research-bot'),
  mkSlot('chat-triage', 'Triage needs-triage queue', 'kirocrew'),
  mkSlot('chat-notes', 'Draft weekly notes', 'writer'),
  mkSlot('chat-sweep', 'Dependency sweep', 'kirocrew'),
]

/** The shipped payload: no `effective_agent` anywhere. */
const beforeSlots = baseSlots

/** The same sessions, with the backend reporting what actually answers. */
const afterSlots = baseSlots.map((s, i) => {
  if (DIVERGED[s.key]) return { ...s, effective_agent: DIVERGED[s.key] }
  // Alternate the two honored spellings so both are exercised in one frame.
  return i % 2 === 0 ? { ...s, effective_agent: '' } : s
})

const MARKER = '[data-testid="session-effective-agent"]'

/** Slot keys whose row currently renders a marker. */
async function markedRows(page) {
  return page.evaluate((sel) => {
    const out = []
    for (const el of document.querySelectorAll(sel)) {
      const row = el.closest('[data-slot-key]')
      if (row) out.push(row.getAttribute('data-slot-key'))
    }
    return out
  }, MARKER)
}

/**
 * Whether the row's trailing meta (timestamp + glyphs) is still fully inside the
 * row. Read from real layout: the marker shrinking correctly is a geometry fact,
 * and the failure it guards against — an overflowing fixed-height row clipping
 * the timestamp — is invisible to any class-name check.
 */
async function trailingMetaVisible(page, slotKey) {
  return page.evaluate((key) => {
    const row = document.querySelector(`[data-slot-key="${key}"]`)
    if (!row) return { ok: false, why: 'no row' }
    const line = row.querySelector('.session-agent-label')
    if (!line) return { ok: false, why: 'no meta line' }
    // The trailing group is the ml-auto span; it holds the timestamp.
    const trailing = [...line.children].find(el => el.className.includes('ml-auto'))
    if (!trailing) return { ok: false, why: 'no trailing group' }
    const lineBox = line.getBoundingClientRect()
    const box = trailing.getBoundingClientRect()
    return {
      ok: box.width > 0 && box.right <= lineBox.right + 1 && box.left >= lineBox.left - 1,
      why: `trailing ${JSON.stringify(box)} vs line ${JSON.stringify(lineBox)}`,
      text: trailing.textContent,
    }
  }, slotKey)
}

async function renderSidebar(browser, base, slots, sidebarWidth = 360) {
  // deviceScaleFactor 2: the meta line is 10px/12px by design (see ROW_META_CLS),
  // which at 1x photographs as an illegible grey smudge in a pull-request body.
  const context = await browser.newContext({
    viewport: { width: 1420, height: 760 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots,
    // The flat single-lane list — the default a user gets, and the view the
    // marker lives in. Board columns render their own card and are not in scope.
    localStorageEntries: { 'mc-sidebar-width': String(sidebarWidth) },
  })
  await page.goto(`${base}/chat`)
  await page.waitForSelector('[data-slot-key="chat-mochi"]', { timeout: 15000 })
  await page.waitForTimeout(700)
  return { context, page }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  let failure = null

  try {
    // ── Frame 1: as shipped — the divergence is invisible ──────────────────
    {
      const { context, page } = await renderSidebar(browser, base, beforeSlots)
      const marked = await markedRows(page)
      if (marked.length !== 0) {
        throw new Error(`frame 1: expected no marker without the field, saw ${JSON.stringify(marked)}`)
      }
      await page.screenshot({ path: `${OUT}/effective-agent-before.png` })
      await page.screenshot({
        path: `${OUT}/sidebar-before.png`,
        clip: { x: 0, y: 0, width: 420, height: 760 },
      })
      console.log('frame 1 OK: 6 rows, no marker — the substitution is invisible today')
      await context.close()
    }

    // ── Frame 2: with the field — exactly the diverged rows are marked ─────
    {
      const { context, page } = await renderSidebar(browser, base, afterSlots)
      const marked = (await markedRows(page)).sort()
      const expected = Object.keys(DIVERGED).sort()
      const missing = expected.filter(k => !marked.includes(k))
      const extra = marked.filter(k => !expected.includes(k))
      if (missing.length || extra.length) {
        throw new Error(
          `frame 2: marker placement wrong — missing ${JSON.stringify(missing)}, ` +
          `unexpected ${JSON.stringify(extra)}`,
        )
      }
      // The marker must NAME the agent, not merely exist: an empty or
      // placeholder marker would photograph fine and tell the user nothing.
      const text = await page.textContent(MARKER)
      if (!text || !text.includes('kirocrew')) {
        throw new Error(`frame 2: marker does not name the effective agent (read ${JSON.stringify(text)})`)
      }
      // Readable without hovering: the visible text carries the meaning, and the
      // title only repeats it for a truncated row.
      const title = await page.getAttribute(MARKER, 'title')
      if (!title || !text.includes(title)) {
        throw new Error(`frame 2: title ${JSON.stringify(title)} is not a repeat of the visible text`)
      }
      await page.screenshot({ path: `${OUT}/effective-agent-after.png` })
      await page.screenshot({
        path: `${OUT}/sidebar-after.png`,
        clip: { x: 0, y: 0, width: 420, height: 760 },
      })
      // A zoomed crop of one diverged row, so the 10px marker is legible in a
      // pull-request body rather than a grey smudge.
      const row = page.locator('[data-slot-key="chat-mochi"]')
      await row.screenshot({ path: `${OUT}/effective-agent-row.png` })
      console.log(`frame 2 OK: ${marked.length} marked rows (${marked.join(', ')}), 4 honored rows untouched`)
      await context.close()
    }

    // ── Frame 3: minimum width — the marker yields, the timestamp survives ──
    {
      // A deliberately long effective agent, so the marker WANTS more room than
      // the row has. If it took it, the timestamp would be the thing that lost.
      const crowded = afterSlots.map(s =>
        DIVERGED[s.key] ? { ...s, effective_agent: 'kirocrew-research-orchestrator' } : s,
      )
      const { context, page } = await renderSidebar(browser, base, crowded, 260)
      for (const key of Object.keys(DIVERGED)) {
        const seen = await trailingMetaVisible(page, key)
        if (!seen.ok) {
          throw new Error(`frame 3: ${key} clipped its trailing meta — ${seen.why}`)
        }
      }
      await page.screenshot({
        path: `${OUT}/effective-agent-narrow.png`,
        clip: { x: 0, y: 0, width: 300, height: 500 },
      })
      console.log('frame 3 OK: 260px sidebar, long effective agent, timestamps still in-row')
      await context.close()
    }
  } catch (e) {
    failure = e
  }

  await browser.close()
  srv.close()
  if (failure) throw failure
  console.log(`\nwrote 6 frames to ${OUT}`)
}

main().catch(e => {
  console.error(e)
  process.exit(1)
})
