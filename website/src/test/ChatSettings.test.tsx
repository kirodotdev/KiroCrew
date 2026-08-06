import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ChatSettings, { loadChatConfig, type ChatConfig } from '../pages/chat/ChatSettings'

const DEFAULTS: ChatConfig = { historyExpanded: true, showTimestamps: true, sendOnEnter: 'enter', collapseAllSteps: true }

function renderSettings(props: { config: ChatConfig; onChange: ReturnType<typeof vi.fn> }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><ChatSettings {...props} /></QueryClientProvider>)
}

describe('loadChatConfig', () => {
  beforeEach(() => { localStorage.removeItem('mc-chat-config') })

  it('defaults sendOnEnter to true', () => {
    expect(loadChatConfig().sendOnEnter).toBe('enter')
  })

  it('respects stored sendOnEnter=false', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ sendOnEnter: false }))
    expect(loadChatConfig().sendOnEnter).toBe('ctrl-enter')
  })

  it('defaults confirmCloseSession to false', () => {
    expect(loadChatConfig().confirmCloseSession).toBe(false)
  })

  it('respects stored confirmCloseSession=true', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: true }))
    expect(loadChatConfig().confirmCloseSession).toBe(true)
  })

  it('shows turn stats by default', () => {
    expect(loadChatConfig().showTurnStats).toBe(true)
  })

  it('respects stored showTurnStats=false', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ showTurnStats: false }))
    expect(loadChatConfig().showTurnStats).toBe(false)
  })

  it('repairs a non-boolean showTurnStats value to the enabled default', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ showTurnStats: 'no' }))
    expect(loadChatConfig().showTurnStats).toBe(true)
  })

  it('pins the latest prompt by default', () => {
    expect(loadChatConfig().pinLastPrompt).toBe(true)
  })

  it('adopts the new default for a stored config that predates the setting', () => {
    // Configs written before the sticky-banner setting existed carry no
    // pinLastPrompt key, so the DEFAULTS spread is what upgrades them.
    localStorage.setItem('mc-chat-config', JSON.stringify({ showTimestamps: false }))
    const cfg = loadChatConfig()
    expect(cfg.pinLastPrompt).toBe(true)
    expect(cfg.showTimestamps).toBe(false)
  })

  it('respects stored pinLastPrompt=false', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: false }))
    expect(loadChatConfig().pinLastPrompt).toBe(false)
  })

  it('repairs a non-boolean pinLastPrompt value to the enabled default', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: 'yes' }))
    expect(loadChatConfig().pinLastPrompt).toBe(true)
  })
})

describe('ChatSettings – session restore UI', () => {
  let onChange: ReturnType<typeof vi.fn>

  beforeEach(() => {
    localStorage.removeItem('mc-chat-config')
    onChange = vi.fn()
    // Mock fetch to return default dashboard config
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, opts) => {
      if (url === '/api/dashboard/config') {
        if (opts?.method === 'PUT') {
          return new Response(JSON.stringify({ ok: true }), { status: 200 })
        }
        return new Response(JSON.stringify({ restore_sessions: false, restore_window_minutes: 30 }), { status: 200 })
      }
      return new Response('{}', { status: 200 })
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders restore toggle after opening popover', async () => {
    renderSettings({ config: DEFAULTS, onChange })
    const btn = screen.getByRole('button', { name: 'Chat settings' })
    fireEvent.click(btn)
    await waitFor(() => {
      expect(screen.getByText('Restore sessions on restart')).toBeInTheDocument()
    })
  })

  it('does not show restore window selector when toggle is off', async () => {
    renderSettings({ config: DEFAULTS, onChange })
    fireEvent.click(screen.getByRole('button', { name: 'Chat settings' }))
    await waitFor(() => {
      expect(screen.getByText('Restore sessions on restart')).toBeInTheDocument()
    })
    expect(screen.queryByText('Restore window')).not.toBeInTheDocument()
  })

  it('shows restore window selector after enabling restore toggle', async () => {
    // Return restore_sessions: true from the API
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, opts) => {
      if (url === '/api/dashboard/config') {
        if (opts?.method === 'PUT') {
          return new Response(JSON.stringify({ ok: true }), { status: 200 })
        }
        return new Response(JSON.stringify({ restore_sessions: true, restore_window_minutes: 30 }), { status: 200 })
      }
      return new Response('{}', { status: 200 })
    })

    renderSettings({ config: DEFAULTS, onChange })
    fireEvent.click(screen.getByRole('button', { name: 'Chat settings' }))
    await waitFor(() => {
      expect(screen.getByText('Restore window')).toBeInTheDocument()
    })
  })

  it('fetches dashboard config on mount', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderSettings({ config: DEFAULTS, onChange })
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith('/api/dashboard/config')
    })
  })

  it('displays startup section header', async () => {
    renderSettings({ config: DEFAULTS, onChange })
    fireEvent.click(screen.getByRole('button', { name: 'Chat settings' }))
    await waitFor(() => {
      expect(screen.getByText('Startup')).toBeInTheDocument()
    })
  })

  it('renders Quick Send toggle from dashboard config', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, opts) => {
      if (url === '/api/dashboard/config') {
        if (opts?.method === 'PUT') return new Response(JSON.stringify({ ok: true }), { status: 200 })
        return new Response(JSON.stringify({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more', quick_send: false }), { status: 200 })
      }
      return new Response('{}', { status: 200 })
    })
    renderSettings({ config: DEFAULTS, onChange: vi.fn() })
    fireEvent.click(screen.getByRole('button', { name: 'Chat settings' }))
    await waitFor(() => {
      expect(screen.getByText('Quick Send')).toBeInTheDocument()
    })
  })

  it('persists disabling elapsed time and credits', async () => {
    const config = { ...DEFAULTS, showTurnStats: true }
    renderSettings({ config, onChange })
    fireEvent.click(screen.getByRole('button', { name: 'Chat settings' }))
    const toggle = await screen.findByRole('switch', { name: 'Show elapsed time and credits' })
    expect(toggle).toHaveAttribute('aria-checked', 'true')

    fireEvent.click(toggle)

    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ showTurnStats: false }))
    expect(JSON.parse(localStorage.getItem('mc-chat-config') || '{}').showTurnStats).toBe(false)
  })

  it('shows Quick Send hint when enabled', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, opts) => {
      if (url === '/api/dashboard/config') {
        if (opts?.method === 'PUT') return new Response(JSON.stringify({ ok: true }), { status: 200 })
        return new Response(JSON.stringify({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more', quick_send: true }), { status: 200 })
      }
      return new Response('{}', { status: 200 })
    })
    renderSettings({ config: DEFAULTS, onChange: vi.fn() })
    fireEvent.click(screen.getByRole('button', { name: 'Chat settings' }))
    await waitFor(() => {
      expect(screen.getByText(/Click a suggested reply to send it instantly/)).toBeInTheDocument()
    })
  })
})
