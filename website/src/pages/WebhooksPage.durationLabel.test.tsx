import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { WebhooksView } from '../api/client'

function viewWithRun(durationMs: number): WebhooksView {
  return {
    enabled: true,
    switch_on: true,
    has_tokens: true,
    url: 'https://example.test/api/hooks/agent',
    slots: { in_use: 0, max: 6 },
    limits: {
      session_key_prefix: 'hook:',
      message_max: 49999,
      timeout_default: 599,
      timeout_max: 3593,
      max_concurrent: 6,
      body_max_bytes: 262144,
      signature_window_seconds: 300,
    },
    tokens: [],
    contexts: [],
    runs: [
      {
        id: 'run-1',
        hook_id: 'hook:my-job',
        session_key: 'hook:my-job',
        name: 'CI',
        outcome: 'completed',
        started_at: 1_700_000_000,
        duration_ms: durationMs,
        result_chars: 10,
        token_id: null,
        delivered: true,
        detail: '',
      },
    ],
  }
}

const webhooks = vi.fn<[], Promise<WebhooksView>>()

vi.mock('../api/client', () => ({
  api: {
    webhooks: () => webhooks(),
    agentsInstalled: () => Promise.resolve([]),
  },
  // WebhooksPage imports ApiError as a value from this module.
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status = 0, body = '') {
      super(body)
      this.status = status
      this.body = body
    }
  },
}))

/** Renders the page with one completed run, opens Activity and returns the
 *  run's rail-row text, whose subtitle carries `durationLabel(duration_ms)`.
 *  NBSP (fmtUnit's digit/unit space) is normalized to a plain space. */
async function runRowText(durationMs: number): Promise<string> {
  webhooks.mockResolvedValue(viewWithRun(durationMs))
  const { default: WebhooksPage } = await import('./WebhooksPage')
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WebhooksPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  fireEvent.click(await waitFor(() => screen.getByRole('tab', { name: /activity/i })))
  const row = await waitFor(() => screen.getByTestId('webhook-row-run-run-1'))
  return (row.textContent ?? '').replace(/\u00A0/g, ' ')
}

describe('WebhooksPage run duration label', () => {
  beforeEach(() => {
    webhooks.mockReset()
  })

  it('carries a rounded-up 60th second into the minute (119.6s reads 2m 0s, not 1m 60s)', async () => {
    const text = await runRowText(119_600)
    expect(text).not.toMatch(/\b1m 60s\b/)
    expect(text).toMatch(/— 2m 0s ·/)
  })

  it('rolls a run that rounds up to 60s over to 1m 0s (59.6s)', async () => {
    const text = await runRowText(59_600)
    expect(text).not.toMatch(/— 60s ·/)
    expect(text).toMatch(/— 1m 0s ·/)
  })

  it('control: a run that still rounds below a minute keeps whole seconds (59.4s reads 59s)', async () => {
    expect(await runRowText(59_400)).toMatch(/— 59s ·/)
  })

  it('control: a short run keeps its tenth (4.2s)', async () => {
    expect(await runRowText(4_200)).toMatch(/— 4\.2s ·/)
  })

  it('control: a clean 1m 1s run renders correctly', async () => {
    expect(await runRowText(61_000)).toMatch(/— 1m 1s ·/)
  })
})
