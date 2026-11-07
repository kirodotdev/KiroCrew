import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))

vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  chatSlotsGenerateTitles: vi.fn(),
  chatSlotsGenerateTitlesStatus: vi.fn(),
  chatFolders: vi.fn(),
  chatTags: vi.fn(),
  tagColumns: vi.fn(),
  kirocrewConfig: vi.fn(),
  sessions: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as unknown as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

const SLOTS = [
  { key: 'k-a', title: 'Idle A', running: false, messages: 1 },
  { key: 'k-b', title: 'Idle B', running: false, messages: 1 },
]

const storeRef: { current: ReturnType<typeof createTestStore> | null } = { current: null }

function renderSidebar(slots: Record<string, unknown>[] = SLOTS, onSelectSlot?: (key: string) => void) {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      automations: {}, workflowRuns: {}, subagentQueued: {}, slotHistory: [],
      revealRequest: null, revealNonce: 0,
    } as unknown as RootState['chat'],
  })
  storeRef.current = store
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  qc.setQueryData(['tag-columns'], [])
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as typeof SLOTS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false}
              defaultAgent="" installedAgents={[{ name: 'builder', source: 'builtin' }]}
              onSelectSlot={onSelectSlot}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}


// The preview query waits for the status query, so the counts render after two chained fetches.
const PREVIEW_CHAIN = { timeout: 5000 }

// Confirm starts the apply mutation, whose callbacks refetch the status (and the preview when nothing queued) before the asserted state renders.
const CONFIRM_CHAIN = { timeout: 5000 }

const PLAN = {
  ok: true, dry_run: true, queued: ['k-a', 'k-b'], skipped_running: ['k-c'],
  skipped_renamed: ['k-d'], skipped_recent: [], skipped_empty: ['k-e'], renamed: 1,
}

async function openPanel() {
  fireEvent.keyDown(screen.getAllByLabelText('More options')[0], { key: 'Enter' })
  fireEvent.click(await screen.findByText('Regenerate all titles…'))
  return screen.findByTestId('bulk-titles-panel')
}

