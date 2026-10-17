/**
 * Screenshot harness for #19580: a 429 the gateway sends itself shows its own
 * reason, and only the edge throttle reads as the tunnel edge.
 *
 * Serves the REAL built SPA (website/dist) and answers /api/** from the shared
 * fixture router. The scene-specific stub is `POST /api/channels`, which answers
 * one of two 429 bodies, picked by the `body` query param:
 *
 * - `gateway`: the channel-limit refusal `handlers_channel.py` really returns,
 *   `{"error": "Channel limit reached. …", "code": "channel_limit_reached"}`.
 * - `edge`: API Gateway's throttle, `{"message":"Rate exceeded","throttlingReasons":null}`.
 *
 * Builds the SPA first: serve-dist serves whatever is on disk, so shooting
 * against a stale dist yields an "after" image identical to before.
 *
 * Usage: node scripts/capture-19580-gateway-429.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, json, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/19580-gateway-429'
const PREFIX = process.argv[3] || 'after'

const BODIES = {
  gateway: {
    error: 'Channel limit reached. Close an existing channel first.',
    code: 'channel_limit_reached',
  },
  edge: { message: 'Rate exceeded', throttlingReasons: null },
}

mkdirSync(OUT, { recursive: true })

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  }

  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  const context = await browser.newContext({ viewport: { width: 1300, height: 800 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  await installApiFixtures(page, {
    '/api/channels': { channels: [] },
    '/api/channels/presets': { presets: [] },
  })
  // Registered after the fixture router, so it is consulted first.
  await page.route('**/api/channels', async route => {
    if (route.request().method() !== 'POST') return route.fallback()
    const which = new URL(page.url()).searchParams.get('body') === 'edge' ? 'edge' : 'gateway'
    return json(route, BODIES[which], 429)
  })
  logPageFailures(page)

  await page.addInitScript(() => {
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-lang', 'en')
  })

  for (const which of ['gateway', 'edge']) {
    await page.goto(`${base}/channels?body=${which}`, { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: /^\+ New$/ }).first().click()
    await page.getByRole('textbox', { name: 'Topic' }).fill('Investigate gamma deployment failure')
    await page.getByRole('button', { name: /^Create$/ }).click()
    // Wait for the banner rather than sleeping, so the shot cannot race the render.
    await page.getByText('Failed to create channel').first().waitFor({ state: 'visible', timeout: 15000 })
    const out = `${OUT}/${PREFIX}-${which}-429.png`
    await page.screenshot({ path: out })
    console.log('wrote', out)
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
