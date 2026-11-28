/**
 * An app page the shell keeps running while the person is elsewhere (`useKeepAlive`)
 * must not touch the dashboard the person is using. Each SDK path that reaches
 * shared dashboard state is paused while the host says it is hidden (`active`
 * false), and resumes once shown; the app's own work keeps running throughout.
 *
 *   - chat slot activation and switch-back (`useOwnSlotChat`, behind ChatPanel);
 *   - the embedded chat itself, which renders the ACTIVE slot and syncs its URL;
 *   - route changes (`useNavigate`), including a navigate kept from a visible render;
 *   - chat launches (`useChatLauncher().openChat`), which also leave a launch intent.
 */
import React from 'react'
import { act, renderHook } from '@testing-library/react'
import { Provider } from 'react-redux'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestStore } from './helpers'

const { switchSlotCalls } = vi.hoisted(() => ({ switchSlotCalls: [] as string[] }))

vi.mock('../store/chatSlice', async importOriginal => {
  const real = await importOriginal<typeof import('../store/chatSlice')>()
  return {
    ...real,
    // Settles at once and makes the slot active, as a successful switch does.
    switchSlot: (key: string) => (dispatch: (action: unknown) => unknown) => {
      switchSlotCalls.push(key)
      dispatch(real.setActiveSlot(key))
      const done = Promise.resolve()
      return Object.assign(done, { unwrap: () => done })
    },
  }
})

import { useOwnSlotChat } from '../hooks/useOwnSlotChat'
import { setActiveSlot } from '../store/chatSlice'
import { addSlotOptimistic } from '../store/dashboardSlice'
import { AppApiProvider, useChatLauncher, useNavigate } from '../app-sdk'
import type { ChatSlot } from '../types'

const slot = (key: string) => ({ key, messages: 0, running: false }) as ChatSlot

afterEach(() => {
  switchSlotCalls.length = 0
  delete (window as Window & { __mc_chat_launch?: unknown }).__mc_chat_launch
  vi.restoreAllMocks()
})

describe('useOwnSlotChat while its host is hidden', () => {
  function renderOwnSlot(active: boolean) {
    const store = createTestStore()
    store.dispatch(addSlotOptimistic(slot('app-chat')))
    store.dispatch(addSlotOptimistic(slot('person-chat')))
    store.dispatch(setActiveSlot('person-chat'))
    const wrapper = ({ children }: { children: React.ReactNode }) => <Provider store={store}>{children}</Provider>
    const hook = renderHook(({ active: isActive }: { active: boolean }) => useOwnSlotChat({
      slotKey: 'app-chat', onSessionGone: () => {}, active: isActive,
    }), { wrapper, initialProps: { active } })
    return { store, hook }
  }

  it('does not make its session active, and shows no chat, while hidden', async () => {
    const { store, hook } = renderOwnSlot(false)
    await act(async () => { await Promise.resolve() })
    expect(switchSlotCalls).toEqual([])
    expect(store.getState().chat.activeSlot).toBe('person-chat')
    expect(hook.result.current.showChat).toBe(false)
  })

  it('does not switch back when the person picks another chat while it is hidden', async () => {
    const { store, hook } = renderOwnSlot(true)
    await act(async () => { await Promise.resolve() })
    expect(store.getState().chat.activeSlot).toBe('app-chat')
    switchSlotCalls.length = 0

    hook.rerender({ active: false })
    act(() => { store.dispatch(setActiveSlot('person-chat')) })
    await act(async () => { await Promise.resolve() })
    expect(switchSlotCalls).toEqual([])
    expect(store.getState().chat.activeSlot).toBe('person-chat')
  })

  it('makes its session active again once shown', async () => {
    const { store, hook } = renderOwnSlot(false)
    hook.rerender({ active: true })
    await act(async () => { await Promise.resolve() })
    expect(switchSlotCalls).toEqual(['app-chat'])
    expect(store.getState().chat.activeSlot).toBe('app-chat')
    expect(hook.result.current.showChat).toBe(true)
  })
})

function sdkWrapper(navigateFn: (path: string) => void) {
  return function Wrapper({ active, children }: { active: boolean; children: React.ReactNode }) {
    return (
      <AppApiProvider appName="recorder" appVersion="1" allowedApiPaths={[]} allowedEvents={[]}
        subscribeFn={() => () => {}} navigateFn={navigateFn} notifyFn={() => {}} active={active}>
        {children}
      </AppApiProvider>
    )
  }
}

describe('SDK navigation while the host is hidden', () => {
  it('useNavigate moves the dashboard only while shown, including a navigate kept from a visible render', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const navigateFn = vi.fn()
    const Wrapper = sdkWrapper(navigateFn)
    const hook = renderHook(() => useNavigate(), {
      wrapper: ({ children }) => <Wrapper active>{children}</Wrapper>,
    })
    const keptFromVisibleRender = hook.result.current
    keptFromVisibleRender('/schedule')
    expect(navigateFn).toHaveBeenCalledTimes(1)

    const hidden = renderHook(() => useNavigate(), {
      wrapper: ({ children }) => <Wrapper active={false}>{children}</Wrapper>,
    })
    hidden.result.current('/chat')
    expect(navigateFn).toHaveBeenCalledTimes(1)
  })

  it('a navigate taken while shown is refused once the host is hidden', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const navigateFn = vi.fn()
    const Wrapper = sdkWrapper(navigateFn)
    let active = true
    const hook = renderHook(() => useNavigate(), {
      wrapper: ({ children }) => <Wrapper active={active}>{children}</Wrapper>,
    })
    const kept = hook.result.current
    active = false
    hook.rerender()
    kept('/chat')
    expect(navigateFn).not.toHaveBeenCalled()
  })

  it('openChat leaves no launch intent and does not navigate while hidden', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const navigateFn = vi.fn()
    const Wrapper = sdkWrapper(navigateFn)
    const hook = renderHook(() => useChatLauncher(), {
      wrapper: ({ children }) => <Wrapper active={false}>{children}</Wrapper>,
    })
    hook.result.current.openChat({ slotKey: 'recorder-notes', message: 'hello' })
    expect(navigateFn).not.toHaveBeenCalled()
    expect((window as Window & { __mc_chat_launch?: unknown }).__mc_chat_launch).toBeUndefined()
  })

  it('openChat still launches while shown', () => {
    const navigateFn = vi.fn()
    const Wrapper = sdkWrapper(navigateFn)
    const hook = renderHook(() => useChatLauncher(), {
      wrapper: ({ children }) => <Wrapper active>{children}</Wrapper>,
    })
    hook.result.current.openChat({ slotKey: 'recorder-notes' })
    expect(navigateFn).toHaveBeenCalledWith('/chat?sid=recorder-notes')
    expect((window as Window & { __mc_chat_launch?: { slotKey?: string } }).__mc_chat_launch?.slotKey).toBe('recorder-notes')
  })
})
