/**
 * Screenshot harness for #32966: while the gateway restarts, the dashboard's
 * socket is down and the KAS status reads fail. Before the fix the sign-in
 * notice and the KAS engine notice each showed a causeless error alert over
 * the offline state; after it, only the offline state shows.
 *
 * Serves the REAL built SPA (website/dist) and answers /api/** from the shared
 * stub map, with `agent.acp_backend` set to `kas`. The dashboard socket is
 * closed as soon as it opens (the gateway is down), and `GET /api/kas-login`
 * and `GET /api/kas-bundle` answer 503, as a restarting gateway's proxy does.
 *
 * Usage: node scripts/capture-32966-restart-alerts.mjs [outDir] [prefix]
 * (SKIP_BUILD=1 reuses website/dist; DIST=<path> serves another build.)
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { DEFAULT_DIST, serveDist } from './lib/serve-dist.mjs'
import {
  KIROCREW_CONFIG_FIXTURE,
  json,
  logPageProblems,
  stubDashboardApi,
} from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/32966-restart-alerts'
const PREFIX = process.argv[3] || 'after'
const DIST = process.env.DIST || DEFAULT_DIST

const CONFIG = {
  ...KIROCREW_CONFIG_FIXTURE,
  agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kas' },
}

/** Long enough for both chained status reads to fail and render. */
const SETTLE_MS = 6000

mkdirSync(OUT, { recursive: true })

async function main() {
  if (!process.env.SKIP_BUILD && !process.env.DIST) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  }

  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })

  for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({ viewport: { width: 1300, height: 800 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots: [],
      theme,
      localStorageEntries: { 'mc-onboarded': '1', 'mc-lang': 'en' },
      // The gateway is down: every socket it accepts drops at once.
      onWebSocket: ws => ws.close(),
      // `return json(...), true` -- the comma marks the request handled.
      extra: async (path, route) => {
        if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' }), true
        if (path === '/api/config/kirocrew') return json(route, CONFIG), true
        if (path === '/api/kas-login' || path === '/api/kas-bundle') {
          return json(route, { error: 'gateway restarting' }, 503), true
        }
        if (path === '/api/acp-backends') return json(route, { backends: [] }), true
        return false
      },
    })
    await page.goto(`${base}/settings/display`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(SETTLE_MS)
    const shown = {
      signIn: await page.getByTestId('kiro-sign-in-status-error').count(),
      engine: await page.getByTestId('kas-engine-status-error').count(),
    }
    console.log(theme, 'status alerts shown:', JSON.stringify(shown))
    const out = `${OUT}/${PREFIX}-restart-${theme}.png`
    await page.screenshot({ path: out })
    console.log('wrote', out)
    await context.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
