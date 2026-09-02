import type { ChatMessage } from '../../types'
import type { DisplayItem, TurnItem } from './types'
import { isSubagentCompletionMessage } from './subagentCompletion'
import { isNoteRow } from '../../lib/noteContract'

/** Roles that fold into a collapsible group in the turn view. Thinking is NOT
 *  here: it carries real content and renders as its own standalone block (a
 *  content-bearing reasoning trace), so grouping it into the "N tool calls"
 *  collapsible would bury and mislabel it. */
export const GROUPABLE = new Set(['permission'])

/**
 * The reasoning roles, and what "content-bearing reasoning" means. These are
 * THE single definition of the classification, shared by every display-layer
 * site that acts on it:
 *
 *  - the wrap gate below (`contentThinkingCount` via `isReasoningBurst`, which
 *    decides when a batch is routed into a {kind:'turn'} wrapper),
 *  - the per-turn fold that gate feeds (`mergeTurnThinking` in TurnBlock.tsx,
 *    which folds a turn's bursts into one hoisted row),
 *  - ChatPage's `renderMessage` (content-bearing → ThinkingBlock, empty
 *    placeholder → nothing, via `hasReasoningContent` + `isReasoningRole`),
 *  - the shared-transcript registry entry (`transcriptRenderers.tsx`, whose
 *    `roles` key and render guard both derive from here).
 *
 * These sites used to keep hand-written copies of the same condition; any
 * future refinement (a new reasoning role, a whitespace guard, a meta flag)
 * must happen HERE so the wrap threshold, the fold, and the row renderers can
 * never drift apart — that drift is exactly how the duplicate
 * "Thought process" rows of #6376 would regrow. (The store's burst-lifecycle
 * mechanics in chatSlice are a different concern — they manage streaming
 * placeholders, not display classification — and deliberately stay separate.)
 */
export const REASONING_ROLES = ['thinking'] as const

const REASONING_ROLE_SET: ReadonlySet<string> = new Set(REASONING_ROLES)

/** Is this message a reasoning trace (regardless of whether it has content)?
 *  Structurally typed so raw-snapshot (wire-shape) surfaces can reuse it. */
export const isReasoningRole = (msg: { role: string }): boolean =>
  REASONING_ROLE_SET.has(msg.role)

/** A content-bearing reasoning message; empty placeholders render nothing and never count. */
export const hasReasoningContent = (msg: { role: string; content: string }): boolean =>
  isReasoningRole(msg) && !!msg.content

/** Item-level form of {@link hasReasoningContent} for TurnItem scans. */
export const isReasoningBurst = (t: TurnItem): t is Extract<TurnItem, { kind: 'single' }> =>
  t.kind === 'single' && hasReasoningContent(t.msg)

/**
 * Roles that OPEN a turn, and are therefore the rows a reader can be anchored to.
 *
 * `nudge` and `subagent` are machine-injected but they ARE the thing that started
 * the turn below them, so a reader looking for "what am I inside" needs them. This
 * set is exported because the pinned-prompt scan has to agree with the grouping
 * exactly: when the two lists were maintained by hand they drifted, and a role
 * that opened a turn without being pinnable made the pin scan walk past every one
 * of them — measured at a 61-display-row gap in a loop-driven session.
 */
export const TURN_OPENER_ROLES = new Set(['user', 'nudge', 'subagent'])

/**
 * The synthesis injection that closes a sub-agent fan-out.
 *
 * `_run_pending_synthesis` (chat_runner.py) appends this row before dispatching
 * the one turn that folds every sub-agent's result into a single answer, and
 * stamps `meta.injectKind = 'synthesis'` on it. Its presence is therefore proof
 * that everything the agent emitted since the user's prompt was INTERIM: the
 * per-completion summaries the synthesis turn is about to restate.
 *
 * Keyed on the meta, not on the prompt's text: the prose is a backend constant
 * that may be reworded, the meta key is a wire contract.
 */
const isSynthesisInjection = (msg: ChatMessage): boolean =>
  msg.role === 'inject' &&
  (msg.meta as Record<string, unknown> | undefined)?.injectKind === 'synthesis'

/**
 * An injected row that is NOT part of the fan-out, and whose presence therefore
 * disqualifies the region from being folded.
 *
 * The slot's queue is shared. While a wave is landing, the drain can deliver a
 * CRON notification (`injectKind: 'cron'`) — an unrelated prompt with its own
 * reply — or replay the USER'S OWN message verbatim when a turn emitted nothing
 * (`user_replay`, see `build_recovery_requeue`). A NOTE is the third case and it
 * carries no `injectKind` at all: notes are appended as `inject` with
 * `meta.noteSession` (`slot_buffers.py`, `chat_handlers.py`), so they are
 * recognised through the shared `isNoteRow` contract rather than by a kind — the
 * same predicate every other note consumer uses, and the reason a `cls`-only
 * check would not survive a reload.
 *
 * None of the three is restated by the synthesis turn, so folding them behind
 * the fan-out toggle would hide content on the promise that something below
 * repeats it, which nothing does.
 *
 * `recovery` is deliberately NOT here: a tool-stall recovery inside a fan-out is
 * a continuation of the very work being folded.
 */
