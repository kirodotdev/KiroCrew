/**
 * The busy decision a send from outside the composer carries.
 *
 * A comment batch, a voice auto-submit and a follow-up answer used to send with
 * no `steer` flag, so a busy slot always queued them (or parked them behind
 * running sub-agents), whatever its busy-send mode said. `busySteerFlag` is the
 * composer's own default-Send decision (`decideBusySend`) with the inputs its
 * hosts give it; `slotBusySteer` reads those inputs for a slot from the store.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { busySteerFlag, slotBusySteer } from '../components/chat-input/busySend'
import { BUSY_SEND_MODE_LS_KEY } from '../components/BusySendButton'
import { createTestStore } from './helpers'
import type { RootState } from '../store'

const storeMode = (slot: string, mode: string) => localStorage.setItem(`${BUSY_SEND_MODE_LS_KEY}:${slot}`, mode)
const flag = (over: Partial<Parameters<typeof busySteerFlag>[0]> = {}) =>
  busySteerFlag({ slotKey: 'slot-a', busy: true, turnRunning: true, jevAutoConsented: false, ...over })

beforeEach(() => localStorage.clear())

describe('busySteerFlag', () => {
  it('is a plain send while the slot is idle', () => {
    expect(flag({ busy: false, turnRunning: false })).toBeUndefined()
  })

  it('steers a running turn in the default Steer mode', () => {
    expect(flag()).toBe(true)
  })

  it('steers past the sub-agent hold when only sub-agents run', () => {
    expect(flag({ turnRunning: false })).toBe(true)
  })

  it('queues in Queue mode', () => {
    storeMode('slot-a', 'queue')
    expect(flag()).toBeUndefined()
  })

  it('reads the mode of its own slot only', () => {
    storeMode('slot-b', 'queue')
    expect(flag()).toBe(true)
  })

  it('asks Jev in Auto mode while consented and a turn runs', () => {
    storeMode('slot-a', 'auto')
    expect(flag({ jevAutoConsented: true })).toBe('auto')
  })

  it('reads Auto as a plain steer without consent, or with no turn to decide about', () => {
    storeMode('slot-a', 'auto')
    expect(flag({ jevAutoConsented: false })).toBe(true)
    expect(flag({ jevAutoConsented: true, turnRunning: false })).toBe(true)
  })

  it('falls back to the plain send while a stop is pending', () => {
    expect(flag({ stopState: 'soft_pending' })).toBeUndefined()
    expect(flag({ stopState: 'killing' })).toBeUndefined()
  })

  it('steers on a steer-only surface whatever mode the slot stored', () => {
    storeMode('slot-a', 'queue')
    expect(flag({ busyMode: 'steer-only' })).toBe(true)
  })
})

describe('slotBusySteer', () => {
  const stateWith = (chat: Partial<RootState['chat']>, slots: Record<string, unknown>[]) => {
    const initial = createTestStore().getState()
    return createTestStore({
      ...initial,
      chat: { ...initial.chat, ...chat },
      dashboard: { ...initial.dashboard, slots: slots as unknown as RootState['dashboard']['slots'] },
    }).getState()
  }

  it('reads the active slot the way its composer does', () => {
    const state = stateWith({ activeSlot: 'slot-a', slotRunning: true }, [{ key: 'slot-a', messages: 1, running: true }])
    expect(slotBusySteer(state, 'slot-a', false)).toBe(true)
    storeMode('slot-a', 'auto')
    expect(slotBusySteer(state, 'slot-a', true)).toBe('auto')
  })

  it('is a plain send for an idle slot', () => {
    const state = stateWith({ activeSlot: 'slot-a', slotRunning: false }, [{ key: 'slot-a', messages: 1, running: false }])
    expect(slotBusySteer(state, 'slot-a', true)).toBeUndefined()
  })

  it('counts a background slot running from its slots-stream row', () => {
    const state = stateWith({ activeSlot: 'front', slotRunning: false }, [{ key: 'slot-b', messages: 1, running: true }])
    expect(slotBusySteer(state, 'slot-b', false)).toBe(true)
    storeMode('slot-b', 'auto')
    expect(slotBusySteer(state, 'slot-b', true)).toBe('auto')
  })

  it('steers a slot busy only with sub-agents, without asking Jev', () => {
    const state = stateWith({ activeSlot: 'front' }, [{ key: 'slot-b', messages: 1, running: false, subagents_running: true }])
    storeMode('slot-b', 'auto')
    expect(slotBusySteer(state, 'slot-b', true)).toBe(true)
  })

  it('takes the slot row stop state', () => {
    const state = stateWith({ activeSlot: 'slot-a', slotRunning: true }, [{ key: 'slot-a', messages: 1, running: true, stop_state: 'soft_pending' }])
    expect(slotBusySteer(state, 'slot-a', false)).toBeUndefined()
  })
})
