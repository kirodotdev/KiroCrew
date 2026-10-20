/**
 * Geometry harness for a Slack code channel row's meta line at narrow widths.
 *
 * The meta line reads: agent name, the code-channel chip, the time. When it is
 * short of room the agent name must give way first, then the channel name, and
 * the Slack mark and the time must never clip. jsdom computes no layout, so the
 * unit test can only pin the classes; this loads the REAL built SPA with canned
 * slots, nested the two ways the sidebar nests them (three folders deep, and the
 * conductor lane's conductor > worker > worker), with a long agent name and long
 * channel names, and MEASURES each row at the default sidebar width (260px), the
 * narrowest one (180px) and a 320px phone:
 *
 *   - the Slack mark is drawn at exactly its fixed 10px, wholly inside the chip
 *     and its line;
 *   - the chip ends before the time starts, and the time is whole -- unless the
 *     line is too short for the time beside an icon-only chip with the agent
 *     name gone, which is reported (NOTE) next to what an ordinary row at the
 *     same depth does (BASELINE);
 *   - the channel name shows "#" plus two characters or leaves (never "#…");
 *   - when the channel name is cut, the agent name is cut too (it yields first)
 *     and the link glyph has already gone;
 *   - an icon-only chip keeps its mark centred, never an empty tail.
 *
 * Usage: node scripts/capture-slack-code-channel-row-fit.mjs [outDir] [--expect-bug]
 *   --expect-bug  exit 0 only if some check FAILS (run it against a build
 *                 without the fix, to prove the harness can see the bug).
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2)
const EXPECT_BUG = args.includes('--expect-bug')
const OUT = args.find(a => !a.startsWith('--')) || '../temp-screenshots/slack-code-channel-row-fit'
mkdirSync(OUT, { recursive: true })

const now = new Date()
const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1, 5, 55).toISOString()
const channel = (id, name, status = 'active') => ({
  channel_name: name,
  status,
  web_url: `https://slack.com/app_redirect?channel=${id}&team=T0TEAM1`,
})
const FOLDER = 'f-code'
const slackLink = { channel: 'slack', label: 'Slack', target: '…0001', binding: 'b', direction: 'out', drives_session: true, live: true }
const row = (key, title, ts, code, parent = null) => ({
  key, title, running: false, messages: 4, agent: 'kirocrew-conductor', last_ts: ts, last_turn_ts: ts,
  folder_id: FOLDER, last_message: 'Pushed the fix and opened the pull request.', source_links: [], source_links_total: 0,
  links: [slackLink], code_channel: code, created: ts,
  ...(parent ? { parent: { slot: parent, key: parent } } : {}),
})
const recent = new Date(now.getTime() - 60_000).toISOString()
const older = new Date(now.getTime() - 3 * 3600_000).toISOString()
// The real nesting depth Kyle's screenshot shows: a conductor at the top of the
// folder, its workers under it, and a worker's own worker under that, so two
// indents eat into every nested row's meta line.
const ROWS = [
  ['Run the Slack Code goal', row('chat-cond', 'Run the Slack Code goal', older, channel('C0CODE0', 'slack-code-goal-conductor'))],
  ['Fix the login redirect loop', row('chat-today', 'Fix the login redirect loop', recent, channel('C0CODE1', 'fix-login-redirect-loop'), 'chat-cond')],
  ['Bump the Node runtime', row('chat-yday', 'Bump the Node runtime', yesterday, channel('C0CODE2', 'bump-node-runtime-and-lockfile'), 'chat-today')],
  ['Draft the release notes', row('chat-arch', 'Draft the release notes', yesterday, channel('C0CODE3', 'release-notes', 'archived'), 'chat-today')],
]
// An ordinary row at the same depth, as the baseline: what the sidebar does to
// a row with no chip at all.
const PLAIN = ['Tidy the changelog', row('chat-plain', 'Tidy the changelog', yesterday, undefined, 'chat-today')]
const SLOTS = [...ROWS, PLAIN].map(([, s]) => s)
// Three folders deep, like this crew's own "Slack Code › Code channels ›
// kirocrew-worker": every level indents the rows filed at the bottom.
const FOLDERS = [
  { id: 'f-top', name: 'Slack Code', order: 0 },
  { id: 'f-mid', name: 'Code channels', order: 0, parent_id: 'f-top', default_agent: 'kirocrew-conductor' },
  { id: FOLDER, name: 'kirocrew-worker', order: 0, parent_id: 'f-mid', default_agent: 'kirocrew-conductor' },
]
/** The two ways rows nest: folders (the tree lane), and the conductor lane's
 *  lineage (conductor, its worker, that worker's workers). */
