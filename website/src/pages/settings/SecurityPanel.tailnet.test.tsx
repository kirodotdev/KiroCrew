/**
 * Tailnet origin section (Settings → Security).
 *
 * The property under test is that the card renders off the SERVER-OWNED `state`
 * field and nothing else. `state` is derived by the backend with a fixed
 * precedence (`pinned` > `off` > `unresolved` > `active`) precisely so the two
 * layers cannot disagree, so the fixtures below pin each state's rendering
 * INDEPENDENTLY of the other fields — including the case the precedence exists
 * for: `enabled: true` plus a governance pin must render as off-and-locked, not
 * as an active origin.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import type { TailnetStatusData } from '../../api/client'

vi.mock('../../api/client', () => ({
  api: {
    // The panel's rail reads these on mount regardless of the selected section.
    deniedCommands: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    tailnetStatus: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'

const TITLE = "Trust this machine's tailnet name"
const HOST = 'desk.tail1a2b3c.ts.net'
const ORIGIN = `https://${HOST}`
/** 2026-08-07T08:00:00Z, in the epoch SECONDS the contract specifies. */
const RESOLVED_AT = 1786176000

function status(overrides: Partial<TailnetStatusData> = {}): TailnetStatusData {
  return {
    enabled: true,
    governance_pinned: false,
    host: HOST,
    origin: ORIGIN,
    resolved_at: RESOLVED_AT,
    state: 'active',
    ...overrides,
  }
}

/** The badge/summary label each server-owned `state` renders as. */
const STATE_LABEL: Record<TailnetStatusData['state'], string> = {
  active: 'Active',
  unresolved: 'Not resolved',
  off: 'Off',
  pinned: 'Disabled by policy',
}

/** Render the panel on the tailnet section with the status query pre-resolved.
 *
 *  Waits on the STATE LABEL, not on the card title: the title is present while
 *  the query is still in flight (and in the failed-read branch), so waiting on
 *  it hands back a card that has not received `state` yet and every assertion
 *  below races the query. */
async function renderTailnet(data: TailnetStatusData) {
  ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(data)
  const utils = renderWithProviders(<SecurityPanel />, { route: '/?section=tailnet' })
  await screen.findAllByText(STATE_LABEL[data.state])
  return utils
}

