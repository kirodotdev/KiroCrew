import { renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatSlot } from '../../../types'
import {
  hasPlaceholderTitle,
  isEmptyNewSlot,
  prepareCurrentSlots,
  sessionStatus,
  shouldShowHistorySession,
  useRecentsProvider,
  type HistorySession,
} from './recentsProvider'

type MockState = {
  dashboard: { slots: ChatSlot[]; unreadSlots: string[] }
  chat: {
    slotStatusDetail: Record<string, { kind?: string; text?: string; toolName?: string; ts?: number }>
  }
}

const mocks = vi.hoisted(() => ({
  state: {
    dashboard: { slots: [], unreadSlots: [] },
    chat: { slotStatusDetail: {} },
  } as MockState,
  dispatch: vi.fn(),
  navigate: vi.fn(),
  fetchQuery: vi.fn(),
}))

vi.mock('../../../store', () => ({
  useAppDispatch: () => mocks.dispatch,
  useAppSelector: (selector: (state: MockState) => unknown) => selector(mocks.state),
}))

vi.mock('react-router-dom', () => ({
  useNavigate: () => mocks.navigate,
}))

vi.mock('@tanstack/react-query', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@tanstack/react-query')>()
  return {
    ...actual,
    useQueryClient: () => ({ fetchQuery: mocks.fetchQuery }),
  }
})

/**
 * Unit tests for the pure slot/history filtering helpers behind the recents
 * quick-switcher (duplicate "+ New Session…" rows on empty query).
 * Exercises only the exported pure functions — no React hooks, React-Query,
 * or Redux.
 */

function slot(over: Partial<ChatSlot> = {}): ChatSlot {
  return {
    key: over.key ?? 'dashboard_s1',
    title: 'Session One',
    messages: 4,
    running: false,
    ...over,
  } as ChatSlot
}

describe('hasPlaceholderTitle', () => {
  it('matches both ellipsis spellings of the placeholder', () => {
    expect(hasPlaceholderTitle(slot({ title: 'New Session…' }))).toBe(true)
    expect(hasPlaceholderTitle(slot({ title: 'New Session...' }))).toBe(true)
  })

  it('does not match user titles that merely start with the placeholder', () => {
    expect(hasPlaceholderTitle(slot({ title: 'New Session Planning' }))).toBe(false)
  })

  it('does not match a normal or missing title', () => {
    expect(hasPlaceholderTitle(slot({ title: 'Tax return' }))).toBe(false)
    expect(hasPlaceholderTitle(slot({ title: undefined }))).toBe(false)
  })
})

describe('isEmptyNewSlot', () => {
  it('is true only for a placeholder title with zero messages', () => {
    expect(isEmptyNewSlot(slot({ title: 'New Session…', messages: 0 }))).toBe(true)
  })

  it('is false for an untitled slot that already has messages', () => {
    expect(isEmptyNewSlot(slot({ title: 'New Session…', messages: 3 }))).toBe(false)
  })

  it('is false for a titled slot with zero messages', () => {
    expect(isEmptyNewSlot(slot({ title: 'Tax return', messages: 0 }))).toBe(false)
  })

  it('treats a missing messages count as empty', () => {
    expect(
      isEmptyNewSlot(slot({ title: 'New Session…', messages: undefined as unknown as number })),
    ).toBe(true)
  })
})

describe('prepareCurrentSlots — duplicate "+ New Session…" collapse', () => {
  it('keeps at most one empty-new slot when several are open', () => {
    const slots = [
      slot({ key: 'a', title: 'New Session…', messages: 0, last_ts: '2026-07-22T10:00:00Z' }),
      slot({ key: 'b', title: 'New Session…', messages: 0, last_ts: '2026-07-22T12:00:00Z' }),
      slot({ key: 'c', title: 'New Session…', messages: 0, last_ts: '2026-07-22T11:00:00Z' }),
      slot({ key: 'd', title: 'Tax return' }),
    ]
    const { ordered, hasEmptyNew } = prepareCurrentSlots(slots)
    expect(hasEmptyNew).toBe(true)
    const emptyNew = ordered.filter((s) => isEmptyNewSlot(s))
    expect(emptyNew).toHaveLength(1)
    // The most recent empty-new slot survives.
    expect(emptyNew[0].key).toBe('b')
    // The real session is untouched.
    expect(ordered.map((s) => s.key)).toContain('d')
    expect(ordered).toHaveLength(2)
  })

  it('keeps an untitled slot with messages as a normal row, not a create affordance', () => {
    const slots = [
      slot({ key: 'a', title: 'New Session…', messages: 5 }),
      slot({ key: 'b', title: 'New Session…', messages: 0 }),
    ]
    const { ordered, hasEmptyNew } = prepareCurrentSlots(slots)
    expect(hasEmptyNew).toBe(true)
    expect(ordered).toHaveLength(2)
    expect(ordered.filter(isEmptyNewSlot)).toHaveLength(1)
  })

  it('reports hasEmptyNew=false when no empty untitled slot exists', () => {
    const { ordered, hasEmptyNew } = prepareCurrentSlots([
      slot({ key: 'a', title: 'Tax return' }),
      slot({ key: 'b', title: 'New Session…', messages: 2 }),
    ])
    expect(hasEmptyNew).toBe(false)
    expect(ordered).toHaveLength(2)
  })

  it('orders empty-new first, then pinned, then recency', () => {
    const slots = [
      slot({ key: 'old', title: 'Old', last_ts: '2026-07-20T10:00:00Z' }),
      slot({ key: 'pin', title: 'Pinned', pinned: true, last_ts: '2026-07-19T10:00:00Z' }),
      slot({ key: 'new', title: 'New Session…', messages: 0 }),
      slot({ key: 'recent', title: 'Recent', last_ts: '2026-07-22T10:00:00Z' }),
    ]
    const { ordered } = prepareCurrentSlots(slots)
    expect(ordered.map((s) => s.key)).toEqual(['new', 'pin', 'recent', 'old'])
  })

  it('handles the empty slot list', () => {
    const { ordered, hasEmptyNew } = prepareCurrentSlots([])
    expect(ordered).toEqual([])
    expect(hasEmptyNew).toBe(false)
  })
})

