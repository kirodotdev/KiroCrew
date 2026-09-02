/**
 * Shared frame-shooter for governance-viewer screenshot harnesses.
 *
 * Owns the boilerplate every settings-page capture repeats — context sizing,
 * fixture install, the Electron-bridge stub, path-routed navigation, settle,
 * shoot — so a harness only supplies its API fixtures and an output path.
 * Extracted from capture-profile-fallback-banner.mjs when a second governance
 * harness (capture-security-unknown-scopes.mjs) would otherwise have cloned it.
 */
import { installApiFixtures, logPageFailures } from './api-fixtures.mjs'

/**
 * Open one browser context, render the settings page with the given API
 * fixtures, and screenshot it to `outPath`. Closes the context before
 * returning.
 *
 * @param {import('playwright').Browser} browser  launched Chromium
 * @param {string} base       origin of the serve-dist server
 * @param {string} outPath    PNG destination
 * @param {Record<string, unknown>} fixtures  route → JSON payload map for installApiFixtures
 * @param {string} [url]      page path; defaults to the governance section
 */
export async function shootSettingsFrame(browser, base, outPath, fixtures, url = '/settings?tab=security&section=governance') {
  const context = await browser.newContext({
    viewport: { width: 1500, height: 980 },
    // Settings rows are 12-13px type; a 1x shot renders soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  await installApiFixtures(page, fixtures)
  logPageFailures(page)
  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    // The app shell reads the Electron updater bridge during boot and does not
    // tolerate its absence in a plain browser — without this stub every
    // settings tab dies in the shell's error boundary before the panel renders.
    window.updateAPI = {
      onState: () => () => {},
      check: async () => ({ ok: true }),
      download: async () => ({ ok: true }),
      install: async () => ({ ok: true }),
      getInfo: async () => ({
        version: '0.5.0', channel: 'stable', stampedChannel: 'stable',
        channelSwitchable: true, channelPreference: '',
        platform: 'darwin-arm64', packaged: true,
      }),
      setChannel: async () => ({ ok: true }),
    }
  })
  // Path-routed, NOT hash-routed: serve-dist has an index.html fallback, and a
  // '#/settings' URL leaves location.pathname at '/' so the shell error-boundaries.
  await page.goto(`${base}${url}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1800)
  await page.screenshot({ path: outPath })
  await context.close()
}
