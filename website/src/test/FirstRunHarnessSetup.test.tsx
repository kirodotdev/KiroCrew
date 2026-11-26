/**
 * The first-run harness step (`FirstRunHarnessSetup`) on its own: what each
 * pick saves, which screen follows, and how failed reads and saves show.
 */
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AcpBackendProbe } from '../api/client'
import { ApiError } from '../api/apiError'
import FirstRunHarnessSetup from '../components/FirstRunHarnessSetup'
import { renderWithProviders } from './helpers'
import { probe, status } from './KiroPrerequisiteGateTestUtils'

vi.mock('../api/client', async () => ({
  ApiError: (await import('../api/apiError')).ApiError,
  api: {
    kasBundleStatus: vi.fn(),
    acpBackends: vi.fn(),
    acpBackendRecheck: vi.fn(),
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    kiroPrerequisite: vi.fn(),
    kasLoginStatus: vi.fn(),
  },
}))

// The shared sign-in flow has its own tests; here it only has to report a sign-in.
vi.mock('../components/KiroSignInDialog', () => ({
  KiroSignInFlow: ({ onSignedIn }: { onSignedIn: () => void }) => (
    <button type="button" onClick={onSignedIn}>stub sign-in</button>
  ),
  KiroSignInStrip: () => null,
}))

const schemaQuery = vi.hoisted(() => ({ value: { schema: undefined as unknown, error: null as Error | null } }))
vi.mock('../components/settingRef/useConfigSchema', () => ({
  useConfigSchemaQuery: () => schemaQuery.value,
  useConfigSchema: () => schemaQuery.value.schema,
}))

vi.mock('../providers/adapters/acp', () => ({ clearCachedModels: vi.fn() }))

import { api } from '../api/client'

const WAIT = { timeout: 10_000 }
const signedOut = { authenticated: false, provider: null, identity: null, transport: 'loopback' as const }

function backends(...rows: AcpBackendProbe[]) {
  vi.mocked(api.acpBackends).mockResolvedValue({ backends: rows })
}

const kiroMissing = probe({ id: 'kiro', policy_id: 'kiro', installed: 'missing', independent_setup: false, install_command: '' })
const kiroInstalled = probe({ id: 'kiro', policy_id: 'kiro', installed: 'installed', independent_setup: false })
const strands = probe({
  id: 'strands',
  policy_id: 'strands',
  installed: 'installed',
  independent_setup: true,
  auth: { signs_in_separately: true, sign_in_remedy: 'Save a model provider key.' },
})

// Continue is off until the saved config (and, for a saved harness, the list) is
// read, and a click on it before then is dropped.
async function continueReady() {
  await waitFor(() => expect(screen.getByTestId('harness-setup-continue')).toBeEnabled(), WAIT)
}

function renderStep(overrides: Parameters<typeof status>[0] = {}) {
  const onDone = vi.fn()
  const view = renderWithProviders(<FirstRunHarnessSetup status={status(overrides)} onDone={onDone} />)
  return { onDone, ...view }
}

