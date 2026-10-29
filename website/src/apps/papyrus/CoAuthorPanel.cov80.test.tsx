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
import { useState } from 'react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../../test/helpers'
import { addSlotOptimistic, confirmCloseHold, releaseCloseHold, removeSlotOptimistic, sseSlotsPartial } from '../../store/dashboardSlice'
import type { ChatSlot } from '../../types'
import { i18nT } from '../../i18n/t'
import { clearSlotSuccession, recordSlotSuccession } from '../../utils/slotSuccession'
import CoAuthorPanel, { type CoAuthorPanelProps } from './CoAuthorPanel'

// Counts mounts, so a test can tell a chat that stayed mounted from a remount.
const chatMounts = vi.hoisted(() => ({ count: 0 }))
vi.mock('../../pages/ChatPage', async () => {
  const { useEffect } = await import('react')
  return {
    default: function ChatPageStub(props: Record<string, unknown>) {
      useEffect(() => { chatMounts.count += 1 }, [])
      return <div data-testid="zzq-chatpage">{`zzq:${props.embedMode}:${String(props.noUrlSync)}`}</div>
    },
  }
})

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
  chatMounts.count = 0
  clearSlotSuccession()
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

// Stands in for PapyrusPage: holds the paper's slot key and handles the panel's
// reports the way the page does.
let setHostKey: (key: string | null) => void = () => {}
function Host({ onSessionGone, onSessionReplaced }: {
  onSessionGone: (key: string) => void
  onSessionReplaced: (oldKey: string, newKey: string) => void
}) {
  const [slotKey, setSlotKey] = useState<string | null>(null)
  setHostKey = setSlotKey
  return (
    <CoAuthorPanel
      slotKey={slotKey}
      creating={false}
      onStartSession={() => {}}
      onSessionGone={gone => { onSessionGone(gone); setSlotKey(k => (k === gone ? null : k)) }}
      onSessionReplaced={(old, next) => { onSessionReplaced(old, next); setSlotKey(k => (k === old ? next : k)) }}
      onOpenFull={() => {}}
      onClose={() => {}}
    />
  )
}

