/**
 * Screenshot harness for the session menu's two file-export rows.
 *
 * Runs the REAL built SPA (website/dist) against fixture APIs — no gateway, no
 * credential. The only substitution is the export endpoint, which answers a
 * small body plus the Content-Disposition the real handler sends, so the saved
 * filename is produced by the same code path a user exercises.
 *
 * ## What this proves — and what a picture cannot
 *
 * The frames are evidence of the TWO ROWS, their labels and their order, plus
 * the non-persistent state where both are refused. What a
 * picture cannot show is which document each row actually asks for, so the
 * assertions below capture the request URLs: the Markdown row must send
 * `?format=md` and the JSON row must send no format at all. That half is
 * load-bearing — it fails the harness if the rows are ever wired to the same
 * format, which re-reading a screenshot would never catch.
 *
 * Usage: node scripts/capture-session-markdown-export.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-markdown-export'
const SLOT = 'chat-mdexport'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Markdown export',
  running: false,
  last_message: '',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = { running: false, has_more: false, total: 0, queue: [], project: PROJECT, messages: [] }

const { srv, base } = await serveDist()

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
const page = await context.newPage()
const errors = []
const exportCalls = []
page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })

await page.routeWebSocket(/\/api\/ws/, () => {})

const fixedApi = makeFixedApi(PROJECT)
await page.route('**/api/**', route => {
  const url = new URL(route.request().url())
  const path = url.pathname
  if (path.endsWith('/export')) {
    // Record what the frontend asked for, then answer as the handler does.
    const fmt = url.searchParams.get('format') || ''
    exportCalls.push(fmt)
    // `md` is the only spelling the real handler accepts, so the stub must not
    // be looser -- a stub that answered "markdown" too would keep passing if the
    // frontend regressed to sending it.
    const markdown = fmt === 'md'
    return route.fulfill({
      status: 200,
      headers: {
        'Content-Type': markdown ? 'text/markdown; charset=utf-8' : 'application/gzip',
        'Content-Disposition':
          `attachment; filename="markdown-export-20261003T225500Z.kcsession.${markdown ? 'md' : 'json.gz'}"`,
        'X-Content-Type-Options': 'nosniff',
      },
      body: markdown ? '# Markdown export\n\n## User\n\nhi\n' : 'x',
    })
  }
  if (path === '/api/chat/slots') return json(route, slots)
  if (path.startsWith('/api/chat/slots/')) return json(route, detail)
  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript((slot) => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-active-slot-chat', slot)
}, SLOT)

await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
await page.getByLabel('Message input').waitFor({ timeout: 20000 })

await page.getByRole('button', { name: /session options/i }).click()
const menu = page.getByRole('menu').first()
await menu.waitFor({ timeout: 10000 })

const rows = page.getByRole('menuitem', { name: /^Export (for import|as readable)/i })
await rows.first().waitFor({ timeout: 10000 })
await page.waitForTimeout(300)

// Both rows, in the menu, before anything is clicked.
await menu.screenshot({ path: `${OUT}/export-rows-dark.png` })

const count = await rows.count()
if (count !== 2) errors.push(`ASSERT: expected 2 export rows, found ${count}`)
const labels = await rows.allInnerTexts()
// JSON keeps its original position as the established export; Markdown sits
// below it and above the Install row that reads the JSON file back. Each label
// names what its file is FOR, so neither carries a muted format suffix.
if (!/^Export for import \(JSON\)/.test(labels[0] || '')) {
  errors.push(`ASSERT: first row is not the JSON export: ${labels[0]}`)
}
if (!/^Export as readable Markdown/.test(labels[1] || '')) {
  errors.push(`ASSERT: second row is not the Markdown export: ${labels[1]}`)
}

// Click the Markdown row and catch the download it triggers.
const dl = page.waitForEvent('download', { timeout: 10000 })
await rows.nth(1).click()
const download = await dl
const mdName = download.suggestedFilename()
if (!mdName.endsWith('.kcsession.md')) errors.push(`ASSERT: markdown filename is ${mdName}`)

// The menu stays open and the row reports the outcome in place.
await page.getByText(/^Exported$/).first().waitFor({ timeout: 10000 })
await page.waitForTimeout(300)
await menu.screenshot({ path: `${OUT}/export-markdown-done-dark.png` })

await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
await page.waitForTimeout(400)
await menu.screenshot({ path: `${OUT}/export-markdown-done-light.png` })

// Now the JSON row, to prove the two are wired to different formats.
const dl2 = page.waitForEvent('download', { timeout: 10000 })
await rows.first().click()
const jsonName = (await dl2).suggestedFilename()
if (!jsonName.endsWith('.kcsession.json.gz')) errors.push(`ASSERT: json filename is ${jsonName}`)

if (exportCalls.length !== 2) errors.push(`ASSERT: expected 2 export calls, got ${exportCalls.length}`)
if (exportCalls[0] !== 'md') errors.push(`ASSERT: markdown row sent format=${exportCalls[0] || '(none)'}`)
if (exportCalls[1] !== '') errors.push(`ASSERT: json row sent format=${exportCalls[1]}`)

// The non-persistent state, which is the widest the rows ever render: both
// disabled, each carrying the "session not saved to disk" reason. The backend
// refuses to export an incognito or temporary transcript, so this is the state a
// reader needs to see is legible rather than clipped -- the longer labels made
// that a fair question.
await page.keyboard.press('Escape')
slots[0].memory_mode = 'incognito'
await page.reload({ waitUntil: 'domcontentloaded' })
await page.getByLabel('Message input').waitFor({ timeout: 20000 })
await page.getByRole('button', { name: /session options/i }).click()
const menu2 = page.getByRole('menu').first()
await menu2.waitFor({ timeout: 10000 })
const offRows = page.getByRole('menuitem', { name: /^Export (for import|as readable)/i })
await offRows.first().waitFor({ timeout: 10000 })
await page.waitForTimeout(300)

const offCount = await offRows.count()
if (offCount !== 2) errors.push(`ASSERT: expected 2 disabled export rows, found ${offCount}`)
for (let i = 0; i < offCount; i++) {
  const r = offRows.nth(i)
  const disabled = await r.getAttribute('aria-disabled')
  if (disabled !== 'true') {
    errors.push(`ASSERT: row ${i} is not disabled for an incognito session (aria-disabled=${disabled})`)
  }
  if (!/session not saved to disk/i.test(await r.innerText())) {
    errors.push(`ASSERT: row ${i} does not carry the reason: ${await r.innerText()}`)
  }
}
// A disabled row must not reach the endpoint at all.
const callsBefore = exportCalls.length
await offRows.first().click({ force: true })
await page.waitForTimeout(300)
if (exportCalls.length !== callsBefore) {
  errors.push(`ASSERT: a disabled row called the endpoint (format=${exportCalls.at(-1)})`)
}
await menu2.screenshot({ path: `${OUT}/export-rows-not-persistent-dark.png` })

await context.close()
await browser.close()
srv.close()

console.log(JSON.stringify({ out: OUT, exportCalls, mdName, jsonName, labels, errors }, null, 2))
if (errors.length) process.exitCode = 1
