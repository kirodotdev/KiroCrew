/**
 * Windows and Linux fullscreen hides the caption buttons and the menu bar, so
 * the desktop shell must say how to leave it. These pin when the shared notice
 * banner shows the F11 hint and when it goes away.
 */
import React from 'react'
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { renderHook, act, cleanup } from '@testing-library/react'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { setAgentSwitchNotice } from '../store/chatSlice'
import { useFullScreenExitHint } from '../shell/platform/fullScreenExitHint'
import { i18nT } from '../i18n/t'

type Relay = (fs: boolean, menuBarOverlap: boolean, pageRaised: boolean) => void

let relay: Relay | null = null
let unsubscribed = false

function install(platform: string) {
  ;(window as { kirocrew?: unknown }).kirocrew = { isElectron: true, platform }
  ;(window as { electronAPI?: unknown }).electronAPI = {
    onFullScreenChanged: (cb: Relay) => {
      relay = cb
      return () => { unsubscribed = true }
    },
  }
}

function mount() {
  const store = createTestStore()
  const view = renderHook(() => useFullScreenExitHint(), {
    wrapper: ({ children }: { children: React.ReactNode }) => <Provider store={store}>{children}</Provider>,
  })
  return { store, view }
}

const hint = () => i18nT('app.fullscreen_exit_hint')

describe('useFullScreenExitHint', () => {
  beforeEach(() => {
    relay = null
    unsubscribed = false
  })
  afterEach(() => {
    cleanup()
    delete (window as { kirocrew?: unknown }).kirocrew
    delete (window as { electronAPI?: unknown }).electronAPI
  })

  it('shows the F11 hint when a Windows window enters fullscreen and clears it on exit', () => {
    install('win32')
    const { store } = mount()
    expect(relay).not.toBeNull()
    act(() => relay!(true, true, false))
    expect(store.getState().chat.agentSwitchNotice?.message).toBe(hint())
    expect(hint()).toContain('F11')
    act(() => relay!(false, true, false))
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('shows the hint once per fullscreen, not on every settle re-send', () => {
    install('linux')
    const { store } = mount()
    act(() => relay!(true, true, false))
    act(() => { store.dispatch(setAgentSwitchNotice(null)) })
    act(() => relay!(true, true, false))
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('leaves a newer notice alone when fullscreen ends', () => {
    install('win32')
    const { store } = mount()
    act(() => relay!(true, true, false))
    act(() => { store.dispatch(setAgentSwitchNotice('something else')) })
    act(() => relay!(false, true, false))
    expect(store.getState().chat.agentSwitchNotice?.message).toBe('something else')
  })

  it('does not replace a notice already on screen', () => {
    install('win32')
    const { store } = mount()
    act(() => { store.dispatch(setAgentSwitchNotice('open the session')) })
    act(() => relay!(true, true, false))
    expect(store.getState().chat.agentSwitchNotice?.message).toBe('open the session')
    act(() => relay!(false, true, false))
    expect(store.getState().chat.agentSwitchNotice?.message).toBe('open the session')
  })

  it('stays quiet for a fullscreen a video raised', () => {
    install('win32')
    const { store } = mount()
    act(() => relay!(true, true, true))
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })

  it('does not subscribe on macOS, which keeps its own controls', () => {
    install('darwin')
    mount()
    expect(relay).toBeNull()
  })

  it('unsubscribes on unmount', () => {
    install('win32')
    const { view } = mount()
    view.unmount()
    expect(unsubscribed).toBe(true)
  })
})
