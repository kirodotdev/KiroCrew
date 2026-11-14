import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import KiroSignInPrompt from './KiroSignInPrompt'
import { api } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kirocrewConfig: vi.fn(),
      kasBundleStatus: vi.fn(),
      kasLoginStatus: vi.fn(),
    },
  }
})

const kirocrewConfig = vi.mocked(api.kirocrewConfig)
const kasBundleStatus = vi.mocked(api.kasBundleStatus)
const kasLoginStatus = vi.mocked(api.kasLoginStatus)

/**
 * Ceiling for a wait behind the prompt's chained reads: config, then (enabled
 * by its answer) the engine status, then (enabled by that) the sign-in status
 * the embedded flow reads. Three resolved mocks on a fresh query cache; the
 * ceiling only bounds a loaded worker.
 */
const CHAIN_TIMEOUT_MS = 5_000

const SIGNED_OUT = { authenticated: false, provider: null, identity: null, transport: 'device' }

function setup({ backend = 'kas', needs = true, signedIn = false } = {}) {
  kirocrewConfig.mockResolvedValue({ agent: { acp_backend: backend } } as never)
  kasBundleStatus.mockResolvedValue({ chat_needs_sign_in: needs } as never)
  kasLoginStatus.mockResolvedValue(
    (signedIn ? { ...SIGNED_OUT, authenticated: true, provider: 'Google', identity: 'social' } : SIGNED_OUT) as never,
  )
}

describe('KiroSignInPrompt', () => {
  beforeEach(() => {
    kirocrewConfig.mockReset()
    kasBundleStatus.mockReset()
    kasLoginStatus.mockReset()
  })

  it('offers the sign-in choices when the gateway says a new chat needs it', async () => {
    setup()
    renderWithProviders(<KiroSignInPrompt />)
    // Config -> engine status: the prompt's frame.
    expect(await screen.findByTestId('kiro-sign-in-prompt', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    // Config -> engine status -> sign-in status: the flow's chooser inside it.
    expect(await screen.findByTestId('kas-login-card-intro', {}, { timeout: CHAIN_TIMEOUT_MS })).toHaveTextContent(
      'Your chats run on your Kiro account. Choose how to sign in.',
    )
  })

  it.each([
    ['the gateway says no', { needs: false }],
    ['another harness is selected', { backend: 'claude' }],
  ])('shows nothing when %s', async (_label, opts) => {
    setup(opts)
    renderWithProviders(<KiroSignInPrompt />)
    // The last read each case makes: the engine status on KAS, the config otherwise.
    const last = opts.backend === undefined ? kasBundleStatus : kirocrewConfig
    await waitFor(() => expect(last).toHaveBeenCalled(), { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.queryByTestId('kiro-sign-in-prompt')).toBeNull()
  })

  it('goes away once the stored sign-in reads as signed in', async () => {
    setup({ signedIn: true })
    renderWithProviders(<KiroSignInPrompt />)
    // Config -> engine status -> sign-in status, which answers signed in.
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalled(), { timeout: CHAIN_TIMEOUT_MS })
    await waitFor(() => expect(screen.queryByTestId('kiro-sign-in-prompt')).toBeNull(), { timeout: CHAIN_TIMEOUT_MS })
  })
})
