/**
 * Render tests for the workflow graph view (#11796, from #1652).
 *
 * The reconciliation is unit-tested in workflowPlanModel.test.ts; what these cover is
 * the half a model test cannot see — that a prediction is visibly different from a
 * fact, that an unpredictable region says so on the picture, that the strings are
 * localized, and that the drawing is NOT a control surface.
 */
import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import WorkflowRunGraph from '../apps/workflows/WorkflowRunGraph'
import type { RunPlan } from '../apps/workflows/planModel'
import { i18next, initI18n } from '../i18n/all'
import { renderWithProviders } from './helpers'

afterEach(async () => {
  await i18next.changeLanguage('en')
})

function ev(type: string, data: Record<string, unknown>, seq: number, ts = '2026-09-18T10:00:00.000Z') {
  return { run_id: 'wf_1', seq, ts, type, data }
}

const PLAN: RunPlan = {
  phases: [
    {
      title: 'Research',
      certain: true,
      nodes: [
        { kind: 'agent', label: 'spec', certain: true },
        { kind: 'agent', label: 'code', certain: true },
      ],
    },
    {
      title: 'Ship',
      certain: true,
      nodes: [
        { kind: 'unknown', label: 'for', certain: false },
        { kind: 'agent', label: 'file', certain: false },
      ],
    },
  ],
  truncated: false,
}

const RAN_FIRST = [
  ev('phase_started', { title: 'Research' }, 0),
  ev('agent_started', { agent_id: 'a0', label: 'spec', phase: 'Research' }, 1),
  ev('agent_finished', { agent_id: 'a0', ok: true }, 2, '2026-09-18T10:00:03.000Z'),
]

