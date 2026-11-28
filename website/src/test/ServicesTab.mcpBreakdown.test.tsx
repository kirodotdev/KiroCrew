/**
 * System > Services: the MCP Processes value must break only at its `·`
 * separators and name the provider in readable words.
 *
 * The breakdown is one interpolated catalog string, so in a narrow column it
 * wrapped at plain spaces and split mid-phrase ("36 (0 sandbox · 36" / "acp_cli ·
 * 0 mcp)"), and it printed the ACP adapter's internal id `acp_cli`. happy-dom does
 * no layout, so these pin the markup that decides where a line may break.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'

import { renderWithProviders } from './helpers'

vi.mock('../api/client', () => ({
  api: {
    system: () => Promise.resolve({
      hostname: 'test', os: 'linux', arch: 'x86_64', cpu_count: 8,
      load_1m: 0, load_5m: 0, load_15m: 0,
      ip: '127.0.0.1', net_rx_mb: 0, net_tx_mb: 0, net_rx_kbs: 0, net_tx_kbs: 0,
      python: '3.12', pid: 42, cwd: '/tmp',
      proc_mem_mb: 200, proc_cpu_pct: 1, child_processes: 0, thread_count: 10,
      mcp_total: 36,
      mcp_processes: { sandbox: 0, kiro_cli: 36, builder_mcp: 0 },
    }),
    mcpGatewayStatus: () => Promise.resolve({ enabled: false, running: false, ping_ok: false }),
    mcpGatewayMetrics: () => Promise.resolve({ running: false, backends: [] }),
  },
}))

import ServicesTab from '../pages/system/ServicesTab'

describe('ServicesTab MCP Processes breakdown', () => {
  it('keeps each separated part whole, so the value breaks only at `·`', async () => {
    renderWithProviders(<ServicesTab />)
    const value = await screen.findByTestId('services-mcp-breakdown')
    const parts = Array.from(value.querySelectorAll('span')).map(s => [s.textContent, s.className])
    expect(parts).toEqual([
      ['36 (0 sandbox', 'whitespace-nowrap'],
      ['36 acp', 'whitespace-nowrap'],
      ['0 mcp)', 'whitespace-nowrap'],
    ])
    expect(value.textContent).toBe('36 (0 sandbox · 36 acp · 0 mcp)')
  })

  it('lets a value too wide for its column drop under its label and shrink', async () => {
    renderWithProviders(<ServicesTab />)
    const value = await screen.findByTestId('services-mcp-breakdown')
    const cell = value.parentElement!
    expect(cell.className).toContain('min-w-0')
    expect(cell.className).toContain('ml-auto')
    expect(cell.parentElement!.className).toContain('flex-wrap')
  })
})
