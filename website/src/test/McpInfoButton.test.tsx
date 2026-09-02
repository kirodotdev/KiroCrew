import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import McpInfoButton from '../pages/chat/McpInfoButton'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    mcpActive: vi.fn().mockResolvedValue([
      { name: 'builder-mcp', enabled: true },
      { name: 'slack-mcp', enabled: false },
    ]),
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { tool_search: true } }),
  },
}))

describe('McpInfoButton', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders info button', () => {
    render(<McpInfoButton />)
    expect(screen.getByTitle('Session MCP servers')).toBeInTheDocument()
  })

  it('shows server list on click', async () => {
    render(<McpInfoButton />)
    fireEvent.click(screen.getByTitle('Session MCP servers'))
    await waitFor(() => {
      expect(screen.getByText('builder-mcp')).toBeInTheDocument()
      expect(screen.getByText('slack-mcp')).toBeInTheDocument()
    })
  })

  it('shows disabled label for disabled servers', async () => {
    render(<McpInfoButton />)
    fireEvent.click(screen.getByTitle('Session MCP servers'))
    await waitFor(() => {
      expect(screen.getByText('disabled')).toBeInTheDocument()
    })
  })

  it('closes on outside click', async () => {
    render(<McpInfoButton />)
    fireEvent.click(screen.getByTitle('Session MCP servers'))
    await waitFor(() => expect(screen.getByText('builder-mcp')).toBeInTheDocument())
    fireEvent.pointerDown(document.body)
    expect(screen.queryByText('builder-mcp')).not.toBeInTheDocument()
  })

  it('shows the "Deferred" Tool Search status when tool_search is on', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { tool_search: true } })
    render(<McpInfoButton />)
    fireEvent.click(screen.getByTitle('Session MCP servers'))
    await waitFor(() => expect(screen.getByText('Tool Search · Deferred')).toBeInTheDocument())
    expect(screen.queryByText('Tool Search · Fully loaded')).not.toBeInTheDocument()
  })

  it('shows the "Fully loaded" Tool Search status when tool_search is off', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { tool_search: false } })
    render(<McpInfoButton />)
    fireEvent.click(screen.getByTitle('Session MCP servers'))
    await waitFor(() => expect(screen.getByText('Tool Search · Fully loaded')).toBeInTheDocument())
    expect(screen.queryByText('Tool Search · Deferred')).not.toBeInTheDocument()
  })
})
