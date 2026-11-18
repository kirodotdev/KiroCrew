import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { KiroAccountStatusLine } from '../pages/developer/KiroAccountStatusLine'
import { KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'
import { api, type KasLoginStatus } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, kasLoginStatus: vi.fn(), kasLoginSources: vi.fn(async () => ({ kiro_crew: { stored: false }, kiro_cli: { stored: false } })) } }
})

const kasLoginStatus = vi.mocked(api.kasLoginStatus)
const kasLoginSources = vi.mocked(api.kasLoginSources)
const SIGNED_IN: KasLoginStatus = {
  authenticated: true, provider: 'Google', identity: 'social', transport: 'device',
  expires_at: '2099-01-01T00:00:00Z', expired: false, has_refresh_token: true, refresh_rejected: false, usable: true,
}

afterEach(() => {
  cleanup()
  kasLoginStatus.mockReset()
  kasLoginSources.mockReset()
  kasLoginSources.mockResolvedValue({ kiro_crew: { stored: false }, kiro_cli: { stored: false } } as unknown as Awaited<ReturnType<typeof api.kasLoginSources>>)
})

describe('KiroAccountStatusLine', () => {
  it('says Not signed in, with the way to the Kiro account tab', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, authenticated: false, provider: '', identity: '' })
    renderWithProviders(<KiroAccountStatusLine />)
    await waitFor(() => expect(screen.getByTestId('kiro-account-status')).toHaveTextContent('Not signed in'))
    expect(screen.getByRole('link', { name: 'Go to Kiro account' })).toHaveAttribute('href', KIRO_SIGN_IN_PATH)
  })

  it('says the kiro-cli sign-in is in use, not Not signed in, when KAS runs on it', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, authenticated: false, provider: '', identity: '' })
    kasLoginSources.mockResolvedValue({ active: 'kiro_cli', kiro_crew: { stored: false }, kiro_cli: { stored: true } } as unknown as Awaited<ReturnType<typeof api.kasLoginSources>>)
    renderWithProviders(<KiroAccountStatusLine />)
    await waitFor(() => expect(screen.getByTestId('kiro-account-status')).toHaveTextContent('Using your Kiro CLI sign-in'))
    expect(screen.queryByText('Not signed in')).toBeNull()
  })

  it('says a sign-in store could not be read, instead of Not signed in', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, authenticated: false, provider: '', identity: '' })
    kasLoginSources.mockResolvedValue({
      active: 'unknown', kiro_crew: { stored: false }, kiro_cli: { stored: null },
    } as unknown as Awaited<ReturnType<typeof api.kasLoginSources>>)
    renderWithProviders(<KiroAccountStatusLine />)
    expect(await screen.findByTestId('kiro-sign-in-kiro-cli-unreadable')).toBeInTheDocument()
    expect(screen.queryByText('Not signed in')).toBeNull()
  })

  it('says the sources read failed, instead of Not signed in', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, authenticated: false, provider: '', identity: '' })
    kasLoginSources.mockRejectedValue(new Error('sign-in sources could not be read'))
    renderWithProviders(<KiroAccountStatusLine />)
    expect(await screen.findByTestId('kiro-sign-in-sources-error', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.queryByText('Not signed in')).toBeNull()
  })

  it('names the account when the KAS harness reading names it', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    const { queryClient } = renderWithProviders(<KiroAccountStatusLine />)
    queryClient.setQueryData(['kirocrewConfig'], { agent: { acp_backend: 'kas' } })
    queryClient.setQueryData(['kiro-usage'], { account: 'Ada Lovelace', used: 1, limit: 2 })
    await waitFor(() => expect(screen.getByTestId('kiro-account-status')).toHaveTextContent('Signed in as Ada Lovelace'))
  })

  it('names only the provider off KAS, never the kiro-cli account the reading names there', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    const { queryClient } = renderWithProviders(<KiroAccountStatusLine />)
    queryClient.setQueryData(['kirocrewConfig'], { agent: { acp_backend: 'kiro' } })
    queryClient.setQueryData(['kiro-usage'], { account: 'Someone Else', used: 1, limit: 2 })
    await waitFor(() => expect(screen.getByTestId('kiro-account-status')).toHaveTextContent('Signed in with Google'))
    expect(screen.queryByText(/Someone Else/)).toBeNull()
  })
})
