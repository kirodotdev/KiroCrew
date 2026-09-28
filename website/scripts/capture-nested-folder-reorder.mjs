/**
 * Capture + regression harness for "drag a nested subfolder to reorder it among
 * its siblings" in LIST view -- the change this PR adds (#10428).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server with
 * /api/** answered by the shared fixture stub. The client code under test is
 * unmodified -- only the network is stubbed -- and the drag is driven with REAL
 * pointer events (dnd-kit's PointerSensor is pointer-event based, so there is no
 * synthetic shortcut). That is what makes this a regression test and not just a
 * camera: before this PR a nested row was a bare draggable with no sortable id,
 * so it appeared in no sibling ring, the drop below produced NO reorder, and the
 * order assertions would fail.
 *
 * It photographs the flow AND asserts the things that matter, exiting non-zero
 * if any fails:
 *   1. Before: two subfolders under one parent, drawn in their stored order.
 *   2. The nested rows are registered sortables (the ring the drag moves within).
 *   3. Mid-drag: the ghost follows the pointer.
 *   4. On drop near a sibling's edge: ONE atomic reorder POST fires, carrying new
 *      `order` values for the two siblings and for nobody else -- `order` is a
 *      per-container index, so a root row's number must not be touched.
 *   5. After: the two subfolders render in the swapped order, and the parent and
 *      the root lane are unchanged (a reorder is not a re-parent).
 *   6. Re-parent still works from a nested row: dropping on the middle band of a
 *      DIFFERENT root folder's header fires a parent_id PATCH.
 *
 * Usage: node scripts/capture-nested-folder-reorder.mjs [outDir]
 *
 * RECORD_VIDEO=1 records one continuous webm of the whole flow instead of only
 * stills, because a still cannot answer the question a reviewer asks about a drag
 * -- whether the siblings SLIDE or teleport. Clip mode keeps every assertion: the
 * flow it films is the flow it checks, so a green clip is a verified clip. Turn it
 * into a GIF with:
 *   ffmpeg -i <webm> -vf "fps=10,scale=760:-1:flags=lanczos,palettegen" pal.png
 *   ffmpeg -i <webm> -i pal.png -lavfi "fps=10,scale=760:-1:flags=lanczos[v];\
 *     [v][1:v]paletteuse=dither=bayer:bayer_scale=5" out.gif
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/nested-folder-reorder'
const VIEW = { width: 1400, height: 900 }
const PARENT = 'fteam'       // root folder holding the two subfolders
const A = 'falpha'           // first subfolder (order 0)
const B = 'fbeta'            // second subfolder (order 1), the one dragged up
const OTHER = 'fwork'        // a second ROOT folder, the re-parent target

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

// Two root folders; PARENT holds two children. The array is mutated in place by
// the write handlers so the `onSettled` refetch of /api/chat/folders returns the
// new state -- which is what the real server does (persist, serve it next read).
const folders = [
  { id: PARENT, name: 'Team', order: 0 },
  { id: OTHER, name: 'Work', order: 1 },
  { id: A, name: 'Alpha', order: 0, parent_id: PARENT },
  { id: B, name: 'Beta', order: 1, parent_id: PARENT },
]

const mkSlot = (key, title, folderId) => ({
  key, title, running: false, last_message: '', messages: 3, agent: 'kirocrew',
  memory_mode: 'persistent', project: '', folder_id: folderId, modified: now,
  tags: [], source_links: [], source_links_total: 0,
})

const slots = [
  mkSlot('chat-a1', 'Alpha notes', A),
  mkSlot('chat-b1', 'Beta notes', B),
  mkSlot('chat-w1', 'Sprint planning', OTHER),
]

/** Every reorder POST body, so the drop is asserted rather than eyeballed. */
const reorders = []
/** Every re-parent PATCH, for the second gesture. */
const patches = []

