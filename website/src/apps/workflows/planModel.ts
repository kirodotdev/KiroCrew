/**
 * planModel — reconcile a run's PREDICTED plan with what actually happened.
 *
 * The backend (`workflows/preview.py`) reads a script's planned phases and nodes off
 * its source; the event stream says what ran. This module merges the two into the rows
 * the graph view draws, and its whole job is to never claim an identity it cannot
 * justify.
 *
 * The one rule that makes that true: an `unknown` node is a FENCE. It marks a region
 * whose shape only the run knows (a loop, a branch, a computed fan-out), so positions
 * after it carry no meaning and ordinal pairing STOPS there. Before the fence, planned
 * node i is the same work as actual node i, because the script's calls run in source
 * order. After it, the graph shows the markers (the explanation) and then reality once
 * reality exists — never a guess sitting next to the thing it guessed about.
 *
 * Run status and plan provenance are separate fields throughout. Collapsing them into
 * one enum loses a real state: an unpredicted agent that FAILED is both unpredicted and
 * failed, and a reader needs the failure more than the surprise.
 *
 * Pure / non-mutating, and unit-tested in src/test/workflowPlanModel.test.ts.
 */
import { groupByPhase, type AgentRow, type PhaseGroup, type WfEvent } from './runModel'

/**
 * Anything, as text.
 *
 * `runModel` reads an event's fields with a cast and no runtime narrowing, and says in
 * its own comment that narrowing there would change what a malformed event renders --
 * a decision that belongs to the tree view, not to this module. But nothing coerces a
 * workflow script's arguments either: `ctx.phase(123)` records a numeric `title`, and
 * `ctx.agent(..., label=123)` a numeric `label`. The graph then does string work on
 * them -- cutting a title to the plan's limit, sanitizing a label -- so a number
 * reaches `.slice` and throws, and the whole view goes blank instead of one node
 * reading oddly. A workflow author can produce that input, so the graph coerces at its
 * own door rather than trusting the wire.
 *
 * Total by construction: every input has a text form, so there is no branch that can
 * pass a non-string through.
 */
export function asText(value: unknown): string {
  if (typeof value === 'string') return value
  if (value === null || value === undefined) return ''
  return String(value)
}

/** One phase group with every event-derived string coerced. */
function textualPhase(group: PhaseGroup): PhaseGroup {
  return {
    ...group,
    title: asText(group.title),
    agents: group.agents.map(row => ({
      ...row,
      agent_id: asText(row.agent_id),
      // No branch to preserve an absent label: the only consumer is `label || agent_id`,
      // so '' and undefined take the same path, which is the fallback the runner's own
      // `label or prompt[:40]` also takes for a falsy label.
      label: asText(row.label),
    })),
  }
}

/** A node the backend predicted. `certain: false` means it may not run, or may run a
 *  number of times only the run knows. */
export interface PlanNode {
  kind: 'agent' | 'unknown'
  label: string
  certain: boolean
}

export interface PlanPhase {
  title: string
  certain: boolean
  nodes: PlanNode[]
}

/** One plan phase with every string coerced, mirroring `textualPhase`. */
function textualPlanPhase(phase: PlanPhase): PlanPhase {
  return {
    ...phase,
    title: asText(phase.title),
    nodes: phase.nodes.map(node => ({ ...node, label: asText(node.label) })),
  }
}

/** `plan` on the run-detail snapshot. Absent (not null) when no plan is readable. */
export interface RunPlan {
  phases: PlanPhase[]
  truncated: boolean
  /**
   * Characters the backend keeps of a phase title. A title longer than this is stored
   * cut, while the run's own `phase_started` event carries it whole, so pairing cuts
   * the event title to the same length instead of comparing unequal strings.
   */
  titleLimit?: number
}

/**
 * What the graph knows about one node's execution.
 *
 *  - `ran_ok` / `ran_failed` / `running` — the event stream has it; this is fact.
 *  - `planned` — nothing has run here yet.
 *  - `unknown` — a region the plan refuses to predict. Not work; an explanation.
 */