const LAYOUTS = ['folders', 'conductor']

const detail = { running: false, has_more: false, total: 0, queue: [], messages: [] }
const extra = (path, route) => {
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  return false
}

const failures = []
/** Lines too short for their own time, reported (not failed) with the baseline. */
const tooNarrow = []
const baseline = []
/** The ordinary row's time at the same depth: whole, or cut by how much. */
async function measureBaseline(page, where) {
  const r = await page.locator('.session-row').filter({ hasText: PLAIN[0] }).evaluate(el => {
    const line = el.querySelector('.session-agent-label').getBoundingClientRect()
    const time = el.querySelector('[data-testid="session-row-time"]').getBoundingClientRect()
    return { over: Math.max(0, time.right - line.right) }
  })
  if (r.over > 0.5) baseline.push(`${where}: an ordinary row's time is cut by ${r.over.toFixed(0)}px`)
}
function check(label, cond, detailText = '') {
  if (!cond) failures.push(`${label}${detailText ? ` (${detailText})` : ''}`)
}

/** Every measurement for one row's meta line, taken in the page. */
async function measure(page, title) {
  return page.locator('.session-row').filter({ hasText: title }).evaluate(el => {
    const line = el.querySelector('.session-agent-label')
    const chip = line.querySelector('[data-testid="code-channel-badge-row"]')
    const mark = chip.querySelector('[data-testid="code-channel-slack-mark"]') || chip.querySelector('svg, img')
    const agent = line.querySelector('[data-testid="session-row-agent"]') || line.firstElementChild
    const name = chip.querySelector('[data-testid="code-channel-name"]') || chip.querySelector('span[dir="auto"]')
    const time = line.querySelector('[data-testid="session-row-time"]')
    const glyph = chip.querySelector('[data-testid="code-channel-link-glyph"]') || chip.querySelector('svg.lucide-external-link')
    const box = n => { const r = n.getBoundingClientRect(); return { x: r.x, right: r.right, w: r.width, top: r.top } }
    let floor = 0
    if (name) {
      // The readable minimum: "#" plus two characters and the ellipsis, in the
      // name's own font. Anything narrower is a bare "#…" or worse.
      const probe = name.cloneNode(false)
      probe.textContent = `${(name.textContent || '').slice(0, 3)}\u2026`
      probe.style.cssText = 'position:absolute;visibility:hidden;max-width:none;min-width:0;width:auto;overflow:visible'
      name.parentElement.appendChild(probe)
      floor = probe.getBoundingClientRect().width
      probe.remove()
    }
    const nb = name ? name.getBoundingClientRect() : null
    const cb = chip.getBoundingClientRect()
    const onFirstLine = n => { const r = n.getBoundingClientRect(); return r.width >= 1 && r.top < cb.top + 8 && r.right <= cb.right + 0.5 }
    // Right edge of what the chip actually draws on its one visible line: its
    // leaves (text runs and glyphs), not the boxes that hold them.
    let inkRight = cb.x
    let inkLeft = cb.right
    for (const n of chip.querySelectorAll('*')) {
      if (n.closest('svg') && n.tagName.toLowerCase() !== 'svg') continue
      if (n.tagName.toLowerCase() !== 'svg' && n.children.length > 0) continue
      if (getComputedStyle(n).position === 'absolute' || !onFirstLine(n)) continue
      inkRight = Math.max(inkRight, n.getBoundingClientRect().right)
      inkLeft = Math.min(inkLeft, n.getBoundingClientRect().left)
    }
    const minChip = parseFloat(getComputedStyle(chip).minWidth) || 0
    return {
      slot: (() => { const s = chip.querySelector('[data-testid="code-channel-text"]'); return s ? s.getBoundingClientRect().toJSON() : null })(),
      word: (() => { const s = chip.querySelector('[data-testid="code-channel-status"]'); return s ? s.getBoundingClientRect().toJSON() : null })(),
      depth: Number(el.getAttribute('data-conductor-depth') || el.closest('[data-conductor-depth]')?.getAttribute('data-conductor-depth') || 0),
      glyphShown: glyph ? onFirstLine(glyph) : false,
      stateGlyph: (() => { const g = chip.querySelector('[data-testid="code-channel-status-glyph"]'); return g ? onFirstLine(g) : null })(),
      line: box(line), chip: box(chip), mark: box(mark), time: box(time), inkRight, inkLeft, minChip,
      timeCut: time.scrollWidth > time.clientWidth + 0.5,
      agentCut: agent.scrollWidth > agent.clientWidth + 0.5,
      agentW: agent.getBoundingClientRect().width,
      name: name ? {
        w: nb.width, cut: name.scrollWidth > name.clientWidth + 0.5, floor,
        // An sr-only name, or one the chip wrapped onto its clipped second line.
        visible: getComputedStyle(name).position !== 'absolute',
        // Not on the chip's first line: the chip's fixed height clips it away
        // (wholly or, worse, in half).
        wrapped: nb.top > cb.top + 3,
      } : null,
    }
  })
}

