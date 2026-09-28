/**
 * Tests for TagManagerList — the tag-management list shared by the board
 * column-filter popover and the header "Manage tags…" panel in list view.
 * Covers both modes
 * (manage / column-filter) for rename · status · delete-with-confirm · create,
 * plus the include/exclude swatch behaviour that keeps the board mutating its
 * column's tag_ids identically.
 *
 * Also carries the SessionActionsMenu regression: the per-session "Tags…" item
 * must render regardless of the (board-only) tagColumnsEnabled config, so
 * the tag picker is reachable from the list-view row menu too.
 */
import { StrictMode } from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: {
    chatTags: vi.fn().mockResolvedValue([
      { id: 't1', name: 'Alpha', color: '#ff0000', order: 0, agent: 'none', agent_provenanced: true, agent_store_degraded: false },
      { id: 't2', name: 'Beta', color: '#00ff00', order: 1, status: true, agent: 'none', agent_provenanced: false, agent_store_degraded: false },
    ]),
    createChatTag: vi.fn().mockResolvedValue({ ok: true }),
    updateChatTag: vi.fn().mockResolvedValue({ ok: true }),
    adoptChatTag: vi.fn().mockResolvedValue({ ok: true }),
    deleteChatTag: vi.fn().mockResolvedValue({ ok: true }),
    chatFolders: vi.fn().mockResolvedValue([]),
    slackChannels: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    mcpActive: vi.fn().mockResolvedValue([]),
  },
}))

// The board gates its column strip behind this flag; the list-view "Tags…" item
// and pills must NOT depend on it. Force it off so the regression is explicit.
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

import { api } from '../api/client'
import TagManagerList from '../components/TagManagerList'
import SessionActionsMenu from '../components/SessionActionsMenu'
import { TagPopoverProvider } from '../hooks/useTagPopover'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent } from '../components/ui/dropdown-menu'
import type { RootState } from '../store'

function renderList(props: React.ComponentProps<typeof TagManagerList>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <TagManagerList {...props} />
    </QueryClientProvider>,
  )
}

function renderCoMountedLists() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <div data-testid="manage-instance">
        <TagManagerList mode="manage" createTestId="manage-create" />
      </div>
      <div data-testid="filter-instance">
        <TagManagerList mode="column-filter" createTestId="filter-create" />
      </div>
    </QueryClientProvider>,
  )
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, resolve, reject }
}

beforeEach(() => vi.clearAllMocks())

