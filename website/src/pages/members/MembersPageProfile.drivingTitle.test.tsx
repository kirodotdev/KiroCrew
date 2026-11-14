/**
 * The Profile card's Sessions tab names an untitled driving session
 * "Untitled session", never by its internal slot key (#20908), and a click on a
 * row expands it in place: only the recap's Open session button leaves the page,
 * through the side panel's unsaved-edits question.
 *
 * The card is rendered for real; only its heavy siblings are stubbed, and the
 * panel stub mounts the `sessionsBody` it is handed, which is the list under test.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'
import { createTestStore } from '../../test/helpers'

const crewWorkerRecap = vi.fn()
vi.mock('../../api/client', async (orig) => {
  const real = await orig<typeof import('../../api/client')>()
  return {
    ...real,
    api: {
      ...real.api,
      crewWorkers: () => Promise.reject(Object.assign(new Error('not found'), { status: 404 })),
      crewWorkerRecap: (root: string, worker: string) => crewWorkerRecap(root, worker),
    },
  }
})

vi.mock('./CrewProfilePanel', () => ({
  default: ({ sessionsBody }: { sessionsBody: ReactNode }) => <div>{sessionsBody}</div>,
}))
vi.mock('./CrewNotesTab', () => ({ default: () => null }))
vi.mock('./CrewGoals', () => ({ default: () => null, useMemberGoals: () => ({}) }))
vi.mock('./CrewProfileSettings', () => ({ default: () => null }))
vi.mock('../../components/CrewWakeSection', () => ({ default: () => null }))

import { MembersPageProfileCard } from './MembersPageProfile'

type MembersPageProfileCardProps = Parameters<typeof MembersPageProfileCard>[0]

type Slot = { key: string; title: string; created_by: string; mate_root?: string }

function mountCard(slots: Slot[], openSessionGuarded = vi.fn()) {
  const ref = { current: () => false }
  const confirmSessionLeave = vi.fn(() => Promise.resolve(true))
  const openSessionOnChatPage = vi.fn()
  const member = { name: 'mate', slot_key: 'chat-mate' }
  const props = {
    crewEditing: { profileRegistryQuery: { data: undefined, isError: false } },
    profileCard: { cardFaceRef: { current: null }, schedGuardRef: ref, schedAtStakeRef: ref, requestCloseProfile: vi.fn() },
    rosterView: { isRunning: () => false, activeView: member },
    thread: { activeSlot: '', freshStart: { isPending: false }, askFreshStart: vi.fn(), confirmedSlot: '' },
    sidePanel: {
      profileCardW: 400, activeMemberKey: 'mate', openCrewView: vi.fn(), openSessionGuarded,
      drivingSessions: slots, confirmSessionLeave, openSessionOnChatPage,
    },
    schedules: {
      schedAgentField: '', profileGoalsQuery: { data: undefined, isError: false },
      schedulesCountQuery: { isPending: false, isError: false }, schedulesForActive: [],
      setSchedDraftDirty: vi.fn(), setSchedSaving: vi.fn(), requestCancelSchedDraft: vi.fn(),
    },
    profileFlight: {
      nowTs: 0, reduceMotion: true, profileDocked: true, faceFlight: null,
      profileAutomation: null, profileMonitorReadState: undefined,
    },
    navigate: vi.fn(),
    leave: vi.fn(),
    setEditingCrew: vi.fn(),
    profile: { tab: 'sessions', openNonce: 0, placement: 'docked' },
    threadColumnRef: { current: null },
    slotsLoaded: true,
    active: member,
    activeSlug: '',
    activeMemberName: '',
    onGoalsDraftChange: vi.fn(),
  } as unknown as MembersPageProfileCardProps
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={createTestStore({ dashboard: { slots: [] } } as never)}>
      <QueryClientProvider client={qc}><MembersPageProfileCard {...props} /></QueryClientProvider>
    </Provider>,
  )
  return { openSessionGuarded, confirmSessionLeave, openSessionOnChatPage, navigate: props.navigate as unknown as ReturnType<typeof vi.fn> }
}

describe('MembersPageProfileCard Sessions tab rows', () => {
  it('labels an untitled session "Untitled session" and keeps a named one', async () => {
    mountCard([
      { key: 'chat-3-1785440411', title: '', created_by: 'mate', mate_root: 'mate' },
      { key: 'chat-named', title: 'Fix the parser', created_by: 'mate', mate_root: 'mate' },
    ])
    const rows = await screen.findAllByTestId('mate-session-row')
    const [untitled, named] = rows
    expect(untitled.textContent).toContain('Untitled session')
    expect(untitled.textContent).not.toContain('chat-3-1785440411')
    expect(named.textContent).toContain('Fix the parser')
  })

  it('expands the clicked row in place, and only its Open session button opens it', async () => {
    crewWorkerRecap.mockResolvedValue({
      key: 'chat-named', root: 'mate', title: 'Fix the parser', ledger_item: '', status: '', reason: '',
      state: '', summary: '', decision: '', pr: null, pr_url: '', artifacts: {}, last_update: '',
      open_approvals: 0, approvals: [], open_questions: 0, live: true,
    })
    const { openSessionGuarded, confirmSessionLeave, openSessionOnChatPage, navigate } = mountCard([{ key: 'chat-named', title: 'Fix the parser', created_by: 'mate', mate_root: 'mate' }])
    fireEvent.click(await screen.findByTestId('mate-session-toggle'))
    expect((await screen.findByTestId('mate-recap-empty')).textContent).toBe('No report yet')
    expect(crewWorkerRecap).toHaveBeenCalledWith('mate', 'chat-named')
    expect(openSessionGuarded).not.toHaveBeenCalled()
    expect(navigate).not.toHaveBeenCalled()
    expect(openSessionOnChatPage).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Open session: Fix the parser' }))
    await vi.waitFor(() => expect(openSessionOnChatPage).toHaveBeenCalledWith('chat-named'))
    expect(confirmSessionLeave).toHaveBeenCalledTimes(1)
  })
})
