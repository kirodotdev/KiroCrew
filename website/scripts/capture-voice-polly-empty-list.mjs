/**
 * Screenshot harness for Settings > Voice when Amazon Polly lists no voices.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server and
 * answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free -- no kiro-cli, no live backend).
 *
 * `GET /api/voice/voices` answers `{"voices": []}` before AWS consent is recorded and
 * when the AWS CLI is missing. The Voice picker then shows the offline fallback
 * voices instead of an empty list.
 *
 * This ASSERTS as well as photographs: it exits non-zero unless the open picker
 * offers the fallback voices.
 *
 * Usage: node scripts/capture-voice-polly-empty-list.mjs [outDir] [prefix] [distDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/29026'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST

mkdirSync(OUT, { recursive: true })

const CONFIG = {
  enabled: true,
  provider: 'polly',
  voice: 'Ruth',
  engine: 'generative',
  rate: '100%',
  autoSpeak: false,
  aws_profile: '',
  region: '',
  system_voice: '',
}

const extra = async (path, route) => {
  if (path === '/api/voice/config') return json(route, CONFIG), true
  if (path === '/api/voice/voices') return json(route, { voices: [] }), true
  if (path === '/api/voice/system-voices') return json(route, { available: true, voices: [] }), true
  return false
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1200, height: 1000 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { slots: [], extra, localStorageEntries: { 'mc-lang': 'en' } })
  logPageProblems(page)
  await page.goto(base + '/settings/voice', { waitUntil: 'domcontentloaded' })
  await page.waitForResponse(r => r.url().includes('/api/voice/voices'), { timeout: 20000 })
  const picker = page.getByRole('combobox', { name: /^Voice$/ })
  await picker.waitFor({ timeout: 20000 })
  await picker.click()
  for (const name of ['Ruth (US F)', 'Matthew (US M)']) {
    await page.getByRole('option', { name }).waitFor({ timeout: 10000 })
  }
  await page.screenshot({ path: `${OUT}/${PREFIX}-picker-open.png` })
  console.log('wrote', `${OUT}/${PREFIX}-picker-open.png`)
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
