/**
 * Screenshot harness for the Overview page's status hero.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * with the boot-time /api calls answered by the shared `handleBootRoute`, so no
 * gateway and no dashboard token are needed. Only the hero's own fixtures live
 * here.
 *
 * Pinned to deviceScaleFactor 1: a 2x shot of a 1280px viewport crosses the
 * 2000px per-edge cap that gets a whole request rejected.
 *
 * Captures:
 *   overview-hero.png   the status hero plus the first row of stat tiles
 *
 * Usage: node scripts/capture-overview-hero.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { handleBootRoute, json, makeFixedApi } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '/tmp/shots'
const PROJECT = '/home/kirocrew/workspace'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()

// Populated on purpose: the hero sits directly above the stat tiles, and an
// empty status renders them all as dashes, which hides whether the row below
// the hero moved.
const STATUS = {
  sessions: 12, messages: 4821, cron_jobs: 7, subagents: 3, lessons: 52,
  uptime: 273840, version: '0.1.0',
}

const fixedApi = makeFixedApi(PROJECT)
fixedApi.set('/api/status', STATUS)
// TWO WORDS, matching the backend default: the nav brand row accents the last
// word only, so a single-word name renders the mark without its "CREW" half.
fixedApi.set('/api/dashboard/branding', { bot_name: 'Kiro Crew', avatar: '/logo.png' })

const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1280, height: 420 },
  deviceScaleFactor: 1,
})
const page = await context.newPage()

let wsServer = null
await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

await page.route('**/api/**', async route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/memory/settings') {
    return json(route, { history_idle_hours: 3, history_max_days: 90, migrated: false })
  }
  // The hero sits above the usage summary card, which fetches on mount.
  if (path.includes('usage')) {
    return json(route, {
      sessions: {
        total_sessions: 42,
        today: { sessions: 3, messages: 128, tool_calls: 61 },
        this_week: { sessions: 18, messages: 900, tool_calls: 400 },
        this_month: { sessions: 42, messages: 2100, tool_calls: 950 },
        avg_msgs_per_session: 50,
        daily_history: [],
      },
      billing: { plan: 'Kiro Pro', credits_used: 633, credits_plan: 1000, resets: 'in 6h' },
    })
  }
  return handleBootRoute(route, path, { project: PROJECT, theme: 'dark', fixedApi })
})

page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))

await page.addInitScript(() => { localStorage.setItem('mc-onboarded', '1') })

await page.goto(`${base}/settings?tab=overview`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(1800)
if (wsServer) wsServer.send(JSON.stringify({ type: 'status', data: STATUS }))
await page.waitForTimeout(900)

// Assert on the RENDERED text rather than eyeballing the image: the whole point
// of this shot is whether an apply/restart control is present in the hero.
console.log('restart-buttons-in-viewport:', await page.getByRole('button', { name: /Restart/i }).count())

await page.screenshot({ path: `${OUT}/overview-hero.png` })

await context.close()
await browser.close()
srv.close()
console.log('done')