export type GraphNodeState = 'ran_ok' | 'ran_failed' | 'running' | 'planned' | 'unknown'

export interface GraphNode {
  id: string
  label: string
  state: GraphNodeState
  /** False when this node ran without the plan predicting it. */
  predicted: boolean
  /**
   * Only on an `unknown` marker, and only once the region it stands for has produced
   * real work: how many nodes materialized there. Absent means nothing has run there
   * yet, so the marker still stands for something being waited on.
   */
  resolvedCount?: number
  /** ms the agent took, when it finished and the stream carried both instants. */
  elapsedMs?: number
}

/** `planned` means the run has not entered the phase yet. */
export type GraphPhaseState = 'running' | 'ok' | 'failed' | 'planned'

export interface GraphPhase {
  title: string
  state: GraphPhaseState
  /** False when this phase ran without the plan naming it. */
  predicted: boolean
  /** False when the plan could not promise the phase runs at all. */
  certain: boolean
  nodes: GraphNode[]
}

export interface RunGraph {
  phases: GraphPhase[]
  /** The plan hit its own node/phase ceiling, so it is partial. */
  truncated: boolean
  /** Whether a plan existed at all — an empty graph means different things either way. */
  hasPlan: boolean
}

export type RunStatus = 'running' | 'paused' | 'finished' | 'failed' | 'cancelled' | string

function actualState(row: AgentRow): GraphNodeState {
  if (row.ok === undefined) return 'running'
  return row.ok ? 'ran_ok' : 'ran_failed'
}

function actualNode(row: AgentRow, id: string, predicted: boolean): GraphNode {
  return {
    id,
    label: row.label || row.agent_id,
    state: actualState(row),
    predicted,
    elapsedMs: row.elapsed_ms,
  }
}

/**
 * Status of a phase the run has entered.
 *
 * Phases are emitted in order and a phase persists until the next one starts, so a
 * phase the run has moved past is complete whatever it spawned — without that, a phase
 * whose work was pure narration would spin forever. This mirrors the run tree's own
 * rule so the two views cannot disagree about the same run.
 */
function startedPhaseState(
  agents: AgentRow[],
  status: RunStatus | undefined,
  isLast: boolean,
): GraphPhaseState {
  if (agents.some(a => a.ok === false)) return 'failed'
  if (!isLast) return 'ok'
  if (status && status !== 'running' && status !== 'paused') {
    return status === 'failed' ? 'failed' : 'ok'
  }
  if (agents.length > 0 && agents.every(a => a.ok === true)) return 'ok'
  return 'running'
}

/**
 * Merge one phase's predicted nodes with the agents that actually ran in it.
 *
 * `planned` may be empty (the phase was not predicted) and `actual` may be empty (the
 * run has not reached it). The fence rule is implemented here.
 *
 * `hasPlan` is false when the run has no readable plan at all. Then nothing can be a
 * surprise: `predicted: false` is a claim that the plan MISSED this work, and with no
 * plan there is nothing to have missed. Marking every node unplanned there put "not
 * planned" on every row of a task-plan run, which reads as the preview being wrong
 * rather than absent.
 */