async function checkRow(page, title, where, { nameMustShow = false } = {}) {
  const m = await measure(page, title)
  const at = `${where} / ${title}`
  if (process.env.ROWFIT_DEBUG) console.log(at, JSON.stringify(m))
  if (nameMustShow && m.name && m.name.visible) {
    check(`${at}: channel name stays on the chip at this width`, !m.name.wrapped)
  }
  // The top invariant, measured: the mark is drawn at exactly its fixed size and
  // nothing (chip, meta line) cuts any of it off.
  check(`${at}: Slack mark is exactly its fixed 10px`, Math.abs(m.mark.w - 10) < 0.5, `width ${m.mark.w.toFixed(1)}`)
  check(`${at}: Slack mark lies inside the chip`, m.mark.x >= m.chip.x - 0.5 && m.mark.right <= m.chip.right + 0.5,
    `mark ${m.mark.x.toFixed(1)}-${m.mark.right.toFixed(1)} chip ${m.chip.x.toFixed(1)}-${m.chip.right.toFixed(1)}`)
  check(`${at}: Slack mark lies inside its line`, m.mark.x >= m.line.x - 0.5 && m.mark.right <= m.line.right + 0.5)
  // An archived or wrapped-up chip keeps its state glyph at every width, so it
  // never reads as an active one.
  if (m.stateGlyph !== null) check(`${at}: state glyph stays beside the mark`, m.stateGlyph === true)
  // A state word wraps away only when the chip truly lacks room for its floor.
  if (m.word && m.slot && m.slot.top > m.chip.top + 3) {
    check(`${at}: state word leaves only when the chip has no room for it`, m.chip.w - (m.inkRight - m.inkLeft) < 40,
      `chip ${m.chip.w.toFixed(0)}px, icons ${(m.inkRight - m.inkLeft).toFixed(0)}px`)
  }
  check(`${at}: chip ends before the time`, m.chip.right <= m.time.x + 0.5, `chip right ${m.chip.right.toFixed(1)} time x ${m.time.x.toFixed(1)}`)
  // The time never truncates on the chip's account. The one case it is cut is
  // a line too short for the time beside even an icon-only chip with the agent
  // gone; there nothing in the chip has width left to give (and an ordinary row
  // at that depth fares no better, see `baseline`).
  const timeWhole = m.time.right <= m.line.right + 0.5 && m.time.x >= m.line.x - 0.5 && !m.timeCut
  const chipGaveAll = m.chip.w <= m.minChip + 0.5 && m.agentW < 1
  check(`${at}: time is whole, or the chip already gave all it has`, timeWhole || chipGaveAll,
    `time ${m.time.x.toFixed(1)}-${m.time.right.toFixed(1)} line ${m.line.x.toFixed(1)}-${m.line.right.toFixed(1)} chip ${m.chip.w.toFixed(1)} (min ${m.minChip})`)
  if (!timeWhole) tooNarrow.push(`${at}: line ${m.line.w.toFixed(0)}px for a ${m.time.w.toFixed(0)}px time`)
  // An icon-only chip wider than its mark keeps the mark centred, never an empty tail.
  const left = m.inkLeft - m.chip.x
  const right = m.chip.right - m.inkRight
  check(`${at}: chip has no blank slot`, right <= 6 || Math.abs(left - right) <= 1.5, `left ${left.toFixed(1)}px right ${right.toFixed(1)}px`)
  if (m.name && m.name.visible) {
    // The name may leave the chip only once the agent name has gone entirely:
    // that is the line being too short for the mark, "#abc" and the time at once.
    if (m.name.wrapped) {
      check(`${at}: channel name leaves only after the agent name has gone`, m.agentW < 1, `agent still ${m.agentW.toFixed(1)}px`)
    } else {
      check(`${at}: channel name keeps "#" and two characters (never a bare "#…")`, m.name.w + 0.5 >= m.name.floor, `shown ${m.name.w.toFixed(1)} need ${m.name.floor.toFixed(1)}`)
      if (m.name.cut) {
        check(`${at}: agent name yields before the channel name`, m.agentCut || m.agentW < 1)
        check(`${at}: link glyph leaves before the channel name loses a character`, !m.glyphShown)
      }
    }
  }
  return m
}

