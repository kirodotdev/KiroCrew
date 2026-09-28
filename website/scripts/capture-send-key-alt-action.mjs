/**
 * Screenshot harness for the send-key / busy alternate-action work (#4608 P2).
 *
 * Real built SPA on the repo's static server, every /api/** call answered by
 * the shared stub — no gateway, no dashboard token. Captures Settings → Chat's
 * Composer card (the send shortcut + the new "what Enter does while the agent
 * is working" default) and the busy split-button menu with its ⌘↩ hint, light
 * and dark. Run the same script on a base build for a before/after.
 *
 * Usage: DIST=<dist dir> OUT_DIR=<out dir> node scripts/capture-send-key-alt-action.mjs
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.env.OUT_DIR || '/tmp/send-key-shots'
mkdirSync(OUT, { recursive: true })

const SLOTS = [
  { key: 'chat-1-a', title: 'Send key — P2', running: true, orchestrating: true, messages: 4, agent: 'kirocrew', last_ts: new Date().toISOString() },
]

const { srv, base } = await serveDist(process.env.DIST || DEFAULT_DIST)
const browser = await chromium.launch({ executablePath: chromiumExecutable() })

async function open(theme) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 1100 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await stubDashboardApi(page, { slots: SLOTS, folders: [], theme, localStorageEntries: { 'mc-lang': 'en' } })
  return { ctx, page }
}

for (const theme of ['light', 'dark']) {
  // Settings → Chat, Composer card.
  {
    const { ctx, page } = await open(theme)
    await page.goto(`${base}/settings/chat`)
    const label = page.getByText('Send shortcut', { exact: true }).first()
    await label.waitFor({ timeout: 20000 })
    await label.evaluate(el => el.scrollIntoView({ block: 'start' }))
    await page.waitForTimeout(500)
    // The card that holds the send shortcut: closest bordered container.
    const card = label.locator('xpath=ancestor::div[contains(@class,"rounded")][1]')
    await card.screenshot({ path: `${OUT}/settings-composer-${theme}.png` })
    console.log(`settings-composer-${theme}.png`)
    await ctx.close()
  }
  // Same card with "⌘/Ctrl+Enter sends" selected: the description switches to the
  // no-chord variant (the flip chord is the send key in that mode).
  {
    const { ctx, page } = await open(theme)
    await page.addInitScript(() => { localStorage.setItem('mc-chat-config', JSON.stringify({ sendOnEnter: 'ctrl-enter' })) })
    await page.goto(`${base}/settings/chat`)
    const label = page.getByText('Send shortcut', { exact: true }).first()
    await label.waitFor({ timeout: 20000 })
    await label.evaluate(el => el.scrollIntoView({ block: 'start' }))
    await page.waitForTimeout(500)
    const card = label.locator('xpath=ancestor::div[contains(@class,"rounded")][1]')
    await card.screenshot({ path: `${OUT}/settings-composer-ctrl-enter-${theme}.png` })
    console.log(`settings-composer-ctrl-enter-${theme}.png`)
    await ctx.close()
  }
  // Busy split-button menu on a running session with a draft.
  {
    const { ctx, page } = await open(theme)
    await page.goto(`${base}/chat?sid=chat-1-a`)
    const ta = page.getByLabel('Message input')
    await ta.waitFor({ timeout: 20000 })
    await ta.fill('follow-up while the agent works')
    await page.waitForTimeout(400)
    const caret = page.getByTestId('busy-send-caret')
    if (await caret.count()) {
      await caret.click()
      await page.getByRole('menu').waitFor({ timeout: 5000 })
      await page.waitForTimeout(300)
      await page.screenshot({ path: `${OUT}/busy-split-menu-${theme}.png`, clip: { x: 640, y: 560, width: 640, height: 540 } })
      console.log(`busy-split-menu-${theme}.png`)
    } else {
      console.log(`busy split not rendered in ${theme} (slot not steerable under the stub) — skipped`)
    }
    await ctx.close()
  }
}

await browser.close()
srv.close()
console.log('DONE', OUT)
