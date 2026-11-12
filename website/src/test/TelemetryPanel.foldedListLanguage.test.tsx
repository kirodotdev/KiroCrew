/**
 * The spend block folds every row past the top five into one "Other (N)" row,
 * and that row's tooltip names what it folded. The names are a list shown to a
 * person, so they must be joined the way the active language joins a list: a
 * fixed ", " is wrong in Chinese and Japanese, which use "、" and no space.
 */

import { describe, it, expect, vi, afterAll } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import TelemetryPanel from '../pages/TelemetryPanel'
import { api } from '../api/client'
// `/all` for the non-English catalogs: `../i18n` registers English only.
import { i18next } from '../i18n/all'

vi.mock('../api/client', () => ({ api: { telemetryStartup: vi.fn() } }))

const row = (name: string, credits: number) => ({
  name,
  credits,
  turns: 10,
  per_turn: credits / 10,
  share_pct: credits / 10,
  delta_pct: null,
})

// The same payload shape TelemetryPanel.spendDashboard.test.tsx mounts, with
// seven models so the two smallest fold into one row.
function payload() {
  return {
    enabled: true,
    window_days: 7,
    shard_count: 1,
    metrics_dir: '/metrics',
    startup: null,
    turn: null,
    context: null,
    other: [],
    cost: {
      window_days: 7,
      credits: 2100,
      turns: 70,
      per_turn: 30,
      prior_credits: 1000,
      prior_turns: 50,
      prior_per_turn: 20,
      delta_pct: 100,
      priciest: { credits: 90, slot: 'chat-1-1700000000', ts: '2026-08-05' },
      by_model: [
        row('m1', 600),
        row('m2', 500),
        row('m3', 400),
        row('m4', 300),
        row('m5', 200),
        row('m6', 70),
        row('m7', 30),
      ],
      by_channel: [],
      by_category: [],
      context_bands: [],
      conversations: [],
      conversation_count: 0,
      navigable_category: 'dashboard',
    },
  }
}

afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('TelemetryPanel spend block — the folded row names its rows in the active language', () => {
  it('joins the folded names with the Japanese list separator', async () => {
    await act(async () => {
      await i18next.changeLanguage('ja')
    })
    vi.mocked(api.telemetryStartup).mockResolvedValue(payload() as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <TelemetryPanel />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByTitle('m6、m7')).toBeInTheDocument())
    expect(screen.queryByTitle('m6, m7')).toBeNull()
  })
})
