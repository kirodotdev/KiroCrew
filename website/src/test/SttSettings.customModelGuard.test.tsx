/**
 * Settings → Voice for a custom model: a local file in the models folder plus its
 * SHA-256.
 *
 * Four things are pinned here. When the pair cannot deliver a model the panel says
 * so (all four quadrants of that condition, since it is easy to get backwards). A
 * file name or digest the gateway refuses shows the gateway's reason under the rows,
 * with the agent hand-off on. A configured file that is missing says why instead
 * of offering a download, because nothing is downloaded for a custom model. And the
 * setup copy: the SHA-256 row says first where its value comes from, gives the
 * refusal reassurance once (in its hint) and names its folded section after what it
 * works out; the model site is linked once, from step 1, which says the file page
 * lists the SHA-256; and step 3 shows a real file name.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { initI18n, i18next } from '../i18n'
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

const FILE = 'ggml-my-model.bin'
const SHA = 'a'.repeat(64)
const DIR = '/home/you/.kiro/crew/models/whisper'

type Row = { name: string; size_bytes: number; present: boolean; error?: string }

/** Mount with a served `stt` config, which is what a reload restores from. */
function mount(
  config: Record<string, unknown> = {},
  opts: { pairWithheld?: boolean; models?: Row[] } = {},
) {
  const served: Record<string, unknown> = {
    enabled: true,
    provider: 'local',
    model: 'base',
    streaming: false,
    providers: ['local'],
    streaming_providers: ['local'],
    language_codes: ['en-US'],
    prereqs: [],
    custom_model_path: '',
    custom_model_sha256: '',
    custom_model_dir: DIR,
    ...config,
  }
  if (opts.pairWithheld) {
    // What a non-owner dashboard session is served: the gateway OMITS the keys
    // rather than emptying them, and that difference is the whole signal.
    delete served.custom_model_path
    delete served.custom_model_sha256
    delete served.custom_model_dir
  }
  mockApi.sttConfig.mockResolvedValue(served)
  mockApi.saveSttConfig.mockResolvedValue({})
  mockApi.sttStatus.mockResolvedValue({
    available: true,
    code: '',
    detail: '',
    models: opts.models ?? [{ name: 'base', size_bytes: 147_951_465, present: true }],
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
const explanation = () => screen.queryByText(/add both the file name and the SHA-256/i)
const askAgent = () => screen.queryByRole('button', { name: /ask the agent/i })

/** Pick a model the way a user does: open the listbox, click the option. */
async function pickModel(name: RegExp) {
  const trigger = await screen.findByRole('combobox', { name: /model/i })
  await waitFor(() => expect(trigger).not.toHaveAttribute('data-disabled'))
  fireEvent.click(trigger)
  const option = within(screen.getByRole('listbox')).getByRole('option', { name })
  fireEvent.click(option)
}

beforeEach(async () => {
  vi.clearAllMocks()
  await initI18n('en')
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { enumerateDevices: async () => [] },
  })
})
afterEach(() => cleanup())

describe('SttSettings custom-model pair explanation', () => {
  it('explains the degrade when custom is picked with both fields empty', async () => {
    mount()
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    expect(explanation()).toBeNull()

    await pickModel(/custom/i)

    await waitFor(() => expect(explanation()).not.toBeNull())
  })

  it('keeps explaining while only one half of the pair is filled', async () => {
    mount({ custom_model_path: FILE, custom_model_sha256: '' })
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    await waitFor(() => expect(explanation()).not.toBeNull())
  })

  it('says nothing once the pair is complete and custom is in force', async () => {
    mount({ model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA })
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    expect(explanation()).toBeNull()
  })

  it('does not claim both are needed when a catalog model is deliberately chosen', async () => {
    mount({ model: 'base', custom_model_path: FILE, custom_model_sha256: SHA })
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    expect(explanation()).toBeNull()
    // The rows stay open so the stored file can be cleared, and say they are idle.
    expect(await screen.findByText(/not used while base is selected/i)).toBeTruthy()

    await pickModel(/custom/i)
    await waitFor(() => expect(mockApi.saveSttConfig).toHaveBeenCalled())
    expect(explanation()).toBeNull()

    await pickModel(/^base/i)
    expect(explanation()).toBeNull()
  })

  it('stays silent for a caller the pair was withheld from, whose rows read empty', async () => {
    mount({ model: 'custom' }, { pairWithheld: true })
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    expect(explanation()).toBeNull()
  })

  it('leaves a sibling row alone when the other half is saved', async () => {
    mount({ model: 'custom', custom_model_path: '', custom_model_sha256: '' })
    await waitFor(() => expect(modelSelect()).toBeTruthy())

    const sha = await screen.findByLabelText(/SHA-256/i)
    fireEvent.change(sha, { target: { value: SHA } })

    mockApi.saveSttConfig.mockResolvedValue({ custom_model_path: FILE, custom_model_sha256: '' })
    const file = await screen.findByLabelText(/custom model file/i)
    fireEvent.change(file, { target: { value: FILE } })
    fireEvent.blur(file)

    await waitFor(() => expect(mockApi.saveSttConfig).toHaveBeenCalled())
    expect(sha).toHaveValue(SHA)
  })

  it('quotes the file name in the copyable commands so it is never shell syntax', async () => {
    const hostile = "ggml-$(touch x)'; rm -rf ~.bin"
    mount({ model: 'custom', custom_model_path: hostile, custom_model_sha256: SHA })
    await waitFor(() => expect(modelSelect()).toBeTruthy())

    const unix = await screen.findByText((_, el) => el?.tagName === 'CODE' && el.textContent!.startsWith('shasum'))
    const win = screen.getByText((_, el) => el?.tagName === 'CODE' && el.textContent!.startsWith('Get-FileHash'))
    // POSIX: one single-quoted word, each ' closed, escaped and reopened.
    expect(unix.textContent).toBe(`shasum -a 256 'ggml-$(touch x)'\\''; rm -rf ~.bin'`)
    // PowerShell: -LiteralPath and a single-quoted string with each ' doubled.
    expect(win.textContent).toBe(`Get-FileHash -LiteralPath 'ggml-$(touch x)''; rm -rf ~.bin' -Algorithm SHA256`)
  })

  it('names the folder the file has to be in', async () => {
    mount({ model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA })
    expect(await screen.findByText(new RegExp(DIR.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&')))).toBeTruthy()
  })
})

describe('SttSettings custom-model SHA-256 row', () => {
  it('says where the SHA-256 comes from before what it is for', async () => {
    mount({ model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA })
    const desc = await screen.findByText(i18next.t('pages.settings.sttSettings.custom_model_sha256_desc'))
    const firstSentence = (desc.textContent ?? '').split(/(?<=\.)\s/)[0]
    expect(firstSentence).toMatch(/download page/i)
  })

  it('gives the refusal reassurance once, in the hint, not again in the description', async () => {
    mount({ model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA })
    const desc = await screen.findByText(i18next.t('pages.settings.sttSettings.custom_model_sha256_desc'))
    expect(desc.textContent).not.toMatch(/refused/i)
    expect(i18next.t('pages.settings.sttSettings.custom_model_sha256_hint')).toMatch(/refused/i)
  })

  it('links the model site once, from step 1, which says the file page lists the SHA-256', async () => {
    mount({ model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA })
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    const links = (await screen.findAllByRole('link'))
      .filter(a => a.getAttribute('href') === 'https://huggingface.co/ggerganov/whisper.cpp')
    expect(links).toHaveLength(1)
    expect(links[0].textContent).toMatch(/hugging face/i)
    expect(links[0].closest('li')?.textContent).toMatch(/SHA-256/)
  })

  it('names its folded section after what it works out', async () => {
    mount({ model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA })
    const summary = await screen.findByText(i18next.t('pages.settings.sttSettings.custom_model_sha256_yourself'))
    expect(summary.tagName).toBe('SUMMARY')
    expect(summary.textContent).toMatch(/SHA-256/)
  })
})

describe('SttSettings custom-model file-name row', () => {
  it('gives step 3 a real file name to type', async () => {
    mount({ model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA })
    const step = await screen.findByText(i18next.t('pages.settings.sttSettings.custom_model_path_step_name'))
    expect(step.tagName).toBe('LI')
    expect(step.textContent).toMatch(/\bggml-[\w.-]+\.bin\b/)
  })
})

describe('SttSettings custom-model refusals', () => {
  it('shows the reason a file name was refused under the rows, with the hand-off on', async () => {
    mount({ model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA })
    await waitFor(() => expect(modelSelect()).toBeTruthy())

    const reason = 'The model file must be inside the speech models folder.'
    mockApi.saveSttConfig.mockRejectedValue(new Error(reason))
    const file = await screen.findByLabelText(/custom model file/i)
    fireEvent.change(file, { target: { value: '../outside.bin' } })
    fireEvent.blur(file)

    const notice = await screen.findByTestId('stt-custom-model-error')
    expect(notice.textContent).toContain(reason)
    // Names what is still in force, so the Status above is not read as contradicting it.
    expect(notice.textContent).toMatch(/^not saved:/i)
    expect(notice.textContent).toMatch(/not saved: the model file must be inside/i)
    expect(notice.textContent).toMatch(/voice input keeps using ggml-my-model\.bin/i)
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeTruthy()
    // Kept in the row so it can be corrected rather than retyped.
    expect(file).toHaveValue('../outside.bin')
    // Marked invalid, so the kept text does not read as saved.
    expect(file).toHaveAttribute('aria-invalid', 'true')
    expect(file).toHaveAttribute('aria-describedby', 'stt-custom-model-error')
  })

  it('clears the refusal once a corrected value saves', async () => {
    mount({ model: 'custom', custom_model_path: '', custom_model_sha256: SHA })
    await waitFor(() => expect(modelSelect()).toBeTruthy())

    mockApi.saveSttConfig.mockRejectedValueOnce(new Error('No file with that name is in the speech models folder.'))
    const file = await screen.findByLabelText(/custom model file/i)
    fireEvent.change(file, { target: { value: 'ggml-typo.bin' } })
    fireEvent.blur(file)
    await screen.findByTestId('stt-custom-model-error')

    mockApi.saveSttConfig.mockResolvedValue({ model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA })
    fireEvent.change(file, { target: { value: FILE } })
    fireEvent.blur(file)

    await waitFor(() => expect(screen.queryByTestId('stt-custom-model-error')).toBeNull())
  })

  it('says why a configured file cannot be used instead of offering a download', async () => {
    const reason = 'No file with that name is in the speech models folder.'
    mount(
      { model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA },
      { models: [
        { name: 'base', size_bytes: 147_951_465, present: true },
        { name: 'custom', size_bytes: 0, present: false, error: reason },
      ] },
    )

    const notice = await screen.findByTestId('stt-custom-model-missing')
    expect(within(notice).getByText(reason)).toBeTruthy()
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /download now/i })).toBeNull()
  })

  it('withholds the page-level hand-off while a custom draft is unsaved', async () => {
    // The page notice navigates on Ask Agent, which would throw away a file name the
    // user typed and has not saved yet.
    mount({ model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA })
    await waitFor(() => expect(modelSelect()).toBeTruthy())

    const file = await screen.findByLabelText(/custom model file/i)
    fireEvent.change(file, { target: { value: 'ggml-other.bin' } })

    mockApi.saveSttConfig.mockRejectedValue(new Error('transcribe profile is unknown'))
    fireEvent.click(screen.getByRole('switch', { name: /enabled/i }))
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())

    expect(askAgent()).toBeNull()
  })

  it('offers the page-level hand-off once no draft differs from what is stored', async () => {
    mount({ model: 'custom', custom_model_path: FILE, custom_model_sha256: SHA })
    await waitFor(() => expect(modelSelect()).toBeTruthy())

    mockApi.saveSttConfig.mockRejectedValue(new Error('transcribe profile is unknown'))
    fireEvent.click(screen.getByRole('switch', { name: /enabled/i }))
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())

    expect(askAgent()).not.toBeNull()
  })
})
