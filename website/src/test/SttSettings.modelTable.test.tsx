/**
 * The speech-model table (#17602): each catalog model's state, and Use, Download
 * and Remove as that state allows.
 *
 * Pinned here: the selected model is never offered for removal, removal asks
 * first and names the size, a gateway refusal is explained by its code, one
 * transfer at a time holds every other Download, and every row carries at most
 * two buttons.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { initI18n } from '../i18n'
import SttSettings from '../pages/settings/SttSettings'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'

vi.mock('../api/client', async () => {
  const { ApiError: RealApiError } = await vi.importActual<typeof import('../api/apiError')>('../api/apiError')
  return {
    ApiError: RealApiError,
    api: {
      sttConfig: vi.fn(),
      saveSttConfig: vi.fn(),
      sttStatus: vi.fn(),
      sttPrepare: vi.fn(),
      sttDeleteModel: vi.fn(),
    },
  }
})

const mockApi = api as unknown as Record<string, ReturnType<typeof vi.fn>>

const MODELS = [
  { name: 'tiny', size_bytes: 77_691_713, present: false },
  { name: 'base', size_bytes: 147_951_465, present: true },
  { name: 'small', size_bytes: 487_601_967, present: true },
  { name: 'large-v3-turbo', size_bytes: 1_624_555_275, present: false },
]
const IDLE = { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' }

function mount(opts: { model?: string; resolved?: string; models?: typeof MODELS; download?: typeof IDLE } = {}) {
  const config = {
    enabled: true, provider: 'local', model: opts.model ?? 'base', streaming: false,
    providers: ['local'], streaming_providers: ['local'], language_codes: ['en-US'], prereqs: [],
  }
  mockApi.sttConfig.mockResolvedValue(config)
  mockApi.saveSttConfig.mockImplementation(async (patch: object) => ({ ...config, ...patch }))
  mockApi.sttStatus.mockResolvedValue({
    available: true, code: '', detail: '', model: opts.resolved, models: opts.models ?? MODELS, download: opts.download ?? IDLE,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <SttSettings />
      </QueryClientProvider>
    </Provider>,
  )
}

const row = (name: string) => within(screen.getByTestId(`stt-model-${name}`))

beforeEach(async () => {
  vi.clearAllMocks()
  await initI18n('en')
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { enumerateDevices: async () => [] },
  })
})
afterEach(() => cleanup())

describe('SttSettings model table', () => {
  it('shows every catalog model with its state', async () => {
    mount()
    await screen.findByRole('table', { name: /model/i })
    expect(row('base').getByText('Selected')).toBeTruthy()
    expect(row('base').getByText('Installed')).toBeTruthy()
    expect(row('small').getByText('Installed')).toBeTruthy()
    expect(row('small').queryByText('Selected')).toBeNull()
    expect(row('tiny').getByText('Not downloaded')).toBeTruthy()
    expect(screen.getByTestId('stt-model-base').getAttribute('aria-current')).toBe('true')
  })

  // The standard table pattern: `.table-striped` in index.css gives every
  // other row the card highlight, as on every other settings table.
  it('uses the shared striped-table class', async () => {
    mount()
    const table = await screen.findByRole('table', { name: /model/i })
    expect(table.classList.contains('table-striped')).toBe(true)
  })

  it('keeps a notice row on its model stripe and the stripes below it unshifted', async () => {
    mount({ download: { step: 'failed', model: 'tiny', downloaded_bytes: 0, total_bytes: 1, error: 'expected 10 bytes, received 4' } })
    const failed = await screen.findByTestId('stt-model-tiny-failed')
    const bg = (id: string) => (screen.getByTestId(id) as HTMLElement).style.background
    // Catalog order is tiny, base, small, large-v3-turbo: odd positions striped.
    expect(bg('stt-model-tiny')).toBe('transparent')
    expect(failed.style.background).toBe(bg('stt-model-tiny'))
    expect(bg('stt-model-base')).toBe('var(--card-hl)')
    expect(bg('stt-model-small')).toBe('transparent')
    expect(bg('stt-model-large-v3-turbo')).toBe('var(--card-hl)')
  })

  it('marks the model a superseded config name resolves to as selected', async () => {
    // `medium` is still accepted from old configs and loads large-v3-turbo, so
    // that row is the one in use and must not offer Remove.
    const turboPresent = MODELS.map(m => (m.name === 'large-v3-turbo' ? { ...m, present: true } : m))
    mount({ model: 'medium', resolved: 'large-v3-turbo', models: turboPresent })
    await screen.findByRole('table', { name: /model/i })
    expect(row('large-v3-turbo').getByText('Selected')).toBeTruthy()
    expect(row('large-v3-turbo').queryByRole('button', { name: /remove/i })).toBeNull()
    expect(row('base').queryByText('Selected')).toBeNull()
  })

  it('never offers Remove on the selected model, and adds no standing hint for it', async () => {
    mount()
    await screen.findByRole('table', { name: /model/i })
    expect(row('base').queryByRole('button', { name: /remove/i })).toBeNull()
    expect(row('base').queryByRole('button', { name: /^select /i })).toBeNull()
    expect(screen.queryByText(/to remove the selected model/i)).toBeNull()
  })

  it('offers Download on the selected row when its model is not on disk', async () => {
    mount({ model: 'tiny' })
    await screen.findByRole('table', { name: /model/i })
    expect(row('tiny').getByText('Selected')).toBeTruthy()
    expect(row('tiny').getByRole('button', { name: /download tiny/i })).toBeTruthy()
    expect(row('tiny').queryByRole('button', { name: /^select |remove/i })).toBeNull()
    // The prompt is the row right under the selected model, not a line below the
    // table, where it read as being about the last row.
    const notice = screen.getByTestId('stt-model-tiny-notice')
    expect(notice.textContent).toMatch(/77\.7MB/)
    expect(screen.getByTestId('stt-model-tiny').nextElementSibling).toBe(notice)
  })

  it('shows a failed download on its own full-width row under that model', async () => {
    mount({ download: { step: 'failed', model: 'large-v3-turbo', downloaded_bytes: 0, total_bytes: 1, error: 'expected 10 bytes, received 4' } })
    const failed = await screen.findByTestId('stt-model-large-v3-turbo-failed')
    // A sentence with the next step; the raw reason goes to the agent hand-off.
    expect(failed.textContent).toMatch(/the download stopped before it finished\. choose download now to try again/i)
    expect(failed.textContent).not.toMatch(/expected 10 bytes/)
    expect(failed.querySelector('td')?.getAttribute('colspan')).toBe('4')
    // The row that failed still offers its Download, so a retry is one click.
    expect(row('large-v3-turbo').getByRole('button', { name: /download large-v3-turbo/i })).toBeTruthy()
  })

  it('holds at most two buttons per row', async () => {
    mount()
    await screen.findByRole('table', { name: /model/i })
    for (const m of MODELS) {
      expect(row(m.name).queryAllByRole('button').length).toBeLessThanOrEqual(2)
    }
  })

  it('Select writes the model to the config', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /select small/i }))
    await waitFor(() => expect(mockApi.saveSttConfig).toHaveBeenCalledWith({ model: 'small' }))
  })

  it('downloads a model that is not selected, without selecting it', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /download tiny/i }))
    await waitFor(() => expect(mockApi.sttPrepare).toHaveBeenCalledWith('tiny'))
    expect(mockApi.saveSttConfig).not.toHaveBeenCalled()
  })

  it('holds every Download while one transfer runs, so none is started twice', async () => {
    mount({ download: { step: 'downloading', model: 'tiny', downloaded_bytes: 1, total_bytes: 77_691_713, error: '' } })
    await screen.findByRole('table', { name: /model/i })
    expect(row('tiny').queryByRole('button', { name: /download/i })).toBeNull()
    expect(row('tiny').getByText(/downloading the speech model/i)).toBeTruthy()
    const turbo = row('large-v3-turbo').getByRole('button', { name: /download large-v3-turbo/i }) as HTMLButtonElement
    expect(turbo.disabled).toBe(true)
  })

  it('asks before removing, names the size, and removes on confirm', async () => {
    mockApi.sttDeleteModel.mockResolvedValue({ model: 'small', removed: true })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /remove small/i }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/487\.6MB/)).toBeTruthy()
    expect(mockApi.sttDeleteModel).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: /^remove$/i }))
    await waitFor(() => expect(mockApi.sttDeleteModel).toHaveBeenCalledWith('small'))
    // The table re-reads the catalog, so the row shows what is now on disk.
    await waitFor(() => expect(mockApi.sttStatus).toHaveBeenCalledTimes(2))
  })

  it('does nothing when the confirmation is cancelled', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /remove small/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(mockApi.sttDeleteModel).not.toHaveBeenCalled()
  })

  it.each([
    ['stt_model_in_use', /voice input is still using this model/i],
    ['stt_model_loading', /loading a model right now/i],
    ['stt_model_selected', /selected for transcription/i],
    ['stt_model_downloading', /this model is downloading/i],
  ])('explains a %s refusal', async (code, message) => {
    mockApi.sttDeleteModel.mockRejectedValue(
      new ApiError(409, 'refused', JSON.stringify({ error: 'refused', code })),
    )
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /remove small/i }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /^remove$/i }))
    const notice = await screen.findByTestId('stt-model-remove-error')
    expect(notice.textContent).toMatch(message)
    // On a row under the model it was for, not below the whole table.
    expect(screen.getByTestId('stt-model-small-remove-error').contains(notice)).toBe(true)
  })
})
