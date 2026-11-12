/**
 * The gh proxy switch on Settings > Session Environments: what it says before it
 * is turned on (whose account, what it may do, where it runs), its sign-in and
 * lookup-failure states, and that a change made mid-save is not lost.
 *
 * Split from SessionEnvironmentsCard.test.tsx (file line cap); the harness here
 * is the subset these cases use.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { SessionEnvironmentsCard } from '../pages/settings/SessionEnvironmentsCard'
import { PREVIEW_SESSION_ENVIRONMENTS } from '../utils/previewFlags'

vi.mock('../pages/settings/DecisionsCard', () => ({ DecisionsCard: () => null }))

interface Section {
  coder_url: string
  profiles: unknown[]
  default_profile?: string
  stop_on_shutdown?: boolean
  gh_proxy?: boolean
}
let stored: { session_environments?: Section }
let writes: unknown[]
let qc: QueryClient

beforeEach(() => {
  localStorage.clear()
  writes = []
  stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [] } }
  server.use(
    http.get('*/api/config/kirocrew', () => HttpResponse.json(stored)),
    http.patch('*/api/config/kirocrew', async ({ request }) => {
      const body = await request.json() as { path: string; value: Section }
      writes.push(body)
      stored = { session_environments: body.value }
      return HttpResponse.json(stored)
    }),
    http.get('*/api/session-environments/templates', () => HttpResponse.json({ templates: [] })),
    http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ workspaces: [] })),
  )
})
afterEach(() => { cleanup(); qc?.clear(); localStorage.clear(); vi.restoreAllMocks() })

/** The section's one Save. */
const save = () => screen.getByRole('button', { name: 'Save settings' })
async function mounted() {
  localStorage.setItem(PREVIEW_SESSION_ENVIRONMENTS, '1')
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const view = render(<Provider store={createTestStore()}><QueryClientProvider client={qc}><MemoryRouter><SessionEnvironmentsCard /></MemoryRouter></QueryClientProvider></Provider>)
  await screen.findByLabelText('Coder URL')
  return view
}

describe('Session Environments gh proxy switch', () => {
  it('says whose account the gh proxy would use and what it may do, and saves it on', async () => {
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [], default_profile: '', stop_on_shutdown: true, gh_proxy: false } as Section }
    server.use(http.get('*/api/session-environments/gh-login', () => HttpResponse.json({ login: 'octo-owner' })))
    await mounted()
    const proxy = () => screen.getByRole('switch', { name: 'Open and update pull requests from remote sessions' })
    expect(proxy()).not.toBeChecked()
    // Names the gh proxy and the account it would act as, before it is on.
    expect(await screen.findByText('Lets agents in a remote session open pull requests and post comments on GitHub as your GitHub account @octo-owner. Each one starts with a line saying an agent wrote it, and anyone who can see the repository can read it.')).toBeInTheDocument()
    // The limits, as a list the switch is described by, in reading order.
    const notes = screen.getByTestId('gh-proxy-notes')
    expect(proxy().getAttribute('aria-describedby')?.split(' ')).toContain(notes.id)
    expect([...notes.querySelectorAll('li')].map(li => li.textContent)).toEqual([
      'Acts only on repositories the session is working in, and writes only where it has uploaded its changes',
      'Marks every pull request description and comment it writes as written by an agent',
      "Can't merge, approve or change settings",
      "Runs through gh-proxy, Kiro Crew's helper on this computer, with the GitHub sign-in here; the remote session never gets it",
      'Off stops it immediately',
    ])
    fireEvent.click(proxy())
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: Section }).value).toMatchObject({ gh_proxy: true })
  })

  it('holds every switch while a save is in flight, so a newer change is never lost', async () => {
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [], default_profile: '', stop_on_shutdown: true, gh_proxy: false } as Section }
    let release: () => void = () => {}
    server.use(http.patch('*/api/config/kirocrew', async ({ request }) => {
      const body = await request.json() as { path: string; value: Section }
      writes.push(body)
      await new Promise<void>(resolve => { release = resolve })
      stored = { session_environments: body.value }
      return HttpResponse.json(stored)
    }))
    await mounted()
    const proxy = () => screen.getByRole('switch', { name: 'Open and update pull requests from remote sessions' })
    fireEvent.click(proxy())
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    // Mid-save: the switch must not take a change the save would then discard.
    fireEvent.click(proxy())
    expect(proxy()).toBeChecked()
    act(() => release())
    await waitFor(() => expect(proxy()).toBeChecked())
  })

  it('says the login lookup failed and retries it, keeping the switch usable', async () => {
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [], default_profile: '', stop_on_shutdown: true, gh_proxy: false } as Section }
    let calls = 0
    server.use(http.get('*/api/session-environments/gh-login', () => (++calls === 1 ? HttpResponse.error() : HttpResponse.json({ login: 'octo-owner' }))))
    await mounted()
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    expect(await screen.findByText(/as your GitHub account @octo-owner\. Each one starts/)).toBeInTheDocument()
    expect(screen.queryByText(/run the command below, then select Retry\./)).toBeNull()
    expect(screen.queryByTestId('gh-proxy-sign-in')).toBeNull()
  })

  it('says plainly when this computer has no GitHub sign-in yet', async () => {
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [], default_profile: '', stop_on_shutdown: true, gh_proxy: false } as Section }
    server.use(http.get('*/api/session-environments/gh-login', () => HttpResponse.json({ login: null })))
    await mounted()
    expect(await screen.findByText(/This computer has no GitHub sign-in yet: you can turn this on now, and it can post once you sign in\./)).toBeInTheDocument()
    // The next step, copyable, and no claim about a sign-in that is not there.
    const signIn = screen.getByTestId('gh-proxy-sign-in')
    expect(signIn).toHaveTextContent('open a terminal on this computer')
    expect(within(signIn).getByText('gh auth login').tagName).toBe('CODE')
    const notes = [...screen.getByTestId('gh-proxy-notes').querySelectorAll('li')].map(li => li.textContent)
    expect(notes).toContain("Runs through gh-proxy, Kiro Crew's helper on this computer; the remote session never gets your GitHub sign-in")
  })
})
