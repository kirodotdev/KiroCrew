/**
 * Screenshot harness for #26385: choosing KAS on a host where kiro-cli's relay
 * cannot run takes Kiro Crew's own engine, and the gateway downloads it.
 *
 * Serves the REAL built SPA (website/dist) and answers /api/** from the shared
 * stub map, with `agent.acp_backend` set to `kas` and `GET /api/kas-bundle`
 * answering the scene: the app-level notice while the gateway downloads the
 * engine, the notice for a failed (offline) download with its retry command, and
 * the KAS engine card on Settings > Developer > Feature Previews saying the engine
 * runs because kiro-cli is not installed.
 *
 * Usage: node scripts/capture-26385-kas-own-engine.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import {
  KAS_BUNDLE_STATUS_FIXTURE,
  KIROCREW_CONFIG_FIXTURE,
  json,
  logPageProblems,
  stubDashboardApi,
} from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/26385-kas-own-engine'
const PREFIX = process.argv[3] || 'after'

const CONFIG = {
  ...KIROCREW_CONFIG_FIXTURE,
  agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kas' },
}

const AUTO_ROUTE = {
  route: 'bundle',
  automatic: true,
  reason: 'no_kiro_cli',
  summary: "KAS runs on Kiro Crew's own engine because kiro-cli is not installed",
}

const OFFLINE_ERROR = 'Kiro Crew could not install its KAS engine: could not fetch '
  + 'https://downloads.example.com/kas/0.66.33/manifest.json: <urlopen error [Errno -3] '
  + 'Temporary failure in name resolution>. If this host is offline, connect it first. Then '
  + 'retry: run `kirocrew kas install` in a terminal, or press Retry in the dashboard.'

const SCENES = {
  downloading: {
    status: { ...KAS_BUNDLE_STATUS_FIXTURE, installing: true, progress: { done: 23_068_672, total: 57_671_680 }, route: AUTO_ROUTE },
    path: '/settings/display',
    testId: 'kas-engine-downloading',
  },
  failed: {
    status: { ...KAS_BUNDLE_STATUS_FIXTURE, error: OFFLINE_ERROR, route: AUTO_ROUTE },
    path: '/settings/display',
    testId: 'kas-engine-failed',
  },
  statuserror: {
    status: null,
    path: '/settings/display',
    testId: 'kas-engine-status-error',
  },
  card: {
    status: {
      ...KAS_BUNDLE_STATUS_FIXTURE,
      installed: { version: '0.66.33', node_version: '22.23.3', bundled: false },
      route: AUTO_ROUTE,
    },
    path: '/settings/developer/feature-previews',
    testId: 'kas-bundle-route-auto',
  },
}

mkdirSync(OUT, { recursive: true })

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  }

  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })

  for (const [name, scene] of Object.entries(SCENES)) {
    for (const theme of ['dark', 'light']) {
      const context = await browser.newContext({ viewport: { width: 1300, height: 800 }, deviceScaleFactor: 2 })
      const page = await context.newPage()
      logPageProblems(page)
      await stubDashboardApi(page, {
        slots: [],
        theme,
        localStorageEntries: { 'mc-onboarded': '1', 'mc-lang': 'en', 'mc-dev-mode': '1' },
        // `return json(...), true` -- the comma marks the request handled.
        extra: async (path, route) => {
          if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' }), true
          if (path === '/api/config/kirocrew') return json(route, CONFIG), true
          if (path === '/api/kas-bundle') {
            return (scene.status ? json(route, scene.status) : json(route, { error: 'gateway restarting' }, 503)), true
          }
          if (path === '/api/acp-backends') return json(route, { backends: [] }), true
          if (path.startsWith('/api/chat/slots/')) {
            return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }), true
          }
          return false
        },
      })
      await page.goto(`${base}${scene.path}`, { waitUntil: 'domcontentloaded' })
      const target = page.getByTestId(scene.testId)
      await target.waitFor({ state: 'visible', timeout: 15000 })
      if (name === 'card') await target.scrollIntoViewIfNeeded()
      const out = `${OUT}/${PREFIX}-${name}-${theme}.png`
      await page.screenshot({ path: out })
      console.log('wrote', out)
      await context.close()
    }
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
