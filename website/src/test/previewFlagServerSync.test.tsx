/**
 * Feature previews are kept by the gateway (`dashboard.feature_previews.<name>`)
 * with localStorage as a cache. These tests pin the three parts of that contract:
 * the server copy wins over the cache, a toggle is written to the server, and a
 * failed config read leaves the cache in charge.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { ReactNode } from 'react'
import { renderHook, act, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import {
  __resetPreviewFlagServerSyncForTests, clearPreviewFlagWriteError, usePreviewFlagServerSync, usePreviewFlagWriteError,
} from '../hooks/usePreviewFlagServerSync'
import * as previewFlags from '../utils/previewFlags'
import {
  PREVIEW_ARTIFACT_DEPLOY, PREVIEW_CREW_MODE, PREVIEW_FLAGS, PREVIEW_FLAG_PREFIX, PREVIEW_INSTANCE_SESSIONS,
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

/** Mount the sync next to a reader of the Ultra Mode flag, as the app shell does. */
function mountSync(qc: QueryClient) {
  return renderHook(() => {
    usePreviewFlagServerSync()
    return usePreviewFlag(PREVIEW_CREW_MODE)
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
  patchSpy = vi.spyOn(api, 'patchConfig').mockImplementation(async (path: string, value: unknown) => {
    server[path.replace('dashboard.feature_previews.', '')] = value as boolean
    return { ok: true }
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('preview flag config paths', () => {
  it('names each flag by its snake_case config path', () => {
    expect(previewFlagConfigPath(PREVIEW_CREW_MODE)).toBe('dashboard.feature_previews.crew_mode')
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
  it('turns Ultra Mode on from the server when this browser has no copy', async () => {
    server = { crew_mode: true }
    const { result } = mountSync(newClient())
    await waitFor(() => expect(result.current).toBe(true))
    expect(localStorage.getItem(PREVIEW_CREW_MODE)).toBe('1')
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('lets the server copy win over a stale browser copy', async () => {
    server = { crew_mode: false }
    localStorage.setItem(PREVIEW_CREW_MODE, '1')
    const { result } = mountSync(newClient())
    await waitFor(() => expect(result.current).toBe(false))
    expect(readPreviewFlag(PREVIEW_CREW_MODE)).toBe(false)
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('reads a switch set only in this browser as off, without writing it', async () => {
    localStorage.setItem(PREVIEW_CREW_MODE, '1')
    const { result } = mountSync(newClient())
    await waitFor(() => expect(result.current).toBe(false))
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('clears the keys the one-time carry-up kept', async () => {
    localStorage.setItem('kc-feature-previews-synced', '1')
    localStorage.setItem('kc-feature-previews-server-backed', JSON.stringify([PREVIEW_CREW_MODE]))
    mountSync(newClient())
    await waitFor(() => expect(localStorage.getItem('kc-feature-previews-synced')).toBeNull())
    expect(localStorage.getItem('kc-feature-previews-server-backed')).toBeNull()
  })

  it('writes a toggle to the server', async () => {
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { expect(setPreviewFlag(PREVIEW_CREW_MODE, true)).toBe(true) })
    expect(result.current).toBe(true)
    await waitFor(() => expect(server).toEqual({ crew_mode: true }))
    expect(patchSpy).toHaveBeenCalledWith('dashboard.feature_previews.crew_mode', true)
    await waitFor(() => expect(qc.getQueryData<Body>(['kirocrewConfig'])?.dashboard?.feature_previews).toEqual({ crew_mode: true }))
    expect(result.current).toBe(true)
  })

  it('does not flip a switch back while its write is still in flight', async () => {
    let release: () => void = () => {}
    patchSpy.mockImplementation(async (path: string, value: unknown) => {
      await new Promise<void>(r => { release = r })
      server[path.replace('dashboard.feature_previews.', '')] = value as boolean
      return { ok: true }
    })
    server = { crew_mode: false }
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, true) })
    // A config body read before the write landed still says off.
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(result.current).toBe(true)
    await act(async () => { release() })
    await waitFor(() => expect(server.crew_mode).toBe(true))
    await waitFor(() => expect(qc.getQueryData<Body>(['kirocrewConfig'])?.dashboard?.feature_previews).toEqual({ crew_mode: true }))
    expect(result.current).toBe(true)
  })

  it('puts a refused write right from the server copy', async () => {
    server = { crew_mode: false }
    patchSpy.mockRejectedValue(new Error('owner only'))
    const { result } = mountSync(newClient())
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, true) })
    expect(result.current).toBe(true)
    await waitFor(() => expect(result.current).toBe(false))
  })

  it('reports a toggle the gateway refused as owner-only on a 403', async () => {
    server = { crew_mode: false }
    patchSpy.mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return usePreviewFlagWriteError()
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    expect(result.current).toBeNull()
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, true) })
    await waitFor(() => expect(result.current).toEqual({ flag: PREVIEW_CREW_MODE, denied: true }))
    act(() => { clearPreviewFlagWriteError() })
    expect(result.current).toBeNull()
  })

  it('reports any other refused toggle as a failed save, and clears it on the next accepted one', async () => {
    server = { crew_mode: false }
    patchSpy.mockRejectedValueOnce(Object.assign(new Error('boom'), { status: 500 }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return usePreviewFlagWriteError()
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, true) })
    await waitFor(() => expect(result.current).toEqual({ flag: PREVIEW_CREW_MODE, denied: false }))
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, true) })
    await waitFor(() => expect(result.current).toBeNull())
    expect(server.crew_mode).toBe(true)
  })

  it('keeps the browser copy when the config cannot be read', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new Error('offline'))
    localStorage.setItem(PREVIEW_CREW_MODE, '1')
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(qc.getQueryState(['kirocrewConfig'])?.status).toBe('error'))
    expect(result.current).toBe(true)
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('puts two refused clicks back to the value before the first when the config cannot be read', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new Error('offline'))
    patchSpy.mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(qc.getQueryState(['kirocrewConfig'])?.status).toBe('error'))
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, true) })
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, false) })
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, true) })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(result.current).toBe(false))
  })

  it('keeps another switch\'s notice when a different switch saves', async () => {
    server = { crew_mode: false, webhooks: false }
    patchSpy.mockRejectedValueOnce(Object.assign(new Error('boom'), { status: 500 }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return usePreviewFlagWriteError()
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, true) })
    await waitFor(() => expect(result.current).toEqual({ flag: PREVIEW_CREW_MODE, denied: false }))
    act(() => { setPreviewFlag(PREVIEW_WEBHOOKS, true) })
    await waitFor(() => expect(server.webhooks).toBe(true))
    expect(result.current).toEqual({ flag: PREVIEW_CREW_MODE, denied: false })
  })

  it('reports a refused click only once the last queued click of that switch has settled', async () => {
    server = { crew_mode: false }
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
      return [usePreviewFlagWriteError(), usePreviewFlag(PREVIEW_CREW_MODE)] as const
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, true) })
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, false) })
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, true) })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(1))
    await act(async () => { refuseFirst() })
    await waitFor(() => expect(patchSpy).toHaveBeenCalledTimes(2))
    expect(result.current[0]).toBeNull()
    await act(async () => { acceptSecond() })
    await waitFor(() => expect(server.crew_mode).toBe(true))
    await waitFor(() => expect(result.current[1]).toBe(true))
    expect(result.current[0]).toBeNull()
  })

  it('reports a 403 from a session that no longer authenticates as a failed save, not owner-only', async () => {
    server = { crew_mode: false }
    patchSpy.mockRejectedValue(Object.assign(new Error('sign in'), { status: 403, authRequired: true }))
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return usePreviewFlagWriteError()
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, true) })
    await waitFor(() => expect(result.current).toEqual({ flag: PREVIEW_CREW_MODE, denied: false }))
  })

  it('keeps a refused toggle notice when the server value arrives', async () => {
    server = { crew_mode: false }
    const qc = newClient()
    const { result } = renderHook(() => {
      usePreviewFlagServerSync()
      return [usePreviewFlagWriteError(), usePreviewFlag(PREVIEW_CREW_MODE)] as const
    }, { wrapper: harness(qc) })
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    patchSpy.mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, true) })
    await waitFor(() => expect(result.current[0]).toEqual({ flag: PREVIEW_CREW_MODE, denied: true }))
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(result.current[0]).toEqual({ flag: PREVIEW_CREW_MODE, denied: true })
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
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, true) })
    act(() => { setPreviewFlag(PREVIEW_CREW_MODE, false) })
    // The second write waits for the first to settle.
    await waitFor(() => expect(order).toEqual([true]))
    await act(async () => { releases[0]() })
    await waitFor(() => expect(order).toEqual([true, false]))
    await act(async () => { releases[1]() })
    await waitFor(() => expect(server.crew_mode).toBe(false))
    await waitFor(() => expect(result.current).toBe(false))
  })

  it('reads a switch the server dropped as off', async () => {
    server = { crew_mode: true }
    const qc = newClient()
    const { result } = mountSync(qc)
    await waitFor(() => expect(result.current).toBe(true))
    server = {}
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    await waitFor(() => expect(result.current).toBe(false))
    expect(patchSpy).not.toHaveBeenCalled()
  })

  it('clears the retired Remote crew sessions key on load, even when the config cannot be read', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new Error('offline'))
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    const qc = newClient()
    mountSync(qc)
    await waitFor(() => expect(qc.getQueryState(['kirocrewConfig'])?.status).toBe('error'))
    expect(readPreviewFlag(PREVIEW_INSTANCE_SESSIONS)).toBe(false)
    expect(readPreviewFlag(PREVIEW_REMOTE_CREW_CHAT)).toBe(false)
    expect(patchSpy).not.toHaveBeenCalled()
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
    expect(setPreviewFlag(PREVIEW_CREW_MODE, true)).toBe(true)
    expect(patchSpy).not.toHaveBeenCalled()
  })
})
