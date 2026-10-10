/**
 * Screenshot harness for the default custom agent bar on the Custom agents tab
 * (#18411, option A): one bar above the list that is both the readout and the
 * control for `agent.default_agent`.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Frames, each in dark and light:
 *   bar        the tab with the bar showing the EFFECTIVE default (`kirocrew`,
 *              the runtime's own template, while the stored value is "")
 *   open       the picker open: a package template is offered, a crewmate's
 *              private copy is not
 *   picked     after choosing `atlas`: the bar reads `atlas` with "Saved"
 *              beside the select, shot as the product leaves it (no row is
 *              clicked after the PUT)
 *   refused    the list went stale: `reviewer` was deleted after the list
 *              loaded; the 404 is reported IN the bar and the re-read list drops it
 *   overridden  config.local.json pins the value: before any pick the picker
 *              is disabled, the notice leads with why and what still runs, and
 *              the file is a second, smaller line
 *   read-failed the GET fails; the notice says so with a Retry beside it, the
 *              picker is disabled and the usage line stops claiming a default
 *
 * SELF-CHECKS (throw = no stale frame): the bar's trigger text, the open
 * list's membership, the PUT body, the "Saved" confirmation (and that no roster
 * row was clicked after the PUT), the inline error, the overridden note's two
 * lines, and that no dialog is open when a frame is taken.
 *
 * Usage: node scripts/capture-default-template-bar.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/default-template-bar'
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const BAR_LABEL = 'New sessions use'

const CREWS = [
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Used for all new chats', source: 'user', model: '', triggers: '', session_color: '' },
  { name: 'pr-bot', kiro_agent: 'pr-bot-copy', workspace: 'default', memory_store: 'default', description: 'Reviews pull requests', source: 'user', model: '', triggers: '', session_color: '' },
]

const tmpl = over => ({
  name: 'x', filename: 'x.json', description: '', model: '', skills: [], mcp_servers: [],
  source: 'builtin', package: '', scope: 'global', kirocrew_owned: false, forked_from: '', private_to: '',
  read_only: null, default_eligible: true, used_by: [], ...over,
})
const defaultRef = { kind: 'default', id: 'default', label: 'default agent' }
const templates = (defaultName, reviewerDeleted = false) => [
  ...(reviewerDeleted ? [] : [tmpl({ name: 'reviewer', filename: 'reviewer.json', description: 'Careful code reviewer', model: 'claude-opus-4.8', used_by: defaultName === 'reviewer' ? [defaultRef] : [] })]),
  tmpl({ name: 'atlas', filename: 'atlas.json', description: 'Long-horizon planner', used_by: defaultName === 'atlas' ? [defaultRef] : [] }),
  tmpl({ name: 'papyrus-writer', filename: 'papyrus-papyrus-writer.json', description: 'LaTeX co-author', source: 'package', package: 'papyrus', read_only: 'package' }),
  tmpl({ name: 'pr-bot-copy', filename: 'pr-bot-copy.json', description: 'Careful code reviewer', forked_from: 'reviewer', private_to: 'pr-bot', read_only: 'private_copy', default_eligible: false, used_by: [{ kind: 'crew', id: 'pr-bot', label: 'pr-bot' }] }),
  tmpl({ name: 'kirocrew', filename: 'kirocrew.json', description: 'Built-in', source: 'kirocrew', kirocrew_owned: true, read_only: 'runtime', used_by: [{ kind: 'crew', id: 'default', label: 'default' }, ...(defaultName === 'kirocrew' ? [defaultRef] : [])] }),
]

// One prompt per template, so the editor in a frame shows the agent it is open on.
const PROMPTS = {
  reviewer: 'You are a careful reviewer. Read before you judge.',
  atlas: 'You plan long tasks. Break the goal into steps and track each one.',
  'papyrus-writer': 'You co-author LaTeX papers. Keep the author’s voice.',
  'pr-bot-copy': 'You review pull requests for pr-bot. Read before you judge.',
  kirocrew: 'You are the Kiro Crew assistant.',
}
const OVERRIDE_PATH = '/home/user/.kiro/crew/config.local.json'

const DETAIL = name => ({
  name, model: '', description: templates('').find(t => t.name === name)?.description || '',
  prompt: PROMPTS[name] || `You are ${name}.`, skills: [], tools: ['fs_read', 'fs_write', 'execute_bash'],
  allowedTools: ['fs_read'], resources: [], mcpServers: { 'kirocrew-core': {} },
})

const CFG = {
  ...KIROCREW_CONFIG_FIXTURE,
  agents: Object.fromEntries(CREWS.map(c => [c.name, { kiro_agent: c.kiro_agent, workspace: c.workspace, memory_store: c.memory_store, description: c.description, source: 'config' }])),
  default_agent: 'default',
  workspaces: { default: { dir: '~/.kiro/crew/workspace' } },
  memory_stores: { default: { description: 'Shared memory', embedding_provider: 'bge-m3' } },
}

/** Per-context server state: the stored default and the PUT bodies seen. */
function makeExtra(state) {
  return async function extra(path, route) {
    const method = route.request().method()
    if (path === '/api/config/kirocrew') return json(route, CFG), true
    if (path === '/api/agents') return json(route, { agents: CREWS, default_agent: 'default' }), true
    if (path === '/api/agents/installed') return json(route, []), true
    if (path === '/api/agents/templates') return json(route, { templates: templates(state.stored || 'kirocrew', state.reviewerDeleted) }), true
    if (path === '/api/config/default-template' && method === 'PUT') {
      const body = route.request().postDataJSON() || {}
      state.puts.push(body.template)
      if (state.overridden) {
        return json(route, { error: 'config.local.json pins agent.default_agent; remove that override before changing the default template', code: 'default_template_overridden_by_local', override_path: OVERRIDE_PATH }, 409), true
      }
      if (body.template === 'reviewer' && state.reviewerDeleted) {
        return json(route, { error: "No installed template is named 'reviewer'.", code: 'template_not_found' }, 404), true
      }
      if (body.template === 'pr-bot-copy') {
        return json(route, { error: "Template 'pr-bot-copy' is crewmate 'pr-bot's private copy; it cannot be the default template.", code: 'template_private_copy' }, 409), true
      }
      state.stored = body.template
      return json(route, { ok: true, default_template: body.template, effective: body.template || 'kirocrew' }), true
    }
    if (path === '/api/config/default-template' && state.readFails) return json(route, { error: 'failed to read config file', code: 'config_unreadable' }, 500), true
    if (path === '/api/config/default-template') {
      return json(route, { default_template: state.stored, effective: state.stored || 'kirocrew', overridden: state.overridden, ...(state.overridden ? { override_path: OVERRIDE_PATH } : {}) }), true
    }
    if (path.startsWith('/api/agents/detail/')) return json(route, DETAIL(decodeURIComponent(path.split('/api/agents/detail/')[1] || ''))), true
    if (path === '/api/mcp' || path === '/api/mcp/probe') return json(route, []), true
    if (path === '/api/connections/status') return json(route, { schema_version: 1, connections: [] }), true
    if (path === '/api/workspaces') return json(route, { workspaces: [{ name: 'default' }] }), true
    if (path === '/api/skills' || path === '/api/skills/catalog') return json(route, []), true
    if (path === '/api/models') return json(route, { models: [{ name: 'claude-opus-4.8' }] }), true
    return false
  }
}

