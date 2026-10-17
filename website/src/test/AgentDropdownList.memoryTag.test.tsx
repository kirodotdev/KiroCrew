/**
 * Agent picker — telling a same-name crewmate and custom agent apart (#19472).
 *
 * Since #19304 a crewmate with its own memory can sit in the picker next to a
 * custom agent (template) of the same name. Both rows read the same name, so
 * each one's second line says which memory the pick runs on. A name with one row
 * has nothing to tell apart and carries no tag.
 *
 * The Default badge and the "default for new sessions" footer must point at the
 * same row of such a pair: the default for new sessions is a custom agent, so the
 * badge sits on the template row, and the footer names the default only from a
 * template chat (a crewmate chat shows no footer at all).
 */
import { describe, it, expect, vi, beforeAll } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import AgentDropdownList, { DefaultAgentRow, isCrewmateChat, type AgentItem } from '../components/AgentDropdownList'
import '../i18n/all'

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})

const PAIR: AgentItem[] = [
  { name: 'reviewer', source: 'kirocrew', selection_kind: 'template', description: 'Reviews code' },
  { name: 'reviewer', source: 'kirocrew', selection_kind: 'member', memory_store: 'member-reviewer', description: 'Reviews code' },
  { name: 'writer', source: 'kirocrew', selection_kind: 'template' },
]

function rowOf(kind: 'member' | 'template', name: string): HTMLElement {
  const group = screen.getByRole('group', { name: kind === 'member' ? 'Crewmates' : 'Custom agents' })
  const row = within(group).getAllByRole('option').find(o => o.textContent?.includes(name))
  if (!row) throw new Error(`no ${kind} row for ${name}`)
  return row
}

const tagOf = (row: HTMLElement) => row.querySelector('[data-testid="agent-row-memory-tag"]')?.textContent ?? null

describe('AgentDropdownList memory tag (#19472)', () => {
  it('says "Own memory" on the crewmate and "Shared memory" on the custom agent', () => {
    render(<AgentDropdownList agents={PAIR} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(tagOf(rowOf('member', 'reviewer'))).toBe('Own memory')
    expect(tagOf(rowOf('template', 'reviewer'))).toBe('Shared memory')
  })

  it('puts the tag on the second line, ahead of the description', () => {
    render(<AgentDropdownList agents={PAIR} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    const row = rowOf('member', 'reviewer')
    const lines = [...row.children].map(el => el.textContent)
    expect(lines[1]).toBe('Own memory')
    expect(lines[2]).toBe('Reviews code')
  })

  it('shows no tag on a name with one row', () => {
    render(<AgentDropdownList agents={PAIR} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(tagOf(rowOf('template', 'writer'))).toBeNull()
  })

  it('shows no tag anywhere when no name is shared', () => {
    const agents: AgentItem[] = [
      { name: 'reviewer', source: 'kirocrew', selection_kind: 'member', memory_store: 'member-reviewer' },
      { name: 'writer', source: 'kirocrew', selection_kind: 'template' },
    ]
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(document.querySelector('[data-testid="agent-row-memory-tag"]')).toBeNull()
  })

  it('shows no tag on a flat, name-only roster', () => {
    const agents: AgentItem[] = [{ name: 'reviewer', source: 'kirocrew' }, { name: 'writer', source: 'builtin' }]
    render(<AgentDropdownList agents={agents} activeAgent="" defaultAgent="" onSelect={() => {}} />)
    expect(document.querySelector('[data-testid="agent-row-memory-tag"]')).toBeNull()
  })
})

describe('Default badge and default footer agree on a same-name pair (#19472)', () => {
  const badgeOf = (row: HTMLElement) => row.querySelector('[title="New sessions start with this custom agent"]')

  it('puts the Default badge on the custom-agent row only', () => {
    render(<AgentDropdownList agents={PAIR} activeAgent="reviewer" activeKind="template" defaultAgent="reviewer" onSelect={() => {}} />)
    expect(badgeOf(rowOf('template', 'reviewer'))).not.toBeNull()
    expect(badgeOf(rowOf('member', 'reviewer'))).toBeNull()
  })

  it('names the default in the footer only from the custom-agent chat the badge marks', () => {
    // The hosts render the footer only when the chat is not a crewmate's.
    expect(isCrewmateChat('template', 'reviewer', PAIR)).toBe(false)
    expect(isCrewmateChat('member', 'reviewer', PAIR)).toBe(true)
    // A kind-less chat runs the crewmate (name-first), so it shows no footer either.
    expect(isCrewmateChat(undefined, 'reviewer', PAIR)).toBe(true)

    render(<DefaultAgentRow agentName="reviewer" isDefault onSetDefault={() => {}} />)
    expect(screen.getByTestId('agent-dropdown-default-state')).toHaveTextContent('reviewer is the default for new sessions')
  })

  it('says which reviewer the footer names when the name is shared', () => {
    render(<DefaultAgentRow agentName="reviewer" isDefault memoryTag="shared" onSetDefault={() => {}} />)
    expect(screen.getByTestId('agent-dropdown-default-state'))
      .toHaveTextContent('reviewer · shared memory is the default for new sessions')
  })

  it('marks the set-default offer the same way', () => {
    render(<DefaultAgentRow agentName="reviewer" isDefault={false} memoryTag="shared" onSetDefault={() => {}} />)
    expect(screen.getByRole('button')).toHaveTextContent('Set reviewer · shared memory as the default for new sessions')
  })
})
