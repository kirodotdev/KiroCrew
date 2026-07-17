import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import AgentSelector from '../components/AgentSelector'
import type { KiroCrewAgent } from '../components/AgentSelector'

const agents: KiroCrewAgent[] = [
  { name: 'coding', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Coding agent', source: 'kirocrew' },
  { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'oncall-kb', description: 'Oncall agent', source: 'kirocrew' },
  { name: 'research', kiro_agent: 'kirocrew', workspace: 'research', memory_store: 'research-mem', description: 'Research agent', source: 'kirocrew' },
]

describe('AgentSelector — keyboard navigation', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    delete (window as unknown as { matchMedia?: typeof window.matchMedia }).matchMedia
  })

  /** Force a mouse pointer so isTouchDevice() is false (input auto-focuses). */
  function mockMouse() {
    window.matchMedia = vi.fn().mockImplementation((q: string) => ({
      matches: /pointer:\s*fine|hover:\s*hover/.test(q), media: q, onchange: null,
      addListener: vi.fn(), removeListener: vi.fn(),
      addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
    }))
  }

  const flush = () => act(async () => { await new Promise(r => setTimeout(r, 5)) })
  const opt = (name: string) => screen.getByRole('option', { name: new RegExp(name) })

  function open(value = 'coding') {
    const onChange = vi.fn()
    render(<AgentSelector agents={agents} defaultAgent="coding" value={value} onChange={onChange} />)
    fireEvent.click(screen.getByLabelText('Switch agent'))
    return { onChange }
  }

  it('auto-focuses the filter input on open', async () => {
    mockMouse()
    open()
    await flush()
    expect(document.activeElement).toBe(screen.getByPlaceholderText('Type to filter…'))
  })

  it('ArrowDown from the filter input moves focus to the first option', async () => {
    mockMouse()
    open()
    await flush()
    fireEvent.keyDown(screen.getByPlaceholderText('Type to filter…'), { key: 'ArrowDown' })
    expect(document.activeElement).toBe(opt('coding'))
  })

  it('ArrowDown / ArrowUp rove between options', async () => {
    mockMouse()
    open()
    await flush()
    opt('coding').focus()
    fireEvent.keyDown(opt('coding'), { key: 'ArrowDown' })
    expect(document.activeElement).toBe(opt('oncall'))
    fireEvent.keyDown(opt('oncall'), { key: 'ArrowUp' })
    expect(document.activeElement).toBe(opt('coding'))
  })

  it('Escape closes and returns focus to the trigger', async () => {
    mockMouse()
    open()
    await flush()
    const trigger = screen.getByLabelText('Switch agent')
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(document.activeElement).toBe(trigger)
  })

  it('Enter in the filter input selects the sole remaining match', async () => {
    mockMouse()
    const { onChange } = open()
    await flush()
    const input = screen.getByPlaceholderText('Type to filter…')
    fireEvent.change(input, { target: { value: 'onc' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('oncall', undefined)
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('selecting an option returns focus to the trigger', async () => {
    mockMouse()
    const { onChange } = open()
    await flush()
    const trigger = screen.getByLabelText('Switch agent')
    fireEvent.click(opt('research'))
    expect(onChange).toHaveBeenCalledWith('research', undefined)
    expect(document.activeElement).toBe(trigger)
  })
})
