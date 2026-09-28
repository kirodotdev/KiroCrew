/**
 * ChatPane composer agent chip — the inherited-default label must resolve the
 * PEER's default on a remote pane, never this machine's (#8770 GPT review).
 *
 * A remote (peer-bound) pane runs its agent-less session under the PEER's
 * default agent. Feeding the local `defaultAgent` into the inherited-default
 * label would mark such a session `<local-default> · default`, falsely naming a
 * roster the peer does not use. ChatPane mirrors ChatPage's `effectiveDefaultAgent`
 * guard: remote -> the peer's default (or '' until it loads), local -> the local
 * default. These tests pin both branches.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, waitFor, screen, fireEvent, within } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

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
    models: vi.fn().mockResolvedValue([
      { model_name: 'auto', description: 'Models chosen by task' },
      { model_name: 'gpt-6-sol[low]', description: 'GPT low' },
      { model_name: 'gpt-6-sol[medium]', description: 'GPT medium' },
    ]),
    chatSlotSelectionCapabilities: vi.fn().mockResolvedValue({ known: false }),
    chatSlotReasoningEffort: vi.fn().mockImplementation(async (_slot: string, effort: string) => ({ reasoning_effort: effort, model: 'gpt-6-sol' })),
    chatSlotModel: vi.fn().mockImplementation(async (_slot: string, model: string) => ({ model })),
    effortLevels: vi.fn().mockResolvedValue(['low', 'medium', 'high']),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
// Local roster: the machine's default is `localboss`.
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'localboss' }], defaultAgent: 'localboss' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

// The remote-capabilities hook is what distinguishes a peer pane. Controlled per test.
const remoteState = { isRemote: false, capabilities: undefined as undefined | { default_agent: string }, isLoading: false, failed: false }
vi.mock('../hooks/useRemoteCapabilities', () => ({ useRemoteCapabilities: () => remoteState }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

function makeStore(slotKey: string, slot: Record<string, unknown>) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', model: 'auto', pending_approval: false, waiting_for_input: false, ...slot }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(slotKey: string, slot: Record<string, unknown>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey, slot)
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

function agentChipText(): string {
  const bot = document.querySelector('button svg.lucide-bot') as SVGElement | null
  const span = bot?.closest('button')?.querySelector('span')
  return span?.textContent ?? ''
}

beforeEach(() => {
  vi.clearAllMocks()
  remoteState.isRemote = false
  remoteState.capabilities = undefined
})

describe('ChatPane — inherited-default label resolves the peer default on a remote pane (#8770)', () => {
  it('a LOCAL agent-less pane marks with the local default', async () => {
    renderPane('pane-local', { agent: '' })
    await waitFor(() => expect(document.querySelector('button svg.lucide-bot')).not.toBeNull())
    // Local default `localboss` -> the inherited marker names it.
    expect(agentChipText()).toBe('localboss \u00b7 default')
  })

  it('a REMOTE agent-less pane does NOT use the local default', async () => {
    // Peer's default is `peerboss`, not this machine's `localboss`.
    remoteState.isRemote = true
    remoteState.capabilities = { default_agent: 'peerboss' }
    renderPane('pane-remote', { agent: '', executor: 'remote', instance_id: 'inst-1' })
    await waitFor(() => expect(document.querySelector('button svg.lucide-bot')).not.toBeNull())
    const text = agentChipText()
    // The peer default, never the local one — that is the whole bug.
    expect(text).toContain('peerboss')
    expect(text).not.toContain('localboss')
  })

  it('a REMOTE pane whose capabilities have not loaded shows no false local marker', async () => {
    // isRemote true but capabilities undefined -> effective default '' -> no
    // marker rather than the local default's.
    remoteState.isRemote = true
    remoteState.capabilities = undefined
    renderPane('pane-remote-loading', { agent: '', executor: 'remote', instance_id: 'inst-2' })
    await waitFor(() => expect(document.querySelector('button svg.lucide-bot')).not.toBeNull())
    expect(agentChipText()).not.toContain('localboss')
  })
})

// Model + effort are ONE control (docs/decisions/2026-06-14): the capability
// read decides whether the model picker embeds the effort slider, and the
// composer never grows a standalone effort button.
describe('ChatPane — ACP model and effort controls', () => {
  it('groups Codex pair IDs and embeds effort inside the model picker in a split pane', async () => {
    vi.mocked(api.chatSlotSelectionCapabilities).mockResolvedValueOnce({
      known: true, backend: 'codex', effort_supported: true,
      effort_levels: ['low', 'medium', 'high'], model_effort_pair_ids: true,
    })
    renderPane('pane-codex', { model: 'gpt-6-sol[medium]', reasoning_effort: '' })
    // The chip names the level in force; there is no second composer control.
    // `aria-label` replaces the chip's content in its accessible name, so the
    // level has to be IN it (and in the hover title) or it is announced nowhere.
    const modelChip = await screen.findByTitle('Model: gpt-6-sol · Reasoning effort: Medium')
    expect(modelChip.textContent).toContain('Medium')
    expect(modelChip).toHaveAccessibleName('Model: gpt-6-sol · Reasoning effort: Medium')
    expect(screen.queryByTestId('composer-effort-chip')).toBeNull()
    fireEvent.click(modelChip)
    const dialog = await screen.findByRole('dialog', { name: 'Model list' })
    const modelList = within(dialog).getByRole('listbox', { name: 'Model list' })
    expect(modelList.textContent).toContain('gpt-6-sol')
    expect(modelList.textContent).not.toContain('gpt-6-sol[low]')
    expect(modelList.textContent).not.toContain('gpt-6-sol[medium]')
    // The slider renders INSIDE the picker dialog, over the advertised levels.
    const slider = await within(dialog).findByRole('slider', { name: 'Reasoning effort' })
    expect(slider).toHaveAttribute('aria-valuemax', '2')
    expect(within(dialog).getByRole('switch', { name: 'Use default effort' })).toBeInTheDocument()
    expect(api.effortLevels).not.toHaveBeenCalled()
  })

  it('keeps advertised model IDs and shows no effort row when the ACP backend reports none', async () => {
    vi.mocked(api.chatSlotSelectionCapabilities).mockResolvedValueOnce({
      known: true, backend: 'claude', effort_supported: false,
      effort_levels: [], model_effort_pair_ids: false,
    })
    renderPane('pane-claude', { model: 'gpt-6-sol[medium]' })
    await waitFor(() => expect(api.chatSlotSelectionCapabilities).toHaveBeenCalledWith('pane-claude'))
    expect(screen.queryByTestId('composer-effort-chip')).toBeNull()
    const modelChip = await screen.findByTitle('Model: gpt-6-sol[medium]')
    fireEvent.click(modelChip)
    const dialog = await screen.findByRole('dialog', { name: 'Model list' })
    expect(within(dialog).queryByRole('slider', { name: 'Reasoning effort' })).toBeNull()
  })

  it('moves a legacy pair level into the slot before changing its model', async () => {
    vi.mocked(api.chatSlotSelectionCapabilities).mockResolvedValueOnce({
      known: true, backend: 'codex', effort_supported: true,
      effort_levels: ['low', 'medium', 'high'], model_effort_pair_ids: true,
    })
    renderPane('pane-migration', { model: 'gpt-6-sol[medium]', reasoning_effort: '' })
    const modelChip = await screen.findByTitle(/^Model: gpt-6-sol · /)
    fireEvent.click(modelChip)
    fireEvent.click(await screen.findByRole('option', { name: /gpt-6-sol/ }))
    await waitFor(() => expect(api.chatSlotModel).toHaveBeenCalledWith('pane-migration', 'gpt-6-sol'))
    expect(api.chatSlotReasoningEffort).toHaveBeenCalledWith('pane-migration', 'medium')
    expect(vi.mocked(api.chatSlotReasoningEffort).mock.invocationCallOrder[0])
      .toBeLessThan(vi.mocked(api.chatSlotModel).mock.invocationCallOrder[0])
  })

  it('keeps the model picker (and its embedded slider) inside a 320px viewport', async () => {
    vi.mocked(api.chatSlotSelectionCapabilities).mockResolvedValueOnce({
      known: true, backend: 'codex', effort_supported: true,
      effort_levels: ['low', 'medium', 'high'], model_effort_pair_ids: true,
    })
    renderPane('pane-narrow', { model: 'gpt-6-sol[medium]' })
    const modelChip = await screen.findByTitle(/^Model: gpt-6-sol · /)
    modelChip.getBoundingClientRect = () => new DOMRect(280, 500, 24, 28)
    vi.stubGlobal('innerWidth', 320)
    try {
      fireEvent.click(modelChip)
      const dialog = await screen.findByRole('dialog', { name: 'Model list' })
      await within(dialog).findByRole('slider', { name: 'Reasoning effort' })
      // 320 - 348 < 8 -> clamped to the 8px gutter.
      expect(dialog).toHaveStyle({ left: '8px' })
      // Capped to the space above the chip (top 500 - 12), like
      // ModelEffortDropdown: the effort block adds height, and only the model
      // list may shrink to absorb it in a short split pane.
      expect(dialog).toHaveStyle({ maxHeight: '488px' })
      expect(dialog).toHaveClass('flex', 'flex-col', 'overflow-hidden')
      expect(within(dialog).getByRole('listbox', { name: 'Model list' })).toHaveClass('min-h-[96px]', 'flex-1', 'overflow-y-auto')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('keeps the picker cap at the space above a chip near the viewport top', async () => {
    // Three stacked split-down panes on a 768px display put the chip ~120px
    // from the top. The dialog is bottom-anchored, so a cap larger than that
    // space would overhang the viewport top and hide the filter; instead the
    // model list (the only child allowed to shrink) absorbs the overflow
    // while the filter row keeps its height. Same cap as ModelEffortDropdown.
    vi.mocked(api.chatSlotSelectionCapabilities).mockResolvedValueOnce({
      known: true, backend: 'codex', effort_supported: true,
      effort_levels: ['low', 'medium', 'high'], model_effort_pair_ids: true,
    })
    renderPane('pane-stacked', { model: 'gpt-6-sol[medium]' })
    const modelChip = await screen.findByTitle(/^Model: gpt-6-sol · /)
    modelChip.getBoundingClientRect = () => new DOMRect(280, 120, 24, 28)
    fireEvent.click(modelChip)
    const dialog = await screen.findByRole('dialog', { name: 'Model list' })
    await within(dialog).findByRole('slider', { name: 'Reasoning effort' })
    expect(dialog).toHaveStyle({ maxHeight: '108px' })
    expect(within(dialog).getByPlaceholderText('Type to filter…').parentElement).toHaveClass('shrink-0')
    expect(within(dialog).getByRole('listbox', { name: 'Model list' })).toHaveClass('min-h-[96px]', 'flex-1')
    // Once the cap is smaller than the fixed rows themselves, the body column
    // scrolls so the effort block stays reachable instead of being clipped.
    expect(within(dialog).getByRole('listbox', { name: 'Model list' }).parentElement).toHaveClass('min-h-0', 'flex-1', 'overflow-y-auto')
  })
})
