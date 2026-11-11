import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { SecretsPanel } from './SecretsPanel'
import en from '../../i18n/locales/en.json'
import type { SecretBackendView } from '../../api/client'

vi.mock('../../hooks/useContainerWidth', () => ({
  useContainerWidth: () => [{ current: null }, 1200],
}))
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => false, useIsNarrowViewport: () => false }))

const S = en.settings.secrets

type Call = { url: string; method: string; body?: unknown }
let calls: Call[] = []
let backends: SecretBackendView[] | undefined
let connectAnswer: { status: number; body: unknown } = { status: 200, body: { ok: true, account: 'Team' } }

/** A provider the panel has never heard of: everything it shows comes from this data. */
function fakeView(over: Partial<SecretBackendView> = {}): SecretBackendView {
  return {
    id: 'fakepm',
    name: 'FakePM',
    status: 'not_connected',
    connected: false,
    form: [
      { name: 'token', label_key: 'settings.secrets.backend_op_token_label', secret: true, required: true, placeholder_key: 'settings.secrets.backend_op_token_placeholder', hint_key: 'settings.secrets.backend_op_token_hint' },
      { name: 'vault', label_key: 'settings.secrets.backend_op_vault_label', secret: false, required: false, placeholder_key: '', hint_key: 'settings.secrets.backend_op_vault_hint' },
    ],
    capabilities: { item_types: ['secret', 'login'] },
    help_url: 'https://example.com/help',
    help_label_key: 'settings.secrets.backend_op_help',
    ...over,
  }
}

function respond(status: number, body: unknown): Response {
  const text = JSON.stringify(body)
  return {
    ok: status < 400,
    status,
    url: '',
    headers: { get: () => null },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(text),
  } as unknown as Response
}