describe('TagManagerList — shared CRUD (both modes)', () => {
  it('renames a tag on blur (persists only a changed, non-empty value)', async () => {
    renderList({ mode: 'manage' })
    const input = await screen.findByTestId('tag-name-t1')
    fireEvent.change(input, { target: { value: 'Renamed' } })
    fireEvent.blur(input)
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledWith('t1', { name: 'Renamed' }))
  })

  it('reverts the rename on Escape and restores on empty blur (no persist)', async () => {
    renderList({ mode: 'manage' })
    const input = await screen.findByTestId('tag-name-t1') as HTMLInputElement
    // Escape reverts the in-progress edit to the canonical name without persisting.
    fireEvent.change(input, { target: { value: 'Scratch' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(input.value).toBe('Alpha')
    // A blank blur restores the name rather than leaving an empty row, and never persists.
    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.blur(input)
    expect(input.value).toBe('Alpha')
    expect(api.updateChatTag).not.toHaveBeenCalled()
  })

  it('toggles a provenanced tag\'s status flag', async () => {
    renderList({ mode: 'manage' })
    fireEvent.click(await screen.findByTestId('tag-status-t1')) // t1 has no status → turn on
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledWith('t1', { status: true }))
  })

  it('surfaces update failures through ErrorNotice', async () => {
    vi.mocked(api.updateChatTag).mockRejectedValueOnce(new Error('Rename could not be saved.'))
    renderList({ mode: 'manage' })
    const input = await screen.findByTestId('tag-name-t1') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Renamed' } })
    fireEvent.blur(input)
    expect(await screen.findByText('Rename could not be saved.')).toBeInTheDocument()
    expect(screen.getByTestId('tag-crud-mutation-error')).toBeInTheDocument()
    expect(input.value).toBe('Renamed')
  })

  it('deletes a tag only after confirm', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm')
    renderList({ mode: 'manage' })
    const del = await screen.findByTestId('tag-delete-t1')

    confirmSpy.mockReturnValueOnce(false)
    fireEvent.click(del)
    expect(api.deleteChatTag).not.toHaveBeenCalled()

    confirmSpy.mockReturnValueOnce(true)
    fireEvent.click(del)
    await waitFor(() => expect(api.deleteChatTag).toHaveBeenCalledWith('t1'))
    confirmSpy.mockRestore()
  })

  it('surfaces delete failures through ErrorNotice', async () => {
    vi.mocked(api.deleteChatTag).mockRejectedValueOnce(new Error('Tag could not be deleted.'))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderList({ mode: 'manage' })
    fireEvent.click(await screen.findByTestId('tag-delete-t1'))
    expect(await screen.findByText('Tag could not be deleted.')).toBeInTheDocument()
    expect(screen.getByTestId('tag-crud-mutation-error')).toBeInTheDocument()
    confirmSpy.mockRestore()
  })

  it('creates a tag on Enter and clears the exact submitted draft after success', async () => {
    renderList({ mode: 'manage' })
    const input = await screen.findByTestId('tag-create') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Gamma' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(api.createChatTag).toHaveBeenCalledWith('Gamma', undefined, undefined))
    await waitFor(() => expect(input.value).toBe(''))
  })

  it('clears the submitted create draft under StrictMode', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(
      <StrictMode>
        <QueryClientProvider client={qc}>
          <TagManagerList mode="manage" />
        </QueryClientProvider>
      </StrictMode>,
    )
    const input = await screen.findByTestId('tag-create') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Gamma' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(api.createChatTag).toHaveBeenCalledWith('Gamma', undefined, undefined))
    await waitFor(() => expect(input.value).toBe(''))
  })

  it('surfaces create failures and preserves the submitted draft', async () => {
    vi.mocked(api.createChatTag).mockRejectedValueOnce(new Error('Tag could not be created.'))
    renderList({ mode: 'manage' })
    const input = await screen.findByTestId('tag-create') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Gamma' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(await screen.findByText('Tag could not be created.')).toBeInTheDocument()
    expect(screen.getByTestId('tag-crud-mutation-error')).toBeInTheDocument()
    expect(input.value).toBe('Gamma')
  })

  it('does not erase typing entered while create is pending', async () => {
    const pending = deferred<{ ok: boolean }>()
    vi.mocked(api.createChatTag).mockReturnValueOnce(pending.promise)
    renderList({ mode: 'manage' })
    const input = await screen.findByTestId('tag-create') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Gamma' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(api.createChatTag).toHaveBeenCalledWith('Gamma', undefined, undefined))

    fireEvent.change(input, { target: { value: 'Gamma revised' } })
    await act(async () => { pending.resolve({ ok: true }) })
    await waitFor(() => expect(input.value).toBe('Gamma revised'))
  })

  it('submits only once for two same-tick Enter events', async () => {
    const pending = deferred<{ ok: boolean }>()
    vi.mocked(api.createChatTag).mockReturnValueOnce(pending.promise)
    renderList({ mode: 'manage' })
    const input = await screen.findByTestId('tag-create') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Gamma' } })

    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(api.createChatTag).toHaveBeenCalledTimes(1))

    await act(async () => { pending.resolve({ ok: true }) })
  })

  it.each([
    { newerOutcome: 'success', newerError: null },
    { newerOutcome: 'failure', newerError: 'Newer create failed.' },
  ])('keeps an older rename failure visible after an unrelated create $newerOutcome', async ({ newerError }) => {
    const olderUpdate = deferred<{ ok: boolean }>()
    const newerCreate = deferred<{ ok: boolean }>()
    vi.mocked(api.updateChatTag).mockReturnValueOnce(olderUpdate.promise)
    vi.mocked(api.createChatTag).mockReturnValueOnce(newerCreate.promise)
    renderList({ mode: 'manage' })

    const rename = await screen.findByTestId('tag-name-t1') as HTMLInputElement
    fireEvent.change(rename, { target: { value: 'Older rename' } })
    fireEvent.blur(rename)
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledWith('t1', { name: 'Older rename' }))

    const create = screen.getByTestId('tag-create')
    fireEvent.change(create, { target: { value: 'Newer tag' } })
    fireEvent.keyDown(create, { key: 'Enter' })
    await waitFor(() => expect(api.createChatTag).toHaveBeenCalledWith('Newer tag', undefined, undefined))

    await act(async () => {
      if (newerError) newerCreate.reject(new Error(newerError))
      else newerCreate.resolve({ ok: true })
    })
    if (newerError) expect(await screen.findByText(newerError)).toBeInTheDocument()

    // The rename did not persist: a create on another target must not hide that.
    await act(async () => { olderUpdate.reject(new Error('Older rename failed.')) })
    expect(await screen.findByText('Older rename failed.')).toBeInTheDocument()
    if (newerError) expect(screen.getByText(newerError)).toBeInTheDocument()
  })

  it('ignores a rename failure superseded by a newer rename of the same tag', async () => {
    const olderRename = deferred<{ ok: boolean }>()
    const newerRename = deferred<{ ok: boolean }>()
    vi.mocked(api.updateChatTag)
      .mockReturnValueOnce(olderRename.promise)
      .mockReturnValueOnce(newerRename.promise)
    renderList({ mode: 'manage' })

    const rename = await screen.findByTestId('tag-name-t1') as HTMLInputElement
    fireEvent.change(rename, { target: { value: 'First' } })
    fireEvent.blur(rename)
    fireEvent.change(rename, { target: { value: 'Second' } })
    fireEvent.blur(rename)
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledTimes(2))

    await act(async () => { newerRename.resolve({ ok: true }) })
    await act(async () => { olderRename.reject(new Error('Superseded rename failed.')) })
    expect(screen.queryByText('Superseded rename failed.')).toBeNull()
    expect(screen.queryByTestId('tag-crud-mutation-error')).toBeNull()
  })

  it('keeps a rename failure visible after a status flip on the same tag', async () => {
    const rename = deferred<{ ok: boolean }>()
    vi.mocked(api.updateChatTag)
      .mockReturnValueOnce(rename.promise)
      .mockResolvedValueOnce({ ok: true })
    renderList({ mode: 'manage' })

    const input = await screen.findByTestId('tag-name-t1') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Renamed' } })
    fireEvent.blur(input)
    fireEvent.click(screen.getByTestId('tag-status-t1'))
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledWith('t1', { status: true }))

    await act(async () => { rename.reject(new Error('Rename could not be saved.')) })
    expect(await screen.findByText('Rename could not be saved.')).toBeInTheDocument()
  })
})

