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
  ['instances.warm_set_cap', { path: 'instances.warm_set_cap', type: 'integer', label: 'Warm Set Cap' }],
  ['custom.paths', { path: 'custom.paths', type: 'array', label: 'Paths' }],
  ['custom.title', { path: 'custom.title', type: 'string', label: 'Title' }],
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

  it('instances.warm_set_cap resolves to file mode (not unknown)', () => {
    const { container } = renderRef('instances.warm_set_cap')
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

const WIN_LAUNCHER = 'C:\\Kiro Crew\\resources\\bin\\kirocrew.cmd'
const V1_STATUS = {
  platform: 'Windows',
  config_set_hints: {
    version: 1,
    encoding: 'utf-8-base64',
    shells: [
      { id: 'powershell', prefix: `& '${WIN_LAUNCHER}'` },
      { id: 'cmd', prefix: `"${WIN_LAUNCHER}"` },
    ],
  },
}

/** Mounts a SettingRef over a gate cache that already holds `status` (the owner's status poll). */
function renderRefWithStatus(configKey: string, status: unknown, valuePlaceholder?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['kiro-prerequisite'], status)
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SettingRef configKey={configKey} {...(valuePlaceholder ? { valuePlaceholder } : {})} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SettingRef file-mode with launcher hints from the gateway', () => {
  it('renders one command per shell, built from the status prefixes', async () => {
    const user = userEvent.setup()
    renderRefWithStatus('instances.warm_set_cap', V1_STATUS, '7')
    await user.click(screen.getByRole('button'))

    const commands = [...document.querySelectorAll('code')].map((c) => c.textContent)
    // `7` is the sample the caller passed; its UTF-8 Base64 is `Nw==`.
    expect(commands).toContain(`& '${WIN_LAUNCHER}' config set instances.warm_set_cap --value-base64=Nw==`)
    expect(commands).toContain(`"${WIN_LAUNCHER}" config set instances.warm_set_cap --value-base64=Nw==`)
    expect(screen.queryByText('This command may need quoting changes in some shells.')).toBeNull()
  })

  it('selects a textarea for an array schema and a numeric input for an integer schema', async () => {
    const user = userEvent.setup()
    const list = renderRefWithStatus('custom.paths', V1_STATUS)
    await user.click(screen.getByRole('button'))
    expect((await screen.findByLabelText('Values for the command, one per line')).tagName).toBe('TEXTAREA')
    list.unmount()

    renderRefWithStatus('instances.warm_set_cap', V1_STATUS)
    await user.click(screen.getByRole('button'))
    const number = await screen.findByLabelText('Value for the command')
    expect(number.tagName).toBe('INPUT')
    expect(number.getAttribute('type')).toBe('number')
  })

  it('does not infer the control from the key name', async () => {
    const user = userEvent.setup()
    // A string setting whose name sounds like a list is still a single text value.
    renderRefWithStatus('custom.title', V1_STATUS)
    await user.click(screen.getByRole('button'))
    expect((await screen.findByLabelText('Value for the command')).tagName).toBe('INPUT')
  })
})

describe('SettingRef file-mode with no launcher hints', () => {
  it('shows the positional command and the neutral shell note, without the encoded flag', async () => {
    const user = userEvent.setup()
    // The status carries no hints: an older gateway, or a non-owner's redacted payload
    // from a current one, so the note must not say which.
    renderRefWithStatus('instances.enabled', { platform: 'Windows' })
    await user.click(screen.getByRole('button'))

    expect(await screen.findByText(/kirocrew config set instances\.enabled true/)).toBeTruthy()
    expect(screen.getByText('This command may need quoting changes in some shells.')).toBeTruthy()
    expect(document.body.textContent).not.toMatch(/Update Kiro Crew/)
    expect(document.body.textContent).not.toContain('--value-base64')
  })

  it('treats a malformed hint payload as absent', async () => {
    const user = userEvent.setup()
    renderRefWithStatus('instances.enabled', {
      platform: 'Windows',
      config_set_hints: { version: 1, encoding: 'utf-8-base64', shells: [{ id: 'fish', prefix: 'kirocrew' }] },
    })
    await user.click(screen.getByRole('button'))

    expect(await screen.findByText(/kirocrew config set instances\.enabled true/)).toBeTruthy()
    expect(document.body.textContent).not.toContain('--value-base64')
  })
})