function mergeNodes(
  planned: PlanNode[],
  actual: AgentRow[],
  key: string,
  hasPlan: boolean,
): GraphNode[] {
  const fence = planned.findIndex(n => n.kind === 'unknown')
  const pairUpTo = fence < 0 ? planned.length : fence
  const out: GraphNode[] = []

  for (let i = 0; i < pairUpTo; i++) {
    const row = actual[i]
    if (row) out.push(actualNode(row, `${key}:a${i}`, true))
    else {
      out.push({ id: `${key}:p${i}`, label: planned[i].label, state: 'planned', predicted: true })
    }
  }

  if (fence < 0) {
    // Nothing was left unpredicted, so anything past the plan's length is a surprise
    // and is marked as one rather than quietly filling a planned slot.
    for (let i = pairUpTo; i < actual.length; i++) {
      out.push(actualNode(actual[i], `${key}:x${i}`, !hasPlan))
    }
    return out
  }

  const tail = actual.slice(pairUpTo)

  // At and past the fence the plan cannot say which node is which. Show every marker,
  // because the markers are why the counts may differ -- and once the region has
  // produced real work, say how much, so a marker standing beside real boxes reads as
  // settled rather than as something still being waited on.
  planned.slice(fence).forEach((node, i) => {
    if (node.kind === 'unknown') {
      out.push({
        id: `${key}:u${fence + i}`,
        label: node.label,
        state: 'unknown',
        predicted: true,
        ...(tail.length > 0 ? { resolvedCount: tail.length } : {}),
      })
    }
  })
  if (tail.length > 0) {
    // ...then reality once reality exists. These are predicted in the honest sense: the
    // plan said work would happen here, it just could not say how much.
    tail.forEach((row, i) => out.push(actualNode(row, `${key}:a${pairUpTo + i}`, true)))
    return out
  }
  // Nothing has materialized yet, so the region's predicted work is still worth
  // showing — dimmed, and never paired with anything.
  planned.slice(fence).forEach((node, i) => {
    if (node.kind === 'agent') {
      out.push({
        id: `${key}:p${fence + i}`,
        label: node.label,
        state: 'planned',
        predicted: true,
      })
    }
  })
  return out
}

/**
 * Build the graph rows for a run.
 *
 * `plan` is the snapshot's `plan` field, or null/undefined when the backend could read
 * none — then the graph is exactly what happened, which is still a useful drawing and
 * is why `hasPlan` is reported separately from an empty `phases`.
 *
 * Row order takes the PLAN as its spine, because that is the order the script declares
 * and the order a reader is asking about. Phases that ran without being predicted are
 * appended in the order they ran, so a surprise is visible as a surprise.
 */
export function buildGraph(
  plan: RunPlan | null | undefined,
  events: WfEvent[],
  status?: RunStatus,
): RunGraph {
  // groupByPhase already folds repeated titles into one row, so a title identifies a
  // row here exactly as it does there. The plan may have cut a long title, so both
  // sides are cut to the plan's own limit before they are compared.
  const actualPhases: PhaseGroup[] = groupByPhase(events).map(textualPhase)
  const lastActual = actualPhases.length - 1
  const limit = plan?.titleLimit
  // Both sides reach here already coerced -- the events through `textualPhase`, the plan
  // through `textualPlanPhase` -- so this does no coercion of its own. A second copy here
  // would be a branch no input can exercise.
  const key = (title: string) => (limit && limit > 0 ? title.slice(0, limit) : title)
  const indexByTitle = new Map<string, number>()
  actualPhases.forEach((p, i) => indexByTitle.set(key(p.title), i))

  const phases: GraphPhase[] = []
  const matched = new Set<number>()
  const hasPlan = !!plan

  // The plan arrives over HTTP too. Its strings are produced by the previewer's own
  // bounding helper, so they are text by construction -- but coercing both inputs at one
  // seam costs two calls and leaves no side more trusted than the other, which is the
  // asymmetry a later reader would have to re-derive.
  for (const planned of (plan?.phases ?? []).map(textualPlanPhase)) {
    const at = indexByTitle.get(key(planned.title))
    const actual = at === undefined ? null : actualPhases[at]
    if (at !== undefined) matched.add(at)
    phases.push({
      title: planned.title,
      predicted: true,
      certain: planned.certain,
      state:
        actual === null
          ? 'planned'
          : startedPhaseState(actual.agents, status, at === lastActual),
      nodes: mergeNodes(
        planned.nodes,
        actual?.agents ?? [],
        `${planned.title}#${phases.length}`,
        hasPlan,
      ),
    })
  }

  actualPhases.forEach((actual, i) => {
    if (matched.has(i)) return
    phases.push({
      title: actual.title,
      predicted: !hasPlan,
      certain: true, // it ran: nothing about it is uncertain any more
      state: startedPhaseState(actual.agents, status, i === lastActual),
      nodes: mergeNodes([], actual.agents, `${actual.title}#${phases.length}`, hasPlan),
    })
  })

  return { phases, truncated: !!plan?.truncated, hasPlan }
}
