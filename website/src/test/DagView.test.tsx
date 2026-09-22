import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
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
    // The node card and the legend row share one word per status.
    expect(screen.getAllByText('Done')).toHaveLength(2)
    expect(screen.getAllByText('Approval gate ahead')).toHaveLength(2)
    expect(screen.getByText('Needs Approval')).toBeInTheDocument()
  })

  it('shows empty state when no nodes', () => {
    render(<DagView nodes={[]} edges={[]} onNodeClick={() => {}} />)
    expect(screen.getByText(/No tasks to visualize/)).toBeInTheDocument()
  })

  it('renders selected node indicator', () => {
    const nodes = [{ id: '1', title: 'Setup', status: 'passed' }]
    const { container } = render(<DagView nodes={nodes} edges={[]} onNodeClick={() => {}} selectedId="1" />)
    const ring = container.querySelector('rect[stroke="var(--accent)"]')
    expect(ring).toBeInTheDocument()
  })

  it('renders pending edit dot', () => {
    const nodes = [{ id: '1', title: 'Setup', status: 'passed' }]
    const { container } = render(<DagView nodes={nodes} edges={[]} onNodeClick={() => {}} pendingEditIds={new Set(['1'])} />)
    const pendingDot = container.querySelector('circle[fill="var(--warn)"]')
    expect(pendingDot).toBeInTheDocument()
  })

  describe('approval states', () => {
    // Task 2 is in_progress and the executor has parked it at its approval gate
    // (an approvalMap entry); task 3 has a gate it has not reached yet.
    const nodes = [
      { id: '1', title: 'Plan', status: 'passed' },
      { id: '2', title: 'Implement', status: 'in_progress', requires_approval: true },
      { id: '3', title: 'Deploy', status: 'pending', requires_approval: true },
    ]
    const edges = [{ from: '1', to: '2' }, { from: '2', to: '3' }]

    it('names the node that is waiting for a decision, and puts Approve / Deny on it', () => {
      const onApprove = vi.fn()
      const { container } = render(
        <DagView nodes={nodes} edges={edges} onNodeClick={() => {}} approvalMap={{ 2: 'appr-1' }} onApprove={onApprove} />,
      )
      const cards = Array.from(container.querySelectorAll('svg g.cursor-pointer'))
      const implement = cards.find(g => g.textContent?.includes('Implement'))!
      const deploy = cards.find(g => g.textContent?.includes('Deploy'))!

      expect(implement.querySelector('text')?.textContent).toBe('Needs Approval')
      const halo = implement.querySelector('rect[stroke="var(--warn)"][stroke-width="3"]')!
      expect(halo).toBeInTheDocument()
      expect(implement.querySelectorAll('button')).toHaveLength(2)
      // The halo reaches past the card to enclose the button row (card 56 high, row at +60..+84).
      const row = implement.querySelector('foreignObject:has(button)')!
      const haloBottom = Number(halo.getAttribute('y')) + Number(halo.getAttribute('height'))
      expect(haloBottom).toBeGreaterThanOrEqual(Number(row.getAttribute('y')) + Number(row.getAttribute('height')))

      expect(deploy.querySelector('text')?.textContent).toBe('Approval gate ahead')
      expect(deploy.querySelector('rect[stroke="var(--warn)"]')).toBeNull()
      expect(deploy.querySelectorAll('button')).toHaveLength(0)

      // The buttons act on the node they sit under.
      fireEvent.click(screen.getByRole('button', { name: /Approve/ }))
      expect(onApprove).toHaveBeenCalledWith(2, 'approve')
      fireEvent.click(screen.getByRole('button', { name: /Deny/ }))
      expect(onApprove).toHaveBeenCalledWith(2, 'reject')
    })

    it('reads a running node as running until an approval is actually pending', () => {
      const { container } = render(<DagView nodes={nodes} edges={edges} onNodeClick={() => {}} approvalMap={{}} onApprove={() => {}} />)
      const implement = Array.from(container.querySelectorAll('svg g.cursor-pointer')).find(g => g.textContent?.includes('Implement'))!
      expect(implement.querySelector('text')?.textContent).toBe('Running')
      expect(implement.querySelectorAll('button')).toHaveLength(0)
      expect(screen.queryByText('Needs Approval')).not.toBeNull() // legend row only
      expect(screen.getAllByText('Needs Approval')).toHaveLength(1)
    })
  })
})
