import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import KasLoginGate from './KasLoginGate'
import KasProfileChoice from './KasProfileChoice'
import { api, type KasLoginPollResult } from '../api/client'
import { ApiError } from '../api/apiError'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasLoginStatus: vi.fn(),
      kasLoginBeginDevice: vi.fn(),
      kasLoginPoll: vi.fn(),
      kasLoginCancel: vi.fn().mockResolvedValue({ ok: true }),
      sessionsUsageRefresh: vi.fn().mockRejectedValue(new Error('no refresh in this test')),
    },
  }
})

const kasLoginStatus = vi.mocked(api.kasLoginStatus)
const kasLoginBeginDevice = vi.mocked(api.kasLoginBeginDevice)
const kasLoginPoll = vi.mocked(api.kasLoginPoll)
const sessionsUsageRefresh = vi.mocked(api.sessionsUsageRefresh)

const A = 'arn:aws:codewhisperer:us-east-1:111122223333:profile/AAAAAAAAAAAA'
const B = 'arn:aws:codewhisperer:us-east-1:111122223333:profile/BBBBBBBBBBBB'
const C = 'arn:aws:codewhisperer:eu-central-1:111122223333:profile/CCCCCCCCCCCC'
const PROFILES = [
  { arn: A, name: 'KiroProfile-us-east-1', region: 'us-east-1' },
  { arn: B, name: 'Team-B', region: 'us-east-1' },
  { arn: C, name: 'KiroProfile-eu-central-1', region: 'eu-central-1' },
]
const CHOOSE: KasLoginPollResult = { status: 'choose_profile', profiles: PROFILES, selected: A }
const SIGNED_OUT = { authenticated: false, provider: null, identity: null, transport: 'device' } as never
const SIGNED_IN = {
  authenticated: true,
  provider: 'Enterprise',
  identity: 'identity_center',
  transport: 'device',
} as never

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (err: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

/** Start a company sign-in whose first poll answers `choose_profile`; picks go to *pick*. */
async function startChoice(pick: (arn: string) => Promise<KasLoginPollResult>) {
  kasLoginPoll.mockImplementation((_id: string, arn?: string) => (arn ? pick(arn) : Promise.resolve(CHOOSE)))
  const view = renderWithProviders(
    <KasLoginGate>
      <p>the app</p>
    </KasLoginGate>,
  )
  fireEvent.click(await screen.findByRole('button', { name: 'Continue with company SSO' }))
  fireEvent.change(await screen.findByLabelText('Company sign-in URL'), {
    target: { value: 'https://example-co.awsapps.com/start' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByTestId('kas-profile-choice')
  return view
}

describe('KasProfileChoice', () => {
  beforeEach(() => {
    kasLoginStatus.mockReset()
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    kasLoginBeginDevice.mockReset()
    kasLoginBeginDevice.mockResolvedValue({
      login_id: 'login-1',
      user_code: 'ABCD-EFGH',
      verification_uri_complete: 'https://device.sso.example.invalid/?user_code=ABCD-EFGH',
      expires_at: '2099-01-01T00:00:00Z',
    })
    kasLoginPoll.mockReset()
    sessionsUsageRefresh.mockClear()
  })

  it('renders one row per profile with its name and region and preselects `selected`', () => {
    render(
      <KasProfileChoice profiles={PROFILES} selected={B} busy={false} notice="" askAgent={false} onContinue={() => {}} onCancel={() => {}} />,
    )
    const group = screen.getByRole('radiogroup', { name: 'Choose a Kiro profile' })
    const rows = within(group).getAllByRole('radio')
    expect(rows).toHaveLength(3)
    expect(rows.map(r => r.closest('label')?.textContent)).toEqual([
      'KiroProfile-us-east-1us-east-1',
      'Team-Bus-east-1',
      'KiroProfile-eu-central-1eu-central-1',
    ])
    expect(screen.getByRole('radio', { name: /Team-B/ })).toBeChecked()
    expect(screen.queryByRole('heading')).toBeNull()
  })

  it('one Continue sends the pick through the poll and lands the sign-in', async () => {
    const pick = deferred<KasLoginPollResult>()
    await startChoice(() => pick.promise)
    expect(screen.getByRole('radio', { name: /KiroProfile-us-east-1/ })).toBeChecked()
    fireEvent.click(screen.getByRole('radio', { name: /KiroProfile-eu-central-1/ }))
    const next = screen.getByRole('button', { name: 'Continue' })
    fireEvent.click(next)
    expect(kasLoginPoll).toHaveBeenLastCalledWith('login-1', C)
    // In flight: Continue cannot send the pick a second time.
    await waitFor(() => expect(next).toBeDisabled())
    fireEvent.click(next)
    expect(kasLoginPoll.mock.calls.filter(([, arn]) => arn)).toHaveLength(1)

    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    pick.resolve({ status: 'authorized', provider: 'Enterprise' })
    expect(await screen.findByText('the app')).toBeInTheDocument()
    // The signed-in after-effects ran: the credit reading is asked for afresh.
    await waitFor(() => expect(sessionsUsageRefresh).toHaveBeenCalled())
  })

  it('a refetch before Continue sends no profile', async () => {
    const { queryClient } = await startChoice(() => Promise.reject(new Error('no pick expected')))
    const before = kasLoginPoll.mock.calls.length
    await queryClient.refetchQueries({ queryKey: ['kas-login-poll'] })
    expect(kasLoginPoll.mock.calls.length).toBeGreaterThan(before)
    expect(kasLoginPoll.mock.calls.every(([, arn]) => arn === undefined)).toBe(true)
    expect(screen.getByTestId('kas-profile-choice')).toBeInTheDocument()
  })

  it.each([
    [404, 'unknown_login_id', 'No pending login with that id.'],
    [409, 'signed_out_during_login', 'Signed out while the sign-in was pending.'],
  ])('a %i after Continue reaches the problem screen', async (status, code, detail) => {
    await startChoice(() => Promise.reject(new ApiError(status, detail, JSON.stringify({ error: detail, code }))))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Sign-in failed')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start over' })).toBeInTheDocument()
    expect(screen.queryByTestId('kas-profile-choice')).toBeNull()
  })
})
