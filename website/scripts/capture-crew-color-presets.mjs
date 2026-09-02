/**
 * Screenshot harness for the crew editor's session-colour preset row.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, answering every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * The change is a row of quick-pick swatches added above the existing custom
 * swatch + hex field, so the evidence has to show three states rather than one:
 * a crew with no colour (nothing ringed), a crew whose colour IS a preset (that
 * swatch ringed), and a crew whose colour is a custom hex outside the preset set
 * (nothing ringed, hex field still truthful). One crew per state, in both
 * themes, so the ring is shown to read on each background.
 *
 * Usage: node scripts/capture-crew-color-presets.mjs [outDir] [prefix]
 *   Run against the branch (after) and a main build (before) — on main the
 *   preset row does not exist, so the `before` run shows the same panes with
 *   only the custom picker.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-color-presets'
const PREFIX = process.argv[3] || 'after'

// One crew per state the preset row can be in. `#6366f1` is the first preset
// (indigo); `#0a7d55` is deliberately NOT in the set.
const CREWS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' },
  { name: 'devops', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', session_color: '#6366f1' },
  { name: 'makerworks', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', session_color: '#0a7d55' },
]

/** Endpoints the editor reads that `crewsApi` does not cover. A table, not a
 *  branch chain: the editor only needs these to be present and inert. */
const EXTRA = {
  '/api/crons': { jobs: [] },
  '/api/webhooks': { tokens: [] },
  '/api/agents/resolved-model': { model: 'claude-opus-5', pinned: false },
}

mkdirSync(OUT, { recursive: true })

const fixtures = crewsApi({ crews: CREWS, defaultAgent: 'kirocrew' })
const routeAll = async (path, route) => {
  const canned = EXTRA[path]
  if (!canned) return fixtures(path, route)
  await route.fulfill({ contentType: 'application/json', body: JSON.stringify(canned) })
  return true
}

// Flat (theme, crew) matrix: every shot is one independent navigation, so there
// is no per-theme setup worth nesting a loop for.
const SHOTS = ['dark', 'light'].flatMap(theme => CREWS.map(c => [theme, c.name]))

const browser = await chromium.launch()
const { srv, base } = await serveDist()
try {
  for (const [theme, crew] of SHOTS) {
    const ctx = await browser.newContext({
      viewport: { width: 1400, height: 900 },
      deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
    })
    const page = await ctx.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { theme, extra: routeAll })

    await page.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
    const cards = page.locator('#main-content [data-testid="crew-card"]')
    await cards.first().waitFor({ state: 'visible', timeout: 15000 })
    await cards.filter({ hasText: crew }).first().click()

    const sheet = page.getByRole('dialog')
    await sheet.waitFor({ state: 'visible', timeout: 15000 })
    // Session colour lives in the routing pane, under Triggers.
    await sheet.locator('[data-testid="crew-rail-routing"]').click()
    await page.waitForTimeout(400) // the pane swaps, then the field lays out

    await page.screenshot({ path: `${OUT}/${PREFIX}-${theme}-${crew}.png` })
    console.log(`${PREFIX}-${theme}-${crew}.png`)
    await ctx.close()
  }
} finally {
  await browser.close()
  srv.close()
}
