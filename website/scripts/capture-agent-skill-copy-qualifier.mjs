/**
 * Screenshot harness + behaviour check for the colliding-copy qualifier in the
 * agent skills editor.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server with
 * every /api/** call answered from fixtures: minting a dashboard token is
 * refused for an agent, so a live gateway would only photograph an error state.
 *
 * Scenes: 1 the bound chip's "Located in" qualifier; 2 both colliding copies in
 * the picker; 3 a mapping whose copy is gone; 4 a failed catalog read.
 *
 * Usage: node scripts/capture-agent-skill-copy-qualifier.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/agent-skill-copy-qualifier'
mkdirSync(OUT, { recursive: true })

const CREWS = [
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Used for all new chats', source: 'user', model: '', triggers: '', session_color: '' },
  { name: 'atlas', kiro_agent: 'atlas', workspace: 'default', memory_store: 'default', description: 'Long-horizon planner', source: 'user', model: '', triggers: 'migration', session_color: '' },
]

const INSTALLED = [
  { name: 'kirocrew', description: 'Built-in', source: 'kirocrew', model: '', skills: [], mcp_servers: ['kirocrew-core'], filename: 'kirocrew.json', kirocrew_owned: true },
  { name: 'atlas', description: 'Long-horizon planner', source: 'builtin', model: 'claude-opus-4.8', skills: [], mcp_servers: ['kirocrew-core'], filename: 'atlas.json', kirocrew_owned: false },
]

/* Two bundles ship a `code-review` skill, so its name alone cannot address either
   copy. `source: 'package'` is what makes the pair a collision: the qualifier is
   for colliding BUNDLES, and a user's own same-named copy is not an ambiguity. */
const PAPYRUS_COPY = 'package/3f2a91c4e7b05d68:code-review'
const OSPRO_COPY = 'package/9d17b8402fc6ea35:code-review'
/* Mapped in scene 3 and deliberately absent from the catalog: the copy it named
   is no longer installed. */
const RETIRED_COPY = 'package/aa11bb22cc33dd44:code-review'

const CATALOG = [
  {
    key: PAPYRUS_COPY,
    name: 'code-review',
    description: 'Review a change against the paper-writing conventions.',
    source: 'package',
    path: '/home/user/.kiro/crew/skills/papyrus-writer/code-review/SKILL.md',
  },
  {
    key: OSPRO_COPY,
    name: 'code-review',
    description: 'Review a change against the team review bar.',
    source: 'package',
    path: '/home/user/.kiro/crew/skills/ospro-team/code-review/SKILL.md',
  },
  {
    key: 'memory',
    name: 'memory',
    description: 'Recall and record durable facts.',
    source: 'user',
    path: '/home/user/.kiro/skills/memory/SKILL.md',
  },
  {
    key: 'package/5c0b7ae913f4d2a6:grill',
    name: 'grill',
    description: 'Interrogate a plan before committing to it.',
    source: 'package',
    path: '/home/user/.kiro/crew/skills/atlas-pack/grill/SKILL.md',
  },
]

const BASE_DETAIL = {
  name: 'atlas',
  model: 'claude-opus-4.8',
  prompt: 'Plan before acting. Write the plan to the workspace first, then execute it step by step.',
  tools: ['fs_read', 'fs_write', 'execute_bash'],
  allowedTools: ['fs_read'],
  mcpServers: { 'kirocrew-core': {} },
  unmanaged_skills: [],
}

/** Land on the Agent Template pane, which is where AgentSkillsEditor mounts. */
async function openSkillsPane(page) {
  await page.goto('/capabilities?tab=crews', { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').getByText('Agents you chat with', { exact: false })
    .waitFor({ state: 'visible', timeout: 15000 })
  const card = page.getByRole('button', { name: /Edit (crew|agent) atlas/i })
  await card.waitFor({ state: 'visible', timeout: 15000 })
  await card.click()
  const sheet = page.getByRole('dialog', { name: /edit (crew|agent)/i })
  await sheet.waitFor({ state: 'visible', timeout: 10000 })
  await sheet.getByRole('button', { name: /agent template/i }).first().click()
  await sheet.getByRole('button', { name: /add skill/i }).first()
    .waitFor({ state: 'visible', timeout: 15000 })
  return sheet
}

/**
 * `skills` seeds the agent's mapped set. `catalog` is either the literal string
 * 'fail' -- the route is fulfilled with a 500 so react-query settles isError,
 * which is a different state from an empty array -- or the rows to serve.
 */
async function scene(browser, base, { skills, catalog }) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, baseURL: base })
  const page = await ctx.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/config/kirocrew') { await json(route, KIROCREW_CONFIG_FIXTURE); return true }
      if (path === '/api/agents') { await json(route, { agents: CREWS, default_agent: 'default' }); return true }
      if (path === '/api/agents/installed') { await json(route, INSTALLED); return true }
      if (path.startsWith('/api/agents/detail/')) {
        const name = decodeURIComponent(path.split('/api/agents/detail/')[1] || '')
        if (name !== 'atlas') { await json(route, { name, skills: [], unmanaged_skills: [] }); return true }
        await json(route, { ...BASE_DETAIL, skills })
        return true
      }
      if (path.startsWith('/api/skills')) {
        if (catalog === 'fail') {
          await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"catalog unavailable"}' })
          return true
        }
        await json(route, catalog)
        return true
      }
      if (path === '/api/models') { await json(route, { models: [{ name: 'claude-opus-4.8' }] }); return true }
      return false
    },
  })
  const sheet = await openSkillsPane(page)
  await page.waitForTimeout(600)
  return { ctx, page, sheet }
}

