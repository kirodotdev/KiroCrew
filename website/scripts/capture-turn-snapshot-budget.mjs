/**
 * Screenshots for the file-change card when a turn's snapshot budget dropped a
 * file's content.
 *
 * `_apply_turn_snapshot_budget` keeps the newest real change whole and demotes
 * the rest to path-only. A demoted row carries `content_omitted`, so the card
 * says the turn-budget notice ONCE under its header, tags each demoted row, and
 * the row opens the real file rather than an empty comparison. Five frames per
 * theme:
 *
 *   01-card-mixed-<theme>    the whole card: header, the two notice rows under
 *                            it (turn budget, then omitted files), one kept diff
 *                            as the first row, and three demoted rows each
 *                            showing its filename plus a short tag.
 *   02-row-notice-<theme>    the two notice rows alone, clipped from the top of
 *                            the turn-budget row to the bottom of the
 *                            omitted-files row, so their wording is legible at
 *                            full scale — the budget row must name the turn's
 *                            size, never one file's, and tell the reader the
 *                            files open; the omitted-files row must count files,
 *                            the header's unit, say they are NOT LISTED, and
 *                            promise no way to see them.
 *   03-minimal-chip-<theme>  the minimal (glass pill) style of the same turn,
 *                            the whole pill row: the kept file's stats pill
 *                            names its file beside its numbers, each demoted
 *                            pill shows its own filename plus the muted
 *                            "File only" marker, and after the pills the row
 *                            says the turn-budget sentence once and the
 *                            omitted-files notice once.
 *   04-demoted-row-<theme>   one demoted row of the card by itself: its
 *                            filename and the "File only" tag whose title
 *                            restates the turn-budget reason.
 *   05-minimal-demoted-pill-<theme>
 *                            the kept file's stats pill beside the first
 *                            demoted pill, clipped from the pill row, so the
 *                            muted "File only" marker is read against a
 *                            healthy neighbour at full scale: a demoted pill
 *                            must not pass for a complete one.
 *
 * Two states, two vocabularies, and the assertions hold them apart: a demoted
 * row or pill is a file the reader can still open, so its marker says "File
 * only"; a file the turn left out of the list is absent, so the card-level
 * line says it is "not listed" and offers nothing. Neither string may appear
 * on the other state's surface.
 *
 * The turn also reports `file_changes_omitted_files`: files the snapshot
 * limits left out of the list entirely. Both surfaces state that notice once
 * (a count of files, the same unit as the header's count, so the two add up),
 * so every whole-surface frame carries it.
 *
 * No frame shows the per-file "too large to compare" pill or row: that surface
 * predates the turn budget and renders exactly as it does without it, so the
 * fixture holds no such entry and the assertions below refuse one.
 *
 * Runs the REAL built SPA (website/dist) behind the shared dashboard stub: no
 * gateway, no token, no network.
 *
 * Usage: node scripts/capture-turn-snapshot-budget.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/turn-snapshot-budget'
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const MAX_EDGE = 2000
const TURN_BUDGET = 400000
const OMITTED_FILES = 12

mkdirSync(OUT, { recursive: true })

const KEPT_BEFORE = `def _flush_file_changes(slot):
    deduped = {}
    for fc in slot._file_changes:
        deduped[fc["path"]] = fc
    return list(deduped.values())`

const KEPT_AFTER = `def _flush_file_changes(slot):
    deduped = {}
    for write_order, fc in enumerate(slot._file_changes):
        deduped.setdefault(fc["path"], fc)
        deduped[fc["path"]]["_last_write"] = write_order
    fc_list, demoted, dropped = _apply_turn_snapshot_budget(list(deduped.values()))
    return fc_list`

/* The kept row FIRST, exactly as the backend orders them: entries that still
 * carry content sort ahead of the demoted ones. */
const DEMOTED = path => ({
  path,
  before: '',
  after: '',
  truncated: true,
  content_omitted: true,
  turn_budget_chars: TURN_BUDGET,
})
const FILE_CHANGES = [
  { path: 'src/kiro_crew/dashboard/chat_runner.py', before: KEPT_BEFORE, after: KEPT_AFTER },
  DEMOTED('src/kiro_crew/dashboard/chat_utils.py'),
  DEMOTED('website/src/store/chatSlice.ts'),
  DEMOTED('test/test_file_change_snapshots.py'),
]
const DEMOTED_ROW = 'src/kiro_crew/dashboard/chat_utils.py'
const KEPT = FILE_CHANGES[0]
const TURN_SENTENCE = /too large to keep every file's diff/
const OMITTED_NOTICE = /12 more files changed in this turn are not listed\./
/* Words a cold reader has no referent for, and any promise of a way to see a
 * file the turn did not keep; the omitted-files notice may use none of them. */
