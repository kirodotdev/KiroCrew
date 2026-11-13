/**
 * The model picker must disclose what a model costs BEFORE the click, and it must
 * offer a way to pay that cost at a moment the user chose.
 *
 * Weights are fetched once per model, and the smallest is 78 MB while the largest
 * is 1.6 GB. Desktop releases bundle the recogniser and every runtime dependency,
 * so model download must be the user's only setup action. Three things pin that
 * contract: the per-option size, copy naming the one-click path, and the explicit
 * download control with the size on it.
 */
import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act, within } from '@testing-library/react'
import { defaultScheduler, notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { initI18n } from '../i18n'
import SttSettings from '../pages/settings/SttSettings'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    sttConfig: vi.fn(),
    saveSttConfig: vi.fn(),
    sttStatus: vi.fn(),
    sttPrepare: vi.fn(),
  },
}))

const mockApi = api as unknown as {
  sttConfig: ReturnType<typeof vi.fn>
  sttStatus: ReturnType<typeof vi.fn>
  sttPrepare: ReturnType<typeof vi.fn>
}

/** 148 MB and 1.6 GB, the catalog's real `base` and `large-v3-turbo` sizes. */
const BASE_BYTES = 147_951_465
const TURBO_BYTES = 1_624_555_275

/** The status body the gateway serves, with `status` laid over it. A deep copy, so
 *  each read is a fresh object the way a parsed response is. */
function statusBody(status: Record<string, unknown> = {}) {
  return structuredClone({
    available: true,
    code: '',
    detail: '',
    models: [
      { name: 'base', size_bytes: BASE_BYTES, present: false },
      { name: 'large-v3-turbo', size_bytes: TURBO_BYTES, present: false },
    ],
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
    ...status,
  })
}

function mount(status: Record<string, unknown> = {}) {
  mockApi.sttConfig.mockResolvedValue({
    enabled: true,
    provider: 'local',
    model: 'base',
    streaming: false,
    providers: ['local', 'transcribe'],
    streaming_providers: ['local', 'transcribe'],
    language_codes: ['en-US'],
    prereqs: [],
  })
  mockApi.sttStatus.mockImplementation(async () => statusBody(status))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <SttSettings />
      </QueryClientProvider>
    </Provider>,
  )
  return qc
}

/** A transfer that ended in `failed` for the selected model. */
function failedDownload(error: string) {
  return { step: 'failed', model: 'base', downloaded_bytes: 0, total_bytes: BASE_BYTES, error }
}

const modelSelect = () => screen.getByRole('combobox', { name: /model/i })

