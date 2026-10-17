/**
 * Screenshots for a file-change card whose turn is older than the chat keeps
 * copies for.
 *
 * `session.file_changes_keep_turns` keeps whole before/after copies for a
 * chat's most recent file-changing turns. An older turn's entries come back
 * with no copies, `content_omitted`, and `omitted_reason: "older_turn"`: the
 * card still lists every file, tags each "File only", says once why the diff is
 * gone, and opens the real file. A recent turn's card below it is unchanged.
 * Three frames per theme:
 *
 *   01-older-and-recent-<theme>  the transcript with both cards: the older
 *                                turn's file-only card above, the recent
 *                                turn's full diff card below.
 *   02-older-card-<theme>        the older turn's card by itself, so the
 *                                notice is legible at full scale.
 *   03-older-minimal-<theme>     the minimal (glass pill) style of the older
 *                                turn: filenames, the marker, the notice once.
 *
 * Runs the REAL built SPA (website/dist) behind the shared dashboard stub: no
 * gateway, no token, no network.
 *
 * Usage: node scripts/capture-file-changes-older-turn.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/file-changes-older-turn'
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const MAX_EDGE = 2000

mkdirSync(OUT, { recursive: true })

const OLDER = path => ({ path, before: '', after: '', truncated: true, content_omitted: true, omitted_reason: 'older_turn' })
const OLDER_CHANGES = [
  OLDER('src/kiro_crew/dashboard/chat_runner.py'),
  OLDER('src/kiro_crew/history.py'),
]
const RECENT_CHANGES = [{
  path: 'src/kiro_crew/dashboard/slot_persistence/transcript_merge.py',
  before: 'def compose_payload(state, slot):\n    prefix = read_prefix(slot)\n    return prefix',
  after: 'def compose_payload(state, slot):\n    prefix = read_prefix(slot)\n    prefix = bound(prefix)\n    return prefix',
}]
const OLDER_SENTENCE = /This turn is too old to keep its diffs\. Only the chat's most recent file-changing turns keep them\./
const OPEN_HINT = /Click a file name to open it/
const FILE_ONLY = 'File only'

const t0 = Math.floor(Date.now() / 1000) - 7200
const SURFACE = {
  key: 'chat-file-changes-older-turn',
  title: 'Older turn file changes',
  messages: [
    { role: 'user', content: 'Refactor the runner and the history log.', ts: String(t0) },
    { role: 'assistant', ts: String(t0 + 60), content: 'Done: both files are refactored.', meta: { file_changes: OLDER_CHANGES } },
    { role: 'user', content: 'Now bound the prefix.', ts: String(t0 + 3600) },
    { role: 'assistant', ts: String(t0 + 3660), content: 'Done: the prefix is bounded.', meta: { file_changes: RECENT_CHANGES } },
  ],
}
const slots = [{
  key: SURFACE.key,
  title: SURFACE.title,
  running: false,
  last_message: SURFACE.title,
  messages: SURFACE.messages.length,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]
const detail = { running: false, has_more: false, total: SURFACE.messages.length, queue: [], messages: SURFACE.messages }

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  const wrote = []
  for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({ viewport: { width: 1180, height: 900 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    const extra = async (path, route) => {
      if (path === '/api/chat/slots') return json(route, slots), true
      if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
      if (path === '/api/file-read') return route.fulfill({ status: 200, body: '' }), true
      if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
      return false
    }
    await stubDashboardApi(page, { slots, extra, theme })
    logPageProblems(page)

    async function shot(target, name) {
      const file = `${OUT}/${name}.png`
      await target.screenshot({ path: file })
      const { w, h } = pngSize(file)
      console.log(`wrote ${file}  ${w}x${h}`)
      wrote.push({ file, over: w > MAX_EDGE || h > MAX_EDGE })
    }
    async function open(fileChipStyle) {
      await page.addInitScript(([key, t, style]) => {
        localStorage.clear()
        localStorage.setItem('mc-theme', t)
        localStorage.setItem('mc-onboarded', '1')
        localStorage.setItem('mc-active-slot-chat', key)
        localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: false, fileChipStyle: style, streamMode: 'immediate' }))
      }, [SURFACE.key, theme, fileChipStyle])
      await page.goto(base + '/?sid=' + encodeURIComponent(SURFACE.key), { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(2600)
      await page.keyboard.press('Escape')
      const close = page.locator('[aria-label="Close"]')
      if (await close.count()) await close.first().click().catch(() => {})
      await page.waitForTimeout(400)
    }

    await open('expanded')
    const cards = page.locator('div.ft-block-reveal:has([data-testid^="fcc-row-"])')
    await cards.first().waitFor({ state: 'visible', timeout: 15000 })
    if ((await cards.count()) !== 2) throw new Error(`expected 2 cards, got ${await cards.count()}`)
    const older = cards.nth(0)
    const recent = cards.nth(1)
    const notice = older.locator('[data-fcc-turn-notice]')
    if ((await notice.count()) !== 1 || !(await notice.getByText(OLDER_SENTENCE).count()) || !(await notice.getByText(OPEN_HINT).count())) {
      throw new Error('expected ONE older-turn notice on the older card, with the open hint')
    }
    if ((await older.getByText(FILE_ONLY).count()) !== OLDER_CHANGES.length) throw new Error('expected a File only tag per older row')
    if (await older.getByText(/too large/).count()) throw new Error('an older turn must not claim it was too large')
    // A native title tooltip is never painted in a headless frame, so assert its text instead.
    for (const title of await older.locator('[data-fcc-demoted-tag]').evaluateAll(els => els.map(e => e.getAttribute('title')))) {
      if (!OLDER_SENTENCE.test(title || '')) throw new Error(`the File only tag's hover text must be the older-turn sentence, got ${title}`)
    }
    if (await recent.locator('[data-fcc-turn-notice]').count()) throw new Error('the recent card must carry no notice')
    if (await recent.getByText(FILE_ONLY).count()) throw new Error('the recent card must carry no File only tag')
    if (!(await recent.getByText('1 addition').count())) throw new Error('the recent card keeps its diff stats')
    const conversation = page.locator('div.ft-block-reveal:has([data-testid^="fcc-row-"])').first().locator('xpath=ancestor::*[.//text()[contains(., "Now bound the prefix.")]][1]')
    await shot((await conversation.count()) ? conversation : page.locator('main').first(), `01-older-and-recent-${theme}`)
    await shot(older, `02-older-card-${theme}`)

    await open('minimal')
    const pillRow = page.locator('div.ft-block-reveal:has(button.file-chip)').first()
    await pillRow.waitFor({ state: 'visible', timeout: 15000 })
    const pillNotice = pillRow.locator('[data-fcc-turn-notice]')
    if ((await pillNotice.count()) !== 1 || !(await pillNotice.getByText(OLDER_SENTENCE).count())) throw new Error('expected ONE older-turn line after the pills')
    if ((await pillRow.getByText(FILE_ONLY).count()) !== OLDER_CHANGES.length) throw new Error('expected a File only marker per pill')
    await shot(pillRow, `03-older-minimal-${theme}`)
    await context.close()
  }
  await browser.close()
  srv.close()
  const over = wrote.filter(x => x.over)
  if (over.length) throw new Error('frames over the 2000px budget: ' + over.map(x => x.file).join(', '))
  console.log(`done: ${wrote.length} frames in ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
