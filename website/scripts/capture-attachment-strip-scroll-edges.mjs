/**
 * Screenshot harness for the ATTACHMENT PREVIEW STRIP's scroll-edge cues in
 * the chat composer (issue #4024).
 *
 * Reproduces the reported defect against the REAL built SPA (website/dist),
 * gateway-free: staging more attachments than a 390px phone viewport can show
 * makes the strip scroll, and with no edge affordance the row simply ends —
 * on macOS/iOS the overlay scrollbar only appears mid-scroll, so nothing says
 * chips are hidden past the right edge.
 *
 * Pastes eight images so the strip overflows decisively, shoots the strip at
 * 390px parked at offset 0 (right cue expected) and scrolled to the far end
 * (left cue expected).
 *
 * ASSERTS as well as photographs: exits non-zero unless all chips are staged,
 * the strip genuinely overflows, and the cues match the hidden side —
 * `data-testid="preview-strip-cue-left"` / `-right` at each scroll position.
 * Capturing a PRE-FIX build, where no cue exists (the defect on record), is
 * the only case that needs STRIP_EXPECT_NO_CUE=1.
 *
 * Usage: node scripts/capture-attachment-strip-scroll-edges.mjs [outDir]
 *   STRIP_LANG=de            locale to render (default en)
 *   STRIP_LABEL=after        filename prefix (default state)
 *   STRIP_EXPECT_NO_CUE=1    assert the cues are ABSENT (pre-fix build)
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/attachment-strip-scroll-edges'
/** The strip's unique handle. The PRE-FIX build predates data-testid, so the
 *  before capture falls back to data-image-scope — unique there only because
 *  the stubbed transcript renders no markdown message body (those carry the
 *  same attribute); the composer strip is the sole match under this stub. */
const STRIP_SEL = '[data-testid="preview-strip"], [data-image-scope]'
const SLOT = 'chat-strip-edges'
const N_IMAGES = 8

mkdirSync(OUT, { recursive: true })

/** A mock app screenshot at an exact pixel size, rendered by the browser we
 *  already launched so the bytes are a real PNG with no encoder of our own. */
async function mockShot(browser, w, h) {
  const page = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 1 })
  await page.setContent(`<!doctype html><style>
    html,body{margin:0;height:100%;background:#fafafc}
    .bar{height:5%;background:#1c2638}
    .row{height:7.5%;margin:3.5% 6% 0;background:#e2e6ee}
  </style><div class="bar"></div>${'<div class="row"></div>'.repeat(6)}`)
  const buf = await page.screenshot()
  await page.close()
  return buf
}

const UPLOAD_DIR = '/home/user/.kiro/crew/uploads'
const PATHS = Array.from({ length: N_IMAGES }, (_, i) => `${UPLOAD_DIR}/shot-${i + 1}.png`)

const slots = [{
  key: SLOT,
  title: 'Attachment strip demo',
  running: false,
  last_message: 'Send the screenshots over and I will take a look.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '',
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
  project: '',
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 90, content: 'I have a few screenshots of the layout problem.' },
    { role: 'assistant', ts: Date.now() / 1000 - 60, content: 'Send the screenshots over and I will take a look.' },
  ],
}

/** Paste N image Files on the composer textarea in one native ClipboardEvent. */
async function pasteImages(page, files) {
  await page.evaluate(async payload => {
    const ta = document.querySelector('textarea[data-composer-typo]')
    if (!ta) throw new Error('composer textarea not found')
    ta.focus()
    const dt = new DataTransfer()
    for (const { name, b64 } of payload) {
      const bin = atob(b64)
      const bytes = new Uint8Array(bin.length)
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
      dt.items.add(new File([bytes], name, { type: 'image/png' }))
    }
    ta.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dt }))
  }, files)
}

