import { test, expect } from '@playwright/test'

test.describe.configure({ mode: 'serial' })

test.describe('session tags (column sidebar)', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem('mc-onboarded', '1')
    })
  })

  test('seeds defaults with status=true on first GET /api/chat/tags', async ({ request }) => {
    const res = await request.get('/api/chat/tags')
    expect(res.ok()).toBeTruthy()
    const tags = await res.json()
    const byName = Object.fromEntries(tags.map((t: { name: string; status?: boolean }) => [t.name, t]))
    for (const n of ['Planned', 'ToDo', 'Implementation', 'Review', 'Done']) {
      expect(byName[n]).toBeTruthy()
      expect(byName[n].status).toBe(true)
    }
  })

  test('column CRUD + reorder via API', async ({ request }) => {
    // Create two columns; filter by only the ones we created so parallel tests don't collide
    const tags = await (await request.get('/api/chat/tags')).json()
    const todoId = tags.find((t: { name: string }) => t.name === 'ToDo').id
    const doneId = tags.find((t: { name: string }) => t.name === 'Done').id
    const col1 = await (await request.post('/api/chat/tag-columns', { data: { name: 'col-1-crud-test', tag_ids: [todoId], mode: 'any' } })).json()
    const col2 = await (await request.post('/api/chat/tag-columns', { data: { name: 'col-2-crud-test', tag_ids: [doneId], mode: 'any' } })).json()

    const filterOwn = (list: { id: string }[]): string[] => list.filter(c => c.id === col1.id || c.id === col2.id).map(c => c.id)
    const listed = await (await request.get('/api/chat/tag-columns')).json()
    expect(filterOwn(listed)).toEqual([col1.id, col2.id])

    // Reorder
    await request.put('/api/chat/tag-columns/order', { data: { ids: [col2.id, col1.id] } })
    const reordered = await (await request.get('/api/chat/tag-columns')).json()
    expect(filterOwn(reordered)).toEqual([col2.id, col1.id])

    // Patch mode
    const patched = await (await request.patch(`/api/chat/tag-columns/${col1.id}`, { data: { mode: 'none' } })).json()
    expect(patched.mode).toBe('none')

    // Cleanup
    await request.delete(`/api/chat/tag-columns/${col1.id}`)
    await request.delete(`/api/chat/tag-columns/${col2.id}`)
  })

  test('drag-drop (API): dropping on a single-status column swaps status tags only', async ({ request }) => {
    // Seed one session with ToDo tag
    const slot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    const tags = await (await request.get('/api/chat/tags')).json()
    const todoId = tags.find((t: { name: string }) => t.name === 'ToDo').id
    const doneId = tags.find((t: { name: string }) => t.name === 'Done').id
    // Create a non-status user tag and attach too
    const userTag = await (await request.post('/api/chat/tags', { data: { name: 'spike', color: '#22c55e', status: false } })).json()
    await request.put(`/api/chat/slots/${slot.key}/tags`, { data: { tags: [todoId, userTag.id] } })
    // Create a Done column
    const done = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [doneId], mode: 'any' } })).json()
    const res = await (await request.post(`/api/chat/slots/${slot.key}/drop`, { data: { column_id: done.id } })).json()
    expect(res.ok).toBe(true)
    // Status swapped: ToDo removed, Done added; non-status user tag preserved
    expect(res.tags).toEqual(expect.arrayContaining([doneId, userTag.id]))
    expect(res.tags).not.toContain(todoId)
    // Cleanup
    await request.delete(`/api/chat/tags/${userTag.id}`)
    await request.delete(`/api/chat/tag-columns/${done.id}`)
  })

  test('drop on filter-only column is a no-op', async ({ request }) => {
    const slot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    const tags = await (await request.get('/api/chat/tags')).json()
    const todoId = tags.find((t: { name: string }) => t.name === 'ToDo').id
    await request.put(`/api/chat/slots/${slot.key}/tags`, { data: { tags: [todoId] } })
    // Column with zero tags = unfiltered = not a status lane
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [], mode: 'any' } })).json()
    const res = await (await request.post(`/api/chat/slots/${slot.key}/drop`, { data: { column_id: col.id } })).json()
    expect(res.ok).toBe(false)
    expect(res.tags).toContain(todoId)  // unchanged
    await request.delete(`/api/chat/tag-columns/${col.id}`)
  })

  test('UI: board view toggle is available in the sidebar header menu', async ({ page }) => {
    await page.goto('/chat')
    // The board/list view toggle moved into the sidebar header "More options" menu.
    const headerMenu = page.locator('button[aria-haspopup="menu"][aria-label="More options"]').first()
    await headerMenu.waitFor({ timeout: 15_000 })
    await headerMenu.click()
    await expect(page.getByRole('menuitem', { name: /switch to (board|list) view/i })).toBeVisible({ timeout: 5000 })
  })
})
