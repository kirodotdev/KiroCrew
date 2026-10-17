import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { WebhooksPanel } from './WebhooksPanel'
import { api, type WebhooksView } from '../../api/client'

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigateSpy }
})

vi.mock('../../api/client', () => ({ api: { webhooks: vi.fn() } }))

/**
 * Minimal view: the panel reads only `enabled`, `switch_on` and `tokens`, so the
 * rest of `WebhooksView` is filled with inert values rather than realistic ones.
 */
function view(over: Partial<WebhooksView>): WebhooksView {
  return {
    enabled: false,
    switch_on: false,
    has_tokens: false,
    url: 'http://127.0.0.1:5476/api/hooks/agent',
    slots: { in_use: 0, max: 2 },
    limits: {
      session_key_prefix: 'hook-',
      message_max: 4000,
      timeout_default: 60,
      timeout_max: 300,
      max_concurrent: 2,
      signature_window_seconds: 300,
    },
    tokens: [],
    contexts: [],
    runs: [],
    ...over,
  } as WebhooksView
}

function mount() {
  // `retry: false` so the error case resolves on the first rejection instead of
  // outliving the test's timeout.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WebhooksPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { qc, ...utils }
}

const webhooks = vi.mocked(api.webhooks!)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('WebhooksPanel', () => {
  it('reports Enabled when tokens exist and the switch is on', async () => {
    webhooks.mockResolvedValue(
      view({ enabled: true, switch_on: true, has_tokens: true, tokens: [{}, {}] as never }),
    )
    mount()

    expect(await screen.findByText('Enabled')).toBeInTheDocument()
    // The count line only appears above zero, so two tokens must surface it.
    expect(await screen.findByText(/2 access tokens/i)).toBeInTheDocument()
  })

  it('distinguishes the kill switch from an unconfigured endpoint', async () => {
    // switch_on true with no tokens is the FIRST-RUN state, not a disabled one —
    // collapsing the two would report a fresh install as broken.
    webhooks.mockResolvedValue(view({ enabled: false, switch_on: true, has_tokens: false }))
    mount()

    expect(await screen.findByText('No token yet')).toBeInTheDocument()
    expect(screen.queryByText('Disabled')).not.toBeInTheDocument()
    // Redundant beside the badge, so it stays suppressed at zero.
    expect(screen.queryByText(/access tokens/i)).not.toBeInTheDocument()
  })

  it('badges each state with the full page\'s own copy, not a panel synonym', async () => {
    // The card is one click from that page; naming the same state differently on
    // each side made the reader re-orient on every visit.
    const en = (await import('../../i18n/locales/en.json')).default as {
      pages: { webhooksPage: Record<string, string> }
    }
    const page = en.pages.webhooksPage
    webhooks.mockResolvedValue(view({ enabled: true, switch_on: true, has_tokens: true }))
    mount()

    expect(await screen.findByText(page.on)).toBeInTheDocument()
    // Pins the pair that actually drifted: the panel used to say "Turned off"
    // and "No tokens yet" where the page says these.
    expect(page.off).toBe('Disabled')
    expect(page.no_credential_yet).toBe('No token yet')
  })

  it('reports Disabled when the switch itself is off', async () => {
    webhooks.mockResolvedValue(
      view({ enabled: false, switch_on: false, has_tokens: true, tokens: [{}] as never }),
    )
    mount()

    expect(await screen.findByText('Disabled')).toBeInTheDocument()
    expect(screen.queryByText('No token yet')).not.toBeInTheDocument()
  })

  it('says the read failed instead of rendering a stateless card', async () => {
    // Without this line a failed read looks identical to a healthy endpoint with
    // nothing configured — the badge and count both simply vanish.
    webhooks.mockRejectedValue(new Error('gateway unreachable'))
    mount()

    expect(await screen.findByText(/could not read webhook status/i)).toBeInTheDocument()
    for (const badge of ['Enabled', 'Disabled', 'No token yet']) {
      expect(screen.queryByText(badge)).not.toBeInTheDocument()
    }
  })

  it('hands off to the full page for the actual work', async () => {
    webhooks.mockResolvedValue(view({ enabled: true, switch_on: true, has_tokens: true }))
    mount()

    ;(await screen.findByRole('button', { name: /manage webhooks/i })).click()
    expect(navigateSpy).toHaveBeenCalledWith('/webhooks')
  })

  it('refetches when the page invalidates the shared "webhooks" prefix', async () => {
    // The panel deliberately keys off ['webhooks','settings-summary'] rather than
    // reusing the page's bare ['webhooks']: the two have different queryFns (the
    // page substitutes an empty view where this panel wants null so it can tell
    // "unreachable" from "unconfigured"), and sharing one key would let whichever
    // mounts first decide the other's shape. Invalidation still reaches it,
    // because react-query matches query keys by PREFIX — so minting a token on
    // the full page does not leave this badge stale for the 30s staleTime.
    webhooks.mockResolvedValue(view({ enabled: false, switch_on: true, has_tokens: false }))
    const { qc } = mount()
    expect(await screen.findByText('No token yet')).toBeInTheDocument()

    webhooks.mockResolvedValue(
      view({ enabled: true, switch_on: true, has_tokens: true, tokens: [{}] as never }),
    )
    await qc.invalidateQueries({ queryKey: ['webhooks'] })

    expect(await screen.findByText('Enabled')).toBeInTheDocument()
    await waitFor(() => expect(webhooks).toHaveBeenCalledTimes(2))
  })
})
