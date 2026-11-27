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
import { act, fireEvent, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

// The shell flag is read at module load in lib/electron.ts; mock it so the
// same test file can exercise both the browser and desktop branches.
const electronEnv = vi.hoisted(() => ({ isElectron: true }))
vi.mock('../lib/electron', () => electronEnv)

import { canRecordGlobalHotkey, useGlobalHotkey, useGlobalHotkeyRecorder } from './useGlobalHotkey'

type BridgeWindow = Window & {
  electronAPI?: { getGlobalHotkey?: () => Promise<unknown>; setGlobalHotkey?: (accelerator: string) => Promise<unknown> }
}

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

describe('useGlobalHotkeyRecorder', () => {
  const OFF = { accelerator: '', requested: '', default: 'Alt+Shift+K' }
  const bound = (accelerator: string) => ({ accelerator, requested: accelerator, default: 'Alt+Shift+K' })

  // One QueryClient under both hooks, as in the app: the recorder writes the
  // shell's answer into the query the reader holds.
  function mount(set: (accelerator: string) => Promise<unknown>, initial = OFF) {
    ;(window as BridgeWindow).electronAPI = { getGlobalHotkey: async () => initial, setGlobalHotkey: set }
    return renderHook(() => ({ info: useGlobalHotkey(), recorder: useGlobalHotkeyRecorder() }), { wrapper })
  }

  it('records the next real chord, sends it to the shell and shows what the shell bound', async () => {
    const set = vi.fn(async (accelerator: string) => bound(accelerator))
    const { result } = mount(set)
    await waitFor(() => expect(result.current.info).toEqual(OFF))

    act(() => result.current.recorder.startRecording())
    // A bare key is not a chord to grab system-wide: keep waiting.
    fireEvent.keyDown(window, { key: 'j', code: 'KeyJ' })
    expect(set).not.toHaveBeenCalled()
    expect(result.current.recorder.recording).toBe(true)

    fireEvent.keyDown(window, { key: 'J', code: 'KeyJ', altKey: true, shiftKey: true })
    expect(set).toHaveBeenCalledExactlyOnceWith('Alt+Shift+J')
    await waitFor(() => expect(result.current.info).toEqual(bound('Alt+Shift+J')))
    expect(result.current.recorder.recording).toBe(false)

    // Not recording any more: a later chord is not captured.
    fireEvent.keyDown(window, { key: 'L', code: 'KeyL', altKey: true })
    expect(set).toHaveBeenCalledOnce()
  })

  it('stops the recorded chord there, so it cannot also fire an app shortcut', async () => {
    const { result } = mount(async accelerator => bound(accelerator))
    await waitFor(() => expect(result.current.info).not.toBeNull())
    const appShortcut = vi.fn()
    document.addEventListener('keydown', appShortcut)
    try {
      act(() => result.current.recorder.startRecording())
      fireEvent.keyDown(document.body, { key: 'J', code: 'KeyJ', altKey: true })
      expect(appShortcut).not.toHaveBeenCalled()
      await waitFor(() => expect(result.current.info).toEqual(bound('Alt+J')))
    } finally {
      document.removeEventListener('keydown', appShortcut)
    }
  })

  it('Escape cancels recording and leaves the hotkey alone', async () => {
    const set = vi.fn(async (accelerator: string) => bound(accelerator))
    const { result } = mount(set, bound('Alt+Shift+K'))
    await waitFor(() => expect(result.current.info).not.toBeNull())
    act(() => result.current.recorder.startRecording())
    fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' })
    expect(result.current.recorder.recording).toBe(false)
    expect(set).not.toHaveBeenCalled()
    expect(result.current.info).toEqual(bound('Alt+Shift+K'))
  })

  it('clear turns the hotkey off', async () => {
    const set = vi.fn(async () => OFF)
    const { result } = mount(set, bound('Alt+Shift+K'))
    await waitFor(() => expect(result.current.info).toEqual(bound('Alt+Shift+K')))
    act(() => result.current.recorder.clear())
    expect(set).toHaveBeenCalledExactlyOnceWith('')
    await waitFor(() => expect(result.current.info).toEqual(OFF))
  })

  it('reports a change the shell refused, keeps the old hotkey, and recovers on the next change', async () => {
    const set = vi.fn<(accelerator: string) => Promise<unknown>>()
      .mockRejectedValueOnce(new Error('global-hotkey:set is restricted to the local dashboard'))
      .mockImplementation(async accelerator => bound(accelerator))
    const { result } = mount(set, bound('Alt+Shift+K'))
    await waitFor(() => expect(result.current.info).not.toBeNull())

    act(() => result.current.recorder.clear())
    await waitFor(() => expect(result.current.recorder.failed).toBe(true))
    expect(result.current.info).toEqual(bound('Alt+Shift+K'))

    act(() => result.current.recorder.startRecording())
    fireEvent.keyDown(window, { key: 'J', code: 'KeyJ', altKey: true })
    await waitFor(() => expect(result.current.info).toEqual(bound('Alt+J')))
    expect(result.current.recorder.failed).toBe(false)
  })
})

describe('canRecordGlobalHotkey', () => {
  it('is true only on a desktop shell that exposes the setter', () => {
    // A window on a remote host and an older shell: the reader, no setter.
    ;(window as BridgeWindow).electronAPI = { getGlobalHotkey: async () => ({}) }
    expect(canRecordGlobalHotkey()).toBe(false)

    ;(window as BridgeWindow).electronAPI = { getGlobalHotkey: async () => ({}), setGlobalHotkey: async () => ({}) }
    expect(canRecordGlobalHotkey()).toBe(true)

    electronEnv.isElectron = false
    expect(canRecordGlobalHotkey()).toBe(false)
  })
})
