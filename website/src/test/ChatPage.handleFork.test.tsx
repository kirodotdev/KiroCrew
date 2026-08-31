/**
 * Tests for the tail-fork direction-resolution logic used by ChatPage's
 * `handleFork`.
 *
 * IMPORTANT SCOPE NOTE: `handleFork` is a `useCallback` defined inline inside
 * the (very large) `ChatPage` component and is not exported standalone. A full
 * `render(<ChatPage />)` harness was attempted first (real AssistantMessage,
 * real Redux store, mocked api/dashboardConfig) but ChatPage's message list
 * goes through an additional turn-grouping/virtualization layer upstream of
 * the plain render call (`it.msgs` / `renderMessage(it.idx, it.msg)`, see
 * ChatPage.tsx ~L2531-3045) that the existing ChatPage.*.test.tsx harnesses
 * all stub out (`react-virtuoso`, `ChatSidebar`, etc.) rather than drive live.
 * Reproducing that grouping pipeline in a test-only harness is disproportionate
 * to this task and risks testing a divergent re-implementation instead of the
 * real code path.
 *
 * Per the task's explicit fallback, this file instead tests the resolution
 * logic at the smallest feasible unit: a tiny hook that mounts the EXACT same
 * two lines as ChatPage.tsx's handleFork (same useQuery key/queryFn, same
 * `resolvedCfg` / `direction` expressions, same dispatch(forkSlot(...))),
 * driven through the real Redux `forkSlot` thunk and the real `api` module
 * (mocked at the network boundary), inside a real QueryClientProvider. This
 * exercises the real `forkSlot` thunk + real api.forkChatSlot signature (so a
 * signature drift would fail these tests) while being explicit that the
 * direction-selection EXPRESSION is duplicated from ChatPage.tsx rather than
 * imported. If ChatPage.tsx's handleFork expression changes, this file must
 * be updated to match -- there is no single source of truth to import from
 * without exporting handleFork from ChatPage (out of scope: "do not touch
 * non-test source files").
 *
 * The `resolvedCfg` expression is `forkCfg ?? await api.dashboardConfig()`:
 * use the cache when warm, otherwise always fetch a fresh value, regardless of
 * the query's loading state. A guard keyed on loading state
 * (`forkCfg ?? (forkCfgLoading ? await api.dashboardConfig() : forkCfg)`)
 * would break once the ['dashboardConfig'] query settled with no data (errored
 * or resolved to undefined): `forkCfgLoading` is false, so the `: forkCfg`
 * branch evaluates to `undefined` again and silently downgrades direction to
 * 'head'. Because loading state is not consulted, the mirror hook below does
 * not destructure `isLoading` either.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { useQuery, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { renderHook, waitFor } from '@testing-library/react'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { forkSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: vi.fn(),
    forkChatSlot: vi.fn(),
  },
}))

const dashboardConfigMock = api.dashboardConfig as unknown as ReturnType<typeof vi.fn>
const forkChatSlotMock = api.forkChatSlot as unknown as ReturnType<typeof vi.fn>

function makeStore() {
  return configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer } })
}

function makeWrapper(store: ReturnType<typeof makeStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>{children}</Provider>
    </QueryClientProvider>
  )
}

/**
 * Verbatim reproduction of ChatPage.tsx's handleFork direction-resolution
 * (the useQuery call and the two `resolvedCfg` / `direction` lines), wired to
 * the real forkSlot thunk. See file header for why this is duplicated rather
 * than imported.
 */
function useHandleForkUnderTest(store: ReturnType<typeof makeStore>) {
  const { data: forkCfg } = useQuery<{ tail_fork_enabled?: boolean }>({
    queryKey: ['dashboardConfig'], queryFn: () => api.dashboardConfig(), staleTime: 30_000,
  })
  return async (activeSlot: string, visibleIndex: number, messageId?: string) => {
    // forkCfg is undefined until the dashboardConfig query resolves for the
    // first time. Use the cache when warm; otherwise fetch a fresh value
    // directly so direction never silently falls back to an undefined config.
    const resolvedCfg = forkCfg ?? await api.dashboardConfig()
    const direction = resolvedCfg?.tail_fork_enabled ? 'tail' : 'head'
    return store.dispatch(forkSlot({ slot: activeSlot, atIndex: visibleIndex, messageId, direction })).unwrap()
  }
}

