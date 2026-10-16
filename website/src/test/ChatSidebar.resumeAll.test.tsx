/**
 * The sidebar's Resume-all row: absent with nothing interrupted, present with the
 * count, one press -> one POST, the outcome line, and the offline hold.
 *
 * Renders the whole ChatSidebar (the row is composed into the search dock's shelf,
 * and the hook reads the slots the sidebar is handed), with the same dnd / motion /
 * api stubs the folder-order test uses.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: { children?: unknown }) => props.children as never,
  }
})
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

const resumeAll = vi.fn()
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, name: string) => {
      if (name === 'chatSlotsResumeAll') return resumeAll
      if (name === 'kirocrewConfig') return vi.fn().mockResolvedValue({ dashboard: {} })
      return vi.fn().mockResolvedValue([])
    },
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
import { interruptedIdleSlots, resumeAllResultText } from '../pages/chat-sidebar/resumeAll'
import { RESUME_ALL_DONE_EVENT } from '../hooks/useWebSocket'
import type { Slot } from '../pages/chat-sidebar/types'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

function slot(key: string, extra: Partial<ChatSlot> = {}): ChatSlot {
  return { key, title: key, running: false, messages: 2, created: '2026-10-15T00:00:00Z', last_ts: '2026-10-15T00:00:00Z', ...extra } as ChatSlot
}

function renderSidebar(slots: ChatSlot[], connected = true) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  qc.setQueryData(['kirocrewConfig'], { dashboard: {} })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => { localStorage.clear(); resumeAll.mockReset() })
afterEach(() => vi.clearAllMocks())

describe('interruptedIdleSlots', () => {
  it('counts idle interrupted local sessions and nothing else', () => {
    const rows: Slot[] = [
      { key: 'a', running: false, interrupted: true },
      { key: 'busy', running: true, interrupted: true },
      { key: 'children', running: false, interrupted: true, subagents_running: true },
      { key: 'peer', running: false, interrupted: true, peer_id: 'crew-2' },
      { key: 'remote', running: false, interrupted: true, executor: 'remote' },
      { key: 'fine', running: false, interrupted: false },
      { key: 'b', running: false, interrupted: true },
    ]
    expect(interruptedIdleSlots(rows).map(s => s.key)).toEqual(['a', 'b'])
  })
})

describe('resumeAllResultText', () => {
  it('names the full, the partial and the empty outcome', () => {
    expect(resumeAllResultText({ ok: true, total: 2, resumed: ['a', 'b'], skipped: [], failed: [] })).toBe('Resumed 2 sessions')
    expect(resumeAllResultText({ ok: true, total: 3, resumed: ['a'], skipped: [{ slot: 'b', code: 'slot_queue_pending' }], failed: [{ slot: 'c', error: 'RuntimeError' }] }))
      .toBe('Resumed 1 session · 2 not resumed')
    expect(resumeAllResultText({ ok: true, total: 0, resumed: [], skipped: [], failed: [] })).toBe('Nothing to resume')
  })
})

describe('chat sidebar — Resume all interrupted sessions', () => {
  it('draws no row when nothing is interrupted', () => {
    const { queryByTestId } = renderSidebar([slot('a'), slot('b', { running: true })])
    expect(queryByTestId('resume-all-row')).toBeNull()
  })

  it('shows the count and the action while sessions are interrupted', () => {
    const { getByTestId } = renderSidebar([
      slot('a', { interrupted: true }),
      slot('b', { interrupted: true }),
      slot('c', { interrupted: true, running: true }),
      slot('d'),
    ])
    expect(getByTestId('resume-all-row').getAttribute('data-resume-all-count')).toBe('2')
    expect(getByTestId('resume-all-text').textContent).toBe('2 interrupted sessions')
    const button = getByTestId('resume-all-button')
    expect(button.textContent).toBe('Resume 2 sessions')
    expect(button).not.toBeDisabled()
  })

  it('one press is one POST; the row is pending until the run outcome arrives', async () => {
    resumeAll.mockResolvedValue({ ok: true, total: 2, run_id: 'run-1' })
    const { getByTestId } = renderSidebar([slot('a', { interrupted: true }), slot('b', { interrupted: true })])
    await act(async () => { fireEvent.click(getByTestId('resume-all-button')) })
    expect(resumeAll).toHaveBeenCalledTimes(1)
    expect(resumeAll).toHaveBeenCalledWith()
    await waitFor(() => expect(getByTestId('resume-all-button')).toBeDisabled())
    expect(getByTestId('resume-all-button').textContent).toBe('Resuming…')
    // Another run's outcome does not end this row's wait.
    act(() => { window.dispatchEvent(new CustomEvent(RESUME_ALL_DONE_EVENT, { detail: { ok: true, run_id: 'other', total: 1, resumed: ['x'], skipped: [], failed: [] } })) })
    expect(getByTestId('resume-all-button').textContent).toBe('Resuming…')
    act(() => {
      window.dispatchEvent(new CustomEvent(RESUME_ALL_DONE_EVENT, { detail: { ok: true, run_id: 'run-1', total: 2, resumed: ['a'], skipped: [{ slot: 'b', code: 'slot_queue_pending' }], failed: [] } }))
    })
    await waitFor(() => expect(getByTestId('resume-all-text').textContent).toBe('Resumed 1 session · 1 not resumed'))
    expect(getByTestId('resume-all-button')).not.toBeDisabled()
  })

  it('nothing to resume on the server answers at once', async () => {
    resumeAll.mockResolvedValue({ ok: true, total: 0, run_id: '' })
    const { getByTestId } = renderSidebar([slot('a', { interrupted: true })])
    await act(async () => { fireEvent.click(getByTestId('resume-all-button')) })
    await waitFor(() => expect(getByTestId('resume-all-text').textContent).toBe('Nothing to resume'))
  })

  it('a press while another run is going says it is already running and follows that run', async () => {
    resumeAll.mockRejectedValue(Object.assign(new Error('a resume-all run is already in progress'), {
      status: 409, body: JSON.stringify({ code: 'resume_all_running', run_id: 'run-7' }),
    }))
    const { getByTestId, queryByTestId } = renderSidebar([slot('a', { interrupted: true })])
    await act(async () => { fireEvent.click(getByTestId('resume-all-button')) })
    await waitFor(() => expect(getByTestId('resume-all-text').textContent).toBe('A resume is already running'))
    expect(getByTestId('resume-all-button')).toBeDisabled()
    expect(queryByTestId('resume-all-error')).toBeNull()
    act(() => {
      window.dispatchEvent(new CustomEvent(RESUME_ALL_DONE_EVENT, { detail: { ok: true, run_id: 'run-7', total: 1, resumed: ['a'], skipped: [], failed: [] } }))
    })
    await waitFor(() => expect(getByTestId('resume-all-text').textContent).toBe('Resumed 1 session'))
  })

  it('a non-owner gets its own message and no agent hand-off', async () => {
    resumeAll.mockRejectedValue(Object.assign(new Error('owner authorization required'), {
      status: 403, body: JSON.stringify({ error: 'owner authorization required', code: 'owner_only' }),
    }))
    const { getByTestId } = renderSidebar([slot('a', { interrupted: true })])
    await act(async () => { fireEvent.click(getByTestId('resume-all-button')) })
    await waitFor(() => expect(getByTestId('resume-all-error').textContent).toContain('Only the dashboard owner can resume every session'))
    expect(getByTestId('resume-all-error').textContent).not.toContain('owner authorization required')
    expect(getByTestId('resume-all-error').textContent).not.toContain('Ask the agent')
  })

  it('reports a refused request through ErrorNotice and keeps the action', async () => {
    resumeAll.mockRejectedValue(new Error('owner authorization required'))
    const { getByTestId } = renderSidebar([slot('a', { interrupted: true })])
    await act(async () => { fireEvent.click(getByTestId('resume-all-button')) })
    await waitFor(() => expect(getByTestId('resume-all-error').textContent).toContain('owner authorization required'))
    expect(getByTestId('resume-all-error').getAttribute('role')).toBe('alert')
    // The notice is a sibling of the polite status row, never nested in it.
    expect(getByTestId('resume-all-row').contains(getByTestId('resume-all-error'))).toBe(false)
    expect(getByTestId('resume-all-text').textContent).toBe('1 interrupted session')
    expect(getByTestId('resume-all-button')).not.toBeDisabled()
  })

  it('withholds the action while the dashboard is offline', () => {
    const { getByTestId } = renderSidebar([slot('a', { interrupted: true })], false)
    expect(getByTestId('resume-all-button')).toBeDisabled()
    expect(resumeAll).not.toHaveBeenCalled()
  })
})
