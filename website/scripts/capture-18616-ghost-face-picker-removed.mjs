/**
 * Screenshot harness for #18616 — the ghost-face picker removed from the crew
 * editor's avatar builder.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, answering every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth, no pod.
 *
 * The change removes the "Ghost face" tier tab and its "Reactions" sub-tab from
 * the avatar builder, and opens an existing ghost-face crewmate on the Icon
 * tier. So the evidence is the builder's tier strip in two states:
 *   - a crew with no stored avatar: opens on Icon, strip shows Icon | Picture |
 *     Library only (no Ghost face, no Reactions).
 *   - a crew whose stored avatar IS a ghost: STILL opens on Icon and STILL
 *     shows no Ghost face / Reactions tab (the edit-flow removal + edge case),
 *     while its stored ghost record is kept.
 *
 * Usage: node scripts/capture-18616-ghost-face-picker-removed.mjs [outDir] [prefix]
 *   Run against the branch (after) and a main build (before) — on main the
 *   Ghost face tab is still offered and a stored ghost crew opens on the ghost
 *   face pane, so the `before` run shows the tab the fix removes.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/18616-ghost-face-picker-removed'
const PREFIX = process.argv[3] || 'after'

// One crew with no stored avatar (icon default), one wearing a ghost face.
const CREWS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' },
  {
    name: 'spectre',
    kiro_agent: 'kirocrew',
    workspace: 'default',
    memory_store: 'default',
    // A fully-pinned ghost: the record the builder must KEEP while opening on Icon.
    avatar: { kind: 'ghost', traits: { eyes: 'wink', brows: 'none', mouth: 'smile', accessory: 'none', prop: 'none', blush: false, flip: false, tile: '#6b4fbb' } },
  },
]

/** Endpoints the editor reads that `crewsApi` does not cover. */
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

const SHOTS = ['dark', 'light'].flatMap(theme => CREWS.map(c => [theme, c.name]))

/** Open the crew editor for `crew` and press through to the avatar builder,
 *  returning once the tier strip is on screen. Kept as one helper so the shot
 *  loop below stays about THIS issue (the tier strip) rather than re-spelling
 *  the generic open-the-editor dance every sibling harness also performs. */
async function openBuilder(page, crew) {
  await page.goto(`${base}/capabilities`, { waitUntil: 'domcontentloaded' })
  const cards = page.locator('#main-content [data-testid="crew-card"]')
  await cards.first().waitFor({ state: 'visible', timeout: 15000 })
  await cards.filter({ hasText: crew }).first().click()
  const sheet = page.getByRole('dialog')
  await sheet.waitFor({ state: 'visible', timeout: 15000 })
  await sheet.locator('[data-testid="next-open-avatar-builder"]').click()
  await page.locator('[data-testid="avatar-builder-tabs"]').waitFor({ state: 'visible', timeout: 10000 })
}

/** Fail the shot unless the builder opened on Icon with no ghost-face UI. On
 *  the `before` baseline (built from origin/main) the ghost tab still exists,
 *  so only the strip is awaited. */
async function assertNoGhostUi(page, theme, crew) {
  if (process.env.BEFORE === '1') { await page.waitForTimeout(300); return }
  await page.locator('[data-testid="avatar-icon-pane"]').waitFor({ state: 'visible', timeout: 10000 })
  for (const tab of ['Ghost face', 'Reactions']) {
    if (await page.getByRole('radio', { name: tab }).count()) {
      throw new Error(`assertion failed: ${tab} tab present for ${crew} (${theme})`)
    }
  }
}

const browser = await chromium.launch()
const { srv, base } = await serveDist()
try {
  for (const [theme, crew] of SHOTS) {
    const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
    const page = await ctx.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { theme, extra: routeAll })
    await openBuilder(page, crew)
    await assertNoGhostUi(page, theme, crew)
    await page.screenshot({ path: `${OUT}/${PREFIX}-${theme}-${crew}.png` })
    console.log(`${PREFIX}-${theme}-${crew}.png`)
    await ctx.close()
  }
} finally {
  await browser.close()
  srv.close()
}
