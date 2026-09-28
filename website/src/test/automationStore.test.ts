import { configureStore } from '@reduxjs/toolkit'
import { describe, expect, it } from 'vitest'
import reducer, {
  selectAutomationForSlot,
  setAutomations,
  sseAutomation,
} from '../store/chatSlice'
import { normalizeAutomationRecord } from '../monitoring/automation'

const legacy = normalizeAutomationRecord({
  id: 'legacy', slot_key: 'chat-1', message: 'Keep going', idle_secs: 60,
  max_cycles: 0, cycle_count: 3, active: true, last_fire_ts: 0,
})!

const terminal = normalizeAutomationRecord({
  id: 'monitor', slot_key: 'chat-2', message: '', idle_secs: 300,
  max_cycles: 0, cycle_count: 0, active: false, last_fire_ts: 0,
  monitor: {
    version: 1, kind: 'github_pull_request', target: 'https://github.com/o/r/pull/1',
    objective: 'review_ready', cadence_secs: 300, wake_instructions: '',
    budgets: { max_runtime_secs: 14400, max_agent_turns: 8, max_tokens: 250000, max_provider_errors: 3 },
    wake_count: 1, agent_turns: 1, input_tokens: 20, output_tokens: 10,
    probe_count: 2, provider_error_count: 0, token_usage_known: true,
    outcome: 'success', stopped_reason: 'success', stopped_at: 10,
  },
})!

describe('automation collection', () => {
  const makeStore = () => configureStore({ reducer: { chat: reducer } })

  it('uses one authoritative collection for slot selectors', () => {
    const store = makeStore()
    store.dispatch(setAutomations({
      records: [legacy, terminal], legacyComplete: true, structuredComplete: true,
    }))

    expect(selectAutomationForSlot(store.getState(), 'chat-1')).toEqual(legacy)
    expect(selectAutomationForSlot(store.getState(), 'chat-2')).toEqual(terminal)
  })

  it('drops stopped legacy loops but retains terminal structured monitors', () => {
    const store = makeStore()
    store.dispatch(setAutomations({
      records: [legacy, terminal], legacyComplete: true, structuredComplete: true,
    }))
    store.dispatch(sseAutomation({ ...legacy, active: false }))
    store.dispatch(sseAutomation(terminal))

    expect(selectAutomationForSlot(store.getState(), 'chat-1')).toBeNull()
    expect(selectAutomationForSlot(store.getState(), 'chat-2')).toEqual(terminal)
  })
})
