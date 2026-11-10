// The "Refresh Tools" button in the MCP settings section keeps keyboard focus
// while a tool discovery runs. It used native `disabled` while the discovery
// ran, which takes the button out of the tab order and drops a keyboard user's
// focus to the window until discovery finishes.
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

const mochiApi = vi.hoisted(() => ({
  getMcpServers: vi.fn(),
  discoverMcpTools: vi.fn(),
}))
vi.mock('../mochiApi', () => ({ api: mochiApi }))

import { McpSection } from './SettingsPanel'
import type { AppConfig } from '../shared/types'

const SERVER = {
  name: 'acme',
  core: true,
  enabled: true,
  agents: ['chat'] as ('chat' | 'bg')[],
  autoApprove: [],
  disabledTools: [],
  chatPolicy: { autoApprove: [], disabledTools: [] },
  bgPolicy: { autoApprove: [], disabledTools: [] },
}

const config = { mochi: { extraMcpServers: [] } } as unknown as AppConfig

beforeEach(() => {
  mochiApi.getMcpServers.mockReset()
  mochiApi.discoverMcpTools.mockReset()
})

describe('McpSection Refresh Tools button while discovering', () => {
  it('keeps keyboard focus on Refresh Tools while it runs, and ignores a press then', async () => {
    mochiApi.getMcpServers.mockResolvedValue([SERVER])
    // The first discovery (auto-fired when the row is expanded) settles; the
    // second (the Refresh press under test) hangs so the busy state holds.
    let holdRefresh: () => void = () => {}
    mochiApi.discoverMcpTools
      .mockResolvedValueOnce({ tools: [], fromCache: false })
      .mockReturnValueOnce(new Promise<void>((resolve) => { holdRefresh = () => resolve() }))

    await act(async () => {
      render(<McpSection config={config} editMochi={() => {}} trustMode="normal" />)
    })

    // Expand the server row; this auto-runs the first discovery. Let it settle.
    const expand = await screen.findByRole('button', { expanded: false })
    await act(async () => { fireEvent.click(expand) })
    await waitFor(() => expect(mochiApi.discoverMcpTools).toHaveBeenCalledTimes(1))

    const button = await screen.findByRole('button', { name: 'Refresh Tools' })
    button.focus()
    await act(async () => { fireEvent.click(button) })

    await waitFor(() => expect(button).toHaveAttribute('aria-disabled', 'true'))
    expect(button).not.toBeDisabled()
    expect(document.activeElement).toBe(button)

    // A second press while the discovery runs sends nothing more.
    fireEvent.click(button)
    expect(mochiApi.discoverMcpTools).toHaveBeenCalledTimes(2)

    await act(async () => { holdRefresh() })
  })
})
