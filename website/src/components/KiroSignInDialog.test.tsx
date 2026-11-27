import { fireEvent, screen, within } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { KiroSignInDialog, KiroSignInStrip } from './KiroSignInDialog'
import { copyCode } from '../utils/clipboard'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasLoginStatus: vi.fn().mockResolvedValue({
        authenticated: false, provider: '', identity: '', transport: 'device', expires_at: null,
        expired: false, has_refresh_token: false, refresh_rejected: false, usable: false,
      }),
      kasLoginBeginDevice: vi.fn().mockResolvedValue({
        login_id: 'login-1',
        user_code: 'ABCD-EFGH',
        verification_uri_complete: 'https://example.invalid/device?user_code=ABCD-EFGH',
        expires_at: '2099-01-01T00:00:00Z',
      }),
      kasLoginBeginLoopback: vi.fn(),
      kasLoginPoll: vi.fn().mockResolvedValue({ status: 'pending' }),
      kasLoginCancel: vi.fn().mockResolvedValue({ ok: true }),
    },
  }
})

vi.mock('../utils/clipboard', async importOriginal => {
  const mod = await importOriginal<typeof import('../utils/clipboard')>()
  return { ...mod, copyCode: vi.fn() }
})

const copy = vi.mocked(copyCode)
const ID = '69c7830a-1b2c-4d5e'

describe('KiroSignInStrip request id copy', () => {
  beforeEach(() => copy.mockReset())

  it('a copy that fails says so and shows the id to select by hand', async () => {
    copy.mockResolvedValue(false)
    renderWithProviders(<KiroSignInStrip title="Sign in to Kiro to keep chatting" requestId={ID} />)
    fireEvent.click(screen.getByTestId('kiro-sign-in-copy-request-id'))
    // The copy answered false: the failure and the id appear.
    expect(await screen.findByTestId('kiro-sign-in-copy-request-id-failed')).toHaveTextContent(ID)
  })

  it('a copy that works shows no failure', async () => {
    copy.mockResolvedValue(true)
    renderWithProviders(<KiroSignInStrip title="Sign in to Kiro to keep chatting" requestId={ID} />)
    fireEvent.click(screen.getByTestId('kiro-sign-in-copy-request-id'))
    // The copy answered true.
    await vi.waitFor(() => expect(copy).toHaveBeenCalledWith(ID))
    expect(screen.queryByTestId('kiro-sign-in-copy-request-id-failed')).toBeNull()
  })
})

describe('KiroSignInDialog code step', () => {
  it('keeps the screen\'s own heading and hint under the dialog\'s title, with the back arrow in the header', async () => {
    renderWithProviders(<KiroSignInDialog open onOpenChange={() => {}} />)
    const dialog = await screen.findByTestId('kiro-sign-in-dialog')
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Continue with GitHub' }))
    expect(await within(dialog).findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    // The settings page draws this heading itself; the dialog does not, so the screen keeps it.
    expect(within(dialog).getByRole('heading', { name: 'Finish in your browser' })).toBeInTheDocument()
    expect(within(dialog).getByTestId('kas-login-code-heading')).toHaveTextContent('Check the page shows this code')
    // Copy link under the button; the code itself has no copy control.
    expect(within(dialog).getByRole('button', { name: 'Copy link' })).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Copy code' })).toBeNull()
    // The way back sits in the header, not as the screen's own text button.
    expect(within(dialog).queryByRole('button', { name: 'Use another way' })).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Use a different sign-in' }))
    expect(await within(dialog).findByRole('button', { name: 'Continue with Google' })).toBeInTheDocument()
  })
})
