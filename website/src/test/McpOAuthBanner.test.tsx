import { describe, it, expect } from 'vitest'
import { render as rtlRender, screen } from '@testing-library/react'
import type { ReactElement } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import McpOAuthBanner, { renderMcpOAuthMessage } from '../pages/chat/McpOAuthBanner'
import type { ChatMessage } from '../types'

// The banner's relay affordance reads the query client (it invalidates
// ['mcp-servers'] after a successful relay), so every render needs a provider.
const render = (ui: ReactElement) =>
  rtlRender(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>)

describe('McpOAuthBanner', () => {
  describe('needs-auth state', () => {
    it('renders Authorize link with the provided URL', () => {
      render(
        <McpOAuthBanner
          serverName="linear"
          oauthUrl="https://mcp.linear.app/authorize"
          completed={false}
        />,
      )
      const link = screen.getByRole('link', { name: /Authorize linear/i })
      expect(link).toHaveAttribute('href', 'https://mcp.linear.app/authorize')
      expect(link).toHaveAttribute('target', '_blank')
      expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    })

    it('falls back to "MCP server" label when serverName is empty', () => {
      render(
        <McpOAuthBanner
          serverName=""
          oauthUrl="https://mcp.example.com/authorize"
          completed={false}
        />,
      )
      expect(screen.getByText(/requires authentication/)).toBeInTheDocument()
    })

    it('does NOT render when oauthUrl uses an unsafe scheme', () => {
      // Defense-in-depth: backend already gates on this, but the component
      // must also refuse to render <a href> for non-http(s) URLs.
      const { container } = render(
        <McpOAuthBanner
          serverName="evil"
          oauthUrl="javascript:alert(1)"
          completed={false}
        />,
      )
      expect(container.firstChild).toBeNull()
    })

    it('does NOT render when oauthUrl is empty (and not completed/failed)', () => {
      const { container } = render(
        <McpOAuthBanner serverName="x" oauthUrl="" completed={false} />,
      )
      expect(container.firstChild).toBeNull()
    })
  })

  describe('completed state', () => {
    it('shows authenticated message when completed=true', () => {
      render(
        <McpOAuthBanner
          serverName="linear"
          oauthUrl="https://mcp.linear.app/authorize"
          completed={true}
        />,
      )
      expect(screen.getByText(/authenticated/)).toBeInTheDocument()
      expect(screen.queryByRole('link', { name: /Authorize/i })).not.toBeInTheDocument()
    })
  })

  describe('failed state', () => {
    it('shows failure message with error string', () => {
      render(
        <McpOAuthBanner
          serverName="linear"
          oauthUrl=""
          completed={false}
          failed={true}
          error="dns failed"
        />,
      )
      expect(screen.getByText(/authentication failed: dns failed/i)).toBeInTheDocument()
    })

    it('shows failure message without error suffix when error is empty', () => {
      render(
        <McpOAuthBanner
          serverName="linear"
          oauthUrl=""
          completed={false}
          failed={true}
        />,
      )
      expect(screen.getByText(/authentication failed\./i)).toBeInTheDocument()
    })

    it('failed state takes precedence over completed', () => {
      // If both flags are set, failed wins (last write would have been the
      // failure event).
      render(
        <McpOAuthBanner
          serverName="linear"
          oauthUrl=""
          completed={true}
          failed={true}
          error="boom"
        />,
      )
      expect(screen.getByText(/authentication failed: boom/i)).toBeInTheDocument()
      expect(screen.queryByText(/^.*authenticated\.$/)).not.toBeInTheDocument()
    })
  })
})

describe('renderMcpOAuthMessage', () => {
  function makeMsg(meta: Record<string, unknown>): ChatMessage {
    return { role: 'mcp_oauth', content: '', cls: 'msg msg-info', meta }
  }

  it('returns null when there is nothing to show', () => {
    // No oauth_url, not completed, not failed → nothing to render.
    expect(renderMcpOAuthMessage(makeMsg({}))).toBeNull()
  })

  it('renders banner when oauth_url is present', () => {
    const node = renderMcpOAuthMessage(
      makeMsg({
        server_name: 'linear',
        oauth_url: 'https://mcp.linear.app/authorize',
      }),
    )
    expect(node).not.toBeNull()
    render(<>{node}</>)
    expect(screen.getByRole('link', { name: /Authorize linear/i })).toBeInTheDocument()
  })

  it('renders authenticated banner when meta.completed is true', () => {
    const node = renderMcpOAuthMessage(
      makeMsg({ server_name: 'linear', completed: true }),
    )
    render(<>{node}</>)
    expect(screen.getByText(/authenticated/)).toBeInTheDocument()
  })

  it('renders failed banner when meta.failed is true', () => {
    const node = renderMcpOAuthMessage(
      makeMsg({
        server_name: 'linear',
        failed: true,
        error: 'URL contained credential pattern',
      }),
    )
    render(<>{node}</>)
    expect(
      screen.getByText(/authentication failed: URL contained credential pattern/i),
    ).toBeInTheDocument()
  })

  it('coerces missing server_name to empty string', () => {
    const node = renderMcpOAuthMessage(
      makeMsg({ oauth_url: 'https://mcp.example.com/authorize' }),
    )
    render(<>{node}</>)
    // Falls back to default label.
    expect(screen.getByText(/requires authentication/)).toBeInTheDocument()
  })

  /**
   * A card-owned request is annotated by the backend but still delivered — the
   * Connections card reads its approval URL out of that very message. So the
   * decision to hide it belongs here, and only when the card is reachable.
   * Both directions are pinned: hiding it unconditionally would strip the only
   * authorize prompt on installs where the gallery does not exist.
   */
  describe('card_owned', () => {
    const cardOwned = makeMsg({
      server_name: 'notion',
      oauth_url: 'https://mcp.notion.com/authorize',
      card_owned: true,
    })

    it('renders a card_owned message when the caller has no cards', () => {
      // Default argument — every existing call site behaves exactly as before.
      expect(renderMcpOAuthMessage(cardOwned)).not.toBeNull()
      expect(renderMcpOAuthMessage(cardOwned, false)).not.toBeNull()
    })

    it('drops a card_owned message when the caller renders the cards', () => {
      expect(renderMcpOAuthMessage(cardOwned, true)).toBeNull()
    })

    it('renders an unannotated message even when the caller renders the cards', () => {
      const node = renderMcpOAuthMessage(
        makeMsg({ server_name: 'my-remote', oauth_url: 'https://mine.example.com/authorize' }),
        true,
      )
      expect(node).not.toBeNull()
      render(<>{node}</>)
      expect(screen.getByRole('link', { name: /Authorize my-remote/i })).toBeInTheDocument()
    })

    it('renders a card_owned failure notice regardless of the caller', () => {
      // The backend never annotates a rejected URL, but the render layer must
      // not be the thing standing between the user and a security notice.
      const node = renderMcpOAuthMessage(
        makeMsg({ server_name: 'notion', failed: true, error: 'unsafe URL scheme' }),
        true,
      )
      render(<>{node}</>)
      expect(screen.getByText(/authentication failed: unsafe URL scheme/i)).toBeInTheDocument()
    })
  })
})
