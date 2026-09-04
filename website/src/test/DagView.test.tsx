import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import DagView from '../pages/aidlc/DagView'

describe('DagView', () => {
  it('renders nodes and legend', () => {
    const nodes = [
      { id: '1', title: 'Setup', status: 'passed', task_type: undefined },
      { id: '2', title: 'Build', status: 'pending', requires_approval: true },
    ]
    const edges = [{ from: '1', to: '2' }]
    render(<DagView nodes={nodes} edges={edges} onNodeClick={() => {}} />)
    expect(screen.getByText('Setup')).toBeInTheDocument()
    expect(screen.getByText('Build')).toBeInTheDocument()
    expect(screen.getByText('needs approval')).toBeInTheDocument()
    expect(screen.getByText(/Done/)).toBeInTheDocument()
    expect(screen.getByText(/Needs Approval/)).toBeInTheDocument()
  })

  it('shows empty state when no nodes', () => {
    render(<DagView nodes={[]} edges={[]} onNodeClick={() => {}} />)
    expect(screen.getByText(/No tasks to visualize/)).toBeInTheDocument()
  })

  it('renders selected node indicator', () => {
    const nodes = [{ id: '1', title: 'Setup', status: 'passed' }]
    const { container } = render(<DagView nodes={nodes} edges={[]} onNodeClick={() => {}} selectedId="1" />)
    const ring = container.querySelector('rect[stroke="var(--accent, #6366f1)"]')
    expect(ring).toBeInTheDocument()
  })

  it('renders pending edit dot', () => {
    const nodes = [{ id: '1', title: 'Setup', status: 'passed' }]
    const { container } = render(<DagView nodes={nodes} edges={[]} onNodeClick={() => {}} pendingEditIds={new Set(['1'])} />)
    const pendingDot = container.querySelector('circle[fill="#f59e32"]')
    expect(pendingDot).toBeInTheDocument()
  })
})