const FOREIGN_INJECT_KINDS = new Set(['cron', 'user_replay'])
const isForeignInjection = (msg: ChatMessage): boolean => {
  if (msg.role !== 'inject') return false
  if (isNoteRow(msg)) return true
  const kind = (msg.meta as Record<string, unknown> | undefined)?.injectKind
  return typeof kind === 'string' && FOREIGN_INJECT_KINDS.has(kind)
}

export interface GroupedTurns {
  turns: DisplayItem[]
  /** Index into `turns` of the turn object produced by the TRAILING flush, or
   *  -1 when the trailing group did not collapse into a turn (it was spread as
   *  loose items instead, and so carries no `complete` flag). This is the only
   *  element whose `complete` value depends on whether the slot is still
   *  running. */
  trailingTurnIdx: number
}

/**
 * Collapse `turns[start..]` into ONE turn flagged `interim`.
 *
 * The region can hold both loose TurnItems (a batch too short to have been
 * wrapped) and already-wrapped turns (a completion opener splits the region
 * into several), so it is flattened back to a single item list. Flattening is
 * what makes the fold reliable: a two-item reply is spread loose by `flushTurn`
 * and would otherwise have no TurnBlock to fold it.
 *
 * Always `complete: true` — a region is only folded once the synthesis row that
 * terminates it exists, which by definition is after the interim work finished.
 * The trailing (still-running) turn is never part of a folded region.
 */
function foldInterimRegion(turns: DisplayItem[], start: number): void {
  if (start >= turns.length) return
  const region = turns.splice(start)
  const items: TurnItem[] = []
  for (const d of region) {
    if (d.kind === 'turn') items.push(...d.items)
    else items.push(d)
  }
  if (items.length === 0) return
  turns.push({ kind: 'turn', items, complete: true, interim: true })
}

/**
 * Group a slot's messages into transcript display items.
 *
 * Split out of ChatPage for two reasons. It is pure and O(N) over the whole
 * message list, so it must be memoized on `messages` ALONE — bundling the
 * `slotRunning` flag into the same memo re-ran this entire pass on every turn
 * start/stop just to flip one boolean, and the resulting new identity cascaded
 * into the display-index maps and the virtualizer. And it decides what the user
 * actually sees, which makes it worth testing directly rather than through a
 * 4,000-line component.
 *
 * The trailing turn is always flushed as `complete: true`; the caller applies
 * the running state in O(1) via `trailingTurnIdx`.
 */
