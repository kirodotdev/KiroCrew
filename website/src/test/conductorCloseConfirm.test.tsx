/**
 * Closing a conductor that still has open workers asks once (#31629).
 *
 * Since #30877 the person closing a conductor (an Ultra Mode root or a Bot
 * thread) closes the open sessions in its tree too, server-side. One click used
 * to end several workers mid-turn with no prompt. Pinned here:
 * - the prompt names the number of workers the server's own close selection
 *   answers (`GET .../close-preview`), on every close path, whatever the
 *   "Confirm Before Closing Session" setting says; when that read fails it still
 *   asks, without a number;
 * - Close all runs the one close #30877 already cascades; Cancel closes nothing;
 * - a conductor whose close takes no workers closes as before, with no prompt,
 *   and one with no stamped session is not even asked about; a worker that
 *   already closed (now a History row) is not stamped.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const chatConfig = vi.hoisted(() => ({ confirmCloseSession: false }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => chatConfig }))

// Thunk-shaped like the real `deleteSlot`, so `close` can read its outcome.
const deleteSlot = vi.hoisted(() => vi.fn((key: string) => async () => ({ type: 'cc31629/deleteSlot/fulfilled', payload: key })))
vi.mock('../store/chatSlice', async (importOriginal) => {
  const real = await importOriginal<Record<string, unknown> & { deleteSlot: { fulfilled: unknown; rejected: unknown } }>()
  return {
    ...real,
    deleteSlot: Object.assign(deleteSlot, { fulfilled: real.deleteSlot.fulfilled, rejected: real.deleteSlot.rejected }),
  }
})

import AppConfirmHost from '../components/AppConfirmHost'
import { api } from '../api/client'
import { store } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import { openWorkerCount } from '../lib/closeBusyGate'
import { closeConfirmPrompt, workersLine } from '../lib/closeConfirmPrompt'
import { useSessionActions } from '../hooks/useSessionActions'
import { confirmText, stubAppConfirm } from './appConfirmStub'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

const COND = 'cc31629-cond'

const row = (key: string, patch: Partial<ChatSlot> = {}) => ({ key, messages: 1, running: false, ...patch }) as ChatSlot

/** An Ultra Mode conductor, two workers it dispatched (one nested under the
 *  other, both stamped with the root), and an unrelated chat. */
const CREW: ChatSlot[] = [
  row(COND, { crew_mode_root: true }),
  row('cc31629-w1', { crew_root: COND, running: true }),
  row('cc31629-w1-sub', { crew_root: COND }),
  row('cc31629-other'),
]

function stateWith(slots: ChatSlot[], chat: Record<string, unknown> = {}): RootState {
  return createTestStore({
    dashboard: { slots } as unknown as RootState['dashboard'],
    chat: { activeSlot: COND, slotHistory: [], ...chat } as unknown as RootState['chat'],
  }).getState()
}

describe('openWorkerCount', () => {
  it('counts every open session stamped with the conductor, at any depth', () => {
    expect(openWorkerCount(stateWith(CREW), COND)).toBe(2)
  })

  it('counts a Bot thread\'s sessions by their mate_root stamp', () => {
    const slots = [row(COND, { mode: 'member' }), row('m1', { mate_root: COND }), row('m2', { mate_root: COND })]
    expect(openWorkerCount(stateWith(slots), COND)).toBe(2)
  })

  it('does not count a worker that already closed: it is a History row, not a live slot', () => {
    const history = [{ key: 'cc31629-gone', title: 'Done worker', crew_root: COND }]
    const slots = CREW.filter(s => s.key !== 'cc31629-w1-sub')
    expect(openWorkerCount(stateWith(slots, { history }), COND)).toBe(1)
    expect(openWorkerCount(stateWith([row(COND, { crew_mode_root: true })], { history }), COND)).toBe(0)
  })

  it('is zero for a plain chat, for a worker, and for another conductor\'s workers', () => {
    const state = stateWith([...CREW, row('cond-2', { crew_mode_root: true }), row('w-of-2', { crew_root: 'cond-2' })])
    expect(openWorkerCount(state, 'cc31629-other')).toBe(0)
    expect(openWorkerCount(state, 'cc31629-w1')).toBe(0)
    expect(openWorkerCount(state, 'cond-2')).toBe(1)
  })
})

