import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ComponentProps } from 'react'
import type ChatSidePanels from '../pages/chat/page/ChatSidePanels'
import { createConnectedTestStore, renderWithProviders } from './helpers'
import type { ChatSlot } from '../types'

let panels: ComponentProps<typeof ChatSidePanels> | null = null
vi.mock('../pages/chat/page/ChatSidePanels', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../pages/chat/page/ChatSidePanels')>()
  return {
    ...actual,
    default: (props: ComponentProps<typeof ChatSidePanels>) => {
      panels = props
      return <actual.default {...props} />
    },
  }
})
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {}
const apiSpy = (name: string) => apiMocks[name] ??= vi.fn().mockResolvedValue({})
vi.mock('../api/client', () => ({
  api: new Proxy({}, { get: (_target, name: string) => apiSpy(name) }),
  fileReadUrl: (path: string) => `/api/file?path=${encodeURIComponent(path)}`,
  SEARCH_MIN_CHARS: 2,
}))

import ChatPage from '../pages/ChatPage'

const slot: ChatSlot = { key: 'chat-1', title: 'chat-1', messages: 1, running: false, mode: '', created: '', last_ts: '' }

async function renderTurn() {
  const messages = [{ role: 'user', content: 'what changed?', cls: '', ts: '2026-08-12T07:00:00Z' }]
  apiSpy('chatSlots').mockResolvedValue([slot])
  apiSpy('chatSlotDetail').mockResolvedValue({ messages, has_more: false, total: 1 })
  apiSpy('sessions').mockResolvedValue({ sessions: [], has_more: false })
  apiSpy('pendingQuestions').mockResolvedValue([])
  apiSpy('approvals').mockResolvedValue([])
  apiSpy('chatSlotEnvironment').mockResolvedValue({ profile: null, profiles: [] })
  const base = createConnectedTestStore().getState()
  const store = createConnectedTestStore({
    dashboard: { ...base.dashboard, slots: [slot] },
    chat: { ...base.chat, activeSlot: 'chat-1' },
  })
  renderWithProviders(<ChatPage mode="" />, { store, route: '/chat?sid=chat-1' })
  await waitFor(() => expect(panels?.activeSlot).toBe('chat-1'))
  await waitFor(() => expect(apiMocks.chatSlotDetail).toHaveBeenCalled())
}

beforeEach(() => {
  panels = null
  for (const key of Object.keys(apiMocks)) delete apiMocks[key]
  apiSpy('dashboardConfig').mockResolvedValue({ tail_fork_enabled: false })
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-11-23T12:00:00Z'))
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('ChatPage history-preview fork', () => {
  it('returns the pending history-preview fork promise until the request settles', async () => {
    let finish!: (value: { ok: boolean; key: string; title: string }) => void
    const request = new Promise<{ ok: boolean; key: string; title: string }>(resolve => { finish = resolve })
    apiSpy('forkChatSlot').mockReturnValue(request)
    await renderTurn()
    let returned: void | Promise<unknown>
    let settled = false
    try {
      act(() => { returned = panels!.historyActions!.onForkMessage!('m-older') })
      expect(returned!).toBeInstanceOf(Promise)
      void returned!.then(() => { settled = true })
      await waitFor(() => expect(apiMocks.forkChatSlot).toHaveBeenCalledWith('chat-1', 0, undefined, undefined, 'head', 'm-older'))
      expect(settled).toBe(false)
    } finally {
      await act(async () => {
        finish({ ok: true, key: 'chat-2', title: 'fork' })
        await returned!
      })
    }
    expect(settled).toBe(true)
  })

  it.each([false, true])('closes the search after a refused fork (mobile=%s)', async (mobile) => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: mobile && query.includes('max-width'), media: query, onchange: null,
      addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
    })))
    apiSpy('forkChatSlot').mockResolvedValue({ ok: false, error: 'slot is busy' })
    await renderTurn()
    fireEvent.keyDown(document, { key: 'f', ctrlKey: true })
    // Wait for ChatSidePanels' lazy ChatHistorySearch chunk.
    expect(await screen.findByRole('textbox', { name: 'Search this chat' }, { timeout: 5000 })).toBeInTheDocument()
    await act(async () => { await panels!.historyActions!.onForkMessage!('m-older') })
    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Search this chat' })).not.toBeInTheDocument())
    expect(await screen.findByText(/slot is busy/)).toBeVisible()
  })
})
