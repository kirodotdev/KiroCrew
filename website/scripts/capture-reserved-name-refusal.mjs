/**
 * Screenshot harness for the reserved-template-name refusals.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Three states, each in dark and light. All three did not exist before the
 * change that adds `agent_files.KAS_RESERVED_AGENT_IDS`:
 *   publish-reserved   the crew editor's Agent Template pane, "Save as new
 *                      template…" with `default` typed and submitted; the
 *                      server answers 400 template_name_reserved_by_engine and
 *                      the dialog shows the catalog sentence, still open with
 *                      the typed name
 *   create-reserved    the Agent templates tab, New template with `plan`
 *                      typed and submitted; same refusal in the create dialog
 *   chat-refusal       the chat error row a KAS session start leaves behind
 *                      when the crewmate is bound to a reserved id (the text
 *                      `acp/kas_agents.py` raises, relayed as the turn's error
 *                      with no wrapper prefix)
 *
 * SELF-CHECKS (throw = no stale frame): the refusal sentence is visible in the
 * dialog, the dialog is still open with the typed name, and no dialog other
 * than the intended one is open when the frame is taken.
 *
 * Usage: node scripts/capture-reserved-name-refusal.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/reserved-name-refusal'
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const RESERVED_SENTENCE = /is reserved for a built-in agent\. Pick another name[.,]/

const CREWS = [
  { name: 'default', kiro_agent: 'default-2', workspace: 'default', memory_store: 'default', description: 'Used for all new chats', source: 'user', model: '', triggers: '', session_color: '' },
  { name: 'atlas', kiro_agent: 'atlas', workspace: 'default', memory_store: 'default', description: 'Long-horizon planner', source: 'user', model: '', triggers: '', session_color: '' },
]

const INSTALLED = [
  { name: 'kirocrew', description: 'Built-in', source: 'kirocrew', model: '', skills: [], mcp_servers: ['kirocrew-core'], filename: 'kirocrew.json', kirocrew_owned: true },
  // The crewmate's private copy: the state every customized seeded crewmate is in.
  { name: 'default-2', description: 'Built-in', source: 'user', model: '', skills: [], mcp_servers: ['kirocrew-core'], filename: 'default-2.json', kirocrew_owned: false, forked_from: 'kirocrew', private_to: 'default' },
  { name: 'atlas', description: 'Long-horizon planner', source: 'builtin', model: 'claude-opus-4.8', skills: [], mcp_servers: ['kirocrew-core'], filename: 'atlas.json', kirocrew_owned: false },
]

const tmpl = over => ({
  name: 'x', filename: 'x.json', description: '', model: '', skills: [], mcp_servers: [],
  source: 'builtin', package: '', scope: 'global', kirocrew_owned: false, forked_from: '', private_to: '',
  read_only: null, used_by: [], ...over,
})
const TEMPLATES = [
  tmpl({ name: 'atlas', filename: 'atlas.json', description: 'Long-horizon planner', model: 'claude-opus-4.8', used_by: [{ kind: 'crew', id: 'atlas', label: 'atlas' }] }),
  tmpl({ name: 'kirocrew', filename: 'kirocrew.json', description: 'Built-in', source: 'kirocrew', kirocrew_owned: true, read_only: 'runtime' }),
]

const DETAIL = name => ({
  name,
  model: '',
  description: INSTALLED.find(i => i.name === name)?.description || '',
  prompt: 'You are Kiro.',
  skills: [],
  tools: ['fs_read', 'fs_write', 'execute_bash'],
  allowedTools: ['fs_read'],
  resources: [],
  mcpServers: { 'kirocrew-core': {} },
})

const CFG = {
  ...KIROCREW_CONFIG_FIXTURE,
  agents: Object.fromEntries(CREWS.map(c => [c.name, { kiro_agent: c.kiro_agent, workspace: c.workspace, memory_store: c.memory_store, description: c.description, source: 'config' }])),
  default_agent: 'default',
  workspaces: { default: { dir: '~/.kiro/crew/workspace' } },
  memory_stores: { default: { description: 'Shared memory', embedding_provider: 'bge-m3' } },
}

/** The refusal the server sends for a reserved id, verbatim from the handlers. */
const reservedRefusal = (route, name) =>
  json(route, { error: `'${name}' is reserved by the KAS agent engine`, code: 'template_name_reserved_by_engine' }, 400)

