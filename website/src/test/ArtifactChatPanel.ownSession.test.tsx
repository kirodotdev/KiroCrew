/**
 * ArtifactChatPanel — the embedded companion chat for an artifact.
 *
 * The embedded ChatPage renders the dashboard's ACTIVE slot, not a slot of its
 * own. So the panel must make the artifact's session the active slot and show
 * the chat only while that switch has succeeded: if the session is gone (404)
 * or the switch otherwise fails, the active slot stays on whatever chat the
 * dashboard had open, and the panel must never show — or send into — that other
 * chat. ChatPage is stubbed: mounting the real one would pull the whole chat
 * tree into a test about this panel.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import chatReducer from '../store/chatSlice'
import { i18nT } from '../i18n/t'
import { ArtifactChatPanel } from '../components/ArtifactChatPanel'

vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

vi.mock('../pages/ChatPage', () => ({
  default: (props: Record<string, unknown>) => (
    <div data-testid="zzq-chatpage">{`zzq:${String(props.embedMode)}:${String(props.noUrlSync)}`}</div>
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
vi.mock('../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  switchSlot,
}))

function setup(slotKey: string | null, activeSlot?: string) {
  const store = createTestStore(
    activeSlot
      ? { chat: { ...chatReducer(undefined, { type: '@@init' }), activeSlot } }
      : undefined,
  )
  const handlers = { onNewChat: vi.fn(), onOpenFull: vi.fn(), onClose: vi.fn() }
  const utils = renderWithProviders(
    <ArtifactChatPanel slotKey={slotKey} creating={false} {...handlers} />,
    { store },
  )
  return { ...utils, ...handlers, store }
}

beforeEach(() => {
  switchSlot.mockClear()
  goneSlots.clear()
  failingSlots.clear()
})

describe('ArtifactChatPanel keeps the artifact session', () => {
  it('activates the bound session and shows the chat only once it is active', async () => {
    const { store } = setup('art-1')
    expect(switchSlot).toHaveBeenCalledWith('art-1')
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(store.getState().chat.activeSlot).toBe('art-1')
  })

  it('shows "session unavailable" with a Start-chat action (not Try again) when the bound session is gone (404)', async () => {
    goneSlots.add('art-gone')
    const { store, onNewChat } = setup('art-gone', 'other-chat')
    // The store keeps the other chat active after the 404.
    await screen.findByText(i18nT('components.artifactChatPanel.session_unavailable'))
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    expect(store.getState().chat.activeSlot).toBe('other-chat')
    // A gone session cannot be retried, so no "Try again": the way forward is a
    // new chat, which the panel's own New-chat handler starts.
    expect(screen.queryByRole('button', { name: i18nT('components.webPreviewPanel.try_again') })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.artifactChatPanel.start_chat') }))
    expect(onNewChat).toHaveBeenCalledTimes(1)
  })

  it('treats the bound session vanishing while open (active slot drifts to a sibling) as gone', async () => {
    // Session art-1 opens and its chat shows. It is then deleted elsewhere and
    // the store falls back to a sibling: this panel has no sidebar, so that
    // drift means the session is gone — never show or send into the sibling.
    const { store, onNewChat } = setup('art-1')
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'sibling' }) })
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    await screen.findByText(i18nT('components.artifactChatPanel.session_unavailable'))
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.artifactChatPanel.start_chat') }))
    expect(onNewChat).toHaveBeenCalledTimes(1)
  })

  it('shows the error with "Try again" and NO chat on a non-404 failure, while another chat is active', async () => {
    failingSlots.add('art-down')
    setup('art-down', 'other-chat')
    expect(await screen.findByText('gateway unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    const tryAgain = screen.getByRole('button', { name: i18nT('components.webPreviewPanel.try_again') })
    // Try again retries the switch; once the gateway is back the chat appears.
    failingSlots.delete('art-down')
    await act(async () => { fireEvent.click(tryAgain) })
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(screen.queryByText('gateway unavailable')).not.toBeInTheDocument()
  })

  it('never shows a chat that the switch never settled on even when a slot is active', async () => {
    // activeSlot is some unrelated chat and the bound session fails: the guard
    // (showChat requires activeSlot === the bound key) keeps the chat hidden.
    failingSlots.add('art-x')
    setup('art-x', 'unrelated')
    await waitFor(() => expect(screen.getByText('gateway unavailable')).toBeInTheDocument())
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
  })
})
