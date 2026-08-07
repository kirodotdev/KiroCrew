/**
 * Screenshot harness for authoring a new Agent Template from the dashboard.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server from
 * scripts/lib/capture-harness.mjs, and answers every /api/** call from fixtures
 * via Playwright route interception. No gateway, no dashboard auth, no kiro-cli
 * spawn.
 *
 * The client code under test is unmodified — only the network is stubbed — so
 * the Create Template button, the authoring dialog, client-side validation, the
 * POST /api/agents/installed round-trip, and the post-create inspector selection
 * are exercised exactly as they run in production. The stubbed POST mutates the
 * fixture, so the after-shot shows the real re-render, not a mock.
 *
 * Usage: node scripts/capture-agent-template-create.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { json, serveDist, stubCommonApi } from './lib/capture-harness.mjs'

const OUT = process.argv[2] || '/tmp/agent-template-create-shots'

mkdirSync(OUT, { recursive: true })

const SKILLS = [
  { key: 'babysit', name: 'babysit', description: 'Same-session monitoring loop for PRs and CI runs', source: 'kirocrew' },
  { key: 'prepare-pr', name: 'prepare-pr', description: 'Drive working-tree changes to a review-ready pull request', source: 'kirocrew' },
  { key: 'rubber-duck', name: 'rubber-duck', description: 'Adversarial review that turns explaining out loud into a hallucination check', source: 'kirocrew' },
  { key: 'widgets', name: 'widgets', description: 'Render rich HTML inline via mcwidget tags', source: 'kirocrew' },
  { key: 'kiro-user/pod-e2e', name: 'pod-e2e', description: 'Run end-to-end tests against an isolated throwaway pod', source: 'kiro-user' },
]

/** Mutated by the stubbed POST so the after-shot renders real created state. */
const AGENTS = [
  { name: 'kirocrew', description: 'Autonomous personal AI agent', source: 'kirocrew', model: 'claude-opus-4.8', skills: [], mcp_servers: ['kirocrew-core', 'kirocrew-cron'], filename: 'kirocrew.json' },
  { name: 'code-reviewer', description: 'Reviews code changes against the repo conventions', source: 'builtin', model: 'claude-sonnet-4.5', skills: ['prepare-pr'], mcp_servers: [], filename: 'code-reviewer.json' },
]

const DETAIL = {
  kirocrew: {
    name: 'kirocrew', description: 'Autonomous personal AI agent', model: 'claude-opus-4.8',
    tools: ['execute_bash', 'fs_read', 'fs_write', 'grep', 'glob'],
    mcpServers: { 'kirocrew-core': {}, 'kirocrew-cron': {} }, skills: [], unmanaged_skills: [],
  },
  'code-reviewer': {
    name: 'code-reviewer', description: 'Reviews code changes against the repo conventions',
    model: 'claude-sonnet-4.5', tools: ['fs_read', 'grep', 'glob'], allowedTools: ['fs_read', 'grep'],
    skills: ['prepare-pr'], unmanaged_skills: [],
  },
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1500, height: 1050 },
  // Chips and labels are 11–13px type; a 1x shot renders soft on GitHub.
  deviceScaleFactor: 2,
})
const page = await context.newPage()

await page.routeWebSocket(/\/api\/ws/, () => {})

