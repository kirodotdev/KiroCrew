/**
 * Measurement harness for the reported "huge gap below sessions" in the chat
 * sidebar.
 *
 * Reproduces the reported shape EXACTLY, because the gap only appears when the
 * lane's content collapses down to a couple of rows:
 *   - one COLLAPSED folder holding 152 sessions (renders as a single row),
 *   - one fresh ungrouped session (the only visible session row),
 *   - six DORMANT ungrouped sessions (last activity > 2 days => collapsed
 *     behind the "Dormant sessions (6)" expander).
 *
 * A screenshot cannot distinguish the two candidate causes, so this prints
 * geometry instead: for every container between the sidebar root and the last
 * rendered row it reports the rect plus scrollHeight/clientHeight, and it
 * reports whether the "Older Sessions" footer sits inside the viewport.
 *
 *   - footer INSIDE the viewport + lane not scrollable => the blank is ordinary
 *     flex fill (the lane is taller than its content by design),
 *   - footer BELOW the viewport, or scrollHeight > clientHeight with nothing to
 *     scroll to => real phantom height, and the printed rects say which node
 *     owns it.
 *
 * Usage: node scripts/measure-sidebar-gap.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sidebar-gap'
mkdirSync(OUT, { recursive: true })

const NOW = Date.now()
const HOUR = 3600_000
const DAY = 24 * HOUR

const folders = [
  { id: 'f-autofix', name: 'kirocrew-github-autofix', parent_id: '', collapsed: process.env.EXPAND_FOLDER !== '1', color: '' },
]

/** 152 sessions inside the collapsed folder — they must not render any rows.
 *  EMPTY_FOLDER=1 drops them, keeping every other dimension of the fixture
 *  identical. That A/B is the only clean way to attribute the lane's inflated
 *  scrollHeight: a clipped descendant's getBoundingClientRect() still reports
 *  its full unclipped box, so finding a tall hidden node proves nothing about
 *  what actually contributes to scrollHeight. */
const foldered = process.env.EMPTY_FOLDER === '1' ? [] : Array.from({ length: 152 }, (_, i) => ({
  key: `af${i + 1}`,
  title: `autofix session ${i + 1}`,
  messages: 6,
  running: false,
  agent: 'kirocrew',
  created: new Date(NOW - 30 * DAY).toISOString(),
  last_ts: new Date(NOW - i * HOUR).toISOString(),
  folder_id: 'f-autofix',
}))

/** The one fresh ungrouped row from the report. */
const fresh = [{
  key: 'cron-triage',
  title: 'Cron: gh-issue-triage',
  messages: 12,
  running: false,
  agent: 'default',
  created: new Date(NOW - 2 * HOUR).toISOString(),
  last_ts: new Date(NOW - 20 * 60_000).toISOString(),
  folder_id: '',
}]

/** Six ungrouped sessions older than the 2-day default => dormant. */
const dormant = Array.from({ length: 6 }, (_, i) => ({
  key: `old${i + 1}`,
  title: `dormant session ${i + 1}`,
  messages: 3,
  running: false,
  agent: 'kirocrew',
  created: new Date(NOW - 20 * DAY).toISOString(),
  last_ts: new Date(NOW - (5 + i) * DAY).toISOString(),
  folder_id: '',
}))

const slots = [...foldered, ...fresh, ...dormant]

