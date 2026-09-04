/**
 * Screenshot harness for the Sound card's "Test notification" button in
 * Settings → Notifications.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with
 * every /api/** call answered from fixtures — no gateway, no token, no agent.
 * Same technique as capture-dictation-panel.mjs.
 *
 * `/api/notifications/channels` answers an empty set so ChannelsSection
 * renders nothing and the capture frames the two cards this change touches:
 * the Sound card (where the button lands) and the per-category card whose
 * sibling Test buttons the new button mirrors.
 *
 * Captured at 1280px (desktop) and 390px (phone) per the narrow-viewport
 * evidence rule.
 *
 * Usage: node scripts/capture-test-notification.mjs [outDir] [prefix]
 *   prefix: file-name prefix, e.g. "before" / "after"
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/test-notification-button'
const PREFIX = process.argv[3] || 'shot'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()
const browser = await chromium.launch()

const fixedApi = makeFixedApi(PROJECT)

async function capture(viewport, name) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/notifications/channels') return json(route, { channels: [] })
    return handleBootRoute(route, path, { project: PROJECT, fixedApi })
  })
  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
  })
  await page.goto(`${base}/settings?tab=notifications`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('switch', { name: /play sound on new notifications/i }).waitFor({ timeout: 20000 })
  await page.waitForTimeout(800) // let the card entrance stagger settle
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false })
  await context.close()
  return errors
}

const allErrors = []
allErrors.push(...await capture({ width: 1280, height: 900 }, `${PREFIX}-desktop-1280`))
allErrors.push(...await capture({ width: 390, height: 844 }, `${PREFIX}-mobile-390`))

await browser.close()
srv.close()

console.log(JSON.stringify({ out: OUT, prefix: PREFIX, errors: allErrors }, null, 2))
if (allErrors.length) process.exitCode = 1
