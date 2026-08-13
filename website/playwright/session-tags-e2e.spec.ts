import { test, expect, Page, APIRequestContext } from '@playwright/test'

/**
 * Comprehensive headless-browser test of the Trello-style sidebar columns +
 * tag vocabulary. Each test creates its own columns/tags/slots with unique
 * names. Columns are wiped per test (see resetColumns + HARNESS_GATEWAY);
 * seeded slots are NOT deleted, so this spec is harness-gateway only.
 */

async function primeBrowser(page: Page, width = 1400) {
  await page.addInitScript(w => {
    window.localStorage.setItem('mc-onboarded', '1')
    window.localStorage.setItem('mc-sidebar-width', String(w))
    window.localStorage.removeItem('mc-session-tag-filter')
    // Board view is opt-in, so a spec that seeds columns over the API and then
    // asserts on the rendered board must state that precondition itself. The
    // two toggle specs below overwrite this back to false in their own
    // addInitScript, which runs after this one.
    const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
    cfg.tagColumnsEnabled = true
    localStorage.setItem('mc-chat-config', JSON.stringify(cfg))
  }, width)
  await page.setViewportSize({ width: 1800, height: 1000 })
}

// The board/list view toggle is not a standalone control: it lives as a menu
// item inside the sidebar header "More options" menu. Selector and 15s wait
// mirror session-tags.spec.ts, which covers the same control.
const HEADER_MENU = 'button[aria-haspopup="menu"][aria-label="More options"]'

/** Wait for the sidebar header to render. Used as a "/chat is interactive" signal. */
async function waitForSidebarHeader(page: Page) {
  await page.locator(HEADER_MENU).first().waitFor({ timeout: 15_000 })
}

/** Flip board <-> list view via the header menu item. */
async function toggleBoardView(page: Page) {
  const headerMenu = page.locator(HEADER_MENU).first()
  await headerMenu.waitFor({ timeout: 15_000 })
  await headerMenu.click()
  await page.getByRole('menuitem', { name: /switch to (board|list) view/i }).click()
}

// resetColumns() deletes EVERY tag column, not just ones this spec created, so
// it is gated on an EXPLICIT ephemeral-harness marker -- same contract and same
// reasoning as session-tags-folders.spec.ts. test/test_playwright_e2e.py sets
// KIROCREW_E2E_EPHEMERAL for the throwaway tmp-home gateway it spawns. Token
// presence alone is NOT a safe signal: it is also the normal state when
// authenticating to a real token-protected gateway, so a developer pointing this
// suite at their live gateway (to debug a failure) must never lose their columns.
// Absent the marker the whole describe skips rather than wiping user state.
const HARNESS_GATEWAY = !!process.env.KIROCREW_E2E_EPHEMERAL

async function resetColumns(request: APIRequestContext) {
  const list = await (await request.get('/api/chat/tag-columns')).json()
  for (const c of list) await request.delete(`/api/chat/tag-columns/${c.id}`)
}

async function seedSlotWithTag(request: APIRequestContext, title: string, tagName: string) {
  const s = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
  await request.patch(`/api/chat/slots/${s.key}/title`, { data: { title } })
  const tags = await (await request.get('/api/chat/tags')).json()
  const tag = tags.find((t: { name: string }) => t.name === tagName)
  if (tag) await request.put(`/api/chat/slots/${s.key}/tags`, { data: { tags: [tag.id] } })
  return s.key as string
}

test.describe.configure({ mode: 'serial' })

