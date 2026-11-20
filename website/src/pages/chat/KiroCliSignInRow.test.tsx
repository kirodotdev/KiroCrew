import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import KiroCliSignInRow from './KiroCliSignInRow'
import { api } from '../../api/client'

vi.mock('../../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return { ...mod, api: { ...mod.api, kiroPrerequisite: vi.fn() } }
})

const kiroPrerequisite = vi.mocked(api.kiroPrerequisite)

/** One resolved prerequisite read on a fresh cache; the ceiling bounds a loaded worker. */
const READ_TIMEOUT_MS = 5_000

const SIGNED_OUT = {
  platform: 'Linux',
  installed: true,
  authenticated: false,
  login_command: 'kiro-cli login',
  sso_login_command: 'kiro-cli login --use-device-flow --license pro',
  bundled_cli: false,
}

describe('KiroCliSignInRow', () => {
  beforeEach(() => kiroPrerequisite.mockReset())

  it('shows the two labelled commands the gateway serves', async () => {
    kiroPrerequisite.mockResolvedValue(SIGNED_OUT as never)
    renderWithProviders(<KiroCliSignInRow fallback="fallback text" />)
    // The prerequisite read answers, then the commands render.
    expect(await screen.findByText(SIGNED_OUT.sso_login_command, {}, { timeout: READ_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.getByText(SIGNED_OUT.login_command)).toBeInTheDocument()
    expect(screen.getByText(/Personal account/)).toBeInTheDocument()
    expect(screen.getByText(/Organization SSO/)).toBeInTheDocument()
  })

  it('keeps the row text and Check sign-in again when the status still reads signed in', async () => {
    kiroPrerequisite.mockResolvedValue({ ...SIGNED_OUT, authenticated: true } as never)
    renderWithProviders(<KiroCliSignInRow fallback="fallback text" />)
    // A mid-session expiry can leave the status stale: the failed turn still shows.
    // The status answers signed in: the row keeps its text and the recheck.
    expect(await screen.findByRole('button', { name: /Check sign-in again/ }, { timeout: READ_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.getByText('fallback text')).toBeInTheDocument()
    expect(screen.queryByText(SIGNED_OUT.login_command)).toBeNull()
  })

  it('on the relay it offers the commands and the Sign in button, and keeps the button once kiro-cli is signed in', async () => {
    kiroPrerequisite.mockResolvedValue(SIGNED_OUT as never)
    const { unmount } = renderWithProviders(<KiroCliSignInRow fallback="fallback text" crewSignIn />)
    // The prerequisite read answers signed out: both ways in.
    expect(await screen.findByText(SIGNED_OUT.sso_login_command, {}, { timeout: READ_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Check sign-in again/ })).toBeInTheDocument()
    // One primary: Check sign-in again is filled, Sign in is the outlined fallback.
    expect(screen.getByTestId('kiro-sign-in-open')).not.toHaveClass('bg-accent')
    expect(screen.getByRole('button', { name: /Check sign-in again/ })).toHaveClass('bg-accent')
    unmount()
    kiroPrerequisite.mockResolvedValue({ ...SIGNED_OUT, authenticated: true } as never)
    renderWithProviders(<KiroCliSignInRow fallback="fallback text" crewSignIn />)
    // kiro-cli reads signed in (maybe stale): the row text, the recheck and Sign in.
    expect(await screen.findByRole('button', { name: /Check sign-in again/ }, { timeout: READ_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.getByText('fallback text')).toBeInTheDocument()
    expect(screen.getByTestId('kiro-sign-in-open')).toBeInTheDocument()
    expect(screen.queryByText(SIGNED_OUT.login_command)).toBeNull()
  })

  it('keeps the row text when the status cannot name the commands (a viewer sees not installed)', async () => {
    kiroPrerequisite.mockResolvedValue({ ...SIGNED_OUT, installed: false } as never)
    renderWithProviders(<KiroCliSignInRow fallback="fallback text" />)
    expect(await screen.findByText('fallback text', {}, { timeout: READ_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.queryByText(SIGNED_OUT.login_command)).toBeNull()
  })

  it('Check again probes the host, and the row stays as the failed turn\'s record', async () => {
    kiroPrerequisite.mockResolvedValueOnce(SIGNED_OUT as never)
    renderWithProviders(<KiroCliSignInRow fallback="fallback text" />)
    // The first read answers signed out.
    const button = await screen.findByRole('button', { name: /Check sign-in again/ }, { timeout: READ_TIMEOUT_MS })
    kiroPrerequisite.mockResolvedValue({ ...SIGNED_OUT, authenticated: true } as never)
    fireEvent.click(button)
    // The explicit re-read answers signed in: the commands go, the row's text stays.
    await waitFor(() => expect(kiroPrerequisite).toHaveBeenLastCalledWith('explicit'), { timeout: READ_TIMEOUT_MS })
    expect(await screen.findByText('fallback text', {}, { timeout: READ_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.queryByText(SIGNED_OUT.login_command)).toBeNull()
  })
})
