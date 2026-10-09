/**
 * Screenshot harness for the crewmate-panel IA on the Crewmates page:
 *
 *   - the roster column is folded into ONE header chip (stacked faces + count);
 *     open, it is a searchable list that switches the thread;
 *   - the identity pill opens the crewmate's PROFILE CARD — the panel's new
 *     "Who am I" tab — whose pencil is the editor door and whose tiles/rows lead
 *     to Files, Notes, Schedules, Work log and Dashboard.
 *
 * Runs the REAL built SPA behind `serveDist` with every `/api/**` answered from
 * fixtures (`stubDashboardApi`): no gateway, no auth, no kiro-cli.
 *
 * Usage: node scripts/capture-crewmate-panel-ia.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'crewmate-panel-ia')
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const member = (name, extra = {}) => ({
  name, slug: name, bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: 'kirocrew', workspace: `/srv/repos/${name}`, memory_store: `member-${name}`,
  memory_version: 2, memory_owner: name, model: '', source: 'kirocrew', ...extra,
})
const MEMBERS = [
  member('kiro', { display_name: 'Kiro', kiro_agent: 'frontend', running: true, last_active_ts: now - 60, last_message: 'All 85 checks are green. Only human review is left.' }),
  member('atlas', { display_name: 'Atlas', kiro_agent: 'backend', last_active_ts: now - 1500, last_message: 'The work dirs moved under one folder now.' }),
  member('scout', { display_name: 'Scout', kiro_agent: 'research', last_active_ts: now - 5400, last_message: 'Same conclusion as our #3202.' }),
  member('pixel', { display_name: 'Pixel', kiro_agent: 'design', last_active_ts: now - 9000, last_message: 'Three looks. Which one?' }),
  member('ops', { display_name: 'Ops', kiro_agent: 'infra', last_active_ts: now - 20000, last_message: 'Nightly signing passed in 14 min.' }),
  member('scribe', { display_name: 'Scribe', kiro_agent: 'docs', last_active_ts: now - 90000, last_message: 'Spec updated in the same commit.' }),
]
const DESC = {
  kiro: 'Frontend crewmate for the Kiro Crew dashboard. Owns the chat surface, the members page and the Glass design language. Reviews every UI PR from a new-user point of view before it ships, and never merges.',
  atlas: 'Backend crewmate. Owns the gateway, the ACP client and the session store. Keeps the test suite green and the startup path fast.',
  scout: 'Research crewmate. Reads papers, repos and docs, then writes short briefs with sources. Never pushes code.',
  pixel: 'Design crewmate. Turns ideas into mockups rendered with the real theme tokens, and asks you to choose before any code.',
  ops: 'Infra crewmate. Watches CI, nightly builds and signing. Opens an issue when a lane goes red twice.',
  scribe: 'Docs crewmate. Keeps system specs in step with the code and lints every doc move.',
}
const AGENTS = MEMBERS.map((m) => ({
  name: m.name, display_name: m.display_name, kiro_agent: m.kiro_agent, workspace: m.workspace,
  memory_store: m.memory_store, model: m.model, description: DESC[m.name], source: 'kirocrew',
}))
/** Sessions this crewmate opened and steers (`created_by` = its DM slot). */
const iso = (secsAgo) => new Date((now - secsAgo) * 1000).toISOString()
const SLOTS = [
  { key: 'member-kiro', title: 'Kiro', mode: 'member', created: iso(86400), last_ts: iso(60), running: true, project: '/srv/repos/kiro', agent: 'kiro' },
  // Pixel is parked on an approval (needs you); Scout has an unread reply.
  { key: 'member-pixel', title: 'Pixel', mode: 'member', created: iso(86400), last_ts: iso(400), running: false, pending_approval: true, project: '/srv/repos/pixel', agent: 'pixel' },
  { key: 'member-scout', title: 'Scout', mode: 'member', created: iso(86400), last_ts: iso(900), running: false, project: '/srv/repos/scout', agent: 'scout' },
  { key: 'w-capture', title: 'Capture Welcome screenshots at 390px', created_by: 'member-kiro', created: iso(900), last_ts: iso(40), running: true, project: '/srv/repos/kiro' },
  { key: 'w-review', title: 'Review PR #15731 as a new user', created_by: 'member-kiro', created: iso(1500), last_ts: iso(120), running: true, project: '/srv/repos/kiro' },
  { key: 'w-approve', title: 'Rebase topbar-glass onto main', created_by: 'member-kiro', created: iso(3000), last_ts: iso(300), running: true, project: '/srv/repos/kiro' },
  { key: 'w-ask', title: 'Pick the mates button look', created_by: 'member-kiro', created: iso(5000), last_ts: iso(700), running: false, project: '/srv/repos/kiro' },
  { key: 'w-done', title: 'Explain the panel IA from the sketch', created_by: 'member-kiro', created: iso(9000), last_ts: iso(5400), running: false, project: '/srv/repos/kiro' },
]
/** The crewmate's published view: a campaign dashboard it wrote itself (HTML). */
const REPORT_HTML = readFileSync(new URL('./fixtures/crewmate-progress-report.html', import.meta.url), 'utf8')
const PANEL = { template: 'custom', title: 'Campaign dashboard', crew: 'kiro', published_at: new Date((now - 480) * 1000).toISOString(), data: {}, docked_height: 1180 }
/** One goal with two tracking notes, seeded into the browser (goals are local for now). */
/** Browser state every context starts from: English, onboarding done, nav rail
 *  collapsed, the side panel CLOSED (the flow starts from the chat alone), one
 *  seeded goal. */
