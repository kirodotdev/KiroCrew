/**
 * Screenshot harness for #33994: the "Optimizing prompt…" overlay's busy indicator.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures. The optimize request (POST /api/optimizer/optimize) is never
 * answered, so the overlay stays up for as long as the harness needs it.
 *
 * Frames, in the dark and light themes at a desktop width:
 *   <label>-<theme>-optimizing.png   the overlay while the optimize runs
 * plus, with --record, one <label>-dark-optimizing.webm of the overlay in motion.
 *
 * Usage: node scripts/capture-33994-optimize-overlay-spinner.mjs <outDir> [label] [--record]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2)
const RECORD = args.includes('--record')
const [OUT = '../temp-screenshots/33994-optimize-overlay-spinner', LABEL = 'after'] = args.filter(a => a !== '--record')
const SLOT = 'optimize-overlay-fixture'
const DRAFT = 'look at the failing build and tell me what broke'

mkdirSync(OUT, { recursive: true })

async function run(base, theme, record) {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 960, height: 640 },
    deviceScaleFactor: record ? 1 : 2,
    ...(record ? { recordVideo: { dir: OUT, size: { width: 960, height: 640 } } } : {}),
  })
  const page = await context.newPage()
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  await stubDashboardApi(page, {
    theme,
    localStorageEntries: { 'mc-lang': 'en', 'mc-theme': theme, 'mc-onboarded': '1', 'mc-active-slot': SLOT, 'mc-active-slot-chat': SLOT },
    slots: [{ key: SLOT, title: 'Build triage', running: false, messages: 2,
      agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
      modified: 1788780000, source_links: [], source_links_total: 0 }],
    extra: async (path, route) => {
      if (path === `/api/chat/slots/${SLOT}`) {
        await json(route, { running: false, has_more: false, total: 2, queue: [], project: '', messages: [
          { role: 'user', ts: 1788780000, content: 'Hi' },
          { role: 'assistant', ts: 1788780030, content: 'Ready.' },
        ] })
      } else if (path === `/api/chat/slots/${SLOT}/source-links`) {
        await json(route, { links: [], total: 0 })
      } else if (path === '/api/members') {
        // The left dock's roster; the stub's guessed `[]` reads as a load failure.
        await json(route, { members: [], default_agent: 'kirocrew' })
      } else if (path.startsWith('/api/optimizer/optimize')) {
        // Never answered, so the overlay stays up.
      } else return false
      return true
    },
  })
  await page.goto(`${base}/?slot=${SLOT}`, { waitUntil: 'domcontentloaded' })
  const composer = page.locator('textarea[data-composer-input]')
  await composer.waitFor({ timeout: 20000 })
  await composer.fill(DRAFT)
  await page.getByRole('button', { name: 'Optimize prompt' }).first().click()
  const overlay = page.locator('.optimize-overlay')
  await overlay.waitFor({ timeout: 10000 })
  // Let the overlay's fade-in finish before the still.
  await page.waitForTimeout(600)
  const area = page.locator('.input-area')
  if (record) {
    await page.waitForTimeout(3000)
    const video = page.video()
    await context.close()
    const file = `${OUT}/${LABEL}-${theme}-optimizing.webm`
    renameSync(await video.path(), file)
    console.log('wrote', file)
  } else {
    const file = `${OUT}/${LABEL}-${theme}-optimizing.png`
    await area.screenshot({ path: file })
    console.log('wrote', file)
    await context.close()
  }
  await browser.close()
}

const { srv, base } = await serveDist()
try {
  for (const theme of ['dark', 'light']) await run(base, theme, false)
  if (RECORD) await run(base, 'dark', true)
} finally {
  srv.close()
}
