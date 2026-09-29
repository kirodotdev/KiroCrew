import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import AgentSelector from '../components/AgentSelector'
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

  describe('a crew with a display name', () => {
    const crews: KiroCrewAgent[] = [
      ...agents,
      {
        name: 'Release Writer',
        member_id: 'release-writer',
        display_name: 'Release Writer',
        kiro_agent: 'kirocrew',
        workspace: 'default',
        memory_store: 'member-release-writer',
      },
    ]

    it('dispatches the member_id, never the label, so a rename cannot strand the slot', () => {
      const onChange = vi.fn()
      render(<AgentSelector agents={crews} defaultAgent="coding" value="coding" onChange={onChange} />)
      fireEvent.click(screen.getByLabelText('Switch agent'))
      fireEvent.click(screen.getByText('Release Writer'))
      expect(onChange).toHaveBeenCalledWith('release-writer')
    })

    it('shows the label for a slot that stores the member_id', () => {
      render(<AgentSelector agents={crews} defaultAgent="coding" value="release-writer" onChange={() => {}} />)
      expect(screen.getByText('Release Writer')).toBeInTheDocument()
      fireEvent.click(screen.getByLabelText('Switch agent'))
      expect(screen.getByRole('option', { selected: true })).toHaveTextContent('Release Writer')
    })

    it('still matches an older slot that stored the label', () => {
      render(<AgentSelector agents={crews} defaultAgent="coding" value="Release Writer" onChange={() => {}} />)
      fireEvent.click(screen.getByLabelText('Switch agent'))
      expect(screen.getByRole('option', { selected: true })).toHaveTextContent('Release Writer')
    })

    it('keeps the member_id readable beside a label that covers it', () => {
      // `agent=` in spawn params, crons and the CLI all address the id, so the
      // row shows it next to the label -- the id, not the roster's `name`
      // (which is the label itself and would never differ from it).
      render(<AgentSelector agents={crews} defaultAgent="coding" value="coding" onChange={() => {}} />)
      fireEvent.click(screen.getByLabelText('Switch agent'))
      const row = screen.getByRole('option', { name: /Release Writer/ })
      expect(row).toHaveTextContent('release-writer')
      // A template row has one handle; no chip repeats it.
      expect(screen.getByRole('option', { name: /^coding/ })).not.toHaveTextContent(/coding.*coding/)
    })
  })
})
