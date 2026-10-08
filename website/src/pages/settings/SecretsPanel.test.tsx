import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { SecretsPanel } from './SecretsPanel'
import { isAuthBannerShown, __resetAuthRecoveryStateForTests } from '../../api/client'
import en from '../../i18n/locales/en.json'
// The transport's own recovery copy is hand-authored, so it lives in the manual
// catalog rather than in the codemod-regenerated `en.json`. A repo guard keeps a
// key out of both files at once, so this is the one place it can be read from.
import enManual from '../../i18n/locales/en.manual.json'

/**
 * The panel talks to `/api/secrets` through the shared `api` client, which issues
 * the request with the global `fetch`, so the seam under test is still the global
 * fetch, and every assertion about the REQUEST (method, URL, body, headers) reads
 * the same as before. Each case stubs it with a small router keyed on method + URL
 * rather than a single blanket resolve, because the add and delete paths must be
 * asserted on the request they send, not just on the re-render they cause.
 *
 * Stubs are installed with {@link stubFetch}, never with a bare `vi.stubGlobal`:
 * the transport's failure path reads fields off the response that a hand-written
 * `{ ok, status, json }` literal does not have.
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

/**
 * Fill a partial `Response` literal out to the shape the transport reads.
 *
 * `api/client.ts` reads the auth-challenge HEADER (to decide whether a 403 is a
 * lapsed session) and the body TEXT (to unwrap the backend's message) off the
 * response. A stub carrying only `ok`/`status`/`json` makes the transport throw a
 * TypeError about a missing property, which reaches the card in place of the
 * refusal the server actually sent. Completing it in ONE place is what keeps the
 * next stub in this file from being wrong in the same way.
 *
 * `text` is DERIVED from `json` when a stub does not supply it, including the
 * `.catch('')` -- so a stub whose `json` rejects (a non-JSON error body) yields an
 * empty body, which the transport renders as `HTTP <status>`. That is what this
 * panel's own local guard used to do for the same response.
 *
 * Only ABSENT fields are filled, so a case that needs a real header or a real
 * body text supplies its own and this leaves it alone.
 */
function completeResponse(r: Response, url: string): Response {
  const partial = r as unknown as {
    headers?: unknown
    text?: unknown
    url?: unknown
    json: () => Promise<unknown>
  }
  if (!partial.headers) partial.headers = { get: () => null }
  if (!partial.text) {
    partial.text = () =>
      Promise.resolve()
        .then(() => partial.json())
        .then(body => (typeof body === 'string' ? body : JSON.stringify(body)))
        .catch(() => '')
  }
  if (!partial.url) partial.url = url
  return r
}

/**
 * Install a fetch stub, completing whatever it answers with.
 *
 * Use this instead of `vi.stubGlobal('fetch', vi.fn(...))`: a rejection still
 * propagates untouched (the transport never sees a response at all), and a
 * resolved answer is passed through {@link completeResponse} first.
 */
function stubFetch(
  impl: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> | Response,
) {
  const wrapped = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
    completeResponse(await impl(input, init), String(input)),
  )
  vi.stubGlobal('fetch', wrapped)
  return wrapped
}

function installFetch() {
  return stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
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
    // `ok`/`status` are required: the transport rejects a non-OK response, so a
    // stub without them would read as a failure.
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
    } as Response)
  })
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
    stubFetch(() => new Promise<Response>(() => {}))
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
    expect(screen.getByText('Custom secrets')).toBeInTheDocument()
    expect(screen.getByText(/Reference one as secret:\/\/YOUR_KEY in an MCP server environment or a \.env setting/)).toBeInTheDocument()
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
    expect(screen.getByText('Custom secrets')).toBeInTheDocument()
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
    // The Custom secrets card is always shown (it explains the MCP contract), but
    // with no non-managed entries it shows its empty state rather than a row.
    expect(screen.getByText('Custom secrets')).toBeInTheDocument()
    expect(screen.getByText(/No custom secrets stored yet/)).toBeInTheDocument()
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
    stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
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
    })
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
    stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
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
    })
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
    stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
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
    })
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