describe('TagManagerList — manage mode', () => {
  it('renders swatches as colour buttons, not filter checkboxes', async () => {
    renderList({ mode: 'manage' })
    await screen.findByTestId('tag-row-t1')
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.getByTestId('tag-color-t1')).toHaveAttribute('aria-expanded', 'false')
  })

  it('uses a bounded responsive scroll region with a stable visible gutter', async () => {
    renderList({ mode: 'manage' })
    const region = await screen.findByTestId('tag-scroll-region')
    expect(region).toHaveClass('overflow-y-scroll', '[scrollbar-gutter:stable]')
    expect(region.className).toContain('max-h-[min(52vh,420px)]')
    expect(region.className).toContain('sm:max-h-[min(60vh,520px)]')
  })

  it('opens the palette from the swatch and PATCHes the picked colour', async () => {
    renderList({ mode: 'manage' })
    const swatch = await screen.findByTestId('tag-color-t1')
    fireEvent.click(swatch)
    expect(swatch).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('tag-palette-t1')).toBeInTheDocument()
    // Pick green from the shared folder palette.
    fireEvent.click(screen.getByTestId('tag-color-t1-22c55e'))
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledWith('t1', { color: '#22c55e' }))
    // Palette closes and focus returns to the swatch (not <body>).
    expect(screen.queryByTestId('tag-palette-t1')).toBeNull()
    expect(document.activeElement).toBe(swatch)
  })

  it('marks the tag\'s current colour as pressed in the palette', async () => {
    renderList({ mode: 'manage' })
    // t1 is #ff0000 (not in the palette) — nothing pressed.
    fireEvent.click(await screen.findByTestId('tag-color-t1'))
    const pressed = screen.getByTestId('tag-palette-t1').querySelectorAll('[aria-pressed="true"]')
    expect(pressed.length).toBe(0)
  })

  it('Escape closes the palette without persisting and refocuses the swatch', async () => {
    renderList({ mode: 'manage' })
    const swatch = await screen.findByTestId('tag-color-t1')
    fireEvent.click(swatch)
    fireEvent.keyDown(screen.getByTestId('tag-palette-t1'), { key: 'Escape' })
    expect(screen.queryByTestId('tag-palette-t1')).toBeNull()
    expect(api.updateChatTag).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(swatch)
  })

  it('only one palette is open at a time (opening another closes the first)', async () => {
    renderList({ mode: 'manage' })
    fireEvent.click(await screen.findByTestId('tag-color-t1'))
    fireEvent.click(screen.getByTestId('tag-color-t2'))
    expect(screen.queryByTestId('tag-palette-t1')).toBeNull()
    expect(screen.getByTestId('tag-palette-t2')).toBeInTheDocument()
  })
})

