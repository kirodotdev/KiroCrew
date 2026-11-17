import { test, expect, type APIRequestContext } from '@playwright/test'

/**
 * Pinned folder headers in the PHONE sessions list, on WebKit with iPhone
 * emulation (the opt-in `webkit-mobile` project, PLAYWRIGHT_RUN_WEBKIT=1; see
 * playwright.config.ts). `sidebar-sticky-folder-header.spec.ts` covers the same
 * pinning on desktop Chromium; this file is the engine and form the iPhone
 * report came from.
 *
 * The report: on iOS Safari a subfolder header stopped painting mid-scroll and
 * left a blank row, while its sessions and the root folder's header stayed
 * visible. The list body is now a stacking context (`isolate` in
 * `LIST_BODY_CLS`, components/listShell.ts), so the headers paint in the body's
 * own scrolled layer instead of a separate per-header layer chain.
 *
 * What this file can and cannot see: Playwright's WebKit on Linux does not
 * reproduce the iOS paint failure, with or without the fix, because it does not
 * composite and scroll the way iOS does. So the file pins the fix's structure in
 * a real engine (the lane is a stacking context) and the behaviour around it
 * (every subfolder header that should be on screen is the element at its own
 * centre at every scroll step, and nested headers pin one row below their
 * parent). The paint itself is checked by hand on a physical iPhone.
 *
 * SERIAL-RUN DEPENDENCY: same as sidebar-sticky-folder-header.spec.ts (the gate
 * runs workers: 1; session-tags-folders.spec.ts wipes folders in its beforeEach).
 */

const TOLERANCE = 1
const STEP_PX = 12

const seeded = { folders: [] as string[], slots: [] as string[] }

async function seedFolder(request: APIRequestContext, name: string, parentId?: string, collapsed = false) {
  const body: Record<string, string> = { name }
  if (parentId) body.parent_id = parentId
  const res = await request.post('/api/chat/folders', { data: body })
  expect(res.ok(), `POST /api/chat/folders "${name}" should succeed`).toBe(true)
  const folder = await res.json() as { id: string }
  seeded.folders.push(folder.id)
  if (collapsed) {
    const patched = await request.patch(`/api/chat/folders/${folder.id}`, { data: { collapsed: true } })
    expect(patched.ok(), `collapsing "${name}" should succeed`).toBe(true)
  }
  return folder
}

async function seedSlots(request: APIRequestContext, folderId: string, n: number) {
  const keys: string[] = []
  for (let i = 0; i < n; i++) {
    const res = await request.post('/api/chat/slots', { data: { agent: 'default' } })
    expect(res.ok(), 'POST /api/chat/slots should succeed').toBe(true)
    const slot = await res.json() as { key: string }
    seeded.slots.push(slot.key)
    const assign = await request.patch(`/api/chat/slots/${slot.key}/folder`, { data: { folder_id: folderId } })
    expect(assign.ok(), `folder assignment for slot ${slot.key} should succeed`).toBe(true)
    keys.push(slot.key)
  }
  return keys
}

test.afterEach(async ({ request }) => {
  for (const key of seeded.slots.splice(0)) await request.delete(`/api/chat/slots/${key}`)
  for (const id of seeded.folders.splice(0).reverse()) await request.delete(`/api/chat/folders/${id}`)
})

