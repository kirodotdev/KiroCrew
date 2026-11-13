import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { useState, type ReactElement } from 'react'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'

import { renderWithProviders, createTestStore } from '../test/helpers'
import { sseStatus, setUpdateProgress } from '../store/dashboardSlice'
import { i18nT } from '../i18n/t'
import { api, ApiError } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'
import { SNOOZE_SECS } from '../utils/updateNudge'
import UpdateFoundModal from './UpdateFoundModal'
import { updateInfoQuery } from '../api/updateInfoQuery'
import type { UpdateState } from '../hooks/useUpdateSubscription'
import type { StatusData } from '../types'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))

vi.mock('../api/client', () => {
  class MockApiError extends Error {}
  return {
    ApiError: MockApiError,
    api: {
      kirocrewConfig: vi.fn(),
      patchConfig: vi.fn(),
      checkUpdate: vi.fn(),
      applyUpdate: vi.fn(),
      disarmUpdate: vi.fn(),
      restartGateway: vi.fn(),
      armUpdate: vi.fn(),
      armStatus: vi.fn(),
    },
  }
})

const mockedApi = vi.mocked(api)
const mockedCopyToClipboard = vi.mocked(copyToClipboard)

const found: UpdateState = { state: 'found', version: '9.9.9', notes: 'zzq release notes' }

function withNudgeConfig(record: Record<string, unknown> | undefined) {
  mockedApi.kirocrewConfig.mockResolvedValue({ dashboard: { update_nudge: record } } as never)
}

