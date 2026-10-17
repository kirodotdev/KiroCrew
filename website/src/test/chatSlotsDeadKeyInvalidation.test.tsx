/**
 * Regression for #10204: a `queryClient.invalidateQueries({ queryKey:
 * ['chat-slots'] })` call refreshes nothing, and two mutation handlers still
 * ended with one.
 *
 * ## The mechanism
 *
 * `invalidateQueries` marks the queries a key matches as stale and refetches
 * the ACTIVE ones. Nothing in the dashboard registers a query on a
 * `['chat-slots']`-prefixed key: the only keys that match are one-shot
 * `fetchQuery` reads with `gcTime: 0` and no observers (`useSessionActions`'s
 * pin-reconcile snapshot, `ChatSidebar` and `SessionTitleControl`'s rename
 * recovery), which are discarded as they settle and are never "active". The
 * list a slot row actually renders from is the Redux `dashboard` slice, fed by
 * the websocket `sseSlots` frame and the `fetchSlots()` thunk. So the call
 * traverses an empty match set and returns -- silently, with no error and no
 * request. `issueRadarPolling.test.tsx` pins the same absence from the cache
 * side (`getQueryDefaults(['chat-slots']).gcTime` is undefined).
 *
 * Six such call sites accumulated. Three were removed as their surrounding
 * defects were fixed (#8185, and #10172 for the two ChatSidebar ones); the two
 * this test covers were the remainder, and each is a DELETION rather than a
 * replacement because the refresh it reached for already arrives:
 *
 *  - `TagManagerList`'s tag delete -- `api_chat_tag_delete` strips the id from
 *    every slot and then calls `push_slots_update()` on its one success path.
 *  - `useSessionActions`'s pin-reconcile fallback -- every accepted
 *    `PATCH /api/chat/slots/{slot}/pin` ends in `push_slots_update()`, and this
 *    branch is reached only when the client's own re-read already failed.
 *
 * ## Why both a behavioural test and a source ratchet
 *
 * The behavioural halves assert what each handler DOES: the exact keys it
 * invalidates, observed on the real `QueryClient` the component was handed. A
 * dead key is invisible in rendered output -- that is the whole defect -- so
 * the recorded key list is the only place the difference shows.
 *
 * Those two tests cannot see a SEVENTH site in a file nobody wrote a test for,
 * which is how the first six arrived: one plausible-looking line at a time,
 * each in a different handler. Hence the tree scan, in the shape
 * `MemoI18nSubscriptionRatchet.test.ts` established. Its predicate gets its own
 * fixture suite, so a regex tightened against today's tree cannot quietly stop
 * matching the defect and leave a green no-op behind.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fireEvent, render, renderHook, act, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import type { ReactNode } from 'react'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({
  chatTags: vi.fn(),
  deleteChatTag: vi.fn(),
  setSlotPin: vi.fn(),
  chatSlots: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

import TagManagerList from '../components/TagManagerList'
import { store } from '../store'
import { sseSlots, updateSlotPin } from '../store/dashboardSlice'
import { useSessionActions } from '../hooks/useSessionActions'

/** A client whose `invalidateQueries` records every key it was handed. */
function recordingClient(): { qc: QueryClient; keys: () => unknown[][] } {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const seen: unknown[][] = []
  const real = qc.invalidateQueries.bind(qc)
  vi.spyOn(qc, 'invalidateQueries').mockImplementation((filters?: { queryKey?: unknown }) => {
    if (Array.isArray(filters?.queryKey)) seen.push(filters.queryKey as unknown[])
    return real(filters as Parameters<typeof real>[0])
  })
  return { qc, keys: () => seen }
}

/** Keys whose first element names the dead `chat-slots` cache. */
const deadKeys = (keys: unknown[][]) => keys.filter(k => k[0] === 'chat-slots')

beforeEach(() => {
  mocks.chatTags.mockResolvedValue([{ id: 't1', name: 'Alpha', color: '#ff0000', order: 0 }])
  mocks.deleteChatTag.mockResolvedValue({ ok: true })
  mocks.setSlotPin.mockResolvedValue({})
  mocks.chatSlots.mockResolvedValue([])
})
afterEach(() => {
  vi.clearAllMocks()
  vi.restoreAllMocks()
  store.dispatch(sseSlots([]))
})