beforeEach(() => {
  localStorage.clear()
  mocks.chatFolders.mockResolvedValue([])
  mocks.chatTags.mockResolvedValue([])
  mocks.tagColumns.mockResolvedValue([])
  mocks.sessions.mockResolvedValue({ sessions: [], has_more: false })
  mocks.kirocrewConfig.mockResolvedValue({ dashboard: { recent_tint_count: 3 } })
  mocks.chatSlotsGenerateTitlesStatus.mockResolvedValue({ ok: true, run: null })
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('ChatSidebar — Regenerate All Titles', () => {
  it('previews the plan without spending anything', async () => {
    mocks.chatSlotsGenerateTitles.mockResolvedValue(PLAN)
    renderSidebar()
    await openPanel()
    const counts = await screen.findByTestId('bulk-titles-counts', {}, PREVIEW_CHAIN)
    expect(counts.textContent).toContain('Sessions to retitle: 2')
    expect(counts.textContent).toContain('Running, left alone: 1')
    expect(counts.textContent).toContain('No conversation yet, left alone: 1')
    expect(screen.getByText('Include sessions you renamed: 1')).toBeTruthy()
    expect(mocks.chatSlotsGenerateTitles).toHaveBeenCalledTimes(1)
    expect(mocks.chatSlotsGenerateTitles).toHaveBeenCalledWith({ includeRenamed: false, dryRun: true })
  })

  it('re-previews with renamed sessions when the box is ticked', async () => {
    mocks.chatSlotsGenerateTitles.mockResolvedValue(PLAN)
    renderSidebar()
    await openPanel()
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Include sessions you renamed: 1' }, PREVIEW_CHAIN))
    await waitFor(() =>
      expect(mocks.chatSlotsGenerateTitles).toHaveBeenCalledWith({ includeRenamed: true, dryRun: true }),
    )
  })

  it('starts the run and says how many sessions it queued', async () => {
    mocks.chatSlotsGenerateTitles.mockImplementation(({ dryRun }: { dryRun: boolean }) =>
      Promise.resolve(dryRun ? PLAN : { ...PLAN, dry_run: false, run_id: 7 }),
    )
    renderSidebar()
    await openPanel()
    await screen.findByTestId('bulk-titles-counts', {}, PREVIEW_CHAIN)
    fireEvent.click(screen.getByTestId('bulk-titles-confirm'))
    const started = await screen.findByTestId('bulk-titles-started', {}, CONFIRM_CHAIN)
    expect(started.textContent).toContain('Started on 2 sessions')
    expect(mocks.chatSlotsGenerateTitles).toHaveBeenLastCalledWith({ includeRenamed: false, dryRun: false })
    fireEvent.click(screen.getByText('Close'))
    expect(screen.queryByTestId('bulk-titles-panel')).toBeNull()
  })

  it('reads in the singular when one session was queued', async () => {
    const one = { ...PLAN, queued: ['k-a'] }
    mocks.chatSlotsGenerateTitles.mockImplementation(({ dryRun }: { dryRun: boolean }) =>
      Promise.resolve(dryRun ? one : { ...one, dry_run: false, run_id: 8 }),
    )
    renderSidebar()
    await openPanel()
    await screen.findByTestId('bulk-titles-counts', {}, PREVIEW_CHAIN)
    fireEvent.click(screen.getByTestId('bulk-titles-confirm'))
    const started = await screen.findByTestId('bulk-titles-started', {}, CONFIRM_CHAIN)
    expect(started.textContent).toContain('Started on 1 session.')
  })

  it('counts the run down and reports how it ended', async () => {
    const run = { id: 7, total: 3, done: 1, retitled: 1, unchanged: 0, failed: 0, failed_sessions: [], finished: false }
    mocks.chatSlotsGenerateTitles.mockImplementation(({ dryRun }: { dryRun: boolean }) =>
      Promise.resolve(dryRun ? PLAN : { ...PLAN, dry_run: false, run_id: 7 }),
    )
    renderSidebar()
    await openPanel()
    await screen.findByTestId('bulk-titles-counts', {}, PREVIEW_CHAIN)
    mocks.chatSlotsGenerateTitlesStatus.mockResolvedValue({ ok: true, run })
    fireEvent.click(screen.getByTestId('bulk-titles-confirm'))
    const progress = await screen.findByTestId('bulk-titles-progress', {}, CONFIRM_CHAIN)
    expect(progress.textContent).toBe('Done: 1 of 3')
    mocks.chatSlotsGenerateTitlesStatus.mockResolvedValue({
      ok: true, run: { ...run, done: 3, retitled: 1, unchanged: 1, failed: 1, failed_sessions: [{ key: 'k-b', title: 'Idle B' }], finished: true },
    })
    const finished = await screen.findByTestId('bulk-titles-finished', {}, { timeout: 3000 })
    expect(finished.textContent).toContain('Finished. Retitled: 1')
    expect(finished.textContent).toContain('Left as they were: 1')
    expect(finished.textContent).not.toContain('Failed: 1')
    expect(screen.getByTestId('bulk-titles-failed-count').textContent).toContain('Failed: 1')
    expect(screen.queryByTestId('bulk-titles-progress')).toBeNull()
  })

  it('follows a run that replaced its own and reports how that one ended', async () => {
    const own = { id: 7, total: 3, done: 1, retitled: 1, unchanged: 0, failed: 0, failed_sessions: [], finished: false }
    mocks.chatSlotsGenerateTitles.mockImplementation(({ dryRun }: { dryRun: boolean }) =>
      Promise.resolve(dryRun ? PLAN : { ...PLAN, dry_run: false, run_id: 7 }),
    )
    renderSidebar()
    await openPanel()
    await screen.findByTestId('bulk-titles-counts', {}, PREVIEW_CHAIN)
    mocks.chatSlotsGenerateTitlesStatus.mockResolvedValue({ ok: true, run: own })
    fireEvent.click(screen.getByTestId('bulk-titles-confirm'))
    await screen.findByTestId('bulk-titles-progress', {}, CONFIRM_CHAIN)
    mocks.chatSlotsGenerateTitlesStatus.mockResolvedValue({
      ok: true, run: { id: 8, total: 4, done: 2, retitled: 2, unchanged: 0, failed: 0, failed_sessions: [], finished: false },
    })
    await waitFor(() => expect(screen.getByTestId('bulk-titles-progress').textContent).toBe('Done: 2 of 4'), { timeout: 3000 })
    mocks.chatSlotsGenerateTitlesStatus.mockResolvedValue({
      ok: true, run: { id: 8, total: 4, done: 4, retitled: 4, unchanged: 0, failed: 0, failed_sessions: [], finished: true },
    })
    const finished = await screen.findByTestId('bulk-titles-finished', {}, { timeout: 3000 })
    expect(finished.textContent).toContain('Finished. Retitled: 4')
  })

  it('names each session that failed and opens it on click', async () => {
    const onSelectSlot = vi.fn()
    mocks.chatSlotsGenerateTitlesStatus.mockResolvedValue({
      ok: true,
      run: {
        id: 4, total: 2, done: 2, retitled: 0, unchanged: 0, failed: 2,
        failed_sessions: [{ key: 'k-a', title: 'Idle A' }, { key: 'k-b', title: '' }],
        finished: true,
      },
    })
    mocks.chatSlotsGenerateTitles.mockImplementation(({ dryRun }: { dryRun: boolean }) =>
      Promise.resolve(dryRun ? PLAN : { ...PLAN, dry_run: false, run_id: 4 }),
    )
    renderSidebar(SLOTS, onSelectSlot)
    await openPanel()
    await screen.findByTestId('bulk-titles-counts', {}, PREVIEW_CHAIN)
    fireEvent.click(screen.getByTestId('bulk-titles-confirm'))
    const names = await screen.findByTestId('bulk-titles-failed-sessions', {}, { timeout: 3000 })
    const buttons = within(names).getAllByRole('button')
    expect(buttons.map(b => b.textContent)).toEqual(['Idle A', 'Untitled session'])
    fireEvent.click(buttons[1])
    expect(onSelectSlot).toHaveBeenCalledWith('k-b')
    await waitFor(() => expect(storeRef.current?.getState().chat.activeSlot).toBe('k-b'))
  })

  it('scrolls a long failure list and keeps Close outside it', async () => {
    const failed_sessions = Array.from({ length: 30 }, (_, i) => ({ key: `k-${i}`, title: `Session ${i}` }))
    mocks.chatSlotsGenerateTitlesStatus.mockResolvedValue({
      ok: true,
      run: { id: 5, total: 30, done: 30, retitled: 0, unchanged: 0, failed: 30, failed_sessions, finished: true },
    })
    mocks.chatSlotsGenerateTitles.mockImplementation(({ dryRun }: { dryRun: boolean }) =>
      Promise.resolve(dryRun ? PLAN : { ...PLAN, dry_run: false, run_id: 5 }),
    )
    renderSidebar()
    const panel = await openPanel()
    await screen.findByTestId('bulk-titles-counts', {}, PREVIEW_CHAIN)
    fireEvent.click(screen.getByTestId('bulk-titles-confirm'))
    const names = await screen.findByTestId('bulk-titles-failed-sessions', {}, { timeout: 3000 })
    expect(names.className).toContain('overflow-y-auto')
    expect(names.className).toMatch(/max-h-/)
    const close = within(panel).getByRole('button', { name: 'Close' })
    expect(names.contains(close)).toBe(false)
  })

  it('shows the run in flight instead of a new preview when reopened', async () => {
    mocks.chatSlotsGenerateTitlesStatus.mockResolvedValue({
      ok: true, run: { id: 3, total: 4, done: 2, retitled: 2, unchanged: 0, failed: 0, failed_sessions: [], finished: false },
    })
    renderSidebar()
    await openPanel()
    const progress = await screen.findByTestId('bulk-titles-progress')
    expect(progress.textContent).toBe('Done: 2 of 4')
    expect(screen.queryByTestId('bulk-titles-confirm')).toBeNull()
    expect(mocks.chatSlotsGenerateTitles).not.toHaveBeenCalled()
  })

  it('keeps showing a run reopened mid-flight until its summary', async () => {
    const run = { id: 3, total: 2, done: 1, retitled: 1, unchanged: 0, failed: 0, failed_sessions: [], finished: false }
    mocks.chatSlotsGenerateTitles.mockResolvedValue(PLAN)
    mocks.chatSlotsGenerateTitlesStatus.mockResolvedValue({ ok: true, run })
    renderSidebar()
    await openPanel()
    await screen.findByTestId('bulk-titles-progress')
    mocks.chatSlotsGenerateTitlesStatus.mockResolvedValue({ ok: true, run: { ...run, done: 2, retitled: 2, finished: true } })
    const finished = await screen.findByTestId('bulk-titles-finished', {}, { timeout: 3000 })
    expect(finished.textContent).toContain('Finished. Retitled: 2')
    expect(screen.queryByTestId('bulk-titles-counts')).toBeNull()
  })

  it('switches to the other run when a second confirm is refused', async () => {
    mocks.chatSlotsGenerateTitles.mockImplementation(({ dryRun }: { dryRun: boolean }) =>
      dryRun ? Promise.resolve(PLAN) : Promise.reject(new Error('409')),
    )
    renderSidebar()
    await openPanel()
    await screen.findByTestId('bulk-titles-counts', {}, PREVIEW_CHAIN)
    mocks.chatSlotsGenerateTitlesStatus.mockResolvedValue({
      ok: true, run: { id: 9, total: 5, done: 2, retitled: 2, unchanged: 0, failed: 0, failed_sessions: [], finished: false },
    })
    fireEvent.click(screen.getByTestId('bulk-titles-confirm'))
    const progress = await screen.findByTestId('bulk-titles-progress', {}, CONFIRM_CHAIN)
    expect(progress.textContent).toBe('Done: 2 of 5')
  })

  it('previews as usual when the last run already finished', async () => {
    mocks.chatSlotsGenerateTitles.mockResolvedValue(PLAN)
    mocks.chatSlotsGenerateTitlesStatus.mockResolvedValue({
      ok: true, run: { id: 3, total: 1, done: 1, retitled: 1, unchanged: 0, failed: 0, failed_sessions: [], finished: true },
    })
    renderSidebar()
    await openPanel()
    await screen.findByTestId('bulk-titles-counts', {}, PREVIEW_CHAIN)
    expect(screen.queryByTestId('bulk-titles-finished')).toBeNull()
  })

  it('cannot confirm when nothing would be retitled', async () => {
    mocks.chatSlotsGenerateTitles.mockResolvedValue({ ...PLAN, queued: [] })
    renderSidebar()
    await openPanel()
    await screen.findByTestId('bulk-titles-counts', {}, PREVIEW_CHAIN)
    expect((screen.getByTestId('bulk-titles-confirm') as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows a notice when the preview fails', async () => {
    mocks.chatSlotsGenerateTitles.mockRejectedValue(new Error('500'))
    renderSidebar()
    await openPanel()
    const notice = await screen.findByTestId('bulk-titles-preview-error', {}, PREVIEW_CHAIN)
    expect(notice.textContent).toContain("Couldn't count the sessions to retitle.")
    expect((screen.getByTestId('bulk-titles-confirm') as HTMLButtonElement).disabled).toBe(true)
  })

  it('goes back to the preview when the apply queued nothing', async () => {
    const later = { ...PLAN, queued: [], skipped_running: ['k-a', 'k-b', 'k-c'] }
    let applied = false
    mocks.chatSlotsGenerateTitles.mockImplementation(({ dryRun }: { dryRun: boolean }) => {
      if (!dryRun) {
        applied = true
        return Promise.resolve({ ...later, dry_run: false, run_id: null })
      }
      return Promise.resolve(applied ? later : PLAN)
    })
    renderSidebar()
    await openPanel()
    await screen.findByTestId('bulk-titles-counts', {}, PREVIEW_CHAIN)
    fireEvent.click(screen.getByTestId('bulk-titles-confirm'))
    await waitFor(() => expect(screen.getByTestId('bulk-titles-counts').textContent).toContain('Running, left alone: 3'), CONFIRM_CHAIN)
    expect(screen.queryByTestId('bulk-titles-started')).toBeNull()
    expect((screen.getByTestId('bulk-titles-confirm') as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows a notice when the progress cannot be read', async () => {
    mocks.chatSlotsGenerateTitles.mockResolvedValue(PLAN)
    mocks.chatSlotsGenerateTitlesStatus.mockRejectedValue(new Error('500'))
    renderSidebar()
    await openPanel()
    const notice = await screen.findByTestId('bulk-titles-status-error')
    expect(notice.textContent).toContain("Couldn't read the title progress.")
  })
})
