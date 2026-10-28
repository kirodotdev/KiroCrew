/**
 * Pure presentation rules for a KAS-native workflow run.
 *
 * Every decision about WHICH control a run offers is made server-side
 * (`wire.run_controls`) and arrives as `row.controls`; this module only maps
 * that decision onto buttons and badges, so the dashboard can never offer an
 * action the engine would refuse. Kept free of React so the mapping is tested
 * on its own (`kasModel.test.ts`).
 */
import type {
  KasApproval,
  KasApprovalOption,
  KasNodeAction,
  KasQuestion,
  KasRunCapability,
  KasRunDisplay,
  KasRunNode,
  KasRunRow,
  KasStepEntry,
} from '../../../api/client'
import type { ChatMessage } from '../../../types'
import { i18nT } from '../../../i18n/t'

/** True when the gateway's trusted tool identity (`meta.mcp_server`, from the
 *  harness's own `_meta` channel, never the title) says an MCP server served
 *  this tool call — so it is never one of Kiro Agent's own workflow tools,
 *  however its title or output reads (gap 26). */
export function servedByMcp(message: Pick<ChatMessage, 'meta'>): boolean {
  const server = message.meta?.mcp_server
  return typeof server === 'string' && server.length > 0
}

/** Badge for a run's display state. Stopped is grey (`muted`), not red: a
 *  person stopped it, nothing failed. */
export interface KasDisplayBadge {
  variant: 'ok' | 'err' | 'warn' | 'aim' | 'muted'
  label: string
  /** Still in flight: draw the spinner. */
  active: boolean
}

export function displayBadge(display: KasRunDisplay | string | null | undefined): KasDisplayBadge {
  switch (display) {
    case 'running':
      return { variant: 'aim', label: i18nT('apps.workflows.kas.state_running'), active: true }
    case 'pausing':
      return { variant: 'warn', label: i18nT('apps.workflows.kas.state_pausing'), active: true }
    case 'paused':
      return { variant: 'warn', label: i18nT('apps.workflows.kas.state_paused'), active: false }
    case 'completed':
      return { variant: 'ok', label: i18nT('apps.workflows.kas.state_completed'), active: false }
    case 'failed':
      return { variant: 'err', label: i18nT('apps.workflows.kas.state_failed'), active: false }
    case 'stopped':
      return { variant: 'muted', label: i18nT('apps.workflows.kas.state_stopped'), active: false }
    default:
      return { variant: 'muted', label: display ? String(display) : i18nT('apps.workflows.kas.state_unknown'), active: false }
  }
}

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'aborted'])

/** A finished run: no controls but Delete. */
export function isTerminalRun(row: Pick<KasRunRow, 'status'>): boolean {
  return TERMINAL_STATUSES.has(row.status)
}

/** The one primary control a run offers, or `null` for none. `pausing` is a
 *  disabled placeholder so the Pause just pressed does not simply vanish. */
export type KasPrimaryControl =
  | { kind: 'pause' }
  | { kind: 'resume' }
  | { kind: 'open' }
  | { kind: 'pausing' }
  | null

/** A run no live Kiro Agent process drives cannot be paused, resumed or
 *  stopped: the server refuses until it is reconnected, so only Reconnect and
 *  Open chat are offered. */
function undriven(row: { driven?: boolean }): boolean {
  return row.driven === false
}

/** Whether `run.actions` (KAS's own rules) allows `cap`. `controls` picks the
 *  slot a control sits in, but a row that lists its actions must also list the
 *  control: an exhausted loop is paused yet takes no plain Resume (it would
 *  park again at once), so only Grant / End the loop are offered there. */
function allows(row: { actions?: readonly KasRunCapability[] }, cap: KasRunCapability): boolean {
  return !row.actions || row.actions.includes(cap)
}

export function primaryControl(
  row: Pick<KasRunRow, 'status' | 'controls'> & { driven?: boolean; actions?: readonly KasRunCapability[] },
): KasPrimaryControl {
  if (isTerminalRun(row) || undriven(row)) return null
  const { primary, display } = row.controls
  if (primary === 'pause') return allows(row, 'pause') ? { kind: 'pause' } : null
  if (primary === 'resume') return allows(row, 'resume') ? { kind: 'resume' } : null
  if (primary === 'open') return { kind: 'open' }
  if (display === 'pausing') return { kind: 'pausing' }
  return null
}

/** Stop is separate from the primary slot and only on a run still going. */
export function canStop(
  row: Pick<KasRunRow, 'status' | 'controls'> & { driven?: boolean; actions?: readonly KasRunCapability[] },
): boolean {
  return !isTerminalRun(row) && !undriven(row) && row.controls.canStop && allows(row, 'cancel')
}

/** Delete only ever on a finished run, whatever `controls` claims. */
export function canDelete(row: Pick<KasRunRow, 'status' | 'controls'> & { actions?: readonly KasRunCapability[] }): boolean {
  return isTerminalRun(row) && row.controls.canDelete && allows(row, 'delete')
}

/** Reconnect re-attaches a run no live Kiro Agent process drives any more
 *  (after a KAS restart). Needs the chat it reports into, since the route is
 *  per chat. */
export function showReconnect(row: Pick<KasRunRow, 'status' | 'driven' | 'slotKey'>): boolean {
  return !isTerminalRun(row) && row.driven === false && !!row.slotKey
}

/** Kiro Agent's OWN step counts (G1) when it reports them — "known" is
 *  `stepsTotal !== null` — else today's own `completedSteps` / `totalSteps`.
 *  The two counts differ: ours includes loop passes, KAS's is its own tally. */
