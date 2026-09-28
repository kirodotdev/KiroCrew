/**
 * Screenshot harness for the session-import failure notice.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with every /api/** answered from fixtures (gateway-free).
 *
 * The scenario is the production sequence: the user opens New ▾, picks
 * "Import a session from a file", the menu closes while the OS picker is open
 * (the picker's window blur closes a Radix menu), and the import is refused
 * with `400 {code: "transfer_bundle_too_large"}`. The row that started the
 * import is gone by then, so the refusal must land in the app-shell notice.
 *
 * Asserts as well as shoots: the POST must carry the picked bytes, the notice
 * must show the file-worded refusal under its own title, and no native alert
 * may fire.
 *
 * Usage: node scripts/capture-import-session-failure.mjs [outDir] [prefix] [--dist <dir>] [--mobile]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const positional = process.argv.slice(2).filter((a, i, all) => !a.startsWith('--') && all[i - 1] !== '--dist')
const OUT = positional[0] || '../temp-screenshots/import-session-failure'
const PREFIX = positional[1] || 'after'
const distIdx = process.argv.indexOf('--dist')
const DIST = distIdx > -1 ? process.argv[distIdx + 1] : undefined
const MOBILE = process.argv.includes('--mobile')

mkdirSync(OUT, { recursive: true })

const LOCAL_SLOTS = [{
  key: 'chat-local', title: 'Release notes draft', messages: 2, running: false,
  agent: 'kirocrew', created: '2026-09-13T20:00:00Z', last_ts: new Date(Date.now() - 60_000).toISOString(), folder_id: '',
}]

async function main() {
  const { srv, base } = await serveDist(DIST)
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  const context = await browser.newContext(MOBILE
    ? { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true }
    : { viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  let importedBytes = null
  await stubDashboardApi(page, {
    folders: [], slots: LOCAL_SLOTS,
    extra: async (path, route) => {
      if (path === '/api/chat/slots/import' && route.request().method() === 'POST') {
        importedBytes = route.request().postDataBuffer()
        // What the import route answers for a bundle past its expansion ceiling.
        await json(route, { error: 'compressed bundle expands past 65 MiB', code: 'transfer_bundle_too_large' }, 400)
        return true
      }
      if (path === '/api/chat/slots/chat-local') {
        await json(route, { messages: [{ role: 'user', content: 'draft', ts: '2026-09-13T20:00:00Z', meta: { mid: 'l-1' } }], has_more: false, total: 1 })
        return true
      }
      return false
    },
  })
  logPageProblems(page)
  let alerted = null
  page.on('dialog', async d => { alerted = d.message(); await d.dismiss() })
  page.on('pageerror', e => console.log('PAGEERROR', e.message))

  await page.goto(`${base}/chat?sid=chat-local`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[aria-label="Chat messages"]', { timeout: 20_000 })

  // A phone hides the sidebar, so it reaches the row through the chat header's
  // session menu; the desktop frame uses the sidebar's New menu.
  if (MOBILE) await page.getByRole('button', { name: /session options/i }).first().click()
  else await page.locator('[data-create-menu] button[aria-haspopup="menu"], [data-create-menu] button:has(svg.lucide-chevron-down)').last().click()
  const item = page.getByRole('menuitem', { name: /import a session from a file/i })
  await item.waitFor({ state: 'visible', timeout: 10_000 })

  const chooserPromise = page.waitForEvent('filechooser')
  await item.click()
  const chooser = await chooserPromise
  // The OS picker blurs the window and Radix closes the menu before the user
  // confirms. Headless Chromium opens no real picker, so close it the same way.
  await page.keyboard.press('Escape')
  await page.getByRole('menuitem', { name: /import a session from a file/i }).waitFor({ state: 'detached', timeout: 5_000 })
  await chooser.setFiles({ name: 'chat.kcsession.json.gz', mimeType: 'application/gzip', buffer: Buffer.from([0x1f, 0x8b, 0x08, 0x00]) })

  const notice = page.getByText('This file is too large to import.')
  await notice.waitFor({ state: 'visible', timeout: 10_000 })
  // Off every hover target, so the frame shows only shipped UI.
  if (!MOBILE) await page.mouse.move(1100, 500)
  await page.waitForTimeout(400)

  if (!importedBytes || importedBytes[0] !== 0x1f || importedBytes[1] !== 0x8b) {
    throw new Error('the import POST did not carry the picked file bytes')
  }
  if (alerted !== null) throw new Error(`a native alert fired: ${alerted}`)
  if (!(await page.getByText('The import failed').isVisible())) {
    throw new Error('the notice is not titled as an import failure')
  }

  await page.screenshot({ path: `${OUT}/${PREFIX}-1-page.png` })
  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${PREFIX}-1-page.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
