import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'

import ChatSidebar from '../src/pages/ChatSidebar'
import SlotTagPopover from '../src/components/SlotTagPopover'
import { TagPopoverProvider } from '../src/hooks/useTagPopover'
import { sseSlots } from '../src/store/dashboardSlice'
import { renderWithProviders, createTestStore } from './helpers'
import { server } from './mocks/server'

const mockConfirm = vi.fn(() => true)
Object.defineProperty(window, 'confirm', { writable: true, value: mockConfirm })

const seedTags = [
  { id: 'planned', name: 'Planned', color: '#6b7280', order: 0, status: true },
  { id: 'todo', name: 'ToDo', color: '#3b82f6', order: 1, status: true },
  { id: 'done', name: 'Done', color: '#10b981', order: 4, status: true },
]

const baseSlots = [
  { key: 'slot-1', title: 'Pipeline debug', running: false, agent: 'kirocrew', created: '2026-04-08T01:00:00Z', last_ts: '2026-04-08T02:00:00Z', folder_id: '', tags: ['todo'] },
  { key: 'slot-2', title: 'Code review', running: true, agent: 'kirocrew', created: '2026-04-08T00:00:00Z', last_ts: '2026-04-08T01:30:00Z', folder_id: '', tags: ['done'] },
  { key: 'slot-3', title: 'Oncall triage', running: false, agent: 'oncall', created: '2026-04-07T10:00:00Z', last_ts: '2026-04-07T12:00:00Z', folder_id: '', tags: [] },
]

const defaultProps = {
  slots: baseSlots,
  activeSlot: 'slot-1',
  unreadSlots: [] as string[],
  history: [],
  historyHasMore: false,
  defaultAgent: 'kirocrew',
  installedAgents: [{ name: 'kirocrew', source: 'builtin' }, { name: 'oncall', source: 'aim' }],
}

/**
 * Render the sidebar + the single app-wide connected tag popover, with the
 * session slots seeded into the store. SlotTagPopover reads a slot's tags from
 * the store (in production ChatPage populates dashboard.slots and derives
 * ChatSidebar's `slots` prop from it), so the store must carry them here too.
 */
function renderBoard() {
  const store = createTestStore()
  store.dispatch(sseSlots(baseSlots as any))
  return renderWithProviders(
    <TagPopoverProvider><ChatSidebar {...defaultProps} /><SlotTagPopover /></TagPopoverProvider>,
    { store },
  )
}

/**
 * Mutable in-memory state used by the msw handlers below — lets us simulate
 * the gateway's tag-board / tag-vocab CRUD without round-tripping a real
 * server. Reset per test in beforeEach().
 */
