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
import { MORE_BELOW_MASK } from '../components/ModelEffortDropdown'
import { RESUME_ALL_DONE_EVENT } from '../lib/resumeAllEvent'
import { isChatPageSurface } from '../utils/channelOrigin'
import { crewModeView } from '../lib/crewMode'
import { PREVIEW_CREW_MODE, setPreviewFlag } from '../utils/previewFlags'
import countFixture from '../../../test/fixtures/resume_all_candidates.json'
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

// Drains pending promise callbacks and the React updates they schedule (the
// theme provider's boot among them), with no wall-clock wait. A test that
// asserts on the first render calls this first, so no update lands after it.
async function settle(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve()
  })
}

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
  it('draws no row when nothing is interrupted', async () => {
    const { queryByTestId } = renderSidebar([slot('a'), slot('b', { running: true })])
    await settle()
    expect(queryByTestId('resume-all-row')).toBeNull()
  })

  it('shows the count and the action while sessions are interrupted', async () => {
    const { getByTestId } = renderSidebar([
      slot('a', { interrupted: true }),
      slot('b', { interrupted: true }),
      slot('c', { interrupted: true, running: true }),
      slot('d'),
    ])
    await settle()
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
    expect(resumeAll).toHaveBeenCalledWith(false)
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

  it('a non-owner gets its own message, with the agent hand-off', async () => {
    resumeAll.mockRejectedValue(Object.assign(new Error('owner authorization required'), {
      status: 403, body: JSON.stringify({ error: 'owner authorization required', code: 'owner_only' }),
    }))
    const { getByTestId } = renderSidebar([slot('a', { interrupted: true })])
    await act(async () => { fireEvent.click(getByTestId('resume-all-button')) })
    await waitFor(() => expect(getByTestId('resume-all-error').textContent).toContain('Only the dashboard owner can resume every session'))
    expect(getByTestId('resume-all-error').textContent).not.toContain('owner authorization required')
    // Nothing on the sidebar is unsaved and the agent still runs for a
    // non-owner, so the refusal keeps the hand-off (errors-use-error-notice).
    expect(getByTestId('resume-all-error').textContent).toContain('Ask the agent')
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

  it('names each session the run could not resume, and why, through ErrorNotice', async () => {
    resumeAll.mockResolvedValue({ ok: true, total: 4, run_id: 'run-p' })
    const { getByTestId } = renderSidebar([
      slot('a', { interrupted: true }),
      slot('b', { interrupted: true, title: 'Parser refactor' }),
      slot('c', { interrupted: true, title: 'Release notes' }),
      slot('d', { interrupted: true }),
    ])
    await act(async () => { fireEvent.click(getByTestId('resume-all-button')) })
    act(() => {
      window.dispatchEvent(new CustomEvent(RESUME_ALL_DONE_EVENT, { detail: {
        ok: true, run_id: 'run-p', total: 4, resumed: ['a'],
        skipped: [{ slot: 'b', code: 'session_start_repeat' }, { slot: 'd', code: 'turn_not_failed' }],
        failed: [{ slot: 'c', error: 'RuntimeError' }],
      } }))
    })
    const notice = await waitFor(() => getByTestId('resume-all-problems'))
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain('Some sessions were not resumed')
    expect(notice.textContent).toContain('Parser refactor: The agent failed to start twice in a row. Restart the gateway (kirocrew restart), then send your message again.')
    expect(notice.textContent).toContain('Release notes: An error stopped it.')
    // A session that finished on its own needs nothing, so it is not named.
    expect(notice.textContent).not.toContain('d:')
    expect(notice.textContent).toContain('Ask the agent')
    expect(getByTestId('resume-all-row').contains(notice)).toBe(false)
    // The list scrolls in its own bounded box; the hand-off and Dismiss sit
    // outside it, so a long list cannot push them off the panel.
    const list = Array.from(notice.querySelectorAll('span')).find(el => el.textContent?.startsWith('Parser refactor:'))
    expect(list).toBeDefined()
    expect(list!.className).toContain('max-h-24')
    expect(list!.className).toContain('overflow-y-auto')
    const handoff = Array.from(notice.querySelectorAll('button')).find(b => b.textContent?.includes('Ask the agent'))
    expect(handoff).toBeDefined()
    expect(list!.contains(handoff!)).toBe(false)
    expect(list!.contains(notice.querySelector('button[aria-label]'))).toBe(false)
  })

  it('fades the problems list\'s bottom edge only while lines sit below it', async () => {
    resumeAll.mockResolvedValue({ ok: true, total: 3, run_id: 'run-f' })
    const { getByTestId } = renderSidebar([
      slot('a', { interrupted: true, title: 'Parser refactor' }),
      slot('b', { interrupted: true, title: 'Release notes' }),
      slot('c', { interrupted: true, title: 'Upload test' }),
    ])
    await act(async () => { fireEvent.click(getByTestId('resume-all-button')) })
    act(() => {
      window.dispatchEvent(new CustomEvent(RESUME_ALL_DONE_EVENT, { detail: {
        ok: true, run_id: 'run-f', total: 3, resumed: [],
        skipped: [{ slot: 'a', code: 'session_start_repeat' }, { slot: 'b', code: 'slot_approval_pending' }],
        failed: [{ slot: 'c', error: 'RuntimeError' }],
      } }))
    })
    const notice = await waitFor(() => getByTestId('resume-all-problems'))
    const list = Array.from(notice.querySelectorAll('span')).find(el => el.textContent?.startsWith('Parser refactor:'))!
    // Six 16px lines in a 96px box, so the box's edge falls between two lines.
    expect(list.className).toContain('leading-4')
    expect(list.className).toContain('max-h-24')
    // jsdom does no layout: the list fits (0 by 0) until its geometry is stubbed.
    expect(list.className).not.toContain(MORE_BELOW_MASK)
    const scrolledTo = (top: number) => {
      Object.defineProperty(list, 'clientHeight', { configurable: true, get: () => 96 })
      Object.defineProperty(list, 'scrollHeight', { configurable: true, get: () => 160 })
      Object.defineProperty(list, 'scrollTop', { configurable: true, get: () => top })
      act(() => { list.dispatchEvent(new Event('scroll')) })
    }
    scrolledTo(0)
    expect(list.className).toContain(MORE_BELOW_MASK)
    // At the end nothing sits below, so the last line shows at full strength.
    scrolledTo(64)
    expect(list.className).not.toContain(MORE_BELOW_MASK)
  })

  it('a run whose only skips are benign races shows no notice', async () => {
    resumeAll.mockResolvedValue({ ok: true, total: 2, run_id: 'run-b' })
    const { getByTestId, queryByTestId } = renderSidebar([slot('a', { interrupted: true }), slot('b', { interrupted: true })])
    await act(async () => { fireEvent.click(getByTestId('resume-all-button')) })
    act(() => {
      window.dispatchEvent(new CustomEvent(RESUME_ALL_DONE_EVENT, { detail: {
        ok: true, run_id: 'run-b', total: 2, resumed: ['a'], skipped: [{ slot: 'b', code: 'slot_queue_pending' }], failed: [],
      } }))
    })
    await waitFor(() => expect(getByTestId('resume-all-text').textContent).toBe('Resumed 1 session · 1 not resumed'))
    expect(queryByTestId('resume-all-problems')).toBeNull()
  })

  it('withholds the action while the dashboard is offline', async () => {
    const { getByTestId } = renderSidebar([slot('a', { interrupted: true })], false)
    await settle()
    expect(getByTestId('resume-all-button')).toBeDisabled()
    expect(resumeAll).not.toHaveBeenCalled()
  })

  it('names the offline reason in the action\'s title and accessible name', async () => {
    const rows = [slot('a', { interrupted: true }), slot('b', { interrupted: true })]
    const online = renderSidebar(rows)
    await settle()
    const onlineButton = online.getByTestId('resume-all-button')
    expect(onlineButton.getAttribute('title')).toBe('Resume 2 sessions')
    expect(onlineButton.hasAttribute('aria-label')).toBe(false)
    online.unmount()
    const { getByTestId } = renderSidebar(rows, false)
    await settle()
    const button = getByTestId('resume-all-button')
    expect(button.getAttribute('title')).toBe('Gateway offline — reconnect to resume sessions')
    expect(button.getAttribute('aria-label')).toBe('Resume 2 sessions disabled — gateway offline')
    expect(button.getAttribute('aria-disabled')).toBe('true')
  })
})

type CountRow = Pick<ChatSlot, 'mode' | 'surface' | 'running' | 'interrupted' | 'subagents_running' | 'executor' | 'crew_root'>
type CountCase = { name: string; crew_mode?: boolean; slots: { key: string; row: CountRow | null }[]; expected: string[] }

describe('the shared count fixture (test/fixtures/resume_all_candidates.json)', () => {
  // ONE fixture, both suites. The pytest side checks these rows against the
  // server's slots payload for the same facts, and `expected` against the run's
  // candidates and `total`. Here the same rows go through the chat page's
  // surface filter (`pageEnvironment.ts`) into the sidebar, whose count must be
  // that same `expected`.
  for (const c of (countFixture as { cases: CountCase[] }).cases) {
    it(`counts exactly what the server resumes: ${c.name}`, async () => {
      setPreviewFlag(PREVIEW_CREW_MODE, !!c.crew_mode)
      const payload = c.slots.flatMap(({ key, row }) => (row ? [slot(key, row)] : []))
      const page = payload.filter(s => isChatPageSurface(s.surface ?? s.mode))
      // The sidebar counts from `crewModeView`: a worker whose root is listed is out.
      expect(interruptedIdleSlots(crewModeView(page) as Slot[]).map(s => s.key)).toEqual(c.expected)
      const { queryByTestId } = renderSidebar(page)
      await settle()
      if (c.expected.length === 0) {
        expect(queryByTestId('resume-all-row')).toBeNull()
        return
      }
      expect(queryByTestId('resume-all-row')?.getAttribute('data-resume-all-count')).toBe(String(c.expected.length))
      const noun = c.expected.length === 1 ? 'session' : 'sessions'
      expect(queryByTestId('resume-all-button')?.textContent).toBe(`Resume ${c.expected.length} ${noun}`)
      // The press tells the server which count it showed.
      resumeAll.mockResolvedValue({ ok: true, total: 0, run_id: '' })
      fireEvent.click(queryByTestId('resume-all-button')!)
      await waitFor(() => expect(resumeAll).toHaveBeenCalled())
      expect(resumeAll.mock.calls.at(-1)).toEqual([!!c.crew_mode])
    })
  }
})
