import type { ChatSlot, SubagentActivity } from '../../../types'
import { slotApprovalMode } from '../../../utils/slotApprovalMode'
import type { ApprovalModeKey } from '../../../components/ApprovalModePicker'
import { i18nT } from '../../../i18n/t'
import { fmtNumber } from '../../../i18n/format'
import { slotChannelNamespace } from '../../../utils/channelOrigin'
import { deriveToolCallTitle, parseToolArgs } from '../../../utils/toolCallTitle'

export type RunState = 'running' | 'idle' | 'done' | 'blocked' | 'waiting' | 'needs_input' | 'stopped'
export const APPROVAL_MODE_KEYS: Record<ApprovalModeKey, string> = {
  normal: 'components.approvalModePicker.normal_label', trust_reads: 'components.approvalModePicker.reads_label',
  trust: 'components.approvalModePicker.trust_label', yolo: 'components.approvalModePicker.yolo_label',
}
/** The chat header's own mode rule, so the two never disagree: a live
 * app-armed scoped grant auto-approves too, and reads as trust. */
export function effectiveApprovalMode(globalMode: string, slot?: ChatSlot): ApprovalModeKey {
  return slotApprovalMode(globalMode, slot)
}
export interface RunNode {
  id: string
  kind: 'session' | 'subagent' | 'workflow'
  ref: string
  slot: string
  title: string
  ordinal: number
  state: RunState
  detail?: string
  error?: string
}
const RUN_LABEL_KEYS = { session: 'commandCenter.session_label', subagent: 'commandCenter.worker_label', workflow: 'commandCenter.workflow_label' } as const
/** Opaque routing identity is not a user-facing name. Translate at render time. */
export function runTitle(node: RunNode): string {
  return node.title || i18nT(RUN_LABEL_KEYS[node.kind], { number: fmtNumber(node.ordinal) })
}
export interface PendingQuestion {
  slot: string
  ask_id?: string
  card_id?: string
  native?: boolean
  questions: { question: string; header?: string; options: { label: string; description?: string }[]; multiSelect?: boolean }[]
}
export interface PendingApproval {
  id: string
  /** Minted per request by the coordinator; echoed so a stale card cannot resolve a reused id. */
  instance?: string
  request_mid?: string
  slot?: string
  source?: string
  tool?: string
  tool_input?: unknown
  tool_purpose?: string
}
export interface AttentionItem {
  id: string
  slot: string
  kind: 'question' | 'approval' | 'open_session'
  question?: PendingQuestion
  approval?: PendingApproval
  approvalMode?: ApprovalModeKey
  native?: boolean
}
/** The command an approval asks about, as the transcript would title it. Empty
 * when the inventory carried no tool input to title. */
export function approvalTitle(approval: PendingApproval): string {
  const input = approval.tool_input
  return deriveToolCallTitle({
    toolName: approval.tool, title: approval.tool || '',
    rawInput: parseToolArgs(input) ?? (typeof input === 'string' ? { command: input } : input),
  }).title
}
export interface WorkItem {
  item_id: string
  title: string
  state: string
  status?: string
  summary?: string
  worker_session_key?: string
}
export interface CommandCenterSources {
  approvalMode?: string
  root: string | null
  slots: ChatSlot[]
  subagents: Record<string, Record<string, SubagentActivity>>
  workflows: { run_id: string; name?: string; session_key?: string; status?: string; error?: string | null; last_log?: string }[]
  questions: PendingQuestion[]
  approvals: PendingApproval[]
  work?: { items: WorkItem[]; omitted?: number }
}

/** Session keys from the workflow/ledger APIs are scope-qualified; slot keys aren't. */
export function slotKey(key: string): string {
  if (key.startsWith('dashboard:')) return key.slice('dashboard:'.length)
  // Match artifacts._strip_session_scope + history._safe_key. Keep
  // known channel namespaces and Python's Unicode letters/numbers; never fold
  // an arbitrary colon-qualified identity into another session's slot.
  return slotChannelNamespace(key) ? key.replace(/[^\p{L}\p{N}_.-]/gu, '_') : key
}

