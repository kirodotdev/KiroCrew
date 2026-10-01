/**
 * Screenshot harness for the bell sheet's column scrim (App.tsx, the
 * `aria-hidden` strip behind the notification rows).
 *
 * Runs the REAL built SPA (website/dist) gateway-free (stubDashboardApi),
 * seeds the inbox over the stubbed REST route, opens the bell and photographs
 * the whole viewport so the strip's darkening against the page can be read
 * left of the sheet. Optional `SCRIM_CSS` is injected after the sheet settles,
 * so the pre-change strength can be photographed from the same build for a
 * before/after pair, e.g.
 *   SCRIM_CSS='[data-nc-phase] > [aria-hidden].-left-20{background-color:rgba(0,0,0,.12)!important;backdrop-filter:blur(4px)!important}'
 *
 * Frames: sheet-<theme>.png for dark and light, plus a printed sample of the
 * page luminance across the strip (x 760..1000 at y 750, CSS px).
 *
 * Usage: node scripts/capture-notification-scrim.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/notification-scrim'
mkdirSync(OUT, { recursive: true })
const CSS = process.env.SCRIM_CSS || ''
const TAG = process.env.SCRIM_TAG || 'after'

let seq = 0
const note = (over = {}) => ({
  kind: 'cron', source: 'system', channel: 'system.cron', priority: 'default',
  title: 'Nightly digest finished', body: 'Three repos summarized, two PRs need a look.',
  ts: new Date(Date.now() - seq++ * 1000).toISOString(), acked: false, ...over,
})
const seeded = [
  note({ kind: 'agent', title: 'Weekly cost report is ready', body: 'Spend is down 12% week over week; one anomaly on the EU stack.' }),
  note({ kind: 'cron', title: 'Backup completed', body: 'Nightly snapshot stored.' }),
  note({ kind: 'cron', title: 'Backup completed', body: 'Nightly snapshot stored.' }),
  note({ kind: 'cron', title: 'Backup completed', body: 'Nightly snapshot stored.' }),
  note({ kind: 'subagent', title: 'Research subagent finished', body: 'Summary attached to the session.' }),
  note({ kind: 'hook', title: 'Webhook received', body: 'GitHub: PR #12496 was approved.' }),
]

const { srv, base } = await serveDist()
const browser = await chromium.launch({ executablePath: chromiumExecutable() })

for (const theme of ['dark', 'light']) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme,
    extra: async (path, route) => {
      if (path === '/api/notifications' && route.request().method() === 'GET') {
        await json(route, { notifications: seeded, unread: seeded.length })
        return true
      }
      if (path === '/api/notifications/channels') { await json(route, { channels: [] }); return true }
      // Boot opens a first chat slot; without a keyed slot the recents
      // provider crashes the shell on `slot.key.startsWith`.
      if (path === '/api/chat/slots' && route.request().method() === 'POST') {
        await json(route, { key: 'chat-1', name: 'chat-1', title: 'New Session…', messages: [], running: false })
        return true
      }
      return false
    },
  })
  await page.goto(base + '/')
  const bell = page.locator('button:has(svg.lucide-bell)')
  await bell.waitFor({ state: 'visible', timeout: 20000 })
  // Boot redirects `/` to the first chat route and the sheet closes on any
  // pathname change; let the route settle before opening it.
  await page.waitForURL(u => u.pathname !== '/', { timeout: 10000 }).catch(() => {})
  await page.waitForTimeout(600)
  let open = false
  for (let attempt = 0; attempt < 3 && !open; attempt++) {
    await bell.click()
    await page.waitForTimeout(900)
    open = (await page.locator('[data-nc-phase]').getAttribute('data-nc-phase').catch(() => null)) === 'open'
  }
  if (!open) throw new Error('bell sheet did not stay open')
  await page.getByText('Weekly cost report is ready').waitFor({ timeout: 5000 })
  await page.waitForTimeout(1200)
  if (CSS) { await page.addStyleTag({ content: CSS }); await page.waitForTimeout(300) }
  const scrim = await page.locator('[data-nc-phase] > [aria-hidden="true"].-left-20').evaluate(el => {
    const cs = getComputedStyle(el)
    return { background: cs.backgroundColor, filter: cs.backdropFilter || cs.webkitBackdropFilter }
  })
  console.log(`sheet-${theme}-${TAG}: scrim ${JSON.stringify(scrim)}`)
  await page.screenshot({ path: join(OUT, `sheet-${theme}-${TAG}.png`) })
  await context.close()
}
await browser.close()
srv.close()
