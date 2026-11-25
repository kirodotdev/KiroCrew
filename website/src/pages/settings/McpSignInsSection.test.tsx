import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

vi.mock('../../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { McpSignInsSection } from './McpSignInsSection'

type Mock = ReturnType<typeof vi.fn>
const configMock = api.kirocrewConfig as unknown as Mock
const patchMock = api.patchConfig as unknown as Mock

const RESTART_ENTRY = { path: 'agent.kas_persist_mcp_sign_in', type: 'bool', requiresRestart: true }

/** The restart pill reads GET /api/config/schema with a bare fetch: the key is boot-only. */
function stubSchema(entries: object[]) {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({ entries }),
  })) as unknown as typeof fetch)
}

beforeEach(() => {
  configMock.mockReset()
  patchMock.mockReset().mockResolvedValue({ ok: true })
  stubSchema([RESTART_ENTRY])
})

afterEach(() => vi.unstubAllGlobals())

describe('McpSignInsSection — remembering MCP sign-ins (#26891)', () => {
  it('shows the stored value and saves a flip through the config PATCH', async () => {
    configMock.mockResolvedValue({ agent: { acp_backend: 'kas', kas_persist_mcp_sign_in: false } })
    renderWithProviders(<McpSignInsSection />)

    const toggle = await screen.findByRole('switch', { name: 'Remember MCP sign-ins across sessions' })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    // A deep link waits for this anchor, since the section mounts after its reads.
    expect(document.querySelector('[data-setting-id="connections.remember-mcp-sign-ins-across-sessions"]')).toContainElement(toggle)
    expect(await screen.findByTestId('restart-required-badge')).toBeInTheDocument()

    fireEvent.click(toggle)
    await waitFor(() => expect(patchMock).toHaveBeenCalledWith('agent.kas_persist_mcp_sign_in', true))
  })

  it('still renders where Settings search lands when the schema offers no KAS harness', async () => {
    stubSchema([{ path: 'agent.acp_backend', type: 'enum', enumValues: ['kiro'] }, RESTART_ENTRY])
    configMock.mockResolvedValue({ agent: { acp_backend: 'kiro', kas_persist_mcp_sign_in: true } })
    renderWithProviders(<McpSignInsSection />)

    // The pill renders only once the schema has answered, so the switch below is
    // asserted after the harness list was read, not before it arrived.
    expect(await screen.findByTestId('restart-required-badge')).toBeInTheDocument()
    const toggle = await screen.findByRole('switch', { name: 'Remember MCP sign-ins across sessions' })
    expect(document.querySelector('[data-setting-id="connections.remember-mcp-sign-ins-across-sessions"]')).toContainElement(toggle)
  })
})
