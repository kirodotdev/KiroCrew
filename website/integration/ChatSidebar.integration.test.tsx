import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, waitFor, fireEvent, act, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('@radix-ui/react-dropdown-menu', () => import('./__mocks__/@radix-ui/react-dropdown-menu'))
vi.mock('@radix-ui/react-context-menu', () => import('./__mocks__/@radix-ui/react-context-menu'))

import ChatSidebar from '../src/pages/ChatSidebar'
import { renderWithProviders } from './helpers'
import { server } from './mocks/server'
import { http, HttpResponse } from 'msw'
import { __resetAuthRecoveryStateForTests } from '../src/api/client'

const mockConfirm = vi.fn(() => true)
Object.defineProperty(window, 'confirm', { writable: true, value: mockConfirm })

const baseSlots = [
  { key: 'slot-1', title: 'Pipeline debug', running: false, agent: 'kirocrew', created: '2026-04-08T01:00:00Z', last_ts: '2026-04-08T02:00:00Z', folder_id: '' },
  { key: 'slot-2', title: 'Code review', running: true, agent: 'kirocrew', created: '2026-04-08T00:00:00Z', last_ts: '2026-04-08T01:30:00Z', folder_id: '' },
  { key: 'slot-3', title: 'Oncall triage', running: false, agent: 'oncall', created: '2026-04-07T10:00:00Z', last_ts: '2026-04-07T12:00:00Z', folder_id: '' },
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

describe('ChatSidebar Folder Grouping', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockConfirm.mockReturnValue(true)
    // Hermetic focus baseline. api/client.ts shows a session-expired banner on an
    // unhandled auth 403 and its token input grabs focus on a rAF (client.ts
    // ~L137). That module state latches across tests in the same file, so a
    // later rename/create test's rAF flush lets the banner steal focus from the
    // just-opened input → blur → commit → unmount, a jsdom-only focus-theft the
    // real browser never hits. Stub the auth endpoints so the banner never
    // shows, reset the module's latch + remove any stray banner, and clear any
    // bled-in focus before each test.
    server.use(
      http.post('/api/auth/refresh', () => HttpResponse.json({ ok: true })),
      http.get('/api/auth/me', () => HttpResponse.json({ ok: true })),
    )
    __resetAuthRecoveryStateForTests()
    ;(document.activeElement as HTMLElement | null)?.blur?.()
    // Default: no folders
    server.use(
      http.get('/api/chat/folders', () => HttpResponse.json([])),
      http.post('/api/chat/folders', async ({ request }) => {
        const body = await request.json() as { name: string }
        return HttpResponse.json({ id: 'f-new', name: body.name, order: 0, collapsed: false }, { status: 201 })
      }),
      http.patch('/api/chat/folders/:id', async ({ request, params }) => {
        const body = await request.json()
        return HttpResponse.json({ id: params.id, ...body })
      }),
      http.delete('/api/chat/folders/:id', () => HttpResponse.json({ ok: true })),
      http.patch('/api/chat/slots/:slot/folder', async ({ request }) => {
        const body = await request.json() as { folder_id: string }
        return HttpResponse.json({ ok: true, folder_id: body.folder_id })
      }),
    )
  })

  // Unmount + drop focus after each case so an open folder rename/create input
  // (and document.activeElement) doesn't survive into the next test file. jsdom
  // shares one document across files; without this the folder Escape-to-cancel
  // test and cross-file focus assertions become order-dependent. Scoped to this
  // file rather than a global afterEach(cleanup), which unmounts other files'
  // in-flight async trees (e.g. HooksPage) and breaks them.
  afterEach(() => {
    cleanup()
    ;(document.activeElement as HTMLElement | null)?.blur?.()
  })

  it('renders all sessions without folders by default', async () => {
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => {
      expect(screen.getByText('Pipeline debug')).toBeInTheDocument()
      expect(screen.getByText('Code review')).toBeInTheDocument()
      expect(screen.getByText('Oncall triage')).toBeInTheDocument()
    })
    expect(screen.queryByText('UNGROUPED')).not.toBeInTheDocument()
  })

  it('shortens the primary create action label to New', () => {
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    const createButton = screen.getByRole('button', { name: 'New chat session' })
    expect(createButton).toHaveTextContent('New')
    expect(createButton).not.toHaveTextContent('New chat')
  })

  it('shows provider logos on pull request chips', async () => {
    const slotsWithSources = [
      {
        ...baseSlots[0],
        source_links: [
          { provider: 'github' as const, number: 113, url: 'https://github.com/kirodotdev/KiroCrew/pull/113', state: 'merged' as const },
          { provider: 'gitlab' as const, number: 7, url: 'https://gitlab.com/acme/service/-/merge_requests/7' },
        ],
      },
    ]
    renderWithProviders(<ChatSidebar {...defaultProps} slots={slotsWithSources} />)

    const githubChip = (await screen.findByText('#113')).closest('a')
    const gitlabChip = screen.getByText('!7').closest('a')
    expect(githubChip?.querySelector('[data-provider-mark="github"]')).toBeInTheDocument()
    expect(gitlabChip?.querySelector('[data-provider-mark="gitlab"]')).toBeInTheDocument()
    expect(githubChip?.querySelector('[aria-label="Merged"]')).toHaveClass('text-aim')
    expect(githubChip).not.toHaveTextContent('Merged')
  })

  it('hides CI status icons on merged pull request chips', async () => {
    const slotsWithSources = [
      {
        ...baseSlots[0],
        source_links: [
          { provider: 'github' as const, number: 284, url: 'https://github.com/kirodotdev/KiroCrew/pull/284', state: 'merged' as const, ci: 'passed' as const },
          { provider: 'github' as const, number: 285, url: 'https://github.com/kirodotdev/KiroCrew/pull/285', state: 'open' as const, ci: 'passed' as const },
        ],
      },
    ]
    renderWithProviders(<ChatSidebar {...defaultProps} slots={slotsWithSources} />)

    const mergedChip = (await screen.findByText('#284')).closest('a')
    const openChip = screen.getByText('#285').closest('a')
    // Merged chip keeps the merge icon but drops the CI check — merged is terminal.
    expect(mergedChip?.querySelector('[aria-label="Merged"]')).toBeInTheDocument()
    expect(mergedChip?.querySelector('[aria-label="Checks passed"]')).not.toBeInTheDocument()
    // Open chip still shows its CI status.
    expect(openChip?.querySelector('[aria-label="Checks passed"]')).toBeInTheDocument()
  })

  it('shows new folder action in the create menu', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await user.click(await screen.findByLabelText('More create options'))
    await waitFor(() => expect(screen.getByText('New folder')).toBeInTheDocument())
  })

  it('creates a folder via the config modal and API', async () => {
    const user = userEvent.setup()
    let folders: any[] = []
    let posted: any = null
    server.use(
      http.get('/api/chat/folders', () => HttpResponse.json(folders)),
      http.post('/api/chat/folders', async ({ request }) => {
        posted = await request.json()
        const created = { id: 'f-new', name: posted.name, order: 0, collapsed: false }
        folders = [created]
        return HttpResponse.json(created, { status: 201 })
      }),
    )
    renderWithProviders(<ChatSidebar {...defaultProps} />)

    await user.click(await screen.findByLabelText('More create options'))
    await user.click(await screen.findByText('New folder'))
    // The name-only inline input was replaced by a modal that also collects the
    // folder's project directory, default agent and icon.
    const input = await screen.findByTestId('folder-config-name')
    await user.type(input, 'Oncall Work')
    await user.click(screen.getByTestId('folder-config-submit'))

    await waitFor(() => expect(screen.getByText('Oncall Work')).toBeInTheDocument(), { timeout: 3000 })
    // Top-level creation posts an empty parent, not a stale folder id.
    expect(posted.parent_id).toBe('')
  })

  it('closes the folder modal on Escape without creating', async () => {
    const user = userEvent.setup()
    const postSpy = vi.fn()
    server.use(
      http.post('/api/chat/folders', () => { postSpy(); return HttpResponse.json({}, { status: 201 }) }),
    )
    renderWithProviders(<ChatSidebar {...defaultProps} />)

    await user.click(await screen.findByLabelText('More create options'))
    await user.click(await screen.findByText('New folder'))
    expect(await screen.findByTestId('folder-config-name')).toBeInTheDocument()
    // Modal binds Escape at the window, so dispatch there rather than at the
    // input — this is focus-timing-independent (the input focuses on a rAF).
    fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' })

    await waitFor(() => expect(screen.queryByTestId('folder-config-name')).not.toBeInTheDocument())
    expect(postSpy).not.toHaveBeenCalled()
  })

  it('does not submit folder create on Enter while IME is composing', async () => {
    const user = userEvent.setup()
    const postSpy = vi.fn()
    server.use(
      http.post('/api/chat/folders', async ({ request }) => {
        postSpy()
        const body = await request.json() as { name: string }
        return HttpResponse.json({ id: 'f-new', name: body.name, order: 0, collapsed: false }, { status: 201 })
      }),
    )
    renderWithProviders(<ChatSidebar {...defaultProps} />)

    await user.click(await screen.findByLabelText('More create options'))
    await user.click(await screen.findByText('New folder'))
    const input = await screen.findByTestId('folder-config-name') as HTMLInputElement
    fireEvent.compositionStart(input)
    fireEvent.change(input, { target: { value: '测试' } })
    // Enter pressed mid-composition commits the composition, it does NOT submit.
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 13, isComposing: true })
    expect(postSpy).not.toHaveBeenCalled()
    // Modal stays open so the user can keep composing.
    expect(screen.getByTestId('folder-config-name')).toBeInTheDocument()
  })

  it('fetches folders from API on mount', async () => {
    server.use(
      http.get('/api/chat/folders', () => HttpResponse.json([
        { id: 'f1', name: 'Project A', order: 0, collapsed: false },
      ])),
    )
    const slotsWithFolder = [{ ...baseSlots[0], folder_id: 'f1' }, baseSlots[1], baseSlots[2]]
    renderWithProviders(<ChatSidebar {...defaultProps} slots={slotsWithFolder} />)

    await waitFor(() => {
      expect(screen.getByText('Project A')).toBeInTheDocument()
    })
    // Ungrouped slots still visible (no section header, just rendered below folders)
    expect(screen.getByText('Code review')).toBeInTheDocument()
  })

  it('collapses a folder', async () => {
    let folders = [{ id: 'f1', name: 'My Folder', order: 0, collapsed: false }]
    server.use(
      http.get('/api/chat/folders', () => HttpResponse.json(folders)),
      http.patch('/api/chat/folders/:id', async ({ request, params }) => {
        const body = await request.json() as any
        folders = folders.map(f => f.id === params.id ? { ...f, ...body } : f)
        return HttpResponse.json(folders.find(f => f.id === params.id))
      }),
    )
    const slotsWithFolder = [{ ...baseSlots[0], folder_id: 'f1' }, baseSlots[1], baseSlots[2]]
    renderWithProviders(<ChatSidebar {...defaultProps} slots={slotsWithFolder} />)

    await waitFor(() => expect(screen.getByText('My Folder')).toBeInTheDocument())
    // Child session visible before collapse
    expect(screen.getByText('Pipeline debug')).toBeInTheDocument()

    const collapseBtn = screen.getByTestId('folder-collapse-f1')
    fireEvent.click(collapseBtn)

    // Folder name still visible, but child session hidden after collapse
    expect(screen.getByText('My Folder')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText('Pipeline debug')).not.toBeVisible())
  })

  it('deletes a folder via API and slots become ungrouped', async () => {
    let folders = [{ id: 'f1', name: 'Delete Me', order: 0, collapsed: false }]
    server.use(
      http.get('/api/chat/folders', () => HttpResponse.json(folders)),
      http.delete('/api/chat/folders/:id', ({ params }) => {
        folders = folders.filter(f => f.id !== params.id)
        return HttpResponse.json({ ok: true })
      }),
    )
    const slotsWithFolder = [{ ...baseSlots[0], folder_id: 'f1' }, baseSlots[1], baseSlots[2]]
    renderWithProviders(<ChatSidebar {...defaultProps} slots={slotsWithFolder} />)
    await waitFor(() => expect(screen.getByText('Delete Me')).toBeInTheDocument())
    expect(screen.getByText('Pipeline debug')).toBeInTheDocument()

    // Delete now lives in the folder ⋯ overflow menu — open it first.
    fireEvent.click(screen.getByTestId('folder-menu-f1'))
    const deleteBtn = await screen.findByTestId('folder-delete-f1')
    fireEvent.click(deleteBtn)

    await waitFor(() => expect(screen.queryByText('Delete Me')).not.toBeInTheDocument())
    // Slot previously in the deleted folder is still visible, now ungrouped
    expect(screen.getByText('Pipeline debug')).toBeInTheDocument()
  })

  it('exposes folder actions via the ⋯ overflow menu', async () => {
    const folders = [{ id: 'f1', name: 'Menu Folder', order: 0, collapsed: false }]
    server.use(http.get('/api/chat/folders', () => HttpResponse.json(folders)))
    renderWithProviders(<ChatSidebar {...defaultProps} slots={baseSlots} />)
    await waitFor(() => expect(screen.getByText('Menu Folder')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('folder-menu-f1'))
    expect(await screen.findByTestId('folder-delete-f1')).toBeInTheDocument()
    expect(screen.getByTestId('folder-rename-f1')).toBeInTheDocument()
  })

  it('opens the folder ⋯ menu via keyboard (click activation)', async () => {
    const folders = [{ id: 'f1', name: 'Kbd Folder', order: 0, collapsed: false }]
    server.use(http.get('/api/chat/folders', () => HttpResponse.json(folders)))
    renderWithProviders(<ChatSidebar {...defaultProps} slots={baseSlots} />)
    await waitFor(() => expect(screen.getByText('Kbd Folder')).toBeInTheDocument())

    // Enter/Space on the ⋯ <button> fires click (not mousedown) — the open
    // logic must live on onClick so keyboard users can reach the menu.
    fireEvent.click(screen.getByTestId('folder-menu-f1'))
    expect(await screen.findByTestId('folder-rename-f1')).toBeInTheDocument()
  })

  it('renames a folder on double-click via API', async () => {
    const user = userEvent.setup()
    let folders = [{ id: 'f1', name: 'Old Name', order: 0, collapsed: false }]
    server.use(
      http.get('/api/chat/folders', () => HttpResponse.json(folders)),
      http.patch('/api/chat/folders/:id', async ({ request, params }) => {
        const body = await request.json() as any
        folders = folders.map(f => f.id === params.id ? { ...f, ...body } : f)
        return HttpResponse.json(folders.find(f => f.id === params.id))
      }),
    )
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => expect(screen.getByText('Old Name')).toBeInTheDocument())

    await user.dblClick(screen.getByText('Old Name'))
    const renameInput = screen.getByDisplayValue('Old Name')
    await user.clear(renameInput)
    await user.type(renameInput, 'New Name')
    await user.keyboard('{Enter}')

    expect(screen.getByText('New Name')).toBeInTheDocument()
  })

  // Regression: folder rename via the hover Rename button. Upstream drives this
  // through a folder ⋯ menu (whose Radix close-focus-restore blurs the
  // just-opened rename input — a known bug class); the fork's entry point
  // is an inline hover button, so the menu-close race can't fire here, but the
  // test still guards the user-visible survival: the input mounts, stays
  // mounted through the rAF focus flush, and commits.
  //
  // Asserts mount + commit, NOT caret/selection: jsdom drops activeElement to
  // <body> across portal teardown, so focus placement is browser-smoke
  // verified, not here.
  it('keeps the folder rename input open through the rename-button flow (edit not cancelled)', async () => {
    let folders = [{ id: 'f1', name: 'Old Name', order: 0, collapsed: false }]
    server.use(
      http.get('/api/chat/folders', () => HttpResponse.json(folders)),
      http.patch('/api/chat/folders/:id', async ({ request, params }) => {
        const body = await request.json() as any
        folders = folders.map(f => f.id === params.id ? { ...f, ...body } : f)
        return HttpResponse.json(folders.find(f => f.id === params.id))
      }),
    )
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => expect(screen.getByText('Old Name')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('folder-menu-f1'))
    fireEvent.click(await screen.findByTestId('folder-rename-f1'))
    // Flush the rAF focus effect (and any pending close-focus-restore).
    for (let i = 0; i < 3; i++) {
      await act(async () => { await new Promise(r => requestAnimationFrame(() => r(null))) })
    }

    // The edit survives: the input is still mounted. On broken code the
    // restore blurs it, renameCommit fires, editingId clears, and the input
    // unmounts — this would then throw.
    const input = screen.getByDisplayValue('Old Name') as HTMLInputElement
    expect(input).toBeInTheDocument()

    // And it commits: typing a new name + Enter persists via the API.
    fireEvent.change(input, { target: { value: 'New Name' } })
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', keyCode: 13 })
    await waitFor(() => expect(screen.getByText('New Name')).toBeInTheDocument())
  })

  // Regression: "New folder" from the create menu opens an inline input the same
  // way rename does, so it hit the same focus race — the caret didn't land in the
  // box (found in manual smoke). Asserts the input mounts + survives the menu
  // close (jsdom-reliable); caret placement is browser-smoke verified.
  it('keeps the New folder modal open through the create-menu close', async () => {
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    fireEvent.click(await screen.findByLabelText('More create options'))
    fireEvent.click(await screen.findByText('New folder'))
    // Flush the menu's close-focus-restore (double rAF in the mock).
    for (let i = 0; i < 3; i++) {
      await act(async () => { await new Promise(r => requestAnimationFrame(() => r(null))) })
    }
    // The modal survives the restore. A modal is structurally immune to the
    // blur-cancels-the-edit race the inline input had, but the assertion is kept
    // so a future regression to an auto-dismissing surface is caught here.
    expect(screen.getByTestId('folder-config-name')).toBeInTheDocument()
    // Dismiss so the open modal doesn't bleed into the next test.
    fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' })
  })

  it('pinned sessions appear above folders', async () => {
    server.use(
      http.get('/api/chat/folders', () => HttpResponse.json([
        { id: 'f1', name: 'Work', order: 0, collapsed: false },
      ])),
    )
    const slotsWithFolder = [{ ...baseSlots[0], folder_id: 'f1' }, baseSlots[1], { ...baseSlots[2], pinned: true }]
    renderWithProviders(<ChatSidebar {...defaultProps} slots={slotsWithFolder} />)

    await waitFor(() => {
      expect(screen.getByText('Work')).toBeInTheDocument()
      expect(screen.getByText('Oncall triage')).toBeInTheDocument()
    })
    // Folders render first, then ungrouped slots (pinned sort within ungrouped)
    const pinnedSlot = screen.getByText('Oncall triage')
    const folder = screen.getByText('Work')
    expect(folder.compareDocumentPosition(pinnedSlot) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows folder session count', async () => {
    server.use(
      http.get('/api/chat/folders', () => HttpResponse.json([
        { id: 'f1', name: 'Team', order: 0, collapsed: false },
      ])),
    )
    const slotsWithFolder = [
      { ...baseSlots[0], folder_id: 'f1' },
      { ...baseSlots[1], folder_id: 'f1' },
      baseSlots[2],
    ]
    renderWithProviders(<ChatSidebar {...defaultProps} slots={slotsWithFolder} />)

    await waitFor(() => expect(screen.getByText('Team')).toBeInTheDocument())
    const folderHeader = screen.getByText('Team').closest('.flex')
    expect(folderHeader).toHaveTextContent('2')
  })

  it('sessions are draggable', async () => {
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => expect(screen.getByText('Pipeline debug')).toBeInTheDocument())
    // Legacy-lane rows use dnd-kit drag (not native HTML5); drag-enabled state
    // is surfaced via data-draggable since dnd-kit's disabled prop is not a DOM
    // attribute.
    const sessionEl = screen.getByText('Pipeline debug').closest('[data-draggable]')
    expect(sessionEl).toBeTruthy()
    expect(sessionEl?.getAttribute('data-draggable')).toBe('true')
  })

  it('derives slot folders from slot.folder_id prop', async () => {
    server.use(
      http.get('/api/chat/folders', () => HttpResponse.json([
        { id: 'f1', name: 'Backend', order: 0, collapsed: false },
      ])),
    )
    const slotsWithFolder = [{ ...baseSlots[0], folder_id: 'f1' }, baseSlots[1], baseSlots[2]]
    renderWithProviders(<ChatSidebar {...defaultProps} slots={slotsWithFolder} />)

    await waitFor(() => {
      expect(screen.getByText('Backend')).toBeInTheDocument()
    })
    // Ungrouped slots still visible without a section header
    expect(screen.getByText('Code review')).toBeInTheDocument()
  })
})

