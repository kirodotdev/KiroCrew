/**
 * Screenshot harness for the agent-template PANEL in the crew editor
 * (flag `agent_template_pane`): the template selector is the header bar of a
 * panel containing the definition it names.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth. That matters
 * here specifically: the dashboard's API is token-gated and minting a token is
 * refused for an agent, so an unauthenticated live gateway would only ever
 * screenshot this pane's error state.
 *
 * Three states, because each renders a different header contract:
 *   own      — a shared user template: Custom badge, used-by count, fork hint
 *   built-in — the `kirocrew` template: Built-in badge, file:// prompt
 *   own-copy — a crew's private fork: header names the ORIGIN, Customized tag,
 * *              live change-count pill (its popover holds Reset), and a
 *              Save-as-new-template action
 *
 * SELF-CHECKS (throw = no stale frame) assert each contract above.
 *
 * Usage: node scripts/capture-agent-template-pane.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/agent-template-pane'
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

/** The crews the roster shows. Two point at `atlas`, so its blast radius is real.
 *  `scout` is bound to its own private copy of `kirocrew` (blueprint fork). */
const CREWS = [
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Used for all new chats', source: 'user', model: '', triggers: '', session_color: '' },
  { name: 'atlas', kiro_agent: 'atlas', workspace: 'default', memory_store: 'default', description: 'Long-horizon planner', source: 'user', model: '', triggers: 'migration', session_color: '' },
  { name: 'gpu-critic', kiro_agent: 'atlas', workspace: 'default', memory_store: 'default', description: 'Validates review findings', source: 'user', model: '', triggers: '', session_color: '' },
  { name: 'scout', kiro_agent: 'scout', workspace: 'default', memory_store: 'default', description: 'Research scout with its own definition', source: 'user', model: '', triggers: '', session_color: '' },
]

/** A plain ARRAY: what /api/agents/installed really answers.
 *  All three provenance shapes, because the header badge and the dropdown rows
 *  both label from them. `scout` carries fork lineage: one crew's private copy. */
const INSTALLED = [
  { name: 'kirocrew', description: 'Built-in', source: 'kirocrew', model: '', skills: ['memory', 'artifacts'], mcp_servers: ['kirocrew-core'], filename: 'kirocrew.json', kirocrew_owned: true },
  { name: 'kirocrew-heartbeat', description: 'Unattended heartbeat worker', source: 'builtin', model: '', skills: [], mcp_servers: ['kirocrew-core'], filename: 'kirocrew-heartbeat.json', kirocrew_owned: true },
  { name: 'papyrus-writer', description: 'LaTeX co-author', source: 'package', package: 'papyrus', model: '', skills: [], mcp_servers: [], filename: 'papyrus-writer.json', kirocrew_owned: false },
  { name: 'atlas', description: 'Long-horizon planner', source: 'builtin', model: 'claude-opus-4.8', skills: ['grill', 'five-whys'], mcp_servers: ['kirocrew-core'], filename: 'atlas.json', kirocrew_owned: false },
  { name: 'scout', description: 'Built-in', source: 'builtin', model: 'claude-sonnet-4.5', skills: ['memory'], mcp_servers: ['kirocrew-core'], filename: 'scout.json', kirocrew_owned: false, forked_from: 'kirocrew', private_to: 'scout' },
]

const DENIED = [
  'git push --force', 'rm -rf', 'curl | sh', 'DROP TABLE', 'sudo rm',
  'chmod 777 /', 'dd if=', 'mkfs', 'shutdown', 'kill -9 1', 'npm publish',
]

