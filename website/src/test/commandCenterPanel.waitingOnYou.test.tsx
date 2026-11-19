/** The Dynamic Dashboard's Questions tab lists the asks the scope's agents
 *  pinned, counts them in the tab, and answers them through the same stack:
 *  a plain chat send to the ask's own session. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api } from '../api/client'
import { createTestStore, renderWithProviders } from './helpers'
import CommandCenterPanel from '../pages/chat/command-center/CommandCenterPanel'
import type { WaitingOnYouAsk } from '../types'
import { i18nT } from '../i18n/t'

const sendTurn = vi.hoisted(() => vi.fn())
vi.mock('../chat-core/transport/sendTurn', async importActual => ({
  ...(await importActual<typeof import('../chat-core/transport/sendTurn')>()),
  sendTurn,
}))
vi.mock('../pages/members/CrewDynamicDashboard', () => ({ default: () => <div data-testid="session-dynamic-dashboard" /> }))

const ASK: WaitingOnYouAsk = {
  key: 'pick-branch', ask_id: 'wy-7', title: 'Pick the release branch', detail: '',
  options: [{ label: 'main', description: '' }, { label: 'release-2', description: '' }], ts: 1795550000,
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
  afterEach(() => sendTurn.mockReset())

  it('lists a session\'s pinned ask, counts it, and answers it', async () => {
    sendTurn.mockResolvedValue({ status: 'queued', body: {} })
    renderWithProviders(<CommandCenterPanel slot="worker" active />, { store: store([ASK]) })
    const listed = await screen.findByTestId('command-center-waiting')
    expect(listed).toHaveTextContent('Review worker')
    expect(await within(listed).findByTestId('waiting-on-you-title', {}, { timeout: 5000 })).toHaveTextContent('Pick the release branch')
    expect(screen.queryByText(i18nT('commandCenter.no_input'))).toBeNull()
    expect(screen.getByRole('radio', { name: /Questions/ })).toHaveTextContent('1')
    fireEvent.click(within(listed).getAllByTestId('waiting-on-you-option')[1])
    fireEvent.click(within(listed).getByTestId('waiting-on-you-send'))
    await waitFor(() => expect(sendTurn).toHaveBeenCalledTimes(1))
    const [sent] = sendTurn.mock.calls[0]
    expect(sent).toMatchObject({ message: 'release-2', slot: 'worker', meta: { waitingOnYouAnswer: { key: 'pick-branch', ask_id: 'wy-7' } } })
    expect(sent).not.toHaveProperty('steer')
  })

  it('answers a worker\'s ask from the root panel with a send to the worker', async () => {
    sendTurn.mockResolvedValue({ status: 'refused', body: {}, reason: 'Someone else is typing.' })
    renderWithProviders(<CommandCenterPanel slot="root" active />, { store: rootStore([ASK]) })
    fireEvent.click(screen.getByRole('radio', { name: /Questions/ }))
    const listed = await screen.findByTestId('command-center-waiting')
    await within(listed).findAllByTestId('waiting-on-you-option', {}, { timeout: 5000 })
    fireEvent.click(within(listed).getAllByTestId('waiting-on-you-option')[1])
    fireEvent.click(within(listed).getByTestId('waiting-on-you-send'))
    await waitFor(() => expect(sendTurn).toHaveBeenCalledTimes(1))
    expect(sendTurn.mock.calls[0][0]).toMatchObject({ message: 'release-2', slot: 'worker' })
    expect(await within(listed).findByTestId('waiting-on-you-error')).toHaveTextContent('Someone else is typing.')
  })

  it('lists nothing for a scope with no pinned asks', async () => {
    renderWithProviders(<CommandCenterPanel slot="worker" active />, { store: store([]) })
    expect(await screen.findByText(i18nT('commandCenter.no_input'))).toBeInTheDocument()
    expect(screen.queryByTestId('command-center-waiting')).toBeNull()
  })
})
