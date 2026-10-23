/**
 * The Customize > Crewmates roster shows display names and labels for a
 * crewmate's bindings, in both the card and the list view: "Built from" names
 * the template (the worker kit's, for a crewmate that owns its file), and the
 * memory store reads as whose it is.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18nT } from '../i18n/t'
import { ApiError } from '../api/apiError'
import type { KiroCrewAgent } from '../components/AgentSelector'

const mockKit = vi.hoisted(() => ({ get: vi.fn(), preview: vi.fn(), apply: vi.fn() }))
vi.mock('../api/crewCapabilities', async (orig) => {
  const real = await orig<typeof import('../api/crewCapabilities')>()
  return { ...real, crewWorkerKitApi: mockKit }
})

// The hand-off is stubbed to show whether it was handed the request's report.
vi.mock('../components/AskAgentButton', () => ({
  default: ({ report }: { report?: { id?: string } }) => <span data-testid="ask-agent-stub" data-report={report?.id ?? ''} />,
  handoffErrorToAgent: () => true,
}))
const mockReportForError = vi.hoisted(() => vi.fn())
vi.mock('../utils/errorReport', async (orig) => {
  const real = await orig<typeof import('../utils/errorReport')>()
  return { ...real, reportForError: mockReportForError }
})

import { CrewRosterList, type CrewView } from './KiroCrewAgentsPageRoster'

const ADA: KiroCrewAgent = {
  name: 'ada', display_name: 'Ada', kiro_agent: 'ada', workspace: 'member-ada',
  memory_store: 'member-ada-ae2d738f', source: 'kirocrew',
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

beforeEach(() => {
  mockKit.get.mockReset()
})

describe.each<CrewView>(['cards', 'list'])('crew roster labels (%s)', view => {
  it('names the worker kit template and labels private memory for a crewmate that owns its file', async () => {
    mockKit.get.mockResolvedValue({ member: 'ada', template: 'release-helper', origin: 'release-helper', private_copy: false })
    renderRoster([ADA], view)
    const ada = holder('Ada', view)
    await waitFor(() => expect(builtFrom(ada)).toHaveTextContent(/^release-helper$/))
    expect(mockKit.get).toHaveBeenCalledWith('ada')
    expect(within(ada).getByText(i18nT('pages.kiroCrewAgentsPage.memory_label_private'))).toBeInTheDocument()
    expect(within(ada).queryByText('member-ada-ae2d738f')).not.toBeInTheDocument()
  })

  it('names a private copy by the template it was forked from', async () => {
    mockKit.get.mockResolvedValue({ member: 'ada', template: 'crew-5eed01', origin: 'release-helper', private_copy: true })
    renderRoster([ADA], view)
    const ada = holder('Ada', view)
    await waitFor(() => expect(builtFrom(ada)).toHaveTextContent(/^release-helper$/))
  })

  it('names the default template when the file was never copied from another', async () => {
    mockKit.get.mockResolvedValue({ member: 'ada', template: '', origin: '', private_copy: false })
    renderRoster([ADA], view)
    const ada = holder('Ada', view)
    const label = i18nT('pages.membersPage.built_from_default', { agent: 'kirocrew' })
    await waitFor(() => expect(builtFrom(ada)).toHaveTextContent(label))
  })

  it('shows a shared template by its name and shared memory as shared', async () => {
    renderRoster([OPS], view)
    const ops = holder('ops', view)
    expect(builtFrom(ops)).toHaveTextContent(/^oncall-agent$/)
    expect(within(ops).getByText(i18nT('pages.kiroCrewAgentsPage.memory_label_shared'))).toBeInTheDocument()
    expect(within(ops).queryByText('shared-mem')).not.toBeInTheDocument()
    expect(mockKit.get).not.toHaveBeenCalled()
  })

  it('shows a template that shares the crewmate key by that name', async () => {
    mockKit.get.mockRejectedValue(new ApiError(409, 'not_a_mate_file', JSON.stringify({ error: 'not_a_mate_file', code: 'not_a_mate_file' })))
    renderRoster([{ ...OPS, name: 'triage', kiro_agent: 'triage' } as KiroCrewAgent], view)
    const triage = holder('triage', view)
    await waitFor(() => expect(builtFrom(triage)).toHaveTextContent(/^triage$/))
  })

  it('shows a kit read failure as an error, not as the default template', async () => {
    mockKit.get.mockRejectedValue(new ApiError(500, 'invalid_spec', JSON.stringify({ error: 'invalid_spec', code: 'invalid_spec' })))
    renderRoster([ADA], view)
    const ada = holder('Ada', view)
    const notice = await within(ada).findByTestId('crew-built-from-error')
    expect(builtFrom(ada).textContent ?? '').not.toContain(i18nT('pages.membersPage.built_from_default', { agent: 'kirocrew' }))
    // The refusal's code reads as a sentence, never as the code itself.
    expect(notice).toHaveTextContent(i18nT('crewWorkerKit.error_invalid_spec'))
    expect(notice).not.toHaveTextContent('invalid_spec')
  })

  it('hands the agent the failed request, not just the translated sentence', async () => {
    const err = new ApiError(500, 'invalid_spec', JSON.stringify({ error: 'invalid_spec', code: 'invalid_spec' }))
    mockReportForError.mockImplementation((e: unknown) => (e === err ? { id: 'rep-ada-kit' } : undefined))
    mockKit.get.mockRejectedValue(err)
    renderRoster([ADA], view)
    const ada = holder('Ada', view)
    const stub = await within(ada).findByTestId('ask-agent-stub')
    expect(stub).toHaveAttribute('data-report', 'rep-ada-kit')
  })
})
