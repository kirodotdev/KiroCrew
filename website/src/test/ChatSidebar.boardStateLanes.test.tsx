/**
 * Board state lanes: a column whose `source` is `'state'` files each session by
 * its live runtime state rather than by a tag. The defects this pins are the
 * ones that make a board useless — a session appearing in every column (the
 * match-all fallback an empty `tag_ids` produces), a session appearing in none,
 * and a lane offering a drop target that cannot mean anything.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { render, act, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { sseWorkflowEvent, sseSubagentQueued, sseSubagentSpawn, reconcileSubagentQueuedFromSlots } from '../store/chatSlice'
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
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>(
      (props, ref) => {
        const clean: Record<string, unknown> = {}
        for (const k of Object.keys(props)) {
          if (k === 'children') continue
          if (FRAMER_PROPS.has(k)) continue
          clean[k] = props[k]
        }
        return React.createElement(tag, { ...clean, ref }, props.children)
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
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const { columns, createTagColumn, deleteTagColumn } = vi.hoisted(() => ({
  columns: [
    { id: 'lane-approval', name: '', tag_ids: [] as string[], mode: 'any' as const, order: 0, source: 'state' as const, state_key: 'needs_approval' as const },
    { id: 'lane-waiting', name: '', tag_ids: [] as string[], mode: 'any' as const, order: 1, source: 'state' as const, state_key: 'waiting' as const },
    { id: 'lane-working', name: '', tag_ids: [] as string[], mode: 'any' as const, order: 2, source: 'state' as const, state_key: 'working' as const },
    { id: 'lane-idle', name: '', tag_ids: [] as string[], mode: 'any' as const, order: 3, source: 'state' as const, state_key: 'idle' as const },
  ],
  createTagColumn: vi.fn().mockResolvedValue({ id: 'new' }),
  deleteTagColumn: vi.fn().mockResolvedValue({}),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({}, {
    get: (_t, prop: string) => {
      if (prop === 'chatTags') return () => Promise.resolve([])
      if (prop === 'tagColumns') return () => Promise.resolve(columns)
      if (prop === 'createTagColumn') return createTagColumn
      if (prop === 'deleteTagColumn') return deleteTagColumn
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

const base = { title: 'S', created: '', last_ts: '', tags: [] as string[] }
const approvalSlot = { ...base, key: 'chat-approval', title: 'Owed', running: true, pending_approval: true }
const questionSlot = { ...base, key: 'chat-question', title: 'Asked', running: true, needs_input: true }
const optionsSlot = { ...base, key: 'chat-options', title: 'Options', running: false, has_options: true }
const interruptedSlot = { ...base, key: 'chat-interrupted', title: 'Stalled', running: false, interrupted: true }
const workingSlot = { ...base, key: 'chat-working', title: 'Busy', running: true }
const subagentSlot = { ...base, key: 'chat-subagent', title: 'Fanned out', running: false, subagents_running: true }
const idleSlot = { ...base, key: 'chat-idle', title: 'Quiet', running: false }

const ALL = [approvalSlot, questionSlot, optionsSlot, interruptedSlot, workingSlot, subagentSlot, idleSlot]

function renderSidebar(slots = ALL, prepare?: (store: ReturnType<typeof createTestStore>) => void) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, workflowRuns: {}, subagents: {}, slotActivity: {} } as unknown as RootState['chat'],
  })
  prepare?.(store)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], [])
  return {
    ...render(
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
    ),
    qc,
    store,
  }
}

function slotKeysIn(container: HTMLElement, columnId: string): string[] {
  const col = container.querySelector(`[data-testid="column-${columnId}"]`)
  expect(col, `column ${columnId} missing`).toBeTruthy()
  return Array.from((col as HTMLElement).querySelectorAll('[data-slot-key]'))
    .map(el => el.getAttribute('data-slot-key') as string)
}

function startCardDrag(container: HTMLElement, key: string) {
  const row = container.querySelector(`[data-slot-key="${key}"]`) as HTMLElement
  const source = row?.querySelector('[data-session-row]') as HTMLElement
  expect(source, `no draggable card for ${key}`).toBeTruthy()
  fireEvent.dragStart(source, { dataTransfer: { setData: vi.fn(), types: [] } })
}

beforeEach(() => { localStorage.clear(); createTagColumn.mockClear(); deleteTagColumn.mockClear() })
afterEach(() => vi.clearAllMocks())

describe('board state lanes', () => {
  it('files each session into the lane matching its live state', () => {
    const { container } = renderSidebar()
    expect(slotKeysIn(container, 'lane-approval')).toEqual(['chat-approval'])
    expect(new Set(slotKeysIn(container, 'lane-waiting')))
      .toEqual(new Set(['chat-question', 'chat-options', 'chat-interrupted']))
    expect(new Set(slotKeysIn(container, 'lane-working')))
      .toEqual(new Set(['chat-working', 'chat-subagent']))
    expect(slotKeysIn(container, 'lane-idle')).toEqual(['chat-idle'])
  })

  it('places every session in exactly one lane', () => {
    // The two failures this rules out: a match-all column showing the whole
    // list in every lane, and a session that matches nothing disappearing.
    const { container } = renderSidebar()
    const placed = columns.flatMap(c => slotKeysIn(container, c.id))
    expect(placed.slice().sort()).toEqual(ALL.map(s => s.key).sort())
    expect(new Set(placed).size).toBe(placed.length)
  })

  it('does not offer a tag editor on a lane', () => {
    // A lane has no filter to edit; the tag popover trigger must be absent
    // rather than open onto an empty picker.
    const { container } = renderSidebar()
    expect(container.querySelector('[data-testid="column-edit-lane-working"]')).toBeNull()
  })

  it('refuses a card drop onto a lane', () => {
    // Only reaching the state moves a card there, so the drop must not fire a
    // mutation. `preventDefault` staying unset is what tells the browser this
    // is not a drop target.
    const { container } = renderSidebar()
    const lane = container.querySelector('[data-testid="column-lane-working"]') as HTMLElement
    const dragOver = new Event('dragover', { bubbles: true, cancelable: true }) as Event & { dataTransfer: unknown }
    Object.defineProperty(dragOver, 'dataTransfer', { value: { types: ['text/plain'] } })
    lane.dispatchEvent(dragOver)
    expect(dragOver.defaultPrevented).toBe(false)
  })

  it('says why a lane refuses the card while one is dragged over it', () => {
    // The header's hover title is out of sight mid-drag, so the lane shows the
    // reason in a live line -- and the drop must still stay refused.
    const { container } = renderSidebar()
    const lane = container.querySelector('[data-testid="column-lane-working"]') as HTMLElement
    const hint = () => lane.querySelector('[data-testid="column-derived-hint-lane-working"]')
    expect(hint()).toBeNull()
    startCardDrag(container, 'chat-idle')
    expect(fireEvent.dragOver(lane, { dataTransfer: { types: ['text/plain'] } })).toBe(true)
    expect(hint()?.getAttribute('role')).toBe('status')
    expect(hint()?.getAttribute('aria-live')).toBe('polite')
    expect(hint()?.textContent).toBe('Sessions move into this lane on their own as their state changes')
    // Only the hovered lane speaks.
    expect(container.querySelector('[data-testid="column-derived-hint-lane-idle"]')).toBeNull()
    fireEvent.dragLeave(lane)
    expect(hint()).toBeNull()
  })

  it('hides the lane hint when the drag ends anywhere', () => {
    // A refused drop fires no drop event, so the window's dragend is the end.
    const { container } = renderSidebar()
    const lane = container.querySelector('[data-testid="column-lane-idle"]') as HTMLElement
    const hint = () => lane.querySelector('[data-testid="column-derived-hint-lane-idle"]')
    startCardDrag(container, 'chat-working')
    fireEvent.dragOver(lane, { dataTransfer: { types: ['text/plain'] } })
    expect(hint()).toBeTruthy()
    act(() => { window.dispatchEvent(new Event('dragend')) })
    expect(hint()).toBeNull()
    // The next drag starts clean rather than reviving the old lane's hint.
    startCardDrag(container, 'chat-working')
    expect(hint()).toBeNull()
  })

  it('drops the lane hint while the card is over a folder that takes it', async () => {
    // A folder block inside the lane accepts the card and stops the dragover,
    // so the lane must not keep saying it refuses the card.
    const filed = { ...idleSlot, key: 'chat-filed', title: 'Filed', folder_id: 'f-1' }
    const { container, qc } = renderSidebar([idleSlot, filed])
    const folderSel = '[data-testid="col-lane-idle-folder-f-1"]'
    await waitFor(async () => {
      await act(async () => { qc.setQueryData(['chat-folders'], [{ id: 'f-1', name: 'Folder', collapsed: false, order: 0 }]) })
      expect(container.querySelector(folderSel)).toBeTruthy()
    })
    const lane = container.querySelector('[data-testid="column-lane-idle"]') as HTMLElement
    const hint = () => lane.querySelector('[data-testid="column-derived-hint-lane-idle"]')
    startCardDrag(container, 'chat-idle')
    fireEvent.dragOver(lane, { dataTransfer: { types: ['text/plain'] } })
    expect(hint()).toBeTruthy()
    const folder = container.querySelector(folderSel) as HTMLElement
    expect(fireEvent.dragOver(folder, { dataTransfer: { types: ['text/plain'] } })).toBe(false)
    expect(hint()).toBeNull()
  })

  it('shows no lane hint for a column reorder drag', () => {
    const { container } = renderSidebar()
    const lane = container.querySelector('[data-testid="column-lane-working"]') as HTMLElement
    startCardDrag(container, 'chat-idle')
    fireEvent.dragOver(lane, { dataTransfer: { types: ['application/mc-column'] } })
    expect(lane.querySelector('[data-testid="column-derived-hint-lane-working"]')).toBeNull()
  })

  it('labels each lane by its state, not by a tag or a fallback name', () => {
    // The reported board showed one unnamed "All sessions" column; a lane must
    // name the state it holds.
    const { container } = renderSidebar()
    const working = container.querySelector('[data-testid="column-lane-working"]') as HTMLElement
    expect(working.textContent).toContain('Working')
    expect(working.textContent).not.toContain('All sessions')
  })

  it('moves a session to Working when a dynamic workflow starts on it', async () => {
    // A workflow lives in chatSlice.workflowRuns, never on the slot payload, so
    // `running` stays false while it executes. Two defects hide here and this
    // test must catch BOTH: the lane not reading workflowActive at all, and
    // reading it while omitting it from the columnMatches dependency array.
    // Only a re-render after the store changes exposes the second one -- on a
    // first render the callback closes over current state either way -- so the
    // workflow is dispatched into the SAME store after mounting.
    const slots = [{ ...base, key: 'chat-wf', title: 'Workflow', running: false }]
    const { container, store } = renderSidebar(slots)
    expect(slotKeysIn(container, 'lane-idle')).toEqual(['chat-wf'])

    await act(async () => {
      store.dispatch(sseWorkflowEvent({
        run_id: 'run-1', session_key: 'chat-wf', type: 'run_started', data: { name: 'deep research' },
      }))
    })

    // Polled, not asserted once: the store update re-renders the strip, and on a
    // loaded host that re-render can land after `act` returns. A stale-dependency
    // regression never moves the card at all, so this still fails on the defect
    // rather than merely tolerating it.
    await waitFor(() => expect(slotKeysIn(container, 'lane-working')).toEqual(['chat-wf']))
    expect(slotKeysIn(container, 'lane-idle')).toEqual([])
  })
})

/** A child that has not started is not work: the Working lane comes from the
 *  parent's own turn (or a started child), never from the queued count. */
