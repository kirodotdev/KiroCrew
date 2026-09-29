/**
 * Prototype-pollution guard, family by family.
 *
 * Slot keys, sub-agent ids, run ids, tool-call ids and session keys all arrive
 * from WebSocket frames. Every reducer that indexes a per-key state map with
 * one of them must refuse `__proto__` / `constructor` / `prototype` (or reroute
 * it to an inert own property), so no frame can write through to
 * `Object.prototype` or leave a real own entry named after the hostile key.
 */
import { describe, expect, it } from 'vitest'
import type { UnknownAction } from '@reduxjs/toolkit'
import reducer, {
  appendQueuedMessage,
  appendSlotMessage,
  clearFolderSuggestion,
  clearFollowupCard,
  clearQuestionCard,
  clearSlotCache,
  clearTerminalSubagents,
  confirmOptimisticSend,
  dismissFollowupItem,
  editQueuedMessage,
  hydrateSlotMessages,
  markSubagentApproving,
  reconcileWorkflowRuns,
  removeAutomation,
  reorderQueuedMessages,
  resolveByApprovalId,
  resolveOptimisticSteer,
  setAutomations,
  setFolderSuggestion,
  setFollowupCard,
  setQuestionCard,
  setQuestionDraft,
  setSlotStatusDetail,
  setStopPressedAt,
  settleStopNotRunning,
  sideClose,
  sideOptimisticAppend,
  sseActivityEvent,
  sseAutomation,
  sseChatMessage,
  sseContextUsage,
  sseMcpAppRender,
  sseSideQueue,
  sseSideResult,
  sseSubagentBatchChunks,
  sseSubagentBatchUpdate,
  sseSubagentDone,
  sseSubagentPending,
  sseSubagentQueued,
  sseSubagentRetrying,
  sseSubagentSnapshot,
  sseSubagentSpawn,
  sseSubagentStalled,
  sseSubagentTool,
  sseToolActivity,
  sseToolResult,
  sseWorkflowEvent,
  syncSlotRunningFromServer,
} from './chatSlice'
import type { AutomationRecord } from '../monitoring/automation'
import type { McpAppRenderPayload } from '../lib/mcpAppSrcdoc'
import type { WorkflowRunSummary } from '../types'

const HOSTILE = ['__proto__', 'constructor', 'prototype'] as const

const automation = (slotKey: string): AutomationRecord => ({
  kind: 'legacy_goal_loop', slotKey, active: true,
} as unknown as AutomationRecord)

