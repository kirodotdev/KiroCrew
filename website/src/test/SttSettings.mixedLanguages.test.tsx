vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))

/**
 * The Transcribe mixed-language picker.
 *
 * Amazon Transcribe identifies languages in a stream only from two candidates up,
 * with one dialect per language, so the picker's job is to never let a user build
 * a list the backend refuses and to say plainly when the list does not take
 * effect yet. Every tick saves, like the rest of the panel.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { initI18n } from '../i18n'
import SttSettings from '../pages/settings/SttSettings'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    sttConfig: vi.fn(),
    saveSttConfig: vi.fn(),
    restartGateway: vi.fn(),
    sttStatus: vi.fn(),
    sttPrepare: vi.fn(),
    sttVocabularies: vi.fn(),
    awsConsent: vi.fn(),
    grantAwsConsent: vi.fn(),
  },
}))

const mockApi = api as unknown as {
  sttConfig: ReturnType<typeof vi.fn>
  saveSttConfig: ReturnType<typeof vi.fn>
  sttStatus: ReturnType<typeof vi.fn>
  sttVocabularies: ReturnType<typeof vi.fn>
  awsConsent: ReturnType<typeof vi.fn>
}

const CODES = ['en-US', 'en-GB', 'fr-FR', 'de-DE', 'ja-JP', 'ko-KR', 'vi-VN']

function mount(over: Record<string, unknown> = {}, vocabularies: unknown[] = []) {
  let data: Record<string, unknown> = {
    enabled: true,
    provider: 'transcribe',
    model: 'base',
    streaming: true,
    available: true,
    providers: ['local', 'transcribe'],
    streaming_providers: ['local', 'transcribe'],
    language_code: 'en-US',
    language_codes: CODES,
    transcribe_languages: [],
    transcribe_languages_min: 2,
    transcribe_languages_max: 5,
    transcribe_region: 'us-east-1',
    transcribe_profile: '',
    prereqs: [],
    ...over,
  }
  mockApi.sttConfig.mockImplementation(async () => data)
  // The backend answers with the stored config, so a save is what the next render
  // shows, exactly as the PUT's response replaces the cached config.
  mockApi.saveSttConfig.mockImplementation(async (patch: Record<string, unknown>) => {
    data = { ...data, ...patch }
    return data
  })
  mockApi.sttVocabularies.mockResolvedValue({
    profile: '',
    region: 'us-east-1',
    listed: true,
    truncated: false,
    vocabularies,
  })
  mockApi.sttStatus.mockResolvedValue({
    available: true,
    code: '',
    detail: '',
    models: [],
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
  })
  mockApi.awsConsent.mockResolvedValue({
    service: 'transcribe',
    serviceLabel: 'Amazon Transcribe',
    profile: '',
    credentialSource: 'default',
    region: 'us-east-1',
    account: '111111111111',
    arn: 'arn:aws:iam::111111111111:user/test',
    identityResolved: true,
    identityDetail: '',
    granted: true,
    reason: '',
    revokedOnAccountChange: false,
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

const trigger = () => screen.findByRole('button', { name: 'Mixed languages' })

/** A candidate's checkbox: its accessible name is "<language name> (<code>)". */
const box = (code: string) => new RegExp(`\\(${code}\\)$`)

// The vocabulary list is the third query in a chain: sttConfig selects
// Transcribe, which enables awsConsent, whose confirmation enables
// sttVocabularies. Each settles only after the previous answer, so under a
// loaded coverage runner the chain outlasts the default one-second wait.
const VOCABULARY_CHAIN_TIMEOUT_MS = 5_000

async function open() {
  fireEvent.click(await trigger())
}

