/**
 * Screenshot harness for the top-bar Kiro credit segment's three states.
 *
 * Runs a REAL built SPA behind the shared gateway-free static server + API stub,
 * so the segment renders exactly as it does in production with only the network
 * replaced.
 *
 * The dist directory is an ARGUMENT rather than the default, because the point of
 * this harness is comparing two builds of the same tree: one checked out before
 * the fix and one after. Both are driven with the identical stub set, so any
 * pixel difference can only come from the client code.
 *
 *   node scripts/capture-credit-pill-states.mjs <distDir> <outDir> <label>
 *
 * Scenarios, one shot each:
 *   failed   /api/sessions/usage answers 503 — the shape a stale kiro-readiness
 *            latch produces. Reading only `data`, that is indistinguishable from
 *            a cold cache and renders the warming spinner forever; reading
 *            `isError` renders a dash instead.
 *   loading  the request never settles, so the query stays pending. Pins that
 *            the failure branch did not swallow the genuine warming state.
 *   ok       a normal plan payload, to show the reading itself is untouched.
 *
 * Each scenario is shot three ways: the capsule, the whole header, and the
 * account modal the segment opens, because the modal reads the same state and
 * can contradict the segment that opened it.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join, resolve as resolvePath } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const DIST = resolvePath(process.argv[2] || 'dist')
const OUT = resolvePath(process.argv[3] || '/tmp/credit-pill-shots')
const LABEL = process.argv[4] || 'build'
mkdirSync(OUT, { recursive: true })

const USAGE_OK = {
  usage: {
    credits_used: 3044, credits_plan: 10000, credits_overage: 0,
    resets: '2026-09-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: 0.04,
  },
}

/** One settled slot: the shell renders the top bar against real slot state
 *  rather than the empty-list path, which is not what a user ever sees. */
const SLOT = 'credit-pill'
const slots = [{
  key: SLOT,
  title: 'Where did the credit readout go?',
  running: false,
  last_message: 'Checking the top bar.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '/home/user/workspace/notes',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()

for (const scenario of ['failed', 'loading', 'ok']) {
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 3,
    colorScheme: 'dark',
  })
  const page = await ctx.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots,
    extra: async (path, route) => {
      if (path === '/api/sessions/usage') {
        if (scenario === 'failed') {
          await json(route, { error: 'kiro-cli is not ready' }, 503)
          return true
        }
        // Leave the route unfulfilled: the request hangs and the query stays pending.
        if (scenario === 'loading') return true
        await json(route, USAGE_OK)
        return true
      }
      if (path.startsWith('/api/chat/slots/')) {
        await json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] })
        return true
      }
      return false
    },
  })
  await page.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)

  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  const capsule = page.locator('.tb-capsule').first()
  await capsule.waitFor({ state: 'visible', timeout: 15_000 })
  // Let the query settle — or visibly fail to — before shooting.
  await page.waitForTimeout(1_500)

  const credit = capsule.locator('button').last()
  console.log(`${LABEL}/${scenario}: aria-label=${JSON.stringify(await credit.getAttribute('aria-label'))} text=${JSON.stringify((await credit.innerText()).trim())}`)
  await capsule.screenshot({ path: join(OUT, `${LABEL}-${scenario}-capsule.png`) })
  await page.locator('header').first().screenshot({ path: join(OUT, `${LABEL}-${scenario}-header.png`) })

  // The drill-in is part of the same claim: a segment that reports a state must
  // not open a panel that reports a different one.
  await credit.click()
  const dialog = page.locator('[role="dialog"]').first()
  await dialog.waitFor({ state: 'visible', timeout: 10_000 })
  await page.waitForTimeout(400)
  await dialog.screenshot({ path: join(OUT, `${LABEL}-${scenario}-modal.png`) })
  await ctx.close()
}

await browser.close()
srv.close()
console.log(`shots in ${OUT}`)