describe('ChatSidebar confirmCloseSession', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockConfirm.mockReturnValue(true)
    server.use(
      http.get('/api/chat/folders', () => HttpResponse.json([])),
      http.delete('/api/chat/slots/:key', () => HttpResponse.json({ ok: true })),
    )
  })

  it('skips confirm dialog when confirmCloseSession is false', async () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: false }))
    const deleteSpy = vi.fn()
    server.use(
      http.delete('/api/chat/slots/:key', ({ params }) => { deleteSpy(params.key); return HttpResponse.json({ ok: true }) }),
    )
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => expect(screen.getByText('Pipeline debug')).toBeInTheDocument())

    const closeBtn = screen.getByText('Pipeline debug').closest('[draggable]')!.querySelector('[aria-label="Close session"]')!
    fireEvent.click(closeBtn)

    expect(mockConfirm).not.toHaveBeenCalled()
    await waitFor(() => expect(deleteSpy).toHaveBeenCalledWith('slot-1'))
  })

  it('does not delete when user cancels the confirm dialog', async () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: true }))
    mockConfirm.mockReturnValue(false)
    const deleteSpy = vi.fn()
    server.use(
      http.delete('/api/chat/slots/:key', ({ params }) => { deleteSpy(params.key); return HttpResponse.json({ ok: true }) }),
    )
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => expect(screen.getByText('Pipeline debug')).toBeInTheDocument())

    const closeBtn = screen.getByText('Pipeline debug').closest('[draggable]')!.querySelector('[aria-label="Close session"]')!
    fireEvent.click(closeBtn)

    expect(mockConfirm).toHaveBeenCalledWith('Close this session?')
    expect(deleteSpy).not.toHaveBeenCalled()
  })

  it('shows confirm dialog when confirmCloseSession is true', async () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: true }))
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => expect(screen.getByText('Pipeline debug')).toBeInTheDocument())

    const closeBtn = screen.getByText('Pipeline debug').closest('[draggable]')!.querySelector('[aria-label="Close session"]')!
    fireEvent.click(closeBtn)

    expect(mockConfirm).toHaveBeenCalledWith('Close this session?')
  })
})

