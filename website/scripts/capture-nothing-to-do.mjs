/**
 * Evidence harness for `nothing_to_do`: how a quiet patrol turn reads
 * on the Sessions page and on the Crewmate page, before and after.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures — no gateway, no token, no agent. The fixture rows are the rows the
 * backend persists for such a turn: a shell tool row, then the
 * `@kirocrew-core/nothing_to_do` tool row whose `meta.output` is the applier's
 * quiet line (`QUIET_END_OUTCOME_PREFIX` + the model's note). The "before" rows
 * are what main writes for the same turn with no directive: the two
 * empty-response notice cards (`chat_runner` continue + give-up rungs).
 *
 * Usage: node scripts/capture-nothing-to-do.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { chromium } from 'playwright'

import { openTranscriptHarness } from './lib/transcript-harness.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { json } from './lib/boot-api.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

delete process.env.LD_LIBRARY_PATH

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'nothing-to-do-shots')
mkdirSync(OUT, { recursive: true })

const PROJECT = '/home/user/workspace/KiroCrew'
const QUIET_LINE = 'Nothing new to report. patrol: no new activity on the watched PRs'
const NOTICE_CONTINUE = 'ℹ️ The turn ended without a closing reply — continuing once from what already ran.'
const NOTICE_GIVE_UP =
  'ℹ️ The turn ended without a closing reply. Send a message to continue from where it stopped — completed steps will not re-run.'

const t0 = Math.floor(Date.now() / 1000) - 600

/** The rows a patrol turn persists up to the point where it has nothing to say. */
function patrolWork(ts, opener) {
  return [
    opener,
    {
      role: 'tool',
      ts: ts + 4,
      content: '🔧 gh pr view 16351 --json reviews,commits,statusCheckRollup',
      cls: 'msg msg-tool',
      meta: {
        tool_call_id: 'tc-check',
        kind: 'execute',
        purpose: 'Check the two watched PRs for new activity',
        input: 'gh pr view 16351 --json reviews,commits,statusCheckRollup',
        output: 'reviews: 0 new\ncommits: 0 new\nchecks: 86/86 success (unchanged since last cycle)',
      },
    },
  ]
}

/** AFTER: the turn ends on the directive — one more tool row, nothing else. */
function afterRows(ts, opener) {
  return [
    ...patrolWork(ts, opener),
    {
      role: 'tool',
      ts: ts + 9,
      content: '🔧 @kirocrew-core/nothing_to_do',
      cls: 'msg msg-tool',
      meta: {
        tool_call_id: 'tc-quiet',
        kind: 'other',
        tool_name: 'nothing_to_do',
        mcp_server: 'kirocrew-core',
        purpose: 'Nothing to report this cycle',
        input: '{"note": "patrol: no new activity on the watched PRs"}',
        output: QUIET_LINE,
        // Stamped by the runner only when the applier ended the turn.
        ends_turn: true,
      },
    },
  ]
}

/** BEFORE (main): the same turn stops bare; the runner posts its two cards. */
function beforeRows(ts, opener) {
  return [
    ...patrolWork(ts, opener),
    { role: 'notice', ts: ts + 10, content: NOTICE_CONTINUE, cls: 'msg msg-info' },
    { role: 'notice', ts: ts + 24, content: NOTICE_GIVE_UP, cls: 'msg msg-info' },
  ]
}

/** AFTER, no agent-supplied purpose: the pill falls back to the declared MCP
 *  title (`mcp_tool_titles.json`), which is how the row ships when the model
 *  gives the call no purpose line. */
function afterRowsDeclaredTitle(ts, opener) {
  const rows = afterRows(ts, opener)
  const quiet = rows[rows.length - 1]
  const { purpose: _dropped, ...meta } = quiet.meta
  return [...rows.slice(0, -1), { ...quiet, meta }]
}

/** REFUSED: a PERSON opened the turn and the model still called the directive.
 *  The applier refuses (`QUIET_END_REFUSED_USER_TURN`), the row carries no
 *  `ends_turn`, and the runner's ladder runs as before: continue card, give-up
 *  card (both tagged `empty_turn`), and the composer offers Resume. */
