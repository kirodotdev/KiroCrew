/**
 * A config path the backend reports `lockedByPolicy` (an enterprise build ignores
 * its local value) shows the locked badge, and its popover explains the lock
 * instead of offering a set command that would do nothing.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SettingRef } from './SettingRef'
import type { SchemaEntry } from './resolveSettingRef'

const SCHEMA = new Map<string, SchemaEntry>([
  ['slack.use_tunnel_url', { path: 'slack.use_tunnel_url', type: 'boolean', lockedByPolicy: true }],
  ['slack.show_thinking', { path: 'slack.show_thinking', type: 'boolean' }],
])

vi.mock('../commandPalette/settingsRegistry.gen', () => ({ SETTINGS_REGISTRY: [] }))

function renderRef(configKey: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SettingRef configKey={configKey} schemaIndex={SCHEMA} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SettingRef locked by policy', () => {
  it('shows the badge and explains the lock instead of a set command', async () => {
    renderRef('slack.use_tunnel_url')
    expect(screen.getByTestId('locked-by-policy-badge')).toHaveTextContent('Locked by policy')
    await userEvent.setup().click(screen.getByRole('button'))
    expect(await screen.findByText(/organization's policy controls this setting/i)).toBeInTheDocument()
    expect(screen.queryByText(/kirocrew config set/)).not.toBeInTheDocument()
  })

  it('shows no badge for a path that is not locked', () => {
    renderRef('slack.show_thinking')
    expect(screen.queryByTestId('locked-by-policy-badge')).not.toBeInTheDocument()
  })
})
