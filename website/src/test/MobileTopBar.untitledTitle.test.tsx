/**
 * The phone top bar names an untitled session "Untitled session", as the
 * desktop title row does, never by its slot key. The chat page hands the bar
 * the key as the title of an untitled session (the backend's `New Session…`
 * placeholder is not a title), so the bar reads it through
 * `sessionDisplayTitle`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import type { ComponentProps, ReactNode } from 'react'
import { i18nT } from '../i18n/t'

vi.mock('../pages/chat/ChatHeaderMenu', () => ({
  ChatHeaderMenu: ({ triggerLabel }: { triggerLabel: ReactNode }) => <div data-testid="header-menu">{triggerLabel}</div>,
}))
vi.mock('../pages/chat/SessionTitleControl', () => ({ default: () => <div data-testid="title-editor" /> }))
vi.mock('../components/UpdatePill', () => ({ default: () => null }))
vi.mock('../components/ConcurrencyCutChip', () => ({
  ConcurrencyCutLiveRegion: () => null,
  ConcurrencyCutDot: () => null,
  ConcurrencyCutMenuItem: () => null,
}))
vi.mock('../components/milestones/MilestonesIndicator', () => ({ default: () => null }))
vi.mock('../components/milestones/MilestonesMenuItem', () => ({ default: () => null }))

import MobileTopBar from '../pages/chat/page/MobileTopBar'
import type { ChatSlot } from '../types'

type Props = ComponentProps<typeof MobileTopBar>

const KEY = 'chat-1-1792348860'
let slot: HTMLElement
let trail: HTMLElement

function props(over: Partial<Props>): Props {
  return {
    topbarSlot: slot,
    topbarTrailSlot: trail,
    embedMode: undefined,
    topbarSessionsToggle: <button type="button">toggle</button>,
    activeSlot: KEY,
    splitMode: false,
    splitFeatureEnabled: true,
    title: KEY,
    editingTitle: false,
    setEditingTitleSlot: vi.fn(),
    showActionError: vi.fn(),
    setActionError: vi.fn(),
    currentSlot: { key: KEY, title: 'New Session…' } as ChatSlot,
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

beforeEach(() => {
  slot = document.createElement('div')
  trail = document.createElement('div')
  document.body.append(slot, trail)
})
afterEach(() => {
  cleanup()
  slot.remove()
  trail.remove()
})

describe('the phone top bar title of an untitled session', () => {
  it.each([KEY, 'New Session…'])('reads "Untitled session" for the title %s', title => {
    render(<MobileTopBar {...props({ title })} />)
    const menu = screen.getByTestId('header-menu')
    expect(menu).toHaveTextContent(i18nT('pages.artifactDetailPage.untitled_session'))
    expect(menu).not.toHaveTextContent(KEY)
  })

  it('reads a real title as it is', () => {
    render(<MobileTopBar {...props({ title: 'Release notes', currentSlot: { key: KEY, title: 'Release notes' } as ChatSlot })} />)
    expect(screen.getByTestId('header-menu')).toHaveTextContent('Release notes')
  })
})
