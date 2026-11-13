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

beforeEach(() => {
  configMock.mockReset()
  patchMock.mockReset().mockResolvedValue({ ok: true })
  // The section reads GET /api/config/schema with a bare fetch: which harnesses
  // this build offers, and that the key is boot-only (the restart pill).
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({
      entries: [
        { path: 'agent.acp_backend', type: 'enum', enumValues: ['kas', 'kiro'] },
        { path: 'agent.kas_persist_mcp_sign_in', type: 'bool', requiresRestart: true },
      ],
    }),
  })) as unknown as typeof fetch)
})

afterEach(() => vi.unstubAllGlobals())

describe('McpSignInsSection — remembering MCP sign-ins (#26891)', () => {
  it('shows the stored value and saves a flip through the config PATCH', async () => {
    configMock.mockResolvedValue({ agent: { acp_backend: 'kas', kas_persist_mcp_sign_in: false } })
    renderWithProviders(<McpSignInsSection />)

    const toggle = await screen.findByRole('switch', { name: 'Remember MCP sign-ins across sessions' })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(await screen.findByTestId('restart-required-badge')).toBeInTheDocument()

    fireEvent.click(toggle)
    await waitFor(() => expect(patchMock).toHaveBeenCalledWith('agent.kas_persist_mcp_sign_in', true))
  })
})