describe('shouldShowHistorySession — dead "New Session…" rows in Older', () => {
  function hist(over: Partial<HistorySession> = {}): HistorySession {
    return { key: 'h1', ...over }
  }

  it('drops a placeholder-titled session with no preview', () => {
    expect(shouldShowHistorySession(hist({ title: 'New Session…' }))).toBe(false)
    expect(shouldShowHistorySession(hist({ title: 'New Session...' }))).toBe(false)
  })

  it('drops a blank-titled session with no preview', () => {
    expect(shouldShowHistorySession(hist({ title: '' }))).toBe(false)
    expect(shouldShowHistorySession(hist({ title: '   ' }))).toBe(false)
    expect(shouldShowHistorySession(hist({}))).toBe(false)
  })

  it('keeps a placeholder-titled session that has a preview (real content)', () => {
    expect(
      shouldShowHistorySession(hist({ title: 'New Session…', preview: 'Hey, what can I…' })),
    ).toBe(true)
  })

  it('keeps normally titled sessions regardless of preview', () => {
    expect(shouldShowHistorySession(hist({ title: 'Tax return' }))).toBe(true)
    expect(shouldShowHistorySession(hist({ title: 'Tax return', preview: 'x' }))).toBe(true)
  })

  it('treats a whitespace-only preview as no preview', () => {
    expect(shouldShowHistorySession(hist({ title: 'New Session…', preview: '  ' }))).toBe(false)
  })
})


describe('sessionStatus — running detail', () => {
  it('surfaces the live tool call instead of the generic Thinking label', () => {
    expect(
      sessionStatus(slot({ running: true }), [], {
        text: 'Running: read /workspace/src/app.ts',
      }),
    ).toMatchObject({
      style: 'dot',
      pulse: true,
      label: 'Running: read /workspace/src/app.ts',
    })
  })

  it('falls back to Thinking when no live status detail has arrived', () => {
    expect(sessionStatus(slot({ running: true }), [])).toMatchObject({
      style: 'dot',
      pulse: true,
      label: 'Thinking…',
    })
  })

  it('shows the raw tool title when simplifiedToolNames is off', () => {
    // Same preference the inline tool pill obeys, so the palette row and the
    // transcript agree instead of the row always showing the purpose.
    const detail = { kind: 'tool', text: 'Reading the app entrypoint', toolName: 'fs_read /workspace/src/app.ts' }
    expect(sessionStatus(slot({ running: true }), [], detail, false)).toMatchObject({
      label: 'fs_read /workspace/src/app.ts',
    })
    expect(sessionStatus(slot({ running: true }), [], detail, true)).toMatchObject({
      label: 'Reading the app entrypoint',
    })
  })
})

describe('useRecentsProvider — live status bridge', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
    mocks.state.dashboard.slots = []
    mocks.state.dashboard.unreadSlots = []
    mocks.state.chat.slotStatusDetail = {}
    mocks.fetchQuery.mockImplementation(({ queryKey }: { queryKey: unknown[] }) =>
      Promise.resolve(queryKey[2] === 'history' ? { sessions: [] } : []),
    )
  })

  it('maps Redux slot status detail onto the current result row', async () => {
    mocks.state.dashboard.slots = [slot({ key: 'dashboard_live', running: true })]
    mocks.state.chat.slotStatusDetail = {
      dashboard_live: {
        kind: 'tool',
        text: 'Running: read /workspace/src/app.ts',
        ts: 123,
      },
    }

    const { result } = renderHook(() => useRecentsProvider())
    const rows = await result.current.search('')

    expect(rows.find((row) => row.id === 'recents:cur:dashboard_live')).toMatchObject({
      statusStyle: 'dot',
      statusPulse: true,
      statusLabel: 'Running: read /workspace/src/app.ts',
    })
  })

  it('renders the raw tool title when the user turned simplified tool names off', async () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ simplifiedToolNames: false }))
    mocks.state.dashboard.slots = [slot({ key: 'dashboard_live', running: true })]
    mocks.state.chat.slotStatusDetail = {
      dashboard_live: {
        kind: 'tool',
        text: 'Reading the app entrypoint',
        toolName: 'fs_read /workspace/src/app.ts',
        ts: 123,
      },
    }

    const { result } = renderHook(() => useRecentsProvider())
    const rows = await result.current.search('')

    expect(rows.find((row) => row.id === 'recents:cur:dashboard_live')).toMatchObject({
      statusLabel: 'fs_read /workspace/src/app.ts',
    })
  })
})