describe('TagManagerList — agent policy A1', () => {
  it('shows all three labels with exclusive radio selection semantics', async () => {
    renderList({ mode: 'manage' })
    const group = await screen.findByRole('radiogroup', { name: 'Agent permissions for Alpha' })
    const human = within(group).getByRole('radio', { name: 'Human only' })
    const addOnly = within(group).getByRole('radio', { name: 'Agent: add only' })
    const addRemove = within(group).getByRole('radio', { name: 'Agent: add & remove' })
    expect(human).toHaveAttribute('aria-checked', 'true')
    expect(addOnly).toHaveAttribute('aria-checked', 'false')
    expect(addRemove).toHaveAttribute('aria-checked', 'false')
    expect(human).toHaveTextContent('Human only')
    expect(human).toHaveAttribute('title', 'Only people can add or remove this tag from sessions')
    expect(addOnly).toHaveTextContent('Agent: add only')
    expect(addOnly).toHaveAttribute(
      'title',
      'Agents can add this tag to sessions but cannot remove it',
    )
    expect(addRemove).toHaveTextContent('Agent: add & remove')
    expect(addRemove).toHaveAttribute(
      'title',
      'Agents can add or remove this tag from sessions',
    )

    fireEvent.click(addOnly)
    await waitFor(() => {
      expect(api.updateChatTag).toHaveBeenCalledWith('t1', { agent: 'add-only' })
    })
  })

  it('requires an explicit adoption before a legacy tag exposes policy choices', async () => {
    renderList({ mode: 'manage' })
    expect(await screen.findByText('This tag is not set up for agents yet')).toBeInTheDocument()
    expect(screen.queryByRole('radiogroup', { name: 'Agent permissions for Beta' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Set up agent permissions' }))
    await waitFor(() => expect(api.adoptChatTag).toHaveBeenCalledWith('t2', true))
    expect(api.updateChatTag).not.toHaveBeenCalledWith('t2', expect.objectContaining({ agent: expect.anything() }))
  })

  it('disables a legacy tag status toggle until explicit adoption and describes why', async () => {
    renderList({ mode: 'manage' })
    const reason = await screen.findByText('This tag is not set up for agents yet')
    const status = screen.getByTestId('tag-status-t2')
    expect(status).toBeDisabled()
    expect(status).toHaveAttribute('aria-disabled', 'true')
    expect(status).toHaveAttribute('aria-describedby', reason.id)

    fireEvent.click(status)
    expect(api.updateChatTag).not.toHaveBeenCalledWith('t2', { status: false })
    expect(api.adoptChatTag).not.toHaveBeenCalled()
  })

  it('disables degraded status, create, delete, and adoption while keeping name and color editable', async () => {
    vi.mocked(api.chatTags).mockResolvedValueOnce([
      {
        id: 'legacy', name: 'Legacy', color: '#ff0000', order: 0,
        agent: 'none', agent_provenanced: false, agent_store_degraded: true,
      },
      {
        id: 'ready', name: 'Ready', color: '#00ff00', order: 1,
        agent: 'none', agent_provenanced: true, agent_store_degraded: true,
      },
    ])
    renderList({ mode: 'manage' })

    expect(await screen.findByText(
      'Tag permissions cannot be loaded right now, so creating, deleting, status changes, and agent-permission changes are unavailable. Renaming and recoloring still work. Try again later.',
    )).toBeInTheDocument()
    const reason = screen.getByTestId('tag-policy-store-error')
    expect(reason.id).toContain('tag-policy-unavailable')
    const allow = screen.getByRole('button', { name: 'Set up agent permissions' })
    expect(allow).toHaveAttribute('aria-disabled', 'true')
    expect(allow).toHaveAttribute('aria-describedby', reason.id)
    const radios = within(
      screen.getByRole('radiogroup', { name: 'Agent permissions for Ready' }),
    ).getAllByRole('radio')
    expect(radios).toHaveLength(3)
    expect(radios.every(radio => radio.getAttribute('aria-describedby') === reason.id)).toBe(true)

    const readyStatus = screen.getByTestId('tag-status-ready')
    expect(readyStatus).toBeDisabled()
    expect(readyStatus).toHaveAttribute('aria-describedby', reason.id)
    const legacyStatus = screen.getByTestId('tag-status-legacy')
    expect(legacyStatus).toBeDisabled()
    expect(legacyStatus.getAttribute('aria-describedby')).toContain(reason.id)
    const create = screen.getByTestId('tag-create')
    expect(create).toBeDisabled()
    expect(create).toHaveAttribute('aria-describedby', reason.id)
    for (const id of ['legacy', 'ready']) {
      const remove = screen.getByTestId(`tag-delete-${id}`)
      expect(remove).toBeDisabled()
      expect(remove).toHaveAttribute('aria-describedby', reason.id)
    }

    expect(screen.getByTestId('tag-name-ready')).not.toBeDisabled()
    expect(screen.getByTestId('tag-color-ready')).not.toBeDisabled()
    fireEvent.click(allow)
    fireEvent.click(readyStatus)
    fireEvent.click(screen.getByTestId('tag-delete-ready'))
    expect(api.adoptChatTag).not.toHaveBeenCalled()
    expect(api.updateChatTag).not.toHaveBeenCalledWith('ready', expect.objectContaining({ status: expect.anything() }))
    expect(api.deleteChatTag).not.toHaveBeenCalled()
  })

  it('co-mounts manage and filter instances with unique locally owned descriptions', async () => {
    vi.mocked(api.chatTags).mockResolvedValueOnce([
      {
        id: 'legacy', name: 'Legacy', color: '#ff0000', order: 0,
        agent: 'none', agent_provenanced: false, agent_store_degraded: true,
      },
      {
        id: 'ready', name: 'Ready', color: '#00ff00', order: 1,
        agent: 'none', agent_provenanced: true, agent_store_degraded: true,
      },
    ])
    renderCoMountedLists()

    await screen.findAllByTestId('tag-policy-store-error')
    const manage = screen.getByTestId('manage-instance')
    const filter = screen.getByTestId('filter-instance')
    const manageReason = within(manage).getByTestId('tag-policy-store-error')
    const filterReason = within(filter).getByTestId('tag-policy-store-error')
    expect(manageReason.id).not.toBe(filterReason.id)

    for (const [instance, reason] of [[manage, manageReason], [filter, filterReason]] as const) {
      expect(within(instance).getByTestId('tag-status-ready')).toHaveAttribute(
        'aria-describedby',
        reason.id,
      )
      const legacyDescriptionIds = within(instance).getByTestId('tag-status-legacy')
        .getAttribute('aria-describedby')?.split(/\s+/) ?? []
      expect(legacyDescriptionIds).toContain(reason.id)
      expect(legacyDescriptionIds).toHaveLength(2)

      for (const control of instance.querySelectorAll<HTMLElement>('[aria-describedby]')) {
        for (const ownerId of control.getAttribute('aria-describedby')?.split(/\s+/) ?? []) {
          const owner = document.getElementById(ownerId)
          expect(owner, `${ownerId} must resolve`).not.toBeNull()
          expect(instance.contains(owner)).toBe(true)
        }
      }
    }
  })

  it('shows catalog adoption and policy errors, never the server\'s English prose', async () => {
    vi.mocked(api.adoptChatTag).mockRejectedValueOnce(
      Object.assign(new Error('Only the dashboard owner can allow agents to use this tag.'), { status: 403 }),
    )
    vi.mocked(api.updateChatTag).mockRejectedValueOnce(
      Object.assign(new Error('This tag is not set up for agents yet.'), { status: 400 }),
    )
    renderList({ mode: 'manage' })

    fireEvent.click(await screen.findByRole('button', { name: 'Set up agent permissions' }))
    expect(await screen.findByText('Agent permissions could not be set up for this tag. Try again.')).toBeInTheDocument()
    expect(screen.queryByText('Only the dashboard owner can allow agents to use this tag.')).toBeNull()

    const group = screen.getByRole('radiogroup', { name: 'Agent permissions for Alpha' })
    fireEvent.click(within(group).getByRole('radio', { name: 'Agent: add only' }))
    expect(await screen.findByText('Agent permissions could not be saved. Try again.')).toBeInTheDocument()
    expect(screen.queryByText('This tag is not set up for agents yet.')).toBeNull()
  })

  it('passes a localized session-recovery message through on a policy failure', async () => {
    vi.mocked(api.updateChatTag).mockRejectedValueOnce(
      Object.assign(new Error('Your session expired. Sign in again.'), { status: 403, authRequired: true }),
    )
    renderList({ mode: 'manage' })
    const group = await screen.findByRole('radiogroup', { name: 'Agent permissions for Alpha' })
    fireEvent.click(within(group).getByRole('radio', { name: 'Agent: add only' }))
    expect(await screen.findByText('Your session expired. Sign in again.')).toBeInTheDocument()
  })

  it.each([
    { label: 'a server failure', error: Object.assign(new Error('persist failed'), { status: 500 }), raw: 'persist failed' },
    { label: 'a network failure', error: new TypeError('Failed to fetch'), raw: 'Failed to fetch' },
  ])('replaces $label on a rename with a task-shaped message', async ({ error, raw }) => {
    vi.mocked(api.updateChatTag).mockRejectedValueOnce(error)
    renderList({ mode: 'manage' })
    const rename = await screen.findByTestId('tag-name-t1') as HTMLInputElement
    fireEvent.change(rename, { target: { value: 'Renamed' } })
    fireEvent.blur(rename)
    expect(await screen.findByText('The tag change could not be saved. Try again.')).toBeInTheDocument()
    expect(screen.queryByText(raw)).toBeNull()
  })

  it('scopes a pending policy save to its own row and shows the chosen option at once', async () => {
    vi.mocked(api.chatTags).mockResolvedValueOnce([
      { id: 'a', name: 'Alpha', color: '#ff0000', order: 0, agent: 'none', agent_provenanced: true, agent_store_degraded: false },
      { id: 'b', name: 'Bravo', color: '#00ff00', order: 1, agent: 'none', agent_provenanced: true, agent_store_degraded: false },
      { id: 'c', name: 'Charlie', color: '#0000ff', order: 2, agent: 'none', agent_provenanced: false, agent_store_degraded: false },
    ])
    const save = deferred<{ ok: boolean }>()
    vi.mocked(api.updateChatTag).mockReturnValueOnce(save.promise)
    renderList({ mode: 'manage' })

    const alpha = await screen.findByRole('radiogroup', { name: 'Agent permissions for Alpha' })
    fireEvent.click(within(alpha).getByRole('radio', { name: 'Agent: add only' }))
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledWith('a', { agent: 'add-only' }))

    // Immediate feedback: the choice being saved is selected while in flight.
    expect(within(alpha).getByRole('radio', { name: 'Agent: add only' })).toHaveAttribute('aria-checked', 'true')
    expect(within(alpha).getByRole('radio', { name: 'Human only' })).toHaveAttribute('aria-checked', 'false')
    for (const radio of within(alpha).getAllByRole('radio')) {
      expect(radio).toHaveAttribute('aria-disabled', 'true')
    }
    // Other rows stay usable while Alpha saves.
    const bravo = screen.getByRole('radiogroup', { name: 'Agent permissions for Bravo' })
    for (const radio of within(bravo).getAllByRole('radio')) {
      expect(radio).not.toHaveAttribute('aria-disabled')
    }
    expect(screen.getByRole('button', { name: 'Set up agent permissions' })).not.toBeDisabled()

    await act(async () => { save.reject(new Error('Agent permissions could not be saved.')) })
    // A failed save falls back to the unchanged server value.
    expect(within(alpha).getByRole('radio', { name: 'Human only' })).toHaveAttribute('aria-checked', 'true')
  })

  it('keeps a row locked while its own save is in flight after another row saves', async () => {
    vi.mocked(api.chatTags).mockResolvedValueOnce([
      { id: 'a', name: 'Alpha', color: '#ff0000', order: 0, agent: 'none', agent_provenanced: true, agent_store_degraded: false },
      { id: 'b', name: 'Bravo', color: '#00ff00', order: 1, agent: 'none', agent_provenanced: true, agent_store_degraded: false },
    ])
    const alphaSave = deferred<{ ok: boolean }>()
    const bravoSave = deferred<{ ok: boolean }>()
    vi.mocked(api.updateChatTag).mockReturnValueOnce(alphaSave.promise).mockReturnValueOnce(bravoSave.promise)
    renderList({ mode: 'manage' })

    const alpha = await screen.findByRole('radiogroup', { name: 'Agent permissions for Alpha' })
    fireEvent.click(within(alpha).getByRole('radio', { name: 'Agent: add & remove' }))
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledWith('a', { agent: 'add-remove' }))
    const bravo = screen.getByRole('radiogroup', { name: 'Agent permissions for Bravo' })
    fireEvent.click(within(bravo).getByRole('radio', { name: 'Agent: add only' }))
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledWith('b', { agent: 'add-only' }))

    // Alpha's broader save is still in flight, so Alpha must not re-enable (a
    // narrowing click now could be overwritten when the older save lands).
    for (const radio of within(alpha).getAllByRole('radio')) {
      expect(radio).toHaveAttribute('aria-disabled', 'true')
    }
    expect(within(alpha).getByRole('radio', { name: 'Agent: add & remove' })).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(within(alpha).getByRole('radio', { name: 'Human only' }))
    expect(api.updateChatTag).toHaveBeenCalledTimes(2)

    await act(async () => {
      alphaSave.resolve({ ok: true })
      bravoSave.resolve({ ok: true })
    })
  })

  it('moves focus with arrow keys without saving each option it passes', async () => {
    vi.mocked(api.chatTags).mockResolvedValueOnce([
      { id: 'a', name: 'Alpha', color: '#ff0000', order: 0, agent: 'add-remove', agent_provenanced: true, agent_store_degraded: false },
    ])
    renderList({ mode: 'manage' })
    const group = await screen.findByRole('radiogroup', { name: 'Agent permissions for Alpha' })
    const addRemove = within(group).getByRole('radio', { name: 'Agent: add & remove' })
    const addOnly = within(group).getByRole('radio', { name: 'Agent: add only' })
    const human = within(group).getByRole('radio', { name: 'Human only' })
    addRemove.focus()
    fireEvent.keyDown(addRemove, { key: 'ArrowLeft' })
    expect(addOnly).toHaveFocus()
    fireEvent.keyDown(addOnly, { key: 'ArrowLeft' })
    expect(human).toHaveFocus()
    expect(api.updateChatTag).not.toHaveBeenCalled()
    // Space/Enter is a native button click: only that option is committed.
    fireEvent.click(human)
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledTimes(1))
    expect(api.updateChatTag).toHaveBeenCalledWith('a', { agent: 'none' })
  })

  it('keeps one keyboard tab stop on a fully disabled policy group', async () => {
    vi.mocked(api.chatTags).mockResolvedValueOnce([
      { id: 'ready', name: 'Ready', color: '#00ff00', order: 0, agent: 'add-only', agent_provenanced: true, agent_store_degraded: true },
    ])
    renderList({ mode: 'manage' })
    const group = await screen.findByRole('radiogroup', { name: 'Agent permissions for Ready' })
    const stops = within(group).getAllByRole('radio').filter(radio => radio.tabIndex === 0)
    expect(stops).toHaveLength(1)
    expect(stops[0]).toHaveAccessibleName('Agent: add only')
    expect(stops[0]).toHaveAttribute('aria-describedby', screen.getByTestId('tag-policy-store-error').id)
  })

  it.each([
    { newerOutcome: 'success', newerError: null },
    { newerOutcome: 'failure', newerError: 'Agent permissions could not be set up for this tag. Try again.' },
  ])('shows a policy failure under its own row after another tag\'s adoption $newerOutcome', async ({ newerError }) => {
    const olderPolicy = deferred<{ ok: boolean }>()
    const newerAdoption = deferred<{ ok: boolean }>()
    vi.mocked(api.updateChatTag).mockReturnValueOnce(olderPolicy.promise)
    vi.mocked(api.adoptChatTag).mockReturnValueOnce(newerAdoption.promise)
    renderList({ mode: 'manage' })

    const group = await screen.findByRole('radiogroup', { name: 'Agent permissions for Alpha' })
    fireEvent.click(within(group).getByRole('radio', { name: 'Agent: add only' }))
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledWith('t1', { agent: 'add-only' }))

    fireEvent.click(screen.getByRole('button', { name: 'Set up agent permissions' }))
    await waitFor(() => expect(api.adoptChatTag).toHaveBeenCalledWith('t2', true))
    await act(async () => {
      if (newerError) newerAdoption.reject(new Error(newerError))
      else newerAdoption.resolve({ ok: true })
    })
    if (newerError) expect(await screen.findByText(newerError)).toBeInTheDocument()

    // Tag A's save did not persist; tag B's adoption must not suppress it.
    await act(async () => { olderPolicy.reject(new Error('Alpha policy failed.')) })
    const notice = await screen.findByText('Agent permissions could not be saved. Try again.')
    // Rendered beneath Alpha's row, before Beta's, so the user can tell which save failed.
    const alphaRow = screen.getByTestId('tag-row-t1')
    const betaRow = screen.getByTestId('tag-row-t2')
    expect(alphaRow.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(notice.compareDocumentPosition(betaRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    if (newerError) expect(screen.getByText(newerError)).toBeInTheDocument()
  })

  it('hands focus to the new policy group after a successful adoption', async () => {
    vi.mocked(api.chatTags)
      .mockResolvedValueOnce([
        { id: 't2', name: 'Beta', color: '#00ff00', order: 0, status: true, agent: 'none', agent_provenanced: false, agent_store_degraded: false },
      ])
      .mockResolvedValueOnce([
        { id: 't2', name: 'Beta', color: '#00ff00', order: 0, status: true, agent: 'none', agent_provenanced: true, agent_store_degraded: false },
      ])
    renderList({ mode: 'manage' })
    const adopt = await screen.findByRole('button', { name: 'Set up agent permissions' })
    adopt.focus()
    fireEvent.click(adopt)
    const group = await screen.findByRole('radiogroup', { name: 'Agent permissions for Beta' })
    await waitFor(() => expect(within(group).getByRole('radio', { name: 'Human only' })).toHaveFocus())
  })

  it('offers the agent hand-off on a policy failure but not on a rename failure', async () => {
    vi.mocked(api.updateChatTag)
      .mockRejectedValueOnce(new Error('Alpha policy failed.'))
      .mockRejectedValueOnce(new Error('Alpha rename failed.'))
    renderList({ mode: 'manage' })
    const group = await screen.findByRole('radiogroup', { name: 'Agent permissions for Alpha' })
    fireEvent.click(within(group).getByRole('radio', { name: 'Agent: add only' }))
    const policyNotice = (await screen.findByText('Agent permissions could not be saved. Try again.')).closest('[data-testid="tag-policy-mutation-error"]') as HTMLElement
    expect(within(policyNotice).getByText('Ask the agent')).toBeInTheDocument()

    const rename = screen.getByTestId('tag-name-t1') as HTMLInputElement
    fireEvent.change(rename, { target: { value: 'Renamed' } })
    fireEvent.blur(rename)
    const crudNotice = (await screen.findByText('Alpha rename failed.')).closest('[data-testid="tag-crud-mutation-error"]') as HTMLElement
    expect(within(crudNotice).queryByText('Ask the agent')).toBeNull()
  })

  it('labels the policy group with a visible caption', async () => {
    renderList({ mode: 'manage' })
    const group = await screen.findByRole('radiogroup', { name: 'Agent permissions for Alpha' })
    const caption = group.parentElement?.querySelector('span[aria-hidden]')
    expect(caption).toHaveTextContent('Agent permissions')
  })

  it('explains, rather than promises, a legacy row\'s disabled status flip', async () => {
    renderList({ mode: 'manage' })
    const status = await screen.findByTestId('tag-status-t2')
    expect(status).toBeDisabled()
    expect(status).toHaveAttribute('title', 'Set up agent permissions to change status')
  })
})

describe('TagManagerList — colour palette isolation', () => {
  it('column-filter mode never renders the colour trigger or palette', async () => {
    renderList({ mode: 'column-filter', selectedIds: [], onToggleTag: vi.fn() })
    await screen.findByTestId('tag-row-t1')
    expect(screen.queryByTestId('tag-color-t1')).toBeNull()
    // Clicking the filter checkbox must not open a palette either.
    fireEvent.click(screen.getByLabelText('Include Alpha in filter'))
    expect(screen.queryByTestId('tag-palette-t1')).toBeNull()
    expect(api.updateChatTag).not.toHaveBeenCalled()
  })
})

describe('TagManagerList — column-filter mode', () => {
  it('swatches are include/exclude checkboxes reflecting selectedIds', async () => {
    renderList({ mode: 'column-filter', selectedIds: ['t2'], onToggleTag: vi.fn() })
    await screen.findByTestId('tag-row-t1')
    // t2 is selected → aria-checked; t1 is not.
    expect(screen.getByLabelText('Include Beta in filter')).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByLabelText('Include Alpha in filter')).toHaveAttribute('aria-checked', 'false')
  })

  it('toggling a swatch calls onToggleTag with the composed next id list', async () => {
    const onToggleTag = vi.fn()
    renderList({ mode: 'column-filter', selectedIds: ['t2'], onToggleTag })
    fireEvent.click(await screen.findByLabelText('Include Alpha in filter')) // add t1
    expect(onToggleTag).toHaveBeenCalledWith('t1', ['t2', 't1'])
    fireEvent.click(screen.getByLabelText('Include Beta in filter')) // remove t2
    expect(onToggleTag).toHaveBeenCalledWith('t2', [])
  })

  it('uses the provided createTestId for the new-tag input', async () => {
    renderList({ mode: 'column-filter', selectedIds: [], onToggleTag: vi.fn(), createTestId: 'tag-create-col-9' })
    expect(await screen.findByTestId('tag-create-col-9')).toBeInTheDocument()
  })
})

describe('SessionActionsMenu — Tags… item (list-view regression)', () => {
  it('renders the "Tags…" item even when tagColumnsEnabled is false', async () => {
    const store = createTestStore({
      dashboard: {
        status: {}, connected: true, approvalMode: 'normal', channelTrusted: false,
        refreshTrigger: 0, unreadSlots: [], slotsLoaded: true, updateProgress: null,
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
        sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
        slots: [{ key: 'chat-1', title: 'S1' }],
      } as unknown as RootState['dashboard'],
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter>
              <TagPopoverProvider>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild><button aria-label="open">open</button></DropdownMenuTrigger>
                  <DropdownMenuContent><SessionActionsMenu variant="dropdown" slotKey="chat-1" /></DropdownMenuContent>
                </DropdownMenu>
              </TagPopoverProvider>
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
    fireEvent.keyDown(screen.getByLabelText('open'), { key: 'Enter' })
    expect(await screen.findByText('Tags…')).toBeInTheDocument()
  })
})
