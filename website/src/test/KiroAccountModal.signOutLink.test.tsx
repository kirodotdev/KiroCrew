/**
 * The Kiro account popup (the credits pill's modal) links to Sign out and Switch
 * account. Those actions live on the Kiro sign-in card in Settings > Agent
 * Harness; this popup is where people look for them, so it must lead there --
 * but only on the KAS harness, where that card's sign-in is the account the
 * popup names.
 */
import { useQuery } from '@tanstack/react-query'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import KiroAccountModal, { type KiroAccountUsage } from '../components/KiroAccountModal'
import { api } from '../api/client'
import type { AcpBackendConfig } from '../api/acpBackend'
import { KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import { renderWithProviders } from './helpers'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, kirocrewConfig: vi.fn(), kasLoginStatus: vi.fn() } }
})
const configMock = vi.mocked(api.kirocrewConfig)
const signInMock = vi.mocked(api.kasLoginStatus)
const crewSignIn = (authenticated: boolean) =>
  signInMock.mockResolvedValue({ authenticated, provider: authenticated ? 'Google' : '', identity: authenticated ? 'social' : '' } as never)
const harness = (acp_backend: string) => configMock.mockResolvedValue({ agent: { acp_backend } } as never)

function LocationProbe() {
  const { pathname, search } = useLocation()
  return <div data-testid="loc">{pathname + search}</div>
}

const READING: KiroAccountUsage = {
  used: 10,
  limit: 100,
  overage: 0,
  bonusCredits: [],
  stale: false,
  email: 'owner@example.com',
  accountType: 'SocialGoogle',
}

const LINK = { name: 'Sign out or switch account in Settings' }
const SWITCH_LINK = { name: 'Switch account in Settings' }
/** Waits on the link or its notice. The link renders only after a query chain
 *  settles: the `kirocrewConfig` read, then (on KAS) the `kas-login` read that
 *  picks the label. On a loaded worker that chain can outlast the default 1 s. */
const QUERY_CHAIN = { timeout: 5_000 }

function renderModal(usage: KiroAccountUsage, onClose = vi.fn()) {
  renderWithProviders(
    <>
      <KiroAccountModal open onClose={onClose} usage={usage} />
      <LocationProbe />
    </>,
    { route: '/chat' },
  )
  return onClose
}

beforeEach(() => {
  configMock.mockReset()
  signInMock.mockReset()
  harness('kas')
  crewSignIn(true)
})

