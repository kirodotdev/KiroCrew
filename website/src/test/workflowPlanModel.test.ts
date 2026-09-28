/**
 * buildGraph — reconciling a workflow run's predicted plan with what actually ran.
 *
 * Every case here is about an identity claim. The load-bearing one is the FENCE: past
 * an `unknown` node the plan cannot say which actual node is which, so the model must
 * stop pairing rather than guess, and it must not show a prediction sitting beside the
 * reality that superseded it.
 */
import { describe, expect, it } from 'vitest'
import { asText, buildGraph, type RunPlan } from '../apps/workflows/planModel'

function ev(type: string, data: Record<string, unknown> = {}, seq = 0, ts = '2026-09-18T10:00:00.000Z') {
  return { run_id: 'wf_1', seq, ts, type, data }
}

function started(title: string, seq: number) {
  return ev('phase_started', { title }, seq)
}

function agent(id: string, label: string, phase: string, seq: number, ts?: string) {
  return ev('agent_started', { agent_id: id, label, phase }, seq, ts)
}

function finished(id: string, ok: boolean, seq: number, ts?: string) {
  return ev('agent_finished', { agent_id: id, ok }, seq, ts)
}

const plan = (phases: RunPlan['phases'], truncated = false): RunPlan => ({ phases, truncated })
const certainAgent = (label: string) => ({ kind: 'agent' as const, label, certain: true })
const uncertainAgent = (label: string) => ({ kind: 'agent' as const, label, certain: false })
const marker = (label: string) => ({ kind: 'unknown' as const, label, certain: false })

describe('buildGraph without a plan', () => {
  it('draws exactly what ran and says no plan existed', () => {
    const graph = buildGraph(null, [started('Read', 0), agent('a0', 'look', 'Read', 1)], 'running')
    expect(graph.hasPlan).toBe(false)
    expect(graph.phases).toHaveLength(1)
    expect(graph.phases[0].predicted).toBe(true)
    expect(graph.phases[0].nodes.map(n => [n.label, n.state])).toEqual([['look', 'running']])
  })

  it('reports an empty graph and no plan separately', () => {
    const graph = buildGraph(undefined, [], 'running')
    expect(graph.phases).toEqual([])
    expect(graph.hasPlan).toBe(false)
  })

  it('marks nothing unplanned, because there is no plan to have missed it', () => {
    // "not planned" is a claim the PREVIEW was wrong. On a run with no readable plan
    // (a task-plan source) it put that claim on every node, which reads as the preview
    // failing rather than being absent.
    const graph = buildGraph(
      null,
      [started('Triage', 0), agent('a0', 'sort the queue', 'Triage', 1)],
      'running',
    )
    expect(graph.phases[0].nodes.every(n => n.predicted)).toBe(true)
  })
})

describe('buildGraph before the run reaches a phase', () => {
  it('shows every planned phase and node as planned', () => {
    const graph = buildGraph(
      plan([{ title: 'Read', certain: true, nodes: [certainAgent('look')] }]),
      [],
      'running',
    )
    expect(graph.hasPlan).toBe(true)
    expect(graph.phases[0].state).toBe('planned')
    expect(graph.phases[0].nodes.map(n => [n.label, n.state])).toEqual([['look', 'planned']])
  })

  it('carries a phase the plan could not promise', () => {
    const graph = buildGraph(
      plan([{ title: 'Maybe', certain: false, nodes: [] }]),
      [],
      'running',
    )
    expect(graph.phases[0].certain).toBe(false)
  })
})

