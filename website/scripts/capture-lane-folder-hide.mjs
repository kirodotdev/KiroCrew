/**
 * Screenshots, and an assertion per frame, for the folder hide in every sidebar lane.
 *
 * Sixteen frames: each of the five renderers that draw session rows, photographed with
 * nothing hidden, with a root folder unchecked, and with a NESTED folder unchecked. The
 * before frame is the control -- a lane that drew nothing at all would satisfy "the
 * hidden row is gone" on its own, so a reader needs to see the row was there first.
 *
 * This ASSERTS as well as photographs, and the assertions are what make the frames
 * usable as evidence. The unit pin already proves which rows are in the DOM, in jsdom,
 * with framer-motion mocked out; a DOM row is not the same as a row a person can SEE,
 * and the difference is exactly how a screenshot comes to show a folder header over a
 * blank body. So every frame waits for the rows it expects to be painted (a real box and
 * full opacity), checks the rows present by key, checks the pinned theme, and the run
 * exits non-zero on any mismatch.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort      # in another shell
 *   node scripts/capture-lane-folder-hide.mjs http://127.0.0.1:6841 [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/13775-lane-folder-hide'
mkdirSync(OUT, { recursive: true })

const ROOT_HIDE = 'folder-hidden'
const NESTED_HIDE = 'folder-nested'
/** Which row each hide is expected to take away, keyed by the folder unchecked. */
const CONCEALS = { '': null, [ROOT_HIDE]: 'k-hidden-conductor', [NESTED_HIDE]: 'k-nested' }
const ALL_KEYS = ['k-hidden-conductor', 'k-shown-child', 'k-shown-plain', 'k-nested']
/** Three folders, answered over the real `/api/chat/folders` read. */
const FOLDERS = [
  { id: ROOT_HIDE, name: 'hidden folder', collapsed: false, order: 0 },
  { id: 'folder-shown', name: 'shown folder', collapsed: false, order: 1 },
  { id: NESTED_HIDE, name: 'nested folder', collapsed: false, order: 2, parent_id: 'folder-shown' },
]
/** One state-sourced column, which is what puts the board axis in front of the union. */
const COLUMNS = [{ id: 'col-idle', name: '', tag_ids: [], mode: 'any', order: 0, source: 'state', state_key: 'idle' }]

let failed = false
const check = (label, ok, detail) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` -- ${detail}` : ''}`)
  if (!ok) failed = true
}

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
const context = await browser.newContext({ viewport: { width: 560, height: 620 }, deviceScaleFactor: 2 })
const page = await context.newPage()
page.on('pageerror', e => { console.log(`FAIL pageerror -- ${e.message}`); failed = true })

// Which lane the next navigation is for: the board lane is the only one that needs a
// column, and handing every lane one would preempt the three that are not it.
let wantColumns = false
/**
 * Which mode the NEXT navigation's theme boot answers.
 *
 * The boot read is the source of truth for the theme -- localStorage is a render cache the
 * effect overwrites -- so pinning the stub to one mode pins every frame to it whatever the
 * page seeds. A frame that must be photographed in both themes therefore has to move this,
 * not just the query string.
 */
let wantTheme = 'dark'
await stubDashboardApi(page, {
  theme: 'dark',
  folders: FOLDERS,
  // Running against the DEV server, `**/api/**` also matches `/src/api/client.ts`;
  // answering that with JSON serves the browser JSON where it requires JavaScript and
  // the page never mounts. Source paths pass through, gateway paths do not.
  extra: async (path, route) => {
    if (path === '/api/theme/boot') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ mode: wantTheme, theme: '' }) })
      return true
    }
    if (path === '/api/chat/tag-columns') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(wantColumns ? COLUMNS : []) })
      return true
    }
    if (path.startsWith('/api/')) return false
    await route.continue()
    return true
  },
})
logPageProblems(page)

/** Session row keys in rendered document order. */
const renderedKeys = () => page.$$eval('[data-slot-key]', els => [...new Set(els.map(el => el.getAttribute('data-slot-key')))])

