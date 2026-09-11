/**
 * Regression test for #9727: a split-view pane header offers the same rename
 * (Pen / inline editor) and regenerate (Sparkles) controls as the single-session
 * header, wired to the SAME api calls for the pane's own slot.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import { createTestStore } from './helpers'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([{ model_name: 'auto', description: 'Models chosen by task' }]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    renameSlot: vi.fn().mockResolvedValue({}),
    generateTitle: vi.fn().mockResolvedValue({ title: 'Generated title' }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
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

const SLOT = 'pane-1'
const TITLE = 'Alpha session'
const REGEN = 'Regenerate title with LLM'

function makeStore() {
  return createTestStore({
    dashboard: {
      status: null, connected: true, slotsLoaded: true,
      slots: [{ key: SLOT, title: TITLE, messages: 0, running: false, mode: '', agent: 'default', model: 'auto', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
      unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
    } as unknown as RootState['dashboard'],
  })
}

function renderPane() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore()
  const utils = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} onRemove={() => {}} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  return { store, ...utils }
}

const storeTitle = (store: ReturnType<typeof makeStore>) =>
  store.getState().dashboard.slots.find((s) => s.key === SLOT)?.title

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.renameSlot).mockResolvedValue({})
  vi.mocked(api.generateTitle).mockResolvedValue({ title: 'Generated title' })
})

describe('ChatPane header — rename and regenerate controls (#9727)', () => {
  it('renders the title as an editable control with the regenerate button in the pane header', async () => {
    renderPane()
    expect(await screen.findByRole('button', { name: TITLE })).toBeTruthy()
    expect(screen.getByRole('button', { name: REGEN })).toBeTruthy()
    // The bar itself is the hover target that reveals the Pen and Sparkles.
    expect(screen.getByRole('button', { name: TITLE }).closest('.group\\/header')).not.toBeNull()
  })

  it('click -> inline editor; Enter renames the pane slot through renameSlot and the store', async () => {
    const { store } = renderPane()
    const label = await screen.findByRole('button', { name: TITLE })
    act(() => { fireEvent.click(label) })
    const input = screen.getByDisplayValue(TITLE) as HTMLInputElement
    act(() => { fireEvent.change(input, { target: { value: 'Renamed in pane' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    act(() => { fireEvent.blur(input) })
    expect(api.renameSlot).toHaveBeenCalledWith(SLOT, 'Renamed in pane')
    expect(storeTitle(store)).toBe('Renamed in pane')
    expect(await screen.findByRole('button', { name: 'Renamed in pane' })).toBeTruthy()
  })

  it('Escape restores the title without calling the API', async () => {
    const { store } = renderPane()
    act(() => { fireEvent.click(screen.getByText(TITLE)) })
    const input = screen.getByDisplayValue(TITLE) as HTMLInputElement
    act(() => { fireEvent.change(input, { target: { value: 'Abandoned' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Escape' }) })
    act(() => { fireEvent.blur(input) })
    expect(api.renameSlot).not.toHaveBeenCalled()
    expect(storeTitle(store)).toBe(TITLE)
    expect(await screen.findByText(TITLE)).toBeTruthy()
  })

  it('Sparkles calls generateTitle for the pane slot and applies the returned title', async () => {
    const { store } = renderPane()
    const btn = await screen.findByRole('button', { name: REGEN })
    await act(async () => { fireEvent.click(btn) })
    expect(api.generateTitle).toHaveBeenCalledWith(SLOT)
    await waitFor(() => expect(storeTitle(store)).toBe('Generated title'))
    expect(await screen.findByText('Generated title')).toBeTruthy()
  })

  it('a failed rename re-pulls the server title and shows the in-pane error notice', async () => {
    vi.mocked(api.renameSlot).mockRejectedValueOnce(new Error('offline'))
    vi.mocked(api.chatSlots).mockResolvedValue([{ key: SLOT, title: TITLE, messages: 0, running: false, mode: '', agent: 'default', model: 'auto' }])
    const { store } = renderPane()
    act(() => { fireEvent.click(screen.getByText(TITLE)) })
    const input = screen.getByDisplayValue(TITLE) as HTMLInputElement
    act(() => { fireEvent.change(input, { target: { value: 'Never lands' } }) })
    act(() => { fireEvent.blur(input) })
    const notice = await screen.findByTestId('chat-pane-title-error')
    expect(notice.textContent).toContain("Couldn't rename the session")
    await waitFor(() => expect(storeTitle(store)).toBe(TITLE))
    expect(await screen.findByRole('button', { name: TITLE })).toBeTruthy()
  })

  it('when the rename AND the recovery re-read both fail, the pane reverts locally and still shows the notice', async () => {
    // Offline / expired auth takes both requests down (#10203): the pane must
    // not keep the refused title on screen, and the failure must stay visible.
    vi.mocked(api.renameSlot).mockRejectedValueOnce(new Error('gateway down'))
    vi.mocked(api.chatSlots).mockRejectedValueOnce(new Error('gateway down'))
    const { store } = renderPane()
    act(() => { fireEvent.click(screen.getByText(TITLE)) })
    const input = screen.getByDisplayValue(TITLE) as HTMLInputElement
    act(() => { fireEvent.change(input, { target: { value: 'Refused offline' } }) })
    act(() => { fireEvent.blur(input) })
    expect(storeTitle(store)).toBe('Refused offline')
    const notice = await screen.findByTestId('chat-pane-title-error')
    expect(notice.textContent).toContain("Couldn't rename the session")
    expect(notice.textContent).toContain('gateway down')
    await waitFor(() => expect(storeTitle(store)).toBe(TITLE))
    expect(await screen.findByRole('button', { name: TITLE })).toBeTruthy()
  })

  it('a failed regenerate shows the in-pane error notice and leaves the title unchanged', async () => {
    vi.mocked(api.generateTitle).mockRejectedValueOnce(new Error('llm down'))
    const { store } = renderPane()
    const btn = await screen.findByRole('button', { name: REGEN })
    await act(async () => { fireEvent.click(btn) })
    const notice = await screen.findByTestId('chat-pane-title-error')
    expect(notice.textContent).toContain("Couldn't generate a title")
    expect(notice.textContent).toContain('llm down')
    expect(storeTitle(store)).toBe(TITLE)
    expect(screen.getByText(TITLE)).toBeTruthy()
  })
})
