/**
 * Capture + regression harness: "where did my session go?" after a drag.
 *
 * Dragging a session onto a folder used to be silent — the row left the list and
 * nothing said where it landed, so a mis-aimed drop was only findable by opening
 * folders one at a time. This drives the REAL built SPA (website/dist) with
 * /api/** stubbed and REAL pointer events (dnd-kit's sensors are pointer-based,
 * so there is no synthetic shortcut), then captures the confirmation bar.
 *
 * It asserts what the screenshots are supposed to evidence, and exits non-zero
 * when any of it stops being true:
 *   1. Before the drag there is no bar.
 *   2. After the drop the bar names the DESTINATION folder.
 *   3. The bar sits fully ABOVE the "Older Sessions" footer — geometry, not DOM
 *      order — because the whole reason it renders in the flow is that it must
 *      not cover that persistent control or the row that just moved.
 *   4. Undo (⌘Z / Ctrl+Z, driven from the keyboard here) puts the session back
 *      in the list and retires the bar.
 *
 * Usage: node scripts/capture-session-move-undo.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-move-undo'
const VIEW = { width: 1500, height: 950 }
const ARCHIVE = 'folder-archive'
const DRAGGED = 'chat-drag-me'

mkdirSync(OUT, { recursive: true })

const iso = min => new Date(Date.now() - min * 60_000).toISOString()
const slot = (key, title, min, folder = '') => ({
  key, title, running: false, messages: 6, agent: 'kirocrew',
  memory_mode: 'persistent', folder_id: folder, last_ts: iso(min),
  last_turn_ts: iso(min), created: iso(min + 200),
})

const FOLDERS = [{ id: ARCHIVE, name: 'Archive', order: 0, collapsed: false }]
const SLOTS = [
  slot(DRAGGED, 'Session drag lands in the wrong folder', 3),
  slot('chat-2', 'Session move undo bar', 12),
  slot('chat-3', 'i18n catalog parity', 26),
]

function assert(label, ok, detail = '') {
  console.log(`${label}: ${ok ? 'OK' : 'FAIL'}${detail ? ` — ${detail}` : ''}`)
  if (!ok) throw new Error(`${label} failed${detail ? `: ${detail}` : ''}`)
}

const { srv, base } = await serveDist()
// A mise-managed node exports its own lib/node on LD_LIBRARY_PATH, which the
// browser child inherits and then dies resolving libstdc++; point it at the
// system path (harmless otherwise). Same note as the sibling harnesses.
const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' } })
/** Open the dashboard on the given theme with the fixtures above. */
async function open(theme, sidebarWidth = 0) {
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    folders: FOLDERS,
    slots: SLOTS,
    theme,
    // The move itself: answer the PATCH so the optimistic update is never
    // rolled back. The fixture list is not mutated — with the websocket
    // swallowed nothing refetches, so Redux is the only writer.
    extra: async (path, route) => {
      if (/\/api\/chat\/slots\/.+\/folder$/.test(path)) { await json(route, {}); return true }
      return false
    },
  })
  // The sidebar reads its own width from localStorage on mount, so seeding it
  // before navigation is how a narrow sidebar is reproduced without dragging.
  if (sidebarWidth) {
    await page.addInitScript(w => localStorage.setItem('mc-sidebar-width', String(w)), sidebarWidth)
  }
  await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  return { context, page }
}

/** Real dnd-kit drag of the session row onto the Archive folder header. Multi-step
 *  moves are required, not cosmetic: the sensor only activates past a 5px distance
 *  constraint, and collision detection reads pointer coordinates on each move. */
async function dragOntoArchive(page, onStep = null) {
  const from = await page.locator(`[data-slot-key="${DRAGGED}"]`).first().boundingBox()
  const to = await page.locator(`[data-folder-drop="${ARCHIVE}"]`).first().boundingBox()
  const sx = from.x + from.width / 2
  const sy = from.y + from.height / 2
  const tx = to.x + to.width / 2
  const ty = to.y + to.height / 2
  await page.mouse.move(sx, sy)
  await page.mouse.down()
  await page.mouse.move(sx + 8, sy + 4, { steps: 4 })
  await page.waitForTimeout(150)
  for (let i = 1; i <= 12; i++) {
    await page.mouse.move(sx + ((tx - sx) * i) / 12, sy + ((ty - sy) * i) / 12)
    await page.waitForTimeout(35)
    if (onStep && i % 3 === 0) await onStep()
  }
  await page.waitForTimeout(200)
  await page.mouse.up()
}

