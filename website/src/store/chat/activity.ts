/** The activity panel and the live tool log: which tab a slot shows and
 *  whether its panel is open (persisted per slot), the inline tool-focus
 *  signal, and the tool-call / approval / tool-result frames that fill the
 *  log. */
import type { PayloadAction } from '@reduxjs/toolkit'
import type { ChatMessage, ToolActivity } from '../../types'
import { permissionRowTarget, sameApprovalTarget, type ApprovalTarget } from '../../types/approvalTarget'
import { SPAWN_LAUNCH_MARKER } from '../../pages/chat/types'
import { persistActivityOpen, type ChatState } from './state'
import { clampToolOutput, isUnsafeKey, safeKey } from './wire'

/** Write a clamped payload onto a tool-log entry as its `input`/`input_cut` or
 *  `output`/`output_cut` pair. An unclamped payload CLEARS a stale `*_cut`: an
 *  `is_update` frame can replace an oversize `input_preview` with a short one,
 *  and a leftover offset would make the renderer split the new text. */
function setClampedField(entry: ToolActivity, field: 'input' | 'output', payload: string): void {
  const { text, cut } = clampToolOutput(payload)
  const cutField = field === 'input' ? 'input_cut' : 'output_cut'
  entry[field] = text
  if (cut) entry[cutField] = cut
  else delete entry[cutField]
}

/** Load a slot's cached activity-panel state (or the empty defaults) into the
 *  live view. Shared by `switchSlot.pending` (entering the target) and
 *  `switchSlot.rejected` (falling back to the origin when the target is gone,
 *  #6309), so the two entry paths cannot drift apart. */
export function loadSlotActivity(state: ChatState, key: string): void {
  const cached = state.slotActivity[key]
  state.toolLog = cached?.toolLog ?? []
  state.subagents = cached?.subagents ?? {}
  // Inline expansion replaced the old 'tools' tab, and 'files' is no
  // longer one of this viewer's tabs (the file browser is its own pinned
  // panel now, and this viewer hosts 'links' instead). Any of those
  // legacy cached values fall back to 'changes'.
  const legacyTab = (t: unknown) => t === 'tools' || t === 'nav' || t === 'files'
  state.activityTab = (cached?.activityTab && !legacyTab(cached.activityTab)) ? cached.activityTab : 'changes'
  // Panel open/closed is per-chat; a chat we've never opened defaults to closed.
  state.activityOpen = cached?.activityOpen ?? false
}

/**
 * Attach a tool result's output to the tool MESSAGE meta for every message
 * carrying `tid`, in both the live list and the per-slot cache.
 *
 * All matching messages, not just the newest: an auto-approved tool produces
 * TWO tool messages sharing one tool_call_id (🔧 pre-approval + ✅
 * post-approval) and the server patches both, so stopping at the first would
 * leave the pair disagreeing about the same call.
 */
function applyToolOutputToMessages(
  state: ChatState,
  slot: string,
  tid: string,
  output: string,
): void {
  if (isUnsafeKey(slot)) return
  const patch = (msgs: ChatMessage[] | undefined): void => {
    if (!Array.isArray(msgs)) return
    for (const m of msgs) {
      if (m.role !== 'tool') continue
      const meta = m.meta as Record<string, unknown> | undefined
      if (!meta || meta.tool_call_id !== tid) continue
      m.meta = { ...meta, output }
    }
  }
  if (slot === state.activeSlot) patch(state.messages)
  // The cache can hold the SAME array reference as state.messages (switchSlot
  // caches by reference), so this may be a second pass over one list — the
  // patch is idempotent, and skipping it would strand a genuinely separate
  // cached copy with no output. `safeKey` mirrors hydrateSlotMessages: the
  // early return above already rejects unsafe keys, this is the codebase's
  // defense-in-depth companion.
  patch(state.slotMessages[safeKey(slot)])
}