describe('KiroAccountModal sign-out link', () => {
  it.each<[string, KiroAccountUsage]>([
    ['signed in with a reading', READING],
    ['still loading', null],
    ['no reading yet', 'none'],
  ])('offers the link on the KAS harness when %s', async (_label, usage) => {
    renderModal(usage)
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    expect(await within(dialog).findByRole('button', LINK, QUERY_CHAIN)).toBeInTheDocument()
  })

  it('treats an empty harness as the KAS default', async () => {
    harness('')
    renderModal(READING)
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    expect(await within(dialog).findByRole('button', LINK, QUERY_CHAIN)).toBeInTheDocument()
  })

  it('closes the popup and opens the sign-in card in Agent Harness', async () => {
    const onClose = renderModal(READING)
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    fireEvent.click(await within(dialog).findByRole('button', LINK, QUERY_CHAIN))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('loc').textContent).toBe(KIRO_SIGN_IN_PATH)
  })

  it.each(['kiro', 'claude'])(
    'hides the link on the %s harness, whose account that card does not sign out',
    async backend => {
      harness(backend)
      renderModal(READING)
      const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
      await waitFor(() => expect(configMock).toHaveBeenCalled(), QUERY_CHAIN)
      // Let the resolved read render before asserting the absence.
      await screen.findByText('owner@example.com')
      await waitFor(() => expect(within(dialog).queryByTestId('kiro-account-sign-in-settings')).toBeNull(), QUERY_CHAIN)
      // The external account page stays.
      expect(within(dialog).getByRole('link', { name: /Manage account/ })).toBeInTheDocument()
    },
  )

  it.each([true, false])(
    'on a Kiro CLI harness whose Bots run on KAS, links to the Bots\' sign-in (Crew sign-in held: %s)',
    async held => {
      // The popup names kiro-cli's login, which the card does not sign out, so the
      // label names the sign-in the Bots run as, whatever the card holds.
      configMock.mockResolvedValue({ agent: { acp_backend: 'kiro' }, kas_in_use: true } as never)
      crewSignIn(held)
      const onClose = renderModal(READING)
      const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
      const link = await within(dialog).findByRole('button', { name: 'Kiro sign-in for bots in Settings' }, QUERY_CHAIN)
      expect(within(dialog).queryByRole('button', LINK)).toBeNull()
      expect(within(dialog).queryByRole('button', SWITCH_LINK)).toBeNull()
      fireEvent.click(link)
      expect(onClose).toHaveBeenCalledTimes(1)
      expect(screen.getByTestId('loc').textContent).toBe(KIRO_SIGN_IN_PATH)
    },
  )

  it('hides the link on a KAS-named harness the gateway says nothing runs on', async () => {
    configMock.mockResolvedValue({ agent: { acp_backend: 'kas' }, kas_in_use: false } as never)
    renderModal(READING)
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    await screen.findByText('owner@example.com')
    await waitFor(() => expect(configMock).toHaveBeenCalled(), QUERY_CHAIN)
    await waitFor(() => expect(within(dialog).queryByTestId('kiro-account-sign-in-settings')).toBeNull(), QUERY_CHAIN)
  })

  it('says so when the config read fails, instead of dropping the link silently', async () => {
    configMock.mockRejectedValue(new Error('config store unreadable'))
    renderModal(READING)
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    expect(await within(dialog).findByTestId('kiro-account-sign-in-settings-error', {}, QUERY_CHAIN)).toHaveTextContent(
      'Could not load settings, so the sign-out link can’t be shown.',
    )
    expect(within(dialog).queryByTestId('kiro-account-sign-in-settings')).toBeNull()
  })

  it('labels the link Sign out or switch when Crew\'s own sign-in holds an account', async () => {
    crewSignIn(true)
    renderModal(READING)
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    expect(await within(dialog).findByRole('button', LINK, QUERY_CHAIN)).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', SWITCH_LINK)).toBeNull()
  })

  it('labels it Switch account only when Crew\'s own sign-in is empty, and still lands on the card', async () => {
    // KAS relay with no Crew sign-in: the popup names kiro-cli's login, which the
    // card cannot sign out, but signing in there switches the account agents use.
    crewSignIn(false)
    const onClose = renderModal(READING)
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    const link = await within(dialog).findByRole('button', SWITCH_LINK, QUERY_CHAIN)
    expect(within(dialog).queryByRole('button', LINK)).toBeNull()
    fireEvent.click(link)
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('loc').textContent).toBe(KIRO_SIGN_IN_PATH)
  })

  it('labels it Switch account while Crew\'s sign-in read fails, promising no sign-out', async () => {
    signInMock.mockRejectedValue(new Error('vault unreadable'))
    renderModal(READING)
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    expect(await within(dialog).findByRole('button', SWITCH_LINK, QUERY_CHAIN)).toBeInTheDocument()
  })

  it.each([
    ['refuses', false],
    ['allows', true],
  ])('asks the page leave guard first; when it %s the leave, the popup and route follow it', async (_label, allowed) => {
    // An unsaved draft on the page under the popup (an artifact edit, say)
    // registers this guard; the link must not discard it without asking.
    const guard = vi.fn(() => allowed)
    function Draft() {
      useRegisterNavigationLeaveGuard(guard)
      return null
    }
    const onClose = vi.fn()
    renderWithProviders(
      <NavigationLeaveGuardProvider>
        <Draft />
        <KiroAccountModal open onClose={onClose} usage={READING} />
        <LocationProbe />
      </NavigationLeaveGuardProvider>,
      { route: '/artifacts/notes' },
    )
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    fireEvent.click(await within(dialog).findByRole('button', LINK, QUERY_CHAIN))
    expect(guard).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(allowed ? 1 : 0)
    expect(screen.getByTestId('loc').textContent).toBe(allowed ? KIRO_SIGN_IN_PATH : '/artifacts/notes')
  })

  it('does not read Crew\'s sign-in on a harness that shows no link', async () => {
    harness('kiro')
    renderModal(READING)
    await screen.findByText('owner@example.com')
    await waitFor(() => expect(configMock).toHaveBeenCalled(), QUERY_CHAIN)
    expect(signInMock).not.toHaveBeenCalled()
  })
})

