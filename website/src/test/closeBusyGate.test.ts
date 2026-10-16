/**
 * sessionIsBusyForClose — when a close must ask even though the user switched
 * `confirmCloseSession` off.
 *
 * The rule is "closing would retire live work", not "the sidebar lane is not
 * idle". The Waiting lane holds sessions with nothing running (an options card,
 * an unanswered question, an interrupted turn); asking there is a prompt the
 * user has no setting to turn off, and in a browser that suppresses dialogs it
 * is a session that can never be closed.
 */
import { describe, it, expect } from 'vitest'
import { sessionIsBusyForClose } from '../lib/closeBusyGate'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

const KEY = 'slot-1'

function stateWith(
  slot: Partial<ChatSlot> | null,
  chat: Record<string, unknown> = {},
): RootState {
  const slots = slot ? [{ key: KEY, title: 'Chat 1', messages: 1, running: false, ...slot }] : []
  return createTestStore({
    dashboard: { slots } as unknown as RootState['dashboard'],
    chat: { activeSlot: KEY, slotHistory: [], ...chat } as unknown as RootState['chat'],
  }).getState()
}

describe('sessionIsBusyForClose', () => {
  describe('asks: closing would stop live work', () => {
    it('a turn in flight', () => {
      expect(sessionIsBusyForClose(stateWith({ running: true }), KEY)).toBe(true)
    })

    it('a turn parked on a tool approval', () => {
      expect(sessionIsBusyForClose(stateWith({ running: true, pending_approval: true }), KEY)).toBe(true)
    })

    it('a pending approval even when the running flag reads false', () => {
      expect(sessionIsBusyForClose(stateWith({ pending_approval: true }), KEY)).toBe(true)
    })

    it('sub-agents still running on the slot', () => {
      expect(sessionIsBusyForClose(stateWith({ subagents_running: true }), KEY)).toBe(true)
    })

    it('messages queued behind the running turn', () => {
      expect(sessionIsBusyForClose(stateWith({ queue_depth: 1 }), KEY)).toBe(true)
    })

    it('an armed goal loop between cycles (slot.running is false there)', () => {
      const state = stateWith({}, {
        automations: {
          'legacy-1': {
            kind: 'legacy_goal_loop', id: 'legacy-1', slotKey: KEY, message: '',
            idleSecs: 60, maxCycles: 0, cycleCount: 2, active: true,
            lastFireAt: 0, stoppedReason: '',
          },
        },
      })
      expect(sessionIsBusyForClose(state, KEY)).toBe(true)
    })

    it('children accepted but only queued (nothing has started, deleteSlot cancels them)', () => {
      expect(sessionIsBusyForClose(stateWith({}, { subagentQueued: { [KEY]: 2 } }), KEY)).toBe(true)
    })

    it('queued children on a slot the dashboard does not list', () => {
      expect(sessionIsBusyForClose(stateWith(null, { subagentQueued: { [KEY]: 1 } }), KEY)).toBe(true)
    })
  })

  describe('does not ask: nothing is running', () => {
    it('an idle session', () => {
      expect(sessionIsBusyForClose(stateWith({}), KEY)).toBe(false)
    })

    it('a slot the dashboard does not list', () => {
      expect(sessionIsBusyForClose(stateWith(null), KEY)).toBe(false)
    })

    it('a finished turn that ended on an options card', () => {
      expect(sessionIsBusyForClose(stateWith({ has_options: true, waiting_for_input: true }), KEY)).toBe(false)
    })

    it('a finished turn that ended on an unanswered question', () => {
      expect(sessionIsBusyForClose(stateWith({ needs_input: true, waiting_for_input: true }), KEY)).toBe(false)
    })

    it('an interrupted turn with nothing newer running', () => {
      expect(sessionIsBusyForClose(stateWith({ interrupted: true }), KEY)).toBe(false)
    })

    it('an options card on a session that is also running still asks', () => {
      // The Waiting lane outranks Working in `inferLane`, so a lane check would
      // call this one not busy while its turn is in flight.
      expect(sessionIsBusyForClose(stateWith({ has_options: true, running: true }), KEY)).toBe(true)
    })

    it('an interrupted turn that a live sub-agent has since superseded still asks', () => {
      expect(sessionIsBusyForClose(stateWith({ interrupted: true, subagents_running: true }), KEY)).toBe(true)
    })
  })
})
