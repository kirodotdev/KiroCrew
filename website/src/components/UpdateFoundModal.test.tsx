import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'

import { renderWithProviders, createTestStore } from '../test/helpers'
import { sseStatus, setUpdateProgress } from '../store/dashboardSlice'
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
  it('prints the folded display version but keys snooze/skip on the raw stamp', async () => {
    // A promoted stable candidate: the popup's text must show the clean
    // release, while the persisted per-version verdict keys on the raw stamp
    // (folding the key would make dismissing `0.4.0` swallow the next
    // release's rc candidate too).
    mockedApi.checkUpdate.mockResolvedValue({ changes: '' } as never)
    let resolvePatch: (v: unknown) => void = () => {}
    mockedApi.patchConfig.mockReturnValue(new Promise(r => { resolvePatch = r }) as never)
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '0.4.0rc14',
      update_latest_version_display: '0.4.0', update_command: 'x-update',
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByText('0.4.0')).toBeInTheDocument()
    expect(screen.queryByText('0.4.0rc14')).not.toBeInTheDocument()
    fireEvent.click(byName('components.updateFoundModal.skip_this_version'))
    await waitFor(() => expect(mockedApi.patchConfig).toHaveBeenCalledTimes(1))
    expect(mockedApi.patchConfig).toHaveBeenCalledWith('dashboard.update_nudge',
      expect.objectContaining({ version: '0.4.0rc14', skipped: true }))
    await act(async () => { resolvePatch({}) })
  })

  it('falls back to the raw version when an older gateway sends no display field', async () => {
    mockedApi.checkUpdate.mockResolvedValue({ changes: '' } as never)
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '0.4.0rc14', update_command: 'x-update',
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByText('0.4.0rc14')).toBeInTheDocument()
  })

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

  it('a background step failure after the accepted POST replaces "restarting" with the reason', async () => {
    // The endpoint answers `updating` before pull/build/pip run; a pip refusal
    // arrives later on the update_progress stream. Left unread, the modal
    // would say "restarting…" forever for a restart that never comes.
    mockedApi.applyUpdate.mockResolvedValue({} as never)
    const store = gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
    })
    await mount(undefined, store)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.update_now'))
    await waitFor(() => expect(
      screen.getByText(i18nT('components.updateFoundModal.updating_and_restarting')),
    ).toBeInTheDocument())
    // The worker announces its first live step before anything can fail.
    act(() => { store.dispatch(setUpdateProgress({ step: 'pulling', detail: 'Pulling latest changes…' })) })
    act(() => { store.dispatch(setUpdateProgress({ step: 'error', detail: 'zzq pip install -e exited 1' })) })
    await waitFor(() => expect(screen.getByText('zzq pip install -e exited 1')).toBeInTheDocument())
    expect(screen.queryByText(i18nT('components.updateFoundModal.updating_and_restarting'))).toBeNull()
    // The primary action is back so the user can retry once the cause is fixed.
    expect(byName('components.updateFoundModal.update_now')).not.toBeDisabled()
  })

  it('a failure whose frames beat the POST answer still ends the attempt', async () => {
    // The gateway answers `updating` before the worker runs, but a merge that
    // fails at once pushes `pulling` then `error` within milliseconds — both
    // can land before the HTTP answer. An attempt that only started listening
    // on that answer would read the failure as a stale predecessor and spin
    // forever; and the answer, arriving after the failure rendered, must not
    // raise the restarting latch back over it.
    let resolvePost: (v: unknown) => void = () => {}
    mockedApi.applyUpdate.mockReturnValue(new Promise(r => { resolvePost = r }) as never)
    const store = gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
    })
    await mount(undefined, store)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.update_now'))
    await waitFor(() => expect(mockedApi.applyUpdate).toHaveBeenCalledTimes(1))
    // Worker frames first, POST still pending.
    act(() => { store.dispatch(setUpdateProgress({ step: 'pulling', detail: '' })) })
    act(() => { store.dispatch(setUpdateProgress({ step: 'error', detail: 'zzq ff failed before the answer' })) })
    await waitFor(() => expect(screen.getByText('zzq ff failed before the answer')).toBeInTheDocument())
    // Then the late answer.
    await act(async () => { resolvePost({}) })
    for (let i = 0; i < 4; i++) await act(async () => { await new Promise(r => setTimeout(r, 10)) })
    expect(screen.getByText('zzq ff failed before the answer')).toBeInTheDocument()
    expect(screen.queryByText(i18nT('components.updateFoundModal.updating_and_restarting'))).toBeNull()
    expect(byName('components.updateFoundModal.update_now')).not.toBeDisabled()
  })

  it('a retry does not re-read the previous attempt\'s failure still in the store', async () => {
    // Nothing clears update_progress between attempts: the previous terminal
    // step sits in the store until the worker's first push replaces it. A
    // retry that honoured it would flip straight back to the old failure and
    // never show the update it just started.
    mockedApi.applyUpdate.mockResolvedValue({} as never)
    const store = gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
    })
    await mount(undefined, store)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.update_now'))
    await waitFor(() => expect(
      screen.getByText(i18nT('components.updateFoundModal.updating_and_restarting')),
    ).toBeInTheDocument())
    act(() => { store.dispatch(setUpdateProgress({ step: 'pulling', detail: '' })) })
    act(() => { store.dispatch(setUpdateProgress({ step: 'error', detail: 'zzq first attempt' })) })
    await waitFor(() => expect(screen.getByText('zzq first attempt')).toBeInTheDocument())

    fireEvent.click(byName('components.updateFoundModal.update_now'))
    await waitFor(() => expect(
      screen.getByText(i18nT('components.updateFoundModal.updating_and_restarting')),
    ).toBeInTheDocument())
    // Settle: the stale terminal step is still what the store holds.
    for (let i = 0; i < 4; i++) await act(async () => { await new Promise(r => setTimeout(r, 10)) })
    expect(screen.queryByText('zzq first attempt')).toBeNull()
    expect(screen.getByText(i18nT('components.updateFoundModal.updating_and_restarting'))).toBeInTheDocument()

    // The second attempt's own failure still lands.
    act(() => { store.dispatch(setUpdateProgress({ step: 'pulling', detail: '' })) })
    act(() => { store.dispatch(setUpdateProgress({ step: 'error', detail: 'zzq second attempt' })) })
    await waitFor(() => expect(screen.getByText('zzq second attempt')).toBeInTheDocument())
  })

  it('handing the failure to the agent closes the modal so the chat it opens is visible', async () => {
    // The hand-off soft-navigates to chat UNDER this full-screen modal; left
    // open, the click would look like it did nothing.
    mockedApi.applyUpdate.mockResolvedValue({} as never)
    const store = gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
    })
    await mount(undefined, store)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.update_now'))
    await waitFor(() => expect(
      screen.getByText(i18nT('components.updateFoundModal.updating_and_restarting')),
    ).toBeInTheDocument())
    act(() => { store.dispatch(setUpdateProgress({ step: 'pulling', detail: '' })) })
    act(() => { store.dispatch(setUpdateProgress({ step: 'error', detail: 'zzq pip refused' })) })
    await waitFor(() => expect(screen.getByText('zzq pip refused')).toBeInTheDocument())
    fireEvent.click(byName('components.askAgent.ask_the_agent'))
    await waitFor(() => expect(dialog()).toBeNull())
  })

  it('an unrelated progress event before any apply here leaves the modal alone', async () => {
    const store = gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
    })
    await mount(undefined, store)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    act(() => { store.dispatch(setUpdateProgress({ step: 'failed', detail: 'zzq other panel' })) })
    expect(screen.queryByText('zzq other panel')).toBeNull()
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

