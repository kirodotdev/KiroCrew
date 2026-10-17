/**
 * WebhooksPage's copy affordances (the reveal-banner token/signing-secret
 * fields and buttons, and the request-example copy button) must gate their
 * confirmation on the shared clipboard helper's returned boolean, never on
 * the write having merely been attempted. Mocking `../utils/clipboard`
 * directly (rather than stubbing the DOM clipboard) isolates that contract
 * from jsdom's lack of a real `execCommand` backing.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { WebhooksView } from '../api/client'

const webhooks = vi.fn()
const agentsInstalled = vi.fn()
const createWebhookToken = vi.fn()

vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

vi.mock('../api/client', () => ({
  api: {
    webhooks: (...args: unknown[]) => webhooks(...args),
    agentsInstalled: (...args: unknown[]) => agentsInstalled(...args),
    createWebhookToken: (...args: unknown[]) => createWebhookToken(...args),
    updateWebhookToken: vi.fn(),
    deleteWebhookToken: vi.fn(),
    deleteWebhookContext: vi.fn(),
    testWebhook: vi.fn(),
    setWebhooksEnabled: vi.fn(),
  },
}))

vi.mock('../utils/clipboard', () => ({
  // Resolves TRUE by default: the real signature is `Promise<boolean>`, and a
  // bare `vi.fn()` returning undefined is falsy, so it could not tell a
  // successful clipboard write from a failed one.
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyCode: vi.fn().mockResolvedValue(true),
}))

import WebhooksPage from '../pages/WebhooksPage'
import { copyToClipboard, copyCode } from '../utils/clipboard'

const NOW = Math.floor(Date.now() / 1000)
const AGENTS = [
  {
    name: 'reviewer', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default',
    model: '', description: 'Reviews code changes', source: 'user',
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
  slots: { in_use: 1, max: 6 },
  tokens: [
    {
      id: 'wht_review', label: 'Review Bot', display_prefix: 'kc_whk_4f2b', last4: '1f3a',
      created_at: NOW - 7200, last_used_at: NOW - 480, require_signature: true,
      agent: 'reviewer', enabled: true, legacy: false,
    },
  ],
  contexts: [], runs: [],
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

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  agentsInstalled.mockResolvedValue(AGENTS)
  vi.mocked(copyToClipboard).mockReset().mockResolvedValue(true)
  vi.mocked(copyCode).mockReset().mockResolvedValue(true)
})
afterEach(cleanup)

describe('WebhooksPage — one-time secret reveal copy affordances', () => {
  async function revealNewToken() {
    createWebhookToken.mockResolvedValue({
      ok: true,
      token: 'kc_whk_TESTSECRET0123456789abcdefghij',
      signing_secret: 'kc_whs_SIGNSECRET0123456789abcdefghij',
      entry: { ...POPULATED.tokens[0], id: 'wht_new', label: 'CI Bot' },
    })
    mount(EMPTY)
    await screen.findByLabelText('New token label')
    fireEvent.change(screen.getByLabelText('New token label'), { target: { value: 'CI Bot' } })
    fireEvent.click(screen.getByText('Generate token'))
    await screen.findByTestId('webhook-token-reveal')
  }

  it('confirms the CopyField bearer-token icon only once the write actually lands', async () => {
    await revealNewToken()
    const reveal = within(screen.getByTestId('webhook-token-reveal'))
    const fieldCopy = reveal.getByLabelText('Copy webhook token')
    fireEvent.click(fieldCopy)
    await waitFor(() =>
      expect(copyToClipboard).toHaveBeenCalledWith('kc_whk_TESTSECRET0123456789abcdefghij'),
    )
    await waitFor(() => expect(fieldCopy.querySelector('.lucide-check')).not.toBeNull())
  })

  it('leaves the CopyField icon as Copy when the write fails outright', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    await revealNewToken()
    const reveal = within(screen.getByTestId('webhook-token-reveal'))
    const fieldCopy = reveal.getByLabelText('Copy webhook token')
    fireEvent.click(fieldCopy)
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalled())
    expect(fieldCopy.querySelector('.lucide-check')).toBeNull()
  })

  it('confirms the "Copy token" button only on a successful write', async () => {
    await revealNewToken()
    const copyTokenBtn = screen.getByRole('button', { name: /Copy token/ })
    fireEvent.click(copyTokenBtn)
    await waitFor(() =>
      expect(copyToClipboard).toHaveBeenCalledWith('kc_whk_TESTSECRET0123456789abcdefghij'),
    )
    await waitFor(() => expect(copyTokenBtn.querySelector('.lucide-check')).not.toBeNull())
  })

  it('confirms the "Copy signing secret" button independently of the token button', async () => {
    await revealNewToken()
    const copySigningBtn = screen.getByTestId('webhook-reveal-copy-signing')
    fireEvent.click(copySigningBtn)
    await waitFor(() =>
      expect(copyToClipboard).toHaveBeenCalledWith('kc_whs_SIGNSECRET0123456789abcdefghij'),
    )
    await waitFor(() => expect(copySigningBtn.querySelector('.lucide-check')).not.toBeNull())
    // The token button never fired, so it must not have flipped too.
    expect(screen.getByRole('button', { name: /Copy token/ }).querySelector('.lucide-check')).toBeNull()
  })

  it('shows no confirmation on the "Copy signing secret" button when the write fails', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    await revealNewToken()
    const copySigningBtn = screen.getByTestId('webhook-reveal-copy-signing')
    fireEvent.click(copySigningBtn)
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalled())
    expect(copySigningBtn.querySelector('.lucide-check')).toBeNull()
  })
})

describe('WebhooksPage — request example copy button', () => {
  it('routes the example through copyCode (a pasted-at-a-prompt command) and confirms on success', async () => {
    mount(POPULATED)
    await openSource('wht_review')
    fireEvent.click(screen.getByText('Request example'))
    const copyBtn = screen.getByRole('button', { name: 'Copy' })
    fireEvent.click(copyBtn)
    await waitFor(() => expect(copyCode).toHaveBeenCalled())
    await waitFor(() => expect(copyBtn.querySelector('.lucide-check')).not.toBeNull())
  })

  it('shows no confirmation on the request-example copy button when the write fails', async () => {
    vi.mocked(copyCode).mockResolvedValue(false)
    mount(POPULATED)
    await openSource('wht_review')
    fireEvent.click(screen.getByText('Request example'))
    const copyBtn = screen.getByRole('button', { name: 'Copy' })
    fireEvent.click(copyBtn)
    await waitFor(() => expect(copyCode).toHaveBeenCalled())
    expect(copyBtn.querySelector('.lucide-check')).toBeNull()
  })
})
