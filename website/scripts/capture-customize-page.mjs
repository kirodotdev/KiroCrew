/**
 * Screenshot harness for the Customize page (formerly "Agent Capabilities")
 * and the two controls that moved off it.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Frames, each in dark and light:
 *   crewmates       — /capabilities?tab=crews: rail says "Crewmates" /
 *                     "Your AI teammates", page title "Customize", roster first
 *                     (no memory notice, no "New sessions use" row)
 *   editor-built-from — the crewmate editor sheet open on a card: the field
 *                     reads "Built from"
 *   custom-agents   — /capabilities?tab=templates: flat pane, filter and
 *                     "New custom agent" on one row, list + detail
 *   connections-restart — /capabilities?tab=mcp: Apply & Restart in the
 *                     Connections header, right of the Services / MCP servers
 *                     tablist and outside it
 *   default-crewmate — Developer → Config, reached through the roster's Change
 *                     default crewmate link: the Default crewmate select under the agents
 *                     table, ringed on arrival, where the default is changed now
 *   add-crewmate    — the Add crewmate sheet: the Built from combobox, its
 *                     "you can change what this crewmate is built from" note,
 *                     and the "Choose a custom agent to build from" refusal
 *                     when Create is pressed with no custom agent picked
 *   delete-crewmate — the editor's Danger zone with the two-step confirm armed
 *                     ("Delete crewmate atlas? …")
 *   custom-agent-delete — the custom agent's More actions menu open on Delete;
 *                     the native confirm cannot be painted, so its text is
 *                     asserted from the dialog event instead
 *   custom-agents-dirty — a custom agent's prompt edited: the Save bar with
 *                     "Save custom agent" and its hint
 *   empty-crewmates / empty-custom-agents — both tabs with nothing installed
 *   composer-picker — /chat with the Agent selector open: the custom agents
 *                     list (the "Custom agents" header is chrome the picker
 *                     drops when crewmates are withheld; the group keeps that
 *                     accessible name)
 *   default-crewmate-refused — the Default crewmate select after the server
 *                     refused the pick: the inline notice beside the row and the
 *                     select back on the stored default
 *   one-crewmate    — roster with a single crewmate (no Change default crewmate
 *                     link) and the Config row's one-option select
 *   rail-footer-wrapped — the rail footer in a long-word locale: "Report
 *                     issue" on its own full-width line, not truncated
 *   narrow-custom-agents / narrow-connections — the two toolbars at a 320px
 *                     viewport: filter above New custom agent; tabs above
 *                     Apply & Restart; nothing overflows the viewport
 *   restart-failed  — Connections after Apply & Restart was pressed and the
 *                     server refused: the failure sits in the header band as a
 *                     dismissable inline notice (no hand-off there — the MCP
 *                     servers tab beneath holds an editable form)
 *
 * SELF-CHECKS (throw = no stale frame) assert each contract above, and that
 * the removed copy ("Agent Template", "New sessions use", "Apply & Restart")
 * is absent from Customize.
 *
 * Usage: node scripts/capture-customize-page.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/customize-page'
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const CREWS = [
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Used for all new chats', source: 'builtin', model: '', triggers: '', session_color: '' },
  { name: 'atlas', kiro_agent: 'atlas', workspace: 'default', memory_store: 'default', description: 'Long-horizon planner', source: 'builtin', model: '', triggers: 'migration', session_color: '' },
  { name: 'reviewer', kiro_agent: 'reviewer', workspace: 'reviews', memory_store: 'reviews', description: 'Careful code reviewer', source: 'builtin', model: '', triggers: '', session_color: '' },
]

const INSTALLED = [
  { name: 'kirocrew', description: 'Built-in', source: 'kirocrew', model: '', skills: ['memory', 'artifacts'], mcp_servers: ['kirocrew-core'], filename: 'kirocrew.json', kirocrew_owned: true },
  { name: 'atlas', description: 'Long-horizon planner', source: 'builtin', model: 'claude-opus-4.8', skills: ['grill'], mcp_servers: ['kirocrew-core'], filename: 'atlas.json', kirocrew_owned: false },
  { name: 'reviewer', description: 'Careful code reviewer', source: 'builtin', model: 'claude-sonnet-4.5', skills: ['adversarial-review'], mcp_servers: ['kirocrew-core'], filename: 'reviewer.json', kirocrew_owned: false },
]

const tmpl = over => ({
  name: 'x', filename: 'x.json', description: '', model: '', skills: [], mcp_servers: [],
  source: 'builtin', package: '', scope: 'global', kirocrew_owned: false, forked_from: '', private_to: '',
  read_only: null, used_by: [], ...over,
})
const TEMPLATES = [
  tmpl({ name: 'reviewer', filename: 'reviewer.json', description: 'Careful code reviewer', model: 'claude-sonnet-4.5', source: 'builtin', skills: ['adversarial-review'], mcp_servers: ['kirocrew-core'], used_by: [{ kind: 'crew', id: 'reviewer', label: 'reviewer' }] }),
  tmpl({ name: 'atlas', filename: 'atlas.json', description: 'Long-horizon planner', model: 'claude-opus-4.8', skills: ['grill'], mcp_servers: ['kirocrew-core'], used_by: [{ kind: 'crew', id: 'atlas', label: 'atlas' }] }),
  tmpl({ name: 'kirocrew', filename: 'kirocrew.json', description: 'Built-in', source: 'kirocrew', kirocrew_owned: true, read_only: 'runtime', skills: ['memory', 'artifacts'], mcp_servers: ['kirocrew-core'], used_by: [{ kind: 'crew', id: 'default', label: 'default' }] }),
  tmpl({ name: 'papyrus-writer', filename: 'Papyrus-papyrus-writer.json', description: 'LaTeX co-author', source: 'package', package: 'Papyrus', read_only: 'package' }),
  // Nothing points at this one, so Delete reaches the confirm rather than the
  // "cannot delete" holder list.
  tmpl({ name: 'draft-writer', filename: 'draft-writer.json', description: 'Turns notes into a first draft', model: 'claude-sonnet-4.5', source: 'builtin', skills: [], mcp_servers: ['kirocrew-core'] }),
]

/** Flipped for the empty-state frames: both lists answer empty. */
let EMPTY = false
/** Flipped for the one-crewmate frames: the roster and the config hold `default` only. */
let SINGLE = false
/** Flipped for the refused-default frame: the default-agent write answers a refusal. */
let REFUSE_DEFAULT = false
/** Flipped for the restart-failed frame: the restart route answers 500. */
let FAIL_RESTART = false