const REFUSED_LINE =
  'Error: nothing_to_do was not applied — a person opened this turn, so it owes them a reply. Answer in text (even one line) instead.'
function refusedRows(ts, opener) {
  return [
    opener,
    {
      role: 'tool',
      ts: ts + 4,
      content: '🔧 @kirocrew-core/nothing_to_do',
      cls: 'msg msg-tool',
      meta: {
        tool_call_id: 'tc-quiet',
        kind: 'other',
        tool_name: 'nothing_to_do',
        mcp_server: 'kirocrew-core',
        input: '{"note": "nothing to add"}',
        output: REFUSED_LINE,
      },
    },
    { role: 'notice', ts: ts + 10, content: NOTICE_CONTINUE, cls: 'msg msg-info', meta: { kind: 'empty_turn' } },
    { role: 'notice', ts: ts + 24, content: NOTICE_GIVE_UP, cls: 'msg msg-info', meta: { kind: 'empty_turn' } },
  ]
}

const userOpener = (ts) => ({ role: 'user', ts, content: 'Patrol: anything new on the two watched PRs since the last cycle?' })

// ── Sessions page ────────────────────────────────────────────────────────────

async function sessionsShot(name, rows, { title, lastMessage }) {
  const SLOT = `chat-${name}`
  const slots = [{
    key: SLOT, title, running: false, last_message: lastMessage, messages: rows.length,
    agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT,
    modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
  }]
  const detail = { running: false, has_more: false, total: rows.length, queue: [], project: PROJECT, messages: rows }
  const { page, load, close } = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots, detail, viewport: { width: 1280, height: 760 },
  })
  await load('dark', { selector: '[data-chat-pane], main', settle: 1200 })
  await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)
  // Expand the tool rows so the frame shows what each one carries.
  // Expand the LAST tool pill (the directive's row) so the frame shows the
  // declared title and the applied quiet line it carries; the shell row stays
  // folded so the frame reads as a transcript, not a dump.
  const pills = page.locator('[data-testid="tool-pill-label"]')
  const n = await pills.count()
  if (n) {
    await pills.nth(n - 1).evaluate(el => { el.scrollIntoView({ block: 'center' }); el.click() })
    await page.waitForTimeout(700)
  }
  await page.mouse.move(0, 0)
  await page.waitForTimeout(500)
  const text = await page.locator('body').innerText()
  if (process.env.DUMP) console.log('---BODY---\n' + text.slice(0, 3000) + '\n---END---')
  console.log(`[sessions/${name}] has quiet line: ${text.includes('Nothing new to report')}; has refusal: ${text.includes('was not applied')}; has notice: ${text.includes('without a closing reply')}; has title: ${text.includes('Nothing to report')}; offers resume: ${/\bResume\b/.test(text)}`)
  const file = join(OUT, `sessions-${name}.png`)
  await page.screenshot({ path: file })
  console.log('wrote', file)
  await close()
}

if (!process.env.SKIP_SESSIONS) await sessionsShot('after', afterRows(t0, userOpener(t0)), {
  title: 'Patrol the watched PRs', lastMessage: 'Patrol: anything new on the two watched PRs since the last cycle?',
})
if (!process.env.SKIP_SESSIONS) await sessionsShot('before', beforeRows(t0, userOpener(t0)), {
  title: 'Patrol the watched PRs', lastMessage: NOTICE_GIVE_UP,
})
if (!process.env.SKIP_SESSIONS) await sessionsShot('after-declared-title', afterRowsDeclaredTitle(t0, userOpener(t0)), {
  title: 'Patrol the watched PRs', lastMessage: 'Patrol: anything new on the two watched PRs since the last cycle?',
})
if (!process.env.SKIP_SESSIONS) await sessionsShot('refused-person-turn', refusedRows(t0, { role: 'user', ts: t0, content: 'What did the two watched PRs do today?' }), {
  title: 'Patrol the watched PRs', lastMessage: NOTICE_GIVE_UP,
})

// ── Crewmate page ────────────────────────────────────────────────────────────

