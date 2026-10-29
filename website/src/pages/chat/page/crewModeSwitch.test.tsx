/**
 * `useCrewModeSwitch` keeps a pending switch per chat: a press in a second chat
 * while the first chat's switch is still in flight must not drop the first
 * chat's composer hold.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'
import { setCrewModeCatalog } from '../../../lib/crewMode'
import type { ChatSlot } from '../../../types'
import { useCrewModeSwitch } from './crewModeSwitch'
import { crewModePreviewOn } from '../../../test/crewModePreview'

const plain = (key: string, agent = 'default'): ChatSlot => ({ key, agent, agent_kind: 'template', memory_mode: 'persistent', origin: 'user' } as unknown as ChatSlot)

function setup() {
  const pending: Array<{ resolve: () => void }> = []
  const switchAgent = vi.fn((_agent: string) => new Promise<void>(resolve => { pending.push({ resolve }) }))
  const store = createTestStore()
  const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
  const hook = renderHook(
    ({ slot }: { slot: ChatSlot }) => useCrewModeSwitch({ currentSlot: slot, defaultAgent: 'default', hasUserMessage: false, switchAgent, openView: () => {} }),
    { wrapper, initialProps: { slot: plain('a') } },
  )
  return { hook, pending, switchAgent }
}

describe('useCrewModeSwitch pending switches', () => {
  beforeEach(() => setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor']))
  afterEach(() => setCrewModeCatalog(undefined, undefined))

  it("keeps chat A held when chat B turns Crew Mode on while A's switch is in flight", async () => {
    const { hook, pending } = setup()
    act(() => { hook.result.current.turnOnCrewMode() })
    expect(hook.result.current.crewModeSwitching).toBe('on')
    // Move to chat B and press there too; A's switch has not landed.
    hook.rerender({ slot: plain('b') })
    expect(hook.result.current.crewModeSwitching).toBeNull()
    act(() => { hook.result.current.turnOnCrewMode() })
    expect(hook.result.current.crewModeSwitching).toBe('on')
    // Back on A: still held, so a send cannot start the turn on the old agent.
    hook.rerender({ slot: plain('a') })
    expect(hook.result.current.crewModeSwitching).toBe('on')
    // B's switch lands: A stays held until its own lands.
    await act(async () => { pending[1].resolve() })
    expect(hook.result.current.crewModeSwitching).toBe('on')
    await act(async () => { pending[0].resolve() })
    expect(hook.result.current.crewModeSwitching).toBeNull()
  })

  it("keeps chat A's turning-off hold when chat B turns off too", async () => {
    const { hook, pending } = setup()
    hook.rerender({ slot: plain('a', 'kirocrew-conductor') })
    act(() => { hook.result.current.turnOffCrewMode?.() })
    expect(hook.result.current.crewModeSwitching).toBe('off')
    hook.rerender({ slot: plain('b', 'kirocrew-conductor') })
    act(() => { hook.result.current.turnOffCrewMode?.() })
    hook.rerender({ slot: plain('a', 'kirocrew-conductor') })
    expect(hook.result.current.crewModeSwitching).toBe('off')
    await act(async () => { pending[1].resolve() })
    expect(hook.result.current.crewModeSwitching).toBe('off')
    await act(async () => { pending[0].resolve() })
    expect(hook.result.current.crewModeSwitching).toBeNull()
  })
})

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()
