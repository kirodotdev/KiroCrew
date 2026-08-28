import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', () => ({
  api: {
    acpBackends: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpBackendCard, ACP_BACKEND_ANCHOR, ACP_BACKEND_ROUTE } from '../pages/overview/AcpBackendCard'
import type { AcpBackendsPayload } from '../pages/overview/AcpBackendCard'

const acpBackendsMock = api.acpBackends as Mock

const CAPS_ALL_OK = {
  session_sharing: 'supported',
  reasoning_effort: 'supported',
  mcp_tool_search: 'supported',
  agent_profiles: 'supported',
  slash_commands: 'supported',
  turn_usage: 'supported',
  billing: 'supported',
  native_resume: 'supported',
  registry_model_ids: 'supported',
  mid_turn_steer: 'supported',
}

const CAPS_CODEX = {
  ...CAPS_ALL_OK,
  session_sharing: 'unavailable',
  reasoning_effort: 'supported',
  mcp_tool_search: 'unavailable',
  agent_profiles: 'degraded',
  billing: 'unavailable',
}

/** Selectable UNVERIFIED row. Enough fields to render; routing is the point. */
const GOOSE_ROW = {
  id: 'goose',
  label: 'goose',
  experimental: true,
  selectable: true,
  signin_command: 'goose configure',
  install_command: '',
  installed: '',
  dialect: 'spec',
  routing: 'unverified',
  capabilities: CAPS_ALL_OK,
  degraded_count: 0,
}

function payload(over: Partial<AcpBackendsPayload> = {}): AcpBackendsPayload {
  return {
    active: '',
    allow_ungated_tools: false,
    routing_verdict: '',
    backends: [
      {
        id: '',
        label: 'Kiro CLI',
        experimental: false,
        selectable: true,
        signin_command: 'kiro-cli login',
        install_command: '',
        installed: '',
        dialect: 'kiro',
        routing: 'agent_spec',
        capabilities: CAPS_ALL_OK,
        degraded_count: 0,
      },
      {
        id: 'codex',
        label: 'OpenAI Codex',
        experimental: true,
        selectable: true,
        signin_command: 'codex login',
        install_command: 'npm install -g @agentclientprotocol/codex-acp',
        installed: '',
        dialect: 'spec',
        routing: 'external_policy',
        capabilities: CAPS_CODEX,
        degraded_count: 4,
      },
    ],
    ...over,
  }
}

/** The card reads the location hash to honour a deep link, so every render
 *  needs a Router. `route` seeds it. */
