/**
 * useGlobalHotkey — gating and pass-through of the desktop summon hotkey.
 *
 * The hook reports the main process's summon-hotkey state to the shortcuts UI:
 * only inside the Electron shell and only when the preload bridge exists. On a
 * desktop build it returns the info even when the hotkey is UNBOUND
 * (accelerator ""), so the UI can show "Off" with how-to-enable guidance rather
 * than hide it; it returns null only when there is no desktop shell to
 * configure (a plain browser, or a build without the bridge).
 */
import { describe, expect, it, vi, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

// The shell flag is read at module load in lib/electron.ts; mock it so the
// same test file can exercise both the browser and desktop branches.
const electronEnv = vi.hoisted(() => ({ isElectron: true }))
vi.mock('../lib/electron', () => electronEnv)

import { useGlobalHotkey } from './useGlobalHotkey'

type BridgeWindow = Window & { electronAPI?: { getGlobalHotkey?: () => Promise<unknown> } }

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

afterEach(() => {
  delete (window as BridgeWindow).electronAPI
  electronEnv.isElectron = true
})

describe('useGlobalHotkey', () => {
  it('returns the bound accelerator reported by the bridge', async () => {
    ;(window as BridgeWindow).electronAPI = {
      getGlobalHotkey: async () => ({ accelerator: 'Alt+Shift+K', default: 'Alt+Shift+K' }),
    }
    const { result } = renderHook(() => useGlobalHotkey(), { wrapper })
    await waitFor(() => expect(result.current).not.toBeNull())
    expect(result.current).toEqual({ accelerator: 'Alt+Shift+K', default: 'Alt+Shift+K' })
  })

  it('keeps the info object when the hotkey is unbound on a desktop build', async () => {
    // The opt-out default on a fresh install: accelerator "" but still a desktop
    // build, so the UI must be able to render the "Off" row rather than hide it.
    ;(window as BridgeWindow).electronAPI = {
      getGlobalHotkey: async () => ({ accelerator: '', default: 'Alt+Shift+K' }),
    }
    const { result } = renderHook(() => useGlobalHotkey(), { wrapper })
    await waitFor(() => expect(result.current).not.toBeNull())
    expect(result.current).toEqual({ accelerator: '', default: 'Alt+Shift+K' })
  })

  it('returns null in a plain browser without querying anything', () => {
    electronEnv.isElectron = false
    const spy = vi.fn(async () => ({ accelerator: 'Alt+Shift+K', default: 'Alt+Shift+K' }))
    ;(window as BridgeWindow).electronAPI = { getGlobalHotkey: spy }
    const { result } = renderHook(() => useGlobalHotkey(), { wrapper })
    expect(result.current).toBeNull()
    expect(spy).not.toHaveBeenCalled()
  })

  it('returns null in a desktop build whose preload lacks the bridge method', () => {
    ;(window as BridgeWindow).electronAPI = {}
    const { result } = renderHook(() => useGlobalHotkey(), { wrapper })
    expect(result.current).toBeNull()
  })
})
