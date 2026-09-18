/**
 * Screenshot evidence for attachments in a Crew Members DM (ChatPane) next to
 * the same rows on the main chat (ChatPage).
 *
 * Both entries mount the REAL page: capture/chatpage-scroll-shell.html for the
 * main chat and capture/members-page.html for the members page, each fed the
 * SAME two user rows through page.route:
 *   - a row in the shape the pane wrote BEFORE it serialized attachments
 *     (typed text verbatim, the image only on `meta.files`), and
 *   - a row in the producer shape every surface now writes
 *     (`![image](dest)` line ahead of the caption), and
 *   - a non-image attachment in the persisted shape (`[attached_file 1] path`
 *     in the text, the ordered list on `meta.files`) -- also the shape a
 *     drained queued send is rebuilt into.
 * `/api/file-raw` answers with a real PNG so an <img> either paints or does
 * not. Every frame ASSERTS the count of painted images before it is written,
 * so a frame cannot document the wrong state:
 *   MODE=after  (default) the members DM paints both images, like the main chat
 *   MODE=before run against a checkout of the base commit: the members DM
 *               paints only the producer row (a shape the pane never wrote)
 *               and NOT the row the pane actually persisted -- the defect
 * and writes a side-by-side composite per theme (light + dark).
 *
 * Usage (from website/):
 *   npx vite --host 127.0.0.1 --port 6835 --strictPort           # another shell
 *   node scripts/capture-members-dm-attachment.mjs http://127.0.0.1:6835 ../temp-screenshots/members-dm-attachment
 *   MODE=before node scripts/capture-members-dm-attachment.mjs http://127.0.0.1:6836 ../temp-screenshots/members-dm-attachment
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { mockShot } from './lib/mock-shot.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6835'
const OUT = process.argv[3] || '../temp-screenshots/members-dm-attachment'
const MODE = process.env.MODE === 'before' ? 'before' : 'after'
mkdirSync(OUT, { recursive: true })

// The "attached picture": a mock phone-sized app screenshot rendered by the
// browser below (390 x 844, the members-page mobile frame), so the rendered
// thumbnail reads as a real capture without this harness depending on any
// other feature's screenshots.
const IMG_PATH = '/home/user/.kiro/crew/uploads/roster-mobile.png'
const CAPTION = 'Why is the roster row taller on mobile?'
// A non-image attachment in the PERSISTED shape (marker in the text, the
// ordered list on meta.files) -- the shape a dispatched send writes and the
// shape a drained queued send is rebuilt into. Spaced on purpose: the list is
// what keeps the marker lossless.
const PDF_PATH = '/home/user/.kiro/crew/uploads/Q3 roster report.pdf'
const PDF_CAPTION = 'Summarize the attached report.'
// A file the caption MENTIONS inline (the persisted form of an `@notes.md`
// token: the marker sits on the same line as prose), so it renders as an
// `@label` chip in the sentence rather than as a card.
const NOTES_PATH = '/home/user/.kiro/crew/uploads/notes.md'

/** The two user rows, one per shape. `legacy` is what ChatPane persisted
 *  before this fix; `producer` is what every surface writes now. */
const ROWS = [
  { role: 'user', content: `${PDF_CAPTION}\n[attached_file 1] ${PDF_PATH}`, cls: '', ts: '2026-09-08T07:59:00Z', meta: { files: [PDF_PATH] } },
  { role: 'assistant', content: 'Three findings; the roster row height is the first.', cls: '', ts: '2026-09-08T07:59:10Z' },
  { role: 'user', content: `Cross-check it against [attached_file 1] ${NOTES_PATH} first.`, cls: '', ts: '2026-09-08T07:59:30Z', meta: { files: [NOTES_PATH] } },
  { role: 'assistant', content: 'Done; the notes agree on the row height.', cls: '', ts: '2026-09-08T07:59:40Z' },
  { role: 'user', content: CAPTION, cls: '', ts: '2026-09-08T08:00:00Z', meta: { files: [IMG_PATH] } },
  { role: 'assistant', content: 'The row grows to fit the two-line status subtitle; the avatar stays 32px.', cls: '', ts: '2026-09-08T08:00:10Z' },
  { role: 'user', content: `![image](${IMG_PATH})\n\n${CAPTION}`, cls: '', ts: '2026-09-08T08:01:00Z' },
  { role: 'assistant', content: 'Same answer -- and this time I can see the frame.', cls: '', ts: '2026-09-08T08:01:10Z' },
]

