/**
 * Screenshot probe: a refused shortcut or push-to-talk save (#21832, Design and
 * UX Review).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures (gateway-free). An init
 * script makes `localStorage.setItem` throw a QuotaExceededError for the
 * explicit-save marker `mc-ui-prefs-plain-dirty`, the way near-full storage
 * refuses a new key, so `savePrefExplicitly` stores nothing. Each frame shows
 * the switch still on its stored value with the ErrorNotice under it:
 *
 *   1. Settings > Shortcuts, after turning "Enable shortcuts" off.
 *   2. The Alt+K shortcuts modal, after the same click.
 *   3. Settings > Voice > push-to-talk, after turning the key off.
 *
 * Usage: node scripts/capture-shortcut-pref-save-refused.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/21832'
mkdirSync(OUT, { recursive: true })

const slots = [{
  key: 's1', title: 'Worker', messages: 2, running: false, agent: 'kirocrew',
  created: '2026-09-19T01:00:00Z', last_ts: '2026-09-19T21:14:00Z',
}]

const FAIL_WRITES = `
  (() => {
    const real = Storage.prototype.setItem;
    Storage.prototype.setItem = function (k, v) {
      if (k === 'mc-ui-prefs-plain-dirty') {
        throw new DOMException('quota', 'QuotaExceededError');
      }
      return real.call(this, k, v);
    };
  })();
`

const sttConfig = {
  enabled: true, provider: 'whisper', streaming: false, providers: ['whisper'],
  streaming_providers: [], models: { turbo: '1.5 GB' }, installed: ['turbo'], model: 'turbo',
}

async function shoot(page, testId, file) {
  const notice = page.getByTestId(testId).first()
  await notice.waitFor({ timeout: 5000 })
  await notice.scrollIntoViewIfNeeded()
  await page.waitForTimeout(300)
  const nb = await notice.boundingBox()
  const vp = page.viewportSize()
  const y = Math.max(0, nb.y - 170)
  await page.screenshot({
    path: `${OUT}/${file}`,
    clip: { x: Math.max(0, nb.x - 260), y, width: vp.width - Math.max(0, nb.x - 260), height: Math.min(vp.height - y, nb.height + 260) },
  })
  console.log('wrote', `${OUT}/${file}`)
}

async function newPage(browser) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await page.addInitScript(FAIL_WRITES)
  const extra = async (path, route) => {
    if (path === '/api/config/stt') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sttConfig) })
      return true
    }
    return false
  }
  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)
  return { context, page }
}

async function main() {
  const served = await serveDist()
  const base = served.base
  const browser = await chromium.launch()

  {
    const { context, page } = await newPage(browser)
    await page.goto(base + '/settings?tab=shortcuts', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    await page.getByRole('switch', { name: /enable shortcuts/i }).first().click()
    await page.waitForTimeout(500)
    await shoot(page, 'shortcut-pref-save-error', 'settings-shortcuts-save-refused.png')
    await context.close()
  }

  {
    const { context, page } = await newPage(browser)
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    await page.keyboard.press('Alt+KeyK')
    const dialog = page.getByRole('dialog').first()
    await dialog.waitFor({ timeout: 5000 })
    await dialog.getByRole('switch', { name: /enable shortcuts/i }).first().click()
    await page.waitForTimeout(500)
    await shoot(page, 'shortcut-pref-save-error', 'shortcuts-modal-save-refused.png')
    await context.close()
  }

  {
    const { context, page } = await newPage(browser)
    await page.goto(base + '/settings?tab=voice', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    await page.getByRole('button', { name: /fine-tuning/i }).first().click()
    await page.getByRole('button', { name: /start dictation with a key/i }).first().click()
    await page.waitForTimeout(300)
    await page.getByRole('switch', { name: /use a key to start dictation/i }).first().click()
    await page.waitForTimeout(500)
    await shoot(page, 'ptt-save-error', 'voice-push-to-talk-save-refused.png')
    await context.close()
  }

  await browser.close()
  served.srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