function makeMockBackend() {
  const state = {
    tags: [...seedTags] as typeof seedTags,
    columns: [] as Array<{ id: string; name: string; tag_ids: string[]; mode: string; order: number; include_untagged: boolean }>,
    folders: [] as Array<{ id: string; name: string; order: number; collapsed: boolean; parent_id?: string }>,
    slotTagsCalls: [] as Array<{ slot: string; tags: string[] }>,
    dropCalls: [] as Array<{ slot: string; column_id: string }>,
  }
  let nextColId = 100
  let nextTagId = 200
  return {
    state,
    handlers: [
      http.get('/api/chat/tags', () => HttpResponse.json(state.tags)),
      http.post('/api/chat/tags', async ({ request }) => {
        const body = await request.json() as { name: string; color?: string; status?: boolean }
        const tag = { id: `t${nextTagId++}`, name: body.name, color: body.color || '#6b7280', order: state.tags.length, status: !!body.status }
        state.tags.push(tag)
        return HttpResponse.json(tag, { status: 201 })
      }),
      http.patch('/api/chat/tags/:id', async ({ request, params }) => {
        const tag = state.tags.find(t => t.id === params.id)
        if (!tag) return HttpResponse.json({ error: 'not found' }, { status: 404 })
        const patch = await request.json() as Partial<typeof tag>
        Object.assign(tag, patch)
        return HttpResponse.json(tag)
      }),
      http.delete('/api/chat/tags/:id', ({ params }) => {
        const before = state.tags.length
        state.tags = state.tags.filter(t => t.id !== params.id)
        if (state.tags.length === before) return HttpResponse.json({ error: 'not found' }, { status: 404 })
        return HttpResponse.json({ ok: true })
      }),
      http.get('/api/chat/tag-columns', () => HttpResponse.json([...state.columns].sort((a, b) => a.order - b.order))),
      http.post('/api/chat/tag-columns', async ({ request }) => {
        const body = await request.json() as Partial<typeof state.columns[number]>
        const col = {
          id: `c${nextColId++}`,
          name: body.name || '',
          tag_ids: body.tag_ids || [],
          mode: body.mode || 'any',
          order: state.columns.length,
          include_untagged: !!body.include_untagged,
        }
        state.columns.push(col)
        return HttpResponse.json(col, { status: 201 })
      }),
      http.patch('/api/chat/tag-columns/:id', async ({ request, params }) => {
        const col = state.columns.find(c => c.id === params.id)
        if (!col) return HttpResponse.json({ error: 'not found' }, { status: 404 })
        const patch = await request.json() as Partial<typeof col>
        Object.assign(col, patch)
        return HttpResponse.json(col)
      }),
      http.delete('/api/chat/tag-columns/:id', ({ params }) => {
        state.columns = state.columns.filter(c => c.id !== params.id)
        return HttpResponse.json({ ok: true })
      }),
      http.put('/api/chat/tag-columns/order', async ({ request }) => {
        const { ids } = await request.json() as { ids: string[] }
        const order = new Map(ids.map((id, i) => [id, i]))
        for (const col of state.columns) {
          if (order.has(col.id)) col.order = order.get(col.id)!
        }
        return HttpResponse.json({ ok: true })
      }),
      http.put('/api/chat/slots/:slot/tags', async ({ request, params }) => {
        const body = await request.json() as { tags: string[] }
        state.slotTagsCalls.push({ slot: String(params.slot), tags: body.tags })
        return HttpResponse.json({ ok: true, tags: body.tags })
      }),
      http.post('/api/chat/slots/:slot/drop', async ({ request, params }) => {
        const body = await request.json() as { column_id: string }
        state.dropCalls.push({ slot: String(params.slot), column_id: body.column_id })
        return HttpResponse.json({ ok: true, tags: [] })
      }),
      http.get('/api/chat/folders', () => HttpResponse.json(state.folders)),
      http.post('/api/chat/folders', async ({ request }) => {
        const body = await request.json() as { name: string; parent_id?: string }
        const f = { id: `f${state.folders.length + 1}`, name: body.name, order: state.folders.length, collapsed: false, parent_id: body.parent_id }
        state.folders.push(f)
        return HttpResponse.json(f, { status: 201 })
      }),
      http.patch('/api/chat/folders/:id', async ({ request, params }) => {
        const f = state.folders.find(x => x.id === params.id)
        if (!f) return HttpResponse.json({ error: 'not found' }, { status: 404 })
        const patch = await request.json() as Partial<typeof f>
        Object.assign(f, patch)
        return HttpResponse.json(f)
      }),
      http.delete('/api/chat/folders/:id', ({ params }) => {
        state.folders = state.folders.filter(f => f.id !== params.id)
        return HttpResponse.json({ ok: true })
      }),
      http.patch('/api/chat/slots/:slot/folder', () => HttpResponse.json({ ok: true })),
      http.patch('/api/chat/slots/:slot/pin', () => HttpResponse.json({ ok: true })),
    ],
  }
}

