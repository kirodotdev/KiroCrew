/**
 * Screenshot harness for the Switch All Sessions roster-failure notice (#10821).
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call from fixtures through the shared
 * `stubDashboardApi` helper. No gateway, no dashboard auth, no kiro-cli.
 *
 * The scene-specific stub is one route, `GET /api/models`, answered the ways
 * the endpoint really answers today:
 *   1. `503` — the gateway is restarting or kiro-cli's cold start timed out.
 *      The ACP adapter swallows it, serves Auto alone and marks the provider
 *      degraded; the panel must say so above the one-entry list.
 *   2. a live array — the healthy path, where no notice may render. Reached
 *      through the panel's own Retry button, because the app's QueryClient
 *      keeps a cached answer fresh forever (`staleTime: Infinity`), so merely
 *      reopening the panel would not fetch again.
 *
 * The sidebar is pinned to a narrow stored width (`mc-sidebar-width`), the
 * geometry the notice and its Retry button must share without collapsing.
 * Each scenario captures the Switch All panel element and the full page.
 *
 * Usage: node scripts/capture-bulk-model-roster-error.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/bulk-model-roster-error-shots'

mkdirSync(OUT, { recursive: true })

/** Sidebar stored width for the capture — the narrow-sidebar geometry. */
const SIDEBAR_WIDTH = 280

const SLOTS = [
  { key: 'chat-1', title: 'Refactor the auth middleware', messages: 12, running: false, agent: 'kirocrew', mode: '' },
  { key: 'chat-2', title: 'Weekly report draft', messages: 4, running: false, agent: 'kirocrew', mode: '' },
  { key: 'chat-3', title: 'Debug the flaky e2e suite', messages: 27, running: true, agent: 'kirocrew', mode: '' },
]

/** Shape of a live `/api/models` row (`RawModel` in providers/adapters/acp.ts). */
const LIVE_ROSTER = [
  { model_name: 'auto', description: 'Models chosen by task for optimal usage and consistent quality' },
  { model_name: 'opus-4.8', description: 'Deepest reasoning', rate_multiplier: 2.2 },
  { model_name: 'sonnet-4.7', description: 'Balanced', rate_multiplier: 1.3 },
]

/** Mutated between scenarios; the `extra` closure reads it per request. */
let scenario = 'down'

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
const page = await context.newPage()

const extra = async (path, route) => {
  if (path === '/api/models' && route.request().method() === 'GET') {
    if (scenario === 'down') await json(route, { error: 'model list unavailable' }, 503)
    else await json(route, LIVE_ROSTER)
    return true
  }
  return false
}

await stubDashboardApi(page, {
  slots: SLOTS,
  extra,
  localStorageEntries: {
    'mc-active-slot': 'chat-1',
    'mc-lang': 'en',
    'mc-sidebar-width': String(SIDEBAR_WIDTH),
  },
})

await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)

/** The Switch All panel root — the animate-rise card that holds the title. */
const panel = () => page.locator('div.animate-rise').filter({ hasText: 'Switch All Sessions' }).first()

async function openPanel() {
  await page.getByLabel('More options').first().click()
  await page.getByRole('menuitem', { name: /Switch all to model/ }).click()
  await panel().waitFor({ state: 'visible', timeout: 5000 })
}

async function shoot(name) {
  // Settle the panel's rise animation before measuring pixels.
  await page.waitForTimeout(400)
  const panelOut = join(OUT, `${name}-panel.png`)
  await panel().screenshot({ path: panelOut })
  console.log('wrote', panelOut)
  const pageOut = join(OUT, `${name}-page.png`)
  await page.screenshot({ path: pageOut })
  console.log('wrote', pageOut)
}

async function closePanel() {
  await panel().getByRole('button', { name: 'Cancel' }).click()
  await panel().waitFor({ state: 'hidden', timeout: 5000 })
}

// Scenario 1: /api/models is down — Auto alone, with the notice above it.
await openPanel()
const notice = page.getByTestId('bulk-model-roster-error')
await notice.waitFor({ state: 'visible', timeout: 5000 })
// Probe the geometry the frame exists to prove (in-repo capture-*.mjs
// practice: assert, don't trust). The notice sits ABOVE the listbox, spans
// most of the panel, and is not squeezed beside Retry into a sliver.
const noticeBox = await notice.boundingBox()
const listBox = await panel().getByRole('listbox').boundingBox()
const panelBox = await panel().boundingBox()
if (!noticeBox || !listBox || !panelBox) throw new Error('notice, listbox or panel has no bounding box')
if (noticeBox.y + noticeBox.height > listBox.y) throw new Error('notice is not above the listbox')
const widthRatio = noticeBox.width / panelBox.width
if (widthRatio < 0.6) throw new Error(`notice spans ${(widthRatio * 100).toFixed(0)}% of the panel — squeezed`)
const options = await panel().getByRole('option').count()
if (options !== 1) throw new Error(`expected the Auto-only placeholder, got ${options} options`)
await shoot('01-roster-down')
await closePanel()

// Scenario 2: live roster — the full list and no notice.
//
// Reopening the panel does NOT refetch: the app's QueryClient runs with
// `staleTime: Infinity`, so the cached degraded answer stays fresh and the
// only spontaneous refetch is the 8s degraded poll — which may or may not land
// inside a bounded wait. Retry is the deterministic path (and the one a user
// takes), so the frame is reached through it and the wait is for ITS fetch.
scenario = 'live'
await openPanel()
await notice.waitFor({ state: 'visible', timeout: 5000 })
const liveFetch = page.waitForResponse(r => r.url().endsWith('/api/models') && r.request().method() === 'GET')
await panel().getByRole('button', { name: 'Retry' }).click()
await liveFetch
await panel().getByRole('option', { name: /sonnet-4\.7/ }).waitFor({ state: 'visible', timeout: 5000 })
await notice.waitFor({ state: 'hidden', timeout: 5000 })
await shoot('02-roster-live')

await browser.close()
srv.close()
