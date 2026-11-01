import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import CrewDispatchNoticeCard from './CrewDispatchNoticeCard'
import { PREVIEW_CREW_MODE, setPreviewFlag } from '../../utils/previewFlags'
import { createTestStore } from '../../test/helpers'
import { openCrewSession } from './command-center/CrewWorkerList'
import { __resetPanelTabs, panelStripOf } from '../../hooks/usePanelTabs'

describe('CrewDispatchNoticeCard', () => {
  // Crew Mode ships behind a default-off preview; the card renders only when
  // the viewer's browser has that preview ON (see the gate cases below). The
  // reveal/accessibility cases therefore enable it first.
  beforeEach(() => setPreviewFlag(PREVIEW_CREW_MODE, true))
  afterEach(() => {
    __resetPanelTabs()
    vi.clearAllMocks()
    localStorage.removeItem(PREVIEW_CREW_MODE)
  })

  it('renders nothing while the Crew Mode preview is OFF', () => {
    // RED without the useCrewMode() gate: the card would render regardless of
    // the flag, exposing a Crew Mode surface in a browser that has the preview
    // disabled (the decision this change honours).
    setPreviewFlag(PREVIEW_CREW_MODE, false)
    const { container } = render(
      <CrewDispatchNoticeCard slot="conductor-1" onOpenCrewTab={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
    expect(screen.queryByTestId('crew-dispatch-notice-card')).toBeNull()
  })

  it('renders the card while the Crew Mode preview is ON', () => {
    render(<CrewDispatchNoticeCard slot="conductor-1" onOpenCrewTab={vi.fn()} />)
    expect(screen.getByTestId('crew-dispatch-notice-card')).toBeInTheDocument()
    expect(screen.getByTestId('crew-dispatch-notice-open')).toBeInTheDocument()
  })

  it('the Open Board button calls the HOST reveal callback with the card\'s slot', () => {
    // The reveal (close Find, rebind a background pane, select Workers, open the
    // dock) is host-owned; the card only invokes the threaded callback with its
    // OWN slot, so a split-pane notice reveals its pane's Board, not the
    // active chat's.
    const onOpenCrewTab = vi.fn()
    render(<CrewDispatchNoticeCard slot="conductor-1" onOpenCrewTab={onOpenCrewTab} />)
    fireEvent.click(screen.getByTestId('crew-dispatch-notice-open'))
    expect(onOpenCrewTab).toHaveBeenCalledTimes(1)
    expect(onOpenCrewTab).toHaveBeenCalledWith('conductor-1')
  })

  it('the button is inert (disabled) when the host wires no reveal callback', () => {
    render(<CrewDispatchNoticeCard slot="conductor-1" />)
    const button = screen.getByTestId('crew-dispatch-notice-open')
    expect(button).toBeDisabled()
    fireEvent.click(button)
    // Nothing to assert beyond no throw: a disabled button dispatches nothing.
  })

  it('names the Board and its button opens the side panel on the Board tab, not Dashboard', () => {
    // RED on the old copy ("Crew tab" / "Open Crew tab"): the side-panel tab is
    // the Board. The host reveal is the real one (`openCrewSession`), so a click
    // must land on the `workers` view (shown as Board), never `command-center`.
    const store = createTestStore()
    render(
      <CrewDispatchNoticeCard
        slot="conductor-1"
        onOpenCrewTab={slot => openCrewSession(store.dispatch, slot, true)}
      />,
    )
    expect(screen.getByTestId('crew-dispatch-notice-text')).toHaveTextContent(
      "Crew members were started — they're listed on the Board in the side panel, not the session list.",
    )
    expect(screen.getByTestId('crew-dispatch-notice-text')).not.toHaveTextContent(/Crew tab/)
    const button = screen.getByTestId('crew-dispatch-notice-open')
    expect(button).toHaveAccessibleName('Open Board')
    // The Board tab's own glyph (lucide Zap, `SidePanelTabIcons` kind `workers`).
    expect(button.querySelector('svg.lucide-zap')).not.toBeNull()
    fireEvent.click(button)
    expect(panelStripOf('conductor-1').activeId).toBe('workers')
    expect(panelStripOf('conductor-1').ids).not.toContain('command-center')
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('the button takes its accessible name from the visible label (no redundant title/aria-label)', () => {
    render(<CrewDispatchNoticeCard slot="conductor-1" onOpenCrewTab={vi.fn()} />)
    const button = screen.getByTestId('crew-dispatch-notice-open')
    expect(button).toHaveAccessibleName('Open Board')
    expect(button).not.toHaveAttribute('aria-label')
    expect(button).not.toHaveAttribute('title')
  })
})