describe('ChatSidebar Cleanup', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    server.use(
      http.get('/api/chat/folders', () => HttpResponse.json([])),
      http.post('/api/chat/slots/cleanup', async ({ request }) => {
        const body = await request.json() as { max_inactive_days: number; active_slot: string; dry_run?: boolean }
        if (body.dry_run) {
          return HttpResponse.json({ ok: true, dry_run: true, keys: ['slot-old-1', 'slot-old-2'], count: 2 })
        }
        return HttpResponse.json({ ok: true, archived: 2, keys: ['slot-old-1', 'slot-old-2'], failed: [] })
      }),
    )
  })

  async function openCleanup(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByTitle('More options'))
    await user.click(await screen.findByText('Clean up sessions'))
  }

  it('shows cleanup option in More options menu', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await user.click(await screen.findByTitle('More options'))
    await waitFor(() => expect(screen.getByText('Clean up sessions')).toBeInTheDocument())
  })

  it('opens cleanup dialog on click', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await openCleanup(user)
    await waitFor(() => expect(screen.getByText('Clean Up Sessions')).toBeInTheDocument())
  })

  it('shows day selector buttons', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await openCleanup(user)
    await waitFor(() => {
      expect(screen.getByText('1 day')).toBeInTheDocument()
      expect(screen.getByText('3 days')).toBeInTheDocument()
      expect(screen.getByText('7 days')).toBeInTheDocument()
    })
  })

  it('closes dialog on Cancel', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await openCleanup(user)
    await waitFor(() => expect(screen.getByText('Clean Up Sessions')).toBeInTheDocument())
    await user.click(screen.getByText('Cancel'))
    expect(screen.queryByText('Clean Up Sessions')).not.toBeInTheDocument()
  })

  it('excludes pinned sessions from stale count', async () => {
    const user = userEvent.setup()
    const oldTs = '2020-01-01T00:00:00Z'
    const slotsWithPinned = [
      { ...baseSlots[0], last_ts: oldTs },
      { ...baseSlots[1], last_ts: oldTs, pinned: true },
      { ...baseSlots[2], last_ts: oldTs },
    ]
    server.use(
      http.post('/api/chat/slots/cleanup', async ({ request }) => {
        const body = await request.json() as any
        if (body.dry_run) return HttpResponse.json({ ok: true, dry_run: true, keys: ['slot-3'], count: 1 })
        return HttpResponse.json({ ok: true, archived: 1, keys: ['slot-3'], failed: [] })
      }),
    )
    renderWithProviders(<ChatSidebar {...defaultProps} slots={slotsWithPinned} activeSlot="slot-1" />)
    await openCleanup(user)
    // slot-1 is active (excluded), slot-2 is pinned (excluded), only slot-3 is archivable
    await waitFor(() => expect(screen.getByText(/1 session will be moved/)).toBeInTheDocument())
  })

  it('shows active-slot-skipped message when active slot is stale', async () => {
    const user = userEvent.setup()
    server.use(
      http.post('/api/chat/slots/cleanup', async ({ request }) => {
        const body = await request.json() as any
        if (body.dry_run) return HttpResponse.json({ ok: true, dry_run: true, keys: ['slot-2'], count: 1, active_is_stale: true })
        return HttpResponse.json({ ok: true, archived: 1, keys: ['slot-2'], failed: [] })
      }),
    )
    const oldTs = '2020-01-01T00:00:00Z'
    const staleSlots = [
      { ...baseSlots[0], last_ts: oldTs },
      { ...baseSlots[1], last_ts: oldTs },
    ]
    renderWithProviders(<ChatSidebar {...defaultProps} slots={staleSlots} activeSlot="slot-1" />)
    await openCleanup(user)
    await waitFor(() => expect(screen.getByText(/skipped.*currently selected/)).toBeInTheDocument())
  })

  it('shows no-sessions message when nothing is stale', async () => {
    const user = userEvent.setup()
    server.use(
      http.post('/api/chat/slots/cleanup', async ({ request }) => {
        const body = await request.json() as any
        if (body.dry_run) return HttpResponse.json({ ok: true, dry_run: true, keys: [], count: 0 })
        return HttpResponse.json({ ok: true, archived: 0, keys: [], failed: [] })
      }),
    )
    const now = new Date().toISOString()
    const freshSlots = baseSlots.map(s => ({ ...s, last_ts: now, created: now }))
    renderWithProviders(<ChatSidebar {...defaultProps} slots={freshSlots} />)
    await openCleanup(user)
    await waitFor(() => expect(screen.getByText('No inactive sessions to archive.')).toBeInTheDocument())
  })

  it('calls cleanup API and closes dialog on archive', async () => {
    const user = userEvent.setup()
    const cleanupSpy = vi.fn()
    server.use(
      http.post('/api/chat/slots/cleanup', async ({ request }) => {
        const body = await request.json() as any
        if (body.dry_run) return HttpResponse.json({ ok: true, dry_run: true, keys: ['slot-3'], count: 1 })
        cleanupSpy(body)
        return HttpResponse.json({ ok: true, archived: 1, keys: ['slot-3'], failed: [] })
      }),
    )
    const oldTs = '2020-01-01T00:00:00Z'
    const now = new Date().toISOString()
    const slotsWithStale = [
      { ...baseSlots[0], last_ts: now },
      { ...baseSlots[1], last_ts: now },
      { ...baseSlots[2], last_ts: oldTs },
    ]
    renderWithProviders(<ChatSidebar {...defaultProps} slots={slotsWithStale} />)
    await openCleanup(user)
    await waitFor(() => expect(screen.getByText(/1 session will be moved/)).toBeInTheDocument())

    const archiveBtn = screen.getByText(/Archive 1 session/)
    await user.click(archiveBtn)

    await waitFor(() => {
      expect(cleanupSpy).toHaveBeenCalledWith(
        expect.objectContaining({ max_inactive_days: 3, active_slot: 'slot-1' })
      )
    })
    expect(screen.queryByText('Clean Up Sessions')).not.toBeInTheDocument()
  })

  it('switching day threshold updates stale count', async () => {
    const user = userEvent.setup()
    let callCount = 0
    server.use(
      http.post('/api/chat/slots/cleanup', async ({ request }) => {
        const body = await request.json() as any
        if (body.dry_run) {
          callCount++
          // First call (3 days): nothing stale. Second call (1 day): 2 stale.
          if (body.max_inactive_days <= 1) return HttpResponse.json({ ok: true, dry_run: true, keys: ['slot-2', 'slot-3'], count: 2 })
          return HttpResponse.json({ ok: true, dry_run: true, keys: [], count: 0 })
        }
        return HttpResponse.json({ ok: true, archived: 0, keys: [], failed: [] })
      }),
    )
    const now = new Date()
    const twoDaysAgo = new Date(now.getTime() - 2 * 86400000).toISOString()
    const slotsWithMixed = [
      { ...baseSlots[0], last_ts: now.toISOString() },
      { ...baseSlots[1], last_ts: twoDaysAgo },
      { ...baseSlots[2], last_ts: twoDaysAgo },
    ]
    renderWithProviders(<ChatSidebar {...defaultProps} slots={slotsWithMixed} />)
    await openCleanup(user)

    // Default 3 days: nothing stale (2 days < 3 days)
    await waitFor(() => expect(screen.getByText('No inactive sessions to archive.')).toBeInTheDocument())

    // Switch to 1 day: 2 sessions become stale
    await user.click(screen.getByText('1 day'))
    await waitFor(() => expect(screen.getByText(/2 sessions will be moved/)).toBeInTheDocument())
  })

  it('context menu shows Copy link and calls copySessionLink', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, writable: true, configurable: true })
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    const row = await screen.findByText('Pipeline debug')
    fireEvent.contextMenu(row)
    const copyItem = await screen.findByRole('menuitem', { name: /Copy link/ })
    fireEvent.click(copyItem)
    await waitFor(() => expect(writeText).toHaveBeenCalled())
  })

  it('action buttons stay visible while context menu is open', async () => {
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    const row = await screen.findByText('Pipeline debug')
    // Open the context menu (mock sets data-state="open" on trigger)
    fireEvent.contextMenu(row)
    // The ContextMenu trigger child should now have data-state="open",
    // which activates the CSS has-[[data-state=open]]:opacity-100 rule.
    const sessionRow = row.closest('.session-row')!
    expect(sessionRow.getAttribute('data-state')).toBe('open')
  })

  it('clicking Duplicate button calls fork endpoint and switches slot', async () => {
    server.use(
      http.post('/api/chat/slots/:slot/fork', ({ params }) => {
        return HttpResponse.json({ ok: true, key: `${params.slot}-fork` })
      }),
    )
    const { store } = renderWithProviders(<ChatSidebar {...defaultProps} />)
    await screen.findByText('Pipeline debug')
    const dupBtn = screen.getAllByLabelText('Duplicate')[0]
    fireEvent.click(dupBtn)
    await waitFor(() => {
      expect(store.getState().chat.activeSlot).toBe('slot-1-fork')
    })
  })

  it('fork failure does not crash UI', async () => {
    server.use(
      http.post('/api/chat/slots/:slot/fork', () => HttpResponse.json({ error: 'boom' }, { status: 500 })),
    )
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await screen.findByText('Pipeline debug')
    const dupBtn = screen.getAllByLabelText('Duplicate')[0]
    fireEvent.click(dupBtn)
    // UI remains intact — sessions still rendered
    await waitFor(() => {
      expect(screen.getByText('Pipeline debug')).toBeInTheDocument()
    })
  })
})