describe('FirstRunHarnessSetup', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    schemaQuery.value = { schema: undefined, error: null }
    vi.mocked(api.kasBundleStatus).mockResolvedValue({ enabled: true, engine_choice: 'engine' } as never)
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kas' } } as never)
    vi.mocked(api.patchConfig).mockResolvedValue({} as never)
    vi.mocked(api.kasLoginStatus).mockResolvedValue(signedOut as never)
    backends(kiroMissing, strands)
  })

  it('picks the built-in harness first, saves it and signs in before ready', async () => {
    const { onDone } = renderStep()
    expect(await screen.findByRole('radio', { name: /Built-in harness/ }, WAIT)).toBeChecked()
    await continueReady()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    fireEvent.click(await screen.findByRole('button', { name: 'stub sign-in' }, WAIT))
    // The back arrow is ~26 px: a coarse pointer gets the shared 44 px hit area.
    expect(screen.getByTestId('harness-setup-back')).toHaveClass('mc-touch-hit')
    expect(await screen.findByTestId('harness-setup-ready', {}, WAIT)).toHaveTextContent("You're all set")
    expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', 'kas')
    expect(api.patchConfig).toHaveBeenCalledWith('agent.kas_bundle', true)
    fireEvent.click(screen.getByTestId('harness-setup-start'))
    expect(onDone).toHaveBeenCalled()
  })

  it('lists a missing Kiro CLI with its install command, Continue off and Check again', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status() as never)
    renderStep({ installed: false })
    fireEvent.click(await screen.findByRole('radio', { name: /Kiro CLI/ }, WAIT))
    expect((await screen.findAllByText(/cli\.kiro\.dev\/install/, {}, WAIT)).length).toBeGreaterThan(0)
    expect(screen.getByTestId('harness-setup-continue')).toBeDisabled()
    fireEvent.click(screen.getByTestId('harness-setup-check-again'))
    await waitFor(() => expect(api.kiroPrerequisite).toHaveBeenCalledWith('explicit'), WAIT)
  })

  it('shows a failed Check again', async () => {
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new Error('probe failed'))
    renderStep({ installed: false })
    fireEvent.click(await screen.findByRole('radio', { name: /Kiro CLI/ }, WAIT))
    fireEvent.click(await screen.findByTestId('harness-setup-check-again', {}, WAIT))
    expect(await screen.findByTestId('harness-setup-recheck-error', {}, WAIT)).toBeInTheDocument()
  })

  it('saves Kiro CLI on KAS and asks for its own sign-in when it is signed out', async () => {
    backends(kiroInstalled, strands)
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ installed: true, authenticated: true }) as never)
    renderStep({ installed: true, authenticated: false })
    fireEvent.click(await screen.findByRole('radio', { name: /Kiro CLI/ }, WAIT))
    await continueReady()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    expect(await screen.findByRole('heading', { name: 'Sign in to Kiro CLI' }, WAIT)).toBeInTheDocument()
    expect(api.patchConfig).toHaveBeenCalledWith('agent.kas_bundle', false)
    fireEvent.click(screen.getByRole('button', { name: /Check sign-in again/ }))
    await waitFor(() => expect(api.kiroPrerequisite).toHaveBeenCalledWith('explicit'), WAIT)
  })

  it('goes straight to ready, with no extra sign-in, for a signed-in Kiro CLI', async () => {
    backends(kiroInstalled, strands)
    renderStep({ installed: true, authenticated: true })
    fireEvent.click(await screen.findByRole('radio', { name: /Kiro CLI/ }, WAIT))
    await continueReady()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    expect(await screen.findByTestId('harness-setup-ready', {}, WAIT)).toHaveTextContent('Kiro CLI')
  })

  it('saves another harness as itself and says what sign-in is left', async () => {
    const { onDone } = renderStep({ installed: true, authenticated: true })
    fireEvent.click(await screen.findByRole('button', { name: /Other harnesses/ }, WAIT))
    fireEvent.click(await screen.findByRole('radio', { name: /Strands/ }, WAIT))
    await continueReady()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    expect(await screen.findByTestId('harness-setup-own-sign-in', {}, WAIT)).toBeInTheDocument()
    expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', 'strands')
    fireEvent.click(screen.getByTestId('harness-setup-open-settings'))
    expect(onDone).toHaveBeenCalled()
  })

  it('stays on the choice when the save fails', async () => {
    vi.mocked(api.patchConfig).mockRejectedValue(new Error('write failed'))
    renderStep()
    await screen.findByRole('radio', { name: /Built-in harness/ }, WAIT)
    await continueReady()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    expect(await screen.findByTestId('harness-setup-save-error', {}, WAIT)).toBeInTheDocument()
    expect(screen.queryByTestId('harness-setup-ready')).not.toBeInTheDocument()
  })

  it('says when the harness list or the current setup cannot be read', async () => {
    vi.mocked(api.acpBackends).mockRejectedValue(new Error('list failed'))
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new Error('config failed'))
    renderStep()
    expect(await screen.findByTestId('harness-setup-read-error', {}, WAIT)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Other harnesses/ }))
    expect(await screen.findByTestId('harness-setup-backends-error', {}, WAIT)).toBeInTheDocument()
  })

  it('offers no built-in row where policy denies KAS, and saves Kiro CLI as itself', async () => {
    schemaQuery.value = { schema: new Map([['agent.acp_backend', { enum: ['kiro', 'strands'] }]]), error: null }
    backends(kiroInstalled, strands)
    renderStep({ installed: true, authenticated: true })
    fireEvent.click(await screen.findByRole('radio', { name: /Kiro CLI/ }, WAIT))
    expect(screen.queryByRole('radio', { name: /Built-in harness/ })).not.toBeInTheDocument()
    await continueReady()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    await screen.findByTestId('harness-setup-ready', {}, WAIT)
    expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', 'kiro')
    expect(api.patchConfig).not.toHaveBeenCalledWith('agent.kas_bundle', expect.anything())
  })

  it('keeps a harness saved before the first chat picked, and Continue saves it, not kas', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } } as never)
    backends(kiroMissing, strands, probe())
    renderStep({ installed: true, authenticated: true })
    // It lives behind "Other harnesses", which opens on its own to show it.
    expect(await screen.findByRole('radio', { name: /Claude/ }, WAIT)).toBeChecked()
    expect(screen.getByRole('radio', { name: /Built-in harness/ })).not.toBeChecked()
    await continueReady()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    await screen.findByTestId('harness-setup-ready', {}, WAIT)
    expect(vi.mocked(api.patchConfig).mock.calls).toEqual([['agent.acp_backend', 'claude']])
  })

  it('keeps a saved kiro picked after its save, when the config reads back kas', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValueOnce({ agent: { acp_backend: 'kiro' } } as never)
    backends(kiroInstalled, strands)
    const props = { status: status({ installed: true, authenticated: true }), onDone: vi.fn() }
    const { queryClient, rerender } = renderWithProviders(<FirstRunHarnessSetup {...props} />)
    await waitFor(() => expect(screen.getByRole('radio', { name: /Kiro CLI/ })).toBeChecked(), WAIT)
    await continueReady()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    await screen.findByTestId('harness-setup-ready', {}, WAIT)
    expect(api.patchConfig).toHaveBeenCalledWith('agent.kas_bundle', false)
    await waitFor(() => expect(queryClient.getQueryData(['kirocrewConfig'])).toEqual({ agent: { acp_backend: 'kas' } }), WAIT)
    rerender(<FirstRunHarnessSetup {...props} />)
    expect(screen.getByTestId('harness-setup-ready')).toHaveTextContent('Kiro CLI')
  })

  it('keeps a saved kiro picked on Back from its sign-in, when the config reads back kas', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValueOnce({ agent: { acp_backend: 'kiro' } } as never)
    backends(kiroInstalled, strands)
    const props = { status: status({ installed: true, authenticated: false }), onDone: vi.fn() }
    const { queryClient, rerender } = renderWithProviders(<FirstRunHarnessSetup {...props} />)
    await waitFor(() => expect(screen.getByRole('radio', { name: /Kiro CLI/ })).toBeChecked(), WAIT)
    await continueReady()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    await screen.findByRole('heading', { name: 'Sign in to Kiro CLI' }, WAIT)
    await waitFor(() => expect(queryClient.getQueryData(['kirocrewConfig'])).toEqual({ agent: { acp_backend: 'kas' } }), WAIT)
    rerender(<FirstRunHarnessSetup {...props} />)
    fireEvent.click(screen.getByTestId('harness-setup-back'))
    expect(await screen.findByRole('radio', { name: /Kiro CLI/ }, WAIT)).toBeChecked()
    expect(screen.getByRole('radio', { name: /Built-in harness/ })).not.toBeChecked()
  })

  it('keeps the default pick, and saves kas, for a saved harness the step lists no row for', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } } as never)
    renderStep({ installed: true, authenticated: true })
    // Continue turns on only once the list has answered without a Claude row.
    await continueReady()
    expect(screen.getByRole('radio', { name: /Built-in harness/ })).toBeChecked()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    expect(await screen.findByRole('button', { name: 'stub sign-in' }, WAIT)).toBeInTheDocument()
    expect(vi.mocked(api.patchConfig).mock.calls).toEqual([
      ['agent.acp_backend', 'kas'],
      ['agent.kas_bundle', true],
    ])
  })

  it('keeps the default pick for a saved kiro whose CLI is missing', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kiro' } } as never)
    renderStep({ installed: false })
    await continueReady()
    expect(screen.getByRole('radio', { name: /Built-in harness/ })).toBeChecked()
    expect(screen.getByRole('radio', { name: /Kiro CLI/ })).not.toBeChecked()
  })

  it('keeps Continue off until the saved config is read', async () => {
    let answer!: (value: unknown) => void
    vi.mocked(api.kirocrewConfig).mockReturnValue(new Promise(resolve => { answer = resolve }) as never)
    renderStep()
    await screen.findByRole('radio', { name: /Built-in harness/ }, WAIT)
    await waitFor(() => expect(api.acpBackends).toHaveBeenCalled(), WAIT)
    expect(screen.getByTestId('harness-setup-continue')).toBeDisabled()
    await act(async () => {
      answer({ agent: { acp_backend: 'kas' } })
    })
    await continueReady()
  })

  it('keeps Continue off for a saved harness until the list its row comes from is read', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } } as never)
    let answer!: (value: unknown) => void
    vi.mocked(api.acpBackends).mockReturnValue(new Promise(resolve => { answer = resolve }) as never)
    const props = { status: status({ installed: true, authenticated: true }), onDone: vi.fn() }
    const { queryClient, rerender } = renderWithProviders(<FirstRunHarnessSetup {...props} />)
    // The config has answered and rendered, so only the list keeps Continue off.
    await waitFor(() => expect(queryClient.getQueryData(['kirocrewConfig'])).toEqual({ agent: { acp_backend: 'claude' } }), WAIT)
    rerender(<FirstRunHarnessSetup {...props} />)
    await screen.findByRole('radio', { name: /Built-in harness/ }, WAIT)
    expect(screen.getByTestId('harness-setup-continue')).toBeDisabled()
    await act(async () => {
      answer({ backends: [kiroMissing, strands, probe()] })
    })
    expect(await screen.findByRole('radio', { name: /Claude/ }, WAIT)).toBeChecked()
    await continueReady()
  })

  it('says the save is partly done when only agent.kas_bundle fails, and Continue retries both', async () => {
    vi.mocked(api.patchConfig).mockResolvedValueOnce({} as never).mockRejectedValueOnce(new Error('write failed'))
    renderStep()
    await screen.findByRole('radio', { name: /Built-in harness/ }, WAIT)
    await continueReady()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    expect(await screen.findByTestId('harness-setup-save-error', {}, WAIT)).toHaveTextContent('only partly saved')
    await continueReady()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    expect(await screen.findByRole('button', { name: 'stub sign-in' }, WAIT)).toBeInTheDocument()
    expect(vi.mocked(api.patchConfig).mock.calls).toEqual([
      ['agent.acp_backend', 'kas'],
      ['agent.kas_bundle', true],
      ['agent.acp_backend', 'kas'],
      ['agent.kas_bundle', true],
    ])
  })

  // The gateway sends this 503 only on the `agent.acp_backend` write.
  it('counts a save whose only failure is the first-run marker as saved', async () => {
    vi.mocked(api.patchConfig).mockRejectedValueOnce(new ApiError(
      503,
      'Agent selection was saved, but setup completion could not be recorded. Try again.',
      JSON.stringify({ code: 'setup_marker_write_failed', config_saved: true }),
    ))
    renderStep()
    await screen.findByRole('radio', { name: /Built-in harness/ }, WAIT)
    await waitFor(() => expect(api.acpBackends).toHaveBeenCalledTimes(1), WAIT)
    await continueReady()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    expect(await screen.findByRole('button', { name: 'stub sign-in' }, WAIT)).toBeInTheDocument()
    expect(screen.queryByTestId('harness-setup-save-error')).not.toBeInTheDocument()
    expect(api.patchConfig).toHaveBeenCalledWith('agent.kas_bundle', true)
    // The probe reads the stored pick, so it is asked again after the save.
    await waitFor(() => expect(api.acpBackends).toHaveBeenCalledTimes(2), WAIT)
  })

  it('turns Set up later off while the pick is being saved', async () => {
    let finish!: () => void
    vi.mocked(api.patchConfig).mockReturnValue(new Promise(resolve => { finish = () => resolve({} as never) }) as never)
    const { onDone } = renderStep()
    await screen.findByRole('radio', { name: /Built-in harness/ }, WAIT)
    await continueReady()
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    await waitFor(() => expect(screen.getByTestId('harness-setup-later')).toBeDisabled(), WAIT)
    fireEvent.click(screen.getByTestId('harness-setup-later'))
    expect(onDone).not.toHaveBeenCalled()
    finish()
    expect(await screen.findByRole('button', { name: 'stub sign-in' }, WAIT)).toBeInTheDocument()
  })

  it('Set up later admits without saving', async () => {
    const { onDone } = renderStep()
    fireEvent.click(await screen.findByTestId('harness-setup-later', {}, WAIT))
    expect(onDone).toHaveBeenCalled()
    expect(api.patchConfig).not.toHaveBeenCalled()
  })
})
