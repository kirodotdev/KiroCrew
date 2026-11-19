/** The Dynamic Dashboard's Questions tab lists the asks the scope's agents
 *  pinned, counts them in the tab, and answers them through the same stack. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { createTestStore, renderWithProviders } from './helpers'
import CommandCenterPanel from '../pages/chat/command-center/CommandCenterPanel'
import { __resetAnswerIdsForTests } from '../components/waitingOnYou/answerIds'
import type { WaitingOnYouAsk } from '../types'
import { i18nT } from '../i18n/t'
import { registerMainComposer } from '../utils/composerRestore'

vi.mock('../pages/members/CrewDynamicDashboard', () => ({ default: () => <div data-testid="session-dynamic-dashboard" /> }))

const ASK: WaitingOnYouAsk = {
  key: 'pick-branch', ask_id: 'wy-7', title: 'Pick the release branch', detail: '',
  options: [{ label: 'main', description: '' }, { label: 'release-2', description: '' }], ts: 1795550000, state: 'pinned',
}

function store(asks: WaitingOnYouAsk[]) {
  const initial = createTestStore().getState()
  return createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slots: [
    { key: 'worker', title: 'Review worker', created_by: 'root', messages: 0, running: false, waiting_on_you: asks },
  ] } })
}

function rootStore(asks: WaitingOnYouAsk[]) {
  const initial = createTestStore().getState()
  return createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slots: [
    { key: 'root', title: 'Release', messages: 0, running: false, waiting_on_you: [] },
    { key: 'worker', title: 'Review worker', created_by: 'root', messages: 0, running: false, waiting_on_you: asks },
  ] } })
}

describe('Questions tab: pinned asks', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(480)
    vi.spyOn(api, 'pendingQuestions').mockResolvedValue([])
    vi.spyOn(api, 'approvals').mockResolvedValue([])
    vi.spyOn(api, 'workflowRuns').mockResolvedValue({ runs: [] })
    vi.spyOn(api, 'viewerOwnerOk').mockResolvedValue(true)
  })
  afterEach(() => __resetAnswerIdsForTests())

  it('lists a session\'s pinned ask, counts it, and answers it', async () => {
    const answer = vi.spyOn(api, 'answerWaitingOnYou').mockResolvedValue({ ok: true, outcome: 'sent', answer_id: 'x' })
    renderWithProviders(<CommandCenterPanel slot="worker" active />, { store: store([ASK]) })
    const listed = await screen.findByTestId('command-center-waiting')
    expect(listed).toHaveTextContent('Review worker')
    expect(within(listed).getByTestId('waiting-on-you-title')).toHaveTextContent('Pick the release branch')
    expect(screen.queryByText(i18nT('commandCenter.no_input'))).toBeNull()
    expect(screen.getByRole('radio', { name: /Questions/ })).toHaveTextContent('1')
    fireEvent.click(within(listed).getAllByTestId('waiting-on-you-option')[1])
    fireEvent.click(within(listed).getByTestId('waiting-on-you-send'))
    await waitFor(() => expect(answer).toHaveBeenCalledWith('worker', 'pick-branch', expect.objectContaining({ ask_id: 'wy-7', label: 'release-2' })))
  })

  it('returns a stale choice to the ask session draft', async () => {
    vi.spyOn(api, 'answerWaitingOnYou').mockRejectedValue(new ApiError(
      409,
      'superseded',
      JSON.stringify({ ok: false, code: 'superseded' }),
    ))
    const restored: Array<[string, string]> = []
    const unregister = registerMainComposer((slot, text) => {
      restored.push([slot, text])
      return true
    })
    try {
      renderWithProviders(<CommandCenterPanel slot="root" active />, { store: rootStore([ASK]) })
      fireEvent.click(screen.getByRole('radio', { name: /Questions/ }))
      const listed = await screen.findByTestId('command-center-waiting')
      fireEvent.click(within(listed).getAllByTestId('waiting-on-you-option')[1])
      fireEvent.click(within(listed).getByTestId('waiting-on-you-send'))

      await waitFor(() => expect(restored).toEqual([['worker', 'release-2']]))
      expect(within(listed).getByTestId('waiting-on-you-stale')).toHaveTextContent(
        'Your answer is in the message box. Nothing was sent.',
      )
    } finally {
      unregister()
    }
  })

  it('lists nothing for a scope with no pinned asks', async () => {
    renderWithProviders(<CommandCenterPanel slot="worker" active />, { store: store([]) })
    expect(await screen.findByText(i18nT('commandCenter.no_input'))).toBeInTheDocument()
    expect(screen.queryByTestId('command-center-waiting')).toBeNull()
  })
})
