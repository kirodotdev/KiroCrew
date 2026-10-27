/**
 * A run its caller did not label is named by the gateway shortly after it
 * starts, and a `subagent_label` frame announces the name.
 *
 * The frame only fills an empty label, can land after the run's done frame, and
 * is dropped for an id the store holds no card for. The completion event
 * carries the same name in its meta, and the completion card names a labelled
 * run by it while keeping the whole task as the hover title.
 */

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, sseSubagentDone, sseSubagentLabel, sseSubagentSpawn } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import SubagentCompletionCard from '../pages/chat/SubagentCompletionCard'
import { parseSubagentCompletion } from '../pages/chat/subagentCompletion'
import type { ChatMessage } from '../types'

const SLOT = 'label-slot'
const OTHER = 'other-slot'

function makeStore() {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(setActiveSlot(SLOT))
  return store
}

describe('sseSubagentLabel', () => {
  it('names a running run that had no label', () => {
    const store = makeStore()
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'a1', task: 'Audit the retry ladder', agent: 'kirocrew' }))
    store.dispatch(sseSubagentLabel({ slot: SLOT, id: 'a1', label: 'Retry ladder audit' }))
    expect(store.getState().chat.subagents.a1.label).toBe('Retry ladder audit')
  })

  it('names a run whose done frame landed first', () => {
    const store = makeStore()
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'a2', task: 'Audit the retry ladder', agent: 'kirocrew' }))
    store.dispatch(sseSubagentDone({ slot: SLOT, id: 'a2', elapsed: 4, outcome: 'completed' }))
    store.dispatch(sseSubagentLabel({ slot: SLOT, id: 'a2', label: 'Late name' }))
    const a = store.getState().chat.subagents.a2
    expect(a.status).toBe('done')
    expect(a.label).toBe('Late name')
  })

  it('names a run in a background slot, and finds a card filed under another slot key', () => {
    const store = makeStore()
    store.dispatch(sseSubagentSpawn({ slot: OTHER, id: 'b1', task: 'T', agent: 'kirocrew' }))
    store.dispatch(sseSubagentLabel({ slot: OTHER, id: 'b1', label: 'Background name' }))
    expect(store.getState().chat.slotActivity[OTHER].subagents.b1.label).toBe('Background name')

    store.dispatch(sseSubagentSpawn({ slot: OTHER, id: 'b2', task: 'T', agent: 'kirocrew' }))
    store.dispatch(sseSubagentLabel({ slot: SLOT, id: 'b2', label: 'Cross-slot name' }))
    expect(store.getState().chat.slotActivity[OTHER].subagents.b2.label).toBe('Cross-slot name')
    expect(store.getState().chat.subagents.b2).toBeUndefined()
  })

  it('ignores an id it holds no card for, minting nothing', () => {
    const store = makeStore()
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'a1', task: 'T', agent: 'kirocrew' }))
    store.dispatch(sseSubagentLabel({ slot: SLOT, id: 'ghost', label: 'Nobody' }))
    store.dispatch(sseSubagentLabel({ slot: OTHER, id: 'ghost', label: 'Nobody' }))
    const chat = store.getState().chat
    expect(Object.keys(chat.subagents)).toEqual(['a1'])
    expect(chat.subagents.a1.label).toBeUndefined()
    expect(chat.slotActivity[OTHER]).toBeUndefined()
  })

  it('never replaces a label the run already has', () => {
    const store = makeStore()
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'a3', task: 'T', agent: 'kirocrew', label: 'Caller label' }))
    store.dispatch(sseSubagentLabel({ slot: SLOT, id: 'a3', label: 'Generated' }))
    expect(store.getState().chat.subagents.a3.label).toBe('Caller label')

    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'a4', task: 'T', agent: 'kirocrew' }))
    store.dispatch(sseSubagentLabel({ slot: SLOT, id: 'a4', label: 'First' }))
    store.dispatch(sseSubagentLabel({ slot: SLOT, id: 'a4', label: 'Second' }))
    expect(store.getState().chat.subagents.a4.label).toBe('First')
  })

  it('drops a frame whose label is empty or not a string', () => {
    const store = makeStore()
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'a5', task: 'T', agent: 'kirocrew' }))
    store.dispatch(sseSubagentLabel({ slot: SLOT, id: 'a5', label: '' }))
    store.dispatch(sseSubagentLabel({ slot: SLOT, id: 'a5', label: 42 as unknown as string }))
    expect(store.getState().chat.subagents.a5.label).toBeUndefined()
  })
})

const TASK = 'Review the retry ladder in the uploader and report every path that retries without a cap'
const PROSE = [
  '[Subagent completion event]',
  'Agent `53e3e5eb` (kirocrew) completed ✅',
  'Task: the task as the prose header wrote it',
  '',
  'Found two uncapped paths.',
].join('\n')

const singleMeta = (extra: Record<string, unknown> = {}) => ({
  subagentCompletion: { kind: 'single', agentId: '53e3e5eb', agentName: 'kirocrew', outcome: 'ok', task: TASK, note: '', ...extra },
})

describe('completion parser: label', () => {
  it('reads the label from meta', () => {
    const p = parseSubagentCompletion(PROSE, singleMeta({ label: 'Retry ladder audit' }))
    expect(p?.kind).toBe('single')
    if (p?.kind !== 'single') return
    expect(p.label).toBe('Retry ladder audit')
  })

  it('reads no label when meta omits it or carries a non-string one, and still uses the meta', () => {
    for (const label of [undefined, 7, { text: 'x' }]) {
      const p = parseSubagentCompletion(PROSE, singleMeta(label === undefined ? {} : { label }))
      expect(p?.kind).toBe('single')
      if (p?.kind !== 'single') continue
      expect(p.label).toBe('')
      // The task is the meta's, not the prose header's: a bad label does not
      // reject the meta.
      expect(p.task).toBe(TASK)
    }
  })

  it('reads no label on the legacy prose path', () => {
    const p = parseSubagentCompletion(PROSE)
    expect(p?.kind).toBe('single')
    if (p?.kind !== 'single') return
    expect(p.label).toBe('')
  })
})

describe('SubagentCompletionCard: a labelled run', () => {
  // A folded card reads no provider, so it renders bare.
  const message = (meta: Record<string, unknown>): ChatMessage => ({ role: 'subagent', content: PROSE, cls: '', meta })
  const renderCard = (m: ChatMessage) => render(<SubagentCompletionCard message={m} />)

  it('shows the label as the headline with the whole task as its hover title', () => {
    renderCard(message(singleMeta({ label: 'Retry ladder audit' })))
    const headline = screen.getByText('Retry ladder audit')
    expect(headline.getAttribute('title')).toBe(TASK)
    expect(screen.queryByText(TASK)).toBeNull()
  })

  it('shows the task alone, with no hover title, when the run has no label', () => {
    renderCard(message(singleMeta()))
    const headline = screen.getByText(TASK)
    expect(headline.hasAttribute('title')).toBe(false)
  })
})
