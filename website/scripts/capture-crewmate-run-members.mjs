/** Real-browser evidence for #16617 on the REAL Members page: the DM header's
 * identity chip names the speaker, the thread's bubbles carry no author line,
 * and the run's last bubble still shows the message time in its hover footer
 * (same affordance as the single-chat page; always visible under hover:none).
 *
 * Mounts through capture/members-page.html with ?route=/members?member=Radar
 * (deep link into the thread); the API is route-intercepted, gateway-free.
 *
 * Frames per theme (dark, light):
 *   members-<theme>.png        the thread as opened: header chip + bubbles
 *   members-<theme>-hover.png  pointer on the last bubble: footer row with time
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6882 --strictPort            # in website/
 *   node scripts/capture-crewmate-run-members.mjs http://127.0.0.1:6882 ../temp-screenshots/crewmate-run
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6882'
const OUT = resolve(process.argv[3] || '../temp-screenshots/crewmate-run')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (l, ok) => { console.log(`${l} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

const RADAR = {
  name: 'Radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'radar', memory_version: 2, memory_owner: 'Radar',
  model: '', description: 'Watches CI and the issue queue', source: 'kirocrew',
  last_active_ts: Math.floor(Date.now() / 1000) - 30, last_message: 'Back up since 08:22Z.',
}
const MEMBERS = [RADAR, { ...RADAR, name: 'Fixer', slug: 'fixer', slot_key: 'member-fixer', memory_store: 'fixer', memory_owner: 'Fixer', description: 'Opens the fix PRs', last_message: 'Two PRs opened for the queue.' }]
const THREAD = [
  { role: 'user', content: 'Anything new on the nightly?', ts: '2026-10-05T07:40:00Z', meta: { mid: 'm1' } },
  { role: 'assistant', content: 'Shard 1 went red twice on `test_spawn_approval_crew_log`: the test takes the crew-log lock on the event loop.\n\nBuilds are all green, so it is the test, not the code.', ts: '2026-10-05T07:40:21Z', meta: { mid: 'm2' } },
  { role: 'assistant', content: 'Filed it as #16628 with the `asyncio.to_thread` fix sketched. Waiting on triage.', ts: '2026-10-05T07:40:24Z', meta: { mid: 'm3' } },
  { role: 'user', content: 'Good. And the dispatcher?', ts: '2026-10-05T07:52:00Z', meta: { mid: 'm4' } },
  { role: 'assistant', content: 'Back up since 08:22Z: it no longer mints an owner token, it presents the cron\'s own credential. First cycle dispatched 4 of the backlog.', ts: '2026-10-05T08:24:02Z', meta: { mid: 'm5' } },
]
const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/members') return route.fulfill(json({ members: MEMBERS, default_agent: 'kirocrew' }))
    if (path === '/api/teams') return route.fulfill(json({ teams: [] }))
    if (path === '/api/autonudge') return route.fulfill(json({ enabled: true, loops: [] }))
    if (path === '/api/config/default-agent') return route.fulfill(json({ default_agent: 'kirocrew' }))
    if (path === '/api/workspaces') return route.fulfill(json({ workspaces: [{ name: 'default' }] }))
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) return route.fulfill(json({ slot_key: 'member-radar', slug: 'radar', member: 'Radar', created: false }))
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) return route.fulfill(json({ slug: 'radar', member: 'Radar', capped: false, entries: [] }))
    if (/^\/api\/members\/[^/]+\/panel$/.test(path)) return route.fulfill(json({ panel: null, html: null }))
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) return route.fulfill(json({ key: 'member-radar', title: 'Radar', running: false, messages: THREAD }))
    if (path === '/api/crons') return route.fulfill(json({ jobs: [] }))
    if (path === '/api/webhooks') return route.fulfill(json({ tokens: [] }))
    if (path === '/api/agents') return route.fulfill(json({ agents: [], default_agent: 'kirocrew' }))
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}&route=/members%3Fmember%3DRadar`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Good. And the dispatcher?').waitFor()
  await page.waitForTimeout(400)

  const rows = page.locator('[data-testid="crewmate-message"]')
  check(`[${theme}] three crewmate bubbles drawn`, await rows.count() === 3)
  check(`[${theme}] no author line on any bubble`, await page.locator('[data-testid="crewmate-author"]').count() === 0)
  // The header names the speaker: the identity pill (face + name) at the top.
  const chip = page.getByTestId('member-identity-pill').first()
  check(`[${theme}] header identity chip present and names Radar`, await chip.count() === 1 && /Radar/.test(await chip.innerText()))
  await page.screenshot({ path: resolve(OUT, `members-${theme}.png`) })

  // Hover the run's last bubble: its footer reveals the time (tabular-nums span
  // titled with the full date), the same affordance the single chat has.
  const last = rows.nth(2)
  await last.hover({ position: { x: 120, y: 20 } }); await page.waitForTimeout(700)
  const time = last.locator('span.tabular-nums[title]').first()
  const visible = await time.count() === 1 && await time.evaluate(el => {
    const row = el.parentElement
    return !!row && getComputedStyle(row).opacity === '1' && el.textContent.trim().length > 0
  })
  check(`[${theme}] hover on the run's last bubble shows its time`, visible)
  await page.screenshot({ path: resolve(OUT, `members-${theme}-hover.png`) })
  check(`[${theme}] no page errors`, errors.length === 0)
  await ctx.close()
}
await browser.close()
if (failures) { console.error(`${failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
