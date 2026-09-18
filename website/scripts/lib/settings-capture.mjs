/**
 * Shared boot preamble for the Settings-page screenshot harnesses.
 *
 * Every settings-row capture needs the identical gateway-free setup: serve the
 * built SPA from loopback, launch a 2x-scale Chromium (settings rows are
 * 12-13px type and render soft at 1x on GitHub), answer every /api/** call from
 * fixtures, seed the onboarding localStorage flags, and land on the requested
 * settings tab. `jscpd` runs at a 0% threshold, so a second copy of this block
 * is a gate failure, not a style note -- it lives here once and both harnesses
 * call it. Add a consumer by importing `openSettingsPage`, not by pasting the
 * block.
 *
 * Returns the live handles so the caller drives the row and tears down:
 * `{ browser, context, page, srv, base }`.
 */
import { chromium } from 'playwright'
import { json, makeFixedApi, handleBootRoute } from './boot-api.mjs'
import { serveDist } from './serve-dist.mjs'

/**
 * @param {object}  opts
 * @param {string} [opts.tab='chat']       Settings tab query param.
 * @param {string} [opts.project='/home/user/project'] Fixture project path.
 * @param {number} [opts.width=1400]        Viewport width.
 * @param {number} [opts.height=900]        Viewport height.
 */
export async function openSettingsPage({ tab = 'chat', project = '/home/user/project', width = 1400, height = 900 } = {}) {
  const fixedApi = makeFixedApi(project)

  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width, height },
    // Settings rows are 12-13px type; a 1x shot renders soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, [])
    return handleBootRoute(route, path, { project, fixedApi })
  })

  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-import-onboarded', '1')
    localStorage.setItem('mc-privacy-acked', '1')
  })

  await page.goto(`${base}/settings?tab=${tab}`, { waitUntil: 'domcontentloaded' })

  return { browser, context, page, srv, base }
}