export function effectiveSteps(
  row: Pick<KasRunRow, 'stepsCompleted' | 'stepsTotal' | 'completedSteps' | 'totalSteps'>,
): { done: number | null; total: number | null } {
  if (row.stepsTotal != null) return { done: row.stepsCompleted ?? 0, total: row.stepsTotal }
  return { done: row.completedSteps, total: row.totalSteps }
}

/** "Step n/m", or null when the run carries no progress (a finished run). */
export function stepProgress(
  row: Pick<KasRunRow, 'stepsCompleted' | 'stepsTotal' | 'completedSteps' | 'totalSteps'>,
): { done: number; total: number } | null {
  const { done, total } = effectiveSteps(row)
  if (done == null || total == null || total <= 0) return null
  return { done, total }
}

/** The approvals waiting on one run. */
export function approvalsFor(approvals: readonly KasApproval[], workflowId: string): KasApproval[] {
  return approvals.filter(a => a.workflowId === workflowId)
}

/** Reject options render destructive; the first allow option is the primary. */
export function isRejectOption(option: Pick<KasApprovalOption, 'kind'>): boolean {
  return option.kind === 'reject_once' || option.kind === 'reject_always'
}

/** One line of a plan approval's step list. */
export interface PlanStepLine {
  key: string
  label: string
  depth: number
}

const PLAN_CHILD_KEYS = ['children', 'steps', 'branches'] as const
const MAX_PLAN_LINES = 200
const MAX_PLAN_DEPTH = 32

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

/**
 * Flatten a plan approval's `steps` (KAS `WorkflowPlanNode[]`) into indented
 * lines. KAS nests sequence/repeat steps under `steps` and parallel ones under
 * `branches`; descriptors use `children`, so all three are walked. Bounded in
 * depth and length so a hostile payload cannot hang the card.
 */
export function planStepLines(plan: Record<string, unknown> | null | undefined): PlanStepLine[] {
  const out: PlanStepLine[] = []
  const root = plan ? plan.steps : null
  const walk = (nodes: unknown, depth: number, path: string) => {
    if (!Array.isArray(nodes) || depth > MAX_PLAN_DEPTH) return
    nodes.forEach((raw, index) => {
      if (out.length >= MAX_PLAN_LINES) return
      const node = asRecord(raw)
      if (!node) return
      const id = str(node.id) ?? String(index)
      const label = str(node.label) ?? str(node.name) ?? str(node.agent) ?? str(node.prompt) ?? id
      const key = `${path}/${id}`
      out.push({ key, label, depth })
      for (const childKey of PLAN_CHILD_KEYS) walk(node[childKey], depth + 1, key)
    })
  }
  walk(root, 0, '')
  return out
}

/**
 * Stable run order for every live list (gap 22): live runs first, newest
 * CREATED first, then finished runs newest ENDED first. Ordering by created/
 * ended — not `updatedAt` — is what keeps a row from jumping to the top on
 * every step event, so "pausing, resuming or waiting does not move a run".
 */
export function orderRuns(rows: readonly KasRunRow[]): KasRunRow[] {
  const by = (pick: (r: KasRunRow) => string) => (a: KasRunRow, b: KasRunRow) => {
    const x = pick(a)
    const y = pick(b)
    return x < y ? 1 : x > y ? -1 : 0
  }
  const live = rows.filter(r => !isTerminalRun(r)).sort(by(r => r.createdAt ?? ''))
  const done = rows.filter(r => isTerminalRun(r)).sort(by(r => r.endedAt ?? r.updatedAt ?? r.createdAt ?? ''))
  return [...live, ...done]
}

/** The live and finished runs of a list, each in {@link orderRuns} order. */
export function splitRuns(rows: readonly KasRunRow[]): { live: KasRunRow[]; done: KasRunRow[] } {
  return {
    live: orderRuns(rows).filter(r => !isTerminalRun(r)),
    done: orderRuns(rows).filter(r => isTerminalRun(r)),
  }
}

/** A run's heading: its own name, else its workflow's, else its id. */
export function runTitle(row: Pick<KasRunRow, 'name' | 'workflowName' | 'workflowId'>): string {
  return row.name || row.workflowName || row.workflowId
}

// ── IDE-style run card / rail helpers ──

/** How many top-level labels the card's meta line names before `…`. */
export const PLAN_SUMMARY_LABELS = 4

/** The card's `{n}-step workflow · A → B → C` parts, from the TOP-LEVEL nodes,
 *  or null for a run with no plan yet. */
export function planSummary(row: Pick<KasRunRow, 'nodes'>): { count: number; labels: string[]; more: boolean } | null {
  const count = row.nodes.length
  if (count === 0) return null
  const labels = row.nodes.slice(0, PLAN_SUMMARY_LABELS).map(n => n.label || n.id)
  return { count, labels, more: count > PLAN_SUMMARY_LABELS }
}

/** Depth-first search of a run's step tree. Bounded so a hostile tree cannot hang it. */
export function findNode(nodes: readonly KasRunNode[], id: string): KasRunNode | null {
  let seen = 0
  const walk = (list: readonly KasRunNode[], depth: number): KasRunNode | null => {
    if (depth > MAX_PLAN_DEPTH) return null
    for (const node of list) {
      if (++seen > 5000) return null
      if (node.id === id) return node
      const hit = walk(node.children, depth + 1)
      if (hit) return hit
    }
    return null
  }
  return walk(nodes, 0)
}

/**
 * Depth-first search by runtime `path`, descending into a repeat's realised
 * `iterations` as well as its definition `children`, so a loop-body instance is
 * reachable by the path that names its iteration. Bounded like {@link findNode}.
 */
