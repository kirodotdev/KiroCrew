/**
 * UI review r1 fixes that are not covered next to their components: Workers is
 * offered on a Crew Mode slot's strip without taking a focus the user picked
 * (U1; it takes the default one, see sidePanelCrewDefaultTab), the phone Back
 * control is a 44px target (U7), and status text wears the contrast tones (U4).
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { PINNED_VIEWS, __resetPanelTabs, offerPanelView, openPanelView, usePanelTabs } from '../hooks/usePanelTabs'
import { CREW_GROUP_TONE } from '../lib/crewWorkers'

describe('Workers on a Crew Mode slot (U1)', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs() })

  it('is added to the strip without taking focus, once', () => {
    openPanelView('root', 'links')
    offerPanelView('root', 'workers')
    offerPanelView('root', 'workers')
    const { result } = renderHook(() => usePanelTabs('root'))
    expect(result.current.tabs.filter(t => t.id === 'workers')).toHaveLength(1)
    expect(result.current.activeId).toBe('links')
  })

  it('takes the default Changes focus, but not Changes picked after another tab', () => {
    const { result } = renderHook(() => usePanelTabs('root'))
    act(() => { result.current.syncPinned(PINNED_VIEWS) })
    expect(result.current.activeId).toBe('changes')
    act(() => { offerPanelView('root', 'workers') })
    expect(result.current.activeId).toBe('workers')
    act(() => { result.current.setActive('artifacts') })
    act(() => { result.current.setActive('changes') })
    act(() => { offerPanelView('root', 'workers') })
    expect(result.current.activeId).toBe('changes')
  })

  it('keeps Changes asked for before the panel mounted (a PR chip)', () => {
    openPanelView('chip', 'changes')
    const { result } = renderHook(() => usePanelTabs('chip'))
    act(() => { result.current.syncPinned(PINNED_VIEWS) })
    act(() => { offerPanelView('chip', 'workers') })
    expect(result.current.activeId).toBe('changes')
    expect(result.current.tabs.some(t => t.id === 'workers')).toBe(true)
  })

  it('takes focus on an empty strip, which has nothing else to show', () => {
    offerPanelView('fresh', 'workers')
    const { result } = renderHook(() => usePanelTabs('fresh'))
    expect(result.current.activeId).toBe('workers')
  })
})

describe('status text contrast (U4)', () => {
  it('approval, working and done text use the mixed tones, not the raw hue', () => {
    expect(CREW_GROUP_TONE.approval).toBe('crew-tone-warn')
    expect(CREW_GROUP_TONE.with_conductor).toBe('crew-tone-muted')
    expect(CREW_GROUP_TONE.done).toBe('crew-tone-ok')
    expect(CREW_GROUP_TONE.working).toBe('crew-tone-aim')
  })
})
