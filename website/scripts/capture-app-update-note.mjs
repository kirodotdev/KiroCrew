/**
 * Screenshot harness for the app-update notification and its settings row.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with
 * every /api/** call answered from fixtures — no gateway, no token, no agent.
 * Same technique as capture-test-notification.mjs.
 *
 * Three views, each at 1280px (desktop) and 390px (phone):
 *
 *   feed      Notifications feed seeded with both producers of the `update`
 *             kind — an installed app's new version and the gateway's own —
 *             above an unchanged `cron` row for contrast.
 *   detail    The same feed with the app-update note selected, so the kind
 *             chip and the deep link in the detail pane are visible.
 *   settings  Settings → Notifications → Sources, where the `system.apps`
 *             channel is muted or re-prioritised.
 *
 * Usage: node scripts/capture-app-update-note.mjs [outDir] [prefix]
 *   prefix: file-name prefix, e.g. "before" / "after"
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/app-update-note'
const PREFIX = process.argv[3] || 'shot'
const PROJECT = '/home/user/workspace/KiroCrew'

// Fixed timestamps: the feed groups by date and prints a relative age, so a
// clock-derived value would make two runs differ for no reason.
const NOTES = [
  {
    kind: 'update',
    channel: 'system.apps',
    title: 'command-bar 0.2.0 available',
    body: 'You have 0.1.0. Open it to update.',
    ts: '1792490000',
    url: '/apps/detail/command-bar',
    source: 'system',
  },
  {
    kind: 'update',
    channel: 'system.agent',
    title: 'Kiro Crew 0.11.1 is available',
    body: 'Install it from Settings › About.',
    ts: '1792489000',
    url: '/settings/about',
    source: 'system',
  },
  {
    kind: 'cron',
    channel: 'system.cron',
    title: 'Nightly digest finished',
    body: 'Wrote 3 entries.',
    ts: '1792488000',
    source: 'system',
    acked: true,
  },
]

const CHANNELS = [
  { channel: 'system.approval', source: 'system', registered: true, default_priority: 'critical', protected: true, settings: {} },
  { channel: 'system.apps', source: 'system', registered: true, default_priority: 'default', protected: false, settings: {} },
  { channel: 'system.cron', source: 'system', registered: true, default_priority: 'default', protected: false, settings: {} },
  { channel: 'system.member', source: 'system', registered: true, default_priority: 'passive', protected: false, settings: {} },
]

mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()
const browser = await chromium.launch()

const fixedApi = makeFixedApi(PROJECT)

async function capture(viewport, view, name) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/notifications') return json(route, { notifications: NOTES, unread: 2 })
    if (path === '/api/notifications/channels') {
      return json(route, { channels: view === 'settings' ? CHANNELS : [] })
    }
    return handleBootRoute(route, path, { project: PROJECT, fixedApi })
  })
  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
  })

  if (view === 'settings') {
    await page.goto(`${base}/settings/notifications/sources`, { waitUntil: 'domcontentloaded' })
    // The heartbeat-free fixture has `system.cron`; its row proves the list rendered.
    await page.getByText('cron', { exact: true }).waitFor({ timeout: 20000 })
  } else {
    await page.goto(`${base}/notifications`, { waitUntil: 'domcontentloaded' })
    const row = page.getByText('command-bar 0.2.0 available').first()
    await row.waitFor({ timeout: 20000 })
    if (view === 'detail') {
      await row.click()
      // The detail pane repeats the title once selected.
      await page.getByText('command-bar 0.2.0 available').nth(1).waitFor({ timeout: 20000 })
    }
  }
  await page.waitForTimeout(800) // let the entrance stagger settle
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false })
  await context.close()
  return errors
}

const views = process.env.VIEWS ? process.env.VIEWS.split(',') : ['feed', 'detail', 'settings']
const allErrors = []
for (const view of views) {
  allErrors.push(...await capture({ width: 1280, height: 900 }, view, `${PREFIX}-${view}-desktop-1280`))
  allErrors.push(...await capture({ width: 390, height: 844 }, view, `${PREFIX}-${view}-mobile-390`))
}

await browser.close()
srv.close()

console.log(JSON.stringify({ out: OUT, prefix: PREFIX, views, errors: allErrors }, null, 2))
if (allErrors.length) process.exitCode = 1