async function noDialog(page, where) {
  const n = await page.getByRole('dialog').count()
  if (n !== 0) throw new Error(`${where}: ${n} dialog(s) open on top of the frame`)
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const wrote = []

try {
  for (const theme of ['dark', 'light']) {
    const state = { stored: '', puts: [], reviewerDeleted: false, readFails: false, overridden: false }
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, baseURL: base })
    const page = await ctx.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { theme, extra: makeExtra(state) })

    await page.goto('/capabilities?tab=templates', { waitUntil: 'domcontentloaded' })
    const main = page.locator('#main-content')
    const bar = main.getByTestId('default-template-bar')
    await bar.waitFor({ state: 'visible', timeout: 20000 })
    const trigger = bar.getByRole('combobox', { name: BAR_LABEL })
    await trigger.waitFor({ state: 'visible', timeout: 15000 })
    // Stored "" renders as what actually starts, never as a blank or the first option.
    await page.waitForFunction(el => (el.textContent || '').includes('kirocrew'), await trigger.elementHandle(), { timeout: 15000 })
    if (!(await bar.getByText(/^Not offered here: /).count())) throw new Error(`${theme} bar: hidden-rows note missing while a private copy is on the roster`)
    await noDialog(page, `${theme} bar`)
    await page.waitForTimeout(400)
    let out = `${OUT}/${PREFIX}-${theme}-bar.png`
    await page.screenshot({ path: out }); wrote.push(out)

    // ── Open: eligibility is what the list shows ───────────────────────
    await trigger.click()
    const list = page.getByRole('listbox').filter({ hasNot: page.getByRole('option', { name: /Careful code reviewer/ }) }).last()
    await list.waitFor({ state: 'visible', timeout: 10000 })
    const names = await list.getByRole('option').allTextContents()
    for (const want of ['reviewer', 'atlas', 'papyrus-writer', 'kirocrew']) {
      if (!names.some(n => n.trim() === want)) throw new Error(`${theme} open: "${want}" missing from the picker (saw ${JSON.stringify(names)})`)
    }
    if (names.some(n => n.trim() === 'pr-bot-copy')) throw new Error(`${theme} open: a crewmate's private copy leaked into the picker`)
    await page.waitForTimeout(300)
    out = `${OUT}/${PREFIX}-${theme}-open.png`
    await page.screenshot({ path: out }); wrote.push(out)

    // ── Pick atlas: the PUT, the readout, the "Saved" confirmation ─────
    // Shot as the product leaves the bar: counting clicks on the roster's rows
    // proves nothing was clicked after the PUT to stage the frame.
    await page.evaluate(() => {
      window.__rosterClicks = 0
      document.addEventListener('click', e => {
        if (e.target instanceof Element && e.target.closest('[role="listbox"][aria-label="Custom agents"] [role="option"]')) window.__rosterClicks += 1
      }, true)
    })
    await list.getByRole('option', { name: 'atlas', exact: true }).click()
    const saved = bar.getByTestId('default-template-saved')
    await saved.waitFor({ state: 'visible', timeout: 15000 })
    if (state.puts[0] !== 'atlas') throw new Error(`${theme} picked: PUT body was ${JSON.stringify(state.puts)}`)
    await page.waitForFunction(el => (el.textContent || '').includes('atlas'), await trigger.elementHandle(), { timeout: 15000 })
    if (((await saved.textContent()) || '').trim() !== 'Saved') throw new Error(`${theme} picked: confirmation text was ${JSON.stringify(await saved.textContent())}`)
    await noDialog(page, `${theme} picked`)
    await page.waitForTimeout(200)
    out = `${OUT}/${PREFIX}-${theme}-picked.png`
    await page.screenshot({ path: out }); wrote.push(out)
    // Still on screen after the shot, so the frame holds it (the product clears
    // it after a few seconds; the harness never lengthens that).
    if (!(await saved.isVisible())) throw new Error(`${theme} picked: "Saved" cleared before the frame was taken`)
    const rosterClicks = await page.evaluate(() => window.__rosterClicks)
    if (rosterClicks !== 0) throw new Error(`${theme} picked: ${rosterClicks} roster row click(s) after the PUT`)

    // ── Refused: the list went stale under the user, reported in the bar ──
    // `reviewer` is offered (the list loaded when it existed), then is deleted
    // before the pick lands. The server answers 404, the bar says so and names
    // what new sessions still use, and the re-read list no longer offers it.
    await trigger.click()
    const list2 = page.getByRole('listbox').filter({ hasNot: page.getByRole('option', { name: /Careful code reviewer/ }) }).last()
    await list2.waitFor({ state: 'visible', timeout: 10000 })
    if (!(await list2.getByRole('option', { name: 'reviewer', exact: true }).count())) throw new Error(`${theme} refused: reviewer must be offered before it goes stale`)
    state.reviewerDeleted = true
    await list2.getByRole('option', { name: 'reviewer', exact: true }).click()
    if (state.puts[1] !== 'reviewer') throw new Error(`${theme} refused: PUT body was ${JSON.stringify(state.puts)}`)
    const err = bar.getByTestId('default-template-error')
    await err.waitFor({ state: 'visible', timeout: 15000 })
    if (!/^Couldn’t switch to reviewer: it was deleted\. New sessions still use atlas\.$/.test(((await err.textContent()) || '').trim())) throw new Error(`${theme} refused: inline error text not rendered`)
    // The roster was re-read: the deleted name is gone from the list and the picker.
    await main.getByRole('option', { name: /^reviewer\b/ }).waitFor({ state: 'detached', timeout: 15000 })
    await trigger.click()
    const list3 = page.getByRole('listbox').filter({ hasNot: page.getByRole('option', { name: /Long-horizon planner/ }) }).last()
    await list3.waitFor({ state: 'visible', timeout: 10000 })
    if (await list3.getByRole('option', { name: 'reviewer', exact: true }).count()) throw new Error(`${theme} refused: the deleted agent is still offered after the re-read`)
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)
    await noDialog(page, `${theme} refused`)
    await page.waitForTimeout(400)
    out = `${OUT}/${PREFIX}-${theme}-refused.png`
    await page.screenshot({ path: out }); wrote.push(out)

    // ── Overridden: config.local.json pins the value; the bar says so before any pick ──
    state.overridden = true
    const putsBefore = state.puts.length
    await page.reload({ waitUntil: 'domcontentloaded' })
    const barO = main.getByTestId('default-template-bar')
    await barO.waitFor({ state: 'visible', timeout: 20000 })
    const note = barO.getByTestId('default-template-overridden')
    await note.waitFor({ state: 'visible', timeout: 15000 })
    // The fact leads; the file is its own, smaller line under it.
    const lead = ((await note.locator(':scope > span').first().textContent()) || '').trim()
    if (lead !== 'Set by a local config file, so it can’t be changed here. New sessions use atlas.') throw new Error(`${theme} overridden: lead text was ${JSON.stringify(lead)}`)
    const fileLine = ((await note.getByTestId('default-template-overridden-file').textContent()) || '').trim()
    if (fileLine !== `File: ${OVERRIDE_PATH}`) throw new Error(`${theme} overridden: file line was ${JSON.stringify(fileLine)}`)
    const triggerO = barO.getByRole('combobox', { name: BAR_LABEL })
    await page.waitForFunction(el => (el.textContent || '').includes('atlas'), await triggerO.elementHandle(), { timeout: 15000 })
    if (!(await triggerO.isDisabled())) throw new Error(`${theme} overridden: picker still enabled while pinned`)
    if (await barO.getByTestId('default-template-error').count()) throw new Error(`${theme} overridden: an error shows although no pick was made`)
    if (state.puts.length !== putsBefore) throw new Error(`${theme} overridden: a PUT was sent (${JSON.stringify(state.puts)})`)
    await noDialog(page, `${theme} overridden`)
    await page.waitForTimeout(400)
    out = `${OUT}/${PREFIX}-${theme}-overridden.png`
    await page.screenshot({ path: out }); wrote.push(out)
    state.overridden = false

    // ── Read failed: the GET itself fails; the notice says so, the picker is disabled ──
    state.readFails = true
    await page.reload({ waitUntil: 'domcontentloaded' })
    const bar2 = main.getByTestId('default-template-bar')
    await bar2.waitFor({ state: 'visible', timeout: 20000 })
    const err2 = bar2.getByTestId('default-template-error')
    await err2.waitFor({ state: 'visible', timeout: 20000 })
    if (!/^Couldn’t read which custom agent new sessions use\.$/.test(((await err2.textContent()) || '').trim())) throw new Error(`${theme} read-failed: notice not rendered`)
    if (!(await bar2.getByRole('combobox', { name: BAR_LABEL }).isDisabled())) throw new Error(`${theme} read-failed: picker still enabled`)
    if (!(await bar2.getByRole('button', { name: 'Retry', exact: true }).isVisible())) throw new Error(`${theme} read-failed: Retry missing beside the notice`)
    await main.getByRole('option', { name: /^atlas\b/ }).click()
    await main.getByText('Long-horizon planner').first().waitFor({ state: 'visible', timeout: 10000 })
    if (await main.getByText('Used by new sessions', { exact: true }).count()) throw new Error(`${theme} read-failed: usage line still claims a default the bar could not read`)
    await noDialog(page, `${theme} read-failed`)
    await page.waitForTimeout(400)
    out = `${OUT}/${PREFIX}-${theme}-read-failed.png`
    await page.screenshot({ path: out }); wrote.push(out)

    await ctx.close()
  }
} finally {
  await browser.close()
  srv.close()
}
for (const w of wrote) console.log(`wrote ${w}`)