/** One frame per reducer family, each keyed by the hostile value `k`. */
const FRAMES: Array<[string, (k: string) => UnknownAction]> = [
  ['sseChatMessage chunk', k => sseChatMessage({ slot: k, role: 'chunk', content: 'x', seq: 1 })],
  ['sseChatMessage user', k => sseChatMessage({ slot: k, role: 'user', content: 'x', meta: { mid: 'm1', polluted: true } })],
  ['appendSlotMessage', k => appendSlotMessage({ slot: k, message: { role: 'user', content: 'x', cls: '' } })],
  ['appendQueuedMessage', k => appendQueuedMessage({ slot: k, content: 'q', ts: 't', queue_id: 'q1' })],
  ['editQueuedMessage', k => editQueuedMessage({ slot: k, queue_id: 'q1', content: 'q2' })],
  ['reorderQueuedMessages', k => reorderQueuedMessages({ slot: k, order: ['q1'] })],
  ['confirmOptimisticSend', k => confirmOptimisticSend({ slot: k, sendId: 's1' })],
  ['resolveOptimisticSteer', k => resolveOptimisticSteer({ slot: k, sendId: 's1', outcome: 'turn' })],
  ['resolveByApprovalId', k => resolveByApprovalId({ id: 'a1', slot: k, decision: 'rejected' })],
  ['hydrateSlotMessages', k => hydrateSlotMessages({ slot: k, messages: [{ role: 'user', content: 'x', cls: '' }], hasMore: false })],
  ['clearSlotCache', k => clearSlotCache(k)],
  ['setQuestionCard', k => setQuestionCard({ slot: k, card_id: 'c1', questions: [{ question: 'q', options: [] }] })],
  ['setQuestionDraft', k => setQuestionDraft({ slot: k, active: true })],
  ['clearQuestionCard', k => clearQuestionCard({ slot: k })],
  ['setFollowupCard', k => setFollowupCard({ slot: k, items: [{ title: 't', description: 'd', prompt: 'p' }] })],
  ['dismissFollowupItem', k => dismissFollowupItem({ slot: k, index: 0 })],
  ['clearFollowupCard', k => clearFollowupCard({ slot: k })],
  ['setFolderSuggestion', k => setFolderSuggestion({ slot: k, folderId: 'f', folderName: 'n', breadcrumb: 'b' })],
  ['clearFolderSuggestion', k => clearFolderSuggestion({ slot: k })],
  ['sseContextUsage', k => sseContextUsage({ slot: k, pct: 10, used_tokens: 1, window_tokens: 10 })],
  ['setSlotStatusDetail', k => setSlotStatusDetail({ slot: k, kind: 'thinking', ts: 1 } as never)],
  ['setStopPressedAt', k => setStopPressedAt({ slotId: k, ts: 1 })],
  ['settleStopNotRunning', k => settleStopNotRunning({ slot: k })],
  ['syncSlotRunningFromServer', k => syncSlotRunningFromServer({ slot: k, running: false, stopping: false })],
  ['sseSubagentQueued', k => sseSubagentQueued({ slot: k, queued: 2 } as never)],
  ['sseSubagentPending slot', k => sseSubagentPending({ slot: k, id: 'a1', task: 't', approval_id: 'p' })],
  ['markSubagentApproving', k => markSubagentApproving({ id: k, approving: true })],
  ['sseSubagentSpawn slot', k => sseSubagentSpawn({ slot: k, id: 'a1', task: 't', agent: 'x' })],
  ['sseSubagentTool', k => sseSubagentTool({ slot: k, id: k, tool: 't' })],
  ['sseSubagentRetrying', k => sseSubagentRetrying({ slot: k, id: k })],
  ['sseSubagentStalled', k => sseSubagentStalled({ slot: k, id: k, stalled: true })],
  ['sseSubagentBatchUpdate', k => sseSubagentBatchUpdate({ updates: [{ id: k, slot: k, tool: 't' }] })],
  ['sseSubagentBatchChunks', k => sseSubagentBatchChunks({ chunks: [{ id: k, slot: k, text: 't' }] })],
  ['sseSubagentDone', k => sseSubagentDone({ slot: k, id: k, elapsed: 1 })],
  ['sseSubagentSnapshot', k => sseSubagentSnapshot({ id: k, slot: k, task: 't', agent: 'a', streaming: '', last_tool: '', started: 1 })],
  // The same frames with a real slot, so the id guard is reached rather than
  // the slot guard. Once on the active slot (top-level `subagents`) and once
  // on a background slot (`slotActivity[slot].subagents`).
  ...(['active-slot', 's1'] as const).flatMap((slot): Array<[string, (k: string) => UnknownAction]> => [
    [`sseSubagentPending id on ${slot}`, k => sseSubagentPending({ slot, id: k, task: 't', approval_id: 'p' })],
    [`sseSubagentSpawn id on ${slot}`, k => sseSubagentSpawn({ slot, id: k, task: 't', agent: 'x' })],
    [`sseSubagentTool id on ${slot}`, k => sseSubagentTool({ slot, id: k, tool: 't' })],
    [`sseSubagentRetrying id on ${slot}`, k => sseSubagentRetrying({ slot, id: k })],
    [`sseSubagentStalled id on ${slot}`, k => sseSubagentStalled({ slot, id: k, stalled: true })],
    [`sseSubagentBatchUpdate id on ${slot}`, k => sseSubagentBatchUpdate({ updates: [{ id: k, slot, tool: 't' }] })],
    [`sseSubagentBatchChunks id on ${slot}`, k => sseSubagentBatchChunks({ chunks: [{ id: k, slot, text: 't' }] })],
    [`sseSubagentDone id on ${slot}`, k => sseSubagentDone({ slot, id: k, elapsed: 1 })],
    [`sseSubagentSnapshot id on ${slot}`, k => sseSubagentSnapshot({ id: k, slot, task: 't', agent: 'a', streaming: '', last_tool: '', started: 1 })],
  ]),
  ['clearTerminalSubagents', k => clearTerminalSubagents({ slot: k })],
  ['setAutomations', k => setAutomations({ records: [automation(k)], legacyComplete: true, structuredComplete: true })],
  ['sseAutomation', k => sseAutomation(automation(k))],
  ['removeAutomation', k => removeAutomation(k)],
  ['sseSideResult', k => sseSideResult({ slot: k, run_id: 'r', role: 'user', content: 'q' })],
  ['sseSideQueue', k => sseSideQueue({ slot: k, action: 'push', queue_id: 'q1', content: 'c' })],
  ['sideOptimisticAppend', k => sideOptimisticAppend({ slot: k, message: { role: 'user', content: 'q', ts: 't' } })],
  ['sideClose', k => sideClose(k)],
  ['sseWorkflowEvent', k => sseWorkflowEvent({ run_id: k, type: 'run_started', data: { name: 'n' } })],
  ['reconcileWorkflowRuns', k => reconcileWorkflowRuns([{ run_id: k, status: 'running', name: 'n' } as unknown as WorkflowRunSummary])],
  ['sseToolActivity', k => sseToolActivity({ slot: k, tool: 't', kind: 'k', purpose: 'p', input_preview: 'i' })],
  ['sseActivityEvent', k => sseActivityEvent({ slot: k, kind: 'approval', text: 't', approval_id: 'a' })],
  ['sseToolResult', k => sseToolResult({ slot: k, output: 'o', tool_call_id: 'tc' })],
  ['sseMcpAppRender tool id', k => sseMcpAppRender({ tool_call_id: k, session_key: 's' } as unknown as McpAppRenderPayload)],
  ['sseMcpAppRender session', k => sseMcpAppRender({ tool_call_id: 'tc', session_key: k } as unknown as McpAppRenderPayload)],
]

