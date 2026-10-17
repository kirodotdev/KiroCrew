/**
 * Screenshot harness for the Crew Members unread-badge drain fix.
 *
 * Runs the REAL built SPA (website/dist) gateway-free (stubDashboardApi) with
 * an unread member slot seeded the way the websocket marker leaves it
 * (`mc-unread-slots` in localStorage + a live `mode: 'member'` slot in the
 * slots list). The Crew Members rail item then carries a "1" badge.
 *
 * Frames:
 *   01-badge-stuck   /members with the roster visible: the rail badge shows 1
 *                    (the state the bug report screenshots — nothing on this
 *                    page could clear it)
 *   02-badge-drained after clicking the member row and mounting the DM
 *                    thread: the badge is GONE. This frame FAILS on unfixed
 *                    code, where opening the thread never dispatches
 *                    markSlotRead and the badge sticks forever.
 *
 * Usage: node scripts/capture-members-unread-badge.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/members-unread-badge'
mkdirSync(OUT, { recursive: true })

const MEMBER_SLOT = 'member-oncall'
const MEMBERS = [
  {
    name: 'oncall', slug: 'oncall', bound: true, slot_key: MEMBER_SLOT, running: false,
    kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
    last_active_ts: 1000, last_message: 'Rotation summary posted.',
  },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 })
logPageProblems(page)

await stubDashboardApi(page, {
  // The member slot must be LIVE: reconcileSlots drains unread keys missing
  // from the slot list, so an orphan key would self-heal and prove nothing.
  slots: [{ key: MEMBER_SLOT, title: 'oncall', mode: 'member', running: false, pinned: true }],
  // Exactly what the websocket unread-marker persists.
  localStorageEntries: {
    'mc-unread-slots': JSON.stringify([MEMBER_SLOT]),
    // The Crew Members surface is preview-gated (`utils/previewFlags.ts`), and
    // the badge this harness is about rides its RAIL row — so without the
    // opt-in there is no row to carry it and the wait below times out.
    'mc-preview-crew': '1',
  },
  extra: async (path, route) => {
    if (path === '/api/members') {
      await json(route, { members: MEMBERS, default_agent: 'kirocrew' })
      return true
    }
    if (/^\/api\/members\/[^/]+\/thread$/.test(path)) {
      await json(route, { slot_key: MEMBER_SLOT, slug: 'oncall', member: 'oncall', created: false })
      return true
    }
    if (path === `/api/chat/slots/${MEMBER_SLOT}`) {
      await json(route, {
        key: MEMBER_SLOT, title: 'oncall', running: false, messages: [
          { role: 'user', content: 'Anything on the rotation tonight?', ts: '2026-08-29T22:00:00Z' },
          { role: 'assistant', content: 'Quiet shift — one page, auto-resolved. Summary is in the channel.', ts: '2026-08-29T22:00:05Z' },
        ],
      })
      return true
    }
    // The SPA auto-creates a chat slot on first load; answering with a real
    // keyed slot keeps the command-palette recents provider from crashing on
    // a keyless row (see capture-notification-clear-badge.mjs).
    if (path === '/api/chat/slots' && route.request().method() === 'POST') {
      await json(route, { key: 'chat-1', name: 'chat-1', title: 'New Session…', messages: [], running: false })
      return true
    }
    return false
  },
})

await page.goto(base + '/members')

// 01 — the badge the user reported: 1 unread member thread, roster visible,
// and the flagged member's row carries the unread dot saying WHICH member.
const badge = page.locator('[aria-label="1 unread member threads"]')
await badge.waitFor({ state: 'visible', timeout: 15000 })
await page.getByText('Rotation summary posted.').waitFor({ timeout: 15000 })
const rosterDot = page.locator('[data-testid="member-unread-dot"]')
await rosterDot.waitFor({ state: 'visible', timeout: 15000 })
await page.screenshot({ path: `${OUT}/01-badge-stuck.png` })
console.log('01-badge-stuck: rail badge = 1 unread member threads, roster dot visible')

// 02 — open the thread. Mounting it IS reading it: the badge must drain.
await page.getByText('oncall', { exact: true }).first().click()
await page.getByText('Quiet shift — one page, auto-resolved. Summary is in the channel.').waitFor({ timeout: 15000 })
// FAILS on unfixed code: nothing dispatched markSlotRead, the badge sticks.
await badge.waitFor({ state: 'detached', timeout: 10000 })
// The roster dot drains with it — same store row, same drain.
await rosterDot.waitFor({ state: 'detached', timeout: 10000 })
// Gone because it DRAINED, not because the shell crashed.
if (await page.getByText('Something went wrong').isVisible().catch(() => false)) {
  throw new Error('ErrorBoundary visible after open — frame would show a crash, not the fix')
}
await page.screenshot({ path: `${OUT}/02-badge-drained.png` })
console.log('02-badge-drained: thread mounted, badge detached')

await browser.close()
srv.close()