beforeEach(() => {
  dashboardConfigMock.mockReset()
  forkChatSlotMock.mockReset()
  forkChatSlotMock.mockResolvedValue({ ok: true, key: 'chat-1-fork', title: 'Fork', messages: 1 })
})

describe('handleFork direction wiring (zejiangg #5)', () => {
  it('dispatches forkSlot with direction "tail" when dashboardConfig has tail_fork_enabled: true', async () => {
    dashboardConfigMock.mockResolvedValue({ tail_fork_enabled: true })
    const store = makeStore()
    const { result } = renderHook(() => useHandleForkUnderTest(store), { wrapper: makeWrapper(store) })

    await waitFor(() => expect(dashboardConfigMock).toHaveBeenCalled())
    await result.current('chat-1-100', 2)

    expect(forkChatSlotMock).toHaveBeenCalledWith('chat-1-100', 2, undefined, undefined, 'tail')
  })

  it('dispatches forkSlot with direction "head" when dashboardConfig has tail_fork_enabled: false', async () => {
    dashboardConfigMock.mockResolvedValue({ tail_fork_enabled: false })
    const store = makeStore()
    const { result } = renderHook(() => useHandleForkUnderTest(store), { wrapper: makeWrapper(store) })

    await waitFor(() => expect(dashboardConfigMock).toHaveBeenCalled())
    await result.current('chat-1-100', 2)

    expect(forkChatSlotMock).toHaveBeenCalledWith('chat-1-100', 2, undefined, undefined, 'head')
  })

  it('dispatches forkSlot with direction "head" when tail_fork_enabled is absent from config', async () => {
    dashboardConfigMock.mockResolvedValue({})
    const store = makeStore()
    const { result } = renderHook(() => useHandleForkUnderTest(store), { wrapper: makeWrapper(store) })

    await waitFor(() => expect(dashboardConfigMock).toHaveBeenCalled())
    await result.current('chat-1-100', 0)

    expect(forkChatSlotMock).toHaveBeenCalledWith('chat-1-100', 0, undefined, undefined, 'head')
  })

  it('forwards the stable message id with the resolved fork direction', async () => {
    dashboardConfigMock.mockResolvedValue({ tail_fork_enabled: false })
    const store = makeStore()
    const { result } = renderHook(() => useHandleForkUnderTest(store), { wrapper: makeWrapper(store) })

    await waitFor(() => expect(dashboardConfigMock).toHaveBeenCalled())
    await result.current('chat-1-100', 7, 'row-42')

    expect(forkChatSlotMock).toHaveBeenCalledWith(
      'chat-1-100', 7, undefined, undefined, 'head', 'row-42',
    )
  })
})

