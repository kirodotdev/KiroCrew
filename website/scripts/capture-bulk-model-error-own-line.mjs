/**
 * Screenshot harness for the Switch All Sessions failure notice (#10814).
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call from fixtures through the shared
 * `stubDashboardApi` helper. No gateway, no dashboard auth, no kiro-cli.
 *
 * The scene-specific stub is one route, `POST /api/chat/slots/model`, answered
 * two ways the endpoint really answers today (`api_chat_slots_model` in
 * `src/kiro_crew/dashboard/chat_handlers.py`):
 *   1. `200 {ok: true, failed: [...]}` — the routine partial outcome: some
 *      slots' resets raised, the panel stays open and reports the count.
 *   2. `400 {error: "..."}` — the model-validation refusal. The message mirrors
 *      `_model_rejected_reason`'s real sentence shape (with an illustrative
 *      model key), because its length is the point: the notice must wrap at
 *      word boundaries, not one character per line.
 *
 * The sidebar is pinned to a narrow stored width (`mc-sidebar-width`), the
 * geometry the defect reproduces at. Each scenario captures the Switch All
 * panel element and the full page.
 *
 * Usage: node scripts/capture-bulk-model-error-own-line.mjs [outDir] [prefix]
 *   prefix "before" / "after" (default "after") names the frames.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/bulk-model-error-shots'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

/** Sidebar stored width for the capture — the narrow-sidebar geometry. */
const SIDEBAR_WIDTH = 280

const SLOTS = [
  { key: 'chat-1', title: 'Refactor the auth middleware', messages: 12, running: false, agent: 'kirocrew', mode: '' },
  { key: 'chat-2', title: 'Weekly report draft', messages: 4, running: false, agent: 'kirocrew', mode: '' },
  { key: 'chat-3', title: 'Debug the flaky e2e suite', messages: 27, running: true, agent: 'kirocrew', mode: '' },
]

// Sentence shape of `_model_rejected_reason` — long enough that character-wise
// wrapping is unmistakable in the frame. The key is illustrative.
const LONG_REFUSAL =
  "'nova-2-pro' is a display-only model identifier the acp provider does not " +
  "accept; select a listed model or 'auto'."

/** Mutated between scenarios; the `extra` closure reads it per request. */
let scenario = 'partial'

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
const page = await context.newPage()

const extra = async (path, route) => {
  if (path === '/api/chat/slots/model' && route.request().method() === 'POST') {
    if (scenario === 'partial') {
      await json(route, { ok: true, model: 'auto', switched: [], skipped_running: ['chat-3'], unchanged: [], failed: ['chat-1', 'chat-2'] })
    } else {
      await json(route, { error: LONG_REFUSAL }, 400)
    }
    return true
  }
  return false
}

await stubDashboardApi(page, {
  slots: SLOTS,
  extra,
  localStorageEntries: {
    'mc-active-slot': 'chat-1',
    'mc-lang': 'en',
    'mc-sidebar-width': String(SIDEBAR_WIDTH),
  },
})

await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)

/** The Switch All panel root — the animate-rise card that holds the title. */
const panel = () => page.locator('div.animate-rise').filter({ hasText: 'Switch All Sessions' }).first()

async function openPanelAndFail(name, { closeAfter = true } = {}) {
  // Header ⋮ menu → Switch all to model…
  await page.getByLabel('More options').first().click()
  await page.getByRole('menuitem', { name: /Switch all to model/ }).click()
  await panel().waitFor({ state: 'visible', timeout: 5000 })
  // Pick a model, then submit; the stub rejects it per the active scenario.
  await panel().getByRole('option', { name: /auto/i }).first().click()
  await panel().getByRole('button', { name: /^Switch \d+ session/ }).click()
  await page.getByTestId('bulk-model-error').waitFor({ state: 'visible', timeout: 5000 })
  // Settle the notice's own rise animation before measuring pixels.
  await page.waitForTimeout(400)
  // Probe the geometry the frames exist to prove, so a green run means the
  // layout claim holds (in-repo capture-*.mjs practice: assert, don't trust).
  // Fixed layout: the block notice spans most of the panel's content width.
  // Defective layout: the inline notice is squeezed to a sliver of it.
  const noticeBox = await page.getByTestId('bulk-model-error').boundingBox()
  const panelBox = await panel().boundingBox()
  if (!noticeBox || !panelBox) throw new Error('notice or panel has no bounding box')
  const widthRatio = noticeBox.width / panelBox.width
  if (PREFIX === 'after' && widthRatio < 0.7) {
    throw new Error(`after-frame: notice spans ${(widthRatio * 100).toFixed(0)}% of the panel — still squeezed into the button row`)
  }
  if (PREFIX === 'before' && widthRatio >= 0.7) {
    throw new Error(`before-frame: notice spans ${(widthRatio * 100).toFixed(0)}% of the panel — defect did not reproduce`)
  }
  const panelOut = join(OUT, `${PREFIX}-${name}-panel.png`)
  await panel().screenshot({ path: panelOut })
  console.log('wrote', panelOut)
  const pageOut = join(OUT, `${PREFIX}-${name}-page.png`)
  await page.screenshot({ path: pageOut })
  console.log('wrote', pageOut)
  // Close via the panel's own Cancel so the next scenario starts fresh; the
  // page-wide lookup can match a control under the chat pane's swipe overlay.
  // The last scenario skips this: on the defective layout the ultra-tall
  // notice can push Cancel out of reach, and there is nothing left to reset.
  if (!closeAfter) return
  await panel().getByRole('button', { name: 'Cancel' }).click()
  await panel().waitFor({ state: 'hidden', timeout: 5000 })
}

// Scenario 1: the routine partial outcome — failure notice alone.
await openPanelAndFail('01-partial-failure')

// Scenario 2: a long validation refusal — word-boundary wrapping is visible.
scenario = 'refusal'
await openPanelAndFail('02-long-refusal', { closeAfter: false })

await browser.close()
srv.close()
