/**
 * Tests for ChatPage's session-control status wiring.
 *
 * ChatPage owns what the hook and the host cannot: the folder identity
 * forwarded to the status poll, and which control is open. Each has a failure
 * mode that is invisible in the UI — a chip that never colours, a folder-scoped
 * app that cannot answer — so they are asserted here.
 *
 * The poll goes through `api.appSessionStatus` rather than a bare `fetch`, so
 * that is what these tests mock: routing through the api client is what carries
 * the X-Session-Key gate and `checkSessionExpired`, and a test that stubbed
 * `fetch` would pass just as well with the guarantee removed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

import { waitFor } from '@testing-library/react'
import { renderHookWithProviders } from './helpers'
import { useSessionControlStatuses, normalizeStatus, safeStatusPath } from '../hooks/useSessionControls'
import type { ResolvedSessionControl } from '../hooks/useSessionControls'
import { api } from '../api/client'

vi.mock('../api/client', () => ({ api: { appSessionStatus: vi.fn() } }))

const mockStatus = api.appSessionStatus as unknown as ReturnType<typeof vi.fn>

/**
 * The statuses half of the hook's result.
 *
 * These tests are about what ChatPage forwards and what the chips end up
 * showing, so they read statuses only; the error half is asserted separately in
 * the test that covers it. Wrapping keeps every assertion below reading
 * `result.current['key']` rather than `result.current.statuses['key']`.
 */
const useStatusesOf = (...args: Parameters<typeof useSessionControlStatuses>) =>
  useSessionControlStatuses(...args).statuses

const control = (over: Partial<ResolvedSessionControl> = {}): ResolvedSessionControl => ({
  key: 'test-app:scope',
  appName: 'test-app',
  appDisplayName: 'Test App',
  appVersion: '0.1.0',
  id: 'scope',
  entryPoint: 'dist/session-control.mjs',
  label: 'Scope',
  icon: 'Tag',
  allowedApi: [],
  allowedEvents: [],
  statusPath: 'session-status',
  // The resolver always sets this, so the fixture must too — a fixture that
  // omits it would forward `undefined` and hide which prefix is selected.
  processBacked: false,
  ...over,
})

