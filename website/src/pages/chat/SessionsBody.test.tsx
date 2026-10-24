import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, within } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../../test/helpers'

const membersMock = vi.hoisted(() => vi.fn(async () => ({ members: [] })))
const deleteChatSlotMock = vi.hoisted(() => vi.fn(async () => ({})))
const deleteSessionMock = vi.hoisted(() => vi.fn(async () => ({})))
const spawnDeleteMock = vi.hoisted(() => vi.fn(async () => ({})))

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      ...(actual as { api: object }).api,
      members: membersMock,
      deleteChatSlot: deleteChatSlotMock,
      deleteSession: deleteSessionMock,
      spawnDelete: spawnDeleteMock,
    },
  }
})

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SessionsBody from './SessionsBody'
const slot = (over: Record<string, unknown>) => ({
  key: 'chat-a',
  title: 'Alpha',
  messages: 3,
  running: false,
  ...over,
})


function renderBody(opts: { slots?: object[]; subs?: object; activity?: object; active?: string }) {
  const store = createTestStore({
    dashboard: { slots: (opts.slots ?? []), unreadSlots: [] } as never,
    chat: {
      activeSlot: (opts.active ?? null) as never,
      subagents: (opts.subs ?? {}) as never,
      slotActivity: (opts.activity ?? {}) as never,
    } as never,
  })
  return renderWithProviders(<SessionsBody active />, { store })
}

beforeEach(() => {
  membersMock.mockResolvedValue({ members: [{ name: 'Nova', slug: 'nova', slot_key: 'member-nova' }] })
  deleteChatSlotMock.mockResolvedValue({})
  deleteSessionMock.mockClear()
  spawnDeleteMock.mockResolvedValue({})
})

async function confirmDialog(label: string) {
  const dialog = await screen.findByRole('dialog')
  fireEvent.click(within(dialog).getByText(label))
}