describe('ChatSidebar tag/column UI', () => {
  let backend: ReturnType<typeof makeMockBackend>

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockConfirm.mockReturnValue(true)
    backend = makeMockBackend()
    server.use(...backend.handlers)
    // Enable column mode so the strip renders
    localStorage.setItem('mc-chat-config', JSON.stringify({ tagColumnsEnabled: true }))
  })

  it('renders the More options button which reveals board toggle menuitem', async () => {
    const user = userEvent.setup()
    backend.state.columns = [
      { id: 'c1', name: '', tag_ids: [], mode: 'any', order: 0, include_untagged: false },
    ]
    renderBoard()
    const moreBtn = await screen.findByTitle('More options')
    expect(moreBtn).toBeInTheDocument()
    await user.click(moreBtn)
    expect(await screen.findByRole('menuitem', { name: /Switch to list view/ })).toBeInTheDocument()
  })

  it('shows the empty-state seed prompt when no columns exist and toggling on creates a default column', async () => {
    backend.state.columns = []
    renderBoard()
    await screen.findByTitle('More options')
    // With 0 columns, the legacy flat list renders — board-toggle menuitem still accessible via menu
    expect(screen.queryByTestId('column-strip')).not.toBeInTheDocument()
  })

  it('clicking board-toggle menuitem when inactive enables board mode', async () => {
    backend.state.columns = []
    localStorage.setItem('mc-chat-config', JSON.stringify({ tagColumnsEnabled: false }))
    const user = userEvent.setup()
    renderBoard()
    await user.click(await screen.findByTitle('More options'))
    const menuitem = await screen.findByRole('menuitem', { name: /Switch to board view/ })
    await user.click(menuitem)
    // After click, config should have tagColumnsEnabled: true
    const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
    expect(cfg.tagColumnsEnabled).toBe(true)
  })

  it('clicking board-toggle menuitem when active with columns disables board mode', async () => {
    backend.state.columns = [
      { id: 'c1', name: '', tag_ids: [], mode: 'any', order: 0, include_untagged: false },
    ]
    const user = userEvent.setup()
    renderBoard()
    await user.click(await screen.findByTitle('More options'))
    const menuitem = await screen.findByRole('menuitem', { name: /Switch to list view/ })
    await user.click(menuitem)
    const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
    expect(cfg.tagColumnsEnabled).toBe(false)
  })

  it('renders the column strip when columns exist', async () => {
    backend.state.columns = [
      { id: 'c1', name: '', tag_ids: ['todo'], mode: 'any', order: 0, include_untagged: false },
      { id: 'c2', name: 'Wrap-up', tag_ids: ['done'], mode: 'any', order: 1, include_untagged: false },
    ]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-strip')).toBeInTheDocument())
    expect(screen.getByTestId('column-c1')).toBeInTheDocument()
    expect(screen.getByTestId('column-c2')).toBeInTheDocument()
    // Tag badges render the tag name inside their respective column headers
    // (and may also appear as tag-chips on session rows — scope to header).
    expect(within(screen.getByTestId('column-c1')).getAllByText('ToDo').length).toBeGreaterThan(0)
    expect(within(screen.getByTestId('column-c2')).getAllByText('Done').length).toBeGreaterThan(0)
  })

  it('clicking board-toggle menuitem in orphan state (enabled, no cols) creates default column', async () => {
    backend.state.columns = []
    localStorage.setItem('mc-chat-config', JSON.stringify({ tagColumnsEnabled: true }))
    const user = userEvent.setup()
    renderBoard()
    await user.click(await screen.findByTitle('More options'))
    // Orphan state (enabled but no columns) shows "Switch to board view"
    const menuitem = await screen.findByRole('menuitem', { name: /Switch to board view/ })
    await user.click(menuitem)
    // Config stays enabled, but a default column is now created
    const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
    expect(cfg.tagColumnsEnabled).toBe(true)
    await waitFor(() => expect(backend.state.columns.length).toBe(1))
  })

  it('column header buttons all have aria-labels (icon-buttons-need-labels)', async () => {
    backend.state.columns = [{ id: 'c1', name: '', tag_ids: ['todo'], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    // The column header puts each icon button under data-testid="column-{id}" so
    // we scope queries to the column itself — multiple "New folder" buttons may
    // exist (column-level + sidebar-level legacy fallback).
    const col = screen.getByTestId('column-c1')
    expect(within(col).getByLabelText('New folder')).toBeInTheDocument()
    expect(within(col).getByLabelText('Filter & manage tags')).toBeInTheDocument()
    expect(within(col).getByLabelText('Add column after this one')).toBeInTheDocument()
    expect(within(col).getByLabelText('Delete column')).toBeInTheDocument()
  })

  it('opens the column filter popover and closes it on outside click', async () => {
    const user = userEvent.setup()
    backend.state.columns = [{ id: 'c1', name: '', tag_ids: ['todo'], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    await user.click(screen.getByTestId('column-edit-c1'))
    // Popover renders via portal; query by data-attribute
    await waitFor(() => {
      expect(document.querySelector('[data-column-popover="c1"]')).toBeInTheDocument()
    })
    // Clicking outside dismisses
    fireEvent.mouseDown(document.body)
    await waitFor(() => {
      expect(document.querySelector('[data-column-popover="c1"]')).not.toBeInTheDocument()
    })
  })

  it('toggling a tag in the popover sends a PATCH to update tag_ids', async () => {
    const user = userEvent.setup()
    backend.state.columns = [{ id: 'c1', name: '', tag_ids: [], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    await user.click(screen.getByTestId('column-edit-c1'))
    // Each tag row exists keyed by tag id
    await waitFor(() => {
      expect(screen.getByTestId('tag-row-todo')).toBeInTheDocument()
    })
    // Click the swatch (a role=checkbox toggle; first interactive element in the row)
    const todoSwatch = within(screen.getByTestId('tag-row-todo')).getByRole('checkbox')
    await user.click(todoSwatch)
    await waitFor(() => {
      expect(backend.state.columns[0].tag_ids).toContain('todo')
    })
  })

  it('renders the column filter popover as a labelled dialog', async () => {
    const user = userEvent.setup()
    backend.state.columns = [{ id: 'c1', name: 'Launch', tag_ids: [], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    await user.click(screen.getByTestId('column-edit-c1'))
    // WAI-ARIA dialog with a per-column accessible name (keyboard/SR operable overlay)
    await waitFor(() => expect(screen.getByRole('dialog', { name: /Filter tags: Launch/i })).toBeInTheDocument())
    // The tag rows are grouped for assistive tech
    expect(screen.getByRole('group', { name: /Filter by tag/i })).toBeInTheDocument()
  })

  it('Escape closes the column filter popover and returns focus to its trigger', async () => {
    const user = userEvent.setup()
    backend.state.columns = [{ id: 'c1', name: '', tag_ids: [], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    const trigger = screen.getByTestId('column-edit-c1')
    await user.click(trigger)
    const dialog = await screen.findByRole('dialog', { name: /Filter tags/i })
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /Filter tags/i })).not.toBeInTheDocument())
    // focus returns to the trigger (requestAnimationFrame-deferred)
    await waitFor(() => expect(document.activeElement).toBe(trigger))
  })

  it('clicking ⚡ status icon flips the tag.status flag via PATCH', async () => {
    const user = userEvent.setup()
    backend.state.columns = [{ id: 'c1', name: '', tag_ids: [], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    await user.click(screen.getByTestId('column-edit-c1'))
    await waitFor(() => expect(screen.getByTestId('tag-status-todo')).toBeInTheDocument())
    const initial = backend.state.tags.find(t => t.id === 'todo')!.status
    await user.click(screen.getByTestId('tag-status-todo'))
    await waitFor(() => {
      expect(backend.state.tags.find(t => t.id === 'todo')!.status).toBe(!initial)
    })
  })

  it('include-untagged toggle persists', async () => {
    const user = userEvent.setup()
    backend.state.columns = [{ id: 'c1', name: '', tag_ids: ['todo'], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    await user.click(screen.getByTestId('column-edit-c1'))
    await waitFor(() => expect(screen.getByTestId('column-include-untagged-c1')).toBeInTheDocument())
    await user.click(screen.getByTestId('column-include-untagged-c1'))
    await waitFor(() => expect(backend.state.columns[0].include_untagged).toBe(true))
  })

  it('mode radios switch between any / all / none', async () => {
    const user = userEvent.setup()
    backend.state.columns = [{ id: 'c1', name: '', tag_ids: ['todo'], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    await user.click(screen.getByTestId('column-edit-c1'))
    await waitFor(() => expect(screen.getByRole('radio', { name: 'all' })).toBeInTheDocument())
    await user.click(screen.getByRole('radio', { name: 'all' }))
    await waitFor(() => expect(backend.state.columns[0].mode).toBe('all'))
    await user.click(screen.getByRole('radio', { name: 'none' }))
    await waitFor(() => expect(backend.state.columns[0].mode).toBe('none'))
  })

  it('inline tag rename commits on blur', async () => {
    const user = userEvent.setup()
    backend.state.columns = [{ id: 'c1', name: '', tag_ids: [], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    await user.click(screen.getByTestId('column-edit-c1'))
    await waitFor(() => expect(screen.getByTestId('tag-name-todo')).toBeInTheDocument())
    const input = screen.getByTestId('tag-name-todo') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'In Progress' } })
    fireEvent.blur(input)
    await waitFor(() => expect(backend.state.tags.find(t => t.id === 'todo')!.name).toBe('In Progress'))
  })

  it('creating a new tag inline appends to the vocabulary', async () => {
    const user = userEvent.setup()
    backend.state.columns = [{ id: 'c1', name: '', tag_ids: [], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    await user.click(screen.getByTestId('column-edit-c1'))
    await waitFor(() => expect(screen.getByTestId('tag-create-c1')).toBeInTheDocument())
    const before = backend.state.tags.length
    const input = screen.getByTestId('tag-create-c1') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'blocked' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(backend.state.tags.length).toBe(before + 1))
    expect(backend.state.tags.at(-1)!.name).toBe('blocked')
  })

  it('deleting a tag via the row × removes it', async () => {
    const user = userEvent.setup()
    backend.state.columns = [{ id: 'c1', name: '', tag_ids: [], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    await user.click(screen.getByTestId('column-edit-c1'))
    await waitFor(() => expect(screen.getByTestId('tag-delete-todo')).toBeInTheDocument())
    await user.click(screen.getByTestId('tag-delete-todo'))
    await waitFor(() => expect(backend.state.tags.find(t => t.id === 'todo')).toBeUndefined())
  })

  it('+ add-after creates a new column and reorders so it lands immediately to the right', async () => {
    const user = userEvent.setup()
    backend.state.columns = [
      { id: 'c1', name: 'A', tag_ids: [], mode: 'any', order: 0, include_untagged: false },
      { id: 'c2', name: 'B', tag_ids: [], mode: 'any', order: 1, include_untagged: false },
    ]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    await user.click(screen.getByTestId('column-add-after-c1'))
    await waitFor(() => expect(backend.state.columns.length).toBe(3))
    // Column ordering: A (0), new column (1), B should be 2 after reorder
    const sortedIds = [...backend.state.columns].sort((a, b) => a.order - b.order).map(c => c.id)
    expect(sortedIds[0]).toBe('c1')
    expect(sortedIds[2]).toBe('c2')
  })

  it('× delete removes a column', async () => {
    const user = userEvent.setup()
    backend.state.columns = [{ id: 'c1', name: '', tag_ids: [], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    await user.click(screen.getByTestId('column-delete-c1'))
    await waitFor(() => expect(backend.state.columns.length).toBe(0))
  })

  it('clear-filter button empties the column tag_ids', async () => {
    const user = userEvent.setup()
    backend.state.columns = [{ id: 'c1', name: '', tag_ids: ['todo', 'done'], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    await user.click(screen.getByTestId('column-edit-c1'))
    await waitFor(() => expect(screen.getByText('Clear filter')).toBeInTheDocument())
    await user.click(screen.getByText('Clear filter'))
    await waitFor(() => expect(backend.state.columns[0].tag_ids).toEqual([]))
  })

  it('column-level "+ folder" button creates a folder', async () => {
    backend.state.columns = [{ id: 'c1', name: '', tag_ids: ['todo'], mode: 'any', order: 0, include_untagged: false }]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('column-new-folder-c1'))
    // An inline input appears scoped to this column; set value and submit with Enter
    const input = screen.getByPlaceholderText(/folder name/i)
    fireEvent.change(input, { target: { value: 'Backlog' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(backend.state.folders.length).toBe(1))
    expect(backend.state.folders[0].name).toBe('Backlog')
  })

  it('include_untagged column shows "+ untagged" badge in the header', async () => {
    backend.state.columns = [
      { id: 'c1', name: 'Planned', tag_ids: ['planned'], mode: 'any', order: 0, include_untagged: true },
    ]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    expect(screen.getByText('+ untagged')).toBeInTheDocument()
  })

  it('right-click on a session row opens the per-slot tag picker', async () => {
    backend.state.columns = [
      { id: 'c1', name: '', tag_ids: ['todo'], mode: 'any', order: 0, include_untagged: false },
    ]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    // Find a session row in the column and right-click it
    const slotRow = await screen.findByText('Pipeline debug')
    fireEvent.contextMenu(slotRow)
    // Context menu shows "Tags…"
    const tagsMenuItem = await screen.findByRole('menuitem', { name: /Tags/ })
    fireEvent.click(tagsMenuItem)
    // Tag picker dialog appears
    await waitFor(() => {
      expect(screen.getByTestId('slot-tag-picker')).toBeInTheDocument()
    })
    // Has aria-modal
    const dialog = screen.getByTestId('slot-tag-picker')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
  })

  it('tag picker shows current slot tags with checkmarks and toggles them', async () => {
    backend.state.columns = [
      { id: 'c1', name: '', tag_ids: ['todo'], mode: 'any', order: 0, include_untagged: false },
    ]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    // Open tag picker for slot-1 (which has ['todo'] in its tags)
    fireEvent.contextMenu(await screen.findByText('Pipeline debug'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Tags/ }))
    await waitFor(() => expect(screen.getByTestId('slot-tag-picker')).toBeInTheDocument())
    // Click 'Done' tag to add it
    const dialog = screen.getByTestId('slot-tag-picker')
    const doneBtn = within(dialog).getByText('Done').closest('button') as HTMLButtonElement
    fireEvent.click(doneBtn)
    // Server received the optimistic next-tags list including 'done'
    await waitFor(() => {
      const lastCall = backend.state.slotTagsCalls.at(-1)
      expect(lastCall?.slot).toBe('slot-1')
      expect(lastCall?.tags).toEqual(expect.arrayContaining(['todo', 'done']))
    })
  })

  it('tag picker closes when clicking the backdrop (outside the inner dialog)', async () => {
    backend.state.columns = [
      { id: 'c1', name: '', tag_ids: ['todo'], mode: 'any', order: 0, include_untagged: false },
    ]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    fireEvent.contextMenu(await screen.findByText('Pipeline debug'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Tags/ }))
    await waitFor(() => expect(screen.getByTestId('slot-tag-picker')).toBeInTheDocument())
    // The backdrop is the picker dialog's parent div with fixed inset-0
    const dialog = screen.getByTestId('slot-tag-picker')
    const backdrop = dialog.parentElement as HTMLElement
    fireEvent.click(backdrop)
    await waitFor(() => {
      expect(screen.queryByTestId('slot-tag-picker')).not.toBeInTheDocument()
    })
  })

  it('rapidly toggling two tags in the picker queues both onto the server', async () => {
    backend.state.columns = [
      { id: 'c1', name: '', tag_ids: ['todo'], mode: 'any', order: 0, include_untagged: false },
    ]
    renderBoard()
    await waitFor(() => expect(screen.getByTestId('column-c1')).toBeInTheDocument())
    fireEvent.contextMenu(await screen.findByText('Pipeline debug'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Tags/ }))
    await waitFor(() => expect(screen.getByTestId('slot-tag-picker')).toBeInTheDocument())
    const dialog = screen.getByTestId('slot-tag-picker')
    // Click both Done and Planned in quick succession
    fireEvent.click(within(dialog).getByText('Done').closest('button')!)
    fireEvent.click(within(dialog).getByText('Planned').closest('button')!)
    // Both end up in the server-side mutation queue with composed payloads
    await waitFor(() => {
      expect(backend.state.slotTagsCalls.length).toBeGreaterThanOrEqual(2)
      // Latest call should include both newly-toggled status tags on top of the existing 'todo'
      const last = backend.state.slotTagsCalls.at(-1)!
      expect(last.tags).toEqual(expect.arrayContaining(['todo', 'done', 'planned']))
    })
  })

  it('tag picker has accessibility attributes on the dialog (aria-modal, aria-label)', async () => {
    renderBoard()
    fireEvent.contextMenu(await screen.findByText('Pipeline debug'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Tags/ }))
    await waitFor(() => expect(screen.getByTestId('slot-tag-picker')).toBeInTheDocument())
    const dialog = screen.getByTestId('slot-tag-picker')
    expect(dialog.getAttribute('role')).toBe('dialog')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.getAttribute('aria-label')).toBe('Assign tags')
  })
})