/** The composer's catalog: custom agents only, the way the picker shows it
 *  while crewmate choices are withheld. */
const CATALOG = TEMPLATES.map(t => ({
  name: t.name, selection_kind: 'template', source: t.source, description: t.description, scope: 'global',
  kiro_agent: t.name, workspace: 'default', memory_store: 'default', model: t.model, reasoning_effort: '', triggers: '', session_color: '', avatar: {},
}))

/** One prompt per fixture agent, so a pane is recognisably its own. */
const PROMPTS = {
  kirocrew: 'file://~/.kiro/agents/prompts/kirocrew.md',
  atlas: 'Plan in milestones before touching code. Keep a running ledger of what is done, what is next and what was rejected, and re-read it before every step.',
  reviewer: 'Review the diff before the description. Name the file and line for every finding, and say what would make it pass.',
  'papyrus-writer': 'Co-author LaTeX. Keep the macros the document already defines, cite with the bibliography keys in place, and never rewrite a paragraph the author did not ask about.',
  'draft-writer': 'Turn rough notes into a first draft in the author\'s voice. Keep every fact from the notes, mark anything you had to guess, and stop at one draft — no polish pass.',
}
const DETAIL = name => ({
  name, model: INSTALLED.find(i => i.name === name)?.model || '',
  description: INSTALLED.find(i => i.name === name)?.description || '',
  prompt: PROMPTS[name] || `You are ${name}.`,
  skills: INSTALLED.find(i => i.name === name)?.skills || [],
  tools: ['fs_read', 'fs_write', 'execute_bash', 'glob', 'grep'],
  allowedTools: ['fs_read', 'glob'],
  resources: ['file://AGENTS.md'],
  mcpServers: { 'kirocrew-core': {} },
  toolsSettings: { execute_bash: { deniedCommands: ['rm -rf /', 'git push --force'] } },
})

/** The Kiro Crew config, with several crewmates so the Default crewmate row shows. */
const CFG = {
  ...KIROCREW_CONFIG_FIXTURE,
  agents: Object.fromEntries(CREWS.map(c => [c.name, { kiro_agent: c.kiro_agent, workspace: c.workspace, memory_store: c.memory_store, description: c.description, source: 'config' }])),
  default_agent: 'default',
  connections_ui: true,
  workspaces: { default: { dir: '~/.kiro/crew/workspace' }, reviews: { dir: '~/Repos/reviews' } },
  memory_stores: { default: { description: 'Shared memory', embedding_provider: 'bge-m3' }, reviews: { description: '', embedding_provider: '' } },
}

