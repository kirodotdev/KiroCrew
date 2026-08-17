/**
 * Screenshot harness for the session row's 4px type scale.
 *
 * The change is pure geometry, so the evidence has to be geometry: this harness
 * measures the RENDERED boxes in a real browser (jsdom has no layout engine, so
 * the unit test can only assert the classes that ask for them) and fails loudly
 * if a row is not a whole number of grid units, or if the status glyph does not
 * sit on the headline's optical centre.
 *
 * Two rows exist to catch what a plain row cannot: a very long title, which used
 * to wrap to two lines and change the row's height, and a row carrying PR chips,
 * which is ~18px taller than the rest — the case that row-centring the gutter got
 * wrong.
 *
 * This is the ONE place the row's geometry is measured. It prints the per-edge
 * audit (every line box's top and bottom, plus the row-to-row PITCH — the term
 * that caught a 60/61px alternation no row height could have revealed), asserts
 * the invariants, and captures the shots. An earlier revision split the reporting
 * half into its own script; two harnesses stubbing the same API and measuring the
 * same boxes drift apart, and only one of them would get updated.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static server
 * with every /api/** call answered from fixtures (gateway-free).
 *
 * Set GRID_BASELINE=1 to capture the BEFORE state instead: the geometry checks
 * become reports (the baseline fails them, which is the point) and the shots are
 * written as `00-BEFORE-*`. That is how the before/after pair in the PR body is
 * produced from ONE harness, so the two sides cannot drift apart.
 *
 * Usage: node scripts/capture-session-row-grid.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-row-grid'
const ACTIVE = 'chat-run'
const REPO = 'https://github.com/kirodotdev/KiroCrew'

mkdirSync(OUT, { recursive: true })
const now = Math.floor(Date.now() / 1000)

const slots = [
  {
    key: ACTIVE, title: '搜索按钮窄屏错位问题', running: true, messages: 4,
    agent: 'default', project_dir: '/home/z/KiroCrew-clone',
    modified: now, last_ts: '2026-08-17T00:10:00Z', folder_id: '',
    last_message: 'Reading the row anatomy.',
  },
  {
    key: 'chat-long', running: false, messages: 12, agent: 'kirocrew',
    // Long enough to prove the headline truncates instead of wrapping — this row
    // was 80px tall under the two-line clamp and is 60px now, like every other.
    title: '会话行排版按 4px 网格重构，并统一状态行的八个分支与状态槽的垂直定位',
    modified: now - 180, last_ts: '2026-08-17T00:07:00Z', folder_id: '',
    last_message: '3 agents running',
  },
  {
    key: 'chat-approve', running: true, pending_approval: true, messages: 8,
    agent: 'autofix', title: 'board 视图默认值修复',
    modified: now - 660, last_ts: '2026-08-17T00:00:00Z', folder_id: '',
    last_message: 'git push origin fix/board-view',
  },
  {
    key: 'chat-ask', running: true, needs_input: true, messages: 6,
    agent: 'research', title: '竞品 agent 调度机制调研',
    modified: now - 840, last_ts: '2026-08-16T23:58:00Z', folder_id: '',
    last_message: 'Compared three schedulers.',
  },
  {
    // The TALL row: chips add a fourth line. Row-centring the gutter dropped this
    // row's glyph well below the headline; the fixed offset does not move.
    key: 'chat-chips', running: false, messages: 24, agent: 'workflow',
    title: 'topbar 三轨布局回归排查',
    modified: now - 1560, last_ts: '2026-08-16T23:44:00Z', folder_id: '',
    last_message: 'Rebased and pushed; 47 checks green.',
    source_links: [
      { provider: 'github', number: 3663, url: `${REPO}/pull/3663`, state: 'open', ci: 'passed', kind: 'change' },
      { provider: 'github', number: 3789, url: `${REPO}/issues/3789`, kind: 'issue' },
    ],
    source_links_total: 2,
  },
  {
    // Autopilot mode, so the meta line carries a BADGE. The badge lost its own
    // `text-[11px]` and inherits the meta line's 10px: an 11px chip inside a 12px
    // line box overflows it, which would put the line — and every edge below it —
    // back off the grid. Asserted below rather than eyeballed.
    key: 'chat-mode', running: false, messages: 5, agent: 'kirocrew', mode: 'orchestrator',
    title: '自动驾驶模式的会话', modified: now - 2400, last_ts: '2026-08-16T23:30:00Z',
    folder_id: '', last_message: 'Queued three follow-ups.',
  },
  {
    key: 'chat-idle', running: false, messages: 3, agent: 'default',
    project_dir: '/home/z/designs', title: 'PR #3683 迁移方案取舍',
    modified: now - 10800, last_ts: '2026-08-16T21:20:00Z', folder_id: '',
    last_message: '那就先按 B 走，我明天看',
  },
]

/** py-2 top pad + the 12px gutter box, both fixed by the component. */
const PAD = 8
const META = 12
const BASELINE = process.env.GRID_BASELINE === '1'
const problems = []
/** Report in baseline mode, fail in normal mode. */
const check = msg => { if (BASELINE) problems.push(msg); else throw new Error(msg) }
const GUTTER_BOX = 12

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  // deviceScaleFactor 2 for legibility at a 10px glyph, but every shot is CLIPPED
  // to the sidebar: 300x760 CSS px renders 600x1520, inside the 2000px per-edge
  // ceiling a full-window 2x shot would blow.
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })

  let page = null
  async function load(theme) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme })
    await page.addInitScript(([slot, lang]) => {
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-privacy-notice-v1', '1')
      localStorage.setItem('mc-sidebar-pinned', 'true')
      // GRID_LANG renders the row in another UI language. The meta line is now
      // 10px and the mode badges inherit it, so the question "is a translated
      // badge still legible at 10px" needs a shot in the locale with the longest
      // catalogued string (ja オートパイロット), not an English one.
      if (lang) localStorage.setItem('mc-lang', lang)
    }, [ACTIVE, process.env.GRID_LANG || ''])
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  for (const theme of ['dark', 'light']) {
    await load(theme)
    await page.locator('.session-row').first().waitFor({ state: 'visible', timeout: 15000 })

    const rows = await page.locator('.session-row').evaluateAll(els => els.map(el => {
      const box = el.getBoundingClientRect()
      const gutter = el.firstElementChild.getBoundingClientRect()
      const col = el.children[1]
      const line = i => {
        const c = col.children[i]
        return c ? c.getBoundingClientRect() : null
      }
      const headline = line(1)
      const rel = b => [Math.round((b.top - box.top) * 100) / 100, Math.round((b.bottom - box.top) * 100) / 100]
      return {
        title: (col.children[1]?.textContent || '').slice(0, 24),
        absTop: Math.round(box.top * 100) / 100,
        height: Math.round(box.height * 100) / 100,
        edges: {
          meta: rel(col.children[0].getBoundingClientRect()),
          title: rel(headline),
          status: col.children[2] ? rel(col.children[2].getBoundingClientRect()) : null,
        },
        // A fourth child is the PR/issue chip row.
        hasChips: col.children.length > 3,
        // Tallest thing sitting inside the meta line — a mode badge when present.
        metaChildMax: Math.max(0, ...[...col.children[0].children]
          .map(c => Math.round(c.getBoundingClientRect().height * 100) / 100)),
        // Gutter centre minus headline centre. Zero is the whole point.
        glyphOffset: Math.round(((gutter.top + gutter.height / 2) - (headline.top + headline.height / 2)) * 100) / 100,
        headlineH: Math.round(headline.height * 100) / 100,
        gutterTop: Math.round((gutter.top - box.top) * 100) / 100,
      }
    }))

    for (const r of rows) {
      // The headline never wraps, so its box is exactly one 20px line.
      if (r.headlineH !== 20) check(`headline wrapped or drifted (${r.headlineH}px) on "${r.title}"`)
      // Three-line rows are a whole number of 4px units. A row carrying a chip row
      // is NOT, and deliberately is not asserted to be: the chip's own box is 14px
      // (10px text + `py-[1px]` + a 1px border) and `mt-1` adds 4, so such a row
      // lands at 82. Bringing it onto the grid means changing the CHIP, which is a
      // separate decision from the row's type scale — the scale is what this
      // change owns, and it holds for the chip row too (the assertions below).
      if (!r.hasChips && r.height % 4 !== 0) {
        check(`row is ${r.height}px, off the 4px grid, on "${r.title}"`)
      }
      if (!r.hasChips && r.height !== 64) {
        check(`row is ${r.height}px, not the 64px constant, on "${r.title}"`)
      }
      // The glyph sits on the headline's optical centre — on the three-line rows
      // AND on the taller chip row, which is the case row-centring got wrong.
      if (Math.abs(r.glyphOffset) > 0.01) {
        check(`status glyph is ${r.glyphOffset}px off the headline on "${r.title}"`)
      }
      // Nothing inside the meta line may be taller than its 12px box. A badge or
      // chip that overflows would push the headline down and take every edge
      // below it off the grid, which the row-height check alone would still pass.
      if (r.metaChildMax > META) {
        check(`something in the meta line is ${r.metaChildMax}px, taller than its ${META}px box, on "${r.title}"`)
      }
      if (r.gutterTop !== PAD + 12 + (20 - GUTTER_BOX) / 2) {
        check(`gutter top is ${r.gutterTop}px, not the derived offset, on "${r.title}"`)
      }
    }
    // Per-edge audit. A row height that is a multiple of 4 says nothing about the
    // edges INSIDE it: `py-1.5` (6px) produced a 60px row with all 8 interior
    // edges 2px off, which is a grid on paper only.
    const on4 = v => Math.abs(v % 4) < 0.01 || Math.abs((v % 4) - 4) < 0.01
    const mark = v => `${String(v).padStart(6)}${on4(v) ? ' ' : '<'}`
    console.log(`\n${theme}  height  meta.t meta.b ttl.t  ttl.b  st.t   st.b   gut.t`)
    const interior = []
    for (const r of rows) {
      const e = [r.height, r.edges.meta[0], r.edges.meta[1], r.edges.title[0], r.edges.title[1],
        ...(r.edges.status ? r.edges.status : []), r.gutterTop]
      // The chip row's own height is out of scope, so it cannot be judged on the
      // 4px rule; its INTERIOR edges are the scale's and still are.
      interior.push(...(r.hasChips ? e.slice(1) : e))
      console.log(`  ${mark(r.height)}${r.hasChips ? '*' : ' '}${e.slice(1).map(mark).join('')}  ${r.title}`)
    }
    const offGrid = [...new Set(interior.filter(v => !on4(v)))]
    if (offGrid.length) check(`edges off the 4px grid: ${offGrid.join(', ')}`)

    // PITCH == the preceding row's own height, for every pair. That is the exact
    // property the divider fix buys, and it is what actually failed before: the
    // divider added a pixel of layout, and since the active row suppresses its
    // neighbours' dividers the step ALTERNATED (60/61) while every row still
    // measured 60. Asserted as "pitch equals height" rather than "pitch is 64" so
    // it holds across the chip row too, whose own height is out of scope.
    const pitches = rows.slice(1).map((r, i) => Math.round((r.absTop - rows[i].absTop) * 100) / 100)
    pitches.forEach((pitch, i) => {
      if (Math.abs(pitch - rows[i].height) > 0.01) {
        check(`pitch after "${rows[i].title}" is ${pitch}px, not its own ${rows[i].height}px height`)
      }
    })
    // Every number here is MEASURED. An earlier revision asserted the glyph
    // offset in prose ("0px off the headline on all") while the same run was
    // reporting 2.38px in baseline mode — a summary that states a result instead
    // of reading it is how a harness starts lying about its own subject.
    const worstGlyph = Math.max(...rows.map(r => Math.abs(r.glyphOffset)))
    console.log(`  ${rows.length} rows  pitch ${[...new Set(pitches)].join(' / ')}px  ` +
      `${interior.length - interior.filter(v => !on4(v)).length}/${interior.length} edges on 4px  ` +
      `glyph worst ${worstGlyph}px off the headline  (* = chip row, height out of scope)`)

    const side = await page.locator('.session-row').first()
      .evaluate(el => {
        const list = el.closest('[class*="overflow-y-auto"]') || el.parentElement
        const r = list.getBoundingClientRect()
        return { x: r.x, y: r.y, width: r.width }
      })
    await page.screenshot({
      path: `${OUT}/${BASELINE ? '00-BEFORE' : '01'}-session-list-${theme}${process.env.GRID_LANG ? `-${process.env.GRID_LANG}` : ''}.png`,
      clip: { x: Math.max(0, side.x), y: Math.max(0, side.y), width: Math.min(side.width, 340), height: 620 },
    })
  }

  if (BASELINE) {
    console.log(`\nbaseline: ${problems.length} geometry problem(s) — this is the state the change fixes`)
    for (const m of [...new Set(problems)]) console.log(`  - ${m}`)
  }
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
