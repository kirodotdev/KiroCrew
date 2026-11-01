/**
 * Screenshot harness for #24331: in an enterprise build the computer-use enable is
 * locked by policy (`GET /api/computer-use/config` carries `locked_by_policy`), so
 * Settings → Computer Use greys the switch out and shows a "Locked by policy" badge
 * with the reason, instead of a live switch whose save fails.
 *
 * Frames (full app, no clip), named to match temp-screenshots/24331/:
 *   01-computer-use-locked-dark.png   — locked: switch greyed out, badge + reason.
 *   02-computer-use-locked-light.png  — the same in the light theme.
 *   03-computer-use-unlocked-dark.png — oss (no lock): the panel as before.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static server
 * with every /api/** call answered from fixtures: no gateway.
 *
 * Usage: npm run build && node scripts/capture-24331-locked-by-policy.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/24331'
mkdirSync(OUT, { recursive: true })

const config = (locked) => ({
  enabled: false, locked_by_policy: locked, supported: true, platform: 'macos', reason: '',
  max_tree_nodes: 1200, max_tree_depth: 64, text_limit: 500, attach_screenshot: true,
  screenshot_max_px: 1280, screenshot_jpeg_quality: 55, cursor_motion: false,
  cursor_motion_supported: true, allowed_apps: [], extra_denied_apps: [],
  permissions: { accessibility: 'granted', screen_recording: 'granted', responsible_hint: '' },
  limits: { max_tree_nodes: [1, 5000], screenshot_max_px: [320, 4096] },
})

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const extraFor = (locked) => async (path, route) => {
  if (path === '/api/computer-use/config') {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(config(locked)) })
    return true
  }
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })

const frame = async ({ locked, theme, file }) => {
  const page = await context.newPage()
  await stubDashboardApi(page, { extra: extraFor(locked), ...(theme ? { theme } : {}) })
  logPageProblems(page)
  await page.goto(`${base}/settings?tab=computer-use`, { waitUntil: 'domcontentloaded' })
  const toggle = page.getByRole('switch', { name: /enable computer use/i })
  await toggle.waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)
  const badge = page.getByTestId('locked-by-policy-badge')
  const disabled = (await toggle.getAttribute('aria-disabled')) === 'true'
  if (locked && (!(await badge.count()) || !disabled)) fail(`${file}: expected the badge and a greyed-out switch`)
  if (!locked && ((await badge.count()) || disabled)) fail(`${file}: expected no badge and a live switch`)
  await page.screenshot({ path: `${OUT}/${file}` })
}

await frame({ locked: true, file: '01-computer-use-locked-dark.png' })
await frame({ locked: true, theme: 'light', file: '02-computer-use-locked-light.png' })
await frame({ locked: false, file: '03-computer-use-unlocked-dark.png' })

await browser.close()
srv.close()
if (failures) { console.error(`${failures} check(s) failed`); process.exit(1) }
console.log(`OK: wrote frames to ${OUT}`)
