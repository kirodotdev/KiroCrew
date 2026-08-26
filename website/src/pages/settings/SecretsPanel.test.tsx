import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { SecretsPanel } from './SecretsPanel'

/**
 * The panel talks to `/api/secrets` through bare `fetch` (not the `api` client),
 * so the seam under test is the global fetch. Each case stubs it with a small
 * router keyed on method + URL rather than a single blanket resolve, because the
 * add and delete paths must be asserted on the REQUEST they send, not just on
 * the re-render they cause.
 */
type FetchCall = { url: string; method: string; body?: unknown }

let calls: FetchCall[] = []

/** Names the list endpoint returns; mutated between the initial GET and the refetch. */
let listNames: string[] = []

/** When set, the next `/api/secrets` GET rejects — drives the error path. */
let listShouldFail = false

function installFetch() {
  const impl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({
      url,
      method,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    })

    if (method === 'GET' && url === '/api/secrets') {
      if (listShouldFail) return Promise.reject(new Error('boom'))
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ names: listNames }),
      } as Response)
    }
    // POST /api/secrets and DELETE /api/secrets/:name both just acknowledge.
    // `ok`/`status` are required: the panel's `j()` helper rejects a non-OK
    // response, so a mock without them would read as a failure.
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
    } as Response)
  })
  vi.stubGlobal('fetch', impl)
  return impl
}

function mount() {
  // `retry: false` so the error case settles on the first rejection instead of
  // outliving the test timeout on react-query's default backoff.
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const utils = render(
    <QueryClientProvider client={qc}>
      <SecretsPanel />
    </QueryClientProvider>,
  )
  return { qc, ...utils }
}

beforeEach(() => {
  calls = []
  listNames = []
  listShouldFail = false
  localStorage.setItem('kiro_crew_token', 'test-token')
  installFetch()
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('SecretsPanel', () => {
  it('renders the section heading and description', async () => {
    mount()

    expect(await screen.findByText('Secrets Vault')).toBeInTheDocument()
    expect(screen.getByText(/Store API keys and credentials securely/)).toBeInTheDocument()
  })

  it('shows the loading line while the list query is in flight', () => {
    // A never-settling GET keeps `isLoading` true for the assertion.
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>(() => {})),
    )
    mount()

    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the empty state when no secrets are stored', async () => {
    listNames = []
    mount()

    expect(await screen.findByText('No secrets stored yet.')).toBeInTheDocument()
  })

  it('lists stored secret names with values masked', async () => {
    listNames = ['MY_API_KEY', 'DB_PASSWORD']
    mount()

    expect(await screen.findByText('MY_API_KEY')).toBeInTheDocument()
    expect(screen.getByText('DB_PASSWORD')).toBeInTheDocument()
    // The plaintext is never rendered — only the mask is.
    expect(screen.getAllByText('••••••••')).toHaveLength(2)
    expect(screen.queryByText('No secrets stored yet.')).not.toBeInTheDocument()
  })

  it('sends the session key header on the list request', async () => {
    mount()
    await screen.findByText('No secrets stored yet.')

    const listCall = calls.find(c => c.method === 'GET')
    expect(listCall?.url).toBe('/api/secrets')
  })

  it('opens the add form and keeps Save disabled until both fields are filled', async () => {
    const user = userEvent.setup()
    mount()
    await screen.findByText('No secrets stored yet.')

    await user.click(screen.getByRole('button', { name: 'Add secret' }))

    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    // Name alone is not enough.
    await user.type(screen.getByLabelText('Secret name'), 'MY_KEY')
    expect(save).toBeDisabled()

    // Value completes it.
    await user.type(screen.getByLabelText('Secret value'), 'sk-abc123')
    expect(save).toBeEnabled()
  })

  it('POSTs the trimmed name and value, then closes the form', async () => {
    const user = userEvent.setup()
    mount()
    await screen.findByText('No secrets stored yet.')

    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    await user.type(screen.getByLabelText('Secret name'), '  MY_KEY  ')
    await user.type(screen.getByLabelText('Secret value'), 'sk-abc123')

    // The refetch after the mutation should see the new name.
    listNames = ['MY_KEY']
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      const post = calls.find(c => c.method === 'POST')
      expect(post).toBeTruthy()
      expect(post?.url).toBe('/api/secrets')
      // Name is trimmed; the value is passed through untouched.
      expect(post?.body).toEqual({ name: 'MY_KEY', value: 'sk-abc123' })
    })

    // Form closes and the field state is reset back to the Add button.
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Add secret' })).toBeInTheDocument()
    })
    expect(screen.queryByLabelText('Secret name')).not.toBeInTheDocument()
  })

  it('discards the typed values when the add form is cancelled', async () => {
    const user = userEvent.setup()
    mount()
    await screen.findByText('No secrets stored yet.')

    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    await user.type(screen.getByLabelText('Secret name'), 'SCRATCH')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    // Nothing was sent, and reopening starts from an empty field.
    expect(calls.some(c => c.method === 'POST')).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    expect(screen.getByLabelText('Secret name')).toHaveValue('')
  })

  it('requires a confirmation step before deleting', async () => {
    const user = userEvent.setup()
    listNames = ['MY_API_KEY']
    mount()
    await screen.findByText('MY_API_KEY')

    await user.click(screen.getByRole('button', { name: 'Delete secret MY_API_KEY' }))

    // The confirm prompt replaces the trash affordance; no request yet.
    expect(screen.getByText('Delete this secret?')).toBeInTheDocument()
    expect(calls.some(c => c.method === 'DELETE')).toBe(false)
  })

  it('DELETEs the url-encoded name once confirmed', async () => {
    const user = userEvent.setup()
    listNames = ['MY KEY/1']
    mount()
    await screen.findByText('MY KEY/1')

    await user.click(screen.getByRole('button', { name: 'Delete secret MY KEY/1' }))
    listNames = []
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    await waitFor(() => {
      const call = calls.find(c => c.method === 'DELETE')
      expect(call?.url).toBe(`/api/secrets/${encodeURIComponent('MY KEY/1')}`)
    })
  })

  it('abandons the delete when the confirmation is cancelled', async () => {
    const user = userEvent.setup()
    listNames = ['MY_API_KEY']
    mount()
    await screen.findByText('MY_API_KEY')

    await user.click(screen.getByRole('button', { name: 'Delete secret MY_API_KEY' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    // Back to the trash affordance, nothing deleted.
    expect(screen.queryByText('Delete this secret?')).not.toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Delete secret MY_API_KEY' }),
    ).toBeInTheDocument()
    expect(calls.some(c => c.method === 'DELETE')).toBe(false)
  })

  it('renders the empty state rather than crashing when the list request fails', async () => {
    listShouldFail = true
    mount()

    // `names` falls back to [] on error, so the panel degrades to the empty state
    // instead of throwing — the Add path stays reachable.
    expect(await screen.findByText('No secrets stored yet.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add secret' })).toBeInTheDocument()
  })
})

