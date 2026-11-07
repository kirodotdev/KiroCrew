/**
 * #32031: a refusal that means "not permitted" (the relay's `relay_apps_teammate`
 * for a teammate, a 403 `owner_only`) is read as the answer, while a real failure
 * (5xx, network, a 403 with another code, a session-expiry 403) stays an error.
 *
 * Covers the predicate, the shared retry policy, the app list's stop-asking rule
 * and the composer's session-control error.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiError, isPermissionRefusal, isRelayAppsRefusal } from '../api/apiError'
import { retryPolicy } from '../api/queryClient'
import { createAppsEndpoints } from '../api/client/apps'
import type { ClientTransport } from '../api/client/transport'
import { renderHookWithProviders } from './helpers'

function renderControls() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client: qc }, children)
  return { qc, ...renderHook(() => useSessionControls(), { wrapper }) }
}
import { useSessionControls } from '../hooks/useSessionControls'
import { api } from '../api/client'

vi.mock('../api/client', () => ({ api: { listApps: vi.fn() } }))
const mockListApps = api.listApps as unknown as ReturnType<typeof vi.fn>

const body = (code: string) => JSON.stringify({ error: 'refused', code })
const relayApps = () => new ApiError(403, "Apps aren't available to teammates through the relay.", body('relay_apps_teammate'))
const ownerOnly = () => new ApiError(403, 'owner authorization required', body('owner_only'))
const sessionExpired = () => new ApiError(403, 'session expired', body('owner_only'), true)
const otherForbidden = () => new ApiError(403, 'nope', body('app_not_enabled'))
const serverError = () => new ApiError(500, 'boom', body('owner_only'))

describe('isPermissionRefusal', () => {
  it('is true for the relay teammate refusal and an owner_only 403', () => {
    expect(isPermissionRefusal(relayApps())).toBe(true)
    expect(isPermissionRefusal(ownerOnly())).toBe(true)
    // Duck-typed, like the suites that reject with a plain Error carrying a status.
    expect(isPermissionRefusal(Object.assign(new Error('x'), { status: 403, body: body('owner_only') }))).toBe(true)
  })

  it('is false for every real failure that reaches the same readers', () => {
    expect(isPermissionRefusal(sessionExpired())).toBe(false)
    expect(isPermissionRefusal(otherForbidden())).toBe(false)
    expect(isPermissionRefusal(serverError())).toBe(false)
    expect(isPermissionRefusal(new ApiError(403, 'x', '<html>denied</html>'))).toBe(false)
    expect(isPermissionRefusal(new ApiError(403, 'x', ''))).toBe(false)
    expect(isPermissionRefusal(new TypeError('Failed to fetch'))).toBe(false)
    expect(isPermissionRefusal(null)).toBe(false)
    expect(isPermissionRefusal('owner_only')).toBe(false)
  })

  it('isRelayAppsRefusal names only the relay teammate refusal', () => {
    expect(isRelayAppsRefusal(relayApps())).toBe(true)
    expect(isRelayAppsRefusal(ownerOnly())).toBe(false)
    expect(isRelayAppsRefusal(serverError())).toBe(false)
  })
})

describe('retryPolicy', () => {
  it('never retries a permission refusal', () => {
    expect(retryPolicy(0, relayApps())).toBe(false)
    expect(retryPolicy(0, ownerOnly())).toBe(false)
  })

  it('keeps the one retry for a session-expiry 403, another 403 and a 5xx', () => {
    expect(retryPolicy(0, sessionExpired())).toBe(true)
    expect(retryPolicy(0, otherForbidden())).toBe(true)
    expect(retryPolicy(0, serverError())).toBe(true)
    expect(retryPolicy(1, serverError())).toBe(false)
  })
})

describe('listApps after a relay teammate refusal', () => {
  function endpoints(answers: Array<() => Promise<unknown>>) {
    const fetch = vi.fn(async () => ({}) as Response)
    let i = 0
    const jApps = vi.fn(async () => answers[Math.min(i++, answers.length - 1)]())
    const transport = { jfetch: fetch, jApps, j: vi.fn() } as unknown as ClientTransport
    return { fetch, jApps, listApps: createAppsEndpoints(transport).platform.listApps }
  }

  it('asks once, then hands back the same refusal without a request', async () => {
    const refused = relayApps()
    const { fetch, listApps } = endpoints([() => Promise.reject(refused)])
    await expect(listApps()).rejects.toBe(refused)
    await expect(listApps()).rejects.toBe(refused)
    await expect(listApps()).rejects.toBe(refused)
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('keeps asking after an owner_only refusal, a 5xx or a network failure', async () => {
    const { fetch, listApps } = endpoints([
      () => Promise.reject(ownerOnly()),
      () => Promise.reject(serverError()),
      () => Promise.reject(new TypeError('Failed to fetch')),
      () => Promise.resolve([]),
    ])
    await expect(listApps()).rejects.toBeInstanceOf(ApiError)
    await expect(listApps()).rejects.toBeInstanceOf(ApiError)
    await expect(listApps()).rejects.toBeInstanceOf(TypeError)
    await expect(listApps()).resolves.toEqual([])
    expect(fetch).toHaveBeenCalledTimes(4)
  })

  it('reads the owner list through jApps on every call', async () => {
    const { fetch, jApps, listApps } = endpoints([() => Promise.resolve([])])
    await listApps()
    await listApps()
    expect(fetch).toHaveBeenCalledWith('/api/apps')
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(jApps).toHaveBeenCalledTimes(2)
  })
})

describe('useSessionControls error', () => {
  beforeEach(() => vi.clearAllMocks())

  it.each([['relay_apps_teammate', relayApps], ['owner_only', ownerOnly]])(
    'is null for a %s refusal, with no controls',
    async (_code, make) => {
      mockListApps.mockRejectedValue(make())
      const { qc, result } = renderControls()
      // The refusal has settled into the query, so a null error is the answer.
      await waitFor(() => expect(qc.getQueryState(['apps'])?.status).toBe('error'))
      expect(result.current.error).toBeNull()
      expect(result.current.controls).toEqual([])
    },
  )

  it('is still returned for a real failure', async () => {
    mockListApps.mockRejectedValue(serverError())
    const { result } = renderHookWithProviders(() => useSessionControls())
    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.controls).toEqual([])
  })
})