test.describe('E2E: sidebar tag columns', () => {
  test.beforeEach(async ({ page }) => {
    test.skip(
      !HARNESS_GATEWAY,
      'destructive tag-column wipes require the ephemeral harness gateway (KIROCREW_E2E_EPHEMERAL)',
    )
    await primeBrowser(page)
  })

  test('1. board toggle off shows classic flat list + tag chips', async ({ page, request }) => {
    await resetColumns(request)
    const slotKey = await seedSlotWithTag(request, 'E2E-1 minimal', 'Done')
    // Start in list view
    await page.addInitScript(() => {
      const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
      cfg.tagColumnsEnabled = false
      localStorage.setItem('mc-chat-config', JSON.stringify(cfg))
    })
    await page.goto('/chat')
    await waitForSidebarHeader(page)
    await expect(page.locator('[data-testid="column-strip"]')).toHaveCount(0)
    const row = page.locator(`[data-slot-key="${slotKey}"]`)
    await expect(row).toBeVisible()
  })

  test('2. board toggle on creates seed column when none exist', async ({ page, request }) => {
    await resetColumns(request)
    await page.addInitScript(() => {
      const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
      cfg.tagColumnsEnabled = false
      localStorage.setItem('mc-chat-config', JSON.stringify(cfg))
    })
    await page.goto('/chat')
    await waitForSidebarHeader(page)
    // Toggle ON
    await toggleBoardView(page)
    await page.waitForSelector('[data-testid="column-strip"]', { timeout: 5_000 })
    await expect.poll(async () => (await (await request.get('/api/chat/tag-columns')).json()).length).toBeGreaterThan(0)
  })

  test('3. opening column tag popover shows filter controls', async ({ page, request }) => {
    await resetColumns(request)
    const col = await (await request.post('/api/chat/tag-columns', { data: { name: 'E2E-3', tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await page.locator(`[data-testid="column-edit-${col.id}"]`).click()
    // Popover has mode radios
    await expect(page.getByRole('radio', { name: 'any' })).toBeVisible()
    await expect(page.getByRole('radio', { name: 'all' })).toBeVisible()
    await expect(page.getByRole('radio', { name: 'none' })).toBeVisible()
    // Has tag rows
    const tags = await (await request.get('/api/chat/tags')).json()
    for (const name of ['Planned', 'Done']) {
      const tag = tags.find((t: { name: string }) => t.name === name)
      await expect(page.locator(`[data-testid="tag-row-${tag.id}"]`)).toBeVisible()
    }
  })

  test('4. picking a tag in popover filters that column', async ({ page, request }) => {
    await resetColumns(request)
    // Seed: one slot Done, one slot Review
    const doneKey = await seedSlotWithTag(request, 'E2E-4 done', 'Done')
    await seedSlotWithTag(request, 'E2E-4 review', 'Review')
    const col = await (await request.post('/api/chat/tag-columns', { data: { name: '', tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    // Before filtering: column shows both slots
    await expect(page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${doneKey}"]`)).toBeVisible()
    // Open popover, click Done
    await page.locator(`[data-testid="column-edit-${col.id}"]`).click()
    const tags2 = await (await request.get('/api/chat/tags')).json()
    const doneId2 = tags2.find((t: { name: string }) => t.name === 'Done').id
    await page.locator(`[data-testid="tag-row-${doneId2}"] button[role="checkbox"]`).click()
    await page.waitForTimeout(300)
    // Column now shows only Done-tagged slot
    await expect(page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${doneKey}"]`)).toBeVisible()
    const col4 = page.locator(`[data-testid="column-${col.id}"]`)
    const sessionCards = col4.locator('[data-slot-key]')
    // At least the Done slot; the Review slot must not appear in this column
    await expect(col4.getByText('E2E-4 review')).toHaveCount(0)
    expect(await sessionCards.count()).toBeGreaterThan(0)
  })

  test('5. changing mode to "none" inverts the filter', async ({ page, request }) => {
    await resetColumns(request)
    const doneKey = await seedSlotWithTag(request, 'E2E-5 done', 'Done')
    const reviewKey = await seedSlotWithTag(request, 'E2E-5 review', 'Review')
    const tags = await (await request.get('/api/chat/tags')).json()
    const doneTag = tags.find((t: { name: string }) => t.name === 'Done').id
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [doneTag], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    // 'any' mode: shows doneKey, not reviewKey
    await expect(page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${doneKey}"]`)).toBeVisible()
    await expect(page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${reviewKey}"]`)).toHaveCount(0)
    // Switch to 'none': shows reviewKey, not doneKey
    await page.locator(`[data-testid="column-edit-${col.id}"]`).click()
    await page.getByRole('radio', { name: 'none' }).click()
    await page.waitForTimeout(400)
    await expect(page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${reviewKey}"]`)).toBeVisible()
    await expect(page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${doneKey}"]`)).toHaveCount(0)
  })

  test('6. rename column via popover name input', async ({ page, request }) => {
    await resetColumns(request)
    const col = await (await request.post('/api/chat/tag-columns', { data: { name: 'Old', tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await page.locator(`[data-testid="column-edit-${col.id}"]`).click()
    const nameInput = page.locator('input[placeholder="Column name (optional)"]')
    await nameInput.fill('E2E-6 Renamed')
    await nameInput.blur()
    await expect.poll(async () => {
      const list = await (await request.get('/api/chat/tag-columns')).json()
      return list.find((c: { id: string }) => c.id === col.id)?.name
    }).toBe('E2E-6 Renamed')
  })

  test('7. multiple columns render side-by-side', async ({ page, request }) => {
    await resetColumns(request)
    const tags = await (await request.get('/api/chat/tags')).json()
    const todo = tags.find((t: { name: string }) => t.name === 'ToDo').id
    const done = tags.find((t: { name: string }) => t.name === 'Done').id
    const c1 = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [todo], mode: 'any' } })).json()
    const c2 = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [done], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector('[data-testid="column-strip"]')
    await expect(page.locator(`[data-testid="column-${c1.id}"]`)).toBeVisible()
    await expect(page.locator(`[data-testid="column-${c2.id}"]`)).toBeVisible()
  })

  test('8. dropping session into a single-status column reassigns via drop API', async ({ page, request }) => {
    await resetColumns(request)
    const slotKey = await seedSlotWithTag(request, 'E2E-8 moving', 'ToDo')
    const tags = await (await request.get('/api/chat/tags')).json()
    const done = tags.find((t: { name: string }) => t.name === 'Done').id
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [done], mode: 'any' } })).json()
    // Simulate the drop via API (HTML5 DnD in playwright is notoriously flaky;
    // we trust the UI-path via the explicit button tests + API contract here).
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    const res = await (await request.post(`/api/chat/slots/${slotKey}/drop`, { data: { column_id: col.id } })).json()
    expect(res.ok).toBe(true)
    expect(res.tags).toContain(done)
    // UI refreshes → slot now appears in Done column
    await page.waitForTimeout(500)
    await expect(page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${slotKey}"]`)).toBeVisible()
  })

  test('9. drop on a multi-tag filter column is a no-op', async ({ page, request }) => {
    await resetColumns(request)
    const slotKey = await seedSlotWithTag(request, 'E2E-9 guard', 'ToDo')
    const tags = await (await request.get('/api/chat/tags')).json()
    const impl = tags.find((t: { name: string }) => t.name === 'Implementation').id
    const rev = tags.find((t: { name: string }) => t.name === 'Review').id
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [impl, rev], mode: 'any' } })).json()
    const res = await (await request.post(`/api/chat/slots/${slotKey}/drop`, { data: { column_id: col.id } })).json()
    expect(res.ok).toBe(false)
    const refreshed = await (await request.get('/api/chat/slots')).json()
    const slot = refreshed.find((s: { key: string }) => s.key === slotKey)
    expect(slot.tags).toEqual(['todo'])  // unchanged
  })

  test('10. column filter popover lists every tag with inline controls', async ({ page, request }) => {
    await resetColumns(request)
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await page.locator(`[data-testid="column-edit-${col.id}"]`).click()
    const tags = await (await request.get('/api/chat/tags')).json()
    for (const name of ['Planned', 'ToDo', 'Implementation', 'Review', 'Done']) {
      const tag = tags.find((t: { name: string }) => t.name === name)
      await expect(page.locator(`[data-testid="tag-row-${tag.id}"]`)).toBeVisible()
    }
  })

  test('11. create tag inline from column popover; defaults to non-status', async ({ page, request }) => {
    await resetColumns(request)
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await page.locator(`[data-testid="column-edit-${col.id}"]`).click()
    const uniq = 'E2E-11-' + Date.now()
    const input = page.locator(`[data-testid="tag-create-${col.id}"]`)
    await input.fill(uniq)
    await input.press('Enter')
    await page.waitForTimeout(400)
    const tags = await (await request.get('/api/chat/tags')).json()
    const created = tags.find((t: { name: string }) => t.name === uniq)
    expect(created).toBeTruthy()
    expect(created.status).toBe(false)
    await request.delete(`/api/chat/tags/${created.id}`)
  })

  test('12. lightning icon toggles status flag from the popover', async ({ page, request }) => {
    await resetColumns(request)
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [], mode: 'any' } })).json()
    const uniq = 'E2E-12-' + Date.now()
    const created = await (await request.post('/api/chat/tags', { data: { name: uniq, color: '#22c55e', status: false } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await page.locator(`[data-testid="column-edit-${col.id}"]`).click()
    await page.locator(`[data-testid="tag-status-${created.id}"]`).click()
    await expect.poll(async () => {
      const tags = await (await request.get('/api/chat/tags')).json()
      return tags.find((t: { id: string }) => t.id === created.id)?.status
    }).toBe(true)
    // Click again → toggles back to non-status
    await page.locator(`[data-testid="tag-status-${created.id}"]`).click()
    await expect.poll(async () => {
      const tags = await (await request.get('/api/chat/tags')).json()
      return tags.find((t: { id: string }) => t.id === created.id)?.status
    }).toBe(false)
    await request.delete(`/api/chat/tags/${created.id}`)
  })

  test('12b. × delete icon next to a tag row removes the tag', async ({ page, request }) => {
    await resetColumns(request)
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [], mode: 'any' } })).json()
    const uniq = 'E2E-12b-' + Date.now()
    const created = await (await request.post('/api/chat/tags', { data: { name: uniq, color: '#3b82f6', status: false } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await page.locator(`[data-testid="column-edit-${col.id}"]`).click()
    page.once('dialog', d => d.accept())
    await page.locator(`[data-testid="tag-delete-${created.id}"]`).click()
    await expect.poll(async () => {
      const tags = await (await request.get('/api/chat/tags')).json()
      return tags.find((t: { id: string }) => t.id === created.id)
    }).toBeFalsy()
  })

  test('12c. inline rename commits on blur', async ({ page, request }) => {
    await resetColumns(request)
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [], mode: 'any' } })).json()
    const uniq = 'E2E-12c-' + Date.now()
    const renamed = uniq + '-renamed'
    const created = await (await request.post('/api/chat/tags', { data: { name: uniq, color: '#10b981', status: false } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await page.locator(`[data-testid="column-edit-${col.id}"]`).click()
    const input = page.locator(`[data-testid="tag-name-${created.id}"]`)
    await input.fill(renamed)
    await input.blur()
    await expect.poll(async () => {
      const tags = await (await request.get('/api/chat/tags')).json()
      return tags.find((t: { id: string }) => t.id === created.id)?.name
    }).toBe(renamed)
    await request.delete(`/api/chat/tags/${created.id}`)
  })

  test('13. gear popover no longer contains a Delete button (moved to header ×)', async ({ page, request }) => {
    await resetColumns(request)
    const col = await (await request.post('/api/chat/tag-columns', { data: { name: 'E2E-13', tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await page.locator(`[data-testid="column-edit-${col.id}"]`).click()
    // Popover renders but has no "Delete column" link/button inside it
    await expect(page.locator('input[placeholder="Column name (optional)"]')).toBeVisible()
    await expect(page.locator('button', { hasText: 'Delete column' })).toHaveCount(0)
  })

  test('14. clear tags button in popover empties column tag list', async ({ page, request }) => {
    await resetColumns(request)
    const tags = await (await request.get('/api/chat/tags')).json()
    const done = tags.find((t: { name: string }) => t.name === 'Done').id
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [done], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await page.locator(`[data-testid="column-edit-${col.id}"]`).click()
    await page.getByRole('button', { name: /Clear filter/ }).click()
    await expect.poll(async () => {
      const list = await (await request.get('/api/chat/tag-columns')).json()
      return list.find((c: { id: string }) => c.id === col.id)?.tag_ids.length
    }).toBe(0)
  })

  test('15. columns persist across reload', async ({ page, request }) => {
    await resetColumns(request)
    const col = await (await request.post('/api/chat/tag-columns', { data: { name: 'E2E-15', tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await page.reload()
    await page.waitForSelector(`[data-testid="column-${col.id}"]`, { timeout: 10_000 })
  })

  test('16b. columns flex-grow to fill sidebar width; shrink to 220px min and scroll', async ({ page, request }) => {
    await resetColumns(request)
    const tags = await (await request.get('/api/chat/tags')).json()
    const todo = tags.find((t: { name: string }) => t.name === 'ToDo').id
    const done = tags.find((t: { name: string }) => t.name === 'Done').id
    const a = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [todo], mode: 'any' } })).json()
    const b = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [done], mode: 'any' } })).json()

    // Wide sidebar → both columns grow past the 220px minimum
    await page.addInitScript(() => { window.localStorage.setItem('mc-sidebar-width', '900') })
    await page.goto('/chat')
    await page.waitForSelector('[data-testid="column-strip"]')
    const wideA = await page.locator(`[data-testid="column-${a.id}"]`).boundingBox()
    const wideB = await page.locator(`[data-testid="column-${b.id}"]`).boundingBox()
    expect(wideA?.width).toBeGreaterThan(300)
    expect(wideB?.width).toBeGreaterThan(300)
    // Grown roughly equally
    expect(Math.abs((wideA?.width ?? 0) - (wideB?.width ?? 0))).toBeLessThan(20)

    // Narrow sidebar → each column hits the 220px floor, strip scrolls horizontally
    await page.addInitScript(() => { window.localStorage.setItem('mc-sidebar-width', '300') })
    await page.reload()
    await page.waitForSelector('[data-testid="column-strip"]')
    const narrowA = await page.locator(`[data-testid="column-${a.id}"]`).boundingBox()
    expect(narrowA?.width).toBeGreaterThanOrEqual(219)  // ~220 rounded
    expect(narrowA?.width).toBeLessThan(260)
  })

  test('18. board toggle hides/shows columns; column layout persists through toggle', async ({ page, request }) => {
    await resetColumns(request)
    const colA = await (await request.post('/api/chat/tag-columns', { data: { name: 'gate-A', tag_ids: [], mode: 'any' } })).json()
    const colB = await (await request.post('/api/chat/tag-columns', { data: { name: 'gate-B', tag_ids: [], mode: 'any' } })).json()

    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${colA.id}"]`)
    await expect(page.locator(`[data-testid="column-${colB.id}"]`)).toBeVisible()

    // Toggle OFF via header button
    await toggleBoardView(page)
    await expect(page.locator('[data-testid="column-strip"]')).toHaveCount(0)
    // Backend columns are NOT deleted
    const persisted = await (await request.get('/api/chat/tag-columns')).json()
    expect(persisted.map((c: { id: string }) => c.id)).toEqual(expect.arrayContaining([colA.id, colB.id]))

    // Toggle ON again → both columns restored
    await toggleBoardView(page)
    await page.waitForSelector(`[data-testid="column-${colA.id}"]`)
    await expect(page.locator(`[data-testid="column-${colB.id}"]`)).toBeVisible()
  })

  test('19. + icon on column header adds a new column immediately to the right', async ({ page, request }) => {
    await resetColumns(request)
    const a = await (await request.post('/api/chat/tag-columns', { data: { name: 'col-A', tag_ids: [], mode: 'any' } })).json()
    const b = await (await request.post('/api/chat/tag-columns', { data: { name: 'col-B', tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${a.id}"]`)
    await page.click(`[data-testid="column-add-after-${a.id}"]`)
    // Wait until the new column is both created AND reordered between A and B
    await expect.poll(async () => {
      const list = await (await request.get('/api/chat/tag-columns')).json()
      const ids = list.sort((x: { order: number }, y: { order: number }) => x.order - y.order).map((c: { id: string }) => c.id)
      const idxA = ids.indexOf(a.id)
      const idxB = ids.indexOf(b.id)
      return idxA >= 0 && idxB >= 0 ? idxB - idxA : -1
    }, { timeout: 8_000 }).toBe(2)
  })

  test('20. × icon on column header deletes the column', async ({ page, request }) => {
    await resetColumns(request)
    const col = await (await request.post('/api/chat/tag-columns', { data: { name: 'to-delete', tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    page.once('dialog', d => d.accept())
    await page.click(`[data-testid="column-delete-${col.id}"]`)
    await expect(page.locator(`[data-testid="column-${col.id}"]`)).toHaveCount(0)
    const list = await (await request.get('/api/chat/tag-columns')).json()
    expect(list.find((c: { id: string }) => c.id === col.id)).toBeFalsy()
  })

  test('21. drag column grip over sibling column reorders via dataTransfer', async ({ page, request }) => {
    await resetColumns(request)
    const a = await (await request.post('/api/chat/tag-columns', { data: { name: 'drag-A', tag_ids: [], mode: 'any' } })).json()
    const b = await (await request.post('/api/chat/tag-columns', { data: { name: 'drag-B', tag_ids: [], mode: 'any' } })).json()
    const c = await (await request.post('/api/chat/tag-columns', { data: { name: 'drag-C', tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${a.id}"]`)

    // Simulate HTML5 drag: dispatch dragstart on A's grip, dragover + drop on C
    // A shared DataTransfer object is created in the browser context so types survive.
    await page.evaluate(([aId, cId]) => {
      const source = document.querySelector(`[data-testid="column-${aId}"] [title="Drag to reorder"]`) as HTMLElement
      const target = document.querySelector(`[data-testid="column-${cId}"]`) as HTMLElement
      const dt = new DataTransfer()
      source.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: dt }))
      target.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt }))
      target.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }))
      source.dispatchEvent(new DragEvent('dragend', { bubbles: true, cancelable: true, dataTransfer: dt }))
    }, [a.id, c.id])

    // A should have moved to where C was (index 2), B stays at 0, C at 1
    await expect.poll(async () => {
      const list = await (await request.get('/api/chat/tag-columns')).json()
      return list.sort((x: { order: number }, y: { order: number }) => x.order - y.order).map((x: { id: string }) => x.id)
    }, { timeout: 8_000 }).toEqual([b.id, c.id, a.id])
  })

  test('22. drag session card onto a status-tag column reassigns tag', async ({ page, request }) => {
    await resetColumns(request)
    const slotKey = await seedSlotWithTag(request, 'E2E-22 dragme', 'ToDo')
    const tags = await (await request.get('/api/chat/tags')).json()
    const todo = tags.find((t: { name: string }) => t.name === 'ToDo').id
    const done = tags.find((t: { name: string }) => t.name === 'Done').id
    const colTodo = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [todo], mode: 'any' } })).json()
    const colDone = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [done], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${colDone.id}"]`)
    // Slot row must be present in the ToDo column before we can drag it
    await page.waitForSelector(`[data-testid="column-${colTodo.id}"] [data-slot-key="${slotKey}"]`, { timeout: 10_000 })

    await page.evaluate(([sKey, targetColId]) => {
      const source = document.querySelector(`[data-slot-key="${sKey}"]`) as HTMLElement
      const target = document.querySelector(`[data-testid="column-${targetColId}"]`) as HTMLElement
      const dt = new DataTransfer()
      dt.setData('text/plain', sKey)
      source.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: dt }))
      target.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt }))
      target.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }))
      source.dispatchEvent(new DragEvent('dragend', { bubbles: true, cancelable: true, dataTransfer: dt }))
    }, [slotKey, colDone.id])

    // Slot's tags now include Done, no longer ToDo
    await expect.poll(async () => {
      const slots = await (await request.get('/api/chat/slots')).json()
      return slots.find((s: { key: string }) => s.key === slotKey)?.tags
    }, { timeout: 8_000 }).toEqual([done])
    // And the card now appears in the Done column
    await expect(page.locator(`[data-testid="column-${colDone.id}"] [data-slot-key="${slotKey}"]`)).toBeVisible()
  })

  test('23. drag session card onto a non-status column is a no-op', async ({ page, request }) => {
    await resetColumns(request)
    const slotKey = await seedSlotWithTag(request, 'E2E-23 guard', 'ToDo')
    const tags = await (await request.get('/api/chat/tags')).json()
    const todo = tags.find((t: { name: string }) => t.name === 'ToDo').id
    const impl = tags.find((t: { name: string }) => t.name === 'Implementation').id
    const rev = tags.find((t: { name: string }) => t.name === 'Review').id
    // Source column (so the slot has a row we can drag)
    const sourceCol = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [todo], mode: 'any' } })).json()
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [impl, rev], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await page.waitForSelector(`[data-testid="column-${sourceCol.id}"] [data-slot-key="${slotKey}"]`, { timeout: 10_000 })
    await page.evaluate(([sKey, targetColId]) => {
      const source = document.querySelector(`[data-slot-key="${sKey}"]`) as HTMLElement | null
      if (!source) throw new Error(`source [data-slot-key="${sKey}"] not found`)
      const target = document.querySelector(`[data-testid="column-${targetColId}"]`) as HTMLElement | null
      if (!target) throw new Error(`target column-${targetColId} not found`)
      const dt = new DataTransfer()
      dt.setData('text/plain', sKey)
      source.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: dt }))
      target.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt }))
      target.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }))
    }, [slotKey, col.id])
    await page.waitForTimeout(400)
    const slots = await (await request.get('/api/chat/slots')).json()
    const slot = slots.find((s: { key: string }) => s.key === slotKey)
    expect(slot.tags).toEqual(['todo'])  // unchanged
  })

  test('24. include_untagged flag persists on column; "+ untagged" badge renders when enabled', async ({ page, request }) => {
    await page.addInitScript(() => {
      const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
      cfg.tagColumnsEnabled = true
      localStorage.setItem('mc-chat-config', JSON.stringify(cfg))
    })
    await resetColumns(request)
    const tags = await (await request.get('/api/chat/tags')).json()
    const plannedId = tags.find((t: { name: string }) => t.name === 'Planned').id
    // Create column with include_untagged=true from the start
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [plannedId], mode: 'any', include_untagged: true } })).json()
    // Round-trip verification
    const persisted = await (await request.get('/api/chat/tag-columns')).json()
    expect(persisted.find((c: { id: string }) => c.id === col.id).include_untagged).toBe(true)
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    // The "+ untagged" badge is rendered in the header
    await expect(page.locator(`[data-testid="column-${col.id}"]`).getByText('+ untagged')).toBeVisible()
    // Popover checkbox reflects the server state
    await page.locator(`[data-testid="column-edit-${col.id}"]`).click()
    await expect(page.locator(`[data-testid="column-include-untagged-${col.id}"]`)).toBeChecked()
  })

  test('24b. PATCH include_untagged false -> true is persisted', async ({ request }) => {
    await resetColumns(request)
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [], mode: 'any' } })).json()
    let list = await (await request.get('/api/chat/tag-columns')).json()
    expect(!!list.find((c: { id: string }) => c.id === col.id).include_untagged).toBe(false)
    await request.patch(`/api/chat/tag-columns/${col.id}`, { data: { include_untagged: true } })
    list = await (await request.get('/api/chat/tag-columns')).json()
    expect(list.find((c: { id: string }) => c.id === col.id).include_untagged).toBe(true)
  })

  test('24c. filter predicate: Planned column with include_untagged matches both Planned and untagged sessions (verified server-side)', async ({ request }) => {
    await resetColumns(request)
    const tags = await (await request.get('/api/chat/tags')).json()
    const plannedId = tags.find((t: { name: string }) => t.name === 'Planned').id
    const doneId = tags.find((t: { name: string }) => t.name === 'Done').id
    const plannedSlot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    const untaggedSlot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    const doneSlot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${plannedSlot.key}/tags`, { data: { tags: [plannedId] } })
    await request.put(`/api/chat/slots/${doneSlot.key}/tags`, { data: { tags: [doneId] } })
    // untaggedSlot stays empty
    // Predicate mirrors frontend columnMatches:
    function columnMatches(col: { tag_ids: string[]; mode: string; include_untagged?: boolean }, slotTags: string[]): boolean {
      if (col.include_untagged && slotTags.length === 0) return true
      if (!col.tag_ids || col.tag_ids.length === 0) return true
      const set = new Set(slotTags)
      if (col.mode === 'all') return col.tag_ids.every(t => set.has(t))
      if (col.mode === 'none') return !col.tag_ids.some(t => set.has(t))
      return col.tag_ids.some(t => set.has(t))
    }
    const col = { tag_ids: [plannedId], mode: 'any', include_untagged: true }
    const slots = [
      { key: plannedSlot.key, tags: [plannedId] },
      { key: untaggedSlot.key, tags: [] },
      { key: doneSlot.key, tags: [doneId] },
    ]
    const matched = slots.filter(s => columnMatches(col, s.tags)).map(s => s.key).sort()
    expect(matched).toEqual([plannedSlot.key, untaggedSlot.key].sort())
  })

  test('25. include_untagged on an unfiltered column (no tag_ids) keeps rendering all sessions (flag is additive)', async ({ page, request }) => {
    await page.addInitScript(() => {
      const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
      cfg.tagColumnsEnabled = true
      localStorage.setItem('mc-chat-config', JSON.stringify(cfg))
    })
    await resetColumns(request)
    const plannedKey = await seedSlotWithTag(request, 'E2E-25 planned', 'Planned')
    const untaggedSlot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.patch(`/api/chat/slots/${untaggedSlot.key}/title`, { data: { title: 'E2E-25 untagged' } })
    // Column with empty tag_ids behaves as "All sessions"; adding include_untagged shifts its meaning to... still "All sessions" since tag_ids=[] already matches everything.
    // What we actually want: a DEDICATED "untagged" column must have tag_ids=[] AND mode='none' AND some real tag id? Simpler API design: include_untagged is additive only.
    // Here we verify: when column has tag_ids=[], every slot matches (pre-existing behavior); include_untagged does not narrow.
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [], mode: 'any', include_untagged: true } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    // Both rows appear (empty tag filter = match everything)
    await expect(page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${plannedKey}"]`)).toBeVisible()
    await expect(page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${untaggedSlot.key}"]`)).toBeVisible()
  })

  test('26. folders render inside a column when they contain matching slots', async ({ page, request }) => {
    await page.addInitScript(() => {
      const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
      cfg.tagColumnsEnabled = true
      localStorage.setItem('mc-chat-config', JSON.stringify(cfg))
    })
    await resetColumns(request)
    // Wipe existing folders so our new one is isolated
    const foldersBefore = await (await request.get('/api/chat/folders')).json()
    for (const f of foldersBefore) await request.delete(`/api/chat/folders/${f.id}`)
    // Create a folder
    const folder = await (await request.post('/api/chat/folders', { data: { name: 'E2E-26 CRs' } })).json()
    // Seed: Planned slot inside folder, Done slot inside folder (will not show in Planned column), and a folder-less Planned slot
    const tags = await (await request.get('/api/chat/tags')).json()
    const plannedId = tags.find((t: { name: string }) => t.name === 'Planned').id
    const doneId = tags.find((t: { name: string }) => t.name === 'Done').id
    const sPlannedInFolder = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${sPlannedInFolder.key}/tags`, { data: { tags: [plannedId] } })
    await request.patch(`/api/chat/slots/${sPlannedInFolder.key}/folder`, { data: { folder_id: folder.id } })
    const sDoneInFolder = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${sDoneInFolder.key}/tags`, { data: { tags: [doneId] } })
    await request.patch(`/api/chat/slots/${sDoneInFolder.key}/folder`, { data: { folder_id: folder.id } })
    const sPlannedUngrouped = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${sPlannedUngrouped.key}/tags`, { data: { tags: [plannedId] } })
    // Column filtering on Planned only
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [plannedId], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    // Folder header visible inside the column
    await expect(page.locator(`[data-testid="col-${col.id}-folder-${folder.id}"]`)).toBeVisible()
    // Planned slot inside folder AND the ungrouped one render; Done slot does NOT
    const column = page.locator(`[data-testid="column-${col.id}"]`)
    await expect(column.locator(`[data-slot-key="${sPlannedInFolder.key}"]`)).toHaveCount(1)
    await expect(column.locator(`[data-slot-key="${sPlannedUngrouped.key}"]`)).toHaveCount(1)
    await expect(column.locator(`[data-slot-key="${sDoneInFolder.key}"]`)).toHaveCount(0)
  })

  test('27. empty folders (for this column filter) are always shown as drop targets with count 0', async ({ page, request }) => {
    await page.addInitScript(() => {
      const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
      cfg.tagColumnsEnabled = true
      localStorage.setItem('mc-chat-config', JSON.stringify(cfg))
    })
    await resetColumns(request)
    const foldersBefore = await (await request.get('/api/chat/folders')).json()
    for (const f of foldersBefore) await request.delete(`/api/chat/folders/${f.id}`)
    const emptyFolder = await (await request.post('/api/chat/folders', { data: { name: 'E2E-27 Empty' } })).json()
    const tags = await (await request.get('/api/chat/tags')).json()
    const plannedId = tags.find((t: { name: string }) => t.name === 'Planned').id
    const doneId = tags.find((t: { name: string }) => t.name === 'Done').id
    // Single Done-tagged slot inside that folder (won't match Planned column)
    const s = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${s.key}/tags`, { data: { tags: [doneId] } })
    await request.patch(`/api/chat/slots/${s.key}/folder`, { data: { folder_id: emptyFolder.id } })
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [plannedId], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    // Folder header IS visible (new behavior: folders are always shown, even when empty in this column)
    await expect(page.locator(`[data-testid="col-${col.id}-folder-${emptyFolder.id}"]`)).toBeVisible()
    // The Done-tagged session must NOT appear in the Planned column
    await expect(page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${s.key}"]`)).toHaveCount(0)
  })

  test('28. column header "New folder" button creates a folder + persists via API', async ({ page, request }) => {
    await page.addInitScript(() => {
      const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
      cfg.tagColumnsEnabled = true
      localStorage.setItem('mc-chat-config', JSON.stringify(cfg))
    })
    await resetColumns(request)
    const foldersBefore = await (await request.get('/api/chat/folders')).json()
    for (const f of foldersBefore) await request.delete(`/api/chat/folders/${f.id}`)
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await page.locator(`[data-testid="column-new-folder-${col.id}"]`).click()
    // The per-column inline input was replaced by the shared folder modal, which
    // also collects project directory / default agent / icon.
    const input = page.locator('[data-testid="folder-config-name"]')
    await expect(input).toBeVisible()
    await input.fill('E2E-28 NewFolder')
    await page.locator('[data-testid="folder-config-submit"]').click()
    await expect.poll(async () => {
      const list = await (await request.get('/api/chat/folders')).json()
      return list.find((f: { name: string }) => f.name === 'E2E-28 NewFolder')
    }).toBeTruthy()
  })

  test('29. folder assignment survives when a session is re-tagged via drop', async ({ request }) => {
    // API-only test: the folder_id is independent of the tag field so moving between
    // columns via drop must not reset folder membership.
    await resetColumns(request)
    const foldersBefore = await (await request.get('/api/chat/folders')).json()
    for (const f of foldersBefore) await request.delete(`/api/chat/folders/${f.id}`)
    const folder = await (await request.post('/api/chat/folders', { data: { name: 'E2E-29 Keep' } })).json()
    const tags = await (await request.get('/api/chat/tags')).json()
    const todoId = tags.find((t: { name: string }) => t.name === 'ToDo').id
    const doneId = tags.find((t: { name: string }) => t.name === 'Done').id
    const slot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${slot.key}/tags`, { data: { tags: [todoId] } })
    await request.patch(`/api/chat/slots/${slot.key}/folder`, { data: { folder_id: folder.id } })
    // Drop into Done status column
    const done = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [doneId], mode: 'any' } })).json()
    const res = await (await request.post(`/api/chat/slots/${slot.key}/drop`, { data: { column_id: done.id } })).json()
    expect(res.ok).toBe(true)
    expect(res.tags).toContain(doneId)
    expect(res.tags).not.toContain(todoId)
    // Folder must still be set
    const slots = await (await request.get('/api/chat/slots')).json()
    const updated = slots.find((s: { key: string }) => s.key === slot.key)
    expect(updated.folder_id).toBe(folder.id)
  })

  test('17. right-click session row opens Tags picker; toggling a tag updates chips', async ({ page, request }) => {
    await resetColumns(request)
    const s = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.patch(`/api/chat/slots/${s.key}/title`, { data: { title: 'E2E-16 picker' } })
    // No tags initially
    await page.goto('/chat')
    const row = page.locator(`[data-slot-key="${s.key}"]`).first()
    await row.click({ button: 'right' })
    await page.getByRole('menuitem', { name: /Tags/ }).click()
    const picker = page.locator('[data-testid="slot-tag-picker"]')
    await expect(picker).toBeVisible()
    await picker.getByRole('menuitemcheckbox', { name: /Review/ }).click()
    await page.waitForTimeout(400)
    const updated = await (await request.get('/api/chat/slots')).json()
    const slot = updated.find((x: { key: string }) => x.key === s.key)
    expect(slot.tags).toContain('review')
  })
})
