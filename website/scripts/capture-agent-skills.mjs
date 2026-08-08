/**
 * Screenshot harness for mapping skills to an agent template.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * with SPA fallback, and answers every /api/** call from fixtures via Playwright
 * route interception. No gateway, no dashboard auth, no kiro-cli spawn.
 *
 * The client code under test is unmodified — only the network is stubbed — so
 * Agent Capabilities > Agent Templates, the Skills chips, the add-skill
 * dropdown, and the PATCH round-trip are exercised exactly as they run in
 * production. The stubbed PATCH mutates the fixture, so the after-shot shows
 * the real re-render, not a mock.
 *
 * Usage: node scripts/capture-agent-skills.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { json, serveDist, stubCommonApi } from './lib/capture-harness.mjs'

const OUT = process.argv[2] || '/tmp/agent-skills-shots'

mkdirSync(OUT, { recursive: true })

const SKILLS = [
  { key: 'babysit', name: 'babysit', description: 'Same-session monitoring loop for PRs and CI runs', path: '/home/user/.kiro/crew/skills/babysit/SKILL.md', source: 'kirocrew' },
  { key: 'prepare-pr', name: 'prepare-pr', description: 'Drive working-tree changes to a review-ready pull request', path: '/home/user/.kiro/crew/skills/prepare-pr/SKILL.md', source: 'kirocrew' },
  { key: 'rubber-duck', name: 'rubber-duck', description: 'Adversarial review that turns explaining out loud into a hallucination check', path: '/home/user/.kiro/crew/skills/rubber-duck/SKILL.md', source: 'kirocrew' },
  { key: 'widgets', name: 'widgets', description: 'Render rich HTML inline via mcwidget tags', path: '/home/user/.kiro/crew/skills/widgets/SKILL.md', source: 'kirocrew' },
  { key: 'kiro-user/pod-e2e', name: 'pod-e2e', description: 'Run end-to-end tests against an isolated throwaway pod', path: '/home/user/.kiro/skills/pod-e2e/SKILL.md', source: 'kiro-user' },
  { key: 'kiro-user/llm-council', name: 'llm-council', description: 'Convene a cross-vendor LLM council for hard decisions', path: '/home/user/.kiro/skills/llm-council/SKILL.md', source: 'kiro-user' },
]

/** Mutated by the stubbed PATCH so the after-shot renders real state. */
const mapping = {
  kirocrew: [],
  'code-reviewer': ['prepare-pr', 'rubber-duck'],
  'release-captain': [],
}
const UNMANAGED = { 'release-captain': ['skill://~/.kiro/skills/*/SKILL.md'] }

const AGENTS = [
  { name: 'kirocrew', description: 'Autonomous personal AI agent', source: 'kirocrew', model: 'claude-opus-4.8', mcp_servers: ['kirocrew-core', 'kirocrew-cron'], filename: 'kirocrew.json' },
  { name: 'code-reviewer', description: 'Reviews code changes against the repo conventions', source: 'builtin', model: 'claude-sonnet-4.5', mcp_servers: [], filename: 'code-reviewer.json' },
  { name: 'release-captain', description: 'Cuts releases and babysits the pipeline', source: 'builtin', model: 'auto', mcp_servers: [], filename: 'release-captain.json' },
]

const DETAIL = {
  kirocrew: {
    name: 'kirocrew',
    description: 'Autonomous personal AI agent',
    model: 'claude-opus-4.8',
    tools: ['execute_bash', 'fs_read', 'fs_write', 'code', 'grep', 'glob'],
    mcpServers: { 'kirocrew-core': {}, 'kirocrew-cron': {} },
  },
  'code-reviewer': {
    name: 'code-reviewer',
    description: 'Reviews code changes against the repo conventions',
    model: 'claude-sonnet-4.5',
    tools: ['fs_read', 'grep', 'glob'],
    allowedTools: ['fs_read', 'grep'],
  },
  'release-captain': {
    name: 'release-captain',
    description: 'Cuts releases and babysits the pipeline',
    model: 'auto',
    tools: ['fs_read', 'execute_bash'],
  },
}

function installed() {
  return AGENTS.map(a => ({
    ...a,
    skills: [
      ...(mapping[a.name] || []).map(k => k.split('/').pop()),
      ...(UNMANAGED[a.name] || []).map(() => '*'),
    ],
  }))
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1500, height: 950 },
  // Chips and labels are 11–13px type; a 1x shot renders soft on GitHub.
  deviceScaleFactor: 2,
})
const page = await context.newPage()

await page.routeWebSocket(/\/api\/ws/, () => {})

await page.route('**/api/**', async route => {
  const url = new URL(route.request().url())
  const path = url.pathname
  const method = route.request().method()

  if (path.startsWith('/api/agents/detail/')) {
    const name = decodeURIComponent(path.split('/').pop())
    if (method === 'PATCH') {
      const body = JSON.parse(route.request().postData() || '{}')
      if (Array.isArray(body.skills)) mapping[name] = body.skills
      return json(route, { ok: true, model: DETAIL[name]?.model || '', skills: mapping[name] })
    }
    return json(route, {
      ...(DETAIL[name] || { name }),
      skills: mapping[name] || [],
      unmanaged_skills: UNMANAGED[name] || [],
    })
  }
  if (path === '/api/agents/installed') return json(route, installed())
  if (path === '/api/skills') return json(route, SKILLS)
  if (path === '/api/mcp/probe') return json(route, [])
  return stubCommonApi(route, path)
})

page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })

await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
})

async function load() {
  await page.goto(base + '/capabilities?tab=templates', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2400)
}

const shot = async name => {
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
}

/** Crop to the Installed Agents card — the list + detail panel are the story. */
async function card(name, pad = 16) {
  const heading = page.getByRole('heading', { name: /Installed Agents/ })
  if (await heading.count()) {
    const hb = await heading.first().boundingBox()
    if (hb) {
      const y0 = Math.max(0, hb.y - pad * 2)
      await page.screenshot({
        path: `${OUT}/${name}.png`,
        clip: { x: Math.max(0, hb.x - pad * 2), y: y0, width: 1440, height: Math.min(950 - y0, 560) },
      })
      console.log('wrote', `${OUT}/${name}.png`)
      return
    }
  }
  await shot(name)
}

async function selectAgent(name) {
  await page.getByText(name, { exact: true }).first().click()
  await page.waitForTimeout(900)
}

await load()

// 1. An agent with no mapping: the honest empty state, not a silent blank.
await selectAgent('kirocrew')
await shot('01-agent-templates-no-mapping')
await card('02-no-mapping-card')

// 2. An agent with skills already mapped — chips carry catalog display names.
await selectAgent('code-reviewer')
await card('03-mapped-skills-chips')

// 3. The add-skill dropdown, filtered to the catalog minus what's mapped.
await page.getByRole('button', { name: /add skill/i }).click()
await page.waitForTimeout(500)
await shot('04-add-skill-dropdown')

// 4. After adding: the chip row re-renders from the PATCH response.
await page.getByRole('option', { name: /babysit/i }).click()
await page.waitForTimeout(900)
await card('05-after-add')

// 5. A hand-authored wildcard mapping: read-only, no remove control.
await selectAgent('release-captain')
await card('06-unmanaged-wildcard')

await browser.close()
srv.close()
console.log('done')
