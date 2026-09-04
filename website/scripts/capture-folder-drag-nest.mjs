/**
 * Capture + regression harness for "drag a folder onto another to nest it"
 * in BOARD (tag-column) view — the change this PR adds.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server with
 * /api/** answered by the shared fixture stub. The client code under test is
 * unmodified — only the network is stubbed — and the drag is driven with REAL
 * pointer events (dnd-kit's PointerSensor is pointer-event based, so there is no
 * synthetic shortcut). That is what makes this a regression test and not just a
 * camera: before this PR the board folder block had no `folder-drop` droppable,
 * so the drop below produced NO re-parent and the "after" assertions would fail.
 *
 * It photographs the flow AND asserts the things that matter, exiting
 * non-zero if any fails:
 *   1. Before: two ROOT folders, neither nested inside the other.
 *   2. Edge: a drag held past the drawer's right edge still shows the ghost,
 *      and no ancestor clip-path cuts it (the portaled-overlay contract).
 *   3. Mid-drag: dropping onto the middle band of a folder header shows the
 *      nest cue (the ring on that folder's `folder-drop` zone).
 *   4. On drop: a re-parent PATCH fires — folders/<dragged> body {parent_id:<target>}.
 *   5. After: the dragged folder now renders INSIDE the target's subtree.
 *
 * Usage: node scripts/capture-folder-drag-nest.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/folder-drag-nest'
const VIEW = { width: 1400, height: 900 }
const COL = 'all'            // single board column; folder testids are col-<COL>-folder-<id>
const A = 'fdesign'          // dragged folder (becomes the child)
const B = 'fwork'            // target folder (becomes the parent)

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

// Two ROOT folders. The array is mutated in place by the PATCH handler so the
// `onSettled` refetch of /api/chat/folders returns the re-parented state — which
// is exactly what the real server does (persist parent_id, serve it next read).
const folders = [
  { id: A, name: 'Design docs', order: 0 },
  { id: B, name: 'Work', order: 1 },
]

const mkSlot = (key, title, folderId) => ({
  key, title, running: false, last_message: '', messages: 3, agent: 'kirocrew',
  memory_mode: 'persistent', project: '', folder_id: folderId, modified: now,
  tags: [], source_links: [], source_links_total: 0,
})

const slots = [
  mkSlot('chat-a1', 'Onboarding one-pager', A),
  mkSlot('chat-a2', 'Rendering RFC', A),
  mkSlot('chat-b1', 'Sprint planning', B),
]

// One match-all column so both root folders render once in the board strip.
const columns = [{ id: COL, name: '', tag_ids: [], mode: 'any', order: 0, include_untagged: true }]

/** Every re-parent PATCH, so the drop can be asserted rather than eyeballed. */
const patches = []

const fid = (id) => `col-${COL}-folder-${id}`

