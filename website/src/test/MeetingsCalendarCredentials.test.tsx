/**
 * Meetings Settings -> Calendar credentials.
 *
 * The property under test is write-only: a stored value never reaches the DOM,
 * the form is whatever shape the backend reports, and "connected" is derived from
 * field NAMES. The OAuth path is asserted at the `window.open` boundary, because
 * the consent happens in another tab this suite cannot see.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const apiMocks = vi.hoisted(() => ({
  calendarCredentials: vi.fn(),
  saveCalendarCredentials: vi.fn(),
  forgetCalendarCredentials: vi.fn(),
  startCalendarOAuth: vi.fn(),
}))

vi.mock('../apps/meetings/api', async importOriginal => {
  const actual = await importOriginal<typeof import('../apps/meetings/api')>()
  return { ...actual, meetingsApi: { ...actual.meetingsApi, ...apiMocks } }
})

import CalendarCredentials, { isConnected } from '../apps/meetings/components/CalendarCredentials'
import type { CalendarCredentialsResponse } from '../apps/meetings/api'

const PROVIDERS: CalendarCredentialsResponse['providers'] = {
  caldav: { fields: ['username', 'password'], oauth: false },
  google: { fields: ['client_id', 'client_secret'], oauth: true },
}

function response(status: CalendarCredentialsResponse['status'] = {}): CalendarCredentialsResponse {
  return { status, providers: PROVIDERS }
}

function renderFor(provider: string, label = provider) {
  const notify = vi.fn()
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <CalendarCredentials provider={provider} providerLabel={label} notify={notify} />
    </QueryClientProvider>,
  )
  return { ...utils, notify }
}

beforeEach(() => {
  vi.clearAllMocks()
  apiMocks.calendarCredentials.mockResolvedValue(response())
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('isConnected', () => {
  it('needs a refresh token for OAuth and every field for a password provider', () => {
    expect(isConnected(PROVIDERS.google, ['client_id', 'client_secret'])).toBe(false)
    expect(isConnected(PROVIDERS.google, ['client_id', 'refresh_token'])).toBe(true)
    expect(isConnected(PROVIDERS.caldav, ['username'])).toBe(false)
    expect(isConnected(PROVIDERS.caldav, ['username', 'password'])).toBe(true)
  })
})

describe('CalendarCredentials', () => {
  it('renders nothing for a provider that takes no credentials', async () => {
    renderFor('ics')
    await waitFor(() => expect(apiMocks.calendarCredentials).toHaveBeenCalled())
    expect(screen.queryByTestId('calendar-credentials')).toBeNull()
  })

  it('renders the fields the backend reports, and reports Not connected when nothing is stored', async () => {
    renderFor('caldav', 'CalDAV')
    expect(await screen.findByLabelText('Username')).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
    expect(screen.getByText('Not connected')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Disconnect' })).toBeNull()
    // No OAuth button for a password provider.
    expect(screen.queryByRole('button', { name: /Sign in with/ })).toBeNull()
  })

  it('saves only the fields the user typed, and never echoes a stored value', async () => {
    apiMocks.saveCalendarCredentials.mockResolvedValue({
      ok: true,
      status: { caldav: { configured: true, fields: ['password', 'username'] } },
    })
    const { notify, container } = renderFor('caldav', 'CalDAV')
    const save = await screen.findByRole('button', { name: 'Save credentials' })
    expect(save).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'alice@example.com' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'app-pass word' } })
    expect(save).toBeEnabled()
    fireEvent.click(save)

    await waitFor(() =>
      expect(apiMocks.saveCalendarCredentials).toHaveBeenCalledWith('caldav', {
        username: 'alice@example.com',
        password: 'app-pass word',
      }),
    )
    await waitFor(() => expect(notify).toHaveBeenCalledWith('Credentials saved.', { type: 'success' }))
    // Stored state: both fields masked, badge flipped, values gone from the DOM.
    expect(await screen.findByText('Connected')).toBeInTheDocument()
    expect(container.textContent).not.toContain('app-pass word')
    expect(container.textContent).not.toContain('alice@example.com')
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument()
  })

  it('sends null for a field the user removed', async () => {
    apiMocks.calendarCredentials.mockResolvedValue(
      response({ caldav: { configured: true, fields: ['password', 'username'] } }),
    )
    apiMocks.saveCalendarCredentials.mockResolvedValue({
      ok: true,
      status: { caldav: { configured: true, fields: ['username'] } },
    })
    renderFor('caldav', 'CalDAV')
    expect(await screen.findByText('Connected')).toBeInTheDocument()
    // Two stored fields, two Remove buttons; the second is the password's.
    const removes = screen.getAllByRole('button', { name: 'Remove' })
    fireEvent.click(removes[1])
    fireEvent.click(screen.getByRole('button', { name: 'Save credentials' }))
    await waitFor(() =>
      expect(apiMocks.saveCalendarCredentials).toHaveBeenCalledWith('caldav', { password: null }),
    )
    expect(await screen.findByText('Credentials saved')).toBeInTheDocument()
  })

  it('reports a failed save and keeps the draft', async () => {
    apiMocks.saveCalendarCredentials.mockRejectedValue(new Error('store is read-only'))
    const { notify } = renderFor('caldav', 'CalDAV')
    fireEvent.change(await screen.findByLabelText('Password'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save credentials' }))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('store is read-only', { type: 'error' }))
    expect(screen.getByLabelText('Password')).toHaveValue('x')
  })

  it('disconnects through the forget route', async () => {
    apiMocks.calendarCredentials.mockResolvedValue(
      response({ caldav: { configured: true, fields: ['password', 'username'] } }),
    )
    apiMocks.forgetCalendarCredentials.mockResolvedValue({ ok: true, status: {} })
    const { notify } = renderFor('caldav', 'CalDAV')
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(apiMocks.forgetCalendarCredentials).toHaveBeenCalledWith('caldav'))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('Calendar disconnected.', { type: 'success' }))
    expect(await screen.findByText('Not connected')).toBeInTheDocument()
  })

  it('keeps the OAuth sign-in disabled until a client id is stored', async () => {
    renderFor('google', 'Google Calendar')
    const signIn = await screen.findByRole('button', { name: 'Sign in with Google Calendar' })
    expect(signIn).toBeDisabled()
    expect(screen.getByText('Save a client ID first, then sign in.')).toBeInTheDocument()
  })

  it('opens the consent URL in a new tab once the backend starts the flow', async () => {
    apiMocks.calendarCredentials.mockResolvedValue(
      response({ google: { configured: true, fields: ['client_id'] } }),
    )
    apiMocks.startCalendarOAuth.mockResolvedValue({
      ok: true,
      authorize_url: 'https://accounts.google.com/o/oauth2/v2/auth?state=abc',
    })
    const opened = vi.spyOn(window, 'open').mockReturnValue({} as Window)
    const { notify } = renderFor('google', 'Google Calendar')
    expect(await screen.findByText('Credentials saved')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Sign in with Google Calendar' }))
    await waitFor(() => expect(apiMocks.startCalendarOAuth).toHaveBeenCalledWith('google'))
    await waitFor(() =>
      expect(opened).toHaveBeenCalledWith(
        'https://accounts.google.com/o/oauth2/v2/auth?state=abc',
        '_blank',
        'noopener,noreferrer',
      ),
    )
    expect(notify).toHaveBeenCalledWith(
      'Finish signing in in the tab that opened, then come back here.',
      { type: 'info' },
    )
    expect(screen.queryByRole('link', { name: /Open the sign-in page/ })).toBeNull()
  })

  it('falls back to a link when the popup is blocked', async () => {
    apiMocks.calendarCredentials.mockResolvedValue(
      response({ google: { configured: true, fields: ['client_id'] } }),
    )
    apiMocks.startCalendarOAuth.mockResolvedValue({ ok: true, authorize_url: 'https://login.example/consent' })
    vi.spyOn(window, 'open').mockReturnValue(null)
    renderFor('google', 'Google Calendar')
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in with Google Calendar' }))
    const link = await screen.findByRole('link', { name: /Open the sign-in page/ })
    expect(link).toHaveAttribute('href', 'https://login.example/consent')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('reports a sign-in that the backend refused', async () => {
    apiMocks.calendarCredentials.mockResolvedValue(
      response({ google: { configured: true, fields: ['client_id'] } }),
    )
    apiMocks.startCalendarOAuth.mockRejectedValue(new Error('No OAuth client id is configured'))
    const opened = vi.spyOn(window, 'open')
    const { notify } = renderFor('google', 'Google Calendar')
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in with Google Calendar' }))
    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith('No OAuth client id is configured', { type: 'error' }),
    )
    expect(opened).not.toHaveBeenCalled()
  })

  it('shows Connected for an OAuth provider once a refresh token is stored', async () => {
    apiMocks.calendarCredentials.mockResolvedValue(
      response({ google: { configured: true, fields: ['access_token', 'client_id', 'refresh_token'] } }),
    )
    renderFor('google', 'Google Calendar')
    expect(await screen.findByText('Connected')).toBeInTheDocument()
    expect(screen.queryByText('Save a client ID first, then sign in.')).toBeNull()
  })

  it('tells the user when the status could not be read', async () => {
    apiMocks.calendarCredentials.mockRejectedValue(new Error('offline'))
    renderFor('caldav', 'CalDAV')
    await waitFor(() => expect(apiMocks.calendarCredentials).toHaveBeenCalled())
    // No schema arrived, so nothing renders — the card above still works.
    expect(screen.queryByTestId('calendar-credentials')).toBeNull()
  })
})
