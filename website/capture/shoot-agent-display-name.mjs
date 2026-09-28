/**
 * Screenshot harness for the agent display-name feature (issue #13509).
 * House pattern of website/capture/shoot-tree-download-menu.mjs: the REAL
 * built SPA (website/dist) behind the in-process static server, every /api/**
 * answered from fixtures via Playwright route interception — gateway-free,
 * no kiro-cli, no token. The client code under test is unmodified.
 *
 * Frames (the set the UX lane asked for on PR #13561):
 *   10-crews-roster          the Crews roster: "oncall" carries the display
 *                            name "Oncall Sentinel" (label shown, ID beside
 *                            it); "default" has none and shows its name.
 *   11-edit-display-name     the edit sheet's OVERVIEW pane with the Display
 *                            Name field populated; the sheet title shows the
 *                            label. (The field moved from the routing pane to
 *                            overview on the UX lane's placement finding.)
 *   12-edit-empty-state      the same sheet for the unlabelled "default"
 *                            crew: the field is empty and its placeholder is
 *                            the name itself.
 *   13-members-labelled      the Members page with the labelled member
 *                            active: roster row, header label + muted-ID
 *                            chip, composer placeholder.
 *   14-selector-labelled     the chat composer's crew picker open: the
 *                            labelled crew's row shows label + muted ID.
 *
 * The reply-thread author line (ThreadPanel) needs a live thread transcript
 * this stub harness cannot fake shallowly; its label rendering is pinned by
 * ThreadPanel.test.tsx instead.
 *
 * Usage (from website/): node capture/shoot-agent-display-name.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from '../scripts/lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from '../scripts/lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/agent-display-name'
mkdirSync(OUT, { recursive: true })

const CREW = (name, display_name, kiro_agent) => ({
  name, display_name, scope: 'global', kiro_agent, workspace: 'default',
  memory_store: 'default', model: '', reasoning_effort: '', description: '',
  triggers: '', source: '', session_color: '', avatar: {}, starred: false,
  paused: false, last_active_ts: 0,
})

const AGENTS_FIXTURE = {
  agents: [
    CREW('default', '', 'kirocrew'),
    CREW('oncall', 'Oncall Sentinel', 'oncall-agent'),
    CREW('research', 'Deep Research', 'kirocrew'),
  ],
  default_agent: 'default',
}

const MEMBERS_FIXTURE = {
  members: [
    {
      name: 'oncall', slug: 'oncall', display_name: 'Oncall Sentinel',
      slot_key: '', running: false, last_active_ts: 1758750000,
      last_message: 'Watching the pager — all quiet.',
      kiro_agent: 'oncall-agent', workspace: 'default', memory_store: 'default',
      memory_version: 'v2', memory_owner: 'oncall', model: '', avatar: {},
      source: '', starred: false, description: '', triggers: '',
      projections: { asOfSeq: -1, values: {} },
    },
    {
      name: 'default', slug: 'default', display_name: '',
      slot_key: '', running: false, last_active_ts: 0, last_message: '',
      kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default',
      memory_version: 'v2', memory_owner: 'default', model: '', avatar: {},
      source: '', starred: false, description: '', triggers: '',
      projections: { asOfSeq: -1, values: {} },
    },
  ],
}

const { srv, base } = await serveDist()
// mise's node wrapper re-exports LD_LIBRARY_PATH pointing at its own (older)
// libstdc++, which breaks the system graphics libs chrome loads. Strip it from
// the chrome child; harmless when it was never set.
const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
logPageProblems(page)

const EXTRA = async (path, route) => {
    if (path === '/api/agents' || path === '/api/chat/agents' || path === '/api/agents/catalog') return json(route, AGENTS_FIXTURE), true
    if (path === '/api/agents/installed') {
      // Real shape: a BARE ARRAY of AgentInfo rows (agents.py serves
      // `[a.to_dict() ...]`), not an object envelope.
      const row = (name) => ({
        name, filename: name + '.json', description: '', model: '',
        skills: [], mcp_servers: [], source: 'builtin', package: '',
        scope: 'global', kirocrew_owned: false, forked_from: '', private_to: '',
      })
      return json(route, [row('kirocrew'), row('oncall-agent')]), true
    }
    if (path === '/api/members') return json(route, MEMBERS_FIXTURE), true
    if (path.startsWith('/api/members/')) return json(route, {}), true
    if (path === '/api/workspaces') return json(route, { workspaces: ['default'] }), true
    if (path === '/api/crons') return json(route, { jobs: [] }), true
    if (path === '/api/webhooks') return json(route, { hooks: [] }), true
    return false
}
await stubDashboardApi(page, { extra: EXTRA })

// ---- Frame 10: the crews roster with labelled and unlabelled crews ----
await page.goto(base + '/capabilities?tab=crews')
await page.getByText('Oncall Sentinel').first().waitFor({ timeout: 15000 })
await page.waitForTimeout(400)
await page.screenshot({ path: `${OUT}/10-crews-roster.png` })

// ---- Frame 11: the edit sheet's OVERVIEW pane, Display Name filled ----
await page.getByLabel(/Oncall Sentinel/).first().click()
const displayInput = page.getByLabel('Display Name').first()
await displayInput.waitFor({ timeout: 10000 })
await page.waitForTimeout(400)
await page.screenshot({ path: `${OUT}/11-edit-display-name.png` })
await page.keyboard.press('Escape')
await page.waitForTimeout(300)

// ---- Frame 12: the empty state — placeholder is the name itself ----
await page.getByLabel(/default/).first().click()
await page.getByLabel('Display Name').first().waitFor({ timeout: 10000 })
await page.waitForTimeout(400)
await page.screenshot({ path: `${OUT}/12-edit-empty-state.png` })
await page.keyboard.press('Escape')
await page.waitForTimeout(300)

// ---- Frame 13: Members page with the labelled member active ----
await page.goto(base + '/members')
await page.getByText('Oncall Sentinel').first().waitFor({ timeout: 15000 })
await page.getByText('Oncall Sentinel').first().click()
await page.waitForTimeout(1200)
await page.screenshot({ path: `${OUT}/13-members-labelled.png` })

// ---- Frame 14: the crew picker open, labelled row visible ----
// The Schedule page's job form embeds AgentSelector without the chat shell
// (whose recentsProvider trips a pre-existing error boundary under this stub
// harness — verified identical against the pre-change dist).
await page.goto(base + '/schedule')
await page.waitForTimeout(800)
await page.getByRole('button', { name: /Add Job|Create your first job/ }).first().click()
await page.waitForTimeout(500)
await page.getByLabel('Switch agent').first().click()
await page.getByText('Oncall Sentinel').first().waitFor({ timeout: 10000 })
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/14-selector-labelled.png` })

await browser.close()
srv.close()
console.log('frames written to', OUT)
