/**
 * #18239: the chat agent picker must keep the built-in `default` member
 * selectable whatever agent the chat currently runs, so a switch away from it
 * can be reversed from the same picker.
 *
 * The roster hook withholds a member row a listed template already covers
 * (`withoutCoveredCrewmates`), and the built-in `default` crew -- no memory of
 * its own, running the listed `kirocrew` template -- fell under that rule. The
 * trigger chip still read `default` while the chat ran it, so the missing row
 * went unnoticed until the user switched to another agent and searched for
 * `default` to switch back: the pop-up answered "No matches". The member named
 * as the default now stays unless a listed template shares its name, in which
 * case that template row already answers a search for the name.
 *
 * The harness is the chat picker's own pieces, wired the way the composer
 * wires them: `useAgents` (the catalog read) feeds `useFilteredDropdown` (the
 * filter box) which feeds `AgentDropdownList` (the rows and the empty state).
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest'
import { screen, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { useAgents, withoutCoveredCrewmates } from '../hooks/useAgents'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import AgentDropdownList from '../components/AgentDropdownList'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    agentCatalog: vi.fn(),
    kirocrewConfig: vi.fn(),
  },
}))

// happy-dom does not implement scrollIntoView, which the list calls on mount.
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})

// A stock install: the built-in `default` crew on the shared memory, running the
// primary `kirocrew` template, plus the installed templates a chat can run as.
const catalog = [
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'The stock crew agent', source: 'kirocrew', selection_kind: 'member' },
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Main agent', source: 'builtin', selection_kind: 'template' },
  { name: 'pm', kiro_agent: 'pm', workspace: 'default', memory_store: 'default', description: 'Product manager', source: 'package', selection_kind: 'template' },
]

const agentsApi = vi.mocked(api.agentCatalog)
const configApi = vi.mocked(api.kirocrewConfig)

let setFilter: (value: string) => void = () => {}

/** The composer's agent pop-up, reduced to the three pieces that decide its rows. */
function Picker({ activeAgent, activeKind }: { activeAgent: string; activeKind?: 'member' | 'template' }) {
  const { choices, defaultAgent } = useAgents(0, 'chat-1')
  const dd = useFilteredDropdown(choices)
  setFilter = dd.setFilter
  return (
    <div role="listbox" aria-label="Agent list">
      <AgentDropdownList agents={dd.filtered} activeAgent={activeAgent} activeKind={activeKind} defaultAgent={defaultAgent} onSelect={() => {}} filter={dd.filter} />
    </div>
  )
}

describe('chat agent picker keeps the built-in default selectable (#18239)', () => {
  beforeEach(() => {
    agentsApi.mockReset()
    configApi.mockReset()
    agentsApi.mockResolvedValue({ agents: catalog, default_agent: 'default' } as never)
    // The stock config: crewmates are not listed in the picker.
    configApi.mockResolvedValue({} as never)
  })

  it('still offers `default` after the chat switched to another agent and the user searches for it', async () => {
    // 1. A chat bound to `default`.
    const { rerender } = renderWithProviders(<Picker activeAgent="default" activeKind="member" />)
    await waitFor(() => expect(screen.getAllByRole('option').length).toBeGreaterThan(0))

    // 2. The chat is switched to `pm` (what the picker shows afterwards).
    rerender(<Picker activeAgent="pm" activeKind="template" />)
    await waitFor(() => expect(screen.getByRole('option', { name: /pm/ })).toHaveAttribute('aria-selected', 'true'))

    // 3-4. Reopen the picker and search for `default`.
    act(() => setFilter('default'))

    expect(screen.queryByText('No matches')).toBeNull()
    const row = screen.getByRole('option', { name: /default/ })
    expect(row).toHaveAttribute('aria-selected', 'false')
  })

  it('lists `default` beside the templates before any switch, so the row is not a switch-time artefact', async () => {
    renderWithProviders(<Picker activeAgent="default" activeKind="member" />)
    await waitFor(() => expect(screen.getAllByRole('option').length).toBeGreaterThan(0))

    const names = screen.getAllByRole('option').map(o => o.querySelector('.font-mono')?.textContent)
    expect(names).toContain('default')
    // The row the chat currently runs lights up, and it is the one wearing the default badge.
    const row = screen.getByRole('option', { name: /default/ })
    expect(row).toHaveAttribute('aria-selected', 'true')
    expect(row.querySelector('[title="New sessions start with this crewmate"]')).not.toBeNull()
  })
})

describe('withoutCoveredCrewmates never withholds the default agent', () => {
  it('keeps the member named as the default even though a listed template covers it', () => {
    const rows = withoutCoveredCrewmates(catalog as never, 'default')
    expect(rows.map(r => [r.selection_kind, r.name])).toEqual([
      ['member', 'default'],
      ['template', 'kirocrew'],
      ['template', 'pm'],
    ])
  })

  it('still withholds an identity-less crewmate that is not the default', () => {
    const helper = { name: 'helper', kiro_agent: 'pm', workspace: 'default', memory_store: 'default', description: '', source: 'kirocrew', selection_kind: 'member' }
    const rows = withoutCoveredCrewmates([...catalog, helper] as never, 'default')
    expect(rows.map(r => r.name)).toEqual(['default', 'kirocrew', 'pm'])
  })

  it('does not list a default alias beside the same-named template that already answers for it', () => {
    // "Set pm as default" enrols a `pm` crewmate on the shared memory running the
    // `pm` template. A search for `pm` finds the template row, so the alias stays
    // withheld: listing it too would show one agent twice.
    const alias = { name: 'pm', kiro_agent: 'pm', workspace: 'default', memory_store: 'default', description: '', source: 'kirocrew', selection_kind: 'member' }
    const rows = withoutCoveredCrewmates([...catalog, alias] as never, 'pm')
    expect(rows.map(r => [r.selection_kind, r.name])).toEqual([
      ['template', 'kirocrew'],
      ['template', 'pm'],
    ])
  })
})
