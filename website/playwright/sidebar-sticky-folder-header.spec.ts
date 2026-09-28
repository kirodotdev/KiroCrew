import { test, expect, Page, APIRequestContext } from '@playwright/test'

/**
 * Folder headers in the Sessions lane pin to the top while their folder is
 * scrolled (ChatSidebar renderFolderHeader, `.folder-row-sticky` in index.css).
 *
 * jsdom has no layout engine, so `position: sticky` can only be proven in a real
 * browser. The measured claims:
 *   1. scrolled into a root folder's sessions, its header sits on the lane's top edge;
 *   2. scrolled into a nested folder, the parent header stays on the top edge and
 *      the nested header sits exactly one header-height below it, which pins the
 *      `--folder-row-sticky-h` offset to the rendered row height;
 *   3. once a folder's block has scrolled past, the next folder's header takes the
 *      top edge and the previous one is pushed off it;
 *   4. the pinned header is opaque and paints the card's own surface, so rows do
 *      not show through it and it does not read as a separate band. That is
 *      checked on every surface that paints the card differently: the default
 *      theme, kiro-light (card on --panel) and /embed/sessions (card on --bg).
 *
 * The nested case is the one that regressed silently in design: FolderBody's
 * inner box used `overflow: hidden`, which made it the nested header's scroll
 * container, so the nested header never stuck to the lane at all.
 *
 * SERIAL-RUN DEPENDENCY: same as sidebar-folder-alignment.spec.ts (the gate runs
 * workers: 1; session-tags-folders.spec.ts wipes folders in its beforeEach).
 */

const TOLERANCE = 1

const seeded = { folders: [] as string[], slots: [] as string[] }