export const activityReducers = {
  toggleActivity(state: ChatState) { state.activityOpen = !state.activityOpen; if (!state.activityOpen) state.focusToolCallId = null; persistActivityOpen(state.activeSlot, state.activityOpen) },
  openActivityPanel(state: ChatState) { state.activityOpen = true; persistActivityOpen(state.activeSlot, true) },
  openActivityToTab(state: ChatState, action: PayloadAction<'changes' | 'issues' | 'subagents' | 'workflows' | 'logs' | 'links' | 'side' | 'artifacts'>) { state.activityOpen = true; state.activityTab = action.payload; state.activityTabRequest += 1; state.focusToolCallId = null; persistActivityOpen(state.activeSlot, true) },
  /** Tool details expand inline in the chat. This action signals the matching
   *  ToolCallLine pill to auto-expand and scroll into view. */
  openActivityToTool(state: ChatState, action: PayloadAction<string>) { state.focusToolCallId = action.payload },
  /** Clear after the matching pill has consumed the focus signal, so the same trigger
   *  doesn't re-fire on subsequent re-renders. */
  clearFocusToolCallId(state: ChatState) { state.focusToolCallId = null },
  sseToolActivity(state: ChatState, action: PayloadAction<{ slot: string; tool: string; kind: string; purpose: string; input_preview: string; auto?: boolean; tool_call_id?: string; is_update?: boolean; is_shell?: boolean; tool_name?: string; mcp_server?: string }>) {
    if (isUnsafeKey(action.payload.slot)) return
    const log = action.payload.slot !== state.activeSlot
      ? (state.slotActivity[safeKey(action.payload.slot)] ??= { toolLog: [], subagents: {} }).toolLog
      : state.toolLog
    // claude-agent-acp emits an initial tool_call with empty rawInput followed
    // by tool_call_update notifications carrying the populated payload. The
    // backend sets is_update:true on the second-phase event so we merge into
    // the existing entry by tool_call_id. We gate strictly on is_update to
    // avoid silently merging a replayed initial event (e.g. WebSocket
    // reconnect) into an unrelated tool with a colliding id.
    const tcid = action.payload.tool_call_id
    if (tcid && action.payload.is_update) {
      const existing = log.findLast(e => e.type === 'tool' && e.tool_call_id === tcid)
      if (existing) {
        if (action.payload.tool) existing.text = action.payload.tool
        if (action.payload.purpose) existing.purpose = action.payload.purpose
        if (action.payload.input_preview) setClampedField(existing, 'input', action.payload.input_preview)
        if (action.payload.kind) existing.kind = action.payload.kind
        if (action.payload.is_shell !== undefined) existing.is_shell = action.payload.is_shell
        if (action.payload.tool_name) existing.tool_name = action.payload.tool_name
        if (action.payload.mcp_server) existing.mcp_server = action.payload.mcp_server
        // Update ts for recency sorting but NEVER overwrite executionStartedAt
        // — the elapsed timer must reflect real wall time since the tool began.
        existing.ts = Date.now()
        return
      }
    }
    // `input` is fed by the server's `input_preview`, which `_redact_tool_field`
    // caps at the same 1 MB as a result, so it takes the same clamp.
    const entry: ToolActivity = { type: 'tool', text: action.payload.tool, purpose: action.payload.purpose, kind: action.payload.kind, ts: Date.now(), auto: action.payload.auto, tool_call_id: action.payload.tool_call_id, is_shell: action.payload.is_shell, tool_name: action.payload.tool_name, mcp_server: action.payload.mcp_server }
    setClampedField(entry, 'input', action.payload.input_preview)
    log.push(entry)
    if (log.length > 100) log.splice(0, log.length - 100)
  },
  sseActivityEvent(state: ChatState, action: PayloadAction<{ slot: string; kind: string; text: string; approval_id?: string; approval_type?: string; approval_target?: ApprovalTarget }>) {
    if (isUnsafeKey(action.payload.slot)) return
    const log = action.payload.slot !== state.activeSlot
      ? (state.slotActivity[safeKey(action.payload.slot)] ??= { toolLog: [], subagents: {} }).toolLog
      : state.toolLog
    if (action.payload.kind === 'approval_resolved') {
      const id = action.payload.approval_id
      // With a target, only the request it names: the id recurs, and a chat
      // runner's row can share it.
      const target = action.payload.approval_target
      const entry = log.find(e => e.type === 'approval' && (target
        ? sameApprovalTarget(e.approval_target, target)
        : e.approval_id === id))
      if (entry) entry.type = 'approval_resolved'
      // Resolve against the OWNING slot's message array — active slot uses
      // state.messages, a background slot its slotMessages entry. Reading only
      // state.messages would miss a background-slot approval, so its tool
      // timer would never get the post-approval anchor and would inflate by
      // the whole approval wait after switching back to that slot.
      const msgs = action.payload.slot !== state.activeSlot
        ? (state.slotMessages[safeKey(action.payload.slot)] ?? [])
        : state.messages
      const msg = msgs.findLast(m => m.role === 'permission' && (target
        ? sameApprovalTarget(permissionRowTarget(m.meta, action.payload.slot), target)
        : (m.meta as Record<string,unknown>)?.approval_id === id))
      if (msg && !(msg.meta as Record<string,unknown>).resolved) (msg.meta as Record<string,unknown>).resolved = 'approved'
      // Stamp execution_started_at on the EXACT tool entry linked to this
      // approval via the permission message's tool_call_id. This persists in
      // Redux and survives component remounts, preventing the elapsed timer
      // from inflating by the approval wait time.
      const tcid = (msg?.meta as Record<string, unknown>)?.tool_call_id as string | undefined
      if (tcid) {
        const toolEntry = log.findLast(e => e.type === 'tool' && e.tool_call_id === tcid)
        if (toolEntry && !toolEntry.execution_started_at) toolEntry.execution_started_at = Date.now()
      }
      return
    }
    const entry: ToolActivity = { type: action.payload.kind, text: action.payload.text, ts: Date.now() }
    if (action.payload.approval_id) entry.approval_id = action.payload.approval_id
    if (action.payload.approval_type) entry.approval_type = action.payload.approval_type
    if (action.payload.approval_target) entry.approval_target = action.payload.approval_target
    log.push(entry)
  },
  sseToolResult(state: ChatState, action: PayloadAction<{ slot: string; output: string; tool_call_id?: string }>) {
    const tid = action.payload.tool_call_id
    // Land the output on the tool MESSAGE's meta as well as the tool log, for
    // the one consumer that reads scrollback rather than the tool log: the
    // inline SubagentRunCard detects a spawn_run launch by parsing
    // "Spawned N subagent(s)." out of `meta.output`. Without this the card
    // sees nothing until the slot is refetched, since `chatSlotDetail` would
    // be the only source carrying this field — a reload-only artifact. Mirrors
    // the server, which writes the same redacted string to the same field
    // (chat_runner.py EVENT_TOOL_RESULT), so live and reloaded state agree.
    //
    // Restricted to launch results on purpose. `state.messages` has no entry
    // cap and a single output can reach the server's 1 MB cap, so copying
    // EVERY tool result here would let one long autonomous turn grow the
    // heap without bound. The tool log below is bounded on both axes: 100
    // entries, each clamped by `clampToolOutput`.
    //
    // Runs BEFORE the tool-log lookup below, which returns early for a slot
    // that has no toolLog yet — a background slot's scrollback still needs
    // the output.
    //
    // Only with an explicit tool_call_id: the id-less fallback below is safe
    // for the tool log (positional, single-writer) but would attach output
    // to an arbitrary tool bubble in scrollback. The server applies the same
    // condition (`if _tcid:`), so skipping is parity, not a gap.
    if (tid && action.payload.output.includes(SPAWN_LAUNCH_MARKER)) {
      applyToolOutputToMessages(state, action.payload.slot, tid, action.payload.output)
    }
    const log = action.payload.slot !== state.activeSlot
      ? state.slotActivity[action.payload.slot]?.toolLog
      : state.toolLog
    if (!log) return
    // Prefer an exact tool_call_id match when a tid is supplied. Only if no
    // entry carries that id do we fall back to the most-recent id-less tool
    // entry. A single-pass `... || !log[i].tool_call_id` clause would let a
    // supplied tid latch onto an unrelated id-less tool sitting later in the
    // log, attaching the output to the wrong tool bubble.
    let target = -1
    if (tid) {
      for (let i = log.length - 1; i >= 0; i--) {
        if (log[i].type === 'tool' && log[i].tool_call_id === tid) { target = i; break }
      }
    }
    if (target === -1) {
      for (let i = log.length - 1; i >= 0; i--) {
        if (log[i].type === 'tool' && (!tid || !log[i].tool_call_id)) { target = i; break }
      }
    }
    if (target >= 0) setClampedField(log[target], 'output', action.payload.output)
  },
}
