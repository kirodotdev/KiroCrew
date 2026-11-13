import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ArtifactDeployPage from '../pages/ArtifactDeployPage'
import { i18next } from '../i18n'
import '../i18n/all'

/**
 * Verify on the AWS Profiles table: each profile row checks reachability, and
 * two checks can be in flight at once. Only the newest check may set the
 * verdict, and a row's Verify stays locked while its own check runs.
 */

type Deferred = { resolve: (body: unknown) => void }
const verifies: Array<{ profile: string } & Deferred> = []

function stubFetch() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url)
    if (u.endsWith('/verify')) {
      const { profile } = JSON.parse(String(init?.body)) as { profile: string }
      return new Promise<Response>(res => {
        verifies.push({ profile, resolve: body => res({ ok: true, status: 200, json: async () => body } as unknown as Response) })
      })
    }
    const body = u.includes('/api/artifacts')
      ? { artifacts: [] }
      : u.endsWith('/list')
        ? { sites: [], configured: true }
        : u.endsWith('/profiles')
          ? {
              profiles: [
                { name: 'alpha', region: 'us-west-2', account: '1111', verified_at: '', note: '' },
                { name: 'beta', region: 'us-east-1', account: '2222', verified_at: '', note: '' },
              ],
              default: 'alpha',
              available: [],
            }
          : { profile: 'alpha', region: 'us-west-2' }
    return { ok: true, status: 200, json: async () => body } as unknown as Response
  }))
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<MemoryRouter><QueryClientProvider client={qc}><ArtifactDeployPage /></QueryClientProvider></MemoryRouter>)
}

const verifyButton = (profile: string) => {
  const row = screen.getAllByText(profile).map(el => el.closest('tr')).find(Boolean) as HTMLElement
  return within(row).getByRole('button', { name: /Verify/ })
}

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

beforeEach(() => {
  verifies.length = 0
  vi.unstubAllGlobals()
  stubFetch()
})

describe('ArtifactDeployPage profile Verify', () => {
  it('shows the newest check, not an older one that lands after it', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText(/AWS Profiles \(2\)/)).toBeInTheDocument())

    fireEvent.click(verifyButton('alpha'))
    fireEvent.click(verifyButton('beta'))
    await waitFor(() => expect(verifies.map(v => v.profile)).toEqual(['alpha', 'beta']))

    // The newer check (beta) answers first, then the older one (alpha).
    await act(async () => { verifies[1].resolve({ reachable: true, profile: 'beta', account: '2222' }) })
    await waitFor(() => expect(screen.getByText('beta: access reachable (account 2222)')).toBeInTheDocument())
    await act(async () => { verifies[0].resolve({ reachable: true, profile: 'alpha', account: '1111' }) })

    expect(screen.getByText('beta: access reachable (account 2222)')).toBeInTheDocument()
    expect(screen.queryByText(/alpha: access reachable/)).toBeNull()
  })

  it('locks a row\u2019s Verify while its own check runs, and only that row, keeping focus on it', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText(/AWS Profiles \(2\)/)).toBeInTheDocument())

    const alpha = verifyButton('alpha')
    alpha.focus()
    fireEvent.click(alpha)
    await waitFor(() => expect(verifyButton('alpha')).toHaveAttribute('aria-disabled', 'true'))
    // `aria-disabled`, not `disabled`: a disabled button leaves the tab order
    // and drops a keyboard user's focus to the page while the check runs.
    expect(verifyButton('alpha')).not.toBeDisabled()
    expect(verifyButton('alpha')).toHaveFocus()
    expect(verifyButton('beta')).not.toHaveAttribute('aria-disabled')
    fireEvent.click(verifyButton('alpha'))
    expect(verifies).toHaveLength(1)

    await act(async () => { verifies[0].resolve({ reachable: true, profile: 'alpha' }) })
    await waitFor(() => expect(verifyButton('alpha')).not.toHaveAttribute('aria-disabled'))
    expect(verifyButton('alpha')).toHaveFocus()
    expect(screen.getByText('alpha: access reachable')).toBeInTheDocument()
  })

  it('names the account through the translated text', async () => {
    await i18next.changeLanguage('de')
    const { unmount } = renderPage()
    await waitFor(() => expect(screen.getByText('Konto 1111')).toBeInTheDocument())
    expect(screen.queryByText(/acct /)).toBeNull()
    // Unmounted before afterEach switches back to English, so that switch
    // repaints nothing outside the test.
    unmount()
  })
})