describe('UpdateFoundModal — mandatory update (update_required)', () => {
  const requiredStatus = {
    update_available: true as const,
    update_latest_version: '8.8.8',
    update_can_apply: true,
    update_required: true,
    update_min_version: '8.0.0',
  }

  it('opens past a persisted skip and drops every dismissal affordance', async () => {
    // A skip verdict for this very version must not hold: no snooze/skip can
    // apply to a mandatory update.
    withNudgeConfig({ version: '8.8.8', skipped: true })
    await mount(undefined, gatewayStore(requiredStatus))
    await waitFor(() => expect(dialog()).toBeInTheDocument())

    expect(screen.getByTestId('update-required-note')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: i18nT('components.updateFoundModal.dismiss') }))
      .not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: i18nT('components.updateFoundModal.remind_me_tomorrow') }))
      .not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: i18nT('components.updateFoundModal.skip_this_version') }))
      .not.toBeInTheDocument()
    // The primary action survives — a forced prompt with no way forward would
    // just be a lock screen.
    expect(byName('components.updateFoundModal.update_now')).toBeInTheDocument()
  })

  it('neither Escape nor a backdrop click closes it', async () => {
    await mount(undefined, gatewayStore(requiredStatus))
    await waitFor(() => expect(dialog()).toBeInTheDocument())

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(dialog()).toBeInTheDocument()
    fireEvent.click(screen.getByRole('presentation'))
    expect(dialog()).toBeInTheDocument()
    // Nothing was persisted either: a forced prompt must not write snooze
    // records the next (voluntary) prompt would then honour.
    expect(mockedApi.patchConfig).not.toHaveBeenCalled()
  })

  it('names the floor that made the update mandatory', async () => {
    await mount(undefined, gatewayStore(requiredStatus))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByTestId('update-required-note').textContent).toContain('8.0.0')
  })

  it('a failed mandatory apply offers no agent hand-off and stays up', async () => {
    // The hand-off closes the modal; a mandatory prompt that a failed apply
    // could wave away would enforce nothing. The failure still reads, and the
    // installer command remains the way out via a terminal.
    mockedApi.applyUpdate.mockResolvedValue({} as never)
    const store = gatewayStore({ ...requiredStatus, update_command: 'zzq installer --channel stable' })
    await mount(undefined, store)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.update_now'))
    await waitFor(() => expect(
      screen.getByText(i18nT('components.updateFoundModal.updating_and_restarting')),
    ).toBeInTheDocument())
    act(() => { store.dispatch(setUpdateProgress({ step: 'pulling', detail: '' })) })
    act(() => { store.dispatch(setUpdateProgress({ step: 'error', detail: 'zzq pip refused' })) })
    await waitFor(() => expect(screen.getByText('zzq pip refused')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: i18nT('components.askAgent.ask_the_agent') })).toBeNull()
    expect(screen.getByTestId('update-required-fallback-command').textContent)
      .toContain('zzq installer --channel stable')
    expect(dialog()).toBeInTheDocument()
    expect(byName('components.updateFoundModal.update_now')).not.toBeDisabled()
  })

  it('required without a candidate version never opens (nothing to offer)', async () => {
    const { container } = await mount(undefined, gatewayStore({
      update_available: false, update_latest_version: '', update_required: true,
      update_min_version: '8.0.0',
    }))
    expect(container.firstChild).toBeNull()
  })
})
