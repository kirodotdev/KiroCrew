import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation, type Location } from 'react-router-dom'
import { SecretsPanel } from './SecretsPanel'
import { SUBNAV_PUSH_STATE } from '../../components/subNavParams'
import { isAuthBannerShown, __resetAuthRecoveryStateForTests } from '../../api/client'
import en from '../../i18n/locales/en.json'
// The transport's own recovery copy is hand-authored, so it lives in the manual
// catalog rather than in the codemod-regenerated `en.json`. A repo guard keeps a
// key out of both files at once, so this is the one place it can be read from.
import enManual from '../../i18n/locales/en.manual.json'

// SettingsSubNav measures its container; a fixed wide width keeps every case in
// the two-pane layout, where the rail and the selected pane are both on screen.
vi.mock('../../hooks/useContainerWidth', () => ({
  useContainerWidth: () => [{ current: null }, 1200],
}))
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => false, useIsNarrowViewport: () => false }))
// The switch's gateway write the list waits for: settled unless a case holds it.
let previewSaved: Promise<void> = Promise.resolve()
vi.mock('../../hooks/usePreviewFlagServerSync', () => ({ whenPreviewFlagSaved: () => previewSaved }))

/**
 * The panel talks to `/api/secrets` through the shared `api` client, which issues
 * the request with the global `fetch`, so the seam under test is the global fetch,
 * and every assertion about the REQUEST (method, URL, body, headers) reads off it.
 * Each case stubs it with a small router keyed on method + URL rather than a
 * single blanket resolve, because the add and delete paths must be asserted on
 * the request they send, not just on the re-render they cause.
 *
 * Stubs are installed with {@link stubFetch}, never with a bare `vi.stubGlobal`:
 * the transport's failure path reads fields off the response that a hand-written
 * `{ ok, status, json }` literal does not have.
 */
type FetchCall = { url: string; method: string; body?: unknown; headers?: Record<string, string> }
type ManagedSecret = {
  name: string
  kind: 'jira_api_token' | 'jira_host_token' | 'wakatime_api_key' | 'connections_client_secret' | 'wakatime_oauth'
  host?: string
}
type ListItem = {
  name: string
  type: 'secret' | 'login' | 'note'
  revision: string
  updated_at?: string
  fields?: Record<string, unknown>
  corrupt?: boolean
}

let calls: FetchCall[] = []
/** Names and managed catalog entries returned by the list endpoint. */
let listNames: string[] = []
let listManaged: ManagedSecret[] = []
/** Typed rows; a name in `listNames` with no row here lists as a plain secret. */
let listItems: ListItem[] = []
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
 * refusal the server actually sent. `text` is DERIVED from `json` when a stub
 * does not supply it, including the `.catch('')`, so a stub whose `json` rejects
 * yields an empty body, which the transport renders as `HTTP <status>`. Only
 * ABSENT fields are filled.
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

/** Install a fetch stub, completing whatever it answers with. */
function stubFetch(
  impl: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> | Response,
) {
  const wrapped = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
    completeResponse(await impl(input, init), String(input)),
  )
  vi.stubGlobal('fetch', wrapped)
  return wrapped
}

function listBody() {
  return {
    names: listNames,
    items: listNames.map(
      name => listItems.find(item => item.name === name) ?? { name, type: 'secret', revision: 'r-' + name },
    ),
    managed: listManaged,
    ...(listUnused.length ? { unused: listUnused } : {}),
    ...(listManagedError ? { managed_error: listManagedError } : {}),
  }
}

function okJson(body: unknown): Response {
  return { ok: true, status: 200, json: () => Promise.resolve(body) } as Response
}

function recordCall(input: RequestInfo | URL, init?: RequestInit): FetchCall {
  let headers: Record<string, string> | undefined
  if (init?.headers) {
    if (init.headers instanceof Headers) {
      headers = {}
      init.headers.forEach((v, k) => { headers![k] = v })
    } else {
      headers = { ...(init.headers as Record<string, string>) }
    }
  }
  const call: FetchCall = {
    url: String(input),
    method: init?.method ?? 'GET',
    body: init?.body ? JSON.parse(String(init.body)) : undefined,
    headers,
  }
  calls.push(call)
  return call
}