describe('KiroAccountModal with no usable Kiro sign-in', () => {
  // A KAS user who has not signed in yet (#32060): KAS agents run as Crew's own
  // Kiro sign-in, so the popup leads there and never to kiro-cli, and a state
  // in which nothing failed is not dressed as an error.
  const SIGN_IN_LINK = { name: 'Sign in to Kiro in Settings' }

  /** Shows the harness once the config read lands, in the same render as the modal. */
  function HarnessProbe() {
    const { data } = useQuery<AcpBackendConfig>({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig(), refetchOnMount: false })
    return <div data-testid="harness">{data?.agent?.acp_backend ?? ''}</div>
  }

  it('offers the Kiro sign-in on KAS, without kiro-cli or an alert', async () => {
    crewSignIn(false)
    const onClose = renderModal('signin-required')
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    const link = await within(dialog).findByRole('button', SIGN_IN_LINK, QUERY_CHAIN)
    // The notice waits on Crew's sign-in read, which lands after the link's config read.
    await within(dialog).findByText('No active Kiro sign-in. Sign in to see your account and credits.', {}, QUERY_CHAIN)
    expect(within(dialog).queryByRole('alert')).toBeNull()
    expect(within(dialog).queryByRole('button', SWITCH_LINK)).toBeNull()
    expect(within(dialog).queryByRole('button', { name: /^Refresh$/ })).toBeNull()
    expect(dialog.textContent).not.toMatch(/kiro-cli|usage API|again/)
    fireEvent.click(link)
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('loc').textContent).toBe(KIRO_SIGN_IN_PATH)
  })

  it('reports a stored KAS sign-in the read could not use as a failure, still without kiro-cli', async () => {
    // An expired or refused sign-in failed, so it takes the error surface; its
    // hand-off is withheld because the agent runs on that same sign-in.
    crewSignIn(true)
    const onClose = renderModal('signin-required')
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    // Both reads (the harness, then Crew's sign-in) must land before this state shows.
    const LAPSED = 'Your Kiro sign-in is no longer valid, so your account and credits can’t be shown. Sign in again to see them.'
    await within(dialog).findByText(LAPSED, {}, QUERY_CHAIN)
    expect(within(dialog).getByRole('alert')).toHaveTextContent(LAPSED)
    expect(within(dialog).queryByRole('button', { name: /Ask the agent/i })).toBeNull()
    expect(dialog.textContent).not.toMatch(/kiro-cli|usage API/)
    fireEvent.click(within(dialog).getByRole('button', SIGN_IN_LINK))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('loc').textContent).toBe(KIRO_SIGN_IN_PATH)
  })

  it('says the KAS sign-in could not be checked when its read fails, not that there is none', async () => {
    signInMock.mockRejectedValue(new Error('token store unreadable'))
    renderModal('signin-required')
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    // Waits on this notice's own text: before the config read lands the popup shows the kiro-cli alert.
    await within(dialog).findByText('We could not check sign-in status', {}, QUERY_CHAIN)
    expect(within(dialog).getByRole('alert')).toHaveTextContent('We could not check sign-in status')
    expect(within(dialog).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(within(dialog).queryByText(/No active Kiro sign-in/)).toBeNull()
    expect(dialog.textContent).not.toMatch(/kiro-cli/)
  })

  it.each<[string, AcpBackendConfig]>([
    ['kiro', { agent: { acp_backend: 'kiro' } }],
    ['claude', { agent: { acp_backend: 'claude' } }],
    // Bots on KAS do not make the popup's account Crew's sign-in: it still reads
    // the main harness's kiro-cli login.
    ['kiro, Bots on KAS,', { agent: { acp_backend: 'kiro' }, kas_in_use: true }],
  ])('keeps the kiro-cli sentence on the %s harness', async (_label, cfg) => {
    const backend = cfg.agent?.acp_backend
    configMock.mockResolvedValue(cfg as never)
    renderWithProviders(
      <>
        <KiroAccountModal open onClose={vi.fn()} usage="signin-required" />
        <HarnessProbe />
      </>,
    )
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    await waitFor(() => expect(screen.getByTestId('harness').textContent).toBe(backend), QUERY_CHAIN)
    expect(within(dialog).getByRole('alert')).toHaveTextContent('Sign in to Kiro again — for example by running kiro-cli login')
    expect(within(dialog).queryByRole('button', SIGN_IN_LINK)).toBeNull()
  })
})
