import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import McpToolsPanel from '../pages/chat/McpToolsPanel'

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

  it('marks every non-disabled tool active when tool search is off', () => {
    render(
      <McpToolsPanel servers={servers} toolsByServer={toolsByServer} loaded={new Set()} toolSearchOn={false} loading={false} />,
    )
    expect(screen.getByText('Tool Search · Fully loaded')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /slack-mcp/ }))
    expect(screen.getAllByTitle('Loaded this session').length).toBe(2)
    expect(screen.getByTitle('Disabled')).toBeInTheDocument()
  })
})