function renderCard(
  onSave: (path: string, value: string) => Promise<unknown> = vi.fn().mockResolvedValue(undefined),
  route = '/developer?tab=config',
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <AcpBackendCard onSave={onSave} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AcpBackendCard', () => {
  beforeEach(() => vi.clearAllMocks())

  it('keeps the dedicated tab visibly occupied while adapters load', () => {
    ;acpBackendsMock.mockReturnValue(new Promise(() => {}))
    const { container } = renderCard()
    expect(container.querySelector('[data-slot="skeleton"]')).toBeTruthy()
  })

  it('shows a recoverable error for a non-authorization failure', async () => {
    ;acpBackendsMock
      .mockRejectedValueOnce(new Error('gateway unavailable'))
      .mockResolvedValueOnce(payload())
    renderCard()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load ACP adapters. Try again.',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('radio', { name: 'Kiro CLI' })).toBeTruthy()
    expect(acpBackendsMock).toHaveBeenCalledTimes(2)
  })

  it('renders one row per selectable backend', async () => {
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard()
    expect(await screen.findByRole('radio', { name: 'Kiro CLI' })).toBeTruthy()
    expect(screen.getByRole('radio', { name: 'OpenAI Codex' })).toBeTruthy()
    expect(api.acpBackends).toHaveBeenCalledWith({ probe: true })
  })

  it('does NOT render a row for an unselectable backend', async () => {
    // A control that can never be operated invites the reader to hunt for the
    // setting that would enable it. The footnote is the honest surface.
    ;acpBackendsMock.mockResolvedValue(
      payload({
        backends: [
          ...payload().backends,
          {
            id: 'kas',
            label: 'Kiro Agent Service',
            experimental: true,
            selectable: false,
            signin_command: 'kiro-cli login',
            install_command: '',
            installed: '',
            dialect: 'kiro',
            routing: 'agent_spec',
            capabilities: CAPS_ALL_OK,
            degraded_count: 1,
          },
        ],
      }),
    )
    renderCard()
    await screen.findByRole('radio', { name: 'Kiro CLI' })
    expect(screen.queryByRole('radio', { name: 'Kiro Agent Service' })).toBeNull()
    // …but it IS named, so the operator knows it exists and why it is absent.
    expect(screen.getByText(/Kiro Agent Service/)).toBeTruthy()
  })

  it('says a switch does not change the open chat', async () => {
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard()
    expect(await screen.findByText(/does not change it mid-session/)).toBeTruthy()
  })

  it('repeats the mid-session rule in the confirm dialog', async () => {
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard()
    await userEvent.click(await screen.findByRole('radio', { name: 'OpenAI Codex' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toMatch(/does not switch the harness mid-session/)
  })

  it('discloses that switching clears model selections', async () => {
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard()
    await userEvent.click(await screen.findByRole('radio', { name: 'OpenAI Codex' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toMatch(/Model selections will be reset/)
  })

  it('discloses the model reset when switching back to Kiro', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    ;acpBackendsMock.mockResolvedValue(payload({ active: 'codex' }))
    renderCard(onSave)
    await userEvent.click(await screen.findByRole('radio', { name: 'Kiro CLI' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toMatch(/Model selections will be reset/)
    expect(dialog.textContent).toMatch(/includes open chats/)
    expect(onSave).not.toHaveBeenCalled()
  })

  it('links the withheld-adapter note to its documentation', async () => {
    ;acpBackendsMock.mockResolvedValue(
      payload({ backends: [...payload().backends, { ...GOOSE_ROW, selectable: false }] }),
    )
    renderCard()
    await screen.findByRole('radio', { name: 'Kiro CLI' })
    expect(screen.getByRole('link', { name: /Not enabled in this build/ })).toHaveAttribute(
      'href',
      expect.stringContaining('experimental-acp-adapters.md'),
    )
  })

  it('requires confirmation before switching to an experimental backend', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard(onSave)
    await userEvent.click(await screen.findByRole('radio', { name: 'OpenAI Codex' }))
    // The dialog is up and NOTHING has been persisted yet.
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(onSave).not.toHaveBeenCalled()
  })

  it('persists only after the confirm button is pressed', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard(onSave)
    await userEvent.click(await screen.findByRole('radio', { name: 'OpenAI Codex' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Switch adapter' }))
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('agent.acp_backend', 'codex'))
  })

  it('persists nothing when the dialog is cancelled', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard(onSave)
    await userEvent.click(await screen.findByRole('radio', { name: 'OpenAI Codex' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(onSave).not.toHaveBeenCalled()
  })

  it('confirms before switching back to the default', async () => {
    // Returning to the supported default is operationally safe, but switching
    // still clears harness-owned model selections and is therefore not lossless.
    const onSave = vi.fn().mockResolvedValue(undefined)
    ;acpBackendsMock.mockResolvedValue(payload({ active: 'codex' }))
    renderCard(onSave)
    await userEvent.click(await screen.findByRole('radio', { name: 'Kiro CLI' }))
    expect(onSave).not.toHaveBeenCalled()
    await userEvent.click(await screen.findByRole('button', { name: 'Switch adapter' }))
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('agent.acp_backend', ''))
  })

  it('lists the differing capabilities when expanded', async () => {
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard()
    await userEvent.click(await screen.findByRole('button', { name: /Show what changes/ }))
    expect(screen.getByText('Shared subagent runtime')).toBeTruthy()
    expect(screen.getByText('Works differently')).toBeTruthy()
  })

  it('renders unverified separately from unavailable', async () => {
    const data = payload()
    data.backends[1] = {
      ...data.backends[1],
      capabilities: { ...CAPS_CODEX, mid_turn_steer: 'unverified' },
      degraded_count: 5,
    }
    ;acpBackendsMock.mockResolvedValue(data)
    renderCard()
    await userEvent.click(await screen.findByRole('button', { name: /Show what changes/ }))
    expect(screen.getByText('Not verified')).toBeTruthy()
  })

  it('shows install BEFORE sign-in as ordered prerequisites', async () => {
    // Order is load-bearing: `codex login` needs the adapter's CLI present, so
    // listing sign-in first sends the operator to a command that does not exist
    // yet. The list is <ol> for that reason, not styling.
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard()
    await userEvent.click(await screen.findByRole('radio', { name: 'OpenAI Codex' }))
    const dialog = await screen.findByRole('dialog')

    const list = dialog.querySelector('ol')
    expect(list).toBeTruthy()
    const steps = Array.from(list!.querySelectorAll('li')).map(li => li.textContent)
    expect(steps).toEqual([
      'npm install -g @agentclientprotocol/codex-acp',
      'codex login',
    ])
  })

  it('names the official scoped package, not the bare binary', async () => {
    // The unscoped `codex-acp` is not a real npm package; a global install of
    // the scoped one is what puts the bare binary on PATH for the resolver.
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard()
    await userEvent.click(await screen.findByRole('radio', { name: 'OpenAI Codex' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toContain('@agentclientprotocol/codex-acp')
  })

  it('omits the install step for a backend that ships with the host', async () => {
    // kiro-cli has no package to install, so an empty command must render no
    // step rather than an empty bullet.
    ;acpBackendsMock.mockResolvedValue(
      payload({
        active: 'codex',
        backends: payload().backends.map(b =>
          b.id === '' ? { ...b, experimental: true, degraded_count: 1 } : b,
        ),
      }),
    )
    renderCard()
    await userEvent.click(await screen.findByRole('radio', { name: 'Kiro CLI' }))
    const dialog = await screen.findByRole('dialog')
    const steps = Array.from(dialog.querySelectorAll('ol li')).map(li => li.textContent)
    expect(steps).toEqual(['kiro-cli login'])
  })

  it('marks a backend whose adapter is missing', async () => {
    ;acpBackendsMock.mockResolvedValue(
      payload({
        backends: payload().backends.map(b =>
          b.id === 'codex' ? { ...b, installed: 'missing' } : b,
        ),
      }),
    )
    renderCard()
    await screen.findByRole('radio', { name: 'OpenAI Codex' })
    expect(screen.getByText('Adapter not installed')).toBeTruthy()
  })

  it('does NOT mark a backend when the probe could not tell', async () => {
    // `unknown` means the check failed. Telling an operator who already has the
    // adapter to install it is the false negative that matters here — the remedy
    // it implies is a global npm install.
    ;acpBackendsMock.mockResolvedValue(
      payload({
        backends: payload().backends.map(b =>
          b.id === 'codex' ? { ...b, installed: 'unknown' } : b,
        ),
      }),
    )
    renderCard()
    await screen.findByRole('radio', { name: 'OpenAI Codex' })
    expect(screen.queryByText('Adapter not installed')).toBeNull()
  })

  it('does NOT mark an installed backend', async () => {
    ;acpBackendsMock.mockResolvedValue(
      payload({
        backends: payload().backends.map(b =>
          b.id === 'codex' ? { ...b, installed: 'installed' } : b,
        ),
      }),
    )
    renderCard()
    await screen.findByRole('radio', { name: 'OpenAI Codex' })
    expect(screen.queryByText('Adapter not installed')).toBeNull()
  })

  it('drops the install step for an already-installed adapter', async () => {
    // Telling someone to install what they have is noise that makes the whole
    // dialog easier to skip.
    ;acpBackendsMock.mockResolvedValue(
      payload({
        backends: payload().backends.map(b =>
          b.id === 'codex' ? { ...b, installed: 'installed' } : b,
        ),
      }),
    )
    renderCard()
    await userEvent.click(await screen.findByRole('radio', { name: 'OpenAI Codex' }))
    const dialog = await screen.findByRole('dialog')
    const steps = Array.from(dialog.querySelectorAll('ol li')).map(li => li.textContent)
    expect(steps).toEqual(['codex login'])
  })

  it('surfaces an indeterminate verdict so an UNVERIFIED backend is visibly going to refuse', async () => {
    ;acpBackendsMock.mockResolvedValue(
      payload({
        active: 'goose',
        allow_ungated_tools: false,
        routing_verdict: 'indeterminate',
        routing_reason: 'Kiro Crew has not established how this adapter routes tool calls',
        backends: [...payload().backends, GOOSE_ROW],
      }),
    )
    renderCard()
    await screen.findByRole('radio', { name: 'goose' })
    const status = screen.getByRole('status')
    expect(status.textContent).toContain(
      'Tool-call routing is not verified — new sessions will refuse tool calls',
    )
    expect(status.textContent).toContain(
      'Kiro Crew has not established how this adapter routes tool calls',
    )
  })

  it('shows the ungated-tools opt-out warning when allow_ungated_tools is on', async () => {
    ;acpBackendsMock.mockResolvedValue(
      payload({
        active: 'goose',
        allow_ungated_tools: true,
        routing_verdict: 'indeterminate',
        routing_reason: 'Kiro Crew has not established how this adapter routes tool calls',
        backends: [...payload().backends, GOOSE_ROW],
      }),
    )
    renderCard()
    await screen.findByRole('radio', { name: 'goose' })
    expect(
      screen.getByText(
        /Ungated tools are allowed\. Denied-command rules, sensitive-path blocking, and the governance ceiling are not consulted/,
      ),
    ).toBeTruthy()
  })

  it('pins the routed verdict so a verified backend is visibly gated', async () => {
    ;acpBackendsMock.mockResolvedValue(
      payload({
        active: '',
        allow_ungated_tools: false,
        routing_verdict: 'routed',
        routing_reason: 'adapter tools are forwarded to session/request_permission',
      }),
    )
    renderCard()
    await screen.findByRole('radio', { name: 'Kiro CLI' })
    expect(screen.getByRole('status').textContent).toContain(
      "Tool calls reach Kiro Crew's security gate",
    )
  })

  it('renders nothing at all when the owner-only endpoint returns 403', async () => {
    // Owner-only: a non-owner gets 403 and the control is not theirs to operate.
    ;acpBackendsMock.mockRejectedValue({ status: 403 })
    const { container } = renderCard()
    await waitFor(() => expect(container.textContent).toBe(''))
  })

  it('names the radio by the backend, not by its whole description', async () => {
    // A screen reader must announce "OpenAI Codex", not the capability count
    // glued onto it — and the description must still be reachable, never
    // aria-hidden, or the experimental warning is invisible to exactly the
    // users who cannot see the badge.
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard()
    const radio = await screen.findByRole('radio', { name: 'OpenAI Codex' })
    expect(radio.getAttribute('aria-describedby')).toBe('acp-backend-desc-codex')
    const description = document.getElementById('acp-backend-desc-codex')
    expect(description?.getAttribute('aria-hidden')).toBeNull()
    expect(description?.textContent).toMatch(/4 of 10/)
  })
})

/**
 * Deep link from the top-bar harness readout. The scroll lives in the card
 * rather than in `useSettingHighlight` because the backend rows arrive only
 * after `GET /api/acp-backends` answers: a highlight resolved on the first
 * effect would strip the param too early. These pin the ordering that makes the
 * link work — the anchor exists during loading, and the scroll waits for data.
 */
describe('AcpBackendCard — deep link', () => {
  // Restored rather than left installed: jsdom ships no `scrollIntoView`, so a
  // stub left on the prototype would silently satisfy any later test that
  // should have failed for calling it.
  const originalScrollIntoView = Element.prototype.scrollIntoView
  let scrollIntoView: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.clearAllMocks()
    scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView
  })
  afterEach(() => {
    Element.prototype.scrollIntoView = originalScrollIntoView
  })

  it('carries the anchor the harness readout links to', async () => {
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard()
    await screen.findByRole('radio', { name: 'Kiro CLI' })
    expect(document.getElementById(ACP_BACKEND_ANCHOR)).toBeTruthy()
    // The route the header links to must name that same anchor, or the two
    // halves drift silently and the link lands on the tab and stops there.
    expect(ACP_BACKEND_ROUTE).toContain(`#${ACP_BACKEND_ANCHOR}`)
  })

  it('scrolls itself into view once the payload lands', async () => {
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard(vi.fn().mockResolvedValue(undefined), `/developer?tab=config#${ACP_BACKEND_ANCHOR}`)
    // The card exists as a loading skeleton, but the assertion that the scroll
    // is not attempted early is the whole point of gating on `data`.
    expect(scrollIntoView).not.toHaveBeenCalled()
    await screen.findByRole('radio', { name: 'Kiro CLI' })
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled())
  })

  it('leaves the reader where they are when the tab is opened directly', async () => {
    // Developer > Config is reached on its own far more often than through the
    // deep link; scrolling past the rows above it there would lose their place.
    ;acpBackendsMock.mockResolvedValue(payload())
    renderCard()
    await screen.findByRole('radio', { name: 'Kiro CLI' })
    expect(scrollIntoView).not.toHaveBeenCalled()
  })
})
