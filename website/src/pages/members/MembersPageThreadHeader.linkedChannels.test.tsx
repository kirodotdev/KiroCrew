// The Bot thread header mounts the linked-channel chip on the CONFIRMED slot
// only: its rows write, and a roster or cached key can name a session the
// member-thread endpoint refused (#34978).
import { createRef } from 'react'
import { screen } from '@testing-library/react'
import { createTestStore, renderWithProviders } from '../../test/helpers'
import { MembersPageThreadHeader } from './MembersPageThreadHeader'
import { sseSlots } from '../../store/dashboardSlice'
import { api } from '../../api/client'
import type { ChatSlot } from '../../types'

vi.mock('../../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return { ...mod, api: { ...mod.api, channelTargets: vi.fn() } }
})

const SLOT = 'member-mate.memory-member-mate-zzq'
const noop = () => {}

function mount(confirmedSlot: string) {
  const store = createTestStore()
  store.dispatch(sseSlots([{
    key: SLOT, messages: 0, running: false,
    links: [{ channel: 'slack', label: 'Slack', target: 'D1', binding: 'b-1', direction: 'out', drives_session: true, live: true }],
  } as ChatSlot]))
  const active = { name: 'mate', display_name: 'Mate', slot_key: SLOT, avatar: '' }
  const props = {
    layout: { rosterPinned: false, switcherSlotRef: createRef(), setRosterPin: noop, panelVisible: false, showOpener: true },
    profileCard: {
      pillFaceRef: createRef(), openProfile: noop, requestCloseProfile: noop, togglePanel: noop,
      schedGuardRef: { current: () => Promise.resolve(true) }, schedAtStakeRef: { current: () => false },
    },
    drawers: { rosterDrawerOpen: false, openRosterDrawer: noop, rosterDrawerAvailable: false },
    rosterView: { isRunning: () => false, orderedMembers: [active], toggleStar: noop, starPending: new Set() },
    thread: { activeSlot: SLOT, confirmedSlot, entranceFaceHidden: false },
    threadView: {
      threadHeaderRef: createRef(), setSwitcherOpenRequest: noop, switcherOpenRequest: 0, topbarSlot: null,
      headerInBar: false, pillActivity: { kind: 'idle', label: '' },
    },
    rosterSignals: { automationOf: () => null, isLoopOn: () => false, signalsOf: () => ({}) },
    profileFlight: { faceFlight: null },
    navigation: { openMember: noop },
    navigate: noop, location: { state: null }, setSearchParams: noop,
    profile: null, openQuick: noop, createHeld: null, active,
  }
  return renderWithProviders(<MembersPageThreadHeader {...(props as never)} />, { store })
}

describe('MembersPageThreadHeader linked-channel chip', () => {
  beforeEach(() => {
    vi.mocked(api.channelTargets).mockResolvedValue([] as never)
  })

  it('shows the chip for a confirmed Slack-linked Bot slot', () => {
    mount(SLOT)
    expect(screen.getByTestId('member-linked-channels')).toBeInTheDocument()
  })

  it('shows no chip while the thread holds only the roster or cached key', () => {
    mount('')
    expect(screen.getByTestId('member-thread-header')).toBeInTheDocument()
    expect(screen.queryByTestId('member-linked-channels')).toBeNull()
  })
})
