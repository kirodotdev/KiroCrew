// SpecDetail must not mount the embedded chat before the spec's detail loads.
// The chat talks to /api/chat, and for a spec DISCOVERED on disk the worker slot
// does not exist yet — whichever endpoint creates it decides whether it is scoped
// to this app and to the project directory. The app's own detail endpoint scopes
// it; /api/chat would create it bare and an approved tool would then run in the
// gateway's own directory.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../apps/spec-builder/components/ChatColumn', () => ({
  default: () => <div data-testid="chat-column" />,
}))

import SpecDetail from '../apps/spec-builder/components/SpecDetail'

let queryClient: QueryClient

function renderDetail() {
  return render(
    <QueryClientProvider client={queryClient}>
      <SpecDetail name="found" setErr={() => {}} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  localStorage.clear()
})

afterEach(() => { vi.restoreAllMocks() })

describe('SpecDetail chat gating', () => {
  it('withholds the chat until the detail response lands', async () => {
    let resolveDetail: ((v: unknown) => void) | undefined
    const pending = new Promise((res) => { resolveDetail = res })
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => pending))

    renderDetail()

    // Detail still in flight: no chat, and a skeleton in its place.
    expect(screen.queryByTestId('chat-column')).not.toBeInTheDocument()
    expect(screen.getByText(/Loading the conversation/i)).toBeInTheDocument()

    resolveDetail?.({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify({
        name: 'found',
        working_dir: '/w',
        spec_dir: '/w/.kiro/specs/found',
        spec_type: 'feature',
        status: 'planning',
        phase: 'requirements',
        running: false,
        files: { 'requirements.md': 'The system SHALL work.' },
        state: null,
        context: {},
      })),
    })

    await waitFor(() => {
      expect(screen.getByTestId('chat-column')).toBeInTheDocument()
    })
  })
})
