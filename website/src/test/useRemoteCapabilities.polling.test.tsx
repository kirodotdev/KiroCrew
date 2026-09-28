/**
 * Remote capability re-poll + picker loading state.
 *
 * A remote-bound chat on a cold peer used to open with a PERMANENTLY empty
 * model picker: the peer's `/api/models` cold read timed out at the jump
 * gateway, the aggregator answered a partial document (`unavailable.models =
 * capability_unreachable`), and the dashboard cached that partial as fresh for
 * five minutes with `retry: false` — so no second request ever went out, and
 * the picker rendered "no models" for a peer that was healthy seconds later.
 *
 * These tests pin the two fixes: the query re-polls a version-compatible
 * partial document until it is complete (and ONLY that shape), and the model
 * list renders a loading row with `aria-busy` instead of an empty-list result
 * while the roster is pending.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, renderHook, act, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', () => ({
  api: { instancesCapabilities: vi.fn() },
}))

import { api } from '../api/client'
import {
  useRemoteCapabilities,
  capabilityDocIsRetriablePartial,
  CAPABILITY_REPOLL_MS,
} from '../hooks/useRemoteCapabilities'
import ModelDropdownList from '../components/ModelDropdownList'
import type { ChatSlot, RemoteCrewCapabilities } from '../types'

const capsMock = vi.mocked(api.instancesCapabilities)

function doc(overrides: Partial<RemoteCrewCapabilities> = {}): RemoteCrewCapabilities {
  return {
    instance_id: 'inst-1',
    version: '1.0.0',
    local_version: '1.0.0',
    version_match: true,
    agents: [],
    default_agent: '',
    models: [{ model_name: 'auto', display_name: 'Auto', description: '', context_window: 0 }],
    effort_levels: [],
    workspaces: [],
    default_workspace: '',
    unavailable: {},
    ...overrides,
  }
}

const partialDoc = () =>
  doc({ models: [], unavailable: { models: 'capability_unreachable' } })

const remoteSlot = { executor: 'remote', instance_id: 'inst-1' } as unknown as ChatSlot

function wrapperFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

describe('capabilityDocIsRetriablePartial — the poll gate', () => {
  it('a version-compatible partial with a timed-out field is retriable', () => {
    expect(capabilityDocIsRetriablePartial(partialDoc())).toBe(true)
  })

  it('a peer whose models read is revalidating is retriable', () => {
    // The peer answers a deliberate 503 while its entitlement snapshot is
    // revalidated and serves the corrected list on the next read.
    expect(
      capabilityDocIsRetriablePartial(
        doc({ models: [], unavailable: { models: 'capability_peer_revalidating' } }),
      ),
    ).toBe(true)
  })

  it('a refused read stays terminal beside the revalidating case', () => {
    expect(
      capabilityDocIsRetriablePartial(
        doc({ models: [], unavailable: { models: 'capability_peer_refused' } }),
      ),
    ).toBe(false)
  })

  it('a complete document is not', () => {
    expect(capabilityDocIsRetriablePartial(doc())).toBe(false)
  })

  it('a version-skewed peer is never polled — a fresher roster changes nothing', () => {
    expect(
      capabilityDocIsRetriablePartial({ ...partialDoc(), version_match: false }),
    ).toBe(false)
  })

  it('terminal per-field codes are not retriable', () => {
    // A disconnected tunnel and a too-old peer need an operator, not a retry.
    for (const code of [
      'capability_peer_not_connected',
      'capability_no_credential',
      'capability_peer_too_old',
      'capability_unauthorized',
      'capability_peer_refused',
    ]) {
      expect(
        capabilityDocIsRetriablePartial(doc({ models: [], unavailable: { models: code } })),
      ).toBe(false)
    }
  })

  it('no document yet is not a partial', () => {
    expect(capabilityDocIsRetriablePartial(undefined)).toBe(false)
  })
})

describe('useRemoteCapabilities — re-polls a partial document until complete', () => {
  let qc: QueryClient

  beforeEach(() => {
    vi.useFakeTimers()
    capsMock.mockReset()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  })
  afterEach(() => {
    qc.clear()
    vi.useRealTimers()
  })

  it('a partial answer triggers a second request; a complete one stops the poll', async () => {
    capsMock.mockResolvedValueOnce(partialDoc()).mockResolvedValue(doc())

    const { result } = renderHook(() => useRemoteCapabilities(remoteSlot), {
      wrapper: wrapperFor(qc),
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(capsMock).toHaveBeenCalledTimes(1)
    // The partial is NOT treated as fresh for the 5-minute stale window: the
    // re-poll interval fires and the peer, now warm, answers completely. This
    // is the regression main failed — one request, then silence.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CAPABILITY_REPOLL_MS + 50)
    })
    expect(capsMock).toHaveBeenCalledTimes(2)
    expect(result.current.capabilities?.models.length).toBe(1)
    expect(result.current.modelsPending).toBe(false)
    // Complete document -> the interval is off; nothing else goes out.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CAPABILITY_REPOLL_MS * 3)
    })
    expect(capsMock).toHaveBeenCalledTimes(2)
  })

  it('a version-skewed partial is not polled', async () => {
    capsMock.mockResolvedValue({ ...partialDoc(), version_match: false })

    renderHook(() => useRemoteCapabilities(remoteSlot), { wrapper: wrapperFor(qc) })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(capsMock).toHaveBeenCalledTimes(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CAPABILITY_REPOLL_MS * 3)
    })
    expect(capsMock).toHaveBeenCalledTimes(1)
  })

  it('modelsPending is true across the whole pending window, false once complete', async () => {
    capsMock.mockResolvedValueOnce(partialDoc()).mockResolvedValue(doc())

    const { result } = renderHook(() => useRemoteCapabilities(remoteSlot), {
      wrapper: wrapperFor(qc),
    })
    // While the first read is in flight.
    expect(result.current.modelsPending).toBe(true)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    // While the answer is a retriable partial missing the model roster.
    expect(result.current.modelsPending).toBe(true)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CAPABILITY_REPOLL_MS + 50)
    })
    expect(result.current.modelsPending).toBe(false)
  })

  it('a local slot never fetches and never pends', () => {
    const { result } = renderHook(
      () => useRemoteCapabilities({ executor: 'local' } as unknown as ChatSlot),
      { wrapper: wrapperFor(qc) },
    )
    expect(capsMock).not.toHaveBeenCalled()
    expect(result.current.isRemote).toBe(false)
    expect(result.current.modelsPending).toBe(false)
  })
})

describe('ModelDropdownList — pending roster renders loading, not "No matches"', () => {
  it('an empty loading list shows an aria-busy loading row', () => {
    render(<ModelDropdownList models={[]} activeModel="" onSelect={() => {}} loading />)
    const busy = document.querySelector('[aria-busy="true"]')
    expect(busy).not.toBeNull()
    expect(screen.queryByText('No matches')).toBeNull()
  })

  it('an empty NON-loading list keeps the honest empty state', () => {
    render(<ModelDropdownList models={[]} activeModel="" onSelect={() => {}} />)
    expect(document.querySelector('[aria-busy="true"]')).toBeNull()
    expect(screen.getByText('No matches')).not.toBeNull()
  })

  it('a populated list renders its rows even while loading', () => {
    // Filtering an already-populated roster must not flip to the loading row.
    render(
      <ModelDropdownList
        models={[{ name: 'auto' }]}
        activeModel="auto"
        onSelect={() => {}}
        loading
      />,
    )
    expect(document.querySelector('[aria-busy="true"]')).toBeNull()
    expect(screen.getByText('auto')).not.toBeNull()
  })

  it('a failed empty list renders neither the loading row nor "No matches"', () => {
    // The wrapper's ErrorNotice owns the message in the failed state; a
    // sibling "No matches" would contradict it, and `failed` wins over
    // `loading` so no spinner promises a retry either.
    render(<ModelDropdownList models={[]} activeModel="" onSelect={() => {}} loading failed />)
    expect(document.querySelector('[aria-busy="true"]')).toBeNull()
    expect(screen.queryByText('No matches')).toBeNull()
  })

  it('a failed but populated list still renders its rows', () => {
    // A stale-but-present roster stays usable while the refresh read errors.
    render(
      <ModelDropdownList
        models={[{ name: 'auto' }]}
        activeModel="auto"
        onSelect={() => {}}
        failed
      />,
    )
    expect(screen.getByText('auto')).not.toBeNull()
    expect(screen.queryByText('No matches')).toBeNull()
  })
})

describe('useRemoteCapabilities — a failed read is reported, not dressed as empty', () => {
  it('a poll failing AFTER a partial hands over to the error surface', async () => {
    // The stale data is still a retriable partial, so without the error guard
    // the interval would keep firing every 8s AND modelsPending would keep the
    // loading row up beside the ErrorNotice — a spinner promising a retry
    // while the retry budget is actually spent.
    vi.useFakeTimers()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    try {
      capsMock.mockReset()
      capsMock.mockResolvedValueOnce(partialDoc()).mockRejectedValue(new Error('tunnel died'))
      const { result } = renderHook(() => useRemoteCapabilities(remoteSlot), {
        wrapper: wrapperFor(qc),
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(result.current.modelsPending).toBe(true)
      // First poll fires and fails.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(CAPABILITY_REPOLL_MS + 50)
      })
      expect(capsMock).toHaveBeenCalledTimes(2)
      expect(result.current.failed).toBe(true)
      expect(result.current.modelsPending).toBe(false)
      // Polling has stopped: no further requests go out on their own.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(CAPABILITY_REPOLL_MS * 3)
      })
      expect(capsMock).toHaveBeenCalledTimes(2)
    } finally {
      qc.clear()
      vi.useRealTimers()
    }
  })

  it('retrying is false when the error is settled and true while the refetch flies', async () => {
    // `failed` stays true for the whole in-place retry when the query holds
    // stale data (a poll that failed after a partial answer), so `retrying`
    // is what lets the button acknowledge the click instead of sitting dead.
    // (A first read that fails with NO data re-enters `pending` on refetch,
    // so the loading row covers that case and `retrying` stays false.)
    vi.useFakeTimers()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    try {
      capsMock.mockReset()
      capsMock
        .mockResolvedValueOnce(partialDoc())
        .mockRejectedValueOnce(new Error('tunnel died'))
      const { result } = renderHook(() => useRemoteCapabilities(remoteSlot), {
        wrapper: wrapperFor(qc),
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      // The first poll fires and fails, leaving an error with stale data.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(CAPABILITY_REPOLL_MS + 50)
      })
      // Settled error: failed owns the surface, no retry is in flight.
      expect(result.current.failed).toBe(true)
      expect(result.current.retrying).toBe(false)
      // Hold the retry open so the in-flight state is observable, not raced.
      let resolveRetry!: (value: RemoteCrewCapabilities) => void
      capsMock.mockImplementationOnce(() => new Promise(res => { resolveRetry = res }))
      let refetchPromise!: Promise<unknown>
      act(() => {
        refetchPromise = result.current.refetch()
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(result.current.retrying).toBe(true)
      expect(result.current.failed).toBe(true)
      // The retry lands: both flags drop together and the roster renders.
      resolveRetry(doc())
      await act(async () => {
        await refetchPromise
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(result.current.retrying).toBe(false)
      expect(result.current.failed).toBe(false)
      expect(result.current.capabilities?.models.length).toBe(1)
    } finally {
      qc.clear()
      vi.useRealTimers()
    }
  })

  it('exposes failed and an in-place refetch when the request errors', async () => {
    vi.useFakeTimers()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    try {
      capsMock.mockReset()
      capsMock.mockRejectedValue(new Error('503'))
      const { result } = renderHook(() => useRemoteCapabilities(remoteSlot), {
        wrapper: wrapperFor(qc),
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(result.current.failed).toBe(true)
      // An errored read is NOT the pending state: pending promises a fresher
      // answer on its own, failed offers the user a retry instead.
      expect(result.current.modelsPending).toBe(false)
      expect(typeof result.current.refetch).toBe('function')
      // A dead read is not hammered: no poll interval runs on error.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(CAPABILITY_REPOLL_MS * 3)
      })
      expect(capsMock).toHaveBeenCalledTimes(1)
      // The exposed refetch is the recovery path.
      capsMock.mockResolvedValue(doc())
      await act(async () => {
        await result.current.refetch()
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(result.current.failed).toBe(false)
      expect(result.current.capabilities?.models.length).toBe(1)
    } finally {
      qc.clear()
      vi.useRealTimers()
    }
  })
})
