/**
 * The order-load-failure notice in the shared model list renderer.
 *
 * `useAvailableModels` deliberately falls back to the backend order when the
 * `['kirocrewConfig']` read fails, so the pickers never break — but
 * errors-use-error-notice requires the failure to be VISIBLE, and this is the
 * one renderer every picker mounts, so the decision is made once here rather
 * than in the 11 hosts. The durable, actionable surface for the same failure
 * (the ModelOrderEditor on the dropdown's "Model order" page) has its own
 * loadFailed notice and tests.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import '../i18n/all'
import ModelDropdownList from '../components/ModelDropdownList'

const { kirocrewConfigMock } = vi.hoisted(() => ({
  kirocrewConfigMock: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: { kirocrewConfig: kirocrewConfigMock } }))

const MODELS = [
  { name: 'auto', description: '' },
  { name: 'opus', description: 'Opus' },
]

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('ModelDropdownList — saved-order load failure', () => {
  it('surfaces a failed config read once, above the list', async () => {
    kirocrewConfigMock.mockRejectedValue(new Error('boom'))
    wrap(<ModelDropdownList models={MODELS} activeModel="auto" onSelect={() => {}} />)
    expect(await screen.findByText(/Couldn't load your saved model order/)).toBeInTheDocument()
    // The list itself still renders — the fallback order stays usable.
    expect(screen.getByText('opus')).toBeInTheDocument()
  })

  it('renders no notice when the config read succeeds', async () => {
    kirocrewConfigMock.mockResolvedValue({ agent: { model_order: [] } })
    wrap(<ModelDropdownList models={MODELS} activeModel="auto" onSelect={() => {}} />)
    expect(await screen.findByText('opus')).toBeInTheDocument()
    expect(screen.queryByText(/Couldn't load your saved model order/)).not.toBeInTheDocument()
  })
})
