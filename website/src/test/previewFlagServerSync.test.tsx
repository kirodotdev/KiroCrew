/**
 * Feature previews are kept by the gateway (`dashboard.feature_previews.<name>`)
 * with localStorage as a cache. These tests pin the four parts of that contract:
 * the server copy fills an empty cache, a toggle is written to the server, a
 * failed config read leaves the cache in charge, and a switch set before the
 * server kept it is carried up exactly once without replacing a stored value.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { ReactNode } from 'react'
import { renderHook, act, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import {
  PREVIEW_FLAGS_SERVER_BACKED_KEY, PREVIEW_FLAGS_SYNCED_KEY, __resetPreviewFlagServerSyncForTests, clearPreviewFlagWriteError,
  usePreviewFlagServerSync, usePreviewFlagWriteError,
} from '../hooks/usePreviewFlagServerSync'
import * as previewFlags from '../utils/previewFlags'
import {
  PREVIEW_ARTIFACT_DEPLOY, PREVIEW_CREW, PREVIEW_FLAGS, PREVIEW_FLAG_PREFIX, PREVIEW_INSTANCE_SESSIONS,
  PREVIEW_REMOTE_CREW_CHAT, PREVIEW_WEBHOOKS, previewFlagConfigPath, readPreviewFlag, setPreviewFlag,
  setRemoteCrewChat,
} from '../utils/previewFlags'

type Body = { dashboard?: { feature_previews?: unknown } }

function harness(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

function newClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
}

/** Mount the sync next to a reader of the Crewmates flag, as the app shell does. */
function mountSync(qc: QueryClient) {
  return renderHook(() => {
    usePreviewFlagServerSync()
    return usePreviewFlag(PREVIEW_CREW)
  }, { wrapper: harness(qc) })
}

