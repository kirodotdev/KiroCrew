import { test, expect, Page, APIRequestContext } from '@playwright/test'

/**
 * On a phone, a session row's text stops before its ⋮ menu button and that
 * button's touch area, and a tap on the button opens the menu without
 * switching sessions.
 *
 * The phone row draws one ⋮ button at the row's right end (the desktop hover
 * toolbar is a separate branch). It used to be absolutely positioned over the
 * row, so the content column ran to the row's own right padding and a long
 * title painted under the button and under the 44px touch area
 * `mc-touch-hit` grows around it. The text column must end where that touch
 * area begins.
 *
 * jsdom and happy-dom compute no layout, so only a real browser can see this.
 * The measured claims, for the open session's row (selected, then the list
 * reopened), an unselected long row, a short row, and rows in a root and a
 * nested folder, in light and dark themes:
 *   1. the title and the whole content column end at or before the left edge
 *      of the button's touch area;
 *   2. a hit test at the button's centre and just inside its touch area's left
 *      edge lands on the button;
 *   3. a tap on the button opens the row menu and the open session stays the
 *      same one.
 * A desktop control at 1400px checks the hover toolbar still overlays the row
 * out of flow, so the desktop layout is unchanged.
 *
 * SERIAL-RUN DEPENDENCY: same as sidebar-folder-alignment.spec.ts (the gate runs
 * workers: 1; session-tags-folders.spec.ts wipes folders in its beforeEach).
 */

const TOLERANCE = 1
const TOUCH_TARGET_PX = 44
const LONG_TITLE = 'A very long session title that keeps going well past the menu button '.repeat(2).trim()
const SHORT_TITLE = 'Short'

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

async function seedSlot(request: APIRequestContext, title: string, folderId: string | null) {
  const res = await request.post('/api/chat/slots', { data: { agent: 'default' } })
  expect(res.ok(), 'POST /api/chat/slots should succeed').toBe(true)
  const slot = await res.json() as { key: string }
  seeded.slots.push(slot.key)
  const titled = await request.patch(`/api/chat/slots/${slot.key}/title`, { data: { title } })
  expect(titled.ok(), `title for slot ${slot.key} should succeed`).toBe(true)
  if (folderId) {
    const assign = await request.patch(`/api/chat/slots/${slot.key}/folder`, { data: { folder_id: folderId } })
    expect(assign.ok(), `folder assignment for slot ${slot.key} should succeed`).toBe(true)
  }
  return slot.key
}

let savedTheme: { mode: string; color: string } | null = null

test.afterEach(async ({ request }) => {
  if (savedTheme) {
    await request.put('/api/config/theme', { data: savedTheme })
    savedTheme = null
  }
  for (const key of seeded.slots.splice(0)) await request.delete(`/api/chat/slots/${key}`)
  for (const id of seeded.folders.splice(0).reverse()) await request.delete(`/api/chat/folders/${id}`)
})

type Measure = {
  active: boolean
  titleRight: number
  contentRight: number
  button: { left: number; right: number; top: number; bottom: number }
  touchLeft: number
  centreHit: boolean
  touchEdgeHit: boolean
}

async function measureRow(page: Page, key: string): Promise<Measure> {
  return page.evaluate(({ key, touchPx }) => {
    const row = document.querySelector(`[data-slot-key="${window.CSS.escape(key)}"]`)!
      .querySelector<HTMLElement>('[data-session-row]')!
    const title = row.querySelector<HTMLElement>('[data-session-title]')!
    const content = title.parentElement!
    const button = row.querySelector<HTMLElement>('button[aria-label="More options"]')!
    const b = button.getBoundingClientRect()
    // `mc-touch-hit` grows the hit area to 44px under a coarse pointer only.
    const coarse = window.matchMedia('(pointer: coarse)').matches
    const touchLeft = b.left - (coarse ? Math.max(0, (touchPx - b.width) / 2) : 0)
    const lands = (x: number, y: number) => {
      const at = document.elementFromPoint(x, y)
      return !!at && (at === button || button.contains(at))
    }
    const y = (b.top + b.bottom) / 2
    return {
      active: row.classList.contains('session-active'),
      titleRight: title.getBoundingClientRect().right,
      contentRight: content.getBoundingClientRect().right,
      button: { left: b.left, right: b.right, top: b.top, bottom: b.bottom },
      touchLeft,
      centreHit: lands((b.left + b.right) / 2, y),
      touchEdgeHit: lands(touchLeft + 1, y),
    }
  }, { key, touchPx: TOUCH_TARGET_PX })
}

async function activeKey(page: Page) {
  return page.evaluate(() => document.querySelector('[data-session-row].session-active')
    ?.closest('[data-slot-key]')?.getAttribute('data-slot-key') ?? null)
}

/** Opt-in: ROW_MENU_EVIDENCE_DIR=<dir> saves a still of the list per theme,
 *  which is how the PR screenshots were captured. The assertions ignore it. */
