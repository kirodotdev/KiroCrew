import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

const mockApi = vi.hoisted(() => ({ setAgentSpecPin: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import { ApiError } from '../api/apiError'
import { agentSpecAmbiguity } from '../pages/chat/AgentSpecChoiceCard'
import { ErrorCard } from '../pages/chat/ErrorCard'
import { i18nT } from '../i18n/t'

const meta = (over: Record<string, unknown> = {}) => ({
  kind: 'agent_spec_ambiguous',
  code: 'agent_spec_ambiguous',
  agent: 'helper',
  pin: null,
  claimants: [
    { file: 'PkgA-helper.json', mtime: 1_790_000_000, package: 'PkgA', description: 'Reviews pipelines', differs: ['description', 'tools'] },
    { file: 'PkgB-helper.json', mtime: null, package: null },
  ],
  ...over,
})

function renderCard(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

afterEach(() => {
  cleanup()
  mockApi.setAgentSpecPin.mockReset()
})

describe('agentSpecAmbiguity', () => {
  it('reads the payload from meta only, and refuses any other row', () => {
    expect(agentSpecAmbiguity({ meta: meta() })?.claimants.map(c => c.file)).toEqual(['PkgA-helper.json', 'PkgB-helper.json'])
    expect(agentSpecAmbiguity({ meta: { code: 'memory_unavailable' } })).toBeUndefined()
    expect(agentSpecAmbiguity({ meta: meta({ claimants: [] }) })).toBeUndefined()
    expect(agentSpecAmbiguity({ meta: meta({ agent: 7 }) })).toBeUndefined()
    expect(agentSpecAmbiguity({ meta: undefined })).toBeUndefined()
  })
})

describe('AgentSpecChoiceCard', () => {
  it('lists each file with its package and offers a labelled "Use this" per file', () => {
    renderCard(<ErrorCard content="2 specs declare the name 'helper'" meta={meta()} />)
    const list = screen.getByRole('list', { name: i18nT('pages.chat.errorCard.spec_choice_title', { agent: 'helper' }) })
    const items = within(list).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('PkgA-helper.json')
    expect(items[0]).toHaveTextContent(i18nT('pages.chat.errorCard.spec_choice_package', { package: 'PkgA' }))
    expect(items[0]).toHaveTextContent('Reviews pipelines')
    expect(items[0]).toHaveTextContent(i18nT('pages.chat.errorCard.spec_choice_differs', { fields: 'description, tools' }))
    expect(items[1]).toHaveTextContent(i18nT('pages.chat.errorCard.spec_choice_package_unknown'))
    expect(within(items[1]).queryByTestId('agent-spec-choice-differs')).toBeNull()
    expect(screen.getByRole('button', { name: i18nT('pages.chat.errorCard.spec_choice_use_label', { agent: 'helper', file: 'PkgB-helper.json' }) })).toBeEnabled()
  })

  it('says how many files the list leaves out', () => {
    renderCard(<ErrorCard content="x" meta={meta({ omitted: 3 })} />)
    expect(screen.getByTestId('agent-spec-choice-omitted')).toHaveTextContent(
      i18nT('pages.chat.errorCard.spec_choice_more', { count: 3, agent: 'helper' }),
    )
  })

  it('names the broken pin when the refusal is one', () => {
    renderCard(<ErrorCard content="x" meta={meta({ pin: 'Gone.json' })} />)
    expect(screen.getByTestId('error-card')).toHaveTextContent(
      i18nT('pages.chat.errorCard.spec_choice_broken', { agent: 'helper', file: 'Gone.json' }),
    )
  })

  it('pins the chosen file and then starts the session again through Resume', async () => {
    mockApi.setAgentSpecPin.mockResolvedValue({ ok: true, agent: 'helper', file: 'PkgB-helper.json' })
    const onContinue = vi.fn()
    renderCard(<ErrorCard content="x" meta={meta()} onContinue={onContinue} />)
    fireEvent.click(screen.getByRole('button', { name: i18nT('pages.chat.errorCard.spec_choice_use_label', { agent: 'helper', file: 'PkgB-helper.json' }) }))
    await waitFor(() => expect(onContinue).toHaveBeenCalledTimes(1))
    expect(mockApi.setAgentSpecPin).toHaveBeenCalledWith('helper', 'PkgB-helper.json')
    const done = i18nT('pages.chat.errorCard.spec_choice_done_resuming', { agent: 'helper', file: 'PkgB-helper.json' })
    expect(screen.getByTestId('agent-spec-choice-status')).toHaveTextContent(done)
    // Settled: the heading says what runs, only the chosen row stays, and no button is left.
    expect(screen.getByRole('list', { name: done })).toBeInTheDocument()
    expect(screen.getAllByTestId('agent-spec-choice')).toHaveLength(1)
    expect(screen.getByTestId('agent-spec-choice')).toHaveTextContent('PkgB-helper.json')
    expect(screen.queryByTestId('agent-spec-choice-use')).toBeNull()
  })

  it('on a settled row saves the pin and says to send again', async () => {
    mockApi.setAgentSpecPin.mockResolvedValue({ ok: true, agent: 'helper', file: 'PkgA-helper.json' })
    renderCard(<ErrorCard content="x" meta={meta()} />)
    fireEvent.click(screen.getAllByTestId('agent-spec-choice-use')[0])
    await waitFor(() =>
      expect(screen.getByTestId('agent-spec-choice-status')).toHaveTextContent(
        i18nT('pages.chat.errorCard.spec_choice_done', { agent: 'helper', file: 'PkgA-helper.json' }),
      ),
    )
  })

  it('shows a refused pin through the error notice and does not resume', async () => {
    mockApi.setAgentSpecPin.mockRejectedValue(new ApiError(403, 'Only the dashboard owner can do that.'))
    const onContinue = vi.fn()
    renderCard(<ErrorCard content="x" meta={meta()} onContinue={onContinue} />)
    fireEvent.click(screen.getAllByTestId('agent-spec-choice-use')[0])
    await waitFor(() => expect(screen.getByTestId('agent-spec-choice-error')).toHaveTextContent('Only the dashboard owner'))
    expect(onContinue).not.toHaveBeenCalled()
    for (const button of screen.getAllByTestId('agent-spec-choice-use')) expect(button).toBeEnabled()
  })
})