let server: Record<string, boolean>
let patchSpy: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  localStorage.clear()
  __resetPreviewFlagServerSyncForTests()
  server = {}
  vi.spyOn(api, 'kirocrewConfig').mockImplementation(
    async () => ({ dashboard: { feature_previews: { ...server } } }) as Body,
  )
  patchSpy = vi.spyOn(api, 'patchConfig').mockImplementation(async (path: string, value: unknown, opts?: { ifUnset?: boolean }) => {
    const name = path.replace('dashboard.feature_previews.', '')
    if (opts?.ifUnset && name in server) throw Object.assign(new Error('already set'), { status: 409 })
    server[name] = value as boolean
    return { ok: true }
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('preview flag config paths', () => {
  it('names each flag by its snake_case config path', () => {
    expect(previewFlagConfigPath(PREVIEW_CREW)).toBe('dashboard.feature_previews.crew')
    expect(previewFlagConfigPath(PREVIEW_ARTIFACT_DEPLOY)).toBe('dashboard.feature_previews.artifact_deploy')
    expect(new Set(PREVIEW_FLAGS).size).toBe(PREVIEW_FLAGS.length)
  })

  it('keeps every declared flag on the gateway except the retired one', () => {
    const declared = Object.entries(previewFlags)
      .filter(([name, v]) => name.startsWith('PREVIEW_') && typeof v === 'string' && v.startsWith(PREVIEW_FLAG_PREFIX))
      .map(([, v]) => v as string)
      .filter(v => v !== PREVIEW_INSTANCE_SESSIONS && v !== PREVIEW_FLAG_PREFIX && v !== previewFlags.PREVIEW_FLAG_EVENT)
    expect([...PREVIEW_FLAGS].sort()).toEqual(declared.sort())
  })
})

describe('usePreviewFlagServerSync', () => {
  it('turns Crewmates on from the server when this browser has no copy', async () => {
    server = { crew: true }
    const { result } = mountSync(newClient())
    await waitFor(() => expect(result.current).toBe(true))
    expect(localStorage.getItem(PREVIEW_CREW)).toBe('1')
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('lets the server copy win over a stale browser copy', async () => {
    server = { crew: false }
    localStorage.setItem(PREVIEW_CREW, '1')
    localStorage.setItem(PREVIEW_FLAGS_SYNCED_KEY, '1')
    const { result } = mountSync(newClient())
    await waitFor(() => expect(result.current).toBe(false))
    expect(readPreviewFlag(PREVIEW_CREW)).toBe(false)
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('writes a toggle to the server', async () => {
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { expect(setPreviewFlag(PREVIEW_CREW, true)).toBe(true) })
    expect(result.current).toBe(true)
    await waitFor(() => expect(server).toEqual({ crew: true }))
    expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.crew', true, undefined)
    await waitFor(() => expect(qc.getQueryData<Body>(['kirocrewConfig'])?.dashboard?.feature_previews).toEqual({ crew: true }))
    expect(result.current).toBe(true)
  })

  it('does not flip a switch back while its write is still in flight', async () => {
    let release: () => void = () => {}
    patchSpy.mockImplementation(async (path: string, value: unknown) => {
      await new Promise<void>(r => { release = r })
      server[path.replace('dashboard.feature_previews.', '')] = value as boolean
      return { ok: true }
    })
    server = { crew: false }
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    // A config body read before the write landed still says off.
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(result.current).toBe(true)
    await act(async () => { release() })
    await waitFor(() => expect(server.crew).toBe(true))
    await waitFor(() => expect(qc.getQueryData<Body>(['kirocrewConfig'])?.dashboard?.feature_previews).toEqual({ crew: true }))
    expect(result.current).toBe(true)
  })

  it('puts a refused write right from the server copy', async () => {
    server = { crew: false }
    patchSpy.mockRejectedValue(new Error('owner only'))
    const { result } = mountSync(newClient())
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    expect(result.current).toBe(true)
    await waitFor(() => expect(result.current).toBe(false))
  })

  it('reports a toggle the gateway refused as owner-only on a 403', async () => {
    server = { crew: false }
    patchSpy.mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return usePreviewFlagWriteError()
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    expect(result.current).toBeNull()
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await waitFor(() => expect(result.current).toEqual({ flag: PREVIEW_CREW, denied: true }))
    act(() => { clearPreviewFlagWriteError() })
    expect(result.current).toBeNull()
  })

  it('reports any other refused toggle as a failed save, and clears it on the next accepted one', async () => {
    server = { crew: false }
    patchSpy.mockRejectedValueOnce(Object.assign(new Error('boom'), { status: 500 }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return usePreviewFlagWriteError()
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await waitFor(() => expect(result.current).toEqual({ flag: PREVIEW_CREW, denied: false }))
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await waitFor(() => expect(result.current).toBeNull())
    expect(server.crew).toBe(true)
  })

  it('does not report a carry the gateway refused', async () => {
    server = { crew: false }
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    patchSpy.mockRejectedValue(Object.assign(new Error('already set'), { status: 409 }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return usePreviewFlagWriteError()
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.webhooks', true, { ifUnset: true }))
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(result.current).toBeNull()
  })

  it('keeps the browser copy when the config cannot be read', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new Error('offline'))
    localStorage.setItem(PREVIEW_CREW, '1')
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(qc.getQueryState(['kirocrewConfig'])?.status).toBe('error'))
    expect(result.current).toBe(true)
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('carries a switch set before the server kept it up once, without replacing a stored one', async () => {
    server = { webhooks: false }
    localStorage.setItem(PREVIEW_CREW, '1')
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    localStorage.setItem(PREVIEW_ARTIFACT_DEPLOY, '0')
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(server.crew).toBe(true))
    expect(patchSpy).toHaveBeenCalledTimes(1)
    expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.crew', true, { ifUnset: true })
    // The stored webhooks value wins and an OFF copy is never carried.
    await waitFor(() => expect(readPreviewFlag(PREVIEW_WEBHOOKS)).toBe(false))
    expect(server.artifact_deploy).toBeUndefined()
    expect(result.current).toBe(true)
    // Later reads do not carry it again.
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(patchSpy).toHaveBeenCalledTimes(1)
  })

  it('does not retry a refused carry on every refetch', async () => {
    patchSpy.mockRejectedValue(new Error('owner only'))
    localStorage.setItem(PREVIEW_CREW, '1')
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(1))
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(patchSpy).toHaveBeenCalledTimes(1)
    expect(result.current).toBe(true)
  })

  it('puts a refused toggle back in a browser that has never synced, where the server holds no value', async () => {
    // Webhooks' carry is refused too, so this browser really stays unsynced.
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    patchSpy.mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.webhooks', true, { ifUnset: true }))
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.crew', true, undefined))
    await waitFor(() => expect(result.current).toBe(false))
    expect(localStorage.getItem(PREVIEW_FLAGS_SYNCED_KEY)).toBeNull()
    // Nor is the refused value carried up afterwards.
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(result.current).toBe(false)
    expect(patchSpy).not.toHaveBeenCalledWith('dashboard.feature_previews.crew', true, { ifUnset: true })
  })

  it('puts two refused clicks back to the value before the first, in an unsynced browser', async () => {
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    patchSpy.mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(1))
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    act(() => { setPreviewFlag(PREVIEW_CREW, false) })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(3))
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(result.current).toBe(false)
  })

  it('puts two refused clicks back to the value before the first when the config cannot be read', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new Error('offline'))
    patchSpy.mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(qc.getQueryState(['kirocrewConfig'])?.status).toBe('error'))
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    act(() => { setPreviewFlag(PREVIEW_CREW, false) })
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(result.current).toBe(false))
  })

  it('puts refused clicks queued behind a refused carry back to the browser value', async () => {
    localStorage.setItem(PREVIEW_CREW, '1')
    let refuseCarry!: () => void
    patchSpy.mockImplementationOnce(() => new Promise((_, reject) => {
      refuseCarry = () => reject(Object.assign(new Error('owner only'), { status: 403 }))
    }))
    let refuseFirstClick!: () => void
    patchSpy.mockImplementationOnce(() => new Promise((_, reject) => {
      refuseFirstClick = () => reject(Object.assign(new Error('owner only'), { status: 403 }))
    }))
    patchSpy.mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return [usePreviewFlagWriteError(), usePreviewFlag(PREVIEW_CREW)] as const
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.crew', true, { ifUnset: true }))
    act(() => { setPreviewFlag(PREVIEW_CREW, false) })
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await act(async () => { refuseCarry() })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(2))
    // The carry's own outcome is not reported while the clicks behind it are queued.
    expect(result.current[0]).toBeNull()
    await act(async () => { refuseFirstClick() })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(result.current[0]).toEqual({ flag: PREVIEW_CREW, denied: true }))
    expect(result.current[1]).toBe(true)
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(result.current[1]).toBe(true)
  })

  it('does not mark the browser synced when a carry lands while a toggle of another switch is in flight', async () => {
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    let releaseCarry!: () => void
    const accept = patchSpy.getMockImplementation()!
    patchSpy.mockImplementationOnce((...args: Parameters<typeof accept>) => new Promise(resolve => {
      releaseCarry = () => resolve(accept(...args))
    }))
    let refuseToggle!: () => void
    patchSpy.mockImplementationOnce(() => new Promise((_, reject) => {
      refuseToggle = () => reject(Object.assign(new Error('owner only'), { status: 403 }))
    }))
    localStorage.setItem(PREVIEW_CREW, '0')
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.webhooks', true, { ifUnset: true }))
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(2))
    await act(async () => { releaseCarry() })
    await waitFor(() => expect(server.webhooks).toBe(true))
    expect(localStorage.getItem(PREVIEW_FLAGS_SYNCED_KEY)).toBeNull()
    await act(async () => { refuseToggle() })
    await waitFor(() => expect(result.current).toBe(false))
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(result.current).toBe(false)
  })

  it('keeps another switch\'s notice when a different switch saves', async () => {
    server = { crew: false, webhooks: false }
    patchSpy.mockRejectedValueOnce(Object.assign(new Error('boom'), { status: 500 }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return usePreviewFlagWriteError()
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await waitFor(() => expect(result.current).toEqual({ flag: PREVIEW_CREW, denied: false }))
    act(() => { setPreviewFlag(PREVIEW_WEBHOOKS, true) })
    await waitFor(() => expect(server.webhooks).toBe(true))
    expect(result.current).toEqual({ flag: PREVIEW_CREW, denied: false })
  })

  it('reports a refused click only once the last queued click of that switch has settled', async () => {
    server = { crew: false }
    let refuseFirst!: () => void
    let acceptSecond!: () => void
    const accept = patchSpy.getMockImplementation()!
    patchSpy.mockImplementationOnce(() => new Promise((_, reject) => {
      refuseFirst = () => reject(Object.assign(new Error('boom'), { status: 500 }))
    }))
    patchSpy.mockImplementationOnce((...args: Parameters<typeof accept>) => new Promise(resolve => {
      acceptSecond = () => resolve(accept(...args))
    }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return [usePreviewFlagWriteError(), usePreviewFlag(PREVIEW_CREW)] as const
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    act(() => { setPreviewFlag(PREVIEW_CREW, false) })
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(1))
    await act(async () => { refuseFirst() })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(2))
    expect(result.current[0]).toBeNull()
    await act(async () => { acceptSecond() })
    await waitFor(() => expect(server.crew).toBe(true))
    await waitFor(() => expect(result.current[1]).toBe(true))
    expect(result.current[0]).toBeNull()
  })

  it('reports a 403 from a session that no longer authenticates as a failed save, not owner-only', async () => {
    server = { crew: false }
    patchSpy.mockRejectedValue(Object.assign(new Error('sign in'), { status: 403, authRequired: true }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return usePreviewFlagWriteError()
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await waitFor(() => expect(result.current).toEqual({ flag: PREVIEW_CREW, denied: false }))
  })

  it('counts a carry the gateway answered 409 as synced', async () => {
    patchSpy.mockRejectedValue(Object.assign(new Error('already set'), { status: 409 }))
    localStorage.setItem(PREVIEW_CREW, '1')
    mountSync(newClient())
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(localStorage.getItem(PREVIEW_FLAGS_SYNCED_KEY)).toBe('1'))
  })

  it('keeps a switch set in this browser when a toggle of it is refused before the first read lands', async () => {
    localStorage.setItem(PREVIEW_CREW, '1')
    localStorage.setItem(PREVIEW_WEBHOOKS, '0')
    let releaseRead!: () => void
    vi.mocked(api.kirocrewConfig).mockImplementationOnce(() => new Promise(resolve => {
      // Webhooks is held by the server and off in this browser, so the reconcile
      // that reads this body has a visible effect the test can wait for.
      releaseRead = () => resolve({ dashboard: { feature_previews: { webhooks: true } } } as Body)
    }))
    let refuse!: () => void
    patchSpy.mockImplementationOnce(() => new Promise((_, reject) => {
      refuse = () => reject(Object.assign(new Error('owner only'), { status: 403 }))
    }))
    const qc = newClient()
    const { result } = mountSync(qc)
    act(() => { setPreviewFlag(PREVIEW_CREW, false) })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(1))
    await act(async () => { releaseRead() })
    // The reconcile of that read has run (it turned Webhooks on) before the
    // refusal, so the read really landed while the toggle was in flight.
    await waitFor(() => expect(readPreviewFlag(PREVIEW_WEBHOOKS)).toBe(true))
    await act(async () => { refuse() })
    await waitFor(() => expect(result.current).toBe(true))
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(result.current).toBe(true)
    expect(localStorage.getItem(PREVIEW_FLAGS_SYNCED_KEY)).toBeNull()
  })

  it('reports a refused carry as on in this browser only, and keeps the switch on', async () => {
    patchSpy.mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    localStorage.setItem(PREVIEW_CREW, '1')
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return [usePreviewFlagWriteError(), usePreviewFlag(PREVIEW_CREW)] as const
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(result.current[0]).toEqual({ flag: PREVIEW_CREW, denied: false, carry: true }))
    expect(result.current[1]).toBe(true)
    expect(localStorage.getItem(PREVIEW_FLAGS_SYNCED_KEY)).toBeNull()
  })

  it('clears a refused carry notice once the server holds a value for that switch', async () => {
    patchSpy.mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    localStorage.setItem(PREVIEW_CREW, '1')
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return [usePreviewFlagWriteError(), usePreviewFlag(PREVIEW_CREW)] as const
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(result.current[0]).toEqual({ flag: PREVIEW_CREW, denied: true, carry: true }))
    // The owner saves the switch off on another surface; the refresh brings it here.
    server = { crew: false }
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    await waitFor(() => expect(result.current[1]).toBe(false))
    expect(result.current[0]).toBeNull()
  })

  it('keeps a refused toggle notice when the server value arrives', async () => {
    server = { crew: false }
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return [usePreviewFlagWriteError(), usePreviewFlag(PREVIEW_CREW)] as const
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    patchSpy.mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    await waitFor(() => expect(result.current[0]).toEqual({ flag: PREVIEW_CREW, denied: true }))
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(result.current[0]).toEqual({ flag: PREVIEW_CREW, denied: true })
    expect(result.current[1]).toBe(false)
  })

  it('applies rapid toggles of one flag in click order', async () => {
    const order: boolean[] = []
    const releases: Array<() => void> = []
    patchSpy.mockImplementation(async (path: string, value: unknown) => {
      order.push(value as boolean)
      await new Promise<void>(r => { releases.push(r) })
      server[path.replace('dashboard.feature_previews.', '')] = value as boolean
      return { ok: true }
    })
    const { result } = mountSync(newClient())
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW, true) })
    act(() => { setPreviewFlag(PREVIEW_CREW, false) })
    // The second write waits for the first to settle.
    await waitFor(() => expect(order).toEqual([true]))
    await act(async () => { releases[0]() })
    await waitFor(() => expect(order).toEqual([true, false]))
    await act(async () => { releases[1]() })
    await waitFor(() => expect(server.crew).toBe(false))
    await waitFor(() => expect(result.current).toBe(false))
  })

  it('reads a switch the server dropped as off once this browser has synced', async () => {
    server = { crew: true }
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(result.current).toBe(true))
    expect(localStorage.getItem(PREVIEW_FLAGS_SYNCED_KEY)).toBe('1')
    server = {}
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    await waitFor(() => expect(result.current).toBe(false))
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('reads a switch the server dropped as off when another switch failed to carry up', async () => {
    server = { crew: true }
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    patchSpy.mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(result.current).toBe(true))
    await waitFor(() => expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.webhooks', true, { ifUnset: true }))
    // The failed carry keeps the browser as a whole unsynced.
    await waitFor(() => expect(readPreviewFlag(PREVIEW_WEBHOOKS)).toBe(true))
    expect(localStorage.getItem(PREVIEW_FLAGS_SYNCED_KEY)).toBeNull()
    expect(JSON.parse(localStorage.getItem(PREVIEW_FLAGS_SERVER_BACKED_KEY) ?? '[]')).toContain(PREVIEW_CREW)
    // The crew key is deleted on the gateway.
    server = {}
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    await waitFor(() => expect(result.current).toBe(false))
    expect(patchSpy).toHaveBeenCalledTimes(1)
    expect(patchSpy).not.toHaveBeenCalledWith('dashboard.feature_previews.crew', expect.anything(), expect.anything())
    expect(server.crew).toBeUndefined()
    // The switch whose carry failed is still on in this browser.
    expect(readPreviewFlag(PREVIEW_WEBHOOKS)).toBe(true)
  })

  it('reads a dropped switch as off when the server-backed record cannot be stored', async () => {
    const setItem = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === PREVIEW_FLAGS_SERVER_BACKED_KEY) throw new DOMException('full', 'QuotaExceededError')
      setItem.call(this, key, value)
    })
    server = { crew: true }
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    patchSpy.mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(result.current).toBe(true))
    await waitFor(() => expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.webhooks', true, { ifUnset: true }))
    expect(localStorage.getItem(PREVIEW_FLAGS_SERVER_BACKED_KEY)).toBeNull()
    server = {}
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    await waitFor(() => expect(result.current).toBe(false))
    expect(patchSpy).toHaveBeenCalledTimes(1)
    expect(server.crew).toBeUndefined()
    expect(readPreviewFlag(PREVIEW_WEBHOOKS)).toBe(true)
  })

  it('reads an unreadable server-backed record as none and still carries a browser switch up', async () => {
    localStorage.setItem(PREVIEW_FLAGS_SERVER_BACKED_KEY, '{not json')
    localStorage.setItem(PREVIEW_CREW, '1')
    const { result } = mountSync(newClient())
    await waitFor(() => expect(server.crew).toBe(true))
    expect(result.current).toBe(true)
    await waitFor(() => expect(JSON.parse(localStorage.getItem(PREVIEW_FLAGS_SERVER_BACKED_KEY) ?? '[]')).toEqual([PREVIEW_CREW]))
  })

  it('carries the retired Remote crew sessions switch up as Chat on a crew', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    mountSync(newClient())
    await waitFor(() => expect(server.remote_crew_chat).toBe(true))
    expect(patchSpy).toHaveBeenCalledTimes(1)
    expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.remote_crew_chat', true, { ifUnset: true })
    await waitFor(() => expect(readPreviewFlag(PREVIEW_INSTANCE_SESSIONS)).toBe(false))
    expect(readPreviewFlag(PREVIEW_REMOTE_CREW_CHAT)).toBe(true)
  })

  it('never lets the retired key turn Chat on a crew back on after it was turned off', async () => {
    server = { remote_crew_chat: false }
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    mountSync(newClient())
    await waitFor(() => expect(readPreviewFlag(PREVIEW_INSTANCE_SESSIONS)).toBe(false))
    expect(readPreviewFlag(PREVIEW_REMOTE_CREW_CHAT)).toBe(false)
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('sends no write for the retired key when Chat on a crew is turned off', async () => {
    server = { remote_crew_chat: true }
    mountSync(newClient())
    await waitFor(() => expect(readPreviewFlag(PREVIEW_REMOTE_CREW_CHAT)).toBe(true))
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    act(() => { setRemoteCrewChat(false) })
    await waitFor(() => expect(server.remote_crew_chat).toBe(false))
    expect(patchSpy).toHaveBeenCalledTimes(1)
    expect(readPreviewFlag(PREVIEW_INSTANCE_SESSIONS)).toBe(false)
  })

  it('writes nothing to the server once the shell unmounts', async () => {
    const { unmount } = mountSync(newClient())
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    unmount()
    expect(setPreviewFlag(PREVIEW_CREW, true)).toBe(true)
    expect(patchSpy).not.toHaveBeenCalled()
  })
})
