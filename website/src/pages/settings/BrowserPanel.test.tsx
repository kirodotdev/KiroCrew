import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* ── api client mock ───────────────────────────────────────────────────────
 * The panel reads and writes only through these three methods, so mocking them
 * keeps every case network-free. Each save resolves with the server payload the
 * panel re-renders from. */
vi.mock('../../api/client', () => ({
  api: {
    getBrowserConfig: vi.fn(),
    saveBrowserConfig: vi.fn(),
    restartSessions: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { BrowserPanel } from './BrowserPanel'

type Cfg = {
  enabled: boolean
  engine: string
  engines: string[]
  extension_mode: boolean
  token: boolean
  installed: boolean
}

function cfg(overrides: Partial<Cfg> = {}): Cfg {
  return {
    enabled: false,
    engine: 'chromium',
    engines: ['chromium', 'firefox', 'webkit'],
    extension_mode: false,
    token: false,
    installed: false,
    ...overrides,
  }
}

async function renderPanel(data: Cfg = cfg(), saveResult: unknown = { ok: true, enabled: true, engine: 'chromium' }) {
  ;(api.getBrowserConfig as ReturnType<typeof vi.fn>).mockResolvedValue(data)
  ;(api.saveBrowserConfig as ReturnType<typeof vi.fn>).mockResolvedValue(saveResult)
  ;(api.restartSessions as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })
  const utils = renderWithProviders(<BrowserPanel />)
  await screen.findByText('Browser Mode')
  return utils
}

describe('BrowserPanel', () => {
  beforeEach(() => vi.clearAllMocks())

  it('enabling Browser Mode persists enabled=true (the durable capability)', async () => {
    await renderPanel()
    fireEvent.click(await screen.findByRole('switch', { name: /enable browser mode/i }))
    await waitFor(() =>
      expect(api.saveBrowserConfig).toHaveBeenCalledWith(
        expect.objectContaining({ enabled: true, engine: 'chromium' }),
      ),
    )
  })

  it('offers the engine picker only once enabled and in headless mode', async () => {
    // Disabled: no engine picker.
    await renderPanel(cfg({ enabled: false }))
    expect(screen.queryByRole('button', { name: 'Firefox' })).toBeNull()
  })

  it('turning attach on persists extension_mode immediately (token optional)', async () => {
    // The token only skips the approval prompt; flipping attach on must save
    // right away so it does not revert on reload.
    await renderPanel(cfg({ enabled: true, extension_mode: false }))
    fireEvent.click(await screen.findByRole('switch', { name: /attach to my running browser/i }))
    await waitFor(() =>
      expect(api.saveBrowserConfig).toHaveBeenCalledWith(
        expect.objectContaining({ extension_mode: true, token: '' }),
      ),
    )
  })

  it('selecting an engine saves that engine', async () => {
    await renderPanel(cfg({ enabled: true, extension_mode: false }))
    fireEvent.click(await screen.findByRole('button', { name: 'Firefox' }))
    await waitFor(() =>
      expect(api.saveBrowserConfig).toHaveBeenCalledWith(
        expect.objectContaining({ engine: 'firefox' }),
      ),
    )
  })

  it('surfaces a failed install inline rather than a success tick', async () => {
    await renderPanel(
      cfg({ enabled: false }),
      { ok: true, enabled: true, engine: 'chromium', install: { ok: false, step: 'node', detail: 'Node.js is required', engine: 'chromium' } },
    )
    fireEvent.click(await screen.findByRole('switch', { name: /enable browser mode/i }))
    expect(await screen.findByText(/node\.js is required/i)).toBeInTheDocument()
    expect(screen.queryByText(/saved and applied/i)).toBeNull()
  })

  it('connect-your-browser links the Playwright Extension for Chromium browsers only', async () => {
    await renderPanel(cfg({ enabled: true, extension_mode: true, token: true }))
    // The single verified Chrome Web Store link, covering the Chromium family.
    const link = await screen.findByText(/Playwright Extension \(Chrome, Edge, Brave, Arc, Opera\)/i)
    expect(link).toBeInTheDocument()
    expect(link.closest('a')?.getAttribute('href')).toContain('chromewebstore.google.com')
    // The token is presented as optional (it skips per-connection approval).
    expect(screen.getByText(/Connection Token \(optional\)/i)).toBeInTheDocument()
    // No attach option for Firefox or Safari (Playwright ships no extension).
    expect(screen.queryByText(/attach.*firefox/i)).toBeNull()
    expect(screen.queryByText(/attach.*safari/i)).toBeNull()
  })
})
