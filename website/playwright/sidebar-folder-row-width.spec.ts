import { test, expect, Page, APIRequestContext } from '@playwright/test'

/**
 * A session row filed in a folder stays inside the sidebar lane, however long
 * its title and preview are.
 *
 * FolderBody's inner grid item uses `overflow: clip` so nested folder headers
 * can stick to the lane (sidebar-sticky-folder-header.spec.ts). A `clip` box is
 * not a scroll container, so its automatic minimum width is its content's
 * min-content width, and the rows' single-line (`nowrap`) title and preview set
 * that width. Without an explicit `min-width: 0` a long title made every row in
 * the folder wider than the lane: the lane scrolled sideways, the timestamp sat
 * off-screen, and the hide (x) button could only be reached by scrolling.
 *
 * The title is the widest single-line text a row can carry: the preview line is
 * capped at 80 characters by the server (slot_projection.py) and truncates inside
 * the same content column, so a long title is the worst case for both lines.
 *
 * jsdom and happy-dom compute no layout, so only a real browser can see this.
 * The measured claims, for a row in a root folder, a row in a nested (depth 2)
 * folder, and an unfiled row as the control:
 *   1. the lane has no horizontal overflow (scrollWidth <= clientWidth);
 *   2. the row's right edge is inside the lane;
 *   3. at rest, the timestamp is inside the lane and not clipped away;
 *   4. on hover, the close (x) button is inside the lane, opaque, and is what a
 *      click at its centre would hit.
 *
 * SERIAL-RUN DEPENDENCY: same as sidebar-folder-alignment.spec.ts (the gate runs
 * workers: 1; session-tags-folders.spec.ts wipes folders in its beforeEach).
 */

const TOLERANCE = 1
// Under the server's 200-character title cap, and still several lane-widths long.
const LONG_TITLE = 'A very long session title that keeps going well past the sidebar edge '.repeat(2).trim()

const seeded = { folders: [] as string[], slots: [] as string[] }

async function seedFolder(request: APIRequestContext, name: string, parentId?: string) {
  const body: Record<string, string> = { name }
  if (parentId) body.parent_id = parentId
  const res = await request.post('/api/chat/folders', { data: body })
  expect(res.ok(), `POST /api/chat/folders "${name}" should succeed`).toBe(true)
  const folder = await res.json()
  seeded.folders.push(folder.id)
  return folder as { id: string }
}

async function seedLongSlot(request: APIRequestContext, folderId: string | null) {
  const res = await request.post('/api/chat/slots', { data: { agent: 'default' } })
  expect(res.ok(), 'POST /api/chat/slots should succeed').toBe(true)
  const slot = await res.json() as { key: string }
  seeded.slots.push(slot.key)
  const titled = await request.patch(`/api/chat/slots/${slot.key}/title`, { data: { title: LONG_TITLE } })
  expect(titled.ok(), `title for slot ${slot.key} should succeed`).toBe(true)
  if (folderId) {
    const assign = await request.patch(`/api/chat/slots/${slot.key}/folder`, { data: { folder_id: folderId } })
    expect(assign.ok(), `folder assignment for slot ${slot.key} should succeed`).toBe(true)
  }
  return slot.key
}

test.afterEach(async ({ request }) => {
  for (const key of seeded.slots.splice(0)) await request.delete(`/api/chat/slots/${key}`)
  for (const id of seeded.folders.splice(0).reverse()) await request.delete(`/api/chat/folders/${id}`)
})

type Box = { left: number; right: number }
type Measure = {
  scrollWidth: number
  clientWidth: number
  scrollLeft: number
  lane: Box
  row: Box
  time: Box | null
  timeHit: boolean
  close: Box | null
  closeHit: boolean
  closeOpacity: number
}

async function measureRow(page: Page, key: string): Promise<Measure> {
  return page.evaluate((key) => {
    const lane = document.querySelector<HTMLElement>('[data-testid="tree-view-lane"]')!
    const lr = lane.getBoundingClientRect()
    const left = lr.left + lane.clientLeft
    const row = document.querySelector(`[data-slot-key="${window.CSS.escape(key)}"]`)!
      .querySelector<HTMLElement>('[data-session-row]')!
    const rr = row.getBoundingClientRect()
    const box = (el: Element | null) => {
      if (!el) return null
      const r = el.getBoundingClientRect()
      return { left: r.left, right: r.right }
    }
    // Hit testing honours every ancestor's clip, so an element clipped off the
    // lane is not in the stack at its own centre. `top` asks for the topmost
    // element (what a click lands on); otherwise any layer counts, because at
    // rest the row's hover group sits transparently over the timestamp.
    const hits = (el: Element | null, top: boolean) => {
      if (!el) return false
      const r = el.getBoundingClientRect()
      const x = (r.left + r.right) / 2
      const y = (r.top + r.bottom) / 2
      if (top) {
        const at = document.elementFromPoint(x, y)
        return !!at && (el === at || el.contains(at))
      }
      return document.elementsFromPoint(x, y).some(at => el === at || el.contains(at))
    }
    const time = row.querySelector('[data-testid="session-row-time"]')
    const close = row.querySelector('button[aria-label="Close session"]')
    const group = close?.parentElement ?? null
    return {
      scrollWidth: lane.scrollWidth,
      clientWidth: lane.clientWidth,
      scrollLeft: lane.scrollLeft,
      lane: { left, right: left + lane.clientWidth },
      row: { left: rr.left, right: rr.right },
      time: box(time),
      timeHit: hits(time, false),
      close: box(close),
      closeHit: hits(close, true),
      closeOpacity: group ? parseFloat(getComputedStyle(group).opacity) : 0,
    }
  }, key)
}