async function main() {
  const { srv, base } = await serveDist()
  // `--no-sandbox` is required where the OS user namespaces Chromium's zygote
  // sandbox needs are unavailable (containers / locked mount namespaces).
  const browser = await chromium.launch({ args: ['--no-sandbox'] })
  const results = []
  const record = (name, pass, note = '') => {
    results.push({ name, pass, note })
    console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${note ? ` -- ${note}` : ''}`)
  }

  const extra = async (path, route) => {
    const method = route.request().method()
    // Atomic sibling renumber: POST /api/chat/folders/reorder {orders:[...]}.
    if (path === '/api/chat/folders/reorder' && method === 'POST') {
      const body = route.request().postDataJSON?.() ?? {}
      const orders = Array.isArray(body.orders) ? body.orders : []
      reorders.push(orders)
      for (const o of orders) {
        const f = folders.find(x => x.id === o.id)
        if (f) f.order = o.order       // persist so the refetch reflects it
      }
      await json(route, { ok: true })
      return true
    }
    if (path.startsWith('/api/chat/folders/') && method === 'PATCH') {
      const id = decodeURIComponent(path.slice('/api/chat/folders/'.length))
      const body = route.request().postDataJSON?.() ?? {}
      const f = folders.find(x => x.id === id)
      if (f) Object.assign(f, body)
      if ('parent_id' in body) patches.push({ id, parent_id: body.parent_id })
      await json(route, f ?? { ok: true })
      return true
    }
    if (path === '/api/chat/tags') { await json(route, []); return true }
    return false
  }

  // Clip mode drops deviceScaleFactor to 1: a 2x webm is four times the bytes for
  // a frame nobody zooms into, and GitHub caps an attachment at 10MB.
  const recordVideo = process.env.RECORD_VIDEO === '1'
  const context = await browser.newContext({
    viewport: VIEW,
    deviceScaleFactor: recordVideo ? 1 : 2,
    ...(recordVideo ? { recordVideo: { dir: `${OUT}/video-raw`, size: VIEW } } : {}),
  })

  let page = null
  async function load(theme) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { folders, slots, theme, extra })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForSelector(`[data-folder-row="${A}"]`, { timeout: 12000 })
    await page.waitForSelector(`[data-folder-row="${B}"]`, { timeout: 12000 })
    await page.waitForTimeout(500)
  }

  const shot = async (name) => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Header row of a folder block -- the drag handle and the drop band. */
  const header = (id) => page.locator(`[data-folder-row="${id}"]`).first()

  /**
   * The rendered order of the subfolder rows, read from the sortable wrappers in
   * DOM order. Reading the wrappers rather than the folder rows is deliberate:
   * their presence is itself the fix (a bare draggable had none), so an empty
   * list fails loudly instead of silently falling back to a different selector.
   */
  const subfolderOrder = () => page.evaluate(
    () => Array.from(document.querySelectorAll('[data-subfolder-sortable]'))
      .map(el => el.getAttribute('data-subfolder-sortable')),
  )

  /**
   * Drive a real dnd-kit drag from one folder header to a Y offset within
   * another. Multi-step moves are required, not cosmetic: PointerSensor only
   * activates past a 5px distance constraint, and sidebarCollision reads the
   * pointer's offset within the target header on each move to decide
   * nest-vs-reorder.
   *
   * `bandFrac` picks which band of the target header the drop lands in: 0.5 is
   * the middle (re-parent INTO it), 0.08 the top edge (falls through to the
   * sibling reorder). Those are the same two bands a root row has always had.
   */
  async function dragHeaderTo(fromId, toId, bandFrac, midShot) {
    const from = await header(fromId).boundingBox()
    const to = await header(toId).boundingBox()
    if (!from || !to) throw new Error(`header not found: from=${!!from} to=${!!to}`)
    const sx = from.x + from.width / 2
    const sy = from.y + from.height / 2
    const tx = to.x + to.width / 2
    const ty = to.y + to.height * bandFrac

    await page.mouse.move(sx, sy)
    await page.mouse.down()
    await page.mouse.move(sx + 8, sy + 4, { steps: 4 })   // cross the activation threshold
    await page.waitForTimeout(150)
    for (let i = 1; i <= 12; i++) {
      await page.mouse.move(sx + ((tx - sx) * i) / 12, sy + ((ty - sy) * i) / 12)
      await page.waitForTimeout(35)
    }
    await page.waitForTimeout(400)
    const ghostLoc = page.getByTestId('folder-drag-ghost')
    const ghostBox = (await ghostLoc.count()) ? await ghostLoc.first().boundingBox() : null
    if (midShot) await shot(midShot)
    await page.mouse.up()
    await page.waitForTimeout(800)
    return { ghostBox }
  }

  // -- Scenario: dark theme, the full before -> drag -> after story -----------
  await load('dark')

  const beforeOrder = await subfolderOrder()
  record('before: both subfolders are registered sortables',
    beforeOrder.length === 2, `wrappers=[${beforeOrder.join(', ')}]`)
  record('before: they are drawn in their stored order',
    beforeOrder[0] === A && beforeOrder[1] === B, `got [${beforeOrder.join(', ')}]`)
  await shot('01-before-nested-order')

  const drag = await dragHeaderTo(B, A, 0.08, '02-mid-drag-nested-reorder')
  record('mid-drag: the drag ghost follows the pointer', !!drag.ghostBox,
    drag.ghostBox ? `ghost y=${Math.round(drag.ghostBox.y)}` : 'no ghost box')

  record('drop fires exactly one atomic reorder write', reorders.length === 1,
    `posts=${reorders.length}`)
  const orders = reorders[0] ?? []
  const touched = orders.map(o => o.id).sort()
  record('the reorder renumbers the two siblings and nobody else',
    touched.length === 2 && touched[0] === A && touched[1] === B,
    `touched=[${touched.join(', ')}]`)
  const newB = orders.find(o => o.id === B)?.order
  const newA = orders.find(o => o.id === A)?.order
  record('the dragged subfolder takes the earlier index',
    typeof newB === 'number' && typeof newA === 'number' && newB < newA,
    `${B}.order=${newB} ${A}.order=${newA}`)
  record('the reorder wrote no parent_id (a reorder is not a re-parent)',
    patches.length === 0, `patches=${patches.length}`)

  const afterOrder = await subfolderOrder()
  record('after: the subfolders render in the swapped order',
    afterOrder[0] === B && afterOrder[1] === A, `got [${afterOrder.join(', ')}]`)
  record('after: both are still inside the same parent',
    (await page.locator(`[data-folder-row="${PARENT}"] ~ * [data-subfolder-sortable]`).count()) >= 0
    && folders.find(f => f.id === B)?.parent_id === PARENT
    && folders.find(f => f.id === A)?.parent_id === PARENT,
    `${B}.parent=${folders.find(f => f.id === B)?.parent_id}`)
  record('after: the root lane order is untouched',
    folders.find(f => f.id === PARENT)?.order === 0
    && folders.find(f => f.id === OTHER)?.order === 1)
  await shot('03-after-nested-reorder-dark')

  // -- Scenario: the other gesture still works from a nested row -------------
  // Middle band of a DIFFERENT root folder's header = re-parent into it. This is
  // the behaviour a nested row already had; the PR must not have traded it away.
  await dragHeaderTo(A, OTHER, 0.5, '04-mid-drag-nested-reparent')
  const parented = patches.find(p => p.id === A)
  record('a nested row can still re-parent onto another folder', !!parented,
    parented ? `parent_id=${parented.parent_id}` : 'no PATCH recorded')
  record('the re-parent targets the folder it was dropped on',
    parented?.parent_id === OTHER, `expected ${OTHER}, got ${parented?.parent_id}`)
  await shot('05-after-nested-reparent-dark')

  // -- Light theme: the reordered state, for reviewers on light --------------
  // The fixture already carries the new orders (mutated by the writes above), so
  // a fresh load renders the end state directly -- no second drag needed.
  //
  // Skipped in clip mode, and the skip is what keeps the clip usable: `load`
  // closes the page, and a page's video finalizes on ITS close, so a second page
  // would end the recording at the reorder and film the light reload as a
  // separate orphan file. One page means one continuous clip covering both
  // gestures, which is the continuity a still cannot show.
  const clipPath = recordVideo ? await page.video()?.path() : null
  if (!recordVideo) {
    await load('light')
    await shot('06-after-light')
  }

  await page.close()
  await context.close()
  await browser.close()
  srv.close()

  const failed = results.filter(r => !r.pass)
  console.log(`\n--- ${results.length - failed.length}/${results.length} assertions passed ---`)
  if (clipPath) console.log(`WEBM ${clipPath}`)
  if (failed.length) {
    for (const f of failed) console.log(`FAILED: ${f.name} -- ${f.note}`)
    process.exitCode = 1
  }
}

main().catch(e => {
  console.error(e)
  process.exitCode = 1
  // A failure path leaves the browser + static server open, which keeps the node
  // process alive indefinitely; force the exit once the error is printed.
  setTimeout(() => process.exit(1), 500)
})
