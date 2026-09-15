/**
 * The reaction state machine: which face a crew shows, and when a sound plays.
 *
 * The two rules worth pinning are both about SILENCE, because both failures are
 * loud: mounting into a running crew must not count as a transition (otherwise
 * every page load flashes and chimes for the whole roster, reporting turns that
 * finished hours ago), and one edge must produce one sound however many places
 * the same crew is on screen.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'

import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import { AVATAR_FLASH_MS, useCrewAvatarState, __resetCrewAvatarSoundForTests } from '../hooks/useCrewAvatarState'
import type { AvatarSounds } from '../lib/crewAvatarState'
import { loadSoundSettings, playPreset } from '../hooks/useNotificationSound'

vi.mock('../hooks/useNotificationSound', async importOriginal => {
  const actual = await importOriginal<typeof import('../hooks/useNotificationSound')>()
  return {
    ...actual,
    playPreset: vi.fn(),
    loadSoundSettings: vi.fn(() => ({ enabled: true, volume: 0.5, perCategory: {} })),
  }
})

const played = vi.mocked(playPreset)
const settings = vi.mocked(loadSoundSettings)

const SLOT = 'chat-7-1'
const slot = (over: Partial<ChatSlot> = {}): ChatSlot =>
  ({ key: SLOT, messages: 2, running: false, mode: 'member', agent: 'radar', ...over }) as ChatSlot

function makeStore(slots: ChatSlot[]) {
  const store = configureStore({ reducer: { dashboard: dashboardReducer } })
  store.dispatch(sseSlots(slots as never))
  return store
}

function Probe({ running, sounds }: { running: boolean; sounds?: AvatarSounds }) {
  const state = useCrewAvatarState({ slotKey: SLOT, agentName: 'radar', running, sounds })
  return <span data-testid="state">{state}</span>
}

/** Watches whichever slot it is pointed at, with no caller-supplied running
 *  flag — so the hook reads the live slot the way the editor header does. */
function Switcher({ slotKey, sounds }: { slotKey: string; sounds?: AvatarSounds }) {
  const state = useCrewAvatarState({ slotKey, sounds })
  return <span data-testid="state">{state}</span>
}

function mount(running: boolean, slots: ChatSlot[] = [slot()], sounds?: AvatarSounds) {
  const store = makeStore(slots)
  const view = render(
    <Provider store={store}>
      <Probe running={running} sounds={sounds} />
    </Provider>,
  )
  const rerender = (nextRunning: boolean) =>
    view.rerender(
      <Provider store={store}>
        <Probe running={nextRunning} sounds={sounds} />
      </Provider>,
    )
  return { ...view, store, rerender }
}

const stateText = () => screen.getByTestId('state').textContent

beforeEach(() => {
  vi.useFakeTimers()
  played.mockClear()
  settings.mockReturnValue({ enabled: true, volume: 0.5, perCategory: {} })
  __resetCrewAvatarSoundForTests()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useCrewAvatarState — the face', () => {
  it('reports working while the slot is running', () => {
    mount(true)
    expect(stateText()).toBe('working')
  })

  it('rests at idle when nothing is running', () => {
    mount(false)
    expect(stateText()).toBe('idle')
  })

  it('flashes done on the stopping edge, then returns to rest', () => {
    const { rerender } = mount(true)
    act(() => { rerender(false) })
    expect(stateText()).toBe('done')
    act(() => { vi.advanceTimersByTime(AVATAR_FLASH_MS - 1) })
    expect(stateText()).toBe('done')
    act(() => { vi.advanceTimersByTime(1) })
    expect(stateText()).toBe('idle')
  })

  it('flashes error when the slot says the last turn ended without a reply', () => {
    // `interrupted` is the backend's own reading of the transcript — the state
    // behind the composer's Resume button — not a signal invented here.
    const { store, rerender } = mount(true, [slot({ running: true })])
    act(() => {
      store.dispatch(sseSlots([slot({ running: false, interrupted: true })] as never))
      rerender(false)
    })
    expect(stateText()).toBe('error')
  })

  it('does not flash on mount, however the crew arrives', () => {
    // The first reading is a baseline. Without this, opening the page flashes
    // every crew in the roster for a turn that ended before it was opened.
    mount(false, [slot({ running: false, interrupted: true })])
    expect(stateText()).toBe('idle')
    act(() => { vi.advanceTimersByTime(AVATAR_FLASH_MS) })
    expect(stateText()).toBe('idle')
  })

  it('working outranks a flash still on screen', () => {
    const { rerender } = mount(true)
    act(() => { rerender(false) })
    expect(stateText()).toBe('done')
    act(() => { rerender(true) })
    expect(stateText()).toBe('working')
  })

  it('restarts the dwell on a second finish inside the first flash', () => {
    const { rerender } = mount(true)
    act(() => { rerender(false) })
    act(() => { vi.advanceTimersByTime(AVATAR_FLASH_MS - 500) })
    act(() => { rerender(true) })
    act(() => { rerender(false) })
    // The verdict is the same word both times, so without a per-edge identity
    // React would skip the write and the dwell would expire 500 ms later.
    act(() => { vi.advanceTimersByTime(AVATAR_FLASH_MS - 1) })
    expect(stateText()).toBe('done')
  })
})