describe('CoAuthorPanel slot activation', () => {
  it('shows only the paper session, follows its replacement, and reports a deleted or failing one', async () => {
    const onSessionGone = vi.fn()
    const onSessionReplaced = vi.fn()
    const store = createTestStore()
    renderWithProviders(
      <Host onSessionGone={onSessionGone} onSessionReplaced={onSessionReplaced} />,
      { store },
    )
    const active = () => store.getState().chat.activeSlot
    const switchesTo = (key: string) => switchSlot.mock.calls.filter(([k]) => k === key).length
    act(() => {
      store.dispatch(addSlotOptimistic(slot('zzq-slot-1')))
      store.dispatch(addSlotOptimistic(slot('zzq-other')))
    })
    // No session yet: nothing is activated.
    expect(switchSlot).not.toHaveBeenCalled()

    // Opening a paper with a session activates it once and shows the chat;
    // an unrelated re-render does not switch again.
    act(() => setHostKey('zzq-slot-1'))
    act(() => setHostKey('zzq-slot-1'))
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(switchesTo('zzq-slot-1')).toBe(1)

    // A transcript link makes another live session active. Its chat is never
    // shown: the panel makes the paper's session active again.
    act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-other' }) })
    await waitFor(() => expect(active()).toBe('zzq-slot-1'))
    expect(switchesTo('zzq-slot-1')).toBe(2)
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(onSessionGone).not.toHaveBeenCalled()

    // A memory-mode pick records the hand-over, makes the new session active,
    // then deletes the old one. The chat stays mounted, is not switched back,
    // and the page adopts the new session.
    const mounts = chatMounts.count
    act(() => {
      store.dispatch(addSlotOptimistic(slot('zzq-slot-2')))
      recordSlotSuccession('zzq-slot-1', 'zzq-slot-2')
      store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-slot-2' })
    })
    expect(screen.getByTestId('zzq-chatpage')).toBeInTheDocument()
    act(() => { store.dispatch(removeSlotOptimistic('zzq-slot-1')) })
    expect(onSessionReplaced).toHaveBeenCalledWith('zzq-slot-1', 'zzq-slot-2')
    expect(screen.getByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(chatMounts.count).toBe(mounts)
    expect(switchesTo('zzq-slot-1')).toBe(2)
    expect(active()).toBe('zzq-slot-2')

    // The paper's session is deleted elsewhere and the store falls back to a
    // session made after the panel opened (another tab, a sidebar click).
    // Without a recorded hand-over it is never adopted: the panel asks the
    // gateway, and on its 404 the session is reported gone.
    goneSlots.add('zzq-slot-2')
    act(() => {
      store.dispatch(addSlotOptimistic(slot('zzq-late')))
      store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-late' })
      store.dispatch(removeSlotOptimistic('zzq-slot-2'))
    })
    await waitFor(() => expect(onSessionGone).toHaveBeenCalledWith('zzq-slot-2'))
    expect(onSessionReplaced).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    expect(screen.getByTestId('papyrus-co-author').textContent).toContain(
      i18nT('apps.papyrus.coAuthor.no_co_author_session_for_this_paper_yet'),
    )

    // Another paper with a deleted session: the switch 404s and the store keeps
    // the previous chat active. The panel never shows that chat and reports the
    // dead key once, without retrying it.
    goneSlots.add('zzq-gone')
    act(() => setHostKey('zzq-gone'))
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    await waitFor(() => expect(onSessionGone).toHaveBeenCalledWith('zzq-gone'))
    expect(switchesTo('zzq-gone')).toBe(1)

    // A session deleted while the panel was closed is still the active slot but
    // no longer listed: the panel does not trust it, asks the gateway, and on
    // the 404 reports it gone instead of showing a chat.
    goneSlots.add('zzq-stale')
    act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-stale' }) })
    act(() => setHostKey('zzq-stale'))
    await waitFor(() => expect(onSessionGone).toHaveBeenCalledWith('zzq-stale'))
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()

    // A gateway failure is shown in the panel, once, and not retried in a loop;
    // "Try again" retries it, and the chat appears once the gateway is back.
    failingSlots.add('zzq-down')
    act(() => setHostKey('zzq-down'))
    expect(await screen.findByText('gateway unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    expect(switchesTo('zzq-down')).toBe(1)
    // The failed switch can leave the session active and listed: "Try again"
    // still asks the gateway, and while it fails the error stays, with no chat.
    const tryAgain = () => fireEvent.click(screen.getByRole('button', { name: i18nT('components.webPreviewPanel.try_again') }))
    act(() => {
      store.dispatch(addSlotOptimistic(slot('zzq-down')))
      store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-down' })
    })
    tryAgain()
    await waitFor(() => expect(switchesTo('zzq-down')).toBe(2))
    expect(await screen.findByText('gateway unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    failingSlots.delete('zzq-down')
    tryAgain()
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(screen.queryByText('gateway unavailable')).not.toBeInTheDocument()
    expect(switchesTo('zzq-down')).toBe(3)

    // A restarting gateway sends a partial slot list that leaves the paper's
    // session out: it is not reported gone. When the complete list still leaves
    // it out, the panel asks the gateway, which still has it, so the paper
    // keeps its session.
    act(() => {
      store.dispatch(sseSlotsPartial(true))
      store.dispatch(removeSlotOptimistic('zzq-down'))
    })
    expect(onSessionGone).not.toHaveBeenCalledWith('zzq-down')
    expect(switchesTo('zzq-down')).toBe(3)
    act(() => { store.dispatch(sseSlotsPartial(false)) })
    await waitFor(() => expect(switchesTo('zzq-down')).toBe(4))
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(onSessionGone).not.toHaveBeenCalledWith('zzq-down')
    act(() => { store.dispatch(addSlotOptimistic(slot('zzq-down'))) })

    // Ctrl+W on the paper's session: the row leaves the list and another chat
    // becomes active while the DELETE is in flight. The session is not reported
    // gone, and that other chat is not shown.
    const closeStarts = (requestId: string) => {
      store.dispatch({ type: 'chat/deleteSlot/pending', meta: { arg: 'zzq-down', requestId } })
      store.dispatch(removeSlotOptimistic('zzq-down'))
      store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-other' })
    }
    act(() => closeStarts('zzq-close-1'))
    expect(onSessionGone).not.toHaveBeenCalledWith('zzq-down')
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    // The DELETE fails: the hold is dropped before the slot list shows the
    // session again. Still not reported gone; once listed it is made active.
    act(() => { store.dispatch(releaseCloseHold({ key: 'zzq-down', requestId: 'zzq-close-1' })) })
    expect(onSessionGone).not.toHaveBeenCalledWith('zzq-down')
    act(() => { store.dispatch(addSlotOptimistic(slot('zzq-down'))) })
    await waitFor(() => expect(active()).toBe('zzq-down'))
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(onSessionGone).not.toHaveBeenCalledWith('zzq-down')
    // A second close succeeds: only then is the session reported gone.
    act(() => closeStarts('zzq-close-2'))
    expect(onSessionGone).not.toHaveBeenCalledWith('zzq-down')
    act(() => { store.dispatch(confirmCloseHold({ key: 'zzq-down', requestId: 'zzq-close-2' })) })
    expect(onSessionGone).toHaveBeenCalledWith('zzq-down')
  })
})
