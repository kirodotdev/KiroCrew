/**
 * Screenshot harness for issue #3845: the new-token label input on the
 * Webhooks page crushed to ~3 visible characters at 390px instead of letting
 * the `flex-wrap` row wrap.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server with
 * every /api/** call answered from fixtures — gateway-free.
 *
 * Frames (all at 390x844, iPhone-class width the issue names):
 *   before-390px.png  the defect reproduced in the same live layout by
 *                     removing the fix class from the rendered input — the
 *                     field collapses to a sliver next to the Generate button
 *   after-390px.png   the shipped state: `min-w-[210px]` floors the input, the
 *                     row wraps and the field stays readable
 *   after-desktop.png 1280px control frame — the floor changes nothing at
 *                     desktop widths (input still capped at max-w-[260px])
 *
 * Usage: node scripts/capture-webhooks-token-input-width.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/webhooks-token-input-width-3845'
mkdirSync(OUT, { recursive: true })

const NOW = Math.floor(Date.now() / 1000)

/** Same shape the WebhooksPage vitest fixtures use (`WebhooksView`, empty install). */
const VIEW = {
  enabled: false,
  switch_on: true,
  has_tokens: false,
  url: 'http://localhost:6776/api/hooks/agent',
  slots: { in_use: 0, max: 6 },
  limits: {
    session_key_prefix: 'hook:', message_max: 49999,
    timeout_default: 599, timeout_max: 3593, max_concurrent: 6,
    signature_window_seconds: 300,
  },
  tokens: [], contexts: [], runs: [],
  _now: NOW,
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

const LABEL_INPUT = 'input[aria-label="New token label"]'

async function openWebhooks(viewport) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, colorScheme: 'dark' })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: 'dark',
    extra: async (path, route) => {
      if (path === '/api/webhooks') return json(route, VIEW), true
      return false
    },
  })
  await page.addInitScript(() => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme-mode', 'dark')
  })
  await page.goto(`${base}/webhooks`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(LABEL_INPUT, { timeout: 15000 })
  await page.waitForTimeout(800)
  // The Tokens section lives in the detail pane; center it so the row AND the
  // (possibly wrapped) Generate button are both in frame.
  await page.locator(LABEL_INPUT).evaluate(el => el.scrollIntoView({ block: 'center' }))
  await page.waitForTimeout(300)
  return { context, page }
}

// ── after (the shipped state) at 390px ─────────────────────────────────────
{
  const { context, page } = await openWebhooks({ width: 390, height: 844 })
  const w = await page.locator(LABEL_INPUT).evaluate(el => el.getBoundingClientRect().width)
  console.log(`after-390px: input width = ${Math.round(w)}px`)
  await page.screenshot({ path: `${OUT}/after-390px.png` })

  // ── before (defect reproduced) in the same live layout ───────────────────
  // Restore main's exact class state on the rendered element: swap the fix
  // class for the shared <Input>'s base `min-w-0` (which twMerge had dropped
  // in favor of the floor). The flex algorithm then crushes the input again.
  await page.locator(LABEL_INPUT).evaluate(el => {
    el.classList.remove('min-w-[210px]')
    el.classList.add('min-w-0')
  })
  await page.waitForTimeout(300)
  const wBefore = await page.locator(LABEL_INPUT).evaluate(el => el.getBoundingClientRect().width)
  console.log(`before-390px: input width = ${Math.round(wBefore)}px`)
  await page.screenshot({ path: `${OUT}/before-390px.png` })
  await context.close()
}

// ── desktop control frame ───────────────────────────────────────────────────
{
  const { context, page } = await openWebhooks({ width: 1280, height: 900 })
  const w = await page.locator(LABEL_INPUT).evaluate(el => el.getBoundingClientRect().width)
  console.log(`after-desktop: input width = ${Math.round(w)}px`)
  await page.screenshot({ path: `${OUT}/after-desktop.png` })
  await context.close()
}

await browser.close()
srv.close()
console.log('done')