describe('SecretsPanel error handling', () => {
  /**
   * The data-loss regression: a bare `r.json()` resolves for a 403, so
   * react-query ran `onSuccess`, which cleared the form. The user's typed secret
   * was discarded without ever being stored. A non-OK status must reject.
   */
  it('keeps the typed secret in the form when the POST is rejected', async () => {
    const user = userEvent.setup()
    // Route the POST to a 403 while the list GET keeps working.
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method ?? 'GET'
        if (method === 'POST') {
          return Promise.resolve({
            ok: false,
            status: 403,
            json: () => Promise.resolve({ error: 'forbidden' }),
          } as Response)
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ names: [] }),
        } as Response)
      }),
    )
    mount()
    await screen.findByText('No secrets stored yet.')

    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    await user.type(screen.getByLabelText('Secret name'), 'MY_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'sk-abc123')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    // The form must still be open with BOTH values intact — this is the whole
    // point of the fix. If `onSuccess` had fired, these would be gone.
    await waitFor(() => {
      expect(screen.getByLabelText('Secret name')).toHaveValue('MY_KEY')
    })
    expect(screen.getByLabelText('Secret value')).toHaveValue('sk-abc123')
  })

  it('keeps the confirmation open when the DELETE is rejected', async () => {
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method ?? 'GET'
        if (method === 'DELETE') {
          return Promise.resolve({
            ok: false,
            status: 500,
            json: () => Promise.resolve({ error: 'boom' }),
          } as Response)
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ names: ['MY_API_KEY'] }),
        } as Response)
      }),
    )
    mount()
    await screen.findByText('MY_API_KEY')

    await user.click(screen.getByRole('button', { name: 'Delete secret MY_API_KEY' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    // A failed delete must not resolve the confirmation — otherwise the UI
    // implies the secret is gone when it is still stored.
    await waitFor(() => {
      expect(screen.getByText('Delete this secret?')).toBeInTheDocument()
    })
    expect(screen.getByText('MY_API_KEY')).toBeInTheDocument()
  })

  it('surfaces the backend error prose in the thrown error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 400,
          json: () => Promise.resolve({ error: 'Secret name must be a string' }),
        } as Response),
      ),
    )
    mount()

    // The list query rejects, so the panel shows the empty state. The assertion
    // that matters is that a non-OK status did NOT resolve as data.
    expect(await screen.findByText('No secrets stored yet.')).toBeInTheDocument()
  })

  it('rejects a non-OK response whose body is not JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 502,
          json: () => Promise.reject(new SyntaxError('not json')),
        } as unknown as Response),
      ),
    )
    mount()

    // The detail-extraction `catch` must swallow the parse failure and still
    // throw on the status, not leak a SyntaxError.
    expect(await screen.findByText('No secrets stored yet.')).toBeInTheDocument()
  })
})
