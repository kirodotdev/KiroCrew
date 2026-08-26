import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api, ApiError } from '../api/client'
import McpOAuthBanner from '../pages/chat/McpOAuthBanner'

// The affordance is the ONLY reason the banner touches the API, so a tiny mock
// exposing just the relay call is enough — everything else on the banner renders
// without it. ApiError must ride along because the component's catch narrows on
// `instanceof ApiError` to read the backend's stable error code.
vi.mock('../api/client', () => {
  class ApiError extends Error {
    readonly status: number
    readonly body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  }
  return {
    ApiError,
    api: {
      mcpOAuthRelay: vi.fn(),
    },
  }
})

const mcpOAuthRelay = vi.mocked(api.mcpOAuthRelay)

/**
 * These pin issue #4491's frontend half: the paste-back relay is discoverable
 * from the chat banner (where the remote-gateway failure presents), not only on
 * the Connections page, and it works for any server the banner names — including
 * a user-added / self-hosted one that the Connections registry has never heard
 * of. The affordance sits behind a one-line disclosure so it never competes with
 * the Authorize action on the happy path.
 */
describe('McpOAuthBanner remote-gateway relay affordance', () => {
  beforeEach(() => {
    mcpOAuthRelay.mockReset()
  })

  const renderNeedsAuth = (serverName: string) =>
    render(
      <McpOAuthBanner
        serverName={serverName}
        oauthUrl="https://mcp.example.com/authorize"
        completed={false}
      />,
    )

  /** The paste-back input hides behind the disclosure until opened. */
  const openDisclosure = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(
      screen.getByRole('button', { name: /connection error after authorizing/i }),
    )
  }

  it('collapses the paste-back path behind a one-line disclosure', async () => {
    const user = userEvent.setup()
    renderNeedsAuth('linear')

    // Closed by default: the hint and input do not compete with Authorize.
    expect(screen.queryByText(/localhost callback will fail/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()

    await openDisclosure(user)
    expect(screen.getByText(/localhost callback will fail/i)).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /complete connection/i }),
    ).toBeInTheDocument()
  })

  it('is a real expander: button stays mounted, aria-expanded toggles, focus moves to the input', async () => {
    const user = userEvent.setup()
    renderNeedsAuth('linear')

    const disclosure = screen.getByRole('button', { name: /connection error after authorizing/i })
    expect(disclosure).toHaveAttribute('aria-expanded', 'false')

    await user.click(disclosure)
    // The button did NOT unmount when the panel opened (focus is not dropped
    // to <body>), it announces the open state, and focus lands on the input.
    expect(disclosure).toHaveAttribute('aria-expanded', 'true')
    await waitFor(() => expect(screen.getByRole('textbox')).toHaveFocus())

    // And it collapses again.
    await user.click(disclosure)
    expect(disclosure).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('rejects a malformed paste locally with the shared validator, without calling the API', async () => {
    const user = userEvent.setup()
    renderNeedsAuth('linear')
    await openDisclosure(user)

    // A non-loopback host fails the same pre-check the Connections card
    // runs — specific message, no round-trip. (localhost itself is VALID:
    // the backend accepts it, so the client must too.)
    await user.type(screen.getByRole('textbox'), 'http://10.0.0.5:53017/?code=x')
    await user.click(screen.getByRole('button', { name: /complete connection/i }))

    expect(mcpOAuthRelay).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })

  it('relays a pasted return address for a user-added / self-hosted server', async () => {
    mcpOAuthRelay.mockResolvedValueOnce({ ok: true })
    const user = userEvent.setup()
    // A slug the Connections registry does not ship — the #4008 population.
    renderNeedsAuth('my-self-hosted-mcp')
    await openDisclosure(user)

    const returnAddress = 'http://127.0.0.1:53017/?code=abc123&state=xyz'
    await user.type(screen.getByRole('textbox'), returnAddress)
    await user.click(screen.getByRole('button', { name: /complete connection/i }))

    expect(mcpOAuthRelay).toHaveBeenCalledWith('my-self-hosted-mcp', returnAddress)
    // On success it shows the neutral delivered state — NOT "authenticated":
    // only the server's meta.completed flips the banner to authenticated.
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(/code delivered/i),
    )
    expect(screen.queryByText(/^authenticated/i)).not.toBeInTheDocument()
  })

  it('keeps the Complete-connection button disabled until an address is entered', async () => {
    const user = userEvent.setup()
    renderNeedsAuth('linear')
    await openDisclosure(user)
    expect(
      screen.getByRole('button', { name: /complete connection/i }),
    ).toBeDisabled()
  })

  it('shows an error and does not collapse when the relay fails', async () => {
    mcpOAuthRelay.mockRejectedValueOnce(new Error('boom'))
    const user = userEvent.setup()
    renderNeedsAuth('linear')
    await openDisclosure(user)

    await user.type(screen.getByRole('textbox'), 'http://127.0.0.1:53017/?code=x')
    await user.click(screen.getByRole('button', { name: /complete connection/i }))

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(/could not complete/i),
    )
    // Still on the input state so the user can retry, not the delivered one.
    expect(
      screen.getByRole('button', { name: /complete connection/i }),
    ).toBeInTheDocument()
  })

  it('points at Authorize when the approval was superseded (409)', async () => {
    mcpOAuthRelay.mockRejectedValueOnce(
      new ApiError(
        409,
        'approval superseded',
        '{"error":"a newer authorization attempt superseded this approval","code":"approval_superseded"}',
      ),
    )
    const user = userEvent.setup()
    renderNeedsAuth('linear')
    await openDisclosure(user)

    await user.type(screen.getByRole('textbox'), 'http://127.0.0.1:53017/?code=stale')
    await user.click(screen.getByRole('button', { name: /complete connection/i }))

    // The coded 409 gets its own message: retrying the same URL can never
    // succeed, so the copy directs the user back to Authorize (cause-neutral —
    // a spent code and a superseded approval render the same way).
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(/no longer active/i),
    )
  })
})