/**
 * Wait until every named row is genuinely PAINTED: a non-zero box, full opacity on the
 * row and each of its ancestors, and its title text present. A row inside a collapsed
 * group or mid-animation satisfies a selector wait and photographs as empty space, which
 * is the one failure a screenshot cannot show you it made.
 */
async function paintedRows(keys) {
  const deadline = Date.now() + 8000
  let last = null
  while (Date.now() < deadline) {
    last = await page.evaluate(expected => expected.map(k => {
      const el = document.querySelector(`[data-slot-key="${k}"]`)
      if (!el) return { k, why: 'absent' }
      const box = el.getBoundingClientRect()
      if (box.width < 2 || box.height < 2) return { k, why: `box ${Math.round(box.width)}x${Math.round(box.height)}` }
      for (let n = el; n && n !== document.body; n = n.parentElement) {
        const o = Number(getComputedStyle(n).opacity)
        if (Number.isFinite(o) && o < 0.99) return { k, why: `opacity ${o}` }
        if (getComputedStyle(n).visibility === 'hidden') return { k, why: 'visibility hidden' }
      }
      if (!(el.textContent || '').trim()) return { k, why: 'no text' }
      return null
    }).filter(Boolean), keys)
    if (last.length === 0) return null
    await page.waitForTimeout(200)
  }
  return last
}

/**
 * The colour theme is applied asynchronously after mount, so a frame taken too early
 * carries a different theme than its siblings. Wait for `data-theme` to stop moving
 * rather than sleep, so a slow runner does not quietly go back to catching it early.
 */
async function settledTheme() {
  let prev = null
  for (let i = 0; i < 20; i++) {
    const now = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    if (now && now === prev) return now
    prev = now
    await page.waitForTimeout(200)
  }
  return prev
}

/**
 * One frame. `hide` decides both the fixture seed and what is asserted, because the two
 * must never drift apart: a frame named for a hide that photographs the unhidden lane is
 * exactly the evidence this harness exists to make impossible.
 */
async function frame(lane, hide, name, theme = 'dark') {
  wantColumns = lane.startsWith('board')
  wantTheme = theme
  const concealed = CONCEALS[hide]
  const expected = ALL_KEYS.filter(k => k !== concealed)
  await page.goto(`${BASE}/capture/lane-folder-hide.html?lane=${lane}&hide=${hide}&theme=${theme}`)
  await page.waitForSelector('[data-capture-ready]')
  await page.waitForSelector('[data-slot-key]')
  const settled = await settledTheme()
  const label = `${name}`
  check(`${label}: pinned Kiro ${theme}`, settled === `kiro-${theme}`, String(settled))

  const unpainted = await paintedRows(expected)
  check(`${label}: every row a reader must see is painted`, unpainted === null,
    unpainted ? unpainted.map(u => `${u.k} ${u.why}`).join(', ') : `${expected.length} rows`)

  const keys = await renderedKeys()
  for (const k of expected) check(`${label}: ${k} renders`, keys.includes(k), keys.join(' '))
  if (concealed) check(`${label}: ${concealed} is gone`, !keys.includes(concealed), keys.join(' '))

  await page.locator('[data-capture-ready]').screenshot({ path: `${OUT}/${name}.png` })
  console.log(`     ${name}.png`)
}

let n = 1
const pad = () => String(n++).padStart(2, '0')
for (const lane of ['conductor', 'board', 'board-flat', 'tree', 'flat']) {
  await frame(lane, '', `${pad()}-${lane}-before`)
  await frame(lane, ROOT_HIDE, `${pad()}-${lane}-root-hidden`)
  await frame(lane, NESTED_HIDE, `${pad()}-${lane}-nested-hidden`)
}