describe('tag delete invalidates only caches a query is registered on', () => {
  it('refreshes chat-tags and tag-columns, and names no chat-slots key', async () => {
    const { qc, keys } = recordingClient()
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(
      <QueryClientProvider client={qc}>
        <TagManagerList mode="manage" />
      </QueryClientProvider>,
    )
    fireEvent.click(await screen.findByTestId('tag-delete-t1'))
    await waitFor(() => expect(mocks.deleteChatTag).toHaveBeenCalledWith('t1'))
    await waitFor(() => expect(keys().length).toBeGreaterThanOrEqual(2))

    expect(keys()).toEqual([['chat-tags'], ['tag-columns']])
    expect(
      deadKeys(keys()),
      'the un-tagged slot rows arrive on the authoritative frame api_chat_tag_delete pushes; ' +
      'a plain chat-slots invalidation refreshes nothing (#10204)',
    ).toEqual([])
    // And it reaches for the list no other way either: the handler must not
    // trade the dead invalidate for a whole-list GET.
    expect(mocks.chatSlots).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })
})

describe('pin reconcile fallback attempts no dead re-read', () => {
  const SLOT = 'chat-dead-key-pin-1'

  it('names no chat-slots key when the reconcile snapshot read fails', async () => {
    // The fallback branch: the PATCH is accepted, then the client's own
    // snapshot re-read throws, so reconciliation runs off local state.
    mocks.chatSlots.mockRejectedValue(new Error('snapshot unavailable'))
    const slot: ChatSlot = { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '' }
    store.dispatch(sseSlots([slot]))
    store.dispatch(updateSlotPin({ key: SLOT, pinned: false }))

    const { qc, keys } = recordingClient()
    const wrapper = ({ children }: { children: ReactNode }) => (
      <Provider store={store}><QueryClientProvider client={qc}>{children}</QueryClientProvider></Provider>
    )
    const { result } = renderHook(() => useSessionActions('personal'), { wrapper })
    act(() => result.current.togglePin(SLOT))

    await waitFor(() => expect(mocks.setSlotPin).toHaveBeenCalledWith(SLOT, true))
    await waitFor(() => expect(mocks.chatSlots).toHaveBeenCalled())
    // The optimistic pin survives: the write was accepted, and the fallback
    // reconciles to that expectation rather than reverting it.
    await waitFor(() =>
      expect(store.getState().dashboard.slots.find(s => s.key === SLOT)?.pinned).toBe(true),
    )

    expect(
      deadKeys(keys()),
      'this branch IS the failed re-read; the authoritative pinned state arrives on the frame ' +
      'the accepted PATCH pushes, and a chat-slots invalidation could never have retried it (#10204)',
    ).toEqual([])
  })
})

// ── Source ratchet ─────────────────────────────────────────────────────────

const SRC = join(__dirname, '..')

/** Strip line and block comments so prose naming the dead key does not count. */
export function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

/**
 * An `invalidateQueries` whose `queryKey` array OPENS with `'chat-slots'`.
 *
 * The first element is what decides it, not the whole array: `invalidateQueries`
 * matches by prefix, so `['chat-slots', 'pin-reconcile', n]` names the same
 * unregistered cache as the bare key and is equally dead. `[^})]*` keeps the
 * option bag's own scan inside one call.
 */