describe('closeConfirmPrompt for a conductor', () => {
  it('asks with the worker count and a Close all button, even with the setting off', () => {
    const opts = closeConfirmPrompt(stateWith(CREW), COND, { settingOn: false }, 2)
    expect(opts?.title).toBe('Close this session and its 2 workers?')
    expect(opts?.confirmLabel).toBe('Close all')
    expect(confirmText(opts!)).toContain('Every open worker it started closes with it')
  })

  it('uses the singular for one worker', () => {
    const opts = closeConfirmPrompt(stateWith(CREW.slice(0, 2)), COND, { settingOn: false }, 1)
    expect(opts?.title).toBe('Close this session and its 1 worker?')
  })

  it('still asks on the ⌥⇧W alias, which otherwise skips the busy check', () => {
    expect(closeConfirmPrompt(stateWith(CREW), COND, { settingOn: false, checkBusy: false }, 2)?.title)
      .toBe('Close this session and its 2 workers?')
  })

  it('names live work in the conductor itself, without the setting line', () => {
    const slots = [row(COND, { crew_mode_root: true, running: true }), ...CREW.slice(1)]
    const text = confirmText(closeConfirmPrompt(stateWith(slots), COND, { settingOn: true }, 2)!)
    expect(text).toContain('It is still working on a reply.')
    expect(text).not.toContain('Settings')
  })

  it('asks nothing when the server says the close takes no workers and the setting is off', () => {
    expect(closeConfirmPrompt(stateWith(CREW), COND, { settingOn: false }, 0)).toBeNull()
  })

  it('asks without a number when the server count could not be read', () => {
    const opts = closeConfirmPrompt(stateWith(CREW), COND, { settingOn: false }, null)
    expect(opts?.title).toBe('Close this session and its open workers?')
    expect(opts?.confirmLabel).toBe('Close all')
  })

  it('leaves a worker\'s own close on the ordinary prompt', () => {
    expect(closeConfirmPrompt(stateWith(CREW), 'cc31629-w1', { settingOn: false })?.title).toBe('Close this session?')
  })

  it('words the folder-cleanup row line with the count', () => {
    expect(workersLine(2)).toBe('It has 2 open workers, which close with it.')
    expect(workersLine(1)).toBe('It has 1 open worker, which closes with it.')
    expect(workersLine(null)).toBe('It has open workers, which close with it.')
  })
})

describe('closing a conductor through the session close path', () => {
  function harness() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>
        <Provider store={store}>{children}</Provider>
      </QueryClientProvider>
    )
    return renderHook(() => useSessionActions(), { wrapper })
  }

  const preview = () => vi.spyOn(api, 'chatSlotClosePreview')

  beforeEach(() => {
    vi.restoreAllMocks()
    deleteSlot.mockClear()
    chatConfig.confirmCloseSession = false
    store.dispatch(sseSlots([]))
  })

  it('shows the server\'s worker count; Close all sends the one close #30877 cascades', async () => {
    // Two rows are stamped, but the server's close takes one (the other is an
    // adopted tab it does not manage): the prompt says what the close does.
    const read = preview().mockResolvedValue({ workers: 1 })
    store.dispatch(sseSlots(CREW))
    render(<AppConfirmHost />)
    const { result } = harness()
    act(() => result.current.close(COND))
    const dialog = await screen.findByRole('dialog', { name: 'Close this session and its 1 worker?' })
    expect(read).toHaveBeenCalledWith(COND)
    expect(deleteSlot).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close all' }))
    await waitFor(() => expect(deleteSlot).toHaveBeenCalledTimes(1))
    // The server closes the workers with it; the client still sends one close.
    expect(deleteSlot).toHaveBeenCalledWith(COND)
  })

  it('Cancel closes nothing', async () => {
    preview().mockResolvedValue({ workers: 2 })
    store.dispatch(sseSlots(CREW))
    render(<AppConfirmHost />)
    const { result } = harness()
    act(() => result.current.close(COND))
    const dialog = await screen.findByRole('dialog', { name: 'Close this session and its 2 workers?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(deleteSlot).not.toHaveBeenCalled()
  })

  it('asks without a number when the preview cannot be read, rather than skipping the confirm', async () => {
    preview().mockRejectedValue(new Error('offline'))
    store.dispatch(sseSlots(CREW))
    render(<AppConfirmHost />)
    const { result } = harness()
    act(() => result.current.close(COND))
    const dialog = await screen.findByRole('dialog', { name: 'Close this session and its open workers?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(deleteSlot).not.toHaveBeenCalled()
  })

  it('closes with no prompt when the server says the close takes no workers', async () => {
    preview().mockResolvedValue({ workers: 0 })
    store.dispatch(sseSlots(CREW))
    const ask = stubAppConfirm(true)
    const { result } = harness()
    act(() => result.current.close(COND))
    await waitFor(() => expect(deleteSlot).toHaveBeenCalledWith(COND))
    expect(ask).not.toHaveBeenCalled()
  })

  it('a conductor with no stamped sessions closes with no prompt and no preview read', () => {
    const read = preview()
    store.dispatch(sseSlots([row(COND, { crew_mode_root: true }), row('cc31629-other')]))
    const ask = stubAppConfirm(true)
    const { result } = harness()
    act(() => result.current.close(COND))
    expect(ask).not.toHaveBeenCalled()
    expect(read).not.toHaveBeenCalled()
    expect(deleteSlot).toHaveBeenCalledWith(COND)
  })
})
