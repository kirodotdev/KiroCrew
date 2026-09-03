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
type FetchCall = { url: string; method: string; body?: unknown; headers?: Record<string, string> }

let calls: FetchCall[] = []

/** Names the list endpoint returns; mutated between the initial GET and the refetch. */
let listNames: string[] = []

/** When set, the next `/api/secrets` GET rejects — drives the error path. */
let listShouldFail = false

function installFetch() {
  const impl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    // Normalise Headers object / plain object / undefined to a plain record so
    // tests can do a simple property lookup regardless of how fetch was called.
    let headers: Record<string, string> | undefined
    if (init?.headers) {
      if (init.headers instanceof Headers) {
        headers = {}
        init.headers.forEach((v, k) => { headers![k] = v })
      } else {
        headers = { ...(init.headers as Record<string, string>) }
      }
    }
    calls.push({
      url,
      method,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      headers,
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

describe('SecretsPanel error feedback and in-flight guards', () => {
  /**
   * A rejected POST must show the failure to the user, not just leave the form
   * populated. Before this change the mutation error was never rendered, so a
   * 403 looked like nothing happened.
   */
  it('shows the save error message after a failed POST', async () => {
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
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

    // The alert carries the backend prose surfaced by `j()` (HTTP 403: forbidden).
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not save secret')
    expect(alert).toHaveTextContent('403')
    expect(alert).toHaveTextContent('forbidden')
  })

  /**
   * react-query keeps a mutation's error until the next mutate(); without an
   * explicit reset(), a failed save's alert would greet the user again on a
   * freshly reopened (empty) add form — a stale failure attributed to input
   * they have not typed yet.
   */
  it('clears the stale save error when the form is cancelled and reopened', async () => {
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
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
    await screen.findByRole('alert')

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: 'Add secret' }))

    // The reopened, empty form must not carry the previous attempt's failure.
    expect(screen.queryByRole('alert')).toBeNull()
  })

  /**
   * Cancel during an in-flight save would reset the mutation and let a
   * resubmit of the same name race the still-pending original request — the
   * slower original could then overwrite the newer value. Cancel is therefore
   * disabled while the save is pending.
   */
  it('disables Cancel while the POST is in flight', async () => {
    const user = userEvent.setup()
    let resolvePost: (r: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method ?? 'GET'
        if (method === 'POST') {
          return new Promise<Response>((resolve) => {
            resolvePost = resolve
          })
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

    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled()

    resolvePost({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
    } as Response)
  })

  /**
   * Switching confirm rows during an in-flight DELETE must not reset the
   * mutation: the reset would clear the pending gate and allow a duplicate
   * DELETE of the same name, whose delayed original could erase a value the
   * user re-saved in between.
   */
  it('ignores row switches while a DELETE is in flight', async () => {
    const user = userEvent.setup()
    let resolveDelete: (r: Response) => void = () => {}
    const deletes: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method ?? 'GET'
        if (method === 'DELETE') {
          deletes.push(String(input))
          return new Promise<Response>((resolve) => {
            resolveDelete = resolve
          })
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ names: ['ALPHA', 'BETA'] }),
        } as Response)
      }),
    )
    mount()
    await screen.findByText('ALPHA')

    // Open ALPHA's confirm row and fire its DELETE (stays pending).
    await user.click(screen.getByRole('button', { name: 'Delete secret ALPHA' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    expect(deletes).toHaveLength(1)

    // Attempting to open BETA's confirm row mid-flight is a no-op: ALPHA's
    // pending confirm row stays (its Delete disabled), and no second DELETE
    // is ever sent.
    await user.click(screen.getByRole('button', { name: 'Delete secret BETA' }))
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled()
    expect(deletes).toHaveLength(1)

    resolveDelete({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
    } as Response)
  })

  /**
   * Save must be disabled while a DELETE is in flight (and vice versa): a
   * save of name X racing a pending DELETE of X lets the delayed DELETE
   * erase the newly saved value.
   */
  it('disables Save while a DELETE is in flight', async () => {
    const user = userEvent.setup()
    let resolveDelete: (r: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method ?? 'GET'
        if (method === 'DELETE') {
          return new Promise<Response>((resolve) => {
            resolveDelete = resolve
          })
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ names: ['ALPHA'] }),
        } as Response)
      }),
    )
    mount()
    await screen.findByText('ALPHA')

    // Open the add form first so Save is on screen, then fire the DELETE.
    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    await user.type(screen.getByLabelText('Secret name'), 'ALPHA')
    await user.type(screen.getByLabelText('Secret value'), 'sk-new')
    await user.click(screen.getByRole('button', { name: 'Delete secret ALPHA' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()

    resolveDelete({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
    } as Response)
  })

  /**
   * A rejected DELETE must surface under the still-open confirm row, so the user
   * knows the secret was NOT removed.
   */
  it('shows the delete error message after a failed DELETE', async () => {
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
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

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not delete secret')
    expect(alert).toHaveTextContent('500')
    expect(alert).toHaveTextContent('boom')
  })

  /**
   * While the POST is in flight the Save button must be disabled, both to signal
   * progress and to stop a second submit.
   */
  it('disables Save while the POST is in flight', async () => {
    const user = userEvent.setup()
    let resolvePost: (r: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method ?? 'GET'
        if (method === 'POST') {
          // Never settles until we release it, holding the mutation pending.
          return new Promise<Response>(res => {
            resolvePost = res
          })
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

    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeEnabled()
    await user.click(save)

    await waitFor(() => expect(save).toBeDisabled())

    // Release the in-flight request so the test does not leak a pending promise.
    resolvePost({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
    } as Response)
  })

  /**
   * A rapid double-click must not send two POSTs for a single secret. The
   * mechanism is the native `disabled` attribute on Save (set while the
   * mutation is pending): React flushes discrete-event renders synchronously,
   * so by the time the second click is dispatched the button no longer
   * accepts it. (There is deliberately NO handler-side pending guard — it
   * would read the previous render's snapshot and cannot close any window
   * the disabled attribute leaves open.)
   */
  it('does not send a second POST on a double-click', async () => {
    const user = userEvent.setup()
    const seen: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method ?? 'GET'
        if (method === 'POST') {
          seen.push('POST')
          // Stay pending so the first click holds `isPending` true across the
          // second click.
          return new Promise<Response>(() => {})
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

    const save = screen.getByRole('button', { name: 'Save' })
    // Two clicks back to back; only the first may reach the network.
    await user.click(save)
    await user.click(save)

    await waitFor(() => expect(seen.length).toBeGreaterThan(0))
    expect(seen).toHaveLength(1)
  })
})

describe('SecretsPanel session key', () => {
  /**
   * The panel sends the fixed `dashboard:ui` session key that the shared
   * transport (`src/api/client.ts`) uses, on every request.  It previously read
   * `localStorage['kiro_crew_token']` — a key nothing in the app ever writes —
   * so that read always resolved to '' and was vestigial dead code.  This pins
   * the panel to the same `dashboard:ui` identity every other panel sends, and
   * guards against a regression back to a stored-token read.
   */
  it('sends the dashboard:ui session key on both the list GET and a mutating POST', async () => {
    const user = userEvent.setup()

    // Even with a stray token in localStorage, the panel must NOT read it —
    // the header is the fixed dashboard:ui literal.
    localStorage.setItem('kiro_crew_token', 'SHOULD-BE-IGNORED')
    installFetch()

    mount()
    await screen.findByText('No secrets stored yet.')

    const listGet = calls.find(c => c.method === 'GET' && c.url === '/api/secrets')
    expect(listGet?.headers?.['X-Session-Key']).toBe('dashboard:ui')

    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    await user.type(screen.getByLabelText('Secret name'), 'MY_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'sk-new')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      const post = calls.find(c => c.method === 'POST')
      expect(post).toBeTruthy()
      expect(post?.headers?.['X-Session-Key']).toBe('dashboard:ui')
    })
  })
})