try {
  const { context, page } = await open('light')
  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  const bar = page.getByTestId('session-move-undo')
  const footer = page.locator('[aria-label="Older sessions"]').first()
  const row = page.locator(`[data-slot-key="${DRAGGED}"]`).first()

  await row.waitFor({ timeout: 15_000 })
  assert('1 before: no undo bar', (await bar.count()) === 0)
  await shot('1-before-drag')

  await dragOntoArchive(page)

  await bar.waitFor({ timeout: 5_000 })
  const barText = (await bar.innerText()).replace(/\s+/g, ' ').trim()
  assert('2 after drop: bar names the destination', /Archive/.test(barText), barText)
  // The move really happened: the row now lives INSIDE the Archive block.
  const inFolder = () => page.locator(`[data-folder-drop="${ARCHIVE}"] [data-slot-key="${DRAGGED}"]`).count()
  assert('2 after drop: session is inside Archive', (await inFolder()) === 1)

  const barBox = await bar.boundingBox()
  const footerBox = await footer.boundingBox()
  assert(
    '3 placement: bar sits fully above the Older Sessions footer',
    barBox.y + barBox.height <= footerBox.y + 1,
    `bar bottom ${Math.round(barBox.y + barBox.height)} vs footer top ${Math.round(footerBox.y)}`,
  )
  await shot('2-after-drop')

  // CLICKING Undo is the primary path, so it is the one asserted here: the write
  // must actually happen (session leaves Archive) AND the bar must go away.
  // Verified in the real browser, not only in jsdom.
  const undoButton = page.getByTestId('session-move-undo-button')
  assert('4 undo: button reads "Undo" and nothing else',
    (await undoButton.innerText()).trim() === 'Undo', await undoButton.innerText())
  await undoButton.click()
  await page.waitForTimeout(600)
  assert('4 undo (click): bar retired', (await bar.count()) === 0)
  assert('4 undo (click): session left Archive again', (await inFolder()) === 0)
  assert('4 undo (click): session still in the list', (await row.count()) === 1)
  await shot('3-after-undo')

  // The chord is unlabelled but still live: drag again and undo from the keyboard.
  await dragOntoArchive(page)
  await bar.waitFor({ timeout: 5_000 })
  assert('4b undo (chord): armed again', (await inFolder()) === 1)
  await page.keyboard.press('Control+z')
  await page.waitForTimeout(600)
  assert('4b undo (chord): bar retired', (await bar.count()) === 0)
  assert('4b undo (chord): session left Archive again', (await inFolder()) === 0)

  // Hovering the bar suspends the deadline, so the countdown must stop too — a
  // bar that drains to empty while Undo is guaranteed alive tells the user the
  // opposite of the truth, and it tells it to the slow reader the suspension
  // exists for. Read the real transform: this is the one assertion that a
  // jsdom test cannot make, because framer's animation never runs there.
  await dragOntoArchive(page)
  await bar.waitFor({ timeout: 5_000 })
  const countdown = page.getByTestId('session-move-undo-countdown')
  const scaleX = async () => {
    const m = await countdown.evaluate(el => getComputedStyle(el).transform)
    const nums = m.match(/matrix\(([^)]+)\)/)
    return nums ? parseFloat(nums[1].split(',')[0]) : 1
  }
  await page.waitForTimeout(1_200)
  const running = await scaleX()
  assert('7 hover: countdown has started draining', running < 0.95, running.toFixed(3))
  await bar.hover()
  await page.waitForTimeout(200)
  const frozen = await scaleX()
  await page.waitForTimeout(1_500)
  const stillFrozen = await scaleX()
  assert('7 hover: countdown freezes instead of draining',
    Math.abs(stillFrozen - frozen) < 0.02, `${frozen.toFixed(3)} → ${stillFrozen.toFixed(3)}`)
  assert('7 hover: offer is still alive while held', (await bar.count()) === 1)
  // …and releasing does NOT hand out a fresh full window: what is left is the
  // remainder, so the width picks up from where it froze rather than restarting.
  await page.mouse.move(10, 10)
  await page.waitForTimeout(300)
  const resumed = await scaleX()
  assert('7 release: resumes from the frozen width, not a full bar',
    resumed <= frozen + 0.02, `${frozen.toFixed(3)} → ${resumed.toFixed(3)}`)
  await context.close()

  // Flow frames for the committed GIF. What this bar is worth is a SEQUENCE —
  // drop, named destination, countdown draining, undo putting the session back —
  // and a still frame can prove none of that, so the PR carries a GIF too.
  {
    const flow = await open('light')
    const p = flow.page
    await p.locator(`[data-slot-key="${DRAGGED}"]`).first().waitFor({ timeout: 15_000 })
    const sidebar = await p.locator('.sidebar-inner').first().boundingBox()
    // The WHOLE sidebar: the frames have to show the row being dragged out of
    // the list at the top AND the bar appearing at the bottom — a bottom-only
    // crop turns the drag half of the sequence into empty space.
    const clip = {
      x: Math.round(sidebar.x),
      y: Math.round(sidebar.y),
      width: Math.round(sidebar.width),
      height: Math.round(sidebar.height),
    }
    // Frames land in the OS temp dir, NOT under `${OUT}`: they are the GIF's raw
    // material, and a `frames/` subdirectory inside the committed screenshot dir
    // would ride along in the PR with no consumer.
    const FRAMES = join(tmpdir(), 'kc-session-move-undo-frames')
    mkdirSync(FRAMES, { recursive: true })
    let n = 0
    const frame = async () => {
      await p.screenshot({ path: join(FRAMES, `${String(++n).padStart(2, '0')}.png`), clip })
    }
    await frame()
    await dragOntoArchive(p, frame)
    await p.getByTestId('session-move-undo').waitFor({ timeout: 5_000 })
    for (let i = 0; i < 7; i++) { await frame(); await p.waitForTimeout(500) }
    await p.keyboard.press('Control+z')
    await p.waitForTimeout(250)
    await frame()
    await p.waitForTimeout(400)
    await frame()
    await flow.context.close()
    console.log(`wrote ${n} flow frames to ${FRAMES}`)
    console.log('assemble the committed GIF with:')
    console.log(`  python3 -c "from PIL import Image; import glob; fs=[Image.open(f) for f in sorted(glob.glob('${FRAMES}/*.png'))]; fs[0].save('${OUT}/undo-flow.gif', save_all=True, append_images=fs[1:], duration=550, loop=0)"`)
  }

  // Narrow sidebar at SIDEBAR_MIN (180px): the destination must survive, so the
  // "Moved to" prefix and the ⌘Z hint are dropped instead of the folder name.
  {
    const narrow = await open('light', 180)
    await narrow.page.locator(`[data-slot-key="${DRAGGED}"]`).first().waitFor({ timeout: 15_000 })
    await dragOntoArchive(narrow.page)
    const narrowBar = narrow.page.getByTestId('session-move-undo')
    await narrowBar.waitFor({ timeout: 5_000 })
    const text = (await narrowBar.innerText()).replace(/\s+/g, ' ').trim()
    assert('6 narrow: destination survives at 180px', /Archive/.test(text), text)
    assert('6 narrow: prefix dropped', !/Moved to/.test(text), text)
    // The folder name is not merely PRESENT but rendered: a truncated-to-nothing
    // span still holds its text, so measure the box the reviewer would look at.
    const nameBox = await narrow.page.getByTestId('session-move-undo').locator('span', { hasText: 'Archive' }).last().boundingBox()
    assert('6 narrow: destination has real width', nameBox.width >= 40, `${Math.round(nameBox.width)}px`)
    await narrow.page.screenshot({ path: `${OUT}/5-narrow-180.png` })
    console.log('wrote', `${OUT}/5-narrow-180.png`)
    await narrow.context.close()
  }

  // Dark theme: every surface in the bar is a theme token (accent-subtle band,
  // accent countdown, text-strong destination), so this frame is the proof that
  // none of it was a light-mode-only literal.
  {
    const dark = await open('dark')
    await dark.page.locator(`[data-slot-key="${DRAGGED}"]`).first().waitFor({ timeout: 15_000 })
    await dragOntoArchive(dark.page)
    const darkBar = dark.page.getByTestId('session-move-undo')
    await darkBar.waitFor({ timeout: 5_000 })
    assert('5 dark: bar names the destination', /Archive/.test(await darkBar.innerText()))
    await dark.page.screenshot({ path: `${OUT}/4-after-drop-dark.png` })
    console.log('wrote', `${OUT}/4-after-drop-dark.png`)
    await dark.context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
console.log('captures written to', OUT)