const STORAGE = { 'mc-lang': 'en', 'mc-nav': '1', 'mc-members-panel-open': '0', 'mc-unread-shared': JSON.stringify({ 'member-scout': '' }) }

const JOBS = [
  { id: 'cron-digest', name: 'Daily digest', message: 'Summarise what moved.', enabled: true, schedule: 'At 9:00 AM UTC', agent: 'kirocrew', member_id: 'kiro', last_run_ts: now - 10800, next_run_ts: now + 75600, last_status: 'ok' },
  { id: 'cron-babysit', name: 'PR babysit', message: 'Watch the open PR.', enabled: false, schedule: 'every 5m', agent: 'kirocrew', member_id: 'kiro', last_run_ts: now - 540000, last_status: 'ok' },
]

const seenTreeDirs = []
const { srv, base } = await serveDist()
const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

const extra = async (path, route) => {
  if (path === '/api/members') { await json(route, { members: MEMBERS, default_agent: 'kirocrew' }); return true }
  if (path === '/api/agents') { await json(route, { agents: AGENTS, default_agent: 'kiro' }); return true }
  if (path === '/api/crons') { await json(route, { jobs: JOBS }); return true }
  if (path === '/api/cron-folders') { await json(route, []); return true }
  if (path === '/api/default-agent') { await json(route, { default_agent: 'kirocrew' }); return true }
  const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
  if (thread) {
    const slug = decodeURIComponent(thread[1])
    await json(route, { slot_key: `member-${slug}`, slug, member: slug, created: false })
    return true
  }
  if (/^\/api\/members\/[^/]+\/activity$/.test(path)) { await json(route, { slug: '', member: '', capped: false, entries: [] }); return true }
  if (/^\/api\/members\/[^/]+\/briefing$/.test(path)) {
    await json(route, { slug: '', member: '', supported: true, text: '# Briefing\n\n- Hold the push until the CI round is terminal.\n- PR screenshots go under .github/screenshots.\n- Max two buttons per row; the rest in a menu.\n', updated_ts: now - 3600, redacted: false, truncated: false })
    return true
  }
  if (/^\/api\/members\/kiro\/panel$/.test(path)) { await json(route, { panel: PANEL, html: REPORT_HTML }); return true }
  if (/^\/api\/members\/[^/]+\/panel$/.test(path)) { await json(route, { panel: null, html: null }); return true }
  // The Files view, scoped to the crewmate's WORKSPACE folder: the tree it asks
  // for is the fixture below, keyed on the path the request names.
  if (path === '/api/project/tree') {
    const dir = new URL(route.request().url()).searchParams.get('path') || ''
    seenTreeDirs.push(dir)
    const root = dir || '/srv/repos/kiro'
    const paths = ['website/src/pages/members/MembersPage.tsx', 'website/src/pages/members/CrewProfilePanel.tsx', 'website/src/pages/members/CrewmateSwitcher.tsx', 'website/src/pages/members/pillActivity.ts', 'website/src/components/crew/CrewAvatarButton.tsx', 'docs/system-specs/modules/crew-mode.md', 'docs/system-specs/modules/subagent.md', 'notes.md', 'README.md']
    await json(route, { root, paths, directories: ['website', 'website/src', 'website/src/pages', 'website/src/pages/members', 'website/src/components', 'website/src/components/crew', 'docs', 'docs/system-specs', 'docs/system-specs/modules'], repo: true })
    return true
  }
  if (path === '/api/project/git/status') { await json(route, { repo: true, branch: 'feat/crewmate-panel-ia', ahead: 0, behind: 0, files: [{ path: 'website/src/pages/members/MembersPage.tsx', status: 'M', staged: false }] }); return true }
  // The published view's sandboxed document: the gateway mints a one-shot URL
  // for the posted srcdoc; here the document itself is handed back as a data URL.
  if (path === '/api/sandbox-doc') {
    const body = JSON.parse(route.request().postData() || '{}')
    await json(route, { url: 'data:text/html;base64,' + Buffer.from(String(body.html || ''), 'utf8').toString('base64') })
    return true
  }
  if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [] }); return true }
  if (path === '/api/teams') { await json(route, { teams: [] }); return true }
  return false
}

