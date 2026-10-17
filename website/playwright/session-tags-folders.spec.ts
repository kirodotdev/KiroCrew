import { test, expect, Page, APIRequestContext } from '@playwright/test'

/**
 * Deep tests of folder behavior inside the column strip.
 * Reproduces bugs: "sessions disappearing" and "folder not creating".
 */

async function primeBrowser(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-sidebar-width', '1400')
    const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
    cfg.tagColumnsEnabled = true
    localStorage.setItem('mc-chat-config', JSON.stringify(cfg))
  })
  await page.setViewportSize({ width: 1800, height: 1000 })
}

async function wipeFolders(request: APIRequestContext) {
  const list = await (await request.get('/api/chat/folders')).json()
  for (const f of list) await request.delete(`/api/chat/folders/${f.id}`)
}
async function wipeColumns(request: APIRequestContext) {
  const list = await (await request.get('/api/chat/tag-columns')).json()
  for (const c of list) await request.delete(`/api/chat/tag-columns/${c.id}`)
}
// Destructive wipes are gated on an EXPLICIT ephemeral-harness marker: the
// e2e harness sets KIROCREW_E2E_EPHEMERAL for the throwaway tmp-home gateway
// it spawns. Token presence alone is NOT a safe signal -- it is also the normal
// state when authenticating to a real, token-protected gateway, so a developer
// pointing the suite at their live gateway (to debug a failure) must never
// trigger a slot wipe. Absent the marker we skip the wipes and accept the flake
// risk (e.g. a bare local `playwright test` against the port-5476 fallback).
const HARNESS_GATEWAY = !!process.env.KIROCREW_E2E_EPHEMERAL

async function wipeSlots(request: APIRequestContext) {
  // Slots accumulate across this serial describe (and across retries:2) since
  // beforeEach previously only wiped columns/folders. A growing slot set makes
  // the /chat render heavier and worsens the load-timeout flake. Each test
  // creates its own slot, so a clean wipe before each is safe on the harness
  // gateway (and skipped on a personal one — see HARNESS_GATEWAY).
  if (!HARNESS_GATEWAY) return
  const list = await (await request.get('/api/chat/slots')).json()
  for (const s of list) await request.delete(`/api/chat/slots/${s.key}`)
}

test.describe.configure({ mode: 'serial' })

