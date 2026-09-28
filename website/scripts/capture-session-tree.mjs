/**
 * Screenshot harness for the session tree on the System page's Sessions plane.
 *
 * Photographs the REAL built SPA behind the shared static server with the boot
 * fixtures answered by the shared stub -- no gateway, no dashboard auth. The one
 * fixture that matters is `/api/sessions/memory`: its session rows carry the
 * `parent` field the crew-log tree fold adds, and the table nests on it.
 *
 * The payload is the shape the fold produces for a conductor that opened a
 * worker, which opened a worker of its own and spawned a task, beside a created
 * session whose creator is not running (a root that keeps its citation) and a
 * Slack session nobody created. Three frames per theme: every expander open, so
 * the indent per level, the mixed session/task children and the visible
 * "Created by" citation on the orphan row are on screen; the conductor's
 * subtree folded, so the expander that hides sessions is seen doing so; the
 * same table over a payload whose store holds more session logs than the scan admits, so the
 * footer's warn stat is seen in its non-zero state (it is hidden at zero); and
 * that stat's "?" hint open, so the words a reader gets for it are on screen.
 *
 * Usage: node scripts/capture-session-tree.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/session-tree-shots'
mkdirSync(OUT, { recursive: true })

const session = (over) => ({
  untitled: false, agent: 'kirocrew', owns_runtime: true, prompts: 4, channel: 'dashboard',
  procs: 3, mcp: 2, cpu_cores: 0.12, uptime_s: 5400, credits: 3.5, turns: 4, parent: null,
  ...over,
})

const SESSIONS_MEMORY = {
  sessions: [
    session({
      key: 'dashboard:chat-1531-1789617025', slot_key: 'chat-1531-1789617025',
      title: 'Ledger: session tree projection', pid: 4001, rss_mb: 1212.4, credits: 41.2, turns: 88,
      uptime_s: 26100,
    }),
    session({
      key: 'dashboard:chat-1577-1789769995', slot_key: 'chat-1577-1789769995',
      title: 'lineage-probe: child of ledger session', agent: 'kirocrew-worker', pid: 4002,
      rss_mb: 402.1, credits: 1.1, turns: 2, uptime_s: 3900,
      parent: { slot: 'chat-1531-1789617025', key: 'dashboard:chat-1531-1789617025' },
    }),
    session({
      key: 'dashboard:chat-1590-1789771201', slot_key: 'chat-1590-1789771201',
      title: 'worker: verify the fold against dsh', agent: 'kirocrew-worker', pid: 4003,
      rss_mb: 377.9, credits: 0.6, turns: 1, uptime_s: 1200,
      parent: { slot: 'chat-1577-1789769995', key: 'dashboard:chat-1577-1789769995' },
    }),
    session({
      key: 'dashboard:chat-1402-1789600310', slot_key: 'chat-1402-1789600310',
      title: 'worker whose conductor was closed', agent: 'kirocrew-worker', pid: 4004,
      rss_mb: 355.0, credits: 2.0, turns: 6, uptime_s: 9000,
      parent: { slot: 'chat-1399-1789600101', key: null },
    }),
    session({
      key: 'slack:C0A1B2C3:1789700000.1234', slot_key: '', title: 'slack:C0A1B2C3:1789700000.1234',
      channel: 'slack', owns_runtime: false, pid: 4001, rss_mb: 606.2, credits: null, turns: null,
      uptime_s: 700,
    }),
  ],
  tasks: [
    {
      id: 'task-7a1', task: 'run the crew-log tests on the rebased head', agent: 'kirocrew-lite',
      parent: 'dashboard:chat-1590-1789771201', rss_mb: 96.3, peak_rss_mb: 120.0, cpu_cores: 0.05,
      procs: 2, mcp: 0, started_at: Date.now() / 1000 - 240, shared: false, pid: 4010, sampled: true,
    },
    {
      id: 'task-7a2', task: 'summarise the dsh lineage reader', agent: 'kirocrew-lite',
      parent: 'dashboard:chat-1531-1789617025', rss_mb: 88.0, peak_rss_mb: 91.0, cpu_cores: 0.02,
      procs: 2, mcp: 0, started_at: Date.now() / 1000 - 60, shared: false, pid: 4011, sampled: true,
    },
  ],
  totals: {
    rss_mb: 3137.9, runtimes: 5, host_mb: 126000, host_pct: 2.49, rss_is_upper_bound: true,
    lineage_over_cap: false, lineage_cap: 4096,
  },
  history: [{ t: 1789780000, mb: 3020 }, { t: 1789780060, mb: 3137.9 }],
}

// The footer says how many session logs the backend's scan left unread past its
// cap; the stat is hidden at zero, so one variant of the payload reports 1,203.
const OMITTED_MEMORY = {
  ...SESSIONS_MEMORY,
  totals: { ...SESSIONS_MEMORY.totals, lineage_over_cap: true },
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

try {
  for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({
      viewport: { width: 1280, height: 1000 },
      deviceScaleFactor: 2,
    })
    const page = await context.newPage()
    logPageProblems(page)
    let memory = SESSIONS_MEMORY
    await stubDashboardApi(page, {
      theme,
      extra: async (path, route) => {
        if (path === '/api/sessions/memory') { await json(route, memory); return true }
        return false
      },
    })

    await page.goto(`${base}/developer?tab=system&plane=sessions`, { waitUntil: 'domcontentloaded' })
    // The conductor's row is the proof the table painted; wait on it, not on a timer.
    await page.getByText('Ledger: session tree projection').first().waitFor({ timeout: 10000 })
    // Open every level. Expanders are named by what they hide, so the aria-labels
    // for the session-bearing rows are the new `expand_sessions` strings.
    for (let round = 0; round < 4; round++) {
      const collapsed = page.locator('button[aria-expanded="false"][aria-label^="Expand"]')
      const n = await collapsed.count()
      if (n === 0) break
      for (let i = 0; i < n; i++) await collapsed.nth(0).click()
      await page.waitForTimeout(150)
    }
    const labels = await page.locator('button[aria-expanded="true"]').evaluateAll(
      els => els.map(e => e.getAttribute('aria-label')),
    )
    console.log('expanders:', labels)
    await page.waitForTimeout(400)
    // `below` is the room kept under the table: the footer, and for the last
    // frame the tooltip that opens beneath it.
    const shoot = async (name, below = 200) => {
      const table = page.getByRole('table').first()
      const box = await table.boundingBox()
      const path = `${OUT}/${name}-${theme}.png`
      const top = Math.max(0, box.y - 120)
      await page.screenshot({
        path,
        clip: { x: 0, y: top, width: 1280, height: Math.min(1000 - top, box.height + below) },
      })
      console.log(`wrote ${path}`)
    }
    await shoot('nested')
    // The same tree under Group by Channel: the nested rows keep their place
    // under their creator inside the group, and the orphan keeps its "Created
    // by" citation under a grouping too (the group row, not a creator, is its
    // parent in the row tree there). Back to None before the fold below.
    await page.getByRole('button', { name: 'Channel', exact: true }).click()
    await page.waitForTimeout(400)
    await shoot('grouped')
    await page.getByRole('button', { name: 'None', exact: true }).click()
    await page.waitForTimeout(400)
    // Fold the conductor: its expander is labelled by what it hides (sessions),
    // and the frame shows the two nested workers and their task gone with it
    // while the orphan and the Slack session stay where they were.
    await page.getByRole('button', { name: 'Collapse sessions under Ledger: session tree projection' }).click()
    await page.waitForTimeout(300)
    await shoot('collapsed')
    // Same table, a payload whose scan left logs unread: the warn stat appears.
    memory = OMITTED_MEMORY
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.getByText('Stored session logs', { exact: true }).waitFor({ timeout: 10000 })
    await page.waitForTimeout(400)
    await shoot('omitted')
    // The stat's own "?" (the column-header hint pattern), opened: the tooltip
    // is a fixed-position portal, so the same table clip shows it.
    await page.locator('span:has-text("Stored session logs") > button').first().click()
    await page.getByRole('tooltip').waitFor({ timeout: 5000 })
    await page.waitForTimeout(300)
    await shoot('hint-open', 240)
    // The two other new "?" bubbles, each opened on its own: the Memory column
    // header's (a parent's figure is its own, not a sum) and the Sessions
    // footer stat's (task sessions are a separate count). An InfoTip closes on any
    // mousedown outside it, so clicking the next "?" closes the open bubble
    // first and each frame shows exactly one.
    await page.locator('th:has-text("Memory") button[aria-label]').first().click()
    await page.getByRole('tooltip').waitFor({ timeout: 5000 })
    await page.waitForTimeout(300)
    await shoot('memory-hint-open', 240)
    await page.locator('span:has-text("Sessions") > button').first().click()
    await page.getByRole('tooltip').waitFor({ timeout: 5000 })
    await page.waitForTimeout(300)
    await shoot('sessions-hint-open', 240)
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
