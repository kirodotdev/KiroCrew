/**
 * When the custom-model pair cannot deliver a model, the panel has to say so.
 *
 * A `custom` selection whose URL/SHA-256 pair is absent or half-filled is served
 * back as the DEFAULT model, so the picker visibly snaps to `base`. Without a line
 * explaining why, that reads as a control that ignored the click — and the setting
 * the user came to change appears not to work.
 *
 * The condition is easy to get backwards, and shipped backwards: gated on a
 * non-custom `stt.model` plus EITHER field filled, it stayed silent in the empty
 * first-run state (the case it exists for) and asserted "both are needed" over a
 * COMPLETE pair whenever a catalog model was reselected. So all four quadrants are
 * pinned here, not just the one that motivated the line.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
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
    sttStatus: vi.fn(),
    sttPrepare: vi.fn(),
  },
}))

const mockApi = api as unknown as {
  sttConfig: ReturnType<typeof vi.fn>
  saveSttConfig: ReturnType<typeof vi.fn>
  sttStatus: ReturnType<typeof vi.fn>
  sttPrepare: ReturnType<typeof vi.fn>
}

const URL_ = 'https://models.example/ggml-my-model.bin'
const SHA = 'a'.repeat(64)

/** Mount with a served `stt` config, which is what a reload restores from. */
function mount(config: Record<string, unknown> = {}) {
  mockApi.sttConfig.mockResolvedValue({
    enabled: true,
    provider: 'local',
    model: 'base',
    streaming: false,
    providers: ['local'],
    streaming_providers: ['local'],
    language_codes: ['en-US'],
    prereqs: [],
    custom_model_url: '',
    custom_model_sha256: '',
    ...config,
  })
  mockApi.saveSttConfig.mockResolvedValue({})
  mockApi.sttStatus.mockResolvedValue({
    available: true,
    code: '',
    detail: '',
    models: [{ name: 'base', size_bytes: 147_951_465, present: true }],
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
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

const modelSelect = () => screen.getByRole('combobox', { name: /model/i })
/** The explanation line, by its served copy rather than by a class or position. */
const explanation = () => screen.queryByText(/both the URL and the SHA-256 are needed/i)

/** Pick a model the way a user does: open the listbox, click the option. */
async function pickModel(name: RegExp) {
  const trigger = await screen.findByRole('combobox', { name: /model/i })
  await waitFor(() => expect(trigger).not.toHaveAttribute('data-disabled'))
  fireEvent.click(trigger)
  const option = within(screen.getByRole('listbox')).getByRole('option', { name })
  fireEvent.click(option)
}

describe('SttSettings custom-model pair explanation', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    await initI18n('en')
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: async () => [] },
    })
  })
  afterEach(() => cleanup())

  it('explains the degrade when custom is picked with both fields empty', async () => {
    // The first-run case, and the one the old condition was silent for: nothing is
    // filled in yet, so "either field filled" was false and the user watched the
    // selection snap back to `base` with no explanation at all.
    mount()
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    expect(explanation()).toBeNull()

    await pickModel(/custom/i)

    await waitFor(() => expect(explanation()).not.toBeNull())
  })

  it('keeps explaining while only one half of the pair is filled', async () => {
    mount({ custom_model_url: URL_, custom_model_sha256: '' })
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    // A half pair is a custom model someone started and did not finish, so the
    // intent survives the reload that served it.
    expect(explanation()).not.toBeNull()
  })

  it('says nothing once the pair is complete and custom is in force', async () => {
    mount({ model: 'custom', custom_model_url: URL_, custom_model_sha256: SHA })
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    expect(explanation()).toBeNull()
  })

  it('does not claim both are needed when a catalog model is deliberately chosen', async () => {
    // The false-assertion case. A complete pair is stored and the user is on a
    // catalog model on purpose; the old condition fired here and, because the rows
    // reopen from the stored URL on every load, kept firing across reloads.
    mount({ model: 'base', custom_model_url: URL_, custom_model_sha256: SHA })
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    expect(explanation()).toBeNull()

    await pickModel(/custom/i)
    await waitFor(() => expect(mockApi.saveSttConfig).toHaveBeenCalled())
    // Still silent: the pair is complete, so there is nothing to explain.
    expect(explanation()).toBeNull()

    await pickModel(/^base/i)
    expect(explanation()).toBeNull()
  })

  it('clears a digest the backend rejected, so the explanation is not masked', async () => {
    // A 63-character or `sha256:`-prefixed paste does not validate, so the backend
    // drops it and answers without it. The row kept showing the typed text, which
    // made the pair look complete and silenced the line — the user saw both rows
    // filled, `base` in force, and no message at all for the rest of the session.
    mount({ model: 'custom', custom_model_url: URL_, custom_model_sha256: SHA })
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    expect(explanation()).toBeNull()

    // The answer is the server's view: URL kept, digest refused and absent.
    mockApi.saveSttConfig.mockResolvedValue({
      custom_model_url: URL_,
      custom_model_sha256: '',
    })
    const sha = screen.getByLabelText(/SHA-256/i)
    fireEvent.change(sha, { target: { value: 'sha256:' + 'b'.repeat(64) } })
    fireEvent.blur(sha)

    await waitFor(() => expect(sha).toHaveValue(''))
    expect(explanation()).not.toBeNull()
  })

  it('leaves a sibling row alone when the other half is saved', async () => {
    // The resync is per-patched-row on purpose: blurring the URL commits it, and
    // that answer must not wipe a digest the user has already begun typing.
    mount({ model: 'custom', custom_model_url: '', custom_model_sha256: '' })
    await waitFor(() => expect(modelSelect()).toBeTruthy())

    const sha = screen.getByLabelText(/SHA-256/i)
    fireEvent.change(sha, { target: { value: SHA } })

    mockApi.saveSttConfig.mockResolvedValue({ custom_model_url: URL_, custom_model_sha256: '' })
    const url = screen.getByLabelText(/model URL/i)
    fireEvent.change(url, { target: { value: URL_ } })
    fireEvent.blur(url)

    await waitFor(() => expect(mockApi.saveSttConfig).toHaveBeenCalled())
    expect(sha).toHaveValue(SHA)
  })
})


