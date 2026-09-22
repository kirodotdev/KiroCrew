/**
 * Screenshot harness for the chat agent picker while `HIDE_CREWMATE_CHOICES` is
 * on: the pop-up lists templates only, with no group header and no templates
 * hint.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli.
 *
 * `GET /api/agents/catalog` is stubbed with BOTH kinds -- three crewmates and four
 * templates, one name (`reviewer`) shared between a member and a template -- so the
 * frame proves the hide is done by the hook on a mixed catalog, not by a fixture
 * that never listed a member. The frame is taken twice, in the light and dark
 * palettes, so the header-less list is judged in both.
 *
 * Usage: node scripts/capture-agent-picker-hide-crewmates.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/agent-picker-hide-crewmates-shots'

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
  row('gpu-autosde-analyzer', 'template', 'package', 'Analyzer subagent for gpu-autosde: reads assigned chunk file diffs from disk and generates review comments'),
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
      // The route the hook reads; the stub's `/api/agents` default carries no
      // `selection_kind`, which is the field the hook filters on.
      await json(route, { agents: CATALOG, default_agent: 'default' })
      return true
    }
    return false
  }

  // The theme goes through the stub: it serves `/api/theme/boot` with its
  // `theme` option and the SPA writes that mode over whatever `mc-theme` held,
  // so a harness that seeds `mc-theme` itself still renders the stub's default
  // (dark) in both runs. The locale and slot seeds ride the stub's own init
  // script too, after its storage clear.
  await stubDashboardApi(page, {
    slots: [{ key: SLOT, messages: 0, running: false, agent: 'kirocrew', mode: '' }],
    theme,
    localStorageEntries: { 'mc-active-slot': SLOT, 'mc-lang': 'en' },
    extra,
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  // Anchored on the composer control's own label -- a loose /agent/i matches the
  // sidebar's "Agent Capabilities" entry first and opens the wrong surface.
  await page.getByRole('button', { name: /^Agent: / }).first().click()
  const picker = page.getByRole('dialog', { name: 'Agent selector' })
  await picker.waitFor({ state: 'visible', timeout: 5000 })
  return { context, page, picker }
}

for (const theme of ['light', 'dark']) {
  const { context, page, picker } = await openPicker(theme)
  // The assertions the frame is meant to carry, checked before it is written:
  // every template row, no member row, no header, no hint.
  const options = await picker.getByRole('option').allInnerTexts()
  const names = options.map(t => t.split('\n')[0].trim())
  const expected = CATALOG.filter(r => r.selection_kind === 'template').map(r => r.name)
  if (JSON.stringify(names) !== JSON.stringify(expected)) {
    throw new Error(`picker rows ${JSON.stringify(names)} != templates ${JSON.stringify(expected)}`)
  }
  if (await picker.getByText('Agent templates', { exact: true }).count()) throw new Error('group header still rendered')
  if (await picker.getByText('Crewmates', { exact: true }).count()) throw new Error('crewmates header still rendered')
  if (await picker.getByText(/runs the shared template on shared memory/i).count()) throw new Error('templates hint still rendered')
  if (await picker.locator('img').count()) throw new Error('a crewmate avatar is rendered, so a member row leaked')

  const out = join(OUT, `picker-templates-only-${theme}.png`)
  await picker.screenshot({ path: out })
  console.log('wrote', out)
  const full = join(OUT, `chat-with-picker-${theme}.png`)
  await page.screenshot({ path: full })
  console.log('wrote', full)
  await context.close()
}

// The two themes must actually differ: a theme seed the SPA overwrote once
// produced four frames of the same dark UI labelled light and dark.
for (const name of ['picker-templates-only', 'chat-with-picker']) {
  const light = readFileSync(join(OUT, `${name}-light.png`))
  const dark = readFileSync(join(OUT, `${name}-dark.png`))
  if (light.equals(dark)) throw new Error(`${name}: light and dark frames are byte-identical, the theme did not apply`)
}

await browser.close()
srv.close()
