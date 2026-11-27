// Screenshots for the Bot profile's Schedules failed-run notice: an
// auto-paused job named with Resume, a user-paused failure with no notice,
// and two active failures, at a phone width.
//   node capture/shoot-crew-schedule-failed-notice.mjs [outDir] [port]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const OUT = process.argv[2] || '/tmp/crew-schedule-failed-notice-shots'
const PORT = Number(process.argv[3] || 5179)
const CHROMIUM_PATH = process.env.CHROMIUM_PATH
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: PORT, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const browser = await chromium.launch({ executablePath: CHROMIUM_PATH, args: ['--no-sandbox'] })

for (const [scenario, theme] of [['auto', 'dark'], ['user', 'dark'], ['many', 'dark'], ['auto', 'light']]) {
  const ctx = await browser.newContext({ viewport: { width: 440, height: 560 }, deviceScaleFactor: 2, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await page.goto(`http://127.0.0.1:${PORT}/capture/crew-schedule-failed-notice.html?scenario=${scenario}&theme=${theme}`)
  await page.waitForSelector('[data-testid="crew-schedule-list"]', { timeout: 20000 })
  await page.waitForTimeout(300)
  await page.locator('[data-capture-root]').screenshot({ path: join(OUT, `${scenario}-${theme}.png`) })
  console.log('shot', scenario, theme)
  await ctx.close()
}

// The Schedule page table, same pill text, from the isolated SchedulePage
// entry with every /api call answered here.
const now = Math.floor(Date.now() / 1000)
const JOBS = [
  { id: 'a', name: 'Thread ledger to board', schedule: 'every 10m', enabled: false, user_paused: false, agent: 'kirocrew', message: 'go', last_status: 'error', consecutive_failures: 5, last_run_ts: now - 86400 },
  { id: 'b', name: 'Release board patrol', schedule: 'every 20m', enabled: false, user_paused: true, agent: 'kirocrew', message: 'go', last_status: 'error', consecutive_failures: 1, last_run_ts: now - 9 * 86400 },
  { id: 'c', name: 'Nightly digest', schedule: 'every 1d', enabled: true, agent: 'kirocrew', message: 'go', last_status: 'ok', last_run_ts: now - 3600, next_run_ts: now + 7200 },
]
const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 520 }, deviceScaleFactor: 2 })
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/crons') return json(route, { jobs: JOBS })
    if (path === '/api/cron-folders') return json(route, [])
    if (path === '/api/agents') return json(route, { agents: [{ name: 'kirocrew', description: '' }], default_agent: 'kirocrew' })
    if (path === '/api/config/default-agent') return json(route, { default_agent: 'kirocrew' })
    if (path === '/api/models') return json(route, [])
    if (path === '/api/crons/history') return json(route, { runs: [] })
    return json(route, {})
  })
  await page.goto(`http://127.0.0.1:${PORT}/capture/schedule-retry-note.html?theme=${theme}`)
  await page.getByText('Paused after 5 failures').first().waitFor({ timeout: 45000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `schedule-page-${theme}.png`) })
  console.log('shot schedule page', theme)
  await page.close()
}

await browser.close()
await server.close()