export function findNodeByPath(nodes: readonly KasRunNode[], path: string): KasRunNode | null {
  let seen = 0
  const walk = (list: readonly KasRunNode[], depth: number): KasRunNode | null => {
    if (depth > MAX_PLAN_DEPTH) return null
    for (const node of list) {
      if (++seen > 5000) return null
      if (node.path === path) return node
      const inChildren = walk(node.children, depth + 1)
      if (inChildren) return inChildren
      if (node.iterations) {
        for (const iter of node.iterations) {
          const inIter = walk(iter.children, depth + 1)
          if (inIter) return inIter
        }
      }
    }
    return null
  }
  return walk(nodes, 0)
}
export function leafSteps(nodes: readonly KasRunNode[]): KasRunNode[] {
  const out: KasRunNode[] = []
  const walk = (list: readonly KasRunNode[], depth: number) => {
    if (depth > MAX_PLAN_DEPTH) return
    for (const node of list) {
      if (out.length >= 5000) return
      if (node.type === 'step') out.push(node)
      walk(node.children, depth + 1)
    }
  }
  walk(nodes, 0)
  return out
}

/** The card's one live status line. */
export type KasLiveLine =
  | { kind: 'blocking'; text: string }
  | { kind: 'active'; text: string; agent: string | null }
  | { kind: 'reason'; text: string }
  | null

export function liveStatusLine(
  row: Pick<
    KasRunRow,
    'blocking' | 'activeSteps' | 'nodes' | 'pauseReason' | 'controls' | 'status' | 'pause' | 'stepsTotal' | 'currentNodeId' | 'waitingOnWatch'
  >,
): KasLiveLine {
  if (row.blocking) {
    const step = stepDisplayLabel(row.nodes, row.blocking.nodeId, row.blocking.label)
    return {
      kind: 'blocking',
      text:
        row.blocking.kind === 'question'
          ? i18nT('apps.workflows.kas.blocking_question', { step })
          : i18nT('apps.workflows.kas.blocking_permission', { step }),
    }
  }
  if (!isTerminalRun(row)) {
    // G1: Kiro Agent reports a watch running with no step active or waiting.
    if (row.stepsTotal != null && row.waitingOnWatch && row.currentNodeId) {
      const node = findNode(row.nodes, row.currentNodeId)
      const watch = node ? stepDisplayLabel(row.nodes, node.id, node.label) : row.currentNodeId
      return { kind: 'active', text: i18nT('apps.workflows.kas.waiting_on_watch', { watch }), agent: null }
    }
    if (row.activeSteps.length > 0) {
      const first = row.activeSteps[0]
      // Prefer Kiro Agent's OWN current node when it names one (G1): its tally
      // and ours can point at different instances inside a loop.
      const current =
        row.stepsTotal != null && row.currentNodeId && !row.waitingOnWatch ? findNode(row.nodes, row.currentNodeId) : null
      const node = current ?? findNode(row.nodes, first.nodeId)
      return {
        kind: 'active',
        text: node ? stepDisplayLabel(row.nodes, node.id, node.label) : stepDisplayLabel(row.nodes, first.nodeId, first.label),
        agent: node?.agent ?? null,
      }
    }
  }
  // The structured pause (why KAS parked the run) wins over the free text.
  const parked = pauseLine(row)
  if (parked) return { kind: 'reason', text: parked.text }
  if (row.pauseReason && row.controls.display !== 'running') return { kind: 'reason', text: row.pauseReason }
  return null
}

/** "Step n/m" for a run still going; null when unknown or finished (never "0/0"). */
export function liveStepProgress(
  row: Pick<KasRunRow, 'status' | 'stepsCompleted' | 'stepsTotal' | 'completedSteps' | 'totalSteps'>,
): { done: number; total: number } | null {
  return isTerminalRun(row) ? null : stepProgress(row)
}

function parseIso(value: string | null | undefined): number | null {
  if (!value) return null
  const ms = Date.parse(value)
  return Number.isFinite(ms) ? ms : null
}

/** Elapsed run time: start (or creation) to end (or `now`); null when unknown. */
export function runElapsedMs(
  row: Pick<KasRunRow, 'startedAt' | 'createdAt' | 'endedAt'>,
  now: number,
): number | null {
  const start = parseIso(row.startedAt) ?? parseIso(row.createdAt)
  if (start == null) return null
  const end = parseIso(row.endedAt) ?? now
  return Math.max(0, end - start)
}

/** Elapsed between two ISO stamps (`end` absent = `now`); null when unknown. */
export function spanMs(start: string | null | undefined, end: string | null | undefined, now: number): number | null {
  const a = parseIso(start)
  if (a == null) return null
  return Math.max(0, (parseIso(end) ?? now) - a)
}

/** Needs a person: the warning icon replaces the state icon, with no spin. */
export function needsAttention(row: Pick<KasRunRow, 'controls' | 'blocking'>): boolean {
  return row.controls.needsAttention || !!row.blocking
}

/** The "Needs you" pill shown beside the title when a run needs a person AND
 *  its own badge does not already say so (gap 7). A running run reads "Needs
 *  you" in its badge; a paused non-user park (an exhausted loop, a transient
 *  error) keeps the "Paused" badge, so the separate pill is what flags it. */
export function showNeedsYouPill(row: Pick<KasRunRow, 'controls' | 'blocking' | 'status'>): boolean {
  if (isTerminalRun(row) || !needsAttention(row)) return false
  return row.controls.display !== 'running'
}

/** The status pill. A live run waiting on a person reads "Needs you" (as in the
 *  IDE), without its status changing; a paused run keeps its "Paused" badge and
 *  carries the separate needs-you pill instead ({@link showNeedsYouPill}). */
