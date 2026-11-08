/**
 * The Customize > Crewmates roster shows display names and labels for a
 * crewmate's bindings, in both the card and the list view: "Built from" names
 * the shared template a binding points at, or a generic mate label for a
 * crewmate that owns its own file (its abilities are the sum of its base plus
 * additions, not one template). The memory store reads as whose it is.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18nT } from '../i18n/t'
import type { KiroCrewAgent } from '../components/AgentSelector'

import { CrewRosterList, type CrewView } from './KiroCrewAgentsPageRoster'

const ADA: KiroCrewAgent = {
  name: 'ada', display_name: 'Ada', kiro_agent: 'ada', workspace: 'member-ada',
  memory_store: 'member-ada-ae2d738f', source: 'kirocrew', owns_mate_file: true,
} as KiroCrewAgent
/** Key equal to a user-level agent name, left on that shared template: no mate file. */
const REVIEWER: KiroCrewAgent = {
  name: 'reviewer', kiro_agent: 'reviewer', workspace: 'reviewer', memory_store: 'shared-mem',
  source: 'kirocrew', owns_mate_file: false,
} as KiroCrewAgent
const OPS: KiroCrewAgent = {
  name: 'ops', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'shared-mem', source: 'kirocrew',
} as KiroCrewAgent
const STORES = {
  'member-ada-ae2d738f': { memory_version: 2, owner_member: 'ada' },
  'shared-mem': {},
}

function renderRoster(agents: KiroCrewAgent[], view: CrewView) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CrewRosterList
          agents={agents}
          filtered={agents}
          view={view}
          defaultAgent=""
          sharedKind={() => 'none'}
          memoryStores={STORES}
          openEdit={() => {}}
          openCreate={() => {}}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** The card, or the list row, that stands for crewmate `label`. */
function holder(label: string, view: CrewView): HTMLElement {
  const control = screen.getByRole('button', { name: i18nT('pages.kiroCrewAgentsPage.edit_crew_named', { name: label }) })
  return view === 'list' ? control.closest('tr')! : control
}

function builtFrom(scope: HTMLElement): HTMLElement {
  return within(scope).getByTestId('crew-built-from')
}

beforeEach(() => {})

describe.each<CrewView>(['cards', 'list'])('crew roster labels (%s)', view => {
  it('labels a crewmate that owns its file as a mate and marks private memory', () => {
    renderRoster([ADA], view)
    const ada = holder('Ada', view)
    expect(builtFrom(ada)).toHaveTextContent(i18nT('pages.membersPage.built_from_mate_label'))
    expect(within(ada).getByText(i18nT('pages.kiroCrewAgentsPage.memory_label_private'))).toBeInTheDocument()
    expect(within(ada).queryByText('member-ada-ae2d738f')).not.toBeInTheDocument()
  })

  it('names the shared template when the key matches but the server reports no mate file', () => {
    renderRoster([REVIEWER], view)
    const reviewer = holder('reviewer', view)
    expect(builtFrom(reviewer)).toHaveTextContent(/^reviewer$/)
    expect(builtFrom(reviewer)).not.toHaveTextContent(i18nT('pages.membersPage.built_from_mate_label'))
  })

  it('shows a shared template by its name and shared memory as shared', () => {
    renderRoster([OPS], view)
    const ops = holder('ops', view)
    expect(builtFrom(ops)).toHaveTextContent(/^oncall-agent$/)
    expect(within(ops).getByText(i18nT('pages.kiroCrewAgentsPage.memory_label_shared'))).toBeInTheDocument()
    expect(within(ops).queryByText('shared-mem')).not.toBeInTheDocument()
  })
})
