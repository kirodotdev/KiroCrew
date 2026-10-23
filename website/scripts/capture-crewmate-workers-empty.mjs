/**
 * Screenshot harness for #23003: the Profile card's Sessions tab when the
 * crewmate has not opened any worker session yet.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every `/api/**` call from fixtures through `stubDashboardApi`. No gateway,
 * no kiro-cli. The fixture puts one crewmate (Scout) on the roster with NO slot
 * carrying `created_by: 'member-scout'`, which is the state the empty box renders.
 *
 * Every frame asserts its own state before it is written, so a frame cannot document
 * the wrong thing:
 *   01-workers-empty-dark     en, dark: the tab is still named Sessions; the box carries the
 *                             mascot art, the lead naming Scout, the one-line body
 *   02-workers-empty-light    en, light: same frame on the light palette (the faded
 *                             worker poses must still read against #fafafa)
 *   03-workers-empty-zh-CN    zh-CN, dark: the Chinese catalog values, same box
 *   04-workers-filled-dark    en, dark: Atlas, who HAS two worker sessions, so the same
 *                             tab lists them as rows under the Sessions label
 *   05-quiet-chat-link-dark   en, dark: Scout's DM, where it has woken but never spoken,
 *                             so the pane's hint and its link to the Sessions tab show
 *
 * Usage: node scripts/capture-crewmate-workers-empty.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'crew23003-shots')
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const iso = (secsAgo) => new Date((now - secsAgo) * 1000).toISOString()
const member = (name, extra = {}) => ({
  name, slug: name, bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: `member-${name}`,
  memory_version: 2, memory_owner: name, model: '', source: 'kirocrew', ...extra,
})
const MEMBERS = [
  member('scout', { display_name: 'Scout', kiro_agent: 'research', last_active_ts: now - 5400, last_message: 'Same conclusion as our #3202.' }),
  member('atlas', { display_name: 'Atlas', kiro_agent: 'backend', last_active_ts: now - 1500, last_message: 'The work dirs moved under one folder now.' }),
]
const AGENTS = MEMBERS.map((m) => ({
  name: m.name, display_name: m.display_name, kiro_agent: m.kiro_agent, workspace: m.workspace,
  memory_store: m.memory_store, model: m.model, source: 'kirocrew',
  description: m.name === 'scout'
    ? 'Research crewmate. Reads papers, repos and docs, then writes short briefs with sources.'
    : 'Backend crewmate. Owns the gateway, the ACP client and the session store.',
}))
// Nothing here names `created_by: 'member-scout'`, so Scout's Sessions tab has
// nothing to list; Atlas opened two workers, so its tab shows the filled shape.
const SLOTS = [
  { key: 'member-scout', title: 'Scout', mode: 'member', created: iso(86400), last_ts: iso(5400), running: false, project: '/srv/repos/scout', agent: 'scout', messages: 3 },
  { key: 'member-atlas', title: 'Atlas', mode: 'member', created: iso(86400), last_ts: iso(1500), running: false, project: '/srv/repos/atlas', agent: 'atlas' },
  { key: 'w-gateway', title: 'Move the session store behind one lock', created_by: 'member-atlas', created: iso(2400), last_ts: iso(90), running: true, project: '/srv/repos/atlas' },
  { key: 'w-acp', title: 'Retry the ACP handshake on EAGAIN', created_by: 'member-atlas', created: iso(3000), last_ts: iso(1300), running: false, project: '/srv/repos/atlas' },
]

// The mate greeting partitions the crew board's items; an empty board, in the
// shape the page iterates, keeps that read off the error boundary.
const CREW_BOARD = JSON.parse(readFileSync(new URL('./fixtures/crew-board-empty.json', import.meta.url), 'utf8'))

const { srv, base } = await serveDist()
const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

const SCOUT_MACHINERY = [
  { role: 'nudge', content: '[auto-nudge cycle 1]\nPatrol.', cls: '', ts: iso(5460) },
  { role: 'tool', content: '🔧 gh issue list --label needs-triage', cls: '', ts: iso(5455) },
  { role: 'assistant', content: '\u200b', cls: '', ts: iso(5450) },
]
// Fixed answers by exact path. Every one is an empty-but-well-shaped body:
// this harness is about the Sessions tab, so nothing else on the page has
// anything to show, and each shape is the one its reader iterates.
const EXACT = {
  '/api/members': { members: MEMBERS, default_agent: 'kirocrew' },
  '/api/agents': { agents: AGENTS, default_agent: 'kirocrew' },
  '/api/agents/resolved-model': { model: '', resolved: '' },
  '/api/default-agent': { default_agent: 'kirocrew' },
  '/api/crons': { jobs: [] },
  '/api/cron-folders': [],
  '/api/autonudge': { enabled: true, loops: [] },
  '/api/monitors': { enabled: true, monitors: [] },
  '/api/teams': { teams: [] },
  '/api/crew-board': CREW_BOARD,
}
// Per-member reads, keyed on the path's last segment after `/api/members/<slug>/`.
const PER_MEMBER = {
  activity: () => ({ slug: '', member: '', capped: false, entries: [] }),
  goals: () => ({ goals: [] }),
  briefing: () => ({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false }),
  panel: () => ({ panel: null, html: null }),
  thread: (slug) => ({ slot_key: `member-${slug}`, slug, member: slug, created: false }),
}
const extra = async (path, route) => {
  if (path in EXACT) { await json(route, EXACT[path]); return true }
  const member = path.match(/^\/api\/members\/([^/]+)\/([a-z]+)$/)
  if (member && member[2] in PER_MEMBER) { await json(route, PER_MEMBER[member[2]](decodeURIComponent(member[1]))); return true }
  if (/^\/api\/autonudge\/slot\//.test(path)) { await json(route, { enabled: true, loop: null }); return true }
  if (/^\/api\/monitors\/slot\//.test(path)) { await json(route, { enabled: true, monitor: null }); return true }
  const slot = path.match(/^\/api\/chat\/slots\/([^/]+)$/)
  if (slot) {
    const key = decodeURIComponent(slot[1])
    const found = SLOTS.find((x) => x.key === key)
    // Scout has woken once (a patrol turn: nudge, tool, empty reply) and never
    // spoken, which is the state the pane's quiet hint and its link render in.
    const rows = key === 'member-scout' ? SCOUT_MACHINERY : []
    await json(route, found ? { ...found, messages: rows, running: false, has_more: false, total: rows.length } : {}, found ? 200 : 404)
    return true
  }
  return false
}

async function openWorkersTab(theme, lang, name = 'scout') {
  const context = await browser.newContext({ viewport: { width: 1500, height: 940 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await context.newPage()
  logPageProblems(page)
  const storage = { 'mc-lang': lang, 'mc-crewmates-onboarded': '1', 'mc-crewmates-page-entered': '1', 'mc-nav': '1', 'mc-members-panel-open': '0' }
  await stubDashboardApi(page, { theme, extra, slots: SLOTS, localStorageEntries: storage })
  await page.goto(`${base}/members?member=${name}`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('member-identity-pill').waitFor({ state: 'visible', timeout: 30000 })
  await page.getByTestId('member-identity-pill').click()
  const card = page.getByTestId('crew-profile-card')
  await card.waitFor({ state: 'visible', timeout: 20000 })
  // The card opens on Sessions; its body is the proof the fixture worked.
  const body = page.getByTestId(name === 'scout' ? 'crew-profile-sessions-empty' : 'crew-profile-driving')
  await body.waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(500)
  return { context, page, card, empty: body }
}

async function frame(file, theme, lang, expect) {
  const { context, page, card, empty } = await openWorkersTab(theme, lang)
  const rail = page.getByTestId('crew-profile-tabs')
  const selected = await rail.locator('[role="tab"][aria-selected="true"]').getAttribute('aria-label')
  const selectedText = (await rail.locator('[role="tab"][aria-selected="true"]').innerText()).trim()
  const tabWord = selected || selectedText
  check(`${file}: the selected tab keeps its name ${JSON.stringify(expect.tab)}`, tabWord.includes(expect.tab), tabWord)
  const text = (await empty.innerText()).replace(/\s+/g, ' ').trim()
  check(`${file}: the lead names the crewmate`, text.includes(expect.lead), text)
  check(`${file}: the body says the list fills here`, text.includes(expect.body), text)
  check(`${file}: no internal noun leaks into the box`, !/session|chat|driv|会话|聊天|驱动/i.test(text), text)
  check(`${file}: three mascot poses are drawn`, (await empty.locator('img').count()) === 3)
  check(`${file}: two of them are the faded workers`, (await empty.locator('img.opacity-30').count()) === 2)
  await card.screenshot({ path: join(OUT, `${file}.png`) })
  console.log('wrote', join(OUT, `${file}.png`))
  await context.close()
}

await frame('01-workers-empty-dark', 'dark', 'en', {
  tab: 'Sessions',
  lead: 'When a big job comes in, Scout brings in a few workers to share it.',
  body: 'The workers will show up here. Right now it is on its own.',
})
await frame('02-workers-empty-light', 'light', 'en', {
  tab: 'Sessions',
  lead: 'When a big job comes in, Scout brings in a few workers to share it.',
  body: 'The workers will show up here. Right now it is on its own.',
})
await frame('03-workers-empty-zh-CN', 'dark', 'zh-CN', {
  tab: '会话',
  lead: '活大的时候，Scout 会带几个帮手一起干。',
  body: '帮手会在这里出现。现在只有它自己。',
})

// Frame 04: the same tab with workers in it, so the rows are seen beside
// the empty box's wording.
{
  const { context, page, card, empty: list } = await openWorkersTab('dark', 'en', 'atlas')
  const rail = page.getByTestId('crew-profile-tabs')
  const tabWord = (await rail.locator('[role="tab"][aria-selected="true"]').innerText()).trim()
  check('04-workers-filled-dark: the selected tab is named "Sessions"', tabWord.includes('Sessions'), tabWord)
  const rows = list.getByTestId('crew-profile-driving-row')
  check('04-workers-filled-dark: both of Atlas\'s workers are listed', (await rows.count()) === 2, String(await rows.count()))
  check('04-workers-filled-dark: no empty box beside the list', (await page.getByTestId('crew-profile-sessions-empty').count()) === 0)
  await card.screenshot({ path: join(OUT, '04-workers-filled-dark.png') })
  console.log('wrote', join(OUT, '04-workers-filled-dark.png'))
  await context.close()
}

// Frame 05: the quiet-chat hint. Scout has woken but never said anything, so
// the DM pane says so and offers the link that opens the Sessions tab; its
// wording must not claim a worker exists, since the tab may be the empty box.
{
  const context = await browser.newContext({ viewport: { width: 1500, height: 940 }, deviceScaleFactor: 2, colorScheme: 'dark' })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { theme: 'dark', extra, slots: SLOTS, localStorageEntries: { 'mc-lang': 'en', 'mc-crewmates-onboarded': '1', 'mc-crewmates-page-entered': '1', 'mc-nav': '1', 'mc-members-panel-open': '0' } })
  await page.goto(`${base}/members?member=scout`, { waitUntil: 'domcontentloaded' })
  const where = page.getByTestId('crewmate-quiet-where')
  await where.waitFor({ state: 'visible', timeout: 30000 })
  await page.waitForTimeout(500)
  const hint = page.getByTestId('crewmate-quiet-hint')
  const text = (await hint.innerText()).replace(/\s+/g, ' ').trim()
  check('05-quiet-chat-link-dark: the hint names the quiet crewmate', text.includes("Scout hasn't said anything to you yet."), text)
  check('05-quiet-chat-link-dark: the link is neutral about workers existing', text.includes('See what it is working on.') && !/brought in|its workers/.test(text), text)
  check('05-quiet-chat-link-dark: the link is a real button', (await where.evaluate((el) => el.tagName)) === 'BUTTON')
  await page.getByTestId('member-thread-header').waitFor({ state: 'visible', timeout: 20000 })
  const header = await page.getByTestId('member-thread-header').boundingBox()
  const box = await hint.boundingBox()
  await page.screenshot({ path: join(OUT, '05-quiet-chat-link-dark.png'), clip: { x: header.x, y: header.y, width: header.width, height: box.y + box.height + 24 - header.y } })
  console.log('wrote', join(OUT, '05-quiet-chat-link-dark.png'))
  await context.close()
}

await browser.close()
srv.close()
if (failed) { console.error('one or more frames did not match their expected state'); process.exit(1) }
