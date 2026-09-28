import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render as rtlRender, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import McpInfoButton from '../pages/chat/McpInfoButton'
import { SESSION_DOT_CLASS } from '../pages/chat/McpToolsPanel'
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

// The popover's two reads are react-query queries (per website/AGENTS.md), so
// the component needs a provider; a fresh client per render keeps one test's
// cached answer out of the next.
function render(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

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

  // #10320: `enabled` is config only, so the mark must not wear the `ok` hue.
  it('marks a configured server no-report, not the ok status hue', async () => {
    render(<McpInfoButton />)
    fireEvent.click(screen.getByTitle('Session MCP servers'))
    await waitFor(() => expect(screen.getByText('builder-mcp')).toBeInTheDocument())
    const dot = (name: string) =>
      screen.getByText(name).parentElement!.querySelector('span.rounded-full')!
    expect(dot('builder-mcp').className).not.toContain('bg-ok')
    expect(dot('builder-mcp').className).toContain(SESSION_DOT_CLASS.no_report)
    expect(dot('slack-mcp').className).toContain('bg-muted')
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
