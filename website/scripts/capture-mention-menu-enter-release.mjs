/**
 * Screenshot harness + behavior check for the MENTION-MENU ENTER RELEASE
 * (#5029).
 *
 * The composer's @ file-mention menu used to swallow Enter whenever it had
 * zero results — a prompt mention like "@agent-sop:name" (which the Prompts
 * tab tells users to type) matches no file, so the message could not be sent
 * until a trailing space closed the menu. The fix releases Enter/Tab and
 * closes the menu when there is nothing to choose.
 *
 * This asserts as well as photographs, against the REAL built SPA
 * (website/dist): it types a prompt mention, verifies the zero-result menu is
 * open, presses Enter, and exits non-zero unless the menu closed AND the send
 * POST actually fired. Nothing in CI runs this file — the CI-enforced half of
 * the invariant is FilePickerMenu.cov80.test.tsx.
 *
 * Usage: node scripts/capture-mention-menu-enter-release.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mention-menu-enter-release'
const SLOT = 'chat-mention'
const PROJECT = '/home/user/workspace/notes'
const MESSAGE = 'Summarize the sprint with @agent-sop:release-notes'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Sprint wrap-up',
  running: false,
  last_message: 'Ready when you are.',
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
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'How do I invoke a saved prompt from chat?' },
    { role: 'assistant', ts: Date.now() / 1000 - 590, content: 'Type `@agent-sop:name` in the composer and send.' },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 950 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })

  let sendPosted = false

  /** Routes the shared stub does not know about (await json(); return true). */
  const extra = async (path, route) => {
    if (path === '/api/file-search') {
      // A prompt mention matches no file — the state the fix is about.
      await json(route, { results: [], root: PROJECT })
      return true
    }
    if (path === '/api/chat' && route.request().method() === 'POST') {
      sendPosted = true
      await json(route, { ok: true })
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)

  const composer = page.locator('textarea').first()
  await composer.click()
  // pressSequentially drives real keydown/input events so the @-trigger
  // detection and the mention-menu open path both run.
  await composer.pressSequentially(MESSAGE, { delay: 15 })

  // The zero-result menu must be open before Enter means anything.
  const menu = page.locator('[role="listbox"]')
  await menu.getByText(/No matching files/).waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/1-zero-match-menu-open.png` })
  console.log('wrote', `${OUT}/1-zero-match-menu-open.png`)

  await composer.press('Enter')
  await page.waitForTimeout(800)

  const menuStillOpen = await menu.count()
  const composerValue = await composer.inputValue()
  await page.screenshot({ path: `${OUT}/2-after-enter-message-sent.png` })
  console.log('wrote', `${OUT}/2-after-enter-message-sent.png`)

  console.log({ sendPosted, menuStillOpen, composerCleared: composerValue === '' })
  await browser.close()
  srv.close()

  if (!sendPosted || menuStillOpen > 0) {
    console.error('FAIL: Enter did not pass through the zero-result mention menu')
    process.exit(1)
  }
  console.log('PASS: zero-result mention menu released Enter; message sent')
}

main().catch(err => { console.error(err); process.exit(1) })
