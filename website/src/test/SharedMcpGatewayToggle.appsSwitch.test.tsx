import React from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { mcpAppsSwitchState, SharedMcpGatewayToggle } from '../pages/settings/SharedMcpGatewayToggle'
import { api } from '../api/client'

function state(partial: Partial<Parameters<typeof mcpAppsSwitchState>[0]> = {}) {
  return mcpAppsSwitchState({
    gatewayEnabled: true,
    appsEnabled: true,
    loading: false,
    busy: false,
    ...partial,
  })
}

describe('mcpAppsSwitchState', () => {
  it('is togglable when the broker is on', () => {
    expect(state()).toEqual({ checked: true, disabled: false, needsGateway: false })
  })

  it('stays settable while the broker is OFF so the opt-out can be pre-recorded', () => {
    // The load-bearing case. `apps_enabled` defaults on, so gating this behind a
    // running broker would force a cautious user to enable the broker first —
    // exposing themselves to server-authored UI — and then race to switch it off.
    // The endpoint writes config only and needs no broker.
    expect(state({ gatewayEnabled: false }).disabled).toBe(false)
  })

  it('flags needsGateway only when the broker is off', () => {
    expect(state({ gatewayEnabled: false }).needsGateway).toBe(true)
    expect(state({ gatewayEnabled: true }).needsGateway).toBe(false)
  })

  it('reports the stored state regardless of the broker', () => {
    expect(state({ gatewayEnabled: false, appsEnabled: true }).checked).toBe(true)
    expect(state({ gatewayEnabled: false, appsEnabled: false }).checked).toBe(false)
  })

  it('disables only while loading or applying', () => {
    expect(state({ loading: true }).disabled).toBe(true)
    expect(state({ busy: true }).disabled).toBe(true)
  })

  it('exposes no per-state description', () => {
    // The row describes what the switch CONTROLS, not what is happening, so a
    // state-dependent description would be the thing that misreports the trust
    // fact this control exists to answer. Re-adding one should fail here.
    expect(Object.keys(state()).sort()).toEqual(['checked', 'disabled', 'needsGateway'])
  })
})

