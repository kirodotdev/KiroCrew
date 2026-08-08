// Enabling the shared MCP gateway must NOT navigate away.
//
// It used to `navigate('/developer?tab=system')` on success, which was wrong
// twice over: enabling the pool is the first half of the job (the user then
// picks which servers to pool, on this very page), and the destination carried
// no `plane`, so it landed on the Sessions table rather than the metrics card
// the redirect was written for. That table polls a whole-machine process scan,
// so the redirect delivered users straight onto the surface that froze.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { SharedMcpGatewayToggle } from '../pages/settings/SharedMcpGatewayToggle'
import { api } from '../api/client'

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{`${loc.pathname}${loc.search}`}</div>
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={['/developer?tab=mcp-pool']}>
      <QueryClientProvider client={qc}>
        <SharedMcpGatewayToggle />
        <LocationProbe />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('SharedMcpGatewayToggle', () => {
  beforeEach(() => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue({
      enabled: false, running: false, ping_ok: false, supported: true,
    } as never)
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('stays on the current page after a successful enable', async () => {
    const enable = vi.spyOn(api, 'mcpGatewayEnable').mockResolvedValue({
      ok: true, enabled: true, running: true, ping_ok: true,
    } as never)

    mount()

    // The toggle renders immediately but is `disabled` until the status query
    // resolves, and its onClick short-circuits while disabled — so clicking on
    // first paint is a silent no-op. `toBeDisabled()` cannot detect this: the
    // switch is a <div role="switch">, and jest-dom only considers real form
    // elements disable-able, so it passes vacuously. Wait on `aria-disabled`,
    // which is the attribute Toggle actually sets.
    const toggle = await waitFor(() => {
      const el = screen.getByRole('switch')
      expect(el.getAttribute('aria-disabled')).toBeNull()
      return el
    })
    fireEvent.click(toggle)

    // Confirm dialog → Continue
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByText('Continue'))

    await waitFor(() => expect(enable).toHaveBeenCalledWith(true))
    // The verified-state modal is the feedback, and the route is untouched.
    await waitFor(() => expect(screen.getByText(/is active/i)).toBeTruthy())
    expect(screen.getByTestId('loc').textContent).toBe('/developer?tab=mcp-pool')
  })
})