/** The chat error row a KAS session start leaves behind for a crewmate bound to
 *  `default.json`: the text `kas_agents.to_client_custom_agent` raises, wrapped
 *  by the harness as every projection error is. */
const CHAT_SLOT = {
  key: 'default-crewmate',
  title: 'Deploy notes',
  messages: [
    { role: 'user', content: 'Summarize what changed in the deploy branch since Monday.', cls: '', ts: '2026-09-22T09:12:00Z' },
    {
      role: 'error',
      content:
        "Rename this crewmate's template: “default” is reserved for a built-in agent, so the crewmate's own prompt and tools would not run under it. Both fixes are on the crewmate's Agent Template tab: for the crewmate's own copy, 'Save as new template…' under another name (it keeps its customizations) or 'Reset my changes'; for a shared template, the template picker at the top of the tab.",
      cls: '',
      ts: '2026-09-22T09:12:03Z',
    },
  ],
}

async function extra(path, route) {
  const method = route.request().method()
  if (path === '/api/config/kirocrew') return json(route, CFG), true
  if (path === '/api/agents') return json(route, { agents: CREWS, default_agent: 'default' }), true
  if (path === '/api/agents/installed') return json(route, INSTALLED), true
  if (path === '/api/agents/templates' && method === 'POST') {
    const body = route.request().postDataJSON() || {}
    return reservedRefusal(route, body.name), true
  }
  if (path === '/api/agents/templates') return json(route, { templates: TEMPLATES }), true
  if (path.endsWith('/publish') && method === 'POST') {
    const body = route.request().postDataJSON() || {}
    return reservedRefusal(route, body.name), true
  }
  if (path.startsWith('/api/agents/detail/')) {
    return json(route, DETAIL(decodeURIComponent(path.split('/api/agents/detail/')[1] || ''))), true
  }
  if (path === `/api/chat/slots/${CHAT_SLOT.key}`) {
    return json(route, { key: CHAT_SLOT.key, title: CHAT_SLOT.title, running: false, has_more: false, total: CHAT_SLOT.messages.length, messages: CHAT_SLOT.messages }), true
  }
  if (path === '/api/mcp' || path === '/api/mcp/probe') return json(route, []), true
  if (path === '/api/connections/status') return json(route, { schema_version: 1, connections: [] }), true
  if (path === '/api/workspaces') return json(route, { workspaces: [{ name: 'default' }] }), true
  if (path === '/api/skills' || path === '/api/skills/catalog') return json(route, []), true
  if (path === '/api/models') return json(route, { models: [{ name: 'claude-opus-4.8' }] }), true
  return false
}

