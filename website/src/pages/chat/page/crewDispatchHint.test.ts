import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import { openPanelView } from '../../../hooks/usePanelTabs'
import { openActivityPanel } from '../../../store/chatSlice'
import { openCrewTab } from './crewDispatchHint'

beforeEach(() => { vi.mocked(openPanelView).mockClear() })

describe('openCrewTab', () => {
  it('puts the Crew tab in front, opens the panel and closes Find', () => {
    const dispatch = vi.fn()
    const closeSearch = vi.fn()
    openCrewTab('chat-1', dispatch, closeSearch)
    expect(openPanelView).toHaveBeenCalledWith('chat-1', 'workers')
    expect(dispatch).toHaveBeenCalledWith(openActivityPanel())
    // Find hides the panel while it is open, so the button would show nothing.
    expect(closeSearch).toHaveBeenCalledTimes(1)
  })

  it('does nothing without a slot', () => {
    const dispatch = vi.fn()
    const closeSearch = vi.fn()
    openCrewTab(null, dispatch, closeSearch)
    expect(openPanelView).not.toHaveBeenCalled()
    expect(dispatch).not.toHaveBeenCalled()
    expect(closeSearch).not.toHaveBeenCalled()
  })
})