const { srv, base } = await serveDist()
// A version-manager node shim puts node's older bundled libstdc++ on
// LD_LIBRARY_PATH; an inheriting browser then dies loading the system mesa.
const { LD_LIBRARY_PATH: _nodeOwnLibs, ...BROWSER_ENV } = process.env
const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: BROWSER_ENV })
const failures = []

try {
  /* ── Scene 1: the bound chip names which copy it bound ── */
  {
    const { ctx, sheet } = await scene(browser, base, { skills: [PAPYRUS_COPY, 'memory'], catalog: CATALOG })
    const located = sheet.getByText(/Located in /)
    if (await located.count() < 1) failures.push('scene 1: mapped chip carries no "Located in" qualifier')
    if (!(await sheet.getByText(/papyrus-writer/).count())) {
      failures.push('scene 1: qualifier does not name the bound copy\u2019s own path tail')
    }
    // The plain row must stay plain: a qualifier on every chip would be noise.
    if (await sheet.getByText(/Located in /).count() > 1) {
      failures.push('scene 1: a non-colliding skill was given a qualifier too')
    }
    await sheet.screenshot({ path: `${OUT}/1-mapped-copy-qualifier.png` })
    console.log('wrote', `${OUT}/1-mapped-copy-qualifier.png`)
    await ctx.close()
  }

  /* ── Scene 2: both colliding copies are separately addressable in the picker ── */
  {
    const { ctx, page, sheet } = await scene(browser, base, { skills: ['memory'], catalog: CATALOG })
    await sheet.getByRole('button', { name: /add skill/i }).first().click()
    const list = page.getByRole('listbox', { name: /available skills/i })
    await list.waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(400)
    const rows = list.getByRole('option', { name: /code-review/i })
    if (await rows.count() !== 2) {
      failures.push(`scene 2: expected 2 colliding code-review rows, saw ${await rows.count()}`)
    }
    for (const want of ['papyrus-writer', 'ospro-team']) {
      if (!(await list.getByText(new RegExp(want)).count())) {
        failures.push(`scene 2: picker row never named the ${want} copy`)
      }
    }
    await list.screenshot({ path: `${OUT}/2-picker-both-copies.png` })
    console.log('wrote', `${OUT}/2-picker-both-copies.png`)
    await page.keyboard.press('Escape')
    await ctx.close()
  }

  /* ── Scene 3: a mapping whose copy is gone reads as a warning, not as healthy ── */
  {
    const { ctx, sheet } = await scene(browser, base, { skills: [RETIRED_COPY, 'memory'], catalog: CATALOG })
    const note = sheet.getByText(/no longer match(es)? an installed copy/)
    if (await note.count() < 1) failures.push('scene 3: unresolved mapping shows no visible count line')
    // `memory` resolving proves the catalog really loaded: `unresolved` is gated on
    // the query succeeding, so an unserved catalog paints every mapping warn.
    if (!(await sheet.getByText('memory', { exact: false }).count())) {
      failures.push('scene 3: catalog did not load, so the warn state proves nothing')
    }
    await sheet.screenshot({ path: `${OUT}/3-unresolved-mapping.png` })
    console.log('wrote', `${OUT}/3-unresolved-mapping.png`)
    await ctx.close()
  }

  /* ── Scene 4: a failed catalog read is stated, not shown as an empty list ── */
  {
    const { ctx, sheet } = await scene(browser, base, { skills: ['memory'], catalog: 'fail' })
    // react-query retries a 500 with backoff before it settles isError, so a fixed
    // sleep photographs the still-loading state and the notice reads as absent.
    const notice = sheet.getByText(/Could not load the skill catalog/)
    try {
      await notice.first().waitFor({ state: 'visible', timeout: 30000 })
    } catch {
      failures.push('scene 4: catalog failure renders no notice')
    }
    await sheet.screenshot({ path: `${OUT}/4-catalog-load-failure.png` })
    console.log('wrote', `${OUT}/4-catalog-load-failure.png`)
    await ctx.close()
  }
} finally {
  await browser.close()
  srv.close()
}

if (failures.length) {
  for (const f of failures) console.error('FAIL:', f)
  process.exit(1)
}
console.log('PASS: colliding copies are separately addressable, and a dead mapping is marked')
