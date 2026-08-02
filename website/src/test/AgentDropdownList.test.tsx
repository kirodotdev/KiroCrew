import { describe, it, expect, vi, beforeAll } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import AgentDropdownList, { ManageAgentsFooter } from '../components/AgentDropdownList'
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

describe('AgentDropdownList default-agent affordance', () => {
  it('labels the default agent with a Default pill instead of its source badge', () => {
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="kirocrew" onSelect={() => {}} />)
    expect(screen.getByText('Default')).toBeInTheDocument()
  })

  it('renders no default toggle at all when onSetDefault is omitted', () => {
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="kirocrew" onSelect={() => {}} />)
    expect(screen.queryByLabelText('Start new sessions with this agent')).not.toBeInTheDocument()
  })

  it('names the global scope on the toggle rather than saying only "default"', () => {
    // An unqualified "Set as default" reads as session-scoped in a pop-up whose other
    // job is switching the agent for this session.
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="kirocrew" onSelect={() => {}} onSetDefault={() => {}} />)
    expect(screen.getByLabelText('Start new sessions with this agent')).toBeInTheDocument()
  })

  it('sets the default without also selecting the agent for this session', () => {
    const onSelect = vi.fn()
    const onSetDefault = vi.fn()
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="kirocrew" onSelect={onSelect} onSetDefault={onSetDefault} />)
    fireEvent.click(screen.getByLabelText('Start new sessions with this agent'))
    expect(onSetDefault).toHaveBeenCalledWith('builtin')
    // The row click handler must not also fire — picking an agent for one session
    // and changing the global default are different actions.
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('offers no toggle on the row that already holds the default', () => {
    // Clearing the default is destructive (the product ends up with none) and must not
    // hide behind the same gesture that sets one. Only the Templates page clears it.
    const onSetDefault = vi.fn()
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="kirocrew" onSelect={() => {}} onSetDefault={onSetDefault} />)
    // Two agents, one is the default, so exactly one toggle is offered.
    expect(screen.getAllByLabelText('Start new sessions with this agent')).toHaveLength(1)
  })

  it('activates the default toggle from the keyboard without selecting the row', () => {
    const onSelect = vi.fn()
    const onSetDefault = vi.fn()
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="" onSelect={onSelect} onSetDefault={onSetDefault} />)
    const toggles = screen.getAllByLabelText('Start new sessions with this agent')
    fireEvent.keyDown(toggles[0], { key: 'Enter' })
    expect(onSetDefault).toHaveBeenCalledWith('kirocrew')
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('enrols the toggle in the listbox roving-focus ring so a keyboard user can reach it', () => {
    // useListboxKeyboard moves focus across `[data-option],[role="option"]`. Without
    // data-option the control is pointer-only: it cannot take a tab stop of its own
    // because it is nested inside the option button.
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="" onSelect={() => {}} onSetDefault={() => {}} />)
    const toggles = screen.getAllByLabelText('Start new sessions with this agent')
    expect(toggles).toHaveLength(2)
    for (const t of toggles) expect(t).toHaveAttribute('data-option')
  })

  it('explains the two same-row markers rather than relying on colour alone', () => {
    render(<AgentDropdownList agents={agents} activeAgent="kirocrew" defaultAgent="kirocrew" onSelect={() => {}} />)
    expect(screen.getByTitle('New sessions start with this agent')).toBeInTheDocument()
    expect(screen.getByTitle('Active in this session')).toBeInTheDocument()
  })
})

describe('ManageAgentsFooter', () => {
  it('calls onManage when the link is activated', () => {
    const onManage = vi.fn()
    render(<ManageAgentsFooter onManage={onManage} />)
    fireEvent.click(screen.getByText('Manage agents…'))
    expect(onManage).toHaveBeenCalledTimes(1)
  })

  it('stays silent when the default-agent write succeeded', () => {
    render(<ManageAgentsFooter onManage={() => {}} />)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('surfaces a failed default-agent write instead of swallowing it', () => {
    // The write is fire-and-forget, so without this a rejected request looks exactly
    // like a successful one.
    render(<ManageAgentsFooter onManage={() => {}} error />)
    expect(screen.getByRole('alert')).toHaveTextContent('Could not change the default agent')
  })
})
