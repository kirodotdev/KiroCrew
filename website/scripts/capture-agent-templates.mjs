/**
 * Screenshot harness for Agent Capabilities > Agent Templates.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Shoots the four states the redesign is about: the roster + Overview pane, the
 * Guardrails pane (a list that used to scroll inside a 420px box behind a
 * <details>), the armed delete confirm, and the empty roster.
 *
 * Usage: node scripts/capture-agent-templates.mjs [outDir] [prefix]
 *   Run against the branch (after) and against a main build (before).
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/agent-templates'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const INSTALLED = [
  {
    name: 'kirocrew',
    description: 'Full crew agent — memory, crons, subagents, browser and the whole skill catalog.',
    source: 'kirocrew',
    model: 'claude-opus-5',
    skills: ['prepare-pr', 'babysit', 'llm-council'],
    mcp_servers: ['kirocrew-core', 'playwright'],
    filename: 'kirocrew.json',
  },
  {
    name: 'kirocrew-lite',
    description: 'Cheap, fast variant for cron jobs and one-shot checks.',
    source: 'kirocrew',
    model: '',
    skills: ['babysit'],
    mcp_servers: [],
    filename: 'kirocrew-lite.json',
  },
  {
    name: 'reviewer',
    description: 'Reads a diff and answers with findings only.',
    source: 'user',
    model: 'claude-sonnet-4.6',
    skills: ['adversarial-review'],
    mcp_servers: [],
    filename: 'reviewer.json',
  },
  {
    name: 'scratch',
    description: 'A copy to experiment on. No crew points at it, so it can be deleted.',
    source: 'user',
    model: '',
    skills: [],
    mcp_servers: [],
    filename: 'scratch.json',
  },
  {
    name: 'oncall-triage',
    description: 'Reads tickets, correlates alarms, drafts the first update.',
    source: 'package',
    model: 'claude-sonnet-4.6',
    skills: ['mossy'],
    mcp_servers: ['builder-mcp'],
    package: 'oncall-radar',
    filename: 'local-oncall-radar.json',
  },
]

const DETAIL = {
  kirocrew: {
    prompt: 'file://~/.kiro/crew/prompts/kirocrew.md',
    tools: ['fs_read', 'fs_write', 'execute_bash', 'use_aws', 'report_issue'],
    allowedTools: ['fs_read', 'use_aws'],
    mcpServers: { 'kirocrew-core': {}, playwright: { args: ['--include-tools', 'browser_navigate,browser_click'] } },
    toolsSettings: {
      execute_bash: {
        deniedCommands: [
          'rm\\s+-rf\\s+/',
          'DROP\\s+TABLE',
          'git\\s+push\\s+.*--force',
          'curl\\s+.*\\|\\s*sh',
          ':\\(\\)\\{\\s*:\\|:&\\s*\\};:',
        ],
      },
    },
    skills: ['kiro-user/prepare-pr', 'kiro-user/babysit', 'kiro-user/llm-council'],
    unmanaged_skills: ['skill://~/custom/*'],
  },
  reviewer: {
    prompt: 'You review diffs. Report findings with a severity and a file:line. Never rewrite the code.',
    tools: ['fs_read'],
    allowedTools: ['fs_read'],
    skills: ['kiro-user/adversarial-review'],
    unmanaged_skills: [],
  },
}

const CREWS = [
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: '', source: 'user' },
  { name: 'oncall', kiro_agent: 'kirocrew', workspace: 'oncall', memory_store: 'oncall', description: '', source: 'user' },
  { name: 'research', kiro_agent: 'reviewer', workspace: 'research', memory_store: 'research', description: '', source: 'user' },
]

/** Endpoints only the Agent Templates tab needs, layered on the boot stubs. */
function templatesApi(installed) {
  return async (path, route) => {
    if (path === '/api/agents/installed') return json(route, installed), true
    if (path.startsWith('/api/agents/detail/')) {
      const name = decodeURIComponent(path.slice('/api/agents/detail/'.length))
      const listed = installed.find(a => a.name === name) || {}
      return json(route, { ...listed, ...(DETAIL[name] || {}) }), true
    }
    if (path.startsWith('/api/agent-metadata/')) {
      const name = decodeURIComponent(path.slice('/api/agent-metadata/'.length))
      const content = name === 'kirocrew'
        ? 'Use for long multi-step engineering work: repo changes, PR loops, release ops.'
        : ''
      return json(route, { content }), true
    }
    if (path === '/api/agents') return json(route, { agents: CREWS, default_agent: 'default' }), true
    if (path === '/api/config/default-agent') return json(route, { default_agent: 'kirocrew' }), true
    if (path === '/api/sessions/context') return json(route, { sessions: [] }), true
    if (path === '/api/sessions/usage') return json(route, { usage: null }), true
    if (path === '/api/spawn') return json(route, { agents: [] }), true
    if (path === '/api/mcp/probe') return json(route, []), true
    if (path === '/api/skills') return json(route, []), true
    return false
  }
}

async function openTemplatesTab(page, base, api) {
  await stubDashboardApi(page, { extra: api })
  await page.goto(base + '/capabilities?tab=templates', { waitUntil: 'domcontentloaded' })
  const tab = page.locator('#main-content nav').getByRole('button', { name: 'Agent Templates', exact: true })
  await tab.waitFor({ state: 'visible', timeout: 15000 })
  await tab.click()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
  })
  const page = await context.newPage()
  logPageProblems(page)

  await openTemplatesTab(page, base, templatesApi(INSTALLED))

  // Roster content, in whichever DOM the build under test uses: the redesign's
  // listbox rows or main's plain list. Matching both keeps a `before` run
  // against main from hanging for 15s and then failing.
  await page.locator('#main-content [role="option"], #main-content .list-selected')
    .first().waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${PREFIX}-overview.png` })
  const shot = [`${PREFIX}-overview.png`]

  // Guardrails pane — the state that most needed the room.
  const guardrails = page.getByRole('tab', { name: /guardrails/i })
  if (await guardrails.count()) {
    await guardrails.click()
    await page.waitForTimeout(250)
    await page.screenshot({ path: `${OUT}/${PREFIX}-guardrails.png` })
    shot.push(`${PREFIX}-guardrails.png`)
  }

  // Delete withheld, and saying why: `reviewer` is bound by a crew, so the
  // guard refuses it rather than leaving a dangling reference behind.
  const boundRow = page.locator('#main-content [role="option"]', { hasText: 'reviewer' }).first()
  if (await boundRow.count()) {
    await boundRow.click()
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/${PREFIX}-delete-blocked.png` })
    shot.push(`${PREFIX}-delete-blocked.png`)
  }

  // Armed delete, on the one template nothing points at.
  const scratchRow = page.locator('#main-content [role="option"]', { hasText: 'scratch' }).first()
  if (await scratchRow.count()) {
    await scratchRow.click()
    const del = page.getByTestId('delete-template')
    await del.waitFor({ state: 'visible', timeout: 15000 })
    await del.click()
    await page.getByTestId('confirm-delete-template').waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/${PREFIX}-delete-confirm.png` })
    shot.push(`${PREFIX}-delete-confirm.png`)
  }

  // Empty roster, in its own page so the stubs stay simple.
  const empty = await context.newPage()
  logPageProblems(empty)
  await openTemplatesTab(empty, base, templatesApi([]))
  await empty.waitForTimeout(600)
  await empty.screenshot({ path: `${OUT}/${PREFIX}-empty.png` })
  shot.push(`${PREFIX}-empty.png`)

  console.log(`wrote ${shot.map(f => `${OUT}/${f}`).join(', ')}`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
