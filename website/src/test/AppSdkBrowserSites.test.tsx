/**
 * The App SDK's `useBrowserSites`: an app page reads its own browsing list; adding a
 * site sends nothing until the owner presses Allow in the dashboard's own dialog;
 * removing and turning off a site apply at once. And the detail page's card lists the
 * sites with their source and status, removes one, and turns a turned-off or removed
 * one back on only after the owner allows it in the same dialog.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import AppConfirmHost from '../components/AppConfirmHost'
import { AppApiProvider, useBrowserSites } from '../app-sdk/index'
import AppDetailBrowserSites from '../pages/AppDetailBrowserSites'
import { queryClient as dashboardQueryClient } from '../api/queryClient'
import { MemoryRouter } from 'react-router-dom'

const LIST = {
  sites: [
    { site: 'https://www.example.com/watch', source: 'app', status: 'approved' },
    { site: 'https://music.example.net/song/*', source: 'owner', status: 'disabled' },
  ],
  active: ['https://www.example.com/watch'],
}

let calls: { method: string; path: string; body?: unknown }[]
let postAnswer: () => Response
let getAnswer: () => Response
let appAnswer: () => Response

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

beforeEach(() => {
  calls = []
  postAnswer = () => json(LIST)
  getAnswer = () => json(LIST)
  appAnswer = () => json({ name: 'tunes', displayName: 'Tunes Player', manifest: { author: 'someone' } })
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    const method = (init?.method ?? 'GET').toUpperCase()
    calls.push({ method, path: url.pathname, ...(init?.body ? { body: JSON.parse(String(init.body)) } : {}) })
    if (method === 'GET' && url.pathname === '/api/apps/tunes') return appAnswer()
    return method === 'GET' ? getAnswer() : postAnswer()
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function mount() {
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(AppApiProvider, { appName: 'tunes', allowedApiPaths: [], allowedEvents: [] }, children)
  return renderHook(() => useBrowserSites(), { wrapper })
}

const posts = () => calls.filter(c => c.method === 'POST')

describe('useBrowserSites', () => {
  it("reads the app's own list with no declared api path", async () => {
    const { result } = mount()
    await waitFor(() => expect(result.current.sites).toEqual(LIST.sites))
    expect(result.current.active).toEqual(LIST.active)
    expect(calls[0]).toEqual({ method: 'GET', path: '/api/apps/tunes/browser-sites' })
  })

  it('sends an add only after the owner presses Allow in the dashboard dialog', async () => {
    render(<AppConfirmHost />)
    const { result } = mount()
    await waitFor(() => expect(result.current.sites).not.toBeNull())
    let answer: Promise<unknown> | undefined
    act(() => { answer = result.current.requestAdd(' https://music.example.net/song/* ') })
    const dialog = await screen.findByRole('dialog')
    // The dialog names the site exactly, and what allowing it lets the agent do.
    expect(within(dialog).getByTestId('browser-site-add-site').textContent).toBe('https://music.example.net/song/*')
    // The app asked, so the dialog says so, by its display name, author and id.
    expect(dialog.textContent).toContain('Tunes Player asks to add')
    expect(within(dialog).getByTestId('browser-site-add-app').textContent).toBe('By someone · app id tunes')
    expect(calls).toContainEqual({ method: 'GET', path: '/api/apps/tunes' })
    expect(dialog.textContent).toContain('signed in as you')
    expect(dialog.textContent).toContain('Allow only a site you trust.')
    expect(posts()).toEqual([])
    fireEvent.click(within(dialog).getByRole('button', { name: 'Allow site' }))
    let settled: unknown
    await act(async () => { settled = await answer })
    expect(settled).toMatchObject({ ok: true, active: LIST.active })
    expect(posts()).toEqual([
      { method: 'POST', path: '/api/apps/tunes/browser-sites', body: { action: 'add', site: 'https://music.example.net/song/*' } },
    ])
  })

  it("still asks, naming the app by its id, when the app's row cannot be read", async () => {
    appAnswer = () => json({ error: 'app not found' }, 404)
    render(<AppConfirmHost />)
    const { result } = mount()
    await waitFor(() => expect(result.current.sites).not.toBeNull())
    let answer: Promise<unknown> | undefined
    act(() => { answer = result.current.requestAdd('https://music.example.net/song/*') })
    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toContain('tunes asks to add')
    expect(within(dialog).getByTestId('browser-site-add-app').textContent).toBe('App id tunes')
    expect(within(dialog).getByTestId('browser-site-add-site').textContent).toBe('https://music.example.net/song/*')
    expect(posts()).toEqual([])
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }))
    let settled: unknown
    await act(async () => { settled = await answer })
    expect(settled).toMatchObject({ ok: false, code: 'declined' })
    expect(posts()).toEqual([])
  })

  it('a declined add sends nothing', async () => {
    render(<AppConfirmHost />)
    const { result } = mount()
    await waitFor(() => expect(result.current.sites).not.toBeNull())
    let answer: Promise<unknown> | undefined
    act(() => { answer = result.current.requestAdd('https://music.example.net/song/*') })
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }))
    let settled: unknown
    await act(async () => { settled = await answer })
    expect(settled).toEqual({ ok: false, code: 'declined', error: 'the owner did not allow it' })
    expect(posts()).toEqual([])
  })

  it('an add with no dialog host sends nothing', async () => {
    const { result } = mount()
    await waitFor(() => expect(result.current.sites).not.toBeNull())
    let settled: unknown
    await act(async () => { settled = await result.current.requestAdd('https://music.example.net/song/*') })
    expect(settled).toMatchObject({ ok: false, code: 'declined' })
    expect(posts()).toEqual([])
  })

  it('remove and disable apply at once, with no dialog', async () => {
    const { result } = mount()
    await waitFor(() => expect(result.current.sites).not.toBeNull())
    await act(async () => { await result.current.requestDisable('https://www.example.com/watch') })
    await act(async () => { await result.current.requestRemove('https://www.example.com/watch') })
    expect(posts().map(c => c.body)).toEqual([
      { action: 'disable', site: 'https://www.example.com/watch' },
      { action: 'remove', site: 'https://www.example.com/watch' },
    ])
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it("marks the detail page's cached list stale after a change", async () => {
    const spy = vi.spyOn(dashboardQueryClient, 'invalidateQueries')
    const { result } = mount()
    await waitFor(() => expect(result.current.sites).not.toBeNull())
    await act(async () => { await result.current.requestRemove('https://www.example.com/watch') })
    expect(spy).toHaveBeenCalledWith({ queryKey: ['apps', 'tunes', 'browser-sites'] })
    spy.mockRestore()
  })

  it("says the gateway's refusal by code", async () => {
    postAnswer = () => json({ code: 'not_listed', error: 'that site is not on this app\'s list' }, 404)
    const { result } = mount()
    await waitFor(() => expect(result.current.sites).not.toBeNull())
    let settled: unknown
    await act(async () => { settled = await result.current.requestRemove('https://other.example.net/a') })
    expect(settled).toEqual({ ok: false, code: 'not_listed', error: "that site is not on this app's list" })
  })
})

describe('AppDetailBrowserSites', () => {
  it('lists every site with its source and status and removes one', async () => {
    const changed = vi.fn()
    postAnswer = () => json({ sites: [LIST.sites[1]], active: [] })
    render(
      <MemoryRouter>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <AppDetailBrowserSites appName="tunes" onChanged={changed} />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    const rows = await screen.findAllByTestId('app-browser-site-row')
    expect(rows.map(r => r.textContent)).toEqual([
      expect.stringContaining('From the app'),
      expect.stringContaining('Added by you'),
    ])
    expect(rows[0].textContent).toContain('Allowed')
    expect(rows[1].textContent).toContain('Turned off')
    fireEvent.click(within(rows[0]).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(screen.getAllByTestId('app-browser-site-row')).toHaveLength(1))
    expect(posts().map(c => c.body)).toEqual([{ action: 'remove', site: 'https://www.example.com/watch' }])
    expect(changed).toHaveBeenCalled()
  })

  it('refreshes the mounted list when apps change', async () => {
    const rows = await mountCard()
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('https://www.example.com/watch')
    getAnswer = () => json({
      sites: [{ site: 'https://video.example.org/v/*', source: 'app', status: 'pending' }],
      active: [],
    })
    await act(async () => { window.dispatchEvent(new Event('mc:apps-changed')) })
    await waitFor(() => {
      const updatedRows = screen.getAllByTestId('app-browser-site-row')
      expect(updatedRows).toHaveLength(1)
      expect(updatedRows[0].textContent).toContain('https://video.example.org/v/*')
      expect(updatedRows[0].textContent).toContain('Waiting for approval')
    })
    expect(screen.queryByText('https://www.example.com/watch')).toBeNull()
    expect(screen.queryByText('https://music.example.net/song/*')).toBeNull()
  })

  const RULED = {
    sites: [
      { site: 'https://www.example.com/watch', source: 'app', status: 'disabled' },
      { site: 'https://video.example.org/v/*', source: 'app', status: 'removed' },
    ],
    active: [],
  }

  function mountCard() {
    render(
      <MemoryRouter>
        <AppConfirmHost />
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <AppDetailBrowserSites appName="tunes" />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    return screen.findAllByTestId('app-browser-site-row')
  }

  it.each([
    ['Turn on', 0, 'https://www.example.com/watch', 'Turn this browsing site back on?'],
    ['Add back', 1, 'https://video.example.org/v/*', 'Add this browsing site back?'],
  ])('%s asks the owner in the owner\'s words and sends add only after confirming', async (label, index, site, title) => {
    getAnswer = () => json(RULED)
    const rows = await mountCard()
    act(() => { fireEvent.click(within(rows[index]).getByRole('button', { name: label })) })
    const dialog = await screen.findByRole('dialog', { name: title })
    expect(within(dialog).getByTestId('browser-site-restore-site').textContent).toBe(site)
    // The owner chose this on Crew's own page: not the app's request dialog.
    expect(dialog.textContent).not.toContain('asks to add')
    expect(within(dialog).queryByTestId('browser-site-add-site')).toBeNull()
    expect(within(dialog).queryByRole('button', { name: 'Allow site' })).toBeNull()
    expect(dialog.textContent).toContain('signed in as you')
    expect(dialog.textContent).toContain('Allow only a site you trust.')
    expect(posts()).toEqual([])
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: label })) })
    await waitFor(() => expect(posts().map(c => c.body)).toEqual([{ action: 'add', site }]))
  })

  it('a declined Turn on sends nothing', async () => {
    getAnswer = () => json(RULED)
    const rows = await mountCard()
    act(() => { fireEvent.click(within(rows[0]).getByRole('button', { name: 'Turn on' })) })
    const dialog = await screen.findByRole('dialog')
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i })) })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(posts()).toEqual([])
  })

  it('a removed row shows Removed and has no Remove', async () => {
    getAnswer = () => json(RULED)
    const rows = await mountCard()
    expect(rows[1].textContent).toContain('Removed')
    expect(within(rows[1]).queryByRole('button', { name: 'Remove' })).toBeNull()
    expect(within(rows[1]).getByRole('button', { name: 'Add back' })).toBeTruthy()
    expect(within(rows[0]).getByRole('button', { name: 'Remove' })).toBeTruthy()
  })

  it('says so when the list cannot be read', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ code: 'unreadable', error: "this app's site list could not be read" }, 500)))
    render(
      <MemoryRouter>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <AppDetailBrowserSites appName="tunes" />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    const card = await screen.findByTestId('app-browser-sites')
    await waitFor(() => expect(card.textContent).toContain('could not be read'))
  })

  it('draws nothing while the list is empty', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ sites: [], active: [] })))
    const { container } = render(
      <QueryClientProvider client={new QueryClient()}>
        <AppDetailBrowserSites appName="tunes" />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(container.innerHTML).toBe(''))
  })
})
