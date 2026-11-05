/**
 * Screenshot harness for the owner's reveal of a removed credential.
 *
 * Runs the REAL built SPA (website/dist) and answers /api/** from fixtures.
 * One assistant reply carries a lock tag with a `reveal_id`; the reveal route
 * answers with a FAKE value built at runtime.
 *   01 coach + card closed      02 card open (Show)
 *   03 value shown (Copy, Hide) 04 value gone (410 after a restart)
 *   05 refused (403)            06 failed (network)
 *   07 an older reply whose value was never kept (no Show)
 * Each frame is shot dark and light.
 *
 * Usage: node scripts/capture-redacted-reveal.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/redacted-reveal'
const SLOT = 'chat-redacted-reveal'
const PROJECT = '/home/user/workspace/demo'
const FAKE = 'demo-value-not-a-real-secret-7Qm2xK'
const HANDLE = 'rv_' + 'D'.repeat(22)

mkdirSync(OUT, { recursive: true })
const now = () => Date.now() / 1000

const slots = [{
  key: SLOT, title: 'Create the Supabase secret', running: false, last_message: 'Done', messages: 2,
  agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now()),
  source_links: [], source_links_total: 0,
}]

const detail = {
  running: false, has_more: false, total: 4, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now() - 900, content: 'Create a service secret and store it in Supabase.' },
    {
      role: 'assistant', ts: now() - 60,
      content: 'Created the secret and stored it in Supabase:\n\n```env\nSERVICE_SECRET=[REDACTED: credential]\n```',
      meta: { redactions: [{ ordinal: 0, rule: 'credential_pattern', label: '', source: null, view_command: null, profile_command: null, reveal_id: HANDLE }] },
    },
    { role: 'user', ts: now() - 50, content: 'And the old one from last week?' },
    {
      role: 'assistant', ts: now() - 40,
      content: 'That reply was saved before reveal existed:\n\n```env\nOLD_SECRET=[REDACTED: credential]\n```',
      meta: { redactions: [{ ordinal: 0, rule: 'credential_pattern', label: '', source: null, view_command: null, profile_command: null }] },
    },
  ],
}

async function capture(browser, base, theme) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  let mode = 'ok'
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await stubDashboardApi(page, {
    slots, theme,
    extra: async (path, route) => {
      if (path === '/api/security/redacted-value') {
        if (mode === 'gone') json(route, { error: 'gone', code: 'reveal_unavailable' }, 410)
        else if (mode === 'denied') json(route, { error: 'dashboard owner required', code: 'dashboard_owner_required' }, 403)
        else if (mode === 'failed') json(route, { error: 'boom' }, 500)
        else json(route, { value: FAKE })
        return true
      }
      if (path.startsWith('/api/chat/slots/')) { json(route, detail); return true }
      if (path === '/api/recent-projects') { json(route, { dirs: [PROJECT] }); return true }
      if (path === '/api/chat/nav/resolve-links') { json(route, { summaries: [] }); return true }
      return false
    },
  })
  logPageProblems(page)
  await page.addInitScript((t, slot) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', t)
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', slot)
  }, theme, SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-testid="credential-tag"]', { timeout: 15000 })
  const coach = await page.locator('[data-testid="redaction-coach"]').textContent()
  if (coach?.includes('Nothing can show them')) throw new Error('stale dist: old coach text')
  const shot = async n => { await page.waitForTimeout(450); await page.screenshot({ path: `${OUT}/${n}-${theme}.png` }); console.log('wrote', `${OUT}/${n}-${theme}.png`) }
  await shot('01-coach')
  const tags = page.locator('[data-testid="credential-tag"]')
  await tags.first().click()
  await page.waitForSelector('[data-testid="reveal-show"]', { timeout: 5000 })
  await shot('02-card-show')
  await page.locator('[data-testid="reveal-show"]').click()
  await page.waitForSelector('[data-testid="reveal-value"]', { timeout: 5000 })
  if ((await page.locator('[data-testid="reveal-value"]').textContent()) !== FAKE) throw new Error('value not shown')
  await shot('03-value-shown')
  await page.locator('[data-testid="reveal-hide"]').click()
  mode = 'gone'
  await page.locator('[data-testid="reveal-show"]').click()
  await page.waitForSelector('[data-testid="reveal-gone"]', { timeout: 5000 })
  if (!(await page.locator('[data-testid="reveal-show"]').isDisabled())) throw new Error('Show stays live after 410')
  await shot('04-value-gone')
  for (const [m, id, n] of [['denied', 'reveal-denied', '05-refused'], ['failed', 'reveal-failed', '06-failed']]) {
    // Reopen the card for a fresh Show.
    await tags.first().click()
    await page.waitForSelector('[data-testid="credential-card"]', { state: 'detached', timeout: 5000 })
    await tags.first().click()
    mode = m
    await page.locator('[data-testid="reveal-show"]').click()
    await page.waitForSelector(`[data-testid="${id}"]`, { timeout: 5000 })
    await shot(n)
  }
  await tags.first().click()
  await page.waitForSelector('[data-testid="credential-card"]', { state: 'detached', timeout: 5000 })
  await tags.nth(1).click()
  await page.waitForSelector('[data-testid="reveal-unavailable"]', { timeout: 5000 })
  if (await page.locator('[data-testid="reveal-show"]').count() !== 0) throw new Error('older reply offers Show')
  await shot('07-not-kept')
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  await capture(browser, base, 'dark')
  await capture(browser, base, 'light')
  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