function installFetch() {
  return stubFetch((input, init) => {
    const { url, method } = recordCall(input, init)
    if (method === 'GET' && url === '/api/secrets') {
      if (listShouldFail) return Promise.reject(new Error('boom'))
      return Promise.resolve(okJson(listBody()))
    }
    // POST /api/secrets and DELETE /api/secrets/:name both just acknowledge.
    return Promise.resolve(okJson({ ok: true }))
  })
}

let lastLocation: Location | null = null
function LocationProbe() {
  lastLocation = useLocation()
  return null
}

function mount(url: string | Partial<Location> = '/settings') {
  // `retry: false` so the error case settles on the first rejection instead of
  // outliving the test timeout on react-query's default backoff.
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const utils = render(
    <MemoryRouter initialEntries={[url]}>
      <QueryClientProvider client={qc}>
        <SecretsPanel />
      </QueryClientProvider>
      <LocationProbe />
    </MemoryRouter>,
  )
  return { qc, ...utils }
}

const S = en.settings.secrets
/** Keys the codemod does not extract live in the manual catalog. */
const M = enManual.settings.secrets
const LOGIN_FIELDS = { username: 'octocat', urls: ['https://github.com'] }

/** The rail row for *name* (a listbox option) by its visible label. */
function row(name: string) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return screen.getByRole('option', { name: new RegExp(`^${escaped}\\b`) })
}

async function openAdd(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('option', { name: /Add secret/ }))
  await screen.findByLabelText('Secret name')
}

