// On a touch device the shared `DropdownMenu` defaults to non-modal, so one
// tap outside an open menu both closes it and reaches the control under the
// finger. On the approval card the menus open over the request, and the
// control beside them is Allow once: a non-modal menu would let one tap approve
// a call whose command was under the menu. So the card's three menus are modal
// on every device, and an outside tap only closes them.
//
// The real Radix menu, not the test mock: the mock ignores `modal`. happy-dom
// does not enforce `pointer-events`, so "blocked" is read from the body style a
// modal Radix menu sets, as DropdownMenu.touchNonModal.test.tsx does.
import { describe, it, expect, vi, afterEach, onTestFinished } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { defaultProps, stateWithApproval } from './approvalCardFixtures'

vi.mock('../api/client', () => {
  class MockApiError extends Error {
    readonly status: number
    constructor(status: number, message: string) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  }
  return {
    api: {
      resolveApproval: vi.fn(() => Promise.resolve({})),
      decideApproval: vi.fn(() => Promise.resolve({})),
      approveChatSlot: vi.fn(() => Promise.resolve({})),
      spawnList: vi.fn(() => Promise.resolve({ agents: [] })),
      sharedCrewCapabilities: vi.fn(() => Promise.resolve(null)),
      sharedCrewMode: vi.fn(() => Promise.resolve(null)),
      sharedCrewPeople: vi.fn(() => Promise.resolve(null)),
      sharedCrewViewer: vi.fn(() => Promise.resolve(null)),
    },
    ApiError: MockApiError,
  }
})

function stubTouch(): void {
  vi.spyOn(window, 'matchMedia').mockImplementation((query: string) => ({
    matches: query === '(pointer: coarse)' || query === '(hover: none)',
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }) as unknown as MediaQueryList)
}

/** A finger tap on the trigger: on touch the menu opens on the tap's `click`. */
async function tapOpen(trigger: HTMLElement): Promise<void> {
  fireEvent.pointerDown(trigger, { pointerType: 'touch', button: 0 })
  fireEvent.pointerUp(trigger, { pointerType: 'touch', button: 0 })
  fireEvent.click(trigger)
  // Radix arms its outside-pointer handling on a timeout after opening.
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

afterEach(() => { vi.restoreAllMocks() })

describe('the approval card\'s menus on a touch device', () => {
  it.each(['Trust', 'Reject'])('an open %s menu blocks the page, so a tap beside it never reaches Allow once', async (name) => {
    stubTouch()
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    await tapOpen(screen.getByRole('button', { name }))
    expect(screen.getByRole('menu')).toBeInTheDocument()
    expect(document.body.style.pointerEvents).toBe('none')
  })

  it('in the real menu, PageDown on the exact-command row pages the command and keeps focus on the row', async () => {
    // Radix takes PageDown to jump to the last row; on a row whose box can
    // still scroll, the box takes it first.
    const sh = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight')
    const ch = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight')
    const box = (el: Element) => (el as HTMLElement).dataset?.testid === 'trust-command-full'
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { configurable: true, get() { return box(this) ? 600 : 0 } })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get() { return box(this) ? 200 : 0 } })
    onTestFinished(() => {
      if (sh) Object.defineProperty(HTMLElement.prototype, 'scrollHeight', sh)
      else delete (HTMLElement.prototype as { scrollHeight?: number }).scrollHeight
      if (ch) Object.defineProperty(HTMLElement.prototype, 'clientHeight', ch)
      else delete (HTMLElement.prototype as { clientHeight?: number }).clientHeight
    })
    stubTouch()
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    await tapOpen(screen.getByRole('button', { name: 'Trust' }))
    const command = screen.getByTestId('trust-command-full')
    const row = command.closest<HTMLElement>('[role="menuitem"]')!
    act(() => { row.focus() })
    fireEvent.keyDown(row, { key: 'PageDown' })
    expect(command.scrollTop).toBeGreaterThan(0)
    expect(document.activeElement).toBe(row)
  })

  it('in the real menu, the Duration track stays on one line and the hour\'s end time is a caption under it', async () => {
    stubTouch()
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    await tapOpen(screen.getByRole('button', { name: 'Trust' }))
    const track = screen.getByRole('group')
    // A segment never drops to a second row, and none grows a second line.
    expect(track.className).toContain('flex-nowrap')
    expect(track.className.split(/\s+/)).not.toContain('flex-wrap')
    const segments = screen.getAllByRole('menuitemradio')
    for (const segment of segments) expect(segment.className).toContain('whitespace-nowrap')
    act(() => { fireEvent.click(segments[0]) })
    const ends = screen.getByTestId('trust-duration-ends')
    expect(track.contains(ends)).toBe(false)
    expect(track.getAttribute('aria-describedby')).toBe(ends.id)
  })

  it('an open Override menu blocks the page too', async () => {
    stubTouch()
    const state = stateWithApproval()
    Object.assign(state.chat!.messages[1].meta!, { deny_override_rule: 'curl .*', deny_override_command: 'ls /tmp', deny_override_reason: 'Blocked by security policy' })
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    await tapOpen(screen.getByTestId('deny-override-trigger'))
    expect(screen.getByRole('menu')).toBeInTheDocument()
    expect(document.body.style.pointerEvents).toBe('none')
  })
})