describe('buildGraph pairing planned nodes with real ones', () => {
  const twoPlanned = plan([
    { title: 'Read', certain: true, nodes: [certainAgent('look'), certainAgent('check')] },
  ])

  it('replaces a planned node with the agent that ran in its position', () => {
    const graph = buildGraph(
      twoPlanned,
      [
        started('Read', 0),
        agent('a0', 'look', 'Read', 1, '2026-09-18T10:00:00.000Z'),
        finished('a0', true, 2, '2026-09-18T10:00:02.000Z'),
      ],
      'running',
    )
    const nodes = graph.phases[0].nodes
    expect(nodes.map(n => [n.label, n.state])).toEqual([
      ['look', 'ran_ok'],
      ['check', 'planned'],
    ])
    expect(nodes[0].elapsedMs).toBe(2000)
  })

  it('carries a failure onto the node and the phase', () => {
    const graph = buildGraph(
      twoPlanned,
      [started('Read', 0), agent('a0', 'look', 'Read', 1), finished('a0', false, 2)],
      'running',
    )
    expect(graph.phases[0].nodes[0].state).toBe('ran_failed')
    expect(graph.phases[0].state).toBe('failed')
  })

  it('marks an agent the plan did not predict', () => {
    // No fence in this plan, so a third agent is a genuine surprise and is labelled
    // as one instead of quietly filling a planned slot.
    const graph = buildGraph(
      twoPlanned,
      [
        started('Read', 0),
        agent('a0', 'look', 'Read', 1),
        agent('a1', 'check', 'Read', 2),
        agent('a2', 'extra', 'Read', 3),
      ],
      'running',
    )
    const nodes = graph.phases[0].nodes
    expect(nodes.map(n => [n.label, n.predicted])).toEqual([
      ['look', true],
      ['check', true],
      ['extra', false],
    ])
  })
})

describe('buildGraph fence at an unpredictable region', () => {
  const loopPlan = plan([
    {
      title: 'Ship',
      certain: true,
      nodes: [certainAgent('draft'), marker('for'), uncertainAgent('file')],
    },
  ])

  it('shows the marker and the uncertain work while nothing has run there', () => {
    const graph = buildGraph(
      loopPlan,
      [started('Ship', 0), agent('a0', 'draft', 'Ship', 1), finished('a0', true, 2)],
      'running',
    )
    expect(graph.phases[0].nodes.map(n => [n.label, n.state])).toEqual([
      ['draft', 'ran_ok'],
      ['for', 'unknown'],
      ['file', 'planned'],
    ])
  })

  it('replaces the prediction with reality once the region materializes', () => {
    // Three agents came out of a loop the plan drew as one node. Keeping the single
    // predicted node beside them would claim an identity for one of the three.
    const graph = buildGraph(
      loopPlan,
      [
        started('Ship', 0),
        agent('a0', 'draft', 'Ship', 1),
        finished('a0', true, 2),
        agent('a1', 'file one', 'Ship', 3),
        agent('a2', 'file two', 'Ship', 4),
        agent('a3', 'file three', 'Ship', 5),
      ],
      'running',
    )
    expect(graph.phases[0].nodes.map(n => [n.label, n.state])).toEqual([
      ['draft', 'ran_ok'],
      ['for', 'unknown'],
      ['file one', 'running'],
      ['file two', 'running'],
      ['file three', 'running'],
    ])
  })

  it('never calls work past the fence unplanned', () => {
    // The plan DID say work happens here; it only could not say how much. Marking it
    // "not planned" would report the preview as wrong when it was right.
    const graph = buildGraph(
      loopPlan,
      [started('Ship', 0), agent('a0', 'draft', 'Ship', 1), agent('a1', 'file one', 'Ship', 2)],
      'running',
    )
    expect(graph.phases[0].nodes.every(n => n.predicted)).toBe(true)
  })

  it('stops pairing AT the fence, not after the plan runs out', () => {
    // Without the fence the second real agent would be paired with the planned node
    // that follows the marker, which is exactly the wrong identity.
    const graph = buildGraph(
      plan([
        {
          title: 'Ship',
          certain: true,
          nodes: [marker('if'), uncertainAgent('polish')],
        },
      ]),
      [started('Ship', 0), agent('a0', 'something else', 'Ship', 1)],
      'running',
    )
    expect(graph.phases[0].nodes.map(n => [n.label, n.state])).toEqual([
      ['if', 'unknown'],
      ['something else', 'running'],
    ])
  })
})

