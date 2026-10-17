/**
 * The Radar DM: one crewmate thread on the REAL Members page, for the capture
 * harnesses that mount `capture/members-page.html?route=/members?member=Radar`.
 *
 * `capture-crewmate-run-members.mjs` and `capture-crew-composer-one-row.mjs`
 * each had the roster, the thread and the `/api` route table inline, which the
 * jscpd gate flagged; the wiring lives here and each script keeps only what it
 * is actually looking at (the bubbles, or the composer).
 */

export const RADAR = {
  name: 'Radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'radar', memory_version: 2, memory_owner: 'Radar',
  model: '', description: 'Watches CI and the issue queue', source: 'kirocrew',
  last_active_ts: Math.floor(Date.now() / 1000) - 30, last_message: 'Back up since 08:22Z.',
}
export const MEMBERS = [RADAR, { ...RADAR, name: 'Fixer', slug: 'fixer', slot_key: 'member-fixer', memory_store: 'fixer', memory_owner: 'Fixer', description: 'Opens the fix PRs', last_message: 'Two PRs opened for the queue.' }]

/** The thread as the run-members harness reads it: two runs of one and two bubbles. */
export const THREAD = [
  { role: 'user', content: 'Anything new on the nightly?', ts: '2026-10-05T07:40:00Z', meta: { mid: 'm1' } },
  { role: 'assistant', content: 'Shard 1 went red twice on `test_spawn_approval_crew_log`: the test takes the crew-log lock on the event loop.\n\nBuilds are all green, so it is the test, not the code.', ts: '2026-10-05T07:40:21Z', meta: { mid: 'm2' } },
  { role: 'assistant', content: 'Filed it as #16628 with the `asyncio.to_thread` fix sketched. Waiting on triage.', ts: '2026-10-05T07:40:24Z', meta: { mid: 'm3' } },
  { role: 'user', content: 'Good. And the dispatcher?', ts: '2026-10-05T07:52:00Z', meta: { mid: 'm4' } },
  { role: 'assistant', content: 'Back up since 08:22Z: it no longer mints an owner token, it presents the cron\'s own credential. First cycle dispatched 4 of the backlog.', ts: '2026-10-05T08:24:02Z', meta: { mid: 'm5' } },
]

const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

/**
 * Intercept every `/api/*` request on `page` with the Radar DM answers.
 * `thread` is the slot's message list (defaults to THREAD).
 */
export async function routeRadarDm(page, { thread = THREAD } = {}) {
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/members') return route.fulfill(json({ members: MEMBERS, default_agent: 'kirocrew' }))
    if (path === '/api/teams') return route.fulfill(json({ teams: [] }))
    if (path === '/api/autonudge') return route.fulfill(json({ enabled: true, loops: [] }))
    if (path === '/api/config/default-agent') return route.fulfill(json({ default_agent: 'kirocrew' }))
    if (path === '/api/workspaces') return route.fulfill(json({ workspaces: [{ name: 'default' }] }))
    if (/^\/api\/members\/[^/]+\/thread$/.test(path)) return route.fulfill(json({ slot_key: 'member-radar', slug: 'radar', member: 'Radar', created: false }))
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) return route.fulfill(json({ slug: 'radar', member: 'Radar', capped: false, entries: [] }))
    if (/^\/api\/members\/[^/]+\/panel$/.test(path)) return route.fulfill(json({ panel: null, html: null }))
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) return route.fulfill(json({ key: 'member-radar', title: 'Radar', running: false, messages: thread }))
    if (path === '/api/crons') return route.fulfill(json({ jobs: [] }))
    if (path === '/api/webhooks') return route.fulfill(json({ tokens: [] }))
    if (path === '/api/agents') return route.fulfill(json({ agents: [], default_agent: 'kirocrew' }))
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
}

/** Open the Radar DM in `theme` and wait for the last bubble to be drawn. */
export async function openRadarDm(page, base, theme) {
  await page.goto(`${base}/capture/members-page.html?theme=${theme}&route=/members%3Fmember%3DRadar`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Good. And the dispatcher?').waitFor()
}

/** A `check(label, ok)` logger that counts failures into `state.failures`. */
export function makeCheck(state) {
  return (label, ok) => { console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) state.failures++ }
}
