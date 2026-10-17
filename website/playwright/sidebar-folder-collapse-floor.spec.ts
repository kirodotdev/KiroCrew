import { test, expect, Page, APIRequestContext } from '@playwright/test'

/**
 * Collapsing a folder whose header is pinned keeps that header in place
 * (chat-sidebar/stickyCollapse.ts, plus the near-end scroll floor in
 * chat-sidebar/collapseFloor.ts; wired in chat-sidebar/folderBlocks.tsx's list-view toggles).
 *
 * The bug: a list-view folder header is sticky inside its folder block, so with
 * the block's top scrolled off the lane the header sits pinned at the top.
 * Collapsing shrank the block to one row without touching scrollTop, which
 * dropped the header above the viewport and jumped every row below it. Near
 * the end of the list the browser then also clamped scrollTop, sliding the
 * whole list down; a scroll floor at the top of the lane now holds it.
 *
 * jsdom has no layout, so only a real browser can show the header staying put.
 * The measured claims, for both collapse controls (the header toggle and the
 * left-edge rail):
 *   1. before the click, the folder header is pinned (its block top is above it);
 *   2. after the collapse, the header is still visible at the same lane offset;
 *   3. the next folder's header sits directly below it;
 *   4. near the end of the list, scrolling back up gives the reserved space
 *      back (the floor marker is hidden again).
 *
 * SERIAL-RUN DEPENDENCY: same as sidebar-sticky-folder-header.spec.ts.
 */

const TOLERANCE = 1

const seeded = { folders: [] as string[], slots: [] as string[] }

async function seedFolder(request: APIRequestContext, name: string) {
  const res = await request.post('/api/chat/folders', { data: { name } })
  expect(res.ok(), `POST /api/chat/folders "${name}" should succeed`).toBe(true)
  const folder = await res.json()
  seeded.folders.push(folder.id)
  return folder as { id: string }
}

