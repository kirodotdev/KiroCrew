// Screenshot harness for the `capabilities.social_share` governance gate:
// the same assistant reply's "More actions" menu with the ceiling permitting
// Share (default) and with a policy pinning it off, plus the mid-compose case
// where the policy flips while the share dialog is open. The dashboard learns
// the answer from GET /api/dashboard/config (`social_share_enabled`), so the
// captures differ only in that one stubbed field.
//
// Uses the fixture harness (built dist + stubbed API); no gateway, no token.
// Usage: node scripts/capture-social-share-governance.mjs [outDir]
import { mkdirSync, readFileSync } from 'node:fs'
import { chromium } from 'playwright'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/social-share-governance'
const SLOT = 'chat-share-gov'
const PROJECT = '/home/user/workspace/service'

mkdirSync(OUT, { recursive: true })

const ANSWER = [
  '## Overnight batch done',
  'Scanned **47 new issues**, triaged 3 as auto-fixable and dispatched all of them in parallel worktrees.',
  '- #4881 opened PR #7202 — CI all green, awaiting review',
  '- #5104 opened PR #7205 — Windows shard still running',
  'Nobody was at the keyboard for any of it.',
].join('\n\n')

const slots = [{
  key: SLOT,
  title: 'Nightly issue triage',
  running: false,
  last_message: ANSWER,
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'How did the overnight run go?' },
    { role: 'assistant', ts: Date.now() / 1000 - 580, content: ANSWER },
  ],
}

// Mirrors the shared stub's config payload; only the governance answer varies.
const dashboardConfig = (socialShareEnabled) => ({
  restore_sessions: false, restore_window_minutes: 30,
  merge_queued_messages: false, widget_density: 'more',
  social_share_enabled: socialShareEnabled,
})

async function captureMenu(context, base, { socialShareEnabled, file }) {
  const extra = async (path, route) => {
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    if (path === '/api/dashboard/config') { await json(route, dashboardConfig(socialShareEnabled)); return true }
    return false
  }
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)

  await page.locator('.msg-content').last().hover()
  await page.waitForTimeout(400)
  const moreBtn = page.getByTestId('assistant-more-actions')
  let shareVisible
  if (socialShareEnabled) {
    await moreBtn.waitFor({ timeout: 10000 })
    await moreBtn.click()
    await page.getByTestId('share-message').waitFor({ timeout: 5000 })
    await page.waitForTimeout(400)
    shareVisible = await page.getByTestId('share-message').count()
  } else {
    // A loaded window keeps fork/plan as row buttons, so Share was the menu's
    // only item — pinned off, the trigger is withdrawn with it rather than
    // opening an empty menu. The hovered row is the evidence.
    await page.getByTitle('Fork conversation from here').waitFor({ timeout: 10000 })
    shareVisible = (await moreBtn.count()) + (await page.getByTestId('share-message').count())
  }
  await page.screenshot({ path: `${OUT}/${file}` })
  console.log('wrote', `${OUT}/${file}`, { socialShareEnabled, shareVisible })
  await page.close()
  return shareVisible
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 950 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })

  const permitted = await captureMenu(context, base, { socialShareEnabled: true, file: '1-menu-permitted.png' })
  const pinned = await captureMenu(context, base, { socialShareEnabled: false, file: '2-menu-pinned-off.png' })
  const withdrawn = await captureWithdrawnMidCompose(context, base)

  await browser.close()
  srv.close()

  if (permitted !== 1 || pinned !== 0 || !withdrawn) {
    console.error('FAIL: expected Share present when permitted, absent when pinned, and a notice on mid-compose withdrawal', { permitted, pinned, withdrawn })
    process.exit(1)
  }
}

// The policy flips WHILE the share dialog is open: the dialog must stay, with
// the user's edits in it, and say why the actions are gone.
async function captureWithdrawnMidCompose(context, base) {
  let socialShareEnabled = true
  const extra = async (path, route) => {
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    if (path === '/api/dashboard/config') { await json(route, dashboardConfig(socialShareEnabled)); return true }
    if (path === '/logo.png') {
      await route.fulfill({
        status: 200,
        contentType: 'image/png',
        body: readFileSync(new URL('../../src/kiro_crew/static/kirocrew-logo.png', import.meta.url)),
      })
      return true
    }
    return false
  }
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)

  await page.locator('.msg-content').last().hover()
  await page.getByTestId('assistant-more-actions').click()
  await page.getByTestId('share-message').click()
  await page.getByTestId('share-card').waitFor({ timeout: 10000 })
  await page.locator('#share-caption').fill('Edited caption the user typed before the policy changed')

  // The fleet withdraws sharing. The stubbed socket carries no generation
  // frame, so exercise the other refetch path: the query goes stale after
  // 30s and a visibility change refetches it (react-query's focusManager
  // listens to `visibilitychange`, not `focus`).
  socialShareEnabled = false
  await page.waitForTimeout(31_000)
  await page.evaluate(() => window.dispatchEvent(new Event('visibilitychange')))
  const notice = page.getByTestId('share-withdrawn')
  await notice.waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  const stillOpen = (await page.getByTestId('share-card').count()) === 1
  const editKept = (await page.locator('#share-caption').inputValue()).startsWith('Edited caption')
  const disabled = await page.getByTestId('share-x').isDisabled()
  await page.screenshot({ path: `${OUT}/3-dialog-withdrawn-mid-compose.png` })
  console.log('wrote', `${OUT}/3-dialog-withdrawn-mid-compose.png`, { stillOpen, editKept, disabled })
  await page.close()
  return stillOpen && editKept && disabled
}

main().catch(err => { console.error(err); process.exit(1) })