/** The rows really nest, so the indents that cause the clip are on screen. */
async function checkNesting(page, where, layout) {
  if (layout === 'conductor') {
    const depths = await page.locator('[data-conductor-depth]').evaluateAll(els => els.map(el => Number(el.getAttribute('data-conductor-depth'))))
    check(`${where}: rows nest two levels deep`, depths.length > 0 && Math.max(...depths) >= 2, `depths ${JSON.stringify(depths)}`)
  } else {
    const shown = await page.getByText('kirocrew-worker', { exact: true }).count()
    check(`${where}: rows sit three folders deep`, shown > 0)
  }
}

async function shootFolder(page, path) {
  const boxes = await page.locator('.session-row').evaluateAll(els => els.map(el => el.getBoundingClientRect().toJSON()))
  const head = await page.getByText('Slack Code', { exact: true }).first().boundingBox().catch(() => null)
  const x = Math.max(0, Math.floor(Math.min(...boxes.map(b => b.x)) - 8))
  const y = Math.max(0, Math.floor(Math.min(head ? head.y : Infinity, ...boxes.map(b => b.y)) - 8))
  const right = Math.ceil(Math.max(...boxes.map(b => b.right)) + 8)
  const bottom = Math.ceil(Math.max(...boxes.map(b => b.bottom)) + 8)
  await page.screenshot({ path, clip: { x, y, width: right - x, height: bottom - y } })
}

/** Seeds the page's storage before it loads (runs in the page). */
const seed = ({ layout, width }) => {
  localStorage.setItem('mc-active-slot', 'chat-today')
  localStorage.setItem('mc-privacy-notice-v1', '1')
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  localStorage.setItem('mc-sidebar-lane', layout === 'conductor' ? 'conductor' : 'tree')
  // Both conductors open, so the worker and its own workers are on screen.
  localStorage.setItem('mc-sidebar-conductor-expanded', JSON.stringify(['chat-cond', 'chat-today']))
  if (width) {
    localStorage.setItem('mc-sidebar-pinned', 'true')
    localStorage.setItem('mc-sidebar-width', String(width))
  }
}

async function desktop(browser, base, theme, width, layout) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots: SLOTS, folders: FOLDERS, theme, extra })
  await page.addInitScript(seed, { layout, width })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.locator('.session-row').first().waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForTimeout(1500)
  await page.mouse.move(1000, 890)
  const where = `${layout} ${width}px ${theme}`
  await checkNesting(page, where, layout)
  await measureBaseline(page, where)
  for (const [title] of ROWS) await checkRow(page, title, where, { nameMustShow: width >= 260 })
  await shootFolder(page, join(OUT, `sidebar-${layout}-${width}px-${theme}.png`))
  await context.close()
}

async function phone(browser, base, theme, layout) {
  const context = await browser.newContext({ viewport: { width: 320, height: 640 }, deviceScaleFactor: 3, hasTouch: true, isMobile: true })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots: SLOTS, folders: FOLDERS, theme, extra })
  await page.addInitScript(seed, { layout, width: 0 })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.getByTestId('mobile-topbar-title').waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForTimeout(1200)
  await page.getByTestId('mobile-topbar-sessions-toggle').tap()
  await page.locator('.session-row').first().waitFor({ state: 'visible', timeout: 10_000 })
  await page.waitForTimeout(800)
  const where = `${layout} 320px phone ${theme}`
  await checkNesting(page, where, layout)
  await measureBaseline(page, where)
  for (const [title] of ROWS) await checkRow(page, title, where)
  await shootFolder(page, join(OUT, `sidebar-${layout}-320px-phone-${theme}.png`))
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const layout of LAYOUTS) {
      for (const theme of ['dark', 'light']) {
        for (const width of [260, 180]) await desktop(browser, base, theme, width, layout)
        await phone(browser, base, theme, layout)
      }
    }
  } finally {
    await browser.close()
    srv.close()
  }
  for (const n of tooNarrow) console.log(`NOTE ${n}`)
  for (const b of baseline) console.log(`BASELINE ${b}`)
  for (const f of failures) console.log(`FAIL ${f}`)
  if (EXPECT_BUG) {
    if (failures.length === 0) {
      console.error('expected the bug to show, but every check passed')
      process.exit(1)
    }
    console.log(`\nOK (expect-bug) — ${failures.length} check(s) failed as expected; screenshots in ${OUT}`)
    return
  }
  if (failures.length) {
    console.error(`\n${failures.length} check(s) failed`)
    process.exit(1)
  }
  console.log(`\nOK — every row fits; screenshots in ${OUT}`)
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