describe('SttSettings Transcribe mixed languages', () => {
  beforeEach(async () => {
    await initI18n()
    vi.clearAllMocks()
  })
  afterEach(() => cleanup())

  it('is offered for Transcribe and reads as off with nothing picked', async () => {
    mount()
    expect(await trigger()).toHaveTextContent('Off (uses the language above)')
  })

  it('is not offered for the local provider, which has no identification', async () => {
    mount({ provider: 'local', language_codes: ['auto', ...CODES], language_code: 'auto' })
    await screen.findByRole('combobox', { name: 'Language' })
    expect(screen.queryByRole('button', { name: 'Mixed languages' })).toBeNull()
  })

  it('offers Vietnamese and saves each tick, saying when one language is not enough', async () => {
    mount()
    await open()
    fireEvent.click(screen.getByRole('checkbox', { name: box('vi-VN') }))
    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenLastCalledWith({ transcribe_languages: ['vi-VN'] }),
    )
    expect(await screen.findByTestId('stt-transcribe-languages-pending')).toHaveTextContent(
      'Until then, dictation uses the language above.',
    )
    // The box itself says one pick does not turn it on.
    expect(await trigger()).toHaveTextContent('Vietnamese: pick one more to turn on')

    fireEvent.click(screen.getByRole('checkbox', { name: box('en-US') }))
    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenLastCalledWith({
        transcribe_languages: ['vi-VN', 'en-US'],
      }),
    )
    await waitFor(() => expect(screen.queryByTestId('stt-transcribe-languages-pending')).toBeNull())
    // The closed box names the languages; the codes are in the open list.
    await waitFor(async () => expect(await trigger()).toHaveTextContent('Vietnamese and English'))
  })

  it('keeps every row in place while the list is open, and sorts ticked rows first on reopen', async () => {
    mount({ transcribe_languages: ['vi-VN', 'en-US'] })
    await open()
    const labels = () => screen.getAllByRole('checkbox').map(el => el.getAttribute('aria-label'))
    const before = labels()
    fireEvent.click(screen.getByRole('checkbox', { name: box('ko-KR') }))
    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenLastCalledWith({
        transcribe_languages: ['vi-VN', 'en-US', 'ko-KR'],
      }),
    )
    await waitFor(() => expect(screen.getByRole('checkbox', { name: box('ko-KR') })).toBeChecked())
    expect(labels()).toEqual(before)

    fireEvent.click(await trigger())
    await open()
    expect(labels().slice(0, 3)).toEqual(['Vietnamese (vi-VN)', 'English (en-US)', 'Korean (ko-KR)'])
  })

  it('takes no second tick while a save is in flight, so it cannot overwrite the first', async () => {
    mount({ transcribe_languages: ['en-US'] })
    let finish: (value: unknown) => void = () => {}
    const saved = mockApi.saveSttConfig.getMockImplementation()!
    mockApi.saveSttConfig.mockImplementationOnce(
      (patch: Record<string, unknown>) =>
        new Promise(resolve => { finish = () => resolve(saved(patch)) }),
    )
    await open()
    fireEvent.click(screen.getByRole('checkbox', { name: box('vi-VN') }))
    await waitFor(() => expect(mockApi.saveSttConfig).toHaveBeenCalledTimes(1))
    // Built from the list before the first save landed, this would drop vi-VN.
    await waitFor(() => expect(screen.getByRole('checkbox', { name: box('fr-FR') })).toBeDisabled())
    fireEvent.click(screen.getByRole('checkbox', { name: box('fr-FR') }))
    expect(mockApi.saveSttConfig).toHaveBeenCalledTimes(1)

    finish(undefined)
    await waitFor(() => expect(screen.getByRole('checkbox', { name: box('fr-FR') })).toBeEnabled())
    fireEvent.click(screen.getByRole('checkbox', { name: box('fr-FR') }))
    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenLastCalledWith({
        transcribe_languages: ['en-US', 'vi-VN', 'fr-FR'],
      }),
    )
  })

  it('swaps a dialect in instead of sending two of one language', async () => {
    mount({ transcribe_languages: ['vi-VN', 'en-US'] })
    await open()
    fireEvent.click(screen.getByRole('checkbox', { name: box('en-GB') }))
    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenLastCalledWith({
        transcribe_languages: ['vi-VN', 'en-GB'],
      }),
    )
  })

  it('unticking saves the shorter list', async () => {
    mount({ transcribe_languages: ['vi-VN', 'en-US'] })
    await open()
    fireEvent.click(screen.getByRole('checkbox', { name: box('vi-VN') }))
    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenLastCalledWith({ transcribe_languages: ['en-US'] }),
    )
  })

  it('locks the rest at the maximum, but keeps a dialect swap open', async () => {
    mount({ transcribe_languages: ['en-US', 'fr-FR', 'de-DE', 'ja-JP', 'ko-KR'] })
    await open()
    const vietnamese = screen.getByRole('checkbox', { name: box('vi-VN') })
    expect(vietnamese).toBeDisabled()
    // One reason at the top of the list rather than a line under every locked row.
    expect(screen.getByRole('status')).toHaveTextContent('Limit of 5 reached')
    expect(vietnamese.closest('label')).not.toHaveTextContent('Limit of 5 reached')
    // Swapping en-US for en-GB does not grow the list.
    expect(screen.getByRole('checkbox', { name: box('en-GB') })).toBeEnabled()
    expect(screen.getByRole('checkbox', { name: box('ko-KR') })).toBeEnabled()
    fireEvent.click(vietnamese)
    expect(mockApi.saveSttConfig).not.toHaveBeenCalled()
  })

  it('names each language and lists the ticked ones first', async () => {
    mount({ transcribe_languages: ['vi-VN', 'en-US'] })
    await open()
    expect(screen.getByRole('checkbox', { name: 'Vietnamese (vi-VN)' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'English (en-GB)' })).not.toBeChecked()
    const order = screen.getAllByRole('checkbox').map(el => el.getAttribute('aria-label'))
    expect(order.slice(0, 2)).toEqual(['Vietnamese (vi-VN)', 'English (en-US)'])
  })

  it('says the language above is not what live dictation uses once mixed languages are on', async () => {
    mount({ transcribe_languages: ['vi-VN', 'en-US'] })
    const line = await screen.findByTestId('stt-language-overridden')
    expect(line).toHaveTextContent('voice messages')
    // Names the toggle a user would change, by its own label.
    expect(line).toHaveTextContent('when Streaming is off')
  })

  it('names the languages in the Language picker the same way', async () => {
    mount({ language_code: 'vi-VN' })
    expect(await screen.findByRole('combobox', { name: 'Language' })).toHaveTextContent(
      'Vietnamese (vi-VN)',
    )
  })

  it('shows no limit notice below the maximum', async () => {
    mount({ transcribe_languages: ['vi-VN', 'en-US'] })
    await open()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('says nothing about the language above while mixed languages are off', async () => {
    mount({ transcribe_languages: ['vi-VN'] })
    await screen.findByTestId('stt-transcribe-languages-pending')
    expect(screen.queryByTestId('stt-language-overridden')).toBeNull()
  })

  it('accepts a vocabulary in a candidate language that is also the language above', async () => {
    const READY = { name: 'team-terms', language_code: 'en-US', state: 'READY' }
    mount(
      { language_code: 'en-US', transcribe_vocabulary: 'team-terms', transcribe_languages: ['vi-VN', 'en-US'] },
      [READY],
    )
    await screen.findByRole('combobox', { name: /custom vocabulary/i })
    // Waits for the sttConfig -> awsConsent -> sttVocabularies chain to issue its last query.
    await waitFor(() => expect(mockApi.sttVocabularies).toHaveBeenCalled(), {
      timeout: VOCABULARY_CHAIN_TIMEOUT_MS,
    })
    expect(screen.queryByTestId('stt-vocabulary-language-mismatch')).toBeNull()
  })

  it('still warns for voice messages when the vocabulary fits a candidate but not the language above', async () => {
    // Voice messages keep the one language above, with the vocabulary as its own.
    const READY = { name: 'team-terms', language_code: 'en-US', state: 'READY' }
    mount(
      { language_code: 'vi-VN', transcribe_vocabulary: 'team-terms', transcribe_languages: ['vi-VN', 'en-US'] },
      [READY],
    )
    // The warning renders once the sttConfig -> awsConsent -> sttVocabularies chain settles.
    const warning = await screen.findByTestId(
      'stt-vocabulary-language-mismatch',
      {},
      { timeout: VOCABULARY_CHAIN_TIMEOUT_MS },
    )
    expect(warning).toHaveTextContent('vi-VN')
    expect(warning).toHaveTextContent(/dictation is set to vi-VN\./)
    expect(warning).not.toHaveTextContent(/Mixed languages/)
  })

  it('warns when the vocabulary is in none of the candidate languages', async () => {
    const READY = { name: 'team-terms', language_code: 'en-US', state: 'READY' }
    mount(
      { language_code: 'en-US', transcribe_vocabulary: 'team-terms', transcribe_languages: ['vi-VN', 'fr-FR'] },
      [READY],
    )
    // The warning renders once the sttConfig -> awsConsent -> sttVocabularies chain settles.
    const warning = await screen.findByTestId(
      'stt-vocabulary-language-mismatch',
      {},
      { timeout: VOCABULARY_CHAIN_TIMEOUT_MS },
    )
    expect(warning).toHaveTextContent('vi-VN')
    expect(warning).toHaveTextContent('fr-FR')
    // Dictation picks among the candidates; it is not "set to" either of them.
    expect(warning).toHaveTextContent(/not one of your Mixed languages \(vi-VN and fr-FR\)/)
    expect(warning).not.toHaveTextContent(/dictation is set to/)
  })
})