async function evidence(page: Page, tag: string) {
  if (!process.env.ROW_MENU_EVIDENCE_DIR) return
  await page.screenshot({ path: `${process.env.ROW_MENU_EVIDENCE_DIR}/${tag.replace(/\W+/g, '-')}.png` })
}

async function scrollRowIntoLane(page: Page, key: string) {
  await page.evaluate((key) => {
    const lane = document.querySelector<HTMLElement>('[data-testid="tree-view-lane"]')!
    const row = document.querySelector(`[data-slot-key="${window.CSS.escape(key)}"]`)!
    lane.scrollTop += row.getBoundingClientRect().top - lane.getBoundingClientRect().top - lane.clientHeight / 2
  }, key)
}

test.describe('Phone session rows keep their menu clear', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  // The theme is gateway-wide state, so the cases must not run concurrently.
  test.describe.configure({ mode: 'serial' })
  const cases = [
    { theme: 'light', width: 390 },
    { theme: 'dark', width: 360 },
    { theme: 'light', width: 430 },
  ] as const
  for (const { theme, width } of cases) {
    test(`${theme} at ${width}px: text stops before the ⋮ touch area and a tap opens the menu without switching`, async ({ page, request }) => {
      await page.setViewportSize({ width, height: 844 })
      await page.addInitScript((theme) => {
        localStorage.setItem('mc-onboarded', '1')
        localStorage.setItem('mc-theme', theme)
        localStorage.setItem('mc-color-theme', 'kiro')
      }, theme)
      // The server's theme config overrides localStorage on boot, so set it
      // there too; afterEach restores it.
      const current = await request.get('/api/config/theme')
      expect(current.ok(), 'GET /api/config/theme should succeed').toBe(true)
      const { mode, color } = await current.json() as { mode: string; color: string }
      savedTheme = { mode, color }
      const themed = await request.put('/api/config/theme', { data: { mode: theme, color: 'kiro' } })
      expect(themed.ok(), 'PUT /api/config/theme should succeed').toBe(true)
      // Unique per test; afterEach removes the folders, so reruns never collide.
      const uniq = test.info().testId.slice(0, 8)
      const root = await seedFolder(request, `Menu-A-${uniq}`)
      const nested = await seedFolder(request, `Menu-B-${uniq}`, root.id)
      const selected = await seedSlot(request, LONG_TITLE, null)
      const rows = [
        { key: selected, where: 'selected long row' },
        { key: await seedSlot(request, LONG_TITLE, null), where: 'unselected long row' },
        { key: await seedSlot(request, SHORT_TITLE, null), where: 'unselected short row' },
        { key: await seedSlot(request, LONG_TITLE, root.id), where: 'long row in a root folder' },
        { key: await seedSlot(request, LONG_TITLE, nested.id), where: 'long row in a nested folder' },
      ]

      // Open the long session from the list, then come back to the list with
      // it still selected: the path the report describes.
      await page.goto('/chat')
      await expect(page.locator('html')).toHaveAttribute('data-theme', `kiro-${theme}`)
      const toggle = page.getByTestId('mobile-topbar-sessions-toggle')
      await expect(toggle).toBeVisible({ timeout: 15000 })
      // A fresh phone load with no session open starts with the list closed.
      await expect(toggle).toHaveAttribute('aria-expanded', 'false')
      await toggle.tap()
      await expect(toggle).toHaveAttribute('aria-expanded', 'true')
      const selectedRow = page.locator(`[data-slot-key="${selected}"] [data-session-row]`).first()
      await expect(selectedRow).toBeVisible({ timeout: 15000 })
      await selectedRow.locator('[data-session-title]').tap()
      await expect.poll(() => activeKey(page), { message: 'tapping the row opens that session' }).toBe(selected)
      // Opening a session closes the phone list; the toggle brings it back.
      await expect(toggle).toHaveAttribute('aria-expanded', 'false')
      await toggle.tap()
      await expect(toggle).toHaveAttribute('aria-expanded', 'true')
      await expect(page.locator(`[data-folder-row="${nested.id}"]`)).toBeVisible({ timeout: 15000 })
      // The drawer slides in; measure only once the lane sits fully on screen.
      await expect.poll(() => page.evaluate(() =>
        document.querySelector('[data-testid="tree-view-lane"]')?.getBoundingClientRect().left ?? -1),
      { message: 'the session list drawer finishes opening' }).toBeGreaterThanOrEqual(0)

      for (const { key, where } of rows) {
        const row = page.locator(`[data-slot-key="${key}"] [data-session-row]`).first()
        await expect(row).toBeAttached({ timeout: 15000 })
        await scrollRowIntoLane(page, key)
        const m = await measureRow(page, key)
        const detail = JSON.stringify(m)
        expect.soft(m.active, `${where}: selection state (${detail})`).toBe(key === selected)
        expect.soft(m.titleRight, `${where}: title ends before the ⋮ touch area (${detail})`).toBeLessThanOrEqual(m.touchLeft + TOLERANCE)
        expect.soft(m.contentRight, `${where}: content column ends before the ⋮ touch area (${detail})`).toBeLessThanOrEqual(m.touchLeft + TOLERANCE)
        expect.soft(m.centreHit, `${where}: a hit at the ⋮ centre lands on the button (${detail})`).toBe(true)
        expect.soft(m.touchEdgeHit, `${where}: a hit just inside the touch area's left edge lands on the button (${detail})`).toBe(true)
      }
      await evidence(page, `phone-${theme}-${width}`)

      for (const { key, where } of rows) {
        await scrollRowIntoLane(page, key)
        const trigger = page.locator(`[data-slot-key="${key}"] [data-session-row] button[aria-label="More options"]`).first()
        await trigger.tap()
        await expect(page.getByRole('menu'), `${where}: the tap opens the row menu`).toBeVisible()
        expect.soft(await activeKey(page), `${where}: the tap does not switch sessions`).toBe(selected)
        // On touch the menu is non-modal (ui/dropdown-menu.tsx); a second tap
        // on its trigger closes it, the way a phone user dismisses it.
        await trigger.tap()
        await expect(page.getByRole('menu')).toBeHidden()
        expect.soft(await activeKey(page), `${where}: closing the menu does not switch sessions`).toBe(selected)
      }
    })
  }

  test.describe('touch tablet', () => {
    // Wider than the phone breakpoint, so the sidebar is docked, not a drawer.
    // The rows still draw the single ⋮: ChatSidebar passes
    // `isMobile: isMobile || isTouchDevice`, and a no-hover screen is touch.
    test.use({ viewport: { width: 1024, height: 768 }, hasTouch: true, isMobile: true })

    test('at 1024px the row text stops before the ⋮ touch area and a tap opens the menu without switching', async ({ page, request }) => {
      await page.addInitScript(() => { localStorage.setItem('mc-onboarded', '1') })
      const uniq = test.info().testId.slice(0, 8)
      const folder = await seedFolder(request, `Tablet-${uniq}`)
      const selected = await seedSlot(request, LONG_TITLE, null)
      const rows = [
        { key: selected, where: 'selected long row' },
        { key: await seedSlot(request, SHORT_TITLE, null), where: 'unselected short row' },
        { key: await seedSlot(request, LONG_TITLE, folder.id), where: 'long row in a folder' },
      ]
      await page.goto('/chat')
      await expect(page.locator(`[data-folder-row="${folder.id}"]`)).toBeVisible({ timeout: 15000 })
      await page.locator(`[data-slot-key="${selected}"] [data-session-row] [data-session-title]`).first().tap()
      await expect.poll(() => activeKey(page), { message: 'tapping the row opens that session' }).toBe(selected)

      for (const { key, where } of rows) {
        await scrollRowIntoLane(page, key)
        const m = await measureRow(page, key)
        const detail = JSON.stringify(m)
        expect.soft(m.titleRight, `${where}: title ends before the ⋮ touch area (${detail})`).toBeLessThanOrEqual(m.touchLeft + TOLERANCE)
        expect.soft(m.contentRight, `${where}: content column ends before the ⋮ touch area (${detail})`).toBeLessThanOrEqual(m.touchLeft + TOLERANCE)
        expect.soft(m.centreHit, `${where}: a hit at the ⋮ centre lands on the button (${detail})`).toBe(true)
      }

      const trigger = page.locator(`[data-slot-key="${rows[1].key}"] [data-session-row] button[aria-label="More options"]`).first()
      await trigger.tap()
      await expect(page.getByRole('menu'), 'the tap opens the row menu').toBeVisible()
      expect.soft(await activeKey(page), 'the tap does not switch sessions').toBe(selected)
      await trigger.tap()
      await expect(page.getByRole('menu')).toBeHidden()
    })
  })

  test.describe('desktop control', () => {
    test.use({ viewport: { width: 1400, height: 800 }, hasTouch: false, isMobile: false })

    test('the hover toolbar still overlays the row out of flow', async ({ page, request }) => {
      await page.addInitScript(() => { localStorage.setItem('mc-onboarded', '1') })
      const key = await seedSlot(request, LONG_TITLE, null)
      await page.goto('/chat')
      const row = page.locator(`[data-slot-key="${key}"] [data-session-row]`).first()
      await expect(row).toBeAttached({ timeout: 15000 })
      const layout = await page.evaluate((key) => {
        const row = document.querySelector(`[data-slot-key="${window.CSS.escape(key)}"]`)!
          .querySelector<HTMLElement>('[data-session-row]')!
        const group = row.querySelector('button[aria-label="More options"]')!.parentElement!
        const content = row.querySelector('[data-session-title]')!.parentElement!.getBoundingClientRect()
        const rr = row.getBoundingClientRect()
        return { position: getComputedStyle(group).position, gap: rr.right - content.right, pad: parseFloat(getComputedStyle(row).paddingRight) }
      }, key)
      expect(layout.position, JSON.stringify(layout)).toBe('absolute')
      // The content column still runs to the row's own right padding.
      expect(Math.abs(layout.gap - layout.pad), JSON.stringify(layout)).toBeLessThanOrEqual(TOLERANCE)
    })
  })
})