// The gateway FOLLOWS the MCP Apps switch, in one direction and one case only:
//
//   apps ON  + gateway off -> gateway follows on
//   apps ON  + gateway on  -> nothing to follow
//   apps OFF + gateway on  -> gateway STAYS ON, never follows off
//   apps OFF + gateway off -> nothing happens
//
// Following off would tear down pooling for consumers that have nothing to do
// with MCP Apps, which is the separation this pair of controls exists for.
describe('gateway following the MCP Apps switch', () => {
  // Stateful double: the real endpoint's write is visible to the next status
  // refetch. A fixed-value mock reverts every write on refetch, so a test
  // asserting post-write UI would be asserting a fiction.
  function mount(gatewayEnabled: boolean, appsEnabled: boolean, supported = true, poolable = 1) {
    const state = {
      enabled: gatewayEnabled, apps_enabled: appsEnabled,
      running: gatewayEnabled, ping_ok: gatewayEnabled, supported,
    }
    vi.spyOn(api, 'mcpGatewayStatus').mockImplementation(async () => ({ ...state }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockImplementation(async () => ({
      servers: Array.from({ length: poolable }, (_, i) => ({ name: `srv-${i}`, poolable: true })),
    }) as never)
    vi.spyOn(api, 'mcpGatewayAppsEnable').mockImplementation(async (next: boolean) => {
      state.apps_enabled = next
      return { ok: true, enabled: next } as never
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter initialEntries={['/developer?tab=mcp-pool']}>
        <QueryClientProvider client={qc}><SharedMcpGatewayToggle /></QueryClientProvider>
      </MemoryRouter>,
    )
    return state
  }

  async function clickApps() {
    const el = await waitFor(() => {
      const s = screen.getByRole('switch', { name: 'MCP Apps' })
      expect(s.getAttribute('aria-disabled')).toBeNull()
      return s
    })
    fireEvent.click(el)
    return el
  }

  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('follows the gateway ON when both were off', async () => {
    const appsEnable = vi.spyOn(api, 'mcpGatewayAppsEnable')
      .mockResolvedValue({ ok: true, enabled: true } as never)
    const gatewayEnable = vi.spyOn(api, 'mcpGatewayEnable')
      .mockResolvedValue({ ok: true, enabled: true, running: true, ping_ok: true } as never)

    mount(false, false)
    await clickApps()

    // The preference is written first, then the gateway follows — no confirm
    // step in between. Asking would be the wrong shape here: MCP Apps is inert
    // without the broker, so the follow is what the user just asked for.
    await waitFor(() => expect(appsEnable).toHaveBeenCalledWith(true))
    await waitFor(() => expect(gatewayEnable).toHaveBeenCalledWith(true))

    // No dialog at any point: the follow must read as the upper switch moving,
    // so routing it through the apply modal (which covers this card) would hide
    // the very control it changes.
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('does not touch the gateway when it is already on', async () => {
    const appsEnable = vi.spyOn(api, 'mcpGatewayAppsEnable')
      .mockResolvedValue({ ok: true, enabled: true } as never)
    const gatewayEnable = vi.spyOn(api, 'mcpGatewayEnable')

    mount(true, false)
    await clickApps()

    await waitFor(() => expect(appsEnable).toHaveBeenCalledWith(true))
    expect(gatewayEnable).not.toHaveBeenCalled()
  })

  it('never follows the gateway OFF', async () => {
    // The load-bearing asymmetry: opting out of server-authored UI must not
    // disable pooling for everything else.
    const appsEnable = vi.spyOn(api, 'mcpGatewayAppsEnable')
      .mockResolvedValue({ ok: true, enabled: false } as never)
    const gatewayEnable = vi.spyOn(api, 'mcpGatewayEnable')

    mount(true, true)
    await clickApps()

    await waitFor(() => expect(appsEnable).toHaveBeenCalledWith(false))
    expect(gatewayEnable).not.toHaveBeenCalled()
  })

  it('does nothing to the gateway when switching Apps off with it already off', async () => {
    const appsEnable = vi.spyOn(api, 'mcpGatewayAppsEnable')
      .mockResolvedValue({ ok: true, enabled: false } as never)
    const gatewayEnable = vi.spyOn(api, 'mcpGatewayEnable')

    mount(false, true)
    await clickApps()

    await waitFor(() => expect(appsEnable).toHaveBeenCalledWith(false))
    expect(gatewayEnable).not.toHaveBeenCalled()

    // UX Review Watch #2: `mcp_apps_applies_to_new` describes connections
    // recycling, which is false while the broker is down — it must not stack a
    // second "Saved…" line against the state line.
    expect(screen.queryByText(/Applies to new MCP connections/i)).toBeNull()
    // Switch is now OFF with the broker down, so the line discloses what turning
    // it back on would do — not that something is already saved and waiting.
    expect(screen.getByText(/also starts the shared MCP gateway/i)).toBeTruthy()
  })

  it('discloses the session restart BEFORE the click, not after', async () => {
    // The follow is not gated by a confirm, so this line is the only
    // informed-consent surface for an action that restarts active sessions.
    mount(false, false)
    await waitFor(() => expect(
      screen.getByText(/also starts the shared MCP gateway, which restarts active sessions/i),
    ).toBeTruthy())
  })

  it('refetches status and points at the gateway row when the follow fails', async () => {
    // A failed enable still leaves config at enabled=true with the broker
    // unreachable. Skipping the refetch left the gateway row showing a stale OFF
    // beside an error saying "turn it on" — two instructions that disagree.
    const state = mount(false, false)
    const gatewayEnable = vi.spyOn(api, 'mcpGatewayEnable')
      .mockImplementation(async () => {
        state.enabled = true
        state.running = true
        state.ping_ok = false
        return { ok: true, enabled: true, running: true, ping_ok: false } as never
      })

    await clickApps()

    await waitFor(() => expect(gatewayEnable).toHaveBeenCalledWith(true))
    await waitFor(() => expect(screen.getByText(/did not come up/i)).toBeTruthy())
    // Recovery wording must match the gateway row's own, not contradict it.
    expect(screen.getByText(/toggle the shared MCP gateway off and on above/i)).toBeTruthy()
    // The consequence of refetching: the gateway row stops claiming it is off and
    // renders its OWN authoritative recovery text. Asserting this rather than a
    // status-call count, which `runApps` bumps on its own and so proves nothing.
    await waitFor(() => expect(screen.getByText(/broker not reachable/i)).toBeTruthy())
  })

  it('does not promise or attempt a gateway start on an unsupported platform', async () => {
    // Windows: the shared gateway needs Unix-domain sockets, so `supported` is
    // false and its toggle is rendered disabled. Promising a start there is a lie,
    // and the failure copy would point at a control the user cannot operate.
    mount(false, false, /* supported */ false)
    const gatewayEnable = vi.spyOn(api, 'mcpGatewayEnable')
    const appsEnable = vi.spyOn(api, 'mcpGatewayAppsEnable')

    await waitFor(() => expect(screen.getAllByText(/Not available on Windows/i)).toHaveLength(1))
    expect(screen.queryByText(/also starts the shared MCP gateway/i)).toBeNull()

    await clickApps()
    await waitFor(() => expect(appsEnable).toHaveBeenCalledWith(true))
    expect(gatewayEnable).not.toHaveBeenCalled()
  })

  it('says nothing about the gateway in the DEFAULT Windows state', async () => {
    // `apps_enabled` defaults ON, so apps-on + broker-down is the state every
    // Windows user lands in on first visit. "Takes effect once the shared MCP
    // gateway above is on" directly beneath a permanently disabled "Not available
    // on Windows" row is a contradiction the user can never resolve.
    mount(false, /* appsEnabled */ true, /* supported */ false)

    await waitFor(() => expect(screen.getAllByText(/Not available on Windows/i)).toHaveLength(1))
    expect(screen.queryByText(/takes effect once the shared MCP gateway above is on/i)).toBeNull()
    expect(screen.queryByText(/also starts the shared MCP gateway/i)).toBeNull()
  })

  it('survives a server payload with no servers array', async () => {
    // Regression: `data?.servers.filter(...)` guarded `data` but not the property
    // below it, so any response without a `servers` array threw and took the whole
    // MCP Pool page down. Caught by the en-XA render gate, which reported the page
    // as "threw while rendering" rather than as a translation finding.
    //
    // The throw lands on the render AFTER the query resolves, so asserting on first
    // paint passes even when broken. An error boundary is what makes this
    // deterministic: it records the crash whenever it happens.
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue({
      enabled: true, apps_enabled: true, running: true, ping_ok: true, supported: true,
    } as never)
    const servers = vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({} as never)

    const crashes: string[] = []
    class Boundary extends React.Component<{ children: React.ReactNode }> {
      componentDidCatch(e: Error) { crashes.push(e.message) }
      render() { return this.props.children }
    }
    // React logs the caught error; keep the test output readable.
    const consoleErr = vi.spyOn(console, 'error').mockImplementation(() => {})

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter initialEntries={['/developer?tab=mcp-pool']}>
        <QueryClientProvider client={qc}>
          <Boundary><SharedMcpGatewayToggle /></Boundary>
        </QueryClientProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(servers).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByRole('switch', { name: 'MCP Apps' })).toBeTruthy())
    expect(crashes).toEqual([])
    // Claims nothing about an allowlist it could not read.
    expect(screen.queryByText(/No MCP servers are poolable yet/i)).toBeNull()
    consoleErr.mockRestore()
  })

  it('acknowledges the save on an unsupported platform instead of going silent', async () => {
    // Regression from my own Windows fix: `supported` suppressed the state line and
    // `needsGateway` suppressed the closure line, so every toggle on Windows
    // rendered NOTHING — the same "turned it on and nothing happened" silence this
    // feature exists to remove.
    mount(false, false, /* supported */ false)

    await clickApps()
    await waitFor(() => expect(screen.getByText(/not available on this platform/i)).toBeTruthy())
  })

  it('ties the restart warning into the switch accessible description', async () => {
    // The consequence is a sibling element, so without aria-describedby an AT user
    // flips the switch and their sessions restart with no pre-click disclosure.
    mount(false, false)

    const id = await waitFor(() => {
      const sw = screen.getByRole('switch', { name: 'MCP Apps' })
      const v = sw.getAttribute('aria-describedby')
      expect(v).toBeTruthy()
      return v as string
    })
    const described = document.getElementById(id)
    expect(described?.textContent).toMatch(/also starts the shared MCP gateway/i)
  })

  it('offers an inline retry when the follow fails', async () => {
    // Recovering via the gateway toggle costs confirm+apply modals twice (off, then
    // on) — six clicks to retry a one-click follow.
    const state = mount(false, false)
    let calls = 0
    const gatewayEnable = vi.spyOn(api, 'mcpGatewayEnable').mockImplementation(async () => {
      calls += 1
      if (calls === 1) return { ok: true, enabled: true, running: true, ping_ok: false } as never
      state.enabled = true
      state.running = true
      state.ping_ok = true
      return { ok: true, enabled: true, running: true, ping_ok: true } as never
    })

    await clickApps()
    await waitFor(() => expect(screen.getByText(/did not come up/i)).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: /Retry starting the gateway/i }))
    await waitFor(() => expect(gatewayEnable).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByText(/did not come up/i)).toBeNull())
  })

  it('does not claim a restart on the toggle AFTER a successful follow', async () => {
    // `followStarted` decides which closure line renders. Left set, the NEXT
    // toggle — including the one turning MCP Apps OFF — claimed "gateway started,
    // sessions restarted" when nothing started and Apps had just gone off.
    const state = mount(false, false)
    vi.spyOn(api, 'mcpGatewayEnable').mockImplementation(async () => {
      state.enabled = true
      state.running = true
      state.ping_ok = true
      return { ok: true, enabled: true, running: true, ping_ok: true } as never
    })

    await clickApps()
    await waitFor(() => expect(screen.getByText(/Active sessions were restarted/i)).toBeTruthy())

    // Second toggle: MCP Apps back OFF. The gateway stays on (the asymmetry), so
    // nothing restarts and nothing started.
    //
    // Asserted as a POSITIVE end state — the generic closure line, which renders
    // only once the write has settled AND `followStarted` is false. A bare
    // `queryByText(...).toBeNull()` inside waitFor passes on the transient window
    // where `appsApplied` is momentarily false at the top of runApps, so it
    // succeeded even with the reset removed.
    await clickApps()
    await waitFor(() => expect(screen.getByText(/Applies to new MCP connections/i)).toBeTruthy())
    expect(screen.queryByText(/Active sessions were restarted/i)).toBeNull()
  })

  it('acknowledges the restart even when nothing can render yet', async () => {
    // The pre-click warning promised a session restart; a successful follow owes an
    // acknowledgement even with an empty allowlist, alongside the pointer at the
    // missing step. Suppressing it left the promised restart unconfirmed.
    const state = mount(false, false, /* supported */ true, /* poolable */ 0)
    vi.spyOn(api, 'mcpGatewayEnable').mockImplementation(async () => {
      state.enabled = true
      state.running = true
      state.ping_ok = true
      return { ok: true, enabled: true, running: true, ping_ok: true } as never
    })

    await clickApps()

    await waitFor(() => expect(screen.getByText(/Active sessions were restarted/i)).toBeTruthy())
    expect(screen.getByText(/No MCP servers are poolable yet/i)).toBeTruthy()
  })

  it('points at the empty poolable allowlist instead of claiming success', async () => {
    // The second prerequisite. `poolable_servers` defaults to an EMPTY list, and a
    // server only gets a stub — the render path — when it is poolable. So the
    // out-of-the-box state is: MCP Apps on, gateway on, nothing can render. A
    // "Saved." closure there repeats the exact "turned it on and nothing happened"
    // failure this PR exists to remove, one prerequisite down.
    mount(/* gateway */ true, /* apps */ true, /* supported */ true, /* poolable */ 0)

    await waitFor(() => expect(screen.getByText(/No MCP servers are poolable yet/i)).toBeTruthy())
    expect(screen.queryByText(/pick it up when they recycle/i)).toBeNull()
  })

  it('closes the loop truthfully after a successful follow', async () => {
    // The generic "ones already running pick it up when they recycle" is false
    // here: the follow just restarted them, so they picked it up now.
    const state = mount(false, false)
    vi.spyOn(api, 'mcpGatewayEnable').mockImplementation(async () => {
      state.enabled = true
      state.running = true
      state.ping_ok = true
      return { ok: true, enabled: true, running: true, ping_ok: true } as never
    })

    await clickApps()

    await waitFor(() => expect(screen.getByText(/Active sessions were restarted with MCP Apps on/i)).toBeTruthy())
    expect(screen.queryByText(/pick it up when they recycle/i)).toBeNull()
  })

  it('retires the follow error once the gateway comes up by other means', async () => {
    // Recovering via the gateway's own toggle otherwise leaves a red "did not come
    // up" line directly under a gateway row reporting Active. Driven through a REAL
    // invalidation (the gateway toggle's own apply), which never touches appsError —
    // so a pass here is the effect working, not `runApps` resetting the line.
    const state = mount(false, false)
    let calls = 0
    vi.spyOn(api, 'mcpGatewayEnable').mockImplementation(async () => {
      calls += 1
      if (calls === 1) {
        // The follow: broker never answers.
        return { ok: true, enabled: true, running: true, ping_ok: false } as never
      }
      state.enabled = true
      state.running = true
      state.ping_ok = true
      return { ok: true, enabled: true, running: true, ping_ok: true } as never
    })

    await clickApps()
    await waitFor(() => expect(screen.getByText(/did not come up/i)).toBeTruthy())

    // User brings the broker up with the gateway's own control.
    const gw = screen.getByRole('switch', { name: 'Shared MCP gateway' })
    fireEvent.click(gw)
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByText('Continue'))

    await waitFor(() => expect(screen.queryByText(/did not come up/i)).toBeNull())
  })
})