describe('useSessionControlStatuses — what ChatPage forwards', () => {
  beforeEach(() => vi.clearAllMocks())

  it('sends the session key and the folder identity', async () => {
    // A folder-scoped app cannot answer without the folder, and a brand-new chat
    // is exactly the case where it has no record of its own to fall back on.
    mockStatus.mockResolvedValue({ state: 'ok', tooltip: 'bound' })
    const { result } = renderHookWithProviders(() =>
      useStatusesOf([control()], 'dashboard:chat-2-1787502679', '97fff46e2c09', 'Backend'),
    )
    await waitFor(() =>
      expect(result.current['test-app:scope']?.state).toBe('ok'),
    )
    expect(mockStatus).toHaveBeenCalledWith(
      'test-app',
      'session-status',
      {
        session_key: 'dashboard:chat-2-1787502679',
        folder_id: '97fff46e2c09',
        folder_name: 'Backend',
      },
      // The backend kind decides which of the two serving prefixes the status
      // route is resolved against, so it has to be forwarded with the rest.
      false,
    )
  })

  it('forwards the process-backed flag so the proxy prefix is used', async () => {
    mockStatus.mockResolvedValue({ state: 'ok', tooltip: 'bound' })
    const { result } = renderHookWithProviders(() =>
      useStatusesOf(
        [{ ...control(), processBacked: true }],
        'dashboard:chat-2-1787502679',
        '',
        '',
      ),
    )
    await waitFor(() => expect(result.current['test-app:scope']?.state).toBe('ok'))
    expect(mockStatus).toHaveBeenCalledWith(
      'test-app',
      'session-status',
      { session_key: 'dashboard:chat-2-1787502679' },
      true,
    )
  })

  it('omits the folder when the chat is at the top level', async () => {
    mockStatus.mockResolvedValue({ state: 'ok' })
    renderHookWithProviders(() => useStatusesOf([control()], 'chat-1', '', ''))
    await waitFor(() => expect(mockStatus).toHaveBeenCalled())
    expect(mockStatus.mock.calls[0][2]).toEqual({ session_key: 'chat-1' })
  })

  it('costs no request for a control that declares no statusPath', async () => {
    mockStatus.mockResolvedValue({ state: 'ok' })
    renderHookWithProviders(() =>
      useStatusesOf([control({ statusPath: '' })], 'chat-1', '', ''),
    )
    await new Promise(r => setTimeout(r, 20))
    expect(mockStatus).not.toHaveBeenCalled()
  })

  it('costs no request before the chat has a session', async () => {
    // `enabled: !!sessionKey` — there is nothing to ask about yet.
    mockStatus.mockResolvedValue({ state: 'ok' })
    renderHookWithProviders(() => useStatusesOf([control()], '', '', ''))
    await new Promise(r => setTimeout(r, 20))
    expect(mockStatus).not.toHaveBeenCalled()
  })

  it('drops a none state rather than reporting it', async () => {
    // The map is read as "has state"; a none entry would tint nothing but would
    // make every caller check the value as well as the key.
    mockStatus.mockResolvedValue({ state: 'none' })
    const { result } = renderHookWithProviders(() =>
      useStatusesOf([control()], 'chat-1', '', ''),
    )
    await waitFor(() => expect(mockStatus).toHaveBeenCalled())
    expect(result.current['test-app:scope']).toBeUndefined()
  })

  it('leaves the chip stateless when the poll rejects', async () => {
    // The STATUS still fails closed: the api client throws ApiError on a non-ok
    // response, and an unreachable app must not colour the chip or break the
    // composer. It does now report the failure — see the next test — but the
    // status map itself stays empty.
    mockStatus.mockRejectedValue(new Error('HTTP 503'))
    const { result } = renderHookWithProviders(() =>
      useStatusesOf([control()], 'chat-1', '', ''),
    )
    await waitFor(() => expect(mockStatus).toHaveBeenCalled())
    expect(result.current).toEqual({})
  })

  it('reports a failing probe instead of swallowing it', async () => {
    // A stateless chip is indistinguishable from a control that has no state to
    // report, so failing closed without reporting leaves the user nothing to act
    // on. ChatPage renders this error above the composer.
    mockStatus.mockRejectedValue(new Error('HTTP 503'))
    const { result } = renderHookWithProviders(() =>
      useSessionControlStatuses([control()], 'chat-1', '', ''),
    )
    await waitFor(() => expect(result.current.error).toBeTruthy())
    expect(result.current.error?.message).toContain('HTTP 503')
    expect(result.current.statuses).toEqual({})
  })

  it('re-polls when the session changes, because state is per session', async () => {
    // One hook instance and therefore ONE cache: renderHookWithProviders builds
    // a fresh QueryClient per call, so rendering the hook twice would issue one
    // request each whatever the query key was — and would pass just as well
    // with sessionKey dropped from the key. Driving a single instance through a
    // rerender is what actually proves the session participates in the key.
    mockStatus.mockResolvedValue({ state: 'ok' })
    let sessionKey = 'chat-1'
    const { rerender } = renderHookWithProviders(() =>
      useStatusesOf([control()], sessionKey, '', ''),
    )
    await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(1))
    sessionKey = 'chat-2'
    rerender()
    await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(2))
    expect(mockStatus.mock.calls[1][2].session_key).toBe('chat-2')
  })

  it('does not re-poll when only the array identity changes', async () => {
    // ChatPage re-renders constantly; a fresh array with the same contents must
    // not cost a request per render. The query key is derived from the control's
    // fields, not the array.
    mockStatus.mockResolvedValue({ state: 'ok' })
    const { rerender } = renderHookWithProviders(() =>
      useStatusesOf([control()], 'chat-1', '', ''),
    )
    await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(1))
    rerender()
    await new Promise(r => setTimeout(r, 20))
    expect(mockStatus).toHaveBeenCalledTimes(1)
  })

  it('polls both controls when one app declares two sharing a statusPath', async () => {
    // Regression for AutoSDE f-737449bb: the query key omitted the control key,
    // so two controls with distinct ids and the same status route produced
    // identical keys. React Query deduped them into a single query whose result
    // names one control, leaving the sibling chip permanently stateless.
    mockStatus.mockImplementation(async () => ({ state: 'ok', tooltip: 'bound' }))
    const { result } = renderHookWithProviders(() =>
      useStatusesOf(
        [
          control({ key: 'app:one', id: 'one' }),
          control({ key: 'app:two', id: 'two' }),
        ],
        'chat-1',
        '',
        '',
      ),
    )
    await waitFor(() => expect(Object.keys(result.current)).toHaveLength(2))
    expect(result.current['app:one']?.state).toBe('ok')
    expect(result.current['app:two']?.state).toBe('ok')
    expect(mockStatus).toHaveBeenCalledTimes(2)
  })

  it('normalizeStatus bounds a hostile tooltip', () => {
    expect(normalizeStatus({ state: 'ok', tooltip: 'x'.repeat(9999) }).tooltip).toHaveLength(200)
  })

  it('a control whose statusPath was refused is never polled', async () => {
    // safeStatusPath fails closed to '', and an empty statusPath costs no
    // request — so a hostile manifest cannot reach the network at all.
    expect(safeStatusPath('x/../../other-app/secret')).toBe('')
    mockStatus.mockResolvedValue({ state: 'ok' })
    renderHookWithProviders(() =>
      useStatusesOf(
        [control({ statusPath: safeStatusPath('x/../../other-app/secret') })],
        'chat-1',
        '',
        '',
      ),
    )
    await new Promise(r => setTimeout(r, 20))
    expect(mockStatus).not.toHaveBeenCalled()
  })
})
