/**
 * The first-run harness step (`FirstRunHarnessSetup`) on its own: what each
 * pick saves, which screen follows, and how failed reads and saves show.
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AcpBackendProbe } from '../api/client'
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
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    fireEvent.click(await screen.findByRole('button', { name: 'stub sign-in' }, WAIT))
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
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    expect(await screen.findByTestId('harness-setup-ready', {}, WAIT)).toHaveTextContent('Kiro CLI')
  })

  it('saves another harness as itself and says what sign-in is left', async () => {
    const { onDone } = renderStep({ installed: true, authenticated: true })
    fireEvent.click(await screen.findByRole('button', { name: /Other harnesses/ }, WAIT))
    fireEvent.click(await screen.findByRole('radio', { name: /Strands/ }, WAIT))
    await waitFor(() => expect(screen.getByTestId('harness-setup-continue')).toBeEnabled(), WAIT)
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
    fireEvent.click(screen.getByTestId('harness-setup-continue'))
    await screen.findByTestId('harness-setup-ready', {}, WAIT)
    expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', 'kiro')
    expect(api.patchConfig).not.toHaveBeenCalledWith('agent.kas_bundle', expect.anything())
  })

  it('Set up later admits without saving', async () => {
    const { onDone } = renderStep()
    fireEvent.click(await screen.findByTestId('harness-setup-later', {}, WAIT))
    expect(onDone).toHaveBeenCalled()
    expect(api.patchConfig).not.toHaveBeenCalled()
  })
})
