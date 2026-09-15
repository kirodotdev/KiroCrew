/**
 * Screenshot harness for the notification-clear badge sync fix.
 *
 * Runs the REAL built SPA (website/dist) gateway-free (stubDashboardApi) and
 * drives the /api/ws socket directly with Playwright's routeWebSocket, so the
 * frames capture the exact client-side path under test: another view clears
 * the inbox, the backend broadcasts `notifications_clear`, and THIS view's
 * bell badge — which is derived from the Redux notification list, not from a
 * refetch — must converge to zero.
 *
 * Frames:
 *   01-badge-before-clear  bell badge showing the unread count from seeded
 *                          notifications (the state every open view is in
 *                          before anyone clears)
 *   02-badge-after-clear   same view after the `notifications_clear` WS frame:
 *                          the badge is gone (this frame FAILS on unfixed code,
 *                          where the frame is ignored and the badge sticks)
 *
 * Usage: node scripts/capture-notification-clear-badge.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/notification-clear-badge'
mkdirSync(OUT, { recursive: true })

// Unread, attention-worthy notes (non-passive, non-silenced) so the badge
// counts them — mirroring the issue's report of a lingering unread count.
const NOTES = Array.from({ length: 16 }, (_, i) => ({
  kind: 'cron',
  source: 'system',
  channel: 'system.cron',
  priority: 'default',
  title: `Job ${i + 1} finished`,
  body: 'output ready',
  ts: `2026-08-10T0${Math.floor(i / 10)}:${String(10 + i).slice(-2)}:00.000000+00:00`,
  acked: false,
}))

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
logPageProblems(page)

// Bind the WS BEFORE stubDashboardApi installs its swallow-all route:
// Playwright consults routes in reverse-registration order, so registering
// first would let the stub's handler win. Register after instead.
let wsServer = null

await stubDashboardApi(page, {
  // `json()` resolves to undefined, so it must be awaited and `true` returned
  // explicitly — returning it directly makes the base stub fulfill the same
  // route a second time ("Route is already handled!").
  extra: async (path, route) => {
    if (path === '/api/notifications') {
      await json(route, { notifications: NOTES, unread: NOTES.length })
      return true
    }
    // The SPA auto-creates a chat slot on first load; the base stub's
    // fallback answers the POST with `[]`, leaving a keyless slot in Redux
    // that later crashes the command-palette recents provider
    // (`normalizeKey(s.key)` on undefined) and trips the app-shell
    // ErrorBoundary — which would poison the after-clear frame.
    if (path === '/api/chat/slots' && route.request().method() === 'POST') {
      await json(route, { key: 'chat-1', name: 'chat-1', title: 'New Session…', messages: [], running: false })
      return true
    }
    return false
  },
})
// Re-route the socket AFTER the stub so this handler (registered last) wins
// and we can push server frames into the page.
await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

await page.goto(base + '/')
// The badge renders once the seeded notifications land in the store.
const badge = page.locator('button[aria-label]:has(svg.lucide-bell) span[aria-hidden="true"]')
await badge.waitFor({ state: 'visible', timeout: 15000 })
const before = await badge.textContent()
if (before !== '16') throw new Error(`expected badge "16" before clear, got "${before}"`)
await page.screenshot({ path: `${OUT}/01-badge-before-clear.png` })
console.log('01-badge-before-clear: badge =', before)

if (!wsServer) throw new Error('websocket route never bound — badge frame came from fetch only')
// Model the other view clearing the inbox: the backend broadcast reaches
// this view over WS.
wsServer.send(JSON.stringify({ type: 'notifications_clear', data: {} }))

await badge.waitFor({ state: 'detached', timeout: 10000 })
// The badge must be gone because the LIST emptied, not because the shell
// crashed: the bell button itself must still be rendered and no error
// boundary may be showing.
const bell = page.locator('button:has(svg.lucide-bell)')
if (!(await bell.isVisible())) throw new Error('bell button gone after clear — shell crashed, not converged')
if (await page.getByText('Something went wrong').isVisible().catch(() => false)) {
  throw new Error('ErrorBoundary visible after clear — frame would show a crash, not the fix')
}
await page.screenshot({ path: `${OUT}/02-badge-after-clear.png` })
console.log('02-badge-after-clear: badge detached, bell alive (count converged to 0)')

await browser.close()
srv.close()
console.log('OK — frames written to', OUT)
