// MCP Management: the pre-resolve control must report what the pass produced.
//
// Contract under test:
// - a pass that resolved something says how many servers now skip resolution
// - a pass that resolved nothing says so rather than claiming a win
// - "no targets routed yet" is a distinct answer, not an empty success
// - a pass where every install failed reports the failures, never "nothing needed"
// - a mixed pass reports both halves
// - a 409 reads as an in-flight pass, not as a failure
// - a failed pass surfaces an error and does not claim anything was updated
// - the button disables itself while a pass is in flight, so a second press
//   cannot start the duplicate the backend would refuse with a 409 anyway
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api, ApiError } from '../api/client'
import { McpManagement } from '../pages/settings/McpManagement'

function mount() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpManagement />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const status = () => ({
  enabled: false,
  stub: [] as string[],
  stub_count: 0,
  running: false,
  ping_ok: false,
  supported: true,
})

function stubPage() {
  vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
  vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [] } as never)
}

const pressUpdate = async () => {
  const button = await screen.findByRole('button', { name: /update now/i })
  button.click()
  return button
}

beforeEach(() => {
  vi.restoreAllMocks()
})
afterEach(cleanup)

describe('McpManagement pre-resolve', () => {
  it('reports how many servers now launch without resolving', async () => {
    stubPage()
    vi.spyOn(api, 'mcpResolveRefresh').mockResolvedValue({
      ok: true,
      resolved: { 'foo@latest': 'ready', 'bar@1.0.0': 'ready' },
      ready: ['bar@1.0.0', 'foo@latest'],
    } as never)

    mount()
    await pressUpdate()

    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toMatch(/2/)
    })
  })

  it('says nothing needed pre-resolving instead of claiming a win', async () => {
    stubPage()
    vi.spyOn(api, 'mcpResolveRefresh').mockResolvedValue({
      ok: true,
      resolved: {},
      ready: [],
    } as never)

    mount()
    await pressUpdate()

    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toMatch(/nothing needed/i)
    })
  })

  it('states "no targets routed" as information, not as an alarm', async () => {
    // ok:false with this reason means no broker start has computed a target set.
    // That is the operator having nothing stubbed, which this page states as
    // plain text elsewhere -- a red alert would overstate a benign fact.
    stubPage()
    vi.spyOn(api, 'mcpResolveRefresh').mockResolvedValue({
      ok: false,
      reason: 'no_targets',
      resolved: {},
    } as never)

    mount()
    await pressUpdate()

    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toMatch(/no servers are routed/i)
    })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('reports failures instead of a false all-clear when every install failed', async () => {
    // ready === 0 has two causes and they must not read the same. This is the
    // one where nothing worked: a registry outage or a rejected token. Telling
    // the person who just pressed the button "nothing needed pre-resolving"
    // would say launches now skip the network when not one of them does.
    stubPage()
    vi.spyOn(api, 'mcpResolveRefresh').mockResolvedValue({
      ok: true,
      resolved: { 'foo@latest': 'error', 'bar@1.0.0': 'error' },
      ready: [],
    } as never)

    mount()
    await pressUpdate()

    await waitFor(() => {
      const text = screen.getByRole('status').textContent ?? ''
      expect(text).toMatch(/2/)
      expect(text).not.toMatch(/nothing needed/i)
    })
  })

  it('reports both halves of a mixed pass', async () => {
    stubPage()
    vi.spyOn(api, 'mcpResolveRefresh').mockResolvedValue({
      ok: true,
      resolved: { 'foo@latest': 'ready', 'bar@1.0.0': 'error', 'baz@2.0.0': 'error' },
      ready: ['foo@latest'],
    } as never)

    mount()
    await pressUpdate()

    await waitFor(() => {
      const text = screen.getByRole('status').textContent ?? ''
      // One ready, two failed -- a notice naming only the win would still be a
      // partial all-clear.
      expect(text).toMatch(/1/)
      expect(text).toMatch(/2/)
    })
  })

  it('treats a 409 as an in-flight pass, not a failure', async () => {
    // The endpoint answers 409 + resolve_in_progress deliberately, as
    // information rather than something to retry. A second dashboard tab must
    // not be told the pass failed while it is running fine.
    stubPage()
    const conflict = new ApiError(
      409,
      'A pre-resolve pass is already running.',
      JSON.stringify({ error: 'A pre-resolve pass is already running.', code: 'resolve_in_progress' }),
    )
    vi.spyOn(api, 'mcpResolveRefresh').mockRejectedValue(conflict)

    mount()
    await pressUpdate()

    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toMatch(/already running/i)
    })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('surfaces a failed pass without claiming an update', async () => {
    stubPage()
    vi.spyOn(api, 'mcpResolveRefresh').mockRejectedValue(new Error('boom'))

    mount()
    await pressUpdate()

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy()
      expect(screen.queryByRole('status')).toBeNull()
    })
  })

  it('clears the previous result so a new pass cannot contradict it', async () => {
    // Without the clear, a success notice from the last press sits beside the
    // new error and the page states two incompatible outcomes at once.
    stubPage()
    const refresh = vi.spyOn(api, 'mcpResolveRefresh')
    refresh.mockResolvedValueOnce({
      ok: true,
      resolved: { 'foo@latest': 'ready' },
      ready: ['foo@latest'],
    } as never)

    mount()
    const button = await pressUpdate()
    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toMatch(/1/)
    })

    refresh.mockRejectedValueOnce(new Error('boom'))
    button.click()

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy()
      expect(screen.queryByRole('status')).toBeNull()
    })
  })

  it('disables the button while a pass is in flight', async () => {
    stubPage()
    let release: (v: unknown) => void = () => {}
    vi.spyOn(api, 'mcpResolveRefresh').mockReturnValue(
      new Promise(resolve => {
        release = resolve
      }) as never,
    )

    mount()
    const button = await pressUpdate()

    await waitFor(() => {
      expect((button as HTMLButtonElement).disabled).toBe(true)
    })
    release({ ok: true, resolved: {}, ready: [] })
    await waitFor(() => {
      expect((button as HTMLButtonElement).disabled).toBe(false)
    })
  })
})