describe('SecurityPanel — tailnet origin', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue({
      builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false,
    })
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue({ controls: [], counts: {} })
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue({
      version: null, has_policy: false, profile: null, unavailable: false, scopes: [],
    })
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.patchConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })
  })

  it('active: green state badge, the MagicDNS origin, and the three status chips', async () => {
    await renderTailnet(status())

    expect(screen.getAllByText('Active').length).toBeGreaterThan(0)
    // The origin string itself, not just the host — it is what the allow-list
    // actually contains and what the Copy button hands over.
    expect(screen.getByText(ORIGIN)).toBeInTheDocument()
    expect(screen.getByText('Dashboard origin')).toBeInTheDocument()

    // Three chips, each a fact the endpoint reported.
    expect(screen.getByText('Allow-list')).toBeInTheDocument()
    expect(screen.getByText('Name added')).toBeInTheDocument()
    expect(screen.getByText('Resolved')).toBeInTheDocument()
    expect(screen.getByText('Session pin')).toBeInTheDocument()
    expect(screen.getByText('Not bound')).toBeInTheDocument()

    const sw = screen.getByRole('switch', { name: TITLE })
    expect(sw).toHaveAttribute('aria-checked', 'true')
    expect(sw).not.toHaveAttribute('aria-disabled')
  })

  it('active: states the session-pin limitation plainly and links issue #1762', async () => {
    await renderTailnet(status())

    // Not softened: the pin CANNOT bind, and the reason (every request arrives
    // from loopback) is named rather than implied.
    expect(screen.getByText(/cannot bind behind tailscale serve/i)).toBeInTheDocument()
    expect(screen.getByText(/127\.0\.0\.1/)).toBeInTheDocument()

    const link = screen.getByRole('link', { name: /Tracking issue #1762/ })
    expect(link).toHaveAttribute('href', expect.stringContaining('/issues/1762'))
  })

  it('active: Copy writes the origin to the clipboard and Open links to it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    await renderTailnet(status())

    fireEvent.click(screen.getByRole('button', { name: 'Copy the tailnet origin' }))
    expect(writeText).toHaveBeenCalledWith(ORIGIN)
    // Feedback is part of the contract: a copy with no acknowledgement reads as
    // a dead button.
    expect(await screen.findByText('Copied')).toBeInTheDocument()

    const open = screen.getByRole('link', { name: 'Open' })
    expect(open).toHaveAttribute('href', ORIGIN)
    expect(open).toHaveAttribute('rel', expect.stringContaining('noopener'))
  })

  it('active: a REJECTED clipboard write does not claim "Copied"', async () => {
    // A false acknowledgement is worse than none: the user pastes stale content
    // believing the origin is on the clipboard.
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    await renderTailnet(status())

    fireEvent.click(screen.getByRole('button', { name: 'Copy the tailnet origin' }))
    await waitFor(() => expect(writeText).toHaveBeenCalled())
    expect(screen.queryByText('Copied')).not.toBeInTheDocument()
    expect(screen.getByText('Copy')).toBeInTheDocument()
  })

  it('unresolved: amber badge saying nothing was added, and no origin row', async () => {
    await renderTailnet(status({ host: '', origin: '', resolved_at: 0, state: 'unresolved' }))

    expect(screen.getAllByText('Not resolved').length).toBeGreaterThan(0)
    expect(screen.getByText('Nothing added')).toBeInTheDocument()
    expect(screen.getByText('Never')).toBeInTheDocument()
    // The whole point of the state: on, but the Origin check still refuses.
    expect(screen.getByText(/still refused by the origin check/i)).toBeInTheDocument()
    expect(screen.queryByText('Dashboard origin')).not.toBeInTheDocument()

    // No daemon claim of any kind — the endpoint ships no daemon-state field, so
    // the UI must not invent one.
    expect(screen.queryByText(/tailscaled/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/daemon/i)).not.toBeInTheDocument()

    // Still on, and still the user's to turn off.
    const sw = screen.getByRole('switch', { name: TITLE })
    expect(sw).toHaveAttribute('aria-checked', 'true')
    expect(sw).not.toHaveAttribute('aria-disabled')
  })

  it('off: a plain enabled toggle, no chips, no caveat', async () => {
    await renderTailnet(status({ enabled: false, host: '', origin: '', resolved_at: 0, state: 'off' }))

    expect(screen.getAllByText('Off').length).toBeGreaterThan(0)
    expect(screen.queryByText('Allow-list')).not.toBeInTheDocument()
    expect(screen.queryByText('Session pin')).not.toBeInTheDocument()
    expect(screen.queryByText(/cannot bind behind/i)).not.toBeInTheDocument()

    const sw = screen.getByRole('switch', { name: TITLE })
    expect(sw).toHaveAttribute('aria-checked', 'false')
    expect(sw).not.toHaveAttribute('aria-disabled')
  })

  it('pinned: the control is DISABLED and carries the administrator note', async () => {
    // The precedence case: config says enabled, policy says no. `state` is the
    // single owner of that resolution, so the card must read off-and-locked even
    // though `enabled` is true and a host was resolved.
    await renderTailnet(status({ governance_pinned: true, state: 'pinned' }))

    const sw = screen.getByRole('switch', { name: TITLE })
    expect(sw).toHaveAttribute('aria-disabled', 'true')
    expect(sw).toHaveAttribute('aria-checked', 'false')
    // Not tabbable either — a switch reachable by keyboard but inert is worse
    // than one that is plainly out of reach.
    expect(sw).toHaveAttribute('tabindex', '-1')

    // The SHARED admin-pin sentence, so the product says one thing about pins.
    expect(
      screen.getByText("Your administrator's security policy turns this off. It can't be changed here."),
    ).toBeInTheDocument()
    expect(screen.getAllByText('Disabled by policy').length).toBeGreaterThan(0)

    fireEvent.click(sw)
    expect(api.patchConfig).not.toHaveBeenCalled()
  })

  it('writes the config path as a literal boolean when toggled', async () => {
    await renderTailnet(status({ enabled: false, host: '', origin: '', resolved_at: 0, state: 'off' }))
    const sw = screen.getByRole('switch', { name: TITLE })
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))

    fireEvent.click(sw)
    await waitFor(() =>
      expect(api.patchConfig).toHaveBeenCalledWith('dashboard.tailscale.enabled', true),
    )
    const [, written] = (api.patchConfig as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(typeof written).toBe('boolean')
  })

  it('a failed read is not reported as off: no switch, and it says so', async () => {
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=tailnet' })

    expect(await screen.findByText(/Could not read the tailnet setting/i)).toBeInTheDocument()
    // Rendering a switch here would assert a state we could not read; `role=switch`
    // has no "unknown" for aria-checked, so the control is withheld entirely.
    expect(screen.queryByRole('switch', { name: TITLE })).not.toBeInTheDocument()
    cleanup()
  })

  it('the rail summary reports the state, and nothing when it could not be read', async () => {
    await renderTailnet(status())
    // The rail label is the section heading; the summary underneath is the state.
    expect(screen.getAllByText('Tailnet origin').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Active').length).toBeGreaterThan(0)
    cleanup()

    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=rules' })
    await screen.findAllByText('Tailnet origin')
    // No reassuring summary on an unread state.
    expect(screen.queryByText('Off')).not.toBeInTheDocument()
    expect(screen.queryByText('Active')).not.toBeInTheDocument()
  })
})
