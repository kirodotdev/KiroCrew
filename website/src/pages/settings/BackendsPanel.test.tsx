import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../../api/client'
import { BackendsPanel } from './BackendsPanel'

function mount(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

const PAYLOAD = {
  backends: [
    { id: '', label: 'Kiro CLI', is_global_default: true },
    { id: 'claude', label: 'Claude Code', is_global_default: false },
  ],
  invalid: [{ id: 'bad', label: 'Bad Descriptor', reasons: ['missing executable', 'no argv rule'] }],
  unroutable: [{ id: 'no-route', label: 'No Route', reason: 'no recognized routing declared' }],
}

describe('BackendsPanel', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('lists selectable, unroutable and invalid rows with reasons and marks the default', async () => {
    vi.spyOn(api, 'backends').mockResolvedValue(PAYLOAD)
    mount(<BackendsPanel />)
    const inv = await screen.findByTestId('backend-inventory')
    // Selectable rows, keyed by id (kiro-cli's '' folds to the 'kiro' testid).
    expect(within(inv).getByTestId('backend-row-kiro')).toBeTruthy()
    expect(within(inv).getByTestId('backend-row-claude')).toBeTruthy()
    // The default is marked, and only on the default row.
    expect(within(inv).getByTestId('backend-default-kiro')).toBeTruthy()
    expect(within(inv).queryByTestId('backend-default-claude')).toBeNull()
    // Diagnostic rows carry their reasons.
    const unroutable = within(inv).getByTestId('backend-unroutable-no-route')
    expect(within(unroutable).getByText(/no recognized routing declared/)).toBeTruthy()
    const invalid = within(inv).getByTestId('backend-invalid-bad')
    expect(within(invalid).getByText(/missing executable/)).toBeTruthy()
    expect(within(invalid).getByText(/no argv rule/)).toBeTruthy()
  })

  it('renders nothing but the unavailable notice on a failed fetch', async () => {
    vi.spyOn(api, 'backends').mockRejectedValue(new Error('offline'))
    mount(<BackendsPanel />)
    await waitFor(() => expect(screen.queryByTestId('backend-inventory')).toBeNull())
    // No stale rows are shown.
    expect(screen.queryByTestId('backend-row-kiro')).toBeNull()
  })
})
