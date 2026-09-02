/**
 * Webhooks source console contract.
 *
 * Pins the revised Option C information architecture and the safety properties
 * that must survive it: source-only configuration, separate operational
 * activity, operator-owned routing, one-time secrets, and two-step destructive
 * actions.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { WebhooksView } from '../api/client'

const webhooks = vi.fn()
const agentsInstalled = vi.fn()
const createWebhookToken = vi.fn()
const updateWebhookToken = vi.fn()
const deleteWebhookToken = vi.fn()
const deleteWebhookContext = vi.fn()
const testWebhook = vi.fn()
const setWebhooksEnabled = vi.fn()

let mockIsMobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mockIsMobile }))

vi.mock('../api/client', () => ({
  api: {
    webhooks: (...args: unknown[]) => webhooks(...args),
    agentsInstalled: (...args: unknown[]) => agentsInstalled(...args),
    createWebhookToken: (...args: unknown[]) => createWebhookToken(...args),
    updateWebhookToken: (...args: unknown[]) => updateWebhookToken(...args),
    deleteWebhookToken: (...args: unknown[]) => deleteWebhookToken(...args),
    deleteWebhookContext: (...args: unknown[]) => deleteWebhookContext(...args),
    testWebhook: (...args: unknown[]) => testWebhook(...args),
    setWebhooksEnabled: (...args: unknown[]) => setWebhooksEnabled(...args),
  },
}))

import WebhooksPage from '../pages/WebhooksPage'

const NOW = Math.floor(Date.now() / 1000)
const AGENTS = [
  {
    name: 'reviewer', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default',
    model: '', description: 'Reviews code changes', source: 'user',
  },
  {
    name: 'oncall', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default',
    model: '', description: 'Handles incidents', source: 'user',
  },
]

const EMPTY: WebhooksView = {
  enabled: false,
  switch_on: true,
  has_tokens: false,
  url: 'http://localhost:6776/api/hooks/agent',
  slots: { in_use: 0, max: 6 },
  limits: {
    session_key_prefix: 'hook:', message_max: 49999,
    timeout_default: 599, timeout_max: 3593, max_concurrent: 6,
    signature_window_seconds: 300,
  },
  tokens: [], contexts: [], runs: [],
}

const POPULATED: WebhooksView = {
  ...EMPTY,
  enabled: true,
  has_tokens: true,
  slots: { in_use: 2, max: 6 },
  tokens: [
    {
      id: 'wht_review', label: 'Review Bot', display_prefix: 'kc_whk_4f2b', last4: '1f3a',
      created_at: NOW - 7200, last_used_at: NOW - 480, require_signature: true,
      agent: 'reviewer', enabled: true, legacy: false,
    },
    {
      id: 'wht_deploy', label: 'Deploy Bot', display_prefix: 'kc_whk_8a9c', last4: '44ad',
      created_at: NOW - 3600, last_used_at: null, require_signature: false,
      agent: 'oncall', enabled: false, legacy: false,
    },
  ],
  contexts: [
    {
      hook_id: 'review:pr-123', session_key: 'hook:review:pr-123',
      registered_at: NOW - 480, age_seconds: 480, freshness: 'fresh',
      context_summary: 'Reviewing PR #123; awaiting the next analysis pass.', context_chars: 412,
    },
    {
      hook_id: 'deploy:prod-4471', session_key: 'hook:deploy:prod-4471',
      registered_at: NOW - 21600, age_seconds: 21600, freshness: 'stale',
      context_summary: 'Deploy 4471 is mid-rollout at 25%.', context_chars: 268,
    },
    {
      hook_id: 'ci:build-88', session_key: 'hook:ci:build-88',
      registered_at: NOW - 172800, age_seconds: 172800, freshness: 'expired',
      context_summary: 'Build 88 failed on the Coverage Gate.', context_chars: 96,
    },
  ],
  runs: [
    {
      id: 'run_1', hook_id: 'review:pr-123', session_key: 'hook:review:pr-123',
      name: 'Review Bot', outcome: 'completed', started_at: NOW - 480,
      duration_ms: 42000, result_chars: 2150, token_id: 'wht_review',
      delivered: true, detail: 'Delivered to notifications + Slack DM',
    },
    {
      id: 'run_2', hook_id: 'deploy:prod-4471', session_key: 'hook:deploy:prod-4471',
      name: 'Deploy Bot', outcome: 'timeout', started_at: NOW - 3060,
      duration_ms: 599000, result_chars: 0, token_id: 'wht_deploy',
      delivered: false, detail: 'Turn exceeded the 599s timeout and was cancelled.',
    },
  ],
}

function mount(view: WebhooksView) {
  webhooks.mockResolvedValue(view)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <WebhooksPage />
    </QueryClientProvider>,
  )
}

async function openSource(id = 'wht_review') {
  fireEvent.click(await screen.findByTestId(`webhook-row-token-${id}`))
}

async function openActivity() {
  fireEvent.click(await screen.findByRole('tab', { name: /Activity/ }))
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  mockIsMobile = false
  agentsInstalled.mockResolvedValue(AGENTS)
})
afterEach(cleanup)

describe('Webhooks source rail', () => {
  it('shows only first-class sources and their destination agents', async () => {
    mount(POPULATED)
    await screen.findByTestId('webhook-row-token-wht_review')
    const rail = within(screen.getByTestId('webhook-rail'))
    expect(rail.getByTestId('webhook-row-token-wht_review').textContent).toContain('Agent: reviewer')
    expect(rail.getByTestId('webhook-row-token-wht_deploy').textContent).toContain('Agent: oncall')
    expect(rail.queryByText('Registered contexts')).toBeNull()
    expect(rail.queryByText('Recent runs')).toBeNull()
    expect(screen.getByRole('tab', { name: /Webhooks/ }).getAttribute('aria-selected')).toBe('true')
  })

  it('uses a focused first-source state without the endpoint and limits wall', async () => {
    mount(EMPTY)
    expect(await screen.findByText('Let an outside system wake up your agent')).toBeTruthy()
    expect(await screen.findByLabelText('New token label')).toBeTruthy()
    expect(screen.queryByText('Limits & behaviour')).toBeNull()
    expect(screen.queryByText('Capacity')).toBeNull()
    expect(screen.queryByText('Endpoint')).toBeNull()
  })

  it('separates contexts and runs into Activity and scopes filtering there', async () => {
    mount(POPULATED)
    await openActivity()
    const rail = within(screen.getByTestId('webhook-rail'))
    expect(rail.getByText('Registered contexts')).toBeTruthy()
    expect(rail.getByText('Recent runs')).toBeTruthy()
    expect(screen.queryByTestId('webhook-row-token-wht_review')).toBeNull()

    fireEvent.change(screen.getByLabelText('Filter webhooks'), { target: { value: 'pr-123' } })
    expect(screen.getByTestId('webhook-row-context-review:pr-123')).toBeTruthy()
    expect(screen.queryByTestId('webhook-row-context-ci:build-88')).toBeNull()
  })
})

describe('selected source detail', () => {
  it('shows routing, shared connection, and collapsed advanced sections', async () => {
    mount(POPULATED)
    await openSource()
    expect(screen.getByTestId('webhook-detail').getAttribute('data-pane')).toBe('token')
    expect(screen.getByTestId('webhook-detail-title').textContent).toBe('Review Bot')
    expect(screen.getByText('Routing')).toBeTruthy()
    expect(screen.getByText('Connection')).toBeTruthy()
    expect(screen.getAllByText('reviewer')).toHaveLength(2)
    expect(screen.getByLabelText('Copy endpoint URL')).toBeTruthy()
    expect(screen.getByText('Request example').closest('button')?.getAttribute('aria-expanded')).toBe('false')
    expect(screen.getByText('Request signing').closest('button')?.getAttribute('aria-expanded')).toBe('false')
    expect(screen.getByText('Request example').closest('button')?.className).toContain('font-body')
  })

  it('shows only the selected source activity and opens a run in Activity', async () => {
    mount(POPULATED)
    await openSource()
    const section = screen.getByText('Recent runs').closest('section')
    expect(section).toBeTruthy()
    const activity = within(section as HTMLElement)
    expect(activity.getByText('Review Bot')).toBeTruthy()
    expect(activity.queryByText('Deploy Bot')).toBeNull()

    fireEvent.click(activity.getByRole('button', { name: /Review Bot/ }))

    expect(screen.getByRole('tab', { name: /Activity/ }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByTestId('webhook-detail').getAttribute('data-pane')).toBe('run')
  })

  it('keeps at most two source actions visible and hides pause while revoke is armed', async () => {
    mount(POPULATED)
    await openSource()
    const actions = screen.getByTestId('webhook-source-actions')
    expect(actions.className).toContain('flex-wrap')
    expect(within(actions).getAllByRole('button')).toHaveLength(2)

    fireEvent.click(screen.getByTestId('webhook-revoke-wht_review'))
    expect(screen.queryByTestId('webhook-source-toggle-wht_review')).toBeNull()
    expect(within(actions).getAllByRole('button')).toHaveLength(2)
  })

  it('updates the operator-owned destination agent', async () => {
    updateWebhookToken.mockResolvedValue({ ok: true })
    mount(POPULATED)
    await openSource()
    fireEvent.click(screen.getByLabelText('Switch agent'))
    fireEvent.click(await screen.findByRole('option', { name: /oncall/ }))
    await waitFor(() => expect(updateWebhookToken).toHaveBeenCalledWith('wht_review', { agent: 'oncall' }))
  })

  it('pauses one source without touching the global switch', async () => {
    updateWebhookToken.mockResolvedValue({ ok: true })
    mount(POPULATED)
    await openSource()
    const sourceToggle = screen.getByTestId('webhook-source-toggle-wht_review')
    fireEvent.click(sourceToggle)
    await waitFor(() => expect(updateWebhookToken).toHaveBeenCalledWith('wht_review', { enabled: false }))
    expect(setWebhooksEnabled).not.toHaveBeenCalled()
  })

  it('keeps the persisted source state active while the global switch is off', async () => {
    mount({ ...POPULATED, enabled: false, switch_on: false })
    await openSource()
    const detail = within(screen.getByTestId('webhook-detail'))
    expect(detail.getByText('Active')).toBeTruthy()
    expect(detail.queryByText('Paused')).toBeNull()
  })

  it('shows historical unmapped sources as unassigned until routing is saved', async () => {
    updateWebhookToken.mockResolvedValue({ ok: true })
    mount({
      ...POPULATED,
      tokens: [{ ...POPULATED.tokens[0], agent: '' }],
    })
    await openSource()
    const selector = screen.getByLabelText('Switch agent')
    expect(selector.textContent).toContain('Unassigned')
    expect(selector.textContent).not.toContain('reviewer')

    fireEvent.click(selector)
    fireEvent.click(await screen.findByRole('option', { name: /reviewer/ }))
    await waitFor(() => expect(updateWebhookToken).toHaveBeenCalledWith('wht_review', { agent: 'reviewer' }))
  })
})

describe('Activity detail', () => {
  it('renders freshness tiers distinctly and preserves two-step context deletion', async () => {
    deleteWebhookContext.mockResolvedValue({ ok: true })
    mount(POPULATED)
    await openActivity()
    const dots = ['review:pr-123', 'deploy:prod-4471', 'ci:build-88']
      .map(id => screen.getByTestId(`webhook-freshness-${id}`))
    expect(dots.map(dot => dot.getAttribute('data-freshness'))).toEqual(['fresh', 'stale', 'expired'])
    expect(new Set(dots.map(dot => dot.className)).size).toBe(3)

    fireEvent.click(screen.getByTestId('webhook-row-context-review:pr-123'))
    expect(screen.getByTestId('webhook-context-banner').textContent).toContain('injected verbatim')
    fireEvent.click(screen.getByTestId('webhook-delete-context'))
    expect(deleteWebhookContext).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('webhook-delete-context'))
    await waitFor(() => expect(deleteWebhookContext).toHaveBeenCalledWith('review:pr-123'))
  })

  it('reports exact delivery destinations and links back to the source', async () => {
    mount({
      ...POPULATED,
      runs: [{ ...POPULATED.runs[0], id: 'run_partial', detail: 'Delivered to notifications' }],
    })
    await openActivity()
    fireEvent.click(screen.getByTestId('webhook-row-run-run_partial'))
    const detail = screen.getByTestId('webhook-detail')
    expect(detail.textContent).toContain('Delivered to notifications')
    expect(detail.textContent).not.toContain('Slack DM')
    fireEvent.click(screen.getByRole('button', { name: 'Review Bot' }))
    expect(screen.getByRole('tab', { name: /Webhooks/ }).getAttribute('aria-selected')).toBe('true')
  })
})

describe('credential lifecycle safeguards', () => {
  it('creates a routed source, shows both raw secrets once, and dismisses in two steps', async () => {
    createWebhookToken.mockResolvedValue({
      ok: true,
      token: 'kc_whk_TESTSECRET0123456789abcdefghij',
      signing_secret: 'kc_whs_SIGNSECRET0123456789abcdefghij',
      entry: {
        ...POPULATED.tokens[0], id: 'wht_new', label: 'CI Bot', agent: 'reviewer', enabled: true,
      },
    })
    mount(EMPTY)
    await screen.findByLabelText('New token label')
    fireEvent.change(screen.getByLabelText('New token label'), { target: { value: 'CI Bot' } })
    fireEvent.click(screen.getByText('Generate token'))
    await waitFor(() => expect(createWebhookToken).toHaveBeenCalledWith('CI Bot', true, 'reviewer'))

    const reveal = await screen.findByTestId('webhook-token-reveal')
    expect(reveal.textContent).toContain('kc_whk_TESTSECRET0123456789abcdefghij')
    expect(reveal.textContent).toContain('kc_whs_SIGNSECRET0123456789abcdefghij')

    fireEvent.change(screen.getByLabelText('New token label'), { target: { value: 'Second' } })
    expect(screen.getByText('Generate token').closest('button')?.disabled).toBe(true)

    fireEvent.click(screen.getByTestId('webhook-reveal-dismiss'))
    expect(screen.getByTestId('webhook-token-reveal')).toBeTruthy()
    fireEvent.click(screen.getByTestId('webhook-reveal-dismiss-confirm'))
    await waitFor(() => expect(screen.queryByTestId('webhook-token-reveal')).toBeNull())
  })

  it('creates bearer-only when signing is explicitly disabled', async () => {
    createWebhookToken.mockResolvedValue({
      ok: true,
      token: 'kc_whk_BEARERONLY0123456789abcdefghij',
      entry: {
        ...POPULATED.tokens[1], id: 'wht_bearer', label: 'Legacy CI', agent: 'reviewer', enabled: true,
      },
    })
    mount(EMPTY)
    await screen.findByLabelText('New token label')
    fireEvent.change(screen.getByLabelText('New token label'), { target: { value: 'Legacy CI' } })
    fireEvent.click(screen.getByLabelText('Require request signing'))
    fireEvent.click(screen.getByText('Generate token'))
    await waitFor(() => expect(createWebhookToken).toHaveBeenCalledWith('Legacy CI', false, 'reviewer'))
    expect(await screen.findByTestId('webhook-token-reveal')).toBeTruthy()
    expect(screen.queryByTestId('webhook-reveal-signing-secret')).toBeNull()
  })

  it('requires two clicks to revoke and keeps the legacy credential read-only', async () => {
    deleteWebhookToken.mockResolvedValue({ ok: true })
    mount(POPULATED)
    await openSource()
    fireEvent.click(screen.getByTestId('webhook-revoke-wht_review'))
    expect(deleteWebhookToken).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('webhook-revoke-wht_review'))
    await waitFor(() => expect(deleteWebhookToken).toHaveBeenCalledWith('wht_review'))
    cleanup()

    mount({
      ...POPULATED,
      tokens: [{
        id: 'legacy', label: 'Legacy config', display_prefix: 'kc_whk_0000', last4: '0000',
        created_at: NOW - 86400, last_used_at: null, require_signature: false,
        agent: '', enabled: true, legacy: true,
      }],
    })
    await openSource('legacy')
    expect(screen.queryByTestId('webhook-revoke-legacy')).toBeNull()
    expect(screen.getByText(/Remove .* to revoke/)).toBeTruthy()
  })
})

describe('global kill switch', () => {
  it('takes two clicks to turn off and one click to turn back on', async () => {
    setWebhooksEnabled.mockResolvedValue({ ok: true })
    mount(POPULATED)
    await screen.findByTestId('webhook-switch-row')
    fireEvent.click(screen.getByTestId('webhook-switch-off'))
    expect(setWebhooksEnabled).not.toHaveBeenCalled()
    expect(screen.getByTestId('webhook-switch-row').textContent).toMatch(/tokens and run history are kept/i)
    fireEvent.click(screen.getByTestId('webhook-switch-off-confirm'))
    await waitFor(() => expect(setWebhooksEnabled).toHaveBeenCalledWith(false))
    cleanup()

    mount({ ...POPULATED, enabled: false, switch_on: false })
    await screen.findByTestId('webhook-switch-on')
    fireEvent.click(screen.getByTestId('webhook-switch-on'))
    await waitFor(() => expect(setWebhooksEnabled).toHaveBeenCalledWith(true))
  })

  it('lets the operator back out without mutating', async () => {
    mount(POPULATED)
    await screen.findByTestId('webhook-switch-row')
    fireEvent.click(screen.getByTestId('webhook-switch-off'))
    fireEvent.click(screen.getByText('Keep it on'))
    expect(setWebhooksEnabled).not.toHaveBeenCalled()
  })
})

describe('request examples and responsive shell', () => {
  it('ties the collapsed request example to each source signing policy', async () => {
    mount(POPULATED)
    await openSource('wht_review')
    fireEvent.click(screen.getByText('Request example'))
    const signed = screen.getByLabelText('Example signed request for Review Bot').textContent || ''
    expect(signed).toContain('X-KiroCrew-Signature: sha256=$SIG')

    fireEvent.click(screen.getByTestId('webhook-row-token-wht_deploy'))
    fireEvent.click(screen.getByText('Request example'))
    const bearer = screen.getByLabelText('Example curl request for Deploy Bot').textContent || ''
    expect(bearer).toContain('Authorization: Bearer <token>')
    expect(bearer).not.toContain('X-KiroCrew-Signature')
  })

  it('shell-quotes a hostile Activity context id', async () => {
    const hostile = "pr'; printf INJECTED; #"
    mount({
      ...POPULATED,
      contexts: [{ ...POPULATED.contexts[0], hook_id: hostile, session_key: `hook:${hostile}` }],
    })
    await openActivity()
    fireEvent.click(screen.getByTestId(`webhook-row-context-${hostile}`))
    const code = screen.getByLabelText(`Example request for ${hostile}`).textContent || ''
    const bodyLine = code.split('\n').find(line => line.startsWith('BODY=')) || ''
    const withoutEscapes = bodyLine.slice('BODY='.length).replaceAll("'\\''", '\u0000')
    expect(withoutEscapes.startsWith("'")).toBe(true)
    expect(withoutEscapes.endsWith("'")).toBe(true)
    expect(withoutEscapes.slice(1, -1)).not.toContain("'")
  })

  it('keeps the resizable desktop rail and mobile drill-down contract', async () => {
    const { container } = mount(POPULATED)
    expect(await screen.findByTestId('webhook-rail')).toBeTruthy()
    expect(screen.getByLabelText('Resize webhooks rail')).toBeTruthy()
    expect((container.firstElementChild as HTMLElement).className).toContain('min-w-0')
    cleanup()

    mockIsMobile = true
    mount(POPULATED)
    await screen.findByTestId('webhook-detail-title')
    expect(screen.queryByTestId('webhook-rail')).toBeNull()
    fireEvent.click(screen.getByLabelText('Expand webhooks rail'))
    expect((await screen.findByTestId('webhook-rail')).style.width).toBe('100%')
    fireEvent.click(screen.getByTestId('webhook-row-token-wht_review'))
    expect(screen.queryByTestId('webhook-rail')).toBeNull()
    expect(screen.getByTestId('webhook-detail-title').textContent).toBe('Review Bot')
  })
})

describe('failure handling', () => {
  it('keeps source creation usable when settings are unavailable', async () => {
    webhooks.mockRejectedValue(new Error('HTTP 404'))
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <WebhooksPage />
      </QueryClientProvider>,
    )
    expect(await screen.findByText(/Webhook settings are unavailable/)).toBeTruthy()
    expect(screen.getByLabelText('New token label')).toBeTruthy()
  })

  it('reports the outcome of a test request for the selected source', async () => {
    testWebhook.mockResolvedValue({ ok: true, status: 202, session_key: 'hook:test:1' })
    mount(POPULATED)
    await openSource()
    fireEvent.click(screen.getByText('Send test request'))
    await waitFor(() => expect(testWebhook).toHaveBeenCalledWith(undefined, 'reviewer'))
    const banner = await screen.findByTestId('webhook-test-result')
    expect(banner.textContent).toContain('202')
    expect(banner.textContent).toContain('hook:test:1')
  })
})
