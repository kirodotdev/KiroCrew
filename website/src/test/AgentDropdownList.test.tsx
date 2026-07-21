import { describe, it, expect, vi, beforeAll } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import AgentDropdownList from '../components/AgentDropdownList'
import type { AgentItem } from '../components/AgentDropdownList'

// jsdom doesn't implement scrollIntoView
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})

const agents: AgentItem[] = [
  { name: 'kirocrew', source: 'kirocrew', description: 'Main agent' },
  { name: 'builtin', source: 'builtin' },
]

describe('AgentDropdownList', () => {
  it('renders all agents', () => {
    render(<AgentDropdownList agents={agents} activeAgent="kirocrew" defaultAgent="kirocrew" onSelect={() => {}} />)
    expect(screen.getAllByText('kirocrew').length).toBeGreaterThan(0)
    expect(screen.getAllByText('builtin').length).toBeGreaterThan(0)
  })

  it('shows "No matches" when agents list is empty', () => {
    render(<AgentDropdownList agents={[]} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(screen.getByText('No matches')).toBeInTheDocument()
  })

  it('calls onSelect with the agent name when clicked', () => {
    const onSelect = vi.fn()
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="" onSelect={onSelect} />)
    const btn = Array.from(document.querySelectorAll('button')).find(
      b => b.querySelector('.font-mono')?.textContent === 'kirocrew'
    )
    fireEvent.click(btn!)
    expect(onSelect).toHaveBeenCalledWith('kirocrew')
  })

  it('shows description when present', () => {
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(screen.getByText('Main agent')).toBeInTheDocument()
  })
})