async function main() {
  // MEASURE_DIST lets the same harness run against a dist that is NOT this
  // worktree's build -- the installed wheel's bundle, or another branch's --
  // which is the only way to tell "this branch has the bug" from "the build the
  // user actually runs has the bug".
  const { srv, base } = await serveDist(process.env.MEASURE_DIST || undefined)
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 1, // screenshots must stay well under the 2000px cap
  })
  const page = await context.newPage()
  logPageProblems(page)
  // Pin the dormant threshold explicitly. Its DEFAULT differs between builds
  // (7 days on main, 2 days on the shipped 0.5.0rc7), so leaving it implicit
  // makes the same fixture split differently per build and the comparison
  // measures the threshold rather than the bug.
  await stubDashboardApi(page, {
    folders,
    slots,
    theme: 'dark',
    localStorageEntries: {
      'mc-session-stale-collapse-ms': String(Number(process.env.STALE_MS || 2 * 24 * 3600_000)),
    },
  })

  await page.goto(base, { waitUntil: 'networkidle' })
  // The ROOT lane's dormant expander is the last thing to appear, so it gates
  // readiness. Scoped to 'root': the collapsed folder renders its OWN expander
  // (data-testid=stale-expander-f-autofix) which is legitimately hidden, and a
  // prefix match resolves to that one and waits forever.
  await page.locator('[data-testid="stale-expander-root"]')
    .waitFor({ state: 'visible', timeout: 20_000 })

  const probe = await page.evaluate(() => {
    const vh = window.innerHeight
    const rect = el => {
      if (!el) return null
      const r = el.getBoundingClientRect()
      return {
        top: Math.round(r.top),
        bottom: Math.round(r.bottom),
        height: Math.round(r.height),
        scrollH: el.scrollHeight,
        clientH: el.clientHeight,
        overflowY: getComputedStyle(el).overflowY,
        flex: getComputedStyle(el).flex,
      }
    }

    // Walk up from the dormant expander to the sidebar root, reporting every
    // ancestor — whichever node's height exceeds its parent's owns the gap.
    const expander = document.querySelector('[data-testid="stale-expander-root"]')
    const expBottom = expander ? Math.round(expander.getBoundingClientRect().bottom) : null
    const chain = []
    let node = expander
    while (node && node !== document.body) {
      chain.push({
        tag: node.tagName.toLowerCase(),
        cls: (node.className || '').toString().slice(0, 70),
        testid: node.getAttribute?.('data-testid') || '',
        ...rect(node),
      })
      node = node.parentElement
    }

    // The "Older Sessions" footer is the element the gap is measured against.
    const footer = [...document.querySelectorAll('[aria-controls="history-pane"]')][0]

    // Decisive, selector-independent: scan every descendant of the scroll lane
    // and report the ones whose box extends BELOW the lane's own bottom. Those
    // are exactly what inflates scrollHeight, whatever they turn out to be.
    // Resolve the lane by walking UP from the expander to the first scrolling
    // ancestor. Selecting on '.overflow-y-auto.scrollbar-none' is wrong: the
    // sidebar renders four elements with that class pair (tree lane, board
    // column, history pane), and querySelector picks whichever is first in DOM
    // order, which reported laneBottom=724 for a lane whose real bottom is 846.
    let lane = expander?.parentElement || null
    while (lane && getComputedStyle(lane).overflowY !== 'auto') lane = lane.parentElement
    const laneBottom = lane ? lane.getBoundingClientRect().bottom : 0
    const offenders = lane
      ? [...lane.querySelectorAll('*')]
        .map(el => {
          const r = el.getBoundingClientRect()
          const cs = getComputedStyle(el)
          return {
            tag: el.tagName.toLowerCase(),
            cls: (el.className || '').toString().slice(0, 60),
            testid: el.getAttribute('data-testid') || '',
            text: (el.textContent || '').trim().slice(0, 32),
            top: Math.round(r.top),
            bottom: Math.round(r.bottom),
            h: Math.round(r.height),
            position: cs.position,
            display: cs.display,
            visibility: cs.visibility,
            opacity: cs.opacity,
          }
        })
        .filter(o => o.bottom > laneBottom + 1 && o.h > 0)
        // Shallowest/topmost first: the outermost offender is the real owner.
        .sort((a, b) => a.top - b.top || b.h - a.h)
        .slice(0, 12)
      : []

    // FolderBody renders two nested boxes: an outer grid (gridTemplateRows
    // 1fr/0fr) and an inner overflow:hidden box. Which of the two fails to
    // shrink decides the fix, so measure both rather than inferring.
    const grid = [...(lane?.querySelectorAll('*') || [])]
      .find(el => getComputedStyle(el).display === 'grid'
        && getComputedStyle(el).gridTemplateRows === '0px')
      || [...(lane?.querySelectorAll('[aria-hidden="true"]') || [])]
        .find(el => getComputedStyle(el).display === 'grid')
    const gridInner = grid?.firstElementChild || null
    const box = el => {
      if (!el) return null
      const r = el.getBoundingClientRect()
      const cs = getComputedStyle(el)
      return {
        h: Math.round(r.height),
        scrollH: el.scrollHeight,
        clientH: el.clientHeight,
        overflow: cs.overflow,
        gridTemplateRows: cs.gridTemplateRows,
        minHeight: cs.minHeight,
        visibility: cs.visibility,
      }
    }

    return {
      viewportHeight: vh,
      expanderBottom: expBottom,
      /** The blank the report is about: last rendered row -> footer top. */
      gapPx: expBottom != null && footer
        ? Math.round(footer.getBoundingClientRect().top) - expBottom
        : null,
      folderBodyOuterGrid: box(grid),
      folderBodyInnerClip: box(gridInner),
      /** content-visibility:hidden must NOT unmount: sessionRowNav walks these. */
      folderRowsInDom: grid ? grid.querySelectorAll('.session-row').length : 0,
      folderRowsPainted: grid
        ? [...grid.querySelectorAll('.session-row')]
          .filter(el => el.getBoundingClientRect().height > 0).length
        : 0,
      laneBottom: Math.round(laneBottom),
      laneRect: rect(lane),
      offenders,
      footer: rect(footer),
      footerInsideViewport: footer
        ? footer.getBoundingClientRect().bottom <= vh + 1
        : null,
      visibleSessionRows: document.querySelectorAll('[data-session-key]').length,
      chain,
    }
  })

  console.log(JSON.stringify(probe, null, 2))

  await page.screenshot({ path: `${OUT}/sidebar-gap.png`, clip: { x: 0, y: 0, width: 340, height: 900 } })

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
