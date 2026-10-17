// Evidence capture: the "Optimizing prompt…" overlay on a phone, with its
// Cancel button, before and after the tap.
//
// Runs the REAL built SPA (website/dist) with the dashboard API stubbed and the
// optimizer request held open, so the overlay stays up until Cancel aborts it.
// Not a test: ChatInput.optimizeCancel.test.tsx pins the behaviour; this shows it.
//
//   node scripts/capture-optimize-cancel.mjs [outDir]
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/optimize-cancel'
mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const CANCEL = manual.components.chatInput.cancel_optimizing
if (!CANCEL) throw new Error('cancel_optimizing missing from en.manual.json')

const SLOT = 'optimize-demo'
const DRAFT = 'look at the failing build and tell me what broke'
const now = Math.floor(Date.now() / 1000)
const slots = [{
  key: SLOT, title: 'Build triage', running: false, stop_state: 'idle',
  last_message: 'Ready.', messages: 2, agent: 'kirocrew', memory_mode: 'persistent',
  modified: now, source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 120, content: 'Hi' },
    { role: 'assistant', ts: now - 110, content: 'Ready.' },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true,
  })
  const page = await context.newPage()
  logPageProblems(page)
  let optimizeRequests = 0
  await stubDashboardApi(page, {
    slots,
    extra: async (path, route) => {
      if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
      // Never answered: the page's abort is the only thing that ends it.
      if (path.startsWith('/api/optimizer/optimize')) { optimizeRequests++; return true }
      return false
    },
  })
  await page.goto(base + `/chat?sid=${encodeURIComponent(SLOT)}`, { waitUntil: 'domcontentloaded' })
  const box = page.getByRole('textbox', { name: /message/i }).first()
  await box.waitFor({ timeout: 15000 })
  await box.fill(DRAFT)
  await page.getByRole('button', { name: 'Optimize prompt' }).first().tap()
  const cancel = page.getByRole('button', { name: CANCEL })
  await cancel.waitFor({ timeout: 5000 })
  if (optimizeRequests !== 1) throw new Error(`expected 1 optimize request, saw ${optimizeRequests}`)
  await page.waitForTimeout(400)
  const size = await cancel.boundingBox()
  console.log(`cancel target ${Math.round(size.width)}x${Math.round(size.height)} css px`)
  await page.screenshot({ path: `${OUT}/mobile-optimizing.png` })

  await cancel.tap()
  await cancel.waitFor({ state: 'detached', timeout: 5000 })
  if (await box.getAttribute('readonly') !== null) throw new Error('composer still read-only after Cancel')
  await box.press('End')
  await box.type(' please')
  const value = await box.inputValue()
  if (value !== DRAFT + ' please') throw new Error(`draft not editable after Cancel: ${JSON.stringify(value)}`)
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/mobile-after-cancel.png` })
  await captureDesktop(browser, base)
  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/{mobile-optimizing,mobile-after-cancel,desktop-optimizing}.png`)
}

// The mouse-pointer size of the same button (28 px) inside the overlay pill.
async function captureDesktop(browser, base) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots,
    extra: async (path, route) => {
      if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
      return path.startsWith('/api/optimizer/optimize')
    },
  })
  await page.goto(base + `/chat?sid=${encodeURIComponent(SLOT)}`, { waitUntil: 'domcontentloaded' })
  const box = page.getByRole('textbox', { name: /message/i }).first()
  await box.waitFor({ timeout: 15000 })
  await box.fill(DRAFT)
  await page.getByRole('button', { name: 'Optimize prompt' }).first().click()
  const cancel = page.getByRole('button', { name: CANCEL })
  await cancel.waitFor({ timeout: 5000 })
  await page.waitForTimeout(400)
  const size = await cancel.boundingBox()
  console.log(`desktop cancel target ${Math.round(size.width)}x${Math.round(size.height)} css px`)
  const pill = await cancel.evaluate(el => {
    const r = el.parentElement.getBoundingClientRect()
    return { x: r.x, y: r.y, width: r.width, height: r.height }
  })
  const pad = 120
  await page.screenshot({
    path: `${OUT}/desktop-optimizing.png`,
    clip: { x: Math.max(0, pill.x - pad * 3), y: Math.max(0, pill.y - pad), width: pill.width + pad * 6, height: pill.height + pad * 2 },
  })
  await context.close()
}

main().catch(err => { console.error(err); process.exit(1) })