describe('buildGraph row order', () => {
  it('takes the plan as the spine', () => {
    const graph = buildGraph(
      plan([
        { title: 'Read', certain: true, nodes: [] },
        { title: 'Write', certain: true, nodes: [] },
        { title: 'Ship', certain: true, nodes: [] },
      ]),
      [started('Read', 0), started('Write', 1)],
      'running',
    )
    expect(graph.phases.map(p => [p.title, p.state])).toEqual([
      ['Read', 'ok'],
      ['Write', 'running'],
      ['Ship', 'planned'],
    ])
  })

  it('appends a phase that ran without being predicted', () => {
    const graph = buildGraph(
      plan([{ title: 'Read', certain: true, nodes: [] }]),
      [started('Read', 0), started('Surprise', 1), agent('a0', 'x', 'Surprise', 2)],
      'running',
    )
    expect(graph.phases.map(p => [p.title, p.predicted])).toEqual([
      ['Read', true],
      ['Surprise', false],
    ])
  })

  it('keeps a failure on an unpredicted phase rather than only its surprise', () => {
    const graph = buildGraph(
      plan([{ title: 'Read', certain: true, nodes: [] }]),
      [started('Surprise', 0), agent('a0', 'x', 'Surprise', 1), finished('a0', false, 2)],
      'running',
    )
    const surprise = graph.phases.find(p => p.title === 'Surprise')!
    expect(surprise.predicted).toBe(false)
    expect(surprise.state).toBe('failed')
  })

  it('a phase the run has moved past is complete even with no agents', () => {
    const graph = buildGraph(
      plan([
        { title: 'Authoring', certain: true, nodes: [] },
        { title: 'Work', certain: true, nodes: [] },
      ]),
      [started('Authoring', 0), started('Work', 1)],
      'running',
    )
    expect(graph.phases[0].state).toBe('ok')
  })

  it('a terminal run leaves no phase spinning', () => {
    const graph = buildGraph(
      plan([{ title: 'Read', certain: true, nodes: [] }]),
      [started('Read', 0)],
      'cancelled',
    )
    expect(graph.phases[0].state).toBe('ok')
  })
})

describe('buildGraph truncation', () => {
  it('passes the plan ceiling through', () => {
    expect(buildGraph(plan([], true), [], 'running').truncated).toBe(true)
    expect(buildGraph(plan([], false), [], 'running').truncated).toBe(false)
  })
})

describe('buildGraph when the plan cut a long phase title', () => {
  const long = 'T'.repeat(130)
  const cut = long.slice(0, 120)

  it('pairs a cut plan title with the untruncated event title', () => {
    const graph = buildGraph(
      { phases: [{ title: cut, certain: true, nodes: [certainAgent('look')] }], truncated: false, titleLimit: 120 },
      [started(long, 0), agent('a0', 'look', long, 1), finished('a0', true, 2)],
      'completed',
    )
    expect(graph.phases).toHaveLength(1)
    expect(graph.phases[0].predicted).toBe(true)
    expect(graph.phases[0].nodes.map(n => n.state)).toEqual(['ran_ok'])
  })

  it('without the limit the same pair reads as two phases, one of them a surprise', () => {
    // Pins WHY the limit travels: with no limit to cut the event title by, the strings
    // are unequal and the run looks like it did unplanned work.
    const graph = buildGraph(
      { phases: [{ title: cut, certain: true, nodes: [certainAgent('look')] }], truncated: false },
      [started(long, 0), agent('a0', 'look', long, 1)],
      'running',
    )
    expect(graph.phases).toHaveLength(2)
    expect(graph.phases[1].predicted).toBe(false)
  })
})

describe('an unknown marker once its region materializes', () => {
  const fenced = plan([
    { title: 'Work', certain: true, nodes: [marker('for'), uncertainAgent('task')] },
  ])

  it('carries no count while nothing has run there', () => {
    const graph = buildGraph(fenced, [started('Work', 0)], 'running')
    const markers = graph.phases[0].nodes.filter(n => n.state === 'unknown')
    expect(markers).toHaveLength(1)
    expect(markers[0].resolvedCount).toBeUndefined()
  })

  it('says how many nodes materialized once they have', () => {
    const graph = buildGraph(
      fenced,
      [
        started('Work', 0),
        agent('a0', 'one', 'Work', 1),
        agent('a1', 'two', 'Work', 2),
        agent('a2', 'three', 'Work', 3),
      ],
      'running',
    )
    const markers = graph.phases[0].nodes.filter(n => n.state === 'unknown')
    expect(markers[0].resolvedCount).toBe(3)
  })
})

