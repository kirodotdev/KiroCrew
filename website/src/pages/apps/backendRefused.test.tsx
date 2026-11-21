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

import { appActionNotice, backendRefusedNotice, backendRefusedReason, botChatStopFailedNotice, botChatStopFailedSlugs } from '../../components/appstore/backendRefused'
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

const WARN_ON = 'pages.appsPage.action_warnings_enabled'
const WARN_OFF = 'pages.appsPage.action_warnings_disabled'

describe('appActionNotice', () => {
  it('says which action landed, then names the gateway warnings', () => {
    expect(appActionNotice('Notes', { ok: true, warnings: ['hooks failed: boom', 'config sync failed: denied'] }, 'disable'))
      .toBe(`${WARN_OFF} ${JSON.stringify({ name: 'Notes', warnings: 'hooks failed: boom; config sync failed: denied' })}`)
    expect(appActionNotice('Notes', { ok: true, warnings: ['hooks failed: boom'] }, 'enable'))
      .toBe(`${WARN_ON} ${JSON.stringify({ name: 'Notes', warnings: 'hooks failed: boom' })}`)
  })

  it('shows a refused backend and the warnings together, and is empty with no usable warnings', () => {
    expect(appActionNotice('Notes', { backend_refused: REASON, warnings: ['hooks failed: boom'] }, 'enable'))
      .toBe(`${KEY} ${JSON.stringify({ name: 'Notes', reason: REASON })} ${WARN_ON} ${JSON.stringify({ name: 'Notes', warnings: 'hooks failed: boom' })}`)
    expect(appActionNotice('Notes', { backend_refused: REASON }, 'enable'))
      .toBe(`${KEY} ${JSON.stringify({ name: 'Notes', reason: REASON })}`)
    expect(appActionNotice('Notes', { ok: true }, 'disable')).toBe('')
    expect(appActionNotice('Notes', { warnings: ['  ', 7] }, 'disable')).toBe('')
    expect(appActionNotice('Notes', { warnings: 'hooks failed' }, 'disable')).toBe('')
    expect(appActionNotice('Notes', undefined, 'disable')).toBe('')
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

  it('shows the gateway warnings in the page notice after a successful enable', async () => {
    apiEnableApp.mockResolvedValueOnce({ ok: true, warnings: ['hooks failed: boom'] })
    const { result } = setupActions()

    await act(async () => { await result.current.runEnable('notes') })

    expect(result.current.displayError).toBe(`${WARN_ON} ${JSON.stringify({ name: 'Notes', warnings: 'hooks failed: boom' })}`)
  })

  it('leaves the notice empty when the backend was not refused', async () => {
    apiEnableApp.mockResolvedValueOnce({ ok: true })
    const { result } = setupActions()

    await act(async () => { await result.current.runEnable('notes') })

    expect(result.current.displayError).toBe('')
  })
})

import { uninstallBotChatStopFailedNotice } from '../../components/appstore/backendRefused'

const STOP_KEY = 'pages.appsPage.uninstall_bot_chat_stop_failed'

describe('uninstallBotChatStopFailedNotice', () => {
  it('names the app when the response carries bot_chat_stop_failed', () => {
    // The count picks the plural form; i18next does the choosing.
    expect(uninstallBotChatStopFailedNotice('Notes', { bot_chat_stop_failed: ['atlas', 'nova'] }))
      .toBe(`${STOP_KEY} ${JSON.stringify({ count: 2, name: 'Notes', slugs: 'atlas, nova' })}`)
  })

  it('passes a count of one for a single slug', () => {
    expect(uninstallBotChatStopFailedNotice('Notes', { bot_chat_stop_failed: ['atlas'] }))
      .toBe(`${STOP_KEY} ${JSON.stringify({ count: 1, name: 'Notes', slugs: 'atlas' })}`)
  })

  it('is empty when bot_chat_stop_failed is absent, empty, or non-array', () => {
    expect(uninstallBotChatStopFailedNotice('Notes', { ok: true })).toBe('')
    expect(uninstallBotChatStopFailedNotice('Notes', { bot_chat_stop_failed: [] })).toBe('')
    expect(uninstallBotChatStopFailedNotice('Notes', { bot_chat_stop_failed: 'atlas' })).toBe('')
    expect(uninstallBotChatStopFailedNotice('Notes', undefined)).toBe('')
    expect(uninstallBotChatStopFailedNotice('Notes', null)).toBe('')
  })
})

import { render, screen } from '@testing-library/react'
import ErrorNotice from '../../components/ErrorNotice'

describe('uninstall bot-chat-stop warning: accessible', () => {
  it('renders with role="status" (not role="alert") so the uninstall success is not read as a failure', () => {
    // impact="partial" on ErrorNotice renders role="status", not role="alert".
    // This is what the warning slot uses so the reader hears it as a notice, not an error.
    // Drop impact="partial" or add impact="blocking" and this assertion fails.
    render(
      <ErrorNotice
        message="atlas, nova are still running with Test App's tools. They will not get new tools from it."
        impact="partial"
        askAgent
        testId="stop-warn"
      />,
    )
    const notice = screen.getByTestId('stop-warn')
    expect(notice).toHaveAttribute('role', 'status')
    expect(notice).toHaveTextContent('still running')
  })

  it('the sr-only live region carries the same text so it is announced on mount', () => {
    // The sr-only <span role="status"> is always mounted so screen readers hear
    // the message when warnMsg is first set. The ErrorNotice copy is hidden from AT.
    // If the live region is absent, readers miss the announcement.
    const { container } = render(
      <span role="status" className="sr-only">
        Test App was uninstalled, but some bot chats could not be stopped.
      </span>,
    )
    const live = container.querySelector('[role="status"]')
    expect(live).not.toBeNull()
    expect(live?.textContent).toContain('could not be stopped')
  })
})

describe('disable with bot_chat_stop_failed', () => {
  /**
   * The disable notice names the app and the Bot the way the uninstall and trust
   * notices do; route it through appActionNotice's raw warnings instead and the
   * Bot shows by slug with no app named.
   */
  it('names the app and the Bot', () => {
    const slugs = botChatStopFailedSlugs({ ok: true, bot_chat_stop_failed: ['atlas'] })
    expect(botChatStopFailedNotice('disable', 'Test App', slugs, s => (s === 'atlas' ? 'Atlas' : s)))
      .toBe('pages.appsPage.disable_bot_chat_stop_failed {"count":1,"name":"Test App","slugs":"Atlas"}')
  })

  it('appActionNotice returns empty when no warnings and no bot_chat_stop_failed', () => {
    const result = { ok: true }
    const notice = appActionNotice('Test App', result, 'disable')
    expect(notice).toBe('')
  })
})
