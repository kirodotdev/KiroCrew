/**
 * `backend_refused` on an enable or update response is a warning, not a
 * success (#27840). The gateway names a backend the host refused (a shell
 * launcher on native Windows) in that field; the dashboard must route it to
 * the page's notice instead of the green success toast.
 *
 * `i18nT` is mocked to `key {params}` so assertions pin the KEY and the
 * interpolated name and reason, not any locale's copy.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const { apiUpdateApp, apiEnableApp } = vi.hoisted(() => ({ apiUpdateApp: vi.fn(), apiEnableApp: vi.fn() }))

vi.mock('../../api/client', () => ({
  api: {
    updateApp: (...a: unknown[]) => apiUpdateApp(...a),
    enableApp: (...a: unknown[]) => apiEnableApp(...a),
  },
}))

vi.mock('../../rum', () => ({ recordEvent: () => {} }))

vi.mock('../../i18n/t', async importOriginal => {
  const orig = await importOriginal<typeof import('../../i18n/t')>()
  return {
    ...orig,
    i18nT: (key: string, params?: Record<string, unknown>) =>
      params ? `${key} ${JSON.stringify(params)}` : key,
  }
})

import { backendRefusedNotice, backendRefusedReason } from '../../components/appstore/backendRefused'
import { useAppActions } from './useAppActions'
import { useAppUpdates } from './useAppUpdates'
import type { AppsData } from './useAppsData'

const KEY = 'pages.appsPage.backend_refused_notice'
const REASON = 'exec backends are not supported on native Windows'

const notesApp = {
  name: 'notes', displayName: 'Notes', source: '/home/u/apps/notes',
  origin: 'local', enabled: true, lifecycle: 'gateway',
} as unknown as AppsData['apps'][number]
const docsApp = {
  name: 'docs', displayName: 'Docs', source: '/home/u/apps/docs',
  origin: 'local', enabled: true, lifecycle: 'gateway',
} as unknown as AppsData['apps'][number]

describe('backendRefusedNotice', () => {
  it('names the app and the reason when the response carries a refusal', () => {
    expect(backendRefusedNotice('Notes', { backend_refused: REASON }))
      .toBe(`${KEY} ${JSON.stringify({ name: 'Notes', reason: REASON })}`)
  })

  it('is empty for a response without a refusal, a blank one, or a non-string', () => {
    expect(backendRefusedNotice('Notes', { ok: true })).toBe('')
    expect(backendRefusedNotice('Notes', { backend_refused: '   ' })).toBe('')
    expect(backendRefusedNotice('Notes', { backend_refused: 42 })).toBe('')
    expect(backendRefusedNotice('Notes', undefined)).toBe('')
    expect(backendRefusedReason(null)).toBe('')
  })
})

function setupUpdates(pending: boolean = true) {
  const setError = vi.fn()
  const setSuccess = vi.fn()
  const utils = renderHook(() => useAppUpdates({
    apps: [notesApp, docsApp],
    // `pending: false` is a Sync: no registry row offers these path installs an update.
    updatables: (pending ? [notesApp, docsApp] : []) as unknown as AppsData['updatables'],
    announceAppsChanged: vi.fn(),
    updateApp: vi.fn(),
    setError,
    setSuccess,
  }))
  return { ...utils, setError, setSuccess }
}

describe('useAppUpdates — a refused backend is a warning', () => {
  beforeEach(() => { apiUpdateApp.mockReset() })

  it('an in-place update reports the refusal through setError, not setSuccess', async () => {
    apiUpdateApp.mockResolvedValueOnce({ ok: true, backend_refused: REASON })
    const { result, setError, setSuccess } = setupUpdates(false)

    await act(async () => { await result.current.runUpdate('notes') })

    expect(setSuccess).not.toHaveBeenCalled()
    expect(setError).toHaveBeenLastCalledWith(`${KEY} ${JSON.stringify({ name: 'Notes', reason: REASON })}`)
  })

  it('Update All lists the refusal in the batch notice instead of a success', async () => {
    apiUpdateApp
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({ ok: true, backend_refused: REASON })
    const { result, setError, setSuccess } = setupUpdates()

    await act(async () => { await result.current.updateAll() })

    expect(setSuccess).not.toHaveBeenCalled()
    expect(setError).toHaveBeenLastCalledWith(`${KEY} ${JSON.stringify({ name: 'Docs', reason: REASON })}`)
  })

  it('an update without a refusal still reports success', async () => {
    apiUpdateApp.mockResolvedValueOnce({ ok: true })
    const { result, setSuccess } = setupUpdates(false)

    await act(async () => { await result.current.runUpdate('notes') })

    expect(setSuccess).toHaveBeenCalledWith(expect.stringContaining('pages.appsPage.synced_from_its_source_directory'))
  })
})

describe('useAppActions.runEnable — a refused backend is a warning', () => {
  beforeEach(() => { apiEnableApp.mockReset() })

  function setupActions() {
    const qc = new QueryClient()
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>
    )
    return renderHook(() => useAppActions({
      apps: [notesApp],
      browseApps: [],
      appsError: null, registryError: null, announceAppsChanged: () => {}, sources: [],
      registriesUnknown: false,
    } as never), { wrapper })
  }

  it('shows the refusal in the page notice after a successful enable', async () => {
    apiEnableApp.mockResolvedValueOnce({ ok: true, backend_refused: REASON })
    const { result } = setupActions()

    await act(async () => { await result.current.runEnable('notes') })

    expect(result.current.displayError).toBe(`${KEY} ${JSON.stringify({ name: 'Notes', reason: REASON })}`)
  })

  it('leaves the notice empty when the backend was not refused', async () => {
    apiEnableApp.mockResolvedValueOnce({ ok: true })
    const { result } = setupActions()

    await act(async () => { await result.current.runEnable('notes') })

    expect(result.current.displayError).toBe('')
  })
})