/**
 * The Ask Agent hand-off NAVIGATES, so offering it while a draft is unsaved throws
 * that draft away.
 *
 * The panel already withheld the hand-off while the AWS profile / region drafts
 * differed from the stored values, for exactly this reason. The custom-model URL and
 * digest commit through the same `onBlur` -> `set()` path and are deliberately kept
 * on rejection so they can be corrected — but they were left out of that condition,
 * which made the omission worse than the case it was modelled on: a REJECTED URL is
 * the state in which a user is most likely to ask the agent for help, and the string
 * at risk is a long opaque URL or a 64-character digest that has to be retyped.
 */
describe('SttSettings custom-model draft hand-off guard', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    await initI18n('en')
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: async () => [] },
    })
  })
  afterEach(() => cleanup())

  /** Drive a save REJECTION, which is the only state that renders the notice. */
  async function failSaveOf(label: RegExp, value: string) {
    mockApi.saveSttConfig.mockRejectedValue(new Error('custom_model_url is not https'))
    const field = screen.getByLabelText(label)
    fireEvent.change(field, { target: { value } })
    fireEvent.blur(field)
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
  }

  const askAgent = () => screen.queryByRole('button', { name: /ask the agent/i })

  it('withholds the hand-off while a rejected URL is still in the row', async () => {
    mount({ model: 'custom', custom_model_url: '', custom_model_sha256: SHA })
    await waitFor(() => expect(modelSelect()).toBeTruthy())

    await failSaveOf(/model URL/i, 'http://insecure.example/m.bin')

    // The typed text is still in the row and differs from the stored value, so
    // navigating away would discard it.
    expect(askAgent()).toBeNull()
  })

  it('withholds the hand-off while a rejected digest is still in the row', async () => {
    mount({ model: 'custom', custom_model_url: URL_, custom_model_sha256: '' })
    await waitFor(() => expect(modelSelect()).toBeTruthy())

    await failSaveOf(/SHA-256/i, 'not-a-digest')

    expect(askAgent()).toBeNull()
  })

  it('offers the hand-off once no draft differs from what is stored', async () => {
    // Same failure surface, but the custom rows hold exactly the stored values, so
    // there is nothing for the navigation to lose and the agent can help.
    mount({ model: 'custom', custom_model_url: URL_, custom_model_sha256: SHA })
    await waitFor(() => expect(modelSelect()).toBeTruthy())

    mockApi.saveSttConfig.mockRejectedValue(new Error('transcribe profile is unknown'))
    const enabled = screen.getByRole('switch', { name: /enabled/i })
    fireEvent.click(enabled)
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())

    expect(askAgent()).not.toBeNull()
  })
})
