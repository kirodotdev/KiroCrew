/**
 * Screenshot harness for the Push notifications card's copy pass (#23984):
 * Settings → Notifications → Desktop alerts.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with
 * every /api/** call answered from fixtures — no gateway, no token, no agent.
 * Same technique as capture-test-notification.mjs.
 *
 * The three states the issue asked to see are driven through the browser APIs
 * the hook reads, never through component props:
 *   - default:  supported, not subscribed, permission granted
 *   - busy-on:  click the toggle; `PushManager.subscribe` never settles, so the
 *               hook's `busy` stays true with `subscribed` false → "Turning on…"
 *   - busy-off: an existing subscription whose `unsubscribe()` never settles →
 *               "Turning off…"
 *   - blocked:  `Notification.permission === 'denied'`, so the System
 *               notifications row above shows the full sentence and the push
 *               card only points back to it
 *
 * `navigator.serviceWorker` is replaced with a stub registration in an init
 * script so no real worker (and no real push service) is needed.
 *
 * Usage: node scripts/capture-23984-push-settings-copy.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/push-settings-copy-23984'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const fixedApi = makeFixedApi(PROJECT)

/** @param {{ permission: 'granted'|'denied', subscribed: boolean }} state */
function pushStubs({ permission, subscribed }) {
  return `
    (() => {
      const never = () => new Promise(() => {})
      const sub = ${subscribed} ? {
        endpoint: 'https://push.example/stub',
        options: {},
        toJSON: () => ({ endpoint: 'https://push.example/stub', keys: {} }),
        unsubscribe: never,
      } : null
      const reg = { pushManager: { getSubscription: async () => sub, subscribe: never } }
      Object.defineProperty(navigator, 'serviceWorker', {
        configurable: true,
        value: {
          ready: Promise.resolve(reg),
          controller: null,
          register: async () => reg,
          getRegistration: async () => reg,
          getRegistrations: async () => [reg],
          addEventListener: () => {},
          removeEventListener: () => {},
        },
      })
      Object.defineProperty(Notification, 'permission', { configurable: true, get: () => '${permission}' })
      Notification.requestPermission = async () => '${permission}'
    })()
  `
}

async function capture(name, state, { click = false, theme = 'dark' } = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/notifications/channels') return json(route, { channels: [] })
    if (path === '/api/notifications/push/vapid-public-key') return json(route, { publicKey: 'BAAA' })
    return handleBootRoute(route, path, { project: PROJECT, theme, fixedApi })
  })
  await page.addInitScript(theme => {
    localStorage.clear()
    localStorage.setItem('mc-theme', theme)
    localStorage.setItem('mc-onboarded', '1')
  }, theme)
  await page.addInitScript(pushStubs(state))
  await page.goto(`${base}/settings/notifications/alerts`, { waitUntil: 'domcontentloaded' })
  const toggle = page.getByRole('switch', { name: /notify me even when .* is closed/i })
  await toggle.waitFor({ timeout: 20000 })
  if (click) {
    await toggle.click()
    await page.getByTestId('push-busy-status').waitFor({ timeout: 10000 })
  }
  await page.waitForTimeout(800) // let the card entrance stagger settle
  // Frame the Desktop alerts page body: the System notifications row and the
  // Push notifications card are what the copy refers to.
  const heading = await page.getByRole('heading', { name: 'Desktop alerts' }).first().boundingBox()
  const pushHeading = await page.getByRole('heading', { name: 'Push notifications' }).first().boundingBox()
  const toggleBox = await toggle.boundingBox()
  const tails = await Promise.all(
    ['push-busy-status', 'push-blocked-hint'].map(id => page.getByTestId(id).boundingBox().catch(() => null)),
  )
  const bottoms = [toggleBox, ...tails].filter(Boolean).map(b => b.y + b.height)
  const top = Math.max(0, (heading?.y ?? 0) - 10)
  const bottom = Math.max(...bottoms) + 40
  const x = Math.max(0, (pushHeading?.x ?? heading?.x ?? 0) - 24)
  const width = Math.min(1280 - x, 760)
  await page.screenshot({ path: `${OUT}/${name}.png`, clip: { x, y: top, width, height: bottom - top } })
  await context.close()
  return errors
}

const allErrors = []
allErrors.push(...await capture('default-dark', { permission: 'granted', subscribed: false }))
allErrors.push(...await capture('default-light', { permission: 'granted', subscribed: false }, { theme: 'light' }))
allErrors.push(...await capture('busy-turning-on-dark', { permission: 'granted', subscribed: false }, { click: true }))
allErrors.push(...await capture('busy-turning-off-dark', { permission: 'granted', subscribed: true }, { click: true }))
allErrors.push(...await capture('blocked-dark', { permission: 'denied', subscribed: false }))
allErrors.push(...await capture('blocked-light', { permission: 'denied', subscribed: false }, { theme: 'light' }))

await browser.close()
srv.close()

console.log(JSON.stringify({ out: OUT, errors: allErrors }, null, 2))
if (allErrors.length) process.exitCode = 1
