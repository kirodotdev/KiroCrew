/**
 * useCloseAction: the Settings > Display > Desktop app close-button switch
 * (#12204). The row appears only after a read succeeds, and the switch shows
 * only values the main process returned.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useCloseAction } from '../hooks/useCloseAction'

// A stand-in for the main process: one stored value, and writes that settle
// when the test says so.
function fakeMain({ stored = 'background', refuseGet = false } = {}) {
  const writes: Array<{ value: string, store: () => void, refuse: () => void }> = []
  const state = { stored }
  const api = {
    get: vi.fn(() => (refuseGet ? Promise.reject(new Error('restricted')) : Promise.resolve(state.stored))),
    set: vi.fn((value: string) => new Promise<string>((resolve, reject) => {
      writes.push({
        value,
        store: () => { state.stored = value === 'quit' ? 'quit' : 'background'; resolve(state.stored) },
        refuse: () => reject(new Error("close-action:set is restricted to the main window's dashboard")),
      })
    })),
  }
  ;(window as unknown as { closeActionAPI?: unknown }).closeActionAPI = api
  return { api, writes, state }
}

afterEach(() => {
  delete (window as unknown as { closeActionAPI?: unknown }).closeActionAPI
})

async function ready() {
  const hook = renderHook(() => useCloseAction())
  await waitFor(() => expect(hook.result.current.closeActionSupported).toBe(true))
  return hook
}

describe('useCloseAction', () => {
  it('reports unsupported and stays inert without a bridge (browser, PWA)', () => {
    const { result } = renderHook(() => useCloseAction())
    expect(result.current.closeActionSupported).toBe(false)
    expect(result.current.closeAction).toBe('background')
    act(() => { result.current.setCloseAction('quit') })
    expect(result.current.closeAction).toBe('background')
    expect(result.current.closeActionPending).toBe(false)
  })

  it('stays unsupported when the main process refuses the read (connection window)', async () => {
    const main = fakeMain({ refuseGet: true })
    const { result } = renderHook(() => useCloseAction())
    await waitFor(() => expect(main.api.get).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
    expect(result.current.closeActionSupported).toBe(false)
  })

  it('becomes supported with the stored value once a read succeeds', async () => {
    fakeMain({ stored: 'quit' })
    const { result } = await ready()
    expect(result.current.closeAction).toBe('quit')
  })

  it('reads an unexpected stored value as background', async () => {
    fakeMain({ stored: 'exit' })
    const { result } = await ready()
    expect(result.current.closeAction).toBe('background')
  })

  it('never shows a value that is not stored yet, and is disabled meanwhile', async () => {
    const main = fakeMain({ stored: 'quit' })
    const { result } = await ready()
    act(() => { result.current.setCloseAction('background') })
    // Until main answers, the close button still quits, so the switch says so.
    expect(main.api.set).toHaveBeenCalledWith('background')
    expect(result.current.closeAction).toBe('quit')
    expect(result.current.closeActionPending).toBe(true)
    await act(async () => { main.writes[0].store() })
    expect(result.current.closeAction).toBe('background')
    expect(result.current.closeActionPending).toBe(false)
  })

  it('ignores a second toggle while a change is being applied', async () => {
    const main = fakeMain()
    const { result } = await ready()
    act(() => { result.current.setCloseAction('quit') })
    act(() => { result.current.setCloseAction('background') })
    expect(main.api.set).toHaveBeenCalledTimes(1)
    await act(async () => { main.writes[0].store() })
    act(() => { result.current.setCloseAction('background') })
    expect(main.api.set).toHaveBeenCalledTimes(2)
  })

  it('renders the value main stored, not the one requested', async () => {
    fakeMain()
    // A main process that stores something other than what was asked.
    const api = (window as unknown as { closeActionAPI: { set: (v: string) => Promise<string> } }).closeActionAPI
    api.set = vi.fn(() => Promise.resolve('background'))
    const { result } = await ready()
    await act(async () => { result.current.setCloseAction('quit') })
    expect(result.current.closeAction).toBe('background')
  })

  it('a refused write changes nothing, re-enables the switch and reports the failure', async () => {
    const main = fakeMain()
    const { result } = await ready()
    expect(result.current.closeActionFailed).toBe(false)
    act(() => { result.current.setCloseAction('quit') })
    await act(async () => { main.writes[0].refuse() })
    expect(result.current.closeAction).toBe('background')
    expect(result.current.closeActionPending).toBe(false)
    expect(result.current.closeActionFailed).toBe(true)
    // The next attempt clears the notice, and a success keeps it clear.
    act(() => { result.current.setCloseAction('quit') })
    expect(result.current.closeActionFailed).toBe(false)
    await act(async () => { main.writes[1].store() })
    expect(result.current.closeAction).toBe('quit')
    expect(result.current.closeActionFailed).toBe(false)
  })
})
