/**
 * recentsProvider — the palette's unscoped quick-switcher view.
 *
 * The pure exports (ordering, dedupe keys, row-worthiness, status treatment)
 * are asserted directly; the hook's `search()` is then driven end-to-end with a
 * mocked backend so the three groups, their fallbacks, and the row activations
 * are all exercised. The dedupe between live slot keys and history keys is the
 * asymmetric part worth pinning: `dashboard_` comes off, a channel-born
 * `slack_<ts>` must pass through untouched.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const navigateSpy = vi.hoisted(() => vi.fn())
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useNavigate: () => navigateSpy,
}))

const apiMock = vi.hoisted(() => ({
  sessions: vi.fn(),
  crons: vi.fn(),
  chatFolders: vi.fn(),
}))
vi.mock('../../../api/client', () => ({ api: apiMock }))

const thunks = vi.hoisted(() => ({
  createSlot: vi.fn(() => () => ({ unwrap: () => Promise.resolve('zzq-new') })),
  switchSlot: vi.fn((key: string) => ({ type: 'zzq/switchSlot', payload: key })),
  resumeFromHistory: vi.fn((arg: unknown) => ({ type: 'zzq/resume', payload: arg })),
}))
vi.mock('../../../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...thunks,
}))

import { createTestStore } from '../../../test/helpers'
import { sseSlots, markSlotUnread } from '../../../store/dashboardSlice'
import type { ChatSlot, CronJob } from '../../../types'
import type { Result } from '../types'
import {
  hasPlaceholderTitle,
  isEmptyNewSlot,
  normalizeKey,
  prepareCurrentSlots,
  sessionStatus,
  shouldShowHistorySession,
  useRecentsProvider,
} from './recentsProvider'

const NOW_MS = Date.UTC(2024, 4, 15, 12, 0, 0)

function slot(patch: Partial<ChatSlot> & { key: string }): ChatSlot {
  return { messages: 1, running: false, ...patch } as ChatSlot
}

function harness(live: ChatSlot[], unread: string[] = []) {
  const store = createTestStore()
  store.dispatch(sseSlots(live))
  for (const key of unread) store.dispatch(markSlotUnread(key))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <Provider store={store}>{children}</Provider>
    </QueryClientProvider>
  )
  return { store, ...renderHook(() => useRecentsProvider(), { wrapper }) }
}

const group = (rows: Result[], label: string) => rows.filter((r) => r.groupLabel === label)

beforeEach(() => {
  navigateSpy.mockClear()
  Object.values(thunks).forEach((t) => t.mockClear())
  apiMock.sessions.mockReset().mockResolvedValue({ sessions: [] })
  apiMock.crons.mockReset().mockResolvedValue([])
  apiMock.chatFolders.mockReset().mockResolvedValue([])
})

afterEach(() => {
  vi.useRealTimers()
})

describe('hasPlaceholderTitle / isEmptyNewSlot', () => {
  it.each(['New Session…', 'New Session...'])('treats %s as a placeholder', (title) => {
    expect(hasPlaceholderTitle(slot({ key: 'zzq', title }))).toBe(true)
  })

  it('does not misclassify a user-named session with the same prefix', () => {
    expect(hasPlaceholderTitle(slot({ key: 'zzq', title: 'New Session Planning' }))).toBe(false)
  })

  it('an untitled slot with messages is a real conversation, not the create row', () => {
    expect(isEmptyNewSlot(slot({ key: 'zzq', title: 'New Session…', messages: 3 }))).toBe(false)
    expect(isEmptyNewSlot(slot({ key: 'zzq', title: 'New Session…', messages: 0 }))).toBe(true)
  })
})

describe('prepareCurrentSlots', () => {
  it('orders empty-new first, then pinned, then recency', () => {
    const { ordered, hasEmptyNew } = prepareCurrentSlots([
      slot({ key: 'zzq-old', last_activity_ts: 1_000 }),
      slot({ key: 'zzq-pinned', pinned: true, last_activity_ts: 500 }),
      slot({ key: 'zzq-new', title: 'New Session…', messages: 0 }),
      slot({ key: 'zzq-recent', last_activity_ts: 2_000 }),
    ])
    expect(ordered.map((s) => s.key)).toEqual(['zzq-new', 'zzq-pinned', 'zzq-recent', 'zzq-old'])
    expect(hasEmptyNew).toBe(true)
  })

  it('keeps only one empty-new slot', () => {
    const { ordered } = prepareCurrentSlots([
      slot({ key: 'zzq-new-a', title: 'New Session…', messages: 0, last_activity_ts: 100 }),
      slot({ key: 'zzq-new-b', title: 'New Session...', messages: 0, last_activity_ts: 900 }),
    ])
    expect(ordered.map((s) => s.key)).toEqual(['zzq-new-b'])
  })

  it('reports no empty-new slot when every session has messages', () => {
    expect(prepareCurrentSlots([slot({ key: 'zzq-a' })]).hasEmptyNew).toBe(false)
  })

  it('sorts an unparseable or missing recency stamp to the bottom', () => {
    const { ordered } = prepareCurrentSlots([
      slot({ key: 'zzq-broken', created: 'zzq-not-a-date' }),
      slot({ key: 'zzq-none' }),
      slot({ key: 'zzq-dated', last_ts: '2024-05-15T11:00:00Z' }),
    ])
    expect(ordered[0].key).toBe('zzq-dated')
  })
})

describe('shouldShowHistorySession', () => {
  it('drops a session that never got past creation', () => {
    expect(shouldShowHistorySession({ key: 'zzq', title: 'New Session…' })).toBe(false)
    expect(shouldShowHistorySession({ key: 'zzq', title: '  ', preview: ' ' })).toBe(false)
  })

  it('keeps a placeholder-titled session that has a preview', () => {
    expect(shouldShowHistorySession({ key: 'zzq', title: 'New Session…', preview: 'zzq hi' })).toBe(true)
  })

  it('keeps any named session', () => {
    expect(shouldShowHistorySession({ key: 'zzq', title: 'zzq real' })).toBe(true)
  })
})

describe('normalizeKey', () => {
  it('strips the dashboard_ prefix so live and history keys compare equal', () => {
    expect(normalizeKey('dashboard_chat-1-1')).toBe('chat-1-1')
  })

  it('leaves a channel-born key untouched (both sides already agree)', () => {
    expect(normalizeKey('slack_1700000000')).toBe('slack_1700000000')
  })
})

describe('sessionStatus', () => {
  it('ranks a pending approval highest', () => {
    const st = sessionStatus(slot({ key: 'zzq', pending_approval: true, last_message: 'zzq ask' }), [])
    expect(st).toMatchObject({ style: 'pill', colorVar: '--warn', detail: 'zzq ask' })
  })

  it('ranks a blocking question next, above Thinking', () => {
    const st = sessionStatus(slot({ key: 'zzq', needs_input: true, running: true }), [])
    expect(st).toMatchObject({ style: 'pill', colorVar: '--info' })
  })

  it('shows a pulsing dot while running', () => {
    const st = sessionStatus(slot({ key: 'zzq', running: true }), [])
    expect(st).toMatchObject({ style: 'dot', colorVar: '--accent', pulse: true })
    expect(st.label).toBeTruthy()
  })

  it('prefers a live tool label over the generic Thinking copy', () => {
    const st = sessionStatus(slot({ key: 'zzq', running: true }), [], {
      kind: 'tool',
      toolName: 'fs_read',
      text: 'zzq reading',
    })
    expect(st.label).toBeTruthy()
  })

  it('marks an unread session with a right-hand dot', () => {
    const st = sessionStatus(slot({ key: 'zzq', last_message: 'zzq hi' }), ['zzq'])
    expect(st).toMatchObject({ rightDot: { colorVar: '--info' }, subtitle: 'zzq hi' })
  })

  it('marks a your-turn session the same way', () => {
    const st = sessionStatus(slot({ key: 'zzq', waiting_for_input: true }), [])
    expect(st.rightDot).toEqual({ colorVar: '--info' })
  })

  it('falls back to the prompt preview, truncated at 80 chars', () => {
    const st = sessionStatus(slot({ key: 'zzq', prompt_preview: `zzq ${'a'.repeat(120)}` }), [])
    expect(st.subtitle?.endsWith('…')).toBe(true)
    expect(st.subtitle?.length).toBeLessThanOrEqual(81)
  })

  it('collapses whitespace and reports no subtitle for an empty session', () => {
    expect(sessionStatus(slot({ key: 'zzq' }), []).subtitle).toBeUndefined()
    expect(sessionStatus(slot({ key: 'zzq', last_message: 'zzq   a\n b' }), []).subtitle).toBe('zzq a b')
  })
})

describe('useRecentsProvider — identity', () => {
  it('describes itself as the recents provider', () => {
    const { result } = harness([])
    expect(result.current.id).toBe('recents')
    expect(result.current.label).toBeTruthy()
  })
})

describe('useRecentsProvider — Current group', () => {
  it('renders a row per live slot with status, folder and pin', async () => {
    apiMock.chatFolders.mockResolvedValue([{ id: 'zzq-f1', name: 'zzq Papers' }])
    const { result } = harness(
      [slot({ key: 'zzq-a', title: 'zzq A', pinned: true, folder_id: 'zzq-f1', last_message: 'zzq hi' })],
      ['zzq-a'],
    )
    const rows = await result.current.search('')
    const current = rows.filter((r) => r.id.startsWith('recents:cur:'))
    expect(current).toHaveLength(1)
    expect(current[0]).toMatchObject({
      title: 'zzq A',
      providerId: 'recents',
      pinned: true,
      folder: 'zzq Papers',
      subtitle: 'zzq hi',
    })
  })

  it('activating a current row switches to the slot and lands on chat', async () => {
    const { result } = harness([slot({ key: 'zzq-a', title: 'zzq A' })])
    const rows = await result.current.search('')
    rows.find((r) => r.id === 'recents:cur:zzq-a')?.onActivate?.()
    expect(thunks.switchSlot).toHaveBeenCalledWith('zzq-a')
    expect(navigateSpy).toHaveBeenCalledWith('/chat')
  })

  it('synthesizes a create row when no empty untitled slot exists', async () => {
    const { result } = harness([slot({ key: 'zzq-a', title: 'zzq A' })])
    const rows = await result.current.search('')
    expect(rows[0]).toMatchObject({ id: 'recents:new-session', isNew: true })
  })

  it('creates the session before navigating when the create row is activated', async () => {
    const { result } = harness([slot({ key: 'zzq-a', title: 'zzq A' })])
    const rows = await result.current.search('')
    rows[0].onActivate?.()
    expect(thunks.createSlot).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/chat'))
  })

  it('lets an existing empty untitled slot BE the create row (no synthetic row)', async () => {
    const { result } = harness([slot({ key: 'zzq-new', title: 'New Session…', messages: 0 })])
    const rows = await result.current.search('')
    expect(rows.filter((r) => r.id === 'recents:new-session')).toHaveLength(0)
    expect(rows[0]).toMatchObject({ id: 'recents:cur:zzq-new', isNew: true })
    // The bare create affordance carries no status/preview/timestamp chrome.
    expect(rows[0].subtitle).toBeUndefined()
    expect(rows[0].timestamp).toBeUndefined()
    expect(rows[0].pinned).toBeUndefined()
  })
})

describe('useRecentsProvider — Planned group', () => {
  const job = (patch: Partial<CronJob> & { id: string }): CronJob =>
    ({ name: `zzq ${patch.id}`, enabled: true, ...patch }) as CronJob

  it('lists enabled chat-visible crons soonest first, capped at six', async () => {
    apiMock.crons.mockResolvedValue([
      job({ id: 'zzq-1', next_run_ts: 300, schedule: 'zzq daily' }),
      job({ id: 'zzq-2', next_run_ts: 100 }),
      job({ id: 'zzq-off', enabled: false, next_run_ts: 1 }),
      job({ id: 'zzq-hidden', hide_in_chat: true, next_run_ts: 2 }),
      ...[3, 4, 5, 6, 7, 8].map((n) => job({ id: `zzq-${n}`, next_run_ts: 400 + n })),
    ])
    const { result } = harness([])
    const planned = group(await result.current.search(''), 'Scheduled')
    expect(planned).toHaveLength(6)
    expect(planned[0].id).toBe('recents:plan:zzq-2')
    expect(planned[1].id).toBe('recents:plan:zzq-1')
    expect(planned[1].subtitle).toBe('zzq daily')
    expect(planned.some((r) => r.id.includes('zzq-off') || r.id.includes('zzq-hidden'))).toBe(false)
  })

  it('sorts a cron with no next run last and falls back to the agent as subtitle', async () => {
    apiMock.crons.mockResolvedValue({
      jobs: [job({ id: 'zzq-never', agent: 'zzq-agent' }), job({ id: 'zzq-soon', next_run_ts: 5 })],
    })
    const { result } = harness([])
    const planned = group(await result.current.search(''), 'Scheduled')
    expect(planned.map((r) => r.id)).toEqual(['recents:plan:zzq-soon', 'recents:plan:zzq-never'])
    expect(planned[1].subtitle).toBe('zzq-agent')
  })

  it('activating a planned row opens the schedule page', async () => {
    apiMock.crons.mockResolvedValue([job({ id: 'zzq-1', next_run_ts: 5 })])
    const { result } = harness([])
    const planned = group(await result.current.search(''), 'Scheduled')
    planned[0].onActivate?.()
    expect(navigateSpy).toHaveBeenCalledWith('/schedule')
  })
})

describe('useRecentsProvider — Older group', () => {
  it('renders archived sessions faded, deduped against live slots', async () => {
    apiMock.sessions.mockResolvedValue({
      sessions: [
        { key: 'dashboard_zzq-a', title: 'zzq A' }, // already live → dropped
        { key: 'slack_zzq-chan', title: 'zzq channel' }, // live under the same key → dropped
        { key: 'dashboard_zzq-old', title: 'zzq Old', preview: 'zzq body', folder_id: 'zzq-f1' },
        { key: 'zzq-dead', title: 'New Session…' }, // never got past creation → dropped
      ],
    })
    apiMock.chatFolders.mockResolvedValue([{ id: 'zzq-f1', name: 'zzq Papers' }])
    const { result } = harness([slot({ key: 'zzq-a' }), slot({ key: 'slack_zzq-chan' })])
    const older = (await result.current.search('')).filter((r) => r.id.startsWith('recents:old:'))
    expect(older).toHaveLength(1)
    expect(older[0]).toMatchObject({
      id: 'recents:old:dashboard_zzq-old',
      title: 'zzq Old',
      subtitle: 'zzq body',
      faded: true,
      folder: 'zzq Papers',
    })
  })

  it('resumes an archived session on activation', async () => {
    apiMock.sessions.mockResolvedValue({ sessions: [{ key: 'zzq-old', title: 'zzq Old' }] })
    const { result } = harness([])
    const older = (await result.current.search('')).filter((r) => r.id.startsWith('recents:old:'))
    older[0].onActivate?.()
    expect(thunks.resumeFromHistory).toHaveBeenCalledWith({ key: 'zzq-old', title: 'zzq Old' })
    expect(navigateSpy).toHaveBeenCalledWith('/chat')
  })

  it('falls back to the key when a history row has no title', async () => {
    apiMock.sessions.mockResolvedValue({ sessions: [{ key: 'zzq-keyonly', preview: 'zzq body' }] })
    const { result } = harness([])
    const older = (await result.current.search('')).filter((r) => r.id.startsWith('recents:old:'))
    expect(older[0].title).toBe('zzq-keyonly')
  })
})

describe('useRecentsProvider — timestamps', () => {
  it('formats today as a clock time and older rows as dates', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    apiMock.sessions.mockResolvedValue({
      sessions: [
        { key: 'zzq-today', title: 'zzq today', modified: NOW_MS / 1000 - 3600 },
        { key: 'zzq-yesterday', title: 'zzq yesterday', modified: NOW_MS / 1000 - 26 * 3600 },
        { key: 'zzq-thisweek', title: 'zzq week', modified: NOW_MS / 1000 - 3 * 86400 },
        { key: 'zzq-thisyear', title: 'zzq year', modified: NOW_MS / 1000 - 60 * 86400 },
        { key: 'zzq-lastyear', title: 'zzq old', modified: NOW_MS / 1000 - 400 * 86400 },
        { key: 'zzq-nostamp', title: 'zzq none' },
      ],
    })
    const { result } = harness([])
    const rows = (await result.current.search('')).filter((r) => r.id.startsWith('recents:old:'))
    const stamp = (key: string) => rows.find((r) => r.id === `recents:old:${key}`)?.timestamp

    // Clock format follows the app language (en → 12-hour), so match loosely.
    expect(stamp('zzq-today')).toMatch(/^11:00/)
    expect(stamp('zzq-yesterday')).toMatch(/10:00/)
    expect(stamp('zzq-yesterday')?.toLowerCase()).toContain('yesterday')
    expect(stamp('zzq-thisweek')).toBe('Sun')
    expect(stamp('zzq-thisyear')).toBe('Mar 16')
    expect(stamp('zzq-lastyear')).toBe('Apr 11, 2023')
    expect(stamp('zzq-nostamp')).toBeUndefined()
  })

  it('reports no timestamp for an unparseable stamp', async () => {
    apiMock.sessions.mockResolvedValue({
      sessions: [{ key: 'zzq-bad', title: 'zzq bad', modified: -1 }],
    })
    const { result } = harness([])
    const rows = (await result.current.search('')).filter((r) => r.id.startsWith('recents:old:'))
    expect(rows[0].timestamp).toBeUndefined()
  })
})

describe('useRecentsProvider — backend failures', () => {
  it('still renders the live slots when every fetch fails', async () => {
    apiMock.sessions.mockRejectedValue(new Error('zzq offline'))
    apiMock.crons.mockRejectedValue(new Error('zzq offline'))
    apiMock.chatFolders.mockRejectedValue(new Error('zzq offline'))
    const { result } = harness([slot({ key: 'zzq-a', title: 'zzq A', folder_id: 'zzq-f1' })])
    const rows = await result.current.search('')
    expect(rows.map((r) => r.id)).toEqual(['recents:new-session', 'recents:cur:zzq-a'])
    expect(rows[1].folder).toBeUndefined()
  })

  it('tolerates a non-array folders payload', async () => {
    apiMock.chatFolders.mockResolvedValue({ zzq: 'not an array' } as never)
    const { result } = harness([slot({ key: 'zzq-a', title: 'zzq A', folder_id: 'zzq-f1' })])
    const rows = await result.current.search('')
    expect(rows.find((r) => r.id === 'recents:cur:zzq-a')?.folder).toBeUndefined()
  })

  it('tolerates a history payload with no sessions field', async () => {
    apiMock.sessions.mockResolvedValue({} as never)
    const { result } = harness([])
    const rows = await result.current.search('')
    expect(rows.filter((r) => r.id.startsWith('recents:old:'))).toHaveLength(0)
  })
})
