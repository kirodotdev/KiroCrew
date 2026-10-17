/**
 * The page's panel-tab openers (pages/chat/page/panelTabs.ts), driven directly:
 * what the run-in-terminal listener reads at event time, and the bubble
 * placeholder's "bring the app tab back".
 */
import { describe, it, expect, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('../api/client', () => ({ api: { kirocrewConfig: vi.fn() } }))

import { api } from '../api/client'
import { mcpAppKey, openActivityPanel } from '../store/chatSlice'
import { useAppPanelReveal, useRunInTerminalConfig } from './chat/page/panelTabs'
import type { ChatSlot } from '../types'

function withQueryClient() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

const slot = (key: string, project?: string) => ({ key, project }) as unknown as ChatSlot

describe('useRunInTerminalConfig', () => {
  it('mirrors the active slot project and the terminal config onto refs read at event time', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ dashboard: { terminal: { reuse_current: true } } } as never)
    const { result, rerender } = renderHook(
      ({ activeSlot }: { activeSlot: string | null }) => useRunInTerminalConfig({ slots: [slot('a', '/repo/a'), slot('b')], activeSlot }),
      { initialProps: { activeSlot: 'a' as string | null }, wrapper: withQueryClient() },
    )
    // Before the read settles the handler must take the copy-never-run path.
    expect(result.current.terminalCfgUnsettledRef.current).toBe(true)
    expect(result.current.currentProjectRef.current).toBe('/repo/a')
    await waitFor(() => expect(result.current.terminalCfgUnsettledRef.current).toBe(false))
    expect(result.current.terminalReuseRef.current).toBe(true)
    expect(result.current.terminalCfgReadFailedRef.current).toBe(false)
    rerender({ activeSlot: 'b' })
    expect(result.current.currentProjectRef.current).toBeUndefined()
  })

  it('reports a failed config read as failed, never as reuse off', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new Error('down'))
    const { result } = renderHook(() => useRunInTerminalConfig({ slots: [], activeSlot: null }), { wrapper: withQueryClient() })
    await waitFor(() => expect(result.current.terminalCfgReadFailedRef.current).toBe(true))
    expect(result.current.terminalReuseRef.current).toBe(false)
    expect(result.current.terminalCfgUnsettledRef.current).toBe(false)
  })
})

describe('useAppPanelReveal', () => {
  function setup(activeSlot: string | null) {
    const dispatch = vi.fn()
    const close = vi.fn()
    const openApp = vi.fn()
    const payload = { title: 'Board' }
    const boundStore = {
      getState: () => ({ chat: { mcpApps: { [mcpAppKey('slot-a', 'call-1')]: payload } } }),
    }
    const { result } = renderHook(() => useAppPanelReveal({
      dispatch: dispatch as never,
      activeSlot,
      boundStore: boundStore as never,
      search: { close } as never,
      tabsCtlRef: { current: { openApp } } as never,
    }))
    return { result, dispatch, close, openApp }
  }

  it('closes the find pane, opens the activity panel, and re-opens the app tab for the active slot', () => {
    const { result, dispatch, close, openApp } = setup('slot-a')
    result.current.revealAppInPanel('call-1')
    expect(close).toHaveBeenCalledTimes(1)
    expect(dispatch).toHaveBeenCalledWith(openActivityPanel())
    expect(openApp).toHaveBeenCalledTimes(1)
    const [id, title, slotKey] = openApp.mock.calls[0]
    expect(id).toBe('call-1')
    expect(typeof title).toBe('string')
    expect(slotKey).toBe('slot-a')
  })

  it('still opens the tab, without a payload read, when no slot is active', () => {
    const { result, openApp } = setup(null)
    result.current.revealAppInPanel('call-1')
    expect(openApp).toHaveBeenCalledWith('call-1', expect.any(String), null)
  })
})