// Soft, so one run reports every row and every claim: the baseline run against
// the unfixed code should show exactly which rows break, not just the first.
function expectInsideLane(m: Measure, box: Box | null, what: string) {
  expect(box, `${what} should render (${JSON.stringify(m)})`).not.toBeNull()
  if (!box) return
  expect.soft(box.left, `${what} left edge inside the lane (${JSON.stringify(m)})`).toBeGreaterThanOrEqual(m.lane.left - TOLERANCE)
  expect.soft(box.right, `${what} right edge inside the lane (${JSON.stringify(m)})`).toBeLessThanOrEqual(m.lane.right + TOLERANCE)
}

/** Opt-in: ROW_WIDTH_EVIDENCE_DIR=<dir> saves a still of the lane at each probe,
 *  which is how the PR screenshots were captured. The assertions ignore it. */
async function evidence(page: Page, tag: string) {
  if (!process.env.ROW_WIDTH_EVIDENCE_DIR) return
  await page.locator('[data-testid="tree-view-lane"]').screenshot({ path: `${process.env.ROW_WIDTH_EVIDENCE_DIR}/${tag.replace(/\W+/g, '-')}.png` })
}

test.describe('Sidebar folder rows fit the lane', () => {
  test('a long-titled row in a root folder, a nested folder and unfiled keeps its time and close button in view', async ({ page, request }) => {
    await page.addInitScript(() => { localStorage.setItem('mc-onboarded', '1') })
    await page.setViewportSize({ width: 1400, height: 800 })
    const uniq = Date.now().toString(36)
    const root = await seedFolder(request, `Width-A-${uniq}`)
    const nested = await seedFolder(request, `Width-B-${uniq}`, root.id)
    const inRoot = await seedLongSlot(request, root.id)
    const inNested = await seedLongSlot(request, nested.id)
    const unfiled = await seedLongSlot(request, null)
    const rows = [
      { key: inRoot, where: 'root folder' },
      { key: inNested, where: 'nested folder (depth 2)' },
      { key: unfiled, where: 'unfiled' },
    ]

    await page.goto('/chat')
    await expect(page.locator(`[data-folder-row="${nested.id}"]`)).toBeVisible({ timeout: 15000 })
    for (const { key } of rows) {
      const row = page.locator(`[data-slot-key="${key}"] [data-session-row]`).first()
      await expect(row).toBeAttached({ timeout: 15000 })
      await expect(row.locator('[data-session-title]')).toHaveText(LONG_TITLE)
      await expect(row.getByTestId('session-row-time')).toBeAttached()
    }

    for (const { key, where } of rows) {
      // Bring the row to the lane's vertical middle WITHOUT touching scrollLeft:
      // a Playwright hover or scrollIntoView would scroll the lane sideways and
      // hide exactly the overflow this spec measures.
      await page.evaluate((key) => {
        const lane = document.querySelector<HTMLElement>('[data-testid="tree-view-lane"]')!
        const row = document.querySelector(`[data-slot-key="${window.CSS.escape(key)}"]`)!
        lane.scrollTop += row.getBoundingClientRect().top - lane.getBoundingClientRect().top - lane.clientHeight / 2
      }, key)
      await page.mouse.move(0, 0)

      const rest = await measureRow(page, key)
      await evidence(page, `${where}-rest`)
      expect.soft(rest.scrollWidth, `${where}: lane must not overflow sideways (${JSON.stringify(rest)})`).toBeLessThanOrEqual(rest.clientWidth)
      expect.soft(rest.scrollLeft, `${where}: lane is not scrolled sideways`).toBe(0)
      expect.soft(rest.row.right, `${where}: row right edge inside the lane (${JSON.stringify(rest)})`).toBeLessThanOrEqual(rest.lane.right + TOLERANCE)
      expectInsideLane(rest, rest.time, `${where}: timestamp`)
      expect.soft(rest.timeHit, `${where}: the timestamp is not clipped at its own centre (${JSON.stringify(rest)})`).toBe(true)

      // Hover a point near the row's own left edge (a nested row is indented),
      // which is inside the lane even when the row overflows it; the action
      // group (with the close button) only fades in on hover.
      const hoverAt = await page.evaluate((key) => {
        const lane = document.querySelector<HTMLElement>('[data-testid="tree-view-lane"]')!.getBoundingClientRect()
        const r = document.querySelector(`[data-slot-key="${window.CSS.escape(key)}"]`)!
          .querySelector<HTMLElement>('[data-session-row]')!.getBoundingClientRect()
        return { x: Math.max(lane.left, r.left) + 20, y: (r.top + r.bottom) / 2 }
      }, key)
      await page.mouse.move(hoverAt.x, hoverAt.y)
      await expect.poll(async () => (await measureRow(page, key)).closeOpacity, { message: `${where}: close button fades in on hover` }).toBe(1)
      const hovered = await measureRow(page, key)
      expect.soft(hovered.scrollLeft, `${where}: hovering does not scroll the lane sideways`).toBe(0)
      expectInsideLane(hovered, hovered.close, `${where}: close (x) button`)
      expect.soft(hovered.closeHit, `${where}: a click at the close button's centre hits it (${JSON.stringify(hovered)})`).toBe(true)
      await evidence(page, `${where}-hover`)
    }
  })
})
