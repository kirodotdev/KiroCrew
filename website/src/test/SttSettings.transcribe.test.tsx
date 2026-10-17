/**
 * The unavailable surface must name a remedy the user can actually carry out.
 *
 * Transcribe's availability is "boto3 + amazon-transcribe importable by the
 * gateway process", which nothing inside the dashboard can change: a package
 * becomes importable only in a fresh interpreter. So the page renders the
 * backend's prerequisite commands plus the restart that makes them take effect,
 * and it says so cause-neutrally when NO install channel exists at all (a frozen
 * build, a pip-less interpreter, an externally-managed python). These tests pin
 * that, plus the ffmpeg gap, which is deliberately reported even when the status
 * reads ready: the availability probe treats ffmpeg as optional, so a missing
 * one would otherwise surface only as a silent dictation failure.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { initI18n } from '../i18n'
import SttSettings from '../pages/settings/SttSettings'
import { api } from '../api/client'
import {
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
  attachReport,
  consumeChatHandoff,
  installSoftNavigate,
  recordError,
} from '../utils/errorReport'

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
  restartGateway: ReturnType<typeof vi.fn>
  sttStatus: ReturnType<typeof vi.fn>
  sttVocabularies: ReturnType<typeof vi.fn>
  awsConsent: ReturnType<typeof vi.fn>
  grantAwsConsent: ReturnType<typeof vi.fn>
}

function payload(over: Record<string, unknown> = {}) {
  return {
    enabled: true,
    provider: 'local',
    model: 'base',
    streaming: false,
    available: false,
    providers: ['local', 'transcribe'],
    streaming_providers: ['local', 'transcribe'],
    language_codes: ['en-US'],
    prereqs: [],
    ...over,
  }
}

function mount(
  over: Record<string, unknown> = {},
  opts: {
    granted?: boolean
    vocabularies?: unknown
    truncated?: boolean
    seed?: unknown
    pending?: Promise<unknown>
  } = {},
) {
  const data = payload(over)
  mockApi.sttConfig.mockResolvedValue(data)
  mockApi.saveSttConfig.mockImplementation(async (p: Record<string, unknown>) => ({ ...data, ...p }))
  // The list the backend reads from the profile and region the fixture stores, so a
  // fixture cannot accidentally describe another target. A seeded cache never
  // reaches the endpoint: what is on screen is exactly the seed. A `pending`
  // promise is handed to the test, which settles it when the scene calls for it.
  if (opts.pending) mockApi.sttVocabularies.mockReturnValue(opts.pending)
  else if (opts.seed) mockApi.sttVocabularies.mockReturnValue(new Promise(() => {}))
  else if (opts.vocabularies instanceof Error) mockApi.sttVocabularies.mockRejectedValue(opts.vocabularies)
  else mockApi.sttVocabularies.mockResolvedValue({
    profile: data.transcribe_profile ?? '',
    region: data.transcribe_region ?? '',
    listed: true,
    truncated: opts.truncated ?? false,
    vocabularies: opts.vocabularies ?? [],
  })
  // The status endpoint answers the SAME verdict as the config fixture. The two
  // are served from one backend probe, so a fixture where they disagree would
  // exercise a state the gateway cannot produce. That includes the decoder: the
  // block is driven by status's `ffmpeg` object (which names WHICH decoder would
  // run and whether a fetch can fix the host), while the config's
  // `ffmpeg_missing` is only kept for compatibility.
  mockApi.sttStatus.mockResolvedValue({
    available: data.available !== false,
    code: data.available === false ? 'stt_extra_missing' : '',
    detail: '',
    models: [{ name: 'base', size_bytes: 147951465, present: true }],
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
    ffmpeg: {
      present: !data.ffmpeg_missing,
      source: data.ffmpeg_missing ? null : 'system',
      // These fixtures assert the MANUAL command route, which is the branch a
      // platform with no pinned executable takes.
      auto_fetch: data.bundled_interpreter ? 'bundled' : 'unsupported',
      os: 'Linux',
      arch: 'x86_64',
      download: {
        stage: 'idle',
        artifact: '',
        downloaded_bytes: 0,
        total_bytes: 0,
        error_code: '',
        error_detail: '',
      },
    },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (opts.seed) qc.setQueryData(['sttVocabularies'], opts.seed)
  mockApi.awsConsent.mockResolvedValue({
    service: 'transcribe',
    serviceLabel: 'Amazon Transcribe',
    profile: 'old-profile',
    credentialSource: 'profile "old-profile"',
    region: 'us-east-1',
    account: '111111111111',
    arn: 'arn:aws:iam::111111111111:user/old',
    identityResolved: true,
    identityDetail: '',
    granted: opts.granted ?? true,
    reason: '',
    revokedOnAccountChange: false,
  })
  const view = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <SttSettings />
      </QueryClientProvider>
    </Provider>,
  )
  return Object.assign(view, { qc })
}

/**
 * Wait for the loaded card (the Status row only renders post-fetch).
 *
 * Exact text, not a regex: the reason line beneath the badge also contains "not
 * installed", so a loose match finds two nodes and fails on the ambiguity.
 */