async function main() {
  const { srv, base } = await serveDist()
  // `--no-sandbox` is required where the OS user namespaces Chromium's zygote
  // sandbox needs are unavailable (containers / locked mount namespaces); the CI
  // runner has them, a local dev container may not.
  const browser = await chromium.launch({ args: ['--no-sandbox'] })
  const results = []
  const record = (name, pass, note = '') => {
    results.push({ name, pass, note })
    console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${note ? ` — ${note}` : ''}`)
  }

  const extra = async (path, route) => {
    const method = route.request().method()
    // Re-parent (or any folder field update): PATCH /api/chat/folders/<id>.
    if (path.startsWith('/api/chat/folders/') && method === 'PATCH') {
      const id = decodeURIComponent(path.slice('/api/chat/folders/'.length))
      const body = route.request().postDataJSON?.() ?? {}
      const f = folders.find(x => x.id === id)
      if (f) Object.assign(f, body)         // persist so the refetch reflects it
      if ('parent_id' in body) patches.push({ id, parent_id: body.parent_id })
      await json(route, f ?? { ok: true })
      return true
    }
    if (path === '/api/chat/tags') { await json(route, []); return true }
    if (path === '/api/chat/tag-columns') { await json(route, columns); return true }
    return false
  }

  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })

  let page = null
  async function load(theme) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    // Board (tag-columns) view is opt-in: `tagColumnsEnabled` defaults to
    // false and the stub clears localStorage, so without this seed the column
    // strip — and every col-* folder testid asserted below — never mounts.
    // Seeded through the stub's OWN init script: Playwright does not define
    // the evaluation order of separately registered init scripts, so a
    // second addInitScript would race the stub's clear.
    await stubDashboardApi(page, {
      folders, slots, theme, extra,
      localStorageEntries: { 'mc-chat-config': JSON.stringify({ tagColumnsEnabled: true }) },
    })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForSelector(`[data-testid="${fid(A)}"]`, { timeout: 12000 })
    await page.waitForSelector(`[data-testid="${fid(B)}"]`, { timeout: 12000 })
    await page.waitForTimeout(500)
  }

  const shot = async (name) => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Header row (the drag handle / drop band) of a board folder block. */
  const header = (id) => page.locator(`[data-testid="${fid(id)}"] [role="button"]`).first()

  /**
   * Drive a real dnd-kit drag from folder A's header to the MIDDLE band of
   * folder B's header. Multi-step moves are required, not cosmetic: PointerSensor
   * only activates past a 5px distance constraint, and sidebarCollision reads the
   * pointer's offset within the target header on each move to decide nest-vs-reorder.
   */
  async function dragFolderOntoHeader(fromId, toId, midShot) {
    const from = await header(fromId).boundingBox()
    const to = await header(toId).boundingBox()
    if (!from || !to) throw new Error(`header not found: from=${!!from} to=${!!to}`)
    const sx = from.x + from.width / 2
    const sy = from.y + from.height / 2
    const tx = to.x + to.width / 2
    const ty = to.y + to.height / 2   // center = the 0.5 nest band (inside 0.2..0.8)

    await page.mouse.move(sx, sy)
    await page.mouse.down()
    await page.mouse.move(sx + 8, sy + 4, { steps: 4 })   // cross the activation threshold
    await page.waitForTimeout(150)
    for (let i = 1; i <= 12; i++) {
      await page.mouse.move(sx + ((tx - sx) * i) / 12, sy + ((ty - sy) * i) / 12)
      await page.waitForTimeout(35)
    }
    await page.waitForTimeout(400)
    // The nest cue: the target's folder-drop zone rings while hovered in-band.
    const cue = await page.locator(`[data-folder-drop="${toId}"].ring-accent`).count()
    if (midShot) await shot(midShot)
    await page.mouse.up()
    await page.waitForTimeout(700)
    return { cue: cue > 0 }
  }

  /** True when folder `child` renders inside folder `parent`'s block. */
  async function isNestedUnder(childId, parentId) {
    return (await page.locator(`[data-testid="${fid(parentId)}"] [data-testid="${fid(childId)}"]`).count()) > 0
  }

  /**
   * Drag folder A's header out past the drawer's right edge and hold there,
   * probing the ghost. The sidebar rides inside OverlayDrawer's morph
   * clip-path and a clip-path clips fixed-position descendants too, so an
   * in-place overlay is erased the moment the ghost leaves the drawer. A
   * layout box alone does not prove visibility (clipping does not shrink the
   * box), so also walk the overlay's ancestor chain: any clip-path above the
   * ghost means it is being cut.
   */
  async function dragFolderPastDrawerEdge(fromId, midShot) {
    const from = await header(fromId).boundingBox()
    if (!from) throw new Error('drag source header not found')
    const strip = await page.getByTestId('column-strip').boundingBox()
    if (!strip) throw new Error('column strip not found')
    const sx = from.x + from.width / 2
    const sy = from.y + from.height / 2
    // Well past the strip's right edge, into the main pane.
    const tx = strip.x + strip.width + 220
    const ty = sy + 40

    await page.mouse.move(sx, sy)
    await page.mouse.down()
    await page.mouse.move(sx + 8, sy + 4, { steps: 4 })   // cross the activation threshold
    await page.waitForTimeout(150)
    for (let i = 1; i <= 12; i++) {
      await page.mouse.move(sx + ((tx - sx) * i) / 12, sy + ((ty - sy) * i) / 12)
      await page.waitForTimeout(35)
    }
    await page.waitForTimeout(300)
    const ghostLoc = page.getByTestId('folder-drag-ghost')
    const ghostBox = (await ghostLoc.count()) ? await ghostLoc.first().boundingBox() : null
    const ghostClipped = await page.evaluate(() => {
      const g = document.querySelector('[data-testid="folder-drag-ghost"]')
      if (!g) return null
      for (let el = g.parentElement; el; el = el.parentElement) {
        if (getComputedStyle(el).clipPath !== 'none') return true
      }
      return false
    })
    if (midShot) await shot(midShot)
    // Cancel rather than drop: this scenario is about ghost visibility, not
    // about mutating the nest fixture the earlier scenarios asserted.
    await page.keyboard.press('Escape')
    await page.mouse.up()
    await page.waitForTimeout(300)
    return { ghostBox, ghostClipped, stripRight: strip.x + strip.width }
  }

  // ── Scenario: dark theme, the full before → cue → after story ─────────────
  await load('dark')
  const beforeNested = await isNestedUnder(A, B)
  record('before: dragged folder is NOT already nested under the target', beforeNested === false)
  await shot('01-before-two-root-folders')

  // ── Scenario: the ghost survives crossing the drawer edge ─────────────────
  // Cancelled drag (Escape), so the nest story below still starts pristine.
  const edge = await dragFolderPastDrawerEdge(A, '02-ghost-past-drawer-edge')
  record('edge: the drag ghost still exists past the drawer edge', !!edge.ghostBox,
    edge.ghostBox ? `ghost x=${Math.round(edge.ghostBox.x)} stripRight=${Math.round(edge.stripRight)}` : 'no ghost box')
  record('edge: the ghost followed the pointer out of the column strip',
    !!edge.ghostBox && edge.ghostBox.x + edge.ghostBox.width > edge.stripRight,
    edge.ghostBox ? `ghost right=${Math.round(edge.ghostBox.x + edge.ghostBox.width)}` : '')
  record('edge: no ancestor clip-path cuts the ghost (portaled out of the drawer)',
    edge.ghostClipped === false, `ghostClipped=${edge.ghostClipped}`)

  const drag = await dragFolderOntoHeader(A, B, '03-mid-drag-nest-cue')
  record('mid-drag: the target folder shows the nest cue (ring on its drop zone)', drag.cue === true)

  const parented = patches.find(p => p.id === A)
  record('drop fires a re-parent PATCH on the dragged folder', !!parented,
    parented ? `parent_id=${parented.parent_id}` : 'no PATCH recorded')
  record('the re-parent targets the folder it was dropped on', parented?.parent_id === B,
    `expected ${B}, got ${parented?.parent_id}`)

  const afterNested = await isNestedUnder(A, B)
  record('after: the dragged folder now renders inside the target', afterNested === true)
  await shot('04-after-nested-dark')

  // ── Light theme: the resulting nested state, for reviewers on light ───────
  // The `folders` fixture is already re-parented (mutated by the PATCH above), so
  // a fresh load renders the end state directly — no second drag needed.
  await load('light')
  record('light: nested state persists across a reload (server-truth, not just optimistic)',
    await isNestedUnder(A, B))
  await shot('05-after-nested-light')

  await page.close()
  await context.close()
  await browser.close()
  srv.close()

  const failed = results.filter(r => !r.pass)
  console.log(`\n--- ${results.length - failed.length}/${results.length} assertions passed ---`)
  if (failed.length) {
    for (const f of failed) console.log(`FAILED: ${f.name} — ${f.note}`)
    process.exitCode = 1
  }
}

main().catch(e => {
  console.error(e)
  process.exitCode = 1
  // A failure path leaves the browser + static server open, which keeps the
  // node process alive indefinitely; force the exit once the error is printed.
  setTimeout(() => process.exit(1), 500)
})
