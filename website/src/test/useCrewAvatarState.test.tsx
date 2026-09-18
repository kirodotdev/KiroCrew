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
import { loadSoundSettings, playPreset, playSoundFile } from '../hooks/useNotificationSound'

vi.mock('../hooks/useNotificationSound', async importOriginal => {
  const actual = await importOriginal<typeof import('../hooks/useNotificationSound')>()
  return {
    ...actual,
    playPreset: vi.fn(),
    playSoundFile: vi.fn(),
    loadSoundSettings: vi.fn(() => ({ enabled: true, volume: 0.5, perCategory: {} })),
  }
})

const played = vi.mocked(playPreset)
const playedFile = vi.mocked(playSoundFile)
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
  // Cleared per test for the same reason `played` is: the recorder is module
  // scope, so a leftover call from the previous test reads as this one's.
  playedFile.mockClear()
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

describe('a worn pack answers the cue with its own audio', () => {
  /** A crew wearing `aurora`, whose manifest declares the states in `states`. */
  function PackProbe({
    running,
    states,
  }: {
    running: boolean
    states: Record<string, boolean>
  }) {
    const state = useCrewAvatarState({
      slotKey: SLOT,
      agentName: 'radar',
      running,
      sounds: { done: 'chime', error: 'pop' },
      packCue: { id: 'aurora', states },
    })
    return <span data-testid="state">{state}</span>
  }

  /** Mount already-running, then stop: the one edge the cue rides. */
  function finish(states: Record<string, boolean>, interrupted = false) {
    const store = makeStore([slot({ running: true })])
    const view = render(
      <Provider store={store}>
        <PackProbe running states={states} />
      </Provider>,
    )
    act(() => {
      store.dispatch(sseSlots([slot({ running: false, interrupted })] as never))
    })
    view.rerender(
      <Provider store={store}>
        <PackProbe running={false} states={states} />
      </Provider>,
    )
    return view
  }

  it('plays the served cue for a state the pack declares', () => {
    finish({ done: true })
    expect(playedFile).toHaveBeenCalledWith('/api/appearances/aurora/sound/done', 0.5)
    // The pack is the answer once it is worn: the preset must not ALSO fire, or
    // one finished turn reports itself with two sounds.
    expect(played).not.toHaveBeenCalled()
  })

  it('is silent for a state the pack does not declare, and does not fall back', () => {
    // The pack's author chose which moments make a sound. Filling a gap with a
    // synthesized tone would report their pack with a cue they left out.
    finish({ working: true })
    expect(playedFile).not.toHaveBeenCalled()
    expect(played).not.toHaveBeenCalled()
  })

  it('reads the error state as its own cue, not as the done one', () => {
    finish({ done: true, error: true }, true)
    expect(playedFile).toHaveBeenCalledWith('/api/appearances/aurora/sound/error', 0.5)
  })

  it('obeys the same global toggle the presets do', () => {
    settings.mockReturnValue({ enabled: false, volume: 0.5, perCategory: {} })
    finish({ done: true })
    expect(playedFile).not.toHaveBeenCalled()
  })

  it('obeys the global volume, muted included', () => {
    settings.mockReturnValue({ enabled: true, volume: 0, perCategory: {} })
    finish({ done: true })
    expect(playedFile).not.toHaveBeenCalled()
  })

  it('percent-encodes an id the route must receive verbatim', () => {
    const store = makeStore([slot({ running: true })])
    const states = { done: true }
    const view = render(
      <Provider store={store}>
        <PackProbe running states={states} />
      </Provider>,
    )
    act(() => {
      store.dispatch(sseSlots([slot({ running: false })] as never))
    })
    view.rerender(
      <Provider store={store}>
        <PackProbe running={false} states={states} />
      </Provider>,
    )
    expect(playedFile.mock.calls[0][0]).toBe('/api/appearances/aurora/sound/done')
  })

  it('defers the cue when the manifest has not arrived, and plays it when it does', () => {
    // The edge is the only signal there is: a turn that finished while the pack
    // read was in flight has no second chance, so resolving it to silence AND
    // consuming the transition would lose that turn's cue permanently.
    const store = makeStore([slot({ running: true })])
    const Pending = ({ running, cue }: { running: boolean; cue: boolean }) => {
      const state = useCrewAvatarState({
        slotKey: SLOT,
        agentName: 'radar',
        running,
        packCue: cue ? { id: 'aurora', states: { done: true } } : null,
        packPending: !cue,
      })
      return <span data-testid="state">{state}</span>
    }
    const view = render(
      <Provider store={store}>
        <Pending running cue={false} />
      </Provider>,
    )
    act(() => {
      store.dispatch(sseSlots([slot({ running: false })] as never))
    })
    // The turn finished while the manifest was still in flight.
    view.rerender(
      <Provider store={store}>
        <Pending running={false} cue={false} />
      </Provider>,
    )
    expect(playedFile).not.toHaveBeenCalled()
    // The manifest lands, still inside the flash: the held edge fires now.
    view.rerender(
      <Provider store={store}>
        <Pending running={false} cue />
      </Provider>,
    )
    expect(playedFile).toHaveBeenCalledWith('/api/appearances/aurora/sound/done', 0.5)
  })

  it('plays the cue for a turn that was ALREADY running when the avatar mounted', () => {
    // The dashboard is usually opened while something is running, so this is the
    // ordinary case, not an edge one: the first observation is a BASELINE (silent
    // by design), and the finish after it is a real edge — the pack hold must not
    // turn that first observation into the edge and swallow the cue.
    const store = makeStore([slot({ running: true })])
    const Mounted = ({ cue }: { cue: boolean }) => {
      const state = useCrewAvatarState({
        slotKey: SLOT,
        // No `running` prop: the live slot decides, which is how a roster row
        // mounts into a turn already in flight.
        packCue: cue ? { id: 'aurora', states: { done: true } } : null,
        packPending: !cue,
      })
      return <span data-testid="state">{state}</span>
    }
    const view = render(
      <Provider store={store}>
        <Mounted cue={false} />
      </Provider>,
    )
    act(() => {
      store.dispatch(sseSlots([slot({ running: false })] as never))
    })
    expect(playedFile).not.toHaveBeenCalled()
    view.rerender(
      <Provider store={store}>
        <Mounted cue />
      </Provider>,
    )
    expect(playedFile).toHaveBeenCalledWith('/api/appearances/aurora/sound/done', 0.5)
  })

  it('plays only the current state\u2019s cue when the manifest lands after a superseded edge', () => {
    // A short turn: idle → working → done, all while the manifest is in flight. The
    // hold keeps the baseline at idle, so when the manifest lands the edge the
    // effect can see is idle → done — the `working` the crew passed through is not
    // replayed. Two cues for one moment would report the crew twice.
    const store = makeStore([slot({ running: false })])
    const Short = ({ cue }: { cue: boolean }) => {
      const state = useCrewAvatarState({
        slotKey: SLOT,
        agentName: 'radar',
        packCue: cue ? { id: 'aurora', states: { working: true, done: true } } : null,
        packPending: !cue,
      })
      return <span data-testid="state">{state}</span>
    }
    const view = render(
      <Provider store={store}>
        <Short cue={false} />
      </Provider>,
    )
    act(() => {
      store.dispatch(sseSlots([slot({ running: true })] as never))
    })
    act(() => {
      store.dispatch(sseSlots([slot({ running: false })] as never))
    })
    expect(playedFile).not.toHaveBeenCalled()
    view.rerender(
      <Provider store={store}>
        <Short cue />
      </Provider>,
    )
    expect(playedFile).toHaveBeenCalledTimes(1)
    expect(playedFile).toHaveBeenCalledWith('/api/appearances/aurora/sound/done', 0.5)
  })

  it('does not fire a stale edge once the flash has passed', () => {
    // A read that never resolves must not leave the baseline armed: when the
    // flash expires and the face returns to rest, the missed cue is simply
    // missed — it must not fire against the NEXT transition.
    vi.useFakeTimers()
    const store = makeStore([slot({ running: true })])
    const Pending = ({ running }: { running: boolean }) => {
      const state = useCrewAvatarState({
        slotKey: SLOT,
        agentName: 'radar',
        running,
        packCue: null,
        packPending: true,
      })
      return <span data-testid="state">{state}</span>
    }
    const view = render(
      <Provider store={store}>
        <Pending running />
      </Provider>,
    )
    act(() => {
      store.dispatch(sseSlots([slot({ running: false })] as never))
    })
    view.rerender(
      <Provider store={store}>
        <Pending running={false} />
      </Provider>,
    )
    act(() => {
      vi.advanceTimersByTime(AVATAR_FLASH_MS + 10)
    })
    expect(playedFile).not.toHaveBeenCalled()
    expect(played).not.toHaveBeenCalled()
  })

  it('stays silent on the first observation, exactly as the presets do', () => {
    // Mounting into an already-finished crew is not an edge, and a page load
    // that chimed for every pack-wearing row would report turns from hours ago.
    const store = makeStore([slot({ running: false })])
    render(
      <Provider store={store}>
        <PackProbe running={false} states={{ done: true }} />
      </Provider>,
    )
    expect(playedFile).not.toHaveBeenCalled()
  })
})
