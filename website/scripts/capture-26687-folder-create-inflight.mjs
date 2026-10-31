/**
 * Screenshots for issue #26687: the Schedule page "New folder" dialog while
 * its create POST is in flight. Shot from the isolated SchedulePage capture
 * entry (capture/schedule-retry-note.html) with every /api/** call answered
 * here (gateway-free). The create POST is held open, so the frame shows the
 * in-flight state: Create reads "Creating…" and it and Cancel are disabled.
 * Each frame ASSERTS what it documents (one POST after Enter, Enter, click)
 * before writing.
 *
 *   folder-create-inflight-{dark,light}   dialog with the POST held open
 *
 * Usage (from website/; starts its own vite on 127.0.0.1:5199):
 *   node scripts/capture-26687-folder-create-inflight.mjs [outDir]
 */
import { createServer } from 'vite'
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/26687'
mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5199, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const BASE = 'http://127.0.0.1:5199'

const now = Math.floor(Date.now() / 1000)
const JOBS = [
  {
    id: 'job-1', name: 'Nightly report', schedule: 'every 1d', enabled: true, agent: 'kirocrew',
    message: 'Summarise CI failures.', last_status: 'ok', last_run_ts: now - 3600, next_run_ts: now + 7200,
  },
]

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

const browser = await chromium.launch()
let failed = false

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 1000, height: 600 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => console.error('pageerror:', e.message))
  let posts = 0
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()
    if (path === '/api/cron-folders' && method === 'POST') {
      posts += 1
      return // held open: never fulfilled, so the dialog stays in flight
    }
    if (path === '/api/crons') return json(route, { jobs: JOBS })
    if (path === '/api/cron-folders') return json(route, [])
    if (path === '/api/agents') return json(route, { agents: [{ name: 'kirocrew', description: '' }], default_agent: 'kirocrew' })
    if (path === '/api/config/default-agent') return json(route, { default_agent: 'kirocrew' })
    if (path === '/api/models') return json(route, [])
    if (path === '/api/crons/history') return json(route, { runs: [] })
    return json(route, {})
  })
  await page.goto(`${BASE}/capture/schedule-retry-note.html?theme=${theme}`)
  await page.getByText('Nightly report').first().waitFor({ timeout: 45000 })
  await page.getByRole('button', { name: /New folder/ }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.waitFor()
  const input = dialog.getByLabel('New folder name')
  await input.fill('Reports')
  await input.press('Enter')
  await input.press('Enter')
  await dialog.getByRole('button', { name: /^Creat/ }).click({ force: true })
  const busy = dialog.getByRole('button', { name: 'Creating…' })
  await busy.waitFor()
  const createOff = await busy.isDisabled()
  const cancelOff = await dialog.getByRole('button', { name: 'Cancel' }).isDisabled()
  const ok = posts === 1 && createOff && cancelOff
  console.log(`${theme}: ${ok ? 'OK' : 'MISMATCH'} posts=${posts} createDisabled=${createOff} cancelDisabled=${cancelOff}`)
  if (!ok) failed = true
  await dialog.screenshot({ path: `${OUT}/folder-create-inflight-${theme}.png` })
  await page.close()
}

await browser.close()
await server.close()
process.exit(failed ? 1 : 0)
