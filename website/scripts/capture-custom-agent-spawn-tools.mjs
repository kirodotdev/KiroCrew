/**
 * Screenshot harness for granting a custom agent Kiro Crew's spawn tools.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Four states, each in dark and light, on the Custom agents tab:
 *   before-grant   an editable custom agent without the spawn tools: the Tools
 *                  section carries the one-click Allow background agents button
 *   draft-unsaved  right after the click: the six spawn chips, each asking
 *                  first, and the Save bar, with nothing saved yet
 *   after-grant    the same agent after saving: the server answers with the
 *                  tools granted and `kirocrew-core` declared, so the MCP
 *                  servers list carries it and the hint is gone
 *   save-refused   a spec whose mcpServers is not an object: the save is
 *                  refused (409 control_plane_not_declarable) and the save bar
 *                  shows the server's sentence beside Save, the draft kept
 *
 * SELF-CHECKS (throw = no stale frame): the hint is visible before and its
 * button's tooltip names the spawn_* family; the draft shows all six chips and
 * no kirocrew-core yet; the PATCH carried all six spawn tools; after the save
 * the hint is gone and the MCP servers list names kirocrew-core; the refusal
 * shows its sentence and keeps the draft.
 *
 * Usage: node scripts/capture-custom-agent-spawn-tools.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { KIROCREW_CONFIG_FIXTURE, json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/custom-agent-spawn-tools'
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const HINT = /run other agents in the background/
const FAMILY = '@kirocrew-core/spawn_*'
const SPAWN_REFS = ['spawn_run', 'spawn_list', 'spawn_status', 'spawn_steer', 'spawn_continue', 'spawn_release'].map(t => `@kirocrew-core/${t}`)

const CREWS = [
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Used for all new chats', source: 'user', model: '', triggers: '', session_color: '' },
]
const INSTALLED = [
  { name: 'kirocrew', description: 'Built-in', source: 'kirocrew', model: '', skills: [], mcp_servers: ['kirocrew-core'], filename: 'kirocrew.json', kirocrew_owned: true },
  { name: 'orchestrator', description: 'Hands work to named agents', source: 'user', model: '', skills: [], mcp_servers: [], filename: 'orchestrator.json', kirocrew_owned: false },
]
const tmpl = over => ({
  name: 'x', filename: 'x.json', description: '', model: '', skills: [], mcp_servers: [],
  source: 'user', package: '', scope: 'global', kirocrew_owned: false, forked_from: '', private_to: '',
  read_only: null, used_by: [], ...over,
})
const TEMPLATES = [
  tmpl({ name: 'orchestrator', filename: 'orchestrator.json', description: 'Hands work to named agents' }),
  tmpl({ name: 'kirocrew', filename: 'kirocrew.json', description: 'Built-in', source: 'kirocrew', kirocrew_owned: true, read_only: 'runtime' }),
]
const CFG = {
  ...KIROCREW_CONFIG_FIXTURE,
  agents: { default: { kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Used for all new chats', source: 'config' } },
  default_agent: 'default',
  workspaces: { default: { dir: '~/.kiro/crew/workspace' } },
  memory_stores: { default: { description: 'Shared memory', embedding_provider: 'bge-m3' } },
}

const { srv, base } = await serveDist()
const browser = await chromium.launch({ executablePath: chromiumExecutable() })
const wrote = []

// The sentence the server answers a save it cannot declare with (409
// control_plane_not_declarable), for a spec whose mcpServers is not an object.
const REFUSAL = "Could not add kirocrew-core: the MCP servers section of this agent's file is malformed, so the tool would never load. Fix that section in the file, then save again."

/** One scenario in one theme: `grant` saves the family, `refused` is the 409. */
const capture = async (theme, scenario) => {
  // The spec on "disk": the PATCH below rewrites it the way the handler does.
  let spec = {
    name: 'orchestrator', model: '', description: 'Hands work to named agents',
    prompt: 'Delegate each task to the agent that owns it and keep the user posted.',
    skills: [], tools: ['fs_read', 'execute_bash'], allowedTools: ['fs_read'], resources: [],
    mcpServers: scenario === 'refused' ? ['hand-edited'] : {},
  }
  let patched = null
  const extra = async (path, route) => {
    const method = route.request().method()
    if (path === '/api/config/kirocrew') return json(route, CFG), true
    if (path === '/api/agents') return json(route, { agents: CREWS, default_agent: 'default' }), true
    if (path === '/api/agents/installed') return json(route, INSTALLED), true
    if (path === '/api/agents/templates') return json(route, { templates: TEMPLATES }), true
    if (path === '/api/agents/detail/orchestrator' && method === 'PATCH') {
      patched = route.request().postDataJSON() || {}
      if (!spec.mcpServers || typeof spec.mcpServers !== 'object' || Array.isArray(spec.mcpServers)) {
        return json(route, { error: REFUSAL, code: 'control_plane_not_declarable', server: 'kirocrew-core' }, 409), true
      }
      spec = { ...spec, ...patched }
      if ((spec.tools || []).some(t => t.startsWith('@kirocrew-core'))) {
        spec = { ...spec, mcpServers: { ...spec.mcpServers, 'kirocrew-core': { command: 'kirocrew', args: ['mcp-core'] } } }
      }
      return json(route, { ok: true }), true
    }
    if (path.startsWith('/api/agents/detail/')) return json(route, spec), true
    if (path === '/api/mcp' || path === '/api/mcp/probe') return json(route, []), true
    if (path === '/api/connections/status') return json(route, { schema_version: 1, connections: [] }), true
    if (path === '/api/workspaces') return json(route, { workspaces: [{ name: 'default' }] }), true
    if (path === '/api/skills' || path === '/api/skills/catalog') return json(route, []), true
    if (path === '/api/models') return json(route, { models: [] }), true
    return false
  }

  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, baseURL: base })
  const page = await ctx.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { theme, extra })
  const shot = async name => {
    await page.waitForTimeout(400)
    const out = `${OUT}/${PREFIX}-${theme}-${name}.png`
    await page.screenshot({ path: out }); wrote.push(out)
  }

  await page.goto('/capabilities?tab=templates', { waitUntil: 'domcontentloaded' })
  const main = page.locator('#main-content')
  await main.getByText('orchestrator', { exact: true }).first().waitFor({ state: 'visible', timeout: 20000 })
  await main.getByText('orchestrator', { exact: true }).first().click()
  const hint = main.getByText(HINT)
  await hint.waitFor({ state: 'visible', timeout: 15000 })
  const addBtn = main.getByRole('button', { name: /Allow background agents/ })
  if ((await addBtn.getAttribute('title')) !== FAMILY) throw new Error(`${theme} before: the hint's button tooltip does not name ${FAMILY}`)
  if (scenario === 'grant') {
    await hint.scrollIntoViewIfNeeded()
    await shot('before-grant')
  }

  await addBtn.click()
  const saveBtn = main.getByRole('button', { name: 'Save custom agent' })
  await saveBtn.waitFor({ state: 'visible', timeout: 15000 })
  for (const ref of SPAWN_REFS) {
    await main.getByText(ref, { exact: true }).first().waitFor({ state: 'visible', timeout: 15000 })
  }
  const lastChip = main.getByText(SPAWN_REFS[SPAWN_REFS.length - 1], { exact: true }).first()
  // Centred, so the floating Save bar never covers the last chip.
  const centreChips = () => lastChip.evaluate(el => el.scrollIntoView({ block: 'center' }))
  if (scenario === 'grant') {
    // The draft before saving: six chips and the Save bar, and no
    // kirocrew-core under MCP servers yet.
    if (await main.getByText('kirocrew-core', { exact: true }).count()) throw new Error(`${theme} draft: kirocrew-core listed before the save`)
    await centreChips()
    await shot('draft-unsaved')
  }

  await saveBtn.click()
  if (scenario === 'refused') {
    const alert = main.getByRole('alert')
    await alert.waitFor({ state: 'visible', timeout: 15000 })
    if (!((await alert.textContent()) || '').includes(REFUSAL)) throw new Error(`${theme} refused: the save bar does not show the refusal`)
    if (!(await saveBtn.isVisible())) throw new Error(`${theme} refused: the draft was dropped`)
    await centreChips()
    await shot('save-refused')
    await ctx.close()
    return
  }
  await main.getByText('Custom agent saved.').waitFor({ state: 'visible', timeout: 15000 })
  const sent = (patched && patched.tools) || []
  const unsent = SPAWN_REFS.filter(ref => !sent.includes(ref))
  if (unsent.length) throw new Error(`${theme}: the save did not send ${unsent.join(', ')}`)
  await main.getByText('kirocrew-core', { exact: true }).first().waitFor({ state: 'visible', timeout: 15000 })
  if (await main.getByText(HINT).count()) throw new Error(`${theme} after: hint still shown once granted`)
  // The saved confirmation sits over the MCP servers row until it fades.
  await main.getByText('Custom agent saved.').waitFor({ state: 'detached', timeout: 15000 }).catch(() => {})
  await main.getByText('kirocrew-core', { exact: true }).first().scrollIntoViewIfNeeded()
  await shot('after-grant')
  await ctx.close()
}

try {
  for (const theme of ['dark', 'light']) {
    await capture(theme, 'grant')
    await capture(theme, 'refused')
  }
} finally {
  await browser.close()
  srv.close()
}
for (const w of wrote) console.log('wrote', w)