describe('handleFork B3 cold-cache fix (bug-fix regression test, required per ruleset)', () => {
  it('does NOT downgrade to head-fork when the dashboardConfig query has not resolved yet (cold cache, still loading)', async () => {
    // Simulate the cold-cache window: the ['dashboardConfig'] useQuery has not
    // resolved (forkCfg undefined) when fork is invoked. A handler that fell
    // through to `forkCfg?.tail_fork_enabled` (=> undefined => 'head') would
    // silently downgrade an enabled tail-fork. handleFork instead always awaits
    // a fresh api.dashboardConfig() call whenever forkCfg is absent, regardless
    // of the query's loading state.
    let resolveConfig!: (v: { tail_fork_enabled: boolean }) => void
    dashboardConfigMock.mockImplementation(() => new Promise(res => { resolveConfig = res }))

    const store = makeStore()
    const { result } = renderHook(() => useHandleForkUnderTest(store), { wrapper: makeWrapper(store) })

    // Invoke fork immediately -- the useQuery's initial fetch is still
    // in-flight (forkCfg === undefined).
    const forkPromise = result.current('chat-1-100', 3)

    // Resolve the in-flight config fetch with tail_fork_enabled: true. This
    // resolves both the useQuery's own fetch AND (per the B3 fix) the direct
    // api.dashboardConfig() call handleFork awaits when forkCfg is absent.
    resolveConfig({ tail_fork_enabled: true })
    await forkPromise

    expect(forkChatSlotMock).toHaveBeenCalledWith('chat-1-100', 3, undefined, undefined, 'tail')
  })

  it('does NOT downgrade to head-fork when forkCfg is absent and the query has already settled (not loading) -- the exact case the original B3 fix missed', async () => {
    // The hazard: a guard of `forkCfg ?? (forkCfgLoading ? await
    // api.dashboardConfig() : forkCfg)` breaks once the query settles with no
    // data (errored, or resolves to undefined) — `forkCfgLoading` becomes false,
    // so the `: forkCfg` branch evaluates to `undefined` again and silently
    // downgrades to 'head' even though the query is no longer "loading".
    // `resolvedCfg = forkCfg ?? await api.dashboardConfig()` always fetches
    // fresh when forkCfg is nullish, independent of loading state.
    //
    // We reproduce "settled with no data, not loading" by having the
    // dashboardConfig query resolve to `null` (react-query disallows
    // `undefined` as query data, so `null` is the smallest falsy value that
    // leaves forkCfg falsy post-settle) and then invoking fork only after
    // that initial query has fully settled.
    dashboardConfigMock.mockResolvedValueOnce(null)
    const store = makeStore()
    const { result } = renderHook(() => useHandleForkUnderTest(store), { wrapper: makeWrapper(store) })

    // Wait for the initial ['dashboardConfig'] query to settle -- forkCfg is
    // now `undefined` and the query is no longer loading.
    await waitFor(() => expect(dashboardConfigMock).toHaveBeenCalledTimes(1))

    // Second call (the direct api.dashboardConfig() handleFork awaits) returns
    // tail_fork_enabled: true -- proving handleFork fetched fresh rather than
    // trusting the settled-but-empty cache.
    dashboardConfigMock.mockResolvedValueOnce({ tail_fork_enabled: true })
    await result.current('chat-1-100', 3)

    expect(dashboardConfigMock).toHaveBeenCalledTimes(2)
    expect(forkChatSlotMock).toHaveBeenCalledWith('chat-1-100', 3, undefined, undefined, 'tail')
  })

  it('downgrades to head-fork only when the resolved cold-cache config genuinely has tail_fork_enabled: false', async () => {
    // Companion negative case: cold cache resolving to false is a real
    // head-fork, not a bug -- distinguishes "no direction computed" (bug)
    // from "direction computed as head" (correct, config says so).
    let resolveConfig!: (v: { tail_fork_enabled: boolean }) => void
    dashboardConfigMock.mockImplementation(() => new Promise(res => { resolveConfig = res }))

    const store = makeStore()
    const { result } = renderHook(() => useHandleForkUnderTest(store), { wrapper: makeWrapper(store) })

    const forkPromise = result.current('chat-1-100', 3)
    resolveConfig({ tail_fork_enabled: false })
    await forkPromise

    expect(forkChatSlotMock).toHaveBeenCalledWith('chat-1-100', 3, undefined, undefined, 'head')
  })
})

describe('handleFork over-capacity refusal reaches the user as human copy', () => {
  it('carries the backend code ACROSS the thunk boundary, where serialization drops it', async () => {
    // A resolved-result read is unreachable: HTTP 400 makes the thunk REJECT, and
    // default serialization keeps string fields only, so the payload is the only path.
    const body = JSON.stringify({
      error: 'conversation too large to fork: 10001 rows exceed the 10000-row session capacity.',
      code: 'fork_corpus_too_large',
    })
    dashboardConfigMock.mockResolvedValue({ tail_fork_enabled: false })
    forkChatSlotMock.mockRejectedValue(new ApiError(400, 'Bad Request', body))

    const store = makeStore()
    const { result: hook } = renderHook(() => useHandleForkUnderTest(store), {
      wrapper: makeWrapper(store),
    })
    await waitFor(() => expect(dashboardConfigMock).toHaveBeenCalled())

    let rejected: unknown
    try {
      await hook.current!('chat-1-100', 2)
    } catch (e) {
      rejected = e
    }

    expect(rejected).toBeDefined()
    expect((rejected as { code?: string }).code).toBe('fork_corpus_too_large')
    expect((rejected as { status?: number }).status).toBe(400)
  })

  it('rethrows a failure carrying no numeric status, leaving the ordinary path alone', async () => {
    dashboardConfigMock.mockResolvedValue({ tail_fork_enabled: false })
    forkChatSlotMock.mockRejectedValue(new Error('network down'))

    const store = makeStore()
    const { result: hook } = renderHook(() => useHandleForkUnderTest(store), {
      wrapper: makeWrapper(store),
    })
    await waitFor(() => expect(dashboardConfigMock).toHaveBeenCalled())

    let rejected: unknown
    try {
      await hook.current!('chat-1-100', 2)
    } catch (e) {
      rejected = e
    }

    // Negative control: no status means no payload, so `code` must be absent
    // rather than defaulted to the over-capacity string.
    expect((rejected as { code?: string }).code).toBeUndefined()
    expect((rejected as { message?: string }).message).toContain('network down')
  })
})

