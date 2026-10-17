/**
 * Screenshot harness for the Teams and WhatsApp optional-dependency card.
 *
 * Both panels used to print a bare `pip install ...` when their optional extra
 * was missing, which can install into an interpreter the gateway never imports
 * from. They now render the shared OptionalSdkCard Feishu already uses, so the
 * two states worth photographing per panel are:
 *
 *   missing-extra   — the SDK is absent and the gateway can pip: the card shows
 *                     the command naming the gateway's own interpreter
 *   sdk-unsupported — the SDK is absent and no in-place install can work: the
 *                     card withholds the command and says so
 *
 * Each shot asserts its distinguishing text first, and that no bare
 * `pip install` (one not preceded by `-m `) is on the page, so a regression
 * cannot pass as a screenshot. Run against the real built SPA (website/dist)
 * with the dashboard API stubbed; nothing in CI runs this file.
 *
 * Usage: node scripts/capture-channel-sdk-card.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { BASE_CONFIG as WA_BASE } from './lib/whatsapp-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/channel-sdk-card'

const TEAMS_BASE = {
  connected: false,
  connect_error: '',
  configured: true,
  read_only: false,
  app_id_set: true,
  app_password_set: true,
  enabled: true,
  tenant_id: '',
  allowed_emails: ['alice@example.com'],
  jwt_available: false,
  soft_threshold_pct: 80,
  hard_threshold_pct: 95,
  session_folder: '',
  sdk_installed: false,
}

const WA_MISSING = {
  ...WA_BASE,
  state: 'unpaired',
  connected: false,
  configured: false,
  enabled: true,
  // What the gateway writes on a missing extra; the panel must not show it.
  connect_error: 'The WhatsApp channel needs its optional dependency: pip install neonize==0.4.3.post0',
  sdk_installed: false,
}

const SCENARIOS = [
  {
    channel: 'teams',
    name: 'missing-extra',
    path: '/api/teams/config',
    config: {
      ...TEAMS_BASE,
      sdk_install_supported: true,
      sdk_install_command: '/opt/kirocrew/.venv/bin/python -m pip install "PyJWT[crypto]==2.15.0"',
    },
    expectText: /-m pip install "PyJWT\[crypto\]==2\.15\.0"/,
  },
  {
    channel: 'teams',
    name: 'sdk-unsupported',
    path: '/api/teams/config',
    config: { ...TEAMS_BASE, sdk_install_supported: false, sdk_install_command: '' },
    expectText: /cannot install extra packages/,
  },
  {
    channel: 'whatsapp',
    name: 'missing-extra',
    path: '/api/whatsapp/config',
    config: {
      ...WA_MISSING,
      sdk_install_supported: true,
      sdk_install_command: '/opt/kirocrew/.venv/bin/python -m pip install neonize==0.4.3.post0',
    },
    expectText: /-m pip install neonize==0\.4\.3\.post0/,
  },
  {
    channel: 'whatsapp',
    name: 'sdk-unsupported',
    path: '/api/whatsapp/config',
    config: { ...WA_MISSING, sdk_install_supported: false, sdk_install_command: '' },
    expectText: /cannot install extra packages/,
  },
]

function routes(scenario) {
  return async (path, route) => {
    if (path === scenario.path) return json(route, scenario.config), true
    if (path === '/api/whatsapp/qr/status') {
      return json(route, { state: 'unpaired', qr_data_url: null, detail: '' }), true
    }
    if (path === '/api/whatsapp/groups') return json(route, { groups: [] }), true
    // The channel accordion hides settings behind the `channels` governance
    // policy; a failed read renders the policy-unavailable placeholder.
    if (path === '/api/governance/channels') return json(route, { [scenario.channel]: true }), true
    return false
  }
}

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const theme of ['light', 'dark']) {
      for (const scenario of SCENARIOS) {
        const context = await browser.newContext({
          viewport: { width: 1400, height: 1000 },
          deviceScaleFactor: 2,
        })
        const page = await context.newPage()
        logPageProblems(page)
        await stubDashboardApi(page, { theme, extra: routes(scenario) })
        await page.goto(`${base}/settings?tab=channels&channel=${scenario.channel}`, {
          waitUntil: 'domcontentloaded',
        })
        const shown = page.getByText(scenario.expectText).first()
        await shown.waitFor({ state: 'visible', timeout: 20000 })
        await shown.scrollIntoViewIfNeeded()
        await page.waitForTimeout(500)
        const text = await page.locator('body').innerText()
        if (text.replaceAll('-m pip install', '').includes('pip install')) {
          throw new Error(`${scenario.channel}/${scenario.name}: a bare pip install is on the page`)
        }
        const file = `${OUT}/${scenario.channel}-${scenario.name}-${theme}.png`
        await page.screenshot({ path: file, fullPage: true })
        console.log('wrote', file)
        await context.close()
      }
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

main()