async function extra(path, route) {
  if (path === '/api/config/kirocrew') return json(route, SINGLE ? { ...CFG, agents: { default: CFG.agents.default } } : CFG), true
  if (path === '/api/config/default-agent' && route.request().method() === 'PUT') {
    return json(route, REFUSE_DEFAULT ? { error: 'unknown agent: reviewer' } : { ok: true }), true
  }
  if (path === '/api/agents') return json(route, { agents: EMPTY ? [] : SINGLE ? CREWS.slice(0, 1) : CREWS, default_agent: 'default' }), true
  if (path === '/api/sessions/restart' && route.request().method() === 'POST') {
    return FAIL_RESTART
      ? json(route, { error: 'restart refused: the agent process is still shutting down' }, 500)
      : json(route, { ok: true, sessions_reset: 2, mcp_synced: 1, mcp_sync_ok: true }), true
  }
  if (path === '/api/agents/catalog') return json(route, { agents: CATALOG, default_agent: 'default' }), true
  if (path === '/api/agents/installed') return json(route, EMPTY ? [] : INSTALLED), true
  if (path === '/api/agents/templates') return json(route, { templates: EMPTY ? [] : TEMPLATES }), true
  if (path.startsWith('/api/agents/detail/')) {
    return json(route, DETAIL(decodeURIComponent(path.split('/api/agents/detail/')[1] || ''))), true
  }
  if (path === '/api/mcp' || path === '/api/mcp/probe') return json(route, []), true
  if (path === '/api/connections/status') return json(route, { schema_version: 1, connections: [] }), true
  if (path === '/api/workspaces') return json(route, { workspaces: Object.keys(CFG.workspaces).map(name => ({ name })) }), true
  if (path === '/api/skills' || path === '/api/skills/catalog') return json(route, []), true
  if (path === '/api/models') return json(route, { models: [{ name: 'claude-opus-4.8' }, { name: 'claude-sonnet-4.5' }] }), true
  return false
}

const REMOVED = [/Agent Template/i, /New sessions use/i, /Agent Capabilities/]