const JARGON = /snapshot|write|shown|show/i
const OPEN_HINT = /Click a file name to open it/
/* The per-file marker: the card's bordered tag and the pill's muted text share
 * this one string, and the card-level notice never contains it. */
const FILE_ONLY = 'File only'
const OLD_WORDING = /not kept/
const WRONG_NOTICE = /too large to compare/

const t0 = Math.floor(Date.now() / 1000) - 900
const SURFACE = {
  key: 'chat-turn-snapshot-budget',
  title: 'Turn snapshot budget',
  messages: [
    { role: 'user', content: 'Bound what one turn keeps.', ts: String(t0) },
    {
      role: 'assistant',
      ts: String(t0 + 120),
      content: 'Done — the newest change keeps its diff and the older files keep only their path.',
      meta: { file_changes: FILE_CHANGES, file_changes_omitted_files: OMITTED_FILES },
    },
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

const detailByKey = {
  [SURFACE.key]: {
    running: false,
    has_more: false,
    total: SURFACE.messages.length,
    queue: [],
    messages: SURFACE.messages,
  },
}

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

async function main() {
  const { srv, base } = await serveDist()
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  const browser = await chromium.launch({ executablePath })
  const wrote = []

  for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({
      viewport: { width: 1180, height: 900 },
      deviceScaleFactor: 2,
    })
    const page = await context.newPage()

    const extra = async (path, route) => {
      if (path === '/api/chat/slots') return json(route, slots), true
      const m = /^\/api\/chat\/slots\/([^/]+)/.exec(path)
      if (m) {
        const d = detailByKey[decodeURIComponent(m[1])]
        if (d) return json(route, d), true
      }
      if (path === '/api/file-read') return route.fulfill({ status: 200, body: '' }), true
      if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
      return false
    }
    // The boot route decides the palette, so the theme has to reach the STUB;
    // localStorage alone is overwritten by what /api/theme/boot answers.
    await stubDashboardApi(page, { slots, extra, theme })
    logPageProblems(page)

    /* `target` is a locator, or a `{ clip }` rectangle for a frame spanning
     * several sibling elements. */
    async function shot(target, name) {
      const file = `${OUT}/${name}.png`
      if (target.clip) await page.screenshot({ path: file, clip: target.clip })
      else await target.screenshot({ path: file })
      const { w, h } = pngSize(file)
      const flag = w > MAX_EDGE || h > MAX_EDGE ? '  ⚠️ OVER 2000px' : ''
      console.log(`wrote ${file}  ${w}x${h}${flag}`)
      wrote.push({ file, w, h, over: !!flag })
    }

    async function open(fileChipStyle) {
      await page.addInitScript(([key, t, style]) => {
        localStorage.clear()
        localStorage.setItem('mc-theme', t)
        localStorage.setItem('mc-onboarded', '1')
        localStorage.setItem('mc-active-slot-chat', key)
        localStorage.setItem('mc-chat-config', JSON.stringify({
          pinLastPrompt: false,
          fileChipStyle: style,
          streamMode: 'immediate',
        }))
      }, [SURFACE.key, theme, fileChipStyle])

      await page.goto(base + '/?sid=' + encodeURIComponent(SURFACE.key), { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(2600)
      await page.keyboard.press('Escape')
      const close = page.locator('[aria-label="Close"]')
      if (await close.count()) await close.first().click().catch(() => {})
      await page.waitForTimeout(400)
    }

    /* ── expanded card ─────────────────────────────────────────────────── */
    await open('expanded')
    const card = page.locator('div.ft-block-reveal:has([data-testid^="fcc-row-"])').first()
    await card.waitFor({ state: 'visible', timeout: 15000 })

    /* The assertions ARE the capture: a frame is only worth reading if the
     * notice it is meant to show is the one on screen. */
    const rows = page.locator('[data-testid^="fcc-row-"]')
    const rowCount = await rows.count()
    if (rowCount !== FILE_CHANGES.length) {
      throw new Error(`expected ${FILE_CHANGES.length} rows, got ${rowCount}`)
    }
    const firstRow = await rows.first().getAttribute('data-testid')
    if (firstRow !== `fcc-row-${FILE_CHANGES[0].path}`) {
      throw new Error(`expected the kept diff first, got ${firstRow}`)
    }
    const noticeRow = card.locator('[data-fcc-turn-notice]')
    if ((await noticeRow.count()) !== 1) {
      throw new Error(`expected ONE turn-budget notice row per card, got ${await noticeRow.count()}`)
    }
    const sentences = await card.getByText(TURN_SENTENCE).count()
    if (sentences !== 1) {
      throw new Error(`expected the turn-budget sentence once per card, got ${sentences}`)
    }
    if (!(await noticeRow.getByText(OPEN_HINT).count())) {
      throw new Error('expected the notice row to say the files still open')
    }
    const demotedCount = FILE_CHANGES.filter(fc => fc.content_omitted).length
    const tags = await card.locator('[data-fcc-demoted-tag]').count()
    if (tags !== demotedCount) {
      throw new Error(`expected ${demotedCount} demoted-row tags, got ${tags}`)
    }
    for (const fc of FILE_CHANGES.filter(fc => fc.content_omitted)) {
      const row = page.locator(`[data-testid="fcc-row-${fc.path}"]`)
      if (!(await row.getByText(FILE_ONLY).count())) {
        throw new Error(`expected the "${FILE_ONLY}" tag on ${fc.path}`)
      }
      if (await row.getByText(TURN_SENTENCE).count()) {
        throw new Error(`the turn-budget sentence must not repeat on ${fc.path}`)
      }
    }
    if (await card.getByText(WRONG_NOTICE).count()) {
      throw new Error('a demoted row must not claim the FILE was too large to compare')
    }
    const omittedRow = card.locator('[data-fcc-omitted-notice]')
    if ((await omittedRow.count()) !== 1 || !(await omittedRow.getByText(OMITTED_NOTICE).count())) {
      throw new Error(`expected ONE omitted-files notice row per card, got ${await omittedRow.count()}`)
    }
    if (JARGON.test((await omittedRow.textContent()) ?? '')) {
      throw new Error('the omitted-files notice must not say snapshot, write or shown')
    }
    if ((await omittedRow.textContent())?.includes(FILE_ONLY) || (await card.getByText(FILE_ONLY).count()) !== demotedCount) {
      throw new Error(`"${FILE_ONLY}" belongs to the ${demotedCount} demoted rows and nowhere else on the card`)
    }
    if (await card.getByText(OLD_WORDING).count()) {
      throw new Error('the card must not describe either state as not kept')
    }
    for (const fc of FILE_CHANGES.filter(fc => fc.content_omitted)) {
      const tagTitle = await page.locator(`[data-testid="fcc-row-${fc.path}"] [data-fcc-demoted-tag]`).getAttribute('title')
      if (!TURN_SENTENCE.test(tagTitle ?? '')) {
        throw new Error(`expected the demoted tag on ${fc.path} to restate the turn-budget reason in its title`)
      }
    }
    console.log(`${theme}: ${rowCount} rows, kept diff first, one turn-budget row, one omitted-files row, ${tags} titled tags`)

    await shot(card, `01-card-mixed-${theme}`)
    /* The two notice rows are adjacent siblings under the header; one frame
     * holds both so their wording is read together at full scale. */
    const adjacent = await noticeRow.evaluate(el => el.nextElementSibling?.hasAttribute('data-fcc-omitted-notice'))
    if (!adjacent) throw new Error('expected the omitted-files row directly under the turn-budget row')
    const top = await noticeRow.boundingBox()
    const bottom = await omittedRow.boundingBox()
    await shot({ clip: { x: top.x, y: top.y, width: top.width, height: bottom.y + bottom.height - top.y } }, `02-row-notice-${theme}`)
    await shot(page.locator(`[data-testid="fcc-row-${DEMOTED_ROW}"]`), `04-demoted-row-${theme}`)

    /* ── minimal pills ─────────────────────────────────────────────────── */
    await open('minimal')
    const pill = page.locator(`button.file-chip[aria-label="${DEMOTED_ROW}"]`)
    await pill.waitFor({ state: 'visible', timeout: 15000 })
    const pills = page.locator('button.file-chip')
    const pillCount = await pills.count()
    if (pillCount !== FILE_CHANGES.length) {
      throw new Error(`expected ${FILE_CHANGES.length} pills, got ${pillCount}`)
    }
    if (await pill.getByText(WRONG_NOTICE).count()) {
      throw new Error('a demoted pill must not claim the FILE was too large to compare')
    }
    const pillRow = page.locator('div.ft-block-reveal:has(button.file-chip)').first()
    if (await pillRow.locator('[data-fcc-demoted-tag]').count()) {
      throw new Error('the bordered row tag belongs to the expanded card, not to a pill')
    }
    /* The sentence is about the turn, so the row says it ONCE and no pill
     * repeats it; a demoted pill's face is its filename and the short marker. */
    const pillTurnNotice = pillRow.locator('[data-fcc-turn-notice]')
    if ((await pillTurnNotice.count()) !== 1 || !(await pillTurnNotice.getByText(TURN_SENTENCE).count())) {
      throw new Error(`expected ONE turn-budget line after the pills, got ${await pillTurnNotice.count()}`)
    }
    if (!(await pillTurnNotice.getByText(OPEN_HINT).count())) {
      throw new Error('expected the pill row\'s turn-budget line to say the files still open')
    }
    if ((await pillRow.getByText(TURN_SENTENCE).count()) !== 1) {
      throw new Error('the turn-budget sentence must appear once in the pill row and in no pill')
    }
    /* A demoted pill's face is its filename plus the muted marker, so it can
     * never pass for a healthy pill; the kept pill carries no marker. */
    for (const fc of FILE_CHANGES.filter(fc => fc.content_omitted)) {
      const name = fc.path.split('/').pop()
      const own = page.locator(`button.file-chip[aria-label="${fc.path}"]`)
      if ((await own.locator('[data-fcc-pill-filename]').count()) !== 1 || (await own.textContent()) !== `${name}${FILE_ONLY}`) {
        throw new Error(`expected the pill for ${fc.path} to show ${name} and the "${FILE_ONLY}" marker, got ${JSON.stringify(await own.textContent())}`)
      }
      const marker = own.locator('[data-fcc-pill-marker]')
      if ((await marker.count()) !== 1 || !(await marker.getByText(FILE_ONLY).count())) {
        throw new Error(`expected ONE "${FILE_ONLY}" marker on the pill for ${fc.path}`)
      }
      if (!/\btext-muted\b/.test((await marker.getAttribute('class')) ?? '')) {
        throw new Error(`expected the marker on ${fc.path} to be muted`)
      }
      if (!/\btext-left\b/.test((await own.getAttribute('class')) ?? '')) {
        throw new Error(`expected the pill for ${fc.path} to be left-aligned`)
      }
    }
    /* In a row where the demoted pills name their files, the kept file's stats
     * pill names its own too — once, in its face, ahead of its numbers — so no
     * pill in the row leaves the reader guessing which file it stands for. */
    const keptPill = page.locator(`button.file-chip[aria-label="${KEPT.path}"]`)
    const keptName = KEPT.path.split('/').pop()
    if ((await keptPill.locator('[data-fcc-pill-filename]').count()) !== 1 || !((await keptPill.textContent()) ?? '').startsWith(keptName)) {
      throw new Error(`expected the kept pill to name ${keptName} ahead of its stats, got ${JSON.stringify(await keptPill.textContent())}`)
    }
    if (!/[+-]\d/.test((await keptPill.textContent()) ?? '')) {
      throw new Error('expected the kept pill to keep its line counts beside the filename')
    }
    if (await keptPill.locator('[data-fcc-pill-marker]').count()) {
      throw new Error('a pill that is not demoted must carry no marker')
    }
    if ((await pillRow.getByText(FILE_ONLY).count()) !== demotedCount) {
      throw new Error(`expected "${FILE_ONLY}" once per demoted pill (${demotedCount}), got ${await pillRow.getByText(FILE_ONLY).count()}`)
    }
    if (await pillRow.getByText(OLD_WORDING).count()) {
      throw new Error('the pill row must not describe either state as not kept')
    }
    const pillNotice = pillRow.locator('[data-fcc-omitted-notice]')
    if ((await pillNotice.count()) !== 1 || !(await pillNotice.getByText(OMITTED_NOTICE).count())) {
      throw new Error(`expected ONE omitted-files notice after the pills, got ${await pillNotice.count()}`)
    }
    if (JARGON.test((await pillNotice.textContent()) ?? '')) {
      throw new Error('the omitted-files notice must not say snapshot, write or shown')
    }
    console.log(`${theme}: ${pillCount} pills, kept pill named, each demoted pill shows its file plus the muted marker, one turn-budget line, one omitted-files notice`)
    await shot(pillRow, `03-minimal-chip-${theme}`)
    /* The kept pill and the first demoted pill are adjacent siblings; one
     * clip holds both so the marker is read against a healthy neighbour. */
    const keptBox = await keptPill.boundingBox()
    const demotedBox = await pill.boundingBox()
    const x = Math.min(keptBox.x, demotedBox.x)
    const y = Math.min(keptBox.y, demotedBox.y)
    await shot({ clip: {
      x, y,
      width: Math.max(keptBox.x + keptBox.width, demotedBox.x + demotedBox.width) - x,
      height: Math.max(keptBox.y + keptBox.height, demotedBox.y + demotedBox.height) - y,
    } }, `05-minimal-demoted-pill-${theme}`)

    await context.close()
  }

  await browser.close()
  srv.close()

  const over = wrote.filter(x => x.over)
  if (over.length) throw new Error('frames over the 2000px budget: ' + over.map(x => x.file).join(', '))
  console.log(`done — ${wrote.length} frames in ${OUT}`)
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
