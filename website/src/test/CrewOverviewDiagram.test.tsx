/**
 * Guards on the overview diagram's connectors.
 *
 * The property under test is the one the design rests on: the connectors are
 * DERIVED from the two columns' lengths. They are bordered elbows rather than SVG
 * paths (`use-lucide-icons` blocks inline SVG elements in a `.tsx`), so the
 * assertions read geometry off inline styles. A hand-positioned diagram has to be
 * redrawn for every new binding, and that is how a diagram silently stops
 * matching the data it claims to show — so "one more node yields one more curve,
 * with no edit to the component" is asserted mechanically rather than trusted.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Boxes, Clock, Cpu, Database, FolderOpen, ShieldCheck, Webhook } from 'lucide-react'

import CrewOverviewDiagram, { type CrewWireNode } from '../components/crew/CrewOverviewDiagram'

const INPUTS: CrewWireNode[] = [
  { key: 'schedules', icon: Clock, label: 'Schedules', value: '2' },
  { key: 'webhook', icon: Webhook, label: 'Webhook', value: 'Not bound to a crew', ghost: true },
]
const OUTPUTS: CrewWireNode[] = [
  { key: 'template', icon: Boxes, label: 'Agent Template', value: 'kirocrew', mono: true },
  { key: 'workspace', icon: FolderOpen, label: 'Workspace', value: 'oncall', mono: true },
  { key: 'memory', icon: Database, label: 'Memory Store', value: 'oncall-mem', mono: true },
  { key: 'model', icon: Cpu, label: 'Model', value: 'Inherited', muted: true },
]

function renderDiagram(inputs = INPUTS, outputs = OUTPUTS) {
  const { container } = render(
    <CrewOverviewDiagram
      inputs={inputs}
      outputs={outputs}
      inputsLabel="Who wakes it"
      outputsLabel="What it works with"
      hub={<span data-testid="hub" />}
      onNodeSelect={() => {}}
    />,
  )
  const fan = (side: 'in' | 'out') =>
    Array.from(container.querySelector(`[data-testid="crew-wire-fan-${side}"]`)
      ?.querySelectorAll('[data-connector]') ?? []) as HTMLElement[]
  return { container, inPaths: fan('in'), outPaths: fan('out') }
}

describe('crew overview diagram — connectors follow the data', () => {
  it('draws one connector per node on each side', () => {
    const { inPaths, outPaths } = renderDiagram()
    expect(inPaths).toHaveLength(INPUTS.length)
    expect(outPaths).toHaveLength(OUTPUTS.length)
  })

  it('gains a connector when a binding is added, with no change to the component', () => {
    const before = renderDiagram().outPaths.length
    const grown = [
      ...OUTPUTS,
      { key: 'perm', icon: ShieldCheck, label: 'Permission profile', value: 'oncall-limited' },
    ]
    const after = renderDiagram(INPUTS, grown).outPaths.length
    expect(after).toBe(before + 1)
  })

  it('re-fans rather than shifting: every connector spans a distinct band', () => {
    const { outPaths } = renderDiagram()
    const spans = outPaths.map(p => `${p.style.top}/${p.style.height}`)
    expect(new Set(spans).size).toBe(outPaths.length)
  })

  it('spans a band whose height follows the LONGER column', () => {
    // Both fans are computed against the same band, which is what lets a 2-node
    // and a 4-node column meet the hub at the right heights.
    const two = renderDiagram(INPUTS, OUTPUTS.slice(0, 2))
    const four = renderDiagram(INPUTS, OUTPUTS)
    const bandOf = (c: HTMLElement) =>
      (c.querySelector('[data-testid="crew-wire-fan-in"]') as HTMLElement).style.height
    expect(bandOf(two.container)).not.toBe(bandOf(four.container))
  })

  it('dashes the connector of a node that is drawn as a known gap', () => {
    const { inPaths } = renderDiagram()
    expect(inPaths[0].style.borderStyle).toBe('solid')
    expect(inPaths[1].style.borderStyle).toBe('dashed')
  })

  it('lands the horizontal leg on the node end, in BOTH directions', () => {
    // The regression this pins: choosing the leg from the run's direction rather
    // than from which end the node is on collapses the outbound fan onto one
    // trunk with no arm reaching any node — visible only in a screenshot.
    const { inPaths, outPaths } = renderDiagram()
    for (const [side, els] of [['in', inPaths], ['out', outPaths]] as const) {
      for (const el of els) {
        if (el.style.height === '0px') continue
        const edge = el.getAttribute('data-node-edge')
        const px = edge === 'top' ? el.style.borderTopWidth : el.style.borderBottomWidth
        expect(px, `${side} connector's leg sits on its node end`).not.toBe('')
      }
    }
  })

  it('carries no inline SVG, which the icon gate blocks in a .tsx', () => {
    const { container } = renderDiagram()
    const fans = container.querySelectorAll('[data-testid^="crew-wire-fan-"] svg')
    expect(fans).toHaveLength(0)
  })
})

describe('crew overview diagram — content', () => {
  it('labels both columns and renders the hub', () => {
    renderDiagram()
    expect(screen.getByText('Who wakes it')).toBeInTheDocument()
    expect(screen.getByText('What it works with')).toBeInTheDocument()
    expect(screen.getByTestId('hub')).toBeInTheDocument()
  })

  it('renders each node as its kind plus its current value', () => {
    renderDiagram()
    const node = screen.getByTestId('crew-wire-workspace')
    expect(node).toHaveTextContent('Workspace')
    expect(node).toHaveTextContent('oncall')
  })

  it('hides the connectors from assistive tech, since the text carries the meaning', () => {
    const { container } = renderDiagram()
    for (const side of ['in', 'out']) {
      expect(container.querySelector(`[data-testid="crew-wire-fan-${side}"]`))
        .toHaveAttribute('aria-hidden', 'true')
    }
  })

  it('theme colours the connectors through a variable, not a frozen literal', () => {
    // A data-URI or baked colour would not re-theme; borders inherit the palette.
    const { inPaths } = renderDiagram()
    expect(inPaths[0].style.borderColor).toContain('--aim')
  })

  it('survives an empty column without dividing by zero', () => {
    const { inPaths, outPaths } = renderDiagram([], OUTPUTS)
    expect(inPaths).toHaveLength(0)
    expect(outPaths).toHaveLength(OUTPUTS.length)
  })
})
