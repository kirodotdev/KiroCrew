import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../api/client'
import { openInternalLink } from '../utils/internalNav'
import { useInternalNavHandler } from './useInternalNavHandler'

const DASHBOARD_URL = 'https://crew.example.test/some/path'
const TUNNEL_URL = 'https://abc.tunnel.example.test'

function Probe({ enabled, open }: { enabled: boolean; open: (path: string) => void }) {
  useInternalNavHandler(enabled, open)
  return null
}

function mount(enabled: boolean, open: (path: string) => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><Probe enabled={enabled} open={open} /></QueryClientProvider>)
}

afterEach(() => { vi.restoreAllMocks() })

describe('useInternalNavHandler', () => {
  it('trusts the exact origins of dashboard.url and the tunnel, besides the page own', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ dashboard: { url: DASHBOARD_URL } })
    vi.spyOn(api, 'tunnelStatus').mockResolvedValue({ state: 'connected', url: TUNNEL_URL, error: '', uptime: 1, reconnect_attempt: 0 })
    const open = vi.fn()
    const { unmount } = mount(true, open)
    expect(openInternalLink('/schedule')).toBe(true)
    await waitFor(() => expect(openInternalLink('https://crew.example.test/schedule')).toBe(true))
    await waitFor(() => expect(openInternalLink(`${TUNNEL_URL}/sessions`)).toBe(true))
    expect(openInternalLink('https://other.example.test/schedule')).toBe(false)
    expect(open.mock.calls).toEqual([['/schedule'], ['/schedule'], ['/sessions']])
    unmount()
    // Unmounting uninstalls it, so a link falls back to the anchor's new tab.
    expect(openInternalLink('/schedule')).toBe(false)
  })

  it('installs nothing, and asks for nothing, in a window it is disabled for', () => {
    const config = vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({})
    const tunnel = vi.spyOn(api, 'tunnelStatus').mockResolvedValue({ state: 'disabled', url: '', error: '', uptime: 0, reconnect_attempt: 0 })
    mount(false, vi.fn())
    expect(openInternalLink('/schedule')).toBe(false)
    expect(config).not.toHaveBeenCalled()
    expect(tunnel).not.toHaveBeenCalled()
  })

  it('keeps the page own origin when the config read fails', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockRejectedValue(new Error('denied'))
    vi.spyOn(api, 'tunnelStatus').mockRejectedValue(new Error('down'))
    const open = vi.fn()
    const { unmount } = mount(true, open)
    expect(openInternalLink('/schedule')).toBe(true)
    expect(openInternalLink('https://crew.example.test/schedule')).toBe(false)
    unmount()
  })
})