describe('handleFork surfaces its failure on the shared action-error notice', () => {
  const src = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')
  const handler = src.slice(
    src.indexOf('const handleFork = useCallback'),
    src.indexOf('const handlePlanFromHere = useCallback'),
  )
  const shared = src.slice(src.indexOf('title={actionError?.title}'))

  it('extracted the handler slice', () => {
    expect(handler).toContain('forkSlot(')
    expect(handler.length).toBeGreaterThan(200)
  })

  it('raises no browser alert on either failure path', () => {
    expect(handler).not.toContain('alert(')
  })

  it('reads the code off the rejection rather than off a resolved result', () => {
    expect(handler).toContain('showForkError(forkErrorNotice(')
    expect(handler).toMatch(/catch \(e\)[\s\S]*\?: string \} \| null\)\?\.code/)
  })

  it('keeps the structured report the localized message can no longer be matched to', () => {
    // ErrorNotice recovers endpoint/status/code by matching `message` against the
    // journal, which stores the RAW wire text, so a localized string loses it.
    expect(src).toContain('report: findReport(raw)')
    // The report rides the SHARED notice: the fork path needed a report, not a
    // second surface, so `showActionError` grew the argument instead.
    expect(src).toMatch(/const showActionError = useCallback\(\(message: string, title\?: string, report\?: ErrorReport\)/)
    expect(shared.slice(0, 400)).toContain('report={actionError?.report}')
  })

  it('carries no retry affordance: one consumer did not justify a permanent wire field', () => {
    // Pinned so the affordance is not reintroduced by halves; the 400 and its code stay.
    expect(src).not.toContain('fitsAt')
    expect(src).not.toContain('fork-error-retry')
    expect(src).not.toContain('fork_there_action')
    expect(src).not.toContain('fork_too_large_at_error')
    const en = readFileSync(resolve(__dirname, '../i18n/locales/en.manual.json'), 'utf8')
    const chat = JSON.parse(en).pages.chatPage as Record<string, string>
    expect(chat.fork_too_large_at_error).toBeUndefined()
    expect(chat.fork_there_action).toBeUndefined()
    // The prose advice the subtraction keeps, so this is a removal and not a regression.
    // It is now direction-specific: the generic wording asked the reader to move in a
    // direction the UI never showed them, and `handleFork` already knows which way.
    expect(chat.fork_too_large_error_head).toBeTruthy()
    expect(chat.fork_too_large_error_tail).toBeTruthy()
    expect(chat.fork_too_large_error_head).not.toBe(chat.fork_too_large_error_tail)
  })

  it('renders the failure through ErrorNotice with the hand-off decision recorded', () => {
    expect(shared).toContain('<ErrorNotice')
    // The rule blocks on a MISSING askAgent decision, never on its direction.
    expect(/askAgent|No hand-off/.test(shared.slice(0, 400))).toBe(true)
    // The id rides ErrorNotice's own `testId`, so it lands on the `role="alert"`
    // element; a wrapper div would split the two and a lookup would find no role.
    expect(shared.slice(0, 400)).toContain('testId="action-error"')
    expect(shared.slice(0, 400)).not.toContain('data-testid="action-error"')
  })

  it('adds no second error surface: the fork rides the notice that already existed', () => {
    // The fork path needed a report on the shared notice, not a slot of its own.
    // Pinned as a subtraction so a dedicated banner is not reintroduced.
    expect(src).not.toContain('testId="fork-error"')
    expect(src).not.toContain('setForkError')
    expect(src).not.toContain('{forkError && (')
    // Positive control: the four surfaces that legitimately own a slot are still
    // there, so this is not passing because the whole stack went missing.
    for (const kept of ['upload-error', 'sid-error', 'action-error', 'pin-error']) {
      const at = src.indexOf(`testId="${kept}"`)
      expect(at).toBeGreaterThan(-1)
      expect(src.slice(Math.max(0, at - 300), at)).toContain('animate-rise')
    }
  })

  it('inherits the shared notice clearing rule instead of restating it', () => {
    // A fork failure must not outlive its slot. The shared surface already clears
    // on slot switch, which is why the fork path needs no rule of its own.
    expect(handler).not.toContain('setActionError(null)')
    const switcher = src.slice(src.indexOf('knowledgeFetchRef.current.clearResults()'))
    expect(switcher.slice(0, 600)).toContain('setActionError(null)')
  })
})
