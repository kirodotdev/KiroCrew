/**
 * Settings ▸ Chat — the feature-video cache readout and its one action.
 *
 * The row is a READOUT of backend state, so what these tests own is the mapping
 * from that state to what the user sees: which of the three lines is shown, and
 * whether the manual control exists at all. The counts themselves are the
 * backend's business.
 *
 * Two of these are mutation-verified below, in the tests that say so: the
 * download-disabled gate and the in-flight gate are the two places where the
 * wrong answer puts a button in front of the user that cannot do anything.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

import { setActiveSlot } from '../store/chatSlice'
import { i18nT } from '../i18n/t'

const BASE_DASH = {
  restore_sessions: false,
  restore_window_minutes: 30,
  merge_queued_messages: false,
  widget_density: 'more' as const,
  verbosity: 'default' as const,
  quick_send: false,
  session_grid: false,
  tail_fork_enabled: false,
  link_previews: false,
}

/** Every clip of the current release already on disk, downloads permitted. */
const BASE_STATUS = {
  enabled: true,
  download_enabled: true,
  release: '2026.09.1',
  cached: 2,
  total: 3,
  downloading: null as string | null,
}

const { featureVideoStatusMock, featureVideoFetchAllMock } = vi.hoisted(() => ({
  featureVideoStatusMock: vi.fn(),
  featureVideoFetchAllMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ ...BASE_DASH }),
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    kirocrewConfig: () => Promise.resolve({ agent: { completion_keep: 'head', completion_keep_chars: 3000, model: 'auto', reasoning_effort: '' } }),
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    patchConfig: () => Promise.resolve({}),
    updateDashboardConfig: () => Promise.resolve({}),
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
    featureVideoStatus: featureVideoStatusMock,
    featureVideoFetchAll: featureVideoFetchAllMock,
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

import { Provider } from 'react-redux'

// ChatPanel reads the active slot from redux to name the session on its
// feature-video calls, so these renders need a store. A FRESH one per file,
// not the app singleton: a shared store would carry `activeSlot` across suites.
import { createTestStore } from './helpers'

function wrap(ui: React.ReactElement, store = createTestStore()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<Provider store={store}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>)
}

const statusLine = () => screen.queryByTestId('feature-video-status')

/**
 * Wait until the status read has resolved AND its render has landed.
 *
 * Load-bearing for the two "no row" tests. Awaiting an unrelated row (Feature
 * Tips) proves only that a DIFFERENT query resolved, so an absence assertion
 * made after it can pass simply by sampling early -- which is exactly how a
 * mutation that made the row render went uncaught. The mock call is the
 * anchor for the right query; the flush covers the render it enables.
 */
async function statusSettled() {
  await waitFor(() => expect(featureVideoStatusMock).toHaveBeenCalled())
  for (let i = 0; i < 6; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 5)) })
  }
}
const downloadBtn = () =>
  screen.queryByRole('button', { name: i18nT('pages.settings.chatPanel.feature_videos_download_all') })

beforeEach(() => {
  featureVideoStatusMock.mockReset()
  featureVideoStatusMock.mockResolvedValue({ ...BASE_STATUS })
  featureVideoFetchAllMock.mockReset()
  featureVideoFetchAllMock.mockResolvedValue({ ok: true })
})

describe('ChatPanel — the feature-video cache readout', () => {
  it('names the release it is counting, and how much of it is on disk', async () => {
    // The release matters as much as the counts: 2 of 3 for LAST release is a stale
    // cache, and the same two numbers with this release is a cache mid-fill.
    wrap(<ChatPanel />)
    await waitFor(() => expect(statusLine()).toBeInTheDocument())
    expect(statusLine()).toHaveTextContent('2026.09.1')
    expect(statusLine()).toHaveTextContent('2')
    expect(statusLine()).toHaveTextContent('3')
  })

  it('names the clip being fetched while one is in flight', async () => {
    featureVideoStatusMock.mockResolvedValue({ ...BASE_STATUS, downloading: 'monitor-loops' })
    wrap(<ChatPanel />)
    await waitFor(() => expect(statusLine()).toHaveTextContent('monitor-loops'))
  })

  it('the live state outranks the counts', async () => {
    // Both are true at once while a fetch runs. The counts are a fact the user can
    // read a second later; "something is happening right now" is the one that
    // explains why the button is unavailable.
    featureVideoStatusMock.mockResolvedValue({ ...BASE_STATUS, downloading: 'feature-tips' })
    wrap(<ChatPanel />)
    await waitFor(() => expect(statusLine()).toBeInTheDocument())
    expect(statusLine()).toHaveTextContent(
      i18nT('pages.settings.chatPanel.feature_videos_downloading', { id: 'feature-tips' }),
    )
  })

  it('says so when policy forbids downloads', async () => {
    featureVideoStatusMock.mockResolvedValue({ ...BASE_STATUS, download_enabled: false })
    wrap(<ChatPanel />)
    await waitFor(() => expect(statusLine()).toHaveTextContent(
      i18nT('pages.settings.chatPanel.feature_videos_downloads_disabled'),
    ))
  })

  it('shows no row at all when the feature is off', async () => {
    // A cache count for clips that never play is noise, and an operator who turned
    // the feature off is not asking about its disk usage.
    featureVideoStatusMock.mockResolvedValue({ ...BASE_STATUS, enabled: false })
    wrap(<ChatPanel />)
    await statusSettled()
    expect(statusLine()).not.toBeInTheDocument()
    expect(downloadBtn()).not.toBeInTheDocument()
  })

  it('shows no row when the gateway reports no cache at all', async () => {
    // The route already exists on an older gateway, where it answers 200 with a
    // different payload -- `{enabled, state: {...}}` and none of the cache
    // fields. Reading that absent `download_enabled` as `false` would put
    // "downloads are turned off" on screen for an install that has no such
    // policy, which is the one thing worse than an empty row.
    featureVideoStatusMock.mockResolvedValue({
      enabled: true, state: { 'startup-videos-1': { status: 'seen', ts: 1 } },
    })
    wrap(<ChatPanel />)
    await statusSettled()
    expect(statusLine()).not.toBeInTheDocument()
    expect(downloadBtn()).not.toBeInTheDocument()
    expect(screen.queryByText(
      i18nT('pages.settings.chatPanel.feature_videos_downloads_disabled'),
    )).not.toBeInTheDocument()
  })

  it('says which half broke when the read fails', async () => {
    // A row that simply vanishes is indistinguishable from the feature being off.
    featureVideoStatusMock.mockRejectedValue(new Error('HTTP 500'))
    wrap(<ChatPanel />)
    await waitFor(() => expect(screen.getByText(
      i18nT('pages.settings.chatPanel.failed_to_load_feature_video_status'),
    )).toBeInTheDocument())
  })
})

