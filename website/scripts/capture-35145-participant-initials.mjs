/**
 * Screenshot harness for contributor initials on Shared Crew rows.
 *
 * Drives the REAL built SPA (website/dist) on a team Crew (preview on, a
 * teammate observed) and captures, in dark and light:
 *   01-sessions-<theme>.png  session rows: the creator in the byline, the others as chips, "+1" past three
 *   02-bots-<theme>.png      the Bots roster: a Bot whose DM thread two people wrote in
 *   03-personal-<theme>.png  the same session rows on a personal Crew: no chips
 * and exits non-zero unless the chips read and are named as expected.
 *
 * Usage: node scripts/capture-35145-participant-initials.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/35145-participant-initials'
mkdirSync(OUT, { recursive: true })

const person = (id, name, login) => ({ id, kind: 'human', display: { name, login, captured_at: null } })
const OWNER = person('p_owner', 'Tim Finnigan', 'tim@example.com')
const PRIYA = person('p_priya', 'Priya Rao', 'priya@example.com')
const SAM = person('p_sam', 'Sam Avila', 'sam@example.com')
const LEE = person('p_lee', 'lee@example.com', 'lee@example.com')
const KIM = person('p_kim', 'Kim Ng', 'kim@example.com')
const attribution = creator => ({ schema_version: 1, availability: 'recorded', creator, created_at: 1, trigger: null })

const now = Math.floor(Date.now() / 1000)
const iso = s => new Date(Date.now() - s * 1000).toISOString()
const slot = (key, title, creator, participants, ago) => ({
  key, title, running: false, messages: 6, agent: 'kirocrew', modified: now - ago, last_ts: iso(ago),
  folder_id: '', last_message: 'Done.', source_links: [], source_links_total: 0,
  attribution: attribution(creator), participants,
})
const slots = [
  slot('chat-pair', 'Tighten the release checklist', PRIYA, [PRIYA, SAM], 60),
  slot('chat-crowd', 'Plan the Q1 migration', OWNER, [OWNER, PRIYA, SAM, LEE, KIM], 300),
]

const members = [{
  name: 'reviewer', slug: 'reviewer', slot_key: 'member-reviewer', running: false, display_name: 'Reviewer',
  description: 'Reviews pull requests', avatar: {}, source: 'kirocrew', dashboard_created: true,
  last_active_ts: now - 120, last_message: 'Left three comments on the PR.', has_dm_message: true,
  participants: [SAM, PRIYA],
}]

function sharedCrew(team) {
  return (path, route) => {
    if (path === '/api/shared-crew/capabilities') return json(route, { schema_version: 1, shared_crew_preview: true, attribution: 'available', presence: 'unavailable' }), true
    if (path === '/api/shared-crew/mode') return json(route, { mode: team ? 'network' : 'personal' }), true
    if (path === '/api/shared-crew/viewer') return json(route, OWNER), true
    if (path === '/api/shared-crew/people') {
      return json(route, { people: [
        { principal: OWNER, is_owner: true, is_viewer: true, sources: ['observed'] },
        ...(team ? [{ principal: PRIYA, is_owner: false, is_viewer: false, sources: ['observed'] }] : []),
      ] }), true
    }
    if (path === '/api/members') return json(route, { members }), true
    if (path.startsWith('/api/chat/slots/')) return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }), true
    return false
  }
}

function assert(label, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`${label}:\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`)
}

const chipNames = (page, testId) => page.locator(`[data-testid="${testId}"]`).evaluateAll(groups =>
  groups.map(g => [...g.querySelectorAll('[role="img"]')].map(c => `${c.textContent}=${c.getAttribute('aria-label')}`)))

async function crop(page, selector, path) {
  await page.mouse.move(1500, 850)
  const boxes = await page.locator(selector).evaluateAll(els => els.map(el => el.getBoundingClientRect().toJSON()))
  if (!boxes.length) throw new Error(`nothing matched ${selector}`)
  const x = Math.max(0, Math.min(...boxes.map(b => b.x)) - 8)
  const y = Math.max(0, Math.min(...boxes.map(b => b.y)) - 8)
  const w = Math.max(...boxes.map(b => b.right)) + 8 - x
  const h = Math.max(...boxes.map(b => b.bottom)) + 8 - y
  await page.screenshot({ path, clip: { x, y, width: w, height: h } })
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 2 })
  async function open(theme, team, route) {
    const page = await context.newPage()
    logPageProblems(page)
    // Keep the Bots roster open beside the opened Bot (`useMembersPageLayout`).
    await page.addInitScript(() => sessionStorage.setItem('mc-members-roster-pinned', '1'))
    await stubDashboardApi(page, {
      slots, theme, extra: sharedCrew(team),
      localStorageEntries: { 'mc-active-slot': 'chat-pair', 'mc-privacy-notice-v1': '1', 'mc-sidebar-pinned': 'true', 'mc-session-stale-collapse-ms': '0' },
    })
    await page.goto(base + route, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    return page
  }
  try {
    for (const theme of ['dark', 'light']) {
      let page = await open(theme, true, '/chat')
      await page.locator('[data-testid="session-participants"]').first().waitFor({ timeout: 15_000 })
      // The byline names each session's creator, so the chips are the others.
      assert(`${theme} session chips`, await chipNames(page, 'session-participants'), [
        ['SA=Sam Avila'],
        ['PR=Priya Rao', 'SA=Sam Avila', 'LE=lee@example.com', '+1=Others who wrote here: 1'],
      ])
      await crop(page, '.session-row', join(OUT, `01-sessions-${theme}.png`))
      await page.close()

      page = await open(theme, true, '/members')
      await page.locator('[data-testid$="-participants"]').first().waitFor({ timeout: 15_000 })
      assert(`${theme} Bot chips`, await chipNames(page, 'member-participants'), [['SA=Sam Avila', 'PR=Priya Rao']])
      await crop(page, 'li:has([data-testid="member-participants"])', join(OUT, `02-bots-${theme}.png`))
      await page.close()

      page = await open(theme, false, '/chat')
      await page.locator('.session-row').first().waitFor({ timeout: 15_000 })
      assert(`${theme} personal Crew`, await page.locator('[data-testid="session-participants"]').count(), 0)
      await crop(page, '.session-row', join(OUT, `03-personal-${theme}.png`))
      await page.close()
      console.log(`${theme} OK`)
    }
    console.log(`OK, screenshots in ${OUT}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