async function onlyDialogOpen(page, dialog, where) {
  // The frame must show the dialog that sent the name and nothing on top of
  // it: a first-run gate or a second modal would pass a text assertion behind
  // it while hiding what the frame is meant to show.
  const n = await page.getByRole('dialog').count()
  if (n !== 1 || !(await dialog.isVisible())) throw new Error(`${where}: expected exactly one open dialog, saw ${n}`)
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const wrote = []

try {
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, baseURL: base })
    const page = await ctx.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme,
      extra,
      slots: [{ key: CHAT_SLOT.key, messages: CHAT_SLOT.messages.length, running: false, agent: 'default', mode: '' }],
      localStorageEntries: { 'mc-active-slot': CHAT_SLOT.key },
    })

    // ── Create dialog: New template → `plan` ────────────────────────
    await page.goto('/capabilities?tab=templates', { waitUntil: 'domcontentloaded' })
    const main = page.locator('#main-content')
    const newBtn = main.getByRole('button', { name: /New template/ })
    await newBtn.waitFor({ state: 'visible', timeout: 20000 })
    await newBtn.click()
    const createDialog = page.getByRole('dialog', { name: 'New template' })
    await createDialog.waitFor({ state: 'visible', timeout: 15000 })
    await createDialog.getByRole('textbox', { name: 'Name' }).fill('plan')
    await createDialog.getByRole('button', { name: 'Create and edit' }).click()
    const createAlert = createDialog.getByRole('alert')
    await createAlert.waitFor({ state: 'visible', timeout: 15000 })
    if (!RESERVED_SENTENCE.test((await createAlert.textContent()) || '')) throw new Error(`${theme} create: refusal sentence not rendered`)
    if (!/“plan”/.test((await createAlert.textContent()) || '')) throw new Error(`${theme} create: typed name not in the sentence`)
    if ((await createDialog.getByRole('textbox', { name: 'Name' }).inputValue()) !== 'plan') throw new Error(`${theme} create: typed name lost`)
    await onlyDialogOpen(page, createDialog, `${theme} create`)
    await page.waitForTimeout(400)
    let out = `${OUT}/${PREFIX}-${theme}-create-reserved.png`
    await page.screenshot({ path: out }); wrote.push(out)

    // ── Publish dialog: crew editor → Agent Template → Save as new template… → `default`
    await page.goto('/capabilities?tab=crews', { waitUntil: 'domcontentloaded' })
    const card = main.locator('[data-testid="crew-card"]').filter({ hasText: 'Used for all new chats' }).first()
    await card.waitFor({ state: 'visible', timeout: 20000 })
    await card.click()
    const sheet = page.getByRole('dialog')
    await sheet.first().waitFor({ state: 'visible', timeout: 15000 })
    await sheet.first().getByRole('button', { name: /^Agent Template/ }).first().click()
    const saveAs = sheet.first().getByRole('button', { name: /Save as new template/ })
    await saveAs.waitFor({ state: 'visible', timeout: 15000 })
    await saveAs.click()
    const publishDialog = page.getByRole('dialog', { name: 'Save as a new template' })
    await publishDialog.waitFor({ state: 'visible', timeout: 15000 })
    await publishDialog.getByRole('textbox').fill('default')
    await publishDialog.getByRole('button', { name: 'Save template' }).click()
    const publishAlert = publishDialog.getByRole('alert')
    await publishAlert.waitFor({ state: 'visible', timeout: 15000 })
    if (!RESERVED_SENTENCE.test((await publishAlert.textContent()) || '')) throw new Error(`${theme} publish: refusal sentence not rendered`)
    if (!/“default”/.test((await publishAlert.textContent()) || '')) throw new Error(`${theme} publish: typed name not in the sentence`)
    if ((await publishDialog.getByRole('textbox').inputValue()) !== 'default') throw new Error(`${theme} publish: typed name lost`)
    await page.waitForTimeout(400)
    out = `${OUT}/${PREFIX}-${theme}-publish-reserved.png`
    await page.screenshot({ path: out }); wrote.push(out)

    // ── Chat: the session-start refusal as an error row ─────────────
    await page.goto('/chat', { waitUntil: 'domcontentloaded' })
    await page.getByText(/Rename this crewmate's template/).first().waitFor({ state: 'visible', timeout: 20000 })
    if (await page.getByRole('dialog').count()) throw new Error(`${theme} chat: a dialog is open over the transcript`)
    await page.waitForTimeout(600)
    out = `${OUT}/${PREFIX}-${theme}-chat-refusal.png`
    await page.screenshot({ path: out }); wrote.push(out)

    await ctx.close()
  }
} finally {
  await browser.close()
  srv.close()
}
console.log(`wrote ${wrote.join(', ')}`)