async function open(theme, name, viewport = { width: 1500, height: 940 }) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2, colorScheme: theme })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { theme, extra, slots: SLOTS, localStorageEntries: STORAGE })
  await page.goto(`${base}/members?member=${name}`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('member-identity-pill').waitFor({ state: 'visible', timeout: 30000 })
  await page.waitForTimeout(600)
  return { context, page }
}


const stripTabs = (page) => page.getByTestId('side-panel-root').locator('.side-panel-strip [role="tab"], .side-panel-strip button[data-testid^="side-panel-leading-tab-"]')


const rail = (page) => page.getByTestId('crew-profile-tabs')
const visibleWords = (page) => rail(page).evaluate(el => [...el.querySelectorAll('[role="tab"] span:not(.sr-only)')].map(x => x.textContent.trim()).filter(Boolean).join(' '))

/** Stills, light, desktop. The flow starts from the chat alone. */
{
  const { context, page } = await open('light', 'kiro')
  const pill = page.getByTestId('member-identity-pill')
  const panel = page.getByTestId('side-panel-root')
  check('roster column is gone while a thread is open', !(await page.getByTestId('member-roster').isVisible()))
  check('the page opens on the chat alone (side panel closed)', (await panel.count()) === 0 || !(await panel.isVisible()))
  check('switcher chip shows the count', (await page.getByTestId('crewmate-switcher-count').innerText()).trim() === '6')
  const threadBefore = (await page.getByTestId('member-thread-header').boundingBox()).width
  await page.mouse.move(5, 5)
  await page.screenshot({ path: join(OUT, '01-chat-only-light.png') })

  // 02: pill → the card takes its own column; the thread narrows; the pill steps out.
  await pill.click()
  const card = page.getByTestId('crew-profile-card')
  await card.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(700)
  const threadAfter = (await page.getByTestId('member-thread-header').boundingBox()).width
  check(`the card takes its own column and the thread narrows (${Math.round(threadBefore)} → ${Math.round(threadAfter)})`, (await page.getByTestId('crew-profile-docked').count()) === 1 && threadAfter < threadBefore - 300)
  check('the pill steps out while the card holds the column', (await pill.count()) === 0)
  check('exactly one face is on screen (the card head)', (await page.getByTestId('crew-profile-face').count()) === 1)
  check('card names the crewmate', (await page.getByTestId('crew-profile-name').innerText()).trim() === 'Kiro')
  check('card has four tabs', (await rail(page).locator('[role="tab"]').count()) === 4)
  check('only the selected tab shows its word', (await visibleWords(page)) === 'Profile', await visibleWords(page))
  const cb = await card.boundingBox()
  check(`card is a third of the row (${Math.round(cb.width)}px of 1500)`, cb.width >= 360 && cb.width <= 560)
  await page.screenshot({ path: join(OUT, '02-profile-card-light.png') })
  await card.screenshot({ path: join(OUT, '02b-card-profile-light.png') })

  // 02c: Read more → the pushed About page (full description + Edit crewmate).
  await page.getByTestId('crew-profile-read-more').click()
  await page.getByTestId('crew-profile-page-about').waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(500)
  check('About page shows the full description', (await page.getByTestId('crew-profile-about-full').count()) === 1)
  await card.screenshot({ path: join(OUT, '02c-card-about-pushed-light.png') })
  await page.getByTestId('crew-profile-back').click()
  await page.waitForTimeout(400)

  // 03: Schedules.
  await rail(page).getByRole('tab', { name: 'Schedules' }).click()
  await page.getByTestId('crew-schedule-list').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(500)
  check('schedule tab lists two schedules', (await page.getByTestId('crew-schedule-row').count()) === 2)
  await card.screenshot({ path: join(OUT, '03-card-schedule-light.png') })
  await page.getByTestId('crew-schedule-create').click()
  await page.getByTestId('crew-profile-page-new-schedule').waitFor({ state: 'visible', timeout: 10000 })
  await page.getByTestId('crew-wake-section').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(500)
  await card.screenshot({ path: join(OUT, '03b-card-new-schedule-pushed-light.png') })
  await page.getByTestId('crew-profile-back').click()
  await page.waitForTimeout(400)

  // 04: Sessions.
  await rail(page).getByRole('tab', { name: 'Sessions' }).click()
  await page.getByTestId('crew-profile-sessions').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(500)
  check('sessions tab lists the five driven sessions', (await page.getByTestId('crew-profile-driving-row').count()) === 5)
  await card.screenshot({ path: join(OUT, '04-card-sessions-light.png') })

  // 05: Goals, coming soon.
  await rail(page).getByRole('tab', { name: 'Goals' }).click()
  await page.getByTestId('crew-profile-goals-soon').waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(300)
  check('goals tab is a coming-soon placeholder', (await page.getByTestId('crew-goal-input').count()) === 0)
  await card.screenshot({ path: join(OUT, '05-card-goals-light.png') })

  check('there is no Computer tab', (await rail(page).getByRole('tab', { name: 'Computer' }).count()) === 0)

  // 07: Profile → Notes pushed page.
  await rail(page).getByRole('tab', { name: 'Profile' }).click()
  await page.getByTestId('crew-profile-notes').click()
  await page.getByTestId('crew-profile-page-notes').waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(600)
  check('notes page has one back control', (await page.getByTestId('crew-profile-back').count()) === 1)
  await card.screenshot({ path: join(OUT, '07-card-notes-pushed-light.png') })
  await page.getByTestId('crew-profile-back').click()
  await page.waitForTimeout(400)

  // 08: open the side panel → the card folds away, the pill returns, Dashboard shows.
  await page.getByTestId('member-panel-toggle').click()
  await panel.waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(1600)
  check('opening the side panel folds the card away', (await page.getByTestId('crew-profile-docked').count()) === 0)
  check('the pill is back', await pill.isVisible())
  const pw = await panel.boundingBox()
  check(`side panel is ~60% of the row (${Math.round(pw.width)}px of 1500)`, pw.width > 780 && pw.width < 960)
  const stripText = (await panel.locator('.side-panel-strip').innerText()).replace(/\s+/g, ' ')
  check('side panel strip has no Notes / Work log / Schedules', !/Notes|Work log|Schedules/.test(stripText), stripText)
  check('the dashboard report iframe rendered bare', (await page.getByTestId('crew-dashboard-iframe').count()) === 1)
  check('no drawer chrome around the report', (await page.getByTestId('crew-webview-summary').count()) === 0 && (await page.getByTestId('crew-webview-expand').count()) === 0)
  const fb = await page.getByTestId('crew-dashboard-iframe').boundingBox()
  const pb = await page.getByTestId('member-dashboard').boundingBox()
  check(`the report fills the tab (frame ${Math.round(fb.width)}x${Math.round(fb.height)}, tab ${Math.round(pb.width)}x${Math.round(pb.height)})`, Math.abs(fb.width - pb.width) <= 2 && Math.abs(fb.height - pb.height) <= 2)
  check('no identity row in the dashboard', (await page.getByTestId('member-identity-row').count()) === 0)
  await page.mouse.move(5, 5)
  await page.screenshot({ path: join(OUT, '08-side-panel-dashboard-light.png') })

  // 09: Files, scoped to the crewmate's workspace.
  await panel.getByRole('tab', { name: 'Files' }).click()
  await page.waitForTimeout(1200)
  check(`the Files view asked for the crewmate's workspace (${seenTreeDirs.join(', ')})`, seenTreeDirs.includes('/srv/repos/kiro'))
  check('the Files tree rendered', (await page.getByText('MembersPage.tsx').count()) > 0 || (await page.getByText('website').count()) > 0)
  await page.screenshot({ path: join(OUT, '09-side-panel-files-light.png') })
  await page.getByTestId('side-panel-leading-tab-crew-dashboard').click()
  await page.waitForTimeout(600)

  // 10: pill again → the card floats over the thread, beside the open panel.
  await pill.click()
  await card.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(600)
  const cb2 = await card.boundingBox()
  const pw2 = await panel.boundingBox()
  check(`card height matches the side panel (card ${Math.round(cb2.y)}..${Math.round(cb2.y + cb2.height)}, panel ${Math.round(pw2.y)}..${Math.round(pw2.y + pw2.height)})`,
    Math.abs(cb2.y - pw2.y) <= 10 && Math.abs((cb2.y + cb2.height) - (pw2.y + pw2.height)) <= 10)
  check('with the panel open the card floats (modal) and the pill stays', (await page.getByTestId('crew-profile-modal').count()) === 1 && await pill.isVisible())
  const tb = await page.getByTestId('crew-profile-modal').boundingBox()
  const leftGap = cb2.x - tb.x
  const rightGap = (tb.x + tb.width) - (cb2.x + cb2.width)
  check(`the floating card is centred in the chat width (gaps ${Math.round(leftGap)} / ${Math.round(rightGap)})`, Math.abs(leftGap - rightGap) <= 4)
  await page.screenshot({ path: join(OUT, '10-panel-and-profile-both-open-light.png') })
  await page.getByTestId('crew-profile-close').click()
  await page.waitForTimeout(400)

  // 11: the switcher.
  await page.getByTestId('crewmate-switcher').click()
  const list = page.getByTestId('crewmate-switcher-list')
  await list.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(400)
  check('switcher lists every crewmate', (await page.getByTestId('crewmate-switcher-row').count()) === 6)
  check('the chip carries a needs-you dot for Pixel', (await page.getByTestId('crewmate-switcher-needs-you').count()) === 1)
  check('Pixel row shows needs-you, Scout row shows unread', (await page.getByTestId('crewmate-switcher-needs-you-dot').count()) >= 1 && (await page.getByTestId('crewmate-switcher-unread-dot').count()) >= 1)
  const h = await page.getByTestId('member-thread-header').boundingBox()
  const l = await list.boundingBox()
  await page.screenshot({ path: join(OUT, '11-switcher-open-light.png'), clip: { x: h.x, y: h.y, width: Math.max(560, l.x + l.width - h.x + 20), height: l.y + l.height - h.y + 16 } })
  // 11b: the switcher footer pins the full roster beside the thread (teams, stars, filters).
  await page.getByTestId('crewmate-switcher-roster').click()
  await page.getByTestId('member-roster').waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(600)
  check('the footer pins the roster beside the thread', await page.getByTestId('member-roster').isVisible() && await page.getByTestId('member-thread-header').isVisible())
  await page.mouse.move(5, 5)
  await page.screenshot({ path: join(OUT, '13-roster-pinned-light.png') })
  // 11c: the roster header's close folds it away again.
  await page.getByTestId('member-roster-hide').click()
  await page.waitForTimeout(600)
  check('the roster folds away again', !(await page.getByTestId('member-roster').isVisible()))
  await page.screenshot({ path: join(OUT, '13b-roster-folded-light.png') })
  await page.getByTestId('crewmate-switcher').click()
  await page.getByTestId('crewmate-switcher-list').waitFor({ state: 'visible', timeout: 10000 })
  await page.locator('[data-testid="crewmate-switcher-row"]', { hasText: 'Atlas' }).click()
  await page.waitForFunction(() => document.querySelector('[data-testid="member-title-row"]')?.textContent?.includes('Atlas'), null, { timeout: 15000 })
  check('switching lands on Atlas', (await page.getByTestId('member-title-row').innerText()).includes('Atlas'))

  // 12: Atlas has published nothing → the Dashboard tab says so and points at the chat.
  if ((await page.getByTestId('member-side-panel').count()) === 0 || !(await page.getByTestId('member-dashboard').isVisible().catch(() => false))) {
    await page.getByTestId('member-panel-toggle').click()
  }
  await page.getByTestId('crew-webview-empty').waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(600)
  check('unpublished Dashboard points at the chat with no button', (await page.getByTestId('crew-webview-setup').count()) === 0)
  await page.mouse.move(5, 5)
  await page.screenshot({ path: join(OUT, '12-dashboard-empty-light.png') })
  await context.close()
}

