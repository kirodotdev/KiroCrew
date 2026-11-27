/**
 * One shortcut recorder at a time. Search Everywhere, the panel toggles and the
 * summon hotkey each capture keydown on the window, so two left recording would
 * both take the next chord: a press meant for the summon hotkey would also
 * overwrite a panel toggle. Starting one recorder stops the others.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/electron')>()),
  isElectron: true,
}))

import { useGlobalHotkey, useGlobalHotkeyRecorder } from './useGlobalHotkey'
import { usePanelToggleShortcuts } from './usePanelToggleShortcuts'
import { useQuickSearchShortcut } from './useQuickSearchShortcut'

type BridgeWindow = Window & {
  electronAPI?: { getGlobalHotkey?: () => Promise<unknown>; setGlobalHotkey?: (accelerator: string) => Promise<unknown> }
}

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

/** The three editors of Settings → Shortcuts, mounted together as on the panel. */
function mountEditors(set: (accelerator: string) => Promise<unknown>) {
  ;(window as BridgeWindow).electronAPI = {
    getGlobalHotkey: async () => ({ accelerator: '', requested: '', default: 'Alt+Shift+K' }),
    setGlobalHotkey: set,
  }
  return renderHook(
    () => ({ info: useGlobalHotkey(), summon: useGlobalHotkeyRecorder(), panels: usePanelToggleShortcuts(), search: useQuickSearchShortcut() }),
    { wrapper },
  )
}

beforeEach(() => localStorage.clear())
afterEach(() => {
  delete (window as BridgeWindow).electronAPI
})

describe('shortcut recorders are exclusive', () => {
  it('starting the summon recorder stops a panel recorder, so the chord lands on the summon hotkey only', async () => {
    const set = vi.fn(async (accelerator: string) => ({ accelerator, requested: accelerator, default: 'Alt+Shift+K' }))
    const { result } = mountEditors(set)
    await waitFor(() => expect(result.current.info).not.toBeNull())
    const leftSidebarBefore = result.current.panels.bindings['left-sidebar']

    act(() => result.current.panels.startRecording('left-sidebar'))
    expect(result.current.panels.recordingId).toBe('left-sidebar')
    act(() => result.current.summon.startRecording())
    expect(result.current.panels.recordingId).toBeNull()
    expect(result.current.summon.recording).toBe(true)

    fireEvent.keyDown(window, { key: 'J', code: 'KeyJ', altKey: true, shiftKey: true })
    expect(set).toHaveBeenCalledExactlyOnceWith('Alt+Shift+J')
    expect(result.current.panels.bindings['left-sidebar']).toEqual(leftSidebarBefore)
  })

  it('starting a panel recorder stops the summon recorder, so the chord is not grabbed system-wide', async () => {
    const set = vi.fn(async (accelerator: string) => ({ accelerator, requested: accelerator, default: 'Alt+Shift+K' }))
    const { result } = mountEditors(set)
    await waitFor(() => expect(result.current.info).not.toBeNull())

    act(() => result.current.summon.startRecording())
    act(() => result.current.panels.startRecording('left-sidebar'))
    expect(result.current.summon.recording).toBe(false)

    fireEvent.keyDown(window, { key: 'J', code: 'KeyJ', altKey: true, shiftKey: true })
    expect(set).not.toHaveBeenCalled()
    expect(result.current.panels.bindings['left-sidebar']).toMatchObject({ key: 'j', alt: true, shift: true })
  })

  it('the Search Everywhere recorder yields to the others, and they to it', async () => {
    const { result } = mountEditors(async () => ({}))
    await waitFor(() => expect(result.current.info).not.toBeNull())

    act(() => result.current.search.selectMode('custom'))
    expect(result.current.search.recording).toBe(true)
    act(() => result.current.summon.startRecording())
    expect(result.current.search.recording).toBe(false)

    act(() => result.current.search.startRecording())
    expect(result.current.summon.recording).toBe(false)
    expect(result.current.search.recording).toBe(true)
  })

  it('moving between two panel rows keeps the newly chosen row recording', () => {
    const { result } = mountEditors(async () => ({}))
    act(() => result.current.panels.startRecording('left-sidebar'))
    act(() => result.current.panels.startRecording('side-panel'))
    expect(result.current.panels.recordingId).toBe('side-panel')
  })
})