export function groupDisplayItems(messages: ChatMessage[]): GroupedTurns {
  // Phase 1: build raw items (singles + groups)
  const raw: TurnItem[] = []
  let group: ChatMessage[] = [], groupStart = 0
  for (let i = 0; i < messages.length; i++) {
    // Permission messages handled by pinned ApprovalBar — skip entirely
    if (messages[i].role === 'permission') continue
    // A sub-agent completion the card cannot parse stays internal: the LLM sees
    // it, the user does not. One it CAN parse renders as a compact outcome row,
    // which is the only scrollback record that a wave's results arrived.
    if (messages[i].role === 'subagent' && !isSubagentCompletionMessage(messages[i])) continue
    if (GROUPABLE.has(messages[i].role)) {
      if (!group.length) groupStart = i
      group.push(messages[i])
    } else {
      if (group.length) { raw.push({ kind: 'group', msgs: group, startIdx: groupStart }); group = [] }
      raw.push({ kind: 'single', msg: messages[i], idx: i })
    }
  }
  if (group.length) raw.push({ kind: 'group', msgs: group, startIdx: groupStart })

  // Phase 2: group into turns (user message → next user message).
  const turns: DisplayItem[] = []
  let turnItems: TurnItem[] = []
  // First index in `turns` after the last USER/nudge prompt — the start of the
  // region a synthesis injection retroactively folds as interim. A sub-agent
  // completion deliberately does NOT reset it: the completions, and the replies
  // the agent writes to each of them, ARE the interim work being folded.
  let regionStart = 0
  // Set when the open region also carries an injected row that the synthesis
  // turn will not restate (see isForeignInjection). Such a region is left
  // UNFOLDED — degrading to the pre-fold rendering is always safe, whereas
  // folding it would hide unrelated content behind the fan-out's toggle.
  let regionHasForeign = false
  const hasWorkingSteps = (items: TurnItem[]) =>
    items.some(t =>
      (t.kind === 'single' && (t.msg.role === 'tool' || t.msg.role === 'assistant' || t.msg.role === 'streaming')) ||
      t.kind === 'group'
    )
  // A batch that carries TWO OR MORE content-bearing reasoning bursts must be
  // wrapped as a {kind:'turn'}, even when it has no tool/assistant "working
  // steps" and even when it is short. The per-turn reasoning-burst dedup
  // (mergeTurnThinking in TurnBlock) — which folds a turn's many `thinking`
  // bursts into ONE row hoisted above the answer — runs ONLY on {kind:'turn'}
  // items. Left as loose singles (the else branch), each burst renders as its
  // own standalone "Thought process" row via ChatPage's renderMessage,
  // bypassing the dedup entirely: the duplicate-row wall of #6376. This bites a
  // reasoning-only trailing turn (a monitor/nudge cycle that has only emitted
  // reasoning so far) and any turn whose reasoning bursts land as a short/
  // answerless batch — and it became common because finer-grained models (e.g.
  // claude-opus-5) emit many small bursts per turn. The threshold is TWO: a
  // single burst renders as exactly one row whether loose or wrapped (nothing
  // to dedup), so wrapping it would only re-home it needlessly. `thinking` is
  // deliberately NOT counted in hasWorkingSteps (it is a reasoning trace, not a
  // working step that gates the "Worked through N steps" collapse), so this is
  // a separate predicate. Empty placeholder bursts do not count (they render
  // nothing, and mergeTurnThinking ignores them too).
  const contentThinkingCount = (items: TurnItem[]) =>
    items.reduce((n, t) => n + (isReasoningBurst(t) ? 1 : 0), 0)
  const flushTurn = (items: TurnItem[], complete: boolean) => {
    if ((hasWorkingSteps(items) && items.length > 2) || contentThinkingCount(items) >= 2) {
      turns.push({ kind: 'turn', items, complete })
    } else {
      turns.push(...items)
    }
  }
  for (const item of raw) {
    // The synthesis injection closes a fan-out: flush what is open, then fold
    // everything since the user's prompt into ONE interim turn. Checked BEFORE
    // the opener test because an `inject` row is not an opener — without this it
    // would be swallowed into the region it is supposed to terminate, and the
    // synthesis answer would fold away with the summaries it replaces.
    if (item.kind === 'single' && isSynthesisInjection(item.msg)) {
      if (turnItems.length > 0) { flushTurn(turnItems, true); turnItems = [] }
      if (!regionHasForeign) foldInterimRegion(turns, regionStart)
      // The synthesis row leads the turn that carries the answer, so it opens
      // the next batch rather than joining the region behind it.
      turnItems.push(item)
      regionStart = turns.length
      // ...and that batch will contain the synthesis ANSWER, which is a real
      // answer no LATER synthesis restates. A synthesis turn can itself spawn a
      // wave (the gateway re-arms `_pending_synthesis` whenever a wave's last
      // agent finishes, whichever turn spawned it), putting a second synthesis
      // row in the same user turn — and the batch opened here does not reach
      // `turns` until that second row flushes it, so an unguarded fold would
      // collapse round one's answer behind round two's toggle. Marking the new
      // region foreign keeps it unfolded: a nested wave's interim work then
      // renders as it did before this change, which is the safe direction.
      regionHasForeign = true
      continue
    }
    if (item.kind === 'single' && isForeignInjection(item.msg)) regionHasForeign = true
    // A nudge opens a new turn exactly like a user message does — it IS the
    // turn's prompt. Without this it gets swallowed into the previous turn's
    // collapsed step group and the cycle chip disappears. A sub-agent
    // completion is the same case: the gateway injects it as the next turn's
    // input, so the agent's reply belongs BELOW the card, not beside it.
    if (item.kind === 'single' && TURN_OPENER_ROLES.has(item.msg.role)) {
      if (turnItems.length > 0) { flushTurn(turnItems, true); turnItems = [] }
      turns.push(item)
      // A user/nudge prompt begins a fresh interim region; a sub-agent
      // completion belongs to the one already open.
      if (item.msg.role !== 'subagent') { regionStart = turns.length; regionHasForeign = false }
      continue
    }
    turnItems.push(item)
  }
  // Flush the trailing group as complete, and remember whether that flush
  // actually produced a turn object (flushTurn spreads the items instead when
  // the turn is too short to collapse). Only that element carries a `complete`
  // flag for the running state to affect.
  let trailingTurnIdx = -1
  if (turnItems.length > 0) {
    const before = turns.length
    flushTurn(turnItems, true)
    const last = turns[turns.length - 1]
    if (turns.length === before + 1 && last && last.kind === 'turn') {
      trailingTurnIdx = turns.length - 1
    }
  }
  return { turns, trailingTurnIdx }
}