/** The walkthrough, recorded, in the order asked: chat alone → profile → every
 *  tab → open the side panel (the card folds away) → both panel tabs → profile
 *  again, both open. */
{
  const videoDir = join(OUT, 'video-raw')
  mkdirSync(videoDir, { recursive: true })
  const context = await browser.newContext({ viewport: { width: 1500, height: 940 }, deviceScaleFactor: 1, colorScheme: 'light', recordVideo: { dir: videoDir, size: { width: 1500, height: 940 } } })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme: 'light', extra, slots: SLOTS, localStorageEntries: STORAGE })
  await page.goto(`${base}/members?member=kiro`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('member-identity-pill').waitFor({ state: 'visible', timeout: 30000 })
  const beat = (ms = 1400) => page.waitForTimeout(ms)
  await beat(2200)
  await page.getByTestId('member-identity-pill').click()
  await beat(2000)
  for (const name of ['Schedules', 'Sessions', 'Goals', 'Profile']) {
    await rail(page).getByRole('tab', { name }).click()
    await beat(1600)
  }
  await page.getByTestId('crew-profile-notes').click()
  await beat(1500)
  await page.getByTestId('crew-profile-back').click()
  await beat(1000)
  // Open the side panel: the card folds away on its own.
  await page.getByTestId('member-panel-toggle').click()
  await beat(2600)
  await page.getByTestId('side-panel-root').getByRole('tab', { name: 'Files' }).click()
  await beat(2000)
  await page.getByTestId('side-panel-leading-tab-crew-dashboard').click()
  await beat(1400)
  // Profile again: both open.
  await page.getByTestId('member-identity-pill').click()
  await beat(2600)
  await context.close()
  const { readdirSync, renameSync } = await import('node:fs')
  const raw = readdirSync(videoDir).find((f) => f.endsWith('.webm'))
  if (raw) renameSync(join(videoDir, raw), join(OUT, 'walkthrough.webm'))
  console.log('wrote', join(OUT, 'walkthrough.webm'))
}

await browser.close()
srv.close()
console.log(failed ? 'SOME CHECKS FAILED' : 'all checks OK', OUT)
process.exit(failed ? 1 : 0)
