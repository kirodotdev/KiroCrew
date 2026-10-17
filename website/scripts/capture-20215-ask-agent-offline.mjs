/**
 * Screenshot harness for #20215: "Ask the agent" hides while the gateway is
 * unreachable and shows again once the socket is connected.
 *
 * Serves the REAL built SPA (website/dist) and answers /api/** from the shared
 * fixture router. Two scenes, picked by how the `/api/ws` stub behaves:
 *
 * - `offline`: the stub closes every socket at once, so the dashboard sits in
 *   its disconnected state ("gateway offline").
 * - `online`: the stub keeps the socket open, so the dashboard is connected.
 *
 * In both, `POST /api/channels` fails at the network layer ("Failed to
 * fetch"), which is the error a down gateway really produces.
 *
 * Builds the SPA first unless SKIP_BUILD=1: serve-dist serves whatever is on
 * disk, so a stale dist yields an "after" image identical to before.
 *
 * Usage: node scripts/capture-20215-ask-agent-offline.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/20215-ask-agent-offline'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

async function shootScene(browser, base, scene) {
  const context = await browser.newContext({ viewport: { width: 1300, height: 800 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await installApiFixtures(page, {
    '/api/channels': { channels: [] },
    '/api/channels/presets': { presets: [] },
  })
  // Registered after the fixture router, so these are consulted first.
  await page.routeWebSocket(/\/api\/ws/, ws => { if (scene === 'offline') ws.close() })
  await page.route('**/api/channels', async route => {
    if (route.request().method() !== 'POST') return route.fallback()
    return route.abort('failed')
  })
  logPageFailures(page)
  await page.addInitScript(() => {
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-lang', 'en')
  })

  await page.goto(`${base}/channels`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: /^\+ New$/ }).first().click()
  await page.getByRole('textbox', { name: 'Topic' }).fill('Investigate gamma deployment failure')
  await page.getByRole('button', { name: /^Create$/ }).click()
  await page.getByText('Failed to create channel').first().waitFor({ state: 'visible', timeout: 15000 })
  // Let the socket stub settle the connected flag before the shot.
  await page.waitForTimeout(1500)
  const asks = await page.getByRole('button', { name: /ask the agent/i }).count()
  const out = `${OUT}/${PREFIX}-${scene}.png`
  await page.screenshot({ path: out })
  console.log('wrote', out, `ask-the-agent buttons: ${asks}`)
  await context.close()
}

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  }
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  for (const scene of ['offline', 'online']) await shootScene(browser, base, scene)
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