export function runBadge(row: Pick<KasRunRow, 'controls' | 'blocking' | 'status'>): KasDisplayBadge {
  if (!isTerminalRun(row) && needsAttention(row) && row.controls.display === 'running') {
    return { variant: 'warn', label: i18nT('apps.workflows.kas.state_needs_you'), active: false }
  }
  return displayBadge(row.controls.display)
}

/** How many finished runs the side panel lists beneath the live ones. */
export const RECENT_FINISHED_RUNS = 10

/** The panel's run list: every live run, then the newest `limit` finished ones. */
export function windowRuns(rows: readonly KasRunRow[], limit = RECENT_FINISHED_RUNS): KasRunRow[] {
  const { live, done } = splitRuns(rows)
  return [...live, ...done.slice(0, limit)]
}

const FINISHED_STEP = new Set(['completed', 'failed', 'aborted'])

/** A selectable step instance and where it lives in the run. */
export interface KasStepInstance {
  nodeId: string
  path: string
  status: string
  sessionId: string | null
}

/**
 * Every selectable STEP instance, in plan order. A `repeat` with realised
 * `iterations` contributes its body once per iteration (earliest first, so the
 * latest instance of a node is the LAST one with its id); outside a loop a step
 * contributes its single instance.
 */
export function stepInstances(nodes: readonly KasRunNode[]): KasStepInstance[] {
  const out: KasStepInstance[] = []
  const walk = (list: readonly KasRunNode[], depth: number) => {
    if (depth > MAX_PLAN_DEPTH) return
    for (const node of list) {
      if (out.length >= 5000) return
      if (node.type === 'step') {
        out.push({ nodeId: node.id, path: node.path, status: node.status, sessionId: node.sessionId })
        continue
      }
      if (node.type === 'repeat' && node.iterations && node.iterations.length > 0) {
        for (const iter of node.iterations) walk(iter.children, depth + 1)
      } else {
        walk(node.children, depth + 1)
      }
    }
  }
  walk(nodes, 0)
  return out
}

/**
 * The step a run's detail opens on, as a `{nodeId, path}` instance: the blocking
 * step, else the (first) active one — each resolved to its latest iteration's
 * instance — else the last finished step. Only a step with its own session can
 * show a chat, so the finished fallback skips those without one.
 */
export function defaultStep(
  row: Pick<KasRunRow, 'blocking' | 'activeSteps' | 'nodes'>,
): { nodeId: string; path: string } | null {
  const instances = stepInstances(row.nodes)
  // The latest instance matching this session (preferred — it names one exact
  // iteration), else the latest sharing the node id, else the node's own path.
  const resolve = (nodeId: string, sessionId: string | null): { nodeId: string; path: string } => {
    let bySession: KasStepInstance | null = null
    let byId: KasStepInstance | null = null
    for (const inst of instances) {
      if (sessionId && inst.sessionId === sessionId) bySession = inst
      if (inst.nodeId === nodeId) byId = inst
    }
    const hit = bySession ?? byId
    if (hit) return { nodeId: hit.nodeId, path: hit.path }
    const node = findNode(row.nodes, nodeId)
    return { nodeId, path: node?.path ?? nodeId }
  }
  if (row.blocking?.nodeId) return resolve(row.blocking.nodeId, row.blocking.sessionId)
  if (row.activeSteps.length > 0) return resolve(row.activeSteps[0].nodeId, row.activeSteps[0].sessionId)
  const finished = instances.filter(n => FINISHED_STEP.has(n.status) && !!n.sessionId)
  const last = finished[finished.length - 1]
  return last ? { nodeId: last.nodeId, path: last.path } : null
}

/**
 * Whether the selected step instance is parked for the person's next message,
 * so the step chat should offer a reply composer. KAS lists such a wait as a
 * `question` attention entry with NO `toolCallId` whose session is the step's,
 * or the instance's own status is `paused`. Never for a running or finished
 * step, nor for a terminal run.
 */
export function stepAwaitsReply(
  row: Pick<KasRunRow, 'attention' | 'status'>,
  node: Pick<KasRunNode, 'status' | 'sessionId' | 'actions'> | null | undefined,
): boolean {
  if (!node || isTerminalRun(row)) return false
  // A backend that derives per-node controls has decided: trust it alone.
  if (node.actions) return node.actions.includes('reply')
  if (FINISHED_STEP.has(node.status) || node.status === 'running') return false
  if (node.status === 'paused') return true
  const sessionId = node.sessionId
  if (!sessionId) return false
  return row.attention.some(a => a.kind === 'question' && a.toolCallId == null && a.sessionId === sessionId)
}

// ── Parity controls: run / node capabilities, pause reasons, includes, dock ──
//
// Every control below is shown only when the backend lists it in `run.actions`
// or `node.actions` (derived from KAS's own rules); an absent list offers none.

/** True when the run offers `cap` right now. */
export function runCan(row: Pick<KasRunRow, 'actions'>, cap: KasRunCapability): boolean {
  return !!row.actions?.includes(cap)
}

/** True when the node offers `action` right now. */
export function nodeCan(node: Pick<KasRunNode, 'actions'> | null | undefined, action: KasNodeAction): boolean {
  return !!node?.actions?.includes(action)
}

/** A loop iteration's segment in a runtime path (KAS `pathSegmentOf`: `iter-<n>`). */
const ITERATION_SEGMENT = /(^|\/)iter-\d+(\/|$)/

