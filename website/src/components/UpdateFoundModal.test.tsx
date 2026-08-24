import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'

import { renderWithProviders, createTestStore } from '../test/helpers'
import { sseStatus } from '../store/dashboardSlice'
import { i18nT } from '../i18n/t'
import { api, ApiError } from '../api/client'
import { SNOOZE_SECS } from '../utils/updateNudge'
import UpdateFoundModal from './UpdateFoundModal'
import type { UpdateState } from '../hooks/useUpdateSubscription'
import type { StatusData } from '../types'

vi.mock('../api/client', () => {
  class MockApiError extends Error {}
  return {
    ApiError: MockApiError,
    api: {
      kirocrewConfig: vi.fn(),
      patchConfig: vi.fn(),
      checkUpdate: vi.fn(),
      applyUpdate: vi.fn(),
    },
  }
})

const mockedApi = vi.mocked(api)

const found: UpdateState = { state: 'found', version: '9.9.9', notes: 'zzq release notes' }

function withNudgeConfig(record: Record<string, unknown> | undefined) {
  mockedApi.kirocrewConfig.mockResolvedValue({ dashboard: { update_nudge: record } } as never)
}

async function mount(initial?: UpdateState, store = createTestStore()) {
  const rendered = renderWithProviders(<UpdateFoundModal />, { store })
  const push = async (next: UpdateState) => {
    await act(async () => { rendered.queryClient.setQueryData(['update-state'], next) })
  }
  if (initial) await push(initial)
  // Settle across several event-loop turns: the open path spans two chained
  // async hops (cache notification, then the config fetch the candidate
  // enables), each landing on its own tick. A single-tick flush samples
  // between them and lets a "stays closed" assertion pass vacuously.
  for (let i = 0; i < 8; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 10)) })
  }
  return { ...rendered, push }
}

const dialog = () => screen.queryByRole('dialog')
const byName = (key: string) => screen.getByRole('button', { name: i18nT(key) })

function gatewayStore(status: Partial<StatusData>) {
  const store = createTestStore()
  store.dispatch(sseStatus(status as StatusData))
  return store
}

const downloadBridge = vi.fn<() => Promise<unknown>>()

beforeEach(() => {
  mockedApi.kirocrewConfig.mockReset()
  mockedApi.patchConfig.mockReset()
  mockedApi.checkUpdate.mockReset()
  mockedApi.applyUpdate.mockReset()
  mockedApi.patchConfig.mockResolvedValue({} as never)
  mockedApi.checkUpdate.mockResolvedValue({ changes: '' } as never)
  withNudgeConfig({})
  // Desktop candidacy requires a preload that can actually download.
  downloadBridge.mockReset()
  downloadBridge.mockResolvedValue(undefined)
  ;(window as unknown as { updateAPI?: object }).updateAPI = { download: downloadBridge }
})

afterEach(() => {
  delete (window as unknown as { updateAPI?: unknown }).updateAPI
})

