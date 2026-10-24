/**
 * Screenshot harness for the refused-filing notice (SessionNotFiledNotice).
 *
 * Runs the REAL built SPA (website/dist) behind `serveDist` with every /api/**
 * call answered by `stubDashboardApi`. The sidebar loads two folders, then the
 * page receives the event `createChatSlot` dispatches when a create answered a
 * different `folder_id` than it asked for, so the notice names the chat and
 * the folder from the sidebar's folder list.
 *
 * Usage: node scripts/capture-session-not-filed-notice.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/15592'
mkdirSync(OUT, { recursive: true })
const folders = [{ id: 'f1', name: 'Research', order: 0 }, { id: 'f2', name: 'Releases', order: 1 }]
const slots = [
  { key: 'chat-1', messages: 0, running: false, agent: 'kirocrew', mode: '', title: 'Weekly notes', folder_id: 'f2' },
  { key: 'chat-2', messages: 0, running: false, agent: 'kirocrew', mode: '', title: 'Quarterly plan', folder_id: '' },
]
const { srv, base } = await serveDist()
const browser = await chromium.launch()
for (const theme of ['dark', 'light']) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { slots, folders, theme, localStorageEntries: { 'mc-active-slot': 'chat-2', 'mc-lang': 'en' } })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  // The same event createChatSlot dispatches when the answered folder_id
  // differs from the one asked for (api/client/chat.ts).
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('kc-session-not-filed', { detail: { key: 'chat-2', title: 'Quarterly plan', folderId: 'f1' } })))
  const notice = page.getByTestId('session-not-filed-notice')
  await notice.waitFor({ state: 'visible', timeout: 5000 })
  console.log(theme, await notice.innerText())
  await notice.locator('xpath=..').screenshot({ path: join(OUT, `not-filed-notice-${theme}.png`) })
  if (theme === 'dark') await page.screenshot({ path: join(OUT, 'not-filed-notice-in-place.png') })
  await context.close()
}
// The fallback with the least context: a chat with no title, refused filing
// into a folder the sidebar no longer lists (`session_not_filed_untitled_folder_gone`).
{
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { slots, folders, theme: 'dark', localStorageEntries: { 'mc-active-slot': 'chat-2', 'mc-lang': 'en' } })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('kc-session-not-filed', { detail: { key: 'chat-3', title: '', folderId: 'f-gone' } })))
  const notice = page.getByTestId('session-not-filed-notice')
  await notice.waitFor({ state: 'visible', timeout: 5000 })
  console.log('untitled-folder-gone', await notice.innerText())
  await notice.locator('xpath=..').screenshot({ path: join(OUT, 'not-filed-notice-untitled-folder-gone-dark.png') })
  await context.close()
}
await browser.close()
srv.close()