/**
 * Whether this one step can be retried on its own. `node.actions` carries no
 * `retry`, so this follows the rules the retry route applies: the run offers
 * `retry`, the step failed or was stopped (KAS retries both), and it is not
 * inside a loop iteration, since KAS names a step by id alone and the route
 * sends such a step to "Retry failed steps" instead.
 */
export function stepRetryable(row: Pick<KasRunRow, 'actions'>, node: Pick<KasRunNode, 'status' | 'path'>): boolean {
  if (!runCan(row, 'retry')) return false
  if (node.status !== 'failed' && node.status !== 'aborted') return false
  return !ITERATION_SEGMENT.test(node.path ?? '')
}

/** Default and bounds of the "Grant more passes" count. KAS caps a loop's total at 1000. */
export const GRANT_DEFAULT = 1
export const GRANT_MAX = 1000

/** A typed grant count, or null when it is not a whole number in 1..GRANT_MAX. */
export function parseGrantCount(raw: string | number): number | null {
  const text = typeof raw === 'number' ? String(raw) : raw.trim()
  if (!/^\d+$/.test(text)) return null
  const n = Number(text)
  return n >= 1 && n <= GRANT_MAX ? n : null
}

/** The loop a `loop_exhausted` pause names, with what it offers. */
export interface KasExhaustedLoop {
  nodeId: string
  path: string | null
  label: string
  done: number | null
  max: number | null
  canGrant: boolean
  canEnd: boolean
}

/** Search the tree for a node by path first (unique per instance), else by id. */
export function resolveNode(nodes: readonly KasRunNode[], nodeId: string | null | undefined, path: string | null | undefined): KasRunNode | null {
  if (path) {
    const hit = findNodeByPath(nodes, path)
    if (hit) return hit
  }
  return nodeId ? findNode(nodes, nodeId) : null
}

/** The exhausted loop of a run parked by `loop_exhausted`, else null. */
export function exhaustedLoop(row: Pick<KasRunRow, 'pause' | 'nodes' | 'status'>): KasExhaustedLoop | null {
  const pause = row.pause
  if (isTerminalRun(row) || !pause || pause.reason !== 'loop_exhausted' || !pause.nodeId) return null
  const node = resolveNode(row.nodes, pause.nodeId, pause.path)
  const counts = pause.iterations ?? node?.loop ?? null
  return {
    nodeId: pause.nodeId,
    path: pause.path ?? node?.path ?? null,
    label: node?.label || pause.nodeId,
    done: counts ? counts.done : null,
    max: counts ? counts.max : null,
    canGrant: nodeCan(node, 'grant'),
    canEnd: nodeCan(node, 'complete'),
  }
}

/** The one-line account of why a run is parked, or null when it is not. */
export interface KasPauseLine {
  reason: string
  text: string
  /** KAS's own detail (an error message), shown muted after the line. */
  detail: string | null
}

export function pauseLine(row: Pick<KasRunRow, 'pause' | 'nodes' | 'status'>): KasPauseLine | null {
  const pause = row.pause
  if (!pause || isTerminalRun(row)) return null
  const node = resolveNode(row.nodes, pause.nodeId, pause.path)
  const step = node?.label ? stepDisplayLabel(row.nodes, node.id, node.label) : pause.nodeId || ''
  const detail = pause.message && pause.message.trim() ? pause.message.trim() : null
  switch (pause.reason) {
    case 'transient_error':
      return { reason: pause.reason, text: i18nT('apps.workflows.kas.pause_transient'), detail }
    case 'retries_exhausted':
      return {
        reason: pause.reason,
        text: step ? i18nT('apps.workflows.kas.pause_retries_step', { step }) : i18nT('apps.workflows.kas.pause_retries'),
        detail,
      }
    case 'loop_exhausted': {
      const loop = exhaustedLoop(row)
      const name = loop?.label ?? step
      const max = loop?.max
      return {
        reason: pause.reason,
        text: max != null ? i18nT('apps.workflows.kas.pause_loop', { loop: name, max }) : i18nT('apps.workflows.kas.pause_loop_plain', { loop: name }),
        detail,
      }
    }
    case 'awaiting_input':
      return {
        reason: pause.reason,
        text: step ? i18nT('apps.workflows.kas.pause_awaiting_step', { step }) : i18nT('apps.workflows.kas.waiting_reply'),
        detail,
      }
    case 'plan_approval':
      return { reason: pause.reason, text: i18nT('apps.workflows.kas.pause_plan'), detail }
    case 'user':
      return { reason: pause.reason, text: i18nT('apps.workflows.kas.pause_user'), detail }
    default:
      return detail ? { reason: String(pause.reason), text: detail, detail: null } : null
  }
}

/** A KAS effort level's display name; '' is the caller's own "default" wording. */
export function effortLabel(level: string): string {
  switch (level) {
    case 'low':
      return i18nT('apps.workflows.kas.effort_low')
    case 'medium':
      return i18nT('apps.workflows.kas.effort_medium')
    case 'high':
      return i18nT('apps.workflows.kas.effort_high')
    case 'xhigh':
      return i18nT('apps.workflows.kas.effort_xhigh')
    case 'max':
      return i18nT('apps.workflows.kas.effort_max')
    default:
      return level
  }
}

/** The step's label inside an include group: its name without the `<group>/` prefix. */
export function includeMemberLabel(label: string, groupId: string | null | undefined): string {
  if (!groupId) return label
  const prefix = `${groupId}/`
  return label.length > prefix.length && label.startsWith(prefix) ? label.slice(prefix.length) : label
}

/** Acronyms kept all-caps when a launch input's raw name is humanized (gap: CX r8). */
const INPUT_LABEL_ACRONYMS = new Set(['URL', 'PR', 'ID', 'API', 'CI'])

