/**
 * Screenshot harness for issue #22582: the Set up Kiro page must name the `unzip`
 * the Linux Kiro CLI installer needs when the gateway host does not have it.
 *
 * Two states of the first-run gate on a Linux gateway host with Kiro CLI not
 * installed:
 *  - `unzip-missing`: the gateway reports `unzip_missing: true`, the stock
 *    Ubuntu case the issue describes;
 *  - `unzip-present`: the gateway reports `unzip_missing: false` (the control:
 *    the card must look as it does today).
 *
 * Both states are captured in the dark theme, and `unzip-missing` again in the
 * light theme (`unzip-missing-light`), where the muted distro labels have the
 * least contrast.
 *
 * For each state the harness records whether the prerequisite block rendered,
 * the code blocks on the Kiro CLI card in page order, and screenshots the page.
 *
 * Runs the REAL built SPA behind the shared loopback static server with every
 * /api/** call answered from fixtures (gateway-free). `DIST` selects the build.
 *
 * Usage: DIST=<dist dir> node scripts/capture-22582-unzip-prerequisite.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/22582-unzip-prerequisite'
mkdirSync(OUT, { recursive: true })

const status = unzipMissing => ({
  platform: 'Linux', installed: false, authenticated: false, ready: false,
  initial_setup_complete: false, repair_required: false, bundled_cli: false,
  docs_url: 'https://kiro.dev/cli/', login_command: 'kiro-cli login',
  sso_login_command: 'kiro-cli login --use-device-flow --license pro',
  setup_allowed: true, sandbox_unavailable: false, sandbox_backend_available: true,
  sandbox_failure_kind: '', sandbox_detail: '', sandbox_remedy: '',
  missing_agent_specs: [], agent_spec_repair_error: '', unzip_missing: unzipMissing,
  operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
})

async function capture(browser, base, label, unzipMissing, theme) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  const extra = (path, route) => {
    if (path === '/api/kiro-prerequisite') return json(route, status(unzipMissing)), true
    if (path === '/api/acp-backends') return json(route, { backends: [] }), true
    return false
  }
  await stubDashboardApi(page, { theme, extra })
  await page.addInitScript(() => localStorage.setItem('mc-privacy-notice-v1', '1'))
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  const card = page.getByRole('heading', { name: 'Get Kiro CLI' })
  await card.waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(800)
  await page.screenshot({ path: `${OUT}/${label}.png`, fullPage: true })
  const row = {
    state: label,
    theme,
    prerequisiteShown: await page.getByTestId('kiro-gate-unzip-missing').count() > 0,
    codeBlocks: await page.locator('code').evaluateAll(els => els.map(e => e.textContent)),
  }
  await context.close()
  return row
}

async function main() {
  const { srv, base } = await serveDist(process.env.DIST || undefined)
  const browser = await chromium.launch()
  const report = []
  const states = [
    ['unzip-missing', true, 'dark'],
    ['unzip-present', false, 'dark'],
    ['unzip-missing-light', true, 'light'],
  ]
  for (const [label, missing, theme] of states) {
    report.push(await capture(browser, base, label, missing, theme))
  }
  console.log(JSON.stringify(report, null, 1))
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