async function mount(initial?: UpdateState, store = createTestStore(), ui: ReactElement = <UpdateFoundModal />) {
  const rendered = renderWithProviders(ui, { store })
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
  mockedApi.disarmUpdate.mockReset()
  mockedApi.restartGateway.mockReset()
  mockedApi.armUpdate.mockReset()
  mockedApi.armStatus.mockReset()
  mockedApi.patchConfig.mockResolvedValue({} as never)
  mockedApi.checkUpdate.mockResolvedValue({ changes: '' } as never)
  mockedApi.armUpdate.mockResolvedValue({
    ok: true, armed: true, expires_in: 600, approve_command: 'kirocrew update approve',
  } as never)
  mockedApi.armStatus.mockResolvedValue({
    armed: true, expires_in: 590, approve_command: 'kirocrew update approve',
  } as never)
  mockedCopyToClipboard.mockReset()
  mockedCopyToClipboard.mockResolvedValue(true)
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

  it('never opens when the edition update widget owns the prompts', async () => {
    // The edition's own top-bar widget offers the found build itself; a
    // second offer here would contradict it. Not a candidate at all, so the
    // nudge record is never consulted, the same detector as the replay case.
    const { container } = await mount({ ...found, updatePrompts: 'edition' })
    expect(container.firstChild).toBeNull()
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

  it('reads the preference again once a build is found, so a change made in another window counts', async () => {
    // Read as "auto-download on" earlier; turned off in another window since.
    ;(window as unknown as { updateAPI?: object }).updateAPI = {
      download: downloadBridge,
      getInfo: vi.fn().mockResolvedValue({ autoDownload: false }),
    }
    const { queryClient, push } = await mount()
    queryClient.setQueryData(updateInfoQuery.queryKey, { autoDownload: true })
    await push(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
  })

  it('a later find in the same window waits for its own read of the preference', async () => {
    // The popup stays mounted across polls. Found, dismissed, then the
    // preference is turned on in another window before the next poll finds a
    // build: the popup must not open on the earlier read while this one runs.
    const getInfo = vi.fn().mockResolvedValue({ autoDownload: false })
    ;(window as unknown as { updateAPI?: object }).updateAPI = { download: downloadBridge, getInfo }
    const { push } = await mount(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.remind_me_tomorrow'))
    await waitFor(() => expect(dialog()).not.toBeInTheDocument())

    let answer!: (info: { autoDownload: boolean }) => void
    getInfo.mockReturnValue(new Promise(resolve => { answer = resolve }))
    await push({ state: 'checking' })
    await push({ state: 'found', version: '9.9.10', notes: '' })
    for (let i = 0; i < 4; i++) {
      await act(async () => { await new Promise(r => setTimeout(r, 10)) })
    }
    expect(getInfo).toHaveBeenCalledTimes(2)
    expect(dialog()).toBeNull()

    await act(async () => { answer({ autoDownload: true }) })
    for (let i = 0; i < 4; i++) {
      await act(async () => { await new Promise(r => setTimeout(r, 10)) })
    }
    expect(dialog()).toBeNull()
  })

  it('a build found again opens once its own read says nothing downloads by itself', async () => {
    const getInfo = vi.fn().mockResolvedValue({ autoDownload: true })
    ;(window as unknown as { updateAPI?: object }).updateAPI = { download: downloadBridge, getInfo }
    const { push } = await mount(found)
    expect(dialog()).toBeNull()

    getInfo.mockResolvedValue({ autoDownload: false })
    await push({ state: 'found', version: '9.9.10', notes: '' })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(getInfo).toHaveBeenCalledTimes(2)
  })

  it('still asks, and says why, when the preference cannot be read', async () => {
    ;(window as unknown as { updateAPI?: object }).updateAPI = {
      download: downloadBridge,
      getInfo: vi.fn().mockRejectedValue(new Error('zzq')),
    }
    await mount(found)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByTestId('update-found-preference-error')).toHaveTextContent(
      i18nT('pages.settings.aboutPanel.auto_download_unreadable'))
    expect(screen.queryByText(i18nT('components.updateFoundModal.nothing_downloads_until_you_choose_to'))).toBeNull()
  })

  it('still asks when a later re-read fails, rather than trusting the older answer', async () => {
    const getInfo = vi.fn().mockResolvedValue({ autoDownload: true })
    ;(window as unknown as { updateAPI?: object }).updateAPI = { download: downloadBridge, getInfo }
    const { push } = await mount(found)
    expect(dialog()).toBeNull()

    getInfo.mockRejectedValue(new Error('zzq'))
    await push({ state: 'found', version: '9.9.10', notes: '' })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByTestId('update-found-preference-error')).toHaveTextContent(
      i18nT('pages.settings.aboutPanel.auto_download_unreadable'))
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
    // The route merges in a skip another dashboard saved; this is what it stores.
    const stored = { version: '9.9.9', snoozed_until: 0, skipped: true, skipped_versions: ['9.9.8', '9.9.9'] }
    mockedApi.patchConfig.mockImplementation(async () => {
      // A config read after the write fails: the cache must not need one.
      mockedApi.kirocrewConfig.mockRejectedValue(new Error('offline'))
      return { dashboard: { update_nudge: stored } } as never
    })
    const { queryClient, push } = await mount(found)
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
    // The cache holds the STORED record the PATCH answered with, so a later
    // candidate in this session reads the merged skips and still opens.
    await waitFor(() => expect(queryClient.getQueryData(['mc-config-update-nudge'])).toEqual(stored))
    await push({ state: 'found', version: '9.9.10', notes: '' })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
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
    await waitFor(() => expect(screen.getByTestId('update-found-persist-error')).toHaveTextContent(
      i18nT('components.updateFoundModal.could_not_save_choice'),
    ))
    expect(screen.getByRole('button', {
      name: i18nT('components.askAgent.ask_the_agent'),
    })).toBeInTheDocument()
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

  it('an Escape pressed the moment it appears still snoozes it', async () => {
    // An open from a default-priority update (here the hold lifting outside an
    // event) whose render runs past the scheduler's 5 ms slice lets React yield
    // before it runs passive effects, so a listener attached there would miss a
    // press in that gap. A render that outlasts the slice forces the yield.
    function SlowBody() {
      const end = performance.now() + 20
      while (performance.now() < end) { /* outlast the scheduler slice */ }
      return null
    }
    let release: () => void = () => {}
    function Host() {
      const [held, setHeld] = useState(true)
      release = () => setHeld(false)
      return <>{!held && <SlowBody />}<UpdateFoundModal held={held} /></>
    }
    const { queryClient } = await mount(found, undefined, <Host />)
    await waitFor(() => expect(queryClient.getQueryState(['mc-config-update-nudge'])?.status).toBe('success'))
    expect(dialog()).not.toBeInTheDocument()

    const g = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
    const prior = g.IS_REACT_ACT_ENVIRONMENT
    let pressedEscape!: () => void
    const pressed = new Promise<void>(resolve => { pressedEscape = resolve })
    const watch = new MutationObserver(() => {
      const shown = document.querySelector('[role="dialog"]')
      if (!shown) return
      watch.disconnect()
      fireEvent.keyDown(shown, { key: 'Escape' })
      pressedEscape()
    })
    // Undone even when the test times out, so a failure cannot leak into the next test.
    const restore = () => { watch.disconnect(); g.IS_REACT_ACT_ENVIRONMENT = prior }
    onTestFinished(restore)
    // Let React schedule the open as it does in the app, not flush it in act().
    g.IS_REACT_ACT_ENVIRONMENT = false
    watch.observe(document.body, { childList: true, subtree: true })
    release()
    await pressed
    restore()
    await waitFor(() => expect(mockedApi.patchConfig).toHaveBeenCalledWith(
      'dashboard.update_nudge', expect.objectContaining({ version: '9.9.9', skipped: false }),
    ))
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

  it('names no version when a behind-only checkout reports the running one', async () => {
    // A git checkout behind its upstream at the same version string: the
    // candidate is the build already running, so "0.13.0 is available" lies.
    await mount(undefined, gatewayStore({
      version: '0.13.0', update_available: true, update_latest_version: '0.13.0',
      update_latest_version_display: '0.13.0', update_can_apply: true, update_commits_behind: 3,
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByText(i18nT('pages.settings.aboutPanel.a_newer_version_is_available'))).toBeInTheDocument()
    expect(screen.queryByText('0.13.0')).not.toBeInTheDocument()
  })

  it('opens with Update when I restart when the gateway can apply in-process', async () => {
    mockedApi.checkUpdate.mockResolvedValue({ changes: 'zzq gw notes' } as never)
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByText('8.8.8')).toBeInTheDocument()
    expect(byName('components.updateFoundModal.update_when_i_restart')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('zzq gw notes')).toBeInTheDocument())
  })

  it('Update when I restart ARMS the update and says nothing restarts now', async () => {
    mockedApi.applyUpdate.mockResolvedValue({ armed: true, version: '8.8.8' } as never)
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.update_when_i_restart'))
    await waitFor(() => expect(mockedApi.applyUpdate).toHaveBeenCalledTimes(1))
    expect(mockedApi.restartGateway).not.toHaveBeenCalled()
    expect(screen.queryByText(i18nT('components.updateFoundModal.updating_and_restarting'))).toBeNull()
  })

  it('an arm response that lands after the status frame reports it armed leaves the modal usable', async () => {
    let resolveArm: (v: unknown) => void = () => {}
    mockedApi.applyUpdate.mockReturnValue(new Promise(r => { resolveArm = r }) as never)
    const status = { update_available: true, update_latest_version: '8.8.8', update_can_apply: true }
    const store = gatewayStore(status)
    await mount(undefined, store)
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.update_when_i_restart'))
    await waitFor(() => expect(mockedApi.applyUpdate).toHaveBeenCalledTimes(1))
    act(() => { store.dispatch(sseStatus({ ...status, update_armed_version: '8.8.8' } as StatusData)) })
    await act(async () => { resolveArm({ armed: true, version: '8.8.8' }) })
    expect(mockedApi.restartGateway).not.toHaveBeenCalled()
    expect(screen.queryByText(i18nT('components.updateFoundModal.updating_and_restarting'))).toBeNull()
    expect(byName('components.updateFoundModal.restart_to_update')).not.toBeDisabled()
  })

  it('an armed update offers the restart and the way back, not another install', async () => {
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
      update_armed_version: '8.8.8',
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(byName('components.updateFoundModal.restart_to_update')).toBeInTheDocument()
    expect(screen.getByText(i18nT('components.updateFoundModal.update_armed_note'))).toBeInTheDocument()
    mockedApi.disarmUpdate.mockResolvedValue({ armed: false } as never)
    fireEvent.click(screen.getByTestId('update-cancel-armed'))
    await waitFor(() => expect(mockedApi.disarmUpdate).toHaveBeenCalledTimes(1))
  })

  it('says so when taking the armed update back fails', async () => {
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
      update_armed_version: '8.8.8',
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    mockedApi.disarmUpdate.mockRejectedValue(new Error('nope'))
    fireEvent.click(screen.getByTestId('update-cancel-armed'))
    await waitFor(() => expect(screen.getByTestId('update-cancel-armed-error').textContent)
      .toContain(i18nT('components.updateFoundModal.cancel_update_failed')))
  })

  it('shows why the last boot-time apply failed', async () => {
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
      pending_apply_error: 'zzq installer said no',
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByTestId('update-apply-error-note').textContent).toContain('zzq installer said no')
  })

  it('a command provider offers "Don\'t remind me" in place of Skip this version', async () => {
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
      update_managed_by: 'command',
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByRole('button', {
      name: i18nT('components.updateFoundModal.dont_remind_me_about_version', { version: '8.8.8' }),
    })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: i18nT('components.updateFoundModal.skip_this_version') })).toBeNull()
  })

  it('a declined restart confirmation is a quiet cancel, with no error shown', async () => {
    const { WorkConfirmDeclined } = await import('../utils/workInFlight')
    mockedApi.restartGateway.mockRejectedValue(new WorkConfirmDeclined('Restart cancelled.'))
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
      update_restart_ready: true,
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.restart_to_update'))
    await waitFor(() => expect(mockedApi.restartGateway).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('Restart cancelled.')).toBeNull()
    expect(byName('components.updateFoundModal.restart_to_update')).not.toBeDisabled()
  })

  it('an update already installed beside the running one offers the restart, not another install', async () => {
    mockedApi.restartGateway.mockResolvedValue({} as never)
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
      update_restart_ready: true, update_restart_ready_version: '8.8.8', update_restart_ready_reason: 'staged',
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByTestId('update-restart-ready-note')).toHaveTextContent(
      i18nT('components.updateFoundModal.update_installed_restart_note'),
    )
    expect(screen.queryByRole('button', { name: i18nT('components.updateFoundModal.update_when_i_restart') })).toBeNull()

    fireEvent.click(byName('components.updateFoundModal.restart_to_update'))

    await waitFor(() => expect(mockedApi.restartGateway).toHaveBeenCalledTimes(1))
    expect(mockedApi.applyUpdate).not.toHaveBeenCalled()
    await waitFor(() => expect(
      screen.getByText(i18nT('components.updateFoundModal.updating_and_restarting')),
    ).toBeInTheDocument())
  })

  it('a staged update on a gateway that cannot apply still offers the restart, not the copy-command path', async () => {
    mockedApi.restartGateway.mockResolvedValue({} as never)
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: false,
      update_can_arm: true, update_restart_ready: true,
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())

    expect(byName('components.updateFoundModal.restart_to_update')).toBeInTheDocument()
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
    fireEvent.click(byName('components.updateFoundModal.update_when_i_restart'))
    await waitFor(() => expect(screen.getByText('zzq dirty tree')).toBeInTheDocument())
  })

  it('a transport failure while arming ends the attempt and leaves Dismiss usable', async () => {
    mockedApi.applyUpdate.mockRejectedValue(new TypeError('fetch failed'))
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8', update_can_apply: true,
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    const arm = byName('components.updateFoundModal.update_when_i_restart')
    fireEvent.click(arm)
    await waitFor(() => expect(mockedApi.applyUpdate).toHaveBeenCalledTimes(1))
    expect(await screen.findByTestId('update-found-action-error'))
      .toHaveTextContent(i18nT('components.updateFoundModal.update_failed'))
    expect(byName('components.updateFoundModal.update_when_i_restart')).toBeEnabled()
    expect(byName('components.updateFoundModal.dismiss')).toBeEnabled()
  })

  it('an armable managed install prepares the host approval command, never the installer', async () => {
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8',
      update_can_apply: false, update_can_arm: true,
      update_command: 'curl -fsSL zzq.sh | sh',
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    const action = screen.getByTestId('in-app-update-action')
    expect(action).toHaveTextContent(/update to v8\.8\.8/i)
    expect(screen.queryByText('curl -fsSL zzq.sh | sh')).not.toBeInTheDocument()

    fireEvent.click(action)

    await waitFor(() => expect(mockedApi.armUpdate).toHaveBeenCalledTimes(1))
    expect(await screen.findByTestId('approve-command'))
      .toHaveTextContent('kirocrew update approve')
    expect(screen.getByTestId('in-app-update-action')).toBe(action)
    expect(action).toHaveTextContent(/copy command/i)
    expect(screen.getByTestId('in-app-update-armed')).toHaveTextContent(/gateway host/i)
    expect(screen.getByTestId('arm-countdown')).toBeInTheDocument()
    expect(screen.queryByText('curl -fsSL zzq.sh | sh')).not.toBeInTheDocument()
  })

  it('an arm failure renders the shared error notice with agent hand-off', async () => {
    mockedApi.armUpdate.mockRejectedValue(new ApiError('zzq arm refused'))
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8',
      update_can_apply: false, update_can_arm: true,
      update_command: 'curl -fsSL zzq.sh | sh',
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('in-app-update-action'))

    const notice = await screen.findByTestId('arm-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('zzq arm refused')
    expect(within(notice).getByRole('button', {
      name: i18nT('components.askAgent.ask_the_agent'),
    })).toBeInTheDocument()
  })

  it('a failed clipboard write never reports Copied and offers agent hand-off', async () => {
    mockedCopyToClipboard.mockResolvedValue(false)
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8',
      update_can_apply: false, update_can_arm: true,
      update_command: 'curl -fsSL zzq.sh | sh',
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    const action = screen.getByTestId('in-app-update-action')
    fireEvent.click(action)
    await screen.findByTestId('approve-command')

    fireEvent.click(action)

    await waitFor(() => expect(mockedCopyToClipboard).toHaveBeenCalledWith(
      'kirocrew update approve',
    ))
    expect(action).not.toHaveTextContent(i18nT('pages.settings.aboutPanel.copied'))
    const notice = await screen.findByTestId('arm-copy-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(within(notice).getByRole('button', {
      name: i18nT('components.askAgent.ask_the_agent'),
    })).toBeInTheDocument()
  })

  it('a non-armable install retains the copyable installer fallback', async () => {
    await mount(undefined, gatewayStore({
      update_available: true, update_latest_version: '8.8.8',
      update_can_apply: false, update_can_arm: false,
      update_command: 'curl -fsSL zzq.sh | sh',
    }))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByTestId('update-found-command')).toHaveTextContent('curl -fsSL zzq.sh | sh')
    expect(mockedApi.armUpdate).not.toHaveBeenCalled()
  })

  it('the desktop candidate is skipped when a policy provider governs a bundled install', async () => {
    // When the provider can apply (non-Windows), the Electron auto-updater
    // stands down; no desktop candidate is offered.
    const store = gatewayStore({
      update_bundled_by_app: true,
      update_managed_by: 'command',
      update_can_apply: true,
    } as Partial<StatusData>)
    withNudgeConfig({})
    const { container } = await mount(found, store)
    // With the desktop candidate suppressed, the modal must not open.
    expect(container.firstChild).toBeNull()
    expect(mockedApi.kirocrewConfig).not.toHaveBeenCalled()
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
    expect(byName('components.updateFoundModal.update_when_i_restart')).toBeInTheDocument()
  })

  it('a required apply failure keeps the enforcement overlay visible', async () => {
    mockedApi.applyUpdate.mockRejectedValue(new ApiError('zzq apply refused'))
    await mount(undefined, gatewayStore(requiredStatus))
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(byName('components.updateFoundModal.update_when_i_restart'))

    const notice = await screen.findByTestId('update-found-action-error')
    expect(within(notice).queryByRole('button', {
      name: i18nT('components.askAgent.ask_the_agent'),
    })).not.toBeInTheDocument()
    expect(dialog()).toBeInTheDocument()
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

  it('required without a candidate version never opens (nothing to offer)', async () => {
    const { container } = await mount(undefined, gatewayStore({
      update_available: false, update_latest_version: '', update_required: true,
      update_min_version: '8.0.0',
    }))
    expect(container.firstChild).toBeNull()
  })
})
