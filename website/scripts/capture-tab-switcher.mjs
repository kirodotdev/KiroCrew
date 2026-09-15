/**
 * Screenshot harness for the tab-switcher unification.
 *
 * Photographs the rails that changed, on the REAL built SPA behind the shared
 * static server with the boot fixtures answered by the shared stub -- no gateway,
 * no dashboard auth. Both themes, because the selected pill's accent and the
 * recessed track read differently on each.
 *
 * `/system` and `/apps` render the Radix `ui/tabs.tsx` (a panel per tab);
 * `/webhooks` renders `Tablist` (a tablist with no panel), which is the pair this
 * change has to make indistinguishable.
 *
 * Usage: node scripts/capture-tab-switcher.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/tab-switcher-shots'
mkdirSync(OUT, { recursive: true })

/**
 * `/api/webhooks` is the one surface here the shared stub has no fixture for, and
 * an absent payload error-boundaries the page instead of rendering its rail. The
 * shape mirrors the page's own `EMPTY_VIEW`, with two tokens and one run so BOTH
 * tabs carry a count -- a rail photographed with every count hidden would not show
 * that half of the component at all.
 */
const WEBHOOKS_VIEW = {
  enabled: true,
  switch_on: true,
  has_tokens: true,
  url: 'https://example.invalid/api/hooks/agent',
  slots: { in_use: 1, max: 6 },
  limits: {
    session_key_prefix: 'hook:', message_max: 49999,
    timeout_default: 599, timeout_max: 3593, max_concurrent: 6,
    body_max_bytes: 262144, signature_window_seconds: 300,
  },
  tokens: [
    { id: 'tok-a', label: 'ci-pipeline', agent: 'kirocrew', created: 1756600000, last_used: 1756680000 },
    { id: 'tok-b', label: 'release-bot', agent: 'kirocrew-autofix', created: 1756500000, last_used: null },
  ],
  contexts: [],
  runs: [
    { id: 'run-1', hook_id: 'ci-pipeline', started: 1756680000, status: 'ok' },
  ],
}

/**
 * `/api/sessions/memory` feeds the System page's Sessions plane. It matters here
 * beyond "the page renders": that plane puts its own `Group by` SegmentedControl
 * directly under the plane rail, so System is the ONE surface where a navigation
 * pill and a filter pill are visible together — which is exactly the hierarchy
 * this change has to be judged on. Rows are needed for the table (and therefore
 * the filter) to appear at all.
 */
const SESSIONS_MEMORY = {
  sessions: [
    {
      key: 'chat-1-alpha', title: 'Placeholder session', slot_key: 'chat-1-alpha',
      untitled: false, agent: 'kirocrew', pid: 4001, owns_runtime: true, prompts: 12,
      channel: 'dashboard', rss_mb: 512, procs: 3, mcp: 2, cpu_cores: 0.4,
      uptime_s: 3600, credits: null, turns: 12,
    },
    {
      key: 'chat-1-beta', title: 'Placeholder session', slot_key: 'chat-1-beta',
      untitled: false, agent: 'kirocrew-autofix', pid: 4002, owns_runtime: false, prompts: 3,
      channel: 'slack', rss_mb: 288, procs: 2, mcp: 1, cpu_cores: 0.1,
      uptime_s: 900, credits: null, turns: 3,
    },
  ],
  tasks: [],
  totals: { rss_mb: 800, runtimes: 2, host_mb: 62000, host_pct: 1.3, rss_is_upper_bound: false },
  history: [{ t: 1756680000, mb: 780 }, { t: 1756680060, mb: 800 }],
}

/**
 * `system` is reached through the Developer page's own tab, not a top-level route:
 * `SystemPage` renders `embedded` inside `DeveloperPage`. Its plane rail is the
 * one worth photographing, because the Sessions plane below it carries a
 * `SegmentedControl` `Group by` filter — the only place a navigation pill and a
 * filter pill appear together.
 */
const SURFACES = [
  ['system', '/developer?tab=system', 'tablist'],
  ['discover', '/apps', 'tablist'],
  ['webhooks', '/webhooks', 'tablist'],
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()

try {
  for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({
      viewport: { width: 1280, height: 900 },
      // 12px type on a 1px track: a 1x frame cannot show the border or the shadow
      // that separates the selected pill from the track behind it.
      deviceScaleFactor: 2,
    })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme,
      extra: async (path, route) => {
        if (path === '/api/webhooks') { await json(route, WEBHOOKS_VIEW); return true }
        if (path === '/api/sessions/memory') { await json(route, SESSIONS_MEMORY); return true }
        return false
      },
    })

    for (const [name, route, waitRole] of SURFACES) {
      await page.goto(`${base}${route}`, { waitUntil: 'domcontentloaded' })
      // Assert the rail itself is on screen rather than sleeping: a frame taken
      // before the tablist mounts photographs an empty page and still "passes".
      try {
        await page.getByRole(waitRole).first().waitFor({ timeout: 8000 })
      } catch {
        console.log(`SKIP ${name} (${theme}): no tablist rendered`)
        continue
      }
      // Let the indicator's spring settle so the pill is not caught mid-travel.
      await page.waitForTimeout(600)
      const path = `${OUT}/${name}-${theme}.png`
      await page.screenshot({ path, clip: { x: 0, y: 0, width: 1280, height: 420 } })
      console.log(`wrote ${path}`)
    }
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