async function seedFolder(request: APIRequestContext, name: string, parentId?: string, defaultAgent?: string) {
  const body: Record<string, string> = { name }
  if (parentId) body.parent_id = parentId
  if (defaultAgent) body.default_agent = defaultAgent
  const res = await request.post('/api/chat/folders', { data: body })
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

/** The gateway's saved theme before a test changed it, restored in afterEach.
 *  The suite can run against a developer's own gateway, so cleanup puts back
 *  what was there rather than a fixed default. */
let savedTheme: { mode: string; color: string } | null = null

test.afterEach(async ({ request }) => {
  if (savedTheme) {
    await request.put('/api/config/theme', { data: savedTheme })
    savedTheme = null
  }
  for (const key of seeded.slots.splice(0)) await request.delete(`/api/chat/slots/${key}`)
  for (const id of seeded.folders.splice(0).reverse()) await request.delete(`/api/chat/folders/${id}`)
})

type Probe = { laneTop: number; tops: Record<string, number | null>; heights: Record<string, number | null>; bg: string | null; cardBg: string | null }

/** Scroll `rowKey` (inside `folderId`'s block) to the lane's vertical middle, wait
 *  for the scroll to settle, and read every header's top relative to the lane. */
async function scrollRowToMiddle(page: Page, folderId: string, rowKey: string, headerIds: string[], evidenceTag: string): Promise<Probe> {
  await page.evaluate(({ folderId, rowKey }) => {
    const lane = document.querySelector<HTMLElement>('[data-testid="tree-view-lane"]')!
    const row = document.querySelector(`[data-folder-drop="${folderId}"]`)!
      .querySelector<HTMLElement>(`[data-slot-key="${window.CSS.escape(rowKey)}"]`)!
    const delta = row.getBoundingClientRect().top - lane.getBoundingClientRect().top
    lane.scrollTop += delta - lane.clientHeight / 2
  }, { folderId, rowKey })
  let prev = ''
  let probe: Probe | null = null
  await expect.poll(async () => {
    probe = await page.evaluate((ids) => {
      const lane = document.querySelector<HTMLElement>('[data-testid="tree-view-lane"]')!
      const laneTop = lane.getBoundingClientRect().top
      const tops: Record<string, number | null> = {}
      const heights: Record<string, number | null> = {}
      let bg: string | null = null
      const card = lane.closest<HTMLElement>('.sidebar-inner')
      const cardBg = card ? getComputedStyle(card).backgroundColor : null
      for (const id of ids) {
        const el = document.querySelector<HTMLElement>(`[data-folder-row="${id}"]`)
        tops[id] = el ? el.getBoundingClientRect().top - laneTop : null
        heights[id] = el ? el.getBoundingClientRect().height : null
        if (el && bg === null) bg = getComputedStyle(el).backgroundColor
      }
      return { laneTop, tops, heights, bg, cardBg }
    }, headerIds)
    const cur = JSON.stringify(probe)
    const stable = cur === prev
    prev = cur
    return stable
  }, { timeout: 10000, message: 'header positions should settle after scrolling' }).toBe(true)
  // Opt-in: STICKY_EVIDENCE_DIR=<dir> saves a still of the lane at each probe,
  // which is how the PR screenshots were captured. The assertions ignore it.
  if (process.env.STICKY_EVIDENCE_DIR) {
    await page.locator('[data-testid="tree-view-lane"]').screenshot({ path: `${process.env.STICKY_EVIDENCE_DIR}/${evidenceTag}-${rowKey}.png` })
  }
  return probe!
}

/** Each surface paints the sidebar card differently, so each needs its own
 *  pinned-row surface; `theme` is the expected `<html data-theme>`. */
const SURFACES = [
  { name: 'default theme', path: '/chat', mode: 'dark', theme: 'kiro-dark' },
  { name: 'kiro-light', path: '/chat', mode: 'light', theme: 'kiro-light' },
  { name: 'embedded sessions view', path: '/embed/sessions', mode: 'dark', theme: 'kiro-dark' },
] as const

test.describe('Sidebar folder headers pin while scrolling', () => {
  // The theme is gateway-wide state, so the surfaces must not run concurrently.
  test.describe.configure({ mode: 'serial' })
  for (const surface of SURFACES) test(`root and nested headers stick to the lane top and hand off at the folder end (${surface.name})`, async ({ page, request }) => {
    await page.addInitScript((mode) => {
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-theme', mode)
      localStorage.setItem('mc-color-theme', 'kiro')
    }, surface.mode)
    // The server's theme config is the source of truth and overrides
    // localStorage on boot, so the theme is set there too (restored in afterEach).
    const current = await request.get('/api/config/theme')
    expect(current.ok(), 'GET /api/config/theme should succeed').toBe(true)
    const { mode, color } = await current.json() as { mode: string; color: string }
    savedTheme = { mode, color }
    const themed = await request.put('/api/config/theme', { data: { mode: surface.mode, color: 'kiro' } })
    expect(themed.ok(), 'PUT /api/config/theme should succeed').toBe(true)
    await page.setViewportSize({ width: 1400, height: 700 })
    const uniq = Date.now().toString(36)
    // A carries a default agent: its badge is the tallest thing a header row
    // renders at rest, so the row-height assertion below covers it.
    const a = await seedFolder(request, `Sticky-A-${uniq}`, undefined, 'default')
    const b = await seedFolder(request, `Sticky-B-${uniq}`, a.id)
    const c = await seedFolder(request, `Sticky-C-${uniq}`)
    const bKeys = await seedSlots(request, b.id, 14)
    const aKeys = await seedSlots(request, a.id, 14)
    const cKeys = await seedSlots(request, c.id, 14)

    await page.goto(surface.path)
    await expect(page.locator('html')).toHaveAttribute('data-theme', surface.theme)
    await expect(page.locator(`[data-folder-row="${c.id}"]`)).toBeVisible({ timeout: 15000 })
    for (const k of [...aKeys, ...bKeys, ...cKeys]) {
      await expect(page.locator(`[data-slot-key="${k}"]`).first()).toBeAttached({ timeout: 15000 })
    }
    const ids = [a.id, b.id, c.id]
    await expect(page.locator(`[data-folder-row="${a.id}"]`).getByText('default', { exact: true })).toBeVisible()

    // Inside the nested folder: A on the top edge, B exactly one row below it.
    const inB = await scrollRowToMiddle(page, b.id, bKeys[Math.floor(bKeys.length / 2)], ids, surface.name.replace(/\W+/g, '-'))
    await expect(page.locator('html'), 'theme must still be applied when measured').toHaveAttribute('data-theme', surface.theme)
    const rowH = inB.heights[a.id]!
    expect(Math.abs(inB.tops[a.id]!), `A pinned at lane top (${JSON.stringify(inB)})`).toBeLessThanOrEqual(TOLERANCE)
    expect(Math.abs(inB.tops[b.id]! - rowH), `B pinned one row (${rowH}px) below A (${JSON.stringify(inB)})`).toBeLessThanOrEqual(TOLERANCE)
    // The offset constant must match the rendered row, or nested headers gap/overlap.
    const cssH = await page.evaluate((id) => parseFloat(getComputedStyle(document.querySelector(`[data-folder-row="${id}"]`)!).getPropertyValue('--folder-row-sticky-h')), a.id)
    expect(Math.abs(cssH - rowH), `--folder-row-sticky-h ${cssH} should equal the header height ${rowH}`).toBeLessThanOrEqual(TOLERANCE)
    // Opaque, and the card's own surface: a transparent pinned row lets the
    // sessions show through, and a different colour reads as a stray band.
    expect(inB.bg, 'pinned header needs an opaque background').not.toMatch(/rgba\(0, 0, 0, 0\)|transparent/)
    expect(inB.bg, `pinned header should paint the card surface ${inB.cardBg}`).toBe(inB.cardBg)
    // Hover swaps in the row's `hover:bg-bg-hover` fill, which must stay opaque
    // too, or the rows scrolled under the pinned header show through it.
    await page.locator(`[data-folder-row="${a.id}"]`).hover()
    const hoverBg = await page.evaluate((id) => getComputedStyle(document.querySelector(`[data-folder-row="${id}"]`)!).backgroundColor, a.id)
    expect(hoverBg, 'hovered pinned header needs an opaque background').not.toMatch(/rgba\(0, 0, 0, 0\)|transparent|rgba\([^)]*, 0(\.\d+)?\)/)
    await page.mouse.move(0, 0)

    // Keyboard roving upward inside the scrolled subfolder: each focused row is
    // scrolled into view BELOW the pinned A and B headers, never behind them.
    await page.locator(`[data-folder-drop="${b.id}"] [data-slot-key="${bKeys[Math.floor(bKeys.length / 2)]}"] [data-session-row]`).first().focus()
    for (let i = 0; i < 8; i++) {
      await page.keyboard.press('ArrowUp')
      const cover = await page.evaluate((bId) => {
        const bHeader = document.querySelector<HTMLElement>(`[data-folder-row="${bId}"]`)!.getBoundingClientRect()
        const row = (document.activeElement as HTMLElement).closest<HTMLElement>('[data-session-row]')!.getBoundingClientRect()
        return { headerBottom: bHeader.bottom, rowTop: row.top }
      }, b.id)
      expect(cover.rowTop, `focused row ${i + 1} should sit below the pinned headers (${JSON.stringify(cover)})`).toBeGreaterThanOrEqual(cover.headerBottom - TOLERANCE)
    }

    // Past B's block, still inside A: A stays pinned, B has been pushed up off its slot.
    // Rows render newest-first, so the earliest-created keys sit at the END of A's block.
    const inA = await scrollRowToMiddle(page, a.id, aKeys[1], ids, surface.name.replace(/\W+/g, '-'))
    expect(Math.abs(inA.tops[a.id]!), `A still pinned (${JSON.stringify(inA)})`).toBeLessThanOrEqual(TOLERANCE)
    expect(inA.tops[b.id]!, `B no longer pinned below A (${JSON.stringify(inA)})`).toBeLessThan(rowH - TOLERANCE)

    // Into C: C owns the top edge, A has scrolled off it.
    const inC = await scrollRowToMiddle(page, c.id, cKeys[Math.floor(cKeys.length / 2)], ids, surface.name.replace(/\W+/g, '-'))
    expect(Math.abs(inC.tops[c.id]!), `C pinned at lane top (${JSON.stringify(inC)})`).toBeLessThanOrEqual(TOLERANCE)
    expect(inC.tops[a.id]!, `A pushed off the top (${JSON.stringify(inC)})`).toBeLessThan(-TOLERANCE)
  })
})
