/**
 * Browser harness for a Slack code channel's crew in the folder view.
 *
 * Loads the REAL built SPA with canned slots: a "Code channels" folder holding a
 * code-channel conductor, the worker sessions it opened (each cites it as its
 * creator), an ordinary parent with a child, and a second code channel. For each
 * width (the default 260px sidebar and the narrowest 180px) and theme it checks
 * and shoots:
 *
 *   - collapsed (the default): only the conductor row shows; its fold names the
 *     workers; an ordinary parent's child is still listed;
 *   - waiting on you: a hidden worker waiting on an approval puts "Waiting on
 *     you · <what it last said>" on the conductor row;
 *   - expanded: every worker shows under the conductor, and the fold persists
 *     across a reload.
 *
 * Usage: node scripts/capture-slack-code-channel-crew.mjs [outDir] [--expect-bug] [--record]
 *   --expect-bug  exit 0 only if some check FAILS (run it against a build
 *                 without the fold, to prove the harness can see it).
 *   --record      instead, record one WebM of the fold opening and closing
 *                 (260px, dark), for the PR's recording evidence.
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2)
const EXPECT_BUG = args.includes('--expect-bug')
const RECORD = args.includes('--record')
const OUT = args.find(a => !a.startsWith('--')) || '../temp-screenshots/slack-code-channel-crew'
mkdirSync(OUT, { recursive: true })

const now = Date.now()
const iso = ago => new Date(now - ago * 1000).toISOString()
const FOLDER = 'f-code'
const channel = (id, name) => ({ channel_name: name, status: 'active', web_url: `https://slack.com/app_redirect?channel=${id}&team=T0TEAM1` })
const slackLink = { channel: 'slack', label: 'Slack', target: '…0001', binding: 'b', direction: 'out', drives_session: true, live: true }
const row = (key, title, ago, extra = {}) => ({
  key, title, running: false, messages: 4, agent: 'kirocrew-conductor', last_ts: iso(ago), last_turn_ts: iso(ago), created: iso(ago),
  folder_id: FOLDER, last_message: 'All three chats are still working.', source_links: [], source_links_total: 0, ...extra,
})
const worker = (key, title, ago, extra = {}) => row(key, title, ago, {
  agent: 'kirocrew-worker', parent: { slot: 'chat-cond', key: 'chat-cond' }, last_message: 'Running the focused tests.', ...extra,
})

function slots(waiting, aggregate = null) {
  return [
    row('chat-cond', 'Fix Kiro Crew issue 22452', 60, {
      links: [slackLink], code_channel: channel('C0CODE1', 'fix-kirocrew-issue-22452'), running: !aggregate,
      ...(aggregate ? { work_aggregate: aggregate, last_message: 'All three fixes are merged.' } : {}),
    }),
    worker('chat-w1', 'Fix #22452: Bitbucket comment wake', 120, { running: !waiting && !aggregate }),
    worker('chat-w2', 'Fix #22452: GitLab note wake', 180, waiting
      ? { pending_approval: true, running: true, last_message: 'approve git push' }
      : { running: !aggregate }),
    worker('chat-w3', 'Fix #22452: tests and docs', 240),
    row('chat-c2', 'Fix Kiro Crew issue 21964', 3600, { links: [slackLink], code_channel: channel('C0CODE2', 'fix-kirocrew-issue-21964'), last_message: 'No news yet.' }),
    row('chat-plain', 'Plan the Q4 roadmap', 4000, { agent: 'kirocrew' }),
    row('chat-plain-kid', 'Draft the Q4 survey', 4100, { agent: 'kirocrew', parent: { slot: 'chat-plain', key: 'chat-plain' } }),
  ]
}
const FOLDERS = [{ id: FOLDER, name: 'Code channels', order: 0 }]
const detail = { running: false, has_more: false, total: 0, queue: [], messages: [] }
const extra = (path, route) => {
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  return false
}

const failures = []
function check(label, cond, detailText = '') {
  if (!cond) failures.push(`${label}${detailText ? ` (${detailText})` : ''}`)
}
const rowOf = (page, title) => page.locator('.session-row').filter({ has: page.locator('[data-session-title]', { hasText: title }) })

async function load(browser, base, { theme, width, waiting, expanded, storage, aggregate = null }) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, storageState: storage })
  const page = await context.newPage()
  logPageProblems(page)
  // Seeds go through the stub's own init script, which clears storage first
  // unless told to keep it (the reload below keeps the fold the click saved).
  const entries = {
    'mc-active-slot': 'chat-c2', 'mc-privacy-notice-v1': '1', 'mc-session-stale-collapse-ms': '0',
    'mc-sidebar-lane': 'tree', 'mc-sidebar-pinned': 'true', 'mc-sidebar-width': String(width),
    ...(expanded !== null ? { 'mc-sidebar-conductor-expanded': JSON.stringify(expanded) } : {}),
  }
  await stubDashboardApi(page, { slots: slots(waiting, aggregate), folders: FOLDERS, theme, extra, localStorageEntries: entries, preserveStorage: !!storage })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.locator('.session-row').first().waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForTimeout(1500)
  await page.mouse.move(1000, 890)
  return { context, page }
}

async function shootFolder(page, path) {
  const boxes = await page.locator('.session-row').evaluateAll(els => els.map(el => el.getBoundingClientRect().toJSON()))
  const head = await page.getByText('Code channels', { exact: true }).first().boundingBox().catch(() => null)
  const x = Math.max(0, Math.floor(Math.min(...boxes.map(b => b.x)) - 8))
  const y = Math.max(0, Math.floor(Math.min(head ? head.y : Infinity, ...boxes.map(b => b.y)) - 8))
  const right = Math.ceil(Math.max(...boxes.map(b => b.right)) + 8)
  const bottom = Math.ceil(Math.max(...boxes.map(b => b.bottom)) + 8)
  await page.screenshot({ path, clip: { x, y, width: right - x, height: bottom - y } })
}

async function scene(browser, base, theme, width) {
  const where = `${width}px ${theme}`
  // Collapsed (the default) and working.
  {
    const { context, page } = await load(browser, base, { theme, width, waiting: false, expanded: null })
    const cond = rowOf(page, 'Fix Kiro Crew issue 22452')
    check(`${where}: conductor row shows`, await cond.count() === 1)
    for (const w of ['Bitbucket comment wake', 'GitLab note wake', 'tests and docs']) {
      check(`${where}: worker "${w}" folded by default`, await rowOf(page, w).count() === 0)
    }
    const toggle = cond.getByTestId('code-channel-crew-toggle')
    check(`${where}: fold names its 3 workers`, (await toggle.count()) === 1 && (await toggle.getAttribute('aria-label')) === 'Show 3 worker sessions')
    check(`${where}: fold is collapsed`, (await toggle.getAttribute('aria-expanded').catch(() => null)) === 'false')
    const line = cond.getByTestId('code-channel-crew-state')
    check(`${where}: crew line reads "3 working"`, (await line.count()) === 1 && (await line.textContent()) === '3 working')
    check(`${where}: an ordinary parent's child is still listed`, await rowOf(page, 'Draft the Q4 survey').count() === 1)
    check(`${where}: a crew with no workers has no fold`, await rowOf(page, 'Fix Kiro Crew issue 21964').getByTestId('code-channel-crew-toggle').count() === 0)
    if (await toggle.count()) {
      const box = await toggle.boundingBox()
      const rowBox = await cond.boundingBox()
      check(`${where}: fold sits inside its row`, !!box && !!rowBox && box.x >= rowBox.x && box.x + box.width <= rowBox.x + rowBox.width + 0.5)
    }
    await shootFolder(page, join(OUT, `crew-collapsed-${width}px-${theme}.png`))
    await context.close()
  }
  // Needs approval: a hidden worker waits on an approval.
  {
    const { context, page } = await load(browser, base, { theme, width, waiting: true, expanded: null })
    const cond = rowOf(page, 'Fix Kiro Crew issue 22452')
    const line = cond.getByTestId('code-channel-crew-state')
    check(`${where}: a hidden worker's approval surfaces on the conductor row`,
      (await line.count()) === 1 && (await line.textContent()) === 'Needs approval · approve git push',
      (await line.count()) ? await line.textContent() : 'no crew line')
    check(`${where}: the waiting worker itself is still folded`, await rowOf(page, 'GitLab note wake').count() === 0)
    await shootFolder(page, join(OUT, `crew-waiting-${width}px-${theme}.png`))
    await context.close()
  }
  // The optional work_aggregate on the conductor slot: "done" and "stuck",
  // which only the ledger knows.
  for (const [aggregate, text, file] of [
    [{ state: 'done', label: '', counts: { done: 3, total: 3 } }, '3 of 3 done', 'done'],
    [{ state: 'stuck', label: 'tests worker went quiet', counts: { stuck: 1 } }, '1 stuck · tests worker went quiet', 'stuck'],
  ]) {
    const { context, page } = await load(browser, base, { theme, width, waiting: false, expanded: null, aggregate })
    const line = rowOf(page, 'Fix Kiro Crew issue 22452').getByTestId('code-channel-crew-state')
    check(`${where}: aggregate ${file} reads "${text}"`, (await line.count()) === 1 && (await line.textContent()) === text,
      (await line.count()) ? await line.textContent() : 'no crew line')
    await shootFolder(page, join(OUT, `crew-aggregate-${file}-${width}px-${theme}.png`))
    await context.close()
  }
  // Expanded by a click, then persisted across a reload.
  {
    const { context, page } = await load(browser, base, { theme, width, waiting: true, expanded: null })
    const toggle = rowOf(page, 'Fix Kiro Crew issue 22452').getByTestId('code-channel-crew-toggle')
    if (await toggle.count()) {
      await toggle.click()
      await page.waitForTimeout(600)
      await page.mouse.move(1000, 890)
    }
    for (const w of ['Bitbucket comment wake', 'GitLab note wake', 'tests and docs']) {
      check(`${where}: worker "${w}" shows once expanded`, await rowOf(page, w).count() === 1)
      if (await rowOf(page, w).count() !== 1) continue
      // At the narrowest width the worker's agent name gives way first, never the time.
      const fit = await rowOf(page, w).evaluate(el => {
        const line = el.querySelector('.session-agent-label').getBoundingClientRect()
        const time = el.querySelector('[data-testid="session-row-time"]')
        const t = time.getBoundingClientRect()
        return { whole: t.right <= line.right + 0.5 && time.scrollWidth <= time.clientWidth + 0.5, nested: !!el.closest('[data-code-channel-crew]') }
      })
      check(`${where}: worker "${w}" sits in the crew under its conductor`, fit.nested)
      check(`${where}: worker "${w}" keeps its time whole`, fit.whole)
    }
    // One content column (docs/decisions/2026-10-09-folder-glyph-sits-on-its-
    // connector-line.md): a worker's content starts where its conductor's and
    // a sibling row's do; the rail is drawn out of flow and moves nothing.
    const titleX = async title => {
      const r = rowOf(page, title)
      if (await r.count() !== 1) return null
      const t = r.locator('[data-session-title]').first()
      if (await t.count() === 0) return null
      return t.evaluate(el => el.getBoundingClientRect().left)
    }
    const sibling = await titleX('Fix Kiro Crew issue 21964')
    const conductorX = await titleX('Fix Kiro Crew issue 22452')
    for (const w of ['Bitbucket comment wake', 'GitLab note wake', 'tests and docs']) {
      const x = await titleX(w)
      check(`${where}: worker "${w}" content aligns with its sibling rows`,
        x !== null && sibling !== null && conductorX !== null && Math.abs(x - sibling) <= 0.5 && Math.abs(x - conductorX) <= 0.5,
        `worker ${x}, sibling ${sibling}, conductor ${conductorX}`)
    }
    await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur() })
    const state = await context.storageState()
    await shootFolder(page, join(OUT, `crew-expanded-${width}px-${theme}.png`))
    await context.close()
    const again = await load(browser, base, { theme, width, waiting: true, expanded: null, storage: state })
    const stored = await again.page.evaluate(() => localStorage.getItem('mc-sidebar-conductor-expanded'))
    check(`${where}: the open fold persists across a reload`, await rowOf(again.page, 'tests and docs').count() === 1,
      `stored ${stored}; saved ${JSON.stringify(state.origins.map(o => o.localStorage.find(e => e.name === 'mc-sidebar-conductor-expanded')))}`)
    await again.context.close()
  }
}

async function record(browser, base) {
  const size = { width: 1100, height: 480 }
  const dir = join(OUT, 'video')
  mkdirSync(dir, { recursive: true })
  const context = await browser.newContext({ viewport: size, recordVideo: { dir, size } })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots: slots(true), folders: FOLDERS, theme: 'dark', extra,
    localStorageEntries: {
      'mc-active-slot': 'chat-c2', 'mc-privacy-notice-v1': '1', 'mc-session-stale-collapse-ms': '0',
      'mc-sidebar-lane': 'tree', 'mc-sidebar-pinned': 'true', 'mc-sidebar-width': '260',
    },
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.locator('.session-row').first().waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForTimeout(1500)
  const toggle = rowOf(page, 'Fix Kiro Crew issue 22452').getByTestId('code-channel-crew-toggle')
  await toggle.hover()
  await page.waitForTimeout(800)
  await toggle.click() // open
  await rowOf(page, 'tests and docs').waitFor({ state: 'visible' })
  check('record: the fold opens', await rowOf(page, 'tests and docs').count() === 1)
  await page.waitForTimeout(1800)
  await rowOf(page, 'Fix Kiro Crew issue 22452').getByTestId('code-channel-crew-toggle').click() // close
  await page.waitForTimeout(400)
  check('record: the fold closes', await rowOf(page, 'tests and docs').count() === 0)
  await page.mouse.move(1050, 470)
  await page.waitForTimeout(1500)
  const video = page.video()
  await context.close() // flushes the video file
  const src = await video.path()
  const dest = join(OUT, 'crew-fold-open-close-260px-dark.webm')
  renameSync(src, dest)
  console.log('WEBM', dest)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    if (RECORD) await record(browser, base)
    else for (const theme of ['dark', 'light']) for (const width of [260, 180]) await scene(browser, base, theme, width)
  } finally {
    await browser.close()
    srv.close()
  }
  for (const f of failures) console.log(`FAIL ${f}`)
  if (EXPECT_BUG) {
    if (failures.length === 0) {
      console.error('expected the harness to see no fold, but every check passed')
      process.exit(1)
    }
    console.log(`\nOK (expect-bug) — ${failures.length} check(s) failed as expected; screenshots in ${OUT}`)
    return
  }
  if (failures.length) {
    console.error(`\n${failures.length} check(s) failed`)
    process.exit(1)
  }
  console.log(`\nOK — screenshots in ${OUT}`)
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