describe('UpdateFoundModal — desktop source', () => {
  it('renders nothing without an update state', async () => {
    const { container } = await mount()
    expect(container.firstChild).toBeNull()
    expect(mockedApi.kirocrewConfig).not.toHaveBeenCalled()
  })

  it('opens on a live found payload with version and notes', async () => {
    await mount(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByText('9.9.9')).toBeInTheDocument()
    expect(screen.getByText('zzq release notes')).toBeInTheDocument()
    expect(byName('components.updateFoundModal.download')).toBeInTheDocument()
  })

  it('never opens for a replayed payload', async () => {
    const { container } = await mount({ ...found, replayed: true })
    expect(container.firstChild).toBeNull()
    // The deterministic detector: a replayed payload is not a candidate, so
    // the nudge record must never even be consulted — a paint-timing race
    // cannot fake this the way an empty container can.
    expect(mockedApi.kirocrewConfig).not.toHaveBeenCalled()
  })

  it('never opens once the state moves past found/available', async () => {
    const { container } = await mount({ state: 'downloading', version: '9.9.9', percent: 10 })
    expect(container.firstChild).toBeNull()
    expect(mockedApi.kirocrewConfig).not.toHaveBeenCalled()
  })

  it('stays closed until the persisted record has loaded', async () => {
    let resolve!: (v: unknown) => void
    mockedApi.kirocrewConfig.mockReturnValue(new Promise(r => { resolve = r }) as never)
    await mount(found)
    expect(dialog()).not.toBeInTheDocument()
    await act(async () => { resolve({ dashboard: { update_nudge: {} } }) })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
  })

  it('a persisted skip for this version keeps it closed', async () => {
    withNudgeConfig({ version: '9.9.9', skipped: true })
    const { container } = await mount(found)
    expect(container.firstChild).toBeNull()
  })

  it('a skip for a PREVIOUS version does not suppress the next release', async () => {
    withNudgeConfig({ version: '9.9.8', skipped: true })
    await mount(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
  })

  it('an unexpired snooze keeps it closed', async () => {
    withNudgeConfig({ version: '9.9.9', snoozed_until: Date.now() / 1000 + 3600 })
    const { container } = await mount(found)
    expect(container.firstChild).toBeNull()
  })

  it('an expired snooze opens again', async () => {
    withNudgeConfig({ version: '9.9.9', snoozed_until: Date.now() / 1000 - 60 })
    await mount(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
  })

  it('never opens when the preload cannot download (older-bridge skew)', async () => {
    ;(window as unknown as { updateAPI?: object }).updateAPI = {}
    const { container } = await mount(found)
    expect(container.firstChild).toBeNull()
    expect(mockedApi.kirocrewConfig).not.toHaveBeenCalled()
  })

  it('never interrupts a user who enabled background auto-download', async () => {
    ;(window as unknown as { updateAPI?: object }).updateAPI = {
      download: downloadBridge,
      getInfo: vi.fn().mockResolvedValue({ autoDownload: true }),
    }
    const { container } = await mount(found)
    // Their consent prompt is the staged-build modal at `downloaded`; a popup
    // here would claim "nothing downloads until you choose" while the main
    // process is already downloading.
    expect(container.firstChild).toBeNull()
    expect(mockedApi.kirocrewConfig).not.toHaveBeenCalled()
  })

  it('a save failure for one version does not bypass persistence for the next', async () => {
    mockedApi.patchConfig.mockRejectedValueOnce(new Error('boom'))
    const { push } = await mount(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.skip_this_version'))
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    // Next release arrives; its verdict must go through the PATCH again —
    // a sticky degraded mode would silently lose this choice on reload.
    await push({ state: 'found', version: '9.9.10', notes: '' })
    await waitFor(() => expect(screen.getByText(/9\.9\.10/)).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.skip_this_version'))
    await waitFor(() => expect(mockedApi.patchConfig).toHaveBeenCalledTimes(2))
    expect(mockedApi.patchConfig).toHaveBeenLastCalledWith('dashboard.update_nudge',
      expect.objectContaining({ version: '9.9.10', skipped: true }))
  })

  it('dismissing one version does not consume the NEXT version\'s prompt', async () => {
    const { push } = await mount(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.remind_me_tomorrow'))
    await waitFor(() => expect(dialog()).not.toBeInTheDocument())
    await push({ state: 'found', version: '9.9.10', notes: '' })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByText(/9\.9\.10/)).toBeInTheDocument()
  })

  it('Download asks the Electron bridge for consent and closes', async () => {
    await mount(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.download'))
    expect(downloadBridge).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(dialog()).not.toBeInTheDocument())
    // Download consent is not a snooze — nothing is persisted.
    expect(mockedApi.patchConfig).not.toHaveBeenCalled()
  })

  it('Skip this version persists ONE atomic record and closes', async () => {
    const { queryClient } = await mount(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.skip_this_version'))
    await waitFor(() => expect(dialog()).not.toBeInTheDocument())
    // Atomicity is load-bearing: one PATCH of the whole record, so neither a
    // crash between writes nor two concurrent dashboards can assemble a
    // verdict nobody expressed (e.g. an old skip attached to a new version).
    await waitFor(() => expect(mockedApi.patchConfig).toHaveBeenCalledTimes(1))
    expect(mockedApi.patchConfig).toHaveBeenCalledWith('dashboard.update_nudge', {
      version: '9.9.9', snoozed_until: 0, skipped: true,
    })
    // The cache mirrors the write, so a later candidate in this session
    // reads the fresh record instead of the pre-skip one.
    await waitFor(() => expect(queryClient.getQueryData(['mc-config-update-nudge'])).toEqual({
      version: '9.9.9', snoozed_until: 0, skipped: true,
    }))
  })

  it('Remind me tomorrow persists a ~24h snooze in one record and closes', async () => {
    const before = Date.now() / 1000
    await mount(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.remind_me_tomorrow'))
    await waitFor(() => expect(dialog()).not.toBeInTheDocument())
    await waitFor(() => expect(mockedApi.patchConfig).toHaveBeenCalledTimes(1))
    const [path, rec] = mockedApi.patchConfig.mock.calls[0] as [string, { version: string; snoozed_until: number; skipped: boolean }]
    expect(path).toBe('dashboard.update_nudge')
    expect(rec.version).toBe('9.9.9')
    expect(rec.skipped).toBe(false)
    expect(rec.snoozed_until).toBeGreaterThanOrEqual(before + SNOOZE_SECS - 5)
    expect(rec.snoozed_until).toBeLessThanOrEqual(Date.now() / 1000 + SNOOZE_SECS + 5)
  })

  it('the header close button snoozes rather than plain-closing', async () => {
    await mount(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.dismiss'))
    await waitFor(() => expect(dialog()).not.toBeInTheDocument())
    await waitFor(() => expect(mockedApi.patchConfig).toHaveBeenCalledWith(
      'dashboard.update_nudge', expect.objectContaining({ version: '9.9.9', skipped: false }),
    ))
  })

  it('a failed persist keeps the modal open and says so', async () => {
    mockedApi.patchConfig.mockRejectedValue(new Error('boom'))
    await mount(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.skip_this_version'))
    // Closing optimistically here would silently discard the failed write:
    // the reload re-nags a user who believes they answered.
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(
      i18nT('components.updateFoundModal.could_not_save_choice'),
    ))
    expect(dialog()).toBeInTheDocument()
    // But a modal that can NEVER close over a persistently failing write
    // holds the whole dashboard hostage: once the user has seen the error,
    // the next dismissal closes session-only (informed, un-persisted).
    fireEvent.click(byName('components.updateFoundModal.remind_me_tomorrow'))
    await waitFor(() => expect(dialog()).not.toBeInTheDocument())
    expect(mockedApi.patchConfig).toHaveBeenCalledTimes(1)
  })

  it('Escape during a pending skip cannot overwrite it with a snooze', async () => {
    let resolvePatch!: (v: unknown) => void
    mockedApi.patchConfig.mockReturnValue(new Promise(r => { resolvePatch = r }) as never)
    await mount(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.skip_this_version'))
    // The skip PATCH is in flight; Escape must be a no-op, not a second
    // verdict — a snooze here would overwrite the user's stored skip.
    fireEvent.keyDown(window, { key: 'Escape' })
    await act(async () => { resolvePatch({}) })
    await waitFor(() => expect(dialog()).not.toBeInTheDocument())
    expect(mockedApi.patchConfig).toHaveBeenCalledTimes(1)
    expect(mockedApi.patchConfig).toHaveBeenCalledWith('dashboard.update_nudge',
      expect.objectContaining({ skipped: true }))
  })
})

describe('UpdateFoundModal — gateway source', () => {
  it('opens with Update now when the gateway can apply in-process', async () => {
    mockedApi.checkUpdate.mockResolvedValue({ changes: 'zzq gw notes' } as never)
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByText('8.8.8')).toBeInTheDocument()
    expect(byName('components.updateFoundModal.update_now')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('zzq gw notes')).toBeInTheDocument())
  })

  it('Update now applies via the gateway endpoint', async () => {
    mockedApi.applyUpdate.mockResolvedValue({} as never)
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.update_now'))
    await waitFor(() => expect(mockedApi.applyUpdate).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(
      screen.getByText(i18nT('components.updateFoundModal.updating_and_restarting')),
    ).toBeInTheDocument())
  })

  it('a network drop during apply reads as the restart, not a failure', async () => {
    mockedApi.applyUpdate.mockRejectedValue(new TypeError('fetch failed'))
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.update_now'))
    await waitFor(() => expect(
      screen.getByText(i18nT('components.updateFoundModal.updating_and_restarting')),
    ).toBeInTheDocument())
  })

  it('a real server rejection surfaces its message', async () => {
    mockedApi.applyUpdate.mockRejectedValue(new ApiError('zzq dirty tree'))
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.update_now'))
    await waitFor(() => expect(screen.getByText('zzq dirty tree')).toBeInTheDocument())
  })

  it('a wheel install gets the copyable command, never Update now', async () => {
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8',
      update_can_apply: false, update_command: 'curl -fsSL zzq.sh | sh',
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByTestId('update-found-command')).toHaveTextContent('curl -fsSL zzq.sh | sh')
    expect(screen.queryByRole('button', { name: i18nT('components.updateFoundModal.update_now') }))
      .not.toBeInTheDocument()
  })

  it('an install with no affordance is never interrupted', async () => {
    const { container } = await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8',
      update_can_apply: false, update_command: '',
    }))
    expect(container.firstChild).toBeNull()
  })

  it('a null verdict never opens it', async () => {
    const { container } = await mount(undefined, gatewayStore({
      update_available: null, update_latest_version: '8.8.8', update_can_apply: true,
    }))
    expect(container.firstChild).toBeNull()
  })
})