/** Two turn items describe the same rows: same kind, same underlying message
 *  REFERENCES, same transcript indices. Reference equality on `msg` is the
 *  load-bearing check — the store replaces a message object whenever its
 *  content changes, so an unchanged reference means the row's input is
 *  byte-identical. */
// PURITY INVARIANT for the reconcile below: every field of a DisplayItem/
// TurnItem must be a pure function of (its message references, their indices,
// and `complete`). The equality helpers compare exactly those inputs, so a
// future field derived from anything else will be silently frozen by the
// substitution — such a field must be added to these comparisons.
const sameTurnItem = (a: TurnItem, b: TurnItem): boolean => {
  if (a.kind === 'single') return b.kind === 'single' && a.msg === b.msg && a.idx === b.idx
  if (b.kind !== 'group') return false
  if (a.startIdx !== b.startIdx || a.msgs.length !== b.msgs.length) return false
  for (let i = 0; i < a.msgs.length; i++) if (a.msgs[i] !== b.msgs[i]) return false
  return true
}

const sameDisplayItem = (a: DisplayItem, b: DisplayItem): boolean => {
  if (a.kind === 'turn') {
    if (b.kind !== 'turn') return false
    if (a.complete !== b.complete || a.items.length !== b.items.length) return false
    // Part of the PURITY INVARIANT above: `interim` is derived from the message
    // list (the presence of a synthesis row), so it must be compared or the
    // substitution below would freeze a turn at a stale fold state.
    if (!!a.interim !== !!b.interim) return false
    for (let i = 0; i < a.items.length; i++) if (!sameTurnItem(a.items[i], b.items[i])) return false
    return true
  }
  if (b.kind === 'turn') return false
  return sameTurnItem(a, b)
}

/**
 * Identity-preserving wrapper around {@link groupDisplayItems}.
 *
 * The grouping is memoized on `messages` alone, but every streaming rAF flush
 * replaces the messages array, so the memo re-runs and mints FRESH turn objects
 * for every turn in the transcript — handing each mounted TurnBlock a new
 * `turn` prop per flush and defeating all downstream memoization
 * (memo(TurnBlock), mergeTurnThinking's [turn.items] memo, the disclosure
 * machinery). This factory reconciles each fresh result against the previous
 * one and substitutes the PRIOR object wherever the new element describes the
 * same underlying message references — so a flush that only grew the trailing
 * message returns the identical settled-turn objects and only the trailing
 * turn carries a new identity.
 *
 * Cache shape and why it cannot leak: the closure holds exactly ONE
 * (messages, result) pair — the last call's — and both slots are overwritten
 * on every call, so the previous messages array is released as soon as the
 * next one arrives. Each caller creates its own grouper (one per mounted
 * ChatPage via useMemo), so two transcripts never thrash a shared slot and the
 * whole cache dies with the component. `applyRunningState` stays downstream
 * and untouched: it already applies the running flag in O(1) on top of
 * whatever this returns.
 */
export function createTurnGrouper(): (messages: ChatMessage[]) => GroupedTurns {
  let prevMessages: ChatMessage[] | null = null
  let prevResult: GroupedTurns | null = null
  return (messages: ChatMessage[]): GroupedTurns => {
    if (prevMessages === messages && prevResult) return prevResult
    const next = groupDisplayItems(messages)
    if (prevResult) {
      const prevTurns = prevResult.turns
      let allReused = next.turns.length === prevTurns.length &&
        next.trailingTurnIdx === prevResult.trailingTurnIdx
      for (let i = 0; i < next.turns.length; i++) {
        const p = prevTurns[i]
        if (p && sameDisplayItem(next.turns[i], p)) next.turns[i] = p
        else allReused = false
      }
      // Every element (and the trailing index) survived: keep the previous
      // top-level object too, so the [groupedTurns] memos downstream also hit.
      if (allReused) {
        prevMessages = messages
        return prevResult
      }
    }
    prevMessages = messages
    prevResult = next
    return next
  }
}

/**
 * Apply the slot's running state to the grouped output. O(1): when the slot is
 * still running the trailing turn is not complete yet, so exactly one element is
 * replaced and every other item keeps its identity.
 */
export function applyRunningState(grouped: GroupedTurns, slotRunning: boolean): DisplayItem[] {
  const { turns, trailingTurnIdx } = grouped
  if (trailingTurnIdx < 0 || !slotRunning) return turns
  const out = turns.slice()
  const t = out[trailingTurnIdx]
  if (t && t.kind === 'turn') out[trailingTurnIdx] = { ...t, complete: false }
  return out
}