const DETAIL = {
  atlas: {
    name: 'atlas', model: 'claude-opus-4.8',
    prompt: 'Plan before acting. Write the plan to the workspace first, then execute it step by step, checking each step against the plan before moving on.\n\nNever touch a repository without cutting a branch. Stop and ask if a schema change is implied, a migration would run, or the change touches billing, auth or data retention.',
    skills: ['grill', 'five-whys'],
    tools: ['fs_read', 'fs_write', 'execute_bash', 'git', 'web', 'web_search', 'glob', 'grep'],
    allowedTools: ['fs_read', 'glob'],
    mcpServers: { 'kirocrew-core': {}, figma: { args: ['--include-tools', 'get_design_context,get_screenshot,get_metadata'] } },
    toolsSettings: { execute_bash: { deniedCommands: DENIED } },
  },
  kirocrew: {
    name: 'kirocrew', model: '',
    prompt: 'file://~/.kiro/agents/prompts/kirocrew.md',
    skills: ['memory', 'artifacts'],
    tools: ['fs_read', 'fs_write', 'execute_bash'],
    allowedTools: ['fs_read'],
    mcpServers: { 'kirocrew-core': {}, 'kirocrew-cron': {} },
    toolsSettings: { execute_bash: { deniedCommands: ['rm -rf /', 'git push --force'] } },
  },
  // scout differs from kirocrew in EXACTLY two sections (model, skills), so
  // the change-count pill must read "2 changes" — a wrong diff fails the run.
  scout: {
    name: 'scout', model: 'claude-sonnet-4.5',
    prompt: 'file://~/.kiro/agents/prompts/kirocrew.md',
    skills: ['memory'],
    tools: ['fs_read', 'fs_write', 'execute_bash'],
    allowedTools: ['fs_read'],
    mcpServers: { 'kirocrew-core': {}, 'kirocrew-cron': {} },
    toolsSettings: { execute_bash: { deniedCommands: ['rm -rf /', 'git push --force'] } },
  },
}