beforeEach(() => {
  previewSaved = Promise.resolve()
  calls = []
  listNames = []
  listManaged = []
  listItems = []
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

describe('SecretsPanel rail', () => {
  it('shows the loading line while the list query is in flight', () => {
    stubFetch(() => new Promise<Response>(() => {}))
    mount()
    expect(screen.getByText(S.loading)).toBeInTheDocument()
  })

  it('opens on the Add pane when the vault is empty', async () => {
    mount()
    expect(await screen.findByLabelText('Secret name')).toBeInTheDocument()
  })

  it('reads the list only after the preview switch has reached the gateway', async () => {
    // Read before the write lands, the gateway still lists names only, which
    // would show every login as a plain secret.
    let release: () => void = () => {}
    previewSaved = new Promise<void>(resolve => { release = resolve })
    listNames = ['GITHUB']
    listItems = [{ name: 'GITHUB', type: 'login', revision: 'r1', fields: { username: 'octo', urls: [] } }]
    mount()
    await act(async () => {})
    expect(calls.some(c => c.method === 'GET' && c.url === '/api/secrets')).toBe(false)
    expect(screen.getByText(S.loading)).toBeInTheDocument()
    await act(async () => release())
    expect(await screen.findByRole('option', { name: /GITHUB/ })).toHaveTextContent('octo')
    expect(calls.some(c => c.method === 'GET' && c.url === '/api/secrets')).toBe(true)
  })

  it('groups managed credentials apart from custom items and shows a status per row', async () => {
    listNames = ['JIRA_API_TOKEN', 'OPENAI_API_KEY', 'GITHUB']
    listManaged = [
      { name: 'JIRA_API_TOKEN', kind: 'jira_api_token' },
      { name: 'WAKATIME_API_KEY', kind: 'wakatime_api_key' },
    ]
    listItems = [{ name: 'GITHUB', type: 'login', revision: 'r1', fields: LOGIN_FIELDS }]
    mount()
    await screen.findByRole('option', { name: /OPENAI_API_KEY/ })
    expect(screen.getByText(M.managed_title.replace('{{productName}}', 'Kiro Crew'))).toBeInTheDocument()
    expect(screen.getByText(M.custom_title)).toBeInTheDocument()
    expect(row('JIRA_API_TOKEN')).toHaveTextContent(S.status_set)
    expect(row('WAKATIME_API_KEY')).toHaveTextContent(S.status_not_set)
    expect(row('GITHUB')).toHaveTextContent('octocat')
  })

  it('marks a managed credential the runtime will not use', async () => {
    listNames = ['WAKATIME_API_KEY']
    listManaged = [{ name: 'WAKATIME_API_KEY', kind: 'wakatime_api_key' }]
    listUnused = [{ name: 'WAKATIME_API_KEY', reason: 'wakatime_disabled' }]
    mount()
    expect(await screen.findByRole('option', { name: /WAKATIME_API_KEY/ })).toHaveTextContent(S.status_unused)
  })

  it('marks an unused stored credential the managed catalog leaves out, and explains it in its pane', async () => {
    // The real backend: WakaTime disabled lists the stored key in `unused` and
    // drops it from `managed`, so it rails as a custom item.
    const user = userEvent.setup()
    listNames = ['WAKATIME_API_KEY']
    listUnused = [{ name: 'WAKATIME_API_KEY', reason: 'wakatime_disabled' }]
    mount()
    const option = await screen.findByRole('option', { name: /WAKATIME_API_KEY/ })
    expect(option).toHaveTextContent(S.status_unused)
    await user.click(option)
    expect(await screen.findByText(M.wakatime_unused_hint)).toBeInTheDocument()
  })

  it('keeps the Add path when the list fails to load, and offers the hand-off', async () => {
    listShouldFail = true
    mount()
    expect(await screen.findByText(/Could not load secrets: boom/)).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /Add secret/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('shows a non-fatal managed config warning while keeping stored names', async () => {
    listNames = ['TEST_KEY']
    listManagedError = true
    mount()
    expect(await screen.findByText(M.managed_config_error)).toBeInTheDocument()
    expect(row('TEST_KEY')).toBeInTheDocument()
  })

  it('sends the session key header on the list request', async () => {
    mount()
    await screen.findByRole('option', { name: /Add secret/ })
    const get = calls.find(c => c.method === 'GET' && c.url === '/api/secrets')
    expect(get?.headers?.['X-Session-Key']).toBe('dashboard:ui')
  })
})

describe('SecretsPanel add pane', () => {
  it('keeps Save disabled until the name and the value are filled, then POSTs the trimmed name', async () => {
    const user = userEvent.setup()
    mount()
    await openAdd(user)
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    await user.type(screen.getByLabelText('Secret name'), '  MY_KEY  ')
    expect(save).toBeDisabled()
    await user.type(screen.getByLabelText('Secret value'), 'sk-abc')
    expect(save).toBeEnabled()
    await user.click(save)
    await waitFor(() => expect(calls.some(c => c.method === 'POST')).toBe(true))
    expect(calls.find(c => c.method === 'POST')?.body).toEqual({
      name: 'MY_KEY',
      type: 'secret',
      fields: { value: 'sk-abc' },
    })
  })

  it('refuses a name with # and says why', async () => {
    const user = userEvent.setup()
    mount()
    await openAdd(user)
    await user.type(screen.getByLabelText('Secret name'), 'A#B')
    await user.type(screen.getByLabelText('Secret value'), 'v')
    expect(screen.getByText(S.name_no_hash)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('previews the reference the name will be used by', async () => {
    const user = userEvent.setup()
    mount()
    await openAdd(user)
    await user.type(screen.getByLabelText('Secret name'), 'GITHUB')
    expect(screen.getByText('secret://GITHUB')).toBeInTheDocument()
  })

  it('stores a login with its fields and websites', async () => {
    const user = userEvent.setup()
    mount()
    await openAdd(user)
    await user.click(screen.getByRole('radio', { name: /Login/ }))
    await user.type(screen.getByLabelText('Secret name'), 'GITHUB')
    await user.type(screen.getByLabelText(S.username_label), 'octocat')
    await user.type(screen.getByLabelText(S.password_label), 'pw-1')
    await user.type(screen.getByLabelText(S.add_website), 'https://github.com{Enter}')
    expect(screen.getByText('https://github.com')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some(c => c.method === 'POST')).toBe(true))
    expect(calls.find(c => c.method === 'POST')?.body).toEqual({
      name: 'GITHUB',
      type: 'login',
      fields: {
        username: 'octocat',
        password: 'pw-1',
        urls: ['https://github.com'],
        notes: '',
      },
    })
  })

  it('saves a website typed but not yet added', async () => {
    const user = userEvent.setup()
    mount()
    await openAdd(user)
    await user.click(screen.getByRole('radio', { name: /Login/ }))
    await user.type(screen.getByLabelText('Secret name'), 'GITHUB')
    await user.type(screen.getByLabelText(S.password_label), 'pw-1')
    await user.type(screen.getByLabelText(S.add_website), '  https://github.com  ')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some(c => c.method === 'POST')).toBe(true))
    expect(calls.find(c => c.method === 'POST')?.body).toMatchObject({
      type: 'login',
      fields: { urls: ['https://github.com'] },
    })
  })

  it('holds the name and type while a save and its refetch run', async () => {
    // A save writes to the typed name and then opens it; an edit of the name
    // made meanwhile would be dropped by that navigation.
    const user = userEvent.setup()
    let release: () => void = () => {}
    stubFetch((input, init) => {
      const { url, method } = recordCall(input, init)
      if (method === 'GET' && url === '/api/secrets') return okJson(listBody())
      if (method === 'POST') return new Promise<Response>(resolve => { release = () => resolve(okJson({ ok: true })) })
      return okJson({ ok: true })
    })
    mount()
    await openAdd(user)
    await user.type(screen.getByLabelText('Secret name'), 'MY_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'sk-abc')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some(c => c.method === 'POST')).toBe(true))
    expect(screen.getByLabelText('Secret name')).toBeDisabled()
    expect(screen.getByRole('radio', { name: /Login/ })).toBeDisabled()
    listNames = ['MY_KEY']
    await act(async () => release())
  })

  it('stores a note as text', async () => {
    const user = userEvent.setup()
    mount()
    await openAdd(user)
    await user.click(screen.getByRole('radio', { name: /Note/ }))
    await user.type(screen.getByLabelText('Secret name'), 'WIFI')
    await user.type(screen.getByLabelText(S.text_label), 'ssid=home')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(calls.find(c => c.method === 'POST')?.body).toEqual({
        name: 'WIFI',
        type: 'note',
        fields: { text: 'ssid=home' },
      }),
    )
  })

  it('explains a managed-name collision and saves through the canonical name', async () => {
    const user = userEvent.setup()
    listManaged = [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }]
    mount()
    await openAdd(user)
    await user.type(screen.getByLabelText('Secret name'), 'jira_api_token')
    expect(screen.getByText(/Saving here updates JIRA_API_TOKEN/)).toBeInTheDocument()
    await user.type(screen.getByLabelText('Secret value'), 'tok')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.find(c => c.method === 'POST')?.body).toMatchObject({ name: 'JIRA_API_TOKEN' }))
  })

  it('keeps the typed value and shows the error when the POST is rejected', async () => {
    const user = userEvent.setup()
    stubFetch((input, init) => {
      const { method } = recordCall(input, init)
      if (method === 'POST') {
        return Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({ error: 'disk full' }) } as Response)
      }
      return Promise.resolve(okJson(listBody()))
    })
    mount()
    await openAdd(user)
    await user.type(screen.getByLabelText('Secret name'), 'MY_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'sk-abc')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/Could not save secret: disk full/)).toBeInTheDocument()
    expect(screen.getByLabelText('Secret value')).toHaveValue('sk-abc')
    // No hand-off next to an unsaved draft.
    expect(screen.queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
  })

  it('does not send a second POST on a double-click', async () => {
    const user = userEvent.setup()
    let release: () => void = () => {}
    stubFetch((input, init) => {
      const { method } = recordCall(input, init)
      if (method === 'POST') {
        return new Promise<Response>(resolve => {
          release = () => resolve(okJson({ ok: true }))
        })
      }
      return Promise.resolve(okJson(listBody()))
    })
    mount()
    await openAdd(user)
    await user.type(screen.getByLabelText('Secret name'), 'MY_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'sk-abc')
    const save = screen.getByRole('button', { name: 'Save' })
    await user.dblClick(save)
    await waitFor(() => expect(save).toBeDisabled())
    expect(calls.filter(c => c.method === 'POST')).toHaveLength(1)
    await act(async () => release())
  })
})

describe('SecretsPanel item pane', () => {
  it("shows a login's public fields and its references, never a credential", async () => {
    const user = userEvent.setup()
    listNames = ['GITHUB']
    listItems = [{ name: 'GITHUB', type: 'login', revision: 'r1', updated_at: '2026-10-01T00:00:00Z', fields: LOGIN_FIELDS }]
    mount()
    await user.click(await screen.findByRole('option', { name: /GITHUB/ }))
    expect(await screen.findByRole('heading', { name: 'GITHUB' })).toBeInTheDocument()
    // The rail row and the pane both name the user; the pane's is the definition term's value.
    expect(screen.getAllByText('octocat')).toHaveLength(2)
    expect(screen.getByText('https://github.com')).toBeInTheDocument()
    expect(screen.getByText('secret://GITHUB')).toBeInTheDocument()
    expect(screen.getByText('secret://GITHUB#username')).toBeInTheDocument()
    // A login keeps no one-time-password seed, so there is nothing to show.
    expect(screen.queryByText('One-time password')).toBeNull()
    expect(screen.getAllByText('••••••••')).not.toHaveLength(0)
    expect(screen.getByRole('link', { name: S.how_it_works })).toHaveAttribute(
      'href',
      expect.stringContaining('secrets-env.md'),
    )
  })

  it('offers only the bare reference for a plain secret', async () => {
    const user = userEvent.setup()
    listNames = ['OPENAI_API_KEY']
    mount()
    await user.click(await screen.findByRole('option', { name: /OPENAI_API_KEY/ }))
    expect(await screen.findByText('secret://OPENAI_API_KEY')).toBeInTheDocument()
    expect(screen.queryByText(/secret:\/\/OPENAI_API_KEY#/)).not.toBeInTheDocument()
  })

  it('edits a login through the typed write, prefilling only what the list showed', async () => {
    const user = userEvent.setup()
    listNames = ['GITHUB']
    listItems = [{ name: 'GITHUB', type: 'login', revision: 'r1', fields: LOGIN_FIELDS }]
    mount()
    await user.click(await screen.findByRole('option', { name: /GITHUB/ }))
    await user.click(await screen.findByRole('button', { name: S.edit }))
    expect(screen.getByLabelText(S.username_label)).toHaveValue('octocat')
    expect(screen.getByLabelText(S.password_label)).toHaveValue('')
    expect(screen.getByText(S.keep_blank_hint)).toBeInTheDocument()
    await user.type(screen.getByLabelText(S.password_label), 'pw-2')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(calls.find(c => c.method === 'POST')?.body).toMatchObject({
        name: 'GITHUB',
        type: 'login',
        fields: { username: 'octocat', password: 'pw-2' },
      }),
    )
  })

  it('saves an edit that leaves the hidden fields blank without sending them', async () => {
    // The page never holds the stored password, seed or notes, so a blank one
    // is left out of the body and the server keeps it instead of erasing it.
    const user = userEvent.setup()
    listNames = ['GITHUB']
    listItems = [{ name: 'GITHUB', type: 'login', revision: 'r1', fields: LOGIN_FIELDS }]
    mount()
    await user.click(await screen.findByRole('option', { name: /GITHUB/ }))
    await user.click(await screen.findByRole('button', { name: S.edit }))
    await user.clear(screen.getByLabelText(S.username_label))
    await user.type(screen.getByLabelText(S.username_label), 'hubot')
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeEnabled()
    await user.click(save)
    await waitFor(() => expect(calls.some(c => c.method === 'POST')).toBe(true))
    expect(calls.find(c => c.method === 'POST')?.body).toEqual({
      name: 'GITHUB',
      type: 'login',
      fields: { username: 'hubot', urls: ['https://github.com'] },
    })
  })

  it('withholds the agent hand-off once only a visible login field is edited', async () => {
    // The hand-off navigates away and unmounts the form, so a username-only
    // edit must count as a draft just as a typed password does.
    const user = userEvent.setup()
    listNames = ['GITHUB']
    listItems = [{ name: 'GITHUB', type: 'login', revision: 'r1', fields: LOGIN_FIELDS }]
    listManagedError = true
    mount()
    await user.click(await screen.findByRole('option', { name: /GITHUB/ }))
    await user.click(await screen.findByRole('button', { name: S.edit }))
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    await user.type(screen.getByLabelText(S.username_label), 'x')
    await waitFor(() => expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull())
  })

  it('keeps the phone push marker when a save opens the saved item', async () => {
    // SettingsSubNav's Back pops only an entry carrying this marker, so the
    // replace that follows a save must keep it.
    const user = userEvent.setup()
    mount({ pathname: '/settings', search: '?sub=%23', state: { [SUBNAV_PUSH_STATE]: true } })
    await user.type(await screen.findByLabelText('Secret name'), 'MY_KEY')
    await user.type(screen.getByLabelText('Secret value'), 'sk-abc')
    listNames = ['MY_KEY']
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(lastLocation?.search).toBe('?sub=MY_KEY'))
    expect(lastLocation?.state).toEqual({ [SUBNAV_PUSH_STATE]: true })
  })

  it('withholds the agent hand-off once only a name is typed in Add', async () => {
    const user = userEvent.setup()
    listNames = ['OPENAI_API_KEY']
    listManagedError = true
    mount()
    await openAdd(user)
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    await user.type(screen.getByLabelText('Secret name'), 'MY_KEY')
    await waitFor(() => expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull())
  })

  it('leaves the user on the pane they opened while an earlier delete was still running', async () => {
    const user = userEvent.setup()
    listNames = ['A_KEY', 'GITHUB']
    listItems = [{ name: 'GITHUB', type: 'login', revision: 'r1', fields: LOGIN_FIELDS }]
    let release: () => void = () => {}
    stubFetch((input, init) => {
      const { url, method } = recordCall(input, init)
      if (method === 'GET' && url === '/api/secrets') return okJson(listBody())
      if (method === 'DELETE') return new Promise<Response>(resolve => { release = () => resolve(okJson({ ok: true })) })
      return okJson({ ok: true })
    })
    mount()
    await user.click(await screen.findByRole('option', { name: /A_KEY/ }))
    await user.click(await screen.findByRole('button', { name: 'Delete secret A_KEY' }))
    await user.click(screen.getByRole('button', { name: S.delete }))
    await waitFor(() => expect(calls.some(c => c.method === 'DELETE')).toBe(true))
    await user.click(screen.getByRole('option', { name: /GITHUB/ }))
    await user.click(await screen.findByRole('button', { name: S.edit }))
    await user.type(screen.getByLabelText(S.username_label), 'x')
    listNames = ['GITHUB']
    await act(async () => release())
    // The delete's late success refetches the list but does not pull the
    // user to Add: the GITHUB edit is still open with what they typed.
    await waitFor(() => expect(calls.filter(c => c.method === 'GET').length).toBeGreaterThan(1))
    expect(screen.getByLabelText(S.username_label)).toHaveValue('octocatx')
    expect(screen.queryByLabelText('Secret name')).toBeNull()
  })

  it('says so when a reference cannot be copied', async () => {
    const user = userEvent.setup()
    listNames = ['OPENAI_API_KEY']
    mount()
    await user.click(await screen.findByRole('option', { name: /OPENAI_API_KEY/ }))
    await screen.findByText('secret://OPENAI_API_KEY')
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: () => Promise.reject(new Error('denied')) } })
    const execCommand = document.execCommand
    document.execCommand = (() => false) as typeof document.execCommand
    try {
      await user.click(screen.getByRole('button', { name: S.copy_reference.replace('{{reference}}', 'secret://OPENAI_API_KEY') }))
      expect(await screen.findByText(S.copy_failed)).toBeInTheDocument()
    } finally {
      document.execCommand = execCommand
    }
  })

  it('offers no Edit while that item’s delete is still in flight', async () => {
    const user = userEvent.setup()
    listNames = ['OPENAI_API_KEY']
    let release: () => void = () => {}
    stubFetch((input, init) => {
      const { url, method } = recordCall(input, init)
      if (method === 'GET' && url === '/api/secrets') return okJson(listBody())
      if (method === 'DELETE') return new Promise<Response>(resolve => { release = () => resolve(okJson({ ok: true })) })
      return okJson({ ok: true })
    })
    mount()
    await user.click(await screen.findByRole('option', { name: /OPENAI_API_KEY/ }))
    await user.click(await screen.findByRole('button', { name: 'Delete secret OPENAI_API_KEY' }))
    // The confirmation alone already holds Edit back.
    expect(screen.getByRole('button', { name: S.edit })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: S.delete }))
    await waitFor(() => expect(calls.some(c => c.method === 'DELETE')).toBe(true))
    expect(screen.getByRole('button', { name: S.edit })).toBeDisabled()
    await act(async () => release())
  })

  it('deletes only after confirmation, by url-encoded name, and abandons on cancel', async () => {
    const user = userEvent.setup()
    listNames = ['my key/1']
    mount()
    await user.click(await screen.findByRole('option', { name: /my key\/1/ }))
    await user.click(await screen.findByRole('button', { name: 'Delete secret my key/1' }))
    expect(screen.getByText(/Permanently delete/)).toBeInTheDocument()
    expect(calls.some(c => c.method === 'DELETE')).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByText(/Permanently delete/)).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Delete secret my key/1' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(calls.find(c => c.method === 'DELETE')?.url).toBe('/api/secrets/my%20key%2F1'))
  })

  it('keeps the confirmation open and shows the error when the DELETE is rejected', async () => {
    const user = userEvent.setup()
    listNames = ['TEST_KEY']
    stubFetch((input, init) => {
      const { method } = recordCall(input, init)
      if (method === 'DELETE') {
        return Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({ error: 'locked' }) } as Response)
      }
      return Promise.resolve(okJson(listBody()))
    })
    mount()
    await user.click(await screen.findByRole('option', { name: /TEST_KEY/ }))
    await user.click(await screen.findByRole('button', { name: 'Delete secret TEST_KEY' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    expect(await screen.findByText(/Could not delete secret: locked/)).toBeInTheDocument()
    expect(screen.getByText(/Permanently delete/)).toBeInTheDocument()
  })

  it('names a corrupt item so it can be deleted', async () => {
    const user = userEvent.setup()
    listNames = ['BROKEN']
    listItems = [{ name: 'BROKEN', type: 'login', revision: 'r', fields: {}, corrupt: true }]
    mount()
    await user.click(await screen.findByRole('option', { name: /BROKEN/ }))
    expect(await screen.findByText(S.corrupt_item)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete secret BROKEN' })).toBeInTheDocument()
  })
})

