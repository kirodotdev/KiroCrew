/**
 * closeConfirmPrompt — the session-close confirmation names why it asked and
 * what confirming does.
 *
 * Before this, every close path raised the same bare "Close this session?",
 * whether it asked because a turn was running or because the user had turned
 * on "Confirm Before Closing Session", and it said nothing about an unsent
 * draft. A user could not tell what they were agreeing to.
 */
import { describe, it, expect } from 'vitest'
import { closeBusyReasons } from '../lib/closeBusyGate'
import { closeConfirmPrompt } from '../lib/closeConfirmPrompt'
import { i18nT } from '../i18n/t'
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

const GOAL_LOOP = {
  automations: {
    'legacy-1': {
      kind: 'legacy_goal_loop', id: 'legacy-1', slotKey: KEY, message: '',
      idleSecs: 60, maxCycles: 0, cycleCount: 2, active: true,
      lastFireAt: 0, stoppedReason: '',
    },
  },
}

const MONITOR = {
  automations: {
    'monitor-1': { kind: 'structured_monitor', id: 'monitor-1', slotKey: KEY, active: true, terminal: null },
  },
}

const t = (k: string) => i18nT(`hooks.useSessionActions.close_confirm.${k}`)

describe('closeBusyReasons', () => {
  it('names a running turn', () => {
    expect(closeBusyReasons(stateWith({ running: true }), KEY)).toEqual(['turn'])
  })

  it('names a pending approval once, not also as a running turn', () => {
    expect(closeBusyReasons(stateWith({ running: true, pending_approval: true }), KEY)).toEqual(['approval'])
  })

  it('names every kind of live work present, in prompt order', () => {
    const state = stateWith({ running: true, subagents_running: true, queue_depth: 2 }, GOAL_LOOP)
    expect(closeBusyReasons(state, KEY)).toEqual(['turn', 'subagents', 'queue', 'goal_loop'])
  })

  it('names queued children, even on an unlisted slot', () => {
    expect(closeBusyReasons(stateWith({}, { subagentQueued: { [KEY]: 1 } }), KEY)).toEqual(['subagents'])
    expect(closeBusyReasons(stateWith(null, { subagentQueued: { [KEY]: 1 } }), KEY)).toEqual(['subagents'])
  })

  it('names an active structured monitor as a monitor, not a goal loop', () => {
    expect(closeBusyReasons(stateWith({}, MONITOR), KEY)).toEqual(['monitor'])
  })

  it('names a reason for each term of hasLiveSessionWork on its own', () => {
    // Each live-work signal the sidebar's Working lane reads must still ask.
    expect(closeBusyReasons(stateWith({ running: true }), KEY)).toEqual(['turn'])
    expect(closeBusyReasons(stateWith({ subagents_running: true }), KEY)).toEqual(['subagents'])
    expect(closeBusyReasons(stateWith({ queue_depth: 1 }), KEY)).toEqual(['queue'])
    // workflowActive: a running dynamic workflow linked to this session.
    expect(closeBusyReasons(stateWith({}, {
      workflowRuns: { 'run-1': { run_id: 'run-1', status: 'running', sessionKey: KEY, name: 'wf' } },
    }), KEY)).toEqual(['workflow'])
    // detailedSubagentsRunning: a started child in the detailed activity map,
    // while the slot snapshot's own `subagents_running` still reads false.
    expect(closeBusyReasons(stateWith({}, {
      subagents: { 'sub-1': { id: 'sub-1', status: 'running', streaming: '' } },
    }), KEY)).toEqual(['subagents'])
    expect(closeBusyReasons(stateWith({}, { subagentQueued: { [KEY]: 1 } }), KEY)).toEqual(['subagents'])
  })

  it('is empty for a session with nothing running', () => {
    expect(closeBusyReasons(stateWith({ has_options: true, needs_input: true, interrupted: true }), KEY)).toEqual([])
  })
})

describe('closeConfirmPrompt', () => {
  it('returns null when nothing is running and the setting is off', () => {
    expect(closeConfirmPrompt(stateWith({}), KEY, { settingOn: false })).toBeNull()
  })

  it('names the setting as the trigger when nothing is running', () => {
    const prompt = closeConfirmPrompt(stateWith({}), KEY, { settingOn: true })
    expect(prompt).toContain(t('title'))
    expect(prompt).toContain('Nothing is running in it.')
    expect(prompt).toContain('Confirm Before Closing Session')
    expect(prompt).toContain(t('actions'))
  })

  it('names the live work and what closing stops, not the setting', () => {
    const prompt = closeConfirmPrompt(stateWith({ running: true }), KEY, { settingOn: true })
    expect(prompt).toContain('It is still working on a reply. Closing stops that reply.')
    expect(prompt).not.toContain('Confirm Before Closing Session')
  })

  it('asks for live work with the setting off', () => {
    const prompt = closeConfirmPrompt(stateWith({ pending_approval: true }), KEY, { settingOn: false })
    expect(prompt).toContain(t('reason_approval'))
  })

  it('always says an unsent message is kept, without reading the debounced draft store', () => {
    // Drafts survive a close (stored per session), so the line holds whether or
    // not one exists; reading the store would lag the composer's save debounce.
    for (const slot of [{}, { running: true }]) {
      expect(closeConfirmPrompt(stateWith(slot), KEY, { settingOn: true })).toContain('the draft in your message box are kept')
    }
  })

  it('keeps the prompt to three paragraphs and never names the dialog buttons', () => {
    const busy = closeConfirmPrompt(stateWith({ running: true, subagents_running: true, queue_depth: 1 }, GOAL_LOOP), KEY, { settingOn: false })!
    const parts = busy.split('\n\n')
    expect(parts).toHaveLength(3)
    expect(parts[1]).toContain(t('reason_turn'))
    expect(parts[1]).toContain(t('reason_goal_loop'))
    // The browser draws OK / Cancel in its own language, so the copy must not name them.
    expect(busy).not.toMatch(/\bOK\b|\bCancel\b/)
  })

  it('names where the setting lives', () => {
    expect(closeConfirmPrompt(stateWith({}), KEY, { settingOn: true })).toContain('“Chat › Confirm Before Closing Session” is turned on in Settings')
  })

  it('names a monitor in the prompt', () => {
    expect(closeConfirmPrompt(stateWith({}, MONITOR), KEY, { settingOn: false })).toContain(t('reason_monitor'))
  })

  it('the ⌥⇧W alias (checkBusy false) asks only with the setting on, but still names live work', () => {
    expect(closeConfirmPrompt(stateWith({ running: true }), KEY, { settingOn: false, checkBusy: false })).toBeNull()
    const busy = closeConfirmPrompt(stateWith({ running: true }), KEY, { settingOn: true, checkBusy: false })
    expect(busy).toContain(t('reason_turn'))
    expect(busy).not.toContain('Nothing is running in it.')
    const idle = closeConfirmPrompt(stateWith({}), KEY, { settingOn: true, checkBusy: false })
    expect(idle).toContain('Confirm Before Closing Session')
  })

  it('every reason key resolves to authored English, not the raw key', () => {
    for (const k of ['title', 'reason_approval', 'reason_turn', 'reason_subagents', 'reason_queue',
      'reason_workflow', 'reason_goal_loop', 'reason_monitor', 'reason_work', 'reason_setting', 'actions']) {
      expect(t(k)).not.toContain('close_confirm')
    }
  })
})
