/**
 * Screenshot harness for the Developer > Feature Previews page.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Two shots, because the card only tells half its story with the switch off: the
 * ingress into the hidden page is progressively disclosed, so it exists in the
 * `on` frame and must be absent from the `off` one.
 *
 * Run against a main build with the `before` prefix and the tab is absent; the
 * script then shoots Developer > Config, which is where the toggles live on a
 * main build, so the pair reads as a move rather than two unrelated pages.
 *
 * Usage: node scripts/capture-feature-previews.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../.github/screenshots/feature-previews'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

/**
 * The Config tab's two viewers, which the `before` run has to render.
 *
 * Named here rather than in the shared stub because the shared stub's catch-all
 * answers anything path-matching `config` with `{}`, and `KiroCrewCfgTab` calls
 * `Object.entries(cfg.agents)` — undefined under that default, which throws
 * inside the app-shell error boundary and leaves the whole page blank. Only a
 * harness that actually opens Config needs these, so the fixture lives with it.
 *
 * Returns `true` after fulfilling, never the `json()` promise: the shared stub
 * tests `await extra(...)` to decide whether the route is already handled, and
 * that promise resolves to `undefined`, so the catch-all would fulfil a second
 * time and Playwright throws `Route is already handled!`.
 */
const CONFIG_API = async (path, route) => {
  if (path === '/api/config/kirocrew') {
    await json(route, {
      agents: { kirocrew: { kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' } },
      default_agent: 'kirocrew',
      workspaces: { default: { path: '~/.kiro/crew/workspace' } },
      default_workspace: 'default',
      memory_stores: { default: { path: '~/.kiro/crew/workspace/memory' } },
      default_memory_store: 'default',
      agent: {
        default_agent: 'kirocrew', provider: 'acp', model: 'auto',
        approval_mode: 'interactive', sandbox: 'auto', max_channels: 8,
        max_channel_agents: 4, enforce_denied_commands: 'always',
      },
      session: { timeout_secs: 1800, pool_size: 2, pool_agent: 'kirocrew', pool_ttl_secs: 600 },
      memory: { embedding_provider: 'local' },
      auto_update: true,
    })
    return true
  }
  if (path === '/api/agent/config') {
    await json(route, { name: 'kirocrew', mcpServers: {} })
    return true
  }
  return false
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
  })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, { extra: CONFIG_API })
  // AFTER the shared stub, whose own init script clears storage: Developer Mode
  // is what puts the Developer row in the sidebar at all.
  await page.addInitScript(() => localStorage.setItem('mc-dev-mode', '1'))

  const shot = []
  const save = async (name) => {
    await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
    shot.push(`${PREFIX}-${name}.png`)
  }

  await page.goto(base + '/developer?tab=feature-previews', { waitUntil: 'domcontentloaded' })
  const rail = page.getByRole('button', { name: /feature previews/i })
  const onBranch = await rail.count() > 0

  if (!onBranch) {
    // A main build: the toggles are a card at the top of Config.
    await page.goto(base + '/developer?tab=config', { waitUntil: 'domcontentloaded' })
  }
  const toggle = page.getByRole('switch', { name: /webhooks/i })
  await toggle.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(500) // let the card's rise animation finish

  await save('off')

  await toggle.click()
  await page.getByRole('button', { name: /open webhooks/i })
    .waitFor({ state: 'visible', timeout: 5000 })
  await page.waitForTimeout(300)
  await save('on')

  await browser.close()
  srv.close()
  console.log(`wrote ${shot.length} shot(s) to ${OUT}: ${shot.join(', ')}`)
}

main().catch(err => { console.error(err); process.exit(1) })