describe('SecretsPanel managed pane', () => {
  it('opens the managed slot a settings-search highlight names', async () => {
    // Search lands on /settings/secrets?highlight=<id> with no pane selected.
    listManaged = [{ name: 'WAKATIME_API_KEY', kind: 'wakatime_api_key' }]
    mount('/settings?highlight=secrets.wakatime-api-key')
    expect(await screen.findByLabelText(M.wakatime_api_key_label)).toBeInTheDocument()
    expect(screen.queryByLabelText('Secret name')).toBeNull()
  })

  it('configures an unset managed credential without asking for its key name', async () => {
    const user = userEvent.setup()
    listManaged = [{ name: 'WAKATIME_API_KEY', kind: 'wakatime_api_key' }]
    mount()
    await user.click(await screen.findByRole('option', { name: /WAKATIME_API_KEY/ }))
    expect(screen.queryByLabelText('Secret name')).not.toBeInTheDocument()
    // Labelled with the slot's own copy: the anchor settings search highlights.
    await user.type(await screen.findByLabelText(M.wakatime_api_key_label), 'waka-1')
    await user.click(screen.getByRole('button', { name: 'Save WAKATIME_API_KEY' }))
    await waitFor(() =>
      expect(calls.find(c => c.method === 'POST')?.body).toEqual({ name: 'WAKATIME_API_KEY', value: 'waka-1' }),
    )
  })

  it('removes a configured managed credential only after confirmation, and returns focus on cancel', async () => {
    const user = userEvent.setup()
    listNames = ['JIRA_API_TOKEN']
    listManaged = [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }]
    mount()
    await user.click(await screen.findByRole('option', { name: /JIRA_API_TOKEN/ }))
    await user.click(await screen.findByRole('button', { name: 'Delete Jira API token' }))
    expect(screen.getByText(/Permanently delete/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete Jira API token' })).toHaveFocus())
    await user.click(screen.getByRole('button', { name: 'Delete Jira API token' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(calls.find(c => c.method === 'DELETE')?.url).toBe('/api/secrets/JIRA_API_TOKEN'))
  })

  it('explains an inactive global Jira token next to its field', async () => {
    const user = userEvent.setup()
    listNames = ['JIRA_API_TOKEN']
    listManaged = [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }]
    listUnused = [{ name: 'JIRA_API_TOKEN', reason: 'jira_multi_host' }]
    mount()
    await user.click(await screen.findByRole('option', { name: /JIRA_API_TOKEN/ }))
    expect(await screen.findByText(M.jira_global_unused_multi_host)).toBeInTheDocument()
  })

  it('points a Connections client secret at Settings > Connections instead of editing it', async () => {
    const user = userEvent.setup()
    listNames = ['CONNECTIONS_CLIENT_SECRET__GITHUB']
    listManaged = [{ name: 'CONNECTIONS_CLIENT_SECRET__GITHUB', kind: 'connections_client_secret', host: 'github' }]
    mount()
    await user.click(await screen.findByRole('option', { name: /CONNECTIONS_CLIENT_SECRET__GITHUB/ }))
    expect(await screen.findByRole('link', { name: M.connections_client_secret_manage })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Delete/ })).not.toBeInTheDocument()
  })

  it('shows the WakaTime sign-in tokens read-only, managed from the WakaTime panel', async () => {
    // The vault routes refuse this name with 409, so no edit, value, reference or Delete.
    const user = userEvent.setup()
    listNames = ['WAKATIME_OAUTH_TOKENS']
    listManaged = [{ name: 'WAKATIME_OAUTH_TOKENS', kind: 'wakatime_oauth' }]
    mount()
    await user.click(await screen.findByRole('option', { name: /WAKATIME_OAUTH_TOKENS/ }))
    expect(await screen.findByText(/Managed in the WakaTime panel/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: M.wakatime_oauth_tokens_manage })).toHaveAttribute(
      'href',
      '/settings/overview?view=wakatime',
    )
    expect(screen.queryByRole('button', { name: /Delete|Edit|Replace|Save/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/secret:\/\//)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Secret value')).not.toBeInTheDocument()
  })

  it('blocks an Add that targets a managed name whose own delete is still in flight', async () => {
    const user = userEvent.setup()
    listNames = ['JIRA_API_TOKEN']
    listManaged = [{ name: 'JIRA_API_TOKEN', kind: 'jira_api_token' }]
    let release: () => void = () => {}
    stubFetch((input, init) => {
      const { method } = recordCall(input, init)
      if (method === 'DELETE') {
        return new Promise<Response>(resolve => {
          release = () => resolve(okJson({ ok: true }))
        })
      }
      return Promise.resolve(okJson(listBody()))
    })
    mount()
    await user.click(await screen.findByRole('option', { name: /JIRA_API_TOKEN/ }))
    await user.click(await screen.findByRole('button', { name: 'Delete Jira API token' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(calls.some(c => c.method === 'DELETE')).toBe(true))
    await user.click(screen.getByRole('option', { name: /Add secret/ }))
    await user.type(await screen.findByLabelText('Secret name'), 'JIRA_API_TOKEN')
    await user.type(screen.getByLabelText('Secret value'), 'tok')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    await act(async () => release())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled())
  })
})

describe('SecretsPanel on an expired session', () => {
  /** The auth challenge the gateway answers with once the dashboard session no
   *  longer authenticates: 403 carrying `X-Auth-Required`, and a body whose
   *  `error` names the CRYPTOGRAPHIC reason, which the transport substitutes. */
  const AUTH_CHALLENGE = { error: 'invalid signature', code: 'forbidden' }
  function authDenied(body: unknown) {
    return {
      ok: false,
      status: 403,
      headers: { get: (k: string) => (k === 'X-Auth-Required' ? 'true' : null) },
      json: () => Promise.resolve(body),
      text: () => Promise.resolve(JSON.stringify(body)),
    } as unknown as Response
  }
  /** The list GET succeeds so the Add pane is reachable; the save POST is denied
   *  with the auth challenge; the silent refresh that follows comes back 401,
   *  the terminal answer that takes the recovery to the banner. */
  function denySaveWithExhaustedRefresh() {
    return stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
      const { url, method } = recordCall(input, init)
      if (url === '/api/auth/refresh') {
        return Promise.resolve({
          ok: false,
          status: 401,
          json: () => Promise.reject(new SyntaxError('revoked')),
        } as unknown as Response)
      }
      if (method === 'POST' && url === '/api/secrets') return Promise.resolve(authDenied(AUTH_CHALLENGE))
      return Promise.resolve(okJson(listBody()))
    })
  }
  const banner = () => document.getElementById('mc-session-expired')
  async function save(user: ReturnType<typeof userEvent.setup>) {
    await openAdd(user)
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

  it('attempts the silent refresh, raises the banner, and names signing in on the card', async () => {
    const user = userEvent.setup()
    denySaveWithExhaustedRefresh()
    mount()
    await save(user)
    await waitFor(() => expect(calls.some(c => c.url === '/api/auth/refresh')).toBe(true))
    await waitFor(() => expect(banner()).not.toBeNull())
    expect(isAuthBannerShown()).toBe(true)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(enManual.api.client.session_expired_sign_in_again)
    expect(alert).not.toHaveTextContent('invalid signature')
    expect(screen.getByLabelText('Secret value')).toHaveValue('sk-abc123')
  })

  it('keeps a non-auth 403 on its own prose, with no banner', async () => {
    const user = userEvent.setup()
    stubFetch((input, init) => {
      const { method } = recordCall(input, init)
      if (method === 'POST') {
        return Promise.resolve({
          ok: false,
          status: 403,
          headers: { get: () => null },
          json: () => Promise.resolve({ error: 'owner authorization required' }),
        } as unknown as Response)
      }
      return Promise.resolve(okJson(listBody()))
    })
    mount()
    await save(user)
    expect(await screen.findByText(/owner authorization required/)).toBeInTheDocument()
    expect(banner()).toBeNull()
    expect(isAuthBannerShown()).toBe(false)
  })
})