/** Open one crew's editor and land on the Agent Template pane. */
async function openTemplatePane(page, crew) {
  await page.goto('/capabilities?tab=crews', { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').getByText('Agents you chat with', { exact: false })
    .waitFor({ state: 'visible', timeout: 15000 })
  const card = page.getByRole('button', { name: new RegExp(`Edit (crew|agent) ${crew}`, 'i') })
  await card.waitFor({ state: 'visible', timeout: 15000 })
  await card.click()
  const sheet = page.getByRole('dialog', { name: /edit (crew|agent)/i })
  await sheet.waitFor({ state: 'visible', timeout: 10000 })
  await sheet.getByRole('button', { name: /agent template/i }).first().click()
  return sheet
}

const { srv, base } = await serveDist()
// The repo's pinned playwright and this machine's browser cache disagree on
// build number, so drive the installed Chrome instead of downloading one.
const browser = await chromium.launch({ channel: 'chrome' })

try {
  for (const [crew, template, expectBadge, expectOwnCopy] of [
    ['atlas', 'atlas', 'Custom', false],
    ['default', 'kirocrew', 'Built-in', false],
    ['scout', 'kirocrew', '', true],
  ]) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, baseURL: base })
    const page = await ctx.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      extra: async (path, route) => {
        if (path === '/api/config/kirocrew') {
          await json(route, { ...KIROCREW_CONFIG_FIXTURE, agent_template_pane: true })
          return true
        }
        if (path === '/api/agents') {
          await json(route, { agents: CREWS, default_agent: 'default' })
          return true
        }
        if (path === '/api/agents/installed') {
          await json(route, INSTALLED)
          return true
        }
        if (path.startsWith('/api/agents/detail/')) {
          const name = decodeURIComponent(path.split('/api/agents/detail/')[1] || '')
          await json(route, DETAIL[name] || { name })
          return true
        }
        if (path === '/api/skills') {
          await json(route, [])
          return true
        }
        if (path === '/api/models') {
          await json(route, { models: [{ name: 'claude-opus-4.8' }, { name: 'claude-sonnet-4.5' }] })
          return true
        }
        return false
      },
    })

    const sheet = await openTemplatePane(page, crew)

    // Self-checks: a stale or half-rendered frame must fail the run, not ship.
    // The header SELECT displays the ORIGIN for a customized copy — the copy's
    // auto-derived filename must never surface as the selection.
    const headerSelect = sheet.getByRole('combobox', { name: /agent template/i }).first()
    await headerSelect.waitFor({ state: 'visible', timeout: 10000 })
    const selected = ((await headerSelect.textContent()) || '').trim()
    if (!selected.includes(template)) {
      throw new Error(`${crew}: header select shows "${selected}", expected "${template}"`)
    }
    if (!(await sheet.getByText(/^Template$/i).count())) {
      throw new Error(`${crew}: header never rendered the "Template" prefix`)
    }
    const customizedTag = await sheet.getByText(/^Customized$/).count()
    if (expectOwnCopy && !customizedTag) throw new Error(`${crew}: own copy lacks the Customized tag`)
    if (!expectOwnCopy && customizedTag) throw new Error(`${crew}: shared template leaked a Customized tag`)

    if (expectOwnCopy) {
      // The pill arrives on its own fetch pair (copy + origin details), so it
      // is waited for, not counted — an immediate count races the queries.
      try {
        await sheet.getByText(/2 changes/).first().waitFor({ state: 'visible', timeout: 10000 })
      } catch {
        throw new Error(`${crew}: own-copy header never rendered the "2 changes" pill`)
      }
      if (!(await sheet.getByText(/Save as new template/).count())) {
        throw new Error(`${crew}: own-copy header is missing Save as new template`)
      }
      // Reset lives INSIDE the changes popover (two-action header cap) —
      // asserted when the popover frame is taken below.
      if (await sheet.getByText(/Reset my changes/).count()) {
        throw new Error(`${crew}: Reset leaked into the own-copy header`)
      }
      if (!(await sheet.getByText(/Based on the kirocrew template/i).count())) {
        throw new Error(`${crew}: own copy lacks the based-on hint`)
      }
    } else {
      if (!(await sheet.getByText(new RegExp(`^${expectBadge}$`)).count())) {
        throw new Error(`${crew}: template is not badged "${expectBadge}"`)
      }
      if (!(await sheet.getByText(/give this agent its own copy/i).count())) {
        throw new Error(`${crew}: shared template shows no fork hint`)
      }
      const usedText = crew === 'atlas' ? /used by 2 agents/i : /used by this agent only/i
      if (!(await sheet.getByText(usedText).count())) {
        throw new Error(`${crew}: header is missing the used-by reach text`)
      }
    }

    const tag = expectOwnCopy ? 'own-copy' : template === 'kirocrew' ? 'builtin' : 'own'
    const out = `${OUT}/${PREFIX}-${tag}.png`
    await sheet.screenshot({ path: out })
    console.log(`wrote ${out}`)

    if (expectOwnCopy) {
      // The change-count pill opens the diff popover — its own frame, because
      // detail-on-demand is the usability fix under test here.
      await sheet.getByText(/2 changes/).first().click()
      const pop = page.getByText(/was:/).first()
      await pop.waitFor({ state: 'visible', timeout: 5000 })
      if (!(await page.getByText(/Reset my changes/).count())) {
        throw new Error(`${crew}: changes popover is missing Reset my changes`)
      }
      // "Visible" fires at opacity ~0 — the popover animates in, and a frame
      // taken mid-fade shows the header bleeding through a translucent box.
      await page.waitForTimeout(400)
      const out2 = `${OUT}/${PREFIX}-own-copy-popover.png`
      await sheet.screenshot({ path: out2 })
      console.log(`wrote ${out2}`)
      await page.keyboard.press('Escape')
    }

    // The prompt and guardrails sit below the fold — a frame that never shows
    // them proves nothing about the read-only half.
    await sheet.getByText(/never allowed to run|read-only here|lives in a file/i).last()
      .scrollIntoViewIfNeeded().catch(() => {})
    await page.waitForTimeout(400)
    const out3 = `${OUT}/${PREFIX}-${tag}-lower.png`
    await sheet.screenshot({ path: out3 })
    console.log(`wrote ${out3}`)

    // Open dropdown: source labels live in the rows, and another crew's
    // private copy must never be offered.
    if (crew === 'atlas') {
      await headerSelect.scrollIntoViewIfNeeded()
      await headerSelect.click()
      const list = page.getByRole('listbox')
      await list.waitFor({ state: 'visible', timeout: 10000 })
      for (const want of ['Built-in', 'papyrus', 'Custom']) {
        if (!(await list.getByText(want, { exact: true }).count())) {
          throw new Error(`open dropdown never rendered a "${want}" row`)
        }
      }
      if (await list.getByText('scout', { exact: true }).count()) {
        throw new Error('another crew\'s private copy leaked into the dropdown')
      }
      const out4 = `${OUT}/${PREFIX}-dropdown.png`
      await list.screenshot({ path: out4 })
      console.log(`wrote ${out4}`)
      await page.keyboard.press('Escape')
    }
    await ctx.close()
  }
} finally {
  await browser.close()
  srv.close()
}