async function assertRemovedCopyGone(page, where) {
  const main = page.locator('#main-content')
  for (const re of REMOVED) {
    if (await main.getByText(re).count()) throw new Error(`${where}: removed copy "${re}" is still rendered`)
  }
  // The CONTROL is gone from Customize; the Save bar hint may still name it as
  // the remedy that lives on Connections, so this is a role check, not a text one.
  if (await main.getByRole('button', { name: /Apply & Restart/i }).count()) throw new Error(`${where}: an Apply & Restart button is still rendered`)
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const wrote = []

try {
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, baseURL: base })
    const page = await ctx.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { theme, extra })

    // ── Crewmates tab ────────────────────────────────────────────────
    await page.goto('/capabilities?tab=crews', { waitUntil: 'domcontentloaded' })
    const main = page.locator('#main-content')
    await main.getByText('Customize', { exact: true }).first().waitFor({ state: 'visible', timeout: 15000 })
    // The rail rows are plain buttons; the two renamed ones must both be there.
    const railTab = label => main.getByRole('button', { name: label, exact: true }).first()
    for (const label of ['Crewmates', 'Custom agents']) {
      await railTab(label).waitFor({ state: 'visible', timeout: 15000 })
    }
    await main.getByText('Your AI teammates').first().waitFor({ state: 'visible', timeout: 15000 })
    await main.locator('[data-testid="crew-card"]').first().waitFor({ state: 'visible', timeout: 15000 })
    // The toolbar link is the roster's pointer to where the default is changed.
    const changeLink = main.getByRole('link', { name: /^Change default crewmate/ })
    await changeLink.waitFor({ state: 'visible', timeout: 15000 })
    if (!/developer\?tab=config&highlight=key%3Adefault-crewmate$/.test((await changeLink.getAttribute('href')) || '')) {
      throw new Error(`${theme} crewmates: Change default crewmate does not link to the Default crewmate row`)
    }
    // The default card's own name must stay readable beside its badge.
    const defaultName = main.locator('[data-testid="crew-card"]').filter({ hasText: 'Used for all new chats' }).locator('span.truncate').first()
    const nameBox = await defaultName.evaluate(el => ({ cw: el.clientWidth, sw: el.scrollWidth }))
    if (nameBox.cw < nameBox.sw) throw new Error(`${theme} crewmates: default card name is truncated (${nameBox.cw} < ${nameBox.sw})`)
    await assertRemovedCopyGone(page, `${theme} crewmates`)
    await page.waitForTimeout(400)
    let out = `${OUT}/${PREFIX}-${theme}-crewmates.png`
    await page.screenshot({ path: out }); wrote.push(out)

    // ── Crewmate editor: the field is "Built from" ───────────────────
    await main.locator('[data-testid="crew-card"]').filter({ hasText: 'atlas' }).first().click()
    const sheet = page.getByRole('dialog')
    await sheet.waitFor({ state: 'visible', timeout: 15000 })
    // The rail names the pane "Built from"; the pane's header select carries the
    // same accessible name.
    await sheet.getByRole('button', { name: /^Built from/ }).first().click()
    await sheet.getByRole('combobox', { name: 'Built from' }).waitFor({ state: 'visible', timeout: 15000 })
    if (await sheet.getByText(/Agent Template/).count()) throw new Error(`${theme} editor: "Agent Template" still rendered`)
    await page.waitForTimeout(400) // the sheet slides in
    out = `${OUT}/${PREFIX}-${theme}-editor-built-from.png`
    await page.screenshot({ path: out }); wrote.push(out)
    await page.keyboard.press('Escape')
    await sheet.waitFor({ state: 'hidden', timeout: 10000 })

    // ── Custom agents tab ────────────────────────────────────────────
    await railTab('Custom agents').click()
    await main.getByText('What a crewmate is built from', { exact: false }).first().waitFor({ state: 'visible', timeout: 15000 })
    const newBtn = main.getByRole('button', { name: 'New custom agent' })
    await newBtn.waitFor({ state: 'visible', timeout: 15000 })
    const filter = main.getByRole('searchbox').or(main.getByPlaceholder(/filter|search/i)).first()
    await filter.waitFor({ state: 'visible', timeout: 15000 })
    // Same toolbar row: the filter and the primary button share a vertical band.
    const [fb, bb] = [await filter.boundingBox(), await newBtn.boundingBox()]
    if (!fb || !bb || Math.abs((fb.y + fb.height / 2) - (bb.y + bb.height / 2)) > 12) {
      throw new Error(`${theme} custom-agents: filter and New custom agent are not on one row (${JSON.stringify(fb)} vs ${JSON.stringify(bb)})`)
    }
    if (await main.getByText(/^Glossary$|^Template$|^Chat$/).count()) throw new Error(`${theme} custom-agents: glossary rows still rendered`)
    await main.getByRole('option', { name: /^reviewer\b/ }).waitFor({ state: 'visible', timeout: 15000 })
    await main.getByText(/Careful code reviewer/).first().waitFor({ state: 'visible', timeout: 15000 })
    await assertRemovedCopyGone(page, `${theme} custom-agents`)
    await page.waitForTimeout(400)
    out = `${OUT}/${PREFIX}-${theme}-custom-agents.png`
    await page.screenshot({ path: out }); wrote.push(out)

    // ── Connections: Apply & Restart in its new home ─────────────────
    await railTab('Connections').click()
    const tablist = main.getByRole('tablist', { name: /connection views/i })
    await tablist.waitFor({ state: 'visible', timeout: 15000 })
    const restart = main.getByRole('button', { name: 'Apply & Restart' })
    await restart.waitFor({ state: 'visible', timeout: 15000 })
    // Same header band as the tablist, to its right, and not inside the tablist.
    const [tb, rb] = [await tablist.boundingBox(), await restart.boundingBox()]
    if (!tb || !rb || rb.x < tb.x + tb.width || Math.abs((tb.y + tb.height / 2) - (rb.y + rb.height / 2)) > 24) {
      throw new Error(`${theme} connections: Apply & Restart is not beside the tablist (${JSON.stringify(tb)} vs ${JSON.stringify(rb)})`)
    }
    if (await tablist.getByRole('button', { name: 'Apply & Restart' }).count()) throw new Error(`${theme} connections: Apply & Restart leaked into the tablist`)
    await page.waitForTimeout(400)
    out = `${OUT}/${PREFIX}-${theme}-connections-restart.png`
    await page.screenshot({ path: out }); wrote.push(out)

    // ── Default crewmate row (Settings → Developer → Kiro Crew config) ──
    // Arrive the way a user does: through the roster's Change default crewmate
    // link, so the ring on the row is in the frame.
    await railTab('Crewmates').click()
    await main.getByRole('link', { name: /^Change default crewmate/ }).click()
    await page.waitForURL(/\/developer\?tab=config/, { timeout: 15000 })
    const sel = page.getByRole('combobox', { name: 'Default crewmate' }).or(page.getByRole('button', { name: 'Default crewmate' })).first()
    await sel.waitFor({ state: 'visible', timeout: 15000 })
    if (!/default/.test((await sel.textContent()) || '')) throw new Error(`${theme} default-crewmate: select does not show the configured default`)
    await sel.scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)
    out = `${OUT}/${PREFIX}-${theme}-default-crewmate.png`
    await page.screenshot({ path: out }); wrote.push(out)

    // ── Add crewmate: Built from combobox, edit-later note, refusal ────
    await page.goto('/capabilities?tab=crews', { waitUntil: 'domcontentloaded' })
    await main.locator('[data-testid="crew-card"]').first().waitFor({ state: 'visible', timeout: 15000 })
    await main.getByTestId('new-crew').click()
    const addSheet = page.getByRole('dialog')
    await addSheet.waitFor({ state: 'visible', timeout: 15000 })
    await addSheet.getByRole('combobox', { name: 'Built from' }).waitFor({ state: 'visible', timeout: 15000 })
    await addSheet.getByText(/you can change what this crewmate is built from/).waitFor({ state: 'visible', timeout: 15000 })
    // A name but no custom agent: Create must refuse in the new words.
    await addSheet.getByPlaceholder('e.g. oncall').first().fill('oncall-scout')
    await addSheet.getByRole('button', { name: 'Create', exact: true }).click()
    const hint = addSheet.getByTestId('crew-sheet-hint')
    await hint.waitFor({ state: 'visible', timeout: 15000 })
    if ((await hint.textContent())?.trim() !== 'Choose a custom agent to build from') throw new Error(`${theme} add-crewmate: refusal reads "${await hint.textContent()}"`)
    if (await addSheet.getByText(/Agent Template/).count()) throw new Error(`${theme} add-crewmate: "Agent Template" still rendered`)
    await page.waitForTimeout(400)
    out = `${OUT}/${PREFIX}-${theme}-add-crewmate.png`
    await page.screenshot({ path: out }); wrote.push(out)
    await page.keyboard.press('Escape')
    await addSheet.waitFor({ state: 'hidden', timeout: 10000 })

    // ── Delete crewmate: the Danger zone's armed confirm ─────────────
    await main.locator('[data-testid="crew-card"]').filter({ hasText: 'atlas' }).first().click()
    const editSheet = page.getByRole('dialog')
    await editSheet.waitFor({ state: 'visible', timeout: 15000 })
    await editSheet.getByRole('tab', { name: /^Danger zone/ }).click()
    await editSheet.getByRole('button', { name: 'Delete crewmate', exact: true }).click()
    await editSheet.getByTestId('confirm-delete-crew').waitFor({ state: 'visible', timeout: 15000 })
    await editSheet.getByText(/^Delete crewmate atlas\? New chats will no longer use it\./).waitFor({ state: 'visible', timeout: 15000 })
    if (await editSheet.getByText(/Delete crew\b(?!mate)/).count()) throw new Error(`${theme} delete-crewmate: old "Delete crew" copy still rendered`)
    await page.waitForTimeout(400)
    out = `${OUT}/${PREFIX}-${theme}-delete-crewmate.png`
    await page.screenshot({ path: out }); wrote.push(out)
    // The dismiss names what it keeps, not "Cancel" (the footer has one of those).
    if ((await editSheet.getByTestId('cancel-delete-crew').textContent())?.trim() !== 'Keep crewmate') throw new Error(`${theme} delete-crewmate: confirm dismiss does not read Keep crewmate`)
    await editSheet.getByTestId('cancel-delete-crew').click()
    await page.keyboard.press('Escape')
    await editSheet.waitFor({ state: 'hidden', timeout: 10000 })

    // ── Custom agent delete: menu open on Delete; confirm text asserted ──
    await railTab('Custom agents').click()
    await main.getByRole('option', { name: /^draft-writer\b/ }).click()
    await main.getByText('Turns notes into a first draft').first().waitFor({ state: 'visible', timeout: 15000 })
    await main.getByRole('button', { name: 'More actions' }).click()
    const menu = page.getByRole('menu')
    await menu.getByRole('menuitem', { name: 'Delete', exact: true }).waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(300)
    out = `${OUT}/${PREFIX}-${theme}-custom-agent-delete.png`
    await page.screenshot({ path: out }); wrote.push(out)
    // The confirm is window.confirm: not paintable, so its text is the evidence.
    const confirmText = new Promise(resolve => page.once('dialog', d => { resolve(d.message()); void d.dismiss() }))
    await menu.getByRole('menuitem', { name: 'Delete', exact: true }).click()
    const msg = await Promise.race([confirmText, page.waitForTimeout(5000).then(() => null)])
    if (msg !== 'Delete the custom agent “draft-writer”? Nothing points at it any more, so only its file is removed from ~/.kiro/agents; chats that used it keep their history.') {
      throw new Error(`${theme} custom-agent-delete: confirm reads ${JSON.stringify(msg)}`)
    }
    console.log(`${theme} custom-agent-delete confirm: ${msg}`)

    // ── Custom agent edited: the Save bar ─────────────────────────────
    await main.getByRole('option', { name: /^reviewer\b/ }).click()
    const prompt = main.getByRole('textbox', { name: 'Prompt' })
    await prompt.waitFor({ state: 'visible', timeout: 15000 })
    await prompt.fill((await prompt.inputValue()) + '\nAlways end with the one question a reviewer would ask.')
    const saveBtn = main.getByRole('button', { name: 'Save custom agent' })
    await saveBtn.waitFor({ state: 'visible', timeout: 15000 })
    await main.getByText(/^Press Save custom agent to keep these edits\./).waitFor({ state: 'visible', timeout: 15000 })
    if (await main.getByText(/Save template|Save agent template/).count()) throw new Error(`${theme} custom-agents-dirty: old Save copy still rendered`)
    await saveBtn.scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)
    out = `${OUT}/${PREFIX}-${theme}-custom-agents-dirty.png`
    await page.screenshot({ path: out }); wrote.push(out)
    // Drop the draft through the page's own Discard (its confirm is native).
    page.once('dialog', d => void d.accept())
    await main.getByRole('button', { name: 'Discard', exact: true }).click()
    await saveBtn.waitFor({ state: 'hidden', timeout: 10000 })

    // ── Empty states: nothing installed ──────────────────────────────
    EMPTY = true
    await page.goto('/capabilities?tab=crews', { waitUntil: 'domcontentloaded' })
    await main.getByText('No crewmates yet', { exact: true }).waitFor({ state: 'visible', timeout: 15000 })
    await main.getByRole('button', { name: 'Create your first crewmate' }).waitFor({ state: 'visible', timeout: 15000 })
    await main.getByText(/^A crewmate is a standing teammate/).waitFor({ state: 'visible', timeout: 15000 })
    if (await main.getByText(/No crews yet|first crew\b(?!mate)/).count()) throw new Error(`${theme} empty-crewmates: old "crew" copy still rendered`)
    await page.waitForTimeout(400)
    out = `${OUT}/${PREFIX}-${theme}-empty-crewmates.png`
    await page.screenshot({ path: out }); wrote.push(out)
    await railTab('Custom agents').click()
    await main.getByText('No custom agents yet', { exact: true }).waitFor({ state: 'visible', timeout: 15000 })
    await main.getByText('Create one here, or install a package that ships agents.').waitFor({ state: 'visible', timeout: 15000 })
    if (await main.getByText(/No templates yet|No agent templates/).count()) throw new Error(`${theme} empty-custom-agents: old "templates" copy still rendered`)
    await page.waitForTimeout(400)
    out = `${OUT}/${PREFIX}-${theme}-empty-custom-agents.png`
    await page.screenshot({ path: out }); wrote.push(out)
    EMPTY = false
    await ctx.close()

    // ── Composer picker: the custom agents list in the chat ───────────
    const chatCtx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, baseURL: base })
    const chat = await chatCtx.newPage()
    logPageProblems(chat)
    await stubDashboardApi(chat, {
      theme, extra,
      slots: [{ key: 'chat-1', messages: 0, running: false, agent: 'kirocrew', mode: '' }],
      localStorageEntries: { 'mc-active-slot': 'chat-1', 'mc-lang': 'en' },
    })
    await chat.goto('/chat', { waitUntil: 'domcontentloaded' })
    await chat.getByRole('button', { name: /^Agent: / }).first().waitFor({ state: 'visible', timeout: 20000 })
    await chat.getByRole('button', { name: /^Agent: / }).first().click()
    const picker = chat.getByRole('dialog', { name: 'Agent selector' })
    await picker.waitFor({ state: 'visible', timeout: 10000 })
    // The group carries the launch name for assistive technology even though
    // the picker drops the header chrome when only custom agents are listed.
    await picker.getByRole('group', { name: 'Custom agents' }).waitFor({ state: 'attached', timeout: 10000 })
    const names = (await picker.getByRole('option').allInnerTexts()).map(t => t.split('\n')[0].trim())
    const expected = CATALOG.map(r => r.name)
    if (JSON.stringify(names) !== JSON.stringify(expected)) throw new Error(`${theme} composer-picker: rows ${JSON.stringify(names)} != ${JSON.stringify(expected)}`)
    if (await picker.getByText(/Agent Template|templates?\b/i).count()) throw new Error(`${theme} composer-picker: "template" copy still rendered`)
    await chat.waitForTimeout(400)
    out = `${OUT}/${PREFIX}-${theme}-composer-picker.png`
    await chat.screenshot({ path: out }); wrote.push(out)
    await chatCtx.close()

    // ── Default crewmate: the server refuses the pick ─────────────────
    {
      REFUSE_DEFAULT = true
      const c = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, baseURL: base })
      const p = await c.newPage()
      logPageProblems(p)
      await stubDashboardApi(p, { theme, extra })
      await p.goto('/developer?tab=config&highlight=key:default-crewmate', { waitUntil: 'domcontentloaded' })
      const sel = p.getByRole('combobox', { name: 'Default crewmate' }).or(p.getByRole('button', { name: 'Default crewmate' })).first()
      await sel.waitFor({ state: 'visible', timeout: 15000 })
      await sel.click()
      await p.getByRole('option', { name: 'reviewer' }).click()
      const notice = p.getByTestId('cfg-default-crewmate-error')
      await notice.waitFor({ state: 'visible', timeout: 15000 })
      // Our sentence first, naming the refused pick; the server's reason after it.
      if (!/^Could not set reviewer as the default crewmate — unknown agent: reviewer/.test(((await notice.textContent()) || '').trim())) throw new Error(`${theme} default-crewmate-refused: notice reads "${await notice.textContent()}"`)
      // The select is back on what the config says, not on the refused pick.
      await p.waitForTimeout(400)
      const shown = (await sel.textContent()) || ''
      if (!/default/.test(shown) || /reviewer/.test(shown)) throw new Error(`${theme} default-crewmate-refused: select shows "${shown}" after a refusal`)
      await sel.scrollIntoViewIfNeeded()
      out = `${OUT}/${PREFIX}-${theme}-default-crewmate-refused.png`
      await p.screenshot({ path: out }); wrote.push(out)
      await c.close()
      REFUSE_DEFAULT = false
    }

    // ── One crewmate: no change link on the roster; one-option select ──
    {
      SINGLE = true
      const c = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, baseURL: base })
      const p = await c.newPage()
      logPageProblems(p)
      await stubDashboardApi(p, { theme, extra })
      await p.goto('/capabilities?tab=crews', { waitUntil: 'domcontentloaded' })
      const m = p.locator('#main-content')
      await m.locator('[data-testid="crew-card"]').first().waitFor({ state: 'visible', timeout: 15000 })
      if (await m.locator('[data-testid="crew-card"]').count() !== 1) throw new Error(`${theme} one-crewmate: roster is not one card`)
      if (await m.getByRole('link', { name: /^Change default crewmate/ }).count()) throw new Error(`${theme} one-crewmate: Change default crewmate link shown with one crewmate`)
      await p.waitForTimeout(400)
      out = `${OUT}/${PREFIX}-${theme}-one-crewmate-roster.png`
      await p.screenshot({ path: out }); wrote.push(out)
      await p.goto('/developer?tab=config', { waitUntil: 'domcontentloaded' })
      const sel = p.getByRole('combobox', { name: 'Default crewmate' }).or(p.getByRole('button', { name: 'Default crewmate' })).first()
      await sel.waitFor({ state: 'visible', timeout: 15000 })
      await sel.click()
      const opts = await p.getByRole('option').allInnerTexts()
      if (opts.length !== 1 || !/default/.test(opts[0])) throw new Error(`${theme} one-crewmate: select options are ${JSON.stringify(opts)}`)
      // Shot with the list open: the one option IS the fact the row states.
      await p.waitForTimeout(300)
      out = `${OUT}/${PREFIX}-${theme}-one-crewmate-config.png`
      await p.screenshot({ path: out }); wrote.push(out)
      await c.close()
      SINGLE = false
    }

    // ── Rail footer in a long-word locale: "Report issue" wraps, whole ──
    {
      const c = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, baseURL: base })
      const p = await c.newPage()
      logPageProblems(p)
      await stubDashboardApi(p, { theme, extra, localStorageEntries: { 'mc-lang': 'ru' } })
      await p.goto('/capabilities?tab=crews', { waitUntil: 'domcontentloaded' })
      const links = p.locator('.rail-community-links')
      await links.waitFor({ state: 'visible', timeout: 15000 })
      const star = links.locator('a').first()
      const report = links.locator('button').first()
      await report.waitFor({ state: 'visible', timeout: 15000 })
      const [sb, rb] = [await star.boundingBox(), await report.boundingBox()]
      if (!sb || !rb || rb.y < sb.y + sb.height - 2) throw new Error(`${theme} rail-footer-wrapped: Report issue did not wrap below Star us (${JSON.stringify(sb)} vs ${JSON.stringify(rb)})`)
      const fit = await report.evaluate(el => ({ cw: el.clientWidth, sw: el.scrollWidth }))
      if (fit.cw < fit.sw) throw new Error(`${theme} rail-footer-wrapped: Report issue is truncated (${fit.cw} < ${fit.sw})`)
      // Same frame, roster toolbar: the long Russian "Change default crewmate"
      // link must fold inside itself, not push Add crewmate onto a second row —
      // the primary action keeps its place in every locale.
      const newCrew = p.getByTestId('new-crew')
      await newCrew.waitFor({ state: 'visible', timeout: 15000 })
      const search = p.getByRole('textbox').first()
      const [nb, fb] = [await newCrew.boundingBox(), await search.boundingBox()]
      if (!nb || !fb || Math.abs((nb.y + nb.height / 2) - (fb.y + fb.height / 2)) > 12) throw new Error(`${theme} rail-footer-wrapped: Add crewmate left the filter row (${JSON.stringify(fb)} vs ${JSON.stringify(nb)})`)
      await p.waitForTimeout(300)
      out = `${OUT}/${PREFIX}-${theme}-rail-footer-wrapped.png`
      await p.screenshot({ path: out }); wrote.push(out)
      await c.close()
    }

    // ── 320px: both toolbars stack, nothing overflows ─────────────────
    {
      const c = await browser.newContext({ viewport: { width: 320, height: 760 }, deviceScaleFactor: 2, baseURL: base })
      const p = await c.newPage()
      logPageProblems(p)
      await stubDashboardApi(p, { theme, extra })
      const noOverflow = async where => {
        const w = await p.evaluate(() => document.documentElement.scrollWidth)
        if (w > 320) throw new Error(`${theme} ${where}: page overflows the 320px viewport (${w}px)`)
      }
      await p.goto('/capabilities?tab=templates', { waitUntil: 'domcontentloaded' })
      const m = p.locator('#main-content')
      const nb = m.getByRole('button', { name: 'New custom agent' })
      await nb.waitFor({ state: 'visible', timeout: 15000 })
      const f = m.getByRole('searchbox').or(m.getByPlaceholder(/filter|search/i)).first()
      await f.waitFor({ state: 'visible', timeout: 15000 })
      const [fb2, bb2] = [await f.boundingBox(), await nb.boundingBox()]
      if (!fb2 || !bb2 || bb2.y < fb2.y + fb2.height - 2) throw new Error(`${theme} narrow-custom-agents: New custom agent is not below the filter (${JSON.stringify(fb2)} vs ${JSON.stringify(bb2)})`)
      if (bb2.x + bb2.width > 320 || fb2.x + fb2.width > 320) throw new Error(`${theme} narrow-custom-agents: toolbar clips the viewport`)
      await noOverflow('narrow-custom-agents')
      await p.waitForTimeout(300)
      out = `${OUT}/${PREFIX}-${theme}-narrow-custom-agents.png`
      await p.screenshot({ path: out }); wrote.push(out)

      await p.goto('/capabilities?tab=mcp', { waitUntil: 'domcontentloaded' })
      const tl = m.getByRole('tablist', { name: /connection views/i })
      await tl.waitFor({ state: 'visible', timeout: 15000 })
      const rs = m.getByRole('button', { name: 'Apply & Restart' })
      await rs.waitFor({ state: 'visible', timeout: 15000 })
      const [tb2, rb2] = [await tl.boundingBox(), await rs.boundingBox()]
      if (!tb2 || !rb2 || rb2.y < tb2.y + tb2.height - 2) throw new Error(`${theme} narrow-connections: Apply & Restart is not below the tablist (${JSON.stringify(tb2)} vs ${JSON.stringify(rb2)})`)
      if (rb2.x + rb2.width > 320) throw new Error(`${theme} narrow-connections: Apply & Restart clips the viewport`)
      await noOverflow('narrow-connections')
      await p.waitForTimeout(300)
      out = `${OUT}/${PREFIX}-${theme}-narrow-connections.png`
      await p.screenshot({ path: out }); wrote.push(out)
      await c.close()
    }

    // ── Apply & Restart refused: the failure notice in the header ─────
    {
      FAIL_RESTART = true
      const c = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, baseURL: base })
      const p = await c.newPage()
      logPageProblems(p)
      await stubDashboardApi(p, { theme, extra })
      await p.goto('/capabilities?tab=mcp', { waitUntil: 'domcontentloaded' })
      const m = p.locator('#main-content')
      const rs = m.getByRole('button', { name: 'Apply & Restart' })
      await rs.waitFor({ state: 'visible', timeout: 15000 })
      // The button asks first; the frame is what happens after the operator said yes.
      p.once('dialog', d => void d.accept())
      await rs.click()
      const notice = m.getByTestId('restart-button-error')
      await notice.waitFor({ state: 'visible', timeout: 15000 })
      if ((await notice.getAttribute('role')) !== 'alert') throw new Error(`${theme} restart-failed: notice is not role=alert`)
      if (!/restart refused/.test((await notice.textContent()) || '')) throw new Error(`${theme} restart-failed: notice reads "${await notice.textContent()}"`)
      // No hand-off at this mount: the MCP servers tab under the header holds an editable form.
      if (await notice.getByRole('button', { name: /ask the agent/i }).count()) throw new Error(`${theme} restart-failed: hand-off rendered on the Connections mount`)
      await notice.getByRole('button', { name: 'Dismiss' }).waitFor({ state: 'visible', timeout: 5000 })
      // It stays: no five-second auto-clear on a failure.
      await p.waitForTimeout(5500)
      if (!(await notice.isVisible())) throw new Error(`${theme} restart-failed: notice vanished on its own`)
      out = `${OUT}/${PREFIX}-${theme}-restart-failed.png`
      await p.screenshot({ path: out }); wrote.push(out)
      await c.close()
      FAIL_RESTART = false
    }
  }
} finally {
  await browser.close()
  srv.close()
}
console.log(`wrote ${wrote.join(', ')}`)