/**
 * A readable label for a recipe input whose raw name is a code identifier
 * (`pr_url` → "PR URL", `workItemId` → "Work item ID"). Splits on `_`/`-` and
 * camelCase boundaries, sentence-cases the result, and keeps known acronyms
 * upper. Display only — the launch payload still carries the raw name. KAS's
 * recipe wire schema (`parse_recipe`) gives inputs only a name and a type, with
 * no description/title field to prefer, so the name is all there is to show.
 */
export function humanizeInputName(name: string): string {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .split(/[\s_-]+/)
    .filter(Boolean)
  if (words.length === 0) return name
  return words
    .map((word, i) => {
      const upper = word.toUpperCase()
      if (INPUT_LABEL_ACRONYMS.has(upper)) return upper
      const lower = word.toLowerCase()
      return i === 0 ? lower.charAt(0).toUpperCase() + lower.slice(1) : lower
    })
    .join(' ')
}

/** The id of the nearest included-workflow group above `nodeId`, if any. */
function includeGroupOf(nodes: readonly KasRunNode[], nodeId: string, group: string | null = null): string | null | undefined {
  for (const node of nodes) {
    const here = node.include ? node.id : group
    if (node.id === nodeId) return group
    const kids = [...node.children, ...(node.iterations ?? []).flatMap(it => it.children)]
    const hit = includeGroupOf(kids, nodeId, here)
    if (hit !== undefined) return hit
  }
  return undefined
}

/** A step's name as the tree shows it: inside an include group, without the `<group>/` prefix. */
export function stepDisplayLabel(nodes: readonly KasRunNode[], nodeId: string | null | undefined, label: string): string {
  if (!nodeId) return label
  return includeMemberLabel(label, includeGroupOf(nodes, nodeId) ?? null)
}

/** The open question-tool asks of one run. */
export function questionsFor(questions: readonly KasQuestion[] | undefined, workflowId: string): KasQuestion[] {
  return (questions ?? []).filter(q => q.workflowId === workflowId)
}

/** True when the answer to one question is filled enough to send. */
export function questionAnswered(value: string | string[] | undefined): boolean {
  if (Array.isArray(value)) return value.length > 0
  return typeof value === 'string' && value.trim().length > 0
}

/** The summary numbers of the chat's workflow dock (several live runs at once). */
export interface KasDockSummary {
  count: number
  needsYou: number
  stepsDone: number
  stepsTotal: number
  /** 0..100, from completed steps; 0 when no run declares a total. */
  progressPct: number
}

export function dockSummary(rows: readonly KasRunRow[], questions: readonly KasQuestion[] = []): KasDockSummary {
  let needsYou = 0
  let stepsDone = 0
  let stepsTotal = 0
  for (const row of rows) {
    const asked = questions.some(q => q.workflowId === row.workflowId)
    // One rule everywhere now: the backend folds non-user parks into
    // `controls.needsAttention` (gap 7), so rows, badges, sort and this count agree.
    if (needsAttention(row) || asked) needsYou++
    const { done, total } = effectiveSteps(row)
    stepsDone += done ?? 0
    stepsTotal += total ?? 0
  }
  return {
    count: rows.length,
    needsYou,
    stepsDone,
    stepsTotal,
    progressPct: stepsTotal > 0 ? Math.min(100, Math.round((stepsDone / stepsTotal) * 100)) : 0,
  }
}

/** The chat's live runs for its dock, the ones needing the person first. */
export function dockRuns(rows: readonly KasRunRow[]): KasRunRow[] {
  const live = orderRuns(rows).filter(r => !isTerminalRun(r))
  return [...live.filter(needsAttention), ...live.filter(r => !needsAttention(r))]
}

/**
 * A plan approval's structured `plan.nodes`, made safe to draw as a step tree:
 * every node gets an id, path, label, status and children (KAS may send a
 * plan before any of them has run state). Bounded like {@link planStepLines}.
 */
export function planTreeNodes(plan: { nodes?: unknown } | null | undefined): KasRunNode[] {
  let seen = 0
  const walk = (raw: unknown, depth: number, parent: string): KasRunNode[] => {
    if (!Array.isArray(raw) || depth > MAX_PLAN_DEPTH) return []
    const out: KasRunNode[] = []
    raw.forEach((item, index) => {
      if (++seen > MAX_PLAN_LINES) return
      const node = asRecord(item)
      if (!node) return
      const id = str(node.id) ?? String(index)
      const nodePath = str(node.path) ?? (parent ? `${parent}/${id}` : id)
      const include = asRecord(node.include)
      const recipe = include ? str(include.recipe) : null
      const children = PLAN_CHILD_KEYS.flatMap(key => walk(node[key], depth + 1, nodePath))
      out.push({
        id,
        path: nodePath,
        type: str(node.type) ?? (children.length > 0 ? 'sequence' : 'step'),
        agent: str(node.agent),
        label: str(node.label) ?? str(node.name) ?? str(node.agent) ?? id,
        maxIterations: typeof node.maxIterations === 'number' ? node.maxIterations : null,
        status: str(node.status) ?? 'pending',
        sessionId: null,
        iteration: null,
        reason: null,
        model: str(node.model),
        effort: str(node.effort),
        children,
        ...(include && recipe ? { include: { recipe, path: str(include.path) } } : {}),
      })
    })
    return out
  }
  return walk(plan?.nodes, 0, '')
}

// ── Round 4: plan-update outcome, per-step duration, watch outcome, waits ──

/** The localized line for the run's latest resolved plan change (gap 11), or
 *  null when none. "Plan updated" / "Plan update rejected: {reason}" / "Plan
 *  update dropped: the run ended first." */