/**
 * The undo, photographed where it was previously unreachable.
 *
 * A board column draws no folder header, so it carries no reveal row: this menu IS the
 * way back from a hide made in any view, and until the gate came off it was not rendered
 * while a board was active. A frame of fewer rows cannot show that the control exists, so
 * this one opens the real menu over a board and asserts the three parts a person needs.
 */
{
  wantColumns = true
  wantTheme = 'dark'
  const name = `${pad()}-board-filter-menu-open`
  // The menu is taller than the sidebar's own frame: FILTER and SORT BY come first and
  // the Folders section is last on purpose (it grows with the folder count). A frame at
  // the standard height cuts it off, and a cut-off control is not evidence the control
  // is reachable -- so this one frame gets a window tall enough to hold the whole menu.
  await page.setViewportSize({ width: 560, height: 1240 })
  await page.goto(`${BASE}/capture/lane-folder-hide.html?lane=board&hide=${ROOT_HIDE}&theme=dark`)
  await page.waitForSelector('[data-capture-ready]')
  await page.waitForSelector('[data-slot-key]')
  const theme = await settledTheme()
  check(`${name}: pinned Kiro dark`, theme === 'kiro-dark', String(theme))
  const funnel = page.locator('[data-folder-hide-active]')
  check(`${name}: the funnel is marked while the hide withholds rows`, await funnel.count() === 1,
    `${await funnel.count()} marked button(s)`)
  await funnel.click()
  for (const [what, sel] of [
    ['the Folders heading with its hidden count', '[data-testid="folder-filter-shelve"]'],
    ['Show all folders', '[data-testid="folder-filter-show-all"]'],
    [`the ${ROOT_HIDE} checkbox`, `[data-testid="folder-filter-${ROOT_HIDE}"]`],
  ]) {
    const loc = page.locator(sel)
    const found = await loc.count().catch(() => 0)
    check(`${name}: ${what} is reachable over a board`, found > 0, `${found} match(es)`)
    // Present is not the same as photographed: the menu can overflow the window and cut
    // the very rows this frame exists to show, and a cut-off control proves nothing.
    const box = found > 0 ? await loc.first().boundingBox() : null
    const inFrame = box != null && box.y >= 0 && box.y + box.height <= 1240
    check(`${name}: ${what} is inside the frame`, inFrame,
      box ? `y=${Math.round(box.y)} h=${Math.round(box.height)}` : 'no box')
  }
  // Radix's popup fades and scales in, so a frame taken the moment the items are
  // queryable catches it translucent over the rows behind it.
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log(`     ${name}.png`)
}

/**
 * The count, on screen, in both themes and in both grammatical numbers.
 *
 * A board column draws no folder header, so the announcement the other three lanes hang
 * off a container sits at the LANE level here. These frames are what show it is CONTENT:
 * the assertions read the row's rendered text rather than an attribute, because a count
 * that lives in a hover `title` is unreachable on a touch device and unspoken by a screen
 * reader, which is the gap the tint alone left.
 *
 * Both themes, because the row is tinted from `--warn` / `--warn-subtle`: a literal colour
 * would be legible in whichever theme it was chosen against and wrong in the other.
 */
