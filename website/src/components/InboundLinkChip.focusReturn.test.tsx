// Unlinking from the chip's OWN menu removes the last `both` link, so the chip
// renders `null` and its trigger leaves the tree in the same commit. Radix's
// FocusScope then restores focus to that detached trigger — landing on
// `document.body` — and a keyboard or assistive-technology user loses their
// place. The chip owns Radix's close-focus hook (`onCloseAutoFocus`) to catch
// exactly that case: when the trigger is gone it prevents the default restore
// and focuses a stable header control instead; when the trigger is still there
// (an ordinary close) it does nothing and lets Radix restore normally.
//
// These tests drive the handler the way Radix drives it: happy-dom cannot run a
// real FocusScope, so the mock exposes the `onCloseAutoFocus` prop the chip
// passes and the test fires it, with the trigger connected or detached. The
// ordering guarantee — that Radix calls this at teardown rather than racing a
// React effect — is what a real-browser check covers; see the PR's manual
// verification note.
import { render } from '@testing-library/react'
import { act } from 'react'
import { Provider } from 'react-redux'
import { createTestStore } from '../test/helpers'
import InboundLinkChip from './InboundLinkChip'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot, SessionLink } from '../types'

// happy-dom cannot drive a real Radix menu (no PointerEvent / FocusScope), so
// the menu shell collapses to plain elements AND captures the chip's
// `onCloseAutoFocus` so the test can fire it as Radix's teardown would. The
// content renders inline; the trigger child renders as-is.
let closeAutoFocus: ((event: Event) => void) | undefined
vi.mock('./ui/dropdown-menu', () => {
  const DropdownMenu = ({ children }: { children?: React.ReactNode }) => <>{children}</>
  const DropdownMenuTrigger = ({ children }: { asChild?: boolean; children?: React.ReactNode }) => <>{children}</>
  const DropdownMenuContent = ({ onCloseAutoFocus, children }: {
    onCloseAutoFocus?: (event: Event) => void
    children?: React.ReactNode
  }) => {
    closeAutoFocus = onCloseAutoFocus
    return <div role="menu">{children}</div>
  }
  return { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent }
})

// The focus handler does not depend on the menu rows' real content. Stub them.
vi.mock('./LinkedSurfacesSection', () => ({
  default: () => <button type="button" data-testid="menu-row">Unlink from zzq-chan</button>,
}))

function link(over: Partial<SessionLink> = {}): SessionLink {
  return {
    channel: 'discord', label: 'zzq-chan', target: 'C1', binding: 'b-1',
    direction: 'both', drives_session: true, live: true, paused: true, ...over,
  }
}

function slot(links: SessionLink[]): ChatSlot {
  return { key: 'zzq-slot', messages: 0, running: false, links } as ChatSlot
}

/**
 * The chat header the chip lives in: a `data-chat-header-focus` control (the
 * session-title menu trigger, the chip's focus-return target) beside the chip.
 */
function Header({ store, extra }: { store: ReturnType<typeof createTestStore>; extra?: React.ReactNode }) {
  return (
    <Provider store={store}>
      <button type="button" data-chat-header-focus data-testid="header-focus">zzq title</button>
      <InboundLinkChip slotKey="zzq-slot" />
      {extra}
    </Provider>
  )
}

/** Fire the captured onCloseAutoFocus and report whether the chip prevented it. */
function fireCloseAutoFocus(): { defaultPrevented: boolean } {
  const event = new Event('closeAutoFocus', { cancelable: true })
  closeAutoFocus?.(event)
  return { defaultPrevented: event.defaultPrevented }
}

describe('InboundLinkChip close-focus handling', () => {
  it('on close with the trigger gone (chip emptied), prevents the default and focuses the header', () => {
    const store = createTestStore()
    store.dispatch(sseSlots([slot([link()])]))
    const { container } = render(<Header store={store} />)
    expect(typeof closeAutoFocus).toBe('function')

    // The unlink empties the chip: it renders null, so its trigger leaves the
    // tree. Radix fires onCloseAutoFocus during that teardown — simulate the
    // trigger being gone by removing the chip's link, then fire the handler.
    act(() => { store.dispatch(sseSlots([slot([])])) })
    const { defaultPrevented } = fireCloseAutoFocus()

    expect(defaultPrevented).toBe(true)
    const headerFocus = container.querySelector('[data-testid="header-focus"]')
    expect(document.activeElement).toBe(headerFocus)
  })

  it('on an ordinary close with the trigger still present, does nothing (Radix restores it)', () => {
    const store = createTestStore()
    store.dispatch(sseSlots([slot([link()])]))
    render(<Header store={store} />)

    // The link is intact, so the chip and its trigger are still mounted. The
    // handler must not prevent the default or move focus — Radix returns focus
    // to the trigger as usual.
    const { defaultPrevented } = fireCloseAutoFocus()
    expect(defaultPrevented).toBe(false)
    // Focus was not forced onto the header control.
    expect(document.activeElement).not.toBe(document.querySelector('[data-testid="header-focus"]'))
  })

  it('does not force focus when there is no header control to return to', () => {
    const store = createTestStore()
    store.dispatch(sseSlots([slot([link()])]))
    // No data-chat-header-focus in this tree.
    render(
      <Provider store={store}>
        <InboundLinkChip slotKey="zzq-slot" />
      </Provider>,
    )
    act(() => { store.dispatch(sseSlots([slot([])])) })
    const { defaultPrevented } = fireCloseAutoFocus()
    // Nothing to focus: leave Radix's default alone rather than prevent it with
    // no replacement.
    expect(defaultPrevented).toBe(false)
  })
})