test('phone list: subfolder headers stay the topmost element at their own position while the list scrolls', async ({ page, request }) => {
  // Seeding 26 sessions over the API is most of the run.
  test.setTimeout(60_000)
  await page.addInitScript(() => { localStorage.setItem('mc-onboarded', '1') })
  const uniq = test.info().testId.slice(0, 8)
  // The recording's shape: a root folder holding an expanded subfolder, a run
  // of collapsed subfolders, then more expanded subfolders, then the next root.
  const root = await seedFolder(request, `WK-Root-${uniq}`)
  const open1 = await seedFolder(request, `WK-Open1-${uniq}`, root.id)
  const shut = [] as { id: string }[]
  for (let i = 1; i <= 4; i++) shut.push(await seedFolder(request, `WK-Shut${i}-${uniq}`, root.id, true))
  const open2 = await seedFolder(request, `WK-Open2-${uniq}`, root.id)
  const next = await seedFolder(request, `WK-Next-${uniq}`)
  await seedSlots(request, open1.id, 6)
  for (const f of shut) await seedSlots(request, f.id, 1)
  await seedSlots(request, open2.id, 8)
  await seedSlots(request, next.id, 6)
  const depth: Record<string, number> = { [root.id]: 0, [open1.id]: 1, [open2.id]: 1, [next.id]: 0 }
  for (const f of shut) depth[f.id] = 1

  await page.goto('/chat')
  const toggle = page.getByTestId('mobile-topbar-sessions-toggle')
  await expect(toggle).toBeVisible({ timeout: 15000 })
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.tap()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await expect(page.locator(`[data-folder-row="${next.id}"]`)).toBeAttached({ timeout: 15000 })
  // The drawer slides in; measure only once the lane sits fully on screen.
  await expect.poll(() => page.evaluate(() =>
    document.querySelector('[data-testid="tree-view-lane"]')?.getBoundingClientRect().left ?? -1),
  { message: 'the session list drawer finishes opening' }).toBeGreaterThanOrEqual(0)

  // The fix itself, read from the engine: the lane is a stacking context.
  const isolation = await page.evaluate(() => getComputedStyle(document.querySelector('[data-testid="tree-view-lane"]')!).isolation)
  expect(isolation, 'the phone sessions lane must be its own stacking context').toBe('isolate')

  // Walk the root folder's whole block in small steps, in one page-side loop.
  // At each step every seeded header whose middle is inside the lane below
  // the dock must be the element at its own centre, unless a shallower pinned
  // header is over it (the designed handoff at a folder's end). While an
  // expanded subfolder's block runs from above its pin slot to well below it,
  // the root must sit on the dock's bottom edge and the subfolder header one
  // header height below that.
  const walk = await page.evaluate(async ({ rootId, openIds, depth, step, tol }) => {
    const lane = document.querySelector<HTMLElement>('[data-testid="tree-view-lane"]')!
    const block = document.querySelector<HTMLElement>(`[data-folder-drop="${rootId}"]`)!
    const rootHeader = document.querySelector<HTMLElement>(`[data-folder-row="${rootId}"]`)!
    const rowH = rootHeader.offsetHeight
    const blockTop = block.getBoundingClientRect().top - lane.getBoundingClientRect().top + lane.scrollTop
    const from = Math.max(0, blockTop - lane.clientHeight)
    const to = Math.min(blockTop + block.offsetHeight, lane.scrollHeight - lane.clientHeight)
    const out = { from, to, checked: 0, pinChecks: 0, bad: [] as string[] }
    for (let y = from; y <= to; y += step) {
      lane.scrollTop = y
      await new Promise(r => requestAnimationFrame(r))
      const laneBox = lane.getBoundingClientRect()
      const dock = lane.parentElement?.querySelector<HTMLElement>('[data-testid="list-dock"]')
      const pinEdge = dock ? dock.getBoundingClientRect().bottom : laneBox.top
      const at = `scrollTop ${lane.scrollTop}`
      for (const [id, d] of Object.entries(depth)) {
        const h = document.querySelector<HTMLElement>(`[data-folder-row="${id}"]`)
        if (!h) { out.bad.push(`${at}: ${id} header not attached`); continue }
        const r = h.getBoundingClientRect()
        const mid = (r.top + r.bottom) / 2
        if (mid <= pinEdge + 1 || mid >= laneBox.bottom - 1) continue
        const hit = document.elementFromPoint(r.left + Math.min(40, r.width / 2), mid)
        const owner = hit?.closest<HTMLElement>('[data-folder-row]')?.getAttribute('data-folder-row') ?? null
        if (owner === id) { out.checked++; continue }
        if (owner !== null && owner in depth && depth[owner] < d) continue
        out.bad.push(`${at}: ${id} (depth ${d}) at top ${Math.round(r.top)}, centre hit ${owner ?? hit?.tagName ?? 'nothing'}`)
      }
      for (const id of openIds) {
        const b = document.querySelector<HTMLElement>(`[data-folder-drop="${id}"]`)!.getBoundingClientRect()
        if (b.top > pinEdge + rowH - 2 || b.bottom < pinEdge + rowH * 3) continue
        out.pinChecks++
        const rootTop = rootHeader.getBoundingClientRect().top
        const subTop = document.querySelector<HTMLElement>(`[data-folder-row="${id}"]`)!.getBoundingClientRect().top
        if (Math.abs(rootTop - pinEdge) > tol) out.bad.push(`${at}: root at ${rootTop}, not on the dock edge ${pinEdge}`)
        if (Math.abs(subTop - pinEdge - rowH) > tol) out.bad.push(`${at}: ${id} at ${subTop}, not one row (${rowH}px) below the dock edge ${pinEdge}`)
      }
    }
    return out
  }, { rootId: root.id, openIds: [open1.id, open2.id], depth, step: STEP_PX, tol: TOLERANCE })
  console.log(`[webkit-sticky] checked ${walk.checked} header positions and ${walk.pinChecks} pinned pairs, scrollTop ${walk.from}..${walk.to} step ${STEP_PX}, failures ${walk.bad.length}`)
  expect(walk.checked, 'the walk must have seen the subfolder headers on screen').toBeGreaterThan(20)
  expect(walk.pinChecks, 'the walk must have scrolled through both expanded subfolders').toBeGreaterThan(5)
  expect(walk.bad, walk.bad.slice(0, 10).join('\n')).toEqual([])
})