for (const theme of ['dark', 'light']) {
  for (const [hide, number, expected] of [
    [ROOT_HIDE, 'singular', /1 hidden folder(?!s)/],
    [`${ROOT_HIDE},${NESTED_HIDE}`, 'plural', /2 hidden folders/],
  ]) {
    wantColumns = true
    wantTheme = theme
    const name = `${pad()}-board-hidden-count-${number}-${theme}`
    await page.setViewportSize({ width: 560, height: 620 })
    await page.goto(`${BASE}/capture/lane-folder-hide.html?lane=board&hide=${hide}&theme=${theme}`)
    await page.waitForSelector('[data-capture-ready]')
    const settled = await settledTheme()
    check(`${name}: pinned Kiro ${theme}`, settled === `kiro-${theme}`, String(settled))

    const row = page.locator('[data-testid="board-hidden-folders"]')
    const found = await row.count()
    check(`${name}: the lane draws a hidden-folder row`, found === 1, `${found} row(s)`)
    if (found === 1) {
      // Painted, not merely present: a row at zero height or full transparency satisfies
      // a selector wait and photographs as empty space above the columns.
      const box = await row.boundingBox()
      check(`${name}: the row is painted`, box != null && box.width > 2 && box.height > 2,
        box ? `${Math.round(box.width)}x${Math.round(box.height)}` : 'no box')
      const text = (await row.textContent() || '').trim()
      check(`${name}: the count is in the row's own text`, expected.test(text), JSON.stringify(text))
      // The ACTION, on screen too. A count plus a glyph leaves a sighted reader with no
      // pointer to guess the row is tappable -- the same hover-only gap as the count.
      const action = (await row.locator('[data-testid="board-hidden-folders-action"]').textContent() || '').trim()
      check(`${name}: the way back is a visible word, not only a name`, action.length > 0, JSON.stringify(action))
      const actionBox = await row.locator('[data-testid="board-hidden-folders-action"]').boundingBox()
      check(`${name}: that word is painted`, actionBox != null && actionBox.width > 2 && actionBox.height > 2,
        actionBox ? `${Math.round(actionBox.width)}x${Math.round(actionBox.height)}` : 'no box')
      const label = await row.getAttribute('aria-label')
      check(`${name}: the row is named for a screen reader`, !!label && expected.test(label), String(label))
      // The funnel must agree with it: two numbers for one hide, side by side, is the
      // defect a single derived count exists to prevent.
      const marked = await page.locator('[data-folder-hide-active]').getAttribute('data-folder-hide-active')
      check(`${name}: the funnel reports the same number`,
        marked === (number === 'plural' ? '2' : '1'), String(marked))
    }
    await page.locator('[data-capture-ready]').screenshot({ path: `${OUT}/${name}.png` })
    console.log(`     ${name}.png`)
  }
}

/**
 * The citation glyph, named.
 *
 * A concealed conductor with a visible child puts a bent arrow on that child's row: the
 * creator is open and running, the lane simply is not nesting under a row it cannot draw.
 * The arrow alone is a shape a reader reported as meaningless, so the frame's assertions
 * are about its NAME -- carried on an element whose role admits one, with the drawing
 * inside marked decorative so the name is not announced twice.
 */
{
  wantColumns = false
  wantTheme = 'dark'
  const name = `${pad()}-conductor-citation-named`
  await page.setViewportSize({ width: 560, height: 620 })
  await page.goto(`${BASE}/capture/lane-folder-hide.html?lane=conductor&hide=${ROOT_HIDE}&theme=dark`)
  await page.waitForSelector('[data-capture-ready]')
  await page.waitForSelector('[data-slot-key]')
  const settled = await settledTheme()
  check(`${name}: pinned Kiro dark`, settled === 'kiro-dark', String(settled))

  const glyph = page.locator('[data-cites-parent]')
  const found = await glyph.count()
  check(`${name}: a row carries an open-creator citation`, found >= 1, `${found} glyph(s)`)
  if (found >= 1) {
    const first = glyph.first()
    check(`${name}: the name sits on a role that admits one`,
      await first.getAttribute('role') === 'img', String(await first.getAttribute('role')))
    const label = await first.getAttribute('aria-label') || ''
    check(`${name}: the name says who opened this session`, /Opened by/i.test(label), JSON.stringify(label))
    check(`${name}: the name identifies the creator`, label.includes('k-hidden-conductor'), JSON.stringify(label))
    const svgHidden = await first.locator('svg').first().getAttribute('aria-hidden')
    check(`${name}: the drawing itself is decorative`, svgHidden === 'true', String(svgHidden))
  }
  await page.locator('[data-capture-ready]').screenshot({ path: `${OUT}/${name}.png` })
  console.log(`     ${name}.png`)
}

await context.close()
await browser.close()
console.log(failed ? 'FAILED' : 'all frames ok')
process.exit(failed ? 1 : 0)
