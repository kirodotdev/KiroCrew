/**
 * Screenshot harness for the grouped chat agent picker: with
 * `dashboard.crewmates_in_agent_picker` on, the pop-up lists the **Custom agents**
 * group (with its hint) FIRST and the **Crewmates** group below it.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli.
 *
 * The catalog carries both kinds, with one name (`reviewer`) shared between a
 * member and a template, so the frame shows two headers, the templates hint under
 * the first one, and crewmate avatars only in the second group. Taken in the light
 * and dark palettes.
 *
 * Usage: node scripts/capture-agent-picker-templates-first.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'agent-picker-templates-first-shots')

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'
const row = (name, selection_kind, source, description, extra = {}) => ({
  name, selection_kind, source, description, scope: 'global',
  kiro_agent: name, workspace: 'default', memory_store: selection_kind === 'member' ? `member-${name}` : 'default',
  model: '', reasoning_effort: '', triggers: '', session_color: '', avatar: {}, ...extra,
})
const CATALOG = [
  row('default', 'member', 'kirocrew', 'The stock crew agent'),
  row('reviewer', 'member', 'kirocrew', 'My reviewer, with its own memory'),
  row('radar', 'member', 'kirocrew', 'Watches the issue queue'),
  row('kirocrew', 'template', 'builtin', 'The stock crew agent template'),
  row('reviewer', 'template', 'package', 'Shared reviewer template'),
  row('oncall-triage', 'template', 'package', 'Triage pager alerts against the runbook'),
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()

/** Open the picker on a fresh page in the given theme. */
async function openPicker(theme) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 1, colorScheme: theme })
  const page = await context.newPage()

  /** Each branch AWAITS `json()` then returns true; a falsy return means "not handled". */
  const extra = async (path, route) => {
    if (path === '/api/agents/catalog') {
      await json(route, { agents: CATALOG, default_agent: 'kirocrew' })
      return true
    }
    if (path === '/api/config/kirocrew') {
      // The key the hook reads to list every crewmate beside the templates.
      await json(route, { ...KIROCREW_CONFIG_FIXTURE, dashboard: { ...(KIROCREW_CONFIG_FIXTURE.dashboard || {}), crewmates_in_agent_picker: true } })
      return true
    }
    return false
  }

  await stubDashboardApi(page, {
    slots: [{ key: SLOT, messages: 0, running: false, agent: 'kirocrew', mode: '' }],
    theme,
    localStorageEntries: { 'mc-active-slot': SLOT, 'mc-lang': 'en' },
    extra,
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  await page.getByRole('button', { name: /^Agent: / }).first().click()
  const picker = page.getByRole('dialog', { name: 'Agent selector' })
  await picker.waitFor({ state: 'visible', timeout: 5000 })
  return { context, page, picker }
}

for (const theme of ['light', 'dark']) {
  const { context, page, picker } = await openPicker(theme)
  // The assertions the frame is meant to carry, checked before it is written:
  // the two group headers in the new order, and the templates hint under the first.
  const groups = await picker.getByRole('group').evaluateAll(els => els.map(e => e.getAttribute('aria-label')))
  if (JSON.stringify(groups) !== JSON.stringify(['Custom agents', 'Mates'])) {
    throw new Error(`group order ${JSON.stringify(groups)} != ['Custom agents', 'Mates']`)
  }
  const options = await picker.getByRole('option').allInnerTexts()
  const names = options.map(t => t.split('\n')[0].trim())
  if (names.length !== CATALOG.length) throw new Error(`picker rows ${JSON.stringify(names)}: expected ${CATALOG.length}`)
  if (!(await picker.getByText(/a reusable agent setup/i).count())) throw new Error('templates hint not rendered')

  const out = join(OUT, `picker-templates-first-${theme}.png`)
  await picker.screenshot({ path: out })
  console.log('wrote', out)
  const full = join(OUT, `chat-with-picker-${theme}.png`)
  await page.screenshot({ path: full })
  console.log('wrote', full)
  await context.close()
}

for (const name of ['picker-templates-first', 'chat-with-picker']) {
  const light = readFileSync(join(OUT, `${name}-light.png`))
  const dark = readFileSync(join(OUT, `${name}-dark.png`))
  if (light.equals(dark)) throw new Error(`${name}: light and dark frames are byte-identical, the theme did not apply`)
}

await browser.close()
srv.close()
