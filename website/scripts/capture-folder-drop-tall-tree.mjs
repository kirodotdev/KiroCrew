/**
 * Self-checking capture harness: can a chat session be drag-and-dropped INTO a
 * parent folder whose expanded block is TALLER than the sidebar viewport?
 *
 * Pins the two collision inversions fixed in sidebarCollision (see
 * dndCollisionDepth.test.tsx for the unit-level pins): the root lane's
 * viewport-sized box out-ranking the tall folder under pointerWithin, and the
 * closestCenter near-miss fallback preferring a small subfolder. Run against
 * origin/main for the BEFORE frames and against the fix for AFTER — same
 * fixtures, same gestures, the only variable is the collision strategy.
 *
 * Default fixture: minimal nested tree (scenarios A–G; G pins that deliberate
 * un-filing on the empty lane area still works). REPRO_REAL=1 mirrors the
 * reporting user's sidebar (~170 sessions, a 2000px expanded parent) —
 * scenarios R1/R2, which FAIL before the fix. REPRO_DIST=<path> serves a
 * different built dist (e.g. an installed bundle).
 *
 * Seeds: parent folder "Parent" (f1, expanded) containing subfolder "Child"
 * (f1a, expanded). Session s-child lives in the Child subfolder; s-parent
 * lives directly in Parent; s-root is ungrouped.
 *
 * For each scenario we simulate a REAL pointer drag (mouse.down, stepped
 * moves, mouse.up) and record every PATCH /api/chat/slots/:key/folder call
 * the SPA fires, printing PASS/FAIL per scenario.
 *
 * Usage: node scripts/capture-folder-drop-tall-tree.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || (process.env.KIROCREW_SCRATCH || '/tmp') + '/repro-drop-parent'
mkdirSync(OUT, { recursive: true })

const REAL = process.env.REPRO_REAL === '1'

// REAL mode mirrors the reporting user's actual sidebar shape: an EXPANDED
// parent ("Kiro") holding ~37 direct sessions plus four subfolders (two
// expanded), sibling collapsed folders, and enough total rows (~170) that the
// list scrolls several viewports.
const realFolders = [
  { id: 'kiro', name: 'Kiro', order: 1, collapsed: false },
  { id: 'docw', name: 'doc writing', order: 8, collapsed: false, parent_id: 'kiro' },
  { id: 'kas', name: 'KAS', order: 11, collapsed: false, parent_id: 'kiro' },
  { id: 'research', name: 'research', order: 12, collapsed: true, parent_id: 'kiro' },
  { id: 'appstore', name: 'App Store', order: 13, collapsed: true, parent_id: 'kiro' },
  { id: 'mesh', name: 'MeshClaw', order: 2, collapsed: true },
  { id: 'meshsub1', name: 'cr review', order: 3, collapsed: true, parent_id: 'mesh' },
  { id: 'personal', name: 'Personal', order: 9, collapsed: true },
  { id: 'autofix', name: 'kirocrew-github-autofix', order: 12, collapsed: true },
]

const folders = REAL ? realFolders : [
  { id: 'f1', name: 'Parent', order: 0, collapsed: false },
  { id: 'f1a', name: 'Child', order: 0, collapsed: false, parent_id: 'f1' },
  { id: 'f1a1', name: 'Grand', order: 0, collapsed: false, parent_id: 'f1a' },
  { id: 'f2', name: 'Shut', order: 1, collapsed: true },
]

const slot = (key, title, folder_id, last_ts) => ({
  key, title, messages: 4, running: false, agent: 'kirocrew',
  created: '2026-07-20T01:00:00Z', last_ts, folder_id,
})

const slots = REAL ? (() => {
  const out = []
  const mk = (n, fid, tag) => { for (let i = 0; i < n; i++) out.push(slot(`s-${tag}-${i}`, `${tag} task number ${i}`, fid, '2026-08-2' + (i % 6) + 'T0' + (i % 9) + ':00:00Z')) }
  mk(37, 'kiro', 'kiro'); mk(2, 'docw', 'docw'); mk(5, 'kas', 'kas'); mk(3, 'research', 'research'); mk(3, 'appstore', 'appstore')
  mk(19, 'mesh', 'mesh'); mk(92, 'autofix', 'autofix'); mk(1, 'personal', 'personal')
  mk(7, '', 'loose')
  return out
})() : [
  slot('s-child', 'Session in Child', 'f1a', '2026-08-26T10:00:00Z'),
  slot('s-parent', 'Session in Parent', 'f1', '2026-08-26T09:00:00Z'),
  slot('s-grand', 'Session in Grand', 'f1a1', '2026-08-26T07:00:00Z'),
  slot('s-shut', 'Session in Shut', 'f2', '2026-08-26T06:00:00Z'),
  slot('s-root', 'Ungrouped session', '', '2026-08-26T08:00:00Z'),
]

async function main() {
  const { srv, base } = await serveDist(process.env.REPRO_DIST || undefined)
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 } })
  const page = await context.newPage()

  await stubDashboardApi(page, { folders, slots })
  logPageProblems(page)

  // Record folder-assignment PATCH calls fired by the SPA.
  const patches = []
  await page.route('**/api/chat/slots/*/folder', async (route) => {
    const req = route.request()
    const m = req.url().match(/slots\/([^/]+)\/folder/)
    patches.push({ slot: decodeURIComponent(m[1]), body: req.postDataJSON() })
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  await page.screenshot({ path: `${OUT}/00-initial.png` })


  const center = (box) => ({ x: box.x + box.width / 2, y: box.y + box.height / 2 })

  /** Drag from a source locator's center to an absolute target point, with
   *  stepped moves so dnd-kit's PointerSensor activation distance is crossed
   *  and onDragOver fires along the way. */
  async function drag(srcLoc, target, shotName) {
    const sb = await srcLoc.boundingBox()
    if (!sb) throw new Error('source not found')
    const s = center(sb)
    await page.mouse.move(s.x, s.y)
    await page.mouse.down()
    // small jiggle to satisfy activation constraint, then stepped travel
    await page.mouse.move(s.x + 6, s.y + 6, { steps: 3 })
    await page.mouse.move(target.x, target.y, { steps: 15 })
    await page.waitForTimeout(300) // let measuring/over settle
    // Record which folder-drop target (if any) is highlighted at hover time.
    const ringed = await page.evaluate(() =>
      [...document.querySelectorAll('[data-folder-drop]')]
        .filter(el => el.className.includes('ring-accent'))
        .map(el => el.getAttribute('data-folder-drop')))
    await page.screenshot({ path: `${OUT}/${shotName}-hover.png` })
    await page.mouse.up()
    await page.waitForTimeout(400)
    return ringed
  }

  // Locators. Session rows are draggable cards; find them by title text.
  const row = (title) => page.locator(`[data-testid^="session-"], [role="button"], div`).filter({ hasText: title }).last()
  // Safer: use the drag ghost source = the visible row containing the exact title.
  const sessionRow = (title) => page.getByText(title, { exact: true }).first()

  const results = []
  async function scenario(name, srcTitle, targetTitle, opts = {}) {
    // Fresh page state per scenario — a prior scenario's optimistic move must
    // not change this scenario's starting folder layout.
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2200)
    if (opts.scrollList) {
      // Scroll the session list container so droppable rects were measured at
      // one scroll position and used at another (the stale-rect failure mode).
      await page.evaluate((px) => {
        const el = document.querySelector('.sidebar .overflow-y-auto')
        if (el) el.scrollTop = px
      }, opts.scrollList)
      await page.waitForTimeout(400)
    }
    patches.length = 0
    const t = await page.getByText(targetTitle, { exact: true }).first().boundingBox()
    if (!t) { results.push(`${name}: TARGET NOT VISIBLE (${targetTitle})`); return }
    const ringed = await drag(sessionRow(srcTitle), center(t), name)
    results.push({ name, patches: [...patches], ringed })
    await page.screenshot({ path: `${OUT}/${name}-after.png` })
  }

  if (REAL) {
    // The user's real gesture: a session inside the "KAS" subfolder dragged up
    // into the "Kiro" parent. The Kiro block is ~2000px tall, so the KAS rows
    // sit several viewports below Kiro's header — reaching it needs the drag
    // to auto-scroll the list upward. Scroll KAS into view first, then drag to
    // the top edge and let dnd-kit autoscroll until "Kiro" header is under the
    // pointer.
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    // Bring a KAS row into view.
    await page.evaluate(() => {
      const rows = [...document.querySelectorAll('[data-session-row]')]
      const kas = rows.find(r => r.getAttribute('data-session-row')?.startsWith('s-kas'))
      kas?.scrollIntoView({ block: 'center' })
    })
    await page.waitForTimeout(500)
    patches.length = 0
    const src = await page.getByText('kas task number 1', { exact: true }).first().boundingBox()
    if (!src) throw new Error('kas row not visible')
    const s = center(src)
    await page.mouse.move(s.x, s.y)
    await page.mouse.down()
    await page.mouse.move(s.x + 6, s.y + 6, { steps: 3 })
    // Drag to near the top of the list viewport and hold — dnd-kit autoscroll
    // should scroll the list up under the pointer.
    const listBox = await page.locator('.sidebar .overflow-y-auto').first().boundingBox()
    const holdX = listBox.x + listBox.width / 2
    const holdY = listBox.y + 8
    await page.mouse.move(holdX, holdY, { steps: 12 })
    // Hold until the Kiro folder header is under the pointer (autoscroll), max ~12s.
    let ringNow = []
    for (let i = 0; i < 60; i++) {
      await page.waitForTimeout(200)
      ringNow = await page.evaluate(() =>
        [...document.querySelectorAll('[data-folder-drop]')]
          .filter(el => el.className.includes('ring-accent'))
          .map(el => el.getAttribute('data-folder-drop')))
      const kiroHeader = await page.getByText('Kiro', { exact: true }).first().boundingBox().catch(() => null)
      if (kiroHeader && Math.abs(kiroHeader.y + kiroHeader.height / 2 - holdY) < 60) break
    }
    await page.screenshot({ path: `${OUT}/R1-autoscroll-hold-hover.png` })
    // Nudge onto the Kiro header text position and drop.
    const kh = await page.getByText('Kiro', { exact: true }).first().boundingBox().catch(() => null)
    if (kh) await page.mouse.move(kh.x + kh.width / 2, kh.y + kh.height / 2, { steps: 4 })
    await page.waitForTimeout(400)
    const ringAtDrop = await page.evaluate(() =>
      [...document.querySelectorAll('[data-folder-drop]')]
        .filter(el => el.className.includes('ring-accent'))
        .map(el => el.getAttribute('data-folder-drop')))
    await page.screenshot({ path: `${OUT}/R1-on-kiro-header-hover.png` })
    await page.mouse.up()
    await page.waitForTimeout(500)
    results.push({ name: 'R1-kas-to-kiro-header-autoscroll', patches: [...patches], ringed: ringAtDrop, ringDuringHold: ringNow })
    await page.screenshot({ path: `${OUT}/R1-after.png` })

    // R2: same source, but drop onto one of Kiro's DIRECT session rows
    // (visible without autoscroll: kiro rows sit directly above the KAS block).
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    await page.evaluate(() => {
      const rows = [...document.querySelectorAll('[data-session-row]')]
      const kas = rows.find(r => r.getAttribute('data-session-row')?.startsWith('s-kas'))
      kas?.scrollIntoView({ block: 'center' })
    })
    await page.waitForTimeout(500)
    patches.length = 0
    // A kiro direct row visible in the same viewport as the KAS rows.
    const kiroRow = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('[data-session-row]')]
      const vis = rows.filter(r => {
        const b = r.getBoundingClientRect()
        return r.getAttribute('data-session-row')?.startsWith('s-kiro') && b.top > 80 && b.bottom < window.innerHeight - 40
      })
      const b = vis[0]?.getBoundingClientRect()
      return b ? { x: b.x + b.width / 2, y: b.y + b.height / 2, key: vis[0].getAttribute('data-session-row') } : null
    })
    const src2 = await page.getByText('kas task number 2', { exact: true }).first().boundingBox()
    if (kiroRow && src2) {
      const ringed = await drag(page.getByText('kas task number 2', { exact: true }).first(), { x: kiroRow.x, y: kiroRow.y }, 'R2-kas-to-kiro-sessionrow')
      results.push({ name: 'R2-kas-to-kiro-sessionrow', patches: [...patches], ringed })
    } else {
      results.push({ name: 'R2-kas-to-kiro-sessionrow', patches: [], ringed: [], error: 'no kiro direct row visible alongside KAS rows' })
    }
  } else {
  // Scenario A: child-folder session -> parent header. Expect PATCH folder f1.
  await scenario('A-child-to-parent-header', 'Session in Child', 'Parent')
  // Scenario B: child-folder session -> parent's own session row. Expect f1.
  await scenario('B-child-to-parent-sessionrow', 'Session in Child', 'Session in Parent')
  // Scenario C: ungrouped session -> parent header. Expect f1.
  await scenario('C-root-to-parent-header', 'Ungrouped session', 'Parent')
  // Scenario D: ungrouped session -> parent's own session row. Expect f1.
  await scenario('D-root-to-parent-sessionrow', 'Ungrouped session', 'Session in Parent')
  // Scenario E: grandchild session -> its NESTED parent's header ("Child").
  // The parent being targeted is itself a subfolder. Expect f1a.
  await scenario('E-grand-to-nested-parent-header', 'Session in Grand', 'Child')
  // Scenario F: child session -> a COLLAPSED folder's header. Expect f2.
  await scenario('F-child-to-collapsed-header', 'Session in Child', 'Shut')
  // Scenario G: deliberate UN-FILING must stay reachable — a foldered session
  // dropped on the ungrouped session at the bottom (root-lane/root-group
  // territory, outside every folder block) leaves its folder (folder_id "").
  await scenario('G-child-to-empty-lane-unfiles', 'Session in Child', 'Ungrouped session')
  }

  console.log('=== RESULTS ===')
  let failed = 0
  // slot + folder_id the drop must PATCH (exactly one write), and the folder
  // whose ring must be lit at drop time ('' = a null-folder target, no ring
  // expectation because the lane/ungroup bucket carries no folder marker).
  const EXPECT = {
    'A-child-to-parent-header': { slot: 's-child', folder: 'f1' },
    'B-child-to-parent-sessionrow': { slot: 's-child', folder: 'f1' },
    'C-root-to-parent-header': { slot: 's-root', folder: 'f1' },
    'D-root-to-parent-sessionrow': { slot: 's-root', folder: 'f1' },
    'E-grand-to-nested-parent-header': { slot: 's-grand', folder: 'f1a' },
    'F-child-to-collapsed-header': { slot: 's-child', folder: 'f2' },
    'G-child-to-empty-lane-unfiles': { slot: 's-child', folder: '' },
    'R1-kas-to-kiro-header-autoscroll': { slot: 's-kas-1', folder: 'kiro' },
    'R2-kas-to-kiro-sessionrow': { slot: 's-kas-2', folder: 'kiro' },
  }
  for (const r of results) {
    const want = EXPECT[r.name]
    const patchOk = !!want && r.patches.length === 1
      && r.patches[0].slot === want.slot && r.patches[0].body?.folder_id === want.folder
    const ringOk = !!want && (want.folder === '' || (r.ringed || []).includes(want.folder))
    const ok = patchOk && ringOk && !r.error
    if (!ok) failed++
    console.log(`${ok ? 'PASS' : 'FAIL'} ${r.name}: patches=${JSON.stringify(r.patches)} ring=${JSON.stringify(r.ringed)}${r.error ? ' error=' + r.error : ''}`)
  }
  process.exitCode = failed ? 1 : 0

  await browser.close()
  srv.close()
}

main().catch((e) => { console.error(e); process.exit(1) })