async function seedSlots(request: APIRequestContext, folderId: string, n: number) {
  const keys: string[] = []
  for (let i = 0; i < n; i++) {
    const res = await request.post('/api/chat/slots', { data: { agent: 'default' } })
    expect(res.ok(), 'POST /api/chat/slots should succeed').toBe(true)
    const slot = await res.json()
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

type Probe = { header: number | null; blockTop: number | null; next: number | null; laneH: number }

/** Lane-relative tops of the folder header, its block, and the next folder's header. */
async function probe(page: Page, folderId: string, nextId: string): Promise<Probe> {
  return page.evaluate(({ folderId, nextId }) => {
    const lane = document.querySelector<HTMLElement>('[data-testid="tree-view-lane"]')!
    const laneRect = lane.getBoundingClientRect()
    const top = (sel: string) => {
      const el = document.querySelector<HTMLElement>(sel)
      return el ? el.getBoundingClientRect().top - laneRect.top : null
    }
    return {
      header: top(`[data-folder-row="${folderId}"]`),
      blockTop: top(`[data-folder-drop="${folderId}"]`),
      next: top(`[data-folder-row="${nextId}"]`),
      laneH: laneRect.height,
    }
  }, { folderId, nextId })
}

/** Poll until two consecutive probes match, so scroll and layout have settled. */
async function settled(page: Page, folderId: string, nextId: string): Promise<Probe> {
  let prev = ''
  let last: Probe | null = null
  await expect.poll(async () => {
    last = await probe(page, folderId, nextId)
    const cur = JSON.stringify(last)
    const stable = cur === prev
    prev = cur
    return stable
  }, { timeout: 10000, message: 'sidebar layout should settle' }).toBe(true)
  return last!
}

const CONTROLS = [
  { name: 'header toggle', click: (page: Page, id: string) => page.locator(`[data-folder-row="${id}"] button[aria-expanded="true"]`).first().click() },
  { name: 'folder rail', click: (page: Page, id: string) => page.getByTestId(`folder-rail-${id}`).click() },
] as const

/** Run with Chromium's native scroll anchoring on (the default) and off
 *  (Safari has none), so the fix holds whichever the engine does. Each
 *  anchoring mode is paired with one control rather than crossed with both:
 *  both controls reach the floor through the one
 *  `holdPinnedHeaderThroughCollapse` call site, and the full 2x2x2 product
 *  cost about a minute of the shared E2E run's fixed time bound. */
const CASES = [
  { control: CONTROLS[0], anchoring: 'auto' },
  { control: CONTROLS[1], anchoring: 'none' },
] as const

/** `rows`: a long folder below A keeps the lane scrollable after the collapse.
 *  `tail`: A is the last long folder, so the collapse shortens the list below
 *  the scroll position and the browser clamps scrollTop. */
const BELOW = ['rows', 'tail'] as const

test.describe('Collapsing a pinned folder keeps its header in place', () => {
  test.describe.configure({ mode: 'serial' })
  for (const { control, anchoring } of CASES) for (const below of BELOW) test(`collapse via ${control.name} (overflow-anchor: ${anchoring}, ${below})`, async ({ page, request }, testInfo) => {
    await page.addInitScript(() => { localStorage.setItem('mc-onboarded', '1') })
    await page.setViewportSize({ width: 1400, height: 700 })
    // Unique per case and per retry, so a leftover folder from an earlier
    // attempt can never be mistaken for this one's.
    const uniq = `${testInfo.testId}-${testInfo.retry}`
    // A long folder ABOVE A, as in a real sidebar where the collapsed folder
    // is not the first one.
    const p = await seedFolder(request, `Anchor-0-${uniq}`)
    const pKeys = await seedSlots(request, p.id, 12)
    const a = await seedFolder(request, `Anchor-A-${uniq}`)
    const b = await seedFolder(request, `Anchor-B-${uniq}`)
    const c = await seedFolder(request, `Anchor-C-${uniq}`)
    const aKeys = await seedSlots(request, a.id, 24)
    const bKeys = await seedSlots(request, b.id, 4)
    // Plenty of rows below A, so the lane stays scrollable after the collapse
    // and the browser's scrollTop clamp cannot put the header back by itself.
    const cKeys = below === 'rows' ? await seedSlots(request, c.id, 24) : []

    await page.goto('/chat')
    for (const k of [...pKeys, ...aKeys, ...bKeys, ...cKeys]) {
      await expect(page.locator(`[data-slot-key="${k}"]`).first()).toBeAttached({ timeout: 15000 })
    }

    // Scroll so a row in the middle of A sits mid-lane: A's block top is off
    // screen and its header is pinned.
    await page.evaluate(({ folderId, rowKey, anchoring }) => {
      const lane = document.querySelector<HTMLElement>('[data-testid="tree-view-lane"]')!
      lane.style.overflowAnchor = anchoring
      const row = document.querySelector(`[data-folder-drop="${folderId}"]`)!
        .querySelector<HTMLElement>(`[data-slot-key="${window.CSS.escape(rowKey)}"]`)!
      lane.scrollTop += row.getBoundingClientRect().top - lane.getBoundingClientRect().top - lane.clientHeight / 2
    }, { folderId: a.id, rowKey: aKeys[Math.floor(aKeys.length / 2)], anchoring })
    const before = await settled(page, a.id, b.id)
    expect(before.header! - before.blockTop!, `A's header should be pinned before collapse (${JSON.stringify(before)})`).toBeGreaterThan(50)
    if (process.env.COLLAPSE_EVIDENCE_DIR) {
      await page.locator('[data-testid="tree-view-lane"]').screenshot({ path: `${process.env.COLLAPSE_EVIDENCE_DIR}/${control.name.replace(/\W+/g, "-")}-${anchoring}-${below}-before.png` })
    }

    await control.click(page, a.id)
    await expect(page.locator(`[data-folder-row="${a.id}"] button[aria-expanded="false"]`).first()).toBeAttached()
    const after = await settled(page, a.id, b.id)
    if (process.env.COLLAPSE_EVIDENCE_DIR) {
      await page.locator('[data-testid="tree-view-lane"]').screenshot({ path: `${process.env.COLLAPSE_EVIDENCE_DIR}/${control.name.replace(/\W+/g, "-")}-${anchoring}-${below}-after.png` })
    }

    // The header ends at its own block's top (no longer stuck) and exactly
    // where it was pinned. Without the fix it lands ~280px above the lane.
    expect(Math.abs(after.header! - after.blockTop!), `collapsed header should sit at its block top (${JSON.stringify(after)})`).toBeLessThanOrEqual(TOLERANCE)
    expect(Math.abs(after.header! - before.header!), `collapsed header should stay where it was pinned (${JSON.stringify({ before, after })})`).toBeLessThanOrEqual(TOLERANCE)
    const rowH = await page.locator(`[data-folder-row="${a.id}"]`).evaluate(el => el.getBoundingClientRect().height)
    expect(Math.abs(after.next! - after.header! - rowH), `B's header should sit directly below collapsed A (${JSON.stringify(after)})`).toBeLessThanOrEqual(8)

    // With rows below, the content alone holds the anchored position, so the
    // floor releases on its own once the collapse settles, without a scroll.
    const lane = page.locator('[data-testid="tree-view-lane"]')
    const floorDisplay = () => lane.evaluate(el => {
      const marker = el.querySelector<HTMLElement>(':scope > [data-collapse-floor] > *')
      return marker ? getComputedStyle(marker).display : 'missing'
    })
    if (below === 'rows') {
      await expect.poll(floorDisplay, { message: 'scroll floor should release once settled when rows fill the lane' }).toBe('none')
    }

    // Past the floor's settle check, and after a small upward scroll, the
    // collapsed header must still be where it was held: neither the settle
    // check nor the scroll-up check may let the browser clamp scrollTop.
    // The rows case releases at the settle check, the tail case marks the
    // anchor `settled` and keeps the floor.
    await expect.poll(() => lane.evaluate(el => {
      const anchor = el.querySelector<HTMLElement>(':scope > [data-collapse-floor]')!
      const marker = anchor.firstElementChild as HTMLElement
      return anchor.getAttribute('data-collapse-floor') === 'settled' || getComputedStyle(marker).display === 'none'
    }), { message: 'scroll floor settle check should run' }).toBe(true)
    const pastSettle = await settled(page, a.id, b.id)
    expect(Math.abs(pastSettle.header! - before.header!), `collapsed header should stay held past the settle check (${JSON.stringify({ before, pastSettle })})`).toBeLessThanOrEqual(TOLERANCE)
    const nudge = 20
    await lane.evaluate((el, d) => { el.scrollTop -= d }, nudge)
    const nudged = await settled(page, a.id, b.id)
    expect(Math.abs(nudged.header! - before.header! - nudge), `a ${nudge}px scroll-up should move the header down by exactly ${nudge}px (${JSON.stringify({ before, nudged })})`).toBeLessThanOrEqual(TOLERANCE)

    // Scrolling back to the top gives any reserved bottom space back: the
    // floor marker is hidden again.
    await lane.evaluate(el => { el.scrollTop = 0 })
    await expect.poll(floorDisplay, { message: 'scroll floor should be released after scrolling back up' }).toBe('none')
  })
})