/** Byte-equal to capture/chatpage-scroll-shell.tsx's `short` scene. */
const PRELOADED_SHORT = [
  { role: 'user', content: 'Status check #1: anything new in the queue?', cls: '', ts: '2026-08-27T01:10:00Z' },
  { role: 'assistant', content: 'Sweep 1 done.\n\n- two issues triaged as duplicates\n- one PR moved to review-ready\n- CI green on the retry', cls: '', ts: '2026-08-27T01:10:30Z' },
]

const MEMBERS = [
  { name: 'radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: false, kiro_agent: 'kirocrew-autofix', workspace: 'autofix', memory_store: 'default', model: '', last_active_ts: 1000, last_message: CAPTION },
  { name: 'scribe', slug: 'scribe', bound: false, slot_key: '', running: false, kiro_agent: 'kirocrew-lite', workspace: 'docs', memory_store: 'default', model: '' },
]

const browser = await chromium.launch()
const PNG = await mockShot(browser, 390, 844)
let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function routeApi(page, slotKey) {
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (path === '/api/file-raw') {
      return url.searchParams.get('path') === IMG_PATH
        ? route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
        : route.fulfill({ status: 404, body: 'not the fixture image' })
    }
    if (path === '/api/members') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ members: MEMBERS, default_agent: 'kirocrew' }) })
    }
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1])
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ slot_key: `member-${slug}`, slug, member: slug, created: false }) })
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ slug: 'radar', member: 'radar', capped: false, entries: [] }) })
    }
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      // The main-chat entry preloads its own two `short` rows and the mount
      // refetch MERGES by identity, so answer with those two FIRST (as older
      // history) or the preloaded assistant row lands below the fixture rows.
      const rows = slotKey === 'slot-a' ? [...PRELOADED_SHORT, ...ROWS] : ROWS
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ key: slotKey, title: 'radar', running: false, has_more: false, total: rows.length, messages: rows }) })
    }
    if (path === '/api/agents') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ agents: [], default_agent: 'kirocrew' }) })
    }
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$|crons|webhooks/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
}

/** Painted attachment images inside the transcript: an <img> whose src is the
 *  fixture's file-raw URL and that actually decoded (naturalWidth > 0). */
const paintedImages = (page) => page.evaluate((imgPath) => {
  const imgs = [...document.querySelectorAll('img')].filter((el) => decodeURIComponent(el.getAttribute('src') || '').includes(imgPath))
  return { total: imgs.length, painted: imgs.filter((el) => el.complete && el.naturalWidth > 0).length }
}, IMG_PATH)

/** The non-image attachment card: how many render, and whether the one in
 *  view is an interactive control (main chat: opens the file) or inert (pane:
 *  no file viewer to open it in). */
const fileCards = (page) => page.evaluate((pdfPath) => {
  const cards = [...document.querySelectorAll(`[title*="${pdfPath}"]`)]
  return {
    total: cards.length,
    buttons: cards.filter((el) => el.getAttribute('role') === 'button').length,
    // A native `title` tooltip never paints in a headless frame, so the inert
    // card's explanation is asserted here rather than photographed.
    titles: cards.map((el) => el.getAttribute('title') || ''),
  }
}, PDF_PATH)

/** The inline `@notes.md` mention chip: rendered as `@<label>` text with the
 *  path in its tooltip; a control on the main chat, an inert span in the pane. */
const mentionChips = (page) => page.evaluate((notesPath) => {
  const chips = [...document.querySelectorAll(`[title*="${notesPath}"]`)].filter((el) => (el.textContent || '').startsWith('@'))
  return { total: chips.length, buttons: chips.filter((el) => el.getAttribute('role') === 'button').length }
}, NOTES_PATH)