/** The strip's live geometry and which cues are painted. */
async function readStrip(page) {
  return page.evaluate(sel => {
    const strip = document.querySelector(sel)
    if (!strip) return null
    return {
      chips: strip.children.length,
      clientWidth: strip.clientWidth,
      scrollWidth: strip.scrollWidth,
      scrollLeft: Math.round(strip.scrollLeft),
      cueLeft: !!document.querySelector('[data-testid="preview-strip-cue-left"]'),
      cueRight: !!document.querySelector('[data-testid="preview-strip-cue-right"]'),
    }
  }, STRIP_SEL)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const lang = process.env.STRIP_LANG || 'en'
  const label = process.env.STRIP_LABEL || 'state'
  const expectNoCue = !!process.env.STRIP_EXPECT_NO_CUE
  const context = await browser.newContext({
    viewport: { width: 390, height: 900 },
    deviceScaleFactor: 2,
    locale: lang,
  })
  const page = await context.newPage()
  logPageProblems(page)

  const PORTRAIT = await mockShot(browser, 1170, 2532) // phone screenshot, aspect 0.46
  const LANDSCAPE = await mockShot(browser, 1920, 1080) // desktop screenshot, aspect 1.78
  // Alternate the two aspects: landscape thumbnails are ~112px wide at h-16,
  // so the strip overflows a 390px viewport decisively rather than by a hair.
  const bytesFor = name => (parseInt(name.match(/shot-(\d+)/)[1], 10) % 2 ? PORTRAIT : LANDSCAPE)

  await page.addInitScript(l => localStorage.setItem('mc-lang', l), lang)

  const stubExtra = async (path, route) => {
    if (path === '/api/upload/file') { await json(route, { paths: PATHS }); return true }
    if (path === '/api/file-raw') {
      const name = decodeURIComponent(route.request().url())
      await route.fulfill({ status: 200, contentType: 'image/png', body: bytesFor(name) })
      return true
    }
    if (path.startsWith('/api/chat/slot/')) { await json(route, detail); return true }
    return false
  }
  await stubDashboardApi(page, { slots, extra: stubExtra })

  await page.goto(`${base}/chat/${SLOT}`)
  await page.waitForSelector('textarea[data-composer-typo]')
  await page.waitForTimeout(500)

  await pasteImages(page, PATHS.map((p, i) => ({ name: `shot-${i + 1}.png`, b64: bytesFor(`shot-${i + 1}`).toString('base64') })))
  await page.waitForSelector(`img[alt*="shot-${N_IMAGES}.png"]`, { timeout: 15000 })
  await page.waitForTimeout(400)

  const strip = page.locator(STRIP_SEL).first()

  // Parked at offset 0: content is hidden to the right only.
  const atStart = await readStrip(page)
  console.log(`  ${label} @390px offset 0:`, JSON.stringify(atStart))
  await strip.screenshot({ path: `${OUT}/${label}-${lang}-390.png` })

  if (!atStart || atStart.chips !== N_IMAGES) {
    throw new Error(`expected ${N_IMAGES} chips, saw ${atStart?.chips}`)
  }
  if (atStart.scrollWidth <= atStart.clientWidth) {
    throw new Error('strip did not overflow; the scenario under test never happened')
  }
  if (expectNoCue) {
    if (atStart.cueLeft || atStart.cueRight) throw new Error('pre-fix build unexpectedly paints a cue')
  } else {
    if (!atStart.cueRight) throw new Error('clipped-right strip must paint the right cue')
    if (atStart.cueLeft) throw new Error('nothing is hidden to the left at offset 0')
  }

  // Scrolled to the far end: the hidden side flips.
  await page.evaluate(sel => {
    const el = document.querySelector(sel)
    el.scrollLeft = el.scrollWidth
    el.dispatchEvent(new Event('scroll'))
  }, STRIP_SEL)
  await page.waitForTimeout(300)
  const atEnd = await readStrip(page)
  console.log(`  ${label} @390px far end:`, JSON.stringify(atEnd))
  await strip.screenshot({ path: `${OUT}/${label}-scrolled-${lang}-390.png` })

  if (expectNoCue) {
    if (atEnd.cueLeft || atEnd.cueRight) throw new Error('pre-fix build unexpectedly paints a cue')
  } else {
    if (!atEnd.cueLeft) throw new Error('clipped-left strip must paint the left cue')
    if (atEnd.cueRight) throw new Error('nothing is hidden to the right at the far end')
  }

  console.log(`${label} OK: ${N_IMAGES} chips staged, overflow reproduced, cues ${expectNoCue ? 'absent (pre-fix)' : 'match the hidden side'}`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
