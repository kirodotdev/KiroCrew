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
type ManagedSecret = { name: string; kind: 'jira_api_token' | 'jira_host_token'; host?: string }

let calls: FetchCall[] = []

/** Names and managed catalog entries returned by the list endpoint. */
let listNames: string[] = []
let listManaged: ManagedSecret[] = []
let listUnused: Array<{ name: string; reason: 'wakatime_disabled' | 'jira_multi_host' | 'jira_host_precedence' }> = []

/** Non-fatal managed catalog warning returned by the list endpoint. */
let listManagedError = false

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
        json: () => Promise.resolve({
          names: listNames,
          managed: listManaged,
          ...(listUnused.length ? { unused: listUnused } : {}),
          ...(listManagedError ? { managed_error: listManagedError } : {}),
        }),
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
  listManaged = []
  listUnused = []
  listManagedError = false
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
    const user = userEvent.setup()
    listNames = []
    mount()

    expect(await screen.findByText('No secrets stored yet.')).toBeInTheDocument()
    expect(screen.getByText(/KIROCREW_HOME\/config.json/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open the secrets setup guide' })).toHaveAttribute(
      'href',
      'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/secrets-env.md',
    )

    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    expect(screen.queryByText(/configured in config.json/)).not.toBeInTheDocument()
  })

  it('shows a non-fatal managed config warning while keeping stored names', async () => {
    listNames = ['MY_API_KEY']
    listManagedError = true
    mount()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Some automatic credentials are hidden because $KIROCREW_HOME/config.json',
    )
    expect(screen.getByText('MY_API_KEY')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add secret' })).toBeInTheDocument()
  })

  it('lists stored secret names with values masked', async () => {
    listNames = ['MY_API_KEY', 'DB_PASSWORD']
    mount()

    expect(await screen.findByText('MY_API_KEY')).toBeInTheDocument()
    expect(screen.getByText('DB_PASSWORD')).toBeInTheDocument()
    expect(screen.getByText('Stored secrets')).toBeInTheDocument()
    expect(screen.getByText(/MCP server configuration or \.env/)).toBeInTheDocument()
    // The plaintext is never rendered — only the mask is.
    expect(screen.getAllByText('••••••••')).toHaveLength(2)
    expect(screen.queryByText('No secrets stored yet.')).not.toBeInTheDocument()
    expect(screen.queryByText(/To use WakaTime or Jira automatically/)).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open the secrets setup guide' })).toBeInTheDocument()
  })


  it('marks an orphaned WakaTime key unused while WakaTime is disabled', async () => {
    listNames = ['WAKATIME_API_KEY']
    listUnused = [{ name: 'WAKATIME_API_KEY', reason: 'wakatime_disabled' }]
    mount()

    expect(await screen.findByText('Not used while WakaTime is disabled. Enable WakaTime or delete this entry.')).toBeInTheDocument()
  })

  it('renders managed Jira credentials separately from other stored names', async () => {
    const user = userEvent.setup()
    listNames = ['JIRA_API_TOKEN', 'WEATHER_API_KEY']
    listManaged = [
      { name: 'JIRA_API_TOKEN', kind: 'jira_api_token' },
    ]
    mount()

    expect(await screen.findByText('Used automatically by Kiro Crew')).toBeInTheDocument()
    expect(screen.getByText('Jira API token')).toBeInTheDocument()
    expect(screen.getByText(/Authenticates Jira issue lookups/)).toBeInTheDocument()
    expect(screen.getByText('JIRA_API_TOKEN')).toBeInTheDocument()
    expect(screen.getByText('Other stored secrets')).toBeInTheDocument()
    expect(screen.getByText('WEATHER_API_KEY')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Replace' })).toBeInTheDocument()
    expect(screen.queryByText('test-value-123')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    const replace = screen.getByRole('button', { name: 'Replace' })
    expect(replace).toBeEnabled()
    expect(replace).toHaveAttribute('title', 'Replace')
  })

  it('configures an unset managed credential without asking for its key name', async () => {
    const user = userEvent.setup()
    listManaged = [
      { name: 'JIRA_API_TOKEN', kind: 'jira_api_token' },
    ]
    mount()
    await screen.findByText('Jira API token')

    expect(screen.queryByLabelText('Secret name')).not.toBeInTheDocument()
    expect(screen.getAllByText('JIRA_API_TOKEN')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Add secret' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Save JIRA_API_TOKEN' })).toBeDisabled()
    await user.type(screen.getByLabelText('Jira API token'), 'jira-token-value')
    listNames = ['JIRA_API_TOKEN']
    listManaged = [
      { name: 'JIRA_API_TOKEN', kind: 'jira_api_token' },
    ]
    await user.click(screen.getByRole('button', { name: 'Save JIRA_API_TOKEN' }))

    await waitFor(() => {
      const post = calls.find(c => c.method === 'POST')
      expect(post?.body).toEqual({ name: 'JIRA_API_TOKEN', value: 'jira-token-value' })
    })
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
  })

  it('keeps managed row drafts independent and leaves Add available', async () => {
    const user = userEvent.setup()
    listNames = ['JIRA_API_TOKEN']
    listManaged = [
      { name: 'WAKATIME_API_KEY', kind: 'wakatime_api_key' },
      { name: 'JIRA_API_TOKEN', kind: 'jira_api_token' },
    ]
    mount()
    await screen.findByText('WakaTime API key')

    await user.type(screen.getByLabelText('WakaTime API key'), 'waka-draft')
    await user.click(screen.getByRole('button', { name: 'Replace' }))

    expect(screen.getByLabelText('WakaTime API key')).toHaveValue('waka-draft')
    expect(screen.getByLabelText('Jira API token')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add secret' })).toBeEnabled()
  })

  it('clears Saved feedback when deletion starts', async () => {
    const user = userEvent.setup()
    listNames = ['JIRA_API_TOKEN']
    listManaged = [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }]
    mount()
    await screen.findByText('Jira API token')

    await user.click(screen.getByRole('button', { name: 'Replace' }))
    await user.type(screen.getByLabelText('Jira API token'), 'replacement-value')
    await user.click(screen.getByRole('button', { name: 'Save JIRA_API_TOKEN' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')

    await user.click(screen.getByRole('button', { name: 'Delete Jira API token' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('removes a configured managed credential only after confirmation', async () => {
    const user = userEvent.setup()
    listNames = ['JIRA_API_TOKEN']
    listManaged = [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }]
    mount()
    await screen.findByText('Jira API token')

    const deleteAction = screen.getByRole('button', { name: 'Delete Jira API token' })
    expect(deleteAction).toHaveTextContent('Delete')
    expect(deleteAction).toHaveClass('border-danger', 'text-danger')
    await user.click(deleteAction)
    expect(screen.queryByRole('button', { name: 'Delete Jira API token' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus()
    expect(screen.getByRole('button', { name: 'Replace' })).toBeInTheDocument()
    expect(screen.getByText('Permanently delete “Jira API token”? The current value cannot be recovered.')).toBeInTheDocument()
    expect(calls.some(call => call.method === 'DELETE')).toBe(false)

    listNames = []
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => {
      expect(calls.find(call => call.method === 'DELETE')?.url).toBe('/api/secrets/JIRA_API_TOKEN')
    })
  })

  it('returns focus to managed Delete after cancelling confirmation', async () => {
    const user = userEvent.setup()
    listNames = ['JIRA_API_TOKEN']
    listManaged = [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }]
    mount()
    await screen.findByText('Jira API token')

    await user.click(screen.getByRole('button', { name: 'Delete Jira API token' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete Jira API token' })).toHaveFocus())
  })

  it('clears a managed delete confirmation when replacement editing begins', async () => {
    const user = userEvent.setup()
    listNames = ['JIRA_API_TOKEN']
    listManaged = [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }]
    mount()
    await screen.findByText('Jira API token')

    await user.click(screen.getByRole('button', { name: 'Delete Jira API token' }))
    expect(screen.getByText('Permanently delete “Jira API token”? The current value cannot be recovered.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Replace' }))
    await user.type(screen.getByLabelText('Jira API token'), 'replacement-draft')

    expect(screen.queryByText('Permanently delete “Jira API token”? The current value cannot be recovered.')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Jira API token')).toHaveValue('replacement-draft')
    expect(calls.some(call => call.method === 'DELETE')).toBe(false)
  })

  it('labels configured per-host Jira tokens as managed credentials', async () => {
    listNames = ['JIRA_TOKEN_6578616D706C652E636F6D']
    listManaged = [
      {
        name: 'JIRA_TOKEN_6578616D706C652E636F6D',
        kind: 'jira_host_token',
        host: 'example.com',
      },
    ]
    mount()

    expect(await screen.findByText('Jira host API token — example.com')).toBeInTheDocument()
    expect(screen.getByText(/one configured host/)).toBeInTheDocument()
    expect(screen.getByText('JIRA_TOKEN_6578616D706C652E636F6D')).toBeInTheDocument()
  })

  it('explains when a stored global Jira token is inactive in multi-host mode', async () => {
    listNames = ['JIRA_API_TOKEN', 'JIRA_TOKEN_6578616D706C652E636F6D']
    listUnused = [{ name: 'JIRA_API_TOKEN', reason: 'jira_multi_host' }]
    listManaged = [
      {
        name: 'JIRA_TOKEN_6578616D706C652E636F6D',
        kind: 'jira_host_token',
        host: 'example.com',
      },
      {
        name: 'JIRA_TOKEN_6A697261322E6578616D706C652E636F6D',
        kind: 'jira_host_token',
        host: 'jira2.example.com',
      },
    ]
    mount()

    expect(await screen.findByText('JIRA_API_TOKEN')).toBeInTheDocument()
    expect(screen.getByText('Not used by Jira while multiple hosts are configured. Set each host’s credential above, or delete this entry.')).toBeInTheDocument()
  })

  it('explains when a stored per-host Jira token takes precedence over the global token', async () => {
    listNames = ['JIRA_API_TOKEN', 'JIRA_TOKEN_6578616D706C652E636F6D']
    listUnused = [{ name: 'JIRA_API_TOKEN', reason: 'jira_host_precedence' }]
    listManaged = [{
      name: 'JIRA_TOKEN_6578616D706C652E636F6D',
      kind: 'jira_host_token',
      host: 'example.com',
    }]
    mount()

    expect(await screen.findByText('JIRA_API_TOKEN')).toBeInTheDocument()
    expect(screen.getByText('Not used because the host-specific Jira credential takes precedence. Delete this entry if you no longer need the fallback.')).toBeInTheDocument()
  })

  it('sends the session key header on the list request', async () => {
    mount()
    await screen.findByText('No secrets stored yet.')

    const listCall = calls.find(c => c.method === 'GET')
    expect(listCall?.url).toBe('/api/secrets')
  })

  it('renders the vault-only WakaTime credential as managed without an empty Other section', async () => {
    listManaged = [
      { name: 'WAKATIME_API_KEY', kind: 'wakatime_api_key' },
    ]
    mount()

    expect(await screen.findByText('WakaTime API key')).toBeInTheDocument()
    expect(screen.getByText(/coding-activity sync/)).toBeInTheDocument()
    expect(screen.queryByText('Other stored secrets')).not.toBeInTheDocument()
    expect(screen.getByLabelText('WakaTime API key')).toHaveAttribute('type', 'password')
    expect(screen.getByRole('button', { name: 'Save WAKATIME_API_KEY' })).toBeDisabled()
    expect(screen.getByLabelText('WakaTime API key')).toHaveAttribute('placeholder', 'Paste secret value')
    expect(screen.getByRole('link', { name: 'Open WakaTime API key settings' })).toHaveAttribute(
      'href',
      'https://wakatime.com/settings/api-key',
    )
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

  it('explains managed-name collisions and saves through the canonical name', async () => {
    const user = userEvent.setup()
    listManaged = [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }]
    mount()
    await screen.findByText('Used automatically by Kiro Crew')

    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    await user.type(screen.getByLabelText('Secret name'), 'jira_api_token')
    await user.type(screen.getByLabelText('Secret value'), 'jira-token-value')

    expect(screen.getByText('Saving here updates JIRA_API_TOKEN under Used automatically by Kiro Crew.')).toBeInTheDocument()
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeEnabled()
    await user.click(save)
    await waitFor(() => expect(calls.find(call => call.method === 'POST')?.body).toEqual({
      name: 'JIRA_API_TOKEN', value: 'jira-token-value',
    }))
  })

  it('disables a managed row while its save is pending', async () => {
    const user = userEvent.setup()
    let resolvePost: (response: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method ?? 'GET'
        if (method === 'POST') {
          return new Promise<Response>((resolve) => { resolvePost = resolve })
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            names: ['JIRA_API_TOKEN'],
            managed: [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }],
          }),
        } as Response)
      }),
    )
    mount()
    await screen.findByText('Jira API token')

    await user.click(screen.getByRole('button', { name: 'Replace' }))
    const input = screen.getByLabelText('Jira API token')
    await user.type(input, 'submitted-value')
    await user.click(screen.getByRole('button', { name: 'Save JIRA_API_TOKEN' }))

    expect(input).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Show' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Save JIRA_API_TOKEN' })).toBeDisabled()

    resolvePost({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
    } as Response)
  })

  it('blocks an Add that targets a managed name whose row delete is still in flight', async () => {
    // Regression: the Add-save gate only watched the parent mutations, so a
    // managed row's in-flight DELETE could be overtaken by a same-name Add and
    // the delayed DELETE would erase the just-saved credential. The gate now
    // shares pending managed-row names, so the Add stays disabled until the
    // delete settles.
    const user = userEvent.setup()
    let resolveDelete: (response: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method ?? 'GET'
        if (method === 'DELETE') {
          return new Promise<Response>((resolve) => { resolveDelete = resolve })
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            names: ['JIRA_API_TOKEN'],
            managed: [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }],
          }),
        } as Response)
      }),
    )
    mount()
    await screen.findByText('Jira API token')

    // Start deleting the managed row and leave the DELETE pending.
    await user.click(screen.getByRole('button', { name: 'Delete Jira API token' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    // Open Add and type the same canonical name + a value.
    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    await user.type(screen.getByLabelText('Secret name'), 'jira_api_token')
    await user.type(screen.getByLabelText('Secret value'), 'replacement-value')

    // The Add-save button must stay disabled while the row delete is in flight.
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()

    // Once the delete settles, no stray POST should have been sent.
    resolveDelete({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
    } as Response)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled())
    expect(calls.find(call => call.method === 'POST')).toBeUndefined()
  })

  it('freezes a managed row while a parent Add save targeting its canonical name is in flight', async () => {
    // Regression (reverse direction): while the parent Add POST is in flight
    // against a managed canonical name, that row stayed interactive, so a
    // concurrent delete/replace on the row could reorder around the Add and
    // corrupt the credential. The row is now frozen for the duration of the Add.
    const user = userEvent.setup()
    let resolvePost: (response: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method ?? 'GET'
        if (method === 'POST') {
          return new Promise<Response>((resolve) => { resolvePost = resolve })
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            names: ['JIRA_API_TOKEN'],
            managed: [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }],
          }),
        } as Response)
      }),
    )
    mount()
    await screen.findByText('Jira API token')

    // Open Add, type the managed canonical name + value, and start the save.
    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    await user.type(screen.getByLabelText('Secret name'), 'jira_api_token')
    await user.type(screen.getByLabelText('Secret value'), 'add-value')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    // The matching managed row's delete control must be frozen while the Add
    // POST is in flight (the row's own fieldset is disabled).
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Delete Jira API token' })).toBeDisabled(),
    )

    resolvePost({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
    } as Response)
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

    expect(screen.getByText('Permanently delete “MY_API_KEY”? The current value cannot be recovered.')).toBeInTheDocument()
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

    expect(screen.queryByText('Permanently delete “MY_API_KEY”? The current value cannot be recovered.')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete secret MY_API_KEY' })).toBeInTheDocument()
    expect(calls.some(c => c.method === 'DELETE')).toBe(false)
  })

  it('shows an actionable error while preserving the Add path', async () => {
    const user = userEvent.setup()
    listShouldFail = true
    mount()

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load secrets: boom')
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    expect(screen.queryByText('No secrets stored yet.')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    expect(screen.getByLabelText('Secret name')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
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
      expect(screen.getByText('Permanently delete “MY_API_KEY”? The current value cannot be recovered.')).toBeInTheDocument()
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

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('HTTP 400: Secret name must be a string')
    expect(screen.queryByText('No secrets stored yet.')).not.toBeInTheDocument()
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

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not load secrets: HTTP 502')
    expect(alert).not.toHaveTextContent('not json')
    expect(screen.queryByText('No secrets stored yet.')).not.toBeInTheDocument()
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

    // Open ALPHA's confirmation and fire its DELETE (stays pending).
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
    expect(screen.queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
  })

  it('does not offer delete-error handoff while the add form holds a draft', async () => {
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        if ((init?.method ?? 'GET') === 'DELETE') {
          return Promise.resolve({
            ok: false,
            status: 500,
            json: () => Promise.resolve({ error: 'boom' }),
          } as Response)
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ names: ['MY_API_KEY'], managed: [] }),
        } as Response)
      }),
    )
    mount()
    await screen.findByText('MY_API_KEY')

    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    await user.type(screen.getByLabelText('Secret name'), 'UNSAVED_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'unsaved-value')
    await user.click(screen.getByRole('button', { name: 'Delete secret MY_API_KEY' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Secret value')).toHaveValue('unsaved-value')
  })

  it('does not offer page-level handoff while a managed row holds a draft', async () => {
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        if ((init?.method ?? 'GET') === 'DELETE') {
          return Promise.resolve({
            ok: false,
            status: 500,
            json: () => Promise.resolve({ error: 'boom' }),
          } as Response)
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            names: ['MY_API_KEY'],
            managed: [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }],
          }),
        } as Response)
      }),
    )
    mount()
    await screen.findByText('Jira API token')

    await user.type(screen.getByLabelText('Jira API token'), 'unsaved-managed-value')
    await user.click(screen.getByRole('button', { name: 'Delete secret MY_API_KEY' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Jira API token')).toHaveValue('unsaved-managed-value')
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