export function planUpdateLine(row: Pick<KasRunRow, 'lastPlanUpdate'>): string | null {
  const update = row.lastPlanUpdate
  if (!update) return null
  switch (update.outcome) {
    case 'applied':
      return i18nT('apps.workflows.kas.plan_updated')
    case 'rejected': {
      const reason = update.reason && update.reason.trim() ? update.reason.trim() : null
      return reason
        ? i18nT('apps.workflows.kas.plan_update_rejected', { reason })
        : i18nT('apps.workflows.kas.plan_update_rejected_plain')
    }
    case 'dropped':
      return i18nT('apps.workflows.kas.plan_update_dropped')
    default:
      return null
  }
}

/** A plan-update outcome worth a warning accent (a rejection the user should see). */
export function planUpdateIsWarning(row: Pick<KasRunRow, 'lastPlanUpdate'>): boolean {
  return row.lastPlanUpdate?.outcome === 'rejected'
}

/** A step instance's duration in ms (gap 23): `startedAt`→`endedAt` (or `now`
 *  while it runs), or null when KAS carries no start stamp for this node. */
export function stepDurationMs(
  node: Pick<KasRunNode, 'startedAt' | 'endedAt' | 'status'>,
  now: number,
): number | null {
  return spanMs(node.startedAt, node.endedAt, now)
}

/** KAS's retry-wait backoff line for a step, or null (gap 1). */
export function retryWaitLine(node: Pick<KasRunNode, 'retryWait'>): string | null {
  const wait = node.retryWait
  if (!wait) return null
  const reason = wait.reason && wait.reason.trim() ? wait.reason.trim() : null
  return reason ? i18nT('apps.workflows.kas.retry_wait', { reason }) : i18nT('apps.workflows.kas.retry_wait_plain')
}

/** A watch node's last-poll outcome as a localized line, or null (gap 23). */
export function watchOutcomeLabel(node: Pick<KasRunNode, 'type' | 'watchOutcome'>): string | null {
  if (node.type !== 'watch' || !node.watchOutcome) return null
  switch (node.watchOutcome.outcome) {
    case 'new-activity':
      return i18nT('apps.workflows.kas.watch_new_activity')
    case 'terminal-state':
      return i18nT('apps.workflows.kas.watch_terminal')
    case 'idle':
    default:
      return i18nT('apps.workflows.kas.watch_idle')
  }
}

/** A step row's "Needs approval" / "Waiting for you" tag from `waitingFor` (gap 23). */
export function nodeWaitLabel(node: Pick<KasRunNode, 'waitingFor'>): string | null {
  if (node.waitingFor === 'approval') return i18nT('apps.workflows.kas.needs_approval')
  if (node.waitingFor === 'input') return i18nT('apps.workflows.kas.waiting_for_you')
  return null
}

/** A collapsed group's one-line summary of what it contains (gap 23). */
export function groupSummary(node: Pick<KasRunNode, 'children' | 'iterations' | 'type'>): string | null {
  const kids =
    node.type === 'repeat' && node.iterations && node.iterations.length > 0
      ? node.iterations.flatMap(it => it.children)
      : node.children
  const steps = kids.filter(k => k.type === 'step')
  if (steps.length === 0) return null
  const needInput = steps.filter(s => s.waitingFor === 'input' || s.waitingFor === 'approval').length
  if (needInput > 0) return i18nT('apps.workflows.kas.group_summary_input', { count: needInput })
  const failed = steps.filter(s => s.status === 'failed').length
  if (failed > 0) return i18nT('apps.workflows.kas.group_summary_failed', { count: failed })
  if (steps.every(s => s.status === 'completed')) return i18nT('apps.workflows.kas.group_summary_completed')
  return i18nT('apps.workflows.kas.group_summary_steps', { count: steps.length })
}


// ── Round 7: one step tally, loop body + iteration pager, parent notes, reply folds ──

/**
 * The run's SINGLE step tally — leaf step executions — so the card's
 * "{n}-step workflow", the header's "{done}/{total} steps" and the "Step n/m"
 * line all read the same number (gap: card said "4-step workflow" beside
 * "Step 1/5"). Kiro Agent's own count when it reports one (it includes loop
 * passes), else the leaf nodes of the plan.
 */
export function runStepTotal(
  row: Pick<KasRunRow, 'stepsCompleted' | 'stepsTotal' | 'completedSteps' | 'totalSteps' | 'nodes'>,
): number | null {
  const { total } = effectiveSteps(row)
  if (total != null && total > 0) return total
  const leaves = leafSteps(row.nodes).length
  return leaves > 0 ? leaves : null
}

/** How many passes a repeat node has run, for its `↻ N` row; null when unknown.
 *  Realised iterations when KAS lists them, else its live counter, else the
 *  current 1-based iteration index. */
export function loopIterationCount(node: Pick<KasRunNode, 'iterations' | 'loop' | 'iteration'>): number | null {
  if (node.iterations && node.iterations.length > 0) return node.iterations.length
  if (node.loop) return node.loop.done
  if (node.iteration != null) return node.iteration
  return null
}

/** The loop body a repeat renders ONCE: the latest realised iteration's nodes
 *  (real paths / status), else the definition children before any ran. */
export function loopBodyChildren(node: KasRunNode): readonly KasRunNode[] {
  if (node.type === 'repeat' && node.iterations && node.iterations.length > 0) {
    return node.iterations[node.iterations.length - 1].children
  }
  return node.children
}

/** Every realised instance of a loop-body step (by node id), in iteration order,
 *  for the selected row's `‹ n/m ›` pager. Empty when the step has no instances. */