beforeEach(() => {
  calls = []
  backends = [fakeView()]
  connectAnswer = { status: 200, body: { ok: true, account: 'Team' } }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const call = { url: String(input), method: init?.method ?? 'GET', body: init?.body ? JSON.parse(String(init.body)) : undefined }
      calls.push(call)
      if (call.method === 'GET' && call.url === '/api/secrets') {
        return respond(200, {
          names: ['GITHUB'],
          items: [{ name: 'GITHUB', type: 'secret', revision: 'r1' }],
          managed: [],
          ...(backends ? { backends } : {}),
        })
      }
      if (call.url === '/api/secrets/backends/fakepm' && call.method === 'POST') {
        return respond(connectAnswer.status, connectAnswer.body)
      }
      return respond(200, { ok: true })
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function mount(url: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <SecretsPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const paneUrl = (id: string) => `/settings?sub=${encodeURIComponent(`#backend:${id}`)}`
const itemUrl = (id: string, name: string) => `/settings?sub=${encodeURIComponent(`#backend:${id}:${name}`)}`

describe('Secrets: password managers', () => {
  it('shows no password manager row when the gateway sends no backends (preview off)', async () => {
    backends = undefined
    mount('/settings')
    await screen.findByText('GITHUB')
    expect(screen.queryByText(S.backend_group)).toBeNull()
  })

  it('renders the connect form the backend declares and posts its values to the generic route', async () => {
    mount(paneUrl('fakepm'))
    expect(await screen.findByText(S.backend_op_token_label)).toBeTruthy()
    expect(screen.getByText(S.backend_op_vault_label)).toBeTruthy()
    // A field the backend declares not required says so; the required one does not.
    expect(screen.getAllByText(S.backend_optional)).toHaveLength(1)
    expect(screen.getByRole('link', { name: S.backend_op_help }).getAttribute('href')).toBe('https://example.com/help')
    const connect = screen.getByRole('button', { name: S.backend_connect })
    expect((connect as HTMLButtonElement).disabled).toBe(true)
    await userEvent.type(screen.getByPlaceholderText(S.backend_op_token_placeholder), 'tok-1')
    await userEvent.click(connect)
    await waitFor(() => expect(calls.find(c => c.method === 'POST')?.body).toEqual({ token: 'tok-1' }))
    expect(calls.find(c => c.method === 'POST')?.url).toBe('/api/secrets/backends/fakepm')
  })

  it('words a backend-specific failure with the key it sent', async () => {
    connectAnswer = {
      status: 409,
      body: { error: 'x', code: 'choose_vault', message_key: 'settings.secrets.backend_op_err_choose_vault', params: { vaults: 'Personal, Work' } },
    }
    mount(paneUrl('fakepm'))
    await userEvent.type(await screen.findByPlaceholderText(S.backend_op_token_placeholder), 'tok-1')
    await userEvent.click(screen.getByRole('button', { name: S.backend_connect }))
    await screen.findByText(/Personal, Work/)
  })

  it('words a generic failure with the backend name', async () => {
    connectAnswer = { status: 409, body: { error: 'x', code: 'rate_limited' } }
    mount(paneUrl('fakepm'))
    await userEvent.type(await screen.findByPlaceholderText(S.backend_op_token_placeholder), 'tok-1')
    await userEvent.click(screen.getByRole('button', { name: S.backend_connect }))
    await screen.findByText(new RegExp(S.backend_err_rate_limited.replace('{{name}}', 'FakePM')))
  })

  it('lists backend items beside local ones, read-only, and marks a shadowed name', async () => {
    backends = [
      fakeView({
        status: 'connected',
        connected: true,
        account: 'Team',
        items: [
          { name: 'AWS_CONSOLE', type: 'login', source: 'fakepm', updated_at: '2026-10-25T07:47:00Z', fields: { username: 'zezhen@example.com', urls: [] } },
          { name: 'GITHUB', type: 'secret', source: 'fakepm', shadowed: true, fields: {} },
        ],
      }),
    ]
    mount(itemUrl('fakepm', 'AWS_CONSOLE'))
    expect(await screen.findByText(S.backend_items_group.replace('{{name}}', 'FakePM').replace('{{account}}', 'Team'))).toBeTruthy()
    expect(await screen.findByText(`${S.type_login} · FakePM`)).toBeTruthy()
    expect(screen.queryByRole('button', { name: S.edit })).toBeNull()
    expect(screen.queryByRole('button', { name: S.delete_secret_name.replace('{{name}}', 'AWS_CONSOLE') })).toBeNull()
    expect(screen.queryByText(/2026/)).toBeNull()
    expect(screen.getByText(S.backend_shadowed)).toBeTruthy()
  })

  it('shows a backend this install cannot run as unavailable, with the install command and no form', async () => {
    backends = [
      fakeView({
        status: 'unavailable',
        error: { code: 'sdk_missing', message: 'x', install_command: 'pip install "fakepm-sdk==1.0"' },
      }),
    ]
    mount(paneUrl('fakepm'))
    expect(await screen.findByText(S.backend_err_sdk_missing.split('{{name}}').join('FakePM'))).toBeTruthy()
    expect(screen.getByText('pip install "fakepm-sdk==1.0"')).toBeTruthy()
    expect(screen.getAllByText(S.backend_unavailable).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: S.backend_connect })).toBeNull()
  })

  it('shows a 3P item named WAKATIME_OAUTH_TOKENS as an ordinary read-only row, never the WakaTime pane', async () => {
    backends = [
      fakeView({
        status: 'connected',
        connected: true,
        account: 'Team',
        items: [{ name: 'WAKATIME_OAUTH_TOKENS', type: 'secret', source: 'fakepm', shadowed: true, fields: {} }],
      }),
    ]
    mount(itemUrl('fakepm', 'WAKATIME_OAUTH_TOKENS'))
    expect(await screen.findByText(`${S.type_secret} · FakePM`)).toBeTruthy()
    expect(screen.queryByText('Manage in WakaTime')).toBeNull()
    expect(screen.queryByRole('button', { name: S.edit })).toBeNull()
  })

  // The 1Password-specific keys the gateway can send as `message_key` (onepassword.py).
  it.each([
    'settings.secrets.backend_op_err_invalid_token',
    'settings.secrets.backend_op_err_no_vaults',
    'settings.secrets.backend_op_err_vault_not_found',
    'settings.secrets.backend_op_err_ambiguous_vault',
  ])('words the gateway-sent key %s from the catalog', async messageKey => {
    connectAnswer = { status: 409, body: { error: 'x', code: 'any', message_key: messageKey, params: { vaults: 'Personal' } } }
    mount(paneUrl('fakepm'))
    await userEvent.type(await screen.findByPlaceholderText(S.backend_op_token_placeholder), 'tok-1')
    await userEvent.click(screen.getByRole('button', { name: S.backend_connect }))
    const leaf = messageKey.split('.').pop() as keyof typeof S
    const sentence = String(S[leaf]).split('{{vaults}}').join('Personal')
    await screen.findByText(text => text.includes(sentence))
  })

  it('disconnects through the generic route', async () => {
    backends = [fakeView({ status: 'connected', connected: true, account: 'Team', items: [] })]
    mount(paneUrl('fakepm'))
    await userEvent.click(await screen.findByRole('button', { name: S.backend_disconnect }))
    await waitFor(() => expect(calls.some(c => c.method === 'DELETE' && c.url === '/api/secrets/backends/fakepm')).toBe(true))
  })
})