describe('a workflow script that passes a non-string where text is expected', () => {
  // Nothing coerces a script's arguments: `ctx.phase(123)` records a numeric `title`,
  // `ctx.agent(..., label=123)` a numeric `label`, and `runModel` reads both with a cast
  // and no runtime narrowing. The graph then cuts a title to the plan's limit and
  // sanitizes a label, so before coercion a number reached `.slice` and the whole view
  // threw instead of one node reading oddly. A workflow author can produce that input.
  const PLAN: RunPlan = {
    phases: [{ title: 'Research', certain: true, nodes: [{ kind: 'agent', label: 'spec', certain: true }] }],
    truncated: false,
    titleLimit: 120,
  }

  it('does not throw on a numeric phase title', () => {
    const graph = buildGraph(
      PLAN,
      [ev('phase_started', { title: 123 }, 0) as never],
      'running',
    )
    // The title is text in the model, so every later string operation is safe.
    const titles = graph.phases.map(p => p.title)
    expect(titles).toContain('123')
    expect(titles.every(t => typeof t === 'string')).toBe(true)
  })

  it('does not throw on a numeric agent label', () => {
    const graph = buildGraph(
      PLAN,
      [
        ev('phase_started', { title: 'Research' }, 0) as never,
        ev('agent_started', { agent_id: 'a0', label: 456, phase: 'Research' }, 1) as never,
      ],
      'running',
    )
    const labels = graph.phases.flatMap(p => p.nodes.map(n => n.label))
    expect(labels).toContain('456')
    expect(labels.every(l => typeof l === 'string')).toBe(true)
  })

  it('pairs a numeric title with the plan row that carries its text form', () => {
    // Coercion is not enough on its own: the coerced title must be the value pairing
    // compares, or a phase that ran would still render as one the plan missed.
    const graph = buildGraph(
      { ...PLAN, phases: [{ title: '123', certain: true, nodes: [] }] },
      [ev('phase_started', { title: 123 }, 0) as never],
      'running',
    )
    expect(graph.phases).toHaveLength(1)
    expect(graph.phases[0].predicted).toBe(true)
  })

  it('still falls back to the agent id when a label is absent', () => {
    // The coercion must not turn "no label" into a label: the runner's own
    // `label or prompt[:40]` collapses a falsy label, and the id is the fallback here.
    const graph = buildGraph(
      { ...PLAN, phases: [{ title: 'Research', certain: true, nodes: [] }] },
      [
        ev('phase_started', { title: 'Research' }, 0) as never,
        ev('agent_started', { agent_id: 'a7', phase: 'Research' }, 1) as never,
      ],
      'running',
    )
    const labels = graph.phases.flatMap(p => p.nodes.map(n => n.label))
    expect(labels).toContain('a7')
  })

  it('reads an absent title as empty text rather than the word undefined', () => {
    const graph = buildGraph(
      PLAN,
      [ev('phase_started', {}, 0) as never],
      'running',
    )
    expect(graph.phases.map(p => p.title)).not.toContain('undefined')
  })

  it('coerces the plan side too, so neither input is trusted more than the other', () => {
    // The plan arrives over HTTP as well. Its strings come from the previewer's own
    // bounding helper, so this case is not reachable from our own backend -- it exists
    // because trusting one of two inputs is the asymmetry that becomes the next defect.
    const graph = buildGraph(
      {
        phases: [
          { title: 7 as never, certain: true, nodes: [{ kind: 'agent', label: 9 as never, certain: true }] },
        ],
        truncated: false,
        titleLimit: 120,
      },
      [],
      'running',
    )
    expect(graph.phases[0].title).toBe('7')
    expect(graph.phases[0].nodes.map(n => n.label)).toEqual(['9'])
    expect(graph.phases[0].nodes.every(n => typeof n.label === 'string')).toBe(true)
  })
})

describe('asText', () => {
  it('is total: every input has a text form', () => {
    expect(asText('x')).toBe('x')
    expect(asText(0)).toBe('0')
    expect(asText(123)).toBe('123')
    expect(asText(false)).toBe('false')
    expect(asText(null)).toBe('')
    expect(asText(undefined)).toBe('')
  })

  it('does not stringify null or undefined into their own names', () => {
    // 'null' / 'undefined' on a node would read as a real label a script had written.
    expect(asText(null)).not.toBe('null')
    expect(asText(undefined)).not.toBe('undefined')
  })
})
