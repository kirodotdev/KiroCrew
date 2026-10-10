import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useLayoutEffect, type ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { selectComposerBusy } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { TerminalHostContext } from '../hooks/useTerminalCommand'
import { readPaneDraft, __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'
import { i18nT } from '../i18n/t'

/* The pane composer (the Crew page DM, split-view panes) offers the same
 * client-only slash commands as the main chat (`/side`, `/btw`, `/kb`,
 * `/onboarding`), so a send must run them through the same
 * `interceptSlashCommand` ChatPage uses instead of posting them to the agent
 * as text. A command whose surface the host does not render is refused on the
 * pane with the composer kept, never sent. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    sideOpen: vi.fn().mockResolvedValue({ ok: true }),
    sideTurn: vi.fn().mockResolvedValue({ ok: true }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
// Seam on the composer's Voice atom: captures the auto-submit callback (what
// the endpointer fires) and counts `disarmForSend`, over the real hook.
const voiceSeam = vi.hoisted(() => ({
  autoSubmit: undefined as (() => void) | undefined,
  disarmForSend: vi.fn(),
}))
vi.mock('../chat-core/composer/useComposerVoice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../chat-core/composer/useComposerVoice')>()
  return {
    ...actual,
    useComposerVoice: (host: Parameters<typeof actual.useComposerVoice>[0]) => {
      useLayoutEffect(() => { voiceSeam.autoSubmit = host.onAutoSubmit })
      const cv = actual.useComposerVoice(host)
      return { ...cv, disarmForSend: () => { voiceSeam.disarmForSend(); cv.disarmForSend() } }
    },
  }
})
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

const SLOT = 'pane-slash'

function renderPane(opts: { busy?: boolean; openSideChat?: (slot: string) => boolean; agentLocked?: boolean } = {}) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running: false, subagents_running: !!opts.busy, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (slot: string) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <TerminalHostContext.Provider value="docked">
              <ChatPane slotKey={slot} openSideChat={opts.openSideChat} agentLocked={opts.agentLocked} />
            </TerminalHostContext.Provider>
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
  const view = render(tree(SLOT))
  return { store, rebind: (slot: string) => view.rerender(tree(slot)) }
}

async function typeAndSend(text: string) {
  const box = (await screen.findAllByRole('textbox'))[0] as HTMLTextAreaElement
  await act(async () => {
    fireEvent.change(box, { target: { value: text } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
  })
  return box
}

beforeEach(() => {
  __resetPaneDraftsForTests()
  vi.clearAllMocks()
})

describe('ChatPane client-only slash commands', () => {
  it('/side opens the host Side Chat and asks there, never posting to the agent', async () => {
    const openSideChat = vi.fn(() => true)
    renderPane({ openSideChat })
    const box = await typeAndSend('/side what changed?')
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'what changed?'))
    expect(api.sideOpen).toHaveBeenCalledWith(SLOT)
    expect(openSideChat).toHaveBeenCalledWith(SLOT)
    expect(api.sendChat).not.toHaveBeenCalled()
    await waitFor(() => expect(box.value).toBe(''))
  })

  it('/btw is refused on a pane whose host has no Side Chat, and the text stays', async () => {
    renderPane()
    const box = await typeAndSend('/btw quick one')
    expect(await screen.findByText(i18nT('pages.chatPage.command_not_available_here', { command: '/btw' }))).toBeTruthy()
    expect(api.sideOpen).not.toHaveBeenCalled()
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(box.value).toBe('/btw quick one')
  })

  it('/kb is refused on the pane (no knowledge picker here), and the text stays', async () => {
    renderPane({ openSideChat: () => true })
    const box = await typeAndSend('/kb deploy runbook')
    expect(await screen.findByText(i18nT('pages.chatPage.command_not_available_here', { command: '/kb' }))).toBeTruthy()
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(box.value).toBe('/kb deploy runbook')
  })

  it('/onboarding replays the tour locally instead of being sent', async () => {
    const seen = vi.fn()
    window.addEventListener('mc-start-import', seen)
    try {
      renderPane()
      // The trailing space is what picking the command from the slash menu
      // leaves; bare "/onboarding" + Enter selects the menu row instead.
      await typeAndSend('/onboarding ')
      await waitFor(() => expect(seen).toHaveBeenCalledTimes(1))
      expect(api.sendChat).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('mc-start-import', seen)
    }
  })

  it('a busy pane does not steer /side into the running turn', async () => {
    const openSideChat = vi.fn(() => true)
    const { store } = renderPane({ busy: true, openSideChat })
    await waitFor(() => expect(selectComposerBusy(store.getState(), SLOT)).toBe(true))
    await typeAndSend('/side still there?')
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'still there?'))
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a refusal landing after the pane rebinds goes to the sending slot transcript', async () => {
    let reject!: (e: Error) => void
    vi.mocked(api.sideOpen).mockImplementationOnce(() => new Promise((_, rej) => { reject = rej }))
    const { store, rebind } = renderPane({ openSideChat: () => true })
    await typeAndSend('/side are you there?')
    await waitFor(() => expect(api.sideOpen).toHaveBeenCalledWith(SLOT))
    rebind('pane-other')
    await act(async () => { reject(new Error('side chat busy')) })
    await waitFor(() => {
      const rows = store.getState().chat.slotMessages[SLOT] ?? []
      expect(rows.some(m => m.role === 'error' && m.content === 'side chat busy')).toBe(true)
    })
    expect(screen.queryByTestId('chat-pane-slash-error')).toBeNull()
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a dictated command ends the dictation, as a send does', async () => {
    renderPane()
    const box = (await screen.findAllByRole('textbox'))[0] as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box, { target: { value: '/kb deploy runbook' } }) })
    await waitFor(() => expect(voiceSeam.autoSubmit).toBeTypeOf('function'))
    voiceSeam.disarmForSend.mockClear()
    await act(async () => { voiceSeam.autoSubmit!() })
    expect(voiceSeam.disarmForSend).toHaveBeenCalledTimes(1)
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a command that runs after the pane rebinds is not parked to run again', async () => {
    let open!: () => void
    vi.mocked(api.sideOpen).mockImplementationOnce(() => new Promise(res => { open = () => res({ ok: true } as never) }))
    const { rebind } = renderPane({ openSideChat: () => true })
    await typeAndSend('/side what changed?')
    await waitFor(() => expect(api.sideOpen).toHaveBeenCalledWith(SLOT))
    rebind('pane-other')
    // The rebind parked the composer, command included, under the sending slot.
    expect(readPaneDraft(SLOT).text.trim()).toBe('/side what changed?')
    await act(async () => { open() })
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'what changed?'))
    await waitFor(() => expect(readPaneDraft(SLOT).text).toBe(''))
  })

  it.each([
    ['with a host Side Chat', true, ['/side', '/btw'], ['/kb']],
    ['without a host Side Chat', false, [], ['/kb', '/side', '/btw']],
  ])('the slash menu leaves out what the pane refuses (%s)', async (_label, side, shown, hidden) => {
    renderPane(side ? { openSideChat: () => true } : {})
    const box = (await screen.findAllByRole('textbox'))[0]
    await act(async () => { fireEvent.change(box, { target: { value: '/' } }) })
    const list = await screen.findByRole('listbox')
    const names = Array.from(list.querySelectorAll('[role="option"] .font-mono')).map(n => n.textContent)
    expect(names.length).toBeGreaterThan(0)
    for (const c of shown) expect(names).toContain(c)
    for (const c of hidden) expect(names).not.toContain(c)
  })

  it('/agent <name> is refused on an agent-locked pane, and the text stays', async () => {
    renderPane({ agentLocked: true })
    const box = await typeAndSend('/agent reviewer')
    expect(await screen.findByText(i18nT('pages.chatPage.command_not_available_here', { command: '/agent' }))).toBeTruthy()
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(box.value).toBe('/agent reviewer')
  })

  it('ordinary text still sends', async () => {
    renderPane()
    await typeAndSend('hello there')
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
  })
})