export function stepIterationInstances(repeat: KasRunNode, nodeId: string): KasRunNode[] {
  if (repeat.type !== 'repeat' || !repeat.iterations) return []
  const out: KasRunNode[] = []
  for (const iter of repeat.iterations) {
    const hit = iter.children.find(child => child.id === nodeId)
    if (hit) out.push(hit)
  }
  return out
}

/** Leaf steps inside an included workflow, for its "included workflow · N steps" row. */
export function includeStepCount(node: Pick<KasRunNode, 'children'>): number {
  return leafSteps(node.children).length
}

/** A step turn's `send_message` is Kiro Agent's step-completion signal AND the
 *  step's note to the run's parent chat; the step pane draws it as its own card. */
const PARENT_MESSAGE_RE = /(^|[._/@])send_message$/i

export function isParentMessageTool(entry: KasStepEntry): entry is Extract<KasStepEntry, { kind: 'tool' }> {
  return entry.kind === 'tool' && PARENT_MESSAGE_RE.test(entry.name)
}

export interface ParentMessage {
  text: string
  /** KAS's completion severity, when the call carried one: success / warning / error. */
  severity: 'success' | 'warning' | 'error' | null
}

function asSeverity(value: unknown): ParentMessage['severity'] {
  return value === 'success' || value === 'warning' || value === 'error' ? value : null
}

/** The message and severity of a step's `send_message` note, from its tool input
 *  (KAS serialises the call's args as JSON when no command/path is present). */
export function parseParentMessage(entry: Extract<KasStepEntry, { kind: 'tool' }>): ParentMessage {
  const raw = entry.input
  if (raw && raw.trimStart().startsWith('{')) {
    try {
      const obj = JSON.parse(raw) as Record<string, unknown>
      const text = [obj.message, obj.text, obj.summary, obj.body].find(v => typeof v === 'string' && v) as
        | string
        | undefined
      if (text) return { text, severity: asSeverity(obj.severity ?? obj.level) }
    } catch {
      /* not JSON: fall through to the raw input */
    }
  }
  return { text: raw || entry.title || entry.name, severity: null }
}

/** The first step instance parked for the person's INPUT (its `waitingFor`),
 *  descending into loop iterations; null when nothing waits on input. The run
 *  card uses it to name the real question and offer a Reply. */
export function inputWaitNode(nodes: readonly KasRunNode[]): KasRunNode | null {
  let seen = 0
  const walk = (list: readonly KasRunNode[], depth: number): KasRunNode | null => {
    if (depth > MAX_PLAN_DEPTH) return null
    for (const node of list) {
      if (++seen > 5000) return null
      if (node.type === 'step' && node.waitingFor === 'input') return node
      const inChildren = walk(node.children, depth + 1)
      if (inChildren) return inChildren
      if (node.iterations) {
        for (const iter of node.iterations) {
          const inIter = walk(iter.children, depth + 1)
          if (inIter) return inIter
        }
      }
    }
    return null
  }
  return walk(nodes, 0)
}

/** The step's latest `send_message` note, newest-last scan — the text a parked
 *  step showed when it asked for input, so a run card can name the real question. */
export function lastStepNote(entries: readonly KasStepEntry[]): ParentMessage | null {
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i]
    if (isParentMessageTool(entry)) return parseParentMessage(entry)
  }
  return null
}

/** A chip summarising the reasoning + tool rows between two replies. */
export interface StepChip {
  thoughts: number
  tools: number
  entries: KasStepEntry[]
  running: boolean
}

/**
 * One rendered unit of the step transcript, folded PER REPLY (the IDE's run
 * view, replacing the per-turn fold):
 *
 * - `prompt` — a user prompt, never folded.
 * - `parent` — a `send_message` note to the parent chat, its own card.
 * - `reply` — a plain assistant reply, preceded by a `chip` of the reasoning
 *   and tool rows since the previous reply (null when there were none).
 * - `work` — trailing reasoning / tools after the last reply (or before a
 *   prompt / parent card), with no reply of their own.
 */
export type StepReplyBlock =
  | { kind: 'prompt'; entry: Extract<KasStepEntry, { kind: 'user' }> }
  | { kind: 'parent'; entry: Extract<KasStepEntry, { kind: 'tool' }> }
  | { kind: 'reply'; chip: StepChip | null; reply: Extract<KasStepEntry, { kind: 'assistant' }>; ts: string | null }
  | { kind: 'work'; chip: StepChip }

function stepChip(entries: KasStepEntry[]): StepChip {
  return {
    thoughts: entries.filter(e => e.kind === 'assistant' && e.reasoning).length,
    tools: entries.filter(e => e.kind === 'tool').length,
    entries,
    running: entries.some(e => e.kind === 'tool' && (e.status === 'running' || e.status === 'in_progress')),
  }
}

export function foldReplies(entries: readonly KasStepEntry[]): StepReplyBlock[] {
  const out: StepReplyBlock[] = []
  let work: KasStepEntry[] = []
  const flush = () => {
    if (work.length) out.push({ kind: 'work', chip: stepChip(work) })
    work = []
  }
  for (const entry of entries) {
    if (entry.kind === 'user') {
      flush()
      out.push({ kind: 'prompt', entry })
    } else if (isParentMessageTool(entry)) {
      flush()
      out.push({ kind: 'parent', entry })
    } else if (entry.kind === 'assistant' && !entry.reasoning) {
      out.push({ kind: 'reply', chip: work.length ? stepChip(work) : null, reply: entry, ts: entry.ts })
      work = []
    } else {
      work.push(entry)
    }
  }
  flush()
  return out
}