describe('useCrewAvatarState — the sound', () => {
  it('stays silent on mount even when the crew is already working', () => {
    mount(true, [slot({ running: true })], { working: 'blip' })
    expect(played).not.toHaveBeenCalled()
  })

  it('plays the state preset on a real transition, at the user volume', () => {
    const { rerender } = mount(false, [slot()], { working: 'blip', done: 'chime' })
    act(() => { rerender(true) })
    expect(played).toHaveBeenCalledWith('blip', 0.5)
    act(() => { rerender(false) })
    expect(played).toHaveBeenCalledWith('chime', 0.5)
  })

  it('says nothing for a state with no preset', () => {
    const { rerender } = mount(false, [slot()], { done: 'chime' })
    act(() => { rerender(true) })
    expect(played).not.toHaveBeenCalled()
  })

  it('treats an explicit none as deliberate silence', () => {
    const { rerender } = mount(false, [slot()], { working: 'none' })
    act(() => { rerender(true) })
    expect(played).not.toHaveBeenCalled()
  })

  it('honours the global sound switch', () => {
    settings.mockReturnValue({ enabled: false, volume: 0.5, perCategory: {} })
    const { rerender } = mount(false, [slot()], { working: 'blip' })
    act(() => { rerender(true) })
    expect(played).not.toHaveBeenCalled()
  })

  it('honours a muted global volume', () => {
    settings.mockReturnValue({ enabled: true, volume: 0, perCategory: {} })
    const { rerender } = mount(false, [slot()], { working: 'blip' })
    act(() => { rerender(true) })
    expect(played).not.toHaveBeenCalled()
  })

  it('plays once when a flapping running flag re-enters the same state', () => {
    const { rerender } = mount(true, [slot({ running: true })], { done: 'chime' })
    act(() => { rerender(false) })
    act(() => { rerender(true) })
    act(() => { rerender(false) })
    expect(played).toHaveBeenCalledTimes(1)
  })

  it('stays silent when one instance is pointed at an already-running crew', () => {
    // The editor header and the open DM thread reuse ONE instance across crews.
    // Selecting a crew that is already working is not that crew starting.
    const store = configureStore({ reducer: { dashboard: dashboardReducer } })
    store.dispatch(sseSlots([
      slot({ key: 'chat-1-1', agent: 'idle-crew', running: false }),
      slot({ key: 'chat-2-2', agent: 'busy-crew', running: true }),
    ] as never))
    const tree = (key: string) => (
      <Provider store={store}>
        <Switcher slotKey={key} sounds={{ working: 'blip' }} />
      </Provider>
    )
    const view = render(tree('chat-1-1'))
    expect(screen.getByTestId('state').textContent).toBe('idle')
    act(() => { view.rerender(tree('chat-2-2')) })
    expect(screen.getByTestId('state').textContent).toBe('working')
    expect(played).not.toHaveBeenCalled()
  })

  it('plays once when the same crew is on screen twice', () => {
    // A roster row and the open thread's header are two observers of one edge.
    const store = makeStore([slot({ running: true })])
    const tree = (running: boolean) => (
      <Provider store={store}>
        <Probe running={running} sounds={{ done: 'chime' }} />
        <Probe running={running} sounds={{ done: 'chime' }} />
      </Provider>
    )
    const view = render(tree(true))
    act(() => { view.rerender(tree(false)) })
    expect(played).toHaveBeenCalledTimes(1)
  })
})
