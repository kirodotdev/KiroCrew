import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import McpToolsPanel, { SESSION_DOT_CLASS } from '../pages/chat/McpToolsPanel'

const servers = [{ name: 'slack-mcp', enabled: true }]
const toolsByServer = {
  'slack-mcp': { tools: ['post_message', 'delete_message', 'legacy'], disabledTools: ['legacy'] },
}

describe('McpToolsPanel', () => {
  it('renders the Tool Search mode line (deferred)', () => {
    render(
      <McpToolsPanel servers={servers} toolsByServer={toolsByServer} loaded={new Set()} toolSearchOn={true} loading={false} />,
    )
    expect(screen.getByText('Tool Search · Deferred')).toBeInTheDocument()
  })

  it('shows a per-server loaded/total count and marks each tool loaded / deferred / disabled', () => {
    const loaded = new Set(['slack-mcp::post_message'])
    render(
      <McpToolsPanel servers={servers} toolsByServer={toolsByServer} loaded={loaded} toolSearchOn={true} loading={false} />,
    )
    // 1 of 2 loadable loaded (legacy is disabled → excluded from the denominator;
    // delete_message deferred; post_message loaded)
    expect(screen.getByText('1/2')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /slack-mcp/ }))
    expect(screen.getByText('post_message')).toBeInTheDocument()
    expect(screen.getByTitle('Loaded this session')).toBeInTheDocument()
    expect(screen.getByTitle('Deferred')).toBeInTheDocument()
    expect(screen.getByTitle('Disabled')).toBeInTheDocument()
  })

  // #10320: with no session report the config-only `enabled` flag must not read as `ok`.
  it('marks a configured server no-report when no session report has arrived', () => {
    render(
      <McpToolsPanel
        servers={[{ name: 'slack-mcp', enabled: true }, { name: 'off-mcp', enabled: false }]}
        toolsByServer={toolsByServer}
        loaded={new Set()}
        toolSearchOn={true}
        loading={false}
      />,
    )
    const dot = (name: string) =>
      screen.getByRole('button', { name: new RegExp(name) }).querySelector('span.rounded-full')!
    expect(dot('slack-mcp').className).not.toContain('bg-ok')
    expect(dot('slack-mcp').className).toContain(SESSION_DOT_CLASS.no_report)
    expect(dot('off-mcp').className).toContain('bg-muted')
  })

  it('marks every non-disabled tool active when tool search is off', () => {
    render(
      <McpToolsPanel servers={servers} toolsByServer={toolsByServer} loaded={new Set()} toolSearchOn={false} loading={false} />,
    )
    expect(screen.getByText('Tool Search · Fully loaded')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /slack-mcp/ }))
    expect(screen.getAllByTitle('Loaded this session').length).toBe(2)
    expect(screen.getByTitle('Disabled')).toBeInTheDocument()
  })

  it('tells the user to start a new session for a server that had no credentials', () => {
    const report = {
      configured: [],
      ready: [],
      failed: ['aws-mcp', 'slack-mcp'],
      awaiting_auth: [],
      failures: { 'aws-mcp': 'No AWS credentials available', 'slack-mcp': 'spawn ENOENT' },
      restart_to_load: ['aws-mcp'],
    }
    render(
      <McpToolsPanel
        servers={[{ name: 'aws-mcp', enabled: true }, { name: 'slack-mcp', enabled: true }]}
        toolsByServer={{}}
        loaded={new Set()}
        toolSearchOn={false}
        loading={false}
        sessionReport={report}
      />,
    )
    const hints = screen.getAllByText(/start a new session to load/)
    expect(hints).toHaveLength(1)
    expect(hints[0].textContent).toContain('aws-mcp')
  })

  it('shows no restart hint when an older gateway sends no restart list', () => {
    const report = {
      configured: [],
      ready: [],
      failed: ['aws-mcp'],
      awaiting_auth: [],
      failures: { 'aws-mcp': 'No AWS credentials available' },
    }
    render(
      <McpToolsPanel
        servers={[{ name: 'aws-mcp', enabled: true }]}
        toolsByServer={{}}
        loaded={new Set()}
        toolSearchOn={false}
        loading={false}
        sessionReport={report}
      />,
    )
    expect(screen.queryByText(/start a new session to load/)).toBeNull()
  })
})