describe('ChatSidebar Folder Reorder', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    server.use(
      http.get('/api/chat/folders', () => HttpResponse.json([
        { id: 'f-1', name: 'Alpha', order: 0, collapsed: false, parent_id: '' },
        { id: 'f-2', name: 'Beta', order: 1, collapsed: false, parent_id: '' },
        { id: 'f-3', name: 'Gamma', order: 2, collapsed: false, parent_id: '' },
      ])),
    )
  })

  it('renders folders sorted by order field', async () => {
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => {
      expect(screen.getByText('Alpha')).toBeInTheDocument()
      expect(screen.getByText('Beta')).toBeInTheDocument()
      expect(screen.getByText('Gamma')).toBeInTheDocument()
    })
    // Verify order: Alpha before Beta before Gamma. Anchored full-string
    // match: the empty-folder rows render "New chat in <name>", which an
    // unanchored /Alpha|Beta|Gamma/ would also collect.
    const folderNames = screen.getAllByText(/^(Alpha|Beta|Gamma)$/).map(el => el.textContent)
    expect(folderNames).toEqual(['Alpha', 'Beta', 'Gamma'])
  })

  it('renders sortable wrapper with data-folder-sortable attribute', async () => {
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => expect(screen.getByText('Alpha')).toBeInTheDocument())
    expect(document.querySelector('[data-folder-sortable="f-1"]')).toBeInTheDocument()
    expect(document.querySelector('[data-folder-sortable="f-2"]')).toBeInTheDocument()
  })

  it('folder header is the whole-row drag handle (no grip)', async () => {
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => expect(screen.getByText('Alpha')).toBeInTheDocument())
    // The grip handle was removed; the whole folder header row is now the
    // drag handle (pointer listeners forwarded onto the header, cursor-grab).
    const handles = document.querySelectorAll('[data-folder-sortable] .cursor-grab')
    expect(handles.length).toBeGreaterThanOrEqual(3)
    // The legacy "Reorder" grip handle no longer exists.
    expect(document.querySelector('[data-folder-sortable] [aria-label^="Reorder"]')).toBeNull()
  })

  // Reorder logic is unit-tested in src/test/reorderFolders.test.ts
  // (jsdom cannot simulate dnd-kit drag interactions)
})