async function settleImages(page) {
  await page.waitForFunction((imgPath) => {
    const imgs = [...document.querySelectorAll('img')].filter((el) => decodeURIComponent(el.getAttribute('src') || '').includes(imgPath))
    return imgs.every((el) => el.complete)
  }, IMG_PATH, { timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(250)
}

/** Both transcripts open scrolled to the bottom; the legacy row is the FIRST
 *  row, so scroll to the top or the frame never shows the row that failed. */
async function scrollTranscriptTop(page) {
  await page.evaluate(() => {
    const scrollers = [...document.querySelectorAll('*')].filter((el) => {
      const s = getComputedStyle(el)
      return /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 4 && el.clientHeight > 200
    })
    scrollers.forEach((el) => { el.scrollTop = 0 })
  })
  await page.waitForTimeout(250)
}

async function shootMainChat(theme) {
  const page = await browser.newPage({ viewport: { width: 640, height: 820 }, deviceScaleFactor: 1 })
  await routeApi(page, 'slot-a')
  await page.goto(`${BASE}/capture/chatpage-scroll-shell.html?theme=${theme}&scene=short`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('this time I can see the frame').first().waitFor()
  await settleImages(page)
  const imgs = await paintedImages(page)
  // The main chat has always drawn the producer row's image; the legacy
  // row's image is drawn from meta.files only once the heal is in.
  const want = MODE === 'before' ? 1 : 2
  check(`main-chat ${theme} images`, imgs.painted === want, `painted=${imgs.painted}/${imgs.total} want=${want}`)
  // ChatPage has always drawn the file card, as a control that opens the file.
  const cards = await fileCards(page)
  check(`main-chat ${theme} file card`, cards.total === 1 && cards.buttons === 1, `cards=${cards.total} buttons=${cards.buttons} want=1/1`)
  const chips = await mentionChips(page)
  check(`main-chat ${theme} mention chip`, chips.total === 1 && chips.buttons === 1, `chips=${chips.total} buttons=${chips.buttons} want=1/1`)
  await scrollTranscriptTop(page)
  const path = `${OUT}/main-chat-${theme}-${MODE}.png`
  await page.screenshot({ path })
  await page.close()
  return path
}

async function shootMembersDm(theme) {
  const page = await browser.newPage({ viewport: { width: 640, height: 820 }, deviceScaleFactor: 1 })
  await routeApi(page, 'member-radar')
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('radar', { exact: true }).first().waitFor()
  await page.getByText('radar', { exact: true }).first().click()
  await page.getByText('this time I can see the frame').first().waitFor()
  await settleImages(page)
  const imgs = await paintedImages(page)
  // The defect, in two parts. The row the pane ACTUALLY wrote (legacy shape)
  // paints nothing -- meta.files is read for file cards only. The producer
  // row paints on base too, but only because the fixture hands it over: the
  // pane never produced that shape, so a real member DM had only legacy rows
  // and showed no picture at all. After the fix both paint: the producer row
  // is what the pane now sends, the legacy row is healed from meta.files.
  const want = MODE === 'before' ? 1 : 2
  check(`members-dm ${theme} images`, imgs.painted === want, `painted=${imgs.painted}/${imgs.total} want=${want}`)
  const captions = await page.getByText(CAPTION).count()
  check(`members-dm ${theme} captions`, captions >= 2, `captions=${captions} (both user rows rendered)`)
  // The registry's old private renderer read no meta.files at all, so the
  // pane drew NO card for a non-image attachment. Now it draws the same card
  // the main chat does -- inert (no button role), because the pane has no
  // file viewer to open it in.
  const cards = await fileCards(page)
  const wantCards = MODE === 'before' ? 0 : 1
  check(`members-dm ${theme} file card`, cards.total === wantCards && cards.buttons === 0, `cards=${cards.total} buttons=${cards.buttons} want=${wantCards}/0`)
  if (MODE === 'after') {
    check(`members-dm ${theme} inert card tooltip`, cards.titles.some((t) => /can't be opened here/.test(t)), `titles=${JSON.stringify(cards.titles)}`)
  }
  // Same story for the inline mention: no chip at all on base, an inert chip now.
  const chips = await mentionChips(page)
  check(`members-dm ${theme} mention chip`, chips.total === wantCards && chips.buttons === 0, `chips=${chips.total} buttons=${chips.buttons} want=${wantCards}/0`)
  await scrollTranscriptTop(page)
  const path = `${OUT}/members-dm-${theme}-${MODE}.png`
  await page.screenshot({ path })
  await page.close()
  return path
}

async function composite(theme, left, right) {
  const page = await browser.newPage({ viewport: { width: 1320, height: 860 }, deviceScaleFactor: 1 })
  const dataUrl = (p) => `data:image/png;base64,${readFileSync(p).toString('base64')}`
  const bg = theme === 'light' ? '#f5f5f5' : '#111'
  const fg = theme === 'light' ? '#222' : '#eee'
  await page.setContent(`<!doctype html><body style="margin:0;background:${bg};color:${fg};font:13px system-ui;display:flex;gap:20px;padding:10px">
    <figure style="margin:0"><figcaption style="height:20px">Main chat (ChatPage)</figcaption><img src="${dataUrl(left)}" width="640" height="820"></figure>
    <figure style="margin:0"><figcaption style="height:20px">Crew Members DM (ChatPane) -- ${MODE}</figcaption><img src="${dataUrl(right)}" width="640" height="820"></figure>
  </body>`)
  await page.screenshot({ path: `${OUT}/side-by-side-${theme}-${MODE}.png` })
  await page.close()
}

for (const theme of ['dark', 'light']) {
  const left = await shootMainChat(theme)
  const right = await shootMembersDm(theme)
  await composite(theme, left, right)
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log(`all frames verified (${MODE})`)