describe('ChatPanel — the feature-video calls name the session', () => {
  it('sends the active slot key on the status read AND the fetch-all write', async () => {
    // MUTATION-VERIFIED both ways: drop the key from either call and this fails.
    //
    // `_blocks_reads_session` returns "not restricted" for a MISSING key and for
    // the shared `dashboard:ui` placeholder alike, so a request without one is
    // served the permanent engagement history even from a temporary session --
    // the one kind of session whose contract is that reads are withheld. The key
    // is what makes the server's own gate reachable.
    const store = createTestStore()
    store.dispatch(setActiveSlot('slot-4'))
    wrap(<ChatPanel />, store)

    await waitFor(() => expect(featureVideoStatusMock).toHaveBeenCalled())
    expect(featureVideoStatusMock).toHaveBeenCalledWith('dashboard:slot-4')

    await waitFor(() => expect(downloadBtn()).toBeInTheDocument())
    fireEvent.click(downloadBtn() as HTMLElement)
    await waitFor(() => expect(featureVideoFetchAllMock).toHaveBeenCalled())
    expect(featureVideoFetchAllMock).toHaveBeenCalledWith('dashboard:slot-4')
  })

  it('sends no key when no slot is active, rather than a fake one', async () => {
    // `dashboard:` with nothing after it would name a slot that does not exist,
    // so the key is omitted and the server falls back to its own default.
    wrap(<ChatPanel />)
    await waitFor(() => expect(featureVideoStatusMock).toHaveBeenCalled())
    expect(featureVideoStatusMock).toHaveBeenCalledWith(undefined)
  })
})

describe('ChatPanel — downloading every clip now', () => {
  it('offers the control once downloads are permitted, and starts the fetch', async () => {
    wrap(<ChatPanel />)
    await waitFor(() => expect(downloadBtn()).toBeInTheDocument())
    fireEvent.click(downloadBtn() as HTMLElement)
    await waitFor(() => expect(featureVideoFetchAllMock).toHaveBeenCalledTimes(1))
  })

  it('hides the control entirely when policy forbids downloads', async () => {
    // MUTATION-VERIFIED: rendering the button `disabled` instead of absent, or
    // dropping the `download_enabled` condition, both fail here. Hidden and not
    // greyed, because a control whose only outcome is a refusal explains a policy
    // the user cannot act on.
    featureVideoStatusMock.mockResolvedValue({ ...BASE_STATUS, download_enabled: false })
    wrap(<ChatPanel />)
    await waitFor(() => expect(statusLine()).toBeInTheDocument())
    expect(downloadBtn()).not.toBeInTheDocument()
    // Not a greyed one either, in any spelling.
    const disabled = screen.queryAllByRole('button').filter(b => b.hasAttribute('disabled'))
    expect(disabled).toHaveLength(0)
    expect(featureVideoFetchAllMock).not.toHaveBeenCalled()
  })

  it('cannot queue the same work twice while a fetch is already running', async () => {
    // MUTATION-VERIFIED: dropping the `!!fv.downloading` clause lets this click
    // through and starts a second pass over the same clips.
    featureVideoStatusMock.mockResolvedValue({ ...BASE_STATUS, downloading: 'feature-tips' })
    wrap(<ChatPanel />)
    await waitFor(() => expect(downloadBtn()).toBeInTheDocument())
    expect(downloadBtn()).toBeDisabled()
    fireEvent.click(downloadBtn() as HTMLElement)
    expect(featureVideoFetchAllMock).not.toHaveBeenCalled()
  })

  it('says so when the fetch could not be started', async () => {
    featureVideoFetchAllMock.mockRejectedValue(new Error('HTTP 503'))
    wrap(<ChatPanel />)
    await waitFor(() => expect(downloadBtn()).toBeInTheDocument())
    fireEvent.click(downloadBtn() as HTMLElement)
    await waitFor(() => expect(screen.getByText(
      i18nT('pages.settings.chatPanel.failed_to_start_feature_video_download'),
    )).toBeInTheDocument())
  })
})