test.describe('Folders inside columns (deep)', () => {
  test.beforeEach(async ({ page, request }) => {
    await primeBrowser(page)
    await wipeSlots(request)
    await wipeColumns(request)
    await wipeFolders(request)
  })

  test('F1. POST /api/chat/folders creates folder that shows in every column as drop target', async ({ page, request }) => {
    // Seed one session + column
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [], mode: 'any' } })).json()
    // Create folder via API
    const folder = await (await request.post('/api/chat/folders', { data: { name: 'F1-deep' } })).json()
    expect(folder.id).toBeTruthy()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    // Folder header should render in the column, even with 0 matching sessions
    await expect(page.locator(`[data-testid="col-${col.id}-folder-${folder.id}"]`)).toBeVisible({ timeout: 15000 })
  })

  test('F2. Column "New folder" UI button creates folder and it appears in the column', async ({ page, request }) => {
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    // Before: 0 folders
    expect(((await (await request.get('/api/chat/folders')).json()) as unknown[]).length).toBe(0)
    // Click "New folder" on the column header
    await page.locator(`[data-testid="column-new-folder-${col.id}"]`).click()
    // Inline input -> shared folder modal (see FolderConfigModal).
    const input = page.locator('[data-testid="folder-config-name"]')
    await expect(input).toBeVisible()
    await input.fill('F2-ui-created')
    await page.locator('[data-testid="folder-config-submit"]').click()
    // After: folder exists + renders in column
    await expect.poll(async () => {
      const list = await (await request.get('/api/chat/folders')).json()
      return list.find((f: { name: string }) => f.name === 'F2-ui-created')
    }, { timeout: 15000 }).toBeTruthy()
    const folder = ((await (await request.get('/api/chat/folders')).json()) as { id: string; name: string }[]).find(f => f.name === 'F2-ui-created')!
    await expect(page.locator(`[data-testid="col-${col.id}-folder-${folder.id}"]`)).toBeVisible({ timeout: 15000 })
  })

  test('F3. Session with folder_id shows under that folder in the column, not in ungrouped area', async ({ page, request }) => {
    const folder = await (await request.post('/api/chat/folders', { data: { name: 'F3-folder' } })).json()
    const tags = await (await request.get('/api/chat/tags')).json()
    const plannedId = tags.find((t: { name: string }) => t.name === 'Planned').id
    const slot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${slot.key}/tags`, { data: { tags: [plannedId] } })
    await request.patch(`/api/chat/slots/${slot.key}/folder`, { data: { folder_id: folder.id } })
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [plannedId], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    // Session must be inside the folder's drop-target region, not directly under the column
    const folderEl = page.locator(`[data-testid="col-${col.id}-folder-${folder.id}"]`)
    await expect(folderEl).toBeVisible()
    await expect(folderEl.locator(`[data-slot-key="${slot.key}"]`)).toHaveCount(1, { timeout: 15000 })
  })

  test('F4. Session without folder_id lands in ungrouped area of every matching column', async ({ page, request }) => {
    const tags = await (await request.get('/api/chat/tags')).json()
    const doneId = tags.find((t: { name: string }) => t.name === 'Done').id
    const slot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${slot.key}/tags`, { data: { tags: [doneId] } })
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [doneId], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    // Session is rendered inside the column
    await expect(page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${slot.key}"]`)).toHaveCount(1)
  })

  test('F5. Simulated drag of a session onto a folder header inside a column assigns folder_id via API', async ({ page, request }) => {
    const folder = await (await request.post('/api/chat/folders', { data: { name: 'F5-drop-target' } })).json()
    const tags = await (await request.get('/api/chat/tags')).json()
    const plannedId = tags.find((t: { name: string }) => t.name === 'Planned').id
    const slot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${slot.key}/tags`, { data: { tags: [plannedId] } })
    // Slot starts in ungrouped area
    expect(((await (await request.get('/api/chat/slots')).json()) as { key: string; folder_id?: string }[]).find(s => s.key === slot.key)?.folder_id).toBeFalsy()
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [plannedId], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    const row = page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${slot.key}"]`).first()
    const folderEl = page.locator(`[data-testid="col-${col.id}-folder-${folder.id}"]`)
    await expect(row).toBeVisible()
    await expect(folderEl).toBeVisible()
    // Fire HTML5 DnD events with a shared dataTransfer so dataTransfer.getData works
    await page.evaluate(({ slotKey, folderSelector }) => {
      const source = document.querySelector(`[data-slot-key="${slotKey}"]`) as HTMLElement
      const target = document.querySelector(folderSelector) as HTMLElement
      if (!source || !target) throw new Error(`Missing source/target: ${!!source}/${!!target}`)
      const dt = new DataTransfer()
      dt.setData('text/plain', slotKey)
      const rect = target.getBoundingClientRect()
      const cx = rect.left + rect.width / 2
      const cy = rect.top + rect.height / 2
      source.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt, clientX: cx, clientY: cy }))
      target.dispatchEvent(new DragEvent('dragenter', { bubbles: true, dataTransfer: dt, clientX: cx, clientY: cy }))
      target.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt, clientX: cx, clientY: cy }))
      target.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, clientX: cx, clientY: cy }))
      source.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt, clientX: cx, clientY: cy }))
    }, { slotKey: slot.key, folderSelector: `[data-testid="col-${col.id}-folder-${folder.id}"]` })
    // Poll backend for folder assignment
    await expect.poll(async () => {
      const slots = await (await request.get('/api/chat/slots')).json()
      return slots.find((s: { key: string }) => s.key === slot.key)?.folder_id
    }, { timeout: 15000 }).toBe(folder.id)
    // Re-read the page; session should now be nested inside the folder element
    await page.reload()
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await expect(page.locator(`[data-testid="col-${col.id}-folder-${folder.id}"] [data-slot-key="${slot.key}"]`)).toHaveCount(1)
  })

  test('F6. Drop onto column body does NOT clear folder_id (folder assignment only changes via explicit folder-header drop)', async ({ page, request }) => {
    const folder = await (await request.post('/api/chat/folders', { data: { name: 'F6-start' } })).json()
    const tags = await (await request.get('/api/chat/tags')).json()
    const plannedId = tags.find((t: { name: string }) => t.name === 'Planned').id
    const slot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${slot.key}/tags`, { data: { tags: [plannedId] } })
    await request.patch(`/api/chat/slots/${slot.key}/folder`, { data: { folder_id: folder.id } })
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [plannedId], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    // Wait for the slot row (nested in the folder) to be in DOM
    await page.waitForSelector(`[data-slot-key="${slot.key}"]`, { timeout: 15000 })
    // Simulate drop onto the column body's scroll area (not folder)
    await page.evaluate(({ slotKey, columnTestId }) => {
      const source = document.querySelector(`[data-slot-key="${slotKey}"]`) as HTMLElement
      const column = document.querySelector(`[data-testid="${columnTestId}"]`) as HTMLElement
      const target = column.querySelector('div.flex-1.overflow-y-auto') as HTMLElement
      if (!source || !target) throw new Error(`Missing source/target: src=${!!source} tgt=${!!target}`)
      const dt = new DataTransfer()
      dt.setData('text/plain', slotKey)
      const rect = target.getBoundingClientRect()
      // Drop near the bottom, below any folders
      const cx = rect.left + 10
      const cy = rect.bottom - 10
      source.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt, clientX: cx, clientY: cy }))
      target.dispatchEvent(new DragEvent('dragenter', { bubbles: true, dataTransfer: dt, clientX: cx, clientY: cy }))
      target.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt, clientX: cx, clientY: cy }))
      target.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, clientX: cx, clientY: cy }))
      source.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt, clientX: cx, clientY: cy }))
    }, { slotKey: slot.key, columnTestId: `column-${col.id}` })
    // Wait a bit to let any PATCH fire
    await page.waitForTimeout(500)
    // Folder_id should STILL be set — column-body drop does not change folder
    const slots = await (await request.get('/api/chat/slots')).json()
    expect(slots.find((s: { key: string }) => s.key === slot.key)?.folder_id).toBe(folder.id)
  })

  test('F7ui. UI drag: session in Folder in Todo column → drop on Done column → session stays in Folder, now in Done column', async ({ page, request }) => {
    const folder = await (await request.post('/api/chat/folders', { data: { name: 'F7ui-sticky' } })).json()
    const tags = await (await request.get('/api/chat/tags')).json()
    const todoId = tags.find((t: { name: string }) => t.name === 'ToDo').id
    const doneId = tags.find((t: { name: string }) => t.name === 'Done').id
    const slot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${slot.key}/tags`, { data: { tags: [todoId] } })
    await request.patch(`/api/chat/slots/${slot.key}/folder`, { data: { folder_id: folder.id } })
    const todoCol = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [todoId], mode: 'any' } })).json()
    const doneCol = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [doneId], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${todoCol.id}"]`)
    await page.waitForSelector(`[data-slot-key="${slot.key}"]`)
    // Session starts nested inside the ToDo column's folder
    await expect(page.locator(`[data-testid="col-${todoCol.id}-folder-${folder.id}"] [data-slot-key="${slot.key}"]`)).toHaveCount(1)
    // Fire HTML5 DnD: drop the session on the Done column body
    await page.evaluate(({ slotKey, doneColTestId }) => {
      const source = document.querySelector(`[data-slot-key="${slotKey}"]`) as HTMLElement
      const target = document.querySelector(`[data-testid="${doneColTestId}"]`) as HTMLElement
      if (!source || !target) throw new Error(`Missing source/target: src=${!!source} tgt=${!!target}`)
      const dt = new DataTransfer()
      dt.setData('text/plain', slotKey)
      const rect = target.getBoundingClientRect()
      const cx = rect.left + rect.width / 2
      const cy = rect.top + rect.height / 2
      source.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt, clientX: cx, clientY: cy }))
      target.dispatchEvent(new DragEvent('dragenter', { bubbles: true, dataTransfer: dt, clientX: cx, clientY: cy }))
      target.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt, clientX: cx, clientY: cy }))
      target.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, clientX: cx, clientY: cy }))
      source.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt, clientX: cx, clientY: cy }))
    }, { slotKey: slot.key, doneColTestId: `column-${doneCol.id}` })
    // Backend: status should have flipped, folder_id preserved
    await expect.poll(async () => {
      const slots = await (await request.get('/api/chat/slots')).json()
      const s = slots.find((x: { key: string }) => x.key === slot.key)
      return { tags: s?.tags, folder_id: s?.folder_id }
    }, { timeout: 15000 }).toEqual({ tags: [doneId], folder_id: folder.id })
    // UI: session should now nest inside Done column's folder
    await page.reload()
    await page.waitForSelector(`[data-testid="col-${doneCol.id}-folder-${folder.id}"] [data-slot-key="${slot.key}"]`, { timeout: 15000 })
    await expect(page.locator(`[data-testid="col-${todoCol.id}-folder-${folder.id}"] [data-slot-key="${slot.key}"]`)).toHaveCount(0)
  })

  test('F7. Drag session between status columns preserves folder_id', async ({ page, request }) => {
    const folder = await (await request.post('/api/chat/folders', { data: { name: 'F7-persists' } })).json()
    const tags = await (await request.get('/api/chat/tags')).json()
    const todoId = tags.find((t: { name: string }) => t.name === 'ToDo').id
    const doneId = tags.find((t: { name: string }) => t.name === 'Done').id
    const slot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${slot.key}/tags`, { data: { tags: [todoId] } })
    await request.patch(`/api/chat/slots/${slot.key}/folder`, { data: { folder_id: folder.id } })
    const todoCol = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [todoId], mode: 'any' } })).json()
    const doneCol = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [doneId], mode: 'any' } })).json()
    // Use drop API directly (mirrors column-drop handler)
    const res = await (await request.post(`/api/chat/slots/${slot.key}/drop`, { data: { column_id: doneCol.id } })).json()
    expect(res.ok).toBe(true)
    expect(res.tags).toContain(doneId)
    expect(res.tags).not.toContain(todoId)
    const slots = await (await request.get('/api/chat/slots')).json()
    const updated = slots.find((s: { key: string }) => s.key === slot.key)
    expect(updated.folder_id).toBe(folder.id)  // folder must survive
    // And it must render inside the folder in the Done column
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${doneCol.id}"]`)
    await expect(page.locator(`[data-testid="col-${doneCol.id}-folder-${folder.id}"] [data-slot-key="${slot.key}"]`)).toHaveCount(1)
    // TodoCol should render the same folder but with this slot absent
    await expect(page.locator(`[data-testid="col-${todoCol.id}-folder-${folder.id}"]`)).toBeVisible()
    await expect(page.locator(`[data-testid="col-${todoCol.id}-folder-${folder.id}"] [data-slot-key="${slot.key}"]`)).toHaveCount(0)
  })

  test('F8. Delete folder via UI × button removes it and ungroups its sessions', async ({ page, request }) => {
    const folder = await (await request.post('/api/chat/folders', { data: { name: 'F8-deleteme' } })).json()
    const tags = await (await request.get('/api/chat/tags')).json()
    const plannedId = tags.find((t: { name: string }) => t.name === 'Planned').id
    const slot = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${slot.key}/tags`, { data: { tags: [plannedId] } })
    await request.patch(`/api/chat/slots/${slot.key}/folder`, { data: { folder_id: folder.id } })
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [plannedId], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="col-${col.id}-folder-${folder.id}"]`)
    // Delete folder via its More-menu → "Delete folder" item (menu opens on hover).
    await page.locator(`[data-testid="col-${col.id}-folder-${folder.id}"]`).hover()
    await page.locator(`[data-testid="col-${col.id}-folder-${folder.id}-menu"]`).click({ force: true })
    page.once('dialog', d => d.accept())
    // Column-context delete item has no testid (unlike the sidebar folder menu),
    // so target it by role + label — robust across both menu contexts.
    await page.getByRole('menuitem', { name: /delete folder/i }).click()
    // Folder gone
    await expect.poll(async () => {
      const list = await (await request.get('/api/chat/folders')).json()
      return list.find((f: { id: string }) => f.id === folder.id)
    }, { timeout: 15000 }).toBeFalsy()
    // Session ungrouped (folder_id cleared server-side)
    await expect.poll(async () => {
      const slots = await (await request.get('/api/chat/slots')).json()
      return slots.find((s: { key: string }) => s.key === slot.key)?.folder_id
    }, { timeout: 15000 }).toBeFalsy()
  })

  test('F9. Session-disappearing repro: column with no matching sessions renders empty, sessions still exist on disk', async ({ page, request }) => {
    // Create 3 sessions: 1 Planned, 1 Done, 1 untagged
    const tags = await (await request.get('/api/chat/tags')).json()
    const plannedId = tags.find((t: { name: string }) => t.name === 'Planned').id
    const doneId = tags.find((t: { name: string }) => t.name === 'Done').id
    const implId = tags.find((t: { name: string }) => t.name === 'Implementation').id
    const planned = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${planned.key}/tags`, { data: { tags: [plannedId] } })
    const done = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${done.key}/tags`, { data: { tags: [doneId] } })
    const untagged = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    // 2 columns: Implementation (will be empty) and Done (will contain `done`)
    const implCol = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [implId], mode: 'any' } })).json()
    const doneCol = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [doneId], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${implCol.id}"]`)
    // Implementation column renders empty state
    const implColLocator = page.locator(`[data-testid="column-${implCol.id}"]`)
    await expect(implColLocator.getByText('No sessions')).toBeVisible()
    // Done column contains the done slot
    await expect(page.locator(`[data-testid="column-${doneCol.id}"] [data-slot-key="${done.key}"]`)).toHaveCount(1)
    // Untagged slot does NOT appear in either column (neither has include_untagged)
    await expect(implColLocator.locator(`[data-slot-key="${untagged.key}"]`)).toHaveCount(0)
    await expect(page.locator(`[data-testid="column-${doneCol.id}"] [data-slot-key="${untagged.key}"]`)).toHaveCount(0)
    // But backend still has all 3 sessions
    const all = await (await request.get('/api/chat/slots')).json()
    const ourKeys = [planned.key, done.key, untagged.key]
    for (const k of ourKeys) {
      expect(all.find((s: { key: string }) => s.key === k)).toBeTruthy()
    }
  })

  test('F12. Column filter popover closes on outside click', async ({ page, request }) => {
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    await page.locator(`[data-testid="column-edit-${col.id}"]`).click()
    await expect(page.locator(`[data-column-popover="${col.id}"]`)).toBeVisible()
    // Click somewhere outside (the column header text)
    await page.mouse.click(20, 20)
    await expect(page.locator(`[data-column-popover="${col.id}"]`)).toHaveCount(0)
  })

  test('F11. Create subfolder from column view: hover folder → + icon → subfolder appears with parent_id set', async ({ page, request }) => {
    const parent = await (await request.post('/api/chat/folders', { data: { name: 'F11-parent' } })).json()
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [], mode: 'any' } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="col-${col.id}-folder-${parent.id}"]`)
    // "New subfolder" lives in the folder More-menu and now opens the shared
    // folder modal (the per-column inline input is gone).
    await page.locator(`[data-testid="col-${col.id}-folder-${parent.id}"]`).hover()
    await page.locator(`[data-testid="col-${col.id}-folder-${parent.id}-menu"]`).click({ force: true })
    await page.getByRole('menuitem', { name: /new subfolder/i }).click()
    const input = page.locator('[data-testid="folder-config-name"]')
    await expect(input).toBeVisible()
    // The destination is fixed by the entry point and restated read-only, so the
    // modal shows the parent rather than asking for it.
    await expect(page.locator('[data-testid="folder-config-destination"]')).toContainText('F11-parent')
    await input.fill('F11-child')
    await page.locator('[data-testid="folder-config-submit"]').click()
    // Subfolder persisted with parent_id
    await expect.poll(async () => {
      const list = await (await request.get('/api/chat/folders')).json()
      return list.find((f: { name: string }) => f.name === 'F11-child')?.parent_id
    }, { timeout: 15000 }).toBe(parent.id)
    // Subfolder also renders inside the parent in the column
    const child = ((await (await request.get('/api/chat/folders')).json()) as { id: string; name: string }[]).find(f => f.name === 'F11-child')!
    await expect(page.locator(`[data-testid="col-${col.id}-folder-${parent.id}"] [data-testid="col-${col.id}-folder-${child.id}"]`)).toBeVisible()
  })

  test('F10. Adding include_untagged to the Planned column surfaces the untagged session there', async ({ page, request }) => {
    const tags = await (await request.get('/api/chat/tags')).json()
    const plannedId = tags.find((t: { name: string }) => t.name === 'Planned').id
    const planned = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    await request.put(`/api/chat/slots/${planned.key}/tags`, { data: { tags: [plannedId] } })
    const untagged = await (await request.post('/api/chat/slots', { data: { agent: 'default' } })).json()
    const col = await (await request.post('/api/chat/tag-columns', { data: { tag_ids: [plannedId], mode: 'any', include_untagged: true } })).json()
    await page.goto('/chat')
    await page.waitForSelector(`[data-testid="column-${col.id}"]`)
    // Both appear
    await expect(page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${planned.key}"]`)).toHaveCount(1)
    await expect(page.locator(`[data-testid="column-${col.id}"] [data-slot-key="${untagged.key}"]`)).toHaveCount(1)
  })

  test('F12. Folder action strip hides after dismissing the ⋯ menu with an outside click', async ({ page, request }) => {
    // Regression: Radix restores focus to the ⋯ trigger on close; the trigger
    // sits inside the focus-within-revealed hover group, so an outside-click
    // dismissal left the strip pinned visible with the pointer elsewhere.
    // Pointer dismissals now suppress the focus restore (keyboard Esc keeps it).
    const folder = await (await request.post('/api/chat/folders', { data: { name: 'F12-dismiss' } })).json()
    await page.goto('/chat')
    const strip = page.locator(`[data-testid="folder-menu-${folder.id}"]`).locator('..')
    await page.locator(`[data-testid="folder-collapse-${folder.id}"]`).hover()
    await page.locator(`[data-testid="folder-menu-${folder.id}"]`).click()
    await page.getByRole('menuitem', { name: /rename/i }).waitFor()
    // Dismiss by clicking far outside, then move the pointer away from the row.
    await page.mouse.click(600, 400)
    await page.mouse.move(600, 500)
    await expect(page.getByRole('menuitem', { name: /rename/i })).toHaveCount(0)
    await expect.poll(async () => strip.evaluate(el => getComputedStyle(el).opacity), { timeout: 5000 }).toBe('0')
  })
})
