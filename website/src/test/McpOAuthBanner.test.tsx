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

  // A newer authorize request kills the older flow's loopback listener, so the
  // older banner's link would walk the user through a full provider login and
  // dead-end on `http://127.0.0.1:<dead-port>/?code=…` — a page that looks like
  // success and consumes nothing (issue #7580).
  describe('superseded state', () => {
    it('renders no authorize link', () => {
      render(
        <McpOAuthBanner
          serverName="miro"
          oauthUrl="https://mcp.miro.com/authorize"
          completed={false}
          superseded={true}
        />,
      )
      expect(screen.queryByRole('link')).not.toBeInTheDocument()
    })

    it('tells the user the sign-in is dead and to use the newest button', () => {
      render(
        <McpOAuthBanner serverName="miro" oauthUrl="" completed={false} superseded={true} />,
      )
      expect(screen.getByText(/no longer active/i)).toBeInTheDocument()
      expect(screen.getByText(/latest Authorize button/i)).toBeInTheDocument()
    })

    it('keeps naming the server so the user knows which sign-in died', () => {
      render(
        <McpOAuthBanner serverName="miro" oauthUrl="" completed={false} superseded={true} />,
      )
      expect(screen.getByText('miro')).toBeInTheDocument()
    })

    it('yields to failed and completed, which are the authoritative outcomes', () => {
      const { unmount } = render(
        <McpOAuthBanner
          serverName="miro"
          oauthUrl=""
          completed={false}
          failed={true}
          superseded={true}
          error="dns"
        />,
      )
      expect(screen.getByText(/authentication failed: dns/i)).toBeInTheDocument()
      unmount()
      render(
        <McpOAuthBanner
          serverName="miro"
          oauthUrl=""
          completed={true}
          superseded={true}
        />,
      )
      expect(screen.getByText(/authenticated/)).toBeInTheDocument()
    })
  })
  describe('expired state', () => {
    it('renders no authorize link', () => {
      render(
        <McpOAuthBanner
          serverName="miro"
          oauthUrl="https://mcp.miro.com/authorize"
          completed={false}
          expired={true}
        />,
      )
      expect(screen.queryByRole('link')).not.toBeInTheDocument()
    })

    it('does not claim a newer request replaced it, because none did', () => {
      // The whole reason this is a separate state from `superseded`: after a
      // restart or a reset there is no newer request and no "latest Authorize
      // button" to point at, so that copy would be a lie.
      render(<McpOAuthBanner serverName="miro" oauthUrl="" completed={false} expired={true} />)
      expect(screen.getByText(/no longer active/i)).toBeInTheDocument()
      expect(screen.queryByText(/newer request/i)).not.toBeInTheDocument()
      expect(screen.queryByText(/latest Authorize button/i)).not.toBeInTheDocument()
    })

    it('points at a recovery the user can actually reach', () => {
      render(<McpOAuthBanner serverName="miro" oauthUrl="" completed={false} expired={true} />)
      expect(screen.getByText(/Send a message/i)).toBeInTheDocument()
    })

    it('keeps naming the server so the user knows which sign-in died', () => {
      render(<McpOAuthBanner serverName="miro" oauthUrl="" completed={false} expired={true} />)
      expect(screen.getByText('miro')).toBeInTheDocument()
    })

    it('yields to failed and completed, which are the authoritative outcomes', () => {
      const { unmount } = render(
        <McpOAuthBanner
          serverName="miro"
          oauthUrl=""
          completed={false}
          failed={true}
          expired={true}
          error="dns"
        />,
      )
      expect(screen.getByText(/authentication failed: dns/i)).toBeInTheDocument()
      unmount()
      render(<McpOAuthBanner serverName="miro" oauthUrl="" completed={true} expired={true} />)
      expect(screen.getByText(/authenticated/i)).toBeInTheDocument()
    })

    it('yields to superseded, which names a live button to use instead', () => {
      render(
        <McpOAuthBanner
          serverName="miro"
          oauthUrl=""
          completed={false}
          superseded={true}
          expired={true}
        />,
      )
      expect(screen.getByText(/latest Authorize button/i)).toBeInTheDocument()
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

  it('renders a superseded banner even though it carries no oauth_url', () => {
    // The backend POPS oauth_url when it retires a banner, so `superseded` is
    // the only thing left to render on. Falling through to null here would make
    // the dead flow vanish silently instead of telling the user what to do
    // (issue #7580).
    const node = renderMcpOAuthMessage(makeMsg({ server_name: 'miro', superseded: true }))
    expect(node).not.toBeNull()
    render(<>{node}</>)
    expect(screen.getByText(/no longer active/i)).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('renders an expired banner even though it carries no oauth_url', () => {
    // Same trap as `superseded`, and the reason the flag has to be named in the
    // presence guard: the backend withdraws the url, so `expired` is the only
    // signal left. Falling through to null would make the dead sign-in vanish,
    // which reads as success -- the exact confusion #7654 is about.
    const node = renderMcpOAuthMessage(makeMsg({ server_name: 'miro', expired: true }))
    expect(node).not.toBeNull()
    render(<>{node}</>)
    expect(screen.getByText(/no longer active/i)).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('renders expired even when the stored row still carries a url', () => {
    // A tab holding a pre-retirement row merges the patch over it, so both keys
    // can be present at once. The flag must win, or the dead link comes back.
    const node = renderMcpOAuthMessage(
      makeMsg({ server_name: 'miro', oauth_url: 'https://mcp.miro.com/a', expired: true }),
    )
    render(<>{node}</>)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByText(/no longer active/i)).toBeInTheDocument()
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
