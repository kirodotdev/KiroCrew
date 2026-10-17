/**
 * Screenshot harness for the System > Services "Leaked agent runtimes" card.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server with
 * every /api/** call answered by the shared boot stub. Only the two leaked-runtime
 * routes are scene-specific. Scenes: the card at rest, the armed Reclaim (taken
 * LAST in its scene, before the arm decays), the result after a confirmed reclaim,
 * a refused reclaim rendered through ErrorNotice, a failed read (500), the 390px stack, and the armed
 * label at 320px, the longest copy at the narrowest width.
 *
 * Labels are read from the catalog, so a key rename breaks the capture loudly.
 *
 * Usage: node scripts/capture-leaked-runtimes.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/leaked-runtimes'
mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const st = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8')).pages.servicesTab
for (const key of ['leaked_runtimes', 'leaked_reclaim', 'leaked_reclaim_confirm']) {
  if (!st[key]) throw new Error(`catalog key pages.servicesTab.${key} missing — renamed?`)
}

const LEAKED = {
  supported: true,
  count: 3,
  rss_bytes: 1_840_000_000,
  runtimes: [
    { pid: 41210, rss_bytes: 912_000_000 },
    { pid: 41877, rss_bytes: 604_000_000 },
    { pid: 43002, rss_bytes: 324_000_000 },
  ],
}
const AFTER = { supported: true, count: 1, rss_bytes: 324_000_000, runtimes: [{ pid: 43002, rss_bytes: 324_000_000 }] }
const RECLAIMED = { killed: [41210, 41877], refused: [{ pid: 43002, reason: 'its group leader is alive' }] }

// The Services plane mounts the task-queue card beside this one, and it needs a
// well-formed summary to render at all.
const TASKS = {
  generated_at: 1_757_800_000, available: true,
  depth: { by_state: {}, queued: 0, waiting: 0, recovering: 0, running: 0, total: 0 },
  oldest_wait_secs: 0, lanes: {}, degrade_reason: null, adaptive: null, slots: [], waiting: [],
  recovering: { tasks: [], task_attempts: 0, slots: [], ladder: [] }, stalled: {}, counts: {},
  stall_after_secs: 600,
}

const SHOTS = [
  { name: 'leaked-runtimes-rest' },
  { name: 'leaked-runtimes-tip-open', tip: true },
  { name: 'leaked-runtimes-armed', clicks: 1, expect: st.leaked_reclaim_confirm },
  { name: 'leaked-runtimes-reclaimed', clicks: 2, reclaim: { status: 200, body: RECLAIMED }, ready: 'leaked-runtimes-result' },
  {
    name: 'leaked-runtimes-refused',
    clicks: 2,
    reclaim: { status: 403, body: { error: 'owner authorization required', code: 'owner_only' } },
    ready: 'leaked-runtimes-error',
  },
  { name: 'leaked-runtimes-load-failed', leakStatus: 500, ready: 'leaked-runtimes-load-error' },
  { name: 'leaked-runtimes-narrow', viewport: { width: 390, height: 1400 } },
  { name: 'leaked-runtimes-narrow-320-armed', viewport: { width: 320, height: 1400 }, clicks: 1, expect: st.leaked_reclaim_confirm },
]

const { srv, base: origin } = await serveDist()
const browser = await chromium.launch()

try {
  for (const { name, viewport, clicks = 0, reclaim, ready, expect, leakStatus, tip } of SHOTS) {
    let reclaimed = false
    const context = await browser.newContext({ viewport: viewport ?? { width: 1280, height: 1400 }, deviceScaleFactor: 1 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme: 'light',
      extra: async (path, route) => {
        if (path === '/api/system/leaked-runtimes') {
          if (leakStatus) await route.fulfill({ status: leakStatus, body: '{"error": "reconciler pass refused"}', contentType: 'application/json' })
          // After a confirmed reclaim the reading the card refetches is the one the
          // gateway would publish next: only the kept runtime is left.
          else await json(route, reclaimed ? AFTER : LEAKED)
          return true
        }
        if (path === '/api/tasks/summary') { await json(route, TASKS); return true }
        if (path === '/api/system/leaked-runtimes/reclaim' && reclaim) {
          await route.fulfill({ status: reclaim.status, body: JSON.stringify(reclaim.body), contentType: 'application/json' })
          reclaimed = reclaim.status === 200
          return true
        }
        return false
      },
    })
    await page.goto(`${origin}/developer?tab=system&plane=services`, { waitUntil: 'domcontentloaded' })
    const card = page.getByTestId('leaked-runtimes-card')
    await card.waitFor({ timeout: 15000 })
    await card.scrollIntoViewIfNeeded()
    const button = page.getByTestId('leaked-runtimes-reclaim')
    for (let i = 0; i < clicks; i++) await button.click()
    if (ready) await page.getByTestId(ready).waitFor({ timeout: 10000 })
    if (reclaim?.status === 200) await page.getByTestId('leaked-runtimes-count').getByText('1').waitFor({ timeout: 10000 })
    if (tip) {
      await card.getByRole('button', { expanded: false }).first().click()
      await page.getByRole('tooltip').waitFor({ timeout: 5000 })
    }
    if (expect) {
      const label = (await button.textContent())?.trim()
      if (label !== expect) throw new Error(`${name}: button reads ${JSON.stringify(label)}, expected ${JSON.stringify(expect)}`)
    }
    if (!(await card.getByRole('heading').first().isVisible())) throw new Error(`${name}: the card title is not visible`)
    const path = `${OUT}/${name}.png`
    // The open tooltip floats outside the card, so its frame is the card plus the tip.
    if (tip) {
      const a = await card.boundingBox()
      const b = await page.getByRole('tooltip').boundingBox()
      const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y)
      const w = Math.max(a.x + a.width, b.x + b.width) - x, h = Math.max(a.y + a.height, b.y + b.height) - y
      await page.screenshot({ path, clip: { x, y, width: w, height: h } })
    } else {
      await card.screenshot({ path })
    }
    console.log(`wrote ${path}`)
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
