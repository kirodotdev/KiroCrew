/**
 * A failed config load stays local to the Config tab.
 *
 * Found by using the real UI: the Config tab showed "Failed to fetch" and every
 * control vanished with it, including the ACP adapter card. The selector now
 * owns a dedicated Developer tab and reads /api/acp-backends independently, so
 * this component should render only its own error boundary.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', () => ({
  api: {
    // The failure mode observed in the browser: fetch rejects at the network
    // layer, so the message is a TypeError string rather than an HTTP body.
    kirocrewConfig: () => Promise.reject(new Error('Failed to fetch')),
  },
}))

vi.mock('../hooks/useProvider', () => ({
  useProvider: () => ({ labels: { agentTemplateField: 'Agent' } }),
}))

let KiroCrewCfgTab: React.ComponentType

beforeEach(async () => {
  vi.resetModules()
  KiroCrewCfgTab = (await import('../pages/overview/KiroCrewCfgTab')).default
})

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <KiroCrewCfgTab />
    </QueryClientProvider>,
  )
}

describe('KiroCrewCfgTab with a failed config query', () => {
  it('still surfaces the error rather than hiding it', async () => {
    mount()
    await waitFor(() => expect(screen.getByText(/Failed to fetch/)).toBeTruthy())
    expect(screen.queryByTestId('acp-card')).toBeNull()
  })
})
