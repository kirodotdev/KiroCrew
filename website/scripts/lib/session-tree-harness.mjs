/**
 * Shared boot for the conductor-lane (session tree) capture harnesses: one
 * Playwright page over the stubbed dashboard API, a pass/fail tally, the row
 * readers the lane exposes as data attributes, and the theme settle.
 *
 * Extracted from capture-session-tree-member-anchor.mjs so the peer-lineage
 * harness could share it instead of cloning it (jscpd runs at a 0% duplication
 * threshold). Each harness still owns its fixtures, frames and checks.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './chromium-executable.mjs'
import { stubDashboardApi, logPageProblems } from './stub-dashboard-api.mjs'

/**
 * @param {string} outDir screenshot directory, created if missing
 * @returns {Promise<{
 *   page: import('playwright').Page,
 *   browser: import('playwright').Browser,
 *   check: (label: string, ok: boolean, detail?: string) => void,
 *   failed: () => boolean,
 *   rows: () => Promise<Array<{ key: string|null, depth: string|null, anchor: string|null }>>,
 *   keys: () => Promise<string>,
 *   rowOf: (key: string) => Promise<{ key: string|null, depth: string|null, anchor: string|null } | undefined>,
 *   settleTheme: () => Promise<string|null>,
 *   shot: (name: string) => Promise<Buffer>,
 *   finish: () => Promise<never>,
 * }>}
 */
export async function openSessionTreeHarness(outDir) {
  mkdirSync(outDir, { recursive: true })

  let failed = false
  const check = (label, ok, detail) => {
    console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`)
    if (!ok) failed = true
  }

  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  const context = await browser.newContext({ viewport: { width: 620, height: 640 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  page.on('pageerror', e => { console.log(`FAIL pageerror — ${e.message}`); failed = true })
  await stubDashboardApi(page, {
    theme: 'dark',
    folders: [],
    extra: async (path, route) => {
      if (path.startsWith('/api/')) return false
      await route.continue()
      return true
    },
  })
  logPageProblems(page)

  const rows = () => page.$$eval('[data-slot-key]', els => els.map(el => ({
    key: el.getAttribute('data-slot-key'),
    depth: el.closest('[data-conductor-depth]')?.getAttribute('data-conductor-depth') ?? null,
    anchor: el.closest('[data-conductor-depth]')?.getAttribute('data-conductor-anchor') ?? null,
  })))
  const keys = async () => (await rows()).map(r => r.key).join(' ')
  const rowOf = async key => (await rows()).find(r => r.key === key)

  async function settleTheme() {
    let prev = null
    for (let i = 0; i < 20; i++) {
      const now = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
      if (now && now === prev) return now
      prev = now
      await page.waitForTimeout(250)
    }
    return prev
  }

  const shot = name => page.locator('.sidebar-inner').screenshot({ path: `${outDir}/${name}.png` })

  const finish = async () => {
    await browser.close()
    console.log(failed ? 'RESULT: FAIL' : 'RESULT: ok')
    process.exit(failed ? 1 : 0)
  }

  return { page, browser, check, failed: () => failed, rows, keys, rowOf, settleTheme, shot, finish }
}
