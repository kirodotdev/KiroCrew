/**
 * app SDK ChatPanel — mounts the full ChatPage inside an app.
 *
 * ChatPage renders the dashboard's ACTIVE slot. When the workspace session is
 * gone (404) or the switch otherwise fails, the active slot stays on whatever
 * chat the dashboard had open, so the panel must show a "session unavailable"
 * state (or the error with Try again) and NEVER the other chat. ChatPage is
 * stubbed here: mounting the real one would pull the whole chat tree in.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'
import { i18nT } from '../i18n/t'
import ChatPanel from '../app-sdk/ChatPanel'

vi.mock('../pages/ChatPage', () => ({
  default: (props: Record<string, unknown>) => (
    <div data-testid="zzq-chatpage">{`zzq:${String(props.embedMode)}`}</div>
  ),
}))

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

const slot = (key: string): ChatSlot => ({ key, title: key, messages: 0, running: false } as ChatSlot)

// The handed slot is pre-listed so the panel's optimistic-add / fetchSlots
// branch is skipped — this test is about the switch guard, not that branch
// (covered by AppSdkChatPanel.unlistedSlot.test.tsx). conversationOnly selects
// the no-sidebar mode (the default here); pass false for the full-sidebar mode.
function setup(slotKey: string, activeSlot?: string, conversationOnly = true) {
  const store = createTestStore({
    chat: { ...chatReducer(undefined, { type: '@@init' }), ...(activeSlot ? { activeSlot } : {}) },
    dashboard: {
      ...dashboardReducer(undefined, { type: '@@init' }),
      slots: activeSlot ? [slot(slotKey), slot(activeSlot)] : [slot(slotKey)],
    } as RootState['dashboard'],
  })
  const utils = renderWithProviders(<ChatPanel slotKey={slotKey} conversationOnly={conversationOnly} />, { store })
  return { ...utils, store }
}

beforeEach(() => {
  switchSlot.mockClear()
  goneSlots.clear()
  failingSlots.clear()
})

describe('app SDK ChatPanel keeps the workspace session', () => {
  it('activates the handed slot and shows the chat only once it is active', async () => {
    const { store } = setup('coder-1')
    expect(switchSlot).toHaveBeenCalledWith('coder-1')
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(store.getState().chat.activeSlot).toBe('coder-1')
  })

  it('full mode: keeps the chat mounted when the active slot later moves away (sidebar navigation)', async () => {
    // The default (full) panel embeds the whole ChatPage, sidebar included.
    // Clicking another session there moves the active slot legitimately; the
    // panel must NOT blank — once the switch settled the chat stays mounted
    // regardless of where the active slot goes next.
    const { store } = setup('coder-1', undefined, /* conversationOnly */ false)
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'coder-other' }) })
    expect(store.getState().chat.activeSlot).toBe('coder-other')
    expect(screen.getByTestId('zzq-chatpage')).toBeInTheDocument()
  })

  it('conversationOnly: treats the session vanishing (active slot drifts to a sibling) as gone', async () => {
    // No sidebar in conversationOnly mode, so a drift of the active slot means
    // the session was deleted elsewhere and the store fell back to a sibling.
    // The panel must show the unavailable state, never the sibling's chat.
    const { store } = setup('coder-1')
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'sibling' }) })
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    expect(screen.getByText(i18nT('appSdk.chatPanel.session_unavailable'))).toBeInTheDocument()
    expect(screen.getByText(i18nT('appSdk.chatPanel.session_unavailable_detail'))).toBeInTheDocument()
  })

  it('shows "session unavailable" (with guidance, no Try again) and NO chat when the handed session is gone (404)', async () => {
    goneSlots.add('coder-gone')
    const { store } = setup('coder-gone', 'other-chat')
    await screen.findByText(i18nT('appSdk.chatPanel.session_unavailable'))
    expect(screen.getByText(i18nT('appSdk.chatPanel.session_unavailable_detail'))).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    expect(store.getState().chat.activeSlot).toBe('other-chat')
    expect(screen.queryByRole('button', { name: i18nT('components.webPreviewPanel.try_again') })).not.toBeInTheDocument()
  })

  it('shows the error with "Try again" and NO chat on a non-404 failure, while another chat is active', async () => {
    failingSlots.add('coder-down')
    setup('coder-down', 'other-chat')
    expect(await screen.findByText('gateway unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    const tryAgain = screen.getByRole('button', { name: i18nT('components.webPreviewPanel.try_again') })
    failingSlots.delete('coder-down')
    await act(async () => { fireEvent.click(tryAgain) })
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(screen.queryByText('gateway unavailable')).not.toBeInTheDocument()
  })

  it('never shows a chat the switch never settled on even when a slot is active', async () => {
    failingSlots.add('coder-x')
    setup('coder-x', 'unrelated')
    await waitFor(() => expect(screen.getByText('gateway unavailable')).toBeInTheDocument())
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
  })
})
