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
import { useQuery, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { renderHook, waitFor } from '@testing-library/react'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { forkSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { api } from '../api/client'

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