describe('SttSettings model download disclosure', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    await initI18n('en')
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: async () => [] },
    })
  })
  afterEach(() => cleanup())

  it('says model download is the only desktop setup action', async () => {
    mount()
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    // Read off the tip's `title`, not the row: this sentence explains what the
    // Model row IS rather than deciding which model to pick, so it moved behind a
    // "?" when the panel stopped spending permanent space on prose. `InfoTip`
    // always carries its text as `title`, so the copy is still asserted verbatim.
    const desc = screen.getByTitle(/models download on demand/i)
    expect(desc.title).toMatch(/click Download now/i)
    expect(desc.title).toMatch(/every other runtime dependency/i)
  })

  it('states each model size in its own option, from the served catalog', async () => {
    // The size is what makes the choice informed, and it has to be visible in the
    // list rather than after the commit. Sizes are formatted for the active locale
    // (SI, so 148 MB rather than a 1024-based mislabel).
    mount()
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    fireEvent.click(modelSelect())
    await waitFor(() => expect(screen.getByRole('option', { name: /^base/ })).toBeTruthy())
    expect(screen.getByRole('option', { name: /base \(148MB\)/ })).toBeTruthy()
    expect(screen.getByRole('option', { name: /large-v3-turbo \(1\.6GB\)/ })).toBeTruthy()
  })

  it('offers a download control naming the one-time cost, and calls prepare', async () => {
    mount()
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    // The cost, before the press.
    expect(screen.getByText(/one-time 148MB download/i)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /download now/i }))
    // The SELECTED model, explicitly: sending no id would race the config write.
    await waitFor(() => expect(mockApi.sttPrepare).toHaveBeenCalledWith('base'))
  })

  it('shows byte progress instead of the offer while the transfer runs', async () => {
    mount({
      download: {
        step: 'downloading',
        model: 'base',
        downloaded_bytes: 74_000_000,
        total_bytes: BASE_BYTES,
        error: '',
      },
    })
    await waitFor(() => expect(screen.getByText(/downloading the speech model/i)).toBeTruthy())
    // Percent AND absolute bytes: percent alone hides how much is left.
    expect(screen.getByText(/50%/)).toBeTruthy()
    expect(screen.getByText(/74MB of 148MB/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /download now/i })).toBeNull()
  })

  it('puts the failed download hand-off on a line of its own on a phone', async () => {
    // Beside the message, the hand-off squeezes the sentence into ~60% of a phone's
    // width. jsdom has no layout, so the wrap is read off the classes.
    mount({
      download: { step: 'failed', model: 'base', downloaded_bytes: 0, total_bytes: BASE_BYTES, error: 'connection reset' },
    })
    const message = await screen.findByText('The download failed: connection reset')
    const row = message.closest('[role="alert"]') as HTMLElement
    expect(row.classList).toContain('flex-wrap')
    expect(row.classList).toContain('sm:flex-nowrap')
    expect(message.classList).toContain('basis-[calc(100%-2rem)]')
    expect(message.classList).toContain('sm:basis-auto')
    expect(within(row).getByRole('button', { name: /ask the agent/i })).toBeTruthy()
  })

  it('does not attribute another model transfer to the selected one', async () => {
    // The gateway runs one transfer at a time, so a switch mid-download leaves the
    // store reporting the PREVIOUS model. Claiming that progress here would show a
    // download the selected model never started.
    mount({
      download: {
        step: 'downloading',
        model: 'large-v3-turbo',
        downloaded_bytes: 800_000_000,
        total_bytes: TURBO_BYTES,
        error: '',
      },
    })
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    expect(screen.queryByText(/downloading the speech model/i)).toBeNull()
    expect(screen.getByRole('button', { name: /download now/i })).toBeTruthy()
  })

  it('lets the user dismiss a failed download until another failure or attempt', async () => {
    // Notifications run on microtasks here, so the `act` around each refetch
    // flushes the re-render: an absence asserted after it is a real absence.
    notifyManager.setScheduler(queueMicrotask)
    onTestFinished(() => notifyManager.setScheduler(defaultScheduler))
    const qc = mount({ download: failedDownload('connection reset') })
    await screen.findByText(/connection reset/)
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByText(/connection reset/)).toBeNull()

    // A re-read reporting the same failure keeps it dismissed.
    await act(() => qc.refetchQueries({ queryKey: ['sttStatus'] }))
    expect(screen.queryByText(/connection reset/)).toBeNull()

    // The same failure after the transfer was seen running is a new attempt's.
    mockApi.sttStatus.mockImplementation(async () => statusBody({
      download: { ...failedDownload(''), step: 'downloading', downloaded_bytes: 1_000_000 },
    }))
    await act(() => qc.refetchQueries({ queryKey: ['sttStatus'] }))
    mockApi.sttStatus.mockImplementation(async () => statusBody({ download: failedDownload('connection reset') }))
    await act(() => qc.refetchQueries({ queryKey: ['sttStatus'] }))
    expect(await screen.findByText(/connection reset/)).toBeTruthy()

    // A different failure is news, so it shows.
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    mockApi.sttStatus.mockImplementation(async () => statusBody({ download: failedDownload('disk full') }))
    await act(() => qc.refetchQueries({ queryKey: ['sttStatus'] }))
    expect(await screen.findByText(/disk full/)).toBeTruthy()

    // Pressing Download now again reports that attempt, even when it fails the
    // same way before any poll sees it running.
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByText(/disk full/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /download now/i }))
    expect(await screen.findByText(/disk full/)).toBeTruthy()
  })

  it('reports a model already on disk instead of offering it again', async () => {
    mount({
      models: [{ name: 'base', size_bytes: BASE_BYTES, present: true }],
    })
    // The contract is that a present model is not offered AGAIN. Asserted as the
    // ABSENCE of the download prompt rather than the presence of a reassuring line:
    // the missing prompt already says it, and a row reassuring the user about the
    // ordinary case crowds out the warnings worth reading.
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    expect(screen.queryByRole('button', { name: /download now/i })).toBeNull()
    expect(screen.queryByText(/download .* now to avoid/i)).toBeNull()
  })
})