const loaded = () => screen.findByText('not installed')

describe('SttSettings provider-aware install surface', () => {
  beforeEach(async () => {
    await initI18n()
    vi.clearAllMocks()
  })
  afterEach(cleanup)

  it('hides the Install button and shows the restart hint for Transcribe', async () => {
    mount({
      provider: 'transcribe',
      prereqs: ["/opt/kirocrew/bin/python -m pip install 'boto3>=1.34,<2' 'amazon-transcribe>=0.6,<1'"],
    })
    await loaded()
    // No install affordance of any kind — the button installs a local Whisper
    // runtime, which cannot change Transcribe's availability.
    expect(screen.queryByRole('button', { name: /install/i })).toBeNull()
    // The prerequisite command from the backend is rendered verbatim…
    expect(screen.getByText(/amazon-transcribe>=0\.6,<1/)).toBeTruthy()
    // …with the next step that makes it take effect.
    expect(screen.getByText(/restart the gateway/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /restart gateway/i })).toBeTruthy()
  })

  it('confirms before restarting and disables the action in flight', async () => {
    let finish!: () => void
    mockApi.restartGateway.mockImplementation(() => new Promise<void>(resolve => { finish = resolve }))
    mount({
      provider: 'transcribe',
      prereqs: ["/opt/kirocrew/bin/python -m pip install 'boto3>=1.34,<2' 'amazon-transcribe>=0.6,<1'"],
    })
    await loaded()

    const restart = screen.getByTestId('stt-restart-gateway')
    fireEvent.click(restart)
    expect(mockApi.restartGateway).not.toHaveBeenCalled()
    expect(restart).toHaveTextContent(/click again to restart/i)

    fireEvent.click(screen.getByTestId('stt-restart-gateway'))
    await waitFor(() => expect(mockApi.restartGateway).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('stt-restart-gateway')).toBeDisabled())
    finish()
  })

  it('offers the restart for the local provider too, which also needs the extra', async () => {
    // The restart follows the pip command rather than the provider: `local` needs
    // the same extra, and the in-dashboard installer that used to cover it is gone.
    mount({
      provider: 'local',
      prereqs: ["/opt/kirocrew/bin/python -m pip install 'boto3>=1.34,<2' 'amazon-transcribe>=0.6,<1'"],
    })
    await loaded()
    expect(screen.getByTestId('stt-restart-gateway')).toBeTruthy()
  })

  it('shows the unsupported notice when no install channel can get the voice extra', async () => {
    mount({ provider: 'transcribe', transcribe_unsupported: true, prereqs: [] })
    await loaded()
    // Frozen build, pip-less interpreter, or externally-managed python: no
    // button and no command can help — the page must say so, cause-neutrally.
    expect(screen.getByText(/can't install extra packages/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /install/i })).toBeNull()
    expect(screen.queryByText(/run these commands/i)).toBeNull()
  })

  it('names the desktop app in the unsupported notice on the bundled interpreter', async () => {
    mount({ provider: 'transcribe', transcribe_unsupported: true, bundled_interpreter: true, prereqs: [] })
    await loaded()
    // "Run the gateway from a different Python environment" is not actionable
    // inside the app bundle — the copy must name the pip-install remedy.
    expect(screen.getByText(/desktop app can't add transcribe support/i)).toBeTruthy()
    expect(screen.queryByText(/this gateway's python can't install extra packages/i)).toBeNull()
  })

  it('surfaces the ffmpeg gap even when Status reads ready', async () => {
    mount({
      provider: 'transcribe',
      available: true,
      ffmpeg_missing: true,
      prereqs: ['sudo apt-get install -y ffmpeg'],
    })
    // `available: true` renders the Ready badge, so the not-installed anchor
    // never appears — wait on the warning itself.
    expect(await screen.findByText(/ffmpeg is missing/i)).toBeTruthy()
    expect(screen.getByText('sudo apt-get install -y ffmpeg')).toBeTruthy()
  })

  it('renders no restart hint for an ffmpeg-only prereq list', async () => {
    // The list carries only the ffmpeg command. ffmpeg needs no restart: the PATH
    // probe re-runs on every settings read, so promising one would be busywork.
    mount({ provider: 'transcribe', prereqs: ['sudo apt-get install -y ffmpeg'] })
    await loaded()
    expect(screen.getByText('sudo apt-get install -y ffmpeg')).toBeTruthy()
    expect(screen.queryByText(/restart the gateway/i)).toBeNull()
  })

  it('surfaces the ffmpeg gap for the local provider too', async () => {
    // The availability checks skip ffmpeg for every provider, so the warning
    // is not Transcribe-gated.
    mount({
      provider: 'local',
      available: true,
      ffmpeg_missing: true,
      prereqs: ['sudo apt-get install -y ffmpeg'],
    })
    expect(await screen.findByText(/ffmpeg is missing/i)).toBeTruthy()
  })

  it('tells a packaged desktop user to reinstall instead of installing FFmpeg', async () => {
    mount({
      provider: 'local',
      available: true,
      ffmpeg_missing: true,
      bundled_interpreter: true,
      prereqs: [],
    })
    expect(await screen.findByText(/bundled audio decoder is missing or damaged/i)).toBeTruthy()
    expect(screen.getByText(/reinstall the Kiro Crew desktop app/i)).toBeTruthy()
    expect(screen.queryByText(/run these commands/i)).toBeNull()
  })

  it('shows no ffmpeg warning when ffmpeg is present', async () => {
    mount({ provider: 'transcribe', available: true, ffmpeg_missing: false, prereqs: [] })
    // Exact, not /ready/i: that substring also matches "already" inside the AI
    // cleanup description, so the loose form started finding two nodes as soon as
    // the panel's copy grew. The assertion is about the STATUS badge.
    await screen.findByText('ready', { exact: true })
    expect(screen.queryByText(/ffmpeg is missing/i)).toBeNull()
  })

  it('renders no Runtime row for any provider', async () => {
    mount({ provider: 'local' })
    await loaded()
    // The backend never serves `docker_mode`, so the row could only ever
    // read "Native" — it conveys nothing and is gone.
    expect(screen.queryByText(/^runtime$/i)).toBeNull()
  })
})

/**
 * The consent gate caches the resolved account under ['awsConsent','transcribe'],
 * which the save mutation must invalidate on a credential change but not otherwise.
 */
describe('SttSettings Transcribe consent-gate refresh', () => {
  beforeEach(async () => {
    await initI18n()
    vi.clearAllMocks()
  })
  afterEach(cleanup)

  it('re-probes the consent gate when the Transcribe profile is saved', async () => {
    const view = mount({ provider: 'transcribe', transcribe_profile: 'old-profile' })
    await loaded()
    const invalidate = vi.spyOn(view.qc, 'invalidateQueries')

    const profileInput = screen.getByLabelText(/aws.*profile/i)
    fireEvent.change(profileInput, { target: { value: 'new-profile' } })
    fireEvent.blur(profileInput)

    await waitFor(() => expect(mockApi.saveSttConfig).toHaveBeenCalledWith({ transcribe_profile: 'new-profile' }))
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['awsConsent', 'transcribe'] }),
    )
  })

  it('re-probes the consent gate when the Transcribe region is saved', async () => {
    const view = mount({ provider: 'transcribe', transcribe_region: 'us-east-1' })
    await loaded()
    const invalidate = vi.spyOn(view.qc, 'invalidateQueries')

    const regionInput = screen.getByLabelText(/aws.*region/i)
    fireEvent.change(regionInput, { target: { value: 'eu-west-1' } })
    fireEvent.blur(regionInput)

    await waitFor(() => expect(mockApi.saveSttConfig).toHaveBeenCalledWith({ transcribe_region: 'eu-west-1' }))
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['awsConsent', 'transcribe'] }),
    )
  })

  it('leaves the consent gate cache alone for a save that is not a credential change', async () => {
    const view = mount({ provider: 'transcribe', enabled: true })
    await loaded()
    const invalidate = vi.spyOn(view.qc, 'invalidateQueries')

    // A save whose patch touches neither profile nor region.
    fireEvent.click(screen.getByLabelText(/^enabled$/i))

    await waitFor(() => expect(mockApi.saveSttConfig).toHaveBeenCalledWith({ enabled: false }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['sttStatus'] })
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ['awsConsent', 'transcribe'] })
  })
})