describe('WorkflowRunGraph', () => {
  it('draws the whole plan, not only what ran', async () => {
    await initI18n()
    renderWithProviders(<WorkflowRunGraph events={RAN_FIRST} plan={PLAN} status="running" />)

    // Both stages are on the picture even though the run is inside the first.
    expect(screen.getByText('Research')).toBeInTheDocument()
    expect(screen.getByText('Ship')).toBeInTheDocument()
    expect(screen.getAllByTestId('workflow-graph-phase')).toHaveLength(2)
    // One edge between the two stages: phases are sequential, agents inside are not.
    expect(screen.getAllByTestId('workflow-graph-edge')).toHaveLength(1)
  })

  it('tells a fact apart from a prediction', async () => {
    await initI18n()
    renderWithProviders(<WorkflowRunGraph events={RAN_FIRST} plan={PLAN} status="running" />)

    const ran = screen.getByText('spec').closest('[data-testid="workflow-graph-node"]')
    const predicted = screen.getByText('code').closest('[data-testid="workflow-graph-node"]')
    expect(ran).toHaveAttribute('data-node-state', 'ran_ok')
    expect(predicted).toHaveAttribute('data-node-state', 'planned')
    // The dashed border is what carries the distinction, so it must be on the chip
    // rather than only in the accessible name.
    expect(predicted!.className).toContain('border-dashed')
    expect(ran!.className).not.toContain('border-dashed')
  })

  it('says on the picture that a region\u2019s shape is only known at run time', async () => {
    await initI18n()
    renderWithProviders(<WorkflowRunGraph events={RAN_FIRST} plan={PLAN} status="running" />)

    const region = screen.getByText('for').closest('[data-testid="workflow-graph-node"]')
    expect(region).toHaveAttribute('data-node-state', 'unknown')
    expect(within(region!).getByText('shape decided at run time')).toBeInTheDocument()
  })

  it('shows how long a finished agent took', async () => {
    await initI18n()
    renderWithProviders(<WorkflowRunGraph events={RAN_FIRST} plan={PLAN} status="running" />)

    const ran = screen.getByText('spec').closest('[data-testid="workflow-graph-node"]')
    expect(within(ran!).getByText('3.0s')).toBeInTheDocument()
  })

  it('marks a phase the plan could not promise runs at all', async () => {
    await initI18n()
    renderWithProviders(
      <WorkflowRunGraph
        events={[]}
        plan={{ phases: [{ title: 'Maybe', certain: false, nodes: [] }], truncated: false }}
        status="running"
      />,
    )
    const phase = screen.getByText('Maybe').closest('[data-testid="workflow-graph-phase"]')
    expect(phase).toHaveAttribute('data-phase-certain', 'no')
    expect(within(phase!).getByText('may not run')).toBeInTheDocument()
  })

  it('says when no plan could be read, rather than drawing an empty one', async () => {
    await initI18n()
    renderWithProviders(<WorkflowRunGraph events={RAN_FIRST} status="running" />)

    expect(screen.getByTestId('workflow-graph-no-plan')).toBeInTheDocument()
    // What ran is still drawn.
    expect(screen.getByText('spec')).toBeInTheDocument()
  })

  it('says when the plan hit its own ceiling', async () => {
    await initI18n()
    renderWithProviders(
      <WorkflowRunGraph events={RAN_FIRST} plan={{ ...PLAN, truncated: true }} status="running" />,
    )
    expect(screen.getByTestId('workflow-graph-truncated')).toBeInTheDocument()
  })

  it('renders nothing to draw when there is neither a plan nor an event', async () => {
    await initI18n()
    renderWithProviders(<WorkflowRunGraph events={[]} status="running" />)
    expect(screen.getByTestId('workflow-graph-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('workflow-run-graph')).not.toBeInTheDocument()
  })

  it('is not a control surface', async () => {
    await initI18n()
    renderWithProviders(<WorkflowRunGraph events={RAN_FIRST} plan={PLAN} status="running" />)

    // Nothing in the drawing can start, stop or re-run anything. Wiring a node to
    // rerun would let a misclick spend tokens and restart real agents.
    const graph = screen.getByTestId('workflow-run-graph')
    expect(graph.querySelectorAll('button')).toHaveLength(0)
    expect(graph.querySelectorAll('a')).toHaveLength(0)
    expect(graph.querySelectorAll('[role="button"]')).toHaveLength(0)
  })

  it('renders its own strings in the active language', async () => {
    // A hardcoded English caption would survive an en-only assertion.
    await initI18n()
    await i18next.changeLanguage('de')
    renderWithProviders(<WorkflowRunGraph events={RAN_FIRST} plan={PLAN} status="running" />)

    const region = screen.getByText('for').closest('[data-testid="workflow-graph-node"]')
    expect(within(region!).getByText('Form wird zur Laufzeit bestimmt')).toBeInTheDocument()
    // The construct keyword itself is Python, so it stays as written.
    expect(within(region!).getByText('for')).toBeInTheDocument()
  })

  it('redacts a credential an authored label carries', async () => {
    await initI18n()
    renderWithProviders(
      <WorkflowRunGraph
        events={[]}
        plan={{
          phases: [
            {
              title: 'Read',
              certain: true,
              nodes: [
                { kind: 'agent', label: 'post xoxb-123456789012-abcdefghijkl', certain: true },
              ],
            },
          ],
          truncated: false,
        }}
        status="running"
      />,
    )
    // Plan labels are the script's own string literals, so they are LLM-derived and
    // ride the same redaction the run tree applies to an agent label.
    const node = screen.getByTestId('workflow-graph-node')
    expect(node.textContent).not.toContain('xoxb-123456789012-abcdefghijkl')
  })
})

describe('a fenced region that has materialized', () => {
  const FENCED: RunPlan = {
    phases: [
      {
        title: 'Ship',
        certain: true,
        nodes: [
          { kind: 'unknown', label: 'for', certain: false },
          { kind: 'agent', label: 'file', certain: false },
        ],
      },
    ],
    truncated: false,
  }

  it('leaves the marker asking while nothing has run there', async () => {
    await initI18n()
    renderWithProviders(
      <WorkflowRunGraph
        events={[ev('phase_started', { title: 'Ship' }, 0)]}
        plan={FENCED}
        status="running"
      />,
    )
    const marker = screen
      .getAllByTestId('workflow-graph-node')
      .find(n => n.getAttribute('data-node-state') === 'unknown')!
    expect(marker.getAttribute('data-node-resolved')).toBeNull()
    expect(marker.textContent).toContain('shape decided at run time')
  })

  it('answers the marker with what ran once real work exists', async () => {
    await initI18n()
    renderWithProviders(
      <WorkflowRunGraph
        events={[
          ev('phase_started', { title: 'Ship' }, 0),
          ev('agent_started', { agent_id: 'a0', label: 'one', phase: 'Ship' }, 1),
          ev('agent_started', { agent_id: 'a1', label: 'two', phase: 'Ship' }, 2),
        ]}
        plan={FENCED}
        status="running"
      />,
    )
    // A marker that still reads "shape decided at run time" beside real boxes leaves a
    // reader unable to tell waiting from settled.
    const marker = screen
      .getAllByTestId('workflow-graph-node')
      .find(n => n.getAttribute('data-node-state') === 'unknown')!
    expect(marker.getAttribute('data-node-resolved')).toBe('2')
    expect(marker.textContent).not.toContain('shape decided at run time')
    expect(marker.textContent).toContain('ran 2')
  })

  it('says a partial plan is partial without naming the preview', async () => {
    await initI18n()
    renderWithProviders(
      <WorkflowRunGraph events={[]} plan={{ ...FENCED, truncated: true }} status="running" />,
    )
    const banner = screen.getByTestId('workflow-graph-truncated')
    expect(banner.textContent).toContain('too large to draw fully')
    expect(banner.textContent).not.toContain('ceiling')
  })
})

describe('a node a reader needs to read', () => {
  // The visible label is cut twice: once at MAX_LABEL, again by CSS `truncate`. Without
  // a title the nodes a reader most wants to inspect -- the ones that ran, and the one
  // that failed -- are the two with no way to see their own name.
  const LONG = 'patch the build script so the desktop matrix stops resolving twice'

  const PLAN_LONG: RunPlan = {
    phases: [
      {
        title: 'Fix',
        certain: true,
        nodes: [
          { kind: 'agent', label: LONG, certain: true },
          { kind: 'agent', label: 'later', certain: true },
        ],
      },
    ],
    truncated: false,
  }

  function nodeFor(label: string) {
    return screen
      .getAllByTestId('workflow-graph-node')
      .find(n => (n.getAttribute('title') ?? '').includes(label))
  }

  it('reveals the full name of a node whose label is clipped', async () => {
    await initI18n()
    renderWithProviders(
      <WorkflowRunGraph
        events={[
          ev('phase_started', { title: 'Fix' }, 0),
          ev('agent_started', { agent_id: 'a0', label: LONG, phase: 'Fix' }, 1),
          ev('agent_finished', { agent_id: 'a0', ok: true }, 2, '2026-09-18T10:00:03.000Z'),
        ]}
        plan={PLAN_LONG}
        status="running"
      />,
    )
    const ran = nodeFor(LONG)
    expect(ran).toBeDefined()
    expect(ran).toHaveAttribute('data-node-state', 'ran_ok')
    // The whole name, not the cut one: the reveal is the point.
    expect(ran!.getAttribute('title')).toContain(LONG)
  })

  it('points a failed node at the view that holds the detail', async () => {
    await initI18n()
    renderWithProviders(
      <WorkflowRunGraph
        events={[
          ev('phase_started', { title: 'Fix' }, 0),
          ev('agent_started', { agent_id: 'a0', label: LONG, phase: 'Fix' }, 1),
          ev('agent_finished', { agent_id: 'a0', ok: false }, 2, '2026-09-18T10:00:03.000Z'),
        ]}
        plan={PLAN_LONG}
        status="failed"
      />,
    )
    const failed = nodeFor(LONG)
    expect(failed).toHaveAttribute('data-node-state', 'ran_failed')
    const title = failed!.getAttribute('title')!
    expect(title).toContain(LONG)
    // Named so the pointer matches the button on screen, and a pointer only: the graph
    // stays inert, so this must not become a control.
    expect(title).toContain('Open Tree for the detail.')
    expect(failed!.querySelector('button')).toBeNull()
    // Hover text reaches neither a touch nor a keyboard reader, so the same sentence is
    // also on the picture. Without this the red node is a dead end for both of them.
    expect(screen.getByTestId('workflow-graph-failed-hint')).toHaveTextContent(
      'Open Tree for the detail.',
    )
  })

  it('shows no failure hint when nothing failed', async () => {
    // The complement: a hint standing over a healthy run would read as a failure that
    // is not there, which is the opposite defect.
    await initI18n()
    renderWithProviders(
      <WorkflowRunGraph
        events={[
          ev('phase_started', { title: 'Fix' }, 0),
          ev('agent_started', { agent_id: 'a0', label: LONG, phase: 'Fix' }, 1),
          ev('agent_finished', { agent_id: 'a0', ok: true }, 2, '2026-09-18T10:00:03.000Z'),
        ]}
        plan={PLAN_LONG}
        status="running"
      />,
    )
    expect(screen.queryByTestId('workflow-graph-failed-hint')).toBeNull()
  })

  it('still gives a planned node its own hint', async () => {
    await initI18n()
    renderWithProviders(
      <WorkflowRunGraph
        events={[ev('phase_started', { title: 'Fix' }, 0)]}
        plan={PLAN_LONG}
        status="running"
      />,
    )
    const planned = nodeFor('later')
    expect(planned).toHaveAttribute('data-node-state', 'planned')
    expect(planned!.getAttribute('title')).toContain('planned')
  })
})
