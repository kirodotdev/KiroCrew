/**
 * Shared page scaffolding for the transcript screenshot harnesses.
 *
 * Every harness that photographs a chat transcript needs the same five things
 * before its own scene matters: a static server over the built SPA, a browser
 * page at 2x (the cards are 11-13px type, and a 1x shot renders them soft), the
 * `/api/ws` route bound so the app's socket does not hang, `/api/**` answered
 * from `slots` + `detail` fixtures with everything else falling through to
 * `handleBootRoute`, and a cold `load(theme)` that boots the SPA with a chat slot
 * already selected.
 *
 * Kept here rather than copied per harness so a harness file holds only its
 * scene — the fixture transcript and the shots it takes.
 */
import { chromium } from 'playwright'
import { serveDist } from './serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './boot-api.mjs'

/**
 * Open a transcript harness.
 *
 * @param {object} opts
 * @param {string} opts.slot            chat slot key the fixtures describe
 * @param {string} opts.project         project dir the boot fixtures report
 * @param {unknown} opts.slots          body for `/api/chat/slots`
 * @param {unknown} opts.detail         body for `/api/chat/slots/<key>`
 * @param {object} [opts.viewport]      browser viewport
 * @param {number} [opts.deviceScaleFactor]
 * @returns {Promise<{browser: import('playwright').Browser, page: import('playwright').Page, base: string, load: (theme?: string, waitFor?: {selector?: string, settle?: number}) => Promise<void>, close: () => Promise<void>}>}
 */
export async function openTranscriptHarness({
  slot,
  project,
  slots,
  detail,
  viewport = { width: 1280, height: 900 },
  deviceScaleFactor = 2,
}) {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport, deviceScaleFactor })
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})

  const fixedApi = makeFixedApi(project)
  // Mutable so `load` can switch theme between shots without re-registering the
  // route — the boot fixtures read the theme at request time.
  const scene = { theme: 'dark' }

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    return handleBootRoute(route, path, { project, theme: scene.theme, fixedApi })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })

  /** Cold-load the SPA on `theme` with `slot` pre-selected. */
  async function load(theme = 'dark', waitFor = {}) {
    scene.theme = theme
    await page.addInitScript(([t, s]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot-chat', s)
    }, [theme, slot])
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    if (waitFor.selector) await page.waitForSelector(waitFor.selector, { timeout: 20000 })
    await page.waitForTimeout(waitFor.settle ?? 600)
  }

  async function close() {
    await browser.close()
    srv.close()
  }

  return { browser, page, base, load, close }
}
