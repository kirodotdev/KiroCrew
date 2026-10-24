/**
 * CoAuthorPanel — the embedded Papyrus co-author chat.
 *
 * The panel's own logic is the body (mounted chat / error / creating spinner /
 * empty invitation), the header buttons, and the switchSlot effect that makes the
 * paper's session the ACTIVE slot: the embedded ChatPage renders whatever slot is
 * active, so the panel must never show the chat the store falls back to when that
 * switch fails, and must follow the embedded chat when it replaces the session.
 * ChatPage is stubbed: mounting the real one would pull the whole chat tree into
 * a test about this panel.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../../test/helpers'
import { addSlotOptimistic, removeSlotOptimistic } from '../../store/dashboardSlice'
import type { ChatSlot } from '../../types'
import { i18nT } from '../../i18n/t'
import CoAuthorPanel, { type CoAuthorPanelProps } from './CoAuthorPanel'

vi.mock('../../pages/ChatPage', () => ({
  default: (props: Record<string, unknown>) => (
    <div data-testid="zzq-chatpage">{`zzq:${props.embedMode}:${String(props.noUrlSync)}`}</div>
  ),
}))

// Mirrors the real thunk: a live session becomes the active slot; a deleted one
// rejects with the 404 payload and leaves the previous active slot in place; a
// gateway failure rejects with its status.
const goneSlots = vi.hoisted(() => new Set<string>())
const failingSlots = vi.hoisted(() => new Set<string>())
const switchSlot = vi.hoisted(() => vi.fn((key: string) => (dispatch: (a: unknown) => void) => {
  if (goneSlots.has(key)) return { unwrap: () => Promise.reject({ status: 404, message: 'not found' }) }
  if (failingSlots.has(key)) return { unwrap: () => Promise.reject({ status: 503, message: 'gateway unavailable' }) }
  dispatch({ type: 'chat/setActiveSlot', payload: key })
  return { unwrap: () => Promise.resolve() }
}))
vi.mock('../../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  switchSlot,
}))

function setup(props: Partial<CoAuthorPanelProps> = {}) {
  const handlers = {
    onStartSession: vi.fn(),
    onSessionGone: vi.fn(),
    onSessionReplaced: vi.fn(),
    onOpenFull: vi.fn(),
    onClose: vi.fn(),
  }
  const store = createTestStore()
  const utils = renderWithProviders(
    <CoAuthorPanel slotKey={null} creating={false} {...handlers} {...props} />,
    { store },
  )
  return { ...utils, ...handlers, store }
}

beforeEach(() => {
  switchSlot.mockClear()
  goneSlots.clear()
  failingSlots.clear()
})

const slot = (key: string) => ({ key, title: key, messages: 0, running: false }) as ChatSlot

describe('CoAuthorPanel body', () => {
  it('mounts the embedded chat page for a project with a session', async () => {
    setup({ slotKey: 'zzq-slot-1' })
    expect((await screen.findByTestId('zzq-chatpage')).textContent).toBe('zzq:chat:true')
  })

  it('shows a progress status while a session is being created', () => {
    setup({ creating: true })
    expect(screen.getByRole('status').textContent).toContain(
      i18nT('apps.papyrus.coAuthor.starting_session'),
    )
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
  })

  it('invites the user to start a session when none exists', () => {
    const { onStartSession } = setup()
    expect(screen.getByTestId('papyrus-co-author').textContent).toContain(
      i18nT('apps.papyrus.coAuthor.no_co_author_session_for_this_paper_yet'),
    )
    fireEvent.click(
      screen.getByRole('button', { name: i18nT('apps.papyrus.coAuthor.start_a_session') }),
    )
    expect(onStartSession).toHaveBeenCalledTimes(1)
  })

  it('prefers the mounted chat over the creating spinner when both apply', async () => {
    setup({ slotKey: 'zzq-slot-1', creating: true })
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})

describe('CoAuthorPanel header', () => {
  it('offers the open-in-chat-page shortcut only with a session', () => {
    const { onOpenFull } = setup({ slotKey: 'zzq-slot-1' })
    fireEvent.click(
      screen.getByRole('button', { name: i18nT('apps.papyrus.coAuthor.open_in_chat_page') }),
    )
    expect(onOpenFull).toHaveBeenCalledTimes(1)
  })

  it('hides that shortcut when there is no session', () => {
    setup()
    expect(
      screen.queryByRole('button', { name: i18nT('apps.papyrus.coAuthor.open_in_chat_page') }),
    ).not.toBeInTheDocument()
  })

  it('closes the panel', () => {
    const { onClose } = setup({ slotKey: 'zzq-slot-1' })
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('apps.papyrus.coAuthor.close_co_author_panel'),
      }),
    )
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('CoAuthorPanel slot activation', () => {
  it('shows only the paper session, follows its replacement, and reports a deleted or failing one', async () => {
    const { rerender, store, onSessionGone, onSessionReplaced } = setup()
    const panel = (slotKey: string | null) => (
      <CoAuthorPanel
        slotKey={slotKey}
        creating={false}
        onStartSession={() => {}}
        onSessionGone={onSessionGone}
        onSessionReplaced={onSessionReplaced}
        onOpenFull={() => {}}
        onClose={() => {}}
      />
    )
    act(() => {
      store.dispatch(addSlotOptimistic(slot('zzq-slot-1')))
      store.dispatch(addSlotOptimistic(slot('zzq-other')))
    })
    // No session yet: nothing is activated.
    expect(switchSlot).not.toHaveBeenCalled()

    // Opening a paper with a session activates it once and shows the chat;
    // an unrelated re-render does not switch again.
    rerender(panel('zzq-slot-1'))
    rerender(panel('zzq-slot-1'))
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(switchSlot).toHaveBeenCalledTimes(1)

    // The embedded chat replaces the session (a memory-mode pick: create, make
    // active, delete the old one). The chat stays mounted, is not switched back,
    // and the new session is handed to the page.
    act(() => {
      store.dispatch(addSlotOptimistic(slot('zzq-slot-2')))
      store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-slot-2' })
    })
    act(() => { store.dispatch(removeSlotOptimistic('zzq-slot-1')) })
    expect(onSessionReplaced).toHaveBeenCalledWith('zzq-slot-1', 'zzq-slot-2')
    rerender(panel('zzq-slot-2'))
    expect(screen.getByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(switchSlot).toHaveBeenCalledTimes(1)

    // The paper's session is deleted and the store falls back to a session that
    // already existed: that chat is never adopted, the session is reported gone.
    act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-other' }) })
    act(() => { store.dispatch(removeSlotOptimistic('zzq-slot-2')) })
    expect(onSessionGone).toHaveBeenCalledWith('zzq-slot-2')
    expect(onSessionReplaced).toHaveBeenCalledTimes(1)

    // Another paper with a deleted session: the switch 404s and the store keeps
    // the previous chat active. The panel never shows that chat and reports the
    // dead key once, without retrying it.
    goneSlots.add('zzq-gone')
    rerender(panel('zzq-gone'))
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    await waitFor(() => expect(onSessionGone).toHaveBeenCalledWith('zzq-gone'))
    expect(switchSlot.mock.calls.filter(([key]) => key === 'zzq-gone')).toHaveLength(1)

    // A session deleted while the panel was closed is still the active slot but
    // no longer listed: the panel does not trust it, asks the gateway, and on
    // the 404 reports it gone instead of showing a chat.
    goneSlots.add('zzq-stale')
    act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-stale' }) })
    rerender(panel('zzq-stale'))
    await waitFor(() => expect(onSessionGone).toHaveBeenCalledWith('zzq-stale'))
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()

    // A gateway failure is shown in the panel, once, and not retried in a loop;
    // "Try again" retries it, and the chat appears once the gateway is back.
    failingSlots.add('zzq-down')
    rerender(panel('zzq-down'))
    expect(await screen.findByText('gateway unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    expect(switchSlot.mock.calls.filter(([key]) => key === 'zzq-down')).toHaveLength(1)
    failingSlots.delete('zzq-down')
    act(() => { store.dispatch(addSlotOptimistic(slot('zzq-down'))) })
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.webPreviewPanel.try_again') }))
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(screen.queryByText('gateway unavailable')).not.toBeInTheDocument()
    expect(switchSlot.mock.calls.filter(([key]) => key === 'zzq-down')).toHaveLength(2)
  })
})