await page.route('**/api/**', async route => {
  const url = new URL(route.request().url())
  const path = url.pathname
  const method = route.request().method()

  if (path === '/api/agents/installed' && method === 'POST') {
    const body = JSON.parse(route.request().postData() || '{}')
    // Same duplicate rule as the backend: identity by name across all specs.
    if (AGENTS.some(a => a.name === body.name)) {
      return json(route, { error: `agent '${body.name}' already exists`, code: 'name_exists', field: 'name' }, 409)
    }
    AGENTS.push({
      name: body.name, description: body.description || '', source: 'builtin',
      model: body.model || 'auto', skills: body.skills || [],
      mcp_servers: Object.keys(body.mcpServers || {}), filename: `${body.name}.json`,
    })
    DETAIL[body.name] = {
      name: body.name, description: body.description, model: body.model || 'auto',
      prompt: body.prompt, tools: body.tools, allowedTools: body.allowedTools,
      mcpServers: body.mcpServers,
      toolsSettings: body.deniedCommands ? { execute_bash: { deniedCommands: body.deniedCommands } } : undefined,
      skills: body.skills || [], unmanaged_skills: [],
    }
    return json(route, { ok: true, name: body.name, filename: `${body.name}.json`, skills: body.skills || [] }, 201)
  }
  if (path === '/api/agents/installed') return json(route, AGENTS)
  if (path.startsWith('/api/agents/detail/')) {
    const name = decodeURIComponent(path.split('/').pop())
    return json(route, DETAIL[name] || { name })
  }
  if (path === '/api/skills') return json(route, SKILLS)
  if (path === '/api/mcp/probe') return json(route, [{ name: 'github-tools', tools: ['create_issue', 'get_pr'] }])
  return stubCommonApi(route, path)
})

page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })

await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
})

const shot = async name => {
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
}

/** Crop to the dialog when open, else full page. */
async function dialogShot(name, pad = 24) {
  const dialog = page.getByRole('dialog')
  if (await dialog.count()) {
    const bb = await dialog.first().boundingBox()
    if (bb) {
      await page.screenshot({
        path: `${OUT}/${name}.png`,
        clip: {
          x: Math.max(0, bb.x - pad), y: Math.max(0, bb.y - pad),
          width: Math.min(1500, bb.width + pad * 2), height: Math.min(1050, bb.height + pad * 2),
        },
      })
      console.log('wrote', `${OUT}/${name}.png`)
      return
    }
  }
  await shot(name)
}

await page.goto(base + '/capabilities?tab=templates', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2400)

// 1. The entry point: Create template button in the Installed Agents header.
await shot('01-templates-tab-create-button')

// 2. The empty authoring dialog.
await page.getByRole('button', { name: /create template/i }).click()
await page.waitForTimeout(600)
await dialogShot('02-create-dialog-empty')

// 3. Client-side validation: a duplicate name is refused before any request.
await page.getByLabel(/^name$/i).fill('code-reviewer')
await page.waitForTimeout(300)
await dialogShot('03-duplicate-name-rejected')

// 4. A complete draft: identity, prompt, skills, tools with one auto-approved,
//    an inline MCP server, and the advanced guardrails section.
await page.getByLabel(/^name$/i).fill('release-captain')
await page.getByLabel(/description/i).fill('Cuts releases and babysits the pipeline')
await page.getByLabel(/system prompt/i).fill('You are a release captain. Verify CI is green before every release step.')
await page.getByRole('button', { name: 'babysit', exact: true }).click()
await page.getByRole('button', { name: 'widgets', exact: true }).click()
await page.getByLabel(/add tool/i).fill('fs_read')
await page.getByRole('button', { name: /^add$/i }).click()
await page.getByRole('button', { name: /\+ @github-tools/i }).click()
await page.getByRole('button', { name: /toggle auto-approve for fs_read/i }).click()
await page.getByRole('button', { name: /add server/i }).click()
await page.getByLabel(/server name/i).fill('release-notes')
await page.getByLabel(/^command$/i).fill('npx')
await page.getByLabel(/arguments/i).fill('-y release-notes-mcp')
await page.getByRole('button', { name: /advanced/i }).click()
await page.getByLabel(/denied commands/i).fill('git push --force*')
await page.waitForTimeout(300)
await dialogShot('04-complete-draft')

// 5. After create: dialog closes, list refreshes, new template selected in the
//    inspector with everything the form authored.
await page.getByRole('button', { name: /create template$/i }).click()
await page.waitForTimeout(1200)
await shot('05-created-and-selected')

await browser.close()
srv.close()
console.log('done')
