/**
 * Capture the ACP Agent Hooks table with the `agent.kiro_hooks` documents Crew
 * accepted but did not hand to kiro-cli: one row per document, after
 * the hooks kiro-cli runs, carrying the `never fires` mark and its reason.
 *
 * Gateway-free, like every other harness in this folder: it serves the real
 * built SPA and answers `/api/**` from `stub-dashboard-api.mjs`, plus the two
 * hooks endpoints below, so all four reasons render without hand-seeding a home.
 *
 *   npm run build && node scripts/capture-hooks-unsent-rows.mjs <outDir>
 */
import { chromium } from '@playwright/test'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2]
if (!OUT) { console.error('usage: capture-hooks-unsent-rows.mjs <outDir>'); process.exit(2) }

const KIRO_HOOKS = {
  hooks: {
    agentSpawn: [{ command: '/opt/kirocrew/hooks/session-start.sh', matcher: '', source: 'bundled' }],
    preToolUse: [{ command: '/home/me/.kiro/hooks/guard.sh', matcher: 'execute_bash', source: 'user' }],
  },
  // One row per reason, in the gateway's own wire shape.
  unsent: [
    { name: 'lint on save', trigger: 'PostFileSave', matcher: '', action_type: 'command', command: '/home/me/.kiro/hooks/lint.sh', reason: 'trigger' },
    { name: 'wrap up', trigger: 'Stop', matcher: '', action_type: 'agent', command: 'Summarize what changed', reason: 'agent_action' },
    { name: 'block writes', trigger: 'PreToolUse', matcher: 'fs_write', action_type: 'command', command: '/home/me/.kiro/hooks/block.sh', reason: 'disabled' },
    { name: 'ask first', trigger: 'UserPromptSubmit', matcher: '', action_type: 'command', command: '/home/me/.kiro/hooks/ask.sh', reason: 'confirm' },
  ],
}

const REASONS = [
  'kiro-cli has no event for this trigger',
  'kiro-cli runs commands only',
  '"enabled": false',
  '"confirm": true',
  'Remove it to run the hook',
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()
try {
  for (const scheme of ['dark', 'light']) {
    const ctx = await browser.newContext({
      viewport: { width: 1440, height: 1100 }, colorScheme: scheme, locale: 'en-US',
    })
    const page = await ctx.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme: scheme,
      extra: async (path, route) => {
        if (path === '/api/hooks') { await json(route, { hooks: [] }); return true }
        if (path === '/api/kiro-hooks') { await json(route, KIRO_HOOKS); return true }
        return false
      },
    })
    await page.goto(`${base}/capabilities?tab=hooks`, { waitUntil: 'networkidle' })
    const region = page.getByRole('region', { name: 'ACP Agent Hooks' })
    await region.waitFor({ timeout: 20000 })
    // Every claim the shot makes is asserted first: all four reasons as visible
    // text, and four marks, so a capture taken before the query lands fails here.
    for (const copy of REASONS) {
      await region.getByText(copy, { exact: false }).first().waitFor({ timeout: 20000 })
    }
    const marks = await region.getByText('never fires', { exact: true }).count()
    if (marks !== 4) throw new Error(`expected 4 marks, saw ${marks}`)
    // The reason must not push the table past its scroller: the card is read-only
    // and a clipped reason is an unread one.
    const overflow = await region.evaluate(el => el.scrollWidth - el.clientWidth)
    if (overflow > 0) throw new Error(`provider table overflows its scroller by ${overflow}px`)
    await region.scrollIntoViewIfNeeded()
    await page.waitForTimeout(300)
    await region.screenshot({ path: `${OUT}/hooks-unsent-rows-${scheme}.png` })
    await ctx.close()
  }
} finally {
  await browser.close()
  srv.close()
}
console.log('ok')