describe('SecretsPanel custom secrets card', () => {
  it('always renders the Custom secrets card with the MCP disclaimer, even when empty', async () => {
    // The card and its consumption contract must be visible up front so users
    // understand how a custom secret is referenced, before storing anything.
    mount()
    expect(await screen.findByText('Custom secrets')).toBeInTheDocument()
    // The disclaimer names BOTH supported consumers — MCP server env AND .env
    // credential settings — so an owner whose entry came from `secrets import`
    // is not told it is MCP-only — AND the mediated chat route (#8628).
    expect(screen.getByText(/Encrypted secrets, never shown in chat/)).toBeInTheDocument()
    expect(screen.getByText(/in an MCP server environment or a \.env setting/)).toBeInTheDocument()
    expect(screen.getByText(/authorize a secret for an exact https origin with kirocrew secrets authorize/)).toBeInTheDocument()
    // The token appears in both the disclaimer and the empty-state line.
    expect(screen.getAllByText(/secret:\/\/YOUR_KEY/).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText(/never shown in chat/)).toBeInTheDocument()
    // Empty state, not a row.
    expect(screen.getByText(/No custom secrets stored yet/)).toBeInTheDocument()
    expect(screen.queryByText('••••••••')).not.toBeInTheDocument()
  })

  it('keeps the disclaimer visible while custom entries are populated', async () => {
    listNames = ['WEATHER_API_KEY']
    mount()
    expect(await screen.findByText('WEATHER_API_KEY')).toBeInTheDocument()
    // Disclaimer persists alongside populated rows, and the empty line is gone.
    expect(screen.getByText(/Encrypted secrets, never shown in chat/)).toBeInTheDocument()
    expect(screen.queryByText(/No custom secrets stored yet/)).not.toBeInTheDocument()
    // Value is masked, never rendered in plaintext.
    expect(screen.getByText('••••••••')).toBeInTheDocument()
  })

  it('adds a custom secret through the write-only vault contract', async () => {
    const user = userEvent.setup()
    mount()
    await screen.findByText(/No custom secrets stored yet/)

    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    await user.type(screen.getByLabelText('Secret name'), 'WEATHER_API_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'wk-secret-value')

    // The refetch after the mutation sees the new custom name.
    listNames = ['WEATHER_API_KEY']
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      const post = calls.find(c => c.method === 'POST')
      expect(post?.url).toBe('/api/secrets')
      expect(post?.body).toEqual({ name: 'WEATHER_API_KEY', value: 'wk-secret-value' })
    })
    // The typed value is never echoed back into the DOM.
    expect(screen.queryByText('wk-secret-value')).not.toBeInTheDocument()
  })

  it('deletes a custom secret only after confirmation', async () => {
    const user = userEvent.setup()
    listNames = ['WEATHER_API_KEY']
    mount()
    await screen.findByText('WEATHER_API_KEY')

    await user.click(screen.getByRole('button', { name: 'Delete secret WEATHER_API_KEY' }))
    // A confirmation step gates the destructive action.
    expect(screen.getByText(/Permanently delete/)).toBeInTheDocument()
    expect(calls.some(c => c.method === 'DELETE')).toBe(false)

    await user.click(screen.getByRole('button', { name: 'Delete', exact: true }))
    await waitFor(() => {
      const del = calls.find(c => c.method === 'DELETE')
      expect(del?.url).toBe('/api/secrets/WEATHER_API_KEY')
    })
  })

  it('does not present an empty vault when the list load fails', async () => {
    // Regression: the Custom secrets card always renders, but on a failed list
    // GET otherNames is empty for lack of data — showing "no custom secrets"
    // there would invite re-adding an existing key and overwriting its value.
    // The empty-state line must be suppressed; the error notice explains it.
    listShouldFail = true
    mount()

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load secrets')
    // The card still renders its title + disclaimer, but NOT the empty-state line.
    expect(screen.getByText('Custom secrets')).toBeInTheDocument()
    expect(screen.queryByText(/No custom secrets stored yet/)).not.toBeInTheDocument()
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
    stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
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
    })
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
    stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
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
    })
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
    stubFetch(() =>
      Promise.resolve({
        ok: false,
        status: 400,
        json: () => Promise.resolve({ error: 'Secret name must be a string' }),
      } as Response),
    )
    mount()

    const alert = await screen.findByRole('alert')
    // The backend's sentence, with no status code in front of it. The transport
    // renders `HTTP <status>` only for a refusal that carried no human message,
    // so a refusal that DID carry one now reads as the sentence alone -- the same
    // rule every other panel's failures follow.
    expect(alert).toHaveTextContent('Could not load secrets: Secret name must be a string')
    expect(screen.queryByText('No secrets stored yet.')).not.toBeInTheDocument()
  })

  it('rejects a non-OK response whose body is not JSON', async () => {
    stubFetch(() =>
      Promise.resolve({
        ok: false,
        status: 502,
        json: () => Promise.reject(new SyntaxError('not json')),
      } as unknown as Response),
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
    stubFetch((_input: RequestInfo | URL, init?: RequestInit) => {
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
    })
    mount()
    await screen.findByText('No secrets stored yet.')

    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    await user.type(screen.getByLabelText('Secret name'), 'MY_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'sk-abc123')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    // The alert carries the backend prose the transport unwrapped. The status
    // digits are deliberately no longer asserted: a refusal that sent a message
    // is shown as that message, and the status still travels on the `ApiError`
    // and into the error journal for diagnostics.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not save secret')
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
    stubFetch((_input: RequestInfo | URL, init?: RequestInit) => {
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
    })
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
    stubFetch((_input: RequestInfo | URL, init?: RequestInit) => {
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
    })
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
    stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
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
    })
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
    stubFetch((_input: RequestInfo | URL, init?: RequestInit) => {
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
    })
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
    stubFetch((_input: RequestInfo | URL, init?: RequestInit) => {
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
    })
    mount()
    await screen.findByText('MY_API_KEY')

    await user.click(screen.getByRole('button', { name: 'Delete secret MY_API_KEY' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not delete secret')
    // Same as the save case: the unwrapped sentence, not the status digits.
    expect(alert).toHaveTextContent('boom')
    expect(screen.queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
  })

  it('does not offer delete-error handoff while the add form holds a draft', async () => {
    const user = userEvent.setup()
    stubFetch((_input: RequestInfo | URL, init?: RequestInit) => {
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
    })
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
    stubFetch((_input: RequestInfo | URL, init?: RequestInit) => {
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
    })
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
    stubFetch((_input: RequestInfo | URL, init?: RequestInit) => {
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
    })
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
    stubFetch((_input: RequestInfo | URL, init?: RequestInit) => {
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
    })
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

/**
 * What an EXPIRED session sees (issue #12240).
 *
 * The panel used to issue its own `fetch` behind a local `!r.ok` guard, which
 * reaches none of the shared transport's session-expiry recovery: no silent
 * cookie refresh, no re-auth banner, and an error message built from the
 * gateway's own reason text. A signed-out user was therefore told
 * `Could not save secret: HTTP 403: <cryptographic reason>` and offered a retry
 * that could not succeed, while every sibling settings panel raised the banner.
 *
 * These cases assert the observable halves of that recovery, each produced only
 * by going through the client: the refresh attempt, the banner in the document,
 * and the sign-in instruction on the card. The last two cases are the controls
 * that keep the fix from being a blanket re-auth prompt on any refusal.
 */
describe('SecretsPanel on an expired session', () => {
  /** The auth challenge the gateway answers an API call with once the dashboard
   *  session no longer authenticates: 403 carrying `X-Auth-Required`, and a body
   *  whose `error` names the CRYPTOGRAPHIC reason -- accurate, and useless to a
   *  user, which is why the transport substitutes its own message. */
  const AUTH_CHALLENGE = { error: 'invalid signature', code: 'forbidden' }

  /** A `Response` whose header read answers from *headers*. Written out here
   *  rather than left to `completeResponse` because these cases are about the
   *  header, so supplying it is the point. */
  function authDenied(body: unknown) {
    return {
      ok: false,
      status: 403,
      headers: { get: (k: string) => (k === 'X-Auth-Required' ? 'true' : null) },
      json: () => Promise.resolve(body),
      text: () => Promise.resolve(JSON.stringify(body)),
    } as unknown as Response
  }

  /**
   * The list GET succeeds so the Add form is reachable; the save POST is denied
   * with the auth challenge; the silent refresh that follows comes back 401.
   *
   * 401 is what takes the recovery all the way to the banner -- a TRANSIENT
   * refresh failure deliberately does NOT banner, so it is the terminal answer
   * that makes the end of the pipeline observable.
   */
  function denySaveWithExhaustedRefresh() {
    return stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/auth/refresh') {
        return Promise.resolve({
          ok: false,
          status: 401,
          json: () => Promise.reject(new SyntaxError('revoked')),
        } as unknown as Response)
      }
      if (method === 'POST' && url === '/api/secrets') {
        return Promise.resolve(authDenied(AUTH_CHALLENGE))
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ names: [], managed: [] }),
      } as Response)
    })
  }

  const banner = () => document.getElementById('mc-session-expired')

  /** Fill the Add form and submit it, which is the click the issue names. */
  async function save(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole('button', { name: 'Add secret' }))
    await user.type(screen.getByLabelText('Secret name'), 'MY_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'sk-abc123')
    await user.click(screen.getByRole('button', { name: 'Save' }))
  }

  beforeEach(() => {
    __resetAuthRecoveryStateForTests()
  })

  afterEach(() => {
    __resetAuthRecoveryStateForTests()
    vi.unstubAllGlobals()
  })

  it('attempts the silent refresh instead of failing the save outright', async () => {
    const user = userEvent.setup()
    const fetchMock = denySaveWithExhaustedRefresh()
    mount()
    await screen.findByText('No secrets stored yet.')

    await save(user)

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith('/api/auth/refresh', expect.anything()),
    )
  })

  it('raises the re-auth banner once the refresh comes back terminal', async () => {
    const user = userEvent.setup()
    denySaveWithExhaustedRefresh()
    mount()
    await screen.findByText('No secrets stored yet.')

    await save(user)

    await waitFor(() => expect(banner()).not.toBeNull())
    expect(isAuthBannerShown()).toBe(true)
    // The banner is only worth raising for what it carries: the command that
    // mints a fresh token, and somewhere to paste the result.
    expect(banner()?.textContent).toContain('kirocrew token')
    expect(banner()?.querySelector('input')).not.toBeNull()
  })

  it('names signing in on the card, not the gateway reason', async () => {
    const user = userEvent.setup()
    denySaveWithExhaustedRefresh()
    mount()
    await screen.findByText('No secrets stored yet.')

    await save(user)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(enManual.api.client.session_expired_sign_in_again)
    // Not the cryptographic reason, which describes HMAC verification and names
    // nothing the user can do -- and which is exactly what the panel's own local
    // guard put on the card.
    expect(alert).not.toHaveTextContent('invalid signature')
    // The panel's frame stays: it says WHICH action failed and prescribes
    // nothing, so it does not compete with the recovery instruction inside it.
    expect(alert.textContent).toContain(en.settings.secrets.save_error.split('{{')[0].trim())
  })

  it('keeps the typed secret in the form through the denial', async () => {
    // The data-loss guard has to survive the move onto the shared transport: the
    // value is unrecoverable, and a session the user can still repair is the
    // worst moment to discard it.
    const user = userEvent.setup()
    denySaveWithExhaustedRefresh()
    mount()
    await screen.findByText('No secrets stored yet.')

    await save(user)
    await screen.findByRole('alert')

    expect(screen.getByLabelText('Secret name')).toHaveValue('MY_KEY')
    expect(screen.getByLabelText('Secret value')).toHaveValue('sk-abc123')
  })

  it('keeps a non-auth 403 on its own prose, with no banner', async () => {
    // An ordinary permission denial is a 403 too, and it carries NO
    // `X-Auth-Required`. Its sentence already names the remedy, and a re-auth
    // banner beside it would send the user to fix something that is not broken.
    // The transport keys on the header rather than the status, and this is the
    // control that proves it.
    const user = userEvent.setup()
    const refused = { error: 'Secrets are read-only on a remote dashboard.', code: 'read_only_remote' }
    stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET') === 'POST' && String(input) === '/api/secrets') {
        return Promise.resolve({
          ok: false,
          status: 403,
          json: () => Promise.resolve(refused),
        } as Response)
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ names: [], managed: [] }),
      } as Response)
    })
    mount()
    await screen.findByText('No secrets stored yet.')

    await save(user)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(refused.error)
    expect(alert).not.toHaveTextContent(enManual.api.client.session_expired_sign_in_again)
    expect(banner()).toBeNull()
    expect(isAuthBannerShown()).toBe(false)
  })

  it('clears a banner left by an earlier lapse when a later save succeeds', async () => {
    const user = userEvent.setup()
    denySaveWithExhaustedRefresh()
    const view = mount()
    await screen.findByText('No secrets stored yet.')
    await save(user)
    await waitFor(() => expect(banner()).not.toBeNull())
    view.unmount()

    installFetch()
    mount()
    await screen.findByText('No secrets stored yet.')

    await waitFor(() => expect(banner()).toBeNull())
    expect(isAuthBannerShown()).toBe(false)
  })

  /**
   * The remaining two of the three requests the issue names. Save is the one the
   * acceptance criteria describe and is covered in full above; these pin that the
   * list and delete requests reach the same recovery, so a later change that moves
   * one of them back onto a raw `fetch` fails here rather than being found by a
   * signed-out user.
   */
  it('raises the banner and names signing in when the LIST is denied', async () => {
    stubFetch((input: RequestInfo | URL) =>
      String(input) === '/api/auth/refresh'
        ? Promise.resolve({
            ok: false,
            status: 401,
            json: () => Promise.reject(new SyntaxError('revoked')),
          } as unknown as Response)
        : Promise.resolve(authDenied(AUTH_CHALLENGE)),
    )
    mount()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(enManual.api.client.session_expired_sign_in_again)
    expect(alert).not.toHaveTextContent('invalid signature')
    await waitFor(() => expect(banner()).not.toBeNull())
  })

  it('raises the banner and names signing in when the DELETE is denied', async () => {
    const user = userEvent.setup()
    stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/auth/refresh') {
        return Promise.resolve({
          ok: false,
          status: 401,
          json: () => Promise.reject(new SyntaxError('revoked')),
        } as unknown as Response)
      }
      if ((init?.method ?? 'GET') === 'DELETE') {
        return Promise.resolve(authDenied(AUTH_CHALLENGE))
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ names: ['MY_API_KEY'], managed: [] }),
      } as Response)
    })
    mount()
    await screen.findByText('MY_API_KEY')

    await user.click(screen.getByRole('button', { name: 'Delete secret MY_API_KEY' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(enManual.api.client.session_expired_sign_in_again)
    expect(alert).not.toHaveTextContent('invalid signature')
    await waitFor(() => expect(banner()).not.toBeNull())
    // The secret is still listed: a denied delete must not read as a removal.
    expect(screen.getByText('MY_API_KEY')).toBeInTheDocument()
  })

  /**
   * The draft survives the RECOVERY, not just the denial (issue #12240 review).
   *
   * Preserving the form through a refused save is only half the promise. The
   * re-auth banner used to submit its pasted token with a full-page navigation,
   * which discarded the draft on the way to fixing the session -- so the panel
   * handed the value back and then the recovery threw it away. The banner now
   * exchanges the token against `/api/auth/me?token=...` instead, which
   * authenticates by the same mechanism (the gateway's auth middleware reads a
   * query token ahead of the cookie and writes the session cookie onto the
   * response) without leaving the page.
   */
  function grantOnExchange() {
    return stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url.startsWith('/api/auth/me')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) } as Response)
      }
      if (url === '/api/auth/refresh') {
        return Promise.resolve({
          ok: false,
          status: 401,
          json: () => Promise.reject(new SyntaxError('revoked')),
        } as unknown as Response)
      }
      if (method === 'POST' && url === '/api/secrets') {
        return Promise.resolve(authDenied(AUTH_CHALLENGE))
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ names: [], managed: [] }),
      } as Response)
    })
  }

  /** Paste *token* into the banner and press Enter. */
  function pasteToken(token: string) {
    const field = banner()!.querySelector('input') as HTMLInputElement
    field.value = token
    field.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
  }

  it('keeps the typed secret through the re-auth, which no longer reloads', async () => {
    const user = userEvent.setup()
    const fetchMock = grantOnExchange()
    mount()
    await screen.findByText('No secrets stored yet.')

    await save(user)
    await waitFor(() => expect(banner()).not.toBeNull())

    pasteToken('fresh-token')

    // The exchange goes through the API, not through a navigation.
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith('/api/auth/me?token=fresh-token', expect.anything()),
    )
    // The banner clears itself once the session is live again.
    await waitFor(() => expect(banner()).toBeNull())
    // And the whole point: the draft is still on the form, so one click completes
    // the save the lapsed session refused.
    expect(screen.getByLabelText('Secret name')).toHaveValue('MY_KEY')
    expect(screen.getByLabelText('Secret value')).toHaveValue('sk-abc123')
  })

  it('never writes the secret to localStorage or sessionStorage', async () => {
    // A regression guard against the rejected alternative, not a restatement of
    // the test above: the other way to survive a reload is to persist the draft,
    // which would put a plaintext credential in web storage. This fails if anyone
    // implements that, and it scans every key and value rather than a known key so
    // a differently-named implementation cannot slip past it.
    const user = userEvent.setup()
    grantOnExchange()
    mount()
    await screen.findByText('No secrets stored yet.')

    await save(user)
    await waitFor(() => expect(banner()).not.toBeNull())
    pasteToken('fresh-token')
    await waitFor(() => expect(banner()).toBeNull())

    for (const store of [window.localStorage, window.sessionStorage]) {
      const contents: string[] = []
      for (let i = 0; i < store.length; i++) {
        const k = store.key(i)
        if (k === null) continue
        contents.push(k, store.getItem(k) ?? '')
      }
      const joined = contents.join('\u0000')
      expect(joined).not.toContain('sk-abc123')
      expect(joined).not.toContain('fresh-token')
    }
  })

  it('keeps the banner up when the exchange is refused, and re-enables the field', async () => {
    // A rejected token must not read as recovery: the banner is the only way back,
    // so clearing it on a failed exchange would strand the user. The field is
    // re-enabled with its text intact so a mistyped paste can be corrected.
    const user = userEvent.setup()
    stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.startsWith('/api/auth/me')) {
        return Promise.resolve({
          ok: false,
          status: 403,
          json: () => Promise.resolve({ error: 'invalid signature' }),
        } as Response)
      }
      if (url === '/api/auth/refresh') {
        return Promise.resolve({
          ok: false,
          status: 401,
          json: () => Promise.reject(new SyntaxError('revoked')),
        } as unknown as Response)
      }
      if ((init?.method ?? 'GET') === 'POST' && url === '/api/secrets') {
        return Promise.resolve(authDenied(AUTH_CHALLENGE))
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ names: [], managed: [] }),
      } as Response)
    })
    mount()
    await screen.findByText('No secrets stored yet.')

    await save(user)
    await waitFor(() => expect(banner()).not.toBeNull())
    pasteToken('stale-token')

    const field = () => banner()?.querySelector('input') as HTMLInputElement | undefined
    await waitFor(() => expect(field()?.disabled).toBe(false))
    expect(banner()).not.toBeNull()
    expect(field()?.value).toBe('stale-token')
    // The draft is untouched either way.
    expect(screen.getByLabelText('Secret value')).toHaveValue('sk-abc123')
  })

  it('drops the stale sign-in card once auth is restored, keeping the draft', async () => {
    const user = userEvent.setup()
    denySaveWithExhaustedRefresh()
    mount()
    await screen.findByText('No secrets stored yet.')

    await save(user)
    // The card is right while the session is down: it names the failure and
    // tells the reader to use the banner above.
    const card = await screen.findByText(/Could not save secret/)
    expect(card.textContent).toContain('paste the sign-in URL into the banner')

    // Auth genuinely comes back. `mc-auth-recovered` is emitted only from
    // `removeAuthBanner`, whose every caller is gated on a 2xx or an accepted
    // token exchange -- NOT `mc-auth-cleared`, which the banner's own X also
    // emits while the session is still broken. From here the card is describing
    // a session that no longer exists and pointing at a banner that is gone, so
    // it has to go.
    await act(async () => {
      window.dispatchEvent(new CustomEvent('mc-auth-recovered'))
    })
    await waitFor(() => {
      expect(screen.queryByText(/Could not save secret/)).toBeNull()
    })
    // What the user typed is still theirs. Clearing a stale ERROR must not be a
    // back door to clearing the draft the whole fix exists to protect.
    expect(screen.getByLabelText('Secret name')).toHaveValue('MY_KEY')
    expect(screen.getByLabelText('Secret value')).toHaveValue('sk-abc123')
  })

  it('keeps the sign-in card when the banner is only dismissed', async () => {
    const user = userEvent.setup()
    denySaveWithExhaustedRefresh()
    mount()
    await screen.findByText('No secrets stored yet.')

    await save(user)
    await waitFor(() => expect(banner()).not.toBeNull())
    await screen.findByText(/Could not save secret/)

    // Drive the banner's REAL dismiss button, not a synthetic event: the whole
    // defect lived in the chain from that click to this card, so a dispatched
    // event would skip the part under test. The X tears the banner down inline
    // and emits `mc-auth-cleared` -- it never calls `removeAuthBanner`, so no
    // `mc-auth-recovered` is emitted and nothing has authenticated.
    const dismiss = Array.from(banner()?.querySelectorAll('button') ?? []).find(
      b => b.textContent === '✕',
    )
    expect(dismiss).toBeDefined()
    await act(async () => {
      dismiss?.click()
    })
    // Precondition, not the claim: prove the click landed. Without this the test
    // would also pass when the button was never found and nothing happened.
    await waitFor(() => expect(banner()).toBeNull())

    // Settle a full render cycle, for the same reason the sibling case does: an
    // unguarded reset lands on the NEXT render.
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 50))
    })
    // The session is still broken, so the card is still true and must stay.
    expect(screen.getByText(/Could not save secret/)).toBeInTheDocument()
  })

  it('leaves a failure that is still true on screen', async () => {
    const user = userEvent.setup()
    // A plain refusal with no auth challenge: nothing about it is resolved by
    // signing in, so even a REAL `mc-auth-recovered` must not erase it. The
    // event is deliberately the recovery one, not `mc-auth-cleared`: the hook no
    // longer listens to `mc-auth-cleared` at all, so dispatching that here would
    // pass whatever `isAuthExpiredError` did and test nothing.
    stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET') === 'POST' && url === '/api/secrets') {
        return Promise.resolve({
          ok: false,
          status: 500,
          headers: { get: () => null },
          json: () => Promise.resolve({ error: 'disk full' }),
          text: () => Promise.resolve('{"error":"disk full"}'),
        } as unknown as Response)
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ names: [], managed: [] }),
      } as Response)
    })
    mount()
    await screen.findByText('No secrets stored yet.')

    await save(user)
    await screen.findByText(/Could not save secret/)
    await act(async () => {
      window.dispatchEvent(new CustomEvent('mc-auth-recovered'))
    })
    // Settle a full render cycle before looking. Asserting straight after the
    // dispatch proves nothing: an unguarded reset lands on the NEXT render, so
    // the card is still on screen at that instant either way and the case passes
    // even when the reset is wrong. Waiting is what makes it discriminate --
    // with the guard removed, this find now fails.
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 50))
    })
    expect(screen.getByText(/Could not save secret/)).toBeInTheDocument()
  })
})