const DEAD_INVALIDATION =
  /invalidateQueries\s*\(\s*\{[^})]*queryKey\s*:\s*\[\s*['"]chat-slots['"]/

/** Whether one file's source carries the dead-invalidation shape. */
export function hasDeadSlotsInvalidation(src: string): boolean {
  // Collapse newlines first, so a call wrapped across lines reads the same as
  // a single-line one.
  return DEAD_INVALIDATION.test(stripComments(src).replace(/\s+/g, ' '))
}

function sourceFiles(): string[] {
  return readdirSync(SRC, { recursive: true, encoding: 'utf8' })
    .map(p => p.split('\\').join('/'))
    .filter(p => /\.tsx?$/.test(p))
    .filter(p => !p.startsWith('test/') && !p.includes('__tests__') && !/\.test\.tsx?$/.test(p))
}

describe('no surface invalidates the unregistered chat-slots cache', () => {
  it('the tree carries no invalidateQueries on a chat-slots key', () => {
    const offenders = sourceFiles()
      .filter(rel => hasDeadSlotsInvalidation(readFileSync(join(SRC, rel), 'utf8')))
    expect(
      offenders,
      'No query is registered on a chat-slots-prefixed key, so invalidating one refreshes nothing ' +
      'and fails silently (#10204). Slot rows render from the Redux dashboard slice: let the ' +
      "server's authoritative slots frame land, or dispatch(fetchSlots()) when a surface genuinely " +
      'needs a whole-list re-read.',
    ).toEqual([])
  })

  it('the fetchQuery reads this rule deliberately permits are still in the tree', () => {
    // The rule bans one verb, not the key. If a rename emptied this set, the
    // scan above would be guarding a cache nothing uses and its green would
    // stop meaning anything.
    const readers = sourceFiles().filter(rel => {
      const code = stripComments(readFileSync(join(SRC, rel), 'utf8')).replace(/\s+/g, ' ')
      return /queryKey\s*:\s*\[\s*['"]chat-slots['"]/.test(code)
    })
    expect(readers.length, 'expected the one-shot chat-slots fetchQuery reads to still exist')
      .toBeGreaterThan(0)
  })
})

describe('hasDeadSlotsInvalidation predicate', () => {
  it('flags the bare single-line form both fixed handlers used', () => {
    expect(hasDeadSlotsInvalidation(
      `queryClient.invalidateQueries({ queryKey: ['chat-slots'] })`,
    )).toBe(true)
  })

  it('flags a call wrapped across lines', () => {
    expect(hasDeadSlotsInvalidation(
      `queryClient.invalidateQueries({\n  queryKey: ['chat-slots'],\n})`,
    )).toBe(true)
  })

  it('flags a longer key too, because matching is by prefix', () => {
    expect(hasDeadSlotsInvalidation(
      `qc.invalidateQueries({ queryKey: ['chat-slots', 'pin-reconcile', 3] })`,
    )).toBe(true)
  })

  it('flags the double-quoted spelling', () => {
    expect(hasDeadSlotsInvalidation(`qc.invalidateQueries({ queryKey: ["chat-slots"] })`)).toBe(true)
  })

  it('flags a call carrying other options beside the key', () => {
    expect(hasDeadSlotsInvalidation(
      `qc.invalidateQueries({ exact: true, queryKey: ['chat-slots'] })`,
    )).toBe(true)
  })

  it('permits the one-shot fetchQuery reads on the same key', () => {
    expect(hasDeadSlotsInvalidation(
      `await qc.fetchQuery({ queryKey: ['chat-slots'], queryFn: () => api.chatSlots(), staleTime: 0, gcTime: 0 })`,
    )).toBe(false)
  })

  it('permits invalidations on the keys queries really are registered on', () => {
    for (const key of ['chat-tags', 'tag-columns', 'session-grid-slots', 'slots']) {
      expect(hasDeadSlotsInvalidation(`qc.invalidateQueries({ queryKey: ['${key}'] })`), key).toBe(false)
    }
  })

  it('does not count prose in a line or block comment', () => {
    expect(hasDeadSlotsInvalidation(
      `// the old invalidateQueries({ queryKey: ['chat-slots'] }) was a no-op`,
    )).toBe(false)
    expect(hasDeadSlotsInvalidation(
      `/* invalidateQueries({ queryKey: ['chat-slots'] }) refreshed nothing */`,
    )).toBe(false)
  })

  it('does not let an unrelated invalidation next to a chat-slots read match', () => {
    // The `[^})]*` bound keeps one call's option bag from reaching into the
    // next statement's key.
    expect(hasDeadSlotsInvalidation(
      `qc.invalidateQueries({ queryKey: ['chat-tags'] })\n` +
      `await qc.fetchQuery({ queryKey: ['chat-slots'], queryFn: f })`,
    )).toBe(false)
  })
})
