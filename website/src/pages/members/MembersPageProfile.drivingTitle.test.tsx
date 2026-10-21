/**
 * The Profile card's Sessions tab names an untitled driving session
 * "Untitled session", never by its internal slot key (#20908).
 *
 * The card is rendered for real; only its heavy siblings are stubbed, and the
 * panel stub mounts the `sessionsBody` it is handed, which is the list under test.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ReactNode } from 'react'

vi.mock('./CrewProfilePanel', () => ({
  default: ({ sessionsBody }: { sessionsBody: ReactNode }) => <div>{sessionsBody}</div>,
}))
vi.mock('./CrewNotesTab', () => ({ default: () => null }))
vi.mock('./CrewGoals', () => ({ default: () => null, useMemberGoals: () => ({}) }))
vi.mock('./CrewProfileSettings', () => ({ default: () => null }))
vi.mock('../../components/CrewWakeSection', () => ({ default: () => null }))

import { MembersPageProfileCard } from './MembersPageProfile'

type MembersPageProfileCardProps = Parameters<typeof MembersPageProfileCard>[0]

type Slot = { key: string; title: string; created_by: string }

function mountCard(slots: Slot[], openSessionGuarded = vi.fn()) {
  const ref = { current: () => false }
  const member = { name: 'mate', slot_key: 'chat-mate' }
  const props = {
    crewEditing: { profileRegistryQuery: { data: undefined, isError: false } },
    profileCard: { cardFaceRef: { current: null }, schedGuardRef: ref, schedAtStakeRef: ref, requestCloseProfile: vi.fn() },
    rosterView: { isRunning: () => false, activeView: member },
    thread: { activeSlot: '', freshStart: { isPending: false }, askFreshStart: vi.fn(), confirmedSlot: '' },
    sidePanel: {
      profileCardW: 400, activeMemberKey: 'mate', openCrewView: vi.fn(), openSessionGuarded,
      drivingSessions: slots,
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
  render(<MembersPageProfileCard {...props} />)
  return openSessionGuarded
}

describe('MembersPageProfileCard driving-session rows', () => {
  it('labels an untitled session "Untitled session" and keeps a named one', () => {
    const open = mountCard([
      { key: 'chat-3-1785440411', title: '', created_by: 'mate' },
      { key: 'chat-named', title: 'Fix the parser', created_by: 'mate' },
    ])
    const [untitled, named] = screen.getAllByTestId('crew-profile-driving-row')
    expect(untitled.textContent).toContain('Untitled session')
    expect(untitled.textContent).not.toContain('chat-3-1785440411')
    expect(untitled.getAttribute('title')).toMatch(/^Untitled session · /)
    expect(named.textContent).toContain('Fix the parser')
    // Opening the row still targets the session's own key.
    fireEvent.click(untitled)
    expect(open).toHaveBeenCalledWith('chat-3-1785440411')
  })
})