const member = (name, extra = {}) => ({
  name, slug: name, bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: `member-${name}`, model: '', ...extra,
})
const MEMBERS = [
  member('radar', { last_active_ts: t0 + 9, last_message: 'Both PRs are green; #16351 is waiting on your review.' }),
  member('scribe', { last_active_ts: t0 - 5400, last_message: 'Weekly summary filed.' }),
]

/** The crewmate thread: an earlier exchange, then a monitor wake that found nothing. */
function crewmateRows(tail) {
  const wake = {
    role: 'nudge', ts: t0,
    content: '[auto-nudge cycle 7] Patrol the two watched PRs. Report only real signals; if nothing changed, call nothing_to_do.',
  }
  return [
    { role: 'user', ts: t0 - 1800, content: 'How are the two PRs doing?', },
    { role: 'assistant', ts: t0 - 1790, content: 'Both PRs are green; #16351 is waiting on your review.' },
    ...tail(t0, wake),
  ]
}

async function crewmateShot(name, rows) {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1440, height: 820 }, deviceScaleFactor: 1, colorScheme: 'dark' })
  const page = await context.newPage()
  logPageProblems(page)
  const detail = { key: 'member-radar', title: 'radar', running: false, has_more: false, total: rows.length, queue: [], messages: rows }
  const extra = async (path, route) => {
    // The first-run Meet CrewMates dialog gates on this flag; a patrol frame is
    // about an existing crew, so the workspace has already met them.
    if (path === '/api/theme/boot') { await json(route, { mode: 'dark', theme: '', crewmates_onboarded: true, onboarded: true }); return true }
    if (path === '/api/members') { await json(route, { members: MEMBERS, default_agent: 'kirocrew' }); return true }
    if (path === '/api/crons') { await json(route, { jobs: [] }); return true }
    if (path === '/api/cron-folders') { await json(route, []); return true }
    if (path === '/api/default-agent') { await json(route, { default_agent: 'kirocrew' }); return true }
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1])
      await json(route, { slot_key: `member-${slug}`, slug, member: slug, created: false })
      return true
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) { await json(route, { slug: 'radar', member: 'radar', capped: false, entries: [] }); return true }
    if (/^\/api\/members\/[^/]+\/briefing$/.test(path)) { await json(route, { slug: 'radar', member: 'radar', supported: true, text: '', updated_ts: null, redacted: false, truncated: false }); return true }
    if (/^\/api\/members\/[^/]+\/panel$/.test(path)) { await json(route, { panel: null, html: null }); return true }
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) { await json(route, detail); return true }
    if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [] }); return true }
    if (path === '/api/teams') { await json(route, { teams: [] }); return true }
    return false
  }
  await stubDashboardApi(page, { theme: 'dark', extra, localStorageEntries: { 'mc-lang': 'en' } })
  await page.goto(`${base}/members?member=radar`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('member-roster').waitFor({ state: 'visible', timeout: 30000 })
  // Dispatched on the node: the roster row animates on mount, so Playwright's
  // stability wait can outlast the timeout while the React onClick is fine.
  await page.locator('#main-content li button', { hasText: 'radar' }).first().evaluate(el => el.click())
  // Text presence, not Playwright visibility: the crewmate bubble fades in on
  // mount and the visibility wait can outlast the timeout on the fade.
  await page.waitForFunction(() => document.body.innerText.includes('Both PRs are green'), null, { timeout: 20000 })
  await page.waitForTimeout(1200)
  const text = await page.locator('body').innerText()
  console.log(`[crewmate/${name}] shows notice card: ${text.includes('without a closing reply')}; shows tool row: ${text.includes('nothing_to_do') || text.includes('Nothing to report')}; shows earlier reply: ${text.includes('Both PRs are green')}`)
  const file = join(OUT, `crewmate-${name}.png`)
  await page.screenshot({ path: file })
  console.log('wrote', file)
  await context.close()
  await browser.close()
  srv.close()
}

if (!process.env.SKIP_CREWMATE) await crewmateShot('after', crewmateRows(afterRows))
if (!process.env.SKIP_CREWMATE) await crewmateShot('before', crewmateRows(beforeRows))