/** Every place in the state tree a hostile key landed: an own property named
 *  after it, or (for `__proto__`, whose assignment is not an own key) an object
 *  whose prototype was swapped. Walks nested maps too, because per-slot state
 *  such as `slotActivity[slot].subagents` sits below the top level. */
function ownHostileEntries(state: Record<string, unknown>, k: string): string[] {
  const hits: string[] = []
  const seen = new Set<object>()
  const walk = (value: unknown, path: string) => {
    if (!value || typeof value !== 'object' || seen.has(value)) return
    seen.add(value)
    const proto = Object.getPrototypeOf(value)
    const expected = Array.isArray(value) ? Array.prototype : Object.prototype
    if (proto !== expected && proto !== null) hits.push(`${path} (prototype)`)
    if (Object.prototype.hasOwnProperty.call(value, k)) hits.push(`${path}.${k}`)
    for (const [key, child] of Object.entries(value)) walk(child, `${path}.${key}`)
  }
  walk(state, 'state')
  return hits
}

describe('hostile keys never reach Object.prototype', () => {
  const protoNames = Object.getOwnPropertyNames(Object.prototype).sort()
  const base = reducer(reducer(undefined, { type: '@@INIT' }), { type: 'chat/setActiveSlot', payload: 'active-slot' })

  for (const k of HOSTILE) {
    for (const [label, frame] of FRAMES) {
      it(`${label} with ${k}`, () => {
        const next = reducer(base, frame(k)) as unknown as Record<string, unknown>
        expect(Object.getOwnPropertyNames(Object.prototype).sort()).toEqual(protoNames)
        expect(({} as Record<string, unknown>).polluted).toBeUndefined()
        expect(ownHostileEntries(next, k)).toEqual([])
      })
    }
  }
})