/**
 * The custom vocabulary picker. Two of its warnings exist because Amazon
 * Transcribe refuses a stream whose vocabulary is missing, not ready, or in
 * another language than dictation, so each of those fails every dictation; the
 * panel is where a user learns that before the first attempt.
 */
describe('SttSettings Transcribe custom vocabulary', () => {
  beforeEach(async () => {
    await initI18n()
    vi.clearAllMocks()
    __resetErrorJournalForTests()
    __resetNavSeamForTests()
    sessionStorage.clear()
    installSoftNavigate(() => {})
  })
  afterEach(() => {
    cleanup()
    __resetNavSeamForTests()
  })

  const vocabularySelect = () => screen.getByRole('combobox', { name: /custom vocabulary/i })
  const READY = { name: 'team-terms', language_code: 'en-US', state: 'READY' }

  it('offers only ready vocabularies, labelled with their language, and saves the pick', async () => {
    mount(
      { provider: 'transcribe', language_code: 'en-US', transcribe_region: 'us-east-1' },
      { vocabularies: [READY, { name: 'still-building', language_code: 'en-US', state: 'PENDING' }] },
    )
    // Disabled while the list is read, so wait for the enabled control.
    await waitFor(() => expect(vocabularySelect()).toBeEnabled())
    fireEvent.click(vocabularySelect())
    expect(await screen.findByRole('option', { name: 'team-terms (en-US)' })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'None' })).toBeTruthy()
    // A pending vocabulary cannot be streamed with yet, so offering it would offer a
    // dictation failure.
    expect(screen.queryByRole('option', { name: /still-building/ })).toBeNull()

    fireEvent.click(screen.getByRole('option', { name: 'team-terms (en-US)' }))
    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenCalledWith({ transcribe_vocabulary: 'team-terms' }),
    )
    expect(screen.queryByTestId('stt-vocabulary-unavailable')).toBeNull()
    expect(screen.queryByTestId('stt-vocabulary-language-mismatch')).toBeNull()
  })

  it('stays hidden, and never asks AWS, until Amazon Transcribe is confirmed', async () => {
    mount({ provider: 'transcribe' }, { granted: false })
    await loaded()
    await screen.findByTestId('aws-consent-transcribe')
    expect(screen.queryByRole('combobox', { name: /custom vocabulary/i })).toBeNull()
    expect(mockApi.sttVocabularies).not.toHaveBeenCalled()
  })

  it('keeps a stored vocabulary visible and clearable while confirmation is missing', async () => {
    mount({ provider: 'transcribe', transcribe_vocabulary: 'team-terms' }, { granted: false })
    await waitFor(() => expect(vocabularySelect()).toHaveTextContent('team-terms'))
    expect(mockApi.sttVocabularies).not.toHaveBeenCalled()

    fireEvent.click(vocabularySelect())
    fireEvent.click(await screen.findByRole('option', { name: 'None' }))
    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenCalledWith({ transcribe_vocabulary: '' }),
    )
  })

  it('disables the picker and says the list is loading until the request settles', async () => {
    let settle!: (list: unknown) => void
    const pending = new Promise(resolve => { settle = resolve })
    mount(
      { provider: 'transcribe', transcribe_vocabulary: 'team-terms', language_code: 'en-US', transcribe_region: 'us-east-1' },
      { pending },
    )
    await waitFor(() => expect(vocabularySelect()).toBeDisabled())
    expect(await screen.findByTestId('stt-vocabularies-loading')).toHaveTextContent('Loading vocabularies…')
    // The stored value stays visible while the list is still being read.
    expect(vocabularySelect()).toHaveTextContent('team-terms')
    expect(screen.queryByTestId('stt-vocabulary-unavailable')).toBeNull()

    await act(async () => {
      settle({ profile: '', region: 'us-east-1', listed: true, vocabularies: [READY] })
    })
    await waitFor(() => expect(vocabularySelect()).toBeEnabled())
    expect(screen.queryByTestId('stt-vocabularies-loading')).toBeNull()
    fireEvent.click(vocabularySelect())
    expect(await screen.findByRole('option', { name: 'team-terms (en-US)' })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'None' })).toBeTruthy()
  })

  it('shows no loading line while confirmation is missing, since nothing is being read', async () => {
    mount({ provider: 'transcribe', transcribe_vocabulary: 'team-terms' }, { granted: false })
    await waitFor(() => expect(vocabularySelect()).toHaveTextContent('team-terms'))
    expect(vocabularySelect()).toBeEnabled()
    expect(screen.queryByTestId('stt-vocabularies-loading')).toBeNull()
    fireEvent.click(vocabularySelect())
    expect(await screen.findByRole('option', { name: 'None' })).toBeTruthy()
    expect(mockApi.sttVocabularies).not.toHaveBeenCalled()
  })

  it('judges nothing from an answer the gateway gave without asking AWS', async () => {
    // Consent can be confirmed locally yet refused at call time (expired SSO, no
    // network): the gateway then answers 200 with `listed: false` and no names.
    // That says nothing about whether the stored vocabulary exists.
    mount(
      { provider: 'transcribe', transcribe_vocabulary: 'team-terms', language_code: 'fr-FR', transcribe_region: 'us-east-1' },
      { seed: { profile: '', region: 'us-east-1', listed: false, vocabularies: [] } },
    )
    await waitFor(() => expect(vocabularySelect()).toHaveTextContent('team-terms'))
    expect(vocabularySelect()).toBeEnabled()
    expect(screen.queryByTestId('stt-vocabulary-unavailable')).toBeNull()
    expect(screen.queryByTestId('stt-vocabulary-language-mismatch')).toBeNull()
    expect(screen.queryByTestId('stt-vocabularies-error')).toBeNull()
    expect(screen.queryByTestId('stt-vocabularies-loading')).toBeNull()
    fireEvent.click(vocabularySelect())
    await screen.findByRole('option', { name: 'None' })
    expect(screen.getAllByRole('option').map(o => o.textContent)).toEqual(['None', 'team-terms'])
  })

  it('does not infer a stored vocabulary is absent from a truncated listing', async () => {
    mount(
      { provider: 'transcribe', transcribe_vocabulary: 'stored-after-cap', transcribe_region: 'eu-west-1' },
      { vocabularies: [READY], truncated: true },
    )
    await waitFor(() => expect(vocabularySelect()).toHaveTextContent('stored-after-cap'))
    expect(screen.queryByTestId('stt-vocabulary-unavailable')).toBeNull()
  })

  it('warns when a truncated listing includes the stored vocabulary as pending', async () => {
    mount(
      { provider: 'transcribe', transcribe_vocabulary: 'still-building', transcribe_region: 'eu-west-1' },
      {
        vocabularies: [READY, { name: 'still-building', language_code: 'en-US', state: 'PENDING' }],
        truncated: true,
      },
    )
    expect(await screen.findByTestId('stt-vocabulary-unavailable')).toHaveTextContent('still-building')
  })

  it('still warns when the same missing-name listing is complete', async () => {
    mount(
      { provider: 'transcribe', transcribe_vocabulary: 'stored-after-cap', transcribe_region: 'eu-west-1' },
      { vocabularies: [READY], truncated: false },
    )
    expect(await screen.findByTestId('stt-vocabulary-unavailable')).toHaveTextContent('stored-after-cap')
  })

  it('warns, without replacing it, when the stored vocabulary is not ready in the region', async () => {
    mount(
      { provider: 'transcribe', transcribe_vocabulary: 'deleted-terms', transcribe_region: 'eu-west-1' },
      { vocabularies: [READY, { name: 'deleted-terms', language_code: 'en-US', state: 'FAILED' }] },
    )
    const warning = await screen.findByTestId('stt-vocabulary-unavailable')
    expect(warning).toHaveTextContent('deleted-terms')
    expect(warning).toHaveTextContent('eu-west-1')
    // It appears after the list arrives, so a screen reader must be told without a re-read.
    expect(screen.getByRole('alert')).toBe(warning)
    // Opening the panel must never change the setting by itself.
    expect(vocabularySelect()).toHaveTextContent('deleted-terms')
    expect(mockApi.saveSttConfig).not.toHaveBeenCalled()
  })

  it('names the provider default region in the warning when the region field is cleared', async () => {
    mount(
      { provider: 'transcribe', transcribe_vocabulary: 'team-terms', transcribe_region: '' },
      { vocabularies: [] },
    )
    const warning = await screen.findByTestId('stt-vocabulary-unavailable')
    expect(warning).toHaveTextContent('team-terms')
    expect(warning).toHaveTextContent('in (provider default).')
    expect(warning).not.toHaveTextContent('in .')
  })

  it('warns when the vocabulary is for another language than dictation', async () => {
    mount(
      { provider: 'transcribe', transcribe_vocabulary: 'team-terms', language_code: 'fr-FR' },
      { vocabularies: [READY] },
    )
    const warning = await screen.findByTestId('stt-vocabulary-language-mismatch')
    expect(warning).toHaveTextContent('en-US')
    expect(warning).toHaveTextContent('fr-FR')
    expect(screen.getByRole('alert')).toBe(warning)
    expect(screen.queryByTestId('stt-vocabulary-unavailable')).toBeNull()
  })

  it('judges a stored name only against a list read from the region now configured', async () => {
    const view = mount(
      { provider: 'transcribe', transcribe_vocabulary: 'team-terms', transcribe_region: 'eu-west-1' },
      { seed: { profile: '', region: 'us-east-1', listed: true, vocabularies: [] } },
    )
    await waitFor(() => expect(vocabularySelect()).toBeTruthy())
    // The seeded list describes another region, so it proves nothing about this one.
    expect(screen.queryByTestId('stt-vocabulary-unavailable')).toBeNull()

    act(() => {
      view.qc.setQueryData(['sttVocabularies'], { profile: '', region: 'eu-west-1', listed: true, vocabularies: [] })
    })
    expect(await screen.findByTestId('stt-vocabulary-unavailable')).toHaveTextContent('eu-west-1')
  })

  it('names the IAM permission when AWS refuses to list', async () => {
    const refusal = Object.assign(new Error('502'), {
      body: JSON.stringify({
        error: 'could not list',
        code: 'stt_vocabularies_access_denied',
        permission: 'transcribe:ListVocabularies',
      }),
    })
    mount({ provider: 'transcribe' }, { vocabularies: refusal })
    expect(await screen.findByTestId('stt-vocabularies-error')).toHaveTextContent(
      'transcribe:ListVocabularies',
    )
  })

  it('says the list could not be loaded for any other failure', async () => {
    mount({ provider: 'transcribe' }, { vocabularies: new Error('network down') })
    const notice = await screen.findByTestId('stt-vocabularies-error')
    expect(notice).toHaveTextContent(/could not be loaded/i)
    expect(notice).not.toHaveTextContent('transcribe:ListVocabularies')
  })

  it('hands a structured listing failure to the agent with its endpoint, status, and code', async () => {
    const body = JSON.stringify({
      error: 'could not list custom vocabularies',
      code: 'stt_vocabularies_list_failed',
    })
    const failure = Object.assign(new Error('Bad Gateway'), { status: 502, body })
    attachReport(failure, recordError({
      source: 'api',
      message: failure.message,
      status: failure.status,
      code: 'stt_vocabularies_list_failed',
      endpoint: '/api/stt/vocabularies',
      detail: body,
    }))
    mount({ provider: 'transcribe' }, { vocabularies: failure })
    expect(await screen.findByTestId('stt-vocabularies-error')).toHaveTextContent(/could not be loaded/i)

    fireEvent.click(screen.getByRole('button', { name: /ask the agent/i }))

    const prompt = consumeChatHandoff() ?? ''
    expect(prompt).toContain('/api/stt/vocabularies')
    expect(prompt).toContain('HTTP 502')
    expect(prompt).toContain('stt_vocabularies_list_failed')
  })

  it('re-reads the list when the AWS region is saved', async () => {
    const view = mount({ provider: 'transcribe', transcribe_region: 'us-east-1' })
    await loaded()
    const invalidate = vi.spyOn(view.qc, 'invalidateQueries')

    const regionInput = screen.getByLabelText(/aws.*region/i)
    fireEvent.change(regionInput, { target: { value: 'eu-west-1' } })
    fireEvent.blur(regionInput)

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['sttVocabularies'] }))
  })

  it('re-reads the list once Amazon Transcribe is confirmed', async () => {
    mockApi.grantAwsConsent.mockResolvedValue({})
    const view = mount({ provider: 'transcribe' }, { granted: false })
    const confirm = await screen.findByTestId('aws-consent-transcribe-confirm')
    const invalidate = vi.spyOn(view.qc, 'invalidateQueries')

    fireEvent.click(confirm)

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['sttVocabularies'] }))
  })
})
