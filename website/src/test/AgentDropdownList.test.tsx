import { describe, it, expect, vi, beforeAll } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import AgentDropdownList from '../components/AgentDropdownList'
import type { AgentItem } from '../components/AgentDropdownList'

// jsdom doesn't implement scrollIntoView
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})

const globalAgents: AgentItem[] = [
  { name: 'kirocrew', source: 'kirocrew', description: 'Main agent' },
  { name: 'builtin', source: 'builtin' },
]

const projectAgents: AgentItem[] = [
  { name: 'qbr', source: 'project', project_path: '/home/user/QBR', description: 'QBR agent' },
  { name: 'innovia', source: 'project', project_path: '/home/user/Innovia' },
]

const mixed = [...projectAgents, ...globalAgents]

describe('AgentDropdownList', () => {
  it('renders only Global Agents section when no project agents', () => {
    render(<AgentDropdownList agents={globalAgents} activeAgent="kirocrew" defaultAgent="kirocrew" onSelect={() => {}} />)
    expect(screen.queryByText('Project Agents')).not.toBeInTheDocument()
    expect(screen.getAllByText('kirocrew').length).toBeGreaterThan(0)
    expect(screen.getAllByText('builtin').length).toBeGreaterThan(0)
  })

  it('renders Project Agents section when project agents present', () => {
    render(<AgentDropdownList agents={mixed} activeAgent="kirocrew" activeProjectPath="/home/user/QBR" defaultAgent="kirocrew" onSelect={() => {}} />)
    expect(screen.getByText('Project Agents')).toBeInTheDocument()
    expect(screen.getByText('Global Agents')).toBeInTheDocument()
  })

  it('shows divider only when both sections present', () => {
    const { container: c1 } = render(<AgentDropdownList agents={globalAgents} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(c1.querySelector('.border-t')).toBeNull()

    const { container: c2 } = render(<AgentDropdownList agents={mixed} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(c2.querySelector('.border-t')).toBeTruthy()
  })

  it('shows "No matches" when agents list is empty', () => {
    render(<AgentDropdownList agents={[]} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(screen.getByText('No matches')).toBeInTheDocument()
  })

  it('badge shows "project (FolderName)" for project agents', () => {
    render(<AgentDropdownList agents={projectAgents} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(screen.getByText(/project \(QBR\)/)).toBeInTheDocument()
    expect(screen.getByText(/project \(Innovia\)/)).toBeInTheDocument()
  })

  it('current-project agent appears first in project agents section', () => {
    render(
      <AgentDropdownList
        agents={mixed}
        activeAgent=""
        activeProjectPath="/home/user/Innovia"
        defaultAgent=""
        onSelect={() => {}}
      />
    )
    // All buttons in the container -- innovia should appear before qbr (current project first)
    const buttons = document.querySelectorAll('button')
    const buttonTexts = Array.from(buttons).map(b => b.querySelector('.font-mono')?.textContent)
    const innoviaIdx = buttonTexts.indexOf('innovia')
    const qbrIdx = buttonTexts.indexOf('qbr')
    expect(innoviaIdx).toBeGreaterThanOrEqual(0)
    expect(innoviaIdx).toBeLessThan(qbrIdx)
  })

  it('calls onSelect with name and project_path when project agent clicked', () => {
    const onSelect = vi.fn()
    render(<AgentDropdownList agents={projectAgents} activeAgent="" activeProjectPath="/home/user/QBR" defaultAgent="" onSelect={onSelect} />)
    fireEvent.click(screen.getByText('qbr').closest('button')!)
    expect(onSelect).toHaveBeenCalledWith('qbr', '/home/user/QBR')
  })

  it('calls onSelect with name and undefined project_path for global agents', () => {
    const onSelect = vi.fn()
    render(<AgentDropdownList agents={globalAgents} activeAgent="" defaultAgent="" onSelect={onSelect} />)
    // Find button by font-mono span content
    const btn = Array.from(document.querySelectorAll('button')).find(
      b => b.querySelector('.font-mono')?.textContent === 'kirocrew'
    )
    fireEvent.click(btn!)
    expect(onSelect).toHaveBeenCalledWith('kirocrew', undefined)
  })

  it('shows description when present', () => {
    render(<AgentDropdownList agents={projectAgents} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(screen.getByText('QBR agent')).toBeInTheDocument()
  })

  it('project agents sorted: current project first, rest alphabetical', () => {
    const agents: AgentItem[] = [
      { name: 'z-agent', source: 'project', project_path: '/home/user/ZProject' },
      { name: 'a-agent', source: 'project', project_path: '/home/user/AProject' },
      { name: 'current', source: 'project', project_path: '/home/user/Current' },
    ]
    render(
      <AgentDropdownList
        agents={agents}
        activeAgent=""
        activeProjectPath="/home/user/Current"
        defaultAgent=""
        onSelect={() => {}}
      />
    )
    const buttons = document.querySelectorAll('button')
    const names = Array.from(buttons).map(b => b.querySelector('.font-mono')?.textContent)
    expect(names[0]).toBe('current')
    expect(names[1]).toBe('a-agent')
    expect(names[2]).toBe('z-agent')
  })
})
