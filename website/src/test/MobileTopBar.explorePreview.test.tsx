/**
 * The phone top bar mounts its two Explore entries (the bar ring with no chat
 * open, and the ⋯ menu row with one open) only while the Explore Feature
 * Preview is on. The entries themselves stand in as markers: what they draw is
 * pinned in components/milestones/milestones.test.tsx.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react'
import type { ComponentProps, ReactNode } from 'react'

vi.mock('../pages/chat/ChatHeaderMenu', () => ({ ChatHeaderMenu: () => <div data-testid="header-menu" /> }))
vi.mock('../pages/chat/SessionTitleControl', () => ({ default: () => <div data-testid="title-editor" /> }))
vi.mock('../components/UpdatePill', () => ({ default: () => null }))
vi.mock('../components/ConcurrencyCutChip', () => ({
  ConcurrencyCutLiveRegion: () => null,
  ConcurrencyCutDot: () => null,
  ConcurrencyCutMenuItem: () => null,
}))
vi.mock('../components/milestones/MilestonesIndicator', () => ({
  default: () => <span data-testid="explore-ring" />,
}))
vi.mock('../components/milestones/MilestonesMenuItem', () => ({
  default: ({ Item }: { Item: (p: { children?: ReactNode }) => ReactNode }) => <Item><span data-testid="explore-row" /></Item>,
}))

import MobileTopBar from '../pages/chat/page/MobileTopBar'
import { PREVIEW_EXPLORE, setPreviewFlag } from '../utils/previewFlags'
import type { ChatSlot } from '../types'

type Props = ComponentProps<typeof MobileTopBar>

let slot: HTMLElement
let trail: HTMLElement

function props(over: Partial<Props> = {}): Props {
  return {
    topbarSlot: slot,
    topbarTrailSlot: trail,
    embedMode: undefined,
    topbarSessionsToggle: <button type="button">toggle</button>,
    activeSlot: 'slot-a',
    splitMode: false,
    splitFeatureEnabled: true,
    title: 'Release notes',
    editingTitle: false,
    setEditingTitleSlot: vi.fn(),
    showActionError: vi.fn(),
    setActionError: vi.fn(),
    currentSlot: { key: 'slot-a', title: 'Release notes' } as ChatSlot,
    sidebarAutoHidden: { current: true },
    openSidebar: vi.fn(),
    effectiveMode: 'normal',
    sidebarOnScreen: false,
    activePoppedOut: false,
    focusActivePopout: vi.fn(),
    openActivePopout: vi.fn(),
    activityOpen: false,
    toggleAct: vi.fn(),
    splitAnchorForActive: null,
    activeIsSplitAnchor: false,
    enterSplit: vi.fn(),
    mobileNewSessionItem: undefined,
    dispatch: vi.fn() as never,
    ...over,
  }
}

async function openMore() {
  const more = await screen.findByTestId('mobile-topbar-more')
  fireEvent.pointerDown(more, { button: 0, ctrlKey: false })
  fireEvent.click(more)
}

beforeEach(() => {
  slot = document.createElement('div')
  trail = document.createElement('div')
  document.body.append(slot, trail)
})
afterEach(() => {
  setPreviewFlag(PREVIEW_EXPLORE, false)
  cleanup()
  slot.remove()
  trail.remove()
})

describe('Explore preview on the phone top bar', () => {
  it('off: no ring with no chat open, and no ⋯ menu row with one open', async () => {
    const { rerender } = render(<MobileTopBar {...props({ activeSlot: null, currentSlot: undefined })} />)
    expect(trail.querySelector('[data-testid="explore-ring"]')).toBeNull()
    rerender(<MobileTopBar {...props()} />)
    await openMore()
    await screen.findByRole('menu')
    expect(screen.queryByTestId('explore-row')).toBeNull()
  })

  it('on: the ring with no chat open, and the ⋯ menu row with one open', async () => {
    setPreviewFlag(PREVIEW_EXPLORE, true)
    const { rerender } = render(<MobileTopBar {...props({ activeSlot: null, currentSlot: undefined })} />)
    expect(trail.querySelector('[data-testid="explore-ring"]')).not.toBeNull()
    rerender(<MobileTopBar {...props()} />)
    await openMore()
    expect(await screen.findByTestId('explore-row')).toBeTruthy()
  })

  it('follows the switch without a remount', () => {
    render(<MobileTopBar {...props({ activeSlot: null, currentSlot: undefined })} />)
    expect(trail.querySelector('[data-testid="explore-ring"]')).toBeNull()
    act(() => { setPreviewFlag(PREVIEW_EXPLORE, true) })
    expect(trail.querySelector('[data-testid="explore-ring"]')).not.toBeNull()
  })
})
