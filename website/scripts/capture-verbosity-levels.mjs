/**
 * Screenshot harness for the Response Verbosity row after adding `answer_only`.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli.
 *
 * `GET /api/dashboard/config` is overridden here rather than left to the shared
 * fixture because that fixture omits `verbosity` entirely: the row would render
 * through `asVerbosity`'s fallback, so every frame would photograph `default` and
 * the shot could not tell a working select from a broken one. The handler keeps the
 * value in a local and lets `PUT` mutate it, so the fourth frame shows what the
 * dashboard reads back after the write — the select is only proven if the value it
 * re-reads is the one just chosen, not the one it optimistically rendered.
 *
 * Usage: node scripts/capture-verbosity-levels.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/verbosity-levels-shots'
mkdirSync(OUT, { recursive: true })

const EXPECTED_OPTIONS = [
  'Default — normal length',
  'Concise — trim filler and narration',
  'Ultra-concise — answer first, minimal prose',
  'Answer only — details on request or when critical',
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 1 })
const page = await context.newPage()
logPageProblems(page)

let verbosity = 'default'
const extra = async (path, route) => {
  if (path === '/api/dashboard/config') {
    if (route.request().method() === 'PUT') {
      const body = JSON.parse(route.request().postData() || '{}')
      if (body.verbosity) verbosity = body.verbosity
      await json(route, { ok: true })
      return true
    }
    await json(route, {
      restore_sessions: false,
      restore_window_minutes: 30,
      merge_queued_messages: false,
      widget_density: 'more',
      use_builtin_browser: true,
      verbosity,
      quick_send: false,
      session_grid: false,
      tail_fork_enabled: false,
      link_previews: false,
      mcp_app_panel: false,
      auto_open_git_panel: false,
      folder_suggestions_enabled: true,
    })
    return true
  }
  return false
}

// Pin the locale: without it the SPA negotiates one from the environment and the
// shot comes out in whatever language the runner happens to get.
await stubDashboardApi(page, { extra, localStorageEntries: { 'mc-lang': 'en' } })

/**
 * Frame the setting's row from the union of the label's and the control's own
 * boxes, padded. A `div:has-text(...)` wrapper is not usable here: the deepest
 * match is an inner leaf and produced 2 KB near-empty crops, while the outermost
 * is the whole scroll panel.
 */
const shootRow = async (name) => {
  const label = page.getByText('Response Verbosity', { exact: true }).first()
  const control = page.getByRole('combobox', { name: 'Response Verbosity' })
  const a = await label.boundingBox()
  const b = await control.boundingBox()
  const pad = 18
  const x = Math.max(0, Math.min(a.x, b.x) - pad)
  const y = Math.max(0, Math.min(a.y, b.y) - pad)
  const clip = {
    x,
    y,
    width: Math.max(a.x + a.width, b.x + b.width) - x + pad,
    height: Math.max(a.y + a.height, b.y + b.height) - y + pad,
  }
  const out = join(OUT, name)
  await page.screenshot({ path: out, clip })
  console.log('wrote', out, `${Math.round(clip.width)}x${Math.round(clip.height)}`)
}

async function openChatSettings() {
  await page.goto(base + '/settings?tab=chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  const label = page.getByText('Response Verbosity', { exact: true }).first()
  await label.waitFor({ state: 'visible', timeout: 20000 })
  await label.scrollIntoViewIfNeeded()
  await page.waitForTimeout(400)
  return page.getByRole('combobox', { name: 'Response Verbosity' })
}

// 1 — the row as it ships, on the untouched default.
let trigger = await openChatSettings()
await shootRow('01-verbosity-row-default.png')

// 2 — every level the enum accepts, each with its own label.
await trigger.click()
await page.waitForTimeout(700)
const seen = (await page.getByRole('option').allTextContents()).map(s => s.trim())
console.log('options:', JSON.stringify(seen))
if (seen.length !== EXPECTED_OPTIONS.length) {
  throw new Error(`expected ${EXPECTED_OPTIONS.length} options, saw ${seen.length}: ${JSON.stringify(seen)}`)
}
for (const want of EXPECTED_OPTIONS) {
  if (!seen.some(s => s.includes(want))) throw new Error(`missing option: ${want}`)
}
await page.screenshot({ path: join(OUT, '02-verbosity-options-open.png') })
console.log('wrote', join(OUT, '02-verbosity-options-open.png'))

// 3 — the new level selected.
await page.getByRole('option', { name: 'Answer only — details on request or when critical' }).click()
await page.waitForTimeout(1000)
if (!(await trigger.textContent()).includes('Answer only')) {
  throw new Error('trigger did not adopt the selected level')
}
await shootRow('03-answer-only-selected.png')

// 4 — what the dashboard reads back from the server after the write.
trigger = await openChatSettings()
const after = (await trigger.textContent()).trim()
console.log('re-read value:', after)
if (!after.includes('Answer only')) throw new Error(`did not round-trip, saw: ${after}`)
await shootRow('04-answer-only-after-reload.png')

console.log('OK: 4 labelled levels, answer_only selectable and round-trips through the API')
await context.close()
await browser.close()
srv.close()
