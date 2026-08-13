/**
 * Component test: verifies that SettingRef renders file-mode popover with CLI
 * command text when the hook provides schema data for keys like instances.enabled.
 *
 * Mocks useConfigSchema to return the schema map directly, simulating what
 * happens after GET /api/config/schema succeeds.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SettingRef } from './SettingRef'
import type { SchemaEntry } from './resolveSettingRef'

// Mock useConfigSchema to return a populated schema map
const MOCK_SCHEMA = new Map<string, SchemaEntry>([
  ['instances.enabled', { path: 'instances.enabled', type: 'boolean', label: 'Enabled', help: 'Enable multi-instance management.' }],
  ['telemetry.beacon_enabled', { path: 'telemetry.beacon_enabled', type: 'boolean', label: 'Anonymous Usage Beacon' }],
  ['instances.warm_set_cap', { path: 'instances.warm_set_cap', type: 'integer', label: 'Warm Set Cap' }],
])

vi.mock('./useConfigSchema', () => ({
  useConfigSchema: () => MOCK_SCHEMA,
}))

// Mock registry: these keys are NOT in the UI registry, so they resolve to file mode
vi.mock('../commandPalette/settingsRegistry.gen', () => ({
  SETTINGS_REGISTRY: [],
}))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function renderRef(configKey: string) {
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SettingRef configKey={configKey} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SettingRef file-mode with useConfigSchema hook', () => {
  it('instances.enabled renders as popover trigger button (file mode)', () => {
    const { container } = renderRef('instances.enabled')
    const button = container.querySelector('button')
    expect(button).not.toBeNull()
    // No <a> link — file mode
    expect(container.querySelector('a')).toBeNull()
    expect(container.textContent).toContain('instances.enabled')
  })

  it('instances.enabled popover shows CLI command with true for boolean type', async () => {
    const user = userEvent.setup()
    renderRef('instances.enabled')
    const button = screen.getByRole('button')
    await user.click(button)
    // After click, popover should show the CLI command with boolean placeholder
    expect(await screen.findByText(/kirocrew config set instances\.enabled true/)).toBeTruthy()
  })

  it('telemetry.beacon_enabled resolves to file mode (not unknown)', () => {
    const { container } = renderRef('telemetry.beacon_enabled')
    // File mode = button, not plain code without interactivity
    const button = container.querySelector('button')
    expect(button).not.toBeNull()
  })

  it('unknown key still renders plain code with no button', () => {
    const { container } = renderRef('totally.nonexistent.key')
    expect(container.querySelector('button')).toBeNull()
    expect(container.querySelector('a')).toBeNull()
    const code = container.querySelector('code')
    expect(code).not.toBeNull()
    expect(code!.textContent).toBe('totally.nonexistent.key')
  })
})
