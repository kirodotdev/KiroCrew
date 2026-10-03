import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import AgentSelector, { crewDisplayName } from '../components/AgentSelector'
import type { KiroCrewAgent } from '../components/AgentSelector'

const agents: KiroCrewAgent[] = [
  { name: 'coding', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' },
  { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'oncall-kb' },
  { name: 'research', kiro_agent: 'kirocrew', workspace: 'research', memory_store: 'research-mem' },
]

describe('AgentSelector', () => {
  it('renders current agent name', () => {
    render(<AgentSelector agents={agents} defaultAgent="coding" value="coding" onChange={() => {}} />)
    expect(screen.getByText('coding')).toBeInTheDocument()
  })

  it('shows dropdown on click', () => {
    render(<AgentSelector agents={agents} defaultAgent="coding" value="coding" onChange={() => {}} />)
    fireEvent.click(screen.getByLabelText('Switch agent'))
    expect(screen.getByRole('listbox')).toBeInTheDocument()
  })

  it('displays kiro_agent as subtitle', () => {
    render(<AgentSelector agents={agents} defaultAgent="coding" value="coding" onChange={() => {}} />)
    fireEvent.click(screen.getByLabelText('Switch agent'))
    expect(screen.getByText('oncall-agent')).toBeInTheDocument()
  })

  it('marks default agent with badge', () => {
    render(<AgentSelector agents={agents} defaultAgent="coding" value="oncall" onChange={() => {}} />)
    fireEvent.click(screen.getByLabelText('Switch agent'))
    expect(screen.getByText('default')).toBeInTheDocument()
  })

  it('calls onChange with KiroCrew agent name on selection', () => {
    const onChange = vi.fn()
    render(<AgentSelector agents={agents} defaultAgent="coding" value="coding" onChange={onChange} />)
    fireEvent.click(screen.getByLabelText('Switch agent'))
    fireEvent.click(screen.getByText('oncall'))
    expect(onChange).toHaveBeenCalledWith('oncall')
  })

  it('uses defaultAgent when value is empty', () => {
    render(<AgentSelector agents={agents} defaultAgent="coding" value="" onChange={() => {}} />)
    expect(screen.getByText('coding')).toBeInTheDocument()
  })
})

describe('crewDisplayName', () => {
  it('names the built-in Assistant (assistant + kirocrew-assistant) by its localized label; default and look-alikes keep their name', () => {
    expect(crewDisplayName({ name: 'assistant', kiro_agent: 'kirocrew-assistant' })).toBe('Assistant')
    // A label of the user's own wins.
    expect(crewDisplayName({ name: 'assistant', kiro_agent: 'kirocrew-assistant', display_name: 'Ada' })).toBe('Ada')
    // The reserved default member is presented exactly as before.
    expect(crewDisplayName({ name: 'default', kiro_agent: 'kirocrew' })).toBe('default')
    expect(crewDisplayName({ name: 'default' })).toBe('default')
    // A member merely NAMED assistant on another template is not the Assistant.
    expect(crewDisplayName({ name: 'assistant', kiro_agent: 'kirocrew' })).toBe('assistant')
    expect(crewDisplayName({ name: 'assistant' })).toBe('assistant')
  })
})