/** Only durable creator edges establish ownership. Missing roots never mean "all". */
/** The slot *key* and every slot it was created under, nearest first: the roots
 * whose `scopedSlots` team contains it. A cycle in `created_by` stops the walk. */
export function teamRoots(slots: ChatSlot[], key: string): string[] {
  const byKey = new Map(slots.map(s => [s.key, s]))
  const roots: string[] = []
  for (let current = slotKey(key); current && !roots.includes(current); ) {
    roots.push(current)
    const creator = byKey.get(current)?.created_by
    current = creator ? slotKey(creator) : ''
  }
  return roots
}

export function scopedSlots(slots: ChatSlot[], root: string | null): ChatSlot[] {
  if (root === null) return slots
  if (!slots.some(s => s.key === root)) return []
  const keys = new Set([root])
  let changed = true
  while (changed) {
    changed = false
    for (const s of slots) {
      if (!keys.has(s.key) && s.created_by && keys.has(slotKey(s.created_by))) {
        keys.add(s.key)
        changed = true
      }
    }
  }
  return slots.filter(s => keys.has(s.key))
}

export function buildCommandCenter(source: CommandCenterSources) {
  const slots = scopedSlots(source.slots, source.root)
  const keys = new Set(slots.map(s => s.key))
  const attention: AttentionItem[] = []
  const seen = new Set<string>()
  const addAttention = (item: AttentionItem) => {
    if (!seen.has(item.id)) { seen.add(item.id); attention.push(item) }
  }
  for (const q of source.questions) {
    const slot = slotKey(q.slot)
    if (keys.has(slot)) addAttention({ id: `question:${slot}:${q.ask_id || q.card_id || slot}`, slot, kind: 'question', question: q })
  }
  for (const s of slots) {
    const approval = s.pending_approval_info
    if (s.pending_approval && approval?.request_id) {
      if (approval.origin === 'native' && approval.request_mid) {
        // Registry IDs can collide. Native provenance never comes from the
        // absence of a coordinator record, and each origin keeps its own card.
        addAttention({ id: `native-approval:${s.key}:${approval.request_id}:${approval.request_mid}`, slot: s.key, kind: 'approval', native: true,
          approvalMode: effectiveApprovalMode(source.approvalMode || 'normal', s),
          approval: { id: approval.request_id, request_mid: approval.request_mid, slot: s.key, tool: approval.tool, tool_input: approval.tool_input, tool_purpose: approval.tool_purpose } })
      } else if (approval.origin !== 'coordinator' || !source.approvals.some(a => a.id === approval.request_id && slotKey(a.slot || '') === s.key)) {
        // Only a proven coordinator mirror is deduplicated by the inventory
        // loop below, which retains its exact raw routing slot. Otherwise the
        // host session is the only safe action, including legacy snapshots.
        addAttention({ id: `session-approval:${s.key}`, slot: s.key, kind: 'approval',
          approvalMode: effectiveApprovalMode(source.approvalMode || 'normal', s) })
      }
    }
  }
  for (const approval of source.approvals) {
    const slot = slotKey(approval.slot || '')
    // The coordinator mints `instance` because a caller's approval id can
    // recur. Both render sites key the card by this id, so a replacement
    // request must not reconcile onto a card whose decision already landed.
    if (keys.has(slot)) addAttention({ id: `approval:${slot}:${approval.id}:${approval.instance || ''}`, slot, kind: 'approval', native: false, approval,
      approvalMode: effectiveApprovalMode(source.approvalMode || 'normal', slots.find(s => s.key === slot)) })
  }
  for (const s of slots) {
    if (s.pending_approval && !attention.some(a => a.slot === s.key && a.kind === 'approval')) {
      addAttention({ id: `session-approval:${s.key}`, slot: s.key, kind: 'approval',
        approvalMode: effectiveApprovalMode(source.approvalMode || 'normal', s) })
    }
    if (s.needs_input && !attention.some(a => a.slot === s.key)) {
      addAttention({ id: `session-input:${s.key}`, slot: s.key, kind: 'open_session' })
    }
  }
  // A turn can be idle while delegated or orchestrated work is active, or while
  // a queued message waits to run; those slot flags keep the session running.
  const nodes: RunNode[] = slots.map((s, index) => ({
    id: `session:${s.key}`, kind: 'session', ref: s.key, slot: s.key, title: s.title && s.title !== s.key ? s.title : '', ordinal: index + 1,
    state: s.needs_input || s.pending_approval ? 'needs_input' : s.running || s.subagents_running || s.orchestrating || (s.queue_depth ?? 0) > 0 ? 'running' : 'idle',
    detail: s.todo?.current || undefined,
  }))
  const agentIds = new Set<string>()
  for (const s of slots) {
    for (const a of Object.values(source.subagents[s.key] || {})) {
      if (agentIds.has(a.id)) continue
      agentIds.add(a.id)
      nodes.push({ id: `subagent:${a.id}`, kind: 'subagent', ref: a.id, slot: s.key, title: a.task !== a.id ? a.task : '', ordinal: nodes.length + 1,
        state: a.status === 'done' ? 'done' : a.status === 'stopped' ? 'stopped' : a.status === 'error' || a.stalled ? 'blocked' : a.approval_id ? 'needs_input' : a.status === 'pending' ? 'waiting' : 'running',
        detail: a.lastTool || undefined, error: a.error || undefined })
    }
  }
  for (const w of source.workflows) {
    const slot = slotKey(w.session_key || '')
    if (!keys.has(slot)) continue
    // A paused workflow is resumable, so its label is "waiting" rather than
    // stopped; planning is active work even before execution starts.
    nodes.push({ id: `workflow:${w.run_id}`, kind: 'workflow', ref: w.run_id, slot, title: w.name && w.name !== w.run_id ? w.name : '', ordinal: nodes.length + 1,
      state: w.status === 'finished' ? 'done' : w.status === 'failed' ? 'blocked' : w.status === 'cancelled' ? 'stopped'
        : w.status === 'paused' || w.status === 'pausing' ? 'waiting'
          : w.status === 'running' || w.status === 'planning' || w.status === 'planned' ? 'running' : 'idle',
      detail: w.last_log, error: w.error || undefined })
  }
  const workItems = (source.work?.items || []).map(w => ({ ...w,
    state: (w.state === 'accepted' ? 'done' : w.state === 'rejected' || w.state === 'abandoned' ? 'stopped' : w.status === 'blocked' ? 'blocked' : w.status === 'question' ? 'waiting' : 'running') as RunState,
  }))
  const todo = slots.find(s => s.key === source.root)?.todo
  // A worker's "done" report isn't the conductor's acceptance. Omitted ledger
  // entries make a denominator unknowable; show counts without a percentage.
  const progress = workItems.length && !source.work?.omitted
    ? { done: workItems.filter(w => w.state === 'done').length, total: workItems.length, source: 'work' as const }
    : !workItems.length && todo?.tasks.length
      ? { done: todo.tasks.filter(t => t.completed).length, total: todo.tasks.length, source: 'todo' as const }
      : null
  const RESTING: RunState[] = ['idle', 'done', 'stopped']
  // A paused workflow waits on nobody and no tile counts or lists it, so it
  // rests; a pending subagent or a work item's open question also reads
  // `waiting`, but those are still the task's own unfinished work.
  const atRest = (node: { kind?: RunNode['kind']; state: RunState }) => RESTING.includes(node.state) || (node.kind === 'workflow' && node.state === 'waiting')
  return { nodes, attention, workItems, progress,
    running: nodes.filter(n => n.state === 'running').length,
    blocked: nodes.filter(n => n.state === 'blocked').length + workItems.filter(w => w.state === 'blocked').length,
    // Nothing runs, waits or asks, and every counted item rests. An idle session
    // with an open plan is NOT settled: the plan is the thing the dock still has
    // to show, so the plan is tested on its own whether or not a work board
    // supplies the progress number. A board that omitted entries is not settled
    // either: the items it did not return may be the open ones. The board needs
    // no done-over-total test: `done` counts acceptances, so a rejected or
    // abandoned item rests without ever counting, and `workItems.every` covers it.
    settled: attention.length === 0 && !source.work?.omitted && nodes.every(atRest)
      && workItems.every(atRest) && (!todo?.tasks.length || todo.tasks.every(t => t.completed)),
  }
}

export type CommandCenterModel = ReturnType<typeof buildCommandCenter>
