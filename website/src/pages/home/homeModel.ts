/**
 * Home's numbers, as pure functions over the sources the page reads.
 *
 * The fleet (sessions, subagents, workflows, what needs a person) is the command
 * center's own model (`buildCommandCenter`, fleet scope), so Home never recounts
 * it; what lives here is the grouping Home adds on top, plus the schedule and
 * roster summaries no other surface computes.
 */
import type { AttentionItem, RunNode, RunState } from '../chat/command-center/model'
import type { ChatSlot, CronJob } from '../../types'
import type { MemberRosterRow } from '../../api/client/agents'

/**
 * What needs a person, split by weight. A BLOCKER is a question card, a tool
 * approval or a session the gateway marks needs-input: the session cannot move
 * until someone answers. A SUGGESTION is an idle session whose newest reply ended
 * in `[OPTIONS:]`: the agent offered next steps and nothing is stuck. The two
 * get different room on the page, and only blockers count on the tile.
 */
export interface AttentionSplit {
  blockers: AttentionItem[]
  suggestions: AttentionItem[]
  questions: number
  approvals: number
}

export function splitAttention(items: readonly AttentionItem[]): AttentionSplit {
  const suggestions = items.filter(i => i.kind === 'question' && i.question?.followUp)
  const blockers = items.filter(i => !suggestions.includes(i))
  return {
    blockers,
    suggestions,
    questions: blockers.filter(i => i.kind === 'question' || i.kind === 'open_session').length,
    approvals: blockers.filter(i => i.kind === 'approval').length,
  }
}

/** The slot facts a fleet row shows beside the model's own: when the current run
 *  started and what the agent last said. */
export interface FleetRowExtras { runningSinceMs?: number; lastMessage?: string; subagentsRunning: boolean }

/** `last_turn_ts` moves when a prompt arrives or a turn ends, so on a running
 *  session it is the moment the current run began. */
export function fleetRowExtras(slot: ChatSlot | undefined): FleetRowExtras {
  const since = slot?.running && slot.last_turn_ts ? Date.parse(slot.last_turn_ts) : Number.NaN
  return {
    runningSinceMs: Number.isNaN(since) ? undefined : since,
    lastMessage: slot?.last_message?.trim() || undefined,
    subagentsRunning: !!slot?.subagents_running,
  }
}

/** Running work by kind: the tile's number is `total`, its tip the breakdown. */
interface ActiveCounts { sessions: number; subagents: number; workflows: number; total: number }

export function activeCounts(nodes: readonly RunNode[]): ActiveCounts {
  const running = nodes.filter(n => n.state === 'running')
  const by = (kind: RunNode['kind']) => running.filter(n => n.kind === kind).length
  return { sessions: by('session'), subagents: by('subagent'), workflows: by('workflow'), total: running.length }
}

/** Reading order for the fleet list: what a person must act on first, then what is
 *  blocked, then what is working; the rest is at rest. Ties keep the model's own
 *  order (sessions before their workers, in session order). */
const STATE_RANK: Record<RunState, number> = { needs_input: 0, blocked: 1, running: 2, waiting: 3, idle: 4, done: 5, stopped: 6 }
const RESTING: ReadonlySet<RunState> = new Set(['idle', 'done', 'stopped'])

/** The command center's own rule: a paused workflow waits on nobody, so it rests. */
function isResting(node: RunNode): boolean {
  return RESTING.has(node.state) || (node.kind === 'workflow' && node.state === 'waiting')
}

/** Nodes in reading order, split into the ones worth a row and the ones at rest.
 *
 *  A session with an item in the inbox (`attentionSlots`) reads `needs_input`
 *  whatever the slot flags say: an approval the inventory holds does not set the
 *  slot's own `pending_approval`, and a row that says "idle" beside a card asking
 *  for a decision would contradict the page it sits on. */
export function orderFleet(nodes: readonly RunNode[], attentionSlots: ReadonlySet<string> = new Set()): { active: RunNode[]; resting: RunNode[] } {
  const lifted = nodes.map(n => n.kind === 'session' && attentionSlots.has(n.slot) && n.state !== 'needs_input' ? { ...n, state: 'needs_input' as const } : n)
  const sorted = lifted.sort((a, b) => STATE_RANK[a.state] - STATE_RANK[b.state])
  return { active: sorted.filter(n => !isResting(n)), resting: sorted.filter(isResting) }
}

/** The Schedule tile: how many jobs can fire, how many are paused or failing, and
 *  the next one due. A job's `next_run_ts` is the gateway's own forecast; a job
 *  with none (a one-shot that fired, a job in failure backoff) is not "next". */
interface ScheduleSummary { enabled: number; paused: number; failing: number; running: number; next: CronJob | null }

export function scheduleSummary(jobs: readonly CronJob[]): ScheduleSummary {
  const enabled = jobs.filter(j => j.enabled)
  return {
    enabled: enabled.length,
    paused: jobs.length - enabled.length,
    failing: enabled.filter(j => j.last_status === 'error').length,
    running: jobs.filter(j => j.is_running).length,
    next: upcomingJobs(jobs, 1)[0] ?? null,
  }
}

/** A job the gateway has forecast a next run for. */
export type UpcomingJob = CronJob & { next_run_ts: number }

/** Enabled jobs with a forecast run, soonest first. */
export function upcomingJobs(jobs: readonly CronJob[], limit: number): UpcomingJob[] {
  return jobs
    .filter((j): j is UpcomingJob => j.enabled && typeof j.next_run_ts === 'number')
    .sort((a, b) => a.next_run_ts - b.next_run_ts)
    .slice(0, limit)
}

/** The Bots tile: crewmates mid-turn over the roster size. */
export function botCounts(rows: readonly MemberRosterRow[]): { running: number; total: number } {
  return { running: rows.filter(r => r.running).length, total: rows.length }
}

/** One model's share of today's usage: cost on a dollar-billed harness, tokens otherwise. */
export interface SpendRow { model: string; amount: number }
type TokenBucket = { input: number; output: number; cacheCreate: number; cacheRead: number; costUsd: number }

/** Today's usage by model, largest first, the smallest folded into one "other" row
 *  once there are more than `limit` so the bar's legend stays scannable. A model
 *  with nothing to show is dropped. */
export function spendByModel(models: Record<string, TokenBucket> | undefined, dollars: boolean, limit: number, otherLabel: string): SpendRow[] {
  const measure = (b: TokenBucket) => dollars ? b.costUsd : b.input + b.output + b.cacheCreate + b.cacheRead
  const rows = Object.entries(models ?? {})
    .map(([model, bucket]) => ({ model, amount: measure(bucket) }))
    .filter(r => r.amount > 0)
    .sort((a, b) => b.amount - a.amount)
  if (rows.length <= limit) return rows
  const kept = rows.slice(0, limit - 1)
  return [...kept, { model: otherLabel, amount: rows.slice(limit - 1).reduce((sum, r) => sum + r.amount, 0) }]
}
