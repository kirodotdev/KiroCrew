/**
 * Evidence for #35351: Settings > About's Restart, when the gateway answers 200
 * and then refuses the restart on its update-progress stream.
 *
 * Two frames per theme:
 *   restarting-<theme>.png  the button busy after the 200
 *   refused-<theme>.png     the late refusal's reason shown, the button ready again
 *
 * Runs the built SPA (`npm run build`) behind serve-dist with every /api call
 * stubbed and the dashboard socket faked, so the refusal frame is sent on cue.
 *
 * Usage: node scripts/capture-35351-restart-late-refusal.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/35351-restart-late-refusal'
const REASON = 'Sessions did not drain in time.'

const STATUS = {
  sessions: 0, crons: 0, lessons: 0, uptime: 120,
  version: '0.13.1', version_display: '0.13.1', release_channel: 'stable',
  update_available: true, update_check_status: 'succeeded', update_can_apply: false,
  update_command: 'curl -fsSL https://example.test/cli.sh | sh',
  update_latest_version: '0.13.2', update_latest_version_display: '0.13.2',
  update_channel: 'stable',
}

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  let ok = true
  try {
    for (const theme of ['dark', 'light']) {
      const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2 })
      const page = await context.newPage()
      logPageProblems(page)
      let socket = null
      await stubDashboardApi(page, {
        theme,
        onWebSocket: ws => { socket = ws },
        extra: async (path, route) => {
          if (path === '/api/status') return json(route, STATUS), true
          if (path === '/api/restart') return json(route, { ok: true, status: 'restarting' }), true
          if (path.startsWith('/api/update/check')) return json(route, { check_status: 'succeeded', update_available: true }), true
          return false
        },
      })
      await page.goto(`${base}/settings/about`, { waitUntil: 'domcontentloaded' })
      const dismiss = page.getByRole('button', { name: /not now|dismiss|later/i }).first()
      await dismiss.click({ timeout: 8000 }).catch(() => {})

      const restart = page.getByTestId('gateway-restart')
      await restart.waitFor({ state: 'visible', timeout: 20_000 })
      await restart.scrollIntoViewIfNeeded()
      await restart.click()
      await restart.click()
      await page.waitForFunction(() => document.querySelector('[data-testid="gateway-restart"]')?.hasAttribute('disabled'), null, { timeout: 10_000 })
      if (!socket) throw new Error('dashboard socket never opened')
      const send = (step, detail) => socket.send(JSON.stringify({ type: 'update_progress', data: { step, detail } }))
      send('restarting', 'Restarting server…')
      await page.waitForTimeout(400)
      const panel = page.getByTestId('manual-update-command').locator('xpath=ancestor::*[.//*[@data-testid="gateway-restart"]][1]')
      await panel.screenshot({ path: `${OUT}/restarting-${theme}.png` })
      console.log('wrote', `${OUT}/restarting-${theme}.png`)

      send('error', REASON)
      const notice = page.getByTestId('manual-update-restart-error')
      await notice.waitFor({ state: 'visible', timeout: 10_000 })
      const text = (await notice.textContent()) || ''
      const ready = !(await restart.isDisabled())
      if (!text.includes(REASON) || !ready) { ok = false; console.log(`FAIL ${theme}: notice="${text}" ready=${ready}`) }
      await page.waitForTimeout(300)
      await panel.screenshot({ path: `${OUT}/refused-${theme}.png` })
      console.log('wrote', `${OUT}/refused-${theme}.png`)
      await context.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
  if (!ok) process.exit(1)
  console.log('PASS')
}

await main()
