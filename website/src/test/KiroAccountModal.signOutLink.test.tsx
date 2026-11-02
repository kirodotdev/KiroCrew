/**
 * The Kiro account popup (the credits pill's modal) links to Sign out and Switch
 * account. Those actions live on the Kiro sign-in card in Settings > Agent
 * Harness; this popup is where people look for them, so it must lead there --
 * but only on the KAS harness, where that card's sign-in is the account the
 * popup names.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import KiroAccountModal, { type KiroAccountUsage } from '../components/KiroAccountModal'
import { api } from '../api/client'
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
    ['sign-in expired', 'signin-required'],
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