describe('board state lanes: queued children', () => {
  const queuedOnly = { ...base, key: 'chat-queued', title: 'Queued only', running: false }

  it('never files a session as Working on its queued count alone', () => {
    const { container } = renderSidebar([queuedOnly], store => {
      store.dispatch(sseSubagentQueued({ slot: 'chat-queued', queued: 1, reason: 'low_memory', available_gb: 1.2, required_gb: 2.5 }))
    })
    expect(slotKeysIn(container, 'lane-working')).toEqual([])
    expect(slotKeysIn(container, 'lane-idle')).toEqual(['chat-queued'])
  })

  it('shows the queued children as a Waiting for free RAM badge, not the running pulse', () => {
    const { container } = renderSidebar([queuedOnly], store => {
      store.dispatch(sseSubagentQueued({ slot: 'chat-queued', queued: 2, reason: 'low_memory', available_gb: 1.2, required_gb: 2.5 }))
    })
    const badge = container.querySelector('[data-testid="session-subagents-waiting"]') as HTMLElement
    expect(badge).toBeTruthy()
    expect(badge.textContent).toContain('Waiting for free RAM')
    // The sentence that says what is short rides in the tooltip.
    expect(badge.getAttribute('title')).toContain('free memory')
    expect(badge.querySelector('.animate-pulse, .animate-spin')).toBeNull()
  })

  it('reads the macOS memory-pressure hold as Waiting for free RAM too', () => {
    const { container } = renderSidebar([queuedOnly], store => {
      store.dispatch(sseSubagentQueued({ slot: 'chat-queued', queued: 1, reason: 'memory_pressure' }))
    })
    const badge = container.querySelector('[data-testid="session-subagents-waiting"]') as HTMLElement
    expect(badge.textContent).toContain('Waiting for free RAM')
    expect(slotKeysIn(container, 'lane-idle')).toEqual(['chat-queued'])
  })

  it('keeps a cap wait counted, still without the running pulse', () => {
    const { container } = renderSidebar([queuedOnly], store => {
      store.dispatch(sseSubagentQueued({ slot: 'chat-queued', queued: 3 }))
    })
    const badge = container.querySelector('[data-testid="session-subagents-waiting"]') as HTMLElement
    expect(badge.textContent).toContain('3 agents queued')
    expect(slotKeysIn(container, 'lane-idle')).toEqual(['chat-queued'])
  })

  it('keeps a parent whose own turn is waiting on queued children in Working', () => {
    // The turn is blocked in spawn_sub_agents: that turn is the work.
    const blocked = { ...queuedOnly, running: true }
    const { container } = renderSidebar([blocked], store => {
      store.dispatch(sseSubagentQueued({ slot: 'chat-queued', queued: 1 }))
    })
    expect(slotKeysIn(container, 'lane-working')).toEqual(['chat-queued'])
  })

  it('keeps a session with a started child in Working while more are queued', () => {
    const { container } = renderSidebar([queuedOnly], store => {
      store.dispatch(sseSubagentSpawn({ slot: 'chat-queued', id: 'a1', task: 't', agent: 'kirocrew' }))
      store.dispatch(sseSubagentQueued({ slot: 'chat-queued', queued: 2 }))
    })
    expect(slotKeysIn(container, 'lane-working')).toEqual(['chat-queued'])
    expect(container.querySelector('[data-testid="session-subagents-waiting"]')).toBeNull()
  })

  it('still shows an interrupted turn when its only children are queued', () => {
    // A queued child has not started, so it is no live work that supersedes
    // the interruption: the row keeps its Resume handoff.
    const stalled = { ...queuedOnly, interrupted: true }
    const { container } = renderSidebar([stalled], store => {
      store.dispatch(sseSubagentQueued({ slot: 'chat-queued', queued: 1 }))
    })
    const row = container.querySelector('[data-slot-key="chat-queued"]')
    expect(row?.textContent).toContain('Turn interrupted')
  })

  it('clears a ghost queued count on a session that is not open when a slots push says 0', async () => {
    // The ghost: the client kept "1 queued" after the gateway published 0 (a
    // missed frame). The next slots push carries the published depth for EVERY
    // row, so the background session is corrected without being opened.
    const { container, store } = renderSidebar([queuedOnly], st => {
      st.dispatch(sseSubagentQueued({ slot: 'chat-queued', queued: 1 }))
    })
    expect(container.querySelector('[data-testid="session-subagents-waiting"]')).toBeTruthy()

    await act(async () => {
      store.dispatch(reconcileSubagentQueuedFromSlots([{ ...queuedOnly, subagents_queued: 0 }]))
    })

    await waitFor(() => expect(container.querySelector('[data-testid="session-subagents-waiting"]')).toBeNull())
    expect(store.getState().chat.subagentQueued?.['chat-queued']).toBeUndefined()
    expect(slotKeysIn(container, 'lane-idle')).toEqual(['chat-queued'])
  })
})
