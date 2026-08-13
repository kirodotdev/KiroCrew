/**
 * Hook-level tests for {@link useSessionsProvider} — the React wiring around
 * the pure provider factory (which sessionsProvider.test.ts covers):
 *  - endpoint choice per warm-instance state (federated vs plain local), with
 *    the endpoint baked into the React-Query key so a connect/disconnect can
 *    never serve the other mode's cached rows;
 *  - the local search as the fallback floor when the federated endpoint fails
 *    (including the 403 when the instances feature is off);
 *  - openSession routing: a remote ref switches instance panes and never
 *    resumes a (same-keyed, unrelated) local session; a local ref resumes and
 *    lands on /chat.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { renderHookWithProviders, createTestStore } from '../../../test/helpers'
import { setWarm } from '../../../store/instancesSlice'

const {
  sessionsSearchMock,
  federatedSearchMock,
  listInstancesMock,
  connectInstanceMock,
  chatFoldersMock,
} = vi.hoisted(() => ({
  sessionsSearchMock: vi.fn().mockResolvedValue({ sessions: [{ key: 'local-1', title: 'local hit' }] }),
  federatedSearchMock: vi.fn().mockResolvedValue({
    sessions: [
      { key: 'local-1', title: 'local hit' },
      { key: 'rem-1', title: 'remote hit', instance_id: 'inst-a', instance_name: 'clouddeskARM' },
    ],
  }),
  listInstancesMock: vi.fn().mockResolvedValue({
    instances: [{ id: 'inst-a', name: 'clouddeskARM', status: { state: 'connected' } }],
  }),
  connectInstanceMock: vi.fn().mockResolvedValue({ state: 'connected', local_port: 45123, token: 't2' }),
  chatFoldersMock: vi.fn().mockResolvedValue([]),
}))

vi.mock('../../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      sessionsSearch: sessionsSearchMock,
      instancesSearchSessions: federatedSearchMock,
      listInstances: listInstancesMock,
      connectInstance: connectInstanceMock,
      chatFolders: chatFoldersMock,
    },
  }
})

const resumeSpy = vi.hoisted(() => vi.fn())
vi.mock('../../../store/chatSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../store/chatSlice')>()
  return {
    ...actual,
    // Thunk factory: dispatching the returned function is a no-op beyond the spy.
    resumeFromHistory: (ref: unknown) => {
      resumeSpy(ref)
      return () => Promise.resolve()
    },
  }
})

const navigateSpy = vi.hoisted(() => vi.fn())
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => navigateSpy }
})

import { useSessionsProvider } from './sessionsProvider'

function storeWithWarm(warm: boolean) {
  const store = createTestStore()
  if (warm) store.dispatch(setWarm({ id: 'inst-a', conn: { port: 45123, token: 't' } }))
  return store
}

describe('useSessionsProvider — federated wiring', () => {
  beforeEach(() => {
    sessionsSearchMock.mockClear()
    federatedSearchMock.mockClear()
    federatedSearchMock.mockResolvedValue({
      sessions: [
        { key: 'local-1', title: 'local hit' },
        { key: 'rem-1', title: 'remote hit', instance_id: 'inst-a', instance_name: 'clouddeskARM' },
      ],
    })
    connectInstanceMock.mockClear()
    resumeSpy.mockClear()
    navigateSpy.mockClear()
  })

  it('routes through the federated endpoint when a warm instance exists (local never called)', async () => {
    const { result } = renderHookWithProviders(() => useSessionsProvider(), {
      store: storeWithWarm(true),
    })
    const rows = await result.current.search('deploy')
    expect(federatedSearchMock).toHaveBeenCalledWith('deploy')
    expect(sessionsSearchMock).not.toHaveBeenCalled()
    expect(rows.some(r => r.id === 'sessions:inst-a:rem-1')).toBe(true)
  })

  it('uses the plain local endpoint when no instance is warm', async () => {
    const { result } = renderHookWithProviders(() => useSessionsProvider(), {
      store: storeWithWarm(false),
    })
    const rows = await result.current.search('deploy')
    expect(sessionsSearchMock).toHaveBeenCalledWith('deploy')
    expect(federatedSearchMock).not.toHaveBeenCalled()
    expect(rows.some(r => r.id === 'sessions:local-1')).toBe(true)
  })

  it('falls back to the local search when the federated endpoint rejects (e.g. 403 feature-off)', async () => {
    federatedSearchMock.mockRejectedValue(new Error('403'))
    const { result } = renderHookWithProviders(() => useSessionsProvider(), {
      store: storeWithWarm(true),
    })
    const rows = await result.current.search('deploy')
    expect(federatedSearchMock).toHaveBeenCalled()
    expect(sessionsSearchMock).toHaveBeenCalledWith('deploy')
    expect(rows.map(r => r.id)).toEqual(['sessions:local-1'])
  })

  it('Enter on a remote row switches instance panes and never resumes a local session', async () => {
    const store = storeWithWarm(true)
    const { result } = renderHookWithProviders(() => useSessionsProvider(), { store })
    const rows = await result.current.search('deploy')
    // The instances list feeds the shared selectInstance semantics.
    await waitFor(() => expect(listInstancesMock).toHaveBeenCalled())

    rows.find(r => r.id === 'sessions:inst-a:rem-1')!.onActivate?.()
    // Pane activated (activeId set) — the transcript lives on the other gateway,
    // so the local resume path must not fire.
    expect(store.getState().instances.activeId).toBe('inst-a')
    expect(resumeSpy).not.toHaveBeenCalled()
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('Enter on a local row resumes the session and lands on /chat', async () => {
    const { result } = renderHookWithProviders(() => useSessionsProvider(), {
      store: storeWithWarm(true),
    })
    const rows = await result.current.search('deploy')

    rows.find(r => r.id === 'sessions:local-1')!.onActivate?.()
    expect(resumeSpy).toHaveBeenCalledWith(expect.objectContaining({ key: 'local-1' }))
    expect(navigateSpy).toHaveBeenCalledWith('/chat')
  })
})