describe('SessionsBody', () => {
  it('lists live slots with model, template and Active status', () => {
    renderBody({
      slots: [slot({ model: 'opus', agent: 'crew', effective_agent: 'crew:dev' })],
    })
    const row = screen.getByText('Alpha').closest('li') as HTMLElement
    expect(within(row).getByText('Active')).toBeTruthy()
    expect(within(row).getByText('opus')).toBeTruthy()
    expect(within(row).getByText('crew:dev')).toBeTruthy()
  })
  it('leaves member DM threads out: their Open would miss the chat page', () => {
    renderBody({
      slots: [
        slot({ key: 'chat-a', title: 'Alpha' }),
        slot({ key: 'member-nova', title: 'Nova DM', mode: 'member', agent: 'nova' }),
      ],
    })
    expect(screen.getByText('Alpha')).toBeTruthy()
    expect(screen.queryByText('Nova DM')).toBeNull()
  })

  it('marks approval-waiting and questioning slots Blocked', () => {
    renderBody({
      slots: [
        slot({ key: 'chat-a', title: 'Waiting', pending_approval: true }),
        slot({ key: 'chat-b', title: 'Asked', needs_input: true }),
      ],
    })
    expect(within(screen.getByRole('list')).getAllByText('Blocked')).toHaveLength(2)
  })

  it('reads error runs as Blocked and queued runs as Active, like the fleet board', () => {
    renderBody({
      slots: [slot({})],
      active: 'chat-a',
      subs: {
        r1: { id: 'r1', task: 'Failed job', agent: 'sage', status: 'error' },
        r2: { id: 'r2', task: 'Queued job', agent: 'sage', status: 'pending' },
      },
    })
    const failed = screen.getByText('Failed job').closest('li') as HTMLElement
    expect(within(failed).getByText('Blocked')).toBeTruthy()
    const queued = screen.getByText('Queued job').closest('li') as HTMLElement
    expect(within(queued).getByText('Active')).toBeTruthy()
  })

  it('resolves the driver from a member-created slot', async () => {
    renderBody({
      slots: [slot({ created_by: 'member-nova' })],
    })
    expect(await screen.findByText('Nova')).toBeTruthy()
  })

  it('falls back to the slug when the roster has no name', async () => {
    membersMock.mockResolvedValue({ members: [] })
    renderBody({
      slots: [slot({ created_by: 'dashboard:member-stranger' })],
    })
    expect(await screen.findByText('stranger')).toBeTruthy()
  })

  it('surfaces a roster failure instead of silently falling back', async () => {
    membersMock.mockRejectedValueOnce(new Error('roster down'))
    renderBody({
      slots: [slot({ created_by: 'member-nova' })],
    })
    expect(await screen.findByText('roster down')).toBeTruthy()
    // The slug still reads as the driver behind the notice.
    expect(screen.getByText('nova')).toBeTruthy()
  })

  it('files the active slot runs under the active slot, not the first row', () => {
    renderBody({
      slots: [
        slot({ key: 'chat-a', title: 'First', created_by: 'member-nova' }),
        slot({ key: 'chat-b', title: 'Second', created_by: undefined }),
      ],
      active: 'chat-b',
      subs: {
        r1: { id: 'r1', task: 'Probe', agent: 'sage', model: 'haiku', status: 'running' },
      },
    })
    const running = screen.getByText('Probe').closest('li') as HTMLElement
    expect(within(running).getByText('Active')).toBeTruthy()
    expect(within(running).getByText('haiku')).toBeTruthy()
    // The run belongs to the second slot, which has no driver: no stray name.
    expect(within(running).queryByText('Nova')).toBeNull()
    // Runs close without a jump target: only slots navigate.
    expect(within(running).queryByText('Open')).toBeNull()
  })

  it('merges background buckets onto their own slot', () => {
    renderBody({
      slots: [slot({ key: 'chat-a', title: 'First' }), slot({ key: 'chat-b', title: 'Second' })],
      active: 'chat-b',
      activity: {
        'chat-a': { subagents: { r9: { id: 'r9', task: 'Old job', agent: 'sage', status: 'done' } } },
      },
    })
    const done = screen.getByText('Old job').closest('li') as HTMLElement
    expect(within(done).getByText('Complete')).toBeTruthy()
  })

  it('filters rows by status chip', () => {
    renderBody({
      slots: [
        slot({ key: 'chat-a', title: 'Alpha', running: true }),
        slot({ key: 'chat-b', title: 'Beta', pending_approval: true }),
      ],
    })
    fireEvent.click(screen.getByRole('button', { name: 'Blocked' }))
    expect(screen.queryByText('Alpha')).toBeNull()
    expect(screen.getByText('Beta')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'All' }))
    expect(screen.getByText('Alpha')).toBeTruthy()
  })

  it('offers to clear the filter when it matches nothing', () => {
    renderBody({
      slots: [slot({ running: true })],
    })
    fireEvent.click(screen.getByRole('button', { name: 'Complete' }))
    expect(screen.queryByText('Start a chat or a subagent to see it here.')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /clear/i }))
    expect(screen.getByText('Alpha')).toBeTruthy()
  })

  it('renders the empty state with no sessions', () => {
    renderBody({})
    expect(screen.getByText('No sessions')).toBeTruthy()
  })

  it('closes a slot through the sidebar close, never the history delete', async () => {
    renderBody({
      slots: [slot({})],
    })
    const row = screen.getByText('Alpha').closest('li') as HTMLElement
    fireEvent.click(within(row).getByText('Close'))
    expect(await screen.findByText(/transcript stays in history/)).toBeTruthy()
    await confirmDialog('Close')
    await screen.findByText('No sessions')
    expect(deleteChatSlotMock).toHaveBeenCalledWith('chat-a')
    expect(deleteSessionMock).not.toHaveBeenCalled()
  })

  it('confirms before stopping a run', async () => {
    renderBody({
      slots: [slot({})],
      active: 'chat-a',
      subs: {
        r1: { id: 'r1', task: 'Probe', agent: 'sage', status: 'running' },
      },
    })
    const row = screen.getByText('Probe').closest('li') as HTMLElement
    fireEvent.click(within(row).getByText('Stop run'))
    expect(await screen.findByText(/cannot be resumed/)).toBeTruthy()
    await confirmDialog('Stop run')
    expect(spawnDeleteMock).toHaveBeenCalledWith('r1')
  })

  it('falls back to a message when a refusal carries no text', async () => {
    spawnDeleteMock.mockRejectedValueOnce({ key: 'chat-a', title: 'Probe', code: 'refused' })
    renderBody({
      slots: [slot({})],
      active: 'chat-a',
      subs: {
        r1: { id: 'r1', task: 'Probe', agent: 'sage', status: 'running' },
      },
    })
    const row = screen.getByText('Probe').closest('li') as HTMLElement
    fireEvent.click(within(row).getByText('Stop run'))
    await confirmDialog('Stop run')
    expect(await screen.findByText("Couldn't close the session.")).toBeTruthy()
  })
})
