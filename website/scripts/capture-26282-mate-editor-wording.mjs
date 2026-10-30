/**
 * Screenshot harness for #26282: the mate editor's Model readout, effort line
 * and Danger Zone text, in plain words.
 *
 * One shot per case the Model box can be in, because each case renders a
 * different line (or none):
 *   - picked:    a model chosen for this mate and in use -> no line;
 *   - uses:      nothing chosen here, a model applies -> "Uses X.";
 *   - automatic: nothing chosen anywhere -> "Picked automatically." plus the
 *                effort line "Pick a model to set a reasoning effort.";
 *   - instead:   the chosen model resolves to another one -> "<harness> doesn't
 *                offer X, so this mate runs on Y.";
 *   - foreign-automatic: the chosen model resolves to nothing -> "Picked automatically.";
 * then the Danger Zone section's delete warning.
 *
 * Runs the REAL built SPA (website/dist) behind `serveDist` with every /api/**
 * call answered from fixtures (`stubDashboardApi`): no gateway, no auth.
 *
 * Usage: node scripts/capture-26282-mate-editor-wording.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'mate-editor-wording')
mkdirSync(OUT, { recursive: true })

const crew = (model) => ({
  name: 'reviewer', kiro_agent: 'reviewer-agent', workspace: 'default', memory_store: 'reviewer',
  model, reasoning_effort: '',
})

/** GET /api/models: the picker's list, so it draws no load-failure notice. */
const MODELS = [
  { model_name: 'auto', description: 'Always visible in chat model pickers' },
  { model_name: 'claude-opus-5', display_name: 'Opus 5', description: '' },
  { model_name: 'claude-sonnet-5', display_name: 'Sonnet 5', description: '' },
  { model_name: 'claude-fable-5', display_name: 'Fable 5', description: '' },
]

const CASES = [
  { key: 'picked', model: 'claude-opus-5',
    resolved: { model: 'claude-opus-5', pinned: true, kiro_agent: 'reviewer-agent', reasoning_effort: 'high', effort_pinned: false } },
  { key: 'uses', model: '',
    resolved: { model: 'claude-sonnet-5', pinned: false, kiro_agent: 'reviewer-agent', reasoning_effort: '', effort_pinned: false } },
  { key: 'automatic', model: '',
    resolved: { model: '', pinned: false, kiro_agent: 'reviewer-agent', reasoning_effort: '', effort_pinned: false } },
  // models_backend names the harness the line blames, as the endpoint does.
  { key: 'instead', model: 'claude-fable-5',
    resolved: { model: 'claude-opus-5', pinned: true, kiro_agent: 'reviewer-agent', reasoning_effort: '', effort_pinned: false, models_backend: 'kiro' } },
  // A chosen model the harness cannot claim, with no tier below it naming one.
  { key: 'foreign-automatic', model: 'claude-fable-5',
    resolved: { model: '', pinned: true, kiro_agent: 'reviewer-agent', reasoning_effort: '', effort_pinned: false } },
]

async function openEditor(page, base, c) {
  await stubDashboardApi(page, {
    theme: 'dark',
    extra: async (path, route) => {
      if (path === '/api/agents/resolved-model') return json(route, c.resolved), true
      if (path === '/api/models') return json(route, MODELS), true
      return crewsApi({ crews: [crew(c.model)], defaultAgent: 'kirocrew', installed: ['reviewer-agent'] })(path, route)
    },
  })
  await page.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: 'Edit mate reviewer' }).first().click({ timeout: 15000 })
  const dialog = page.getByTestId('crew-editor-next-dialog')
  await dialog.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(800) // the dialog animates in, then the resolved model lands
  return dialog
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const shot = []
  try {
    for (const c of CASES) {
      const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
      const page = await context.newPage()
      logPageProblems(page)
      const dialog = await openEditor(page, base, c)
      // The Model box sits below the fold of General: bring the picker to the
      // top so the shot holds the picker, its readout and the effort line.
      await dialog.getByRole('combobox', { name: /model/i }).first().evaluate(el => el.scrollIntoView({ block: 'start' }))
      await page.waitForTimeout(300)
      const panel = page.getByTestId('next-model-resolved-error')
      if (await panel.count()) console.log(`${c.key}: error notice: ${await panel.innerText()}`)
      await dialog.screenshot({ path: `${OUT}/model-${c.key}.png` })
      shot.push(`model-${c.key}.png`)
      if (c.key === 'uses') {
        await dialog.getByTestId('next-rail-danger').click()
        await page.waitForTimeout(400)
        await dialog.screenshot({ path: `${OUT}/danger-zone.png` })
        shot.push('danger-zone.png')
      }
      await context.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
  console.log(shot.join('\n'))
}

await main()
